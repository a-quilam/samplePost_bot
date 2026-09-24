# handlers/main_menu.py

from telegram import KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from handlers.drafts import view_drafts
from utils import tg_context as ctx


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """
    Единая reply-клавиатура главного меню.

    Используется в /start, по кнопке «Главное меню» из inline-клавиатур
    и по завершении диалога создания поста: у пользователя всегда есть
    кнопки навигации (раньше ReplyKeyboardRemove скрывал меню и человек
    оставался без кнопок).
    """
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(text="✏️ Создать пост")],
            [KeyboardButton(text="📝 Черновики")],
        ],
        resize_keyboard=True,
    )


def main_menu_handlers() -> list[BaseHandler]:
    """
    Обработчики главного меню.

    Кнопка «✏️ Создать пост» НЕ регистрируется здесь: она зарегистрирована
    как entry_point ConversationHandler в handlers/post_creation.py,
    иначе состояние диалога создания поста не отслеживается.

    /drafts дублирует reply-кнопку «📝 Черновики» (в отличие от кнопки,
    команда не завершает активный диалог — PTB не позволяет завершить
    чужой ConversationHandler извне).
    """
    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("drafts", drafts_command),
        MessageHandler(filters.Regex("^📝 Черновики$"), view_drafts),
    ]
    return handlers


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.message(update).reply_text(
        "Здравствуйте! 👋\n\nВыберите действие:",
        reply_markup=main_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по командам и сценарию работы."""
    await ctx.message(update).reply_text(
        "Я помогаю подготовить пост и провести его согласование.\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/create_post — создать новый пост\n"
        "/drafts — список ваших черновиков\n"
        "/cancel — отменить текущий диалог\n"
        "/help — эта справка\n\n"
        "Администраторам:\n"
        "/add_responsible <Имя> <Telegram_ID> — добавить ответственного\n"
        "/remove_responsible <Telegram_ID> — удалить ответственного"
    )


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /drafts — список черновиков (дублирует reply-кнопку)."""
    await view_drafts(update, context)
