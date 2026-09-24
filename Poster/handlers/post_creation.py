import logging
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
    filters,
)
from utils.validators import validate_date, validate_time, validate_url
from utils.formatter import format_text
from models import Draft, ResponsiblePerson
from sqlalchemy.orm import Session
from config import REVIEW_CHAT_ID
from database import SessionLocal

# Настройка логирования
logger = logging.getLogger(__name__)

# Определение состояний
POST_CREATION, EDIT_FIELD = range(2)

# Определение шагов создания поста
POST_STEPS = [
    {
        'key': 'title',
        'prompt': "Введите заголовок поста или нажмите 'Пропустить':",
        'validator': None,  # Нет валидации для заголовка
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'date',
        'prompt': "Введите дату события (ДД.ММ.ГГГГ) или нажмите 'Пропустить':",
        'validator': validate_date,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'time_start',
        'prompt': "Введите время начала (ЧЧ:ММ) или нажмите 'Пропустить':",
        'validator': validate_time,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'time_end',
        'prompt': "Введите время окончания (ЧЧ:ММ) или нажмите 'Пропустить':",
        'validator': validate_time,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'place',
        'prompt': "Введите место проведения или нажмите 'Пропустить':",
        'validator': None,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'text',
        'prompt': "Введите текст поста или нажмите 'Пропустить':",
        'validator': None,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'contact',
        'prompt': "Введите контактную информацию или нажмите 'Пропустить':",
        'validator': None,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'place_url',
        'prompt': "Введите URL места проведения или нажмите 'Пропустить':",
        'validator': validate_url,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'image',
        'prompt': "Отправьте изображение или нажмите 'Пропустить':",
        'validator': None,  # Специальная обработка для изображений
        'formatter': None,
        'optional': True,
    },
]

