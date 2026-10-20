# handlers/callbacks.py
"""
Обработчики CallbackQuery, которые НЕ входят в ConversationHandler создания поста:
- Возврат в главное меню из inline-кнопок
"""

from telegram import Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from handlers.main_menu import render_main_menu
from utils import tg_context as ctx


async def handle_main_menu_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Обрабатывает нажатие кнопки «Главное меню» (callback main_menu).

    Существующий экран перерисовывается в главное меню — новых сообщений
    не создаётся (Stage 11: один живой экран управления).
    """
    await ctx.query(update).answer()
    await render_main_menu(update, context)


def callbacks_handlers() -> list[BaseHandler]:
    """
    Возвращает список обработчиков CallbackQuery.

    Регистрируются после ConversationHandler создания поста: кнопки диалога
    при активном диалоге обслуживает его состояние, а здесь — только кнопки,
    не относящиеся к диалогу.
    """
    return [
        CallbackQueryHandler(handle_main_menu_selection, pattern=r"^main_menu$"),
    ]
