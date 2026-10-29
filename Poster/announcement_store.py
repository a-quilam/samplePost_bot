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
from collections.abc import Mapping, Sequence
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
    AnnouncementPublicationError,
    AnnouncementSnapshotError,
    AnnouncementStageError,
    AnnouncementTemplateSnapshot,
    AnnouncementValueError,
    Question,
    SnapshotBinding,
    WizardStage,
    answer_payload_to_raw,
    build_questions,
    evaluate_announcement_readiness,
    place_context_values,
    snapshot_brick_ids_by_code,
    snapshot_section,
    snapshot_tokens_by_section,
    visible_brick_issue,
)
from brick_store import BrickDefinition, get_visible_brick
from brick_types import (
    BrickValueResult,
    BrickValueStatus,
    validate_brick_value,
)
from delivery import EMPTY_CAPTION_TEXT
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
from template_compiler import (
    PLACEHOLDER_TEXT,
    check_announcement_length,
    render_announcement,
    render_template,
)
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

# Разумный предел коллекции: защищает сообщение/БД от бесконечного набора.
# Отправка при этом всё равно режется на медиагруппы по лимиту Telegram.
# Решение владельца от 2026-10-27 (S4 §5.3): максимум ровно 10 фото на
# один анонс. 11-е отклоняется, принятые не теряются.
MAX_ANNOUNCEMENT_PHOTOS = 10

PUBLICATION_STATUS_SENDING = "sending"
PUBLICATION_STATUS_PARTIAL = "partial"
PUBLICATION_STATUS_FAILED = "failed"
PUBLICATION_STATUS_SENT = "sent"
PUBLICATION_STATUSES = (
    PUBLICATION_STATUS_SENDING,
    PUBLICATION_STATUS_PARTIAL,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_SENT,
)
PUBLICATION_FROZEN_STATUSES = PUBLICATION_STATUSES

# Источник шаблона анонса (Poster 2.0 S1). Не путать с местом, выбранным
# позже в кирпичике «Место»: `origin` фиксируется при создании черновика.
ORIGIN_PLACE = "place"
ORIGIN_PERSONAL = "personal"
ORIGINS = (ORIGIN_PLACE, ORIGIN_PERSONAL)


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
    place_id: int | None
    place_name_id: int | None
    place_display_name: str | None
    place_address: str | None
    place_url: str | None
    # Poster 2.0 U1a: снимок города площадки; NULL — «сведений нет».
    place_city: str | None
    template_id: int | None
    template_version: int | None
    template_snapshot: AnnouncementTemplateSnapshot | None
    stage: WizardStage
    revision: int
    # Источник шаблона: 'place' — стандарт площадки (place_id обязателен),
    # 'personal' — личный шаблон без площадки в черновике.
    origin: str = ORIGIN_PLACE
    values: tuple[AnnouncementValueDetails, ...] = field(default_factory=tuple)
    extras: tuple[AnnouncementExtraDetails, ...] = field(default_factory=tuple)
    # Адреса всех сообщений живого медиаблока (альбом — несколько ID).
    live_message_ids: tuple[int, ...] = field(default_factory=tuple)
    # Отдельная коллекция вложений анонса. Фото не являются кирпичиками
    # и не участвуют в шаблонном тексте.
    photos: tuple[str, ...] = field(default_factory=tuple)
    publication_status: str | None = None
    publication_text: str | None = None
    publication_photos: tuple[str, ...] = field(default_factory=tuple)
    publication_progress: Mapping[str, Any] = field(default_factory=dict)
    publication_error: str | None = None
    publication_started_at: datetime | None = None
    published_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def is_personal(self) -> bool:
        """Анонс по личному шаблону: площадка в черновике не выбрана."""
        return self.origin == ORIGIN_PERSONAL

    def value_for(self, brick_id: int) -> AnnouncementValueDetails | None:
        return next(
            (value for value in self.values if value.brick_id == brick_id), None
        )

    @property
    def value_map(self) -> dict[int, AnnouncementValueDetails]:
        return {value.brick_id: value for value in self.values}

    @property
    def publication_locked(self) -> bool:
        """Идёт отправка: текст/фото/значения менять нельзя до её завершения."""
        return self.publication_status in PUBLICATION_FROZEN_STATUSES

    @property
    def publication_sent(self) -> bool:
        return self.publication_status == PUBLICATION_STATUS_SENT

    @property
    def delivered_message_ids(self) -> tuple[int, ...]:
        """
        message_id последней доставленной версии (ключ `ids` прогресса).

        Хранятся только в SQLite-прогрессе: новых колонок и «истории
        версий» нет — после пересборки прогресс описывает новую версию.
        """
        return _progress_ids(self.publication_progress, "ids")

    @property
    def previous_message_ids(self) -> tuple[int, ...]:
        """
        message_id прошлой доставленной версии, которую заменяет текущая.

        Начатая пересборка переносит их в `previous_ids` прогресса: пока
        новая версия не подтверждена целиком, прошлая остаётся в чате
        единственной полной копией анонса.
        """
        return _progress_ids(self.publication_progress, "previous_ids")

    @property
    def delivery_fingerprint(self) -> str | None:
        value = self.publication_progress.get("fingerprint")
        return value if isinstance(value, str) and value else None

    @property
    def can_edit_after_delivery(self) -> bool:
        """
        Доставленный анонс можно править, пока не начата новая отправка.

        `sending`/`partial`/`failed` означают незавершённую замену: правка
        в этот момент создала бы третью версию и дубли.
        """
        if self.publication_status is None:
            return True
        return self.publication_status == PUBLICATION_STATUS_SENT and bool(
            self.delivered_message_ids
        )

    @property
    def publication_step(self) -> int:
        value = self.publication_progress.get("step")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    @property
    def publication_total(self) -> int:
        value = self.publication_progress.get("total")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    @property
    def publication_can_reset(self) -> bool:
        """
        Вернуться к правке можно только до первой операции отправки.

        Пересборка уже доставленного анонса не сбрасывается: адреса прошлой
        версии нельзя терять, иначе её не удалить и не заменить — в чате
        остались бы две копии анонса.
        """
        return (
            self.publication_status == PUBLICATION_STATUS_FAILED
            and self.publication_step == 0
            and not self.publication_progress.get("ids")
            and not self.previous_message_ids
        )


