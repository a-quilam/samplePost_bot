# handlers/drafts.py

from html import escape as html_escape

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from approval import remove_draft
from database import SessionLocal
from models import Draft
from utils import tg_context as ctx

# Страница списка черновиков: 2 кнопки на черновик + навигация/меню.
# Без лимита список при 33+ черновиках переполнял бы лимит Telegram
# на inline-кнопки (100) и обработчик падал бы с BadRequest.
DRAFTS_PAGE_SIZE = 15

# Префикс callback листания: draftpage_<offset>
PAGE_PREFIX = "draftpage_"


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
    total: int | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Список черновиков: карточки + кнопки действий + навигация по страницам.

    total=None (вызовы без пагинации) — страница считается единственной,
    кнопок листания нет.
    """
    if not drafts:
        return "У вас пока нет черновиков.", None

    effective_total = total if total is not None else offset + len(drafts)
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

    if effective_total > len(drafts):
        message_text += (
            f"<i>Показано {len(drafts)} из {effective_total} — "
            "листайте кнопками ниже.</i>\n"
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
    if offset + len(drafts) < effective_total:
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


async def view_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Вызывается и по кнопке reply-клавиатуры («📝 Черновики»), и после
    # удаления черновика из callback — effective_message работает в обоих случаях.
    message = ctx.message(update)
    user_id = ctx.user(update).id
    session: Session = SessionLocal()
    try:
        drafts, total = fetch_user_drafts(session, user_id, offset=0)
    finally:
        session.close()
    text, markup = build_drafts_message(drafts, offset=0, total=total)
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def handle_drafts_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Листание списка черновиков: callback draftpage_<offset>.

    Страница перерисовывается в том же сообщении (edit_message_text);
    если запрошенная страница опустела (черновики удалили) — возврат
    к первой странице.
    """
    query = ctx.query(update)
    await query.answer()

    try:
        offset = int((query.data or f"{PAGE_PREFIX}0")[len(PAGE_PREFIX) :])
    except ValueError:
        offset = 0
    offset = max(offset, 0)

    session: Session = SessionLocal()
    try:
        drafts, total = fetch_user_drafts(session, query.from_user.id, offset)
        if not drafts and total > 0:
            offset = 0
            drafts, total = fetch_user_drafts(session, query.from_user.id, 0)
    finally:
        session.close()

    text, markup = build_drafts_message(drafts, offset=offset, total=total)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)


async def delete_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Удаление черновика. Активное согласование (assigned/approved/published)
    запрещает удаление — иначе «осиротеют» записи и уведомления ответственного.
    Отклонённый пост удаляется вместе со своей записью согласования.
    """
    query = ctx.query(update)
    await query.answer()
    data = query.data or "delete_0"
    draft_id = int(data.split("_")[1])
    session: Session = SessionLocal()
    draft = session.query(Draft).filter(Draft.id == draft_id).first()
    try:
        outcome = remove_draft(session, draft, query.from_user.id)
        texts = {
            "ok": f"Черновик {draft_id} удалён.",
            "not_found": "Черновик не найден.",
            "blocked": "Черновик нельзя удалить: пост находится на согласовании.",
        }
        text = texts.get(outcome, "Черновик не найден.")
    except Exception:
        session.rollback()
        text = "Ошибка при удалении черновика."
    finally:
        session.close()
    await query.edit_message_text(text)
    await view_drafts(update, context)


def drafts_handlers() -> list[BaseHandler]:
    # Callback 'main_menu' обрабатывается в handlers/callbacks.py (handle_main_menu_selection);
    # здесь он не регистрируется, чтобы избежать конфликта двух обработчиков.
    return [
        CallbackQueryHandler(delete_draft, pattern=r"^delete_\d+$"),
        CallbackQueryHandler(handle_drafts_page, pattern=r"^draftpage_\d+$"),
    ]
