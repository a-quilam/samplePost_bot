# handlers/announcement_wizard.py
"""
Telegram-интерфейс адаптивного мастера анонсов Stage 27B/27C.

Мастер использует только сервисы Stage 27A и компилятор Stage 24:

- место и вариант названия выбираются в существующем справочнике;
- шаблон фиксируется независимым снимком, повторно снимок не создаётся;
- вопросы строит `announcement_store.build_announcement_questions`, а не
  отдельный алгоритм Telegram-слоя;
- значения и дополнительные кирпичики сохраняются по `brick_id`;
- предпросмотр собирается `build_announcement_preview` из снимка;
- фотографии — отдельная коллекция вложений анонса, не кирпичики;
- публикация замораживает готовый HTML/фото и отправляет их единым
  доставщиком `delivery.send_announcement_post` (тот же формат и прогресс,
  что у остальных сценариев).

В `user_data` хранится только навигационное состояние интерфейса. Место,
шаблон, снимок, ответы, extras, фото, этап, revision и прогресс публикации
живут в SQLite. Поэтому рестарт процесса не теряет работу: черновик
открывается из базы в «📂 Мои анонсы». Кнопки содержат draft_id и
revision; устаревший экран не перезаписывает данные.

Легаси-мастер, его WIP и формат публикации удалены в S5 — единственный
конструктор и единственный доставщик.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import delivery
from announcement_store import (
    FIELD_STATUS_KNOWN,
    FIELD_STATUS_MISSING,
    FIELD_STATUS_SKIPPED,
    FIELD_STATUS_VALID,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_PARTIAL,
    PUBLICATION_STATUS_SENDING,
    PUBLICATION_STATUS_SENT,
    AnnouncementActorError,
    AnnouncementError,
    AnnouncementFieldState,
    AnnouncementNotFoundError,
    AnnouncementPreview,
    add_announcement_extra,
    begin_announcement_publication,
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_render_payload,
    choose_announcement_template,
    clear_announcement_photos,
    confirm_announcement_place,
    get_announcement_draft,
    list_announcement_drafts,
    mark_announcement_publication_failed,
    mark_announcement_publication_sent,
    record_announcement_publication_progress,
    remove_announcement_extra,
    remove_announcement_photo,
    reset_failed_announcement_publication,
    resume_announcement_publication,
    save_announcement_value,
    set_announcement_stage,
    start_announcement_wizard,
)
from announcement_wizard import (
    EXTRA_POSITION_END,
    EXTRA_POSITION_START,
    WizardStage,
    answer_payload_to_raw,
)
from brick_store import BrickError, list_available_bricks
from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_TIME,
    BRICK_TYPE_TITLES,
    BrickValueResult,
    BrickValueStatus,
)
from database import SessionLocal
from handlers import wizard_live, wizard_photos, wizard_questions
from place_store import (
    get_place_details,
    list_places,
    search_places,
)
from template_compiler import (
    PLACEHOLDER_TEXT,
    TemplateCompileError,
    announcement_text_limit,
    check_announcement_length,
)
from template_store import (
    TemplateError,
    get_standard_template,
    list_personal_templates,
)
from template_types import (
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)
from utils import calendar_view, keyboard_layouts, screen, ui_humor
from utils import tg_context as ctx
from utils.parsers import app_today
from utils.place_labels import place_title
from utils.tg_html import capture_message_text, escape_html
from utils.ui_texts import tr, tr_safe

# Poster 2.0 S3 п.1: вид вопроса/календаря вынесен в `wizard_questions`,
# поведение прежнее. Имена с подчёркиванием сохранены как локальные
# адаптеры: оркестратор диалога не переписывается целиком, а тесты и
# соседние экраны продолжают работать без изменений.
_question_text = wizard_questions.question_text
_type_hint = wizard_questions.type_hint
_date_rules_hint = wizard_questions.date_rules_hint
_number_rules_hint = wizard_questions.number_rules_hint
_choice_options = wizard_questions.choice_options
_selected_choices = wizard_questions.selected_choices
_field_status_text = wizard_questions.field_status_text
_field_value_text = wizard_questions.field_value_text
_parse_rule_date = wizard_questions.parse_rule_date
_calendar_bounds = wizard_questions.calendar_bounds
_day_rows = wizard_questions.day_rows

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "announcement_wizard"

AW_PLACE, AW_TEMPLATE, AW_QUESTIONS, AW_EXTRAS, AW_PREVIEW = range(5)

PLACE_PAGE_SIZE = 8
TEMPLATE_PAGE_SIZE = 6
EXTRA_PAGE_SIZE = 8
PHOTO_PAGE_SIZE = 8
PREVIEW_VISIBLE_LIMIT = 1800
FIELD_DISPLAY_LIMIT = 120
MAX_PREVIEW_FIELDS = 12
MAX_PREVIEW_EXTRAS = 6

STAGE_LABELS = {
    WizardStage.PLACE.value: "место",
    WizardStage.TEMPLATE.value: "шаблон",
    WizardStage.QUESTIONS.value: "вопросы",
    WizardStage.EXTRAS.value: "доп. поля",
    WizardStage.PREVIEW.value: "обзор",
    WizardStage.COMPLETED.value: "опубликован",
}

SECTION_LABELS = {
    TEMPLATE_SECTION_HEADER: "шапка",
    TEMPLATE_SECTION_BODY: "тело",
    TEMPLATE_SECTION_FOOTER: "подвал",
}

POSITION_LABELS = {
    EXTRA_POSITION_START: "в начало",
    EXTRA_POSITION_END: "в конец",
}

SCOPE_LABELS = {
    BRICK_SCOPE_SYSTEM: "системный",
    BRICK_SCOPE_PLACE: "места",
    BRICK_SCOPE_PERSONAL: "личный",
}

STALE_TEXT = tr("errors.stale.wizard")
EXPIRED_TEXT = tr("errors.expired.wizard")
PRIVATE_CHAT_ONLY = tr("errors.private_chat_only")


def _user_id(update: Update) -> int:
    return ctx.user(update).id


def _is_private_chat(update: Update) -> bool:
    """
    Анонс доставляется только в личный чат автора.

    Мастер можно открыть и в группе (`/new_wizard`), но автоматическая
    выдача готового текста туда превратила бы личный анонс в сообщение для
    посторонних. Проверка стоит на каждом пути отправки, а не только на
    входе: callback может прийти из чата, где диалог начинался иначе.
    """
    chat = update.effective_chat
    if chat is None:
        return False
    return chat.type == ChatType.PRIVATE


def _ui(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    data = ctx.data(context)
    ui = data.get("announcement_ui")
    if not isinstance(ui, dict):
        ui = _fresh_ui()
        data["announcement_ui"] = ui
    return ui


def _fresh_ui() -> dict[str, Any]:
    return {
        "draft_id": None,
        "revision": None,
        "home_page": 0,
        "template_page": 0,
        "extras_page": 0,
        "question_index": 0,
        "active_brick_id": None,
        "active_source": None,
        "editing": False,
        "photos_page": 0,
        "photo_add": False,
        "place": {
            "phase": "home",
            "query": "",
            "page": 0,
            "place_id": None,
            "name_id": None,
            "return_phase": "hub",
            "return_page": 0,
            "idempotency_key": None,
        },
        "extra_flow": None,
    }


def _clear_ui(context: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.data(context).pop("announcement_ui", None)


def _reset_for_place(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    ui = _fresh_ui()
    ui["place"]["phase"] = "hub"
    ui["place"]["idempotency_key"] = uuid.uuid4().hex
    ctx.data(context)["announcement_ui"] = ui
    return ui


def _set_details(ui: dict[str, Any], details) -> None:
    ui["draft_id"] = details.draft_id
    ui["revision"] = details.revision


def _clear_active(ui: dict[str, Any]) -> None:
    ui["active_brick_id"] = None
    ui["active_source"] = None
    ui["editing"] = False
    ui["photo_add"] = False


def _short(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _h(value: object) -> str:
    return escape_html(str(value if value is not None else ""))


def _place_lines(details: object) -> list[str]:
    """
    Строки «Место: …» для экранов анонса — только если площадка реально есть.

    У личного анонса площадки нет по схеме (`ck_announcement_drafts_origin_place`:
    `origin='personal'` требует `place_id IS NULL` и `place_display_name IS NULL`).
    Печатать в этом случае «Место: » с пустым значением нельзя: пользователь
    читает это как потерянные данные. Единый вариант для всех пяти экранов
    (шаг выбора шаблона, обзор, отправка, доставлено, отказ в неличном чате) —
    строки нет вовсе. Возвращается список 0/1 строк, чтобы вызывающий вставлял
    его через `extend` без ветвлений.
    """
    name = getattr(details, "place_display_name", None)
    if name is None or not str(name).strip():
        return []
    return [f"Место: {_h(name)}"]


async def _answer(
    update: Update, text: str | None = None, *, alert: bool = False
) -> None:
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    try:
        await query.answer(text or "", show_alert=alert)
    except Exception as exc:  # noqa: BLE001 — ответ на callback не должен ломать экран
        logger.debug("Не удалось ответить на callback: %s", exc)


async def _render(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    user_data = ctx.data(context)
    user_id = _user_id(update)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    await screen.render_screen(context, user_data, ctx.chat(update).id, text, keyboard)
    screen.save_screen_ref(user_id, user_data, ctx.chat(update).id)


async def _reanchor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await screen.reanchor_screen(update, context)


async def _sync_live(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> None:
    """
    Обновляет живой верхний блок ПЕРЕД отрисовкой нижнего экрана (S3 п.2).

    Вызывается на всех нижних экранах конструктора (вопрос, extras, шаг
    фото, обзор): иначе при шаблоне без кирпичиков верхний блок не появился
    бы вовсе, а после последнего ответа остался бы устаревшим. Уже
    доставленный анонс не трогаем: там верхний блок — сама доставка.
    """
    if details is None or details.publication_status is not None:
        return
    user_data = ctx.data(context)
    try:
        block = await wizard_live.sync_live_block(
            context.bot,
            ctx.chat(update).id,
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            known_ids=wizard_live.current_live_ids(details),
            has_photos=bool(details.photos),
            # S4: живой блок — медиаблок. Фото и прогресс частичной отправки
            # передаются явно: без них состав блока не совпал бы с payload
            # финальной отправки, и готовый пост появился бы вторым.
            photos=tuple(details.photos),
            user_data=user_data,
        )
    except Exception as exc:  # noqa: BLE001 — живой блок не должен ломать диалог
        logger.info("Живой блок #%s не обновлён: %s", details.draft_id, exc)
        return
    wizard_live.remember_live_state(user_data, draft_id=details.draft_id, block=block)
    await _keep_live_above_screen(update, context, block)


async def _keep_live_above_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    block,
) -> None:
    """
    Гарантирует порядок «анонс сверху, служебный экран снизу» (S3 §4).

    Telegram не умеет переставлять сообщения, поэтому исправляется только
    хронология: если живой блок оказался НИЖЕ служебного экрана (первый вход
    `pl_pick`/`tp_pick`, пересоздание живого блока после потери сообщения),
    наш собственный экран удаляется, и следующий `_render` создаёт его заново
    уже под анонсом.

    Удаляется только своё сообщение-экран из `screen_ref` (`user_data` или
    БД) и только в этом же чате: чужие сообщения и посты это не затрагивает.
    Отказ Telegram на удаление не прерывает сценарий (экран остаётся) — тогда
    порядок сохранить нельзя, и это единственное известное ограничение
    fallback'а; при первом входе сообщение свежее и удаляется штатно.
    """
    ids = tuple(getattr(block, "message_ids", ()) or ())
    if not ids:
        return
    user_data = ctx.data(context)
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, _user_id(update))
    if ref is None:
        return
    chat_id = ctx.chat(update).id
    if int(ref.get("chat_id") or 0) != int(chat_id):
        return
    # Живой блок выше экрана — порядок уже правильный, трогать нечего.
    if int(ref.get("message_id") or 0) > min(ids):
        return
    try:
        await context.bot.delete_message(chat_id, ref["message_id"])
    except Exception as exc:  # noqa: BLE001 — удаление best-effort
        logger.info(
            "Служебный экран %s не удалён (%s): живой блок останется ниже.",
            ref["message_id"],
            exc,
        )
        return
    user_data.pop("screen", None)
    screen.save_screen_ref(_user_id(update), user_data, chat_id)
    logger.info("Служебный экран пересоздаётся ниже живого блока анонса.")


def _end_foreign_conversations(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Явный вход в новый мастер завершает другие диалоги.

    Иначе старый девятишаговый ConversationHandler остался бы активным и
    перехватывал бы следующий текст. WIP при этом не удаляется: завершение
    диалога — не удаление черновика.
    """
    application = getattr(context, "application", None)
    if application is None:
        return
    for group in application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            if getattr(handler, "name", None) == CONVERSATION_NAME:
                continue
            try:
                key = handler._get_key(update)  # noqa: SLF001 — завершение диалога
                if handler._conversations.get(key) is not None:  # noqa: SLF001
                    handler._update_state(ConversationHandler.END, key)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                logger.debug("Не удалось завершить посторонний диалог: %s", exc)


def _clear_other_sessions(user_id: int) -> None:
    from admin_state import clear_admin_session
    from brick_state import clear_brick_session
    from place_state import clear_place_session
    from template_state import clear_template_session

    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)


def _session_details(user_id: int, draft_id: int):
    with SessionLocal() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
        )


def _active_place_draft(user_id: int, place_id: int):
    """
    Незавершённый черновик этого пользователя по этой площадке (U4b).

    Повторное открытие ссылки на площадку должно ПРОДОЛЖАТЬ уже начатый
    анонс, а не создавать второй черновик той же площадки. Завершённые
    (отправленный анонс, `stage=completed`) и чужие черновики не
    считаются: ссылка на площадку после отправки открывает новый анонс,
    как и раньше. Из нескольких незавершённых берётся самый свежий.
    """
    with SessionLocal() as session:
        drafts = list_announcement_drafts(session, actor_telegram_user_id=user_id)
    for draft in drafts:
        if draft.place_id != place_id:
            continue
        if draft.stage == WizardStage.COMPLETED:
            continue
        if draft.publication_status == PUBLICATION_STATUS_SENT:
            continue
        return draft
    return None


def _resume_question_brick(user_id: int, details) -> int | None:
    """
    Вопрос, на котором автор остановился: последний заполненный.

    Так повторный вход по ссылке показывает уже введённое значение
    («Текущее значение: …») вместо пустого вопроса, а «Далее» ведёт к
    следующему незаполненному полю.
    """
    if details.stage != WizardStage.QUESTIONS:
        return None
    answered: int | None = None
    for field in _askable_fields(_load_fields(user_id, details.draft_id)):
        if field.status in {FIELD_STATUS_VALID, FIELD_STATUS_SKIPPED}:
            answered = field.brick_id
    return answered


async def resume_place_announcement(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
) -> int | None:
    """
    Продолжает незавершённый черновик площадки, если он есть (U4b).

    Возвращает состояние диалога или None — тогда вызывающий открывает
    новый черновик обычным путём. Используется входом по ссылке
    `/start <token>`: та же площадка и уже начатый анонс → продолжение
    с сохранёнными данными.
    """
    user_id = _user_id(update)
    draft = _active_place_draft(user_id, place_id)
    if draft is None:
        return None
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(user_id)
    await _reanchor(update, context)
    # R1, дефект №23: вход в конструктор — актуальный UI, без reply-клавиатур.
    await screen.drop_legacy_reply_keyboard(update, context)
    return await _open_draft(
        update,
        context,
        draft.draft_id,
        active_brick_id=_resume_question_brick(user_id, draft),
    )


def _guard_details(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
) -> tuple[dict[str, Any], Any] | None:
    user_id = _user_id(update)
    ui = _ui(context)
    if ui.get("draft_id") != draft_id or ui.get("revision") != revision:
        return None
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return None
    if details.revision != revision:
        ui["revision"] = details.revision
        return None
    ui["revision"] = details.revision
    return ui, details


async def _stale(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str = STALE_TEXT,
) -> int:
    await _answer(update, text, alert=True)
    return await _resume_current(update, context, notice=text)


async def _expired(update: Update) -> int:
    await _answer(update, EXPIRED_TEXT, alert=True)
    return ConversationHandler.END


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    from handlers.main_menu import main_menu_keyboard

    return main_menu_keyboard()


def _edit_blocked(details) -> bool:
    """
    Идёт отправка: данные менять нельзя, пока она не завершилась.

    Доставленный анонс (`sent`) редактируем: следующая сборка заменит
    прошлую версию.
    """
    return details.publication_status in {
        PUBLICATION_STATUS_SENDING,
        PUBLICATION_STATUS_PARTIAL,
        PUBLICATION_STATUS_FAILED,
    }


def _draft_status_label(draft) -> str:
    status = getattr(draft, "publication_status", None)
    if status == PUBLICATION_STATUS_SENDING:
        return "отправка"
    if status == PUBLICATION_STATUS_PARTIAL:
        return "частично отправлен"
    if status == PUBLICATION_STATUS_FAILED:
        return "ошибка отправки"
    if status == PUBLICATION_STATUS_SENT:
        return "готовый анонс отправлен"
    return STAGE_LABELS.get(str(getattr(draft, "stage", "")), "черновик")


def _draft_display_name(draft) -> str:
    """
    Человеческое название анонса для «Мои анонсы» (без HTML-экранирования).

    У анонса по площадке показываем подтверждённое место (оно и есть название
    анонса), у анонса по личному шаблону площадки нет — имя шаблона из
    снимка, иначе нейтральное «Анонс». Экранирует вызывающий слой: текст
    экрана — HTML, подпись кнопки — обычный текст.
    """
    snapshot = getattr(draft, "template_snapshot", None)
    name = getattr(draft, "place_display_name", None)
    if not name and snapshot is not None:
        name = getattr(snapshot, "source_name", None)
    if not name:
        origin = getattr(draft, "origin", None)
        name = "Анонс по личному шаблону" if origin == "personal" else "Анонс"
    return str(name)


def _home_content(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup, int]:
    try:
        with SessionLocal() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=user_id)
    except (AnnouncementActorError, AnnouncementError):
        drafts = []
    total = len(drafts)
    page = max(0, page)
    start = page * TEMPLATE_PAGE_SIZE
    if start >= total and total > 0:
        page = 0
        start = 0
    shown = drafts[start : start + TEMPLATE_PAGE_SIZE]

    lines = [
        tr("buttons.main.announcements"),
        "",
        "Незавершённые анонсы можно продолжить с того же шага, завершённые "
        "остаются в истории и не удаляются при создании новых. Готовый анонс "
        "приходит в этот личный чат отдельным сообщением — ни в каналы, ни "
        "другим людям он не отправляется.",
    ]
    if shown:
        lines.extend(["", "Ваши анонсы:"])
        for number, draft in enumerate(shown, start=1):
            stage = _draft_status_label(draft)
            lines.append(f"{number}. {_h(_draft_display_name(draft))} — {stage}")
    elif drafts:
        lines.extend(["", "На этой странице анонсов нет."])
    else:
        lines.extend(
            [
                "",
                "Анонсов пока нет. Начните с кнопки «📍 Создать для площадки» "
                "или «📝 Создать по личному шаблону».",
            ]
        )

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Новый анонс", callback_data="aw_new")]
    ]
    for index, draft in enumerate(shown):
        stage = _draft_status_label(draft)
        label = f"▶️ {index + 1}. {_short(_draft_display_name(draft), 38)}"
        if stage:
            label += f" — {stage}"
        buttons.append(
            [
                InlineKeyboardButton(
                    _short(label, 62), callback_data=f"aw_open:{draft.draft_id}"
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ Страница", callback_data=f"aw_home_page:{page - 1}"
            )
        )
    if start + len(shown) < total:
        nav.append(
            InlineKeyboardButton(
                "Страница ▶️", callback_data=f"aw_home_page:{page + 1}"
            )
        )
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons), page


