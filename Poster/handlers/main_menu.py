# handlers/main_menu.py
"""
Главное меню и навигация (Stage 11, Poster 2.0 — S2).

Главное меню — это ЭКРАН (постоянное сообщение управления, см. utils.screen):
/start и кнопки навигации перерисовывают его, а не создают новые сообщения.
Меню Poster 2.0 состоит ровно из пяти кнопок:

- «📍 Создать для площадки» — список зарегистрированных площадок; тап по
  названию сразу запускает единый конструктор;
- «📝 Создать по личному шаблону» — список личных шаблонов; тап запускает
  конструктор без площадки, создание/правка — в том же разделе;
- «📂 Мои анонсы» — незавершённые анонсы и история (`announcement_drafts`);
- «⚙️ Управление» — площадки, шаблоны, кирпичики и администрирование
  (последнее только при подтверждённых правах, роль перепроверяется в
  обработчике `/admin`);
- «❓ Помощь» — справка.

Команды /start, /help, /new_wizard, /places, /templates, /bricks, /admin,
/drafts и /cancel продолжают работать: меню — навигация, а команды остаются
совместимым входом в те же разделы. `/drafts` ведёт в единый список
«📂 Мои анонсы»; легаси-мастера и его WIP больше нет (S5).
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from admin_state import clear_admin_session, has_admin_session
from brick_state import clear_brick_session, has_brick_draft
from database import SessionLocal
from permissions import can_access_admin_panel, is_superadmin
from place_state import clear_place_session, has_place_session
from template_state import clear_template_session, has_template_session
from utils import screen
from utils import tg_context as ctx
from utils.ui_texts import tr

logger = logging.getLogger(__name__)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """
    Inline-клавиатура главного меню Poster 2.0 — ровно пять кнопок.

    Callback-данные: pick_place — выбор зарегистрированной площадки и
    немедленный старт конструктора, pick_template — выбор личного шаблона,
    my_announcements — история анонсов, manage — раздел управления,
    help — справка.
    """
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.main.create_place"), callback_data="pick_place"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.main.create_template"), callback_data="pick_template"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.main.announcements"), callback_data="my_announcements"
                )
            ],
            [InlineKeyboardButton(tr("buttons.main.manage"), callback_data="manage")],
            [InlineKeyboardButton(tr("buttons.main.help"), callback_data="help")],
        ]
    )


def manage_keyboard(*, show_admin: bool = False) -> InlineKeyboardMarkup:
    """
    Подменю «⚙️ Управление».

    «Администрирование» добавляется только при подтверждённых правах;
    обработчик `/admin` перепроверяет роль заново, поэтому скрытая кнопка —
    удобство, а не защита.
    """
    rows = [
        [
            InlineKeyboardButton(
                tr("buttons.manage.places"), callback_data="manage_places"
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.manage.templates"), callback_data="manage_templates"
            )
        ],
        [
            InlineKeyboardButton(
                tr("buttons.manage.bricks"), callback_data="manage_bricks"
            )
        ],
    ]
    if show_admin:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.manage.admin"), callback_data="admin_panel"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.main_menu"), callback_data="main_menu")]
    )
    return InlineKeyboardMarkup(rows)


def _show_admin_button(telegram_user_id: int) -> bool:
    """Видимость администрирования; роль всё равно перепроверяется в handler."""
    if is_superadmin(telegram_user_id):
        return True
    session = SessionLocal()
    try:
        return can_access_admin_panel(session, telegram_user_id)
    except Exception as e:  # noqa: BLE001 — меню должно открыться и без кабинета
        # Причина отказа видимости должна быть видна в логах: раньше здесь
        # был только текст ошибки без traceback, и «кнопка исчезла» было
        # невозможно диагностировать (S2, §7.5, гипотеза 3).
        logger.exception("Не удалось проверить доступ в кабинет: %s", e)
        return False
    finally:
        session.close()


def main_menu_text() -> str:
    """Текст экрана главного меню."""
    return tr("menu.main")


def manage_text() -> str:
    """Текст экрана «Управление»."""
    return tr("menu.manage")


async def render_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Рисует главное меню на существующем экране (или создаёт новый экран).

    После рестарта процесса ref экрана восстанавливается из БД один раз —
    /start редактирует старое сообщение, а не плодит дубли. Меню всегда
    состоит из постоянных пяти кнопок: легаси-WIP удалён (S5), поэтому
    условных кнопок «продолжить» нет — незавершённые анонсы живут в
    «📂 Мои анонсы».
    """
    # навигация пользовательским сообщением → старый экран удаляется,
    # новый создаётся рядом с вводом (Stage 16, дефект №7); callback —
    # guard-пропуск, кнопки и так на экране
    await screen.reanchor_screen(update, context)
    # R1, дефект №23: главное меню — вход в актуальный UI, у клиента могла
    # остаться reply-клавиатура старых версий. Снимается один раз на вход.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_data = ctx.data(context)
    user_id = ctx.user(update).id
    # Главное меню — другая ветка сценария: незавершённый выбор места,
    # конструктор личного кирпичика и административный ввод не должны
    # перехватывать следующий текст. WIP-пост при этом не трогается.
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context, user_data, chat_id, main_menu_text(), main_menu_keyboard()
    )
    # ref экрана закрепляется в БД; payload не трогаем
    screen.save_screen_ref(user_id, user_data, chat_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Команда /start: главное меню или конструктор по deep-link.

    Стартовый личный шаблон выдаётся раньше — при первом контакте с ботом
    (`handlers/user_tracking.py`), поэтому `/start` внутри активного диалога
    ничего не теряет: выдача не зависит от того, какая ветка обработала
    команду. Повторный `/start` и удаление шаблона его не воскрешают.

    Deep-link payload разбирается ДО меню: `/start tf_...` — передача прав,
    `/start <token>` — ссылка на создание анонса площадки (Poster 2.2 U4b).
    `/start` без payload работает как прежде.
    """
    from handlers.admin import handle_transfer_start
    from handlers.place_invite import open_invite_payload

    if await handle_transfer_start(update, context):
        # /start с payload передачи — отдельный экран; команду убираем так же,
        # как в обычном меню. WIP-пост не трогается.
        await screen.drop_user_input(update, context)
        return ConversationHandler.END
    invite_state = await open_invite_payload(update, context)
    if invite_state is not None:
        # Ссылка обработана: конструктор площадки или отказ. WIP-пост не
        # трогается; сообщение с командой обработчик убирает сам.
        return invite_state
    await render_main_menu(update, context)
    # Сообщение с командой — временный ввод: меню уже на экране, убираем
    # его best-effort (при сбое очистки диалог не ломается)
    await screen.drop_user_input(update, context)
    return ConversationHandler.END


def help_text() -> str:
    """
    Справка: кнопки, команды и правила ввода Poster 2.0.

    Экран рисуется с parse_mode=HTML (см. utils.screen.render_screen),
    поэтому текст справки — ключ `help.text_html` с доверенной собственной
    разметкой (<b>). Пользовательских данных в нём нет, но лимиты берутся из
    сервисов домена: значения подставляются через `tr` и экранируются.
    Импорт лимитов локальный: справка не должна тянуть сервисы домена при
    импорте модуля.
    """
    from announcement_store import MAX_ANNOUNCEMENT_PHOTOS
    from delivery import ANNOUNCEMENT_CAPTION_LIMIT, ANNOUNCEMENT_TEXT_LIMIT

    return tr(
        "help.text_html",
        max_photos=MAX_ANNOUNCEMENT_PHOTOS,
        text_limit=ANNOUNCEMENT_TEXT_LIMIT,
        caption_limit=ANNOUNCEMENT_CAPTION_LIMIT,
    )


async def render_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка на экране управления (с возвратом в главное меню)."""
    # навигация сообщением ниже экрана → новый экран рядом с вводом
    await screen.reanchor_screen(update, context)
    clear_place_session(ctx.user(update).id)
    clear_admin_session(ctx.user(update).id)
    clear_brick_session(ctx.user(update).id)
    clear_template_session(ctx.user(update).id)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.nav.main_menu"), callback_data="main_menu")]]
    )
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    await screen.render_screen(
        context, user_data, ctx.chat(update).id, help_text(), keyboard
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Справка по кнопкам меню и командам."""
    await render_help(update, context)


async def menu_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inline-кнопка «❓ Помощь» главного меню."""
    await ctx.query(update).answer()
    await render_help(update, context)


async def drafts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Команда /drafts — совместимый вход в единый список «📂 Мои анонсы».

    Отдельного легаси-хранилища Draft больше нет (S5): команда открывает
    тот же экран, что и кнопка меню, без второго списка.
    """
    from handlers.announcement_wizard import open_my_announcements

    await open_my_announcements(update, context)


async def menu_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Совместимость: старая кнопка «📂 Черновики» в открытых экранах."""
    await ctx.query(update).answer()
    from handlers.announcement_wizard import open_my_announcements

    await open_my_announcements(update, context)


async def open_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Экран «⚙️ Управление»: разделы и администрирование по правам."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context,
        user_data,
        chat_id,
        manage_text(),
        manage_keyboard(show_admin=_show_admin_button(user_id)),
    )
    screen.save_screen_ref(user_id, user_data, chat_id)


