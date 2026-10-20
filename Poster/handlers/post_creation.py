import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    MessageEntity,
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

from database import SessionLocal
from handlers.callbacks import handle_main_menu_selection
from handlers.drafts import view_drafts
from handlers.main_menu import help_command, main_menu_keyboard, start
from models import (
    Draft,
    formats_to_json,
    get_draft_formats,
    get_draft_photos,
    photos_to_json,
)
from utils import tg_context as ctx
from utils.formatter import format_text
from utils.tg_html import entities_to_html, escape_html, split_html, utf16_len
from utils.validators import (
    normalize_date,
    normalize_time,
    normalize_url,
    validate_date,
    validate_time,
    validate_url,
)

# Настройка логирования
logger = logging.getLogger(__name__)

# Определение состояний
POST_CREATION, EDIT_FIELD = range(2)

# Единый заголовок поста для ПРЕВЬЮ и ГОТОВОГО ПОСТА: текст, который видит
# пользователь в превью, дословно совпадает с финальной отправкой по «Готово».
POST_HEADING = "📢 <b>Новый пост:</b>"

# Определение шагов создания поста
# list[dict[str, Any]] — явная аннотация: без неё mypy сводит разнотипные
# словари к object и «step['key']» становится ошибкой типов.
# POST_STEPS — единый источник метаданных полей: русская метка (label),
# промпт шага и валидатор берутся отсюда и визардом, и редактором, и сводкой
# поста; текстовые поля форматируются единообразно — через format_text.
POST_STEPS: list[dict[str, Any]] = [
    {
        "key": "title",
        "label": "Заголовок",
        "prompt": "Введите заголовок поста или нажмите 'Пропустить':",
        "validator": None,
    },
    {
        "key": "date",
        "label": "Дата",
        "prompt": (
            "Введите дату события (ДД.ММ.ГГГГ, 17.11, 1711, «17 ноября», "
            "«сегодня», «завтра») или нажмите 'Пропустить':"
        ),
        "validator": validate_date,
        "normalizer": normalize_date,
    },
    {
        "key": "time_start",
        "label": "Время начала",
        "prompt": (
            "Введите время начала (ЧЧ:ММ, 1830, 930, «полседьмого», "
            "просто час — напр. 7) или нажмите 'Пропустить':"
        ),
        "validator": validate_time,
        "normalizer": normalize_time,
    },
    {
        "key": "time_end",
        "label": "Время окончания",
        "prompt": (
            "Введите время окончания (ЧЧ:ММ, 1830, 930, «полседьмого», "
            "просто час — напр. 7) или нажмите 'Пропустить':"
        ),
        "validator": validate_time,
        "normalizer": normalize_time,
    },
    {
        "key": "place_name",
        "label": "Место",
        "prompt": "Введите место проведения или нажмите 'Пропустить':",
        "validator": None,
    },
    {
        "key": "text",
        "label": "Текст",
        "prompt": "Введите текст поста или нажмите 'Пропустить':",
        "validator": None,
    },
    {
        "key": "contact",
        "label": "Контакты",
        "prompt": "Введите контактную информацию или нажмите 'Пропустить':",
        "validator": None,
    },
    {
        "key": "place_url",
        "label": "URL места",
        "prompt": (
            "Введите ссылку на место (t.me/..., @username, example.com "
            "или полный URL) или нажмите 'Пропустить':"
        ),
        "validator": validate_url,
        "normalizer": normalize_url,
    },
    {
        "key": "image",
        "label": "Изображение",
        "prompt": "Отправьте изображение или нажмите 'Пропустить':",
        "validator": None,
    },
]

# Поля черновика выводятся из POST_STEPS: состав совпадает с колонками
# модели Draft (кроме служебных), порядок здесь не важен.
DRAFT_FIELD_KEYS = [step["key"] for step in POST_STEPS]


def get_skip_keyboard():
    """
    Возвращает клавиатуру с кнопкой "Пропустить".
    """
    keyboard = [[InlineKeyboardButton("Пропустить", callback_data="skip")]]
    return InlineKeyboardMarkup(keyboard)


def get_post_actions_keyboard():
    """
    Действия на экране превью: отредактировать, сохранить в черновики,
    завершить — получить готовый пост.
    """
    keyboard = [
        [InlineKeyboardButton("✏️ Редактировать", callback_data="edit_post")],
        [InlineKeyboardButton("📄 Сохранить в черновики", callback_data="save_draft")],
        # Подпись объясняет результат: после нажатия бот пришлёт готовый
        # пост именно в этот чат (сам текст поста от кнопки не зависит)
        [InlineKeyboardButton("✅ Готово — получить пост", callback_data="finish")],
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
        "formats",
        "pending_media",
        "last_rejected_media",
        "editing_draft_id",
        "edit_field",
    ]:
        user_data.pop(key, None)


