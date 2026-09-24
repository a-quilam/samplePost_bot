import logging
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
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
from utils.formatter import format_text, escape_markdown
from models import Draft, ResponsiblePerson
from sqlalchemy.orm import Session
from config import REVIEW_CHAT_ID
from database import SessionLocal
from approval import (
    STATUS_DECLINED,
    draft_to_post_data,
    edit_block_reason,
    get_approval,
    get_draft_photos,
    photos_to_json,
    reset_declined,
)

# Настройка логирования
logger = logging.getLogger(__name__)

# Определение состояний
POST_CREATION, EDIT_FIELD = range(2)

# Единый заголовок поста для ПРЕВЬЮ и ПУБЛИКАЦИИ: текст, который видит автор
# в превью, дословно совпадает с тем, что уйдёт в чат публикации.
POST_HEADING = "📢 *Новый пост:*"

# Поля черновика (совпадают с колонками модели Draft, кроме служебных)
DRAFT_FIELD_KEYS = [
    'title', 'date', 'time_start', 'time_end', 'place_name',
    'place_url', 'text', 'contact', 'image',
]

# Определение шагов создания поста
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
        'key': 'place_name',
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
        'validator': None,
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

def get_media_done_keyboard():
    """Кнопка завершения сбора фотографий медиа-группы."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Фото готово", callback_data='media_done')]]
    )

def _clear_post_data(user_data: dict) -> None:
    """Полностью сбрасывает данные поста (новое создание — без «наследства»)."""
    for key in DRAFT_FIELD_KEYS + ['photos', 'pending_media', 'editing_draft_id', 'edit_field']:
        user_data.pop(key, None)

def _current_photos(user_data: dict) -> list:
    """Фотографии текущего поста из user_data (fallback на одиночную image)."""
    photos = user_data.get('photos') or []
    if not photos and user_data.get('image'):
        photos = [user_data['image']]
    return list(photos)

def _post_fields(user_data: dict) -> dict:
    """
    Поля поста из user_data в формате колонок Draft.
    photos (JSON-список) — основной источник; image дублирует ПЕРВУЮ
    фотографию для совместимости со старым кодом и старыми черновиками.
    """
    photos = _current_photos(user_data)
    fields = {key: user_data.get(key) for key in DRAFT_FIELD_KEYS}
    fields['image'] = photos[0] if photos else None
    fields['photos'] = photos_to_json(photos)
    return fields

def _apply_fields(draft: Draft, user_data: dict) -> None:
    """Заполняет/обновляет поля черновика из user_data (без создания копии)."""
    for key, value in _post_fields(user_data).items():
        setattr(draft, key, value)

def _find_own_draft(session: Session, draft_id, user_id: int):
    """Черновик текущего пользователя по id (None, если id не задан/чужой/удалён)."""
    if not draft_id:
        return None
    return session.query(Draft).filter(Draft.id == draft_id, Draft.user_id == user_id).first()

async def start_post_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Запускает процесс создания поста.
    """
    logger.info("Начало создания поста.")
    _clear_post_data(context.user_data)  # новый пост не должен наследовать старые данные
    context.user_data['current_step'] = 0  # Инициализация текущего шага
    await prompt_step(update, context)
    return POST_CREATION

