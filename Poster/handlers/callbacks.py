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

from handlers.main_menu import main_menu_keyboard
from utils import tg_context as ctx


async def handle_main_menu_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Обрабатывает нажатие кнопки "Главное меню" в inline-клавиатуре.

    editMessageText принимает только inline-клавиатуру (ReplyKeyboardMarkup
    там не поддерживается API), поэтому текст редактируется без reply_markup,
    а reply-клавиатура главного меню отправляется отдельным сообщением.
    """
    query = ctx.query(update)
    await query.answer()
    # Паттерн регистрации — ^main_menu$: другого значения data здесь не бывает
    await query.edit_message_text("Здравствуйте! 👋")
    await ctx.message(update).reply_text(
        "Выберите действие:", reply_markup=main_menu_keyboard()
    )


def callbacks_handlers() -> list[BaseHandler]:
    """
    Возвращает список обработчиков для CallbackQuery, не входящих
    в ConversationHandler создания поста.
    """
    return [
        CallbackQueryHandler(handle_main_menu_selection, pattern="^main_menu$"),
    ]