async def _render_home(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["draft_id"] = None
    ui["revision"] = None
    _clear_active(ui)
    page = int(ui.get("home_page") or 0)
    text, keyboard, page = _home_content(_user_id(update), page)
    ui["home_page"] = page
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, keyboard)
    return AW_PLACE


def _place_hub_content() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📍 Шаг 1. Выберите место\n\n"
        "Поиск учитывает канонические и альтернативные названия. "
        "Новое место здесь не создаётся."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.places.search"), callback_data="aw_place_search"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.places.all"), callback_data="aw_place_all:0"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"), callback_data="aw_place_back"
                )
            ],
        ]
    )
    return text, keyboard


async def _render_place_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "hub"
    ui["place"]["page"] = 0
    text, keyboard = _place_hub_content()
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, keyboard)
    return AW_PLACE


def _clamped_page(page: int, total: int, size: int) -> int:
    if total <= 0:
        return 0
    last = (total - 1) // size
    return max(0, min(page, last))


def _place_buttons(
    places: list[tuple[int, str]], *, prefix: str, page: int, total: int
) -> list[list[InlineKeyboardButton]]:
    # U3a №13: строки списка и пагинация — общие шаблоны компоновки.
    rows = keyboard_layouts.list_rows(
        (
            keyboard_layouts.button(
                _short(f"📍 {name}", 60), f"aw_place_open:{place_id}"
            ),
            None,
        )
        for place_id, name in places
    )
    size = PLACE_PAGE_SIZE
    rows += keyboard_layouts.nav_row(
        keyboard_layouts.button("◀️", f"{prefix}:{page - 1}") if page > 0 else None,
        (
            keyboard_layouts.button("▶️", f"{prefix}:{page + 1}")
            if (page + 1) * size < total
            else None
        ),
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    return rows


async def _render_all_places(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "all"
    ui["place"]["page"] = max(0, page)
    with SessionLocal() as session:
        places, total = list_places(
            session,
            offset=ui["place"]["page"] * PLACE_PAGE_SIZE,
            limit=PLACE_PAGE_SIZE,
        )
    page = _clamped_page(ui["place"]["page"], total, PLACE_PAGE_SIZE)
    if page != ui["place"]["page"]:
        ui["place"]["page"] = page
        with SessionLocal() as session:
            places, total = list_places(
                session,
                offset=page * PLACE_PAGE_SIZE,
                limit=PLACE_PAGE_SIZE,
            )
    lines = [
        tr("buttons.places.all"),
        "",
        (
            f"Всего: {total}. Страница {page + 1}."
            if total
            else "В справочнике пока нет мест."
        ),
    ]
    rows = _place_buttons(
        [
            (item.place_id, place_title(item.canonical_name, item.city))
            for item in places
        ],
        prefix="aw_place_all",
        page=page,
        total=total,
    )
    if not places:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    tr("buttons.places.search"), callback_data="aw_place_search"
                )
            ],
        )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_search_results(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    state["phase"] = "results"
    query = str(state.get("query") or "")
    with SessionLocal() as session:
        results = search_places(session, query)
    total = len(results)
    page = _clamped_page(max(0, page), total, PLACE_PAGE_SIZE)
    state["page"] = page
    shown = results[page * PLACE_PAGE_SIZE : (page + 1) * PLACE_PAGE_SIZE]
    lines = [
        "🔎 Результаты поиска",
        "",
        f"Запрос: {_h(query)}",
        f"Найдено мест: {total}.",
    ]
    for result in shown:
        title = _h(place_title(result.canonical_name, result.city))
        if result.matched_name != result.canonical_name:
            lines.append(f"• {title} — совпадение: " f"{_h(result.matched_name)}")
        else:
            lines.append(f"• {title}")
    if not results:
        lines.extend(
            [
                "",
                "Ничего не найдено. Измените запрос или посмотрите все места.",
            ]
        )
    rows = [
        [
            InlineKeyboardButton(
                _short(f"📍 {place_title(result.canonical_name, result.city)}", 60),
                callback_data=f"aw_place_open:{result.place_id}",
            )
        ]
        for result in shown
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("◀️", callback_data=f"aw_place_results:{page - 1}")
        )
    if (page + 1) * PLACE_PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton("▶️", callback_data=f"aw_place_results:{page + 1}")
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("🔎 Изменить запрос", callback_data="aw_place_search")]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.places.all"), callback_data="aw_place_all:0")]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_search_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    ui["place"]["phase"] = "search_input"
    text = (
        "🔎 Поиск места\n\n"
        "Отправьте название или его часть одним сообщением. Поиск работает "
        "по каноническим и альтернативным названиям."
    )
    body = f"{notice}\n\n{text}" if notice else text
    await _render(
        update,
        context,
        body,
        InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.nav.back"), callback_data="aw_place_back"
                    )
                ]
            ]
        ),
    )
    return AW_PLACE


def _place_card_text(details, selected_name_id: int | None) -> str:
    lines = [
        "📍 Проверьте место",
        "",
        f"Название: {_h(details.canonical_name)}",
        f"Город: {_h(details.city) if details.city else '—'}",
        f"Адрес: {_h(details.address) if details.address else 'не указан'}",
        f"Ссылка: {_h(details.url) if details.url else 'не указана'}",
        "",
        "Названия:",
    ]
    for name in details.names:
        marker = "✅" if name.id == selected_name_id else "•"
        suffix = " (каноническое)" if name.is_canonical else " (альтернативное)"
        lines.append(f"{marker} {_h(name.name)}{suffix}")
    lines.extend(
        [
            "",
            "Известные адрес и ссылка будут взяты из справочника без повторных "
            "вопросов.",
        ]
    )
    return "\n".join(lines)


async def _render_place_card(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    place_id = state.get("place_id")
    name_id = state.get("name_id")
    if not place_id:
        return await _render_place_hub(update, context, notice=notice)
    with SessionLocal() as session:
        details = get_place_details(session, int(place_id))
    if details is None:
        state["phase"] = "hub"
        return await _render_place_hub(
            update, context, notice="Место больше не доступно."
        )
    if name_id is None:
        name_id = details.canonical_name_id
        state["name_id"] = name_id
    known_name_ids = {name.id for name in details.names}
    if int(name_id) not in known_name_ids:
        name_id = details.canonical_name_id
        state["name_id"] = name_id
    state["phase"] = "confirm"
    text = _place_card_text(details, int(name_id))
    body = f"{notice}\n\n{text}" if notice else text
    rows = [
        [
            InlineKeyboardButton(
                "✅ Подтвердить",
                callback_data=(f"aw_place_confirm:{details.place_id}:{int(name_id)}"),
            )
        ]
    ]
    if len(details.names) > 1:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔤 Выбрать название",
                    callback_data=f"aw_place_names:{details.place_id}:0",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


async def _render_place_names(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    place_id = state.get("place_id")
    if not place_id:
        return await _render_place_hub(update, context, notice=notice)
    with SessionLocal() as session:
        details = get_place_details(session, int(place_id))
    if details is None:
        return await _render_place_hub(
            update, context, notice="Место больше не доступно."
        )
    page = _clamped_page(max(0, page), len(details.names), 8)
    state["phase"] = "names"
    selected = state.get("name_id") or details.canonical_name_id
    names = details.names[page * 8 : (page + 1) * 8]
    lines = [
        "🔤 Название места",
        "",
        f"Место: {_h(details.canonical_name)}",
        "",
        "Выберите каноническое или альтернативное название. Это не создаёт "
        "новое место.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for name in names:
        marker = "✅" if name.id == selected else "•"
        suffix = "" if name.is_canonical else " (альт.)"
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"{marker} {name.name}{suffix}", 60),
                    callback_data=(
                        f"aw_place_name_pick:{details.place_id}:{name.id}:{page}"
                    ),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=f"aw_place_names:{details.place_id}:{page - 1}",
            )
        )
    if (page + 1) * 8 < len(details.names):
        nav.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=f"aw_place_names:{details.place_id}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_place_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_PLACE


def _place_return_phase(ui: dict[str, Any]) -> str:
    phase = str(ui["place"].get("return_phase") or "hub")
    if phase not in {"hub", "all", "results"}:
        return "hub"
    return phase


async def _place_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ui = _ui(context)
    state = ui["place"]
    phase = state.get("phase")
    if phase == "names":
        return await _render_place_card(update, context)
    if phase == "confirm":
        if ui.get("draft_id"):
            return await _render_home(update, context)
        target = _place_return_phase(ui)
        if target == "results":
            return await _render_search_results(
                update, context, int(state.get("return_page") or 0)
            )
        if target == "all":
            return await _render_all_places(
                update, context, int(state.get("return_page") or 0)
            )
        return await _render_place_hub(update, context)
    if phase in {"results", "all", "search_input"}:
        return await _render_place_hub(update, context)
    return await _render_home(update, context)


async def _open_draft(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    *,
    active_brick_id: int | None = None,
    notice: str | None = None,
) -> int:
    user_id = _user_id(update)
    try:
        details = _session_details(user_id, draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        ui = _fresh_ui()
        ctx.data(context)["announcement_ui"] = ui
        await _answer(update, "Черновик не найден или недоступен.", alert=True)
        return await _render_home(
            update, context, notice="Черновик не найден или недоступен."
        )

    ui = _fresh_ui()
    _set_details(ui, details)
    # U4b: повторное открытие ссылки возвращает к вопросу с последним
    # сохранённым ответом — человек видит свои данные, а не пустой вопрос.
    if active_brick_id is not None:
        ui["active_brick_id"] = active_brick_id
    ctx.data(context)["announcement_ui"] = ui
    return await _render_draft_stage(update, context, details, notice=notice)


async def _render_draft_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Экран сохранённого черновика по его этапу (общий для всех входов)."""
    ui = _ui(context)
    stage = details.stage
    if stage == WizardStage.PLACE:
        ui["place"].update(
            {
                "phase": "confirm",
                "place_id": details.place_id,
                "name_id": details.place_name_id,
                "return_phase": "hub",
            }
        )
        return await _render_place_card(update, context, notice=notice)
    if stage == WizardStage.TEMPLATE:
        return await _render_template_screen(update, context, details, notice=notice)
    if stage == WizardStage.QUESTIONS:
        return await _enter_questions(update, context, details, notice=notice)
    if stage == WizardStage.EXTRAS:
        return await _render_extras(update, context, details, notice=notice)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    return await _render_preview(update, context, details, notice=notice)


async def _confirm_place(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
    name_id: int,
) -> int:
    ui = _ui(context)
    state = ui["place"]
    if state.get("phase") != "confirm":
        return await _stale(update, context)
    if int(state.get("place_id") or 0) != place_id:
        return await _stale(update, context)
    if int(state.get("name_id") or 0) != name_id:
        return await _stale(update, context)

    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            if ui.get("draft_id"):
                details = get_announcement_draft(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=int(ui["draft_id"]),
                )
                if details.place_id != place_id:
                    return await _stale(update, context)
                if details.stage == WizardStage.PLACE:
                    details = confirm_announcement_place(
                        session,
                        actor_telegram_user_id=user_id,
                        draft_id=details.draft_id,
                        expected_revision=details.revision,
                    )
            else:
                key = state.get("idempotency_key") or uuid.uuid4().hex
                state["idempotency_key"] = key
                details = start_announcement_wizard(
                    session,
                    actor_telegram_user_id=user_id,
                    place_id=place_id,
                    place_name_id=name_id,
                    idempotency_key=key,
                )
                details = confirm_announcement_place(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    except AnnouncementActorError:
        await _answer(
            update,
            "Профиль ещё не зарегистрирован. Отправьте /start и повторите.",
            alert=True,
        )
        return ConversationHandler.END

    _set_details(ui, details)
    state["phase"] = "confirmed"
    await _answer(update, "Место подтверждено.")
    return await _render_template_screen(update, context, details)


def _template_description(details) -> str:
    version = details.current_version
    count = len(version.bindings)
    sections = [
        SECTION_LABELS.get(name, name)
        for name, text in (
            (TEMPLATE_SECTION_HEADER, version.header),
            (TEMPLATE_SECTION_BODY, version.body),
            (TEMPLATE_SECTION_FOOTER, version.footer),
        )
        if text.strip()
    ]
    part = ", ".join(sections) if sections else "без секций"
    return f"{part}; кирпичиков: {count}"


def _selected_template_text(details) -> str:
    snapshot = details.template_snapshot
    assert snapshot is not None
    kind = "стандартный" if snapshot.source_kind == "standard" else "личный"
    name = snapshot.source_name or (
        "Стандартный шаблон места"
        if snapshot.source_kind == "standard"
        else "Личный шаблон"
    )
    sections = []
    for section in (
        TEMPLATE_SECTION_HEADER,
        TEMPLATE_SECTION_BODY,
        TEMPLATE_SECTION_FOOTER,
    ):
        if snapshot.sections.get(section, "").strip():
            sections.append(SECTION_LABELS[section])
    part = ", ".join(sections) if sections else "без секций"
    return (
        f"🧩 Выбранный шаблон: {_h(name)}\n"
        f"Тип: {kind}; версия снимка: {snapshot.source_template_version}.\n"
        f"Секции: {part}; уникальных кирпичиков: "
        f"{len(snapshot.brick_ids)}.\n\n"
        "Снимок уже сохранён. Изменения исходного шаблона не влияют на этот "
        "анонс; повторный выбор шаблона недоступен."
    )


async def _render_template_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    if details.template_snapshot is not None:
        text = _selected_template_text(details)
        body = f"{notice}\n\n{text}" if notice else text
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➡️ Продолжить",
                        callback_data=(
                            f"aw_cont:{details.draft_id}:{details.revision}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📝 К списку черновиков", callback_data="aw_home"
                    )
                ],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]
        )
        _set_template_phase(ui)
        await _render(update, context, body, keyboard)
        return AW_TEMPLATE

    _set_template_phase(ui)
    with SessionLocal() as session:
        try:
            standard = get_standard_template(
                session,
                _user_id(update),
                details.place_id,
            )
        except TemplateError:
            standard = None
        personal = list_personal_templates(session, _user_id(update))

    page = int(ui.get("template_page") or 0)
    page = _clamped_page(page, len(personal), TEMPLATE_PAGE_SIZE)
    ui["template_page"] = page
    shown = personal[page * TEMPLATE_PAGE_SIZE : (page + 1) * TEMPLATE_PAGE_SIZE]

    lines = [
        "🧩 Шаг 2. Выберите шаблон",
        "",
        *_place_lines(details),
        "",
        "Стандартный шаблон места:",
        (
            f"• {_h(standard.display_name)} — {_h(_template_description(standard))}"
            if standard is not None
            else "• не настроен"
        ),
        "",
        "Личные шаблоны:",
    ]
    if shown:
        for number, template in enumerate(shown, start=page * TEMPLATE_PAGE_SIZE + 1):
            lines.append(
                f"{number}. {_h(template.display_name)} — "
                f"{_h(_template_description(template))}"
            )
    else:
        lines.append("• нет личных шаблонов")
    lines.extend(
        [
            "",
            "Шаблон фиксируется независимым снимком. Позже его нельзя заменить.",
        ]
    )

    rows: list[list[InlineKeyboardButton]] = []
    if standard is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"✅ {standard.display_name}", 60),
                    callback_data=(
                        f"aw_template:{details.draft_id}:"
                        f"{details.revision}:{standard.template_id}"
                    ),
                )
            ]
        )
    for template in shown:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"📄 {template.display_name}", 60),
                    callback_data=(
                        f"aw_template:{details.draft_id}:"
                        f"{details.revision}:{template.template_id}"
                    ),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(
                    f"aw_tplpage:{details.draft_id}:{details.revision}:{page - 1}"
                ),
            )
        )
    if (page + 1) * TEMPLATE_PAGE_SIZE < len(personal):
        nav.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=(
                    f"aw_tplpage:{details.draft_id}:{details.revision}:{page + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("📝 К списку черновиков", callback_data="aw_home")]
    )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_TEMPLATE


def _set_template_phase(ui: dict[str, Any]) -> None:
    ui["place"]["phase"] = "template"


async def _select_template(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    template_id: int,
) -> int:
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return await _stale(update, context)
    ui, details = guarded
    if details.template_snapshot is not None:
        if details.template_id == template_id:
            return await _render_template_screen(update, context, details)
        return await _stale(update, context)

    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            standard = None
            try:
                standard = get_standard_template(session, user_id, details.place_id)
            except TemplateError:
                pass
            personal = list_personal_templates(session, user_id)
            allowed_ids = {template.template_id for template in personal}
            if standard is not None:
                allowed_ids.add(standard.template_id)
            if template_id not in allowed_ids:
                return await _stale(
                    update,
                    context,
                    "Этот шаблон недоступен. Выберите шаблон из списка.",
                )
            updated = choose_announcement_template(
                session,
                actor_telegram_user_id=user_id,
                draft_id=draft_id,
                template_id=template_id,
                expected_revision=details.revision,
            )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    except TemplateError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    _set_details(ui, updated)
    await _answer(update, "Шаблон выбран, снимок сохранён.")
    return await _enter_questions(update, context, updated)


def _askable_fields(
    fields: tuple[AnnouncementFieldState, ...],
) -> list[AnnouncementFieldState]:
    return [field for field in fields if field.status != FIELD_STATUS_KNOWN]


def _first_missing_index(fields: list[AnnouncementFieldState]) -> int:
    for index, field in enumerate(fields):
        if field.status == FIELD_STATUS_MISSING:
            return index
    return -1


def _load_fields(user_id: int, draft_id: int):
    with SessionLocal() as session:
        return build_announcement_fields(
            session,
            actor_telegram_user_id=user_id,
            draft_id=draft_id,
            today=app_today(),
        )


def _question_humor_line(
    update: Update,
    details,
    field: AnnouncementFieldState,
    *,
    error: str | None,
) -> str | None:
    """
    Редкая шутливая вариация для экрана вопроса — или `None`.

    Экран с ошибкой получает запрещённый вид (`error`), поэтому шуток в
    диагностике нет ни при каком ключе. Остальные ограничения (лимиты,
    права, потеря данных) живут на своих экранах и сюда не обращаются.
    """
    screen_kind = ui_humor.SCREEN_ERROR if error else ui_humor.SCREEN_QUESTION
    key = (
        f"{_user_id(update)}:{details.draft_id}:" f"{details.revision}:{field.brick_id}"
    )
    return ui_humor.pick_variant(key, screen_kind=screen_kind)


async def _render_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    fields: list[AnnouncementFieldState],
    index: int,
    *,
    editing: bool = False,
    error: str | None = None,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    index = max(0, min(index, len(fields) - 1))
    field = fields[index]
    ui["question_index"] = index
    ui["active_brick_id"] = field.brick_id
    ui["active_source"] = "question"
    ui["editing"] = editing
    # Poster 2.0 S3 п.2: сначала настоящий анонс сверху, затем служебный
    # экран. Порядок в чате фиксирован хронологией сообщений, поэтому
    # верхний блок синхронизируется ДО отрисовки нижнего вопроса.
    await _sync_live(update, context, details)
    text = wizard_questions.question_text(
        field, index, len(fields), editing=editing, error=error, notice=notice
    )
    rows: list[list[InlineKeyboardButton]] = []

    if field.value_type == BRICK_TYPE_CHOICE:
        options = _choice_options(field)
        selected = set(_selected_choices(field))
        for option_index, option in enumerate(options):
            marker = "✅ " if option in selected else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(f"{marker}{option}", 60),
                        callback_data=(
                            f"aw_choice:{details.draft_id}:{details.revision}:"
                            f"{field.brick_id}:{option_index}"
                        ),
                    )
                ]
            )
        if field.rules.get("multiple") and options:
            rows.append(
                [
                    InlineKeyboardButton(
                        "✅ Готово",
                        callback_data=(
                            f"aw_choice_done:{details.draft_id}:"
                            f"{details.revision}:{field.brick_id}"
                        ),
                    )
                ]
            )
    elif field.value_type == BRICK_TYPE_DATE:
        # Poster 2.0 S3 п.4: календарь показывается сразу на вопросе даты —
        # отдельной кнопки «📅 Календарь» больше нет. Callback'и прежние,
        # поэтому ручная навигация по неделям/месяцам не изменилась.
        rows.extend(
            wizard_questions.calendar_shortcut_rows(
                field, f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )
    elif field.value_type == BRICK_TYPE_TIME:
        # S3 п.5: быстрые кнопки времени рядом с ручным вводом.
        rows.extend(
            wizard_questions.time_preset_rows(
                f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )

    if field.status in {FIELD_STATUS_VALID, FIELD_STATUS_SKIPPED}:
        rows.append(
            [
                InlineKeyboardButton(
                    "➡️ Далее",
                    callback_data=(
                        f"aw_qnext:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    if field.status == FIELD_STATUS_VALID and not editing:
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Изменить ответ",
                    callback_data=(
                        f"aw_qedit:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    skip_button = InlineKeyboardButton(
        tr("buttons.wizard.skip"),
        callback_data=(
            f"aw_qskip:{details.draft_id}:{details.revision}:{field.brick_id}"
        ),
    )
    back_button = InlineKeyboardButton(
        tr("buttons.nav.back"),
        callback_data=(
            f"aw_qback:{details.draft_id}:{details.revision}:{field.brick_id}"
        ),
    )
    if field.value_type == BRICK_TYPE_DATE:
        # U2a: внизу первого экрана даты — ровно одна строка навигации.
        # U3a №13: пара собирается общим шаблоном «Назад | Пропустить».
        rows += keyboard_layouts.pair(back_button, skip_button)
    else:
        rows.append([skip_button])
        rows.append([back_button])
    keyboard = InlineKeyboardMarkup(rows)
    # S5 §5.7: шутка подставляется в текст ТОЛЬКО после того, как собраны
    # `callback_data` и клавиатура, — повлиять на действия она не может.
    # Ключ стабилен между перерисовками одного экрана (только ID черновика,
    # ревизии и кирпичика), поэтому текст не «прыгает».
    humor_line = _question_humor_line(update, details, field, error=error)
    if humor_line:
        text = f"{text}\n\n{_h(humor_line)}"
    await _render(update, context, text, keyboard)
    return AW_QUESTIONS


async def _enter_questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    fields = _askable_fields(_load_fields(_user_id(update), details.draft_id))
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    active = ui.get("active_brick_id")
    index = -1
    for position, field in enumerate(fields):
        if field.brick_id == active:
            index = position
            break
    if index < 0:
        index = _first_missing_index(fields)
    if index < 0:
        return await _enter_extras(update, context, details, notice=notice)
    return await _render_question(
        update, context, details, fields, index, notice=notice
    )


async def _question_fields_for(
    update: Update, context: ContextTypes.DEFAULT_TYPE, details
) -> list[AnnouncementFieldState]:
    return _askable_fields(_load_fields(_user_id(update), details.draft_id))


def _find_field(
    fields: list[AnnouncementFieldState], brick_id: int
) -> tuple[int, AnnouncementFieldState] | None:
    for index, field in enumerate(fields):
        if field.brick_id == brick_id:
            return index, field
    return None


async def _question_from_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> (
    tuple[
        dict[str, Any], Any, list[AnnouncementFieldState], int, AnnouncementFieldState
    ]
    | None
):
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    fields = await _question_fields_for(update, context, details)
    found = _find_field(fields, brick_id)
    if found is None:
        return None
    index, field = found
    if ui.get("active_brick_id") != brick_id:
        return None
    return ui, details, fields, index, field


async def _advance_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    next_index: int,
    *,
    notice: str | None = None,
) -> int:
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    if next_index >= len(fields):
        return await _after_questions(update, context, details, notice=notice)
    if next_index < 0:
        next_index = 0
    return await _render_question(
        update, context, details, fields, next_index, notice=notice
    )


async def _after_questions(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Последний кирпичик ведёт на шаг фотографий, а не к готовому посту.

    Poster 2.0 S3 п.6: после всех кирпичиков обязательно спрашиваются
    фотографии; завершает анонс «Без фотографий» или готовая партия фото.
    Кнопки «Готово»/«Сформировать» нет ни на одном шаге.
    """
    try:
        updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    if updated.publication_sent:
        # Анонс уже доставлен. Правка через «Изменить данные» может состоять
        # из нескольких шагов: новую версию отправляет явное «Применить
        # изменения», иначе каждая правка создавала бы отдельное сообщение.
        return await _render_delivered(
            update,
            context,
            updated,
            notice=notice,
            pending=_has_pending_changes(update, updated),
        )
    return await wizard_photos.render_photo_step(
        update,
        context,
        updated,
        notice=notice,
        announcement_is_empty=_announcement_is_empty(update, updated),
    )


async def _back_from_photo_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int:
    """
    «◀️ Назад» с шага фото: возврат к предыдущему шагу мастера.

    Обычный путь — последний кирпичик (шаг фото идёт сразу после вопросов).
    Если кирпичиков нет или все они необязательные и уже пропущены,
    показывается менеджер полей: это тот же шаг вопросов, просто списком.
    Черновик и фотографии не меняются.
    """
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _render_question_manager(update, context, details)
    return await _render_question(update, context, details, fields, len(fields) - 1)


async def _save_question_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    value: object,
    *,
    index: int,
    drop_input: bool = False,
) -> int:
    user_id = _user_id(update)
    try:
        with SessionLocal() as session:
            updated = save_announcement_value(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                brick_id=field.brick_id,
                value=value,
                expected_revision=details.revision,
                today=app_today(),
            )
    except AnnouncementError as exc:
        message = str(exc)
        if drop_input:
            user_data = ctx.data(context)
            screen.track_failed_input(
                user_data,
                f"aw:{field.brick_id}",
                update.effective_message,
            )
        fields = _askable_fields(_load_fields(user_id, details.draft_id))
        return await _render_question(
            update,
            context,
            details,
            fields,
            index,
            editing=True,
            error=message,
        )

    ui = _ui(context)
    _set_details(ui, updated)
    _clear_active(ui)
    if drop_input:
        user_data = ctx.data(context)
        stale = screen.forget_failed_inputs(user_data, f"aw:{field.brick_id}")
        if stale:
            await screen.cleanup_messages(context.bot, ctx.chat(update).id, stale)
        await screen.drop_user_input(update, context)
    # S4 §5.2 п.5: сокращение после проблемного экрана сразу пересобирает
    # анонс и применяет уже сохранённую фотографию без повторного вопроса.
    resumed = await _resume_after_shortening(update, context, updated)
    if resumed is not None:
        return resumed
    return await _advance_question(
        update,
        context,
        updated,
        index + 1,
        notice=f"Значение «{_short(field.title, 50)}» сохранено.",
    )


