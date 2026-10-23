# handlers/places.py
"""
Раздел «Места» (Stage 19B): поиск, карточка, создание и альтернативные имена.

Сценарий самостоятельный и НЕ меняет девятишаговый мастер поста: выбранное
место хранится только в ограниченном состоянии `place_sessions`. Никакого
скрытого создания места при поиске нет; создание и добавление названия —
только после явных действий пользователя.

Один экран (`utils.screen`) перерисовывается на месте. Callback-и проверяют
актуальную фазу и существование места: устаревшая/подменённая кнопка не
меняет данные. Текстовый ввод принимается только в явных фазах ожидания и
регистрируется ПОСЛЕ ConversationHandler мастера, поэтому не перехватывает
названия, даты и другие поля активного визарда.
"""

from __future__ import annotations

import logging
from html import escape as html_escape

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from admin_state import clear_admin_session
from database import SessionLocal
from models import PlaceName, User
from permissions import can_manage_place
from place_state import load_place_session, save_place_session
from place_store import (
    PlaceDetails,
    PlaceNameError,
    SearchResult,
    add_alternative_name,
    clean_display_name,
    create_place,
    find_name_conflicts,
    get_place_details,
    get_place_name,
    list_places,
    normalize_place_name,
    search_places,
    validate_place_name,
)
from user_store import get_or_create_user
from utils import screen
from utils import tg_context as ctx

logger = logging.getLogger(__name__)

PLACES_PAGE_SIZE = 8
SEARCH_PAGE_SIZE = 8
NAMES_PAGE_SIZE = 6
CARD_ALIASES_LIMIT = 5
BUTTON_TEXT_LIMIT = 60
STALE_TEXT = "Кнопка устарела: откройте раздел «Места» заново."

TEXT_INPUT_PHASES = {"search_input", "create_input", "alias_input"}

PHASES = {
    "hub",
    "search_input",
    "search_results",
    "all_places",
    "create_input",
    "create_check",
    "existing_choice",
    "confirm",
    "names",
    "alias_input",
    "alias_warning",
    "admin_request",
    "confirmed",
}


def _h(value: object) -> str:
    """HTML-экранирование; None в карточку не попадает (поля скрываются)."""
    return html_escape(str(value))


def _button_name(prefix: str, name: str) -> str:
    """
    Подпись inline-кнопки ограничена платформой (64 символа).

    Полное название всегда есть в тексте экрана; сокращается только подпись
    кнопки, поэтому данные не теряются.
    """
    available = max(BUTTON_TEXT_LIMIT - len(prefix), 1)
    if len(name) <= available:
        return prefix + name
    return prefix + name[: max(available - 1, 1)] + "…"


