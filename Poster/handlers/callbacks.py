# handlers/callbacks.py
"""
Обработчики CallbackQuery, которые НЕ входят в ConversationHandler создания поста
и НЕ относятся к согласованию (оно вынесено в handlers/approval.py):
- Возврат в главное меню из inline-кнопок
"""

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
)

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

    if query.data == "main_menu":
        keyboard = [
            [KeyboardButton("✏️ Создать пост")],
            [KeyboardButton("📝 Черновики")],
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.edit_message_text("Здравствуйте! 👋")
        await ctx.message(update).reply_text(
            "Выберите действие:", reply_markup=reply_markup
        )
    else:
        await query.edit_message_text("Неизвестное действие.")


def callbacks_handlers() -> list[BaseHandler]:
    """
    Возвращает список обработчиков для CallbackQuery, не входящих
    в ConversationHandler создания поста и в согласование.
    """
    return [
        CallbackQueryHandler(handle_main_menu_selection, pattern="^main_menu$"),
    ]
