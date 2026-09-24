import logging
from typing import Any

from sqlalchemy.orm import Session
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from approval import (
    STATUS_DECLINED,
    draft_to_post_data,
    edit_block_reason,
    get_approval,
    get_draft_photos,
    photos_to_json,
    reset_declined,
)
from config import REVIEW_CHAT_ID
from database import SessionLocal
from handlers.drafts import view_drafts
from handlers.main_menu import main_menu_keyboard
from models import Draft, ResponsiblePerson
from utils import tg_context as ctx
from utils.formatter import escape_markdown, format_text
from utils.validators import validate_date, validate_time, validate_url

# Настройка логирования
logger = logging.getLogger(__name__)

# Определение состояний
POST_CREATION, EDIT_FIELD = range(2)

# Единый заголовок поста для ПРЕВЬЮ и ПУБЛИКАЦИИ: текст, который видит автор
# в превью, дословно совпадает с тем, что уйдёт в чат публикации.
POST_HEADING = "📢 *Новый пост:*"

# Поля черновика (совпадают с колонками модели Draft, кроме служебных)
DRAFT_FIELD_KEYS = [
    "title",
    "date",
    "time_start",
    "time_end",
    "place_name",
    "place_url",
    "text",
    "contact",
    "image",
]

# Определение шагов создания поста
# list[dict[str, Any]] — явная аннотация: без неё mypy сводит разнотипные
# словари к object и «step['key']» становится ошибкой типов
POST_STEPS: list[dict[str, Any]] = [
    {
        "key": "title",
        "prompt": "Введите заголовок поста или нажмите 'Пропустить':",
        "validator": None,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "date",
        "prompt": "Введите дату события (ДД.ММ.ГГГГ) или нажмите 'Пропустить':",
        "validator": validate_date,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "time_start",
        "prompt": "Введите время начала (ЧЧ:ММ) или нажмите 'Пропустить':",
        "validator": validate_time,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "time_end",
        "prompt": "Введите время окончания (ЧЧ:ММ) или нажмите 'Пропустить':",
        "validator": validate_time,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "place_name",
        "prompt": "Введите место проведения или нажмите 'Пропустить':",
        "validator": None,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "text",
        "prompt": "Введите текст поста или нажмите 'Пропустить':",
        "validator": None,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "contact",
        "prompt": "Введите контактную информацию или нажмите 'Пропустить':",
        "validator": None,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "place_url",
        "prompt": "Введите URL места проведения или нажмите 'Пропустить':",
        "validator": validate_url,
        "formatter": format_text,
        "optional": True,
    },
    {
        "key": "image",
        "prompt": "Отправьте изображение или нажмите 'Пропустить':",
        "validator": None,
        "formatter": None,
        "optional": True,
    },
]


def get_skip_keyboard():
    """
    Возвращает клавиатуру с кнопкой "Пропустить".
    """
    keyboard = [[InlineKeyboardButton("Пропустить", callback_data="skip")]]
    return InlineKeyboardMarkup(keyboard)


