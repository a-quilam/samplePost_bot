# handlers/main_menu.py

from telegram import ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import CommandHandler, MessageHandler, filters

def main_menu_handlers():
    keyboard = [
        [KeyboardButton(text='✏️ Создать пост')],
        [KeyboardButton(text='📝 Черновики')],
    ]
    reply_markup = ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    handlers = [
        CommandHandler('start', start),
        MessageHandler(filters.Regex('^✏️ Создать пост$'), create_post),
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

async def create_post(update, context):
    await context.application.dispatcher.handle_update(update)
