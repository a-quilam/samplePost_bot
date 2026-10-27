# template_store.py
"""
Независимый от Telegram доменный слой шаблонов анонсов Stage 23.

Сервис отвечает за:

- стандартный шаблон места и его атомарное создание вместе с местом;
- идемпотентный backfill существующих мест;
- личные шаблоны, изолированные по владельцу;
- три независимые секции: header/body/footer;
- неизменяемые версии и оптимистичную блокировку;
- явные связи версии/снимка с постоянными `bricks.id`;
- самостоятельные неизменяемые снимки для будущих черновиков;
- проверку прав и переносимости привязок.

Границы этапа:

- произвольный `[код]` здесь НЕ разбирается и НЕ превращается в brick_id;
- синтаксис ссылок и подстановка значений — Stage 24;
- Telegram-редактор — Stage 25;
- подключение снимков к мастеру постов — Stage 27.

Все пользовательские тексты сохраняются как строки без trim/HTML-нормализации.
Для изменения используется условный UPDATE по revision; при конфликте более
новая версия не перезаписывается.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_STATUS_ACTIVE,
)
from models import (
    AnnouncementDraft,
    Brick,
    Place,
    Template,
    TemplateSnapshot,
    TemplateSnapshotBrick,
    TemplateVersion,
    TemplateVersionBrick,
    User,
    utcnow,
)
from permissions import can_manage_place
from template_types import (
    BUILTIN_TEMPLATE,
    BUILTIN_TEMPLATE_VERSION,
    INITIAL_PERSONAL_TEMPLATE_NAME,
    STANDARD_TEMPLATE_DISPLAY_NAME,
    TEMPLATE_KIND_PERSONAL,
    TEMPLATE_KIND_STANDARD,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
    BuiltinTemplateDefinition,
    validate_template_name,
    validate_template_section_name,
    validate_template_sections,
)

if TYPE_CHECKING:
    from brick_store import BrickDefinition
    from template_compiler import TemplateCompileError

_UNSET = object()

# Ключ идемпотентности снимка-метки стартового личного шаблона (Poster 2.0, S2).
# Снимок создаётся один раз при выдаче и переживает удаление строки `templates`,
# поэтому `has_personal_template_history` помнит о выдаче даже после того, как
# пользователь осознанно удалил шаблон.
INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY = "poster2.initial-personal-template"


class TemplateError(ValueError):
    """Базовая доменная ошибка шаблонов."""


class TemplatePermissionError(PermissionError):
    """Операция запрещена ролью вызывающего."""


class TemplateActorError(RuntimeError):
    """Telegram-пользователь ещё не зарегистрирован в `users`."""


class TemplateNotFoundError(TemplateError):
    """Шаблон/версия/снимок/место не найдены или невидимы вызывающему."""


class TemplateConflictError(TemplateError):
    """Конфликт ревизии, идемпотентности или одновременной записи."""


class TemplateConfirmationRequired(TemplateError):
    """Сброс стандарта требует явного подтверждения."""


@dataclass(frozen=True)
class TemplateBrickBinding:
    """Текущее состояние одного явно привязанного brick_id."""

    brick_id: int
    code: str
    title: str
    scope: str
    status: str

    @property
    def is_active(self) -> bool:
        return self.status == BRICK_STATUS_ACTIVE


@dataclass(frozen=True)
class TemplateVersionDetails:
    """Неизменяемое содержимое одной версии шаблона."""

    version_id: int
    template_id: int
    version: int
    header: str
    body: str
    footer: str
    created_by_user_id: int | None
    created_at: datetime
    builtin_version: int | None
    bindings: tuple[TemplateBrickBinding, ...] = field(default_factory=tuple)

    @property
    def brick_ids(self) -> tuple[int, ...]:
        return tuple(binding.brick_id for binding in self.bindings)


@dataclass(frozen=True)
class TemplateDetails:
    """Снимок шаблона и его текущей версии для сервисного API."""

    template_id: int
    kind: str
    place_id: int | None
    owner_user_id: int | None
    name: str | None
    revision: int
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime
    current_version: TemplateVersionDetails

    @property
    def is_standard(self) -> bool:
        return self.kind == TEMPLATE_KIND_STANDARD

    @property
    def display_name(self) -> str:
        if self.is_standard:
            return STANDARD_TEMPLATE_DISPLAY_NAME
        return self.name or "Личный шаблон"


@dataclass(frozen=True)
class TemplateSnapshotDetails:
    """Самостоятельный снимок, не зависящий от текущей версии шаблона."""

    snapshot_id: int
    owner_user_id: int
    source_template_id: int
    source_template_version: int
    source_kind: str
    source_name: str | None
    source_place_id: int | None
    source_owner_user_id: int | None
    header: str
    body: str
    footer: str
    builtin_version: int | None
    created_at: datetime
    bindings: tuple[TemplateBrickBinding, ...] = field(default_factory=tuple)

    @property
    def brick_ids(self) -> tuple[int, ...]:
        return tuple(binding.brick_id for binding in self.bindings)


@dataclass(frozen=True)
class TemplateCompatibility:
    """Результат проверки привязок и переносимости без записи в БД."""

    template_id: int
    version: int
    kind: str
    place_id: int | None
    compatible: bool
    issues: tuple[str, ...]
    bindings: tuple[TemplateBrickBinding, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TemplateMutationResult:
    """Понятный результат изменения шаблона."""

    status: str
    template: TemplateDetails | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"updated", "unchanged"}


def _valid_positive_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _actor_user(session: Session, telegram_user_id: int | None) -> User | None:
    if not _valid_positive_id(telegram_user_id):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _require_actor_user(session: Session, telegram_user_id: int | None) -> User:
    actor = _actor_user(session, telegram_user_id)
    if actor is None:
        raise TemplateActorError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _require_place(session: Session, place_id: int) -> Place:
    if not _valid_positive_id(place_id):
        raise TemplateNotFoundError("Некорректный идентификатор места.")
    place = session.get(Place, place_id)
    if place is None:
        raise TemplateNotFoundError("Место не найдено.")
    return place


def _load_template_row(
    session: Session, template_id: int, *, refresh: bool = False
) -> Template | None:
    if not _valid_positive_id(template_id):
        return None
    if refresh:
        return session.scalar(
            select(Template)
            .where(Template.id == template_id)
            .execution_options(populate_existing=True)
        )
    return session.get(Template, template_id)


def _readable_template_row(session: Session, actor: User, template_id: int) -> Template:
    row = _load_template_row(session, template_id)
    if row is None:
        raise TemplateNotFoundError("Шаблон не найден.")
    if row.kind == TEMPLATE_KIND_PERSONAL and row.owner_user_id != actor.id:
        # Чужой личный шаблон неотличим от несуществующего.
        raise TemplateNotFoundError("Шаблон не найден.")
    return row


def _can_edit_template(session: Session, actor: User, row: Template) -> bool:
    if row.kind == TEMPLATE_KIND_STANDARD:
        return row.place_id is not None and can_manage_place(
            session, actor.telegram_user_id, row.place_id
        )
    return row.owner_user_id == actor.id


def _still_authorized(
    session: Session, actor_id: int, telegram_user_id: int, row: Template
) -> bool:
    """Повторная проверка права записи после получения блокировки."""
    actor = session.get(User, actor_id)
    if actor is None or actor.telegram_user_id != telegram_user_id:
        return False
    if row.kind == TEMPLATE_KIND_STANDARD:
        return row.place_id is not None and can_manage_place(
            session, telegram_user_id, row.place_id
        )
    return row.owner_user_id == actor_id


def _still_readable(
    session: Session, actor_id: int, telegram_user_id: int, row: Template
) -> bool:
    """Повторная проверка права чтения перед коммитом снимка."""
    actor = session.get(User, actor_id)
    if actor is None or actor.telegram_user_id != telegram_user_id:
        return False
    if row.kind == TEMPLATE_KIND_STANDARD:
        return True
    return row.owner_user_id == actor_id


def _version_row(
    session: Session, template: Template, version: int | None = None
) -> TemplateVersion:
    number = template.revision if version is None else version
    row = session.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id,
            TemplateVersion.version == number,
        )
    )
    if row is None:
        raise TemplateError(
            f"У шаблона {template.id} отсутствует заявленная версия {number}."
        )
    return row


def _binding_views_for_version(
    session: Session, version_id: int
) -> tuple[TemplateBrickBinding, ...]:
    """Привязки версии: код берётся из binding-строки, а не из текущего Brick."""
    rows = session.execute(
        select(
            TemplateVersionBrick.brick_id,
            TemplateVersionBrick.code,
            Brick.title,
            Brick.scope,
            Brick.status,
        )
        .join(Brick, Brick.id == TemplateVersionBrick.brick_id)
        .where(TemplateVersionBrick.template_version_id == version_id)
        .order_by(TemplateVersionBrick.brick_id)
    ).all()
    return tuple(
        TemplateBrickBinding(
            brick_id=row.brick_id,
            code=row.code or "",
            title=row.title,
            scope=row.scope,
            status=row.status,
        )
        for row in rows
    )


def _binding_views_for_snapshot(
    session: Session, snapshot_id: int
) -> tuple[TemplateBrickBinding, ...]:
    """Привязки снимка: код берётся из binding-строки, а не из текущего Brick."""
    rows = session.execute(
        select(
            TemplateSnapshotBrick.brick_id,
            TemplateSnapshotBrick.code,
            Brick.title,
            Brick.scope,
            Brick.status,
        )
        .join(Brick, Brick.id == TemplateSnapshotBrick.brick_id)
        .where(TemplateSnapshotBrick.template_snapshot_id == snapshot_id)
        .order_by(TemplateSnapshotBrick.brick_id)
    ).all()
    return tuple(
        TemplateBrickBinding(
            brick_id=row.brick_id,
            code=row.code or "",
            title=row.title,
            scope=row.scope,
            status=row.status,
        )
        for row in rows
    )


def _version_details(session: Session, row: TemplateVersion) -> TemplateVersionDetails:
    return TemplateVersionDetails(
        version_id=row.id,
        template_id=row.template_id,
        version=row.version,
        header=row.header,
        body=row.body,
        footer=row.footer,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        builtin_version=row.builtin_version,
        bindings=_binding_views_for_version(session, row.id),
    )


def _template_details(session: Session, row: Template) -> TemplateDetails:
    return TemplateDetails(
        template_id=row.id,
        kind=row.kind,
        place_id=row.place_id,
        owner_user_id=row.owner_user_id,
        name=row.name,
        revision=row.revision,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        current_version=_version_details(session, _version_row(session, row)),
    )


def _template_details_by_id(session: Session, template_id: int) -> TemplateDetails:
    row = _load_template_row(session, template_id, refresh=True)
    if row is None:
        raise TemplateNotFoundError("Шаблон не найден.")
    return _template_details(session, row)


def _snapshot_details(
    session: Session, row: TemplateSnapshot
) -> TemplateSnapshotDetails:
    return TemplateSnapshotDetails(
        snapshot_id=row.id,
        owner_user_id=row.owner_user_id,
        source_template_id=row.source_template_id,
        source_template_version=row.source_template_version,
        source_kind=row.source_kind,
        source_name=row.source_name,
        source_place_id=row.source_place_id,
        source_owner_user_id=row.source_owner_user_id,
        header=row.header,
        body=row.body,
        footer=row.footer,
        builtin_version=row.builtin_version,
        created_at=row.created_at,
        bindings=_binding_views_for_snapshot(session, row.id),
    )


def _normalize_brick_ids(brick_ids: Iterable[int] | None) -> tuple[int, ...]:
    if brick_ids is None:
        return ()
    if isinstance(brick_ids, (str, bytes)):
        raise TemplateError("Привязки кирпичиков должны быть перечислением ID.")
    normalized: set[int] = set()
    for brick_id in brick_ids:
        if not _valid_positive_id(brick_id):
            raise TemplateError("brick_id должен быть положительным целым числом.")
        normalized.add(int(brick_id))
    return tuple(sorted(normalized))


def _validate_binding_ids(
    session: Session,
    *,
    kind: str,
    place_id: int | None,
    owner_user_id: int | None,
    brick_ids: Iterable[int] | None,
) -> tuple[int, ...]:
    """
    Проверяет явные привязки для новой версии/снимка.

    Стандарт: системные + кирпичики СВОЕГО места.
    Личный: системные + личные кирпичики СВОЕГО владельца.
    Чужие приватные, кирпичики другого места и архивные определения
    запрещены для новых связей.
    """
    ids = _normalize_brick_ids(brick_ids)
    if not ids:
        return ()
    rows = session.scalars(select(Brick).where(Brick.id.in_(ids))).all()
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(ids):
        raise TemplateError("Один или несколько brick_id не существуют.")
    for brick_id in ids:
        row = by_id[brick_id]
        if row.status != BRICK_STATUS_ACTIVE:
            raise TemplateError(
                f"Кирпичик #{brick_id} архивирован и не может быть добавлен "
                "в новую версию."
            )
        if row.scope == BRICK_SCOPE_SYSTEM:
            continue
        if kind == TEMPLATE_KIND_STANDARD:
            if row.scope == BRICK_SCOPE_PLACE and row.place_id == place_id:
                continue
            raise TemplateError(
                "Стандартный шаблон может ссылаться только на системные "
                "кирпичики и кирпичики своего места."
            )
        if (
            kind == TEMPLATE_KIND_PERSONAL
            and row.scope == BRICK_SCOPE_PERSONAL
            and row.owner_user_id == owner_user_id
        ):
            continue
        raise TemplateError(
            "Личный шаблон может ссылаться только на системные кирпичики "
            "и личные кирпичики своего владельца."
        )
    return ids


def _insert_bindings(
    session: Session,
    *,
    template_version_id: int,
    bindings: Sequence[tuple[str, int]],
) -> None:
    """Сохраняет пары «код → brick_id» для конкретной версии шаблона."""
    for code, brick_id in bindings:
        session.add(
            TemplateVersionBrick(
                template_version_id=template_version_id,
                brick_id=brick_id,
                code=code,
            )
        )


def _insert_snapshot_bindings(
    session: Session,
    *,
    snapshot_id: int,
    bindings: Sequence[tuple[str, int]],
) -> None:
    """Сохраняет пары «код → brick_id» внутри самостоятельного снимка."""
    for code, brick_id in bindings:
        session.add(
            TemplateSnapshotBrick(
                template_snapshot_id=snapshot_id,
                brick_id=brick_id,
                code=code,
            )
        )


# --- Встроенный стандарт ----------------------------------------------------


def _builtin_binding_pairs(
    session: Session, place_id: int | None
) -> tuple[tuple[str, int], ...]:
    """
    Разбирает встроенный стандарт и возвращает пары «код → brick_id».

    Встроенный текст ссылается только на системные кирпичики Stage 22A
    с известными стабильными ID; компилятор проверяет их доступность.
    """
    from template_compiler import resolve_template_bindings  # ленивый импорт

    compiled = resolve_template_bindings(
        session,
        {
            TEMPLATE_SECTION_HEADER: BUILTIN_TEMPLATE.header,
            TEMPLATE_SECTION_BODY: BUILTIN_TEMPLATE.body,
            TEMPLATE_SECTION_FOOTER: BUILTIN_TEMPLATE.footer,
        },
        actor_telegram_user_id=None,
        place_id=place_id,
    )
    if not compiled.ok:
        raise TemplateCompileError(
            "Встроенный шаблон ссылается на недоступные кирпичики: "
            + "; ".join(f"[код={c}]" for c in compiled.unknown_codes)
        )
    return tuple(sorted(compiled.resolved_brick_ids.items()))


def get_builtin_template() -> BuiltinTemplateDefinition:
    """Возвращает контролируемый встроенный стандарт Poster без записи в БД."""
    return BUILTIN_TEMPLATE


# --- Стандартные шаблоны мест -----------------------------------------------


def _insert_standard_template(
    session: Session,
    place_id: int,
    *,
    created_by_user_id: int | None,
    now: datetime,
) -> Template:
    """
    Транзакционный примитив: место уже существует, стандарта ещё нет.

    Вызывается внутри транзакции создания места или backfill. Не коммитит.
    При наличии стандарта возвращает существующую строку без перезаписи.
    """
    existing = session.scalar(
        select(Template).where(
            Template.kind == TEMPLATE_KIND_STANDARD,
            Template.place_id == place_id,
        )
    )
    if existing is not None:
        return existing

    binding_pairs = _builtin_binding_pairs(session, place_id)
    template = Template(
        kind=TEMPLATE_KIND_STANDARD,
        place_id=place_id,
        owner_user_id=None,
        name=None,
        revision=1,
        created_by_user_id=created_by_user_id,
        created_at=now,
        updated_at=now,
    )
    session.add(template)
    session.flush()
    version = TemplateVersion(
        template_id=template.id,
        version=1,
        header=BUILTIN_TEMPLATE.header,
        body=BUILTIN_TEMPLATE.body,
        footer=BUILTIN_TEMPLATE.footer,
        created_by_user_id=None,
        created_at=now,
        builtin_version=BUILTIN_TEMPLATE.version,
    )
    session.add(version)
    session.flush()
    _insert_bindings(
        session,
        template_version_id=version.id,
        bindings=binding_pairs,
    )
    session.flush()
    return template


def create_standard_template_for_place(
    session: Session,
    place_id: int,
    *,
    created_by_user_id: int | None = None,
    now: datetime | None = None,
    commit: bool = False,
) -> TemplateDetails:
    """
    Гарантирует стандартный шаблон места в текущей транзакции.

    `commit=False` используется `place_store.create_place`, чтобы место,
    название и стандартный шаблон фиксировались одной транзакцией.
    Повторный вызов для того же места не создаёт дубль.
    """
    _require_place(session, place_id)
    row = _insert_standard_template(
        session,
        place_id,
        created_by_user_id=created_by_user_id,
        now=now or utcnow(),
    )
    if commit:
        try:
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise TemplateConflictError(
                "Не удалось создать стандартный шаблон из-за одновременной "
                "записи. Повторите попытку."
            ) from exc
        except Exception:
            session.rollback()
            raise
    return _template_details(session, _load_template_row(session, row.id) or row)


def ensure_standard_template(
    session: Session,
    actor_telegram_user_id: int | None,
    place_id: int,
) -> TemplateDetails:
    """
    Идемпотентно создаёт отсутствующий стандарт (ремонтная операция).

    Требует действующего администратора места или суперадмина. В нормальном
    потоке шаблон уже создан вместе с местом; повторный вызов возвращает его.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    _require_place(session, place_id)
    if not can_manage_place(session, actor.telegram_user_id, place_id):
        raise TemplatePermissionError(
            "Создавать стандартный шаблон может только администратор места "
            "или суперадминистратор."
        )
    existing = session.scalar(
        select(Template).where(
            Template.kind == TEMPLATE_KIND_STANDARD,
            Template.place_id == place_id,
        )
    )
    if existing is None:
        try:
            with session.begin_nested():
                _insert_standard_template(
                    session,
                    place_id,
                    created_by_user_id=actor.id,
                    now=utcnow(),
                )
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            existing = session.scalar(
                select(Template).where(
                    Template.kind == TEMPLATE_KIND_STANDARD,
                    Template.place_id == place_id,
                )
            )
            if existing is None:
                raise TemplateConflictError(
                    "Не удалось создать стандартный шаблон из-за "
                    "одновременной записи. Повторите попытку."
                ) from exc
        except Exception:
            session.rollback()
            raise
    row = _load_template_row(session, existing.id, refresh=True) if existing else None
    if row is None:
        row = session.scalar(
            select(Template).where(
                Template.kind == TEMPLATE_KIND_STANDARD,
                Template.place_id == place_id,
            )
        )
    if row is None:
        raise TemplateConflictError("Стандартный шаблон не создан. Повторите попытку.")
    return _template_details(session, row)


