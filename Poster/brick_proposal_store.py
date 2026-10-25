# brick_proposal_store.py
"""
Предложения новых кирпичиков и их согласование (Stage 26).

Сервис не зависит от Telegram: он получает постоянный Telegram user ID
актора, повторно разрешает его в `users` и проверяет действующие полномочия.
Callback-данные не являются источником ролей.

Ключевые инварианты:

- предложение хранит неизменяемый снимок личного кирпичика, а не ссылку,
  которую можно переписать позднее;
- `pending` не имеет срока действия и не удаляется автоматически;
- решение принимается ровно один раз и только из `pending`;
- одобрение создаёт ОТДЕЛЬНЫЙ `Brick` с новым `brick_id` в целевой области;
- исходный личный кирпичик, его владелец и шаблонные привязки не меняются;
- создание кирпичика и запись решения выполняются одной SQLite-транзакцией;
- конфликт технического кода не переименовывается молча и не создаёт дубль;
- права проверяются заново внутри транзакции решения, а не по состоянию
  открытой карточки.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy import update as sqlalchemy_update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from brick_store import (
    BrickCodeAvailability,
    BrickDefinition,
    check_code_availability,
)
from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_STATUS_ACTIVE,
    BrickValidationError,
    clean_brick_title,
    ensure_brick_source,
    ensure_brick_type,
    parse_rules,
    rules_from_json,
    rules_to_json,
    validate_brick_code,
)
from models import Brick, BrickProposal, Place, PlaceName, User, utcnow
from permissions import can_manage_place, is_superadmin

PROPOSAL_SCOPE_PLACE = "place"
PROPOSAL_SCOPE_SYSTEM = "system"
PROPOSAL_SCOPES = (PROPOSAL_SCOPE_PLACE, PROPOSAL_SCOPE_SYSTEM)

PROPOSAL_STATUS_PENDING = "pending"
PROPOSAL_STATUS_APPROVED = "approved"
PROPOSAL_STATUS_REJECTED = "rejected"
PROPOSAL_STATUS_WITHDRAWN = "withdrawn"
PROPOSAL_STATUSES = (
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_WITHDRAWN,
)

PROPOSAL_PAGE_SIZE = 8
MAX_DECISION_NOTE_LENGTH = 1000


class ProposalError(ValueError):
    """Базовая понятная ошибка предложения кирпичика."""


class ProposalValidationError(ProposalError):
    """Некорректные данные области, снимка или решения."""


class ProposalPermissionError(PermissionError):
    """У актора нет действующих полномочий на это действие."""


class ProposalActorError(RuntimeError):
    """Актор ещё не зарегистрирован в таблице `users`."""


class ProposalNotFoundError(ProposalError):
    """Предложение/место/исходный кирпичик недоступны актору."""


class ProposalConflictError(ProposalError):
    """Технический код занят в целевой области."""


@dataclass(frozen=True)
class BrickProposalSnapshot:
    """Снимок предложения, пригодный после закрытия сессии."""

    id: int
    author_user_id: int
    author_telegram_user_id: int
    author_username: str | None
    author_full_name: str | None
    scope: str
    place_id: int | None
    place_name: str | None
    source_brick_id: int
    title: str
    code: str
    value_type: str
    source: str
    rules: dict[str, object]
    status: str
    created_at: datetime
    decided_at: datetime | None
    decided_by_user_id: int | None
    approved_brick_id: int | None
    decision_note: str | None

    @property
    def is_pending(self) -> bool:
        return self.status == PROPOSAL_STATUS_PENDING

    @property
    def is_system(self) -> bool:
        return self.scope == PROPOSAL_SCOPE_SYSTEM

    @property
    def author_label(self) -> str:
        if self.author_full_name:
            label = self.author_full_name
            if self.author_username:
                label += f" (@{self.author_username})"
            return label
        if self.author_username:
            return f"@{self.author_username}"
        return f"ID {self.author_telegram_user_id}"

    @property
    def target_label(self) -> str:
        if self.is_system:
            return "вся система"
        return self.place_name or f"место #{self.place_id}"


@dataclass(frozen=True)
class ProposalDecisionResult:
    """Результат попытки решения/отзыва без необработанных исключений."""

    status: str
    proposal: BrickProposalSnapshot | None = None
    brick: BrickDefinition | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in {
            PROPOSAL_STATUS_APPROVED,
            PROPOSAL_STATUS_REJECTED,
            PROPOSAL_STATUS_WITHDRAWN,
        }


def _valid_telegram_user_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _actor(session: Session, telegram_user_id: int | None) -> User | None:
    if not _valid_telegram_user_id(telegram_user_id):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _require_actor(session: Session, telegram_user_id: int | None) -> User:
    actor = _actor(session, telegram_user_id)
    if actor is None:
        raise ProposalActorError(
            "Пользователь ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _require_place(session: Session, place_id: object) -> Place:
    if isinstance(place_id, bool) or not isinstance(place_id, int) or place_id <= 0:
        raise ProposalValidationError("Выберите место из справочника.")
    place = session.get(Place, place_id)
    if place is None:
        raise ProposalNotFoundError("Место не найдено в справочнике.")
    return place


def _safe_rules(value_type: str, raw: str | None) -> dict[str, object]:
    """Правила для экрана; ошибку формата окончательно проверяет одобрение."""
    try:
        return rules_from_json(value_type, raw)
    except BrickValidationError:
        return {}


def _proposal_statement():
    return (
        select(BrickProposal, User, PlaceName.name)
        .join(User, User.id == BrickProposal.author_user_id)
        .outerjoin(
            PlaceName,
            (PlaceName.place_id == BrickProposal.place_id)
            & (PlaceName.is_canonical == 1),
        )
        # После условного UPDATE в той же сессии identity map может хранить
        # старое значение. Карточка решения всегда читает строку из БД.
        .execution_options(populate_existing=True)
    )


def _snapshot(
    row: BrickProposal,
    author: User,
    place_name: str | None,
) -> BrickProposalSnapshot:
    return BrickProposalSnapshot(
        id=row.id,
        author_user_id=row.author_user_id,
        author_telegram_user_id=author.telegram_user_id,
        author_username=author.username,
        author_full_name=author.full_name,
        scope=row.scope,
        place_id=row.place_id,
        place_name=place_name,
        source_brick_id=row.source_brick_id,
        title=row.title,
        code=row.code,
        value_type=row.value_type,
        source=row.source,
        rules=_safe_rules(row.value_type, row.rules_json),
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by_user_id=row.decided_by_user_id,
        approved_brick_id=row.approved_brick_id,
        decision_note=row.decision_note,
    )


def _load_by_id(session: Session, proposal_id: int) -> BrickProposalSnapshot | None:
    row = session.execute(
        _proposal_statement().where(BrickProposal.id == proposal_id)
    ).first()
    if row is None:
        return None
    proposal, author, place_name = row
    return _snapshot(proposal, author, place_name)


def _brick_definition(row: Brick) -> BrickDefinition:
    return BrickDefinition(
        id=row.id,
        code=row.code,
        title=row.title,
        value_type=row.value_type,
        scope=row.scope,
        source=row.source,
        place_id=row.place_id,
        owner_user_id=row.owner_user_id,
        rules=_safe_rules(row.value_type, row.rules_json),
        status=row.status,
        revision=row.revision,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _begin_write(session: Session) -> None:
    """
    Берёт SQLite write-lock до проверки прав/статуса решения.

    `BEGIN IMMEDIATE` не даёт двум конкурентным решениям прочитать pending и
    затем по очереди записать два результата. Если вызывающий уже начал
    транзакцию с изменениями, сервис не отбрасывает их; в штатном UI сессия
    всегда новая и транзакция начинается здесь.
    """
    if session.in_transaction() and not (
        session.new or session.dirty or session.deleted
    ):
        # Пустая читающая транзакция не даёт нужного снимка для проверки
        # ролей. Сбрасываем её до получения блокировки записи.
        session.rollback()
    connection = session.connection()
    if not connection.in_transaction():
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def _can_decide(session: Session, actor: User, proposal: BrickProposal) -> bool:
    if proposal.scope == PROPOSAL_SCOPE_SYSTEM:
        return is_superadmin(actor.telegram_user_id)
    if proposal.scope == PROPOSAL_SCOPE_PLACE and proposal.place_id is not None:
        return can_manage_place(session, actor.telegram_user_id, proposal.place_id)
    return False


def _permission_error() -> ProposalPermissionError:
    return ProposalPermissionError(
        "Рассматривать это предложение может только действующий администратор "
        "его места или суперадминистратор."
    )


def _validate_target(session: Session, *, scope: str, place_id: object) -> int | None:
    if scope not in PROPOSAL_SCOPES:
        raise ProposalValidationError("Неизвестная область предложения.")
    if scope == PROPOSAL_SCOPE_SYSTEM:
        if place_id is not None:
            raise ProposalValidationError(
                "Для предложения всей системе место не выбирается."
            )
        return None
    place = _require_place(session, place_id)
    return place.id


def _validated_snapshot(
    *,
    title: object,
    code: object,
    value_type: object,
    source: object,
    rules: str | dict[str, object] | None,
) -> tuple[str, str, str, str, dict[str, object], str | None]:
    checked_title = clean_brick_title(title)
    checked_code = validate_brick_code(code)
    checked_type = ensure_brick_type(value_type)
    checked_source = ensure_brick_source(source)
    validated_rules = parse_rules(checked_type, rules)
    return (
        checked_title,
        checked_code,
        checked_type,
        checked_source,
        validated_rules,
        rules_to_json(checked_type, validated_rules),
    )


def _check_target_code(
    session: Session,
    *,
    code: str,
    scope: str,
    place_id: int | None,
    source_brick_id: int | None = None,
) -> BrickCodeAvailability:
    # Для глобальной области исходный личный кирпичик — единственный
    # разрешённый носитель того же кода: он не переписывается при одобрении.
    # Для места этот параметр ничего не меняет (код живёт в другой области).
    return check_code_availability(
        session,
        code,
        scope=scope,
        place_id=place_id,
        owner_user_id=None,
        exclude_brick_id=source_brick_id,
    )


def _conflict_message(availability: BrickCodeAvailability) -> str:
    return (
        (availability.message or "Технический код уже занят.")
        + " Предложение не изменено; выберите другой код в личном кирпичике "
        "и отправьте новое предложение."
    )


def create_brick_proposal(
    session: Session,
    *,
    actor_telegram_user_id: int,
    source_brick_id: int,
    scope: str,
    place_id: int | None = None,
) -> BrickProposalSnapshot:
    """
    Фиксирует снимок действующего личного кирпичика для места или системы.

    Проверяется владелец, активность источника, допустимость области, код и
    правила. Никакое место не создаётся автоматически.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if (
        isinstance(source_brick_id, bool)
        or not isinstance(source_brick_id, int)
        or source_brick_id <= 0
    ):
        raise ProposalNotFoundError("Личный кирпичик не найден.")
    source = session.get(Brick, source_brick_id)
    if (
        source is None
        or source.scope != BRICK_SCOPE_PERSONAL
        or source.owner_user_id != actor.id
    ):
        raise ProposalNotFoundError(
            "Личный кирпичик не найден или принадлежит другому пользователю."
        )
    if source.status != BRICK_STATUS_ACTIVE:
        raise ProposalValidationError(
            "Архивный кирпичик нельзя предложить. Сначала восстановите его."
        )

    target_place_id = _validate_target(session, scope=scope, place_id=place_id)
    try:
        (
            title,
            code,
            value_type,
            source_kind,
            _rules,
            rules_json,
        ) = _validated_snapshot(
            title=source.title,
            code=source.code,
            value_type=source.value_type,
            source=source.source,
            rules=source.rules_json,
        )
    except BrickValidationError as exc:
        raise ProposalValidationError(
            f"Личный кирпичик содержит некорректные данные: {exc}"
        ) from exc
    availability = _check_target_code(
        session,
        code=code,
        scope=scope,
        place_id=target_place_id,
        source_brick_id=source.id,
    )
    if not availability.available:
        raise ProposalConflictError(_conflict_message(availability))

    row = BrickProposal(
        author_user_id=actor.id,
        scope=scope,
        place_id=target_place_id,
        source_brick_id=source.id,
        title=title,
        code=code,
        value_type=value_type,
        source=source_kind,
        rules_json=rules_json,
        status=PROPOSAL_STATUS_PENDING,
        created_at=utcnow(),
    )
    session.add(row)
    try:
        session.commit()
    except (IntegrityError, OperationalError) as exc:
        session.rollback()
        raise ProposalConflictError(
            "Не удалось отправить предложение: код уже занят или ограничение "
            "базы данных не выполнено. Обновите данные и повторите."
        ) from exc
    except Exception:
        session.rollback()
        raise
    snapshot = _load_by_id(session, row.id)
    if snapshot is None:  # pragma: no cover — строка только что создана
        raise ProposalError("Предложение создано, но не удалось перечитать.")
    return snapshot


