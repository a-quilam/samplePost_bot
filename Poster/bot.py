# bot.py
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from config import TELEGRAM_BOT_TOKEN
from database import init_db
from errors import error_handler
from handlers.admin import admin_handlers
from handlers.bricks import bricks_handlers
from handlers.callbacks import callbacks_handlers
from handlers.drafts import drafts_handlers
from handlers.main_menu import main_menu_handlers
from handlers.places import places_handlers
from handlers.post_creation import post_creation_handlers
from handlers.user_tracking import track_user
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
    дальше по цепочке (main_menu → callbacks → drafts).
    """
    # TELEGRAM_BOT_TOKEN проверяется в config при импорте; здесь — ещё и
    # нарровинг для mypy (build_application типизирован, в отличие от старого main)
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан — см. .env.example")
    # job_queue(None): боту НЕ нужны периодические задачи (Stage 14, §1.2 —
    # TTL-задача удалена). PTB 22 собирает JobQueue автоматически, только если
    # в окружении есть APScheduler (остался транзитивной зависимостью) — без
    # явного отключения процесс поднимал бы простаивающий планировщик. Одно
    # предупреждение «No JobQueue set up» при старте — ожидаемое и разовое.
    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).job_queue(None).build()

    # Регистрация известных пользователей (Stage 20A): отдельная группа -1,
    # выполняется до сценариев, не отвечает в чат и не меняет порядок групп 0.
    application.add_handler(TypeHandler(Update, track_user), group=-1)

    # Обработчики создания поста (ConversationHandler) — первыми
    for handler in post_creation_handlers():
        application.add_handler(handler)

    # Справочник мест: после мастера (не перехватывает его тексты) и до
    # глобальных обработчиков главного меню (иначе outside_dialog_hint съел
    # бы ввод в фазах поиска/создания места).
    for handler in places_handlers():
        application.add_handler(handler)

    # Административный кабинет: после «Мест», до главного меню. Текстовый
    # поиск получателя активен только в своей явной фазе.
    for handler in admin_handlers():
        application.add_handler(handler)

    # Личные кирпичики: после мастера/«Мест»/кабинета, но до глобального
    # обработчика текста главного меню. У него собственный префикс bk_.
    for handler in bricks_handlers():
        application.add_handler(handler)

    # Обработчики главного меню
    for handler in main_menu_handlers():
        application.add_handler(handler)

    # Обработчики CallbackQuery
    for handler in callbacks_handlers():
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

    # Фоновых задач нет: TTL-очистка черновиков отключена (Stage 14) —
    # черновики хранятся бессрочно, пока пользователь не удалит их сам.

    # Явный allowed_updates обязателен: Telegram хранит последнюю подписку
    # между вызовами getUpdates («If not specified, the previous setting will
    # be used»). Без явного параметра PTB не включает его в запрос, и бот
    # наследует любую прошлую серверную настройку. Инцидент P0: подписка
    # «только callback_query» пережила перезапуск — сообщения (/start, ввод
    # полей) перестали доставляться при формально здоровом polling.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
