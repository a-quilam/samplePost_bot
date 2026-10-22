# handlers/drafts.py
"""
Черновики: список, листание, удаление с подтверждением (Stage 11).

Список черновиков живёт на ЭКРАНЕ управления (utils.screen): открытие,
листание страниц и удаление перерисовывают его, а не плодят сообщения.
Удаление — двухшаговое (подтверждение), чтобы случайное нажатие «❌»
не уничтожало работу. Чужие черновики недоступны: проверка user_id
повторяется и при подтверждении.
"""

from html import escape as html_escape

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from database import SessionLocal
from models import Draft
from place_state import clear_place_session
from utils import screen
from utils import tg_context as ctx

# Страница списка черновиков: 2 кнопки на черновик + навигация/меню.
# Без лимита список при 33+ черновиках переполнял бы лимит Telegram
# на inline-кнопки (100) и обработчик падал бы с BadRequest.
DRAFTS_PAGE_SIZE = 15

# Префикс callback листания: draftpage_<offset>
PAGE_PREFIX = "draftpage_"

# Префикс подтверждения удаления: confirmdelete_<id>
CONFIRM_PREFIX = "confirmdelete_"


def _h(value: object) -> str:
    """HTML-экранирование значения с защитой от None (поля в БД nullable)."""
    return html_escape(str(value)) if value is not None else "—"


def fetch_user_drafts(
    session: Session, user_id: int, offset: int = 0
) -> tuple[list[Draft], int]:
    """
    Страница черновиков пользователя (новые сверху) и их общее число.

    offset может выходить за пределы списка (черновики удаляли между
    нажатиями) — тогда возвращается пустая страница, обработчик откатится
    на первую.
    """
    base = session.query(Draft).filter(Draft.user_id == user_id)
    total = base.count()
    drafts = (
        base.order_by(Draft.id.desc())
        .offset(max(offset, 0))
        .limit(DRAFTS_PAGE_SIZE)
        .all()
    )
    return drafts, total


def build_drafts_message(
    drafts: list[Draft],
    *,
    offset: int = 0,
    total: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Список черновиков: карточки + кнопки действий + навигация по страницам.

    total — общее число черновиков пользователя (всегда передаётся вызывающим:
    из него считается «Показано X из Y» и наличие кнопки «Вперёд»).
    """
    if not drafts:
        return "У вас пока нет черновиков.", None

    message_text = "📄 <b>Ваши черновики:</b>\n\n"
    keyboard = []
    for draft in drafts:
        message_text += (
            f"📝 <b>Черновик {draft.id}</b>\n"
            f"📢 {_h(draft.title)}\n"
            f"📅 {_h(draft.date)}\n"
            f"⏰ {_h(draft.time_start)} - {_h(draft.time_end)}\n"
            f"📍 {_h(draft.place_name)}\n\n"
        )
        # editdraft_<id> — точка входа ConversationHandler (handlers/post_creation.py):
        # черновик открывается в редакторе и обновляется без создания копии
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"✏️ Редактировать черновик {draft.id}",
                    callback_data=f"editdraft_{draft.id}",
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"❌ Удалить черновик {draft.id}",
                    callback_data=f"delete_{draft.id}",
                )
            ]
        )

    if total > len(drafts):
        message_text += (
            f"<i>Показано {len(drafts)} из {total} — " "листайте кнопками ниже.</i>\n"
        )

    # Навигация: назад — на страницу назад, вперёд — сразу за текущей
    nav_row = []
    if offset > 0:
        nav_row.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"{PAGE_PREFIX}{max(offset - DRAFTS_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(drafts) < total:
        nav_row.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"{PAGE_PREFIX}{offset + len(drafts)}",
            )
        )
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append(
        [InlineKeyboardButton("↩️ Главное меню", callback_data="main_menu")]
    )
    return message_text, InlineKeyboardMarkup(keyboard)


def _menu_button_keyboard() -> InlineKeyboardMarkup:
    """Кнопка возврата в меню для пустого списка (экран не должен быть тупиком)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Главное меню", callback_data="main_menu")]]
    )


