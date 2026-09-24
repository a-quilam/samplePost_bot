# bot.py
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN
from database import init_db
from errors import error_handler
from handlers.admin import admin_handlers
from handlers.approval import approval_handlers
from handlers.callbacks import callbacks_handlers
from handlers.drafts import drafts_handlers
from handlers.jobs import setup_jobs
from handlers.main_menu import main_menu_handlers
from handlers.post_creation import post_creation_handlers
from log_config import setup_logging
from utils import tg_context as ctx

logger = logging.getLogger(__name__)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Неизвестная команда — подсказываем /help."""
    await ctx.message(update).reply_text(
        "Неизвестная команда. Наберите /help — список команд."
    )


def build_application() -> Application:
    """
    Собирает приложение со ВСЕМИ обработчиками (без запуска — тестируемо).

    Порядок регистрации важен: ConversationHandler создания поста идёт
    ПЕРВЫМ. Так reply-кнопки главного меню («📝 Черновики»), нажатые
    ВНУТРИ диалога, обрабатываются состояниями диалога (с завершением
    диалога), а не глобальным хендлером main_menu — состояние не «висит».
    Вне диалога ConversationHandler возвращает None, и update уходит
    дальше по цепочке (main_menu → admin → callbacks → approval → drafts).
    """
    # TELEGRAM_BOT_TOKEN проверяется в config при импорте; здесь — ещё и
    # нарровинг для mypy (build_application типизирован, в отличие от старого main)
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан — см. .env.example")
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # Обработчики создания поста (ConversationHandler) — первыми
    for handler in post_creation_handlers():
        application.add_handler(handler)

    # Обработчики главного меню
    for handler in main_menu_handlers():
        application.add_handler(handler)

    # Административные обработчики
    for handler in admin_handlers():
        application.add_handler(handler)

    # Обработчики CallbackQuery
    for handler in callbacks_handlers():
        application.add_handler(handler)

    # Обработчики согласования (назначение ответственного, решения)
    for handler in approval_handlers():
        application.add_handler(handler)

    # Обработчики черновиков
    for handler in drafts_handlers():
        application.add_handler(handler)

    # Обработчик неизвестных команд
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Глобальный обработчик ошибок: лог + уведомление пользователя
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    # Настройка логирования — только здесь (побочных эффектов при импорте
    # модуля bot.py нет, поэтому модуль можно импортировать в тестах)
    setup_logging()
    logger.info("Запуск бота.")

    init_db()
    application = build_application()

    # Фоновые задачи
    setup_jobs(application)

    application.run_polling()


if __name__ == "__main__":
    main()
