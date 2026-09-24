import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
from handlers.main_menu import main_menu_handlers
from handlers.admin import admin_handlers
from handlers.callbacks import callbacks_handlers
from handlers.approval import approval_handlers
from handlers.drafts import drafts_handlers
from handlers.post_creation import post_creation_handlers
from handlers.jobs import setup_jobs
from config import TELEGRAM_BOT_TOKEN
from database import init_db

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG  # Изменено на DEBUG для более подробного логирования
)
logger = logging.getLogger(__name__)

def main():
    init_db()
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Добавление обработчиков главного меню
    for handler in main_menu_handlers():
        application.add_handler(handler)

    # Добавление административных обработчиков
    for handler in admin_handlers():
        application.add_handler(handler)

    # Добавление обработчиков CallbackQuery
    for handler in callbacks_handlers():
        application.add_handler(handler)

    # Обработчики согласования (назначение ответственного, решения).
    # Регистрируются ДО ConversationHandler создания поста: их callback_data
    # не пересекается с паттернами диалога, порядок делает перехват явным.
    for handler in approval_handlers():
        application.add_handler(handler)

    # Добавление обработчиков черновиков
    for handler in drafts_handlers():
        application.add_handler(handler)

    # Добавление обработчиков создания постов
    for handler in post_creation_handlers():
        application.add_handler(handler)

    # Добавление фоновых задач
    setup_jobs(application)

    # Обработчик неизвестных команд
    application.add_handler(
        MessageHandler(
            filters.COMMAND,
            lambda update, context: update.effective_message.reply_text("Неизвестная команда.")
        )
    )

    # Добавление глобального обработчика ошибок
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error(msg="Exception while handling an update:", exc_info=context.error)

    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == "__main__":
    main()