def _apply_skip(user_data: dict, key: str) -> None:
    """
    Пропуск шага или правки: поле становится пустым (None), изображение —
    без фото. Общая точка для визарда и режима редактирования.
    """
    if key == "image":
        user_data["image"] = None
        user_data["photos"] = []
        user_data.pop("pending_media", None)
    else:
        user_data[key] = None
    # Формат пропущенного поля неактуален и не должен «воскреснуть» в сводке
    _set_field_html(user_data, key, None)


def _set_field_html(user_data: dict, key: str, html_value: str | None) -> None:
    """
    Запоминает/сбрасывает HTML-представление поля (user_data["formats"]).

    Пустой набор форматирования не хранится: поле без форматирования
    и поле из легаси-записи неразличимы и рендерятся одинаково.
    """
    formats = user_data.get("formats")
    formats = formats if isinstance(formats, dict) else {}
    if html_value:
        formats[key] = html_value
        user_data["formats"] = formats
    else:
        formats.pop(key, None)
        if formats:
            user_data["formats"] = formats
        else:
            user_data.pop("formats", None)


def _store_text_field(
    user_data: dict,
    key: str,
    text: str,
    entities: Sequence[MessageEntity] | None,
) -> None:
    """
    Сохраняет свободное текстовое поле с учётом форматирования.

    Обычный текст уходит через типографику (format_text); сообщение
    с entities — дословно (типографика сдвинула бы смещения и изменила бы
    видимый текст), а его разметка сохраняется отдельно в
    user_data["formats"][key] как безопасный HTML и применяется в сводке.
    """
    if entities:
        user_data[key] = text
        _set_field_html(user_data, key, entities_to_html(text, entities))
    else:
        user_data[key] = format_text(text)
        _set_field_html(user_data, key, None)


def _store_photo(user_data: dict, message: Message) -> str:
    """
    Сохраняет фотографию в user_data (одиночное фото или альбом).
    Возвращает состояние записи:
      "continued" — фото добавлено в уже начатый альбом (молча),
      "started"   — начат новый альбом (нужна кнопка «✅ Фото готово»),
      "single"    — одиночное фото (готово к превью).
    """
    file_id = message.photo[-1].file_id
    media_group_id = message.media_group_id
    if media_group_id and user_data.get("pending_media") == media_group_id:
        user_data.setdefault("photos", []).append(file_id)
        return "continued"
    if media_group_id:
        user_data["pending_media"] = media_group_id
        user_data["photos"] = [file_id]
        user_data["image"] = file_id
        return "started"
    user_data["photos"] = [file_id]
    user_data["image"] = file_id
    user_data.pop("pending_media", None)
    return "single"


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
    fields["formats"] = formats_to_json(user_data.get("formats"))
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


def _persist_wip(user_data: dict, user_id: int) -> int | None:
    """
    Сохраняет незавершённый пост (work-in-progress) в черновик перед выходом
    из диалога: обновляет открытый черновик или создаёт новый.

    Пустой пост не сохраняется. Возвращает id черновика либо None при сбое БД.
    """
    if is_post_empty(_post_fields(user_data)):
        return None
    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(session, user_data.get("editing_draft_id"), user_id)
        if draft is None:
            draft = Draft(user_id=user_id)
            session.add(draft)
        _apply_fields(draft, user_data)
        session.commit()
        logger.info(f"Незавершённый пост сохранён в черновик #{draft.id}.")
        return draft.id
    except Exception as e:
        session.rollback()
        logger.error(f"Не удалось сохранить незавершённый пост: {e}")
        return None
    finally:
        session.close()


