# handlers/callbacks.py
"""
Обработчики CallbackQuery, которые НЕ входят в ConversationHandler создания поста:
- Назначение ответственного в чате согласования
- Возврат в главное меню из inline-кнопок
"""

from telegram import ReplyKeyboardMarkup, Update, KeyboardButton
from telegram.ext import (
    ContextTypes,
    CallbackQueryHandler,
)
from sqlalchemy.orm import Session

from database import SessionLocal
from models import ResponsiblePerson
from utils.formatter import escape_markdown

# Дополнительные функции для обработки назначения ответственного и главного меню
async def handle_responsible_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает выбор ответственного лица в чате согласования.
    Callback data: responsible_<telegram_id>
    """
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
                # Экранируем только пользовательский ввод (название поста), нашу разметку не трогаем
                title = context.user_data.get('title', 'Без заголовка')
                safe_title = escape_markdown(title)
                await context.bot.send_message(
                    chat_id=telegram_id,
                    text=f"Вам назначен ответственный за новый пост:\n\n{safe_title}",
                    parse_mode='MarkdownV2'
                )
                safe_name = escape_markdown(person.name)
                await query.edit_message_text(
                    text=f"Ответственный назначен: {safe_name}",
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

async def handle_main_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие кнопки "Главное меню" в inline-клавиатуре.
    Заменяет inline-клавиатуру на Reply-клавиатуру главного меню.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'main_menu':
        keyboard = [
            [KeyboardButton('✏️ Создать пост')],
            [KeyboardButton('📝 Черновики')]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await query.edit_message_text(
            "Здравствуйте! 👋\n\nВыберите действие:",
            reply_markup=reply_markup
        )
    else:
        await query.edit_message_text("Неизвестное действие.", reply_markup=None)

def callbacks_handlers() -> list:
    """
    Возвращает список обработчиков для CallbackQuery, не входящих в ConversationHandler.
    """
    return [
        CallbackQueryHandler(handle_responsible_selection, pattern=r'^responsible_\d+$'),
        CallbackQueryHandler(handle_main_menu_selection, pattern='^main_menu$'),
    ]