def get_post_actions_keyboard():
    """
    Возвращает клавиатуру с действиями для поста.
    """
    keyboard = [
        [InlineKeyboardButton("📄 В черновик", callback_data="save_draft")],
        [
            InlineKeyboardButton(
                "🚀 Отправить на согласование", callback_data="send_for_approval"
            )
        ],
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_post")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_media_done_keyboard():
    """Кнопка завершения сбора фотографий медиа-группы."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Фото готово", callback_data="media_done")]]
    )


def _clear_post_data(user_data: dict) -> None:
    """Полностью сбрасывает данные поста (новое создание — без «наследства»)."""
    for key in DRAFT_FIELD_KEYS + [
        "photos",
        "pending_media",
        "last_rejected_media",
        "editing_draft_id",
        "edit_field",
    ]:
        user_data.pop(key, None)


def _current_photos(user_data: dict) -> list:
    """Фотографии текущего поста из user_data (fallback на одиночную image)."""
    photos = user_data.get("photos") or []
    if not photos and user_data.get("image"):
        photos = [user_data["image"]]
    return list(photos)


def _post_fields(user_data: dict) -> dict:
    """
    Поля поста из user_data в формате колонок Draft.
    photos (JSON-список) — основной источник; image дублирует ПЕРВУЮ
    фотографию для совместимости со старым кодом и старыми черновиками.
    """
    photos = _current_photos(user_data)
    fields = {key: user_data.get(key) for key in DRAFT_FIELD_KEYS}
    fields["image"] = photos[0] if photos else None
    fields["photos"] = photos_to_json(photos)
    return fields


def _apply_fields(draft: Draft, user_data: dict) -> None:
    """Заполняет/обновляет поля черновика из user_data (без создания копии)."""
    for key, value in _post_fields(user_data).items():
        setattr(draft, key, value)


def _find_own_draft(session: Session, draft_id, user_id: int):
    """Черновик текущего пользователя по id (None, если id не задан/чужой/удалён)."""
    if not draft_id:
        return None
    return (
        session.query(Draft)
        .filter(Draft.id == draft_id, Draft.user_id == user_id)
        .first()
    )


async def start_post_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Запускает процесс создания поста.
    """
    logger.info("Начало создания поста.")
    _clear_post_data(
        ctx.data(context)
    )  # новый пост не должен наследовать старые данные
    ctx.data(context)["current_step"] = 0  # Инициализация текущего шага
    await prompt_step(update, context)
    return POST_CREATION


async def start_edit_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Открывает существующий черновик в редакторе (callback editdraft_<id>).

    Точка входа ConversationHandler: черновик загружается в user_data,
    показывается превью — дальше работают те же кнопки «В черновик»
    (обновление БЕЗ создания копии) и «Отправить на согласование».
    """
    query = ctx.query(update)
    await query.answer()

    try:
        draft_id = int((query.data or "")[len("editdraft_") :])
    except (TypeError, ValueError):
        return ConversationHandler.END

    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(session, draft_id, query.from_user.id)
        if draft is None:
            await ctx.message(update).reply_text("Черновик не найден.")
            return ConversationHandler.END

        block = edit_block_reason(session, draft_id)
        if block is not None:
            reasons = {
                "assigned": "на согласовании",
                "approved": "согласован и ожидает публикации",
                "published": "уже опубликован",
            }
            await ctx.message(update).reply_text(
                f"Редактирование запрещено: пост {reasons.get(block, block)}."
            )
            return ConversationHandler.END

        _clear_post_data(ctx.data(context))
        for key in DRAFT_FIELD_KEYS:
            ctx.data(context)[key] = getattr(draft, key)
        photos = get_draft_photos(draft)
        ctx.data(context)["photos"] = photos
        ctx.data(context)["image"] = photos[0] if photos else None
        ctx.data(context)["editing_draft_id"] = draft.id
        ctx.data(context)["current_step"] = len(POST_STEPS)

        logger.info(
            f"Черновик #{draft.id} открыт на редактирование пользователем {query.from_user.id}."
        )
        await review_post(update, context)
        return POST_CREATION
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка открытия черновика #{draft_id}: {e}")
        await ctx.message(update).reply_text("Не удалось открыть черновик.")
        return ConversationHandler.END
    finally:
        session.close()


async def prompt_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пользователю сообщение с запросом на текущем шаге.
    """
    step_index = ctx.data(context)["current_step"]
    if step_index < len(POST_STEPS):
        step = POST_STEPS[step_index]
        await ctx.message(update).reply_text(
            step["prompt"], reply_markup=get_skip_keyboard()
        )
        logger.info(f"Переход к шагу {step_index + 1}: {step['key']}.")
    else:
        await review_post(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает сообщения пользователя на каждом шаге создания поста.
    """
    step_index = ctx.data(context).get("current_step", 0)
    if step_index >= len(POST_STEPS):
        # Экран превью: раньше любой текст молча завершал диалог (END),
        # и пользователь терял состояние без объяснений. Теперь — подсказка,
        # диалог остаётся активным, действие выбирается кнопками.
        await ctx.message(update).reply_text(
            "Пост готов к действию. Выберите действие кнопками под сообщением "
            "превью — или /cancel, чтобы отменить."
        )
        return POST_CREATION

    step = POST_STEPS[step_index]
    text = ctx.message(update).text or ""

    logger.info(f"Получено сообщение для шага '{step['key']}': {text}")

    if step["optional"] and text.lower() == "пропустить":
        if step["key"] == "image":
            ctx.data(context)["image"] = None
            ctx.data(context)["photos"] = []
            ctx.data(context).pop("pending_media", None)
            logger.info("Пользователь пропустил добавление изображения.")
        else:
            ctx.data(context)[step["key"]] = "Не указано"
            logger.info(f"Пользователь пропустил поле '{step['key']}'.")
    else:
        if step["validator"] and not step["validator"](text):
            await ctx.message(update).reply_text(
                "Некорректный формат. Пожалуйста, используйте правильный формат или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard(),
            )
            logger.warning(f"Некорректный ввод для поля '{step['key']}'.")
            return POST_CREATION

        if step["key"] == "image":
            if ctx.message(update).photo:
                file_id = ctx.message(update).photo[-1].file_id
                ctx.data(context)["photos"] = [file_id]
                ctx.data(context)["image"] = file_id
                ctx.data(context).pop("pending_media", None)
                await ctx.message(update).reply_text("Картинка добавлена.")
                logger.info("Пользователь добавил изображение.")
                ctx.data(context)["current_step"] += 1
                await review_post(update, context)
                # Остаёмся в POST_CREATION, чтобы кнопки действий после обзора работали
                return POST_CREATION
            else:
                await ctx.message(update).reply_text(
                    "Пожалуйста, отправьте изображение или нажмите 'Пропустить'.",
                    reply_markup=get_skip_keyboard(),
                )
                logger.warning("Пользователь не отправил изображение.")
                return POST_CREATION
        else:
            formatter = step["formatter"] if step["formatter"] else (lambda x: x)
            ctx.data(context)[step["key"]] = formatter(text)
            logger.info(
                f"Пользователь ввел '{step['key']}': {ctx.data(context)[step['key']]}"
            )

    ctx.data(context)["current_step"] += 1
    await prompt_step(update, context)
    return POST_CREATION


async def handle_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает нажатие inline-кнопки «Пропустить»: пропускает текущий шаг.
    """
    step_index = ctx.data(context).get("current_step", 0)
    if step_index >= len(POST_STEPS):
        await review_post(update, context)
        return POST_CREATION

    step = POST_STEPS[step_index]
    if step["key"] == "image":
        ctx.data(context)["image"] = None
        ctx.data(context)["photos"] = []
        ctx.data(context).pop("pending_media", None)
        logger.info("Пользователь пропустил добавление изображения.")
    else:
        ctx.data(context)[step["key"]] = "Не указано"
        logger.info(f"Пользователь пропустил поле '{step['key']}'.")

    ctx.data(context)["current_step"] += 1
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
    message = ctx.message(update)
    if not message.photo:
        return POST_CREATION

    user_data = ctx.data(context)
    file_id = message.photo[-1].file_id
    media_group_id = message.media_group_id
    step_index = user_data.get("current_step", 0)
    at_image_step = (
        step_index < len(POST_STEPS) and POST_STEPS[step_index]["key"] == "image"
    )

    # Продолжение уже начатой медиа-группы (в т.ч. запоздавшие фотографии) —
    # добавляем молча, не спамим подтверждениями на каждое фото.
    if media_group_id and user_data.get("pending_media") == media_group_id:
        user_data.setdefault("photos", []).append(file_id)
        return POST_CREATION

    # Первая фотография нового альбома — начинаем сбор
    if media_group_id and at_image_step:
        user_data["pending_media"] = media_group_id
        user_data["photos"] = [file_id]
        user_data["image"] = file_id
        await message.reply_text(
            "📷 Получаю фотографии альбома. Отправьте остальные, затем нажмите «✅ Фото готово».",
            reply_markup=get_media_done_keyboard(),
        )
        logger.info(f"Начат сбор медиа-группы {media_group_id}.")
        return POST_CREATION

    # Одиночная фотография — как раньше: сразу к превью
    if at_image_step:
        user_data["photos"] = [file_id]
        user_data["image"] = file_id
        user_data.pop("pending_media", None)
        user_data["current_step"] += 1
        await message.reply_text("Картинка добавлена.")
        await review_post(update, context)
        return POST_CREATION

    # Фото вне шага изображения: повторяем текущий запрос (или ничего —
    # на экране превью изменение фото делается через «Редактировать»).
    # Альбом отвечаем ОДНИМ сообщением: без дедупликации каждое фото
    # альбома породило бы свой промпт (спам в чат).
    if media_group_id:
        if user_data.get("last_rejected_media") == media_group_id:
            return POST_CREATION
        user_data["last_rejected_media"] = media_group_id
    if step_index < len(POST_STEPS):
        await message.reply_text(
            POST_STEPS[step_index]["prompt"],
            reply_markup=get_skip_keyboard(),
        )
    return POST_CREATION


async def _finish_media_done(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Общая логика кнопки «✅ Фото готово»: фиксирует собранные фотографии.
    Возвращает True, если фотографии есть и шаг можно завершить.
    """
    photos = ctx.data(context).get("photos") or []
    if not photos:
        await ctx.message(update).reply_text(
            "Фотографии не получены. Отправьте фото (альбом) или нажмите «Пропустить».",
            reply_markup=get_skip_keyboard(),
        )
        return False
    ctx.data(context)["image"] = photos[0]
    return True


async def handle_callback_query(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Обрабатывает CallbackQuery от кнопок действий поста.
    """
    query = ctx.query(update)
    await query.answer()
    data = query.data

    logger.info(f"Получен CallbackQuery: {data}")

    if data == "skip":
        return await handle_skip(update, context)
    elif data == "media_done":
        step_index = ctx.data(context).get("current_step", 0)
        if step_index < len(POST_STEPS) and POST_STEPS[step_index]["key"] == "image":
            if not await _finish_media_done(update, context):
                return POST_CREATION
            ctx.data(context)["current_step"] += 1
            await ctx.message(update).reply_text("Фотографии добавлены.")
        await review_post(update, context)
        return POST_CREATION
    elif data == "save_draft":
        await save_draft(update, context)
        return ConversationHandler.END
    elif data == "send_for_approval":
        await send_for_approval(update, context)
        return ConversationHandler.END
    elif data == "edit_post":
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
    return key.replace("_", " ").capitalize()


def build_post_summary(post_data: dict, *, heading: str) -> str:
    """
    Собирает итоговое MarkdownV2-сообщение о посте.
    Наша разметка (*жирный*) остаётся как есть, значения пользователя экранируются.
    """
    lines = [heading, ""]
    for step in POST_STEPS:
        key = step["key"]
        if key == "image":
            value = "Добавлено" if post_data.get("image") else "Не добавлено"
        else:
            value = escape_markdown(str(post_data.get(key) or "Не указано"))
        lines.append(f"• *{_field_label(key)}*: {value}")
    lines.append("")
    return "\n".join(lines)


# Максимальная длина одного сообщения в Telegram — 4096 символов.
# Экранирование MarkdownV2 удлиняет текст (каждый спецсимвол → 2 символа),
# поэтому итоговый текст отправляем частями с запасом до лимита.
MESSAGE_TEXT_LIMIT = 4000


def _trailing_backslashes(s: str) -> int:
    """Сколько подряд идущих '\\' в конце строки."""
    count = 0
    for char in reversed(s):
        if char != "\\":
            break
        count += 1
    return count


def _split_line(line: str, limit: int) -> list[str]:
    """
    Жёсткий сплит одной строки на части не длиннее limit.

    Граница не должна разрывать пару экранирования MarkdownV2 ('\\x'):
    иначе первая часть кончится «висячим» '\\' и Telegram вернёт ошибку
    разбора разметки. Нечётное число '\\' перед границей → откат на 1 символ.
    """
    if len(line) <= limit:
        return [line]
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = limit
        if _trailing_backslashes(rest[:cut]) % 2 == 1:
            cut -= 1
        if cut <= 0:  # защита от вырожденного limit <= 1
            cut = limit
        pieces.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        pieces.append(rest)
    return pieces


def split_long_text(text: str, limit: int = MESSAGE_TEXT_LIMIT) -> list[str]:
    """
    Разбивает текст длиннее limit на части для отправки сообщениями.

    Сначала — по переводам строк (границы частей читаются естественно);
    слишком длинная строка рвётся жёстко через _split_line. На границе
    частей теряется только один перевод строки (сообщения и так отдельные).
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        for piece in _split_line(line, limit):
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_post(
    bot,
    chat_id,
    post_data: dict,
    photos: list,
    *,
    heading: str,
    extra_lines: str = "",
    reply_markup: InlineKeyboardMarkup | None = None,
    markup_lead_text: str | None = None,
) -> None:
    """
    ЕДИНАЯ отправка поста: текст / фото с подписью / медиа-группа.

    Используется в превью, чате согласования, уведомлении ответственного
    и публикации — формат сообщения везде одинаковый, без дублирования
    логики форматирования и без повторного экранирования.

    Текст длиннее MESSAGE_TEXT_LIMIT разбивается на несколько сообщений
    (лимит Telegram — 4096); кнопки прикрепляются к первому сообщению.
    """
    text = build_post_summary(post_data, heading=heading)
    if extra_lines:
        text += extra_lines

    file_ids = [p for p in (photos or []) if p]
    chunks = split_long_text(text)

    async def send_text_chunks() -> None:
        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup if index == 0 else None,
            )

    if not file_ids:
        await send_text_chunks()
        return

    if len(file_ids) == 1:
        # Лимит подписи к фото — 1024 символа: при переполнении фото без подписи
        if len(text) <= 1000:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_ids[0],
                caption=text,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup,
            )
            return
        await bot.send_photo(chat_id=chat_id, photo=file_ids[0])
        await send_text_chunks()
        return

    # Медиа-группа: подпись разрешена только у первого снимка
    with_caption = len(text) <= 1000
    media = []
    for index, file_id in enumerate(file_ids):
        if index == 0 and with_caption:
            media.append(
                InputMediaPhoto(media=file_id, caption=text, parse_mode="MarkdownV2")
            )
        else:
            media.append(InputMediaPhoto(media=file_id))
    await bot.send_media_group(chat_id=chat_id, media=media)

    if not with_caption:
        for chunk in chunks:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="MarkdownV2")
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
        ctx.chat(update).id,
        ctx.data(context),
        _current_photos(ctx.data(context)),
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
            session, ctx.data(context).get("editing_draft_id"), ctx.user(update).id
        )
        is_update = draft is not None
        if draft is None:
            draft = Draft(user_id=ctx.user(update).id)
            session.add(draft)
        _apply_fields(draft, ctx.data(context))
        session.commit()
        if is_update:
            await ctx.message(update).reply_text(
                f"Черновик {draft.id} обновлён.", reply_markup=main_menu_keyboard()
            )
        else:
            await ctx.message(update).reply_text(
                "Пост сохранен в черновики.", reply_markup=main_menu_keyboard()
            )
        logger.info(f"Черновик {draft.id} сохранён (обновление={is_update}).")
    except Exception as e:
        session.rollback()
        await ctx.message(update).reply_text(
            "Произошла ошибка при сохранении черновика.",
            reply_markup=main_menu_keyboard(),
        )
        logger.error(f"Ошибка при сохранении черновика: {e}")
    finally:
        session.close()


def is_post_empty(post_data: dict) -> bool:
    """
    Пост пуст, если нет ни одного содержательного поля и нет фотографий.

    Значения-заглушки «Не указано» (пропущенные шаги) считаются пустыми —
    такой пост бессмысленно отправлять на согласование.
    """
    empty_values = (None, "", "Не указано")
    for key in DRAFT_FIELD_KEYS:
        if key == "image":
            continue  # фото проверяется отдельно (image + photos)
        if post_data.get(key) not in empty_values:
            return False
    return not post_data.get("image") and not post_data.get("photos")


async def send_for_approval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Отправляет пост на согласование: сохраняет (или обновляет) черновик
    и создаёт цикл согласования. Повторная отправка отклонённого поста
    начинает НОВЫЙ цикл; уже согласованный/назначенный — не дублируется.

    Ошибки разделены: сбой БД («не удалось сохранить») и сбой отправки
    в чат согласования («черновик сохранён, но отправить не удалось») —
    пользователь должен знать, что его пост не потерян.
    """
    logger.info("Отправка поста на согласование.")
    user_data = ctx.data(context)

    if is_post_empty(_post_fields(user_data)):
        await ctx.message(update).reply_text(
            "Пост пуст: добавьте хотя бы заголовок, текст или изображение.",
            reply_markup=main_menu_keyboard(),
        )
        return

    if not REVIEW_CHAT_ID:
        await ctx.message(update).reply_text(
            "Не настроен REVIEW_CHAT_ID. Пост не отправлен на согласование.",
            reply_markup=main_menu_keyboard(),
        )
        return

    session: Session = SessionLocal()
    try:
        # --- Работа с БД: черновик + цикл согласования + список ответственных
        draft = _find_own_draft(
            session, user_data.get("editing_draft_id"), ctx.user(update).id
        )
        if draft is None:
            draft = Draft(user_id=ctx.user(update).id)
            session.add(draft)
        _apply_fields(draft, user_data)
        session.commit()

        # Один активный цикл согласования на пост
        approval = get_approval(session, draft.id)
        if approval is not None and approval.status != STATUS_DECLINED:
            await ctx.message(update).reply_text(
                "Этот пост уже отправлен на согласование.",
                reply_markup=main_menu_keyboard(),
            )
            return
        if approval is not None:
            # Отклонён: правки внесены — начинаем новый цикл согласования
            reset_declined(session, draft.id)

        responsible_persons = session.query(ResponsiblePerson).all()
        keyboard = (
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            person.name,
                            callback_data=f"responsible_{draft.id}_{person.telegram_id}",
                        )
                    ]
                    for person in responsible_persons
                ]
            )
            if responsible_persons
            else None
        )

        # Всё, что нужно для отправки, фиксируем ДО закрытия сессии:
        # после close() объекты отсоединены и их атрибуты недоступны.
        draft_id = draft.id
        post_data = draft_to_post_data(draft)
        photos = get_draft_photos(draft)
        author_id = draft.user_id
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка при сохранении поста на согласование: {e}")
        await ctx.message(update).reply_text(
            "Произошла ошибка при сохранении поста.",
            reply_markup=main_menu_keyboard(),
        )
        return
    finally:
        session.close()

    logger.info(f"Черновик {draft_id} сохранён, отправляем в чат согласования.")

    # --- Отправка в чат согласования: БД больше не нужна, ошибки — отдельно
    try:
        await send_post(
            context.bot,
            REVIEW_CHAT_ID,
            post_data,
            photos,
            heading="📋 *Новый пост для согласования:*",
            extra_lines=f"*Автор поста:* {author_id}\n",
        )

        # Клавиатура выбора ответственного: в callback_data передаём id поста,
        # чтобы ответственный и данные поста определялись из БД, а не из
        # ctx.data(context) нажавшего администратора
        if keyboard is not None:
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID,
                text="Выберите ответственного за этот пост:",
                reply_markup=keyboard,
            )
        else:
            await context.bot.send_message(
                chat_id=REVIEW_CHAT_ID, text="Нет ответственных лиц для назначения."
            )
    except Exception as e:
        logger.error(
            f"Черновик {draft_id} сохранён, но отправка в чат согласования "
            f"не удалась: {e}"
        )
        await ctx.message(update).reply_text(
            f"Черновик сохранён (№{draft_id}), но отправить пост в чат "
            "согласования не удалось. Проверьте, что бот добавлен в чат "
            "и REVIEW_CHAT_ID указан верно.",
            reply_markup=main_menu_keyboard(),
        )
        return

    await ctx.message(update).reply_text(
        "Пост отправлен на согласование.", reply_markup=main_menu_keyboard()
    )