async def _save_wip_before_exit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Выход из диалога без потери данных: если пост не пуст — сохраняем его
    в черновики и очищаем user_data; при сбое БД данные остаются в памяти,
    чтобы их можно было сохранить вручную.
    """
    user_data = ctx.data(context)
    if is_post_empty(_post_fields(user_data)):
        _clear_post_data(user_data)
        return
    draft_id = _persist_wip(user_data, ctx.user(update).id)
    if draft_id is None:
        # Ошибка БД: кнопка «Сохранить в черновики» после завершения диалога
        # уже недоступна — подсказываем реально работающий следующий шаг
        # (повторное «Создать пост» сохранит данные ещё раз)
        await ctx.message(update).reply_text(
            "Не удалось сохранить незавершённый пост: ошибка базы данных. "
            "Нажмите «✏️ Создать пост» ещё раз — я повторю попытку сохранения."
        )
        return
    await ctx.message(update).reply_text(
        f"Незавершённый пост сохранён в черновики (№{draft_id})."
    )
    _clear_post_data(user_data)


async def start_post_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Запускает процесс создания поста. Если в user_data остался незавершённый
    пост (предыдущий диалог не завершён) — он сохраняется в черновики.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        # вход по inline-кнопке «✏️ Создать пост»: спиннер не должен висеть
        await query.answer()
    logger.info("Начало создания поста.")
    await _save_wip_before_exit(update, context)
    ctx.data(context)["current_step"] = 0  # Инициализация текущего шага
    await prompt_step(update, context)
    return POST_CREATION


async def start_edit_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Открывает существующий черновик в редакторе (callback editdraft_<id>).

    Точка входа ConversationHandler: черновик загружается в user_data,
    показывается превью — дальше работают те же кнопки «Сохранить в черновики»
    (обновление БЕЗ создания копии) и «Готово».
    """
    query = ctx.query(update)
    await query.answer()

    try:
        draft_id = int((query.data or "")[len("editdraft_") :])
    except (TypeError, ValueError):
        return ConversationHandler.END

    # Открытие другого черновика: незавершённый пост не теряем
    await _save_wip_before_exit(update, context)

    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(session, draft_id, query.from_user.id)
        if draft is None:
            await ctx.message(update).reply_text("Черновик не найден.")
            return ConversationHandler.END

        _clear_post_data(ctx.data(context))
        for key in DRAFT_FIELD_KEYS:
            value = getattr(draft, key)
            # Легаси-заглушка «Не указано» из старых черновиков = пустое поле
            ctx.data(context)[key] = None if value == "Не указано" else value
        formats = get_draft_formats(draft)
        if formats:
            ctx.data(context)["formats"] = formats
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
        # Заголовок шага: номер прогресса + русское имя поля — пользователь
        # всегда видит, какое поле заполняется сейчас
        await ctx.message(update).reply_text(
            f"Шаг {step_index + 1} из {len(POST_STEPS)} — {step['label']}.\n"
            f"{step['prompt']}",
            reply_markup=get_skip_keyboard(),
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

    if text.lower() == "пропустить":
        # Пропуск — пустое поле (None), а не строка-заглушка:
        # «Не указано» не должно попадать ни в сводку, ни в БД
        _apply_skip(ctx.data(context), step["key"])
        logger.info(f"Пользователь пропустил поле '{step['key']}'.")
    else:
        # Нормализация (дата/время/ссылка): успешный разбор возвращает
        # нормализованный вид для хранения, неуспешный — None (ошибка).
        normalized = step["normalizer"](text) if step.get("normalizer") else None
        if step.get("normalizer"):
            valid = normalized is not None
        else:
            valid = not step["validator"] or step["validator"](text)
        if not valid:
            await ctx.message(update).reply_text(
                f"Некорректный формат. {step['prompt']}",
                reply_markup=get_skip_keyboard(),
            )
            logger.warning(f"Некорректный ввод для поля '{step['key']}'.")
            return POST_CREATION

        if step["key"] == "image":
            if ctx.message(update).photo:
                _store_photo(ctx.data(context), ctx.message(update))
                await ctx.message(update).reply_text("Фото добавлено.")
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
            user_data = ctx.data(context)
            if normalized is not None:
                # Нормализованные значения (дата/время/ссылка) — каноничный
                # плоский текст; форматирование исходника здесь не сохраняем
                user_data[step["key"]] = normalized
                _set_field_html(user_data, step["key"], None)
            else:
                _store_text_field(
                    user_data, step["key"], text, ctx.message(update).entities
                )
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
    _apply_skip(ctx.data(context), step["key"])
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
    и гонок с кнопками «Сохранить в черновики»/«Готово».
    """
    message = ctx.message(update)
    if not message.photo:
        return POST_CREATION

    user_data = ctx.data(context)
    media_group_id = message.media_group_id
    step_index = user_data.get("current_step", 0)
    at_image_step = (
        step_index < len(POST_STEPS) and POST_STEPS[step_index]["key"] == "image"
    )

    # Продолжение уже начатой медиа-группы — только пока активен шаг
    # изображения: добавляем молча, не спамим подтверждениями на каждое фото.
    # После «✅ Фото готово» (шаг завершён) запоздавшие фото того же альбома
    # НЕ меняют данные поста — иначе финальный пост содержал бы больше фото,
    # чем показано в превью. Такие фото уходят в общую ветку отклонения ниже.
    if (
        media_group_id
        and at_image_step
        and user_data.get("pending_media") == media_group_id
    ):
        user_data.setdefault("photos", []).append(message.photo[-1].file_id)
        return POST_CREATION

    if not at_image_step:
        # Фото вне шага изображения: НЕ теряем молча — объясняем, где менять
        # фото. Альбом отвечаем ОДНИМ сообщением: без дедупликации каждое
        # фото альбома породило бы свой промпт (спам в чат).
        if media_group_id:
            if user_data.get("last_rejected_media") == media_group_id:
                return POST_CREATION
            user_data["last_rejected_media"] = media_group_id
        if step_index < len(POST_STEPS):
            step = POST_STEPS[step_index]
            await message.reply_text(
                f"Фото можно добавить на шаге «Изображение». "
                f"Сейчас — шаг {step_index + 1} из {len(POST_STEPS)} "
                f"({step['label']}): {step['prompt']}",
                reply_markup=get_skip_keyboard(),
            )
        else:
            # На экране превью — путь к замене фото понятен
            await message.reply_text(
                "Изображение можно заменить через «✏️ Редактировать» "
                "→ «Изображение»."
            )
        return POST_CREATION

    state = _store_photo(user_data, message)
    if state == "started":
        await message.reply_text(
            "📷 Получаю фотографии альбома. Отправьте остальные, "
            "затем нажмите «✅ Фото готово».",
            reply_markup=get_media_done_keyboard(),
        )
        logger.info(f"Начат сбор медиа-группы {media_group_id}.")
        return POST_CREATION
    if state == "single":
        # Одиночная фотография — как раньше: сразу к превью
        user_data["current_step"] += 1
        await message.reply_text("Фото добавлено.")
        await review_post(update, context)
    # "continued" — запоздавшее фото альбома уже добавлено в список
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
        # Пустой пост черновик не создаёт: остаёмся на превью с объяснением
        # (при ошибке БД данные остаются в user_data — тоже остаёмся)
        if await save_draft(update, context):
            return ConversationHandler.END
        return POST_CREATION
    elif data == "finish":
        # «Готово»: пост отправлен → цикл завершён; иначе (пустой пост,
        # сбой отправки) остаёмся на превью, данные не потеряны
        if await finish_post(update, context):
            return ConversationHandler.END
        return POST_CREATION
    elif data == "edit_post":
        # Важно: возвращаем EDIT_FIELD, иначе состояние редактирования не наступит
        return await edit_post(update, context)
    else:
        logger.warning(f"Неизвестный CallbackQuery: {data}")
        return POST_CREATION


def _field_filled(value) -> bool:
    """
    Единый критерий заполненности текстового поля.

    Пусто: None, пустая строка, значение только из пробелов, а также
    легаси-заглушка «Не указано» из старых черновиков. Одно правило для
    сводки (build_post_summary) и запрета пустых черновиков (is_post_empty).
    """
    if value is None:
        return False
    stripped = str(value).strip()
    return bool(stripped) and stripped != "Не указано"


def build_post_summary(post_data: dict, *, heading: str) -> str:
    """
    Собирает итоговое HTML-сообщение о посте: русские названия полей,
    показываются ТОЛЬКО заполненные поля (пропуск не даёт строки в сводке).

    Превью и готовый пост собираются этой же функцией — тексты совпадают
    дословно. Наша разметка (<b>) остаётся как есть; значение пользователя —
    либо готовый HTML сохранённого форматирования (user_data["formats"]),
    либо плоский текст с HTML-экранированием: каждое значение экранируется
    ровно один раз, двойного экранирования нет.
    """
    lines = [heading, ""]
    formats = post_data.get("formats") or {}
    has_fields = False
    for step in POST_STEPS:
        key = step["key"]
        if key == "image":
            if post_data.get("image") or post_data.get("photos"):
                lines.append(f"• <b>{step['label']}</b>: Добавлено")
                has_fields = True
            continue
        value = post_data.get(key)
        # Единый критерий пустоты: пробелы-только и легаси-«Не указано» —
        # пустое поле, его строки в сводке не будет
        if not _field_filled(value):
            continue
        html_value = formats.get(key) or escape_html(str(value))
        lines.append(f"• <b>{step['label']}</b>: {html_value}")
        has_fields = True
    if not has_fields:
        # Превью ещё ничего не заполнено: без этой строки пользователь видит
        # только заголовок и не понимает, что происходит
        lines.append("Пока ни одно поле не заполнено.")
    lines.append("")
    return "\n".join(lines)


# Максимальная длина одного сообщения в Telegram — 4096 символов; подпись
# к фото — 1024. Лимит считается по «сырому» HTML в код-юнитах UTF-16
# (см. utils.tg_html): он не короче видимого текста, поэтому части
# укладываются в лимит и после разбора разметки Telegram — с запасом.
MESSAGE_TEXT_LIMIT = 4000
CAPTION_TEXT_LIMIT = 1000


def split_long_text(text: str, limit: int = MESSAGE_TEXT_LIMIT) -> list[str]:
    """
    Разбивает длинный HTML-пост на части для отправки сообщениями.

    Разбор и границы — в utils.tg_html.split_html: теги и ссылки-сущности
    не рвутся, открытые на границе теги закрываются и переоткрываются,
    видимый текст сохраняется без потерь и дублей, граница предпочитается
    после перевода строки.
    """
    return split_html(text, limit)


async def send_post(
    bot,
    chat_id,
    post_data: dict,
    photos: list,
    *,
    heading: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    markup_lead_text: str | None = None,
) -> None:
    """
    ЕДИНАЯ отправка поста: текст / фото с подписью / медиа-группа.

    Используется в превью и в финальной отправке по кнопке «Готово» —
    формат сообщения одинаковый, без дублирования логики форматирования
    и без повторного экранирования.

    Текст длиннее MESSAGE_TEXT_LIMIT разбивается на несколько сообщений
    (лимит Telegram — 4096); кнопки прикрепляются к первому сообщению.
    """
    text = build_post_summary(post_data, heading=heading)

    file_ids = [p for p in (photos or []) if p]
    chunks = split_long_text(text)

    async def send_text_chunks() -> None:
        for index, chunk in enumerate(chunks):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
                reply_markup=reply_markup if index == 0 else None,
            )

    if not file_ids:
        await send_text_chunks()
        return

    if len(file_ids) == 1:
        # Лимит подписи к фото — 1024 символа (считаем в UTF-16 по сырому
        # HTML): при переполнении фото уходит без подписи, текст — частями
        if utf16_len(text) <= CAPTION_TEXT_LIMIT:
            await bot.send_photo(
                chat_id=chat_id,
                photo=file_ids[0],
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
            return
        await bot.send_photo(chat_id=chat_id, photo=file_ids[0])
        await send_text_chunks()
        return

    # Медиа-группа: подпись разрешена только у первого снимка
    with_caption = utf16_len(text) <= CAPTION_TEXT_LIMIT
    media = []
    for index, file_id in enumerate(file_ids):
        if index == 0 and with_caption:
            media.append(
                InputMediaPhoto(media=file_id, caption=text, parse_mode="HTML")
            )
        else:
            media.append(InputMediaPhoto(media=file_id))
    await bot.send_media_group(chat_id=chat_id, media=media)

    if not with_caption:
        for chunk in chunks:
            await bot.send_message(chat_id=chat_id, text=chunk, parse_mode="HTML")
    if reply_markup is not None:
        # sendMediaGroup не поддерживает кнопки — клавиатура отдельным сообщением
        await bot.send_message(
            chat_id=chat_id,
            text=markup_lead_text or "Выберите действие:",
            reply_markup=reply_markup,
        )


async def review_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Превью поста: фотографии + ровно тот же текст, что уйдёт в готовом
    посте по кнопке «Готово». Кнопки действий — к этому же сообщению
    (для альбома — отдельным сообщением).
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


async def save_draft(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Сохраняет пост в черновики: обновляет открытый черновик (editing_draft_id)
    или создаёт новый — лишних копий не появляется.

    Возвращает True, если запись создана/обновлена. False — пустой пост
    (запись НЕ создаётся и не портит открытый черновик, данные остаются
    в user_data) или ошибка БД (данные тоже не теряются). Диалог в этом
    случае остаётся на превью.
    """
    logger.info("Сохранение поста в черновики.")
    user_data = ctx.data(context)
    if is_post_empty(_post_fields(user_data)):
        await ctx.message(update).reply_text(
            "Пост пуст: заполните хотя бы одно поле или добавьте фото, "
            "чтобы сохранить черновик.",
            reply_markup=main_menu_keyboard(),
        )
        logger.info("Пустой пост не сохранён — черновик не создан.")
        return False
    session: Session = SessionLocal()
    try:
        draft = _find_own_draft(
            session, user_data.get("editing_draft_id"), ctx.user(update).id
        )
        is_update = draft is not None
        if draft is None:
            draft = Draft(user_id=ctx.user(update).id)
            session.add(draft)
        _apply_fields(draft, user_data)
        session.commit()
        # После успешного сохранения данные чистим: повторные выходы не создадут
        # дубль-черновик, а пост уже надёжно в БД
        _clear_post_data(user_data)
        if is_update:
            await ctx.message(update).reply_text(
                f"Черновик {draft.id} обновлён.", reply_markup=main_menu_keyboard()
            )
        else:
            await ctx.message(update).reply_text(
                "Пост сохранён в черновики.", reply_markup=main_menu_keyboard()
            )
        logger.info(f"Черновик {draft.id} сохранён (обновление={is_update}).")
        return True
    except Exception as e:
        session.rollback()
        await ctx.message(update).reply_text(
            "Произошла ошибка при сохранении черновика.",
            reply_markup=main_menu_keyboard(),
        )
        logger.error(f"Ошибка при сохранении черновика: {e}")
        return False
    finally:
        session.close()


def is_post_empty(post_data: dict) -> bool:
    """
    Пост пуст, если нет ни одного содержательного поля и нет фотографий.

    Критерий поля — _field_filled (strip + легаси-«Не указано»): пробелы-только
    это пусто. Фото (image + photos) делает пост заполненным, даже если все
    текстовые поля пусты.
    """
    for key in DRAFT_FIELD_KEYS:
        if key == "image":
            continue  # фото проверяется отдельно (image + photos)
        if _field_filled(post_data.get(key)):
            return False
    return not post_data.get("image") and not post_data.get("photos")


async def finish_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    «Готово»: отправляет пользователю ГОТОВЫЙ пост тем же механизмом
    (send_post), что и превью, — в ТОТ ЖЕ личный чат, без посредников.

    Возвращает True, если пост отправлен (цикл завершён).
    False — пустой пост или сбой отправки: пользователь остаётся
    на превью, введённые данные не теряются.
    """
    user_data = ctx.data(context)

    if is_post_empty(_post_fields(user_data)):
        await ctx.message(update).reply_text(
            "Пост пуст: добавьте хотя бы заголовок, текст или изображение."
        )
        return False

    try:
        await send_post(
            context.bot,
            ctx.chat(update).id,
            user_data,
            _current_photos(user_data),
            heading=POST_HEADING,
        )
    except Exception as e:
        logger.error(f"Не удалось отправить готовый пост: {e}")
        await ctx.message(update).reply_text(
            "Не удалось отправить готовый пост. Попробуйте ещё раз."
        )
        return False

    _clear_post_data(user_data)
    await ctx.message(update).reply_text(
        "Пост готов.", reply_markup=main_menu_keyboard()
    )
    logger.info("Готовый пост отправлен пользователю.")
    return True


async def edit_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Позволяет пользователю выбрать поле для редактирования.

    Список полей берётся из POST_STEPS (единый источник): русские названия
    и ключи полей совпадают с шагами создания.
    """
    logger.info("Пользователь выбрал редактирование поста.")
    keyboard = [
        [InlineKeyboardButton(step["label"], callback_data=f"edit_{step['key']}")]
        for step in POST_STEPS
    ]
    keyboard.append([InlineKeyboardButton("Отмена", callback_data="cancel_edit")])
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
            # В промпте — человекочитаемое имя поля («Время начала»),
            # а не внутренний ключ (time_start)
            prompt_text = (
                f"Введите новое значение для «{step['label']}» "
                "или нажмите 'Пропустить':"
            )
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


async def handle_stale_preview_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Кнопки превью, нажатые во время редактирования поля: «✅ Готово —
    получить пост», «📄 Сохранить в черновики» и повторное «✏️ Редактировать».

    Ничего не сохраняем и не отправляем: бот отвечает короткой подсказкой,
    состояние EDIT_FIELD сохраняется. Раньше «Редактировать» здесь попадал
    в обработчик выбора поля («Неизвестное поле» + завершение диалога),
    а «Готово»/«Сохранить в черновики» молча игнорировались.
    """
    query = ctx.query(update)
    await query.answer("Сначала завершите редактирование поля")
    logger.info(
        f"Кнопка превью '{query.data}' во время редактирования поля — подсказка."
    )
    return EDIT_FIELD


async def process_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Обрабатывает ввод пользователя при редактировании поля.
    После успешного изменения показывает актуальное превью поста
    (устаревший обзор не должен висеть после правок).
    """
    user_data = ctx.data(context)
    message = ctx.message(update)
    field = str(user_data.get("edit_field") or "")
    text = message.text or ""

    logger.info(f"Пользователь редактирует поле '{field}' с вводом: {text}")

    step = next((s for s in POST_STEPS if s["key"] == field), None)
    if not field or step is None:
        # Состояние EDIT_FIELD без выбранного/неизвестного поля (не должно
        # случаться): не пишем мусор в user_data, а возвращаем к превью
        logger.warning("Редактирование без выбранного поля — возврат к превью.")
        await review_post(update, context)
        return POST_CREATION

    if message.photo and field != "image":
        # Фото прислали, пока редактировали другое поле: значение НЕ трогаем
        # (иначе поле стёрлось бы пустой строкой) — объясняем порядок действий
        await message.reply_text(
            "Здесь ожидается текст. Изображение меняется в поле «Изображение».",
            reply_markup=get_skip_keyboard(),
        )
        return EDIT_FIELD

    if text.lower() == "пропустить":
        _apply_skip(user_data, field)
        logger.info(f"Пользователь пропустил обновление поля '{field}'.")
        await review_post(update, context)
        # Возвращаемся в POST_CREATION: кнопки действий должны остаться рабочими
        return POST_CREATION

    # Нормализация (дата/время/ссылка) — единое правило с визардом
    normalized = step["normalizer"](text) if step.get("normalizer") else None
    if step.get("normalizer"):
        valid = normalized is not None
    else:
        valid = not step["validator"] or step["validator"](text)
    if not valid:
        await message.reply_text(
            f"Некорректный формат. {step['prompt']}",
            reply_markup=get_skip_keyboard(),
        )
        logger.warning(f"Некорректный ввод при редактировании поля '{field}': {text}")
        return EDIT_FIELD

    if field == "image":
        if not message.photo:
            await message.reply_text(
                "Пожалуйста, отправьте изображение или нажмите 'Пропустить'.",
                reply_markup=get_skip_keyboard(),
            )
            logger.warning("Пользователь не отправил изображение при редактировании.")
            return EDIT_FIELD
        state = _store_photo(user_data, message)
        if state == "started":
            await message.reply_text(
                "📷 Отправьте остальные фотографии альбома, "
                "затем нажмите «✅ Фото готово».",
                reply_markup=get_media_done_keyboard(),
            )
            logger.info(
                f"Начат сбор медиа-группы при редактировании: "
                f"{message.media_group_id}."
            )
            return EDIT_FIELD
        if state == "continued":
            return EDIT_FIELD  # запоздавшее фото альбома добавлено молча
        logger.info("Пользователь обновил изображение.")
    else:
        # Единое правило форматирования текстовых полей — как в визарде
        # (нормализованные дата/время/ссылка хранятся нормализованно,
        # форматированный ввод — дословно с HTML в user_data["formats"])
        if normalized is not None:
            user_data[field] = normalized
            _set_field_html(user_data, field, None)
        else:
            _store_text_field(user_data, field, text, message.entities)
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
    if field:
        _apply_skip(ctx.data(context), str(field))
        logger.info(f"Пользователь пропустил обновление поля '{field}'.")
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
    Отменяет процесс создания поста (незавершённый пост сохраняется).
    """
    await _save_wip_before_exit(update, context)
    await ctx.message(update).reply_text(
        "Создание поста отменено.", reply_markup=main_menu_keyboard()
    )
    logger.info("Пользователь отменил создание поста.")
    return ConversationHandler.END


async def start_during_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Команда /start ВНУТРИ диалога: незавершённый пост сохраняется, диалог
    завершается — дальше работает обычный /start. Пользователь явно выходит
    из создания, а не получает приветствие «поверх» активного диалога.
    """
    await _save_wip_before_exit(update, context)
    await start(update, context)
    return ConversationHandler.END


async def help_during_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Команда /help ВНУТРИ диалога: незавершённый пост сохраняется, показывается
    справка и диалог завершается — следующее сообщение пользователя не может
    неожиданно записаться в поле поста.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await _save_wip_before_exit(update, context)
    await help_command(update, context)
    return ConversationHandler.END


async def view_drafts_and_exit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Reply-кнопка «📝 Черновики», inline-кнопка «📂 Черновики» и команда
    /drafts ВНУТРИ диалога: незавершённый пост сохраняется, показывается
    список черновиков и диалог завершается — состояние не должно «висеть»
    после перехода в другой раздел (иначе следующий текст пользователя
    уходил бы на шаг диалога, который пользователь уже покинул).
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await _save_wip_before_exit(update, context)
    await view_drafts(update, context)
    return ConversationHandler.END


async def main_menu_during_creation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Кнопка «↩️ Главное меню» ВНУТРИ диалога: незавершённый пост сохраняется,
    диалог завершается — пользователь действительно оказывается в главном
    меню. Раньше кнопку обслуживал глобальный хендлер, и диалог оставался
    активным «поверх» главного меню (следующий текст уходил бы в поле поста).
    """
    await _save_wip_before_exit(update, context)
    await handle_main_menu_selection(update, context)
    return ConversationHandler.END


# Кнопки состояний диалога, нажатые когда диалог уже не активен.
# Глобальные callback'и (main_menu, delete_, draftpage_, editdraft_) сюда
# НЕ входят — ими занимаются свои хендлеры.
STALE_DIALOG_CALLBACK_PATTERN = (
    r"^(?:skip|media_done|save_draft|finish|cancel_edit|edit_[a-z_]+)$"
)


async def stale_dialog_action(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Ответ на устаревшую кнопку диалога («Пропустить», «Готово», поля
    редактирования…), нажатую после завершения диалога: раньше Telegram
    показывал спиннер, а ответа не было.

    Регистрируется СРАЗУ ПОСЛЕ ConversationHandler: при активном диалоге
    кнопку обслуживает состояние/entry/fallback, этот хендлер отвечает
    только когда диалога уже нет (или кнопка не относится к текущему
    состоянию).
    """
    await ctx.query(update).answer("Кнопка устарела: диалог уже завершён.")


def post_creation_handlers() -> list[BaseHandler]:
    """
    Возвращает список обработчиков для создания поста.
    """
    return [
        ConversationHandler(
            entry_points=[
                CommandHandler("create_post", start_post_creation),
                # Inline-кнопка «✏️ Создать пост» главного меню. Обязательна
                # как entry_point: только ConversationHandler умеет отслеживать
                # состояние диалога.
                CallbackQueryHandler(start_post_creation, pattern=r"^create_post$"),
                # Текстовая кнопка «Создать пост» (совместимость: набранный
                # текст работает так же, как и callback)
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
                        pattern="^(skip|save_draft|finish|edit_post|media_done)$",
                    ),
                ],
                EDIT_FIELD: [
                    # То же и в режиме редактирования поля: иначе нажатие
                    # «Черновики» записалось бы в редактируемое поле
                    MessageHandler(
                        filters.Regex("^📝 Черновики$"), view_drafts_and_exit
                    ),
                    # Кнопки превью, нажатые во время выбора поля. ВАЖНО стоять
                    # ДО handle_edit: его паттерн ^edit_.*$ ловит и «Редактировать»
                    CallbackQueryHandler(
                        handle_stale_preview_action,
                        pattern="^(edit_post|finish|save_draft)$",
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
            # Команды во время активного диалога завершают его (с сохранением
            # незавершённого поста): иначе команда обслуживалась бы глобально,
            # диалог оставался бы активным, и следующий текст пользователя
            # неожиданно уходил бы в поле поста
            fallbacks=[
                CommandHandler("cancel", cancel_creation),
                CommandHandler("start", start_during_creation),
                CommandHandler("help", help_during_creation),
                CommandHandler("drafts", view_drafts_and_exit),
                # Кнопки главного меню, нажатые во время диалога: WIP
                # сохраняется, диалог завершается — иначе состояние оставалось
                # бы активным «поверх» меню, а список/справка молча теряли бы
                # незавершённый пост
                CallbackQueryHandler(main_menu_during_creation, pattern=r"^main_menu$"),
                CallbackQueryHandler(view_drafts_and_exit, pattern=r"^drafts$"),
                CallbackQueryHandler(help_during_creation, pattern=r"^help$"),
            ],
            allow_reentry=True,
        ),
        # Кнопки состояний диалога после его завершения: раньше повторное
        # нажатие оставляло пользователя со спиннером. Стоит ПОСЛЕ
        # ConversationHandler: активный диалог обслуживает свои состояния
        CallbackQueryHandler(
            stale_dialog_action, pattern=STALE_DIALOG_CALLBACK_PATTERN
        ),
    ]