async def _skip_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
) -> int:
    return await _save_question_value(update, context, details, field, "", index=index)


async def _render_question_manager(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    fields = await _question_fields_for(update, context, details)
    if not fields:
        return await _enter_extras(update, context, details, notice=notice)
    lines = [
        "🧱 Поля анонса",
        "",
        "Выберите поле, чтобы изменить или заполнить значение. "
        "Необязательные поля можно пропускать.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for index, field in enumerate(fields, start=1):
        status = _field_status_text(field)
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"{index}. {field.title} — {status}", 60),
                    callback_data=(
                        f"aw_qgoto:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "➕ Дополнительный кирпичик",
                callback_data=(f"aw_extras:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "👁 Обзор анонса",
                callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="aw_manage_back")]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


def _extra_field_state(details, extra, definition) -> AnnouncementFieldState:
    value = details.value_for(extra.brick_id)
    status = FIELD_STATUS_MISSING
    raw: object | None = None
    raw_html: str | None = None
    if value is not None and value.status is BrickValueStatus.VALID:
        status = FIELD_STATUS_VALID
        raw = answer_payload_to_raw(
            BrickValueResult(
                status=value.status,
                payload=dict(value.payload) if value.payload is not None else None,
            )
        )
        stored_html = (value.payload or {}).get("html")
        if isinstance(stored_html, str):
            raw_html = stored_html
    elif value is not None and value.status is BrickValueStatus.EMPTY_OPTIONAL:
        status = FIELD_STATUS_SKIPPED
    return AnnouncementFieldState(
        brick_id=extra.brick_id,
        code=extra.code,
        title=extra.title,
        value_type=extra.value_type,
        section=extra.section,
        status=status,
        raw_value=raw,
        rules=dict(definition.rules),
        description=definition.description,
        raw_html=raw_html,
    )


def _extra_definition(details, extra, telegram_user_id: int):
    with SessionLocal() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=details.draft_id,
        )
    return payload.definitions_by_id[extra.brick_id]


def _choice_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> tuple[dict[str, Any], Any, AnnouncementFieldState, int, str] | None:
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    if ui.get("active_brick_id") != brick_id:
        return None
    source = ui.get("active_source")
    if source == "question":
        fields = _askable_fields(_load_fields(_user_id(update), draft_id))
        found = _find_field(fields, brick_id)
        if found is None:
            return None
        index, field = found
    elif source == "extra":
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None:
            return None
        definition = _extra_definition(details, extra, _user_id(update))
        field = _extra_field_state(details, extra, definition)
        index = -1
    else:
        return None
    if field.value_type != BRICK_TYPE_CHOICE:
        return None
    return ui, details, field, index, source


async def _handle_choice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
    option_index: int,
) -> int:
    found = _choice_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    ui, details, field, index, source = found
    options = _choice_options(field)
    if option_index < 0 or option_index >= len(options):
        return await _stale(update, context)
    option = options[option_index]
    multiple = bool(field.rules.get("multiple"))
    if multiple:
        selected = _selected_choices(field)
        if option in selected:
            selected = [item for item in selected if item != option]
        else:
            selected.append(option)
        raw_value: object = selected
    else:
        raw_value = option
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == brick_id)
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            raw_value,
            stay_prompt=multiple,
            notice=(
                "Выбор сохранён. Можно отметить ещё варианты."
                if multiple
                else "Значение дополнительного кирпичика сохранено."
            ),
        )
    if multiple:
        try:
            with SessionLocal() as session:
                updated = save_announcement_value(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    brick_id=brick_id,
                    value=raw_value,
                    expected_revision=details.revision,
                    today=app_today(),
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        _set_details(ui, updated)
        fields = await _question_fields_for(update, context, updated)
        new_found = _find_field(fields, brick_id)
        if new_found is None:
            return await _enter_extras(update, context, updated)
        return await _render_question(
            update,
            context,
            updated,
            fields,
            new_found[0],
            notice="Выбор сохранён. Можно отметить ещё варианты.",
        )
    return await _save_question_value(
        update, context, details, field, raw_value, index=index
    )


async def _handle_choice_done(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
) -> int:
    found = _choice_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    _ui_data, details, field, index, source = found
    if not field.rules.get("multiple"):
        return await _stale(update, context)
    if field.status == FIELD_STATUS_MISSING:
        await _answer(update, "Отметьте хотя бы один вариант.", alert=True)
        return await _resume_current(update, context)
    if source == "extra":
        return await _enter_extras(
            update,
            context,
            details,
            notice="Значение дополнительного кирпичика сохранено.",
        )
    return await _advance_question(update, context, details, index + 1)


def _active_field(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
):
    """
    Поле активного вопроса по callback-адресу (общий путь для даты/времени).

    Устаревший revision или чужой brick_id → None: экран уже перерисован.
    """
    guarded = _guard_details(update, context, draft_id, revision)
    if guarded is None:
        return None
    ui, details = guarded
    if ui.get("active_brick_id") != brick_id:
        return None
    source = ui.get("active_source")
    if source == "question":
        fields = _askable_fields(_load_fields(_user_id(update), draft_id))
        found = _find_field(fields, brick_id)
        if found is None:
            return None
        index, field = found
    elif source == "extra":
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None:
            return None
        definition = _extra_definition(details, extra, _user_id(update))
        field = _extra_field_state(details, extra, definition)
        index = -1
    else:
        return None
    return details, field, index, source


def _calendar_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
):
    found = _active_field(update, context, draft_id, revision, brick_id)
    if found is None:
        return None
    details, field, index, source = found
    if field.value_type != BRICK_TYPE_DATE:
        return None
    return details, field, index, source, _calendar_bounds(dict(field.rules))


async def _render_calendar_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    *,
    error: str | None = None,
    notice: str | None = None,
) -> int:
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _render_extra_value_prompt(
            update, context, details, extra, error=error, notice=notice
        )
    fields = await _question_fields_for(update, context, details)
    return await _render_question(
        update,
        context,
        details,
        fields,
        index,
        error=error,
        notice=notice,
    )


async def _render_calendar_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    *,
    title: str,
    days: list[date],
    back_callback: str,
    back_label: str | None = None,
    per_row: int = 4,
    label: Callable[[date], str] = calendar_view.format_quick_day,
) -> int:
    """Экран дней (раскрытый диапазон или сетка месяца) с возвратом."""
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    rows = _day_rows(days, prefix, per_row=per_row, label=label)
    # Подпись читается в момент отрисовки: правка каталога видна без
    # перезапуска процесса (импорт-time значение застыло бы навсегда).
    label_text = back_label if back_label is not None else tr("buttons.nav.back")
    rows.append([InlineKeyboardButton(label_text, callback_data=back_callback)])
    await _render(update, context, title, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


async def _render_calendar_range_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    value_range: calendar_view.DateRange,
) -> int:
    """
    Раскрытие семидневного диапазона на том же служебном экране.

    Внизу — «◀️ К быстрым датам»: возврат к первому набору из восьми дат
    без перезахода в конструктор.
    """
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    return await _render_calendar_days(
        update,
        context,
        details,
        field,
        index,
        source,
        title=(f"📅 {calendar_view.format_range(*value_range)}\n\nВыберите дату:"),
        days=calendar_view.days_between(*value_range),
        back_callback=f"aw_calquick:{prefix}",
        back_label="◀️ К быстрым датам",
    )


async def _render_calendar_months(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    """Сетка месяцев горизонта: не больше 12 кнопок (месяц горизонта — стрелкой)."""
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    seam = calendar_view.selection_seam(app_today(), allowed)
    months = calendar_view.month_sequence(seam, end)
    months = [key for key in months if calendar_view.month_days(key, allowed)][
        : calendar_view.MONTH_GRID_MAX
    ]
    if not months:
        return await _render_calendar_value(
            update,
            context,
            details,
            field,
            index,
            source,
            error="Для выбора месяца нет доступных дат. Введите дату текстом.",
        )
    rows = keyboard_layouts.rows_of(
        (
            InlineKeyboardButton(
                calendar_view.format_month(key),
                callback_data=f"aw_calmonth:{prefix}:{key[0]}:{key[1]}",
            )
            for key in months
        ),
        3,
    )
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ К быстрым датам", callback_data=f"aw_calquick:{prefix}"
            )
        ]
    )
    await _render(
        update,
        context,
        "📅 Выберите месяц:",
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _render_calendar_month(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    key: calendar_view.MonthKey,
) -> int:
    """
    Календарь месяца: дни сеткой по семь, стрелки соседних месяцев.

    Стрелки показываются только внутри горизонта (`calendar_bounds`),
    поэтому уйти за допустимые даты нельзя.
    """
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    seam = calendar_view.selection_seam(app_today(), allowed)
    # Полная последовательность месяцев горизонта: последний месяц может не
    # попасть в сетку (кнопок не больше 12) и открывается стрелкой «▶️».
    months = [
        item
        for item in calendar_view.month_sequence(seam, end)
        if calendar_view.month_days(item, allowed)
    ]
    if key not in months:
        return await _stale(update, context)
    days = calendar_view.month_days(key, allowed)
    if not days:
        return await _stale(update, context)
    rows = _day_rows(days, prefix, per_row=7, label=calendar_view.format_month_day)
    position = months.index(key)
    # U3a №13: стрелки соседних месяцев — общий шаблон пагинации.
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button(
                "◀️",
                f"aw_calmonth:{prefix}:{months[position - 1][0]}:"
                f"{months[position - 1][1]}",
            )
            if position > 0
            else None
        ),
        (
            keyboard_layouts.button(
                "▶️",
                f"aw_calmonth:{prefix}:{months[position + 1][0]}:"
                f"{months[position + 1][1]}",
            )
            if position + 1 < len(months)
            else None
        ),
    )
    rows.append(
        [InlineKeyboardButton("📅 Месяцы", callback_data=f"aw_calmonths:{prefix}")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ К быстрым датам", callback_data=f"aw_calquick:{prefix}"
            )
        ]
    )
    await _render(
        update,
        context,
        f"📅 {calendar_view.format_month(key)}\n\nВыберите день:",
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _save_calendar_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    day: date,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    if day < start or day > end:
        return await _stale(
            update,
            context,
            "Эта дата вне допустимого диапазона. Выберите дату заново.",
        )
    raw = day.strftime("%d.%m.%Y")
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            raw,
            notice=f"Дата {raw} сохранена.",
        )
    return await _save_question_value(
        update,
        context,
        details,
        field,
        raw,
        index=index,
    )


def _ensure_stage(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    stage: WizardStage,
):
    if details.stage == stage:
        return details
    with SessionLocal() as session:
        updated = set_announcement_stage(
            session,
            actor_telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            stage=stage,
            expected_revision=details.revision,
        )
    ui = _ui(context)
    _set_details(ui, updated)
    return updated


def _extras_with_definitions(details, telegram_user_id: int):
    with SessionLocal() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=details.draft_id,
        )
    return [
        (extra, payload.definitions_by_id[extra.brick_id]) for extra in details.extras
    ]