def get_own_proposal(
    session: Session,
    actor_telegram_user_id: int,
    proposal_id: int,
) -> BrickProposalSnapshot | None:
    """Только своё предложение; чужой ID не раскрывает существование."""
    actor = _actor(session, actor_telegram_user_id)
    if (
        actor is None
        or isinstance(proposal_id, bool)
        or not isinstance(proposal_id, int)
    ):
        return None
    row = session.execute(
        _proposal_statement().where(
            BrickProposal.id == proposal_id,
            BrickProposal.author_user_id == actor.id,
        )
    ).first()
    if row is None:
        return None
    proposal, author, place_name = row
    return _snapshot(proposal, author, place_name)


def list_own_proposals(
    session: Session,
    actor_telegram_user_id: int,
    *,
    offset: int = 0,
    limit: int = PROPOSAL_PAGE_SIZE,
) -> tuple[list[BrickProposalSnapshot], int]:
    """Все свои предложения, новые сверху; статусы не скрываются."""
    actor = _require_actor(session, actor_telegram_user_id)
    statement = (
        select(BrickProposal, User, PlaceName.name)
        .join(User, User.id == BrickProposal.author_user_id)
        .outerjoin(
            PlaceName,
            (PlaceName.place_id == BrickProposal.place_id)
            & (PlaceName.is_canonical == 1),
        )
        .where(BrickProposal.author_user_id == actor.id)
    )
    total = int(
        session.scalar(
            select(func.count(BrickProposal.id)).where(
                BrickProposal.author_user_id == actor.id
            )
        )
        or 0
    )
    rows = session.execute(
        statement.order_by(BrickProposal.created_at.desc(), BrickProposal.id.desc())
        .offset(max(offset, 0))
        .limit(limit)
    ).all()
    return (
        [
            _snapshot(proposal, author, place_name)
            for proposal, author, place_name in rows
        ],
        total,
    )


