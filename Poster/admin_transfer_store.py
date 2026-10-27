# admin_transfer_store.py
"""
Передача администратора места (Stage 21).

Правила этапа:

- предложение создаёт только ДЕЙСТВУЮЩИЙ администратор этого места;
  глобальная роль суперадминистратора сама по себе права передачи не даёт;
- до подтверждения получателем строка `place_admins` не меняется — прежний
  администратор сохраняет полномочия;
- известный получатель фиксируется по `users.id` (постоянный Telegram user ID),
  а не по username; смена username не меняет адресата;
- незнакомый получатель получает одноразовую ссылку. В БД хранится только
  SHA-256 токена. Первое открытие фиксирует кандидата, но не даёт прав:
  инициатор должен явно утвердить кандидата, после чего тот подтверждает
  предложение в своём аккаунте;
- подтверждение атомарно: закрытие предложения, отзыв старого назначения и
  создание нового выполняются в одной транзакции; частичный уникальный
  индекс `place_admins` не даёт двум администраторам быть активными;
- срок — ровно 24 часа (`now < expires_at`); проверка ленивая, при чтении и
  внутри подтверждения. Планировщик не используется;
- после замены/снятия администратора суперадмином ожидающее предложение
  становится `superseded` (см. `place_admin_store`).

Сырой токен возвращается только вызывающему слою для формирования ссылки.
Он не логируется и не сохраняется в SQLite.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import (
    AdminTransfer,
    Place,
    PlaceAdmin,
    PlaceName,
    User,
    utcnow,
)

logger = logging.getLogger(__name__)

TRANSFER_TTL = timedelta(hours=24)
TOKEN_BYTES = 32
USERNAME_MIN_LENGTH = 5
USERNAME_MAX_LENGTH = 32

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_SUPERSEDED = "superseded"

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")


class TransferValueError(ValueError):
    """Некорректный username получателя."""


class TransferPermissionError(PermissionError):
    """Операция доступна только действующему администратору места."""


class TransferActorError(RuntimeError):
    """Пользователь ещё не зарегистрирован в таблице users."""


@dataclass(frozen=True)
class TransferUser:
    """Снимок пользователя для экрана передачи."""

    user_id: int
    telegram_user_id: int
    username: str | None
    full_name: str | None


@dataclass(frozen=True)
class TransferView:
    """Безопасный снимок предложения без токена и ORM-объектов."""

    transfer_id: int
    place_id: int
    place_name: str
    from_user: TransferUser
    to_user: TransferUser | None
    opened_by: TransferUser | None
    to_username: str | None
    status: str
    created_at: datetime
    expires_at: datetime
    opened_at: datetime | None
    confirmed_at: datetime | None
    closed_at: datetime | None
    notified_at: datetime | None
    has_invite_token: bool

    @property
    def awaiting_initiator_approval(self) -> bool:
        """Ссылку открыл кандидат, но инициатор ещё не утвердил получателя."""
        return (
            self.status == STATUS_PENDING
            and self.to_user is None
            and self.opened_by is not None
        )

    @property
    def awaiting_recipient(self) -> bool:
        """Получатель известен, но ещё не подтвердил предложение."""
        return self.status == STATUS_PENDING and self.to_user is not None


@dataclass(frozen=True)
class TransferCreationResult:
    """Создание предложения; invite_token заполнен только для ссылки."""

    status: str
    transfer: TransferView | None = None
    invite_token: str | None = None
    recipient_telegram_user_id: int | None = None


@dataclass(frozen=True)
class TransferMutationResult:
    """Результат утверждения, отмены, отказа или подтверждения."""

    status: str
    transfer: TransferView | None = None
    target_telegram_user_id: int | None = None
    previous_admin_telegram_user_id: int | None = None


def normalize_transfer_username(raw: str) -> str:
    """
    Проверяет username получателя и возвращает его без ведущего «@».

    Telegram username: 5–32 символа, латиница/цифры/подчёркивание, начинается
    с буквы. Права по username не выдаются — значение служит только подсказкой
    для поиска известного пользователя или текстом приглашения.
    """
    if not isinstance(raw, str):
        raise TransferValueError("Username должен быть строкой.")
    cleaned = raw.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    if not cleaned:
        raise TransferValueError("Введите Telegram username получателя.")
    if any(char.isspace() for char in cleaned):
        raise TransferValueError("Username не должен содержать пробелов.")
    if not _USERNAME_RE.fullmatch(cleaned):
        raise TransferValueError(
            "Некорректный username: 5–32 символа, латиница, цифры и «_», "
            "начинается с буквы."
        )
    return cleaned


def _lower_username(username: str) -> str:
    return username.casefold()


def _user_snapshot(user: User | None) -> TransferUser | None:
    if user is None:
        return None
    return TransferUser(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        username=user.username,
        full_name=user.full_name,
    )


def _user_by_telegram_id(session: Session, telegram_user_id: int) -> User | None:
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _active_assignment(
    session: Session, place_id: int
) -> tuple[PlaceAdmin, User] | None:
    row = session.execute(
        select(PlaceAdmin, User)
        .join(User, User.id == PlaceAdmin.user_id)
        .where(
            PlaceAdmin.place_id == place_id,
            PlaceAdmin.revoked_at.is_(None),
        )
        .limit(1)
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def _place_name(session: Session, place_id: int) -> str | None:
    return session.scalar(
        select(PlaceName.name).where(
            PlaceName.place_id == place_id,
            PlaceName.is_canonical == 1,
        )
    )


def _result_after_lost_race(
    session: Session, row: AdminTransfer, actor: User
) -> TransferMutationResult:
    """
    Честный итог для проигравшего гонку подтверждения.

    Условный UPDATE не сработал, значит предложение уже закрыто другим
    потоком/процессом. Своя копия строки устарела (SQLite-чтения вне
    транзакции видят свежие данные, а объект в сессии — нет), поэтому статус
    перечитывается. Получатель, чьё подтверждение уже победило, должен
    увидеть «уже подтверждено», а не «администратор изменился».
    """
    session.refresh(row)
    if row.status == STATUS_CONFIRMED and row.to_user_id == actor.id:
        return TransferMutationResult(
            status="already_confirmed", transfer=_view(session, row)
        )
    return TransferMutationResult(status="conflict", transfer=_view(session, row))


def _view(session: Session, row: AdminTransfer) -> TransferView:
    from_user = session.get(User, row.from_user_id)
    to_user = session.get(User, row.to_user_id) if row.to_user_id else None
    opened_by = (
        session.get(User, row.opened_by_user_id) if row.opened_by_user_id else None
    )
    return TransferView(
        transfer_id=row.id,
        place_id=row.place_id,
        place_name=str(_place_name(session, row.place_id) or "Без названия"),
        from_user=_user_snapshot(from_user)
        or TransferUser(0, 0, None, "Неизвестный инициатор"),
        to_user=_user_snapshot(to_user),
        opened_by=_user_snapshot(opened_by),
        to_username=row.to_username,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        opened_at=row.opened_at,
        confirmed_at=row.confirmed_at,
        closed_at=row.closed_at,
        notified_at=row.notified_at,
        has_invite_token=row.token_hash is not None,
    )


def _close_pending(
    session: Session,
    transfer_id: int,
    status: str,
    *,
    now: datetime,
) -> bool:
    """
    Условно закрывает ТОЛЬКО ещё pending-предложение.

    Возвращает True ровно для одной выигравшей операции; конкурентный вызов
    получает False и не меняет права. Транзакцию закрывает вызывающий.
    """
    closed = session.execute(
        update(AdminTransfer)
        .where(
            AdminTransfer.id == transfer_id,
            AdminTransfer.status == STATUS_PENDING,
        )
        .values(status=status, closed_at=now)
    )
    return getattr(closed, "rowcount", 0) == 1


def _reconcile_pending(session: Session, row: AdminTransfer) -> AdminTransfer:
    """
    Лениво применяет срок и актуальность администратора.

    Вызывается при чтении/действии. Если предложение просрочено или прежний
    администратор уже не является действующим (либо назначение заменено),
    строка получает терминальный статус. Ошибка записи не открывает прав:
    подтверждение всё равно перепроверит все условия в своей транзакции.
    """
    if row.status != STATUS_PENDING:
        return row
    now = utcnow()
    terminal: str | None = None
    if now >= row.expires_at:
        terminal = STATUS_EXPIRED
    else:
        active = _active_assignment(session, row.place_id)
        if (
            active is None
            or active[0].id != row.from_assignment_id
            or active[1].id != row.from_user_id
        ):
            terminal = STATUS_SUPERSEDED
    if terminal is None:
        return row
    try:
        if _close_pending(session, row.id, terminal, now=now):
            session.commit()
        else:
            session.rollback()
    except Exception as e:  # noqa: BLE001 — чтение не должно падать из-за статуса
        session.rollback()
        logger.error("Не удалось обновить статус предложения передачи: %s", e)
    try:
        session.refresh(row)
    except Exception:  # noqa: BLE001 — строка могла быть удалена вместе с местом
        pass
    return row


def find_known_recipients(session: Session, username: str) -> tuple[TransferUser, ...]:
    """
    Точное (без учёта регистра) совпадение username среди известных боту.

    Частичный поиск здесь опасен: он подсказывает не того адресата. Несколько
    строк с одинаковым username возможны только из старых данных; UI обязан
    дать выбрать конкретный постоянный ID.
    """
    needle = _lower_username(normalize_transfer_username(username))
    users = list(
        session.scalars(
            select(User)
            .where(func.lower(User.username) == needle)
            .order_by(User.telegram_user_id)
        ).all()
    )
    return tuple(
        snapshot for user in users if (snapshot := _user_snapshot(user)) is not None
    )


def _new_invite_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_invite_token(token: str) -> str:
    """SHA-256 токена для хранения; открытый токен остаётся только в ссылке."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_admin_transfer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    username: str,
    recipient_telegram_user_id: int | None = None,
) -> TransferCreationResult:
    """
    Создаёт предложение передачи на 24 часа.

    `recipient_telegram_user_id` передаётся только после явного выбора
    известного пользователя. Без него создаётся ссылка-приглашение: даже если
    username уже известен боту, автоматической привязки по тексту нет.
    """
    normalized_username = normalize_transfer_username(username)
    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Инициатор передачи не зарегистрирован в таблице пользователей."
        )
    if session.get(Place, place_id) is None:
        return TransferCreationResult(status="place_missing")

    active = _active_assignment(session, place_id)
    if active is None or active[1].id != actor.id:
        raise TransferPermissionError(
            "Передать админку может только действующий администратор этого места."
        )
    assignment, _current_admin = active

    if actor.username and _lower_username(actor.username) == _lower_username(
        normalized_username
    ):
        return TransferCreationResult(status="self")

    pending = session.scalar(
        select(AdminTransfer).where(
            AdminTransfer.place_id == place_id,
            AdminTransfer.status == STATUS_PENDING,
        )
    )
    if pending is not None:
        _reconcile_pending(session, pending)
        if pending.status == STATUS_PENDING:
            return TransferCreationResult(
                status="active_exists", transfer=_view(session, pending)
            )

    target: User | None = None
    invite_token: str | None = None
    token_hash: str | None = None
    if recipient_telegram_user_id is not None:
        target = _user_by_telegram_id(session, recipient_telegram_user_id)
        if target is None:
            return TransferCreationResult(status="user_missing")
        if target.id == actor.id:
            return TransferCreationResult(status="self")
    else:
        invite_token = _new_invite_token()
        token_hash = hash_invite_token(invite_token)

    now = utcnow()
    row = AdminTransfer(
        place_id=place_id,
        from_user_id=actor.id,
        from_assignment_id=assignment.id,
        to_user_id=target.id if target is not None else None,
        opened_by_user_id=None,
        to_username=normalized_username,
        token_hash=token_hash,
        status=STATUS_PENDING,
        created_at=now,
        expires_at=now + TRANSFER_TTL,
    )
    session.add(row)
    try:
        session.flush()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferCreationResult(status="active_exists")
    except Exception:
        session.rollback()
        raise
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferCreationResult(status="active_exists")
    except Exception:
        session.rollback()
        raise

    status = "created_known" if target is not None else "created_invite"
    return TransferCreationResult(
        status=status,
        transfer=_view(session, row),
        invite_token=invite_token,
        recipient_telegram_user_id=(
            target.telegram_user_id if target is not None else None
        ),
    )


