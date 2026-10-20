# handlers/main_menu.py
"""
Главное меню и навигация (Stage 11).

Главное меню — это ЭКРАН (постоянное сообщение управления, см. utils.screen):
/start и кнопки навигации перерисовывают его, а не создают новые сообщения.
Кнопки меню — inline-кнопки на экране (✏️ Создать пост / 📂 Черновики /
❔ Помощь); команды /start, /help, /drafts, /cancel работают как прежде.

Текстовые entry-кнопки «✏️ Создать пост» и «📝 Черновики» (regex) сохранены
как совместимость: ConversationHandler создания поста реагирует на текст —
и старые сценарии, и привычка пользователя набрать текст не ломаются.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from handlers.drafts import view_drafts
from utils import screen
from utils import tg_context as ctx


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Inline-клавиатура главного меню (кнопки экрана).

    Callback-данные: create_post (entry-точка ConversationHandler),
    drafts — список черновиков, help — справка, main_menu — возврат в меню.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Создать пост", callback_data="create_post")],
            [
                InlineKeyboardButton("📂 Черновики", callback_data="drafts"),
                InlineKeyboardButton("❔ Помощь", callback_data="help"),
            ],
        ]
    )


def main_menu_text() -> str:
    """Текст экрана главного меню."""
    return "Здравствуйте! 👋\n\nВыберите действие:"


async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Рисует главное меню на существующем экране (или создаёт новый экран).

    После рестарта процесса ref экрана восстанавливается из БД один раз —
    /start редактирует старое сообщение, а не плодит дубли.
    """
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, main_menu_text(), main_menu_keyboard()
    )
    # ref экрана закрепляется в БД; WIP-payload не трогаем (пост в работе —
    # он переживёт рестарт и будет предложен к продолжению)
    screen.save_screen_ref(ctx.user(update).id, user_data, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: главное меню на существующем экране."""
    await render_main_menu(update, context)
    # Сообщение с командой — временный ввод: меню уже на экране, убираем
    # его best-effort (при сбое очистки диалог не ломается)
    await screen.drop_user_input(update, context)


def help_text() -> str:
    """Справка по командам и сценарию работы."""
    return (
        "Я помогаю собрать полноценный пост по шагам, проверить его в превью "
        "и получить готовый результат.\n\n"
        "Как работать:\n"
        "— «✅ Готово — получить пост» — бот пришлёт готовый пост в этот чат;\n"
        "— «📄 Сохранить в черновики» — сохранить работу и вернуться к ней "
        "позже;\n"
        "— при выходе из создания (/cancel, /start, /drafts) незавершённый "
        "пост автоматически сохраняется в черновиках — вернуться к нему можно "
        "через «📂 Черновики» → «✏️ Редактировать черновик».\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/create_post — создать новый пост\n"
        "/drafts — список ваших черновиков\n"
        "/cancel — отменить текущий диалог\n"
        "/help — эта справка"
    )


async def render_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка на экране управления (с возвратом в главное меню)."""
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")]]
    )
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    await screen.render_screen(
        context, user_data, ctx.chat(update).id, help_text(), keyboard
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по командам и сценарию работы."""
    await render_help(update, context)


async def menu_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка «📂 Черновики» главного меню."""
    await ctx.query(update).answer()
    await view_drafts(update, context)


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка «❔ Помощь» главного меню."""
    await ctx.query(update).answer()
    await render_help(update, context)


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /drafts — список черновиков (дублирует кнопку меню)."""
    await view_drafts(update, context)


async def cancel_without_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/cancel без активного диалога: раньше попадал в «Неизвестная команда»."""
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    await screen.render_screen(
        context, user_data, ctx.chat(update).id, "Сейчас нет активного создания поста."
    )


async def outside_dialog_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Подсказка на обычный текст/фото ВНЕ диалога: обновляется на экране
    управления, а не копится новыми сообщениями.

    Внутри диалога этот хендлер не вызывается: ConversationHandler создания
    поста зарегистрирован первой группой и текст/фото в своих состояниях
    обслуживает сам.
    """
    message = ctx.message(update)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    if message.photo:
        text = (
            "Фото понадобится на шаге «Изображение» — начните создание "
            "кнопкой «✏️ Создать пост»."
        )
    else:
        text = "Воспользуйтесь кнопками меню или наберите /help — список команд."
    await screen.render_screen(context, user_data, ctx.chat(update).id, text)


def main_menu_handlers() -> list[BaseHandler]:
    """
    Обработчики главного меню.

    Кнопка «✏️ Создать пост» НЕ регистрируется здесь: она — entry_point
    ConversationHandler в handlers/post_creation.py (иначе состояние диалога
    создания поста не отслеживается). Это относится и к callback create_post.

    /drafts дублирует кнопку «📂 Черновики»: когда диалога нет — список
    показывает этот хендлер; во время активного диалога команду и кнопку
    обслуживают fallback'и ConversationHandler (незавершённый пост
    сохраняется, диалог завершается — см. handlers/post_creation.py).

    Порядок важен: /cancel вне диалога и подсказка на обычный текст/фото
    стоят ПОСЛЕ команд и callback'ов — они не перехватывают ни их, ни
    состояния диалога (ConversationHandler создания поста регистрируется
    первой группой в bot.py и свои update обслуживает раньше).
    """
    handlers = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CommandHandler("drafts", drafts_command),
        CommandHandler("cancel", cancel_without_dialog),
        CallbackQueryHandler(menu_drafts, pattern="^drafts$"),
        CallbackQueryHandler(menu_help, pattern="^help$"),
        MessageHandler(filters.Regex("^📝 Черновики$"), view_drafts),
        # Последним: обычный текст/фото вне диалога раньше оставались
        # без ответа («тишина»). Команды исключены (~COMMAND), поэтому
        # unknown_command и dialog-fallback'и работают как прежде.
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, outside_dialog_hint
        ),
    ]
    return handlers