@dataclass(frozen=True)
class AnnouncementPublication:
    """Замороженный payload одной публикации и текущий прогресс."""

    draft: AnnouncementDraftDetails
    text: str
    photos: tuple[str, ...]
    progress: Mapping[str, Any]

    @property
    def step(self) -> int:
        value = self.progress.get("step")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    @property
    def total(self) -> int:
        value = self.progress.get("total")
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)


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


FIELD_STATUS_VALID = "valid"
FIELD_STATUS_SKIPPED = "empty_optional"
FIELD_STATUS_MISSING = "missing"
FIELD_STATUS_KNOWN = "known"


@dataclass(frozen=True)
class AnnouncementFieldState:
    """Состояние одного кирпичика снимка для вопросов и обзора."""

    brick_id: int
    code: str
    title: str
    value_type: str
    section: str
    status: str
    raw_value: object | None = None
    rules: Mapping[str, object] = field(default_factory=dict)
    # Poster 2.0 S5: инструкция автора кирпичика для экрана вопроса. Только
    # подсказка интерфейса — в текст анонса и в шаблон не попадает.
    description: str | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == FIELD_STATUS_VALID

    @property
    def is_skipped(self) -> bool:
        return self.status == FIELD_STATUS_SKIPPED

    @property
    def is_missing(self) -> bool:
        return self.status == FIELD_STATUS_MISSING

    @property
    def is_known(self) -> bool:
        return self.status == FIELD_STATUS_KNOWN


@dataclass(frozen=True)
class AnnouncementPreview:
    """Собранный предварительный текст и диагностика готовности."""

    draft: AnnouncementDraftDetails
    html: str
    visible_text: str
    ready: bool
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    fields: tuple[AnnouncementFieldState, ...]
    missing: tuple[Question, ...]


def _display_raw_value(raw: object | None) -> str:
    if raw is None:
        return ""
    if isinstance(raw, (list, tuple)):
        return ", ".join(str(item) for item in raw)
    return str(raw)