def _short(value: object, limit: int = 100) -> str:
    """Короткая подпись для пояснения; полное название остаётся в карточке."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _safe_link(url: str) -> str:
    """Ссылка — только http(s); иначе показываем как обычный текст."""
    if url.lower().startswith(("http://", "https://")):
        return f'<a href="{_h(url)}">{_h(url)}</a>'
    return _h(url)


def _admin_request_text(details: PlaceDetails) -> str:
    """Объяснение кнопки «Я админ места»; заявка не создаётся."""
    contact = config.SUPERADMIN_CONTACT_URL
    if contact:
        contact_line = (
            "Контакт суперадминистратора Poster: "
            f"{_safe_link(contact)}\n\n"
            "Напишите ему сами и укажите название места."
        )
    else:
        contact_line = (
            "Контакт администратора пока не указан. Попросите владельца "
            "Poster назначить вас администратором этого места."
        )
    return (
        "🛡 Я админ места\n\n"
        f"📍 {_h(details.canonical_name)}\n\n"
        "Назначить администратора места может только суперадминистратор "
        "Poster. Эта кнопка не отправляет заявку и не выдаёт полномочия.\n\n"
        f"{contact_line}"
    )


def _clamp_offset(offset: int, total: int, page_size: int) -> int:
    """Безопасная страница: не уходим в пустоту после удаления записей."""
    if total <= 0:
        return 0
    max_offset = ((total - 1) // page_size) * page_size
    return min(max(offset, 0), max_offset)


def _page_value(state: dict, key: str = "page") -> int:
    """Номер страницы из JSON: битое/отрицательное значение безопасно = 0."""
    try:
        value = int(state.get(key) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _page_note(
    offset: int, count: int, total: int, page_size: int = SEARCH_PAGE_SIZE
) -> str:
    if total <= 0:
        return ""
    return (
        f"Показано {offset + 1}–{offset + count} из {total}.\n"
        f"Страница {offset // page_size + 1}."
    )


def _ensure_user(session: Session, update: Update) -> User:
    """Внутренний users.id для авторства; username — лишь подсказка."""
    telegram_user = ctx.user(update)
    return get_or_create_user(
        session,
        telegram_user.id,
        username=getattr(telegram_user, "username", None),
        full_name=getattr(telegram_user, "full_name", None),
    )


def _new_hub() -> dict:
    return {"phase": "hub"}


def _failed_add(state: dict, update: Update) -> None:
    message = getattr(update, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    ids = state.setdefault("failed_inputs", [])
    if message_id not in ids:
        ids.append(int(message_id))


def _failed_take(state: dict) -> list[int]:
    raw = state.pop("failed_inputs", None) or []
    out: list[int] = []
    for value in raw:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _card_name(details: PlaceDetails, state: dict):
    target = state.get("display_name_id")
    if isinstance(target, int):
        for item in details.names:
            if item.id == target:
                return item
    return next(item for item in details.names if item.is_canonical)


def _card_text(
    details: PlaceDetails, state: dict, *, completion_note: str | None = None
) -> str:
    selected = _card_name(details, state)
    canonical = next(item for item in details.names if item.is_canonical)
    if selected.is_canonical:
        text = f"📍 {_h(selected.name)}"
    else:
        text = (
            f"📍 {_h(selected.name)}\n" f"Каноническое название: {_h(canonical.name)}"
        )

    alternatives = [
        item
        for item in details.names
        if not item.is_canonical and item.id != selected.id
    ]
    if alternatives:
        shown = alternatives[:CARD_ALIASES_LIMIT]
        text += "\n\nАльтернативные названия:\n" + "\n".join(
            f"— {_h(item.name)}" for item in shown
        )
        if len(alternatives) > len(shown):
            text += (
                f"\nЕщё {len(alternatives) - len(shown)} — откройте "
                "«Выбрать альтернативное название»."
            )
    if details.address:
        text += f"\n\nАдрес: {_h(details.address)}"
    if details.url:
        text += f"\nСсылка: {_safe_link(details.url)}"
    text += (
        "\n\nНажмите «✅ Подтвердить», чтобы выбрать это место, или "
        "«🔤 Выбрать альтернативное название» для другого написания."
    )
    return text


def _confirmation_text(details: PlaceDetails, state: dict) -> str:
    selected = _card_name(details, state)
    canonical = next(item for item in details.names if item.is_canonical)
    text = f"✅ Место выбрано\n\n📍 {_h(selected.name)}"
    if not selected.is_canonical:
        text += f"\nКаноническое название: {_h(canonical.name)}"
    return (
        text + "\n\nСценарий выбора места завершён. Выбранное место сохранено "
        "только в состоянии этого сценария — в черновик оно не записывается."
    )


def _result_label(result: SearchResult, *, check: bool = False) -> str:
    if result.category == "canonical_exact":
        return "точное совпадение" if check else "точное название"
    if result.category == "alias_exact":
        return (
            f"точное совпадение (альтернативное название «{_h(_short(result.matched_name))}»)"
            if check
            else f"альтернативное название «{_h(_short(result.matched_name))}»"
        )
    if result.category == "canonical_partial":
        return f"частичное совпадение: «{_h(_short(result.matched_name))}»"
    if result.category == "alias_partial":
        return (
            "частичное совпадение (альтернативное название): "
            f"«{_h(_short(result.matched_name))}»"
        )
    return f"похожее название: «{_h(_short(result.matched_name))}»"


def _hub_content() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "📍 Места\n\n"
        "Найдите место по названию или добавьте новое. Поиск учитывает "
        "канонические и альтернативные названия."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔎 Найти место", callback_data="pl_search")],
            [InlineKeyboardButton("📚 Все места", callback_data="pl_all")],
            [InlineKeyboardButton("➕ Добавить место", callback_data="pl_add")],
            [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
        ]
    )
    return text, keyboard


def _all_places_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    offset = _page_value(state)
    places, total = list_places(session, offset=max(offset, 0), limit=PLACES_PAGE_SIZE)
    if not places and total > 0 and offset > 0:
        offset = 0
        places, total = list_places(session, offset=0, limit=PLACES_PAGE_SIZE)
    state["page"] = offset

    if total == 0:
        text = "📚 Все места\n\nВ справочнике пока нет мест."
        empty_keyboard = [
            [InlineKeyboardButton("➕ Добавить место", callback_data="pl_add")],
            [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
        ]
        return text, InlineKeyboardMarkup(empty_keyboard)

    text = f"📚 Все места — {total}\n\n"
    for index, item in enumerate(places, start=offset + 1):
        text += f"{index}. {_h(item.canonical_name)}\n"
    text += "\n" + _page_note(offset, len(places), total, PLACES_PAGE_SIZE)

    keyboard: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(places, start=offset + 1):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", item.canonical_name),
                    callback_data=f"pl_open:{item.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"pl_page:{max(offset - PLACES_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(places) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"pl_page:{offset + len(places)}",
            )
        )
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("➕ Добавить место", callback_data="pl_add")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="pl_back")])
    return text, InlineKeyboardMarkup(keyboard)


def _search_results_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    query = str(state.get("query") or "")
    results = search_places(session, query)
    total = len(results)
    offset = _clamp_offset(_page_value(state), total, SEARCH_PAGE_SIZE)
    state["page"] = offset
    page_results = results[offset : offset + SEARCH_PAGE_SIZE]

    if not results:
        text = (
            f"🔎 Поиск: «{_h(query)}»\n\n"
            "Ничего не найдено. Место не создаётся автоматически — его можно "
            "добавить отдельно, после проверки дублей."
        )
        empty_keyboard = [
            [InlineKeyboardButton("➕ Добавить место", callback_data="pl_add")],
            [InlineKeyboardButton("🔁 Искать снова", callback_data="pl_search")],
            [InlineKeyboardButton("◀️ Назад", callback_data="pl_back")],
        ]
        return text, InlineKeyboardMarkup(empty_keyboard)

    text = f"🔎 Результаты поиска: «{_h(query)}»\n\n"
    for index, result in enumerate(page_results, start=offset + 1):
        text += f"{index}. {_h(result.canonical_name)} — " f"{_result_label(result)}\n"
    text += "\n" + _page_note(offset, len(page_results), total)

    keyboard: list[list[InlineKeyboardButton]] = []
    for index, result in enumerate(page_results, start=offset + 1):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", result.canonical_name),
                    callback_data=f"pl_open:{result.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"pl_spage:{max(offset - SEARCH_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(page_results) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"pl_spage:{offset + len(page_results)}",
            )
        )
    if nav:
        keyboard.append(nav)
    keyboard.append(
        [InlineKeyboardButton("🔁 Искать снова", callback_data="pl_search")]
    )
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="pl_back")])
    return text, InlineKeyboardMarkup(keyboard)


def _create_check_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    candidate = str(state.get("candidate") or "")
    results = search_places(session, candidate)[:PLACES_PAGE_SIZE]
    text = (
        "🔍 Проверьте, нет ли уже этого места в базе\n\n"
        f"Вы ввели: «{_h(candidate)}»\n\n"
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    if results:
        text += "Возможные совпадения:\n"
        for index, result in enumerate(results, start=1):
            text += (
                f"{index}. {_h(result.canonical_name)} — "
                f"{_result_label(result, check=True)}\n"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        _button_name(f"{index}. ", result.canonical_name),
                        callback_data=f"pl_open:{result.place_id}",
                    )
                ]
            )
        text += (
            "\nЕсли это другое заведение с похожим названием, создание нового "
            "места допустимо — но только по явной кнопке ниже."
        )
    else:
        text += "Похожих мест не найдено."
    keyboard.append(
        [InlineKeyboardButton("✅ Это новое место", callback_data="pl_create_confirm")]
    )
    keyboard.append(
        [InlineKeyboardButton("✏️ Исправить название", callback_data="pl_create_edit")]
    )
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="pl_back")])
    return text, InlineKeyboardMarkup(keyboard)


def _existing_choice_content(
    details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    candidate = str(state.get("candidate") or "")
    try:
        candidate_normalized = normalize_place_name(candidate)
    except PlaceNameError:
        candidate_normalized = ""
    already = any(
        normalize_place_name(item.name) == candidate_normalized
        for item in details.names
    )
    text = (
        "👣 Вы выбрали существующее место\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Вы вводили: «{_h(candidate)}».\n\n"
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    if already:
        text += (
            "Введённое название уже совпадает с названием этого места — "
            "добавлять его как альтернативное не нужно."
        )
    else:
        text += (
            "Можно сохранить введённое название как альтернативное название "
            "этого места. Оно сразу станет доступно всем пользователям. Без "
            "вашего подтверждения оно не добавляется."
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "💾 Сохранить как альтернативное",
                    callback_data="pl_save_candidate",
                )
            ]
        )
    keyboard.append(
        [
            InlineKeyboardButton(
                "Открыть карточку без сохранения",
                callback_data="pl_skip_candidate",
            )
        ]
    )
    keyboard.append(
        [InlineKeyboardButton("◀️ Назад к проверке", callback_data="pl_back")]
    )
    return text, InlineKeyboardMarkup(keyboard)


def _names_content(
    details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(details.names)
    offset = _clamp_offset(_page_value(state, "names_page"), total, NAMES_PAGE_SIZE)
    state["names_page"] = offset
    page = details.names[offset : offset + NAMES_PAGE_SIZE]
    selected = _card_name(details, state)

    text = f"🔤 Названия места\n\n📍 {_h(details.canonical_name)}\n\n"
    for index, item in enumerate(page, start=offset + 1):
        marks: list[str] = []
        if item.is_canonical:
            marks.append("каноническое")
        if item.id == selected.id:
            marks.append("выбрано")
        suffix = f" — {', '.join(marks)}" if marks else ""
        text += f"{index}. {_h(item.name)}{suffix}\n"
    if total > len(page):
        text += f"\nПоказаны {offset + 1}–{offset + len(page)} из {total}."

    keyboard: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(page, start=offset + 1):
        mark = "✓ " if item.id == selected.id else ""
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{mark}{index}. ", item.name),
                    callback_data=f"pl_name:{item.id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"pl_names:{max(offset - NAMES_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(page) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"pl_names:{offset + len(page)}",
            )
        )
    if nav:
        keyboard.append(nav)
    keyboard.append(
        [InlineKeyboardButton("➕ Добавить название", callback_data="pl_addname")]
    )
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="pl_back")])
    return text, InlineKeyboardMarkup(keyboard)


def _alias_warning_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    pending = str(state.get("pending_alias") or "")
    conflicts = find_name_conflicts(session, pending, exclude_place_id=details.place_id)
    other = conflicts[0] if conflicts else None
    if other is None:
        text = (
            f"⚠️ Название «{_h(pending)}» больше не конфликтует с другими "
            "местами. Можно добавить его явным подтверждением."
        )
    else:
        text = (
            "⚠️ Похожее название у другого места\n\n"
            f"Название «{_h(pending)}» очень похоже на «{_h(other.matched_name)}» "
            f"в другом месте «{_h(other.canonical_name)}».\n\n"
            "Возможно, речь об одном и том же месте. Разные места не "
            "объединяются автоматически. Добавить название как альтернативное "
            "именно выбранному месту?"
        )
    if len(conflicts) > 1:
        text += f"\n\nПохожих мест: {len(conflicts)}."
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Да, добавить", callback_data="pl_alias_force")],
            [InlineKeyboardButton("❌ Нет, отмена", callback_data="pl_alias_cancel")],
        ]
    )
    return text, keyboard


async def _show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    text: str,
    keyboard: InlineKeyboardMarkup,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
) -> bool:
    """
    Сохраняет состояние, рисует экран и только потом чистит ввод.

    Порядок важен: неудачный ввод остаётся в чате до подтверждённого
    сохранения нового состояния; принятый — удаляется best-effort.
    """
    user_id = ctx.user(update).id
    body = f"{notice}\n\n{text}" if notice else text
    saved = save_place_session(user_id, state)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(context, user_data, chat_id, body, keyboard)
    # Устраняем окно Stage 17 для этого сценария: ref сохраняется сразу после
    # отправки/правки, не откладываясь до следующего действия.
    screen.save_screen_ref(user_id, user_data, chat_id)
    if saved:
        if cleanup_ids:
            await screen.cleanup_messages(
                context.bot, chat_id, sorted(set(cleanup_ids))
            )
        if drop_input:
            await screen.drop_user_input(update, context)
    return saved


async def _render_phase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
) -> bool:
    """Единая точка отрисовки текущей фазы (в т.ч. после рестарта)."""
    phase = str(state.get("phase") or "")
    if phase not in PHASES:
        state.clear()
        state.update(_new_hub())
        phase = "hub"
    telegram_user_id = ctx.user(update).id

    session = SessionLocal()
    try:
        if phase == "hub":
            text, keyboard = _hub_content()
        elif phase == "search_input":
            text = (
                "🔎 Поиск места\n\n"
                "Введите часть названия места. Регистр, «е/ё», пробелы и "
                "пунктуация не важны."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="pl_back")]]
            )
        elif phase == "search_results":
            if not state.get("query"):
                state.clear()
                state.update(_new_hub())
                text, keyboard = _hub_content()
            else:
                text, keyboard = _search_results_content(session, state)
        elif phase == "all_places":
            text, keyboard = _all_places_content(session, state)
        elif phase == "create_input":
            text = (
                "➕ Новое место\n\n"
                "Введите название нового места. Сначала проверим, нет ли уже "
                "похожего места в справочнике."
            )
            if state.get("candidate"):
                text += f"\n\nТекущее название: «{_h(state['candidate'])}»"
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="pl_back")]]
            )
        elif phase == "create_check":
            if not state.get("candidate"):
                state.clear()
                state.update(_new_hub())
                text, keyboard = _hub_content()
            else:
                text, keyboard = _create_check_content(session, state)
        elif phase == "admin_request":
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_hub())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _hub_content()
            else:
                text = _admin_request_text(details)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "◀️ Назад к карточке", callback_data="pl_back"
                            )
                        ]
                    ]
                )
        elif phase in {"existing_choice", "confirm", "names", "alias_input"}:
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_hub())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _hub_content()
            elif phase == "existing_choice":
                text, keyboard = _existing_choice_content(details, state)
            elif phase == "confirm":
                text = _card_text(details, state)
                confirm_rows = [
                    [
                        InlineKeyboardButton(
                            "✅ Подтвердить",
                            callback_data=f"pl_confirm:{details.place_id}",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔤 Выбрать альтернативное название",
                            callback_data="pl_names:0",
                        )
                    ],
                ]
                if not can_manage_place(session, telegram_user_id, details.place_id):
                    confirm_rows.append(
                        [
                            InlineKeyboardButton(
                                "🛡 Я админ места",
                                callback_data=(f"pl_admin_request:{details.place_id}"),
                            )
                        ]
                    )
                confirm_rows.append(
                    [
                        InlineKeyboardButton(
                            "◀️ Вернуться к списку", callback_data="pl_back"
                        )
                    ]
                )
                keyboard = InlineKeyboardMarkup(confirm_rows)
            elif phase == "names":
                text, keyboard = _names_content(details, state)
            else:  # alias_input
                text = (
                    "➕ Новое название\n\n"
                    f"Введите альтернативное название для "
                    f"«{_h(details.canonical_name)}».\n\n"
                    "Оно появится у всех пользователей после отправки."
                )
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Назад", callback_data="pl_back")]]
                )
        elif phase == "alias_warning":
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_hub())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _hub_content()
            else:
                text, keyboard = _alias_warning_content(session, details, state)
        elif phase == "confirmed":
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_hub())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _hub_content()
            else:
                text = _confirmation_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("📍 К местам", callback_data="pl_hub")],
                        [
                            InlineKeyboardButton(
                                "🏠 Главное меню", callback_data="main_menu"
                            )
                        ],
                    ]
                )
        else:  # pragma: no cover — PHASES уже проверен
            state.clear()
            state.update(_new_hub())
            text, keyboard = _hub_content()
    finally:
        session.close()

    return await _show(
        update,
        context,
        state,
        text,
        keyboard,
        notice=notice,
        cleanup_ids=cleanup_ids,
        drop_input=drop_input,
    )


async def _stale(update: Update, text: str = STALE_TEXT) -> None:
    await ctx.query(update).answer(text, show_alert=True)


async def open_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в раздел: кнопка меню, /places или восстановление после рестарта."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    # «Места» и кабинет — взаимоисключающие ветки: старый административный
    # ввод не должен перехватывать следующий текст.
    clear_admin_session(ctx.user(update).id)
    state = load_place_session(ctx.user(update).id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_hub()
    await _render_phase(update, context, state, drop_input=True)


async def handle_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"hub", "confirmed"}:
        await _stale(update)
        return
    await ctx.query(update).answer()
    await _render_phase(update, context, _new_hub())


async def handle_search_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "hub",
        "search_input",
        "search_results",
        "all_places",
    }:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "search_input"
    state["page"] = 0
    await _render_phase(update, context, state)


async def handle_all_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "hub",
        "all_places",
        "search_results",
    }:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "all_places"
    state["page"] = 0
    await _render_phase(update, context, state)


async def handle_add_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "hub",
        "all_places",
        "search_results",
    }:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state.clear()
    state.update({"phase": "create_input"})
    await _render_phase(update, context, state)


async def handle_places_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "all_places":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("pl_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_search_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "search_results":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("pl_spage:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["page"] = max(offset, 0)
    await _render_phase(update, context, state)


def _visible_place_ids(session: Session, state: dict) -> set[int] | None:
    """
    Места, кнопки которых показаны в текущей фазе поиска/проверки.

    `None` — фаза не привязана к подмножеству (например, «Все места»):
    там достаточно существования места. Для поиска и проверки дублей
    список пересчитывается, поэтому кнопка от предыдущего запроса не
    откроет место, которого в актуальной выдаче уже нет.
    """
    phase = state.get("phase")
    if phase == "search_results":
        query = state.get("query")
        if not isinstance(query, str) or not query:
            return set()
        return {item.place_id for item in search_places(session, query)}
    if phase == "create_check":
        candidate = state.get("candidate")
        if not isinstance(candidate, str) or not candidate:
            return set()
        return {
            item.place_id
            for item in search_places(session, candidate)[:PLACES_PAGE_SIZE]
        }
    return None


async def handle_open_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "all_places",
        "search_results",
        "create_check",
    }:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("pl_open:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        visible = _visible_place_ids(session, state)
        details = get_place_details(session, place_id)
    finally:
        session.close()
    if visible is not None and place_id not in visible:
        await _stale(update)
        return
    await query.answer()
    if details is None:
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    state["place_id"] = place_id
    state["display_name_id"] = details.canonical_name_id
    state["page"] = _page_value(state)
    if state.get("phase") == "create_check":
        state["phase"] = "existing_choice"
    else:
        state["return_phase"] = state.get("phase")
        state["return_page"] = state.get("page")
        state["phase"] = "confirm"
    await _render_phase(update, context, state)


async def handle_place_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None:
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("pl_confirm:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    phase = state.get("phase")
    if phase == "confirmed" and state.get("place_id") == place_id:
        await query.answer("Уже подтверждено.")
        await _render_phase(update, context, state)
        return
    if phase != "confirm" or state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
    finally:
        session.close()
    await query.answer()
    if details is None:
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    display_name_id = details.canonical_name_id
    target = state.get("display_name_id")
    if isinstance(target, int) and any(item.id == target for item in details.names):
        display_name_id = target
    state.clear()
    state.update(
        {
            "phase": "confirmed",
            "place_id": place_id,
            "display_name_id": display_name_id,
        }
    )
    await _render_phase(update, context, state)


async def handle_names(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"confirm", "names"}:
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("pl_names:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["phase"] = "names"
    state["names_page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_name_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "names":
        await _stale(update)
        return
    data = query.data or ""
    try:
        name_id = int(data[len("pl_name:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )
        item = (
            get_place_name(session, int(place_id), name_id)
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if details is None or item is None:
        await _stale(update)
        return
    await query.answer()
    state["display_name_id"] = name_id
    state["phase"] = "confirm"
    await _render_phase(update, context, state)


async def handle_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"confirm", "names"}:
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["alias_return_phase"] = state.get("phase")
    state["phase"] = "alias_input"
    await _render_phase(update, context, state)


async def handle_admin_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Кнопка «Я админ места»: только объяснение, без заявки и без выдачи прав.

    Callback может быть подделан, поэтому место и текущая фаза проверяются,
    а роль никогда не меняется этим обработчиком.
    """
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "confirm":
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("pl_admin_request:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return

    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        manages = can_manage_place(session, ctx.user(update).id, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer()
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    if manages:
        await query.answer("У вас уже есть права на управление этим местом.")
        await _render_phase(update, context, state)
        return
    await query.answer()
    state["phase"] = "admin_request"
    await _render_phase(update, context, state)


async def handle_create_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "create_check":
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "create_input"
    await _render_phase(update, context, state)


async def handle_create_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "create_check":
        await _stale(update)
        return
    candidate = state.get("candidate")
    if not isinstance(candidate, str) or not candidate:
        await _stale(update)
        return
    await query.answer()

    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        place = create_place(session, candidate, created_by_user_id=user.id)
        place_id = int(place.id)
        canonical = session.scalar(
            select(PlaceName).where(
                PlaceName.place_id == place_id,
                PlaceName.is_canonical == 1,
            )
        )
        if canonical is None:  # pragma: no cover — контракт create_place
            raise RuntimeError("У нового места нет канонического названия")
        canonical_id = int(canonical.id)
    except PlaceNameError as exc:
        await _render_phase(
            update, context, state, notice=f"Не удалось создать место: {exc}"
        )
        return
    except Exception as e:  # noqa: BLE001 — экран должен остаться рабочим
        logger.error("Не удалось создать место: %s", e)
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось создать место из-за ошибки базы данных. "
            "Попробуйте ещё раз.",
        )
        return
    finally:
        session.close()

    state.clear()
    state.update(
        {
            "phase": "confirm",
            "place_id": place_id,
            "display_name_id": canonical_id,
            "return_phase": "all_places",
            "return_page": 0,
        }
    )
    await _render_phase(
        update,
        context,
        state,
        notice="✅ Место добавлено. Оно сразу доступно всем пользователям.",
    )


async def handle_save_candidate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "existing_choice":
        await _stale(update)
        return
    place_id = state.get("place_id")
    candidate = state.get("candidate")
    if not isinstance(place_id, int) or not isinstance(candidate, str) or not candidate:
        await _stale(update)
        return
    await query.answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        outcome = add_alternative_name(
            session,
            place_id,
            candidate,
            created_by_user_id=user.id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось сохранить вариант названия: %s", e)
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось сохранить название из-за ошибки базы данных.",
        )
        return
    finally:
        session.close()

    if outcome.status == "place_missing":
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    if outcome.status == "conflict":
        state["phase"] = "alias_warning"
        state["pending_alias"] = outcome.display_name
        state["alias_origin"] = "candidate"
        await _render_phase(update, context, state)
        return
    state["display_name_id"] = outcome.name_id
    state["phase"] = "confirm"
    state.setdefault("return_phase", "all_places")
    state.setdefault("return_page", 0)
    state.pop("candidate", None)
    state.pop("alias_origin", None)
    state.pop("pending_alias", None)
    notice = (
        "Введённое название сохранено как альтернативное."
        if outcome.status == "created"
        else "Такое название уже есть у этого места."
    )
    await _render_phase(update, context, state, notice=notice)


async def handle_skip_candidate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "existing_choice":
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["display_name_id"] = None
    state["phase"] = "confirm"
    state.setdefault("return_phase", "all_places")
    state.setdefault("return_page", 0)
    state.pop("candidate", None)
    await _render_phase(update, context, state)


async def handle_alias_force(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "alias_warning":
        await _stale(update)
        return
    place_id = state.get("place_id")
    pending = state.get("pending_alias")
    if not isinstance(place_id, int) or not isinstance(pending, str) or not pending:
        await _stale(update)
        return
    await query.answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        outcome = add_alternative_name(
            session,
            place_id,
            pending,
            created_by_user_id=user.id,
            force=True,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось добавить название после предупреждения: %s", e)
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось добавить название из-за ошибки базы данных.",
        )
        return
    finally:
        session.close()

    if outcome.status == "place_missing":
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    state["display_name_id"] = outcome.name_id
    state["phase"] = "confirm"
    for key in ("pending_alias", "alias_origin", "candidate"):
        state.pop(key, None)
    notice = (
        "Альтернативное название добавлено и выбрано."
        if outcome.status == "created"
        else "Такое название уже есть у этого места."
    )
    await _render_phase(update, context, state, notice=notice)


async def handle_alias_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "alias_warning":
        await _stale(update)
        return
    await ctx.query(update).answer()
    origin = state.get("alias_origin")
    state.pop("pending_alias", None)
    state.pop("alias_origin", None)
    state["phase"] = "existing_choice" if origin == "candidate" else "names"
    await _render_phase(update, context, state)


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_place_session(ctx.user(update).id)
    if state is None:
        await _stale(update)
        return
    await ctx.query(update).answer()
    phase = state.get("phase")
    if phase == "search_results":
        state["phase"] = "search_input"
    elif phase in {"search_input", "all_places", "create_input", "create_check"}:
        state.clear()
        state.update(_new_hub())
    elif phase == "existing_choice":
        state["phase"] = "create_check"
    elif phase == "confirm":
        return_phase = state.get("return_phase")
        if return_phase == "search_results":
            state["phase"] = "search_results"
            state["page"] = _page_value(state, "return_page")
        elif return_phase == "all_places":
            state["phase"] = "all_places"
            state["page"] = _page_value(state, "return_page")
        else:
            state.clear()
            state.update(_new_hub())
    elif phase == "names":
        state["phase"] = "confirm"
    elif phase == "admin_request":
        state["phase"] = "confirm"
    elif phase == "alias_input":
        state["phase"] = (
            "names" if state.get("alias_return_phase") == "names" else "confirm"
        )
    elif phase == "alias_warning":
        origin = state.get("alias_origin")
        state.pop("pending_alias", None)
        state.pop("alias_origin", None)
        state["phase"] = "existing_choice" if origin == "candidate" else "names"
    elif phase == "confirmed":
        state.clear()
        state.update(_new_hub())
    else:
        state.clear()
        state.update(_new_hub())
    await _render_phase(update, context, state)


class PlaceTextHandler(MessageHandler):
    """
    Текстовый обработчик, который существует для PTB только при активной
    фазе ввода «Мест».

    Обычный MessageHandler(TEXT) в одной группе с главным меню перехватывал
    бы ЛЮБОЙ текст вне диалога поста (PTB останавливается на первом
    подходящем обработчике группы), и подсказка «Воспользуйтесь кнопками
    меню» перестала бы появляться. Поэтому check_update дополнительно
    проверяет сохранённую фазу сценария.
    """

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_place_session(user.id)
        if not isinstance(state, dict):
            return None
        if state.get("phase") not in TEXT_INPUT_PHASES:
            return None
        return matched


async def handle_place_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Текст, принятый ТОЛЬКО в явной фазе ввода раздела «Места».

    Регистрация после ConversationHandler мастера гарантирует: активный шаг
    поста получает свои сообщения раньше и этот обработчик не вызывается.
    """
    state = load_place_session(ctx.user(update).id)
    if state is None:
        return
    phase = state.get("phase")
    if phase not in TEXT_INPUT_PHASES:
        return

    # Экран находится выше нового сообщения — переносим его вниз, чтобы
    # результат/ошибка были видны рядом с вводом (Stage 16).
    await screen.reanchor_screen(update, context)
    raw = ctx.message(update).text or ""

    if phase == "search_input":
        try:
            display_query = clean_display_name(raw)
            session = SessionLocal()
            try:
                search_places(session, display_query)
            finally:
                session.close()
        except PlaceNameError as exc:
            _failed_add(state, update)
            await _render_phase(update, context, state, notice=str(exc))
            return
        state["query"] = display_query
        state["phase"] = "search_results"
        state["page"] = 0
        cleanup = _failed_take(state)
        await _render_phase(
            update,
            context,
            state,
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return

    if phase == "create_input":
        try:
            candidate = validate_place_name(raw)
        except PlaceNameError as exc:
            _failed_add(state, update)
            await _render_phase(update, context, state, notice=str(exc))
            return
        cleanup = _failed_take(state)
        state["candidate"] = candidate
        state["phase"] = "create_check"
        state["page"] = 0
        await _render_phase(
            update,
            context,
            state,
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return

    # alias_input
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    try:
        display = validate_place_name(raw, what="Альтернативное название")
    except PlaceNameError as exc:
        _failed_add(state, update)
        await _render_phase(update, context, state, notice=str(exc))
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        outcome = add_alternative_name(
            session,
            place_id,
            display,
            created_by_user_id=user.id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось добавить альтернативное название: %s", e)
        _failed_add(state, update)
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось добавить название из-за ошибки базы данных.",
        )
        return
    finally:
        session.close()

    if outcome.status == "place_missing":
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice="Место не существует или было удалено.",
        )
        return
    if outcome.status == "duplicate":
        _failed_add(state, update)
        await _render_phase(
            update,
            context,
            state,
            notice="Такое название уже есть у этого места. Введите другое.",
        )
        return
    if outcome.status == "conflict":
        cleanup = _failed_take(state)
        state["phase"] = "alias_warning"
        state["pending_alias"] = outcome.display_name
        state["alias_origin"] = "names"
        await _render_phase(
            update,
            context,
            state,
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return

    cleanup = _failed_take(state)
    state["display_name_id"] = outcome.name_id
    state["phase"] = "confirm"
    state.pop("alias_return_phase", None)
    await _render_phase(
        update,
        context,
        state,
        notice="Альтернативное название добавлено и выбрано.",
        cleanup_ids=cleanup,
        drop_input=True,
    )


async def stale_place_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Неизвестная/подменённая кнопка: спиннер не висит, данные не меняются."""
    await _stale(update)


def places_handlers() -> list[BaseHandler]:
    """
    Обработчики раздела «Места».

    В bot.py регистрируются ПОСЛЕ ConversationHandler поста и ДО глобальных
    обработчиков главного меню: активный мастер получает свои тексты первым,
    а навигация вне мастера попадает сюда. Callback-префикс `pl_` уникален и
    не пересекается с кнопками визарда/черновиков.
    """
    return [
        CommandHandler("places", open_places),
        CallbackQueryHandler(open_places, pattern=r"^places$"),
        CallbackQueryHandler(handle_hub, pattern=r"^pl_hub$"),
        CallbackQueryHandler(handle_search_prompt, pattern=r"^pl_search$"),
        CallbackQueryHandler(handle_all_places, pattern=r"^pl_all$"),
        CallbackQueryHandler(handle_add_place, pattern=r"^pl_add$"),
        CallbackQueryHandler(handle_back, pattern=r"^pl_back$"),
        CallbackQueryHandler(handle_places_page, pattern=r"^pl_page:\d+$"),
        CallbackQueryHandler(handle_search_page, pattern=r"^pl_spage:\d+$"),
        CallbackQueryHandler(handle_open_place, pattern=r"^pl_open:\d+$"),
        CallbackQueryHandler(handle_place_confirm, pattern=r"^pl_confirm:\d+$"),
        CallbackQueryHandler(handle_admin_request, pattern=r"^pl_admin_request:\d+$"),
        CallbackQueryHandler(handle_names, pattern=r"^pl_names:\d+$"),
        CallbackQueryHandler(handle_name_choice, pattern=r"^pl_name:\d+$"),
        CallbackQueryHandler(handle_add_name, pattern=r"^pl_addname$"),
        CallbackQueryHandler(handle_create_edit, pattern=r"^pl_create_edit$"),
        CallbackQueryHandler(handle_create_confirm, pattern=r"^pl_create_confirm$"),
        CallbackQueryHandler(handle_save_candidate, pattern=r"^pl_save_candidate$"),
        CallbackQueryHandler(handle_skip_candidate, pattern=r"^pl_skip_candidate$"),
        CallbackQueryHandler(handle_alias_force, pattern=r"^pl_alias_force$"),
        CallbackQueryHandler(handle_alias_cancel, pattern=r"^pl_alias_cancel$"),
        PlaceTextHandler(filters.TEXT & ~filters.COMMAND, handle_place_text),
        CallbackQueryHandler(stale_place_callback, pattern=r"^pl_"),
    ]