async def _render_drafts_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    offset: int = 0,
    notice: str | None = None,
) -> None:
    """
    Рисует список черновиков на экране управления.

    notice — короткий статус перед списком («Черновик N удалён.»),
    чтобы действие получило видимый результат без нового сообщения.
    """
    user_id = ctx.user(update).id
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    session: Session = SessionLocal()
    try:
        drafts, total = fetch_user_drafts(session, user_id, offset=offset)
        if not drafts and total > 0 and offset > 0:
            # запрошенная страница опустела (черновики удалили) — возврат
            # к первой, как и раньше
            offset = 0
            drafts, total = fetch_user_drafts(session, user_id, offset=0)
    finally:
        session.close()
    text, markup = build_drafts_message(drafts, offset=offset, total=total)
    if markup is None:
        markup = _menu_button_keyboard()
    if notice:
        text = f"{notice}\n\n{text}"
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        text,
        reply_markup=markup,
    )


async def view_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Список черновиков на экране (кнопка меню, команда /drafts, выход из
    диалога). Сообщение команды — временный ввод: удаляется best-effort
    после того, как список показан на экране (в callback-контексте
    drop_user_input ничего не делает).
    """
    # навигация сообщением ниже экрана → список рядом с вводом
    # (Stage 16, дефект №7; callback — guard-пропуск)
    await screen.reanchor_screen(update, context)
    clear_place_session(ctx.user(update).id)
    await _render_drafts_screen(update, context)
    await screen.drop_user_input(update, context)


async def handle_drafts_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Листание списка черновиков: callback draftpage_<offset>.

    Страница перерисовывается на том же экране (edit); если запрошенная
    страница опустела (черновики удалили) — возврат к первой странице.
    """
    query = ctx.query(update)
    await query.answer()

    try:
        offset = int((query.data or f"{PAGE_PREFIX}0")[len(PAGE_PREFIX) :])
    except ValueError:
        offset = 0
    await _render_drafts_screen(update, context, offset=max(offset, 0))


async def delete_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Первый шаг удаления: подтверждение на экране.

    Сам черновик пока НЕ удаляется — только вопрос «Удалить черновик N?»
    с кнопками «✅ Да, удалить»/«◀️ Нет, к списку». Чужой/несуществующий
    черновик — отказ без подтверждения.
    """
    query = ctx.query(update)
    await query.answer()
    data = query.data or "delete_0"
    draft_id = int(data.split("_")[1])
    session: Session = SessionLocal()
    try:
        draft = session.query(Draft).filter(Draft.id == draft_id).first()
        own = draft is not None and draft.user_id == query.from_user.id
        title = draft.title if draft is not None and own else None
    finally:
        session.close()
    if not own:
        # Удаление чужого/несуществующего черновика невозможно: отказ +
        # актуальный список вместо подтверждения
        await _render_drafts_screen(update, context, notice="Черновик не найден.")
        return
    await screen.render_screen(
        context,
        ctx.data(context),
        ctx.chat(update).id,
        f"❓ Удалить черновик {draft_id}?\n\n"
        f"📢 {_h(title)}\n\n"
        "Действие необратимо.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Да, удалить",
                        callback_data=f"{CONFIRM_PREFIX}{draft_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "◀️ Нет, к списку", callback_data=f"{PAGE_PREFIX}0"
                    )
                ],
            ]
        ),
    )


async def confirm_delete_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Второй шаг удаления: подтверждено → удаляется ТОЛЬКО СВОЙ черновик
    (user_id проверяется повторно — чужие и несуществующие недоступны),
    затем список перерисовывается на экране со статусом удаления.
    """
    query = ctx.query(update)
    await query.answer()
    data = query.data or f"{CONFIRM_PREFIX}0"
    draft_id = int(data[len(CONFIRM_PREFIX) :])
    session: Session = SessionLocal()
    notice = "Черновик не найден."
    try:
        draft = session.query(Draft).filter(Draft.id == draft_id).first()
        if draft is not None and draft.user_id == query.from_user.id:
            session.delete(draft)
            session.commit()
            notice = f"Черновик {draft_id} удалён."
    except Exception:
        session.rollback()
        notice = "Ошибка при удалении черновика."
    finally:
        session.close()
    await _render_drafts_screen(update, context, notice=notice)


def drafts_handlers() -> list[BaseHandler]:
    # Callback 'main_menu' обрабатывается в handlers/callbacks.py (handle_main_menu_selection);
    # здесь он не регистрируется, чтобы избежать конфликта двух обработчиков.
    return [
        CallbackQueryHandler(delete_draft, pattern=r"^delete_\d+$"),
        CallbackQueryHandler(confirm_delete_draft, pattern=r"^confirmdelete_\d+$"),
        CallbackQueryHandler(handle_drafts_page, pattern=r"^draftpage_\d+$"),
    ]
