# handlers/main_menu.py

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from utils.formatter import format_text
from models import Draft

# Определяем основные опции меню
MAIN_MENU_OPTIONS = [
    ['✏️ Создать пост'],
    ['📝 Черновики']
]

# Создаём разметку для основного меню
main_menu_markup = ReplyKeyboardMarkup(MAIN_MENU_OPTIONS, resize_keyboard=True, one_time_keyboard=True)

# Определяем константы состояний для ConversationHandler
MAIN_MENU, POST_TITLE = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработчик команды /start.
    Отправляет приветственное сообщение и отображает главное меню.
    """
    user_first_name = update.effective_user.first_name
    welcome_message = (
        f"Здравствуйте, {user_first_name}! 👋\n\n"
        "Я помогу вам создать пост по следующему шаблону.\n\n"
        "Выберите одно из действий ниже:"
    )
    
    await update.message.reply_text(
        welcome_message,
        reply_markup=main_menu_markup
    )
    
    return MAIN_MENU  # Возвращаем состояние MAIN_MENU для дальнейшей обработки

async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обработчик выбора из основного меню.
    Перенаправляет пользователя на создание поста или просмотр черновиков.
    """
    user_choice = update.message.text.lower()
    
    if user_choice == '✏️ создать пост':
        # Перенаправляем пользователя к процессу создания поста
        await update.message.reply_text(
            "Отлично! Начнём создание нового поста.",
            reply_markup=ReplyKeyboardRemove()  # Убираем клавиатуру
        )
        return POST_TITLE  # Переводим в состояние POST_TITLE для начала создания поста
    
    elif user_choice == '📝 черновики':
        # Перенаправляем пользователя к просмотру черновиков
        await view_drafts(update, context)
        return MAIN_MENU  # Возвращаемся в главное меню после просмотра черновиков
    
    else:
        # Если выбор не распознан, просим выбрать снова
        await update.message.reply_text(
            "Пожалуйста, выберите одно из доступных действий.",
            reply_markup=main_menu_markup
        )
        return MAIN_MENU

async def view_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пользователю список его черновиков.
    """
    session = context.bot_data.get('db_session')  # Получаем сессию базы данных из bot_data
    user_id = update.effective_user.id
    
    if not session:
        await update.message.reply_text(
            "Ошибка: не удалось подключиться к базе данных.",
            reply_markup=main_menu_markup
        )
        return
    
    drafts = session.query(Draft).filter(Draft.user_id == user_id).all()
    
    if not drafts:
        await update.message.reply_text(
            "У вас пока нет черновиков.",
            reply_markup=main_menu_markup
        )
    else:
        response = "Ваши черновики:\n\n"
        for draft in drafts:
            response += f"📝 <b>Черновик {draft.id}</b>\n"
            response += f"📢 <b>{format_text(draft.title)}</b>\n"
            response += f"📅 Дата: {format_text(draft.date)}\n\n"
        
        await update.message.reply_text(
            response,
            parse_mode='HTML',
            reply_markup=main_menu_markup
        )
    
    # Не закрываем сессию здесь, так как она хранится в bot_data и используется другими обработчиками

def main_menu_handlers() -> list:
    """
    Возвращает список обработчиков для основного меню.
    """
    return [
        CommandHandler('start', start),
        MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu_handler)
    ]