def get_admin_proposal(
    session: Session,
    actor_telegram_user_id: int,
    proposal_id: int,
) -> BrickProposalSnapshot | None:
    """
    Карточка предложения для действующего администратора.

    Для глобального предложения нужен суперадмин; для места — действующая
    роль именно этого места. Посторонний не получает ни данных, ни признака
    существования предложения.
    """
    actor = _actor(session, actor_telegram_user_id)
    if (
        actor is None
        or isinstance(proposal_id, bool)
        or not isinstance(proposal_id, int)
    ):
        return None
    row = session.execute(
        _proposal_statement().where(BrickProposal.id == proposal_id)
    ).first()
    if row is None:
        return None
    proposal, author, place_name = row
    if not _can_decide(session, actor, proposal):
        return None
    return _snapshot(proposal, author, place_name)


def _pending_statement():
    return (
        select(BrickProposal, User, PlaceName.name)
        .join(User, User.id == BrickProposal.author_user_id)
        .outerjoin(
            PlaceName,
            (PlaceName.place_id == BrickProposal.place_id)
            & (PlaceName.is_canonical == 1),
        )
        .where(BrickProposal.status == PROPOSAL_STATUS_PENDING)
    )


def _page_pending(
    session: Session,
    statement,
    *,
    offset: int,
    limit: int,
) -> tuple[list[BrickProposalSnapshot], int]:
    total = int(
        session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    rows = session.execute(
        statement.order_by(BrickProposal.created_at.asc(), BrickProposal.id.asc())
        .offset(max(offset, 0))
        .limit(limit)
    ).all()
    return (
        [
            _snapshot(proposal, author, place_name)
            for proposal, author, place_name in rows
        ],
        total,
    )


def list_pending_for_place(
    session: Session,
    actor_telegram_user_id: int,
    place_id: int,
    *,
    offset: int = 0,
    limit: int = PROPOSAL_PAGE_SIZE,
) -> tuple[list[BrickProposalSnapshot], int]:
    """Очередь конкретного места; доступна только его действующему админу."""
    place = _require_place(session, place_id)
    if not can_manage_place(session, actor_telegram_user_id, place.id):
        raise ProposalPermissionError(
            "Очередь предложений этого места доступна только его администратору "
            "или суперадминистратору."
        )
    statement = _pending_statement().where(
        BrickProposal.scope == PROPOSAL_SCOPE_PLACE,
        BrickProposal.place_id == place.id,
    )
    return _page_pending(session, statement, offset=offset, limit=limit)


def count_pending_for_place(
    session: Session,
    actor_telegram_user_id: int,
    place_id: int,
) -> int:
    """Число ожидающих предложений места (для кнопки кабинета)."""
    place = _require_place(session, place_id)
    if not can_manage_place(session, actor_telegram_user_id, place.id):
        raise ProposalPermissionError("Нет доступа к предложениям этого места.")
    return int(
        session.scalar(
            select(func.count(BrickProposal.id)).where(
                BrickProposal.scope == PROPOSAL_SCOPE_PLACE,
                BrickProposal.place_id == place.id,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
        )
        or 0
    )


def list_pending_place_proposals(
    session: Session,
    actor_telegram_user_id: int,
    *,
    offset: int = 0,
    limit: int = PROPOSAL_PAGE_SIZE,
) -> tuple[list[BrickProposalSnapshot], int]:
    """Все ожидающие предложения мест: только для суперадминистратора."""
    if not is_superadmin(actor_telegram_user_id):
        raise ProposalPermissionError(
            "Сводная очередь предложений мест доступна только " "суперадминистратору."
        )
    statement = _pending_statement().where(BrickProposal.scope == PROPOSAL_SCOPE_PLACE)
    return _page_pending(session, statement, offset=offset, limit=limit)


def count_pending_place_proposals(session: Session, actor_telegram_user_id: int) -> int:
    if not is_superadmin(actor_telegram_user_id):
        raise ProposalPermissionError(
            "Сводная очередь предложений мест доступна только " "суперадминистратору."
        )
    return int(
        session.scalar(
            select(func.count(BrickProposal.id)).where(
                BrickProposal.scope == PROPOSAL_SCOPE_PLACE,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
        )
        or 0
    )


def list_pending_system_proposals(
    session: Session,
    actor_telegram_user_id: int,
    *,
    offset: int = 0,
    limit: int = PROPOSAL_PAGE_SIZE,
) -> tuple[list[BrickProposalSnapshot], int]:
    """Ожидающие глобальные предложения: только суперадминистратор."""
    if not is_superadmin(actor_telegram_user_id):
        raise ProposalPermissionError(
            "Глобальные предложения рассматривает только суперадминистратор."
        )
    statement = _pending_statement().where(BrickProposal.scope == PROPOSAL_SCOPE_SYSTEM)
    return _page_pending(session, statement, offset=offset, limit=limit)


def count_pending_system_proposals(
    session: Session, actor_telegram_user_id: int
) -> int:
    if not is_superadmin(actor_telegram_user_id):
        raise ProposalPermissionError(
            "Глобальные предложения рассматривает только суперадминистратор."
        )
    return int(
        session.scalar(
            select(func.count(BrickProposal.id)).where(
                BrickProposal.scope == PROPOSAL_SCOPE_SYSTEM,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
        )
        or 0
    )


def _result(
    session: Session,
    proposal_id: int,
    *,
    status: str,
    message: str | None = None,
    brick: BrickDefinition | None = None,
) -> ProposalDecisionResult:
    return ProposalDecisionResult(
        status=status,
        proposal=_load_by_id(session, proposal_id),
        brick=brick,
        message=message,
    )


def _decision_note(note: str | None) -> str | None:
    if note is None:
        return None
    if not isinstance(note, str):
        raise ProposalValidationError("Причина отклонения должна быть текстом.")
    cleaned = " ".join(note.split())
    if len(cleaned) > MAX_DECISION_NOTE_LENGTH:
        raise ProposalValidationError(
            "Причина отклонения слишком длинная: максимум "
            f"{MAX_DECISION_NOTE_LENGTH} символов."
        )
    return cleaned or None


def approve_brick_proposal(
    session: Session,
    *,
    actor_telegram_user_id: int,
    proposal_id: int,
) -> ProposalDecisionResult:
    """
    Одобряет pending-предложение и создаёт отдельный кирпичик.

    Вся операция — одна транзакция: проверка актора/прав/статуса/области,
    проверка кода и правил, INSERT `bricks`, условный UPDATE предложения,
    COMMIT. Любая ошибка откатывает INSERT и не оставляет `approved`.
    """
    actor = _require_actor(session, actor_telegram_user_id)
    if isinstance(proposal_id, bool) or not isinstance(proposal_id, int):
        raise ProposalNotFoundError("Предложение не найдено.")
    try:
        _begin_write(session)
        proposal = session.get(BrickProposal, proposal_id)
        if proposal is None:
            session.rollback()
            return ProposalDecisionResult(
                status="not_found", message="Предложение не найдено."
            )
        if not _can_decide(session, actor, proposal):
            session.rollback()
            raise _permission_error()
        if proposal.status != PROPOSAL_STATUS_PENDING:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="already_decided",
                message=_status_message(proposal.status),
            )

        target_place_id = _validate_target(
            session, scope=proposal.scope, place_id=proposal.place_id
        )
        try:
            (
                title,
                code,
                value_type,
                source_kind,
                _rules,
                rules_json,
            ) = _validated_snapshot(
                title=proposal.title,
                code=proposal.code,
                value_type=proposal.value_type,
                source=proposal.source,
                rules=proposal.rules_json,
            )
        except BrickValidationError:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="invalid",
                message=(
                    "Снимок предложения содержит некорректный тип или настройки. "
                    "Создание кирпичика отменено; предложение осталось ожидающим."
                ),
            )
        availability = _check_target_code(
            session,
            code=code,
            scope=proposal.scope,
            place_id=target_place_id,
            source_brick_id=proposal.source_brick_id,
        )
        if not availability.available:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="conflict",
                message=_conflict_message(availability),
            )

        now = utcnow()
        brick = Brick(
            code=code,
            title=title,
            value_type=value_type,
            scope=proposal.scope,
            source=source_kind,
            place_id=target_place_id,
            owner_user_id=None,
            rules_json=rules_json,
            status=BRICK_STATUS_ACTIVE,
            revision=1,
            created_by_user_id=actor.id,
            created_at=now,
            updated_at=now,
        )
        session.add(brick)
        session.flush()
        changed = session.execute(
            sqlalchemy_update(BrickProposal)
            .where(
                BrickProposal.id == proposal_id,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
            .values(
                status=PROPOSAL_STATUS_APPROVED,
                decided_at=now,
                decided_by_user_id=actor.id,
                approved_brick_id=brick.id,
                decision_note=None,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(changed, "rowcount", 0) != 1:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="stale",
                message="Предложение уже рассмотрено в другом окне.",
            )
        session.commit()
    except (ProposalPermissionError, ProposalValidationError, ProposalNotFoundError):
        session.rollback()
        raise
    except (IntegrityError, OperationalError, SQLAlchemyError):
        session.rollback()
        return _result(
            session,
            proposal_id,
            status="error",
            message=(
                "Не удалось одобрить предложение из-за конфликта или ошибки "
                "базы данных. Предложение осталось ожидающим."
            ),
        )
    except Exception:
        session.rollback()
        raise

    brick_row = session.get(Brick, brick.id)
    return _result(
        session,
        proposal_id,
        status=PROPOSAL_STATUS_APPROVED,
        message="Предложение одобрено; создан отдельный кирпичик.",
        brick=_brick_definition(brick_row) if brick_row is not None else None,
    )


def reject_brick_proposal(
    session: Session,
    *,
    actor_telegram_user_id: int,
    proposal_id: int,
    note: str | None = None,
) -> ProposalDecisionResult:
    """Отклоняет pending-предложение, не затрагивая личный кирпичик автора."""
    actor = _require_actor(session, actor_telegram_user_id)
    if isinstance(proposal_id, bool) or not isinstance(proposal_id, int):
        raise ProposalNotFoundError("Предложение не найдено.")
    checked_note = _decision_note(note)
    try:
        _begin_write(session)
        proposal = session.get(BrickProposal, proposal_id)
        if proposal is None:
            session.rollback()
            return ProposalDecisionResult(
                status="not_found", message="Предложение не найдено."
            )
        if not _can_decide(session, actor, proposal):
            session.rollback()
            raise _permission_error()
        if proposal.status != PROPOSAL_STATUS_PENDING:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="already_decided",
                message=_status_message(proposal.status),
            )
        now = utcnow()
        changed = session.execute(
            sqlalchemy_update(BrickProposal)
            .where(
                BrickProposal.id == proposal_id,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
            .values(
                status=PROPOSAL_STATUS_REJECTED,
                decided_at=now,
                decided_by_user_id=actor.id,
                approved_brick_id=None,
                decision_note=checked_note,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(changed, "rowcount", 0) != 1:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="stale",
                message="Предложение уже рассмотрено в другом окне.",
            )
        session.commit()
    except (ProposalPermissionError, ProposalValidationError, ProposalNotFoundError):
        session.rollback()
        raise
    except (IntegrityError, OperationalError, SQLAlchemyError):
        session.rollback()
        return _result(
            session,
            proposal_id,
            status="error",
            message=(
                "Не удалось отклонить предложение из-за ошибки базы данных. "
                "Предложение осталось ожидающим."
            ),
        )
    except Exception:
        session.rollback()
        raise
    return _result(
        session,
        proposal_id,
        status=PROPOSAL_STATUS_REJECTED,
        message="Предложение отклонено.",
    )


def withdraw_brick_proposal(
    session: Session,
    *,
    actor_telegram_user_id: int,
    proposal_id: int,
) -> ProposalDecisionResult:
    """Отзывает pending-предложение только его автор."""
    actor = _require_actor(session, actor_telegram_user_id)
    if isinstance(proposal_id, bool) or not isinstance(proposal_id, int):
        return ProposalDecisionResult(
            status="not_found", message="Предложение не найдено."
        )
    try:
        _begin_write(session)
        proposal = session.get(BrickProposal, proposal_id)
        if proposal is None or proposal.author_user_id != actor.id:
            session.rollback()
            return ProposalDecisionResult(
                status="not_found", message="Предложение не найдено."
            )
        if proposal.status != PROPOSAL_STATUS_PENDING:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="already_decided",
                message=_status_message(proposal.status),
            )
        now = utcnow()
        changed = session.execute(
            sqlalchemy_update(BrickProposal)
            .where(
                BrickProposal.id == proposal_id,
                BrickProposal.author_user_id == actor.id,
                BrickProposal.status == PROPOSAL_STATUS_PENDING,
            )
            .values(
                status=PROPOSAL_STATUS_WITHDRAWN,
                decided_at=now,
                decided_by_user_id=actor.id,
                approved_brick_id=None,
                decision_note=None,
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(changed, "rowcount", 0) != 1:
            session.rollback()
            return _result(
                session,
                proposal_id,
                status="stale",
                message="Предложение уже рассмотрено в другом окне.",
            )
        session.commit()
    except (IntegrityError, OperationalError, SQLAlchemyError):
        session.rollback()
        return _result(
            session,
            proposal_id,
            status="error",
            message=(
                "Не удалось отозвать предложение из-за ошибки базы данных. "
                "Предложение осталось ожидающим."
            ),
        )
    except Exception:
        session.rollback()
        raise
    return _result(
        session,
        proposal_id,
        status=PROPOSAL_STATUS_WITHDRAWN,
        message="Предложение отозвано.",
    )


def _status_message(status: str) -> str:
    if status == PROPOSAL_STATUS_APPROVED:
        return "Предложение уже одобрено. Повторное решение невозможно."
    if status == PROPOSAL_STATUS_REJECTED:
        return "Предложение уже отклонено. Повторное решение невозможно."
    if status == PROPOSAL_STATUS_WITHDRAWN:
        return "Предложение отозвано автором. Решение невозможно."
    return "Предложение уже не ожидает решения."


__all__ = [
    "MAX_DECISION_NOTE_LENGTH",
    "PROPOSAL_PAGE_SIZE",
    "PROPOSAL_SCOPES",
    "PROPOSAL_SCOPE_PLACE",
    "PROPOSAL_SCOPE_SYSTEM",
    "PROPOSAL_STATUSES",
    "PROPOSAL_STATUS_APPROVED",
    "PROPOSAL_STATUS_PENDING",
    "PROPOSAL_STATUS_REJECTED",
    "PROPOSAL_STATUS_WITHDRAWN",
    "BrickProposalSnapshot",
    "ProposalActorError",
    "ProposalConflictError",
    "ProposalDecisionResult",
    "ProposalError",
    "ProposalNotFoundError",
    "ProposalPermissionError",
    "ProposalValidationError",
    "approve_brick_proposal",
    "count_pending_for_place",
    "count_pending_place_proposals",
    "count_pending_system_proposals",
    "create_brick_proposal",
    "get_admin_proposal",
    "get_own_proposal",
    "list_own_proposals",
    "list_pending_for_place",
    "list_pending_place_proposals",
    "list_pending_system_proposals",
    "reject_brick_proposal",
    "withdraw_brick_proposal",
]
