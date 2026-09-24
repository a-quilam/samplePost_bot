# handlers/main_menu.py

from telegram import ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CommandHandler, MessageHandler, filters
from handlers.drafts import view_drafts

def main_menu_handlers():
    """
    Обработчики главного меню.

    Кнопка «✏️ Создать пост» НЕ регистрируется здесь: она зарегистрирована
    как entry_point ConversationHandler в handlers/post_creation.py,
    иначе состояние диалога создания поста не отслеживается.
    """
    handlers = [
        CommandHandler('start', start),
        MessageHandler(filters.Regex('^📝 Черновики$'), view_drafts),
    ]
    return handlers

async def start(update, context):
    await update.message.reply_text(
        'Здравствуйте! 👋\n\nВыберите действие:',
        reply_markup=ReplyKeyboardMarkup(
            [
                [KeyboardButton(text='✏️ Создать пост')],
                [KeyboardButton(text='📝 Черновики')],
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
    )
