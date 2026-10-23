# place_admin_store.py
"""
Администраторы мест Stage 20A: чтение кабинета и атомарные операции.

Правила:
- суперадминистратор определяется только конфигурацией и проверяется заново
  в каждой операции; роль из callback/состояния экрана не принимается;
- назначение, замена и отзыв сами управляют транзакцией;
- в БД не может быть двух действующих администраторов одного места
  (частичный уникальный индекс `ux_place_admins_one_active`);
- отозванные назначения сохраняются как аудит; замена — это UPDATE старой
  строки + INSERT новой в одной транзакции;
- получатель назначается по постоянному Telegram user ID и должен уже быть
  известен `users`; по одному username права не выдаются.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, exists, func, select, update
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
from permissions import has_any_place_admin_role, is_superadmin

logger = logging.getLogger(__name__)

ADMIN_PLACES_PAGE_SIZE = 8
USER_SEARCH_LIMIT = 8
MAX_USER_SEARCH_LENGTH = 100


class AdminPermissionError(PermissionError):
    """Операция запрещена: вызывающий не является суперадминистратором."""


class AdminActorError(RuntimeError):
    """Суперадминистратор ещё не зарегистрирован в таблице users."""


class UserSearchError(ValueError):
    """Поисковый запрос получателя пустой или слишком длинный."""


@dataclass(frozen=True)
class AdminUser:
    """Снимок пользователя для экрана и подтверждения."""

    user_id: int
    telegram_user_id: int
    username: str | None
    full_name: str | None


@dataclass(frozen=True)
class ActivePlaceAdmin:
    """Действующее назначение места."""

    assignment_id: int
    place_id: int
    user: AdminUser
    assigned_at: datetime


@dataclass(frozen=True)
class ManagedPlace:
    """Строка списка кабинета: место и его текущий администратор."""

    place_id: int
    canonical_name: str
    admin: AdminUser | None
    assigned_at: datetime | None


@dataclass(frozen=True)
class UserSearchResults:
    """Найденные известные пользователи и полное число совпадений."""

    users: tuple[AdminUser, ...]
    total: int


@dataclass(frozen=True)
class AdminMutationResult:
    """Результат назначения/замены/отзыва без ORM-объектов."""

    status: str
    admin: ActivePlaceAdmin | None = None


def _user_snapshot(user: User) -> AdminUser:
    return AdminUser(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        username=user.username,
        full_name=user.full_name,
    )


def _active_row(session: Session, place_id: int) -> tuple[PlaceAdmin, User] | None:
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


def get_active_place_admin(session: Session, place_id: int) -> ActivePlaceAdmin | None:
    """Текущий действующий администратор места (не более одного)."""
    row = _active_row(session, place_id)
    if row is None:
        return None
    assignment, user = row
    return ActivePlaceAdmin(
        assignment_id=assignment.id,
        place_id=assignment.place_id,
        user=_user_snapshot(user),
        assigned_at=assignment.assigned_at,
    )


def _actor_for_superadmin(session: Session, actor_telegram_user_id: int) -> User:
    """Проверяет роль актора по конфигурации и находит его строку в users."""
    if not is_superadmin(actor_telegram_user_id):
        raise AdminPermissionError("Действие доступно только суперадминистратору.")
    actor = session.scalar(
        select(User).where(User.telegram_user_id == actor_telegram_user_id)
    )
    if actor is None:
        raise AdminActorError(
            "Суперадминистратор ещё не зарегистрирован в таблице пользователей."
        )
    return actor


def _target_user(session: Session, telegram_user_id: int) -> User | None:
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        return None
    return session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))


def _supersede_pending_transfers(
    session: Session, place_id: int, *, now: datetime
) -> None:
    """
    Инвалидирует ожидающую передачу при изменении администратора суперадмином.

    Выполняется в той же транзакции, что и замена/снятие. Так старое
    предложение нельзя подтвердить даже после повторного назначения прежнего
    администратора: оно уже закрыто как `superseded`.
    """
    session.execute(
        update(AdminTransfer)
        .where(
            AdminTransfer.place_id == place_id,
            AdminTransfer.status == "pending",
        )
        .values(status="superseded", closed_at=now)
    )


def list_places_for_administration(
    session: Session,
    actor_telegram_user_id: int,
    *,
    offset: int = 0,
    limit: int = ADMIN_PLACES_PAGE_SIZE,
) -> tuple[list[ManagedPlace], int]:
    """
    Страница мест, доступных кабинету.

    Суперадминистратор видит все места; администратор места — только места с
    его действующим назначением. Посторонний вызывающий получает отказ.
    """
    superadmin = is_superadmin(actor_telegram_user_id)
    if not superadmin and not has_any_place_admin_role(session, actor_telegram_user_id):
        raise AdminPermissionError("Кабинет доступен только администраторам.")

    base = (
        select(Place.id, PlaceName.name)
        .join(PlaceName, PlaceName.place_id == Place.id)
        .where(PlaceName.is_canonical == 1)
    )
    if not superadmin:
        base = base.where(
            exists().where(
                and_(
                    PlaceAdmin.place_id == Place.id,
                    PlaceAdmin.revoked_at.is_(None),
                    PlaceAdmin.user_id.in_(
                        select(User.id).where(
                            User.telegram_user_id == actor_telegram_user_id
                        )
                    ),
                )
            )
        )
    total = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = session.execute(
        base.order_by(Place.id).offset(max(offset, 0)).limit(max(limit, 1))
    ).all()

    items: list[ManagedPlace] = []
    for place_id, canonical_name in rows:
        active = get_active_place_admin(session, int(place_id))
        items.append(
            ManagedPlace(
                place_id=int(place_id),
                canonical_name=str(canonical_name),
                admin=active.user if active is not None else None,
                assigned_at=active.assigned_at if active is not None else None,
            )
        )
    return items, total


def search_known_users(session: Session, query: str) -> UserSearchResults:
    """
    Поиск получателя среди уже известных боту пользователей.

    Поддерживаются @username, часть username/имени и точный числовой Telegram
    user ID. Незнакомый username не превращается в пользователя и не даёт
    возможности назначить его по догадке. Пустой/слишком длинный запрос —
    `UserSearchError`.
    """
    cleaned = " ".join(str(query).split())
    if not cleaned:
        raise UserSearchError("Введите @username, имя или Telegram user ID.")
    if len(cleaned) > MAX_USER_SEARCH_LENGTH:
        raise UserSearchError(
            f"Слишком длинный запрос: максимум {MAX_USER_SEARCH_LENGTH} символов."
        )

    if cleaned.isascii() and cleaned.isdecimal():
        try:
            telegram_user_id = int(cleaned)
        except ValueError:
            telegram_user_id = 0
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if user is None:
            return UserSearchResults(users=(), total=0)
        return UserSearchResults(users=(_user_snapshot(user),), total=1)

    needle = cleaned.lstrip("@").casefold()
    users = list(session.scalars(select(User).order_by(User.id)).all())
    matches: list[User] = []
    for user in users:
        username = (user.username or "").casefold()
        full_name = (user.full_name or "").casefold()
        if needle in username or needle in full_name:
            matches.append(user)

    def sort_key(user: User) -> tuple[int, str, str, int]:
        username = (user.username or "").casefold()
        full_name = (user.full_name or "").casefold()
        if username == needle:
            rank = 0
        elif full_name == needle:
            rank = 1
        elif username.startswith(needle):
            rank = 2
        elif full_name.startswith(needle):
            rank = 3
        else:
            rank = 4
        return (rank, username or full_name, full_name, user.telegram_user_id)

    matches.sort(key=sort_key)
    return UserSearchResults(
        users=tuple(_user_snapshot(user) for user in matches[:USER_SEARCH_LIMIT]),
        total=len(matches),
    )


def assign_place_admin(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    target_telegram_user_id: int,
) -> AdminMutationResult:
    """
    Назначает администратора только на место БЕЗ действующего администратора.

    Если место уже занято, вызывающий получает `has_admin`; молчаливой замены
    нет. Конкурентная вставка защищена частичным уникальным индексом.
    """
    actor = _actor_for_superadmin(session, actor_telegram_user_id)
    if session.get(Place, place_id) is None:
        return AdminMutationResult(status="place_missing")
    target = _target_user(session, target_telegram_user_id)
    if target is None:
        return AdminMutationResult(status="user_missing")

    active = _active_row(session, place_id)
    if active is not None:
        active_assignment, active_user = active
        if active_user.telegram_user_id == target.telegram_user_id:
            return AdminMutationResult(
                status="already_admin",
                admin=get_active_place_admin(session, place_id),
            )
        return AdminMutationResult(status="has_admin")

    now = utcnow()
    assignment = PlaceAdmin(
        place_id=place_id,
        user_id=target.id,
        assigned_by_user_id=actor.id,
        assigned_at=now,
    )
    _supersede_pending_transfers(session, place_id, now=now)
    session.add(assignment)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        active = _active_row(session, place_id)
        if active is not None and active[1].telegram_user_id == target.telegram_user_id:
            return AdminMutationResult(status="already_admin")
        return AdminMutationResult(status="has_admin")
    except OperationalError:
        session.rollback()
        return AdminMutationResult(status="conflict")
    except Exception:
        session.rollback()
        raise
    return AdminMutationResult(
        status="assigned",
        admin=get_active_place_admin(session, place_id),
    )


def replace_place_admin(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    target_telegram_user_id: int,
    expected_admin_telegram_user_id: int,
) -> AdminMutationResult:
    """
    Атомарно заменяет администратора, если текущий всё ещё ожидаемый.

    `changed` означает, что за время открытого экрана роль изменилась; новая
    замена без повторного выбора не выполняется.
    """
    actor = _actor_for_superadmin(session, actor_telegram_user_id)
    if session.get(Place, place_id) is None:
        return AdminMutationResult(status="place_missing")
    target = _target_user(session, target_telegram_user_id)
    if target is None:
        return AdminMutationResult(status="user_missing")

    active = _active_row(session, place_id)
    if active is None:
        return AdminMutationResult(status="changed")
    assignment, current_user = active
    if current_user.telegram_user_id != expected_admin_telegram_user_id:
        return AdminMutationResult(status="changed")
    if current_user.telegram_user_id == target.telegram_user_id:
        return AdminMutationResult(
            status="already_admin",
            admin=get_active_place_admin(session, place_id),
        )

    now = utcnow()
    try:
        updated = session.execute(
            PlaceAdmin.__table__.update()
            .where(
                PlaceAdmin.id == assignment.id,
                PlaceAdmin.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_by_user_id=actor.id)
        )
    except OperationalError:
        session.rollback()
        return AdminMutationResult(status="changed")
    if getattr(updated, "rowcount", 0) != 1:
        session.rollback()
        return AdminMutationResult(status="changed")

    _supersede_pending_transfers(session, place_id, now=now)
    session.add(
        PlaceAdmin(
            place_id=place_id,
            user_id=target.id,
            assigned_by_user_id=actor.id,
            assigned_at=now,
        )
    )
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return AdminMutationResult(status="changed")
    except Exception:
        session.rollback()
        raise
    return AdminMutationResult(
        status="replaced",
        admin=get_active_place_admin(session, place_id),
    )


def revoke_place_admin(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    expected_admin_telegram_user_id: int,
) -> AdminMutationResult:
    """
    Отзывает действующую роль, только если она всё ещё у ожидаемого админа.

    Повторный отзыв/смена админа дают `changed`, а не запись в аудит второй
    несуществующей роли.
    """
    actor = _actor_for_superadmin(session, actor_telegram_user_id)
    if session.get(Place, place_id) is None:
        return AdminMutationResult(status="place_missing")
    active = _active_row(session, place_id)
    if active is None:
        return AdminMutationResult(status="changed")
    assignment, current_user = active
    if current_user.telegram_user_id != expected_admin_telegram_user_id:
        return AdminMutationResult(status="changed")

    now = utcnow()
    try:
        updated = session.execute(
            PlaceAdmin.__table__.update()
            .where(
                PlaceAdmin.id == assignment.id,
                PlaceAdmin.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoked_by_user_id=actor.id)
        )
    except OperationalError:
        session.rollback()
        return AdminMutationResult(status="changed")
    if getattr(updated, "rowcount", 0) != 1:
        session.rollback()
        return AdminMutationResult(status="changed")
    _supersede_pending_transfers(session, place_id, now=now)
    try:
        session.commit()
    except (IntegrityError, OperationalError):
        session.rollback()
        return AdminMutationResult(status="changed")
    except Exception:
        session.rollback()
        raise
    return AdminMutationResult(status="revoked")
