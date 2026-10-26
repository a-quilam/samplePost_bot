# announcement_store.py
"""
Хранение и сервисные операции адаптивного мастера анонсов Stage 27A.

Сервис не зависит от Telegram и работает с временной/рабочей SQLite через
обычную SQLAlchemy-сессию. Он реализует:

- создание анонса с местом и выбранным вариантом названия;
- подтверждение места и независимый снимок стандартного/личного шаблона;
- значения по постоянным `brick_id` и различие «нет значения» /
  «допустимое пустое значение»;
- дополнительные кирпичики только текущего анонса;
- optimistic locking по `revision` и атомарные транзакции;
- восстановление состояния после рестарта и изоляцию по автору.

Старые `drafts`, WIP, фотографии и альбомы не читаются и не меняются.
Публикация и Telegram-интерфейс нового мастера — не границы Stage 27A.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from announcement_wizard import (
    EXTRA_POSITION_END,
    EXTRA_POSITIONS,
    SECTION_ORDER,
    AnnouncementActorError,
    AnnouncementBrickError,
    AnnouncementConflictError,
    AnnouncementError,
    AnnouncementNotFoundError,
    AnnouncementSnapshotError,
    AnnouncementStageError,
    AnnouncementTemplateSnapshot,
    AnnouncementValueError,
    Question,
    SnapshotBinding,
    WizardStage,
    answer_payload_to_raw,
    build_questions,
    place_context_values,
    snapshot_tokens_by_section,
    visible_brick_issue,
)
from brick_store import BrickDefinition, get_visible_brick
from brick_types import (
    BrickValueResult,
    BrickValueStatus,
    validate_brick_value,
)
from models import (
    AnnouncementDraft,
    AnnouncementExtra,
    AnnouncementValue,
    Brick,
    Place,
    User,
    utcnow,
)
from place_store import get_place_details, get_place_name
from template_parser import BrickNode
from template_store import (
    TemplateError,
    TemplateNotFoundError,
    TemplatePermissionError,
    TemplateSnapshotDetails,
    create_template_snapshot,
    get_accessible_template,
    get_template_snapshot,
    validate_template_compatibility,
)
from template_types import (
    TEMPLATE_KIND_PERSONAL,
    TEMPLATE_KIND_STANDARD,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)

_UNSET = object()


@dataclass(frozen=True)
class AnnouncementValueDetails:
    """Значение одного brick_id в черновике."""

    brick_id: int
    status: BrickValueStatus
    payload: Mapping[str, object] | None
    updated_at: datetime

    @property
    def is_empty_optional(self) -> bool:
        return self.status is BrickValueStatus.EMPTY_OPTIONAL


@dataclass(frozen=True)
class AnnouncementExtraDetails:
    """Дополнительный кирпичик конкретного анонса."""

    extra_id: int
    brick_id: int
    code: str
    title: str
    value_type: str
    section: str
    position: str
    occurrence: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AnnouncementDraftDetails:
    """Полное состояние незавершённого анонса для восстановления."""

    draft_id: int
    author_user_id: int
    place_id: int
    place_name_id: int | None
    place_display_name: str
    place_address: str | None
    place_url: str | None
    template_id: int | None
    template_version: int | None
    template_snapshot: AnnouncementTemplateSnapshot | None
    stage: WizardStage
    revision: int
    values: tuple[AnnouncementValueDetails, ...] = field(default_factory=tuple)
    extras: tuple[AnnouncementExtraDetails, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def value_for(self, brick_id: int) -> AnnouncementValueDetails | None:
        return next(
            (value for value in self.values if value.brick_id == brick_id), None
        )

    @property
    def value_map(self) -> dict[int, AnnouncementValueDetails]:
        return {value.brick_id: value for value in self.values}


@dataclass(frozen=True)
class AnnouncementRenderPayload:
    """
    Безопасные данные для компилятора Stage 24.

    Токены уже связаны с brick_id из снимка, дополнительные кирпичики
    вставлены только в представление этого анонса, а определения загружены
    по постоянным ID. Никакого повторного разрешения `[код]` нет.
    """

    tokens_by_section: Mapping[str, tuple[Any, ...]]
    values_by_id: Mapping[int, object]
    definitions_by_id: Mapping[int, BrickDefinition]


def _valid_positive_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _require_actor(session: Session, telegram_user_id: int | None) -> User:
    if not _valid_positive_id(telegram_user_id):
        raise AnnouncementActorError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    assert isinstance(telegram_user_id, int)
    actor = session.scalar(
        select(User).where(User.telegram_user_id == telegram_user_id)
    )
    if actor is None:
        raise AnnouncementActorError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _validated_key(value: object | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AnnouncementError("Ключ идемпотентности должен быть непустой строкой.")
    return value


def _load_owned_draft(
    session: Session,
    actor: User,
    draft_id: int,
) -> AnnouncementDraft:
    if not _valid_positive_id(draft_id):
        raise AnnouncementNotFoundError("Черновик не найден.")
    row = session.scalar(
        select(AnnouncementDraft)
        .where(
            AnnouncementDraft.id == draft_id,
            AnnouncementDraft.author_user_id == actor.id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        # Чужой ID неотличим от несуществующего: состояние и автор не
        # раскрываются, подменить ID и получить чужой анонс нельзя.
        raise AnnouncementNotFoundError("Черновик не найден.")
    return row


def _expected_revision(row: AnnouncementDraft, expected: int | None) -> int:
    if expected is None:
        return row.revision
    if not _valid_positive_id(expected):
        raise AnnouncementError("expected_revision должен быть положительным числом.")
    assert isinstance(expected, int)
    if expected != row.revision:
        raise AnnouncementConflictError(
            "Черновик уже изменился. Перечитайте состояние и повторите."
        )
    return expected


def _cas_update_draft(
    session: Session,
    *,
    draft_id: int,
    revision: int,
    fields: Mapping[str, object] | None = None,
) -> None:
    values: dict[str, object] = dict(fields or {})
    values["revision"] = revision + 1
    values["updated_at"] = utcnow()
    result = session.execute(
        sqlalchemy_update(AnnouncementDraft)
        .where(
            AnnouncementDraft.id == draft_id,
            AnnouncementDraft.revision == revision,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if getattr(result, "rowcount", 0) != 1:
        session.rollback()
        raise AnnouncementConflictError(
            "Черновик уже изменился. Перечитайте состояние и повторите."
        )


def _snapshot_view(details: TemplateSnapshotDetails) -> AnnouncementTemplateSnapshot:
    return AnnouncementTemplateSnapshot(
        snapshot_id=details.snapshot_id,
        source_template_id=details.source_template_id,
        source_template_version=details.source_template_version,
        source_kind=details.source_kind,
        source_place_id=details.source_place_id,
        sections={
            TEMPLATE_SECTION_HEADER: details.header,
            TEMPLATE_SECTION_BODY: details.body,
            TEMPLATE_SECTION_FOOTER: details.footer,
        },
        bindings=tuple(
            SnapshotBinding(brick_id=binding.brick_id, code=binding.code)
            for binding in details.bindings
        ),
    )


def _draft_snapshot(
    session: Session,
    actor: User,
    row: AnnouncementDraft,
) -> AnnouncementTemplateSnapshot | None:
    if row.template_snapshot_id is None:
        return None
    details = get_template_snapshot(
        session, actor.telegram_user_id, row.template_snapshot_id
    )
    if details is None:
        raise AnnouncementSnapshotError(
            "Независимый снимок шаблона не найден или недоступен автору."
        )
    return _snapshot_view(details)


def _decode_value(row: AnnouncementValue) -> AnnouncementValueDetails:
    if row.state == "empty_optional":
        return AnnouncementValueDetails(
            brick_id=row.brick_id,
            status=BrickValueStatus.EMPTY_OPTIONAL,
            payload=None,
            updated_at=row.updated_at,
        )
    if row.state != "valid" or row.value_json is None:
        raise AnnouncementSnapshotError(
            f"Значение кирпичика #{row.brick_id} имеет неизвестное состояние."
        )
    try:
        payload = json.loads(row.value_json)
    except (TypeError, ValueError) as exc:
        raise AnnouncementSnapshotError(
            f"Значение кирпичика #{row.brick_id} содержит битый JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise AnnouncementSnapshotError(
            f"Значение кирпичика #{row.brick_id} должно быть JSON-объектом."
        )
    return AnnouncementValueDetails(
        brick_id=row.brick_id,
        status=BrickValueStatus.VALID,
        payload=payload,
        updated_at=row.updated_at,
    )


def _extra_order(row: AnnouncementExtra) -> tuple[int, int, int]:
    section_index = SECTION_ORDER.index(row.section)
    position_index = 0 if row.position == "start" else 1
    return (section_index, position_index, row.id)


def _extra_details(
    session: Session,
    rows: list[AnnouncementExtra],
) -> tuple[AnnouncementExtraDetails, ...]:
    if not rows:
        return ()
    brick_ids = {row.brick_id for row in rows}
    bricks = {
        brick.id: brick
        for brick in session.scalars(select(Brick).where(Brick.id.in_(brick_ids)))
    }
    details: list[AnnouncementExtraDetails] = []
    for row in sorted(rows, key=_extra_order):
        brick = bricks.get(row.brick_id)
        if brick is None:
            raise AnnouncementBrickError(
                f"Определение дополнительного кирпичика #{row.brick_id} не найдено."
            )
        details.append(
            AnnouncementExtraDetails(
                extra_id=row.id,
                brick_id=row.brick_id,
                code=brick.code,
                title=brick.title,
                value_type=brick.value_type,
                section=row.section,
                position=row.position,
                occurrence=row.occurrence,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return tuple(details)


def _draft_details(
    session: Session,
    actor: User,
    row: AnnouncementDraft,
) -> AnnouncementDraftDetails:
    value_rows = list(
        session.scalars(
            select(AnnouncementValue)
            .where(AnnouncementValue.draft_id == row.id)
            .order_by(AnnouncementValue.brick_id)
        ).all()
    )
    extra_rows = list(
        session.scalars(
            select(AnnouncementExtra).where(AnnouncementExtra.draft_id == row.id)
        ).all()
    )
    return AnnouncementDraftDetails(
        draft_id=row.id,
        author_user_id=row.author_user_id,
        place_id=row.place_id,
        place_name_id=row.place_name_id,
        place_display_name=row.place_display_name,
        place_address=row.place_address,
        place_url=row.place_url,
        template_id=row.template_id,
        template_version=row.template_version,
        template_snapshot=_draft_snapshot(session, actor, row),
        stage=WizardStage(row.stage),
        revision=row.revision,
        values=tuple(_decode_value(value) for value in value_rows),
        extras=_extra_details(session, extra_rows),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _details_by_id(
    session: Session,
    actor: User,
    draft_id: int,
) -> AnnouncementDraftDetails:
    row = _load_owned_draft(session, actor, draft_id)
    return _draft_details(session, actor, row)


def _active_definition(
    session: Session,
    *,
    actor: User,
    place_id: int,
    brick_id: int,
) -> BrickDefinition:
    definition = get_visible_brick(
        session,
        brick_id,
        viewer_telegram_user_id=actor.telegram_user_id,
        place_id=place_id,
        include_archived=True,
    )
    if definition is None:
        raise AnnouncementBrickError(
            f"Кирпичик #{brick_id} недоступен для этого пользователя/места."
        )
    issue = visible_brick_issue(
        definition,
        place_id=place_id,
        owner_user_id=actor.id,
    )
    if issue is not None:
        raise AnnouncementBrickError(issue)
    return definition


def _encode_value(
    definition: BrickDefinition,
    raw: object,
    *,
    today: date | None,
) -> tuple[str, str | None]:
    result = validate_brick_value(
        definition.value_type,
        raw,
        rules=definition.rules,
        allow_empty=True,
        today=today,
    )
    if result.status is BrickValueStatus.VALID:
        payload = result.payload or {}
        return (
            "valid",
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
    if result.status is BrickValueStatus.EMPTY_OPTIONAL:
        return ("empty_optional", None)
    raise AnnouncementValueError(
        f"Значение кирпичика «{definition.code}» некорректно: "
        f"{result.error or 'неизвестная ошибка'}"
    )


def _upsert_value(
    session: Session,
    *,
    draft_id: int,
    brick_id: int,
    state: str,
    value_json: str | None,
    now: datetime,
) -> None:
    statement = sqlite_insert(AnnouncementValue).values(
        draft_id=draft_id,
        brick_id=brick_id,
        state=state,
        value_json=value_json,
        created_at=now,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            AnnouncementValue.draft_id,
            AnnouncementValue.brick_id,
        ],
        set_={
            "state": state,
            "value_json": value_json,
            "updated_at": now,
        },
    )
    session.execute(statement)


def _commit(session: Session) -> None:
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось сохранить изменения из-за одновременной записи. "
            "Повторите попытку."
        ) from exc


# --- Создание и место -------------------------------------------------------


def start_announcement_wizard(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    place_id: int,
    place_name_id: int | None = None,
    idempotency_key: object | None = None,
) -> AnnouncementDraftDetails:
    """
    Создаёт состояние нового мастера с выбранным местом.

    Место выбирается первым. `place_name_id=None` означает каноническое
    название; альтернативное сохраняется отдельно и не создаёт новое место.
    Адрес/ссылка копируются из справочника, отсутствие остаётся NULL.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(place_id):
        raise AnnouncementNotFoundError("Некорректный идентификатор места.")
    assert isinstance(place_id, int)
    key = _validated_key(idempotency_key)

    place = session.get(Place, place_id)
    if place is None:
        raise AnnouncementNotFoundError("Место не найдено.")
    details = get_place_details(session, place_id)
    if details is None:
        raise AnnouncementNotFoundError("У места нет корректного названия.")

    if place_name_id is None:
        resolved_name_id = details.canonical_name_id
        display_name = details.canonical_name
    else:
        if not _valid_positive_id(place_name_id):
            raise AnnouncementNotFoundError("Некорректный вариант названия места.")
        assert isinstance(place_name_id, int)
        name = get_place_name(session, place_id, place_name_id)
        if name is None:
            raise AnnouncementNotFoundError(
                "Выбранное название не принадлежит этому месту."
            )
        resolved_name_id = name.id
        display_name = name.name

    if key is not None:
        existing = session.scalar(
            select(AnnouncementDraft).where(
                AnnouncementDraft.author_user_id == actor.id,
                AnnouncementDraft.idempotency_key == key,
            )
        )
        if existing is not None:
            if (
                existing.place_id != place_id
                or existing.place_name_id != resolved_name_id
            ):
                raise AnnouncementConflictError(
                    "Ключ идемпотентности уже использован для другого выбора."
                )
            return _draft_details(session, actor, existing)

    now = utcnow()
    row = AnnouncementDraft(
        author_user_id=actor.id,
        place_id=place_id,
        place_name_id=resolved_name_id,
        place_display_name=display_name,
        place_address=_optional_text(details.address),
        place_url=_optional_text(details.url),
        template_id=None,
        template_version=None,
        template_snapshot_id=None,
        stage=WizardStage.PLACE.value,
        revision=1,
        idempotency_key=key,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        if key is not None:
            existing = session.scalar(
                select(AnnouncementDraft).where(
                    AnnouncementDraft.author_user_id == actor.id,
                    AnnouncementDraft.idempotency_key == key,
                )
            )
            if existing is not None:
                return _draft_details(session, actor, existing)
        raise AnnouncementConflictError(
            "Не удалось создать черновик из-за одновременной записи."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def confirm_announcement_place(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """
    Подтверждает место и переходит к выбору шаблона.

    Название/адрес/ссылка перечитываются из справочника в момент
    подтверждения. Пустые сведения остаются NULL и не превращаются в
    фиктивные значения.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)

    if row.stage in {
        WizardStage.TEMPLATE.value,
        WizardStage.QUESTIONS.value,
        WizardStage.EXTRAS.value,
        WizardStage.PREVIEW.value,
        WizardStage.COMPLETED.value,
    }:
        return _draft_details(session, actor, row)
    if row.stage != WizardStage.PLACE.value:
        raise AnnouncementStageError("Неизвестный этап адаптивного мастера.")

    details = get_place_details(session, row.place_id)
    if details is None:
        raise AnnouncementStageError("Выбранное место больше не доступно.")
    display_name = row.place_display_name
    if row.place_name_id is not None:
        name = get_place_name(session, row.place_id, row.place_name_id)
        if name is None:
            raise AnnouncementStageError(
                "Выбранное название места больше не принадлежит этому месту."
            )
        display_name = name.name

    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={
                "stage": WizardStage.TEMPLATE.value,
                "place_display_name": display_name,
                "place_address": _optional_text(details.address),
                "place_url": _optional_text(details.url),
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def _template_for_selection(
    session: Session,
    *,
    actor: User,
    draft: AnnouncementDraft,
    template_id: int,
) -> Any:
    template = get_accessible_template(session, actor.telegram_user_id, template_id)
    if template is None:
        raise AnnouncementNotFoundError("Шаблон не найден или недоступен.")
    if template.kind == TEMPLATE_KIND_STANDARD:
        if template.place_id != draft.place_id:
            raise AnnouncementStageError(
                "Стандартный шаблон относится к другому месту."
            )
    elif template.kind == TEMPLATE_KIND_PERSONAL:
        if template.owner_user_id != actor.id:
            # Чужой личный шаблон неотличим от несуществующего.
            raise AnnouncementNotFoundError("Шаблон не найден или недоступен.")
    else:
        raise AnnouncementStageError("Неизвестный вид шаблона.")
    return template


def choose_announcement_template(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    template_id: int,
    expected_revision: int | None = None,
    expected_template_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """
    Фиксирует независимый снимок выбранной версии шаблона.

    Стандартный шаблон должен относиться к выбранному месту; личный —
    принадлежать автору. Чужой личный шаблон неотличим от несуществующего.
    Повторный выбор другого шаблона запрещён, а тот же выбор идемпотентен.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(template_id):
        raise AnnouncementNotFoundError("Некорректный идентификатор шаблона.")
    assert isinstance(template_id, int)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)

    if row.template_snapshot_id is not None:
        if row.template_id == template_id:
            return _draft_details(session, actor, row)
        raise AnnouncementConflictError(
            "Шаблон уже зафиксирован в этом анонсе и не может быть заменён."
        )
    if row.stage == WizardStage.PLACE.value:
        raise AnnouncementStageError("Сначала подтвердите выбранное место.")

    try:
        template = _template_for_selection(
            session,
            actor=actor,
            draft=row,
            template_id=template_id,
        )
        compatibility = validate_template_compatibility(
            session,
            actor_telegram_user_id=actor.telegram_user_id,
            template_id=template_id,
            selected_place_id=row.place_id,
        )
        if not compatibility.compatible:
            raise AnnouncementBrickError(
                "Шаблон несовместим с выбранным местом: "
                + "; ".join(compatibility.issues)
            )
        snapshot = create_template_snapshot(
            session,
            actor_telegram_user_id=actor.telegram_user_id,
            template_id=template_id,
            expected_revision=expected_template_revision,
            idempotency_key=f"announcement-wizard:{row.id}",
            commit=False,
        )
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={
                "template_id": template.template_id,
                "template_version": template.current_version.version,
                "template_snapshot_id": snapshot.snapshot_id,
                "stage": WizardStage.QUESTIONS.value,
            },
        )
        _commit(session)
    except AnnouncementError:
        session.rollback()
        raise
    except TemplateNotFoundError as exc:
        session.rollback()
        raise AnnouncementNotFoundError(str(exc)) from exc
    except TemplatePermissionError as exc:
        session.rollback()
        raise AnnouncementNotFoundError(str(exc)) from exc
    except TemplateError as exc:
        session.rollback()
        raise AnnouncementConflictError(str(exc)) from exc
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось зафиксировать шаблон из-за одновременной записи."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


# --- Значения ---------------------------------------------------------------


def _allowed_brick_ids(
    session: Session,
    row: AnnouncementDraft,
    snapshot: AnnouncementTemplateSnapshot,
) -> set[int]:
    extras = session.scalars(
        select(AnnouncementExtra.brick_id).where(AnnouncementExtra.draft_id == row.id)
    ).all()
    return set(snapshot.brick_ids) | {int(brick_id) for brick_id in extras}


def save_announcement_value(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    brick_id: int,
    value: object,
    expected_revision: int | None = None,
    today: date | None = None,
) -> AnnouncementDraftDetails:
    """
    Сохраняет значение кирпичика снимка или дополнительного кирпичика.

    Пустой ввод разрешает только явно необязательное значение: оно хранится
    как `empty_optional`, а отсутствие строки остаётся «не задано».
    Невалидное значение откатывается целиком, включая ревизию.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(brick_id):
        raise AnnouncementBrickError("Некорректный идентификатор кирпичика.")
    assert isinstance(brick_id, int)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    snapshot = _draft_snapshot(session, actor, row)
    if snapshot is None:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    if brick_id not in _allowed_brick_ids(session, row, snapshot):
        raise AnnouncementBrickError(
            f"Кирпичик #{brick_id} не входит в выбранный шаблон или "
            "дополнительные поля этого анонса."
        )
    definition = _active_definition(
        session,
        actor=actor,
        place_id=row.place_id,
        brick_id=brick_id,
    )
    state, value_json = _encode_value(definition, value, today=today)
    now = utcnow()
    try:
        _cas_update_draft(session, draft_id=row.id, revision=revision)
        _upsert_value(
            session,
            draft_id=row.id,
            brick_id=brick_id,
            state=state,
            value_json=value_json,
            now=now,
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def clear_announcement_value(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    brick_id: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Удаляет значение: кирпичик снова становится вопросом, если он нужен."""
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(brick_id):
        raise AnnouncementBrickError("Некорректный идентификатор кирпичика.")
    assert isinstance(brick_id, int)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    existing = session.scalar(
        select(AnnouncementValue).where(
            AnnouncementValue.draft_id == row.id,
            AnnouncementValue.brick_id == brick_id,
        )
    )
    if existing is None:
        return _draft_details(session, actor, row)
    try:
        _cas_update_draft(session, draft_id=row.id, revision=revision)
        session.execute(
            delete(AnnouncementValue).where(AnnouncementValue.id == existing.id)
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


# --- Дополнительные кирпичики ----------------------------------------------


def _validate_section_position(section: object, position: object) -> tuple[str, str]:
    if section not in SECTION_ORDER:
        raise AnnouncementError(
            "Секция дополнительного кирпичика: header, body или footer."
        )
    if position not in EXTRA_POSITIONS:
        raise AnnouncementError("Позиция дополнительного кирпичика: start или end.")
    return str(section), str(position)


def add_announcement_extra(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    brick_id: int,
    section: object,
    position: object = EXTRA_POSITION_END,
    value: object = _UNSET,
    expected_revision: int | None = None,
    today: date | None = None,
) -> AnnouncementDraftDetails:
    """
    Добавляет дополнительный кирпичик только в текущий анонс.

    Исходный шаблон/снимок не меняются. Кирпичик должен быть доступен
    пользователю в выбранном месте, отсутствовать в снимке и ещё не быть
    добавленным. Значение можно передать сразу — одной транзакцией.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(brick_id):
        raise AnnouncementBrickError("Некорректный идентификатор кирпичика.")
    assert isinstance(brick_id, int)
    checked_section, checked_position = _validate_section_position(section, position)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    snapshot = _draft_snapshot(session, actor, row)
    if snapshot is None:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    if brick_id in set(snapshot.brick_ids):
        raise AnnouncementBrickError("Этот кирпичик уже входит в выбранный шаблон.")
    existing = session.scalar(
        select(AnnouncementExtra).where(
            AnnouncementExtra.draft_id == row.id,
            AnnouncementExtra.brick_id == brick_id,
            AnnouncementExtra.occurrence == 1,
        )
    )
    if existing is not None:
        raise AnnouncementBrickError(
            "Этот кирпичик уже добавлен в дополнительные поля анонса."
        )
    definition = _active_definition(
        session,
        actor=actor,
        place_id=row.place_id,
        brick_id=brick_id,
    )
    state: str | None = None
    value_json: str | None = None
    if value is not _UNSET:
        state, value_json = _encode_value(definition, value, today=today)
    now = utcnow()
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={"stage": WizardStage.EXTRAS.value},
        )
        session.add(
            AnnouncementExtra(
                draft_id=row.id,
                brick_id=brick_id,
                section=checked_section,
                position=checked_position,
                occurrence=1,
                created_at=now,
                updated_at=now,
            )
        )
        if state is not None:
            _upsert_value(
                session,
                draft_id=row.id,
                brick_id=brick_id,
                state=state,
                value_json=value_json,
                now=now,
            )
        _commit(session)
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise AnnouncementConflictError(
            "Не удалось добавить кирпичик из-за одновременной записи."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def move_announcement_extra(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    extra_id: int,
    section: object | None = None,
    position: object | None = None,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Меняет секцию/позицию дополнительного кирпичика этого анонса."""
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(extra_id):
        raise AnnouncementNotFoundError("Дополнительный кирпичик не найден.")
    assert isinstance(extra_id, int)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    extra = session.scalar(
        select(AnnouncementExtra).where(
            AnnouncementExtra.id == extra_id,
            AnnouncementExtra.draft_id == row.id,
        )
    )
    if extra is None:
        raise AnnouncementNotFoundError("Дополнительный кирпичик не найден.")
    checked_section = extra.section if section is None else section
    checked_position = extra.position if position is None else position
    checked_section, checked_position = _validate_section_position(
        checked_section, checked_position
    )
    now = utcnow()
    try:
        _cas_update_draft(session, draft_id=row.id, revision=revision)
        extra.section = checked_section
        extra.position = checked_position
        extra.updated_at = now
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def remove_announcement_extra(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    extra_id: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Удаляет дополнительный кирпичик и его значение из этого анонса."""
    actor = _require_actor(session, actor_telegram_user_id)
    if not _valid_positive_id(extra_id):
        raise AnnouncementNotFoundError("Дополнительный кирпичик не найден.")
    assert isinstance(extra_id, int)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    extra = session.scalar(
        select(AnnouncementExtra).where(
            AnnouncementExtra.id == extra_id,
            AnnouncementExtra.draft_id == row.id,
        )
    )
    if extra is None:
        raise AnnouncementNotFoundError("Дополнительный кирпичик не найден.")
    try:
        _cas_update_draft(session, draft_id=row.id, revision=revision)
        session.execute(
            delete(AnnouncementExtra).where(AnnouncementExtra.id == extra.id)
        )
        session.execute(
            delete(AnnouncementValue).where(
                AnnouncementValue.draft_id == row.id,
                AnnouncementValue.brick_id == extra.brick_id,
            )
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def set_announcement_stage(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    stage: object,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Сохраняет текущий этап заполнения (включая возврат назад)."""
    actor = _require_actor(session, actor_telegram_user_id)
    if stage not in {item.value for item in WizardStage}:
        raise AnnouncementStageError("Неизвестный этап адаптивного мастера.")
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    if row.template_snapshot_id is None and stage not in {
        WizardStage.PLACE.value,
        WizardStage.TEMPLATE.value,
    }:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    if row.stage == str(stage):
        return _draft_details(session, actor, row)
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={"stage": str(stage)},
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


# --- Чтение и восстановление ------------------------------------------------


def get_announcement_draft(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
) -> AnnouncementDraftDetails:
    """Читает черновик только его автору; чужой ID даёт NotFound."""
    actor = _require_actor(session, actor_telegram_user_id)
    return _details_by_id(session, actor, draft_id)


def list_announcement_drafts(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
) -> list[AnnouncementDraftDetails]:
    """Черновики только текущего автора, свежие сверху."""
    actor = _require_actor(session, actor_telegram_user_id)
    rows = list(
        session.scalars(
            select(AnnouncementDraft)
            .where(AnnouncementDraft.author_user_id == actor.id)
            .order_by(
                AnnouncementDraft.updated_at.desc(),
                AnnouncementDraft.id.desc(),
            )
        ).all()
    )
    return [_draft_details(session, actor, row) for row in rows]


# --- Вопросы и данные для Stage 24 -----------------------------------------


def build_announcement_questions(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    context_values: Mapping[int, object] | None = None,
    today: date | None = None,
) -> list[Question]:
    """
    Строит вопросы по независимому снимку текущего черновика.

    Определения загружаются по сохранённым brick_id; архивный/чужой/не
    относящийся к месту кирпичик даёт диагностируемую ошибку.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    snapshot = _draft_snapshot(session, actor, row)
    if snapshot is None:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    if context_values:
        for key in context_values:
            if not _valid_positive_id(key):
                raise AnnouncementError(
                    "Контекстные значения адресуются только по brick_id."
                )
    definitions = {
        brick_id: _active_definition(
            session,
            actor=actor,
            place_id=row.place_id,
            brick_id=brick_id,
        )
        for brick_id in snapshot.brick_ids
    }
    place_values = place_context_values(
        display_name=row.place_display_name,
        address=row.place_address,
        url=row.place_url,
    )
    answers = {
        value.brick_id: BrickValueResult(
            status=value.status,
            payload=dict(value.payload) if value.payload is not None else None,
        )
        for value in _draft_details(session, actor, row).values
    }
    return build_questions(
        snapshot,
        definitions,
        place_values=place_values,
        context_values=context_values,
        answers=answers,
        today=today,
    )


def build_announcement_render_payload(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
) -> AnnouncementRenderPayload:
    """
    Формирует связанные секции и значения для `template_compiler.render_template`.

    Дополнительные кирпичики вставляются в представление только этого анонса:
    начало/конец выбранной секции. Исходный снимок шаблона не переписывается.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    snapshot = _draft_snapshot(session, actor, row)
    if snapshot is None:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    details = _draft_details(session, actor, row)
    tokens_by_section = snapshot_tokens_by_section(snapshot)

    all_brick_ids = set(snapshot.brick_ids)
    all_brick_ids.update(extra.brick_id for extra in details.extras)
    definitions = {
        brick_id: _active_definition(
            session,
            actor=actor,
            place_id=row.place_id,
            brick_id=brick_id,
        )
        for brick_id in all_brick_ids
    }

    starts: dict[str, list[BrickNode]] = {section: [] for section in SECTION_ORDER}
    ends: dict[str, list[BrickNode]] = {section: [] for section in SECTION_ORDER}
    for extra in details.extras:
        definition = definitions[extra.brick_id]
        node = BrickNode(code=definition.code, brick_id=definition.id)
        target = starts if extra.position == "start" else ends
        target[extra.section].append(node)

    sections: dict[str, tuple[Any, ...]] = {}
    for section in SECTION_ORDER:
        base = list(tokens_by_section.get(section, []))
        combined = starts[section] + base + ends[section]
        if combined:
            sections[section] = tuple(combined)

    values_by_id: dict[int, object] = {}
    for value in details.values:
        raw = answer_payload_to_raw(
            BrickValueResult(
                status=value.status,
                payload=dict(value.payload) if value.payload is not None else None,
            )
        )
        if raw is not None:
            values_by_id[value.brick_id] = raw
    for brick_id, raw in place_context_values(
        display_name=row.place_display_name,
        address=row.place_address,
        url=row.place_url,
    ).items():
        values_by_id.setdefault(brick_id, raw)
    return AnnouncementRenderPayload(
        tokens_by_section=sections,
        values_by_id=values_by_id,
        definitions_by_id=definitions,
    )
