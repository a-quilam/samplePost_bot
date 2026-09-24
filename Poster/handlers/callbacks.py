# handlers/callbacks.py
"""
Обработчики CallbackQuery, которые НЕ входят в ConversationHandler создания поста
и НЕ относятся к согласованию (оно вынесено в handlers/approval.py):
- Возврат в главное меню из inline-кнопок
"""

from telegram import ReplyKeyboardMarkup, Update, KeyboardButton
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)

async def handle_main_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие кнопки "Главное меню" в inline-клавиатуре.
    Заменяет inline-клавиатуру на Reply-клавиатуру главного меню.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'main_menu':
        keyboard = [
            [KeyboardButton('✏️ Создать пост')],
            [KeyboardButton('📝 Черновики')]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.edit_message_text(
            "Здравствуйте! 👋\n\nВыберите действие:",
            reply_markup=reply_markup
        )
    else:
        await query.edit_message_text("Неизвестное действие.", reply_markup=None)

def callbacks_handlers() -> list:
    """
    Возвращает список обработчиков для CallbackQuery, не входящих
    в ConversationHandler создания поста и в согласование.
    """
    return [
        CallbackQueryHandler(handle_main_menu_selection, pattern='^main_menu$'),
    ]
