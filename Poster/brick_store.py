# brick_store.py
"""
Независимый от Telegram слой реестра кирпичиков (Stage 22A).

Здесь собраны чтение определений, проверка видимости/принадлежности,
уникальность технических кодов, авторизованное создание в трёх областях
(system/place/personal) и контракт источника значения.

Границы этапа:

- значения кирпичиков в черновике не хранятся — `draft_values` относится к
  этапу шаблонов;
- пользовательского конструктора и заявок на согласование ещё нет;
- обычный пользователь может создать только ЛИЧНЫЙ кирпичик, владельцем
  которого является он сам;
- кирпичик места создаёт администратор этого места (или суперадмин);
- системный кирпичик создаёт только суперадмин; в штатном старте системные
  определения приходят из миграции;
- права проверяются в сервисном слое, а БД дополнительно защищает системные
  коды, уникальность и принадлежность.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_SCOPES,
    BRICK_SOURCE_ASK,
    BRICK_SOURCE_CONTEXT,
    BRICK_SOURCE_PLACE,
    BRICK_STATUS_ACTIVE,
    BRICK_STATUS_ARCHIVED,
    FORBIDDEN_BRICK_CODES,
    SYSTEM_BRICK_CODES,
    SYSTEM_BRICKS,
    BrickValueResult,
    BrickValueStatus,
    SystemBrickDefinition,
    clean_brick_title,
    ensure_brick_source,
    ensure_brick_type,
    normalize_brick_code,
    parse_rules,
    rules_from_json,
    rules_to_json,
    validate_brick_code,
    validate_brick_value,
)
from models import Brick, Place, User, utcnow
from permissions import can_manage_place, is_superadmin
from place_store import get_place_details, get_place_name

_SCOPE_ORDER = {
    BRICK_SCOPE_SYSTEM: 0,
    BRICK_SCOPE_PLACE: 1,
    BRICK_SCOPE_PERSONAL: 2,
}

_PLACE_FIELDS = {
    "место": "name",
    "адрес_места": "address",
    "ссылка_места": "url",
}

_UNSET = object()


class BrickError(ValueError):
    """Базовая ошибка операций с кирпичиками."""


class BrickPermissionError(PermissionError):
    """Создание/изменение запрещено ролью вызывающего."""


class BrickActorError(RuntimeError):
    """Пользователь ещё не зарегистрирован в таблице users."""


class BrickNotFoundError(BrickError):
    """Кирпичик/место не найдены или не видимы вызывающему."""


class BrickConflictError(BrickError):
    """Технический код занят или нарушено ограничение БД."""


@dataclass(frozen=True)
class BrickDefinition:
    """Снимок определения, пригодный после закрытия сессии."""

    id: int
    code: str
    title: str
    value_type: str
    scope: str
    source: str
    place_id: int | None
    owner_user_id: int | None
    rules: dict[str, object]
    status: str
    revision: int
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime | None

    @property
    def is_active(self) -> bool:
        return self.status == BRICK_STATUS_ACTIVE


@dataclass(frozen=True)
class BrickCodeCandidates:
    """
    Кандидаты на один и тот же `[код]` в порядке видимости Stage 22A.

    Для будущего редактора шаблонов: если `ambiguous`, одного кода недостаточно
    для однозначного намерения автора. Шаблон обязан сохранить выбранный
    `brick_id`, а не переразрешать код при каждом применении. Текущий runtime
    по-прежнему использует `selected` (system → place → personal).
    """

    code: str | None
    candidates: tuple[BrickDefinition, ...]
    selected: BrickDefinition | None
    ambiguous: bool
    blocked_by_archived_system: bool = False


@dataclass(frozen=True)
class BrickMutationResult:
    """Понятный результат изменяющей операции над личным кирпичиком."""

    status: str
    brick: BrickDefinition | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {
            "updated",
            "archived",
            "restored",
            "already_active",
            "already_archived",
        }


@dataclass(frozen=True)
class BrickCodeAvailability:
    """Результат проверки кода без записи в БД."""

    code: str | None
    available: bool
    reason: str | None = None
    message: str | None = None


def _snapshot(row: Brick) -> BrickDefinition:
    return BrickDefinition(
        id=row.id,
        code=row.code,
        title=row.title,
        value_type=row.value_type,
        scope=row.scope,
        source=row.source,
        place_id=row.place_id,
        owner_user_id=row.owner_user_id,
        rules=rules_from_json(row.value_type, row.rules_json),
        status=row.status,
        revision=row.revision,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _valid_telegram_user_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _actor_user(session: Session, telegram_user_id: int | None) -> User | None:
    if not _valid_telegram_user_id(telegram_user_id):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _require_actor_user(session: Session, telegram_user_id: int | None) -> User:
    actor = _actor_user(session, telegram_user_id)
    if actor is None:
        raise BrickActorError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _require_place(session: Session, place_id: int) -> Place:
    if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
        raise BrickNotFoundError("Некорректный идентификатор места.")
    place = session.get(Place, place_id)
    if place is None:
        raise BrickNotFoundError("Место не найдено.")
    return place


# --- Чтение системного реестра ----------------------------------------------


def list_system_bricks(
    session: Session, *, include_archived: bool = False
) -> list[BrickDefinition]:
    """Все системные определения в стабильном порядке ID."""
    statement = select(Brick).where(Brick.scope == BRICK_SCOPE_SYSTEM)
    if not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    rows = session.scalars(statement.order_by(Brick.id)).all()
    return [_snapshot(row) for row in rows]


def get_system_brick(
    session: Session,
    code: str,
    *,
    include_archived: bool = False,
) -> BrickDefinition | None:
    """Системный кирпичик по коду; личный/местный код сюда не подставляется."""
    try:
        canonical = normalize_brick_code(code)
    except ValueError:
        return None
    statement = select(Brick).where(
        Brick.scope == BRICK_SCOPE_SYSTEM,
        Brick.code == canonical,
    )
    if not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    row = session.scalar(statement)
    return _snapshot(row) if row is not None else None


# --- Видимость и разрешение имён --------------------------------------------


def can_view_brick(
    session: Session,
    brick_id: int,
    *,
    viewer_telegram_user_id: int | None,
    place_id: int | None = None,
    include_archived: bool = False,
) -> bool:
    """Может ли вызывающий видеть именно это определение."""
    return (
        get_visible_brick(
            session,
            brick_id,
            viewer_telegram_user_id=viewer_telegram_user_id,
            place_id=place_id,
            include_archived=include_archived,
        )
        is not None
    )


def get_visible_brick(
    session: Session,
    brick_id: int,
    *,
    viewer_telegram_user_id: int | None,
    place_id: int | None = None,
    include_archived: bool = False,
) -> BrickDefinition | None:
    """
    Определение, видимое в контексте пользователя/выбранного места.

    Системные кирпичики видны всем; местный — только в контексте ЭТОГО места;
    личный — только владельцу. Чужой приватный `brick_id` не раскрывается:
    возвращается None, а не данные определения.
    """
    if isinstance(brick_id, bool) or not isinstance(brick_id, int) or brick_id <= 0:
        return None
    row = session.get(Brick, brick_id)
    if row is None:
        return None
    if not include_archived and row.status != BRICK_STATUS_ACTIVE:
        return None
    if row.scope == BRICK_SCOPE_SYSTEM:
        return _snapshot(row)
    if row.scope == BRICK_SCOPE_PLACE:
        if place_id is None or row.place_id != place_id:
            return None
        return _snapshot(row)
    if row.scope == BRICK_SCOPE_PERSONAL:
        viewer = _actor_user(session, viewer_telegram_user_id)
        if viewer is None or row.owner_user_id != viewer.id:
            return None
        return _snapshot(row)
    return None


def resolve_brick_code_candidates(
    session: Session,
    code: str,
    *,
    viewer_telegram_user_id: int | None = None,
    place_id: int | None = None,
    include_archived: bool = False,
) -> BrickCodeCandidates:
    """
    Все видимые кандидаты на `[код]` и детерминированный выбор runtime.

    Порядок кандидатов: system → place выбранного места → personal зрителя.
    Внутри области код уникален. Системный код зарезервирован навсегда: его
    архивная строка не «проваливается» в другую область.

    Для будущего шаблона `candidates` важнее одного `selected`: если
    `ambiguous=True`, редактор обязан попросить пользователя выбрать конкретный
    `brick_id` и сохранить его. Повторное разрешение кода при применении
    шаблона запрещено — иначе `[стоимость]` могла бы незаметно сменить
    значение после появления одноимённого кирпичика места.
    """
    try:
        canonical = normalize_brick_code(code)
    except ValueError:
        return BrickCodeCandidates(
            code=None, candidates=(), selected=None, ambiguous=False
        )
    status_filter = [] if include_archived else [Brick.status == BRICK_STATUS_ACTIVE]
    candidates: list[Brick] = []

    system_row = session.scalar(
        select(Brick).where(
            Brick.scope == BRICK_SCOPE_SYSTEM,
            Brick.code == canonical,
        )
    )
    if system_row is not None:
        if not include_archived and system_row.status != BRICK_STATUS_ACTIVE:
            return BrickCodeCandidates(
                code=canonical,
                candidates=(),
                selected=None,
                ambiguous=False,
                blocked_by_archived_system=True,
            )
        candidates.append(system_row)

    if not candidates and place_id is not None:
        place_row = session.scalar(
            select(Brick).where(
                Brick.scope == BRICK_SCOPE_PLACE,
                Brick.place_id == place_id,
                Brick.code == canonical,
                *status_filter,
            )
        )
        if place_row is not None:
            candidates.append(place_row)

    # Системный код не может сосуществовать с несистемным (триггер БД), но
    # проверка `not candidates` сохраняет тот же приоритет и на legacy-базах
    # без триггеров. Для несистемного кода место и личный кирпичик могут
    # совпасть — тогда кандидатов два, и это осознанная неоднозначность.
    if not candidates:
        viewer = _actor_user(session, viewer_telegram_user_id)
        if viewer is not None:
            personal_row = session.scalar(
                select(Brick).where(
                    Brick.scope == BRICK_SCOPE_PERSONAL,
                    Brick.owner_user_id == viewer.id,
                    Brick.code == canonical,
                    *status_filter,
                )
            )
            if personal_row is not None:
                candidates.append(personal_row)
    elif place_id is not None:
        viewer = _actor_user(session, viewer_telegram_user_id)
        if viewer is not None:
            personal_row = session.scalar(
                select(Brick).where(
                    Brick.scope == BRICK_SCOPE_PERSONAL,
                    Brick.owner_user_id == viewer.id,
                    Brick.code == canonical,
                    *status_filter,
                )
            )
            if personal_row is not None and all(
                row.id != personal_row.id for row in candidates
            ):
                candidates.append(personal_row)

    snapshots = tuple(_snapshot(row) for row in candidates)
    return BrickCodeCandidates(
        code=canonical,
        candidates=snapshots,
        selected=snapshots[0] if snapshots else None,
        ambiguous=len(snapshots) > 1,
    )


def resolve_brick_by_code(
    session: Session,
    code: str,
    *,
    viewer_telegram_user_id: int | None = None,
    place_id: int | None = None,
    include_archived: bool = False,
) -> BrickDefinition | None:
    """
    Детерминированное разрешение текстового `[код]` (system → place → personal).

    Сохранено для текущего runtime; будущая привязка шаблона должна вызывать
    `resolve_brick_code_candidates` и сохранять конкретный `brick_id`, если
    кандидатов несколько.
    """
    return resolve_brick_code_candidates(
        session,
        code,
        viewer_telegram_user_id=viewer_telegram_user_id,
        place_id=place_id,
        include_archived=include_archived,
    ).selected


# --- Списки доступных определений -------------------------------------------


def _visible_conditions(
    session: Session,
    *,
    viewer_telegram_user_id: int | None,
    place_id: int | None,
) -> tuple[list[ColumnElement[bool]], User | None]:
    if place_id is not None:
        _require_place(session, place_id)
    viewer = _actor_user(session, viewer_telegram_user_id)
    visibility: list[ColumnElement[bool]] = [Brick.scope == BRICK_SCOPE_SYSTEM]
    if place_id is not None:
        visibility.append(
            and_(
                Brick.scope == BRICK_SCOPE_PLACE,
                Brick.place_id == place_id,
            )
        )
    if viewer is not None:
        visibility.append(
            and_(
                Brick.scope == BRICK_SCOPE_PERSONAL,
                Brick.owner_user_id == viewer.id,
            )
        )
    return visibility, viewer


def _search_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _sorted_definitions(rows: list[Brick]) -> list[BrickDefinition]:
    rows.sort(
        key=lambda row: (
            _SCOPE_ORDER.get(row.scope, len(_SCOPE_ORDER)),
            row.code.casefold(),
            row.id,
        )
    )
    return [_snapshot(row) for row in rows]


def list_available_bricks(
    session: Session,
    *,
    viewer_telegram_user_id: int | None = None,
    place_id: int | None = None,
    query: str | None = None,
    value_type: str | None = None,
    include_archived: bool = False,
) -> list[BrickDefinition]:
    """
    Доступные определения: системные + выбранного места + личные вызывающего.

    Чужие личные кирпичики не попадают в выборку ни при каком `place_id`.
    Поиск по коду/названию выполняется без учёта регистра и Unicode-формы;
    порядок детерминирован (system → place → personal, затем код и ID).
    """
    if value_type is not None:
        ensure_brick_type(value_type)
    if query is not None and not isinstance(query, str):
        raise BrickError("Поисковый запрос должен быть строкой.")

    visibility, _viewer = _visible_conditions(
        session,
        viewer_telegram_user_id=viewer_telegram_user_id,
        place_id=place_id,
    )
    statement = select(Brick).where(or_(*visibility))
    if not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    if value_type is not None:
        statement = statement.where(Brick.value_type == value_type)
    rows = list(session.scalars(statement.order_by(Brick.id)).all())

    needle = _search_key(query) if query else ""
    if needle:
        rows = [
            row
            for row in rows
            if needle in row.code.casefold() or needle in _search_key(row.title)
        ]
    return _sorted_definitions(rows)


def list_personal_bricks(
    session: Session,
    actor_telegram_user_id: int | None,
    *,
    include_archived: bool = False,
    status_filter: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[BrickDefinition]:
    """
    Личные определения только этого пользователя.

    `status_filter` (active/archived) имеет приоритет над `include_archived`.
    `limit/offset` — серверная пагинация для Telegram-списка. Чужие личные
    кирпичики не попадают в выборку ни при каких параметрах.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    if status_filter is not None and status_filter not in {
        BRICK_STATUS_ACTIVE,
        BRICK_STATUS_ARCHIVED,
    }:
        raise BrickError(f"Неизвестный статус кирпичика: {status_filter!r}.")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
    ):
        raise BrickError("limit должен быть положительным целым числом.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise BrickError("offset должен быть неотрицательным целым числом.")
    statement = select(Brick).where(
        Brick.scope == BRICK_SCOPE_PERSONAL,
        Brick.owner_user_id == actor.id,
    )
    if status_filter is not None:
        statement = statement.where(Brick.status == status_filter)
    elif not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    statement = statement.order_by(Brick.code, Brick.id)
    if offset:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    rows = list(session.scalars(statement).all())
    return [_snapshot(row) for row in rows]


def count_personal_bricks(
    session: Session,
    actor_telegram_user_id: int | None,
    *,
    include_archived: bool = False,
    status_filter: str | None = None,
) -> int:
    """Количество личных кирпичиков владельца без загрузки всех строк."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    if status_filter is not None and status_filter not in {
        BRICK_STATUS_ACTIVE,
        BRICK_STATUS_ARCHIVED,
    }:
        raise BrickError(f"Неизвестный статус кирпичика: {status_filter!r}.")
    statement = select(func.count(Brick.id)).where(
        Brick.scope == BRICK_SCOPE_PERSONAL,
        Brick.owner_user_id == actor.id,
    )
    if status_filter is not None:
        statement = statement.where(Brick.status == status_filter)
    elif not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    return int(session.scalar(statement) or 0)


def get_personal_brick(
    session: Session,
    actor_telegram_user_id: int | None,
    brick_id: int,
    *,
    include_archived: bool = True,
) -> BrickDefinition | None:
    """
    Личный кирпичик по ID только для владельца.

    Чужой/системный/местный ID возвращает None, не раскрывая определение.
    """
    if isinstance(brick_id, bool) or not isinstance(brick_id, int) or brick_id <= 0:
        return None
    actor = _actor_user(session, actor_telegram_user_id)
    if actor is None:
        return None
    statement = select(Brick).where(
        Brick.id == brick_id,
        Brick.scope == BRICK_SCOPE_PERSONAL,
        Brick.owner_user_id == actor.id,
    )
    if not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    row = session.scalar(statement)
    return _snapshot(row) if row is not None else None


def list_place_bricks(
    session: Session,
    place_id: int,
    *,
    include_archived: bool = False,
) -> list[BrickDefinition]:
    """Определения, принадлежащие конкретному месту."""
    _require_place(session, place_id)
    statement = select(Brick).where(
        Brick.scope == BRICK_SCOPE_PLACE,
        Brick.place_id == place_id,
    )
    if not include_archived:
        statement = statement.where(Brick.status == BRICK_STATUS_ACTIVE)
    rows = list(session.scalars(statement.order_by(Brick.code, Brick.id)).all())
    return [_snapshot(row) for row in rows]


# --- Уникальность и создание ------------------------------------------------


def check_code_availability(
    session: Session,
    code: object,
    *,
    scope: str,
    place_id: int | None = None,
    owner_user_id: int | None = None,
    exclude_brick_id: int | None = None,
) -> BrickCodeAvailability:
    """
    Проверяет технический код для области без записи.

    Системные коды зарезервированы во всех областях. Повтор кода запрещён
    внутри области: `(place_id, code)` или `(owner_user_id, code)`. Два
    разных пользователя/места могут иметь одинаковый несистемный код.
    `exclude_brick_id` применяется только при редактировании того же
    кирпичика, чтобы неизменённый код не считался конфликтом с собой.

    Архивные кирпичики участвуют в проверке: код остаётся занятым навсегда.
    Это исключает неоднозначность при восстановлении и старых ссылках.
    """
    if scope not in BRICK_SCOPES:
        raise BrickError(f"Неизвестная область кирпичика: {scope!r}.")
    if scope == BRICK_SCOPE_PLACE and not place_id:
        raise BrickError("Для области place нужен place_id.")
    if scope == BRICK_SCOPE_PERSONAL and not owner_user_id:
        raise BrickError("Для области personal нужен owner_user_id.")
    if scope == BRICK_SCOPE_SYSTEM and (place_id or owner_user_id):
        raise BrickError("Системный кирпичик не имеет владельца.")
    if exclude_brick_id is not None and (
        isinstance(exclude_brick_id, bool)
        or not isinstance(exclude_brick_id, int)
        or exclude_brick_id <= 0
    ):
        raise BrickError("exclude_brick_id должен быть положительным целым числом.")

    try:
        canonical = normalize_brick_code(code)
    except ValueError as exc:
        return BrickCodeAvailability(
            code=None,
            available=False,
            reason="invalid",
            message=str(exc),
        )
    if canonical in FORBIDDEN_BRICK_CODES:
        return BrickCodeAvailability(
            code=canonical,
            available=False,
            reason="forbidden",
            message=(
                "Код «фото» зарезервирован: фотографии — вложения анонса, "
                "а не кирпичик."
            ),
        )

    system_reserved = canonical in SYSTEM_BRICK_CODES
    system_row = session.scalar(
        select(Brick.id).where(
            Brick.scope == BRICK_SCOPE_SYSTEM,
            Brick.code == canonical,
        )
    )
    if scope != BRICK_SCOPE_SYSTEM and (system_reserved or system_row is not None):
        return BrickCodeAvailability(
            code=canonical,
            available=False,
            reason="system_reserved",
            message=(
                f"Код «{canonical}» зарезервирован системным кирпичиком "
                "и не может быть использован в другой области."
            ),
        )
    if scope == BRICK_SCOPE_SYSTEM:
        statement = select(Brick.id).where(Brick.code == canonical)
        if exclude_brick_id is not None:
            statement = statement.where(Brick.id != exclude_brick_id)
        any_row = session.scalar(statement)
        if system_row is not None or any_row is not None or system_reserved:
            return BrickCodeAvailability(
                code=canonical,
                available=False,
                reason="duplicate",
                message=f"Код «{canonical}» уже используется.",
            )
        return BrickCodeAvailability(code=canonical, available=True)

    if scope == BRICK_SCOPE_PLACE:
        statement = select(Brick.id).where(
            Brick.scope == BRICK_SCOPE_PLACE,
            Brick.place_id == place_id,
            Brick.code == canonical,
        )
    else:
        statement = select(Brick.id).where(
            Brick.scope == BRICK_SCOPE_PERSONAL,
            Brick.owner_user_id == owner_user_id,
            Brick.code == canonical,
        )
    if exclude_brick_id is not None:
        statement = statement.where(Brick.id != exclude_brick_id)
    duplicate = session.scalar(statement)
    if duplicate is not None:
        return BrickCodeAvailability(
            code=canonical,
            available=False,
            reason="duplicate",
            message=f"Код «{canonical}» уже используется в этой области.",
        )
    return BrickCodeAvailability(code=canonical, available=True)


def _insert_brick(
    session: Session,
    *,
    actor: User,
    scope: str,
    code: object,
    title: object,
    value_type: str,
    source: str,
    rules: Mapping[str, object] | str | None,
    place_id: int | None,
    owner_user_id: int | None,
) -> BrickDefinition:
    canonical_code = validate_brick_code(code)
    cleaned_title = clean_brick_title(title)
    checked_type = ensure_brick_type(value_type)
    checked_source = ensure_brick_source(source)
    validated_rules = parse_rules(checked_type, rules)
    availability = check_code_availability(
        session,
        canonical_code,
        scope=scope,
        place_id=place_id,
        owner_user_id=owner_user_id,
    )
    if not availability.available:
        raise BrickConflictError(availability.message or "Код уже используется.")

    now = utcnow()
    row = Brick(
        code=canonical_code,
        title=cleaned_title,
        value_type=checked_type,
        scope=scope,
        source=checked_source,
        place_id=place_id,
        owner_user_id=owner_user_id,
        rules_json=rules_to_json(checked_type, validated_rules),
        status=BRICK_STATUS_ACTIVE,
        created_by_user_id=actor.id,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise BrickConflictError(
            "Не удалось создать кирпичик: код уже занят, ограничение БД не "
            "выполнено или одновременная запись не завершилась. Повторите "
            "проверку."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _snapshot(row)


def create_personal_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    code: object,
    title: object,
    value_type: str,
    source: str = BRICK_SOURCE_ASK,
    rules: Mapping[str, object] | str | None = None,
) -> BrickDefinition:
    """
    Создаёт личный кирпичик действующего пользователя.

    Владелец всегда совпадает с актором; параметра «чужой owner» нет, поэтому
    присвоить чужой кирпичик невозможно. Для операции нужна регистрация
    пользователя (обычный трекинг Update её обеспечивает).
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    return _insert_brick(
        session,
        actor=actor,
        scope=BRICK_SCOPE_PERSONAL,
        code=code,
        title=title,
        value_type=value_type,
        source=source,
        rules=rules,
        place_id=None,
        owner_user_id=actor.id,
    )


def create_place_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    code: object,
    title: object,
    value_type: str,
    source: str = BRICK_SOURCE_ASK,
    rules: Mapping[str, object] | str | None = None,
) -> BrickDefinition:
    """Создаёт кирпичик места; нужно быть админом этого места или суперадмином."""
    actor = _require_actor_user(session, actor_telegram_user_id)
    _require_place(session, place_id)
    if not can_manage_place(session, actor.telegram_user_id, place_id):
        raise BrickPermissionError(
            "Создавать кирпичики места может только его администратор "
            "или суперадминистратор."
        )
    return _insert_brick(
        session,
        actor=actor,
        scope=BRICK_SCOPE_PLACE,
        code=code,
        title=title,
        value_type=value_type,
        source=source,
        rules=rules,
        place_id=place_id,
        owner_user_id=None,
    )