def get_transfer_view(session: Session, transfer_id: int) -> TransferView | None:
    """Актуальный снимок предложения; просрочка/смена админа применяются лениво."""
    row = session.get(AdminTransfer, transfer_id)
    if row is None:
        return None
    row = _reconcile_pending(session, row)
    return _view(session, row)


def get_pending_transfer_for_place(
    session: Session, place_id: int
) -> TransferView | None:
    """Единственное активное предложение места или None."""
    row = session.scalar(
        select(AdminTransfer).where(
            AdminTransfer.place_id == place_id,
            AdminTransfer.status == STATUS_PENDING,
        )
    )
    if row is None:
        return None
    row = _reconcile_pending(session, row)
    if row.status != STATUS_PENDING:
        return None
    return _view(session, row)


def mark_transfer_notified(session: Session, transfer_id: int) -> bool:
    """
    Отмечает успешную доставку уведомления получателю.

    Вызывается только после ответа Telegram API об успехе; при ошибке
    `notified_at` остаётся NULL и инициатор видит, что доставка не подтверждена.
    """
    updated = session.execute(
        update(AdminTransfer)
        .where(
            AdminTransfer.id == transfer_id,
            AdminTransfer.status == STATUS_PENDING,
            AdminTransfer.to_user_id.is_not(None),
        )
        .values(notified_at=utcnow())
    )
    if getattr(updated, "rowcount", 0) != 1:
        session.rollback()
        return False
    session.commit()
    return True


