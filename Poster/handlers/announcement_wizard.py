# handlers/announcement_wizard.py
"""
Telegram-интерфейс адаптивного мастера анонсов Stage 27B.

Мастер использует только сервисы Stage 27A и компилятор Stage 24:

- место и вариант названия выбираются в существующем справочнике;
- шаблон фиксируется независимым снимком, повторно снимок не создаётся;
- вопросы строит `announcement_store.build_announcement_questions`, а не
  отдельный алгоритм Telegram-слоя;
- значения и дополнительные кирпичики сохраняются по `brick_id`;
- предпросмотр собирается `build_announcement_preview` из снимка.

В `user_data` хранится только навигационное состояние интерфейса. Место,
шаблон, снимок, ответы, extras, этап и revision живут в SQLite. Поэтому
рестарт процесса не теряет работу: после входа в «📝 Новый мастер» черновик
открывается из базы. Кнопки содержат draft_id и revision; устаревший экран
не перезаписывает данные.

Публикация, фотографии, каналы и изменение старого мастера сюда не входят.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from announcement_store import (
    FIELD_STATUS_KNOWN,
    FIELD_STATUS_MISSING,
    FIELD_STATUS_SKIPPED,
    FIELD_STATUS_VALID,
    AnnouncementActorError,
    AnnouncementError,
    AnnouncementFieldState,
    AnnouncementNotFoundError,
    AnnouncementPreview,
    add_announcement_extra,
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_render_payload,
    choose_announcement_template,
    confirm_announcement_place,
    get_announcement_draft,
    list_announcement_drafts,
    remove_announcement_extra,
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
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_TITLES,
    BRICK_TYPE_URL,
    BrickValueResult,
    BrickValueStatus,
)
from database import SessionLocal
from place_store import (
    get_place_details,
    list_places,
    search_places,
)
from template_compiler import TemplateCompileError
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
from utils import calendar_view, screen
from utils import tg_context as ctx
from utils.parsers import app_today
from utils.tg_html import escape_html

logger = logging.getLogger(__name__)

CONVERSATION_NAME = "announcement_wizard"

AW_PLACE, AW_TEMPLATE, AW_QUESTIONS, AW_EXTRAS, AW_PREVIEW = range(5)

PLACE_PAGE_SIZE = 8
TEMPLATE_PAGE_SIZE = 6
EXTRA_PAGE_SIZE = 8
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
    WizardStage.COMPLETED.value: "сохранён",
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

STALE_TEXT = "Экран устарел: данные уже изменились. Показано актуальное состояние."
EXPIRED_TEXT = (
    "Кнопка устарела после перезапуска. Откройте «📝 Новый мастер» и "
    "продолжите сохранённый черновик."
)


def _user_id(update: Update) -> int:
    return ctx.user(update).id


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


def _short(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _h(value: object) -> str:
    return escape_html(str(value if value is not None else ""))


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


def _home_keyboard() -> InlineKeyboardMarkup:
    from handlers.main_menu import main_menu_keyboard

    return main_menu_keyboard()


def _main_menu_keyboard() -> InlineKeyboardMarkup:
    from handlers.main_menu import main_menu_keyboard

    return main_menu_keyboard()


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
        "📝 Новый мастер",
        "",
        "Адаптивный мастер сам определит недостающие поля по выбранному "
        "шаблону. Публикация на этом этапе не подключается: результат "
        "сохраняется как черновик.",
    ]
    if shown:
        lines.extend(["", "Незавершённые анонсы:"])
        for number, draft in enumerate(shown, start=1):
            stage = STAGE_LABELS.get(str(getattr(draft, "stage", "")), "черновик")
            lines.append(f"{number}. {_short(draft.place_display_name, 70)} — {stage}")
    elif drafts:
        lines.extend(["", "На этой странице черновиков нет."])
    else:
        lines.extend(["", "Сохранённых черновиков нового мастера пока нет."])

    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Новый анонс", callback_data="aw_new")]
    ]
    for index, draft in enumerate(shown):
        stage = STAGE_LABELS.get(str(getattr(draft, "stage", "")), "черновик")
        label = f"▶️ {index + 1}. {_short(draft.place_display_name, 38)}"
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
            [InlineKeyboardButton("🔎 Найти место", callback_data="aw_place_search")],
            [InlineKeyboardButton("📚 Все места", callback_data="aw_place_all:0")],
            [InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")],
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
    rows = [
        [
            InlineKeyboardButton(
                _short(f"📍 {name}", 60),
                callback_data=f"aw_place_open:{place_id}",
            )
        ]
        for place_id, name in places
    ]
    nav: list[InlineKeyboardButton] = []
    size = PLACE_PAGE_SIZE
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{prefix}:{page - 1}"))
    if (page + 1) * size < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{prefix}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")])
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
        "📚 Все места",
        "",
        (
            f"Всего: {total}. Страница {page + 1}."
            if total
            else "В справочнике пока нет мест."
        ),
    ]
    rows = _place_buttons(
        [(item.place_id, item.canonical_name) for item in places],
        prefix="aw_place_all",
        page=page,
        total=total,
    )
    if not places:
        rows.insert(
            0,
            [InlineKeyboardButton("🔎 Найти место", callback_data="aw_place_search")],
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
        if result.matched_name != result.canonical_name:
            lines.append(
                f"• {_h(result.canonical_name)} — совпадение: "
                f"{_h(result.matched_name)}"
            )
        else:
            lines.append(f"• {_h(result.canonical_name)}")
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
                _short(f"📍 {result.canonical_name}", 60),
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
    rows.append([InlineKeyboardButton("📚 Все места", callback_data="aw_place_all:0")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")])
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
            [[InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")]]
        ),
    )
    return AW_PLACE


def _place_card_text(details, selected_name_id: int | None) -> str:
    lines = [
        "📍 Проверьте место",
        "",
        f"Название: {_h(details.canonical_name)}",
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
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")])
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
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="aw_place_back")])
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
    ctx.data(context)["announcement_ui"] = ui
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
        return await _render_place_card(update, context)
    if stage == WizardStage.TEMPLATE:
        return await _render_template_screen(update, context, details)
    if stage == WizardStage.QUESTIONS:
        return await _enter_questions(update, context, details)
    if stage == WizardStage.EXTRAS:
        return await _render_extras(update, context, details)
    return await _render_preview(update, context, details)


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


def _stage_label(details) -> str:
    return STAGE_LABELS.get(str(details.stage), "черновик")


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
        f"Место: {_h(details.place_display_name)}",
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


def _field_status_text(field: AnnouncementFieldState) -> str:
    if field.status == FIELD_STATUS_VALID:
        return "заполнено"
    if field.status == FIELD_STATUS_SKIPPED:
        return "пропущено"
    if field.status == FIELD_STATUS_MISSING:
        return "не заполнено"
    return "из справочника"


def _format_value(value: object | None, *, limit: int = 800) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _field_value_text(field: AnnouncementFieldState) -> str:
    raw = _format_value(field.raw_value)
    if (
        field.value_type == BRICK_TYPE_NUMBER
        and raw
        and isinstance(field.rules.get("suffix"), str)
        and field.rules["suffix"]
    ):
        return f"{raw} {field.rules['suffix']}"
    return raw


def _number_rules_hint(rules: dict[str, Any]) -> str:
    parts = []
    if rules.get("integer"):
        parts.append("целое число")
    if rules.get("min") is not None and rules.get("max") is not None:
        parts.append(f"от {rules['min']} до {rules['max']}")
    elif rules.get("min") is not None:
        parts.append(f"не меньше {rules['min']}")
    elif rules.get("max") is not None:
        parts.append(f"не больше {rules['max']}")
    if isinstance(rules.get("suffix"), str) and rules["suffix"]:
        parts.append(f"суффикс «{rules['suffix']}»")
    return "; ".join(parts)


def _date_rules_hint(rules: dict[str, Any]) -> str:
    parts = []
    if rules.get("min_date"):
        parts.append(f"не раньше {rules['min_date']}")
    if rules.get("max_date"):
        parts.append(f"не позже {rules['max_date']}")
    if rules.get("horizon_months") is not None:
        parts.append(f"горизонт {rules['horizon_months']} мес.")
    return "; ".join(parts)


def _type_hint(field: AnnouncementFieldState) -> str:
    value_type = field.value_type
    rules = dict(field.rules or {})
    if value_type == BRICK_TYPE_SHORT_TEXT:
        return "Ожидается короткий текст одним сообщением."
    if value_type == BRICK_TYPE_LONG_TEXT:
        return "Можно отправить несколько строк одним сообщением."
    if value_type == BRICK_TYPE_DATE:
        extra = _date_rules_hint(rules)
        base = "Выберите дату календарём или отправьте её текстом, например 31.12.2026."
        return f"{base} Ограничения: {extra}" if extra else base
    if value_type == BRICK_TYPE_TIME:
        return "Отправьте время, например 18:30 или 9:00."
    if value_type == BRICK_TYPE_URL:
        return (
            "Отправьте ссылку: https://…, t.me/… или домен. Опасные схемы "
            "не принимаются."
        )
    if value_type == BRICK_TYPE_NUMBER:
        extra = _number_rules_hint(rules)
        base = "Отправьте число цифрами, например 12."
        return f"{base} Ограничения: {extra}." if extra else base
    if value_type == BRICK_TYPE_CHOICE:
        if rules.get("multiple"):
            return "Отметьте один или несколько вариантов кнопками, затем «Готово»."
        return "Выберите один вариант кнопкой."
    title = BRICK_TYPE_TITLES.get(value_type, value_type)
    return f"Введите значение типа «{title}»."


def _choice_options(field: AnnouncementFieldState) -> list[str]:
    raw = field.rules.get("choices")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def _selected_choices(field: AnnouncementFieldState) -> list[str]:
    raw = field.raw_value
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _question_text(
    field: AnnouncementFieldState,
    index: int,
    total: int,
    *,
    editing: bool = False,
    error: str | None = None,
    notice: str | None = None,
) -> str:
    blocks = [
        f"🧱 Вопрос {index + 1} из {total}: {_h(field.title)}",
        _type_hint(field),
        "Поле необязательное: его можно пропустить.",
    ]
    if field.status == FIELD_STATUS_VALID:
        value = _field_value_text(field)
        blocks.append(f"Текущее значение: {_h(value)}")
    elif field.status == FIELD_STATUS_SKIPPED:
        blocks.append("Сейчас поле пропущено. Можно заполнить его или перейти дальше.")
    elif field.status == FIELD_STATUS_MISSING:
        blocks.append("Значение пока не задано.")
    if error:
        blocks.append(f"⚠️ {_h(error)}")
    if notice:
        blocks.append(_h(notice))
    return "\n\n".join(blocks)


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
    text = _question_text(
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
        rows.append(
            [
                InlineKeyboardButton(
                    "📅 Календарь",
                    callback_data=(
                        f"aw_calroot:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
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
    rows.append(
        [
            InlineKeyboardButton(
                "⏭ Пропустить",
                callback_data=(
                    f"aw_qskip:{details.draft_id}:"
                    f"{details.revision}:{field.brick_id}"
                ),
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ Назад",
                callback_data=(
                    f"aw_qback:{details.draft_id}:"
                    f"{details.revision}:{field.brick_id}"
                ),
            )
        ]
    )
    await _render(update, context, text, InlineKeyboardMarkup(rows))
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
        return await _enter_extras(update, context, details, notice=notice)
    if next_index < 0:
        next_index = 0
    return await _render_question(
        update, context, details, fields, next_index, notice=notice
    )


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
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="aw_manage_back")])
    text = "\n".join(lines)
    body = f"{notice}\n\n{text}" if notice else text
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


def _extra_field_state(details, extra, definition) -> AnnouncementFieldState:
    value = details.value_for(extra.brick_id)
    status = FIELD_STATUS_MISSING
    raw: object | None = None
    if value is not None and value.status is BrickValueStatus.VALID:
        status = FIELD_STATUS_VALID
        raw = answer_payload_to_raw(
            BrickValueResult(
                status=value.status,
                payload=dict(value.payload) if value.payload is not None else None,
            )
        )
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


def _parse_rule_date(value: object) -> date | None:
    try:
        from datetime import datetime

        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def _calendar_bounds(rules: dict[str, Any]) -> tuple[date, date]:
    today = app_today()
    months = rules.get("horizon_months")
    try:
        horizon = calendar_view.add_months(
            today, int(months) if months is not None else 12
        )
    except (TypeError, ValueError):
        horizon = calendar_view.add_months(today, 12)
    start = today
    end = horizon
    minimum = _parse_rule_date(rules.get("min_date"))
    maximum = _parse_rule_date(rules.get("max_date"))
    if minimum is not None:
        start = max(start, minimum)
    if maximum is not None:
        end = min(end, maximum)
    return start, end


def _clip_range(
    value: calendar_view.DateRange, allowed: calendar_view.DateRange
) -> calendar_view.DateRange | None:
    start = max(value[0], allowed[0])
    end = min(value[1], allowed[1])
    if start > end:
        return None
    return start, end


def _calendar_context(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    draft_id: int,
    revision: int,
    brick_id: int,
):
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
    if field.value_type != BRICK_TYPE_DATE:
        return None
    return details, field, index, source, _calendar_bounds(dict(field.rules))


def _calendar_title(field: AnnouncementFieldState) -> str:
    value = _field_value_text(field)
    lines = [
        f"📅 {field.title}",
        "Выберите дату кнопками. Дату также можно отправить текстом.",
    ]
    if value:
        lines.append(f"Текущее значение: {value}")
    hint = _date_rules_hint(dict(field.rules))
    if hint:
        lines.append(f"Ограничения: {hint}.")
    return "\n\n".join(lines)


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


async def _render_calendar_root(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    if start > end:
        return await _render_calendar_value(
            update,
            context,
            details,
            field,
            index,
            source,
            error=("Календарь недоступен для этих правил. Введите дату текстом."),
        )
    today = app_today()
    allowed = (start, end)
    rows: list[list[InlineKeyboardButton]] = []
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"

    current = _clip_range(calendar_view.current_week(today), allowed)
    if current is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Текущая неделя: {calendar_view.format_range(*current)}",
                    callback_data=f"aw_calweek:{prefix}:cur",
                )
            ]
        )
    following = _clip_range(calendar_view.next_week(today), allowed)
    if following is not None:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Следующая неделя: {calendar_view.format_range(*following)}",
                    callback_data=f"aw_calweek:{prefix}:next",
                )
            ]
        )
    other = [
        clipped
        for clipped in (
            _clip_range(item, allowed) for item in calendar_view.other_ranges(today)
        )
        if clipped is not None
    ]
    if other:
        labels = " / ".join(calendar_view.format_range(*item) for item in other)
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"Другая дата: {labels}", 60),
                    callback_data=f"aw_calother:{prefix}",
                )
            ]
        )
    pairs = calendar_view.month_pairs(today, end)
    available = [
        pair
        for pair in pairs
        if any(calendar_view.month_weeks(key, allowed) for key in pair)
    ]
    if available:
        rows.append(
            [
                InlineKeyboardButton(
                    "📆 Выбрать месяц", callback_data=f"aw_calmonths:{prefix}"
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data=f"aw_calback:{prefix}")]
    )
    await _render(update, context, _calendar_title(field), InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


def _day_rows(
    days: list[date],
    prefix: str,
    *,
    per_row: int = 4,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for start in range(0, len(days), per_row):
        rows.append(
            [
                InlineKeyboardButton(
                    calendar_view.format_day(day),
                    callback_data=(f"aw_calday:{prefix}:{day.strftime('%Y-%m-%d')}"),
                )
                for day in days[start : start + per_row]
            ]
        )
    return rows


async def _render_calendar_days(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    *,
    title: str,
    value_range: calendar_view.DateRange,
    back_callback: str,
) -> int:
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    days = calendar_view.days_between(*value_range)
    rows = _day_rows(days, prefix)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=back_callback)])
    await _render(update, context, title, InlineKeyboardMarkup(rows))
    return AW_QUESTIONS


async def _render_calendar_other(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    rows: list[list[InlineKeyboardButton]] = []
    for item_index, item in enumerate(calendar_view.other_ranges(app_today())):
        clipped = _clip_range(item, (start, end))
        if clipped is None:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    calendar_view.format_range(*clipped),
                    callback_data=f"aw_calrange:{prefix}:{item_index}",
                )
            ]
        )
    if not rows:
        return await _render_calendar_root(
            update, context, details, field, index, source
        )
    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data=f"aw_calroot:{prefix}")]
    )
    await _render(
        update,
        context,
        "📆 Другая дата\n\nВыберите двухнедельный диапазон:",
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _render_calendar_months(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    rows: list[list[InlineKeyboardButton]] = []
    for pair in calendar_view.month_pairs(app_today(), end):
        row = []
        for key in pair:
            if not calendar_view.month_weeks(key, allowed):
                continue
            row.append(
                InlineKeyboardButton(
                    f"{calendar_view.MONTH_NAMES[key[1]]} {key[0]}",
                    callback_data=f"aw_calmonth:{prefix}:{key[0]}:{key[1]}",
                )
            )
        if row:
            rows.append(row)
    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data=f"aw_calroot:{prefix}")]
    )
    await _render(
        update,
        context,
        "📆 Выберите месяц:",
        InlineKeyboardMarkup(rows),
    )
    return AW_QUESTIONS


async def _render_calendar_month_weeks(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    field: AnnouncementFieldState,
    index: int,
    source: str,
    key: calendar_view.MonthKey,
) -> int:
    start, end = _calendar_bounds(dict(field.rules))
    allowed = (start, end)
    prefix = f"{details.draft_id}:{details.revision}:{field.brick_id}"
    weeks = calendar_view.month_weeks(key, allowed)
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                calendar_view.format_range(*week),
                callback_data=(
                    f"aw_calweekdays:{prefix}:{key[0]}:{key[1]}:{week_index}"
                ),
            )
        ]
        for week_index, week in enumerate(weeks)
    ]
    rows.append(
        [InlineKeyboardButton("◀️ Назад", callback_data=f"aw_calmonths:{prefix}")]
    )
    await _render(
        update,
        context,
        f"📆 {calendar_view.MONTH_NAMES[key[1]]} {key[0]}\n\n" "Выберите неделю:",
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
    items = _extras_with_definitions(details, _user_id(update))
    lines = [
        "➕ Дополнительные кирпичики",
        "",
        "Дополнительные поля влияют только на этот анонс. Исходный шаблон "
        "не меняется.",
    ]
    if items:
        for number, (extra, definition) in enumerate(items, start=1):
            field = _extra_field_state(details, extra, definition)
            value = _field_value_text(field)
            if field.status == FIELD_STATUS_MISSING:
                value_text = "значение не задано"
            elif field.status == FIELD_STATUS_SKIPPED:
                value_text = "пропущено"
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
    rows.append(
        [
            InlineKeyboardButton(
                "👁 Обзор анонса",
                callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
            )
        ]
    )
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
                "◀️ Назад",
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
    text = f"➕ {brick.title}\n\n" "Выберите секцию анонса: шапка, тело или подвал."
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
                "◀️ Назад",
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
                "◀️ Назад",
                callback_data=f"aw_exback:{details.draft_id}:{details.revision}",
            )
        ],
    ]
    await _render(update, context, body, InlineKeyboardMarkup(rows))
    return AW_EXTRAS


def _extra_value_text(extra, field: AnnouncementFieldState) -> str:
    lines = [
        f"➕ {field.title}",
        f"Секция: {SECTION_LABELS.get(extra.section, extra.section)}; "
        f"положение: {POSITION_LABELS.get(extra.position, extra.position)}.",
        _type_hint(field),
    ]
    if field.status == FIELD_STATUS_VALID:
        lines.append(f"Текущее значение: {_field_value_text(field)}")
    elif field.status == FIELD_STATUS_SKIPPED:
        lines.append("Сейчас значение пропущено.")
    else:
        lines.append("Значение пока не задано.")
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
        rows.append(
            [
                InlineKeyboardButton(
                    "📅 Календарь",
                    callback_data=(
                        f"aw_calroot:{details.draft_id}:"
                        f"{details.revision}:{field.brick_id}"
                    ),
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "⏭ Пропустить",
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
        f"Место: {_h(details.place_display_name)}",
        f"Адрес: {_h(details.place_address) if details.place_address else 'не указан'}",
        f"Ссылка: {_h(details.place_url) if details.place_url else 'не указана'}",
        f"Шаблон: {_h(_preview_template_name(details))}",
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
        lines = ["", "✅ Анонс можно передать на следующий этап."]
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

    lines = ["", "⚠️ Анонс пока не готов к следующему этапу."]
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
        lines.extend(["", "Черновой фрагмент (не готов к публикации):"])
        fragment = preview.visible_text[:PREVIEW_VISIBLE_LIMIT].rstrip()
        if len(preview.visible_text) > PREVIEW_VISIBLE_LIMIT:
            fragment += "…"
        lines.append(_h(fragment))
    return lines


def _preview_keyboard(details, *, saved: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Черновик сохранён" if saved else "✅ Сохранить черновик",
                    callback_data=(f"aw_save:{details.draft_id}:{details.revision}"),
                )
            ],
            [
                InlineKeyboardButton(
                    "✏️ Изменить поля",
                    callback_data=(f"aw_qmanage:{details.draft_id}:{details.revision}"),
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ Дополнительные кирпичики",
                    callback_data=(f"aw_extras:{details.draft_id}:{details.revision}"),
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
                )
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )


async def _render_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    details,
    *,
    notice: str | None = None,
    saved: bool = False,
) -> int:
    ui = _ui(context)
    _clear_active(ui)
    updated = details
    try:
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
    _set_details(ui, preview.draft)
    lines = _preview_summary_lines(preview, _user_id(update))
    lines.extend(_preview_block(preview))
    if notice:
        lines.insert(1, notice)
    await _render(
        update,
        context,
        "\n".join(lines),
        _preview_keyboard(preview.draft, saved=saved),
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
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=(f"aw_preview:{details.draft_id}:{details.revision}"),
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
    return await _render_preview(update, context, details, notice=notice)


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
        raw = update.effective_message.text if update.effective_message else ""
        return await _save_question_value(
            update,
            context,
            details,
            field,
            raw or "",
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
    raw = update.effective_message.text if update.effective_message else ""
    return await _save_extra_value(
        update,
        context,
        details,
        extra,
        raw or "",
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


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _reanchor(update, context)
    return await _resume_current(
        update,
        context,
        notice=(
            "Фотографии в этом этапе не подключаются. Анонс и его значения "
            "сохранены."
        ),
    )


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
        "📝 Новый мастер закрыт. Сохранённый черновик не удалён — "
        "продолжить можно через «📝 Новый мастер».",
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
        await _answer(update)
        _clear_ui(context)
        from handlers.drafts import view_drafts

        await view_drafts(update, context)
        return ConversationHandler.END
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
        await _answer(
            update,
            "Новый мастер закрыт. Старый мастер откроется кнопкой "
            "«✏️ Создать пост» в главном меню.",
            alert=True,
        )
        return await _exit_to_main_menu(update, context)
    if data == "restore_wip":
        await _answer(
            update,
            "Новый мастер закрыт. Продолжение старого поста доступно в "
            "главном меню.",
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
        from handlers.drafts import view_drafts

        await view_drafts(update, context)
        return ConversationHandler.END
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
    prefix = f"{draft_id}:{revision}:{brick_id}"

    if action == "aw_calroot":
        await _answer(update)
        return await _render_calendar_root(
            update, context, details, field, index, source
        )
    if action == "aw_calweek" and len(parts) == 5:
        kind = parts[4]
        if kind == "cur":
            value_range = _clip_range(calendar_view.current_week(today), allowed)
            title = "📅 Текущая неделя"
        elif kind == "next":
            value_range = _clip_range(calendar_view.next_week(today), allowed)
            title = "📅 Следующая неделя"
        else:
            return await _stale(update, context)
        if value_range is None:
            return await _render_calendar_root(
                update, context, details, field, index, source
            )
        await _answer(update)
        return await _render_calendar_days(
            update,
            context,
            details,
            field,
            index,
            source,
            title=(
                f"{title}: {calendar_view.format_range(*value_range)}. "
                "Выберите дату:"
            ),
            value_range=value_range,
            back_callback=f"aw_calroot:{prefix}",
        )
    if action == "aw_calother":
        await _answer(update)
        return await _render_calendar_other(
            update, context, details, field, index, source
        )
    if action == "aw_calrange" and len(parts) == 5:
        try:
            range_index = int(parts[4])
        except ValueError:
            return await _stale(update, context)
        ranges = calendar_view.other_ranges(today)
        if range_index < 0 or range_index >= len(ranges):
            return await _stale(update, context)
        value_range = _clip_range(ranges[range_index], allowed)
        if value_range is None:
            return await _render_calendar_other(
                update, context, details, field, index, source
            )
        await _answer(update)
        return await _render_calendar_days(
            update,
            context,
            details,
            field,
            index,
            source,
            title=(f"📅 {calendar_view.format_range(*value_range)}. " "Выберите дату:"),
            value_range=value_range,
            back_callback=f"aw_calother:{prefix}",
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
        if not calendar_view.month_weeks(key, allowed):
            return await _stale(update, context)
        await _answer(update)
        return await _render_calendar_month_weeks(
            update, context, details, field, index, source, key
        )
    if action == "aw_calweekdays" and len(parts) == 7:
        try:
            key = (int(parts[4]), int(parts[5]))
            week_index = int(parts[6])
        except ValueError:
            return await _stale(update, context)
        weeks = calendar_view.month_weeks(key, allowed)
        if week_index < 0 or week_index >= len(weeks):
            return await _stale(update, context)
        value_range = weeks[week_index]
        await _answer(update)
        return await _render_calendar_days(
            update,
            context,
            details,
            field,
            index,
            source,
            title=(f"📅 {calendar_view.format_range(*value_range)}. " "Выберите дату:"),
            value_range=value_range,
            back_callback=f"aw_calmonth:{prefix}:{key[0]}:{key[1]}",
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
    if action == "aw_save" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
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
            notice="✅ Черновик сохранён. Публикация не выполнялась.",
            saved=True,
        )
    if action == "aw_preview" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _render_preview(update, context, details)
    if action == "aw_qmanage" and len(parts) == 3:
        return await handle_question_callback(update, context)
    if action == "aw_extras" and len(parts) == 3:
        if not all(item.isdigit() for item in parts[1:]):
            return await _stale(update, context)
        guarded = _guard_details(update, context, int(parts[1]), int(parts[2]))
        if guarded is None:
            return await _stale(update, context)
        _ui_data, details = guarded
        await _answer(update)
        return await _enter_extras(update, context, details)
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


async def _command_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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
        "Новый мастер активен. Используйте кнопки экрана или /cancel, "
        "чтобы закрыть мастер без удаления черновика.",
        _main_menu_keyboard(),
    )


def announcement_wizard_handlers() -> list[BaseHandler]:
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