def _valid_positive_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _photo_ids_from_json(raw: str | None, *, field_name: str) -> tuple[str, ...]:
    """Читает JSON-список file_id; битое значение — явная ошибка, не «нет фото»."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AnnouncementSnapshotError(
            f"Поле {field_name} содержит битый JSON."
        ) from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        raise AnnouncementSnapshotError(
            f"Поле {field_name} должно быть JSON-списком непустых file_id."
        )
    # Защитная дедупликация на чтении: повторный Update не должен
    # превращаться в повторное вложение даже при старых/ручных данных.
    seen: set[str] = set()
    result: list[str] = []
    for item in parsed:
        file_id = item.strip()
        if file_id not in seen:
            seen.add(file_id)
            result.append(file_id)
    return tuple(result)


def _photo_ids_to_json(photos: Sequence[object]) -> str | None:
    values: list[str] = []
    seen: set[str] = set()
    for item in photos or ():
        file_id = _validated_photo_id(item)
        if file_id not in seen:
            seen.add(file_id)
            values.append(file_id)
    return json.dumps(values, ensure_ascii=False) if values else None


def _validated_photo_id(value: object) -> str:
    if not isinstance(value, str):
        raise AnnouncementValueError("file_id фотографии должен быть строкой.")
    file_id = value.strip()
    if not file_id or len(file_id) > 4096:
        raise AnnouncementValueError("Некорректный file_id фотографии.")
    return file_id


def _progress_ids(progress: Mapping[str, Any], key: str) -> tuple[int, ...]:
    value = progress.get(key)
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            continue
        result.append(item)
    return tuple(dict.fromkeys(result))


def _publication_progress_from_json(raw: str | None) -> Mapping[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AnnouncementSnapshotError(
            "Прогресс публикации содержит битый JSON."
        ) from exc
    if not isinstance(parsed, dict):
        raise AnnouncementSnapshotError("Прогресс публикации должен быть объектом.")
    return parsed


def _publication_progress_to_json(progress: Mapping[str, Any]) -> str:
    return json.dumps(dict(progress), ensure_ascii=False, sort_keys=True)


def _ensure_content_editable(row: AnnouncementDraft) -> None:
    """
    Правка содержимого разрешена до первой отправки и после доставки.

    Stage 28: готовый анонс доставляется в личный чат автора, поэтому после
    успешной доставки данные снова редактируемы — новая версия заменит
    прошлую. Пока замена не завершена (`sending`/`partial`/`failed`),
    правка запрещена: иначе третья версия останется в чате.
    """
    if row.publication_status is None:
        return
    if row.publication_status == PUBLICATION_STATUS_SENT:
        progress = _publication_progress_from_json(row.publication_progress)
        if _progress_ids(progress, "ids"):
            return
        raise AnnouncementPublicationError(
            "Анонс уже отправлен, но адреса доставки не сохранены. "
            "Повторная отправка не выполняется, чтобы не создать дубль."
        )
    raise AnnouncementPublicationError(
        "Отправка уже начата: изменить текст, значения или фотографии "
        "нельзя. Продолжите отправку или завершите её по статусу."
    )


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
        source_name=details.source_name,
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


def _live_ids_from_json(raw: str | None) -> tuple[int, ...]:
    """Читает JSON-массив адресов живого блока; битое значение — ошибка."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AnnouncementSnapshotError(
            "Поле live_message_ids_json содержит битый JSON."
        ) from exc
    if not isinstance(parsed, list):
        raise AnnouncementSnapshotError(
            "Поле live_message_ids_json должно быть JSON-массивом message_id."
        )
    result: list[int] = []
    for item in parsed:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise AnnouncementSnapshotError(
                "Адреса живого блока должны быть положительными message_id."
            )
        if item not in result:
            result.append(item)
    return tuple(result)


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
    publication_status = row.publication_status
    if (
        publication_status is not None
        and publication_status not in PUBLICATION_STATUSES
    ):
        raise AnnouncementSnapshotError("Неизвестный статус публикации анонса.")
    return AnnouncementDraftDetails(
        draft_id=row.id,
        author_user_id=row.author_user_id,
        place_id=row.place_id,
        place_name_id=row.place_name_id,
        place_display_name=row.place_display_name,
        place_address=row.place_address,
        place_url=row.place_url,
        place_city=row.place_city,
        origin=row.origin,
        template_id=row.template_id,
        template_version=row.template_version,
        template_snapshot=_draft_snapshot(session, actor, row),
        stage=WizardStage(row.stage),
        revision=row.revision,
        values=tuple(_decode_value(value) for value in value_rows),
        extras=_extra_details(session, extra_rows),
        live_message_ids=_live_ids_from_json(row.live_message_ids_json),
        photos=_photo_ids_from_json(row.photos, field_name="photos"),
        publication_status=publication_status,
        publication_text=row.publication_text,
        publication_photos=_photo_ids_from_json(
            row.publication_photos, field_name="publication_photos"
        ),
        publication_progress=_publication_progress_from_json(row.publication_progress),
        publication_error=row.publication_error,
        publication_started_at=row.publication_started_at,
        published_at=row.published_at,
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
    place_id: int | None,
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
    place_id: int | None = None,
    place_name_id: int | None = None,
    template_id: int | None = None,
    origin: object = ORIGIN_PLACE,
    idempotency_key: object | None = None,
) -> AnnouncementDraftDetails:
    """
    Создаёт состояние нового мастера.

    Два взаимоисключающих входа (Poster 2.0):

    - `origin='place'` — сначала выбирается место (`place_id` обязателен,
      `place_name_id` уточняет вариант названия); шаблон фиксируется позже
      через `choose_announcement_template`, как раньше.
    - `origin='personal'` — анонс по личному шаблону: площадки нет
      (`place_id`/`place_name_id` обязаны быть None), `template_id`
      обязателен, снимок шаблона создаётся сразу и мастер начинается с
      вопросов. Личный шаблон должен принадлежать автору; чужой
      неотличим от несуществующего.

    Идемпотентность по `idempotency_key` работает для обоих входов и
    учитывает источник: тот же ключ с другим origin/template/place — конфликт.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    checked_origin = _validate_origin(origin)
    key = _validated_key(idempotency_key)

    if checked_origin == ORIGIN_PLACE:
        if template_id is not None:
            raise AnnouncementStageError(
                "Шаблон площадки выбирается после подтверждения места."
            )
        return _start_place_announcement(
            session,
            actor=actor,
            place_id=place_id,
            place_name_id=place_name_id,
            idempotency_key=key,
        )
    if place_id is not None or place_name_id is not None:
        raise AnnouncementStageError(
            "Анонс по личному шаблону не привязывается к площадке."
        )
    if not _valid_positive_id(template_id):
        raise AnnouncementNotFoundError("Некорректный идентификатор шаблона.")
    assert isinstance(template_id, int)
    return _start_personal_announcement(
        session,
        actor=actor,
        template_id=template_id,
        idempotency_key=key,
    )


def _validate_origin(origin: object) -> str:
    if origin not in ORIGINS:
        raise AnnouncementStageError(
            "Источник анонса: 'place' (площадка) или 'personal' (личный шаблон)."
        )
    return str(origin)


def _start_place_announcement(
    session: Session,
    *,
    actor: User,
    place_id: object,
    place_name_id: object,
    idempotency_key: str | None,
) -> AnnouncementDraftDetails:
    if not _valid_positive_id(place_id):
        raise AnnouncementNotFoundError("Некорректный идентификатор места.")
    assert isinstance(place_id, int)

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

    if idempotency_key is not None:
        existing = _draft_by_key(session, actor, idempotency_key)
        if existing is not None:
            if (
                existing.origin != ORIGIN_PLACE
                or existing.place_id != place_id
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
        place_city=_optional_text(details.city),
        origin=ORIGIN_PLACE,
        template_id=None,
        template_version=None,
        template_snapshot_id=None,
        stage=WizardStage.PLACE.value,
        revision=1,
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        if idempotency_key is not None:
            existing = _draft_by_key(session, actor, idempotency_key)
            if existing is not None:
                return _draft_details(session, actor, existing)
        raise AnnouncementConflictError(
            "Не удалось создать черновик из-за одновременной записи."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def _start_personal_announcement(
    session: Session,
    *,
    actor: User,
    template_id: int,
    idempotency_key: str | None,
) -> AnnouncementDraftDetails:
    """Анонс по личному шаблону: площадки нет, снимок фиксируется сразу."""
    if idempotency_key is not None:
        existing = _draft_by_key(session, actor, idempotency_key)
        if existing is not None:
            if (
                existing.origin != ORIGIN_PERSONAL
                or existing.template_id != template_id
            ):
                raise AnnouncementConflictError(
                    "Ключ идемпотентности уже использован для другого выбора."
                )
            return _draft_details(session, actor, existing)

    template = get_accessible_template(session, actor.telegram_user_id, template_id)
    if template is None or template.kind != TEMPLATE_KIND_PERSONAL:
        # Стандартный шаблон площадки требует выбранного места, а чужой
        # личный неотличим от несуществующего.
        raise AnnouncementNotFoundError("Личный шаблон не найден или недоступен.")
    if template.owner_user_id != actor.id:
        raise AnnouncementNotFoundError("Личный шаблон не найден или недоступен.")

    now = utcnow()
    row = AnnouncementDraft(
        author_user_id=actor.id,
        place_id=None,
        place_name_id=None,
        place_display_name=None,
        place_address=None,
        place_url=None,
        place_city=None,
        origin=ORIGIN_PERSONAL,
        template_id=None,
        template_version=None,
        template_snapshot_id=None,
        stage=WizardStage.PLACE.value,
        revision=1,
        idempotency_key=idempotency_key,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        session.flush()
        compatibility = validate_template_compatibility(
            session,
            actor_telegram_user_id=actor.telegram_user_id,
            template_id=template_id,
            selected_place_id=None,
        )
        if not compatibility.compatible:
            raise AnnouncementBrickError(
                "Шаблон несовместим: " + "; ".join(compatibility.issues)
            )
        snapshot = create_template_snapshot(
            session,
            actor_telegram_user_id=actor.telegram_user_id,
            template_id=template_id,
            idempotency_key=f"announcement-wizard:{row.id}",
            commit=False,
        )
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=1,
            fields={
                "template_id": template.template_id,
                "template_version": template.current_version.version,
                "template_snapshot_id": snapshot.snapshot_id,
                "stage": WizardStage.QUESTIONS.value,
            },
        )
        session.commit()
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
        if idempotency_key is not None:
            existing = _draft_by_key(session, actor, idempotency_key)
            if existing is not None:
                return _draft_details(session, actor, existing)
        raise AnnouncementConflictError(
            "Не удалось создать анонс из-за одновременной записи."
        ) from exc
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def _draft_by_key(
    session: Session,
    actor: User,
    idempotency_key: str,
) -> AnnouncementDraft | None:
    return session.scalar(
        select(AnnouncementDraft).where(
            AnnouncementDraft.author_user_id == actor.id,
            AnnouncementDraft.idempotency_key == idempotency_key,
        )
    )


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

    if row.origin == ORIGIN_PERSONAL:
        # У анонса по личному шаблону площадки нет: подтверждать нечего.
        # Это не «уже подтверждено», а неверный шаг сценария.
        raise AnnouncementStageError(
            "Анонс по личному шаблону не требует подтверждения площадки."
        )
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

    # После ветки origin='personal' выше схема гарантирует place_id у
    # 'place'-черновика; защищаемся явно, чтобы не работать с повреждённой
    # строкой как с валидной (типы и сообщение вместо TypeError).
    if row.place_id is None:
        raise AnnouncementStageError("Выбранное место больше не доступно.")
    place_id = row.place_id
    details = get_place_details(session, place_id)
    if details is None:
        raise AnnouncementStageError("Выбранное место больше не доступно.")
    display_name = row.place_display_name
    if row.place_name_id is not None:
        name = get_place_name(session, place_id, row.place_name_id)
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
                "place_city": _optional_text(details.city),
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
        # Стандарт всегда принадлежит конкретной площадке. У анонса по
        # личному шаблону площадки нет, поэтому стандарт недопустим.
        if draft.origin != ORIGIN_PLACE or draft.place_id is None:
            raise AnnouncementStageError(
                "Стандартный шаблон площадки недоступен без выбранного места."
            )
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


def _refresh_place_snapshot(
    session: Session,
    row: AnnouncementDraft,
) -> dict[str, object]:
    """
    Перечитывает снимок площадки из справочника перед фиксацией шаблона (U1a).

    Дефект: между подтверждением места и выбором шаблона админ мог исправить
    название/адрес/ссылку/город площадки, а анонс показывал старый снимок —
    живой анонс выходил с устаревшими данными. Здесь снимок обновляется
    ОДИН раз, в момент, когда структура анонса фиксируется.

    Возвращает только изменившиеся поля (пустой словарь — обновлять нечего),
    чтобы не поднимать ревизию черновика без причины. Данные справочника
    не меняются: это по-прежнему снимок, а не ссылка.
    """
    if row.origin != ORIGIN_PLACE or row.place_id is None:
        return {}
    details = get_place_details(session, int(row.place_id))
    if details is None:
        raise AnnouncementStageError("Выбранное место больше не доступно.")

    name_id = details.canonical_name_id
    display_name = details.canonical_name
    if row.place_name_id is not None:
        name = get_place_name(session, int(row.place_id), int(row.place_name_id))
        if name is not None:
            name_id = name.id
            display_name = name.name

    fresh = {
        "place_name_id": name_id,
        "place_display_name": display_name,
        "place_address": _optional_text(details.address),
        "place_url": _optional_text(details.url),
        "place_city": _optional_text(details.city),
    }
    return {key: value for key, value in fresh.items() if getattr(row, key) != value}


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
                # U1a: снимок площадки перечитывается ровно здесь — структура
                # анонса фиксируется, и старые данные справочника в живой
                # пост попасть уже не должны.
                **_refresh_place_snapshot(session, row),
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
    _ensure_content_editable(row)
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
    _ensure_content_editable(row)
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
    _ensure_content_editable(row)
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
    _ensure_content_editable(row)
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
    _ensure_content_editable(row)
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
    _ensure_content_editable(row)
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


# --- Живой блок анонса (Poster 2.0 S3) -------------------------------------


def set_announcement_live_message_ids(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    message_ids: Sequence[int],
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """
    Сохраняет упорядоченные адреса сообщений живого верхнего блока.

    Живой блок — это UI-состояние, а не содержимое анонса: адреса сообщений
    **не увеличивают `revision`**. Иначе каждая перерисовка вопроса делала бы
    кнопки на экране устаревшими и ломала optimistic locking ответов.

    `expected_revision`, если задан, проверяется как защита от записи в
    чужую/устаревшую сессию, но сам revision не меняется. Список пишется
    после подтверждённой отправки, поэтому при сбое Telegram остаётся
    прошлая версия. Дубли и неположительные ID отклоняются, `[]` очищает.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    _expected_revision(row, expected_revision)
    cleaned: list[int] = []
    for message_id in message_ids:
        if not _valid_positive_id(message_id):
            raise AnnouncementError(
                "Адрес живого блока должен быть положительным message_id."
            )
        value = int(message_id)
        if value not in cleaned:
            cleaned.append(value)
    session.execute(
        sqlalchemy_update(AnnouncementDraft)
        .where(AnnouncementDraft.id == row.id)
        .values(
            live_message_ids_json=(
                json.dumps(cleaned, ensure_ascii=False) if cleaned else None
            ),
            updated_at=utcnow(),
        )
        .execution_options(synchronize_session=False)
    )
    _commit(session)
    return _details_by_id(session, actor, row.id)