def approve_transfer_recipient(
    session: Session,
    *,
    actor_telegram_user_id: int,
    transfer_id: int,
) -> TransferMutationResult:
    """
    Инициатор утверждает кандидата, открывшего ссылку.

    Только после этого появляется `to_user_id`; кнопки подтверждения у
    получателя и сама передача прав всё ещё требуют отдельного согласия.
    """
    row = session.get(AdminTransfer, transfer_id)
    if row is None:
        return TransferMutationResult(status="not_found")
    row = _reconcile_pending(session, row)
    if row.status != STATUS_PENDING:
        return TransferMutationResult(status=row.status, transfer=_view(session, row))

    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Инициатор передачи не зарегистрирован в таблице пользователей."
        )
    if actor.id != row.from_user_id:
        return TransferMutationResult(
            status="not_initiator", transfer=_view(session, row)
        )

    active = _active_assignment(session, row.place_id)
    if (
        active is None
        or active[0].id != row.from_assignment_id
        or active[1].id != row.from_user_id
    ):
        return TransferMutationResult(status="superseded", transfer=_view(session, row))

    if row.to_user_id is not None:
        return TransferMutationResult(
            status="already_approved", transfer=_view(session, row)
        )
    if row.opened_by_user_id is None:
        return TransferMutationResult(status="not_opened", transfer=_view(session, row))
    if row.opened_by_user_id == row.from_user_id:
        return TransferMutationResult(status="self", transfer=_view(session, row))

    candidate = session.get(User, row.opened_by_user_id)
    if candidate is None:
        return TransferMutationResult(
            status="candidate_missing", transfer=_view(session, row)
        )

    now = utcnow()
    updated = session.execute(
        update(AdminTransfer)
        .where(
            AdminTransfer.id == transfer_id,
            AdminTransfer.status == STATUS_PENDING,
            AdminTransfer.to_user_id.is_(None),
            AdminTransfer.opened_by_user_id == candidate.id,
            AdminTransfer.expires_at > now,
        )
        .values(to_user_id=candidate.id)
    )
    if getattr(updated, "rowcount", 0) != 1:
        session.rollback()
        return TransferMutationResult(status="conflict")
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferMutationResult(status="conflict")
    except Exception:
        session.rollback()
        raise
    session.refresh(row)
    return TransferMutationResult(
        status="approved",
        transfer=_view(session, row),
        target_telegram_user_id=candidate.telegram_user_id,
    )


