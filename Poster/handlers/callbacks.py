# handlers/callbacks.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)
from sqlalchemy.orm import Session

from config import REVIEW_CHAT_ID
from database import SessionLocal
from models import Draft, ResponsiblePerson
from utils.formatter import format_text
from handlers.main_menu import main_menu_handler  # Импортируем обработчик главного меню

# Обработчик действий после создания поста: сохранение в черновики, отправка на согласование, редактирование
async def handle_post_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == 'save_draft':
        await save_draft(query, context)
    elif action == 'send_for_approval':
        await send_for_approval(query, context)
    elif action == 'edit_post':
        await edit_post(query, context)
    else:
        await query.edit_message_text("Неизвестное действие.", reply_markup=None)

async def save_draft(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    session: Session = SessionLocal()
    try:
        draft = Draft(
            user_id=query.from_user.id,
            title=context.user_data.get('title', 'Без заголовка'),
            date=context.user_data.get('date', 'Не указана'),
            time_start=context.user_data.get('time_start', 'Не указано'),
            time_end=context.user_data.get('time_end', 'Не указано'),
            place_name=context.user_data.get('place_name', 'Не указано'),
            place_url=context.user_data.get('place_url', ''),
            text=context.user_data.get('text', 'Без текста'),
            contact=context.user_data.get('contact', 'Не указано'),
            image=context.user_data.get('image')
        )
        session.add(draft)
        session.commit()
    except Exception as e:
        await query.message.reply_text(f"Ошибка при сохранении черновика: {e}")
        session.rollback()
    finally:
        session.close()

    await query.edit_message_caption(
        caption="Пост сохранён в черновики.",
        parse_mode='MarkdownV2',
        reply_markup=None
    )
    await query.message.reply_text(
        "Пост сохранён в черновики.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
        ])
    )
    context.user_data.clear()

async def send_for_approval(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    session: Session = SessionLocal()
    try:
        post_data = context.user_data

        post = (
            f"📢 *{post_data.get('title', 'Без заголовка')}*\n\n"
            f"📅 *Дата*: {post_data.get('date', 'Не указана')}\n"
            f"⏰ *Время*: {post_data.get('time_start', 'Не указано')} - {post_data.get('time_end', 'Не указано')}\n"
            f"📍 *Место*: [{post_data.get('place_name', 'Не указано')}]({post_data.get('place_url', '')})\n\n"
            f"{post_data.get('text', 'Без текста')}\n\n"
            f"📞 *Контакт*: {post_data.get('contact', 'Не указано')}"
        )

        if post_data.get('image'):
            await context.bot.send_photo(
                chat_id=REVIEW_CHAT_ID,
                photo=post_data['image'],
                caption=post,
                parse_mode='MarkdownV2'
            )
        else:
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text=post,
                parse_mode='MarkdownV2'
            )

        # Получение списка ответственных лиц
        responsible_persons = session.query(ResponsiblePerson).all()

        if responsible_persons:
            keyboard = [
                [InlineKeyboardButton(person.name, callback_data=f'responsible_{person.telegram_id}')]
                for person in responsible_persons
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text="Выберите ответственного за этот пост:",
                reply_markup=reply_markup
            )
        else:
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text="Нет ответственных лиц для назначения."
            )
    except Exception as e:
        await query.message.reply_text(f"Ошибка при отправке на согласование: {e}")
        session.rollback()
    finally:
        session.close()

    await query.edit_message_caption(
        caption="Пост отправлен на согласование.",
        parse_mode='MarkdownV2',
        reply_markup=None
    )
    await query.message.reply_text(
        "Пост отправлен на согласование.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Главное меню", callback_data='main_menu')]
        ])
    )
    context.user_data.clear()

async def edit_post(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    await query.edit_message_caption(
        caption="Редактирование поста. Выберите поле для редактирования:",
        parse_mode='MarkdownV2',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Заголовок", callback_data='edit_title')],
            [InlineKeyboardButton("Дата", callback_data='edit_date')],
            [InlineKeyboardButton("Время начала", callback_data='edit_time_start')],
            [InlineKeyboardButton("Время конца", callback_data='edit_time_end')],
            [InlineKeyboardButton("Место", callback_data='edit_place')],
            [InlineKeyboardButton("Текст", callback_data='edit_text')],
            [InlineKeyboardButton("Контакт", callback_data='edit_contact')],
            [InlineKeyboardButton("Картинка", callback_data='edit_image')],
            [InlineKeyboardButton("Отмена", callback_data='cancel_edit')]
        ])
    )
    return

# Обработчик выбора ответственного лица
async def handle_responsible_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith('responsible_'):
        try:
            telegram_id = int(data.split('_')[1])
        except ValueError:
            await query.edit_message_text(
                text="Неверный формат Telegram_ID.",
                parse_mode='MarkdownV2'
            )
            return

        session: Session = SessionLocal()
        try:
            person = session.query(ResponsiblePerson).filter_by(telegram_id=telegram_id).first()
            if person:
                # Отправка уведомления ответственному лицу
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=f"Вам назначен ответственный за новый пост:\n\n{format_text(context.user_data.get('title', 'Без заголовка'))}"
                )
                await query.edit_message_text(
                    text=f"Ответственный назначен: {person.name}",
                    parse_mode='MarkdownV2'
                )
            else:
                await query.edit_message_text(
                    text="Ответственный не найден.",
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            await query.edit_message_text(
                text=f"Ошибка при назначении ответственного: {e}",
                parse_mode='MarkdownV2'
            )
            session.rollback()
        finally:
            session.close()

# Обработчик главного меню из CallbackQuery
async def handle_main_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'main_menu':
        # Вызываем обработчик главного меню
        await main_menu_handler(update, context)
    else:
        await query.edit_message_text("Неизвестное действие.", reply_markup=None)

def callbacks_handlers() -> list:
    """
    Возвращает список обработчиков для CallbackQuery.
    """
    return [
        CallbackQueryHandler(handle_post_action, pattern='^(save_draft|send_for_approval|edit_post)$'),
        CallbackQueryHandler(handle_responsible_selection, pattern='^responsible_\\d+$'),
        CallbackQueryHandler(handle_main_menu_selection, pattern='^main_menu$'),
    ]
