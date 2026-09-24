# handlers/drafts.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import CallbackQueryHandler, ContextTypes
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Draft
from utils.formatter import format_text

def build_drafts_message(drafts: list) -> (str, InlineKeyboardMarkup):
    if not drafts:
        return "У вас пока нет черновиков.", None
    message_text = "📄 <b>Ваши черновики:</b>\n\n"
    keyboard = []
    for draft in drafts:
        message_text += f"📝 <b>Черновик {draft.id}</b>\n📢 {format_text(draft.title)}\n📅 {format_text(draft.date)}\n⏰ {format_text(draft.time_start)} - {format_text(draft.time_end)}\n📍 {format_text(draft.place_name)}\n\n"
        keyboard.append([InlineKeyboardButton(f"❌ Удалить черновик {draft.id}", callback_data=f'delete_{draft.id}')])
    keyboard.append([InlineKeyboardButton("↩️ Главное меню", callback_data='main_menu')])
    return message_text, InlineKeyboardMarkup(keyboard)

async def view_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    user_id = update.effective_user.id
    session: Session = SessionLocal()
    drafts = session.query(Draft).filter(Draft.user_id == user_id).all()
    session.close()
    text, markup = build_drafts_message(drafts)
    await message.reply_text(text, parse_mode='HTML', reply_markup=markup)

async def delete_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    draft_id = int(query.data.split('_')[1])
    session: Session = SessionLocal()
    draft = session.query(Draft).filter(Draft.id == draft_id, Draft.user_id == query.from_user.id).first()
    if draft:
        session.delete(draft)
        session.commit()
        text = f"Черновик {draft_id} удалён."
    else:
        text = "Черновик не найден."
    session.close()
    await query.edit_message_text(text)
    await view_drafts(update, context)

def drafts_handlers() -> list:
    return [
        CallbackQueryHandler(delete_draft, pattern=r'^delete_\d+$'),
        CallbackQueryHandler(view_drafts, pattern='^main_menu$'),
    ]
