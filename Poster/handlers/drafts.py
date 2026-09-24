# handlers/drafts.py

from html import escape as html_escape

from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import BaseHandler, CallbackQueryHandler, ContextTypes

from approval import remove_draft
from database import SessionLocal
from models import Draft
from utils import tg_context as ctx


def _h(value: object) -> str:
    """HTML-экранирование значения с защитой от None (поля в БД nullable)."""
    return html_escape(str(value)) if value is not None else "—"


def build_drafts_message(
    drafts: list[Draft],
) -> tuple[str, InlineKeyboardMarkup | None]:
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
    drafts = session.query(Draft).filter(Draft.user_id == user_id).all()
    session.close()
    text, markup = build_drafts_message(drafts)
    await message.reply_text(text, parse_mode="HTML", reply_markup=markup)


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
    ]