def backfill_standard_templates(session: Session, *, commit: bool = True) -> int:
    """
    Создаёт стандарты для мест, у которых их почему-либо нет.

    Не перезаписывает существующие шаблоны и не меняет данные мест. Возвращает
    число созданных шаблонов. Повторный запуск даёт 0.
    """
    places = session.scalars(select(Place.id).order_by(Place.id)).all()
    now = utcnow()
    created = 0
    for place_id in places:
        existing = session.scalar(
            select(Template.id).where(
                Template.kind == TEMPLATE_KIND_STANDARD,
                Template.place_id == place_id,
            )
        )
        if existing is not None:
            continue
        _insert_standard_template(
            session,
            int(place_id),
            created_by_user_id=None,
            now=now,
        )
        created += 1
    if commit:
        try:
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            raise TemplateConflictError(
                "Backfill стандартных шаблонов не завершён из-за "
                "одновременной записи. Повторите попытку."
            ) from exc
        except Exception:
            session.rollback()
            raise
    return created


def get_standard_template(
    session: Session,
    actor_telegram_user_id: int | None,
    place_id: int,
) -> TemplateDetails:
    """
    Стандарт места доступен любому зарегистрированному пользователю Poster.

    Создатель места не получает права редактирования автоматически: проверка
    записи выполняется отдельно в `update_template_section`.
    """
    _require_actor_user(session, actor_telegram_user_id)
    _require_place(session, place_id)
    row = session.scalar(
        select(Template).where(
            Template.kind == TEMPLATE_KIND_STANDARD,
            Template.place_id == place_id,
        )
    )
    if row is None:
        raise TemplateNotFoundError("У места нет стандартного шаблона.")
    return _template_details(session, row)


