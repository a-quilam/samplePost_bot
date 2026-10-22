# handlers/main_menu.py
"""
Главное меню и навигация (Stage 11).

Главное меню — это ЭКРАН (постоянное сообщение управления, см. utils.screen):
/start и кнопки навигации перерисовывают его, а не создают новые сообщения.
Кнопки меню — inline-кнопки на экране (✏️ Создать пост / 📍 Места /
📂 Черновики / ❔ Помощь); команды /start, /help, /drafts, /places,
/cancel работают как прежде.

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
from place_state import clear_place_session, has_place_session
from utils import screen
from utils import tg_context as ctx


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Inline-клавиатура главного меню (кнопки экрана).

    Callback-данные: create_post (entry-точка ConversationHandler),
    places — справочник мест, drafts — список черновиков, help — справка,
    main_menu — возврат в меню.
    """
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ Создать пост", callback_data="create_post")],
            [InlineKeyboardButton("📍 Места", callback_data="places")],
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
    /start редактирует старое сообщение, а не плодит дубли. Если в БД остался
    снимок незавершённого поста (рестарт прервал диалог) — в меню добавляется
    кнопка «▶️ Продолжить пост».
    """
    # навигация пользовательским сообщением → старый экран удаляется,
    # новый создаётся рядом с вводом (Stage 16, дефект №7); callback —
    # guard-пропуск, кнопки и так на экране
    await screen.reanchor_screen(update, context)
    user_data = ctx.data(context)
    user_id = ctx.user(update).id
    # Главное меню — другая ветка сценария: незавершённый выбор места не
    # должен перехватывать следующий текст. WIP-пост при этом не трогается.
    clear_place_session(user_id)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    keyboard = main_menu_keyboard()
    if screen.load_payload(user_id):
        # WIP пережил рестарт — предлагаем продолжение вместо потери
        keyboard = InlineKeyboardMarkup(
            [
                *keyboard.inline_keyboard,
                [
                    InlineKeyboardButton(
                        "▶️ Продолжить пост", callback_data="restore_wip"
                    )
                ],
            ]
        )
    await screen.render_screen(context, user_data, chat_id, main_menu_text(), keyboard)
    # ref экрана закрепляется в БД; WIP-payload не трогаем (пост в работе —
    # он переживёт рестарт и будет предложен к продолжению)
    screen.save_screen_ref(user_id, user_data, chat_id)


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
        "— «📍 Места» — найти место по названию, посмотреть карточку, выбрать "
        "его или добавить новое место/альтернативное название;\n"
        "— при выходе из создания (/cancel, /start, /drafts, /places) "
        "незавершённый "
        "пост автоматически сохраняется в черновиках — вернуться к нему можно "
        "через «📂 Черновики» → «✏️ Редактировать черновик».\n\n"
        "Команды:\n"
        "/start — главное меню\n"
        "/create_post — создать новый пост\n"
        "/places — справочник мест\n"
        "/drafts — список ваших черновиков\n"
        "/cancel — отменить текущий диалог\n"
        "/help — эта справка"
    )


async def render_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка на экране управления (с возвратом в главное меню)."""
    # навигация сообщением ниже экрана → новый экран рядом с вводом
    await screen.reanchor_screen(update, context)
    clear_place_session(ctx.user(update).id)
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
    """/cancel без активного диалога: отмена выбора места или подсказка."""
    # навигация сообщением ниже экрана → новый экран рядом с вводом
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    place_active = has_place_session(user_id)
    if place_active:
        clear_place_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    text = (
        "Сценарий выбора места отменён."
        if place_active
        else "Сейчас нет активного создания поста."
    )
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        text,
        main_menu_keyboard() if place_active else None,
    )
    if place_active:
        await screen.drop_user_input(update, context)


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
    # навигация сообщением ниже экрана → подсказка рядом с вводом
    await screen.reanchor_screen(update, context)
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
