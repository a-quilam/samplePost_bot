# bot.py

import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from handlers.main_menu import main_menu_handlers, start, help_command

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Пример команды /add_responsible
async def add_responsible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет ответственного пользователя (только админам)"""
    admin_ids = [123456789, 987654321]  # Замените на реальные Telegram ID админов
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    try:
        name = context.args[0]
        telegram_id = int(context.args[1])
        await update.message.reply_text(f"Добавлен ответственный: {name} (ID: {telegram_id})")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /add_responsible <Имя> <Telegram_ID>")

# Пример команды /remove_responsible
async def remove_responsible(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет ответственного пользователя (только админам)"""
    admin_ids = [123456789, 987654321]  # Замените на реальные Telegram ID админов
    if update.effective_user.id not in admin_ids:
        await update.message.reply_text("У вас нет прав для выполнения этой команды.")
        return

    try:
        telegram_id = int(context.args[0])
        await update.message.reply_text(f"Удалён ответственный с ID: {telegram_id}")
    except (IndexError, ValueError):
        await update.message.reply_text("Использование: /remove_responsible <Telegram_ID>")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик неизвестных команд"""
    await update.message.reply_text("Извините, я не понимаю эту команду. Используйте /help для списка доступных команд.")

def main():
    """Основная функция для запуска бота"""
    application = ApplicationBuilder().token("7379612076:AAE3YZV8JiKcNx61w4You4L_OZkixipP7m8").build()

    # Добавление обработчиков команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add_responsible", add_responsible))
    application.add_handler(CommandHandler("remove_responsible", remove_responsible))

    # Добавление ConversationHandler для главного меню
    application.add_handler(main_menu_handlers())

    # Обработчик неизвестных команд
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    # Запуск бота
    application.run_polling()

if __name__ == "__main__":
    main()
