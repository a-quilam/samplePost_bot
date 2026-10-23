# permissions.py
"""
Единая проверка ролей Poster (Stage 20A).

Источник суперадминистратора — только конфигурация `SUPERADMIN_USER_IDS`
(постоянные Telegram user ID). Роль администратора места — действующая строка
`place_admins` с `revoked_at IS NULL`, связанная с `users.id`.

Функции читают актуальное состояние БД при каждом вызове и не кэшируют его в
`user_data` или состоянии экрана. Видимость кнопки — только удобство:
административные операции дополнительно проверяют права в своём слое.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from models import Place, PlaceAdmin, User


def is_superadmin(telegram_user_id: int | None) -> bool:
    """Входит ли постоянный Telegram user ID в настроенный список."""
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        return False
    return telegram_user_id in config.SUPERADMIN_USER_IDS


def is_place_admin(
    session: Session, telegram_user_id: int | None, place_id: int
) -> bool:
    """
    Является ли пользователь действующим администратором ИМЕННО этого места.

    Проверяются одновременно: активная строка назначения, существование места
    и связь с постоянным Telegram ID через `users.id`. Username не участвует.
    """
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        return False
    row = session.scalar(
        select(PlaceAdmin.id)
        .join(Place, Place.id == PlaceAdmin.place_id)
        .join(User, User.id == PlaceAdmin.user_id)
        .where(
            PlaceAdmin.place_id == place_id,
            PlaceAdmin.revoked_at.is_(None),
            User.telegram_user_id == telegram_user_id,
        )
        .limit(1)
    )
    return row is not None


def can_manage_place(
    session: Session, telegram_user_id: int | None, place_id: int
) -> bool:
    """Суперадмин управляет всеми местами; админ места — только своим."""
    return is_superadmin(telegram_user_id) or is_place_admin(
        session, telegram_user_id, place_id
    )


def has_any_place_admin_role(session: Session, telegram_user_id: int) -> bool:
    """Есть ли у пользователя хотя бы одно действующее место."""
    if (
        isinstance(telegram_user_id, bool)
        or not isinstance(telegram_user_id, int)
        or telegram_user_id <= 0
    ):
        return False
    row = session.scalar(
        select(PlaceAdmin.id)
        .join(User, User.id == PlaceAdmin.user_id)
        .where(
            PlaceAdmin.revoked_at.is_(None),
            User.telegram_user_id == telegram_user_id,
        )
        .limit(1)
    )
    return row is not None


def can_access_admin_panel(session: Session, telegram_user_id: int | None) -> bool:
    """Доступ в кабинет: суперадмин или действующий админ хотя бы одного места."""
    if is_superadmin(telegram_user_id):
        return True
    if telegram_user_id is None:
        return False
    return has_any_place_admin_role(session, telegram_user_id)