async def start_edit_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Открывает существующий черновик в редакторе (callback editdraft_<id>).

    Точка входа ConversationHandler: черновик загружается в user_data,
    показывается превью — дальше работают те же кнопки «В черновик»
    (обновление БЕЗ создания копии) и «Отправить на согласование».
    """
    query = update.callback_query
    await query.answer()

    try:
        draft_id = int(query.data[len('editdraft_'):])
    except (TypeError, ValueError):
        return ConversationHandler.END

    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(session, draft_id, query.from_user.id)
        if draft is None:
            await update.effective_message.reply_text("Черновик не найден.")
            return ConversationHandler.END

        block = edit_block_reason(session, draft_id)
        if block is not None:
            reasons = {
                'assigned': 'на согласовании',
                'approved': 'согласован и ожидает публикации',
                'published': 'уже опубликован',
            }
            await update.effective_message.reply_text(
                f"Редактирование запрещено: пост {reasons.get(block, block)}."
            )
            return ConversationHandler.END

        _clear_post_data(context.user_data)
        for key in DRAFT_FIELD_KEYS:
            context.user_data[key] = getattr(draft, key)
        photos = get_draft_photos(draft)
        context.user_data['photos'] = photos
        context.user_data['image'] = photos[0] if photos else None
        context.user_data['editing_draft_id'] = draft.id
        context.user_data['current_step'] = len(POST_STEPS)

        logger.info(f"Черновик #{draft.id} открыт на редактирование пользователем {query.from_user.id}.")
        await review_post(update, context)
        return POST_CREATION
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка открытия черновика #{draft_id}: {e}")
        await update.effective_message.reply_text("Не удалось открыть черновик.")
        return ConversationHandler.END
    finally:
        session.close()

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
            context.user_data['photos'] = []
            context.user_data.pop('pending_media', None)
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
                file_id = update.effective_message.photo[-1].file_id
                context.user_data['photos'] = [file_id]
                context.user_data['image'] = file_id
                context.user_data.pop('pending_media', None)
                await update.effective_message.reply_text("Картинка добавлена.")
                logger.info("Пользователь добавил изображение.")
                context.user_data['current_step'] += 1
                await review_post(update, context)
                # Остаёмся в POST_CREATION, чтобы кнопки действий после обзора работали
                return POST_CREATION
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

async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает нажатие inline-кнопки «Пропустить»: пропускает текущий шаг.
    """
    step_index = context.user_data.get('current_step', 0)
    if step_index >= len(POST_STEPS):
        await review_post(update, context)
        return POST_CREATION

    step = POST_STEPS[step_index]
    if step['key'] == 'image':
        context.user_data['image'] = None
        context.user_data['photos'] = []
        context.user_data.pop('pending_media', None)
        logger.info("Пользователь пропустил добавление изображения.")
    else:
        context.user_data[step['key']] = 'Не указано'
        logger.info(f"Пользователь пропустил поле '{step['key']}'.")

    context.user_data['current_step'] += 1
    await prompt_step(update, context)
    return POST_CREATION

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Приём фотографий на шаге изображения.

    Одиночная фотография — сразу к превью (прежнее поведение).
    Медиа-группа (альбом) приходит отдельными сообщениями с одним
    media_group_id: первое показывает кнопку «✅ Фото готово», остальные
    молча добавляются в список. Шаг завершается кнопкой — без таймеров
    и гонок с кнопками «В черновик»/«Отправить».
    """
    message = update.effective_message
    if not message.photo:
        return POST_CREATION

    user_data = context.user_data
    file_id = message.photo[-1].file_id
    media_group_id = message.media_group_id
    step_index = user_data.get('current_step', 0)
    at_image_step = (
        step_index < len(POST_STEPS) and POST_STEPS[step_index]['key'] == 'image'
    )

    # Продолжение уже начатой медиа-группы (в т.ч. запоздавшие фотографии) —
    # добавляем молча, не спамим подтверждениями на каждое фото.
    if media_group_id and user_data.get('pending_media') == media_group_id:
        user_data.setdefault('photos', []).append(file_id)
        return POST_CREATION

    # Первая фотография нового альбома — начинаем сбор
    if media_group_id and at_image_step:
        user_data['pending_media'] = media_group_id
        user_data['photos'] = [file_id]
        user_data['image'] = file_id
        await message.reply_text(
            "📷 Получаю фотографии альбома. Отправьте остальные, затем нажмите «✅ Фото готово».",
            reply_markup=get_media_done_keyboard(),
        )
        logger.info(f"Начат сбор медиа-группы {media_group_id}.")
        return POST_CREATION

    # Одиночная фотография — как раньше: сразу к превью
    if at_image_step:
        user_data['photos'] = [file_id]
        user_data['image'] = file_id
        user_data.pop('pending_media', None)
        user_data['current_step'] += 1
        await message.reply_text("Картинка добавлена.")
        await review_post(update, context)
        return POST_CREATION

    # Фото вне шага изображения: повторяем текущий запрос (или ничего —
    # на экране превью изменение фото делается через «Редактировать»).
    if step_index < len(POST_STEPS):
        await message.reply_text(
            POST_STEPS[step_index]['prompt'],
            reply_markup=get_skip_keyboard(),
        )
    return POST_CREATION

async def _finish_media_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Общая логика кнопки «✅ Фото готово»: фиксирует собранные фотографии.
    Возвращает True, если фотографии есть и шаг можно завершить.
    """
    photos = context.user_data.get('photos') or []
    if not photos:
        await update.effective_message.reply_text(
            "Фотографии не получены. Отправьте фото (альбом) или нажмите «Пропустить».",
            reply_markup=get_skip_keyboard(),
        )
        return False
    context.user_data['image'] = photos[0]
    return True

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает CallbackQuery от кнопок действий поста.
    """
    query = update.callback_query
    await query.answer()
    data = query.data

    logger.info(f"Получен CallbackQuery: {data}")

    if data == 'skip':
        return await handle_skip(update, context)
    elif data == 'media_done':
        step_index = context.user_data.get('current_step', 0)
        if step_index < len(POST_STEPS) and POST_STEPS[step_index]['key'] == 'image':
            if not await _finish_media_done(update, context):
                return POST_CREATION
            context.user_data['current_step'] += 1
            await update.effective_message.reply_text("Фотографии добавлены.")
        await review_post(update, context)
        return POST_CREATION
    elif data == 'save_draft':
        await save_draft(update, context)
        return ConversationHandler.END
    elif data == 'send_for_approval':
        await send_for_approval(update, context)
        return ConversationHandler.END
    elif data == 'edit_post':
        # Важно: возвращаем EDIT_FIELD, иначе состояние редактирования не наступит
        return await edit_post(update, context)
    else:
        logger.warning(f"Неизвестный CallbackQuery: {data}")
        return POST_CREATION

def _field_label(key: str) -> str:
    """
    Человекочитаемая метка поля для MarkdownV2-сообщения.
    Заменяем '_' на пробел: символ '_' обязан экранироваться в MarkdownV2.
    """
    return key.replace('_', ' ').capitalize()

def build_post_summary(post_data: dict, *, heading: str) -> str:
    """
    Собирает итоговое MarkdownV2-сообщение о посте.
    Наша разметка (*жирный*) остаётся как есть, значения пользователя экранируются.
    """
    lines = [heading, ""]
    for step in POST_STEPS:
        key = step['key']
        if key == 'image':
            value = 'Добавлено' if post_data.get('image') else 'Не добавлено'
        else:
            value = escape_markdown(str(post_data.get(key) or 'Не указано'))
        lines.append(f"• *{_field_label(key)}*: {value}")
    lines.append("")
    return "\n".join(lines)

async def send_post(
    bot,
    chat_id,
    post_data: dict,
    photos: list,
    *,
    heading: str,
    extra_lines: str = '',
    reply_markup: InlineKeyboardMarkup = None,
    markup_lead_text: str = None,
) -> None:
    """
    ЕДИНАЯ отправка поста: текст / фото с подписью / медиа-группа.

    Используется в превью, чате согласования, уведомлении ответственного
    и публикации — формат сообщения везде одинаковый, без дублирования
    логики форматирования и без повторного экранирования.
    """
    text = build_post_summary(post_data, heading=heading)
    if extra_lines:
        text += extra_lines

    file_ids = [p for p in (photos or []) if p]

    if not file_ids:
        await bot.send_message(
            chat_id=chat_id, text=text, parse_mode='MarkdownV2', reply_markup=reply_markup
        )
        return

    if len(file_ids) == 1:
        # Лимит подписи к фото — 1024 символа: при переполнении отправляем раздельно
        if len(text) <= 1000:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_ids[0],
                caption=text,
                parse_mode='MarkdownV2',
                reply_markup=reply_markup,
            )
        else:
            await bot.send_photo(chat_id=chat_id, photo=file_ids[0])
            await bot.send_message(
                chat_id=chat_id, text=text, parse_mode='MarkdownV2', reply_markup=reply_markup
            )
        return

    # Медиа-группа: подпись разрешена только у первого снимка
    media = []
    for index, file_id in enumerate(file_ids):
        if index == 0 and len(text) <= 1000:
            media.append(InputMediaPhoto(media=file_id, caption=text, parse_mode='MarkdownV2'))
        else:
            media.append(InputMediaPhoto(media=file_id))
    await bot.send_media_group(chat_id=chat_id, media=media)

    if len(text) > 1000:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode='MarkdownV2')
    if reply_markup is not None:
        # sendMediaGroup не поддерживает кнопки — клавиатура отдельным сообщением
        await bot.send_message(
            chat_id=chat_id,
            text=markup_lead_text or "Выберите действие:",
            reply_markup=reply_markup,
        )

async def review_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Превью поста: фотографии + ровно тот же текст, что увидит чат публикации.
    Кнопки действий — к этому же сообщению (для альбома — отдельным сообщением).
    """
    logger.info("Переход к превью поста.")
    await send_post(
        context.bot,
        update.effective_chat.id,
        context.user_data,
        _current_photos(context.user_data),
        heading=POST_HEADING,
        reply_markup=get_post_actions_keyboard(),
        markup_lead_text="Выберите действие:",
    )