async def _render_extras(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    await _sync_live(update, context, details)
    items = _extras_with_definitions(details, _user_id(update))
    lines = [
        "➕ Дополнительные кирпичики",
        "",
        "Дополнительные поля влияют только на этот анонс. Исходный шаблон "
        "не меняется.",
        "Если анонс уже отправлен, изменения применяются по кнопке "
        "«Применить изменения»: новая версия заменит прошлую.",
    ]
    if items:
        for number, (extra, definition) in enumerate(items, start=1):
            field = _extra_field_state(details, extra, definition)
            value = _field_value_text(field)
            if field.status == FIELD_STATUS_MISSING:
                value_text = "значение не задано"
            elif field.status == FIELD_STATUS_SKIPPED:
                value_text = tr("wizard.status.skipped")
            else:
                value_text = value or "пусто"
            lines.append(
                f"{number}. {_h(extra.title)} — "
                f"{SECTION_LABELS.get(extra.section, extra.section)}, "
                f"{POSITION_LABELS.get(extra.position, extra.position)}; "
                f"значение: {_h(_short(value_text, 120))}"
            )
    else:
        lines.extend(["", "Дополнительных кирпичиков пока нет."])

    rows: list[list[InlineKeyboardButton]] = []
    for number, (extra, _definition) in enumerate(items, start=1):
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"✏️ {number}. {extra.title}", 60),
                    callback_data=(
                        f"aw_exedit:{details.draft_id}:"
                        f"{details.revision}:{extra.brick_id}"
                    ),
                ),
                InlineKeyboardButton(
                    _short(f"🗑 {number}", 8),
                    callback_data=(
                        f"aw_exdel:{details.draft_id}:"
                        f"{details.revision}:{extra.extra_id}"
                    ),
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "➕ Дополнительный кирпичик",
                callback_data=(f"aw_exadd:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    if details.publication_sent:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.finished.apply"),
                    callback_data=(f"aw_apply:{details.draft_id}:{details.revision}"),
                )
            ]
        )
        # U2b: возврат из правки ведёт на экран завершения, а не в конструктор.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"),
                    callback_data=(f"aw_panel:{details.draft_id}:{details.revision}"),
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "👁 Обзор анонса",
                    callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
                )
            ]
        )
    if not details.publication_sent:
        rows.append(
            [
                InlineKeyboardButton(
                    "🧱 К вопросам",
                    callback_data=(f"aw_qmanage:{details.draft_id}:{details.revision}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _enter_extras(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    try:
        updated = _ensure_stage(update, context, details, WizardStage.EXTRAS)
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    return await _render_extras(update, context, updated, notice=notice)


def _available_extra_bricks(details, telegram_user_id: int):
    with SessionLocal() as session:
        available = list_available_bricks(
            session,
            viewer_telegram_user_id=telegram_user_id,
            place_id=details.place_id,
        )
    snapshot = details.template_snapshot
    used = set(snapshot.brick_ids) if snapshot is not None else set()
    used.update(extra.brick_id for extra in details.extras)
    return [brick for brick in available if brick.id not in used]


async def _render_available_bricks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    page: int = 0,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    bricks = _available_extra_bricks(details, _user_id(update))
    page = _clamped_page(max(0, page), len(bricks), EXTRA_PAGE_SIZE)
    ui["extras_page"] = page
    shown = bricks[page * EXTRA_PAGE_SIZE : (page + 1) * EXTRA_PAGE_SIZE]
    lines = [
        "➕ Дополнительный кирпичик",
        "",
        "Выберите доступное определение. Чужие личные кирпичики и поля "
        "других мест здесь не показываются.",
    ]
    if shown:
        for number, brick in enumerate(shown, start=page * EXTRA_PAGE_SIZE + 1):
            lines.append(
                f"{number}. {_h(brick.title)} — "
                f"{BRICK_TYPE_TITLES.get(brick.value_type, brick.value_type)}, "
                f"{SCOPE_LABELS.get(brick.scope, brick.scope)}"
            )
    else:
        lines.extend(["", "Доступных дополнительных определений нет."])
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _short(f"➕ {brick.title}", 60),
                callback_data=(
                    f"aw_exbrick:{details.draft_id}:" f"{details.revision}:{brick.id}"
                ),
            )
        ]
        for brick in shown
    ]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(
                    f"aw_exbpage:{details.draft_id}:" f"{details.revision}:{page - 1}"
                ),
            )
        )
    if (page + 1) * EXTRA_PAGE_SIZE < len(bricks):
        nav.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=(
                    f"aw_exbpage:{details.draft_id}:" f"{details.revision}:{page + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=(f"aw_extras:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _render_extra_sections(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    brick_id: int,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    ui["extra_flow"] = {
        "phase": "section",
        "brick_id": brick_id,
        "section": None,
        "position": None,
    }
    brick = next(
        (
            item
            for item in _available_extra_bricks(details, _user_id(update))
            if item.id == brick_id
        ),
        None,
    )
    if brick is None:
        return await _render_available_bricks(
            update, context, details, page=ui.get("extras_page", 0)
        )
    # Заголовок кирпичика задаёт человеком и уходит в parse_mode=HTML
    # экран — экранируем (аудит: неэкранированный HTML ломал экран).
    text = f"➕ {_h(brick.title)}\n\n" "Выберите секцию анонса: шапка, тело или подвал."
    body = f"{notice}\n\n{text}" if notice else text
    rows = [
        [
            InlineKeyboardButton(
                label,
                callback_data=(
                    f"aw_exsec:{details.draft_id}:{details.revision}:"
                    f"{brick_id}:{section}"
                ),
            )
        ]
        for section, label in (
            (TEMPLATE_SECTION_HEADER, "Шапка"),
            (TEMPLATE_SECTION_BODY, "Тело"),
            (TEMPLATE_SECTION_FOOTER, "Подвал"),
        )
    ]
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=f"aw_exback:{details.draft_id}:{details.revision}",
            )
        ]
    )
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _render_extra_position(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    brick_id: int,
    section: str,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    flow = ui.get("extra_flow") or {}
    flow.update(
        {
            "phase": "position",
            "brick_id": brick_id,
            "section": section,
            "position": None,
        }
    )
    ui["extra_flow"] = flow
    text = (
        "➕ Дополнительный кирпичик\n\n"
        f"Секция: {SECTION_LABELS.get(section, section)}.\n"
        "Выберите положение внутри секции."
    )
    body = f"{notice}\n\n{text}" if notice else text
    rows = [
        [
            InlineKeyboardButton(
                "В начало",
                callback_data=(
                    f"aw_expos:{details.draft_id}:{details.revision}:"
                    f"{brick_id}:{section}:{EXTRA_POSITION_START}"
                ),
            ),
            InlineKeyboardButton(
                "В конец",
                callback_data=(
                    f"aw_expos:{details.draft_id}:{details.revision}:"
                    f"{brick_id}:{section}:{EXTRA_POSITION_END}"
                ),
            ),
        ],
        [
            InlineKeyboardButton(
                tr("buttons.nav.back"),
                callback_data=f"aw_exback:{details.draft_id}:{details.revision}",
            )
        ],
    ]
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


def _extra_value_text(extra, field: AnnouncementFieldState) -> str:
    lines = [
        # Заголовок кирпичика задаёт человеком и уходит в parse_mode=HTML
        # экран — экранируем (аудит: неэкранированный HTML ломал экран).
        f"➕ {_h(field.title)}",
        f"Секция: {SECTION_LABELS.get(extra.section, extra.section)}; "
        f"положение: {POSITION_LABELS.get(extra.position, extra.position)}.",
    ]
    description = wizard_questions.description_html(field.description)
    if description:
        # №27: описание — моноширинно и уже экранировано внутри `<code>`.
        lines.append(f"📝 {description}")
    lines.append(_type_hint(field))
    if field.status == FIELD_STATUS_VALID:
        # U1b: сохранённое значение — с форматированием (см. stored_value_html).
        lines.append(
            tr_safe(
                "wizard.question.current_value",
                value=wizard_questions.stored_value_html(field),
            )
        )
    elif field.status == FIELD_STATUS_SKIPPED:
        lines.append(tr("wizard.question.skipped"))
    else:
        lines.append(tr("wizard.question.missing"))
    return "\n\n".join(lines)


async def _render_extra_value_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    extra,
    *,
    editing: bool = True,
    error: str | None = None,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    _set_details(ui, details)
    definition = _extra_definition(details, extra, _user_id(update))
    field = _extra_field_state(details, extra, definition)
    ui["active_brick_id"] = field.brick_id
    ui["active_source"] = "extra"
    ui["editing"] = editing
    lines = [_extra_value_text(extra, field)]
    if error:
        lines.append(f"⚠️ {_h(error)}")
    if notice:
        lines.append(_h(notice))
    rows: list[list[InlineKeyboardButton]] = []
    if field.value_type == BRICK_TYPE_CHOICE:
        selected = set(_selected_choices(field))
        for option_index, option in enumerate(_choice_options(field)):
            marker = "✅ " if option in selected else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(f"{marker}{option}", 60),
                        callback_data=(
                            f"aw_choice:{details.draft_id}:{details.revision}:"
                            f"{field.brick_id}:{option_index}"
                        ),
                    )
                ]
            )
        if field.rules.get("multiple") and _choice_options(field):
            rows.append(
                [
                    InlineKeyboardButton(
                        "✅ Готово",
                        callback_data=(
                            f"aw_choice_done:{details.draft_id}:"
                            f"{details.revision}:{field.brick_id}"
                        ),
                    )
                ]
            )
    elif field.value_type == BRICK_TYPE_DATE:
        # U2a: компактный календарь показывается прямо на экране значения
        # дополнительного кирпичика — отдельного входа «📅 Календарь» нет.
        rows.extend(
            wizard_questions.calendar_shortcut_rows(
                field, f"{details.draft_id}:{details.revision}:{field.brick_id}"
            )
        )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.wizard.skip"),
                callback_data=(
                    f"aw_exskip:{details.draft_id}:{details.revision}:"
                    f"{field.brick_id}"
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ К дополнительным полям",
                callback_data=(f"aw_exbackval:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    await _render(update, context, "\n\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_EXTRAS


async def _save_extra_value(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    extra,
    value: object,
    *,
    drop_input: bool = False,
    stay_prompt: bool = False,
    notice: str | None = None,
) -> int:
    try:
        with SessionLocal() as session:
            updated = save_announcement_value(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                brick_id=extra.brick_id,
                value=value,
                expected_revision=details.revision,
                today=app_today(),
            )
    except AnnouncementError as exc:
        message = str(exc)
        if drop_input:
            screen.track_failed_input(
                ctx.data(context),
                f"aw:{extra.brick_id}",
                update.effective_message,
            )
        current_extra = next(
            (item for item in details.extras if item.extra_id == extra.extra_id),
            extra,
        )
        return await _render_extra_value_prompt(
            update,
            context,
            details,
            current_extra,
            error=message,
        )
    ui = _ui(context)
    _set_details(ui, updated)
    if drop_input:
        user_data = ctx.data(context)
        stale = screen.forget_failed_inputs(user_data, f"aw:{extra.brick_id}")
        if stale:
            await screen.cleanup_messages(context.bot, ctx.chat(update).id, stale)
        await screen.drop_user_input(update, context)
    current_extra = next(
        (item for item in updated.extras if item.extra_id == extra.extra_id),
        None,
    )
    if current_extra is None:
        return await _render_extras(
            update, context, updated, notice="Кирпичик больше недоступен."
        )
    if stay_prompt:
        _clear_active(ui)
        return await _render_extra_value_prompt(
            update, context, updated, current_extra, notice=notice
        )
    _clear_active(ui)
    # S4 §5.2 п.5: сокращение дополнительного кирпичика тоже сразу
    # пересобирает анонс с уже сохранённым фото.
    resumed = await _resume_after_shortening(update, context, updated)
    if resumed is not None:
        return resumed
    return await _render_extras(update, context, updated, notice=notice)


async def _add_extra(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    brick_id: int,
    section: str,
    position: str,
) -> int:
    if section not in SECTION_LABELS or position not in POSITION_LABELS:
        return await _stale(update, context)
    try:
        with SessionLocal() as session:
            updated = add_announcement_extra(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                brick_id=brick_id,
                section=section,
                position=position,
                expected_revision=details.revision,
            )
    except AnnouncementError as exc:
        await _answer(update, str(exc), alert=True)
        return await _resume_current(update, context, notice=str(exc))
    extra = next((item for item in updated.extras if item.brick_id == brick_id), None)
    if extra is None:
        return await _render_extras(
            update, context, updated, notice="Кирпичик не добавлен."
        )
    ui = _ui(context)
    _set_details(ui, updated)
    ui["extra_flow"] = None
    return await _render_extra_value_prompt(
        update,
        context,
        updated,
        extra,
        notice="Кирпичик добавлен. Задайте значение или пропустите его.",
    )


def _preview_template_name(details) -> str:
    snapshot = details.template_snapshot
    if snapshot is None:
        return "не выбран"
    if snapshot.source_name:
        return snapshot.source_name
    if snapshot.source_kind == "standard":
        return "Стандартный шаблон места"
    return "Личный шаблон"


def _preview_summary_lines(
    preview: AnnouncementPreview, telegram_user_id: int
) -> list[str]:
    details = preview.draft
    filled: list[str] = []
    skipped: list[str] = []
    missing: list[str] = []
    for field in preview.fields:
        if field.status == FIELD_STATUS_VALID:
            value = _field_value_text(field)
            filled.append(
                f"• {_h(field.title)}: {_h(_short(value, FIELD_DISPLAY_LIMIT))}"
            )
        elif field.status == FIELD_STATUS_KNOWN:
            value = _field_value_text(field)
            filled.append(
                f"• {_h(field.title)}: {_h(_short(value, FIELD_DISPLAY_LIMIT))} "
                "(из справочника)"
            )
        elif field.status == FIELD_STATUS_SKIPPED:
            skipped.append(f"• {_h(field.title)}")
        elif field.status == FIELD_STATUS_MISSING:
            missing.append(f"• {_h(field.title)}")
    lines = [
        "👁 Обзор анонса",
        "",
        *_place_lines(details),
        f"Город: {_h(details.place_city) if details.place_city else '—'}",
        f"Адрес: {_h(details.place_address) if details.place_address else 'не указан'}",
        f"Ссылка: {_h(details.place_url) if details.place_url else 'не указана'}",
        f"Шаблон: {_h(_preview_template_name(details))}",
        f"Фотографии: {len(details.photos)} " "(отдельные вложения, не часть шаблона)",
    ]
    if details.template_snapshot is not None:
        lines.append(
            "Версия снимка: " f"{details.template_snapshot.source_template_version}."
        )
    lines.append("")
    if filled:
        lines.append("Заполненные значения:")
        lines.extend(filled[:MAX_PREVIEW_FIELDS])
        if len(filled) > MAX_PREVIEW_FIELDS:
            lines.append(f"…и ещё {len(filled) - MAX_PREVIEW_FIELDS} значений.")
    else:
        lines.append("Заполненных значений пока нет.")
    if skipped:
        lines.extend(["", "Пропущенные поля:"])
        lines.extend(skipped[:MAX_PREVIEW_FIELDS])
        if len(skipped) > MAX_PREVIEW_FIELDS:
            lines.append(f"…и ещё {len(skipped) - MAX_PREVIEW_FIELDS} полей.")
    if missing:
        lines.extend(["", "Не заполнены:"])
        lines.extend(missing[:MAX_PREVIEW_FIELDS])
        if len(missing) > MAX_PREVIEW_FIELDS:
            lines.append(f"…и ещё {len(missing) - MAX_PREVIEW_FIELDS} полей.")

    extras = _extras_with_definitions(details, telegram_user_id)
    if extras:
        lines.extend(["", "Дополнительные кирпичики:"])
        for extra, definition in extras[:MAX_PREVIEW_EXTRAS]:
            field = _extra_field_state(details, extra, definition)
            section = SECTION_LABELS.get(extra.section, extra.section)
            position = POSITION_LABELS.get(extra.position, extra.position)
            if field.status == FIELD_STATUS_VALID:
                value = _short(_field_value_text(field), FIELD_DISPLAY_LIMIT)
                value_text = f"значение: {_h(value)}"
            elif field.status == FIELD_STATUS_SKIPPED:
                value_text = "значение пропущено"
            else:
                value_text = "значение не задано"
            lines.append(f"• {_h(extra.title)} — {section}, {position}; {value_text}")
        if len(extras) > MAX_PREVIEW_EXTRAS:
            lines.append(f"…и ещё {len(extras) - MAX_PREVIEW_EXTRAS}.")
    return lines


def _preview_block(preview: AnnouncementPreview) -> list[str]:
    if preview.ready:
        lines = [
            "",
            tr("wizard.ready.collecting"),
        ]
        if preview.warnings:
            lines.append(_h(preview.warnings[0]))
        lines.extend(["", "📄 Предпросмотр:"])
        visible = preview.visible_text
        if len(visible) > PREVIEW_VISIBLE_LIMIT:
            shortened = visible[:PREVIEW_VISIBLE_LIMIT].rstrip() + "…"
            lines.append(_h(shortened))
            lines.append("Предпросмотр сокращён. Полные данные сохранены в черновике.")
        else:
            lines.append(preview.html)
        return lines

    lines = ["", "⚠️ Анонс пока не готов."]
    for issue in preview.issues:
        lines.append(f"• {_h(issue)}")
    if preview.missing:
        titles = ", ".join(_h(question.title) for question in preview.missing[:5])
        suffix = "…" if len(preview.missing) > 5 else ""
        lines.append(
            f"Что заполнить: {titles}{suffix}. Можно также заполнить любое "
            "поле из шаблона."
        )
    if preview.visible_text:
        lines.extend(["", "Черновой фрагмент (анонс ещё не готов):"])
        fragment = preview.visible_text[:PREVIEW_VISIBLE_LIMIT].rstrip()
        if len(preview.visible_text) > PREVIEW_VISIBLE_LIMIT:
            fragment += "…"
        lines.append(_h(fragment))
    return lines


def _preview_keyboard(
    details, *, ready: bool = False, saved: bool = False
) -> InlineKeyboardMarkup:
    draft_id = details.draft_id
    revision = details.revision
    delivered = bool(details.publication_sent)
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                (
                    "💾 Сохранить и выйти"
                    if delivered
                    else ("✅ Черновик сохранён" if saved else "✅ Сохранить черновик")
                ),
                callback_data=f"aw_save:{draft_id}:{revision}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "✏️ Изменить поля",
                callback_data=f"aw_qmanage:{draft_id}:{revision}",
            )
        ],
    )
    rows.append(
        [
            InlineKeyboardButton(
                "➕ Дополнительные кирпичики",
                callback_data=f"aw_extras:{draft_id}:{revision}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"📷 Фотографии ({len(details.photos)})",
                callback_data=f"aw_photos:{draft_id}:{revision}:0",
            )
        ]
    )
    if details.photos:
        rows.append(
            [
                InlineKeyboardButton(
                    "👁 Предпросмотр с фото",
                    callback_data=f"aw_media_preview:{draft_id}:{revision}",
                )
            ]
        )
    if ready and details.publication_sent:
        # В обзоре доставленного анонса единственное действие отправки —
        # применение правок: новая версия заменит прошлую.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.finished.apply"),
                    callback_data=f"aw_apply:{draft_id}:{revision}",
                )
            ]
        )
    if ready and not details.publication_sent:
        # Обычный путь доставляет анонс автоматически после последнего ответа.
        # Эта кнопка нужна только черновикам, остановленным на обзоре
        # (сохранённый черновик, рестарт, возврат после полного отказа
        # отправки): без неё их нельзя было бы завершить.
        rows.append(
            [
                InlineKeyboardButton(
                    "📨 Отправить в этот чат",
                    callback_data=f"aw_send:{draft_id}:{revision}",
                )
            ]
        )
    if delivered:
        rows.append(
            [
                InlineKeyboardButton(
                    "◀️ К управлению",
                    callback_data=f"aw_panel:{draft_id}:{revision}",
                )
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=f"aw_preview:{draft_id}:{revision}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


async def _render_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    saved: bool = False,
    preview_only: bool = False,
) -> int:
    """
    Обзор анонса; для уже доставленного — экран управления без отправки.

    Просмотр и повторное открытие черновика никогда не отправляют сообщения
    сами: новую версию создаёт либо последний ответ в мастере, либо явное
    «Применить изменения». `preview_only` показывает сам текст обзора (для
    проверки длинного текста и вложений) и не меняет данные.
    """
    ui = _ui(context)
    _clear_active(ui)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    if details.publication_sent and not preview_only:
        pending = _has_pending_changes(update, details)
        if saved:
            notice = (
                "✅ Черновик сохранён. Есть неприменённые изменения: "
                "нажмите «Применить изменения»."
                if pending
                else "✅ Черновик сохранён. Отправленный анонс не менялся."
            )
        return await _render_delivered(
            update, context, details, notice=notice, pending=pending
        )
    updated = details
    try:
        if not details.publication_sent:
            # Обзор доставленного анонса не возвращает его на этап правки:
            # иначе сохранённый черновик терял бы статус готового.
            updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=updated.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        return await _render_preview_error(update, context, updated, str(exc))
    if preview.draft.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(
            update, context, preview.draft, notice=notice
        )
    _set_details(ui, preview.draft)
    lines = _preview_summary_lines(preview, _user_id(update))
    lines.extend(_preview_block(preview))
    # S3 п.11: шутка пустого анонса — только в нижнем служебном экране,
    # никогда внутри самого текста анонса выше.
    if not preview.visible_text and not preview.draft.photos:
        humor = ui_humor.get_humor_line(announcement_is_empty=True)
        if humor:
            lines.extend(["", humor])
    if notice:
        lines.insert(1, notice)
    await _render(
        update,
        context,
        "\n".join(lines),
        _preview_keyboard(preview.draft, ready=preview.ready, saved=saved),
    )
    return AW_PREVIEW


async def _render_preview_error(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    reason: str,
) -> int:
    ui = _ui(context)
    if details is not None:
        _set_details(ui, details)
    _clear_active(ui)
    text = (
        "👁 Обзор анонса\n\n"
        "⚠️ Не удалось собрать предварительный текст.\n\n"
        f"Причина: {_h(reason)}\n\n"
        "Исправьте значения или дополнительные кирпичики и обновите обзор. "
        "Черновик сохранён, данные не потеряны."
    )
    rows = []
    if details is not None:
        retry_action = "aw_apply" if details.publication_sent else "aw_preview"
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=(
                        f"{retry_action}:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Изменить поля",
                    callback_data=(f"aw_qmanage:{details.draft_id}:{details.revision}"),
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "➕ Дополнительные кирпичики",
                    callback_data=(f"aw_extras:{details.draft_id}:{details.revision}"),
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await _render(update, context, text, InlineKeyboardMarkup(rows))
    return AW_PREVIEW


async def _render_length_problem(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    preview,
    check,
) -> int:
    """
    Проблемный экран превышения лимита текста (S4 §5.2).

    Данные и фото НЕ меняются: значения кирпичиков уже сохранены с CAS,
    `file_id` лежат в черновике, сообщение пользователя, вызвавшее
    превышение, не удаляется. Экран показывает фактическую длину, лимит,
    превышение и самые длинные кирпичики с прямой правкой, чтобы автор мог
    сократить текст сам — автосокращения и разрезания поста нет.
    """
    details = preview.draft
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    # Флаг «ждём сокращения»: после сохранения любого кирпичика анонс
    # пересобирается и ожидавшее фото применяется автоматически, без
    # повторного вопроса и без повторной отправки снимка.
    ui["length_problem"] = {
        "draft_id": details.draft_id,
        "revision": details.revision,
        "has_photos": check.has_photos,
    }
    longest = _longest_fields(preview, limit=3)
    lines = [
        "⚠️ Анонс не помещается в сообщение",
        "",
        f"Длина: {check.length} из {check.limit} символов "
        f"(превышение {check.excess}).",
        (
            "С фотографиями текст идёт подписью к последнему снимку: "
            f"допустимо {check.limit} символа."
            if check.has_photos
            else f"Без фотографий допустимо {check.limit} символов."
        ),
        "",
        "Данные и фотографии сохранены, ваше сообщение не удалено. "
        "Сократите текст — анонс соберётся заново сам.",
    ]
    if longest:
        lines.extend(["", "Самые длинные кирпичики:"])
        for field, length in longest:
            lines.append(f"• {_h(_short(field.title, 60))} — {length} симв.")
    lines.extend(
        [
            "",
            "Автосокращения нет: текст не режется и не уходит второй частью. "
            "Можно также отказаться от фотографий — тогда снова доступно "
            f"{announcement_text_limit(has_photos=False)} символов.",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    for field, length in longest:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"✏️ {field.title} — {length}", 60),
                    callback_data=(
                        f"aw_qgoto:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "✏️ Изменить другой кирпичик",
                callback_data=(f"aw_qmanage:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    if check.has_photos:
        rows.append(
            [
                InlineKeyboardButton(
                    "🚫 Без фотографий",
                    callback_data=(
                        f"aw_photo_drop:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


def _longest_fields(preview, *, limit: int) -> list[tuple[AnnouncementFieldState, int]]:
    """
    Самые длинные заполненные кирпичики снимка — кандидаты на сокращение.

    Длина считается по видимому тексту значения (`_field_value_text`), а не
    по HTML: автор видит в списке те же символы, что и Telegram.
    """
    measured: list[tuple[AnnouncementFieldState, int]] = []
    for field in preview.fields:
        if field.status not in {FIELD_STATUS_VALID, FIELD_STATUS_KNOWN}:
            continue
        length = len(_field_value_text(field))
        if length <= 0:
            continue
        measured.append((field, length))
    measured.sort(key=lambda item: (-item[1], item[0].brick_id))
    return measured[:limit]


def _length_problem_pending(context: ContextTypes.DEFAULT_TYPE, details) -> bool:
    """Ждёт ли этот черновик сокращения текста (S4 §5.2 п.5)."""
    if details is None:
        return False
    flag = _ui(context).get("length_problem")
    if not isinstance(flag, dict):
        return False
    return int(flag.get("draft_id") or 0) == int(details.draft_id)


async def _resume_after_shortening(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int | None:
    """
    После правки кирпичика в проблемном сценарии собирает анонс заново.

    Возвращает None, если превышения не было: обычный поток вопросов не
    меняется. Ожидавшее фото уже сохранено в черновике, поэтому повторно
    присылать его не нужно — финализация берёт его из БД. Если текст всё
    ещё длинный, `_finish_and_deliver` снова покажет проблемный экран.
    """
    if not _length_problem_pending(context, details):
        return None
    _ui(context).pop("length_problem", None)
    return await _finish_and_deliver(update, context, details)


async def _resume_current(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    if not draft_id:
        return await _render_home(update, context, notice=notice)
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        ctx.data(context)["announcement_ui"] = _fresh_ui()
        return await _render_home(
            update,
            context,
            notice="Черновик не найден или больше недоступен.",
        )
    _set_details(ui, details)
    stage = details.stage
    if stage == WizardStage.PLACE:
        ui["place"].update(
            {
                "phase": "confirm",
                "place_id": details.place_id,
                "name_id": details.place_name_id,
                "return_phase": "hub",
            }
        )
        return await _render_place_card(update, context, notice=notice)
    if stage == WizardStage.TEMPLATE:
        return await _render_template_screen(update, context, details, notice=notice)
    if stage == WizardStage.QUESTIONS:
        return await _enter_questions(update, context, details, notice=notice)
    if stage == WizardStage.EXTRAS:
        return await _render_extras(update, context, details, notice=notice)
    if details.publication_status not in {None, PUBLICATION_STATUS_SENT}:
        return await _render_publish_status(update, context, details, notice=notice)
    return await _render_preview(update, context, details, notice=notice)


async def _render_photos(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    page: int | None = None,
    clear_confirm: bool = False,
) -> int:
    """Экран отдельной коллекции фото: просмотр списка, добавление, удаление."""
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    photos = list(details.photos)
    total = len(photos)
    if page is None:
        page = int(ui.get("photos_page") or 0)
    pages = max(1, (total + PHOTO_PAGE_SIZE - 1) // PHOTO_PAGE_SIZE)
    page = max(0, min(int(page), pages - 1))
    ui["photos_page"] = page

    lines = [
        tr("wizard.photos.title"),
        "",
        f"Добавлено: {total}. Фото необязательны и не входят в шаблон: "
        "они отправляются отдельными вложениями.",
        "Можно отправить одиночные снимки или альбомы несколько раз подряд. "
        "Одинаковые file_id не дублируются.",
    ]
    if notice:
        lines.insert(1, notice)
    if clear_confirm:
        lines.extend(
            [
                "",
                "Удалить все фотографии из этого анонса? Текст и значения "
                "кирпичиков останутся без изменений.",
            ]
        )
        await _render(
            update,
            context,
            "\n".join(lines),
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "🗑 Да, удалить все",
                            callback_data=(
                                f"aw_photo_clear_yes:{details.draft_id}:"
                                f"{details.revision}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "◀️ Отмена",
                            callback_data=(
                                f"aw_photos:{details.draft_id}:"
                                f"{details.revision}:{page}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Главное меню", callback_data="main_menu"
                        )
                    ],
                ]
            ),
        )
        return AW_PREVIEW

    if total:
        start = page * PHOTO_PAGE_SIZE
        lines.extend(["", f"Страница {page + 1} из {pages}:"])
        for number, _file_id in enumerate(
            photos[start : start + PHOTO_PAGE_SIZE], start=start + 1
        ):
            lines.append(f"{number}. Фото добавлено")
        lines.append("")
        lines.append("Чтобы заменить фото, удалите его и добавьте новое.")
    else:
        lines.extend(
            [
                "",
                "Фотографий пока нет — анонс придёт без вложений.",
            ]
        )
    if details.publication_sent:
        lines.extend(
            [
                "",
                "Изменения применятся по кнопке «Применить изменения»: "
                "анонс соберётся заново и заменит прошлую версию в чате.",
            ]
        )
    if _edit_blocked(details):
        lines.extend(
            [
                "",
                "⚠️ Отправка уже начата: коллекция фотографий заморожена.",
            ]
        )

    rows: list[list[InlineKeyboardButton]] = []
    if not _edit_blocked(details):
        rows.append(
            [
                InlineKeyboardButton(
                    "📤 Добавить фото",
                    callback_data=(
                        f"aw_photo_add:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    start = page * PHOTO_PAGE_SIZE
    if not _edit_blocked(details):
        for offset in range(min(PHOTO_PAGE_SIZE, max(0, total - start))):
            index = start + offset
            rows.append(
                [
                    InlineKeyboardButton(
                        f"🗑 Удалить фото {index + 1}",
                        callback_data=(
                            f"aw_photo_del:{details.draft_id}:"
                            f"{details.revision}:{index}"
                        ),
                    )
                ]
            )
    if total and not _edit_blocked(details):
        rows.append(
            [
                InlineKeyboardButton(
                    "👁 Показать вложения",
                    callback_data=(
                        f"aw_photo_show:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(
                    f"aw_photos:{details.draft_id}:{details.revision}:{page - 1}"
                ),
            )
        )
    if page + 1 < pages:
        nav.append(
            InlineKeyboardButton(
                "▶️",
                callback_data=(
                    f"aw_photos:{details.draft_id}:{details.revision}:{page + 1}"
                ),
            )
        )
    if nav:
        rows.append(nav)
    if total and not _edit_blocked(details):
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Удалить все фото",
                    callback_data=(
                        f"aw_photo_clear:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    if details.publication_sent:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.finished.apply"),
                    callback_data=(f"aw_apply:{details.draft_id}:{details.revision}"),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ К обзору",
                callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


async def _show_media_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> tuple[bool, str]:
    """
    Показывает временный предпросмотр ровно тем же отправителем, что и пост.

    Это не публикация: сообщения учитываются как preview и удаляются после
    успешной отправки/смены фото. При сбое уже ушедшая часть запоминается
    для очистки, а данные анонса не меняются.
    """
    if not _is_private_chat(update):
        # Временный предпросмотр — тоже реальные фото от имени бота:
        # в группе/канале он недопустим по тем же причинам, что и анонс.
        return False, PRIVATE_CHAT_ONLY
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        return False, f"Не удалось собрать предпросмотр: {_short(exc, 300)}"
    if not preview.ready:
        reason = preview.issues[0] if preview.issues else "анонс не готов"
        return False, f"Предпросмотр недоступен: {_short(reason, 300)}"
    if not preview.draft.photos:
        return False, "В анонсе нет фотографий."
    user_data = ctx.data(context)
    # Контейнер продолжения: частичная отправка запоминается для очистки.
    progress: dict = {}
    try:
        # Единый доставщик Poster 2.0 (S4): тот же отправитель, что у поста.
        message_ids = await delivery.send_announcement_post(
            context.bot,
            ctx.chat(update).id,
            preview.html,
            list(preview.draft.photos),
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 — показываем честный сбой UI
        partial = [int(mid) for mid in progress.get("ids") or []]
        if partial:
            old = [int(mid) for mid in (user_data.get("preview_messages") or [])]
            screen.track_preview(user_data, old + partial)
        logger.exception("Не удалось показать предпросмотр вложений: %s", exc)
        return False, "Не удалось показать предпросмотр. Данные сохранены."
    await screen.replace_preview(context, ctx.chat(update).id, user_data, message_ids)
    return (
        True,
        "👁 Отправлен временный предпросмотр. Он не является публикацией "
        "и будет удалён после отправки или смены фото.",
    )


async def _render_publish_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Экран начатой/частичной/неудачной отправки без права правки."""
    if details.publication_status == PUBLICATION_STATUS_SENT:
        return await _render_delivered(update, context, details, notice=notice)
    if details.publication_status is None:
        return await _render_preview(update, context, details, notice=notice)
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    status = details.publication_status
    step = details.publication_step
    total = details.publication_total
    lines = [
        "📨 Отправка готового анонса в этот чат",
        "",
        *_place_lines(details),
        f"Фотографий: {len(details.publication_photos)}.",
    ]
    if notice:
        lines.insert(1, notice)
    if status == PUBLICATION_STATUS_SENDING:
        lines.extend(
            [
                "",
                "⏳ Отправка не завершена (возможно, процесс был перезапущен). "
                "Продолжение использует замороженный текст и фото и не "
                "перезапускает всю последовательность.",
            ]
        )
    elif status == PUBLICATION_STATUS_PARTIAL:
        lines.extend(
            [
                "",
                "⚠️ Анонс отправлен частично. Уже подтверждённые сообщения "
                "не отправляются повторно; можно дослать только оставшееся.",
            ]
        )
    elif status == PUBLICATION_STATUS_FAILED:
        lines.extend(
            [
                "",
                "❌ Telegram не подтвердил отправку. Ни одна операция не "
                "считается подтверждённой.",
            ]
        )
    if total:
        lines.append(f"Подтверждено операций: {min(step, total)} из {total}.")
    else:
        lines.append(
            "Сохранённого прогресса нет: первая операция могла быть принята "
            "Telegram до сбоя, поэтому повтор может создать дубль."
        )
    if details.previous_message_ids:
        lines.extend(
            [
                "",
                "В чате остаётся прошлая доставленная версия анонса: она не "
                "удаляется, пока новая не подтверждена полностью.",
            ]
        )
    if details.publication_error:
        lines.extend(["", f"Причина: {_h(details.publication_error)}"])
    lines.extend(
        [
            "",
            "Текст, значения кирпичиков и фотографии зафиксированы на момент "
            "начала отправки. Если Telegram принял последнюю часть, но ответ "
            "потерялся, повтор может создать дубль — это ограничение "
            "Telegram API. Адреса подтверждённых сообщений сохранены.",
        ]
    )
    rows: list[list[InlineKeyboardButton]] = []
    label = (
        "🔄 Продолжить отправку"
        if status == PUBLICATION_STATUS_SENDING
        else (
            "🔁 Дослать оставшееся"
            if status == PUBLICATION_STATUS_PARTIAL
            else "🔁 Повторить отправку"
        )
    )
    rows.append(
        [
            InlineKeyboardButton(
                label,
                callback_data=(f"aw_pub_resume:{details.draft_id}:{details.revision}"),
            )
        ]
    )
    if details.publication_can_reset:
        rows.append(
            [
                InlineKeyboardButton(
                    "↩️ Вернуться к редактированию",
                    callback_data=(
                        f"aw_pub_reset:{details.draft_id}:{details.revision}"
                    ),
                )
            ]
        )
    rows.append([InlineKeyboardButton("📝 Мои анонсы", callback_data="aw_start")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


def _delivery_fingerprint(text: str, photos: Sequence[str]) -> str:
    """
    Отпечаток payload — общий с живым блоком и доставкой (S4 п.1).

    Раньше адаптер звал приватную функцию легаси-мастера; теперь один
    алгоритм живёт в `delivery.py`, поэтому fingerprint живого блока и
    финальной отправки не может разойтись.
    """
    return delivery.delivery_fingerprint(text, list(photos))


def _announcement_is_empty(update: Update, details) -> bool:
    """
    Пуст ли анонс по видимому тексту (для заглушки/шутки в нижнем экране).

    Использует тот же рендер, что live-блок и итоговая отправка; ошибка
    сборки не считается пустотой — иначе шутка появлялась бы вместо
    диагностики. Фото учитываются: текст с фото не «пустой анонс».
    """
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError):
        return False
    return not preview.visible_text and not details.photos


def _has_pending_changes(update: Update, details) -> bool:
    """
    Отличается ли текущий анонс от последней доставленной версии.

    Сравнивается только отпечаток текста и фото — то, что реально видит
    автор. При ошибке сборки считаем, что изменения есть: пусть явное
    действие покажет причину, а не «всё отправлено».
    """
    if not details.publication_sent:
        return False
    if not details.delivery_fingerprint:
        return True
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError):
        return True
    return (
        _delivery_fingerprint(preview.html, preview.draft.photos)
        != details.delivery_fingerprint
    )


async def _finish_and_deliver(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """
    Собирает анонс из текущих данных и сразу доставляет его в этот чат.

    Это финальный шаг мастера: отдельного подтверждения и кнопки
    «опубликовать» нет. Повторный вызов с тем же содержимым не отправляет
    дубль: если такая версия уже доставлена, показывается экран действий.
    """
    user_id = _user_id(update)
    if not _is_private_chat(update):
        await _answer(update, PRIVATE_CHAT_ONLY, alert=True)
        return await _render_private_chat_refusal(update, context, details)
    if details.publication_status in {
        PUBLICATION_STATUS_SENDING,
        PUBLICATION_STATUS_PARTIAL,
        PUBLICATION_STATUS_FAILED,
    }:
        # Незавершённая отправка продолжается отдельной кнопкой — новая
        # версия поверх неё создала бы дубли.
        return await _render_publish_status(update, context, details, notice=notice)
    if details.stage != WizardStage.PREVIEW:
        # Сборка всегда начинается из обзора: extras/вопросы уже применены.
        try:
            details = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
    try:
        with SessionLocal() as session:
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                today=app_today(),
            )
    except (
        AnnouncementError,
        TemplateError,
        TemplateCompileError,
        BrickError,
    ) as exc:
        return await _render_preview_error(update, context, details, str(exc))
    if not preview.ready:
        reason = preview.issues[0] if preview.issues else "анонс не готов"
        return await _render_preview_error(
            update,
            context,
            preview.draft,
            f"{reason}. Заполните поля и попробуйте снова.",
        )
    # S4 §5.2: лимит проверяется ДО доставки. Превышение не лечится
    # автосокращением и не режется на части: показывается проблемный экран,
    # данные и фото остаются в черновике.
    length = check_announcement_length(
        preview.html, has_photos=bool(preview.draft.photos)
    )
    if length.over:
        return await _render_length_problem(update, context, preview, length)
    if details.publication_status == PUBLICATION_STATUS_SENT:
        current = _delivery_fingerprint(preview.html, preview.draft.photos)
        if (
            details.delivery_fingerprint == current
            and preview.draft.delivered_message_ids
        ):
            # Ничего не изменилось: дубль не отправляем.
            return await _render_delivered(
                update,
                context,
                details,
                notice=notice or "✅ Эта версия анонса уже отправлена.",
            )
    # S4 п.5: верхний медиаблок уже стоит в чате. Если его payload совпадает
    # с текущим и прошлую версию заменять не нужно, он и есть готовый пост:
    # повторная отправка создала бы второй альбом. Текстовый анонс без фото
    # доставляется отдельным сообщением (контракт S3: превью остаётся в
    # истории выше), поэтому промоушен к нему не применяется.
    if preview.draft.photos and not details.previous_message_ids:
        promoted = await _promote_live_post(
            update,
            context,
            details,
            html=preview.html,
            photos=tuple(preview.draft.photos),
        )
        if promoted is not None:
            return await _render_delivered(
                update,
                context,
                promoted,
                notice=notice or tr("wizard.ready.sent"),
            )
    try:
        with SessionLocal() as session:
            publication = begin_announcement_publication(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                expected_revision=details.revision,
                today=app_today(),
            )
    except (
        AnnouncementError,
        TemplateError,
        TemplateCompileError,
        BrickError,
    ) as exc:
        return await _render_preview_error(update, context, details, str(exc))
    _set_details(_ui(context), publication.draft)
    return await _run_announcement_publication(update, context, publication)


async def _promote_live_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    html: str,
    photos: tuple[str, ...],
):
    """
    Признаёт подтверждённый верхний блок готовым постом без новой отправки.

    S4 п.5: живой блок — это и есть анонс, который видит автор. Если его
    состав и текст совпадают с текущим payload, отправлять второй такой же
    пост нельзя: Telegram-история показала бы две копии.

    Совпадение проверяется по отпечатку, который `_sync_live` записывает
    после подтверждённой отправки/правки блока. Перед сверкой блок
    синхронизируется: после рестарта `user_data` пуст, и только фактическая
    правка/создание блока доказывает, что в чате лежит именно текущая версия.

    Возвращает обновлённый черновик, если блок стал `sent`, иначе None —
    тогда вызывающий собирает и отправляет новую версию обычным путём.
    """
    previous_ids = tuple(details.previous_message_ids)
    user_data = ctx.data(context)
    try:
        await _sync_live(update, context, details)
    except Exception as exc:  # noqa: BLE001 — без доказанной синхронизации нет финала
        logger.info("Живой блок #%s не синхронизирован: %s", details.draft_id, exc)
        return None
    state = wizard_live.live_state(user_data)
    current = _delivery_fingerprint(html, photos)
    ids = tuple(int(item) for item in state.get("ids") or () if isinstance(item, int))
    # Дефект S4-1: блок без фото не может стать готовым медиапостом. Отпечаток
    # у пустого текста один и тот же с фото и без них (подпись пустая), поэтому
    # дополнительно сверяется фактический состав показанного блока.
    shown_photos = int(state.get("photo_count") or 0)
    # Дефект U2b: счётчика фото мало — при том же числе сегментов набор
    # `file_id` мог измениться (добавили второе фото при одном сегменте).
    # Тогда в чате лежит ДРУГОЙ пост, и признавать его доставленным нельзя.
    shown_files = wizard_live.shown_file_ids(
        user_data, draft_id=details.draft_id, message_ids=ids
    )
    if (
        not ids
        or int(state.get("draft_id") or 0) != int(details.draft_id)
        or state.get("fingerprint") != current
        or shown_photos != len(photos)
        or (shown_files is not None and shown_files != photos)
    ):
        return None
    total = len(delivery.media_batches(len(photos))) or 1
    progress: dict[str, object] = {
        "fingerprint": current,
        "step": total,
        "ids": list(ids),
        "total": total,
    }
    if previous_ids:
        progress["previous_ids"] = list(previous_ids)
    try:
        with SessionLocal() as session:
            # Существующий путь: заморозка payload → подтверждённый прогресс →
            # `sent`. Триггеры 13/14 допускают этот переход, потому что
            # published_at, frozen payload и текущие `ids` согласованы.
            begin_announcement_publication(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                expected_revision=details.revision,
                today=app_today(),
            )
            record_announcement_publication_progress(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                progress=progress,
            )
            promoted = mark_announcement_publication_sent(
                session,
                actor_telegram_user_id=_user_id(update),
                draft_id=details.draft_id,
                message_ids=list(ids),
                fingerprint=current,
            )
    except Exception as exc:  # noqa: BLE001 — не выдаём успех без записи в БД
        logger.warning(
            "Живой блок #%s не удалось признать доставленным: %s",
            details.draft_id,
            exc,
        )
        return None
    replaced = [mid for mid in previous_ids if mid not in ids]
    if replaced:
        # Заменённую прошлую версию убираем только после подтверждённого
        # перехода в `sent`: до этого она оставалась единственной копией.
        await screen.cleanup_messages(context.bot, ctx.chat(update).id, replaced)
    _set_details(_ui(context), promoted)
    wizard_live.forget_live_state(user_data)
    return promoted


# U2b: экран завершения — ровно два действия. Текст фиксированный: никаких
# служебных сводок, длин и списков. Единственное допустимое дополнение —
# предупреждение о неприменённых изменениях. Сами строки — в каталоге
# config/ui_texts.yaml: владелец правит их без Python-кода.
DELIVERED_TEXT = tr("wizard.finished.text")
DELIVERED_PENDING_TEXT = tr("wizard.finished.pending")

# U2b: компактный экран редактирования готового анонса — только категории.
DELIVERED_EDIT_TEXT = tr("wizard.finished.edit_text")


def _delivered_keyboard(details) -> InlineKeyboardMarkup:
    """Ровно две кнопки в одной строке: «Изменить» и «В меню»."""
    draft_id = details.draft_id
    revision = details.revision
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.finished.edit"),
                    callback_data=f"aw_edit:{draft_id}:{revision}",
                ),
                InlineKeyboardButton(
                    tr("buttons.finished.menu"), callback_data="main_menu"
                ),
            ]
        ]
    )


def _delivered_edit_keyboard(details) -> InlineKeyboardMarkup:
    """
    Категории правки готового анонса без дублей и служебных текстов.

    «Применить изменения» — единственное действие, отправляющее новую
    версию; «Назад» возвращает на экран завершения, а не в конструктор.
    """
    draft_id = details.draft_id
    revision = details.revision
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.finished.data"),
                    callback_data=f"aw_qmanage:{draft_id}:{revision}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.finished.photos", count=len(details.photos)),
                    callback_data=f"aw_photos:{draft_id}:{revision}:0",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.finished.extras", count=len(details.extras)),
                    callback_data=f"aw_extras:{draft_id}:{revision}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.finished.apply"),
                    callback_data=f"aw_apply:{draft_id}:{revision}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.nav.back"),
                    callback_data=f"aw_panel:{draft_id}:{revision}",
                )
            ],
        ]
    )


async def _ensure_service_screen_below(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    message_ids,
) -> None:
    """
    Гарантирует порядок «готовый анонс выше, служебный экран ниже» (U2b).

    Инвариант проверяется по фактическим `message_id`: при замене версии
    пост отправляется ПОСЛЕ того, как экран уже существует, и порядок в
    чате нарушается. Telegram не переставляет сообщения, поэтому служебный
    экран удаляется и пересоздаётся следующим `_render` уже под постом.
    Удаляется только собственный экран из `screen_ref` и только в этом чате;
    отказ Telegram не ломает сценарий (экран остаётся, анонс доступен).
    """
    ids = [int(item) for item in message_ids or () if item is not None]
    if not ids:
        return
    user_data = ctx.data(context)
    ref = screen.screen_ref(user_data)
    if ref is None:
        ref = screen.hydrate_screen_ref(user_data, _user_id(update))
    if ref is None or int(ref.get("message_id") or 0) > max(ids):
        return
    chat_id = ctx.chat(update).id
    if int(ref.get("chat_id") or 0) != int(chat_id):
        return
    try:
        await context.bot.delete_message(chat_id, ref["message_id"])
    except Exception as exc:  # noqa: BLE001 — удаление best-effort
        logger.info(
            "Служебный экран %s не удалён (%s): анонс останется ниже.",
            ref["message_id"],
            exc,
        )
        return
    user_data.pop("screen", None)
    screen.save_screen_ref(_user_id(update), user_data, chat_id)


async def _render_delivered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    pending: bool = False,
) -> int:
    """
    Экран завершения: готовый анонс уже отдельным сообщением выше.

    Владелец требует короткий экран и ровно две кнопки — «Изменить» и
    «В меню». Служебные сводки (место, число фото, время, планы замены)
    убраны: они дублировали пост и растягивали экран. `notice` намеренно
    не выводится — результат действия сообщается ответом на callback, а
    текст экрана остаётся предсказуемым.
    """
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    # U2b: обязательный инвариант «пост выше экрана». При замене версии
    # сообщение поста отправляется уже после служебного экрана, поэтому
    # экран пересоздаётся ниже — до отрисовки нового экрана.
    await _ensure_service_screen_below(update, context, details.delivered_message_ids)
    # Строки читаются из каталога в момент отрисовки: правка YAML + перезапуск
    # службы меняет экран без правки кода (константы выше — те же дефолты).
    lines = [tr("wizard.finished.text")]
    if pending:
        lines.extend(["", tr("wizard.finished.pending")])
    # `notice` не попадает в текст экрана (он фиксирован требованием U2b):
    # результат действия сообщается коротким ответом на callback.
    if notice:
        await _answer(update, notice[:200])
    await _render(
        update,
        context,
        "\n".join(lines),
        _delivered_keyboard(details),
    )
    return AW_PREVIEW


async def _render_delivered_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Компактный экран редактирования готового анонса."""
    ui = _ui(context)
    _set_details(ui, details)
    _clear_active(ui)
    text = tr("wizard.finished.edit_text")
    if notice:
        text = f"{notice}\n\n{text}"
    await _render(
        update,
        context,
        text,
        _delivered_edit_keyboard(details),
    )
    return AW_PREVIEW


def _publication_error_text(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())[:500]
    name = exc.__class__.__name__
    return f"{name}: {detail}" if detail else name


async def _render_private_chat_refusal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
) -> int:
    """
    Отказ от выдачи в неличный чат: данные не теряются, отправки нет.

    Экран остаётся рабочим, чтобы автор мог открыть тот же черновик в
    личном чате и завершить его. Ничего не удаляется и не замораживается.
    """
    ui = _ui(context)
    if details is not None:
        _set_details(ui, details)
    _clear_active(ui)
    lines = [
        "⚠️ Готовый анонс не отправлен",
        "",
        PRIVATE_CHAT_ONLY,
    ]
    if details is not None:
        lines.extend(
            [
                "",
                *_place_lines(details),
                "Данные и фотографии сохранены в черновике.",
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if details is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    "👁 Обзор анонса",
                    callback_data=f"aw_preview:{details.draft_id}:{details.revision}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("📝 Мои анонсы", callback_data="aw_start")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    await _render(update, context, "\n".join(lines), InlineKeyboardMarkup(rows))
    return AW_PREVIEW


async def _render_publish_status_safely(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
) -> int:
    """Сбой перерисовки статуса не должен скрывать уже сохранённый прогресс."""
    try:
        return await _render_publish_status(update, context, details, notice=notice)
    except Exception:  # noqa: BLE001 — экран можно обновить следующим действием
        logger.exception("Не удалось перерисовать экран статуса отправки.")
        return AW_PREVIEW


async def _run_announcement_publication(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    publication,
) -> int:
    """
    Отправляет только замороженный payload и сохраняет прогресс после каждой
    подтверждённой операции. Успех фиксируется лишь после ответа Telegram.
    """
    user_id = _user_id(update)
    draft_id = publication.draft.draft_id
    chat_id = ctx.chat(update).id
    progress = dict(publication.progress)

    # S4 §5.6: с фото пустой текст уходит без подписи. В замороженном
    # payload лежит техническая заглушка (`.`) или невидимый маркер пустой
    # подписи (их требует форма публикации), но подписью они не становятся —
    # отпечаток от этого не меняется.
    text = publication.text
    if publication.photos and (
        text == PLACEHOLDER_TEXT or not delivery.visible_text(text)
    ):
        text = ""

    def _persist_progress(snapshot: dict) -> None:
        # Сбой записи НЕ отменяет уже подтверждённую отправку: id остаются
        # в памяти прогресса и будут сохранены следующей удачной записью или
        # фиксацией `sent`. Иначе повтор отправил бы подтверждённое заново.
        try:
            with SessionLocal() as session:
                record_announcement_publication_progress(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                    progress=snapshot,
                )
        except Exception as exc:  # noqa: BLE001 — прогресс досохраним позже
            logger.warning(
                "Не удалось сохранить прогресс анонса #%s: %s", draft_id, exc
            )

    # Адреса ПРОШЛОЙ доставленной версии. Их нельзя путать с id текущей
    # отправки: удалять можно только прошлую копию и только после того, как
    # новая версия подтверждена целиком.
    previous_ids = tuple(publication.draft.previous_message_ids)
    try:
        sent_ids = await delivery.send_announcement_post(
            context.bot,
            chat_id,
            text,
            list(publication.photos),
            progress=progress,
            on_progress=_persist_progress,
        )
    except Exception as exc:  # noqa: BLE001 — сбой Telegram/БД должен быть виден
        logger.exception("Отправка анонса #%s не завершена: %s", draft_id, exc)
        # Подтверждённое до сбоя могло не попасть в БД из-за сбоя записи —
        # досохраняем из памяти, чтобы продолжение не отправило дубль.
        _persist_progress(progress)
        error_text = _publication_error_text(exc)
        try:
            with SessionLocal() as session:
                details = mark_announcement_publication_failed(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                    error=error_text,
                )
        except Exception:  # noqa: BLE001 — сохраняем исходную ошибку отправки
            logger.exception(
                "Не удалось сохранить статус сбоя отправки анонса #%s",
                draft_id,
            )
            try:
                with SessionLocal() as session:
                    details = get_announcement_draft(
                        session,
                        actor_telegram_user_id=user_id,
                        draft_id=draft_id,
                    )
            except Exception:  # noqa: BLE001 — показываем хотя бы прошлый снимок
                details = publication.draft
        return await _render_publish_status_safely(
            update,
            context,
            details,
            notice="❌ Отправку не удалось подтвердить. Проверьте статус.",
        )

    try:
        with SessionLocal() as session:
            details = mark_announcement_publication_sent(
                session,
                actor_telegram_user_id=user_id,
                draft_id=draft_id,
                message_ids=sent_ids,
                fingerprint=_delivery_fingerprint(publication.text, publication.photos),
            )
    except Exception as exc:  # noqa: BLE001 — сообщения могли уже уйти
        logger.exception(
            "Telegram подтвердил отправку, но статус анонса #%s не сохранён: %s",
            draft_id,
            exc,
        )
        # Подтверждённые id уже есть в памяти: сохраняем их как прогресс,
        # чтобы следующая попытка дослала только недостающее.
        _persist_progress(progress)
        try:
            with SessionLocal() as session:
                details = get_announcement_draft(
                    session,
                    actor_telegram_user_id=user_id,
                    draft_id=draft_id,
                )
        except Exception:  # noqa: BLE001 — не врём об успехе
            details = publication.draft
        return await _render_publish_status_safely(
            update,
            context,
            details,
            notice=(
                "⚠️ Telegram подтвердил отправку, но статус не сохранился. "
                "Не повторяйте отправку вслепую; обновите экран."
            ),
        )
    await screen.cleanup_preview(context, chat_id, ctx.data(context))
    # Прошлую доставленную версию удаляем только после успешной новой
    # отправки: до этого она — единственная полная копия анонса. Telegram
    # не удаляет сообщения старше 48 часов, поэтому ошибка удаления не
    # считается ошибкой доставки: анонс уже доступен в этом чате.
    if previous_ids:
        replaced = [
            message_id for message_id in previous_ids if message_id not in sent_ids
        ]
        if replaced:
            await screen.cleanup_messages(context.bot, chat_id, replaced)
    return await _render_delivered(
        update,
        context,
        details,
        notice="✅ Анонс отправлен в этот чат. Можно изменить данные.",
    )


async def _handle_place_search_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    ui = _ui(context)
    await _reanchor(update, context)
    raw = update.effective_message.text if update.effective_message else ""
    query = (raw or "").strip()
    if not query:
        return await _render_search_prompt(
            update,
            context,
            notice="Введите непустой запрос.",
        )
    ui["place"]["query"] = query
    ui["place"]["page"] = 0
    result = await _render_search_results(update, context, 0)
    await screen.drop_user_input(update, context)
    return result


async def _reject_choice_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    message = "Выберите вариант кнопкой, текстовый ввод здесь не используется."
    screen.track_failed_input(
        ctx.data(context), f"aw:{field.brick_id}", update.effective_message
    )
    if source == "extra":
        extra = next(item for item in details.extras if item.brick_id == field.brick_id)
        return await _render_extra_value_prompt(
            update, context, details, extra, error=message
        )
    fields = await _question_fields_for(update, context, details)
    found = _find_field(fields, field.brick_id)
    if found is None:
        return await _enter_extras(update, context, details)
    return await _render_question(
        update,
        context,
        details,
        fields,
        found[0],
        editing=True,
        error=message,
    )


async def _handle_active_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    ui = _ui(context)
    brick_id = ui.get("active_brick_id")
    source = ui.get("active_source")
    if brick_id is None or source not in {"question", "extra"}:
        return None
    draft_id = ui.get("draft_id")
    if not draft_id:
        return None
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return await _stale(update, context)
    if details.revision != ui.get("revision"):
        ui["revision"] = details.revision
        return await _stale(update, context)
    _set_details(ui, details)

    if source == "question":
        fields = await _question_fields_for(update, context, details)
        found = _find_field(fields, int(brick_id))
        if found is None:
            return await _stale(update, context)
        index, field = found
        if ui.get("active_brick_id") != field.brick_id:
            return await _stale(update, context)
        await _reanchor(update, context)
        if field.value_type == BRICK_TYPE_CHOICE:
            return await _reject_choice_text(
                update, context, details, field, index, "question"
            )
        raw = capture_message_text(update.effective_message)
        return await _save_question_value(
            update,
            context,
            details,
            field,
            raw,
            index=index,
            drop_input=True,
        )

    extra = next(
        (item for item in details.extras if item.brick_id == int(brick_id)),
        None,
    )
    if extra is None:
        return await _stale(update, context)
    definition = _extra_definition(details, extra, _user_id(update))
    field = _extra_field_state(details, extra, definition)
    await _reanchor(update, context)
    if field.value_type == BRICK_TYPE_CHOICE:
        return await _reject_choice_text(update, context, details, field, -1, "extra")
    raw = capture_message_text(update.effective_message)
    return await _save_extra_value(
        update,
        context,
        details,
        extra,
        raw,
        drop_input=True,
        notice="Значение дополнительного кирпичика сохранено.",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    active = await _handle_active_text(update, context)
    if active is not None:
        return active
    ui = _ui(context)
    if ui["place"].get("phase") == "search_input" and not ui.get("draft_id"):
        return await _handle_place_search_text(update, context)
    await _reanchor(update, context)
    return await _resume_current(
        update,
        context,
        notice="Выберите действие кнопками на экране.",
    )


# Сообщения-источники фото по партиям альбома. Удаляются только после
# подтверждённой записи И только если партия принята целиком: при превышении
# лимита ввод остаётся в чате (S4 §5.2 п.2).
BATCH_INPUTS_KEY = "aw_photo_batch_inputs"


def _track_batch_input(user_data: dict[str, Any], key: str, message) -> None:
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    stored = user_data.get(BATCH_INPUTS_KEY)
    if not isinstance(stored, dict):
        stored = {}
        user_data[BATCH_INPUTS_KEY] = stored
    items = stored.setdefault(key, [])
    if int(message_id) not in items:
        items.append(int(message_id))


def _take_batch_inputs(user_data: dict[str, Any], key: str) -> list[int]:
    stored = user_data.get(BATCH_INPUTS_KEY)
    if not isinstance(stored, dict):
        return []
    items = stored.pop(key, []) or []
    if not stored:
        user_data.pop(BATCH_INPUTS_KEY, None)
    return [int(mid) for mid in items]


def _drop_batch_inputs(user_data: dict[str, Any], key: str) -> None:
    _take_batch_inputs(user_data, key)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Принимает фото и альбомы: партия целиком, затем автоматический финал.

    Telegram доставляет каждое фото альбома отдельным Update с общим
    `media_group_id`, поэтому партия собирается в буфере `wizard_photos` и
    закрывается короткой паузой (или следующим действием пользователя).
    Сохранение в SQLite идёт при закрытии партии: до её окончания анонс НЕ
    считается готовым, иначе трёхсоставный альбом дал бы три финала.

    Одиночное фото без `media_group_id` — партия из одного снимка, она
    закрывается сразу. 11-е фото отклоняется с объяснением: уже принятые
    снимки не теряются, а сообщение пользователя не удаляется.
    """
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    message = update.effective_message
    photo = message.photo[-1] if message is not None and message.photo else None
    if not draft_id or photo is None:
        await _reanchor(update, context)
        return await _resume_current(
            update, context, notice="Не удалось прочитать фотографию."
        )
    try:
        details = _session_details(_user_id(update), int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        ctx.data(context)["announcement_ui"] = _fresh_ui()
        return await _render_home(
            update, context, notice="Черновик не найден или больше недоступен."
        )
    photos_allowed = details.stage == WizardStage.PREVIEW or (
        details.publication_sent and details.stage == WizardStage.COMPLETED
    )
    if not photos_allowed:
        await _reanchor(update, context)
        return await _resume_current(
            update,
            context,
            notice=(
                "Фотографии добавляются на этапе обзора. Анонс и значения "
                "сохранены — завершите текущие шаги."
            ),
        )
    if _edit_blocked(details):
        return await _render_publish_status(
            update,
            context,
            details,
            notice=(
                "Отправка уже начата: новые фотографии не принимаются. "
                "Анонс и прогресс сохранены."
            ),
        )
    if details.publication_sent:
        # Уже доставленный анонс: партия НЕ завершает его автоматически
        # (новую версию отправляет «Применить изменения»). Поэтому ждать
        # весь альбом незачем: каждый снимок сохраняется сразу, а ввод
        # удаляется после подтверждённой записи (S4 §5.3).
        return await _add_photo_to_delivered(update, context, details, photo, message)
    user_data = ctx.data(context)
    key = wizard_photos.buffer_photo(
        user_data,
        getattr(message, "media_group_id", None),
        photo.file_id,
    )
    _track_batch_input(user_data, key, message)
    # Каждый снимок сохраняется сразу (идемпотентно, с CAS и лимитом 10):
    # рестарт посреди альбома ничего не теряет. Готовность наступает
    # отдельно — только когда партия целиком закрыта.
    try:
        batch = wizard_photos.add_photo_batch(
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            file_ids=[photo.file_id],
            # Фабрика сессий берётся из этого модуля: тесты подменяют именно
            # её временной SQLite, рабочая база не затрагивается.
            session_factory=SessionLocal,
        )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        screen.track_failed_input(user_data, f"aw-photo:{details.draft_id}", message)
        _drop_batch_inputs(user_data, key)
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=wizard_photos.rejected_photo_notice(exc),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    wizard_photos.absorb_result(user_data, key, batch)
    _set_details(ui, batch.details)
    if getattr(message, "media_group_id", None) is None:
        # Одиночный снимок — целая партия: финал наступает сразу, без паузы.
        return await _close_photo_batch(update, context, key)
    wizard_photos.schedule_album_close(
        getattr(context, "application", None),
        user_data,
        lambda: _close_photo_batch(update, context, key),
    )
    # До закрытия партии экран не перерисовываем: анонс ещё не готов.
    return AW_PREVIEW


async def _add_photo_to_delivered(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    photo,
    message,
) -> int:
    """
    Фото к уже доставленному анонсу: сохранить сразу, без автофинала.

    Готовый пост не пересобирается на каждую партию — новую версию
    отправляет явное «Применить изменения» (S4 §5.3). Поэтому ожидание
    целого альбома здесь не нужно: каждый `file_id` сохраняется с CAS,
    11-е фото отклоняется, а сообщение пользователя удаляется только после
    подтверждённой записи.
    """
    user_data = ctx.data(context)
    try:
        batch = wizard_photos.add_photo_batch(
            telegram_user_id=_user_id(update),
            draft_id=details.draft_id,
            revision=details.revision,
            file_ids=[photo.file_id],
            session_factory=SessionLocal,
        )
    except (AnnouncementError, TemplateError, TemplateCompileError, BrickError) as exc:
        # Ввод остаётся в чате: пользователь видит, что именно не принято.
        screen.track_failed_input(user_data, f"aw-photo:{details.draft_id}", message)
        return await _render_photos(
            update,
            context,
            details,
            notice=wizard_photos.rejected_photo_notice(exc),
        )
    _set_details(_ui(context), batch.details)
    if batch.changed or batch.duplicate:
        await screen.drop_user_input(update, context)
    return await _render_photos(
        update,
        context,
        batch.details,
        notice=wizard_photos.batch_notice(batch),
    )


async def _close_photo_batch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    key: str,
) -> int:
    """
    Закрывает партию фото: решает, готов ли анонс, и запускает автофинал.

    Снимки уже сохранены по одному; здесь фиксируется «партия получена
    целиком». Отказ по лимиту 10 не теряет принятые фото и оставляет
    сообщение пользователя в чате. Принятая партия удаляет ввод только
    после подтверждённой записи.
    """
    user_data = ctx.data(context)
    ui = _ui(context)
    draft_id = ui.get("draft_id")
    wizard_photos.take_batch(user_data, key)
    result = wizard_photos.take_result(user_data, key)
    if not draft_id or result is None:
        return AW_PREVIEW
    user_id = _user_id(update)
    try:
        details = _session_details(user_id, int(draft_id))
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        return await _resume_current(update, context)
    inputs = _take_batch_inputs(user_data, key)
    if result.rejected:
        # Лимит 10: объясняем отказ, принятые фото сохранены, ввод оставляем
        # в чате — пользователь видит, что именно не принято.
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=wizard_photos.limit_notice(result),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    # Партия подтверждённо сохранена. Ввод удаляется только после
    # ПОДТВЕРЖДЁННОЙ доставки: при превышении лимита экран обещает, что
    # сообщение пользователя осталось (S4 §5.2 п.2), и это должно быть правдой.
    # Исключение — фото к уже доставленному анонсу: там ввод больше не нужен
    # (см. ветку `publication_sent`).
    _set_details(ui, details)
    if details.publication_sent:
        # Фото добавлены к уже доставленному анонсу: новая версия — только по
        # «Применить изменения», иначе каждая партия создавала бы замену.
        await screen.cleanup_messages(context.bot, ctx.chat(update).id, inputs)
        return await _render_photos(
            update,
            context,
            details,
            notice=wizard_photos.batch_notice(result),
        )
    # Вся партия принята → анонс готов автоматически, без кнопки «Готово».
    state = await _finish_and_deliver(
        update,
        context,
        details,
        notice=wizard_photos.batch_notice(result) or None,
    )
    # Ввод удаляется только после ПОДТВЕРЖДЁННОЙ доставки (S4 §5.2 п.2):
    # при превышении лимита экран обещает, что сообщение пользователя
    # осталось, и это должно быть правдой.
    try:
        delivered = _session_details(user_id, details.draft_id)
    except (AnnouncementNotFoundError, AnnouncementActorError, AnnouncementError):
        delivered = None
    if delivered is not None and delivered.publication_sent:
        await screen.cleanup_messages(context.bot, ctx.chat(update).id, inputs)
    return state


async def _exit_to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_ui(context)
    _clear_other_sessions(_user_id(update))
    from handlers.main_menu import render_main_menu

    await render_main_menu(update, context)
    return ConversationHandler.END


async def _close_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_ui(context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    await _render(
        update,
        context,
        "Конструктор анонса закрыт. Сохранённый черновик не удалён — "
        "продолжить можно в «📂 Мои анонсы».",
        _main_menu_keyboard(),
    )
    await screen.drop_user_input(update, context)
    return ConversationHandler.END


async def handle_navigation_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = ctx.query(update)
    data = query.data or ""
    if data == "main_menu":
        await _answer(update)
        return await _exit_to_main_menu(update, context)
    if data == "drafts":
        # /drafts и старый callback ведут в единый список «📂 Мои анонсы»:
        # легаси-хранилища Draft больше нет (S5).
        await _answer(update)
        _clear_ui(context)
        return await open_my_announcements(update, context)
    if data == "places":
        _clear_ui(context)
        from handlers.places import open_places

        await open_places(update, context)
        return ConversationHandler.END
    if data == "bricks":
        _clear_ui(context)
        from handlers.bricks import open_bricks

        await open_bricks(update, context)
        return ConversationHandler.END
    if data == "templates":
        _clear_ui(context)
        from handlers.templates import open_templates

        await open_templates(update, context)
        return ConversationHandler.END
    if data == "admin_panel":
        _clear_ui(context)
        from handlers.admin import open_admin

        await open_admin(update, context)
        return ConversationHandler.END
    if data == "help":
        await _answer(update)
        _clear_ui(context)
        from handlers.main_menu import render_help

        await render_help(update, context)
        return ConversationHandler.END
    if data == "create_post":
        # Старого мастера больше нет (S5): устаревшая кнопка не запускает
        # ничего, а ведёт в меню перерисовкой существующего экрана.
        await _answer(update, "Кнопка устарела: выберите раздел в меню.", alert=True)
        return await _exit_to_main_menu(update, context)
    if data == "restore_wip":
        await _answer(
            update,
            "Незавершённые анонсы открываются в «📂 Мои анонсы».",
            alert=True,
        )
        return await _exit_to_main_menu(update, context)
    if data == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    await _answer(update, "Кнопка устарела.", alert=True)
    return await _resume_current(update, context, notice=STALE_TEXT)


async def handle_unexpected_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await _answer(update, "Кнопка устарела.", alert=True)
    return await _resume_current(update, context, notice=STALE_TEXT)


async def _command_to_section(
    update: Update, context: ContextTypes.DEFAULT_TYPE, target: str
) -> int:
    _clear_ui(context)
    _clear_other_sessions(_user_id(update))
    if target == "drafts":
        # Единый список анонсов вместо легаси-черновиков (S5).
        return await open_my_announcements(update, context)
    if target == "places":
        from handlers.places import open_places

        await open_places(update, context)
        return ConversationHandler.END
    if target == "bricks":
        from handlers.bricks import open_bricks

        await open_bricks(update, context)
        return ConversationHandler.END
    if target == "templates":
        from handlers.templates import open_templates

        await open_templates(update, context)
        return ConversationHandler.END
    if target == "admin":
        from handlers.admin import open_admin

        await open_admin(update, context)
        return ConversationHandler.END
    if target == "help":
        from handlers.main_menu import render_help

        await render_help(update, context)
        return ConversationHandler.END
    from handlers.main_menu import render_main_menu

    await render_main_menu(update, context)
    return ConversationHandler.END


def _callback_parts(update: Update) -> list[str]:
    return (ctx.query(update).data or "").split(":")


async def handle_place_state_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action == "aw_new":
        await _answer(update)
        _reset_for_place(context)
        return await _render_place_hub(update, context)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_home_page" and len(parts) == 2:
        await _answer(update)
        if ui["place"].get("phase") != "home":
            return await _stale(update, context)
        try:
            ui["home_page"] = max(0, int(parts[1]))
        except ValueError:
            return await _stale(update, context)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_open" and len(parts) == 2:
        await _answer(update)
        try:
            draft_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _open_draft(update, context, draft_id)

    if action == "aw_place_search":
        await _answer(update)
        return await _render_search_prompt(update, context)
    if action == "aw_place_all" and len(parts) == 2:
        await _answer(update)
        try:
            page = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _render_all_places(update, context, page)
    if action == "aw_place_results" and len(parts) == 2:
        if ui["place"].get("phase") != "results":
            return await _stale(update, context)
        await _answer(update)
        try:
            page = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        return await _render_search_results(update, context, page)
    if action == "aw_place_open" and len(parts) == 2:
        try:
            place_id = int(parts[1])
        except ValueError:
            return await _stale(update, context)
        phase = ui["place"].get("phase")
        with SessionLocal() as session:
            details = get_place_details(session, place_id)
            if phase == "results":
                query = str(ui["place"].get("query") or "")
                visible = {result.place_id for result in search_places(session, query)}
                if place_id not in visible:
                    details = None
        if details is None:
            return await _stale(update, context)
        await _answer(update)
        ui["place"].update(
            {
                "place_id": place_id,
                "name_id": details.canonical_name_id,
                "return_phase": phase if phase in {"results", "all"} else "hub",
                "return_page": int(ui["place"].get("page") or 0),
            }
        )
        return await _render_place_card(update, context)
    if action == "aw_place_names" and len(parts) == 3:
        try:
            place_id = int(parts[1])
            page = int(parts[2])
        except ValueError:
            return await _stale(update, context)
        if ui["place"].get("place_id") != place_id:
            return await _stale(update, context)
        await _answer(update)
        return await _render_place_names(update, context, page)
    if action == "aw_place_name_pick" and len(parts) == 4:
        try:
            place_id = int(parts[1])
            name_id = int(parts[2])
            int(parts[3])
        except ValueError:
            return await _stale(update, context)
        if ui["place"].get("place_id") != place_id:
            return await _stale(update, context)
        with SessionLocal() as session:
            details = get_place_details(session, place_id)
        if details is None or name_id not in {name.id for name in details.names}:
            return await _stale(update, context)
        await _answer(update)
        ui["place"]["name_id"] = name_id
        return await _render_place_card(update, context)
    if action == "aw_place_back":
        await _answer(update)
        return await _place_back(update, context)
    if action == "aw_place_confirm" and len(parts) == 3:
        try:
            place_id = int(parts[1])
            name_id = int(parts[2])
        except ValueError:
            return await _stale(update, context)
        return await _confirm_place(update, context, place_id, name_id)

    return await _stale(update, context)


async def handle_template_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    if action == "aw_cont" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        await _answer(update)
        return await _resume_current(update, context)
    if action == "aw_tplpage" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        ui, details = guarded
        ui["template_page"] = int(parts[3])
        await _answer(update)
        return await _render_template_screen(update, context, details)
    if action == "aw_template" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        return await _select_template(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def _handle_calendar_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Callback'и компактного календаря (U2a).

    Новые действия: `aw_calquick` (первый экран), `aw_calrange` (раскрытие
    семидневного диапазона), `aw_calmonths` (сетка 12 месяцев),
    `aw_calmonth` (дни месяца), `aw_calday` (выбор даты без подтверждения).
    Старые `aw_calroot` / `aw_calweek` / `aw_calother` / `aw_calweekdays`
    остались в чатах с прежними сообщениями: они не падают, а отвечают и
    перенаправляют на новый первый экран.
    """
    parts = _callback_parts(update)
    action = parts[0]
    if len(parts) < 4:
        return await _stale(update, context)
    try:
        draft_id = int(parts[1])
        revision = int(parts[2])
        brick_id = int(parts[3])
    except ValueError:
        return await _stale(update, context)
    found = _calendar_context(update, context, draft_id, revision, brick_id)
    if found is None:
        return await _stale(update, context)
    details, field, index, source, bounds = found
    start, end = bounds
    allowed = (start, end)
    today = app_today()

    if action == "aw_calquick":
        await _answer(update)
        return await _render_calendar_value(
            update, context, details, field, index, source
        )
    if action in {"aw_calroot", "aw_calweek", "aw_calother", "aw_calweekdays"}:
        # Совместимость со старыми сообщениями: экраны недель и «другой
        # даты» удалены, поэтому кнопка безопасно возвращает к новому
        # первому экрану с быстрыми датами.
        return await _legacy_calendar_redirect(
            update, context, details, field, index, source
        )
    if action == "aw_calrange" and len(parts) == 5:
        try:
            range_index = int(parts[4])
        except ValueError:
            return await _stale(update, context)
        ranges = calendar_view.followup_ranges(today, allowed)
        if range_index < 0 or range_index >= len(ranges):
            return await _stale(update, context)
        value_range = ranges[range_index]
        await _answer(update)
        return await _render_calendar_range_days(
            update, context, details, field, index, source, value_range
        )
    if action == "aw_calmonths":
        await _answer(update)
        return await _render_calendar_months(
            update, context, details, field, index, source
        )
    if action == "aw_calmonth" and len(parts) == 6:
        try:
            key = (int(parts[4]), int(parts[5]))
        except ValueError:
            return await _stale(update, context)
        # Год и месяц проверяются ДО month_days: подделанный callback
        # (`:0:5`, `:-1:5`, `:99999:12`) раньше доходил до date(...) и
        # отдавал пользователю «Произошла внутренняя ошибка».
        if not calendar_view.is_valid_month_key(key):
            return await _stale(update, context)
        if not calendar_view.month_days(key, allowed):
            return await _stale(update, context)
        await _answer(update)
        return await _render_calendar_month(
            update, context, details, field, index, source, key
        )
    if action == "aw_calday" and len(parts) == 5:
        try:
            day = date.fromisoformat(parts[4])
        except ValueError:
            return await _stale(update, context)
        await _answer(update)
        return await _save_calendar_day(
            update, context, details, field, index, source, day
        )
    if action == "aw_calback":
        await _answer(update)
        return await _render_calendar_value(
            update, context, details, field, index, source
        )
    return await _stale(update, context)


async def _legacy_calendar_redirect(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    """Мягкий переход со старого экрана календаря на актуальный первый."""
    await _answer(update, "Календарь обновлён.")
    return await _render_calendar_value(update, context, details, field, index, source)


async def handle_question_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action.startswith("aw_cal"):
        return await _handle_calendar_callback(update, context)
    if action == "aw_choice" and len(parts) == 5:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        await _answer(update)
        return await _handle_choice(
            update,
            context,
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
        )
    if action == "aw_choice_done" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        await _answer(update)
        return await _handle_choice_done(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )

    if action == "aw_timeset" and len(parts) >= 5:
        # S3 п.5: быстрая кнопка времени. Путь сохранения тот же, что у
        # ручного ввода: CAS по revision, drop_user_input, перерисовка
        # вопроса с уже обновлённым верхним блоком.
        # Значение содержит двоеточие («18:00»), поэтому callback разбит
        # больше чем на пять частей — остаток склеиваем обратно.
        if not all(item.isdigit() for item in parts[1:4]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        found = _active_field(
            update, context, int(parts[1]), int(parts[2]), int(parts[3])
        )
        if found is None:
            return await _stale(update, context)
        _owned, field, index, _source = found
        if field.value_type != BRICK_TYPE_TIME:
            return await _stale(update, context)
        preset = ":".join(parts[4:])
        await _answer(update, preset)
        return await _save_question_value(
            update,
            context,
            details,
            field,
            preset,
            index=index,
            drop_input=True,
        )

    if action == "aw_qmanage" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_question_manager(update, context, details)
    if action == "aw_qgoto" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        try:
            updated = _ensure_stage(update, context, details, WizardStage.QUESTIONS)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        fields = await _question_fields_for(update, context, updated)
        field_found = _find_field(fields, int(parts[3]))
        if field_found is None:
            return await _stale(update, context)
        index, field = field_found
        ui["active_brick_id"] = field.brick_id
        ui["active_source"] = "question"
        await _answer(update)
        return await _render_question(update, context, updated, fields, index)
    if action in {"aw_qback", "aw_qnext", "aw_qedit", "aw_qskip"} and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        found = await _question_from_callback(
            update,
            context,
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
        )
        if found is None:
            return await _stale(update, context)
        _ui_data, details, fields, index, field = found
        await _answer(update)
        if action == "aw_qback":
            if index <= 0:
                return await _render_template_screen(update, context, details)
            return await _render_question(update, context, details, fields, index - 1)
        if action == "aw_qnext":
            return await _advance_question(update, context, details, index + 1)
        if action == "aw_qedit":
            return await _render_question(
                update, context, details, fields, index, editing=True
            )
        return await _skip_question(update, context, details, field, index)
    if action == "aw_manage_back":
        draft_id = ui.get("draft_id")
        revision = ui.get("revision")
        guarded = None
        if draft_id and revision:
            guarded = _guard_details(update, context, int(draft_id), int(revision))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        if details.stage == WizardStage.PREVIEW:
            return await _render_preview(update, context, details)
        if details.stage == WizardStage.EXTRAS:
            return await _render_extras(update, context, details)
        return await _render_template_screen(update, context, details)
    if action == "aw_extras" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _enter_extras(update, context, details)
    if action == "aw_preview" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def handle_extras_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]
    ui = _ui(context)

    if action.startswith("aw_cal") or action in {
        "aw_choice",
        "aw_choice_done",
    }:
        return await handle_question_callback(update, context)
    if action in {"aw_apply", "aw_send", "aw_panel", "aw_save"}:
        # Эти действия живут на экране обзора/управления, но доступны и с
        # экрана доп. кирпичиков — иначе правку нельзя было бы применить.
        return await handle_preview_callback(update, context)

    if action == "aw_extras" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _enter_extras(update, context, details)
    if action == "aw_exadd" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        try:
            updated = _ensure_stage(update, context, details, WizardStage.EXTRAS)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        await _answer(update)
        return await _render_available_bricks(update, context, updated)
    if action == "aw_exbpage" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_available_bricks(
            update, context, details, page=int(parts[3])
        )
    if action == "aw_exbrick" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        brick_id = int(parts[3])
        if not any(
            brick.id == brick_id
            for brick in _available_extra_bricks(details, _user_id(update))
        ):
            return await _stale(
                update,
                context,
                "Этот кирпичик недоступен. Выберите из списка.",
            )
        await _answer(update)
        return await _render_extra_sections(update, context, details, brick_id)
    if action == "aw_exsec" and len(parts) == 5:
        try:
            draft_id = int(parts[1])
            revision = int(parts[2])
            brick_id = int(parts[3])
        except ValueError:
            return await _stale(update, context)
        section = parts[4]
        if section not in SECTION_LABELS:
            return await _stale(update, context)
        guarded = _guard_details(update, context, draft_id, revision)
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        flow = ui.get("extra_flow") or {}
        if flow.get("brick_id") != brick_id or flow.get("phase") != "section":
            return await _stale(update, context)
        await _answer(update)
        return await _render_extra_position(update, context, details, brick_id, section)
    if action == "aw_expos" and len(parts) == 6:
        try:
            draft_id = int(parts[1])
            revision = int(parts[2])
            brick_id = int(parts[3])
        except ValueError:
            return await _stale(update, context)
        section = parts[4]
        position = parts[5]
        guarded = _guard_details(update, context, draft_id, revision)
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        flow = ui.get("extra_flow") or {}
        if (
            flow.get("brick_id") != brick_id
            or flow.get("phase") != "position"
            or flow.get("section") != section
        ):
            return await _stale(update, context)
        await _answer(update)
        return await _add_extra(update, context, details, brick_id, section, position)
    if action == "aw_exback" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        flow = ui.get("extra_flow") or {}
        await _answer(update)
        if flow.get("phase") == "position":
            brick_id = int(flow.get("brick_id") or 0)
            return await _render_extra_sections(update, context, details, brick_id)
        if flow.get("phase") == "section":
            return await _render_available_bricks(
                update, context, details, page=ui.get("extras_page", 0)
            )
        ui["extra_flow"] = None
        return await _render_extras(update, context, details)
    if action == "aw_exdel" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        extra_id = int(parts[3])
        extra = next(
            (item for item in details.extras if item.extra_id == extra_id),
            None,
        )
        if extra is None:
            return await _stale(update, context)
        try:
            with SessionLocal() as session:
                updated = remove_announcement_extra(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    extra_id=extra_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        await _answer(update, "Дополнительный кирпичик удалён.")
        return await _render_extras(
            update,
            context,
            updated,
            notice=f"Удалён кирпичик «{_short(extra.title, 50)}».",
        )
    if action == "aw_exedit" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        brick_id = int(parts[3])
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None:
            return await _stale(update, context)
        await _answer(update)
        return await _render_extra_value_prompt(update, context, details, extra)
    if action == "aw_exskip" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        brick_id = int(parts[3])
        extra = next(
            (item for item in details.extras if item.brick_id == brick_id),
            None,
        )
        if extra is None or ui.get("active_brick_id") != brick_id:
            return await _stale(update, context)
        await _answer(update)
        return await _save_extra_value(
            update,
            context,
            details,
            extra,
            "",
            notice="Значение дополнительного кирпичика пропущено.",
        )
    if action == "aw_exbackval" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        _clear_active(ui)
        await _answer(update)
        return await _render_extras(update, context, details)
    if action == "aw_qmanage" and len(parts) == 3:
        return await handle_question_callback(update, context)
    if action == "aw_preview" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


async def handle_preview_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    parts = _callback_parts(update)
    action = parts[0]

    def _guided() -> tuple[dict[str, Any], Any] | None:
        if len(parts) < 3 or not all(item.isdigit() for item in parts[1:3]):
            return None
        return _guard_details(update, context, int(parts[1]), int(parts[2]))

    if action == "aw_save" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        if details.publication_sent:
            # Анонс уже доставлен: «сохранить» здесь означает выйти, не
            # трогая отправленную версию. Этап и снимок не меняются.
            await _answer(update, "Сохранено.")
            pending = _has_pending_changes(update, details)
            return await _render_delivered(
                update,
                context,
                details,
                notice=(
                    "✅ Черновик сохранён. Есть неприменённые изменения: "
                    "нажмите «Применить изменения»."
                    if pending
                    else "✅ Черновик сохранён. Отправленный анонс не менялся."
                ),
                pending=pending,
            )
        try:
            updated = _ensure_stage(update, context, details, WizardStage.PREVIEW)
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        await _answer(update, "Черновик сохранён.")
        return await _render_preview(
            update,
            context,
            updated,
            notice="✅ Черновик сохранён. Анонс в чат не отправлялся.",
            saved=True,
        )
    if action == "aw_send" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, "Отправляю анонс…")
        return await _finish_and_deliver(update, context, details)
    if action == "aw_apply" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, "Применяю изменения…")
        return await _finish_and_deliver(update, context, details)
    if action == "aw_preview" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details, preview_only=True)
    if action == "aw_edit" and len(parts) == 3:
        # U2b: «✏️ Изменить» на экране завершения открывает компактный экран
        # категорий правки, а не конструктор и не длинный служебный список.
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _render_delivered_edit(update, context, details)
    if action == "aw_panel" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_delivered(
            update,
            context,
            details,
            pending=_has_pending_changes(update, details),
        )
    if action == "aw_qmanage" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        return await handle_question_callback(update, context)
    if action == "aw_extras" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _enter_extras(update, context, details)

    # --- фотографии -----------------------------------------------------
    if action == "aw_photos" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_photos(update, context, details, page=int(parts[3]))
    if action == "aw_photo_back" and len(parts) == 3:
        # U2b: «Назад» с шага фото — к предыдущему шагу (последний кирпичик),
        # без потери уже сохранённых фото и значений. Механизм тот же, что у
        # навигации по вопросам: `_render_question` с последним кирпичиком.
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _back_from_photo_step(update, context, details)
    if action == "aw_photo_none" and len(parts) == 3:
        # S4 п.2: «Без фотографий» на шаге фото — это финал: шаг собран в
        # `wizard_photos`, доставка идёт общим путём без кнопки «Готово».
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update, "Продолжаем без фотографий.")
        return await _finish_and_deliver(
            update,
            context,
            details,
            notice="📷 Анонс останется без вложений.",
        )
    if action == "aw_photo_drop" and len(parts) == 3:
        # Проблемный экран §5.2 п.4: отказ от фотографий возвращает лимит
        # 4096 и НЕ теряет текст. Это подтверждённый отказ, поэтому фото
        # убираются из черновика — иначе доставка снова упёрлась бы в 1024.
        # Вернуть снимки можно через «Фотографии».
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        try:
            with SessionLocal() as session:
                updated = clear_announcement_photos(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _render_publish_status(update, context, details)
        _set_details(_ui(context), updated)
        _ui(context).pop("length_problem", None)
        await _answer(update, "Лимит снова 4096 символов.")
        return await _finish_and_deliver(
            update,
            context,
            updated,
            notice=(
                "🚫 Отправляем без фотографий: лимит снова 4096 символов. "
                "Снимки убраны из черновика — вернуть их можно через "
                "«📷 Фотографии»."
            ),
        )
    if action == "aw_photo_add" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await wizard_photos.render_photo_step(
            update,
            context,
            details,
            notice=(
                "Отправьте одну фотографию или альбом — анонс готовится "
                "сразу после получения всей партии."
            ),
            announcement_is_empty=_announcement_is_empty(update, details),
        )
    if action == "aw_photo_del" and len(parts) == 4:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Коллекция фото заморожена.", alert=True)
            return await _render_publish_status(update, context, details)
        index = int(parts[3])
        try:
            with SessionLocal() as session:
                updated = remove_announcement_photo(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    index=index,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _render_photos(
                update, context, details, notice=f"Фото не удалено: {_short(exc, 300)}"
            )
        _set_details(_ui(context), updated)
        await _answer(update, "Фото удалено.")
        await screen.cleanup_preview(context, ctx.chat(update).id, ctx.data(context))
        return await _render_photos(
            update,
            context,
            updated,
            notice="✅ Фото удалено. Чтобы заменить его, добавьте новое.",
            page=index // PHOTO_PAGE_SIZE,
        )
    if action == "aw_photo_clear" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Коллекция фото заморожена.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        return await _render_photos(update, context, details, clear_confirm=True)
    if action == "aw_photo_clear_yes" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Коллекция фото заморожена.", alert=True)
            return await _render_publish_status(update, context, details)
        try:
            with SessionLocal() as session:
                updated = clear_announcement_photos(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _render_photos(
                update, context, details, notice=f"Фото не удалены: {_short(exc, 300)}"
            )
        _set_details(_ui(context), updated)
        await _answer(update, "Все фотографии удалены.")
        await screen.cleanup_preview(context, ctx.chat(update).id, ctx.data(context))
        return await _render_photos(
            update, context, updated, notice="✅ Все фотографии удалены."
        )
    if action == "aw_photo_show" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        ok, notice = await _show_media_preview(update, context, details)
        return await _render_photos(
            update,
            context,
            details,
            notice=notice if ok else f"⚠️ {notice}",
        )
    if action == "aw_media_preview" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if _edit_blocked(details):
            await _answer(update, "Отправка уже начата.", alert=True)
            return await _render_publish_status(update, context, details)
        await _answer(update)
        ok, notice = await _show_media_preview(update, context, details)
        return await _render_preview(
            update,
            context,
            details,
            notice=notice if ok else f"⚠️ {notice}",
        )

    # --- отправка готового анонса ---------------------------------------
    if action == "aw_pub_resume" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        if not _is_private_chat(update):
            await _answer(update, PRIVATE_CHAT_ONLY, alert=True)
            return await _render_private_chat_refusal(update, context, details)
        try:
            with SessionLocal() as session:
                publication = resume_announcement_publication(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _resume_current(update, context, notice=str(exc))
        _set_details(_ui(context), publication.draft)
        await _answer(update, "Продолжаю отправку…")
        return await _run_announcement_publication(update, context, publication)
    if action == "aw_pub_reset" and len(parts) == 3:
        guarded = _guided()
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        try:
            with SessionLocal() as session:
                updated = reset_failed_announcement_publication(
                    session,
                    actor_telegram_user_id=_user_id(update),
                    draft_id=details.draft_id,
                    expected_revision=details.revision,
                )
        except AnnouncementError as exc:
            await _answer(update, str(exc), alert=True)
            return await _render_publish_status(update, context, details)
        _set_details(_ui(context), updated)
        await _answer(update, "Можно снова редактировать анонс.")
        return await _render_preview(
            update,
            context,
            updated,
            notice=(
                "↩️ Публикация не начиналась: можно изменить поля, extras "
                "или фотографии."
            ),
        )

    if action == "aw_new":
        await _answer(update)
        _reset_for_place(context)
        return await _render_place_hub(update, context)
    if action == "aw_home":
        await _answer(update)
        return await _render_home(update, context)
    if action == "aw_start":
        await _answer(update)
        return await _render_home(update, context)
    return await _stale(update, context)


def _common_state_handlers() -> list[BaseHandler]:
    return [
        CallbackQueryHandler(
            handle_navigation_callback,
            pattern=(
                r"^(main_menu|drafts|places|bricks|templates|"
                r"admin_panel|help|create_post|restore_wip)$"
            ),
        )
    ]


async def open_new_wizard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def start_place_announcement_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    place_id: int,
) -> int:
    """
    Немедленный старт анонса по выбранной площадке (Poster 2.0 S2).

    Тап по названию в списке площадок не задаёт вопросов: место
    подтверждается программно, снимком становится стандартный шаблон
    площадки, и пользователь сразу попадает к вопросам. Идемпотентность
    обеспечивает сервис: повтор с тем же черновиком не создаёт второй.
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    # R1, дефект №23: старт конструктора — вход в актуальный UI; у клиента
    # могла остаться reply-клавиатура старых версий.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_id = _user_id(update)
    ui = _fresh_ui()
    ctx.data(context)["announcement_ui"] = ui
    try:
        with SessionLocal() as session:
            details = start_announcement_wizard(
                session,
                actor_telegram_user_id=user_id,
                place_id=place_id,
                idempotency_key=ui["place"]["idempotency_key"] or uuid.uuid4().hex,
            )
            details = confirm_announcement_place(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                expected_revision=details.revision,
            )
            standard = get_standard_template(session, user_id, place_id)
            details = choose_announcement_template(
                session,
                actor_telegram_user_id=user_id,
                draft_id=details.draft_id,
                template_id=standard.template_id,
                expected_revision=details.revision,
                expected_template_revision=standard.revision,
            )
    except AnnouncementNotFoundError:
        await _answer(update, "Площадка или её шаблон больше недоступны.", alert=True)
        return await _render_home(update, context)
    except (AnnouncementError, TemplateError) as exc:
        await _answer(update, str(exc), alert=True)
        return await _render_home(update, context, notice=str(exc))
    _set_details(ui, details)
    return await _enter_questions(update, context, details)


async def start_personal_announcement_pick(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    template_id: int,
) -> int:
    """
    Немедленный старт анонса по личному шаблону (Poster 2.0 S2).

    Снимок шаблона создаётся сервисом S1 сразу; площадки у такого анонса нет.
    Чужой или удалённый шаблон неотличим от несуществующего.
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    # R1, дефект №23: старт конструктора — вход в актуальный UI.
    await screen.drop_legacy_reply_keyboard(update, context)
    user_id = _user_id(update)
    ui = _fresh_ui()
    ctx.data(context)["announcement_ui"] = ui
    try:
        with SessionLocal() as session:
            details = start_announcement_wizard(
                session,
                actor_telegram_user_id=user_id,
                template_id=template_id,
                origin="personal",
                idempotency_key=ui["place"]["idempotency_key"] or uuid.uuid4().hex,
            )
    except AnnouncementNotFoundError:
        await _answer(
            update, "Личный шаблон не найден или больше недоступен.", alert=True
        )
        return await _render_my_announcements(update, context)
    except (AnnouncementError, TemplateError) as exc:
        await _answer(update, str(exc), alert=True)
        return await _render_my_announcements(update, context, notice=str(exc))
    _set_details(ui, details)
    return await _enter_questions(update, context, details)


async def open_my_announcements(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Пункт меню «📂 Мои анонсы»: история и незавершённые черновики.

    Чтение только через `list_announcement_drafts` (автор = текущий
    пользователь); чужой `draft_id` в callback откроется как «не найден».
    """
    await _answer(update)
    _end_foreign_conversations(update, context)
    _clear_other_sessions(_user_id(update))
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def _render_my_announcements(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    notice: str | None = None,
) -> int:
    return await _render_home(update, context, notice=notice)


async def _command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    `/start` внутри активного конструктора.

    Обычный `/start` закрывает конструктор и показывает меню; deep-link
    payload (передача прав или ссылка на создание анонса площадки)
    обрабатывается по общим правилам, иначе открытие ссылки внутри диалога
    молча возвращало бы в меню (Poster 2.2 U4b).
    """
    from handlers.place_invite import open_invite_payload

    invite_state = await open_invite_payload(update, context)
    if invite_state is not None:
        return invite_state
    return await _exit_to_main_menu(update, context)


async def _command_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _close_wizard(update, context)


async def _command_new_wizard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    await _reanchor(update, context)
    ctx.data(context)["announcement_ui"] = _fresh_ui()
    return await _render_home(update, context)


async def _command_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "drafts")


async def _command_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "places")


async def _command_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "bricks")


async def _command_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "templates")


async def _command_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "admin")


async def _command_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _command_to_section(update, context, "help")


async def handle_unknown_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _reanchor(update, context)
    await _render(
        update,
        context,
        "Конструктор анонса активен. Используйте кнопки экрана или /cancel, "
        "чтобы закрыть его без удаления черновика.",
        _main_menu_keyboard(),
    )


def remember_conversation_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: object,
) -> None:
    """
    Запоминает состояние диалога анонсов, начатого вне его entry-точки.

    «➕ Новая площадка» создаётся обработчиком справочника, а конструктор —
    отдельный ConversationHandler. Состояние выставляется явно, иначе
    следующий текст пользователя не попал бы в вопросы анонса. Ошибка не
    ломает уже нарисованный экран: пользователь продолжит кнопками.
    """
    application = getattr(context, "application", None)
    if application is None:
        return
    for group in application.handlers.values():
        for handler in group:
            if not isinstance(handler, ConversationHandler):
                continue
            if getattr(handler, "name", None) != CONVERSATION_NAME:
                continue
            try:
                key = handler._get_key(update)  # noqa: SLF001 — старт вне entry
                handler._update_state(state, key)  # noqa: SLF001
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не удалось открыть диалог анонса: %s", exc)
            return


async def handle_pick_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """
    Точки входа Poster 2.0 из списков площадок и личных шаблонов.

    Callback приходит с `pl_pick:<id>` (площадка) или `tp_pick:<id>`
    (личный шаблон). Здесь же обрабатывается меню «Мои анонсы»; все ветки
    перепроверяют доступ доменными сервисами, поэтому подделанный id
    открывает только «не найдено».
    """
    data = (ctx.query(update).data or "").split(":")
    action = data[0]
    if action == "pl_pick" and len(data) == 2 and data[1].isdigit():
        return await start_place_announcement_pick(update, context, int(data[1]))
    if action == "tp_pick" and len(data) == 2 and data[1].isdigit():
        return await start_personal_announcement_pick(update, context, int(data[1]))
    await _answer(update, EXPIRED_TEXT, alert=True)
    return ConversationHandler.END


def announcement_wizard_handlers() -> list[BaseHandler]:
    from handlers.place_invite import invite_start_handler

    text_handler = MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    photo_handler = MessageHandler(filters.PHOTO, handle_photo)
    states: dict[object, list[Any]] = {
        AW_PLACE: [
            CallbackQueryHandler(handle_place_state_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_TEMPLATE: [
            CallbackQueryHandler(handle_template_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_QUESTIONS: [
            CallbackQueryHandler(handle_question_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_EXTRAS: [
            CallbackQueryHandler(handle_extras_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
        AW_PREVIEW: [
            CallbackQueryHandler(handle_preview_callback, pattern=r"^aw_"),
            *_common_state_handlers(),
            text_handler,
            photo_handler,
        ],
    }
    conversation = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(open_new_wizard, pattern=r"^aw_start$"),
            CommandHandler("new_wizard", open_new_wizard),
            # Poster 2.0 S2: тап по площадке/личному шаблону сразу запускает
            # конструктор — entry-точки, поэтому состояние диалога ведётся.
            CallbackQueryHandler(handle_pick_callback, pattern=r"^(pl|tp)_pick:\d+$"),
            CallbackQueryHandler(open_my_announcements, pattern=r"^my_announcements$"),
            # Poster 2.2 U4b: ссылка `/start <token>` открывает конструктор
            # сразу — только как entry-point диалога он продолжит принимать
            # ответы на вопросы.
            invite_start_handler(),
        ],
        states=states,
        fallbacks=[
            CommandHandler("start", _command_start),
            CommandHandler("cancel", _command_cancel),
            CommandHandler("new_wizard", _command_new_wizard),
            CommandHandler("drafts", _command_drafts),
            CommandHandler("places", _command_places),
            CommandHandler("bricks", _command_bricks),
            CommandHandler("templates", _command_templates),
            CommandHandler("admin", _command_admin),
            CommandHandler("help", _command_help),
            MessageHandler(filters.COMMAND, handle_unknown_command),
            CallbackQueryHandler(handle_unexpected_callback, pattern=r".*"),
        ],
        name=CONVERSATION_NAME,
    )
    return [
        conversation,
        CallbackQueryHandler(outside_wizard_callback, pattern=r"^aw_"),
    ]


async def outside_wizard_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _answer(update, EXPIRED_TEXT, alert=True)
