# bot.py

import logging
import asyncio

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN
from database import SessionLocal, init_db
from handlers.main_menu import main_menu_handlers
from handlers.admin import admin_handlers
from handlers.post_creation import post_creation_handlers
from handlers.callbacks import callbacks_handlers
from jobs import setup_jobs

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Создание всех таблиц в базе данных
init_db()

async def shutdown_callback(application: Application):
    """
    Shutdown Callback для закрытия сессии базы данных.
    """
    session = application.bot_data.get('db_session')
    if session:
        session.close()
        logger.info("Сессия базы данных закрыта.")

async def main():
    # Создание приложения бота
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Создание сессии базы данных и сохранение её в bot_data для доступа в обработчиках
    application.bot_data['db_session'] = SessionLocal()

    # Регистрация обработчиков основного меню
    for handler in main_menu_handlers():
        application.add_handler(handler)

    # Регистрация обработчиков административных команд
    for handler in admin_handlers():
        application.add_handler(handler)

    # Регистрация обработчиков создания поста (ConversationHandler)
    for handler in post_creation_handlers():
        application.add_handler(handler)

    # Регистрация обработчиков CallbackQuery
    for handler in callbacks_handlers():
        application.add_handler(handler)

    # Настройка фоновых задач
    setup_jobs(application)

    # Добавление обработчика команд /help
    async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        help_text = (
            "📚 *Доступные команды:*\n\n"
            "/start - Начало работы с ботом\n"
            "/help - Показать это сообщение\n"
            "/add_responsible <Имя> <Telegram_ID> - Добавить ответственного (только админам)\n"
            "/remove_responsible <Telegram_ID> - Удалить ответственного (только админам)"
        )
        await update.message.reply_text(help_text, parse_mode='MarkdownV2')

    application.add_handler(CommandHandler('help', help_command))

    # Добавление обработчика ошибок
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

    application.add_error_handler(error_handler)

    # Добавление Shutdown Callback
    application.add_shutdown_callback(shutdown_callback)

    # Запуск бота
    logger.info("Запуск бота...")
    await application.run_polling()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except RuntimeError as e:
        logger.error(f"RuntimeError: {e}")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
