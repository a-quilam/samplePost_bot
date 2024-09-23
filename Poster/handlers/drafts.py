# handlers/drafts.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)
from models import Draft
from sqlalchemy.orm import Session
from config import ADMIN_IDS, REVIEW_CHAT_ID
from utils.formatter import format_text

def build_drafts_message(drafts: list) -> (str, InlineKeyboardMarkup):
    """
    Формирует текст сообщения и клавиатуру с кнопками для удаления черновиков.
    """
    if not drafts:
        return "У вас пока нет черновиков.", None
    
    message_text = "📄 <b>Ваши черновики:</b>\n\n"
    keyboard = []
    
    for draft in drafts:
        message_text += f"📝 <b>Черновик {draft.id}</b>\n"
        message_text += f"📢 {format_text(draft.title)}\n"
        message_text += f"📅 {format_text(draft.date)}\n"
        message_text += f"⏰ {format_text(draft.time_start)} - {format_text(draft.time_end)}\n"
        message_text += f"📍 {format_text(draft.place_name)}\n\n"
        
        # Добавляем кнопку для удаления этого черновика
        keyboard.append([InlineKeyboardButton(f"❌ Удалить черновик {draft.id}", callback_data=f'delete_{draft.id}')])
    
    # Добавляем кнопку для возврата в главное меню
    keyboard.append([InlineKeyboardButton("↩️ Главное меню", callback_data='main_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    return message_text, reply_markup

async def view_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message if update.message else update.callback_query.message
    """
    Отправляет пользователю список его черновиков с кнопками для удаления.
    """
    user_id = update.effective_user.id
    session: Session = SessionLocal()
    
    drafts = session.query(Draft).filter(Draft.user_id == user_id).all()
    
    message_text, reply_markup = build_drafts_message(drafts)
    
    await message.reply_text(
        message_text,
        parse_mode='HTML',
        reply_markup=reply_markup
    )

async def delete_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Удаляет черновик по ID и уведомляет пользователя.
    """
    query = update.callback_query
    await query.answer()
    
    data = query.data
    if not data.startswith('delete_'):
        await query.edit_message_text("Неизвестная команда.")
        return
    
    draft_id = int(data.split('_')[1])
    user_id = query.from_user.id
    session: Session = context.bot_data['db_session']
    
    draft = session.query(Draft).filter(Draft.id == draft_id, Draft.user_id == user_id).first()
    
    if draft:
        session.delete(draft)
        session.commit()
        await query.edit_message_text(f"Черновик {draft_id} успешно удалён.")
        
        # Отправить обновлённый список черновиков
        drafts = session.query(Draft).filter(Draft.user_id == user_id).all()
        message_text, reply_markup = build_drafts_message(drafts)
        
        if drafts:
            await query.message.reply_text(
                message_text,
                parse_mode='HTML',
                reply_markup=reply_markup
            )
        else:
            await query.message.reply_text(
                "У вас пока нет черновиков.",
                reply_markup=None
            )
    else:
        await query.edit_message_text("Черновик не найден или у вас нет прав для его удаления.")
    
    session.close()

def drafts_handlers() -> list:
    """
    Возвращает список обработчиков для управления черновиками.
    """
    return [
        CallbackQueryHandler(delete_draft, pattern=r'^delete_\d+$'),
        CallbackQueryHandler(view_drafts, pattern='^main_menu$'),
    ]
