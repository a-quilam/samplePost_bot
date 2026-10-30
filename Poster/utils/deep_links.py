# utils/deep_links.py
"""
Ссылки `https://t.me/<бот>?start=<payload>` (Poster 2.2 U4b).

Username бота не секрет и берётся у самого Telegram (`Bot.username`), а при
его отсутствии — через `getMe`. Хардкода имени бота нет: один и тот же код
работает и в тестах (FakeBot), и в рабочем боте с другим именем.

Payload — короткий URL-safe идентификатор, ссылка на объект, а не на
внутренний ключ: Telegram ограничивает `/start` 64 символами, а `place_id`
в ссылке позволил бы перебирать площадки.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def bot_username(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Username бота для ссылок; None — если Telegram его не сообщил."""
    username = getattr(context.bot, "username", None)
    if username:
        return str(username)
    try:
        me = await context.bot.get_me()
    except Exception as e:  # noqa: BLE001 — без username ссылку не показать
        logger.error("Не удалось получить username бота для ссылки: %s", e)
        return None
    username = getattr(me, "username", None)
    return str(username) if username else None


async def deep_link(context: ContextTypes.DEFAULT_TYPE, payload: str) -> str | None:
    """Готовая ссылка `https://t.me/<бот>?start=<payload>` или None."""
    username = await bot_username(context)
    if not username:
        return None
    return f"https://t.me/{username}?start={quote(payload, safe='')}"
