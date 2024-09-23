# handlers/post_creation.py

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove
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

# Определяем состояния для ConversationHandler
POST_CREATION = range(9)

# Определяем шаги создания поста
POST_STEPS = [
    {
        'key': 'title',
        'prompt': "Введите заголовок поста или нажмите 'Пропустить':",
        'validator': None,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'date',
        'prompt': "Введите дату (например, 25.12.2023) или нажмите 'Пропустить':",
        'validator': validate_date,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'time_start',
        'prompt': "Введите время начала (например, 18:30) или нажмите 'Пропустить':",
        'validator': validate_time,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'time_end',
        'prompt': "Введите время конца (например, 20:30) или нажмите 'Пропустить':",
        'validator': validate_time,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'place_name',
        'prompt': "Введите название места или нажмите 'Пропустить':",
        'validator': None,
        'formatter': format_text,
        'optional': True,
    },
    {
        'key': 'place_url',
        'prompt': "Введите ссылку на место (например, Google Maps URL) или нажмите 'Пропустить':",
        'validator': validate_url,
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
        'key': 'image',
        'prompt': "Отправьте картинку для поста или нажмите 'Пропустить':",
        'validator': None,
        'formatter': None,
        'optional': True,
    },
]

def get_skip_keyboard():
    keyboard = [
        [InlineKeyboardButton("Пропустить", callback_data='skip')]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_post_actions_keyboard():
    keyboard = [
        [InlineKeyboardButton("📄 В черновик", callback_data='save_draft')],
        [InlineKeyboardButton("🚀 Отправить на согласование", callback_data='send_for_approval')],
        [InlineKeyboardButton("✏️ Редактировать", callback_data='edit_post')]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start_post_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['current_step'] = 0
    await prompt_step(update, context)
    return POST_CREATION

async def prompt_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    step_index = context.user_data['current_step']
    if step_index < len(POST_STEPS):
        step = POST_STEPS[step_index]
        await update.message.reply_text(
            step['prompt'],
            reply_markup=get_skip_keyboard()
        )
    else:
        await review_post(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    step_index = context.user_data.get('current_step', 0)
    if step_index >= len(POST_STEPS):
        return POST_CREATION

    step = POST_STEPS[step_index]
    text = update.message.text

    if text.lower() == 'пропустить' and step['optional']:
        context.user_data[step['key']] = 'Не указано' if step['key'] != 'image' else None
    else:
        if step['validator'] and not step['validator'](text):
            await update.message.reply_text(
                "Некорректный формат. Пожалуйста, используйте правильный формат или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard()
            )
            return POST_CREATION
        context.user_data[step['key']] = step['formatter'](text) if step['formatter'] else text

    context.user_data['current_step'] += 1
    await prompt_step(update, context)
    return POST_CREATION

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    data = query.data

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
        return POST_CREATION

async def review_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    post_data = context.user_data
    post = (
        f"📢 *{post_data.get('title')}*\n\n"
        f"📅 *Дата*: {post_data.get('date')}\n"
        f"⏰ *Время*: {post_data.get('time_start')} - {post_data.get('time_end')}\n"
        f"📍 *Место*: [{post_data.get('place_name')}]({post_data.get('place_url')})\n\n"
        f"{post_data.get('text')}\n\n"
        f"📞 *Контакт*: {post_data.get('contact')}"
    )
    
    if post_data.get('image'):
        await update.message.reply_photo(
            photo=post_data['image'],
            caption=post,
            parse_mode='MarkdownV2',
            reply_markup=get_post_actions_keyboard()
        )
    else:
        await update.message.reply_text(
            post,
            parse_mode='MarkdownV2',
            reply_markup=get_post_actions_keyboard()
        )
    
    return POST_CREATION

async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    session: Session = context.bot_data['db_session']
    
    draft = Draft(
        user_id=update.effective_user.id,
        title=context.user_data.get('title'),
        date=context.user_data.get('date'),
        time_start=context.user_data.get('time_start'),
        time_end=context.user_data.get('time_end'),
        place_name=context.user_data.get('place_name'),
        place_url=context.user_data.get('place_url'),
        text=context.user_data.get('text'),
        contact=context.user_data.get('contact'),
        image=context.user_data.get('image')
    )
    session.add(draft)
    session.commit()
    
    await query.edit_message_caption(
        caption="Пост сохранён в черновики.",
        parse_mode='MarkdownV2',
        reply_markup=None
    )
    await query.message.reply_text(
        "Пост сохранён в черновики.",
        reply_markup=ReplyKeyboardMarkup([['Главное меню']], resize_keyboard=True)
    )
    context.user_data.clear()
    return ConversationHandler.END

async def send_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    session: Session = context.bot_data['db_session']
    
    post_data = context.user_data
    post = (
        f"📢 *{post_data.get('title')}*\n\n"
        f"📅 *Дата*: {post_data.get('date')}\n"
        f"⏰ *Время*: {post_data.get('time_start')} - {post_data.get('time_end')}\n"
        f"📍 *Место*: [{post_data.get('place_name')}]({post_data.get('place_url')})\n\n"
        f"{post_data.get('text')}\n\n"
        f"📞 *Контакт*: {post_data.get('contact')}"
    )
    
    # Отправка в общий чат для согласования
    if post_data.get('image'):
        sent_message = await context.bot.send_photo(
            chat_id=REVIEW_CHAT_ID,
            photo=post_data['image'],
            caption=post,
            parse_mode='MarkdownV2'
        )
    else:
        sent_message = await context.bot.send_message(
            chat_id=REVIEW_CHAT_ID,
            text=post,
            parse_mode='MarkdownV2'
        )
    
    # Добавление кнопки выбора ответственного лица
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
    
    await query.edit_message_caption(
        caption="Пост отправлен на согласование.",
        parse_mode='MarkdownV2',
        reply_markup=None
    )
    await query.message.reply_text(
        "Пост отправлен на согласование.",
        reply_markup=ReplyKeyboardMarkup([['Главное меню']], resize_keyboard=True)
    )
    context.user_data.clear()
    return ConversationHandler.END

async def edit_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("Заголовок", callback_data='edit_title')],
        [InlineKeyboardButton("Дата", callback_data='edit_date')],
        [InlineKeyboardButton("Время начала", callback_data='edit_time_start')],
        [InlineKeyboardButton("Время конца", callback_data='edit_time_end')],
        [InlineKeyboardButton("Место", callback_data='edit_place')],
        [InlineKeyboardButton("Текст", callback_data='edit_text')],
        [InlineKeyboardButton("Контакт", callback_data='edit_contact')],
        [InlineKeyboardButton("Картинка", callback_data='edit_image')],
        [InlineKeyboardButton("Отмена", callback_data='cancel_edit')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_caption(
        caption="Редактирование поста. Выберите поле для изменения:",
        parse_mode='MarkdownV2',
        reply_markup=reply_markup
    )
    return POST_CREATION

async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action.startswith('edit_'):
        field = action.split('_')[1]
        context.user_data['edit_field'] = field
        prompts = {
            'title': "Введите новый заголовок или нажмите 'Пропустить':",
            'date': "Введите новую дату (например, 25.12.2023) или нажмите 'Пропустить':",
            'time_start': "Введите новое время начала (например, 18:30) или нажмите 'Пропустить':",
            'time_end': "Введите новое время конца (например, 20:30) или нажмите 'Пропустить':",
            'place': "Введите новое название места или нажмите 'Пропустить':",
            'text': "Введите новый текст поста или нажмите 'Пропустить':",
            'contact': "Введите новую контактную информацию или нажмите 'Пропустить':",
            'image': "Отправьте новую картинку или нажмите 'Пропустить':"
        }
        prompt = prompts.get(field, "Введите новое значение или нажмите 'Пропустить':")
        await query.edit_message_caption(
            caption=prompt,
            parse_mode='MarkdownV2',
            reply_markup=get_skip_keyboard()
        )
        return POST_CREATION
    elif action == 'cancel_edit':
        await query.edit_message_caption(
            caption="Редактирование отменено.",
            parse_mode='MarkdownV2',
            reply_markup=get_post_actions_keyboard()
        )
        return POST_CREATION
    else:
        await query.edit_message_caption(
            caption="Неизвестное действие.",
            parse_mode='MarkdownV2',
            reply_markup=get_post_actions_keyboard()
        )
        return POST_CREATION

async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    field = context.user_data.get('edit_field')
    text = update.message.text

    if text.lower() == 'пропустить':
        if field == 'image':
            context.user_data['image'] = None
            await update.message.reply_text("Картинка не добавлена.", reply_markup=get_post_actions_keyboard())
        else:
            context.user_data[field] = 'Не указано'
            await update.message.reply_text("Поле обновлено.", reply_markup=get_post_actions_keyboard())
        return POST_CREATION

    # Валидация и форматирование
    validators = {
        'date': validate_date,
        'time_start': validate_time,
        'time_end': validate_time,
        'place_url': validate_url
    }

    if field in validators and not validators[field](text):
        await update.message.reply_text(
            "Некорректный формат. Пожалуйста, введите корректные данные или нажмите 'Пропустить'.",
            reply_markup=get_skip_keyboard()
        )
        return POST_CREATION

    if field == 'image':
        if update.message.photo:
            context.user_data['image'] = update.message.photo[-1].file_id
            await update.message.reply_text("Картинка обновлена.", reply_markup=get_post_actions_keyboard())
        else:
            await update.message.reply_text("Пожалуйста, отправьте изображение или нажмите 'Пропустить'.", reply_markup=get_skip_keyboard())
            return POST_CREATION
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
        await update.message.reply_text("Поле обновлено.", reply_markup=get_post_actions_keyboard())
    
    return POST_CREATION

def post_creation_handlers() -> list:
    """
    Возвращает список обработчиков для процесса создания поста.
    """
    return [
        ConversationHandler(
            entry_points=[CommandHandler('create_post', start_post_creation)],
            states={
                POST_CREATION: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                    CallbackQueryHandler(handle_callback_query, pattern='^(skip|save_draft|send_for_approval|edit_post)$'),
                    CallbackQueryHandler(handle_edit, pattern='^edit_.*$'),
                    MessageHandler(filters.PHOTO, process_edit),  # Обработка фото для 'image'
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit),  # Обработка текстовых сообщений для редактирования
                ],
            },
            fallbacks=[CommandHandler('cancel', cancel_creation)],
            allow_reentry=True,
        )
    ]

async def cancel_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Создание поста отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END
