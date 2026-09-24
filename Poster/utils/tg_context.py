# utils/tg_context.py
"""
Narrowing-хелперы для типов python-telegram-bot (см. [tool.mypy] в pyproject).

PTB типизирует callback_query / effective_message / effective_user /
user_data как Optional (контекст «вне диалога»), однако в наших обработчиках
они гарантированы фильтрами и состоянием ConversationHandler. Хелперы
возвращают не-Optional типы; при действительно редком отсутствии бросают
RuntimeError с понятным сообщением — иначе было бы падение на атрибуте None.

Использование:  from utils import tg_context as ctx
                query = ctx.query(update)
"""

from typing import Any

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import ContextTypes


def query(update: Update) -> CallbackQuery:
    """CallbackQuery из update (обработчики регистрируются только на callback)."""
    value = update.callback_query
    if value is None:
        raise RuntimeError("В update нет callback_query")
    return value


def message(update: Update) -> Message:
    """Сообщение, на которое пришёл update (текстовое или кнопочное)."""
    value = update.effective_message
    if value is None:
        raise RuntimeError("В update нет сообщения")
    return value


def user(update: Update) -> User:
    """Инициатор update (в наших сценариях всегда есть)."""
    value = update.effective_user
    if value is None:
        raise RuntimeError("В update нет пользователя")
    return value


def chat(update: Update) -> Chat:
    """Чат, в котором произошёл update (для отправки сообщений)."""
    value = update.effective_chat
    if value is None:
        raise RuntimeError("В update нет чата")
    return value


def data(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Диалоговые данные пользователя (гарантированы ConversationHandler)."""
    value = context.user_data
    if value is None:
        raise RuntimeError("context.user_data недоступен вне диалога")
    return value
