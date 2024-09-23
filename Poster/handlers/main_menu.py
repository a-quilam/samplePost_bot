# handlers/main_menu.py

import logging
from typing import Dict
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode

# Логирование для отладки
logger = logging.getLogger(__name__)

# Определение состояний для ConversationHandler
CREATING_POST = 1

# Словарь для хранения черновиков (можно заменить на базу данных)
drafts: Dict[int, list] = {}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик команды /start. Отправляет приветственное сообщение с кнопками.
    """
    keyboard = [
        [KeyboardButton('✏️ Создать пост')],
        [KeyboardButton('📝 Черновики')]
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True, 
        resize_keyboard=True
    )
    await update.message.reply_text(
        "Здравствуйте, Слава! 👋\n\n"
        "Я помогу вам создать пост по следующему шаблону.\n\n"
        "Выберите одно из действий ниже:",
        reply_markup=reply_markup
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик команды /help. Отправляет список доступных команд.
    """
    help_text = (
        "📚 *Доступные команды:*\n\n"
        "/start \\- Начало работы с ботом\n"
        "/help \\- Показать это сообщение\n"
        "/add_responsible \\<Имя\\> \\<Telegram_ID\\> \\- Добавить ответственного (только админам)\n"
        "/remove_responsible \\<Telegram_ID\\> \\- Удалить ответственного (только админам)"
    )
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def create_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопки "✏️ Создать пост". Переходит в состояние создания поста.
    """
    await update.message.reply_text(
        "Отлично! Начнём создание нового поста.",
        reply_markup=ReplyKeyboardRemove()
    )
    await update.message.reply_text("Пожалуйста, введите текст вашего поста:")
    return CREATING_POST


async def receive_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик ввода текста поста. Сохраняет пост и возвращается к главному меню.
    """
    user_id = update.effective_user.id
    post_text = update.message.text

    if user_id not in drafts:
        drafts[user_id] = []
    drafts[user_id].append(post_text)

    await update.message.reply_text(f"Ваш пост сохранён:\n\n{post_text}")
    
    keyboard = [
        [KeyboardButton('✏️ Создать пост')],
        [KeyboardButton('📝 Черновики')]
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True, 
        resize_keyboard=True
    )
    await update.message.reply_text(
        "Что вы хотите сделать дальше?",
        reply_markup=reply_markup
    )
    return ConversationHandler.END


async def drafts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик кнопки "📝 Черновики". Отображает список черновиков пользователя.
    """
    user_id = update.effective_user.id
    user_drafts = drafts.get(user_id, [])

    if not user_drafts:
        await update.message.reply_text("У вас пока нет черновиков.")
    else:
        message = "📄 *Ваши черновики:*\n\n"
        for idx, draft in enumerate(user_drafts, 1):
            message += f"{idx}. {draft}\n"
        await update.message.reply_text(
            message,
            parse_mode=ParseMode.MARKDOWN
        )
    
    keyboard = [
        [KeyboardButton('✏️ Создать пост')],
        [KeyboardButton('📝 Черновики')]
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard, 
        one_time_keyboard=True, 
        resize_keyboard=True
    )
    await update.message.reply_text(
        "Что вы хотите сделать дальше?",
        reply_markup=reply_markup
    )


def main_menu_handlers():
    """
    Возвращает ConversationHandler для главного меню.
    """
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^✏️ Создать пост$"), create_post),
            MessageHandler(filters.Regex("^📝 Черновики$"), drafts_menu)
        ],
        states={
            CREATING_POST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_post)
            ]
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("help", help_command)
        ]
    )
    return conv_handler