def cancel_admin_transfer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    transfer_id: int,
) -> TransferMutationResult:
    """Инициатор отменяет ожидающее предложение; права не меняются."""
    row = session.get(AdminTransfer, transfer_id)
    if row is None:
        return TransferMutationResult(status="not_found")
    row = _reconcile_pending(session, row)
    if row.status != STATUS_PENDING:
        return TransferMutationResult(status=row.status, transfer=_view(session, row))

    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Инициатор передачи не зарегистрирован в таблице пользователей."
        )
    if actor.id != row.from_user_id:
        return TransferMutationResult(
            status="not_initiator", transfer=_view(session, row)
        )

    now = utcnow()
    if not _close_pending(session, transfer_id, STATUS_CANCELLED, now=now):
        session.rollback()
        return TransferMutationResult(status="conflict")
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferMutationResult(status="conflict")
    except Exception:
        session.rollback()
        raise
    session.refresh(row)
    return TransferMutationResult(status="cancelled", transfer=_view(session, row))


def reject_admin_transfer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    transfer_id: int,
) -> TransferMutationResult:
    """Получатель отклоняет предложение; права инициатора сохраняются."""
    row = session.get(AdminTransfer, transfer_id)
    if row is None:
        return TransferMutationResult(status="not_found")
    row = _reconcile_pending(session, row)
    if row.status != STATUS_PENDING:
        return TransferMutationResult(status=row.status, transfer=_view(session, row))

    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Получатель передачи не зарегистрирован в таблице пользователей."
        )
    if row.to_user_id is None or row.to_user_id != actor.id:
        return TransferMutationResult(
            status="not_recipient", transfer=_view(session, row)
        )

    now = utcnow()
    updated = session.execute(
        update(AdminTransfer)
        .where(
            AdminTransfer.id == transfer_id,
            AdminTransfer.status == STATUS_PENDING,
            AdminTransfer.to_user_id == actor.id,
        )
        .values(status=STATUS_REJECTED, closed_at=now)
    )
    if getattr(updated, "rowcount", 0) != 1:
        session.rollback()
        return TransferMutationResult(status="conflict")
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferMutationResult(status="conflict")
    except Exception:
        session.rollback()
        raise
    session.refresh(row)
    return TransferMutationResult(status="rejected", transfer=_view(session, row))