async def edit_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Позволяет пользователю выбрать поле для редактирования.
    """
    logger.info("Пользователь выбрал редактирование поста.")
    keyboard = [
        [InlineKeyboardButton("Заголовок", callback_data="edit_title")],
        [InlineKeyboardButton("Дата", callback_data="edit_date")],
        [InlineKeyboardButton("Время начала", callback_data="edit_time_start")],
        [InlineKeyboardButton("Время окончания", callback_data="edit_time_end")],
        [InlineKeyboardButton("Место", callback_data="edit_place_name")],
        [InlineKeyboardButton("Текст", callback_data="edit_text")],
        [InlineKeyboardButton("Контакты", callback_data="edit_contact")],
        [InlineKeyboardButton("URL места", callback_data="edit_place_url")],
        [InlineKeyboardButton("Изображение", callback_data="edit_image")],
        [InlineKeyboardButton("Отмена", callback_data="cancel_edit")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await ctx.message(update).reply_text(
        "Выберите поле для редактирования:", reply_markup=reply_markup
    )
    return EDIT_FIELD


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает выбор поля для редактирования.
    """
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""

    if data == "cancel_edit":
        # Возвращаемся к обзору поста, чтобы кнопки действий продолжали работать
        await query.edit_message_text(
            "Редактирование отменено. Выберите действие:",
            reply_markup=get_post_actions_keyboard(),
        )
        logger.info("Редактирование отменено пользователем.")
        return POST_CREATION

    field_to_edit = data.replace("edit_", "")
    ctx.data(context)["edit_field"] = field_to_edit
    step = next((step for step in POST_STEPS if step["key"] == field_to_edit), None)

    if step:
        if step["key"] == "image":
            await query.edit_message_text(
                "Отправьте новое изображение или нажмите 'Пропустить':",
                reply_markup=get_skip_keyboard(),
            )
        else:
            prompt_text = f"Введите новое значение для '{field_to_edit}' или нажмите 'Пропустить':"
            await query.edit_message_text(prompt_text, reply_markup=get_skip_keyboard())
        logger.info(f"Пользователь выбрал редактировать поле '{field_to_edit}'.")
        return EDIT_FIELD
    else:
        # editMessageText не принимает ReplyKeyboardRemove (только inline) —
        # правим текст без reply_markup; диалог уже завершён, reply-клавиатура
        # главного меню остаётся у пользователя.
        await query.edit_message_text("Неизвестное поле для редактирования.")
        logger.warning(
            f"Пользователь выбрал неизвестное поле для редактирования: {field_to_edit}"
        )
        return ConversationHandler.END