# --- Фотографии анонса ------------------------------------------------------


def add_announcement_photo(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    file_id: object,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """
    Добавляет file_id в отдельную коллекцию фото анонса.

    Повторная обработка того же Update/фото идемпотентна: одинаковый
    file_id не создаёт дубль и не меняет revision. После начала публикации
    коллекция заморожена.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    _ensure_content_editable(row)
    revision = _expected_revision(row, expected_revision)
    checked = _validated_photo_id(file_id)
    current = list(_photo_ids_from_json(row.photos, field_name="photos"))
    if checked in current:
        return _draft_details(session, actor, row)
    if len(current) >= MAX_ANNOUNCEMENT_PHOTOS:
        raise AnnouncementValueError(
            f"К анонсу можно добавить не более {MAX_ANNOUNCEMENT_PHOTOS} фотографий."
        )
    current.append(checked)
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={"photos": _photo_ids_to_json(current)},
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def remove_announcement_photo(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    index: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Удаляет фото по 0-based индексу; остальные не сдвигаются в file_id."""
    actor = _require_actor(session, actor_telegram_user_id)
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise AnnouncementValueError("Некорректный номер фотографии.")
    row = _load_owned_draft(session, actor, draft_id)
    _ensure_content_editable(row)
    revision = _expected_revision(row, expected_revision)
    current = list(_photo_ids_from_json(row.photos, field_name="photos"))
    if index >= len(current):
        raise AnnouncementValueError("Фотография уже удалена или не найдена.")
    current.pop(index)
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={"photos": _photo_ids_to_json(current)},
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def clear_announcement_photos(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """Удаляет всю коллекцию фото; текст и кирпичики не меняются."""
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    _ensure_content_editable(row)
    revision = _expected_revision(row, expected_revision)
    current = _photo_ids_from_json(row.photos, field_name="photos")
    if not current:
        return _draft_details(session, actor, row)
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={"photos": None},
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


# --- Публикация -------------------------------------------------------------


def _publication_from_row(
    session: Session,
    actor: User,
    row: AnnouncementDraft,
) -> AnnouncementPublication:
    # После CAS/commit объект в identity map может быть старым (тесты и
    # рабочие сессии используют expire_on_commit=False). Перечитываем строку
    # с populate_existing, иначе статус `sending` выглядит как «не начат».
    row = _load_owned_draft(session, actor, row.id)
    details = _draft_details(session, actor, row)
    if details.publication_status is None:
        raise AnnouncementPublicationError("Публикация ещё не начата.")
    if not details.publication_text:
        raise AnnouncementSnapshotError(
            "У начатой публикации отсутствует замороженный текст."
        )
    return AnnouncementPublication(
        draft=details,
        text=details.publication_text,
        photos=details.publication_photos,
        progress=dict(details.publication_progress),
    )


def begin_announcement_publication(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    expected_revision: int | None = None,
    today: date | None = None,
) -> AnnouncementPublication:
    """
    Замораживает готовый текст и фото и переводит анонс в `sending`.

    Проверка готовности и компиляция Stage 24 выполняются ДО начала
    отправки. Повторный вызов при уже начатой публикации не пересобирает
    payload, а возвращает ошибку: продолжение — отдельная явная операция.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    previous_ids: list[int] = []
    if row.publication_status is not None:
        # Stage 28: доставленный анонс пересобирается после правки. Адреса
        # прежней версии переносятся в `previous_ids` ТОЛЬКО как адреса
        # прошлой отправки — они не смешиваются с `ids` новой. Незавершённая
        # отправка по-прежнему не пересобирается: неизвестный исход нельзя
        # перекрывать новой версией.
        if row.publication_status != PUBLICATION_STATUS_SENT:
            raise AnnouncementPublicationError(
                "Отправка уже начата или завершена; payload не пересобирается."
            )
        previous_ids = list(
            _progress_ids(
                _publication_progress_from_json(row.publication_progress), "ids"
            )
        )
        if not previous_ids:
            raise AnnouncementPublicationError(
                "Адреса прежней доставки не сохранены: замена невозможна."
            )
    if row.stage not in {
        WizardStage.PREVIEW.value,
        WizardStage.COMPLETED.value,
    }:
        raise AnnouncementStageError("Сначала завершите обзор анонса.")
    preview = build_announcement_preview(
        session,
        actor_telegram_user_id=actor.telegram_user_id,
        draft_id=row.id,
        today=today,
    )
    if not preview.ready:
        reason = preview.issues[0] if preview.issues else "анонс не готов"
        raise AnnouncementStageError(f"Анонс не готов к выдаче: {reason}")
    now = utcnow()
    photos = preview.draft.photos
    # S4 §5.1: перед заморозкой payload проверяется видимый размер — без
    # фото 4096, с фото 1024. Превышение не «лечится» автосокращением:
    # вызывающий слой показывает проблемный экран, данные остаются в БД.
    length = check_announcement_length(preview.html, has_photos=bool(photos))
    if length.over:
        raise AnnouncementStageError(
            "Анонс не помещается в сообщение: "
            f"{length.length} из {length.limit} символов "
            f"(превышение {length.excess}). Сократите текст кирпичиков."
        )
    # С фото пустой видимый текст не превращается в подпись-точку: вложения
    # уходят без подписи. Форма публикации требует непустой замороженный
    # текст, поэтому для этого случая хранится невидимый маркер пустой
    # подписи (подписью он не становится, см. `delivery.visible_text`).
    if preview.html:
        frozen_text = preview.html
    elif photos:
        frozen_text = EMPTY_CAPTION_TEXT
    else:
        frozen_text = PLACEHOLDER_TEXT
    # Прогресс новой версии: step/ids/total заполнит отправитель, а
    # `previous_ids` — адреса уже доставленной версии, которую нужно
    # заменить. Никакой «неизменяемой истории»: при следующей пересборке
    # этот payload перезаписывается.
    progress: dict[str, object] = {}
    if previous_ids:
        progress["previous_ids"] = previous_ids
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={
                "publication_status": PUBLICATION_STATUS_SENDING,
                "publication_text": frozen_text,
                "publication_photos": json.dumps(list(photos), ensure_ascii=False),
                "publication_progress": (
                    _publication_progress_to_json(progress) if progress else None
                ),
                "publication_error": None,
                "publication_started_at": now,
                "published_at": None,
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _publication_from_row(session, actor, row)


def resume_announcement_publication(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    expected_revision: int | None = None,
) -> AnnouncementPublication:
    """
    Продолжает только замороженный payload; значения/фото не пересобираются.

    Новый revision закрывает повторное нажатие той же кнопки. Уже
    подтверждённые операции берутся из progress и повторно не отправляются.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    if row.publication_status == PUBLICATION_STATUS_SENT:
        raise AnnouncementPublicationError("Анонс уже опубликован.")
    if row.publication_status is None:
        raise AnnouncementPublicationError("Публикация ещё не начата.")
    if row.publication_status not in PUBLICATION_FROZEN_STATUSES:
        raise AnnouncementPublicationError("Неизвестный статус публикации.")
    if not row.publication_text or row.publication_photos is None:
        raise AnnouncementSnapshotError("Замороженный payload публикации повреждён.")
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={
                "publication_status": PUBLICATION_STATUS_SENDING,
                "publication_error": None,
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _publication_from_row(session, actor, row)


def record_announcement_publication_progress(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    progress: Mapping[str, object],
) -> AnnouncementDraftDetails:
    """
    Сохраняет подтверждённый прогресс отправки в SQLite.

    Progress пишется после каждой подтверждённой Telegram-операции. Ошибка
    записи не «додумывается»: вызывающий отправку останавливается, чтобы
    не продолжать вслепую.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    if row.publication_status != PUBLICATION_STATUS_SENDING:
        raise AnnouncementPublicationError(
            "Прогресс можно записывать только во время отправки."
        )
    normalized = _validated_publication_progress(progress)
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=row.revision,
            fields={"publication_progress": _publication_progress_to_json(normalized)},
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def mark_announcement_publication_failed(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    error: str,
) -> AnnouncementDraftDetails:
    """Фиксирует честный статус: частичная отправка или полный отказ."""
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    if row.publication_status == PUBLICATION_STATUS_SENT:
        return _draft_details(session, actor, row)
    if row.publication_status is None:
        raise AnnouncementPublicationError("Публикация ещё не начата.")
    progress = _publication_progress_from_json(row.publication_progress)
    step = progress.get("step")
    step = step if isinstance(step, int) and not isinstance(step, bool) else 0
    status = PUBLICATION_STATUS_PARTIAL if step > 0 else PUBLICATION_STATUS_FAILED
    message = " ".join(str(error or "").split())[:1000]
    if not message:
        message = "Telegram не подтвердил отправку."
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=row.revision,
            fields={
                "publication_status": status,
                "publication_error": message,
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def mark_announcement_publication_sent(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    message_ids: Sequence[object] | None = None,
    fingerprint: str | None = None,
) -> AnnouncementDraftDetails:
    """
    Переводит анонс в `sent` только после успешного ответа Telegram.

    `message_ids` — id ТЕКУЩЕЙ отправки: они сохраняются в прогрессе как
    `ids` и остаются адресами доставленной версии. `fingerprint` позволяет
    не отправлять повторно неизменившийся анонс. `previous_ids` из прогресса
    сохраняются как есть — это адреса прошлой версии, отдельные от `ids`.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    if row.publication_status == PUBLICATION_STATUS_SENT:
        return _draft_details(session, actor, row)
    if row.publication_status != PUBLICATION_STATUS_SENDING:
        raise AnnouncementPublicationError(
            "Успех можно зафиксировать только после текущей отправки; "
            "сначала продолжите замороженный payload."
        )
    progress = dict(_publication_progress_from_json(row.publication_progress))
    if message_ids is not None:
        checked = _progress_ids({"ids": list(message_ids)}, "ids")
        if not checked:
            raise AnnouncementValueError(
                "Нельзя зафиксировать доставку без message_id."
            )
        progress["ids"] = list(checked)
    if fingerprint is not None:
        if not fingerprint.strip():
            raise AnnouncementValueError("Отпечаток доставки не может быть пустым.")
        progress["fingerprint"] = fingerprint.strip()
    # Без id доставка считается неподтверждённой: анонс можно показать, но
    # заменить/удалить прошлую копию нельзя, поэтому правка не разрешается
    # (`_ensure_content_editable`). Вызывающий мастер всегда передаёт id.
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=row.revision,
            fields={
                "publication_status": PUBLICATION_STATUS_SENT,
                "publication_progress": _publication_progress_to_json(progress),
                "publication_error": None,
                "published_at": utcnow(),
                "stage": WizardStage.COMPLETED.value,
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def reset_failed_announcement_publication(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    expected_revision: int | None = None,
) -> AnnouncementDraftDetails:
    """
    Возвращает к правке только публикацию, не отправившую ни одной операции.

    После частичного успеха сброс запрещён: иначе повторная отправка могла
    бы создать дубли уже доставленных сообщений.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    revision = _expected_revision(row, expected_revision)
    if row.publication_status != PUBLICATION_STATUS_FAILED:
        raise AnnouncementPublicationError(
            "Вернуться к правке можно только после полного отказа отправки."
        )
    progress = _publication_progress_from_json(row.publication_progress)
    if _progress_ids(progress, "previous_ids"):
        raise AnnouncementPublicationError(
            "Это замена уже доставленного анонса: адреса прошлой версии "
            "нельзя потерять. Повторите отправку или оставьте анонс как есть."
        )
    step = progress.get("step")
    ids = progress.get("ids")
    if (isinstance(step, int) and not isinstance(step, bool) and step > 0) or (
        isinstance(ids, list) and ids
    ):
        raise AnnouncementPublicationError(
            "Часть сообщений уже отправлена: сброс создал бы дубли."
        )
    try:
        _cas_update_draft(
            session,
            draft_id=row.id,
            revision=revision,
            fields={
                "publication_status": None,
                "publication_text": None,
                "publication_photos": None,
                "publication_progress": None,
                "publication_error": None,
                "publication_started_at": None,
                "published_at": None,
                "stage": WizardStage.PREVIEW.value,
            },
        )
        _commit(session)
    except Exception:
        session.rollback()
        raise
    return _details_by_id(session, actor, row.id)


def _validated_publication_progress(
    progress: Mapping[str, object],
) -> dict[str, object]:
    if not isinstance(progress, Mapping):
        raise AnnouncementValueError("Прогресс публикации должен быть объектом.")
    fingerprint = progress.get("fingerprint")
    step = progress.get("step")
    ids = progress.get("ids")
    total = progress.get("total")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise AnnouncementValueError("В прогрессе публикации нет отпечатка.")
    if isinstance(step, bool) or not isinstance(step, int) or step < 0:
        raise AnnouncementValueError("В прогрессе публикации некорректный шаг.")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise AnnouncementValueError("В прогрессе публикации нет общего числа.")
    if step > total:
        raise AnnouncementValueError("Шаг публикации больше общего числа операций.")
    if not isinstance(ids, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids
    ):
        raise AnnouncementValueError("В прогрессе публикации некорректные message_id.")
    result: dict[str, object] = {
        "fingerprint": fingerprint,
        "step": step,
        "ids": list(ids),
        "total": total,
    }
    # Адреса ПРЕДЫДУЩЕЙ доставленной версии не смешиваются с `ids` текущей
    # отправки: по ним удаляется прошлая копия после успешной замены.
    if "previous_ids" in progress:
        previous = progress.get("previous_ids")
        if not isinstance(previous, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in previous
        ):
            raise AnnouncementValueError(
                "В прогрессе доставки некорректные message_id прошлой версии."
            )
        result["previous_ids"] = list(previous)
    return result


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
        city=row.place_city,
        brick_ids_by_code=snapshot_brick_ids_by_code(snapshot),
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
        city=row.place_city,
        brick_ids_by_code=snapshot_brick_ids_by_code(snapshot),
    ).items():
        values_by_id.setdefault(brick_id, raw)
    return AnnouncementRenderPayload(
        tokens_by_section=sections,
        values_by_id=values_by_id,
        definitions_by_id=definitions,
    )


def build_announcement_fields(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    today: date | None = None,
) -> tuple[AnnouncementFieldState, ...]:
    """
    Возвращает все кирпичики снимка в порядке шапка → тело → подвал.

    Недостающие поля берутся из того же `build_questions`, что и мастер,
    поэтому второй алгоритм определения вопросов в Telegram-слое не нужен.
    Известные значения места отличаются от явно пропущенных.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    row = _load_owned_draft(session, actor, draft_id)
    snapshot = _draft_snapshot(session, actor, row)
    if snapshot is None:
        raise AnnouncementStageError("Сначала выберите шаблон анонса.")
    details = _draft_details(session, actor, row)
    questions = build_announcement_questions(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
        today=today,
    )
    question_map = {question.brick_id: question for question in questions}
    payload = build_announcement_render_payload(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
    )
    known = place_context_values(
        display_name=row.place_display_name,
        address=row.place_address,
        url=row.place_url,
        city=row.place_city,
        brick_ids_by_code=snapshot_brick_ids_by_code(snapshot),
    )

    states: list[AnnouncementFieldState] = []
    for brick_id in snapshot.brick_ids:
        definition = payload.definitions_by_id[brick_id]
        question = question_map.get(brick_id)
        value = details.value_for(brick_id)
        raw: object | None = None
        if value is not None and value.status is BrickValueStatus.VALID:
            status = FIELD_STATUS_VALID
            raw = answer_payload_to_raw(
                BrickValueResult(
                    status=value.status,
                    payload=dict(value.payload) if value.payload is not None else None,
                )
            )
        elif value is not None and value.status is BrickValueStatus.EMPTY_OPTIONAL:
            status = FIELD_STATUS_SKIPPED
        elif question is not None:
            status = FIELD_STATUS_MISSING
        elif brick_id in known:
            status = FIELD_STATUS_KNOWN
            raw = known[brick_id]
        else:
            raise AnnouncementSnapshotError(
                f"Кирпичик #{brick_id} не имеет значения и не попал в вопросы."
            )

        states.append(
            AnnouncementFieldState(
                brick_id=brick_id,
                code=definition.code,
                title=question.title if question is not None else definition.title,
                value_type=definition.value_type,
                section=(
                    question.section
                    if question is not None
                    else snapshot_section(snapshot, definition.code)
                ),
                status=status,
                raw_value=raw,
                rules=dict(
                    question.rules if question is not None else definition.rules
                ),
                description=definition.description,
            )
        )
    return tuple(states)


def build_announcement_preview(
    session: Session,
    *,
    actor_telegram_user_id: int | None,
    draft_id: int,
    today: date | None = None,
) -> AnnouncementPreview:
    """
    Собирает предварительный текст по снимку и `brick_id`.

    Ошибки компилятора не превращаются в «готовый» текст: вызывающий слой
    показывает их как диагностику. Данные в SQLite при этом не меняются.
    """
    details = get_announcement_draft(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
    )
    fields = build_announcement_fields(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
        today=today,
    )
    payload = build_announcement_render_payload(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
    )
    # `html` — то, что реально уйдёт в Telegram (пустой анонс → `.`,
    # §5.6). Готовность считается по тексту БЕЗ заглушки: иначе пустой
    # анонс выглядел бы непустым для шутки в служебном экране.
    raw_html = render_template(
        payload.tokens_by_section,
        payload.values_by_id,
        payload.definitions_by_id,
    )
    html = render_announcement(
        payload.tokens_by_section,
        payload.values_by_id,
        payload.definitions_by_id,
        has_photos=bool(details.photos),
    )
    snapshot_brick_count = (
        len(details.template_snapshot.brick_ids)
        if details.template_snapshot is not None
        else 0
    )
    snapshot = details.template_snapshot
    relevant_brick_ids = set(snapshot.brick_ids) if snapshot is not None else set()
    if snapshot is not None:
        relevant_brick_ids.update(extra.brick_id for extra in details.extras)
    filled_value_count = sum(
        1 for brick_id in relevant_brick_ids if brick_id in payload.values_by_id
    )
    readiness = evaluate_announcement_readiness(
        raw_html,
        snapshot_brick_count=snapshot_brick_count,
        filled_value_count=filled_value_count,
    )
    questions = build_announcement_questions(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        draft_id=draft_id,
        today=today,
    )
    return AnnouncementPreview(
        draft=details,
        html=html,
        visible_text=readiness.visible_text,
        ready=readiness.ready,
        issues=readiness.issues,
        warnings=readiness.warnings,
        fields=fields,
        missing=tuple(questions),
    )