def create_system_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    code: object,
    title: object,
    value_type: str,
    source: str = BRICK_SOURCE_ASK,
    rules: Mapping[str, object] | str | None = None,
) -> BrickDefinition:
    """
    Минимальный контракт контролируемого создания системного кирпичика.

    Доступен только суперадмину из конфигурации. Пользовательского интерфейса
    на Stage 22A нет; штатный системный реестр сидируется миграцией 6.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    if not is_superadmin(actor.telegram_user_id):
        raise BrickPermissionError(
            "Создавать системные кирпичики может только суперадминистратор."
        )
    return _insert_brick(
        session,
        actor=actor,
        scope=BRICK_SCOPE_SYSTEM,
        code=code,
        title=title,
        value_type=value_type,
        source=source,
        rules=rules,
        place_id=None,
        owner_user_id=None,
    )


# --- Изменение, архив и восстановление личных кирпичиков -------------------


def _owned_personal_brick(
    session: Session,
    actor: User,
    brick_id: int,
) -> Brick | None:
    """Строка только личного кирпичика этого пользователя, без утечки чужого."""
    if isinstance(brick_id, bool) or not isinstance(brick_id, int) or brick_id <= 0:
        return None
    return session.scalar(
        select(Brick).where(
            Brick.id == brick_id,
            Brick.scope == BRICK_SCOPE_PERSONAL,
            Brick.owner_user_id == actor.id,
        )
    )


def _mutation(
    status: str,
    row: Brick | None,
    message: str | None = None,
) -> BrickMutationResult:
    return BrickMutationResult(
        status=status,
        brick=_snapshot(row) if row is not None else None,
        message=message,
    )


def _reload_brick(session: Session, brick_id: int) -> Brick | None:
    session.expire_all()
    return session.get(Brick, brick_id)


def _commit_conditional_brick_update(
    session: Session,
    *,
    brick_id: int,
    actor_id: int,
    expected_revision: int,
    values: Mapping[str, object],
    required_status: str,
) -> bool:
    """
    Условный UPDATE с проверкой владельца, статуса и ревизии.

    Сравнение и запись выполняются одним SQL-оператором, поэтому два
    конкурентных редактора не могут оба получить rowcount=1. Коммит — одна
    транзакция; ошибка SQLite откатывает её целиком.
    """
    statement = (
        sqlalchemy_update(Brick)
        .where(
            Brick.id == brick_id,
            Brick.scope == BRICK_SCOPE_PERSONAL,
            Brick.owner_user_id == actor_id,
            Brick.status == required_status,
            Brick.revision == expected_revision,
        )
        .values(
            **values,
            revision=expected_revision + 1,
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    result = session.execute(statement)
    if getattr(result, "rowcount", 0) != 1:
        session.rollback()
        return False
    session.commit()
    return True


def update_personal_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    brick_id: int,
    expected_revision: int,
    code: object,
    title: object,
    rules: Mapping[str, object] | str | None,
) -> BrickMutationResult:
    """
    Меняет название, код и допустимые настройки личного кирпичика.

    Тип не является параметром и не может измениться. `brick_id` сохраняется.
    При несовпадении ревизии возвращается `stale`, при занятом коде —
    `conflict`; обе операции не пишут частичных данных.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _owned_personal_brick(session, actor, brick_id)
    if row is None:
        return _mutation(
            "not_found",
            None,
            "Кирпичик не найден. Возможно, он уже удалён или принадлежит другому.",
        )
    if row.status != BRICK_STATUS_ACTIVE:
        return _mutation(
            "archived",
            row,
            "Архивный кирпичик нельзя редактировать. Сначала восстановите его.",
        )
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision <= 0
        or row.revision != expected_revision
    ):
        return _mutation(
            "stale",
            row,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )

    canonical_code = validate_brick_code(code)
    cleaned_title = clean_brick_title(title)
    validated_rules = parse_rules(row.value_type, rules)
    availability = check_code_availability(
        session,
        canonical_code,
        scope=BRICK_SCOPE_PERSONAL,
        owner_user_id=actor.id,
        exclude_brick_id=row.id,
    )
    if not availability.available:
        return _mutation(
            "conflict",
            row,
            availability.message or "Код уже используется.",
        )

    values = {
        "code": canonical_code,
        "title": cleaned_title,
        "rules_json": rules_to_json(row.value_type, validated_rules),
    }
    try:
        updated = _commit_conditional_brick_update(
            session,
            brick_id=row.id,
            actor_id=actor.id,
            expected_revision=expected_revision,
            values=values,
            required_status=BRICK_STATUS_ACTIVE,
        )
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            _reload_brick(session, brick_id),
            "Не удалось сохранить изменения: код занят или одновременная "
            "запись не завершилась. Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    if not updated:
        current = _reload_brick(session, brick_id)
        return _mutation(
            "stale",
            current,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )
    current = _reload_brick(session, brick_id)
    return _mutation("updated", current, "Изменения сохранены.")