# --- Личные шаблоны ---------------------------------------------------------


def list_personal_templates(
    session: Session,
    actor_telegram_user_id: int | None,
) -> list[TemplateDetails]:
    """Личные шаблоны только этого владельца, включая текущие секции."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    rows = session.scalars(
        select(Template)
        .where(
            Template.kind == TEMPLATE_KIND_PERSONAL,
            Template.owner_user_id == actor.id,
        )
        .order_by(Template.created_at, Template.id)
    ).all()
    return [_template_details(session, row) for row in rows]


def create_personal_template(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    name: object,
    header: object,
    body: object,
    footer: object,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateDetails:
    """
    Создаёт личный шаблон действующего пользователя.

    Владелец всегда берётся из Telegram user ID → `users.id`; клиент не может
    передать чужой owner_id. Названия могут повторяться, в том числе у одного
    владельца. Привязки кирпичиков всегда выводятся из реального текста
    секций компилятором Stage 24; произвольный список ID невозможен.

    `chosen_brick_ids` — явный выбор для неоднозначных кодов.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    checked_name = validate_template_name(name)
    checked_header, checked_body, checked_footer = validate_template_sections(
        header, body, footer
    )
    from template_compiler import TemplateCompileError, resolve_template_bindings

    compiled = resolve_template_bindings(
        session,
        {
            TEMPLATE_SECTION_HEADER: checked_header,
            TEMPLATE_SECTION_BODY: checked_body,
            TEMPLATE_SECTION_FOOTER: checked_footer,
        },
        actor_telegram_user_id=actor.telegram_user_id,
        place_id=None,
        chosen_brick_ids=chosen_brick_ids,
    )
    if not compiled.ok:
        issues: list[str] = []
        for err in compiled.parse_errors:
            issues.append(str(err))
        for code in compiled.unknown_codes:
            issues.append(f"Неизвестный кирпичик [код={code}].")
        for amb in compiled.ambiguous_codes:
            candidates = ", ".join(f"#{c.brick_id} ({c.scope})" for c in amb.candidates)
            issues.append(
                f"Код [код={amb.code}] неоднозначен (секция {amb.section}); "
                f"кандидаты: {candidates}. Требуется явный выбор."
            )
        raise TemplateCompileError("; ".join(issues))
    checked_bindings: tuple[tuple[str, int], ...] = tuple(
        sorted(compiled.resolved_brick_ids.items())
    )
    now = utcnow()
    template = Template(
        kind=TEMPLATE_KIND_PERSONAL,
        place_id=None,
        owner_user_id=actor.id,
        name=checked_name,
        revision=1,
        created_by_user_id=actor.id,
        created_at=now,
        updated_at=now,
    )
    session.add(template)
    try:
        session.flush()
        version = TemplateVersion(
            template_id=template.id,
            version=1,
            header=checked_header,
            body=checked_body,
            footer=checked_footer,
            created_by_user_id=actor.id,
            created_at=now,
            builtin_version=None,
        )
        session.add(version)
        session.flush()
        _insert_bindings(
            session,
            template_version_id=version.id,
            bindings=checked_bindings,
        )
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise TemplateConflictError(
            "Не удалось создать личный шаблон из-за конфликта или "
            "одновременной записи. Повторите попытку."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _template_details_by_id(session, template.id)


def get_accessible_template(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
) -> TemplateDetails | None:
    """Шаблон, доступный на чтение именно этому пользователю, либо None."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    try:
        row = _readable_template_row(session, actor, template_id)
    except TemplateNotFoundError:
        return None
    return _template_details(session, row)


def get_personal_template(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
) -> TemplateDetails | None:
    """Личный шаблон только его владельца; чужой ID даёт None."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _load_template_row(session, template_id)
    if (
        row is None
        or row.kind != TEMPLATE_KIND_PERSONAL
        or row.owner_user_id != actor.id
    ):
        return None
    return _template_details(session, row)


# --- История версий ---------------------------------------------------------


def get_template_version(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
    version: int,
) -> TemplateVersionDetails | None:
    """
    Историческая версия с проверкой доступа к самому шаблону.

    Перебор версий чужого личного шаблона не раскрывает ни текстов, ни
    привязок: сначала проверяется владелец/читаемость шаблона.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    if not _valid_positive_id(version):
        return None
    try:
        template = _readable_template_row(session, actor, template_id)
    except TemplateNotFoundError:
        return None
    row = session.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id,
            TemplateVersion.version == version,
        )
    )
    return _version_details(session, row) if row is not None else None


def list_template_versions(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
) -> list[TemplateVersionDetails]:
    """Вся история доступного шаблона в порядке возрастания версии."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    try:
        template = _readable_template_row(session, actor, template_id)
    except TemplateNotFoundError:
        return []
    rows = session.scalars(
        select(TemplateVersion)
        .where(TemplateVersion.template_id == template.id)
        .order_by(TemplateVersion.version)
    ).all()
    return [_version_details(session, row) for row in rows]


def list_template_brick_ids(
    session: Session,
    actor_telegram_user_id: int | None,
    template_id: int,
    *,
    version: int | None = None,
) -> tuple[int, ...]:
    """Явные brick_id доступной версии (по умолчанию текущей)."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    template = _readable_template_row(session, actor, template_id)
    details = _version_details(session, _version_row(session, template, version))
    return details.brick_ids


# --- Изменение секций с оптимистичной блокировкой ---------------------------


def _mutation(
    status: str,
    template: TemplateDetails | None,
    message: str,
) -> TemplateMutationResult:
    return TemplateMutationResult(status=status, template=template, message=message)


def _reload_after_failure(
    session: Session, template_id: int
) -> tuple[TemplateDetails | None, int | None]:
    session.rollback()
    row = _load_template_row(session, template_id, refresh=True)
    if row is None:
        return None, None
    return _template_details(session, row), row.revision


def _commit_new_version(
    session: Session,
    *,
    row: Template,
    actor: User,
    expected_revision: int,
    header: str,
    body: str,
    footer: str,
    bindings: Sequence[tuple[str, int]],
    builtin_version: int | None,
) -> TemplateMutationResult:
    """
    Атомарно поднимает revision и добавляет версию с проверенными привязками.

    Условный UPDATE первым получает блокировку записи SQLite. Повторная
    проверка прав идёт после него и до коммита. Любая ошибка откатывает
    revision, версию и привязки вместе.
    """
    now = utcnow()
    statement = (
        sqlalchemy_update(Template)
        .where(
            Template.id == row.id,
            Template.revision == expected_revision,
        )
        .values(revision=expected_revision + 1, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    try:
        result = session.execute(statement)
        if getattr(result, "rowcount", 0) != 1:
            session.rollback()
            current = _load_template_row(session, row.id, refresh=True)
            return _mutation(
                "stale",
                _template_details(session, current) if current is not None else None,
                "Шаблон уже изменился в другом окне. Откройте его заново.",
            )

        if not _still_authorized(session, actor.id, actor.telegram_user_id, row):
            session.rollback()
            current = _load_template_row(session, row.id, refresh=True)
            status = "not_found" if current is None else "forbidden"
            return _mutation(
                status,
                _template_details(session, current) if current is not None else None,
                "Права на изменение шаблона больше не действуют.",
            )

        version = TemplateVersion(
            template_id=row.id,
            version=expected_revision + 1,
            header=header,
            body=body,
            footer=footer,
            created_by_user_id=actor.id,
            created_at=now,
            builtin_version=builtin_version,
        )
        session.add(version)
        session.flush()
        _insert_bindings(
            session,
            template_version_id=version.id,
            bindings=bindings,
        )
        session.commit()
    except (IntegrityError, OperationalError):
        current_details, current_revision = _reload_after_failure(session, row.id)
        if current_revision is not None and current_revision != expected_revision:
            return _mutation(
                "stale",
                current_details,
                "Шаблон уже изменился в другом окне. Откройте его заново.",
            )
        return _mutation(
            "conflict",
            current_details,
            "Не удалось сохранить версию из-за одновременной записи. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise

    current = _load_template_row(session, row.id, refresh=True)
    if current is None:
        raise TemplateError("Шаблон исчез после сохранения версии.")
    return _mutation(
        "updated",
        _template_details(session, current),
        "Изменения сохранены новой версией.",
    )


def update_template_section(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    section: object,
    content: object,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateMutationResult:
    """
    Меняет одну секцию, сохраняя две другие и историю.

    Реальное содержимое ВСЕХ трёх секций разбирается компилятором Stage 24:
    синтаксис, доступность кирпичиков и неоднозначность проверяются до
    создания новой версии. При ошибке версия не создаётся.

    - `chosen_brick_ids=None` — привязки всегда выводятся из текста секций;
    - передан отображение «код → brick_id» — используется ТОЛЬКО для
      неоднозначных кодов; для однозначных кодов выбор вызывающего кода
      игнорируется, так что обход проверки произвольным списком невозможен.
    - то же содержимое и те же привязки не создают новую версию.

    Стандарт может менять только действующий админ места или суперадмин.
    Создатель места, обычный пользователь и чужая админка получают отказ.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    section_name = validate_template_section_name(section)
    if not isinstance(content, str):
        raise TemplateError("Содержимое секции должно быть строкой.")
    if not _valid_positive_id(expected_revision):
        raise TemplateError("expected_revision должен быть положительным числом.")

    row = _load_template_row(session, template_id)
    if row is None:
        return _mutation("not_found", None, "Шаблон не найден.")
    if row.kind == TEMPLATE_KIND_PERSONAL and row.owner_user_id != actor.id:
        return _mutation(
            "not_found",
            None,
            "Шаблон не найден или принадлежит другому пользователю.",
        )
    if not _can_edit_template(session, actor, row):
        return _mutation(
            "forbidden",
            _template_details(session, row),
            (
                "Изменять этот шаблон может только администратор места "
                "или суперадминистратор."
                if row.kind == TEMPLATE_KIND_STANDARD
                else "Изменять личный шаблон может только его владелец."
            ),
        )
    if row.revision != expected_revision:
        return _mutation(
            "stale",
            _template_details(session, row),
            "Шаблон уже изменился в другом окне. Откройте его заново.",
        )

    current = _version_details(session, _version_row(session, row))
    sections = {
        TEMPLATE_SECTION_HEADER: current.header,
        TEMPLATE_SECTION_BODY: current.body,
        TEMPLATE_SECTION_FOOTER: current.footer,
    }
    sections[section_name] = content
    checked_header, checked_body, checked_footer = validate_template_sections(
        sections[TEMPLATE_SECTION_HEADER],
        sections[TEMPLATE_SECTION_BODY],
        sections[TEMPLATE_SECTION_FOOTER],
    )

    # --- Stage 24: разбор и привязка реального содержимого всех секций ---
    from template_compiler import TemplateCompileError, resolve_template_bindings

    try:
        compiled = resolve_template_bindings(
            session,
            {
                TEMPLATE_SECTION_HEADER: checked_header,
                TEMPLATE_SECTION_BODY: checked_body,
                TEMPLATE_SECTION_FOOTER: checked_footer,
            },
            actor_telegram_user_id=actor.telegram_user_id,
            place_id=row.place_id,
            chosen_brick_ids=chosen_brick_ids,
        )
    except TemplateCompileError as exc:
        return _mutation(
            "invalid",
            _template_details(session, row),
            str(exc),
        )
    if not compiled.ok:
        issues: list[str] = []
        for err in compiled.parse_errors:
            issues.append(str(err))
        for code in compiled.unknown_codes:
            issues.append(f"Неизвестный кирпичик [код={code}].")
        for amb in compiled.ambiguous_codes:
            candidates = ", ".join(f"#{c.brick_id} ({c.scope})" for c in amb.candidates)
            issues.append(
                f"Код [код={amb.code}] неоднозначен (секция {amb.section}); "
                f"кандидаты: {candidates}. Требуется явный выбор."
            )
        return _mutation(
            "invalid",
            _template_details(session, row),
            "; ".join(issues),
        )

    new_bindings: tuple[tuple[str, int], ...] = tuple(
        sorted(compiled.resolved_brick_ids.items())
    )
    old_bindings: tuple[tuple[str, int], ...] = tuple(
        sorted((b.code, b.brick_id) for b in current.bindings)
    )
    bindings_changed = new_bindings != old_bindings

    changed = content != getattr(current, section_name) or bindings_changed
    if not changed:
        return _mutation(
            "unchanged",
            _template_details(session, row),
            "Содержимое не изменилось; новая версия не создана.",
        )

    return _commit_new_version(
        session,
        row=row,
        actor=actor,
        expected_revision=expected_revision,
        header=checked_header,
        body=checked_body,
        footer=checked_footer,
        bindings=new_bindings,
        builtin_version=None,
    )


def update_template_header(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    content: object,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateMutationResult:
    """Безопасно меняет только шапку."""
    return update_template_section(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        template_id=template_id,
        expected_revision=expected_revision,
        section=TEMPLATE_SECTION_HEADER,
        content=content,
        chosen_brick_ids=chosen_brick_ids,
    )


def update_template_body(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    content: object,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateMutationResult:
    """Безопасно меняет только тело."""
    return update_template_section(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        template_id=template_id,
        expected_revision=expected_revision,
        section=TEMPLATE_SECTION_BODY,
        content=content,
        chosen_brick_ids=chosen_brick_ids,
    )


def update_template_footer(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    content: object,
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateMutationResult:
    """Безопасно меняет только подвал."""
    return update_template_section(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        template_id=template_id,
        expected_revision=expected_revision,
        section=TEMPLATE_SECTION_FOOTER,
        content=content,
        chosen_brick_ids=chosen_brick_ids,
    )


def _reset_to_builtin(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    confirm: bool,
    expected_kind: str,
    not_found_message: str,
    forbidden_message: str,
    confirmation_message: str,
) -> TemplateMutationResult:
    """
    Общий подтверждённый сброс личного или стандартного шаблона.

    Проверки прав различаются только для стандарта (админ места/суперадмин);
    владелец личного шаблона проверяется напрямую. Дальше логика одинакова:
    revision, явное подтверждение, разбор встроенного стандарта, новая версия
    без изменения истории и снимков.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _load_template_row(session, template_id)
    if row is None or row.kind != expected_kind:
        return _mutation("not_found", None, not_found_message)
    if expected_kind == TEMPLATE_KIND_PERSONAL:
        if row.owner_user_id != actor.id:
            return _mutation(
                "forbidden",
                _template_details(session, row),
                forbidden_message,
            )
    elif not _can_edit_template(session, actor, row):
        return _mutation(
            "forbidden",
            _template_details(session, row),
            forbidden_message,
        )
    if not _valid_positive_id(expected_revision):
        raise TemplateError("expected_revision должен быть положительным числом.")
    if not confirm:
        raise TemplateConfirmationRequired(confirmation_message)
    if row.revision != expected_revision:
        return _mutation(
            "stale",
            _template_details(session, row),
            "Шаблон уже изменился в другом окне. Откройте его заново.",
        )

    current = _version_details(session, _version_row(session, row))
    from template_compiler import resolve_template_bindings

    compiled = resolve_template_bindings(
        session,
        {
            TEMPLATE_SECTION_HEADER: BUILTIN_TEMPLATE.header,
            TEMPLATE_SECTION_BODY: BUILTIN_TEMPLATE.body,
            TEMPLATE_SECTION_FOOTER: BUILTIN_TEMPLATE.footer,
        },
        actor_telegram_user_id=actor.telegram_user_id,
        place_id=row.place_id,
    )
    if not compiled.ok:
        issues: list[str] = []
        for code in compiled.unknown_codes:
            issues.append(f"Неизвестный кирпичик [код={code}].")
        for amb in compiled.ambiguous_codes:
            issues.append(f"Код [код={amb.code}] неоднозначен.")
        return _mutation(
            "invalid",
            _template_details(session, row),
            "; ".join(issues),
        )
    binding_pairs: tuple[tuple[str, int], ...] = tuple(
        sorted(compiled.resolved_brick_ids.items())
    )
    unchanged = (
        current.header == BUILTIN_TEMPLATE.header
        and current.body == BUILTIN_TEMPLATE.body
        and current.footer == BUILTIN_TEMPLATE.footer
        and tuple(sorted((b.code, b.brick_id) for b in current.bindings))
        == binding_pairs
    )
    if unchanged:
        return _mutation(
            "unchanged",
            _template_details(session, row),
            "Шаблон уже совпадает со встроенным стандартом.",
        )

    return _commit_new_version(
        session,
        row=row,
        actor=actor,
        expected_revision=expected_revision,
        header=BUILTIN_TEMPLATE.header,
        body=BUILTIN_TEMPLATE.body,
        footer=BUILTIN_TEMPLATE.footer,
        bindings=binding_pairs,
        builtin_version=BUILTIN_TEMPLATE_VERSION,
    )


def ensure_initial_personal_template(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
) -> TemplateDetails:
    """
    Первый личный шаблон нового пользователя — копия встроенного стандарта.

    Poster 2.0: каждый новый пользователь получает независимую копию
    стандартного шаблона, чтобы сразу работать с «Создать по личному
    шаблону». Копия — обычный `kind='personal'`-шаблон с именем
    «Мой шаблон» и текстом `BUILTIN_TEMPLATE`; последующие правки эталона
    его не меняют.

    Идемпотентность: повторный вызов возвращает существующий первый личный
    шаблон и второй не создаёт. Осознанное удаление всех личных шаблонов
    пользователем **не** восстанавливается самовольно: сам шаблон при удалении
    исчезает, но остаётся его неизменяемый снимок-метка (`ensure_*` создаёт
    его один раз при выдаче), поэтому `has_personal_template_history` помнит
    о выдаче и S2 не выдаёт второй. Если личных шаблонов несколько,
    возвращается самый ранний — новых не появляется.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    existing = list_personal_templates(session, actor.telegram_user_id)
    if existing:
        return existing[0]
    created = create_personal_template(
        session,
        actor_telegram_user_id=actor.telegram_user_id,
        name=INITIAL_PERSONAL_TEMPLATE_NAME,
        header=BUILTIN_TEMPLATE.header,
        body=BUILTIN_TEMPLATE.body,
        footer=BUILTIN_TEMPLATE.footer,
    )
    # Метка выдачи (Poster 2.0, S2). Строка `templates` — не единственный
    # след: снимок переживает её удаление и служит признаком «стартовый
    # шаблон уже выдавался». Без метки `/start` воскрешал бы осознанно
    # удалённый шаблон, потому что список личных шаблонов после удаления пуст.
    # Ключ идемпотентности защищает от второго снимка при повторной выдаче.
    create_template_snapshot(
        session,
        actor_telegram_user_id=actor.telegram_user_id,
        template_id=created.template_id,
        idempotency_key=INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY,
    )
    return created


def reset_standard_template(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    confirm: bool = False,
) -> TemplateMutationResult:
    """
    Подтверждённый сброс стандарта места к встроенному стандарту.

    Создаёт новую версию (если содержимое или привязки отличаются), сохраняя
    всю историю. Существующие снимки не меняются. Автоматического сброса при
    старте/миграции нет: требуется `confirm=True` и актуальная revision.
    """
    return _reset_to_builtin(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        template_id=template_id,
        expected_revision=expected_revision,
        confirm=confirm,
        expected_kind=TEMPLATE_KIND_STANDARD,
        not_found_message="Стандартный шаблон не найден.",
        forbidden_message=(
            "Сбрасывать стандартный шаблон может только администратор места "
            "или суперадминистратор."
        ),
        confirmation_message="Сброс стандартного шаблона требует явного подтверждения.",
    )


def reset_personal_template(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int,
    confirm: bool = False,
) -> TemplateMutationResult:
    """
    Подтверждённый сброс личного шаблона к встроенному стандарту.

    Логика повторяет `reset_standard_template`, но проверка прав владельческая:
    только владелец личного шаблона может его сбрасывать. Системные кирпичики
    в привязках встроенного стандарта общедоступны, поэтому place_id не нужен.
    """
    return _reset_to_builtin(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        template_id=template_id,
        expected_revision=expected_revision,
        confirm=confirm,
        expected_kind=TEMPLATE_KIND_PERSONAL,
        not_found_message="Личный шаблон не найден.",
        forbidden_message="Сбрасывать личный шаблон может только его владелец.",
        confirmation_message="Сброс личного шаблона требует явного подтверждения.",
    )


# --- Неизменяемые снимки ----------------------------------------------------


def _validate_idempotency_key(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TemplateError("Ключ идемпотентности должен быть строкой.")
    if not value.strip():
        raise TemplateError("Ключ идемпотентности не должен быть пустым.")
    return value


def _existing_snapshot(
    session: Session, owner_user_id: int, idempotency_key: str
) -> TemplateSnapshot | None:
    return session.scalar(
        select(TemplateSnapshot).where(
            TemplateSnapshot.owner_user_id == owner_user_id,
            TemplateSnapshot.idempotency_key == idempotency_key,
        )
    )


def create_template_snapshot(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    expected_revision: int | None = None,
    idempotency_key: object | None = None,
    commit: bool = True,
) -> TemplateSnapshotDetails:
    """
    Создаёт самостоятельный снимок текущей версии доступного шаблона.

    Секции и все brick_id копируются одной транзакцией. `idempotency_key`
    делает повтор операции безопасным: возвращается уже созданный снимок,
    а не второй. Снимок не связан со старой таблицей `drafts`.

    `commit=False` позволяет вызывающему сервису (Stage 27A) включить снимок
    в собственную транзакцию вместе с привязкой к черновику. При ошибке
    вызывающий обязан откатить сессию; по умолчанию поведение прежнее.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    key = _validate_idempotency_key(idempotency_key)
    if key is not None:
        existing = _existing_snapshot(session, actor.id, key)
        if existing is not None:
            if existing.source_template_id != template_id:
                raise TemplateConflictError(
                    "Ключ идемпотентности уже использован для другого шаблона."
                )
            return _snapshot_details(session, existing)

    template = _readable_template_row(session, actor, template_id)
    if expected_revision is not None:
        if not _valid_positive_id(expected_revision):
            raise TemplateError("expected_revision должен быть положительным числом.")
        if template.revision != expected_revision:
            raise TemplateConflictError(
                "Текущая версия шаблона уже изменилась. Обновите шаблон."
            )
    version = _version_row(session, template)
    binding_ids = _binding_views_for_version(session, version.id)
    now = utcnow()
    snapshot = TemplateSnapshot(
        owner_user_id=actor.id,
        source_template_id=template.id,
        source_template_version=version.version,
        source_kind=template.kind,
        source_name=template.name,
        source_place_id=template.place_id,
        source_owner_user_id=template.owner_user_id,
        header=version.header,
        body=version.body,
        footer=version.footer,
        builtin_version=version.builtin_version,
        idempotency_key=key,
        created_at=now,
    )
    session.add(snapshot)
    try:
        session.flush()
        _insert_snapshot_bindings(
            session,
            snapshot_id=snapshot.id,
            bindings=[(binding.code, binding.brick_id) for binding in binding_ids],
        )
        if not _still_readable(session, actor.id, actor.telegram_user_id, template):
            session.rollback()
            raise TemplatePermissionError(
                "Права на чтение шаблона больше не действуют."
            )
        if commit:
            session.commit()
        else:
            session.flush()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        if key is not None:
            existing = _existing_snapshot(session, actor.id, key)
            if existing is not None:
                return _snapshot_details(session, existing)
        raise TemplateConflictError(
            "Не удалось создать снимок из-за одновременной записи. "
            "Повторите попытку."
        ) from exc
    except TemplatePermissionError:
        raise
    except Exception:
        session.rollback()
        raise
    stored = session.get(TemplateSnapshot, snapshot.id)
    if stored is None:
        raise TemplateError("Снимок не найден после сохранения.")
    return _snapshot_details(session, stored)


def get_template_snapshot(
    session: Session,
    actor_telegram_user_id: int | None,
    snapshot_id: int,
) -> TemplateSnapshotDetails | None:
    """
    Снимок только для пользователя, для которого он создан.

    Угаданный ID чужого снимка возвращает None. Роль суперадминистратора не
    даёт доступа к чужому личному содержимому.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    if not _valid_positive_id(snapshot_id):
        return None
    row = session.scalar(
        select(TemplateSnapshot).where(
            TemplateSnapshot.id == snapshot_id,
            TemplateSnapshot.owner_user_id == actor.id,
        )
    )
    return _snapshot_details(session, row) if row is not None else None


def list_template_snapshots(
    session: Session,
    actor_telegram_user_id: int | None,
) -> list[TemplateSnapshotDetails]:
    """Снимки только текущего пользователя."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    rows = session.scalars(
        select(TemplateSnapshot)
        .where(TemplateSnapshot.owner_user_id == actor.id)
        .order_by(TemplateSnapshot.created_at, TemplateSnapshot.id)
    ).all()
    return [_snapshot_details(session, row) for row in rows]


# Источник личного анонса. Совпадает с CHECK `ck_announcement_drafts_origin`
# и с `announcement_store.ORIGIN_PERSONAL`; литерал здесь намеренно, чтобы не
# импортировать announcement_store (он сам импортирует этот модуль).
_DRAFT_ORIGIN_PERSONAL = "personal"


def has_personal_template_history(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
) -> bool:
    """
    Был ли у пользователя личный шаблон когда-либо — а не только сейчас.

    Нужен ровно для одного решения: выдавать ли стартовый личный шаблон на
    `/start` (Poster 2.0, S2). Признак «живой личный шаблон есть» недостаточен:
    после осознанного удаления шаблона список пуст, и наивная проверка
    воскрешала бы удалённое при каждом входе.

    Следы владения личным шаблоном переживают удаление строки `templates`:
    снимок `template_snapshots.source_kind='personal'` (создаётся при старте
    любого личного анонса) и сам анонс `announcement_drafts.origin='personal'`.
    Поэтому проверяются три независимых признака: живой шаблон, снимок, анонс.

    Граница: физическое удаление строк шаблона напрямую в SQLite вне продукта
    (вместе со снимками и анонсами) этим признаком не покрывается. В продукте
    такого пути нет: сервисного удаления шаблонов с чисткой истории не
    существует, а удаление из UI (когда появится) обязано сохранять снимки —
    история анонсов не должна теряться. Приоритет — не воскрешать осознанно
    удалённый шаблон; см. решение владельца в S2.
    """
    actor = _actor_user(session, actor_telegram_user_id)
    if actor is None:
        return False
    if (
        session.scalar(
            select(Template.id)
            .where(
                Template.kind == TEMPLATE_KIND_PERSONAL,
                Template.owner_user_id == actor.id,
            )
            .limit(1)
        )
        is not None
    ):
        return True
    if (
        session.scalar(
            select(TemplateSnapshot.id)
            .where(
                TemplateSnapshot.owner_user_id == actor.id,
                TemplateSnapshot.source_kind == TEMPLATE_KIND_PERSONAL,
            )
            .limit(1)
        )
        is not None
    ):
        return True
    return (
        session.scalar(
            select(AnnouncementDraft.id)
            .where(
                AnnouncementDraft.author_user_id == actor.id,
                AnnouncementDraft.origin == _DRAFT_ORIGIN_PERSONAL,
            )
            .limit(1)
        )
        is not None
    )


# --- Совместимость привязок -------------------------------------------------


def validate_template_compatibility(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    version: int | None = None,
    selected_place_id: int | None = None,
) -> TemplateCompatibility:
    """
    Проверяет, можно ли исполнять версию шаблона с её явными brick_id.

    Проверка не разбирает текст. Для стандарта допустимы системные и
    кирпичики своего места; для личного — системные и личные кирпичики
    владельца. Архивный/исчезнувший brick_id остаётся в истории, но делает
    версию несовместимой до явного решения будущего редактора.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    template = _readable_template_row(session, actor, template_id)
    selected_place = None
    if selected_place_id is not None:
        selected_place = _require_place(session, selected_place_id)
    version_row = _version_row(session, template, version)
    details = _version_details(session, version_row)
    issues: list[str] = []
    if (
        template.kind == TEMPLATE_KIND_STANDARD
        and selected_place is not None
        and selected_place.id != template.place_id
    ):
        issues.append("Стандартный шаблон относится к другому месту и не переносится.")
    for binding in details.bindings:
        if binding.status != BRICK_STATUS_ACTIVE:
            issues.append(f"Кирпичик #{binding.brick_id} «{binding.code}» в архиве.")
        if binding.scope == BRICK_SCOPE_SYSTEM:
            continue
        if template.kind == TEMPLATE_KIND_STANDARD:
            if (
                binding.scope == BRICK_SCOPE_PLACE
                and template.place_id is not None
                and _brick_place_id(session, binding.brick_id) == template.place_id
            ):
                continue
            issues.append(
                f"Стандартный шаблон не может использовать кирпичик "
                f"#{binding.brick_id} из чужой области."
            )
            continue
        if (
            binding.scope == BRICK_SCOPE_PERSONAL
            and template.owner_user_id is not None
            and _brick_owner_id(session, binding.brick_id) == template.owner_user_id
        ):
            continue
        issues.append(
            f"Личный шаблон не может зависеть от кирпичика "
            f"#{binding.brick_id} чужого владельца или места."
        )
    return TemplateCompatibility(
        template_id=template.id,
        version=version_row.version,
        kind=template.kind,
        place_id=template.place_id,
        compatible=not issues,
        issues=tuple(issues),
        bindings=details.bindings,
    )


def _brick_place_id(session: Session, brick_id: int) -> int | None:
    return session.scalar(select(Brick.place_id).where(Brick.id == brick_id))


def _brick_owner_id(session: Session, brick_id: int) -> int | None:
    return session.scalar(select(Brick.owner_user_id).where(Brick.id == brick_id))


# --- Исполнение сохранённых версий и снимков (Stage 24) ---------------------


def _restore_bindings_on_tokens(
    tokens: Sequence[Any],
    bindings_by_code: Mapping[str, int],
) -> None:
    """
    Восстанавливает brick_id у токенов по коду из сохранённых привязок.

    Никакого разрешения кода заново: если код отсутствует в привязках,
    это ошибка, а не автоматический выбор другого кирпичика.
    """
    from template_parser import BrickNode, LinkNode  # ленивый импорт

    def _visit(node: Any) -> None:
        if isinstance(node, BrickNode):
            if node.code not in bindings_by_code:
                raise TemplateCompileError(
                    f"У токена [код={node.code}] нет сохранённой привязки; "
                    "отсутствующая привязка — ошибка."
                )
            node.brick_id = bindings_by_code[node.code]
        elif isinstance(node, LinkNode):
            for child in node.label:
                _visit(child)
            if isinstance(node.url, BrickNode):
                _visit(node.url)
        elif hasattr(node, "children"):
            for child in getattr(node, "children"):
                _visit(child)

    for token in tokens:
        _visit(token)


def _load_brick_definitions(
    session: Session, brick_ids: Iterable[int]
) -> dict[int, BrickDefinition]:
    """Загружает определения кирпичиков по набору ID; пропускает удалённые."""
    ids = list(set(brick_ids))
    if not ids:
        return {}
    rows = session.scalars(select(Brick).where(Brick.id.in_(ids))).all()
    from brick_store import _snapshot as brick_snapshot  # локальный импорт

    return {row.id: brick_snapshot(row) for row in rows}


def render_template_version(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    template_id: int,
    values_by_id: Mapping[int, Any],
    version: int | None = None,
) -> str:
    """
    Компилирует сохранённую версию шаблона в HTML-анонс.

    Загружает доступную версию, разбирает сохранённый текст, восстанавливает
    brick_id ИСКЛЮЧИТЕЛЬНО из сохранённых привязок (по коду), проверяет
    соответствие токенов и привязок, подставляет значения по ID.

    Никакого разрешения brick_id по текстовому коду при выполнении:
    отсутствующая привязка — ошибка, а не автоматический выбор другого
    кирпичика.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    template = _readable_template_row(session, actor, template_id)
    version_row = _version_row(session, template, version)
    binding_views = _binding_views_for_version(session, version_row.id)
    bindings_by_code = {b.code: b.brick_id for b in binding_views}
    from template_parser import parse_template  # ленивый импорт

    tokens_by_section = {
        TEMPLATE_SECTION_HEADER: parse_template(version_row.header, section="header"),
        TEMPLATE_SECTION_BODY: parse_template(version_row.body, section="body"),
        TEMPLATE_SECTION_FOOTER: parse_template(version_row.footer, section="footer"),
    }
    brick_ids = set(bindings_by_code.values())
    definitions = _load_brick_definitions(session, brick_ids)
    for tokens in tokens_by_section.values():
        _restore_bindings_on_tokens(tokens, bindings_by_code)
    from template_compiler import render_template

    return render_template(tokens_by_section, values_by_id, definitions)


def render_template_snapshot(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    snapshot_id: int,
    values_by_id: Mapping[int, Any],
) -> str:
    """
    Компилирует сохранённый snapshot шаблона в HTML-анонс.

    Загружает снимок (доступен только владельцу), разбирает сохранённый
    текст, восстанавливает brick_id ИСКЛЮЧИТЕЛЬНО из сохранённых привязок
    снимка (по коду), проверяет соответствие токенов и привязок, подставляет
    значения по ID.

    Никакого разрешения brick_id по текстовому коду при выполнении.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    if not _valid_positive_id(snapshot_id):
        raise TemplateNotFoundError("Некорректный идентификатор снимка.")
    snapshot_row = session.scalar(
        select(TemplateSnapshot).where(
            TemplateSnapshot.id == snapshot_id,
            TemplateSnapshot.owner_user_id == actor.id,
        )
    )
    if snapshot_row is None:
        raise TemplateNotFoundError(
            "Снимок не найден или принадлежит другому пользователю."
        )
    binding_views = _binding_views_for_snapshot(session, snapshot_row.id)
    bindings_by_code = {b.code: b.brick_id for b in binding_views}
    from template_parser import parse_template  # ленивый импорт

    tokens_by_section = {
        TEMPLATE_SECTION_HEADER: parse_template(snapshot_row.header, section="header"),
        TEMPLATE_SECTION_BODY: parse_template(snapshot_row.body, section="body"),
        TEMPLATE_SECTION_FOOTER: parse_template(snapshot_row.footer, section="footer"),
    }
    brick_ids = set(bindings_by_code.values())
    definitions = _load_brick_definitions(session, brick_ids)
    for tokens in tokens_by_section.values():
        _restore_bindings_on_tokens(tokens, bindings_by_code)
    from template_compiler import render_template

    return render_template(tokens_by_section, values_by_id, definitions)