async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает ввод пользователя при редактировании поля.
    После успешного изменения показывает актуальное превью поста
    (устаревший обзор не должен висеть после правок).
    """
    field = str(ctx.data(context).get("edit_field") or "")
    text = ctx.message(update).text or ""

    logger.info(f"Пользователь редактирует поле '{field}' с вводом: {text}")

    if not field:
        # Состояние EDIT_FIELD без выбранного поля (не должно случаться):
        # не пишем мусор в user_data, а возвращаем пользователя к превью
        logger.warning("Редактирование без выбранного поля — возврат к превью.")
        await review_post(update, context)
        return POST_CREATION

    if text.lower() == "пропустить":
        if field == "image":
            ctx.data(context)["image"] = None
            ctx.data(context)["photos"] = []
            ctx.data(context).pop("pending_media", None)
            logger.info(
                "Пользователь пропустил добавление изображения при редактировании."
            )
        elif field:
            ctx.data(context)[field] = "Не указано"
            logger.info(f"Пользователь пропустил обновление поля '{field}'.")
        await review_post(update, context)
        # Возвращаемся в POST_CREATION: кнопки действий должны остаться рабочими
        return POST_CREATION

    # Валидация и форматирование
    validators = {
        "date": validate_date,
        "time_start": validate_time,
        "time_end": validate_time,
        "place_url": validate_url,
    }

    if field in validators and not validators[field](text):
        await ctx.message(update).reply_text(
            "Некорректный формат. Пожалуйста, введите корректные данные или нажмите 'Пропустить'.",
            reply_markup=get_skip_keyboard(),
        )
        logger.warning(f"Некорректный ввод при редактировании поля '{field}': {text}")
        return EDIT_FIELD

    if field == "image":
        if ctx.message(update).photo:
            file_id = ctx.message(update).photo[-1].file_id
            media_group_id = ctx.message(update).media_group_id

            # Продолжение начатого альбома — добавляем молча
            if (
                media_group_id
                and ctx.data(context).get("pending_media") == media_group_id
            ):
                ctx.data(context).setdefault("photos", []).append(file_id)
                return EDIT_FIELD

            # Новый альбом — собираем до нажатия «✅ Фото готово»
            if media_group_id:
                ctx.data(context)["pending_media"] = media_group_id
                ctx.data(context)["photos"] = [file_id]
                ctx.data(context)["image"] = file_id
                await ctx.message(update).reply_text(
                    "📷 Отправьте остальные фотографии альбома, затем нажмите «✅ Фото готово».",
                    reply_markup=get_media_done_keyboard(),
                )
                logger.info(
                    f"Начат сбор медиа-группы при редактировании: {media_group_id}."
                )
                return EDIT_FIELD

            # Одиночная фотография — как раньше: сразу обновляем и показываем превью
            ctx.data(context)["photos"] = [file_id]
            ctx.data(context)["image"] = file_id
            ctx.data(context).pop("pending_media", None)
            logger.info("Пользователь обновил изображение.")
        else:
            await ctx.message(update).reply_text(
                "Пожалуйста, отправьте изображение или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard(),
            )
            logger.warning("Пользователь не отправил изображение при редактировании.")
            return EDIT_FIELD
    else:
        formatter = {
            "title": format_text,
            "date": format_text,
            "time_start": format_text,
            "time_end": format_text,
            "place_name": format_text,
            "text": format_text,
            "contact": format_text,
            "place_url": format_text,
        }.get(field, lambda x: x)
        ctx.data(context)[field] = formatter(text)
        logger.info(f"Пользователь обновил поле '{field}'.")

    # Остаёмся в диалоге: превью с актуальными значениями + кнопки действий
    await review_post(update, context)
    return POST_CREATION


async def handle_skip_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Кнопка «Пропустить» в режиме редактирования поля (callback 'skip'
    в состоянии EDIT_FIELD — до этого она не обрабатывалась и «проваливалась»).
    """
    query = ctx.query(update)
    await query.answer()
    field = ctx.data(context).get("edit_field")
    if field == "image":
        ctx.data(context)["image"] = None
        ctx.data(context)["photos"] = []
        ctx.data(context).pop("pending_media", None)
    elif field:
        ctx.data(context)[field] = "Не указано"
    await review_post(update, context)
    return POST_CREATION


