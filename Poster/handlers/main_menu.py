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

    /drafts дублирует reply-кнопку «📝 Черновики»: когда диалога нет — список
    показывает этот хендлер; во время активного диалога команду обслуживают
    fallback'ы ConversationHandler (незавершённый пост сохраняется, диалог
    завершается — см. handlers/post_creation.py).

    Порядок важен: /cancel вне диалога и подсказка на обычный текст/фото
    стоят ПОСЛЕ команд и reply-кнопок — они не перехватывают ни их, ни
    состояния диалога (ConversationHandler создания поста регистрируется
    первой группой в bot.py и свои update обслуживает раньше).
    """
    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("drafts", drafts_command),
        CommandHandler("cancel", cancel_without_dialog),
        MessageHandler(filters.Regex("^📝 Черновики$"), view_drafts),
        # Последним: обычный текст/фото вне диалога раньше оставались
        # без ответа («тишина»). Команды исключены (~COMMAND), поэтому
        # unknown_command и dialog-fallback'и работают как прежде.
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, outside_dialog_hint
        ),
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
        "Я помогаю собрать полноценный пост по шагам, проверить его в превью "
        "и получить готовый результат.\n\n"
        "Как работать:\n"
        "— «✅ Готово — получить пост» — бот пришлёт готовый пост в этот чат;\n"
        "— «📄 Сохранить в черновики» — сохранить работу и вернуться к ней "
        "позже;\n"
        "— при выходе из создания (/cancel, /start, /drafts) незавершённый "
        "пост автоматически сохраняется в черновиках — вернуться к нему можно "
        "через «📝 Черновики» → «✏️ Редактировать черновик».\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/create_post — создать новый пост\n"
        "/drafts — список ваших черновиков\n"
        "/cancel — отменить текущий диалог\n"
        "/help — эта справка"
    )


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /drafts — список черновиков (дублирует reply-кнопку)."""
    await view_drafts(update, context)


async def cancel_without_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/cancel без активного диалога: раньше попадал в «Неизвестная команда»."""
    await ctx.message(update).reply_text("Сейчас нет активного создания поста.")


async def outside_dialog_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Ответ-подсказка на обычный текст/фото ВНЕ диалога (раньше — тишина).

    Внутри диалога этот хендлер не вызывается: ConversationHandler создания
    поста зарегистрирован первой группой и текст/фото в своих состояниях
    обслуживает сам.
    """
    message = ctx.message(update)
    if message.photo:
        await message.reply_text(
            "Фото понадобится на шаге «Изображение» — начните создание "
            "кнопкой «✏️ Создать пост»."
        )
    else:
        await message.reply_text(
            "Воспользуйтесь кнопками меню ниже или наберите /help — " "список команд."
        )