async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Сохраняет пост в черновики: обновляет открытый черновик (editing_draft_id)
    или создаёт новый — лишних копий не появляется.
    """
    logger.info("Сохранение поста в черновики.")
    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(
            session, context.user_data.get('editing_draft_id'), update.effective_user.id
        )
        is_update = draft is not None
        if draft is None:
            draft = Draft(user_id=update.effective_user.id)
            session.add(draft)
        _apply_fields(draft, context.user_data)
        session.commit()
        if is_update:
            await update.effective_message.reply_text(
                f"Черновик {draft.id} обновлён.", reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.effective_message.reply_text(
                "Пост сохранен в черновики.", reply_markup=ReplyKeyboardRemove()
            )
        logger.info(f"Черновик {draft.id} сохранён (обновление={is_update}).")
    except Exception as e:
        session.rollback()
        await update.effective_message.reply_text("Произошла ошибка при сохранении черновика.", reply_markup=ReplyKeyboardRemove())
        logger.error(f"Ошибка при сохранении черновика: {e}")
    finally:
        session.close()

async def send_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пост на согласование: сохраняет (или обновляет) черновик
    и создаёт цикл согласования. Повторная отправка отклонённого поста
    начинает НОВЫЙ цикл; уже согласованный/назначенный — не дублируется.
    """
    logger.info("Отправка поста на согласование.")
    
    if not REVIEW_CHAT_ID:
        await update.effective_message.reply_text(
            "Не настроен REVIEW_CHAT_ID. Пост не отправлен на согласование.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    session: Session = SessionLocal()
    try:
        # Обновляем открытый черновик либо создаём новый (без лишних копий)
        draft = _find_own_draft(
            session, context.user_data.get('editing_draft_id'), update.effective_user.id
        )
        if draft is None:
            draft = Draft(user_id=update.effective_user.id)
            session.add(draft)
        _apply_fields(draft, context.user_data)
        session.commit()
        logger.info(f"Пост сохранён (черновик {draft.id}) и отправлен на согласование.")

        # Один активный цикл согласования на пост
        approval = get_approval(session, draft.id)
        if approval is not None and approval.status != STATUS_DECLINED:
            await update.effective_message.reply_text(
                "Этот пост уже отправлен на согласование.", reply_markup=ReplyKeyboardRemove()
            )
            return
        if approval is not None:
            # Отклонён: правки внесены — начинаем новый цикл согласования
            reset_declined(session, draft.id)

        # Отправка сообщения в чат согласования (уникальная логика из callbacks.py:
        # отправка фото и клавиатура выбора ответственного).
        # Формат — общий send_post (медиа-группа поддерживается).
        # Автор поста — из сохранённой записи draft.user_id
        await send_post(
            context.bot,
            REVIEW_CHAT_ID,
            draft_to_post_data(draft),
            get_draft_photos(draft),
            heading="📋 *Новый пост для согласования:*",
            extra_lines=f"*Автор поста:* {draft.user_id}\n",
        )

        # Клавиатура выбора ответственного: в callback_data передаём id поста,
        # чтобы ответственный и данные поста определялись из БД, а не из
        # context.user_data нажавшего администратора
        responsible_persons = session.query(ResponsiblePerson).all()
        if responsible_persons:
            keyboard = [
                [InlineKeyboardButton(
                    person.name,
                    callback_data=f'responsible_{draft.id}_{person.telegram_id}'
                )]
                for person in responsible_persons
            ]
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text="Выберите ответственного за этот пост:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text="Нет ответственных лиц для назначения."
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
        [InlineKeyboardButton("Место", callback_data='edit_place_name')],
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
        # Возвращаемся к обзору поста, чтобы кнопки действий продолжали работать
        await query.edit_message_text(
            "Редактирование отменено. Выберите действие:",
            reply_markup=get_post_actions_keyboard()
        )
        logger.info("Редактирование отменено пользователем.")
        return POST_CREATION

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
    После успешного изменения показывает актуальное превью поста
    (устаревший обзор не должен висеть после правок).
    """
    field = context.user_data.get('edit_field')
    text = update.effective_message.text or ''

    logger.info(f"Пользователь редактирует поле '{field}' с вводом: {text}")

    if text.lower() == 'пропустить':
        if field == 'image':
            context.user_data['image'] = None
            context.user_data['photos'] = []
            context.user_data.pop('pending_media', None)
            logger.info("Пользователь пропустил добавление изображения при редактировании.")
        elif field:
            context.user_data[field] = 'Не указано'
            logger.info(f"Пользователь пропустил обновление поля '{field}'.")
        await review_post(update, context)
        # Возвращаемся в POST_CREATION: кнопки действий должны остаться рабочими
        return POST_CREATION

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
            file_id = update.effective_message.photo[-1].file_id
            media_group_id = update.effective_message.media_group_id

            # Продолжение начатого альбома — добавляем молча
            if media_group_id and context.user_data.get('pending_media') == media_group_id:
                context.user_data.setdefault('photos', []).append(file_id)
                return EDIT_FIELD

            # Новый альбом — собираем до нажатия «✅ Фото готово»
            if media_group_id:
                context.user_data['pending_media'] = media_group_id
                context.user_data['photos'] = [file_id]
                context.user_data['image'] = file_id
                await update.effective_message.reply_text(
                    "📷 Отправьте остальные фотографии альбома, затем нажмите «✅ Фото готово».",
                    reply_markup=get_media_done_keyboard(),
                )
                logger.info(f"Начат сбор медиа-группы при редактировании: {media_group_id}.")
                return EDIT_FIELD

            # Одиночная фотография — как раньше: сразу обновляем и показываем превью
            context.user_data['photos'] = [file_id]
            context.user_data['image'] = file_id
            context.user_data.pop('pending_media', None)
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
            'place_name': format_text,
            'text': format_text,
            'contact': format_text,
            'place_url': format_text
        }.get(field, lambda x: x)
        context.user_data[field] = formatter(text)
        logger.info(f"Пользователь обновил поле '{field}'.")

    # Остаёмся в диалоге: превью с актуальными значениями + кнопки действий
    await review_post(update, context)
    return POST_CREATION

async def handle_skip_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Кнопка «Пропустить» в режиме редактирования поля (callback 'skip'
    в состоянии EDIT_FIELD — до этого она не обрабатывалась и «проваливалась»).
    """
    query = update.callback_query
    await query.answer()
    field = context.user_data.get('edit_field')
    if field == 'image':
        context.user_data['image'] = None
        context.user_data['photos'] = []
        context.user_data.pop('pending_media', None)
    elif field:
        context.user_data[field] = 'Не указано'
    await review_post(update, context)
    return POST_CREATION

async def finish_photo_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Кнопка «✅ Фото готово» при редактировании изображения альбомом.
    """
    query = update.callback_query
    await query.answer()
    if not await _finish_media_done(update, context):
        return EDIT_FIELD
    logger.info("Пользователь обновил изображения (медиа-группа).")
    await review_post(update, context)
    return POST_CREATION

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
            entry_points=[
                CommandHandler('create_post', start_post_creation),
                # Кнопка «Создать пост» из Reply-клавиатуры главного меню.
                # Обязательна как entry_point: только ConversationHandler умеет
                # отслеживать состояние диалога.
                MessageHandler(filters.Regex('^✏️ Создать пост$'), start_post_creation),
                # Открытие существующего черновика в редакторе (кнопка в списке черновиков)
                CallbackQueryHandler(start_edit_draft, pattern=r'^editdraft_\d+$'),
            ],
            states={
                POST_CREATION: [
                    # Повторное нажатие кнопки mid-диалога начинает создание заново
                    MessageHandler(filters.Regex('^✏️ Создать пост$'), start_post_creation),
                    MessageHandler(filters.PHOTO, handle_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                    CallbackQueryHandler(handle_callback_query, pattern='^(skip|save_draft|send_for_approval|edit_post|media_done)$'),
                ],
                EDIT_FIELD: [
                    CallbackQueryHandler(handle_edit, pattern='^edit_.*$|^cancel_edit$'),
                    CallbackQueryHandler(finish_photo_edit, pattern='^media_done$'),
                    CallbackQueryHandler(handle_skip_edit, pattern='^skip$'),
                    MessageHandler(filters.PHOTO, process_edit),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit),
                ],
            },
            fallbacks=[CommandHandler('cancel', cancel_creation)],
            allow_reentry=True,
        )
    ]
