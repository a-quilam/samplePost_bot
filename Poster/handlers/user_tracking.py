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
from template_store import (
    TemplateError,
    ensure_initial_personal_template,
    has_personal_template_history,
)
from user_store import get_or_create_user

logger = logging.getLogger(__name__)


async def track_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    UPSERT известного пользователя по постоянному Telegram user ID.

    Здесь же выдаётся стартовый личный шаблон (Poster 2.0, S2). Решение
    принимается по владению шаблоном, а не по «первому контакту»: `/start`
    существующего пользователя без шаблона должен получить однократный
    бэкфилл (план S1 п.7, S2 п.3). Команда `/start` внутри активного диалога
    перехватывается самим диалогом, поэтому выдача живёт в фоновом трекинге,
    который видит каждый Update.
    """
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
        if _is_start_command(update):
            _grant_initial_template(session, user.id)
    except Exception as e:  # noqa: BLE001 — трекинг не должен блокировать сценарий
        session.rollback()
        logger.error("Не удалось обновить профиль известного пользователя: %s", e)
    finally:
        session.close()


def _is_start_command(update: Update) -> bool:
    """
    Это `/start` (в том числе с payload передачи прав)?

    Выдача стартового шаблона привязана именно к `/start` (план S2 п.3), а не
    к любому Update: иначе пользователь, открывший, например, `/templates`,
    получал бы шаблон «самим фактом» другого действия. Telegram выделяет
    bot_command только до первого пробела, поэтому payload не мешает.
    """
    message = getattr(update, "effective_message", None)
    if message is None:
        return False
    entities = getattr(message, "entities", None) or ()
    for entity in entities:
        if getattr(entity, "type", None) != "bot_command":
            continue
        text = message.text or ""
        offset = int(getattr(entity, "offset", 0))
        length = int(getattr(entity, "length", 0))
        command = text[offset : offset + length].split("@", 1)[0]
        return command == "/start"
    return False


def _grant_initial_template(session, telegram_user_id: int) -> None:
    """
    Стартовый личный шаблон: новый пользователь и однократный бэкфилл.

    Poster 2.0, S2: `/start` обязан дать шаблон и новому пользователю, и
    существующему без личных шаблонов (строки `users` могли появиться раньше —
    из прежней версии продукта, из передачи прав). Поэтому признак «первый
    контакт» (`known is None`) больше не используется: он оставлял таких
    пользователей без шаблона навсегда.

    Повторная выдача исключена одним условием: `has_personal_template_history`
    (живой шаблон, его снимок или личный анонс). Так осознанно удалённый
    шаблон не воскресает: метка выдачи остаётся в неизменяемом снимке, даже
    когда строка `templates` удалена.

    Сбой не ломает Update: ошибка логируется без содержимого профиля.
    """
    try:
        if has_personal_template_history(
            session, actor_telegram_user_id=telegram_user_id
        ):
            return
        ensure_initial_personal_template(
            session, actor_telegram_user_id=telegram_user_id
        )
    except TemplateError as exc:
        logger.warning("Стартовый личный шаблон не выдан: %s", exc)
    except Exception as exc:  # noqa: BLE001 — регистрация не должна падать
        session.rollback()
        logger.exception("Не удалось выдать стартовый личный шаблон: %s", exc)