async def manage_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Управление» → «Площадки»: справочник мест (не выбор для анонса)."""
    from handlers.places import open_places_manage

    await open_places_manage(update, context)


async def manage_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «Управление» → «Шаблоны»: только управление шаблонами.

    Вход не возобновляет сохранённую фазу `pick` и никогда не запускает
    создание анонса (R2, дефект №25): раздел открывается списком управления.
    """
    from handlers.templates import open_templates

    await open_templates(update, context)


async def manage_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«Управление» → «Кирпичики»."""
    from handlers.bricks import open_bricks

    await open_bricks(update, context)


async def cancel_without_dialog(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """/cancel без активного диалога: отмена места/кабинета или подсказка."""
    # навигация сообщением ниже экрана → новый экран рядом с вводом
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    place_active = has_place_session(user_id)
    admin_active = has_admin_session(user_id)
    brick_active = has_brick_draft(user_id)
    template_active = has_template_session(user_id)
    if place_active:
        clear_place_session(user_id)
    if admin_active:
        clear_admin_session(user_id)
    if brick_active:
        clear_brick_session(user_id)
    if template_active:
        clear_template_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    if place_active:
        text = tr("menu.cancel.place")
    elif admin_active:
        text = tr("menu.cancel.admin")
    elif brick_active:
        text = tr("menu.cancel.brick")
    elif template_active:
        text = tr("menu.cancel.template")
    else:
        text = tr("menu.cancel.none")
    cancelled = place_active or admin_active or brick_active or template_active
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        text,
        main_menu_keyboard() if cancelled else None,
    )
    if cancelled:
        await screen.drop_user_input(update, context)


async def outside_dialog_hint(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Подсказка на обычный текст/фото ВНЕ диалога: обновляется на экране
    управления, а не копится новыми сообщениями.

    Внутри диалога этот хендлер не вызывается: ConversationHandler анонсов и
    прежнего мастера зарегистрированы первой группой и свой ввод обслуживают
    сами.
    """
    message = ctx.message(update)
    # навигация сообщением ниже экрана → подсказка рядом с вводом
    await screen.reanchor_screen(update, context)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, ctx.user(update).id)
    if message.photo:
        text = tr("menu.outside.photo")
    else:
        text = tr("menu.outside.text")
    await screen.render_screen(context, user_data, ctx.chat(update).id, text)