async def finish_photo_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Кнопка «✅ Фото готово» при редактировании изображения альбомом.
    """
    query = ctx.query(update)
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
    await ctx.message(update).reply_text(
        "Создание поста отменено.", reply_markup=main_menu_keyboard()
    )
    logger.info("Пользователь отменил создание поста.")
    return ConversationHandler.END


async def view_drafts_and_exit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Reply-кнопка «📝 Черновики» ВНУТРИ диалога: показывает список черновиков
    и завершает диалог — состояние не должно «висеть» после перехода
    в другой раздел (иначе следующий текст пользователя уходил бы
    на шаг диалога, который пользователь уже покинул).
    """
    await view_drafts(update, context)
    return ConversationHandler.END


def post_creation_handlers() -> list[BaseHandler]:
    """
    Возвращает список обработчиков для создания поста.
    """
    return [
        ConversationHandler(
            entry_points=[
                CommandHandler("create_post", start_post_creation),
                # Кнопка «Создать пост» из Reply-клавиатуры главного меню.
                # Обязательна как entry_point: только ConversationHandler умеет
                # отслеживать состояние диалога.
                MessageHandler(filters.Regex("^✏️ Создать пост$"), start_post_creation),
                # Открытие существующего черновика в редакторе (кнопка в списке черновиков)
                CallbackQueryHandler(start_edit_draft, pattern=r"^editdraft_\d+$"),
            ],
            states={
                POST_CREATION: [
                    # Повторное нажатие кнопки mid-диалога начинает создание заново
                    MessageHandler(
                        filters.Regex("^✏️ Создать пост$"), start_post_creation
                    ),
                    # «Черновики» в диалоге: показать список и завершить диалог
                    # (важно стоять ДО общего TEXT-хендлера — иначе текст
                    # кнопки запишется в поле поста)
                    MessageHandler(
                        filters.Regex("^📝 Черновики$"), view_drafts_and_exit
                    ),
                    MessageHandler(filters.PHOTO, handle_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                    CallbackQueryHandler(
                        handle_callback_query,
                        pattern="^(skip|save_draft|send_for_approval|edit_post|media_done)$",
                    ),
                ],
                EDIT_FIELD: [
                    # То же и в режиме редактирования поля: иначе нажатие
                    # «Черновики» записалось бы в редактируемое поле
                    MessageHandler(
                        filters.Regex("^📝 Черновики$"), view_drafts_and_exit
                    ),
                    CallbackQueryHandler(
                        handle_edit, pattern="^edit_.*$|^cancel_edit$"
                    ),
                    CallbackQueryHandler(finish_photo_edit, pattern="^media_done$"),
                    CallbackQueryHandler(handle_skip_edit, pattern="^skip$"),
                    MessageHandler(filters.PHOTO, process_edit),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_edit),
                ],
            },
            fallbacks=[CommandHandler("cancel", cancel_creation)],
            allow_reentry=True,
        )
    ]