def confirm_admin_transfer(
    session: Session,
    *,
    actor_telegram_user_id: int,
    transfer_id: int,
) -> TransferMutationResult:
    """
    Получатель подтверждает предложение и получает админку атомарно.

    Проверки и изменения идут в одной транзакции SQLite:
    предложение всё ещё pending и не просрочено, получатель — именно
    нажавший пользователь, прежнее назначение всё ещё активно. Только затем
    закрывается предложение, отзывается старая строка `place_admins` и
    создаётся новая. Любая ошибка откатывает всё; rowcount каждого
    условного UPDATE обязателен.
    """
    row = session.get(AdminTransfer, transfer_id)
    if row is None:
        return TransferMutationResult(status="not_found")

    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Получатель передачи не зарегистрирован в таблице пользователей."
        )

    if row.status == STATUS_CONFIRMED:
        if row.to_user_id == actor.id:
            return TransferMutationResult(
                status="already_confirmed", transfer=_view(session, row)
            )
        return TransferMutationResult(status="not_recipient")

    if row.to_user_id is None:
        return TransferMutationResult(status="not_recipient")
    if row.to_user_id != actor.id:
        return TransferMutationResult(status="not_recipient")
    if row.status != STATUS_PENDING:
        return TransferMutationResult(status=row.status, transfer=_view(session, row))

    now = utcnow()
    if now >= row.expires_at:
        closed_expired = _close_pending(session, transfer_id, STATUS_EXPIRED, now=now)
        try:
            session.commit()
        except (IntegrityError, OperationalError):
            session.rollback()
        except Exception:
            session.rollback()
            raise
        if not closed_expired:
            # Пока шли проверки, предложение закрыл другой вызов: нельзя
            # отдавать устаревший «expired» вместо фактического исхода.
            return _result_after_lost_race(session, row, actor)
        session.refresh(row)
        return TransferMutationResult(status="expired", transfer=_view(session, row))

    active = _active_assignment(session, row.place_id)
    if (
        active is None
        or active[0].id != row.from_assignment_id
        or active[1].id != row.from_user_id
    ):
        # `row.status` мог быть прочитан ДО конкурентного подтверждения, а
        # SQLite-чтения вне явной транзакции сразу видят свежие строки. Поэтому
        # «администратор изменился» — это только гипотеза: она подтверждается
        # условным закрытием pending. Если строку уже закрыл победитель гонки,
        # отдаём его исход, а не «superseded» (дефект флейка Stage 21).
        closed_superseded = _close_pending(
            session, transfer_id, STATUS_SUPERSEDED, now=now
        )
        try:
            session.commit()
        except (IntegrityError, OperationalError):
            session.rollback()
        except Exception:
            session.rollback()
            raise
        if not closed_superseded:
            return _result_after_lost_race(session, row, actor)
        session.refresh(row)
        return TransferMutationResult(status="superseded", transfer=_view(session, row))

    old_assignment, old_admin = active
    if old_admin.id == actor.id:
        return TransferMutationResult(status="already_admin")

    try:
        closed = session.execute(
            update(AdminTransfer)
            .where(
                AdminTransfer.id == transfer_id,
                AdminTransfer.status == STATUS_PENDING,
                AdminTransfer.to_user_id == actor.id,
                AdminTransfer.expires_at > now,
            )
            .values(
                status=STATUS_CONFIRMED,
                confirmed_at=now,
                closed_at=now,
            )
        )
        if getattr(closed, "rowcount", 0) != 1:
            session.rollback()
            return _result_after_lost_race(session, row, actor)

        revoked = session.execute(
            update(PlaceAdmin)
            .where(
                PlaceAdmin.id == old_assignment.id,
                PlaceAdmin.place_id == row.place_id,
                PlaceAdmin.user_id == old_admin.id,
                PlaceAdmin.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_by_user_id=actor.id)
        )
        if getattr(revoked, "rowcount", 0) != 1:
            session.rollback()
            return TransferMutationResult(status="superseded")

        session.add(
            PlaceAdmin(
                place_id=row.place_id,
                user_id=actor.id,
                assigned_by_user_id=old_admin.id,
                assigned_at=now,
            )
        )
        session.flush()
    except (IntegrityError, OperationalError):
        session.rollback()
        return TransferMutationResult(status="conflict")
    except Exception:
        session.rollback()
        raise

    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return _result_after_lost_race(session, row, actor)
    except Exception:
        session.rollback()
        raise

    session.refresh(row)
    return TransferMutationResult(
        status="confirmed",
        transfer=_view(session, row),
        target_telegram_user_id=actor.telegram_user_id,
        previous_admin_telegram_user_id=old_admin.telegram_user_id,
    )


