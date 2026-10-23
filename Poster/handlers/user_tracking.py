# handlers/user_tracking.py
"""
Регистрация известных пользователей при каждом Telegram Update (Stage 20A).

Кабинет суперадмина ищет получателя только среди тех, кто уже взаимодействовал
с Poster. Слой `users` существовал с Stage 19A, но заполнялся лишь при работе
со справочником мест. Этот фоновый обработчик группы -1 обновляет username/
имя и `last_seen_at` до основного сценария, не меняя UX и не отвечая в чат.

Сбой регистрации не должен ломать обычные функции: ошибка логируется без
содержимого профиля, а обработка Update продолжается.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import SessionLocal
from user_store import get_or_create_user

logger = logging.getLogger(__name__)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """UPSERT известного пользователя по постоянному Telegram user ID."""
    user = getattr(update, "effective_user", None)
    if user is None or getattr(user, "is_bot", False):
        return
    session = SessionLocal()
    try:
        get_or_create_user(
            session,
            user.id,
            username=getattr(user, "username", None),
            full_name=getattr(user, "full_name", None),
        )
    except Exception as e:  # noqa: BLE001 — трекинг не должен блокировать сценарий
        session.rollback()
        logger.error("Не удалось обновить профиль известного пользователя: %s", e)
    finally:
        session.close()