def archive_personal_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    brick_id: int,
    expected_revision: int,
) -> BrickMutationResult:
    """
    Мягко архивирует личный кирпичик владельца.

    Строка не удаляется, ID и код не меняются. Архивный код остаётся занятым,
    поэтому восстановление однозначно. Системный/чужой ID даёт `not_found`.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _owned_personal_brick(session, actor, brick_id)
    if row is None:
        return _mutation(
            "not_found",
            None,
            "Кирпичик не найден или принадлежит другому пользователю.",
        )
    if row.status == BRICK_STATUS_ARCHIVED:
        return _mutation("already_archived", row, "Кирпичик уже в архиве.")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision <= 0
        or row.revision != expected_revision
    ):
        return _mutation(
            "stale",
            row,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )
    try:
        updated = _commit_conditional_brick_update(
            session,
            brick_id=row.id,
            actor_id=actor.id,
            expected_revision=expected_revision,
            values={"status": BRICK_STATUS_ARCHIVED},
            required_status=BRICK_STATUS_ACTIVE,
        )
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            _reload_brick(session, brick_id),
            "Не удалось переместить кирпичик в архив из-за ошибки базы. "
            "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    if not updated:
        current = _reload_brick(session, brick_id)
        if current is not None and current.status == BRICK_STATUS_ARCHIVED:
            return _mutation("already_archived", current, "Кирпичик уже в архиве.")
        return _mutation(
            "stale",
            current,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )
    current = _reload_brick(session, brick_id)
    return _mutation("archived", current, "Кирпичик перемещён в архив.")


def restore_personal_brick(
    session: Session,
    *,
    actor_telegram_user_id: int,
    brick_id: int,
    expected_revision: int,
) -> BrickMutationResult:
    """
    Восстанавливает архивный личный кирпичик владельца.

    Код не переприсваивается и остаётся тем же: пока кирпичик был в архиве,
    код был занят, поэтому конфликт с новым кирпичиком невозможен.
    """
    actor = _require_actor_user(session, actor_telegram_user_id)
    row = _owned_personal_brick(session, actor, brick_id)
    if row is None:
        return _mutation(
            "not_found",
            None,
            "Кирпичик не найден или принадлежит другому пользователю.",
        )
    if row.status == BRICK_STATUS_ACTIVE:
        return _mutation("already_active", row, "Кирпичик уже активен.")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision <= 0
        or row.revision != expected_revision
    ):
        return _mutation(
            "stale",
            row,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )
    try:
        updated = _commit_conditional_brick_update(
            session,
            brick_id=row.id,
            actor_id=actor.id,
            expected_revision=expected_revision,
            values={"status": BRICK_STATUS_ACTIVE},
            required_status=BRICK_STATUS_ARCHIVED,
        )
    except (IntegrityError, OperationalError):
        session.rollback()
        return _mutation(
            "conflict",
            _reload_brick(session, brick_id),
            "Не удалось восстановить кирпичик из-за ошибки базы. " "Повторите попытку.",
        )
    except Exception:
        session.rollback()
        raise
    if not updated:
        current = _reload_brick(session, brick_id)
        if current is not None and current.status == BRICK_STATUS_ACTIVE:
            return _mutation("already_active", current, "Кирпичик уже активен.")
        return _mutation(
            "stale",
            current,
            "Кирпичик уже изменился в другом окне. Откройте его заново.",
        )
    current = _reload_brick(session, brick_id)
    return _mutation("restored", current, "Кирпичик восстановлен.")


# --- Источник значения и пять состояний -------------------------------------


def _missing(message: str) -> BrickValueResult:
    return BrickValueResult(BrickValueStatus.MISSING, error=message)


def _needs_input(message: str) -> BrickValueResult:
    return BrickValueResult(BrickValueStatus.NEEDS_INPUT, error=message)


def resolve_brick_value(
    session: Session,
    brick: BrickDefinition,
    *,
    raw_value: object = _UNSET,
    allow_empty: bool = False,
    place_id: int | None = None,
    selected_place_name_id: int | None = None,
    context_values: Mapping[str, object] | None = None,
    today: date | None = None,
) -> BrickValueResult:
    """
    Определяет состояние значения по источнику кирпичика.

    - явно переданное `raw_value` проверяется как ввод (VALID/INVALID/
      EMPTY_OPTIONAL);
    - `ask` без значения — NEEDS_INPUT;
    - `place` берёт название/адрес/ссылку выбранного места; отсутствие поля —
      MISSING, а не пустая строка и не повторный вопрос о заведомо известном;
    - `context` берёт значение из словаря по коду; отсутствие — MISSING.

    Выбранное альтернативное название места используется для кода `место` при
    сохранении того же `place_id`. Для `адрес_места` и `ссылка_места` данные
    берутся из справочника. Фотографии к этому контракту не относятся.
    """
    if raw_value is not _UNSET:
        return validate_brick_value(
            brick.value_type,
            raw_value,
            rules=brick.rules,
            allow_empty=allow_empty,
            today=today,
        )

    if brick.source == BRICK_SOURCE_ASK:
        return _needs_input("Значение нужно запросить у пользователя.")

    if brick.source == BRICK_SOURCE_CONTEXT:
        if context_values is None or brick.code not in context_values:
            return _missing("Значение отсутствует в системном контексте.")
        context_value = context_values[brick.code]
        if context_value is None or (
            isinstance(context_value, str) and not context_value.strip()
        ):
            if allow_empty:
                return BrickValueResult(BrickValueStatus.EMPTY_OPTIONAL)
            return _missing("Значение в системном контексте пусто.")
        return validate_brick_value(
            brick.value_type,
            context_value,
            rules=brick.rules,
            allow_empty=allow_empty,
            today=today,
        )

    if brick.source != BRICK_SOURCE_PLACE:
        return BrickValueResult(
            BrickValueStatus.INVALID,
            error=f"Неизвестный источник значения: {brick.source!r}.",
        )

    if place_id is None:
        return _missing("Место ещё не выбрано.")
    details = get_place_details(session, place_id)
    if details is None:
        return _missing("Выбранное место не найдено в справочнике.")

    field = _PLACE_FIELDS.get(brick.code)
    if field is None:
        return _missing("Для этого кирпичика не определено поле выбранного места.")

    place_value: object
    if field == "name":
        place_value = details.canonical_name
        if selected_place_name_id is not None:
            selected = get_place_name(session, place_id, selected_place_name_id)
            if selected is None:
                return BrickValueResult(
                    BrickValueStatus.INVALID,
                    error="Выбранное название не принадлежит этому месту.",
                )
            place_value = selected.name
    elif field == "address":
        place_value = details.address
    else:
        place_value = details.url

    if place_value is None or (
        isinstance(place_value, str) and not place_value.strip()
    ):
        return _missing("В справочнике места это значение не заполнено.")
    result = validate_brick_value(
        brick.value_type,
        place_value,
        rules=brick.rules,
        allow_empty=False,
        today=today,
    )
    if result.status is BrickValueStatus.INVALID:
        return BrickValueResult(
            BrickValueStatus.INVALID,
            error="Значение в справочнике места некорректно для этого типа.",
        )
    return result


def system_definition_codes() -> tuple[str, ...]:
    """Публичный стабильный список системных кодов Stage 22A."""
    return SYSTEM_BRICK_CODES


def system_definitions() -> tuple[SystemBrickDefinition, ...]:
    """Публичное неизменяемое описание сидированных определений."""
    return SYSTEM_BRICKS


def forbidden_brick_codes() -> frozenset[str]:
    """Коды, которые не могут стать токенами шаблона (фотографии)."""
    return FORBIDDEN_BRICK_CODES