def open_transfer_invitation(
    session: Session,
    *,
    transfer_id: int,
    token: str,
    actor_telegram_user_id: int,
) -> TransferMutationResult:
    """
    Открывает ссылку-приглашение и фиксирует первого кандидата.

    Токен проверяется по хешу в постоянном времени. Открытие не назначает
    администратора: при неизвестном получателе `to_user_id` остаётся NULL до
    отдельного утверждения инициатором. Повторное/чужое открытие ссылки не
    перепривязывает кандидата.
    """
    if not isinstance(token, str) or not token:
        return TransferMutationResult(status="invalid_invite")
    row = session.get(AdminTransfer, transfer_id)
    if row is None or row.token_hash is None:
        return TransferMutationResult(status="invalid_invite")
    expected = row.token_hash
    actual = hash_invite_token(token)
    if not hmac.compare_digest(expected, actual):
        return TransferMutationResult(status="invalid_invite")

    actor = _user_by_telegram_id(session, actor_telegram_user_id)
    if actor is None:
        raise TransferActorError(
            "Получатель передачи не зарегистрирован в таблице пользователей."
        )

    row = _reconcile_pending(session, row)
    if row.status != STATUS_PENDING:
        return TransferMutationResult(status=row.status, transfer=_view(session, row))

    if row.to_user_id is not None:
        if row.to_user_id == actor.id:
            return TransferMutationResult(
                status="approved", transfer=_view(session, row)
            )
        return TransferMutationResult(status="not_recipient")

    if actor.id == row.from_user_id:
        return TransferMutationResult(status="self", transfer=_view(session, row))

    if row.opened_by_user_id is None:
        now = utcnow()
        updated = session.execute(
            update(AdminTransfer)
            .where(
                AdminTransfer.id == transfer_id,
                AdminTransfer.status == STATUS_PENDING,
                AdminTransfer.opened_by_user_id.is_(None),
                AdminTransfer.to_user_id.is_(None),
            )
            .values(opened_by_user_id=actor.id, opened_at=now)
        )
        if getattr(updated, "rowcount", 0) != 1:
            session.rollback()
            return TransferMutationResult(status="conflict")
        try:
            session.commit()
        except (IntegrityError, OperationalError):
            session.rollback()
            return TransferMutationResult(status="conflict")
        except Exception:
            session.rollback()
            raise
        session.refresh(row)
        return TransferMutationResult(status="opened", transfer=_view(session, row))

    if row.opened_by_user_id == actor.id:
        return TransferMutationResult(
            status="already_opened", transfer=_view(session, row)
        )
    return TransferMutationResult(
        status="opened_by_other", transfer=_view(session, row)
    )


def transfer_status_label(status: str) -> str:
    """Понятная человеку подпись статуса (для экранов и уведомлений)."""
    return {
        STATUS_PENDING: "ожидает подтверждения",
        STATUS_CONFIRMED: "передача завершена",
        STATUS_REJECTED: "получатель отказался",
        STATUS_CANCELLED: "отменена инициатором",
        STATUS_EXPIRED: "срок истёк",
        STATUS_SUPERSEDED: "недействительна: администратор места изменился",
    }.get(status, status)
