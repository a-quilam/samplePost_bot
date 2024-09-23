# handlers/admin.py

from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, filters
from sqlalchemy.orm import Session

from config import ADMIN_IDS
from database import SessionLocal
from models import ResponsiblePerson

async def add_responsible(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /add_responsible <Имя> <Telegram_ID>
    Добавляет нового ответственного лица в базу данных.
    """
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    args = context.args

    if len(args) != 2:
        await update.message.reply_text("Использование: /add_responsible <Имя> <Telegram_ID>")
        return

    name, telegram_id_str = args

    if not telegram_id_str.isdigit():
        await update.message.reply_text("Telegram_ID должен быть числом.")
        return

    telegram_id = int(telegram_id_str)

    session: Session = SessionLocal()

    existing_person = session.query(ResponsiblePerson).filter_by(telegram_id=telegram_id).first()

    if existing_person:
        await update.message.reply_text(f"Ответственный с Telegram_ID {telegram_id} уже существует.")
        session.close()
        return

    new_person = ResponsiblePerson(name=name, telegram_id=telegram_id)
    session.add(new_person)
    session.commit()
    session.close()

    await update.message.reply_text(f"Ответственный {name} с Telegram_ID {telegram_id} добавлен успешно.")

async def remove_responsible(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /remove_responsible <Telegram_ID>
    Удаляет ответственного лица из базы данных.
    """
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    args = context.args

    if len(args) != 1:
        await update.message.reply_text("Использование: /remove_responsible <Telegram_ID>")
        return

    telegram_id_str = args[0]

    if not telegram_id_str.isdigit():
        await update.message.reply_text("Telegram_ID должен быть числом.")
        return

    telegram_id = int(telegram_id_str)

    session: Session = SessionLocal()

    person = session.query(ResponsiblePerson).filter_by(telegram_id=telegram_id).first()

    if not person:
        await update.message.reply_text(f"Ответственный с Telegram_ID {telegram_id} не найден.")
        session.close()
        return

    session.delete(person)
    session.commit()
    session.close()

    await update.message.reply_text(f"Ответственный {person.name} с Telegram_ID {telegram_id} удалён успешно.")

def admin_handlers() -> list:
    """
    Возвращает список обработчиков административных команд.
    """
    return [
        CommandHandler('add_responsible', add_responsible, filters=filters.ChatType.PRIVATE),
        CommandHandler('remove_responsible', remove_responsible, filters=filters.ChatType.PRIVATE),
    ]