async def handle_main_menu_selection(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Callback «main_menu» ВНЕ диалога конструктора: возврат в главное меню.

    Раньше жил в `handlers/callbacks.py`; после удаления легаси (S5) это
    единственный живой маршрут того модуля, поэтому он переехал сюда.
    Экран перерисовывается (Stage 11: один живой экран управления), новых
    сообщений не создаётся.
    """
    await ctx.query(update).answer()
    await render_main_menu(update, context)


def main_menu_handlers() -> list[BaseHandler]:
    """
    Обработчики главного меню Poster 2.0.

    Кнопки создания анонса — entry-точки ConversationHandler анонсов
    (`handlers/announcement_wizard.py`), поэтому здесь они не регистрируются:
    состояние диалога иначе не отслеживалось бы. «Мои анонсы» и «Управление»
    — экраны навигации без собственного диалога.

    Порядок важен: /cancel вне диалога и подсказка на обычный текст/фото
    стоят ПОСЛЕ команд и callback'ов — они не перехватывают ни их, ни
    состояния диалогов (ConversationHandler'ы идут первой группой в bot.py).
    """
    handlers: list[BaseHandler] = [
        CommandHandler("start", start),
        CommandHandler("help", help_command),
        CallbackQueryHandler(handle_main_menu_selection, pattern=r"^main_menu$"),
        CommandHandler("drafts", drafts_command),
        CommandHandler("cancel", cancel_without_dialog),
        CallbackQueryHandler(menu_drafts, pattern="^drafts$"),
        CallbackQueryHandler(menu_help, pattern="^help$"),
        CallbackQueryHandler(open_manage, pattern="^manage$"),
        CallbackQueryHandler(manage_places, pattern="^manage_places$"),
        CallbackQueryHandler(manage_templates, pattern="^manage_templates$"),
        CallbackQueryHandler(manage_bricks, pattern="^manage_bricks$"),
        # Последним: обычный текст/фото вне диалога раньше оставались
        # без ответа («тишина»). Команды исключены (~COMMAND), поэтому
        # unknown_command и dialog-fallback'и работают как прежде.
        MessageHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND, outside_dialog_hint
        ),
    ]
    return handlers