def get_skip_keyboard():
    """
    Возвращает клавиатуру с кнопкой "Пропустить".
    """
    keyboard = [
        [InlineKeyboardButton("Пропустить", callback_data='skip')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_actions_keyboard():
    """
    Возвращает клавиатуру с действиями для поста.
    """
    keyboard = [
        [InlineKeyboardButton("📄 В черновик", callback_data='save_draft')],
        [InlineKeyboardButton("🚀 Отправить на согласование", callback_data='send_for_approval')],
        [InlineKeyboardButton("✏️ Редактировать", callback_data='edit_post')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_post_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запускает процесс создания поста.
    """
    logger.info("Начало создания поста.")
    context.user_data['current_step'] = 0  # Инициализация текущего шага
    await prompt_step(update, context)
    return POST_CREATION

async def prompt_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пользователю сообщение с запросом на текущем шаге.
    """
    step_index = context.user_data['current_step']
    if step_index < len(POST_STEPS):
        step = POST_STEPS[step_index]
        await update.effective_message.reply_text(
            step['prompt'],
            reply_markup=get_skip_keyboard()
        )
        logger.info(f"Переход к шагу {step_index + 1}: {step['key']}.")
    else:
        await review_post(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает сообщения пользователя на каждом шаге создания поста.
    """
    step_index = context.user_data.get('current_step', 0)
    if step_index >= len(POST_STEPS):
        return ConversationHandler.END

    step = POST_STEPS[step_index]
    text = update.effective_message.text

    logger.info(f"Получено сообщение для шага '{step['key']}': {text}")

    if step['optional'] and text.lower() == 'пропустить':
        if step['key'] == 'image':
            context.user_data['image'] = None
            logger.info("Пользователь пропустил добавление изображения.")
        else:
            context.user_data[step['key']] = 'Не указано'
            logger.info(f"Пользователь пропустил поле '{step['key']}'.")
    else:
        if step['validator'] and not step['validator'](text):
            await update.effective_message.reply_text(
                "Некорректный формат. Пожалуйста, используйте правильный формат или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard()
            )
            logger.warning(f"Некорректный ввод для поля '{step['key']}'.")
            return POST_CREATION

        if step['key'] == 'image':
            if update.effective_message.photo:
                context.user_data['image'] = update.effective_message.photo[-1].file_id
                await update.effective_message.reply_text("Картинка добавлена.", reply_markup=get_post_actions_keyboard())
                logger.info("Пользователь добавил изображение.")
                context.user_data['current_step'] += 1
                await review_post(update, context)
                return ConversationHandler.END
            else:
                await update.effective_message.reply_text(
                    "Пожалуйста, отправьте изображение или нажмите 'Пропустить'.",
                    reply_markup=get_skip_keyboard()
                )
                logger.warning("Пользователь не отправил изображение.")
                return POST_CREATION
        else:
            formatter = step['formatter'] if step['formatter'] else (lambda x: x)
            context.user_data[step['key']] = formatter(text)
            logger.info(f"Пользователь ввел '{step['key']}': {context.user_data[step['key']]}")

    context.user_data['current_step'] += 1
    await prompt_step(update, context)
    return POST_CREATION

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает CallbackQuery от кнопок действий поста.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    logger.info(f"Получен CallbackQuery: {data}")

    if data == 'skip':
        await handle_message(update, context)
        return POST_CREATION
    elif data in ['save_draft', 'send_for_approval', 'edit_post']:
        if data == 'save_draft':
            await save_draft(update, context)
        elif data == 'send_for_approval':
            await send_for_approval(update, context)
        elif data == 'edit_post':
            await edit_post(update, context)
        return ConversationHandler.END
    else:
        logger.warning(f"Неизвестный CallbackQuery: {data}")
        return POST_CREATION

async def review_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пользователю обзор созданного поста с возможностью редактирования или отправки.
    """
    logger.info("Переход к обзору поста.")
    post = context.user_data
    review_text = "📋 **Обзор поста:**\n\n"
    for step in POST_STEPS:
        key = step['key']
        if key == 'image':
            value = 'Добавлено' if post.get('image') else 'Не добавлено'
        else:
            value = post.get(key, 'Не указано')
        review_text += f"• *{key.capitalize()}*: {value}\n"

    review_text += "\nВыберите действие:"

    await update.effective_message.reply_text(
        review_text,
        parse_mode='MarkdownV2',
        reply_markup=get_post_actions_keyboard()
    )

async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Сохраняет пост в черновики.
    """
    logger.info("Сохранение поста в черновики.")
    session: Session = SessionLocal()
    try:
        draft = Draft(
            user_id=update.effective_user.id,
            title=context.user_data.get('title'),
            date=context.user_data.get('date'),
            time_start=context.user_data.get('time_start'),
            time_end=context.user_data.get('time_end'),
            place=context.user_data.get('place'),
            text=context.user_data.get('text'),
            contact=context.user_data.get('contact'),
            place_url=context.user_data.get('place_url'),
            image=context.user_data.get('image'),
        )
        session.add(draft)
        session.commit()
        await update.effective_message.reply_text("Пост сохранен в черновики.", reply_markup=ReplyKeyboardRemove())
        logger.info("Пост успешно сохранен в черновики.")
    except Exception as e:
        session.rollback()
        await update.effective_message.reply_text("Произошла ошибка при сохранении черновика.", reply_markup=ReplyKeyboardRemove())
        logger.error(f"Ошибка при сохранении черновика: {e}")
    finally:
        session.close()

async def send_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пост на согласование.
    """
    logger.info("Отправка поста на согласование.")
    session: Session = SessionLocal()
    try:
        # Создание объекта Draft
        draft = Draft(
            user_id=update.effective_user.id,
            title=context.user_data.get('title'),
            date=context.user_data.get('date'),
            time_start=context.user_data.get('time_start'),
            time_end=context.user_data.get('time_end'),
            place=context.user_data.get('place'),
            text=context.user_data.get('text'),
            contact=context.user_data.get('contact'),
            place_url=context.user_data.get('place_url'),
            image=context.user_data.get('image'),
            approved=False
        )
        session.add(draft)
        session.commit()
        logger.info("Пост сохранен и отправлен на согласование.")

        # Отправка сообщения в чат согласования
        review_text = "📋 **Новый пост для согласования:**\n\n"
        for step in POST_STEPS:
            key = step['key']
            if key == 'image':
                value = 'Добавлено' if draft.image else 'Не добавлено'
            else:
                value = draft.__dict__.get(key, 'Не указано')
            review_text += f"• *{key.capitalize()}*: {value}\n"

        await context.bot.send_message(
            chat_id=REVIEW_CHAT_ID,
            text=review_text,
            parse_mode='MarkdownV2'
        )

        await update.effective_message.reply_text("Пост отправлен на согласование.", reply_markup=ReplyKeyboardRemove())
    except Exception as e:
        session.rollback()
        await update.effective_message.reply_text("Произошла ошибка при отправке поста на согласование.", reply_markup=ReplyKeyboardRemove())
        logger.error(f"Ошибка при отправке поста на согласование: {e}")
    finally:
        session.close()

async def edit_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Позволяет пользователю выбрать поле для редактирования.
    """
    logger.info("Пользователь выбрал редактирование поста.")
    keyboard = [
        [InlineKeyboardButton("Заголовок", callback_data='edit_title')],
        [InlineKeyboardButton("Дата", callback_data='edit_date')],
        [InlineKeyboardButton("Время начала", callback_data='edit_time_start')],
        [InlineKeyboardButton("Время окончания", callback_data='edit_time_end')],
        [InlineKeyboardButton("Место", callback_data='edit_place')],
        [InlineKeyboardButton("Текст", callback_data='edit_text')],
        [InlineKeyboardButton("Контакты", callback_data='edit_contact')],
        [InlineKeyboardButton("URL места", callback_data='edit_place_url')],
        [InlineKeyboardButton("Изображение", callback_data='edit_image')],
        [InlineKeyboardButton("Отмена", callback_data='cancel_edit')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.effective_message.reply_text("Выберите поле для редактирования:", reply_markup=reply_markup)
    return EDIT_FIELD

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает выбор поля для редактирования.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'cancel_edit':
        await query.edit_message_text("Редактирование отменено.", reply_markup=ReplyKeyboardRemove())
        logger.info("Редактирование отменено пользователем.")
        return ConversationHandler.END

    field_to_edit = data.replace('edit_', '')
    context.user_data['edit_field'] = field_to_edit
    step = next((step for step in POST_STEPS if step['key'] == field_to_edit), None)

    if step:
        if step['key'] == 'image':
            await query.edit_message_text(
                "Отправьте новое изображение или нажмите 'Пропустить':",
                reply_markup=get_skip_keyboard()
            )
        else:
            prompt_text = f"Введите новое значение для '{field_to_edit}' или нажмите 'Пропустить':"
            await query.edit_message_text(
                prompt_text,
                reply_markup=get_skip_keyboard()
            )
        logger.info(f"Пользователь выбрал редактировать поле '{field_to_edit}'.")
        return EDIT_FIELD
    else:
        await query.edit_message_text("Неизвестное поле для редактирования.", reply_markup=ReplyKeyboardRemove())
        logger.warning(f"Пользователь выбрал неизвестное поле для редактирования: {field_to_edit}")
        return ConversationHandler.END

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает ввод пользователя при редактировании поля.
    """
    field = context.user_data.get('edit_field')
    text = update.effective_message.text

    logger.info(f"Пользователь редактирует поле '{field}' с вводом: {text}")

    if text.lower() == 'пропустить':
        if field == 'image':
            context.user_data['image'] = None
            await update.effective_message.reply_text("Картинка не добавлена.", reply_markup=get_post_actions_keyboard())
            logger.info("Пользователь пропустил добавление изображения при редактировании.")
        else:
            context.user_data[field] = 'Не указано'
            await update.effective_message.reply_text(f"Поле '{field}' обновлено.", reply_markup=get_post_actions_keyboard())
            logger.info(f"Пользователь пропустил обновление поля '{field}'.")
        return ConversationHandler.END

    # Валидация и форматирование
    validators = {
        'date': validate_date,
        'time_start': validate_time,
        'time_end': validate_time,
        'place_url': validate_url
    }

    if field in validators and not validators[field](text):
        await update.effective_message.reply_text(
            "Некорректный формат. Пожалуйста, введите корректные данные или нажмите 'Пропустить'.",
            reply_markup=get_skip_keyboard()
        )
        logger.warning(f"Некорректный ввод при редактировании поля '{field}': {text}")
        return EDIT_FIELD

    if field == 'image':
        if update.effective_message.photo:
            context.user_data['image'] = update.effective_message.photo[-1].file_id
            await update.effective_message.reply_text("Картинка обновлена.", reply_markup=get_post_actions_keyboard())
            logger.info("Пользователь обновил изображение.")
        else:
            await update.effective_message.reply_text(
                "Пожалуйста, отправьте изображение или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard()
            )
            logger.warning("Пользователь не отправил изображение при редактировании.")
            return EDIT_FIELD
    else:
        formatter = {
            'title': format_text,
            'date': format_text,
            'time_start': format_text,
            'time_end': format_text,
            'place': format_text,
            'text': format_text,
            'contact': format_text,
            'place_url': format_text
        }.get(field, lambda x: x)
        context.user_data[field] = formatter(text)
        await update.effective_message.reply_text(f"Поле '{field}' обновлено.", reply_markup=get_post_actions_keyboard())
        logger.info(f"Пользователь обновил поле '{field}'.")

    return ConversationHandler.END

async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Отменяет процесс создания поста.
    """
    await update.effective_message.reply_text("Создание поста отменено.", reply_markup=ReplyKeyboardRemove())
    logger.info("Пользователь отменил создание поста.")
    return ConversationHandler.END

def post_creation_handlers() -> list:
    """
    Возвращает список обработчиков для создания поста.
    """
    return [
        ConversationHandler(
            entry_points=[CommandHandler('create_post', start_post_creation)],
            states={
                POST_CREATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                    CallbackQueryHandler(handle_callback_query, pattern='^(skip|save_draft|send_for_approval|edit_post)$'),
                ],
                EDIT_FIELD: [
                    CallbackQueryHandler(handle_edit, pattern='^edit_.*$|^cancel_edit$'),
                    MessageHandler(filters.PHOTO, process_edit),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit),
                ],
            },
            fallbacks=[CommandHandler('cancel', cancel_creation)],
            allow_reentry=True,
        )
    ]
