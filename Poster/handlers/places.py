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
from brick_state import clear_brick_session
from database import SessionLocal
from models import PlaceName, User
from permissions import can_manage_place, is_superadmin
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
    validate_place_city,
    validate_place_name,
)
from template_state import clear_template_session
from user_store import get_or_create_user
from utils import screen
from utils import tg_context as ctx
from utils.place_labels import place_title

logger = logging.getLogger(__name__)

PLACES_PAGE_SIZE = 8
SEARCH_PAGE_SIZE = 8
NAMES_PAGE_SIZE = 6
CARD_ALIASES_LIMIT = 5
BUTTON_TEXT_LIMIT = 60
STALE_TEXT = "Кнопка устарела: откройте раздел «Места» заново."

TEXT_INPUT_PHASES = {
    "search_input",
    "create_input",
    "create_city_input",
    "alias_input",
    "pick_search_input",
    "manage_search_input",
}

PHASES = {
    "hub",
    "search_input",
    "search_results",
    "all_places",
    "create_input",
    "create_check",
    # Poster 2.0 U1a: последовательный вопрос о городе при регистрации.
    "create_city_input",
    "existing_choice",
    "confirm",
    "names",
    "alias_input",
    "alias_warning",
    "admin_request",
    "confirmed",
    # Poster 2.0 S2: выбор площадки для немедленного старта анонса.
    "pick",
    "pick_search_input",
    "pick_search_results",
    # Poster 2.1 R2: справочник «Управление → Площадки» (без анонса).
    "manage_list",
    "manage_search_input",
    "manage_search_results",
    "manage_card",
    "manage_admin_request",
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


def _place_title(name: str, city: str | None) -> str:
    """
    «Название · Город» для списков (Poster 2.0 U1a).

    Общее правило живёт в `utils.place_labels`: им же пользуется визард
    анонса, поэтому списки не расходятся в формате строки.
    """
    return place_title(name, city)


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
    # Poster 2.0 U1a: город есть у новых площадок; у старых записей его нет,
    # и пустое значение показывается явным «—», а не пропадает из карточки.
    text += f"\nГород: {_h(details.city) if details.city else '—'}"
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
    if result.category == "city":
        city = result.city or ""
        return f"совпадение по городу: «{_h(_short(city))}»"
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
        text += f"{index}. {_h(_place_title(item.canonical_name, item.city))}\n"
    text += "\n" + _page_note(offset, len(places), total, PLACES_PAGE_SIZE)

    keyboard: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(places, start=offset + 1):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name(
                        f"{index}. ",
                        _place_title(item.canonical_name, item.city),
                    ),
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
        text += (
            f"{index}. {_h(_place_title(result.canonical_name, result.city))} — "
            f"{_result_label(result)}\n"
        )
    text += "\n" + _page_note(offset, len(page_results), total)

    keyboard: list[list[InlineKeyboardButton]] = []
    for index, result in enumerate(page_results, start=offset + 1):
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name(
                        f"{index}. ",
                        _place_title(result.canonical_name, result.city),
                    ),
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
                f"{index}. {_h(_place_title(result.canonical_name, result.city))} — "
                f"{_result_label(result, check=True)}\n"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        _button_name(
                            f"{index}. ",
                            _place_title(result.canonical_name, result.city),
                        ),
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


def _pending_alias_text(state: dict) -> str | None:
    """
    Название из предупреждения о конфликте — только если оно валидно.

    Фаза `alias_warning` восстанавливается из БД после рестарта, а ключ
    `pending_alias` мог не сохраниться (старый снимок WIP, частичная запись).
    `find_name_conflicts` на пустой строке бросает `PlaceNameError`, поэтому
    проверка стоит до него: `None` — названия нет, предупреждать не о чем.
    """
    raw = state.get("pending_alias")
    if not isinstance(raw, str):
        return None
    try:
        display = clean_display_name(raw)
        normalize_place_name(display)
    except PlaceNameError:
        return None
    return display


def _drop_alias_warning_keys(state: dict) -> None:
    """Сбрасывает ключи предупреждения о конфликте названия."""
    for key in ("pending_alias", "alias_origin"):
        state.pop(key, None)


def _alias_warning_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    pending = _pending_alias_text(state) or ""
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
        elif phase == "pick":
            text, keyboard = _pick_content(session, state)
        elif phase == "pick_search_input":
            text = (
                "🔎 Поиск площадки\n\n"
                "Введите часть названия. Регистр, «е/ё», пробелы и пунктуация "
                "не важны."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ К списку", callback_data="pl_pick_back")]]
            )
        elif phase == "pick_search_results":
            if not state.get("query"):
                state.clear()
                state.update(_new_pick())
                text, keyboard = _pick_content(session, state)
            else:
                text, keyboard = _pick_search_content(session, state)
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
        elif phase == "manage_list":
            text, keyboard = _manage_list_content(session, state)
        elif phase == "manage_search_input":
            text = (
                "🔎 Поиск места\n\n"
                "Введите часть названия. Регистр, «е/ё», пробелы и пунктуация "
                "не важны."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ К списку", callback_data="plm_back")]]
            )
        elif phase == "manage_search_results":
            if not state.get("query"):
                state["phase"] = "manage_list"
                text, keyboard = _manage_list_content(session, state)
            else:
                text, keyboard = _manage_search_content(session, state)
        elif phase == "manage_card":
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_manage())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _manage_list_content(session, state)
            else:
                text, keyboard = _manage_card_content(
                    session,
                    details,
                    manages=can_manage_place(
                        session, telegram_user_id, details.place_id
                    ),
                    superadmin=is_superadmin(telegram_user_id),
                )
        elif phase == "manage_admin_request":
            place_id = state.get("place_id")
            details = (
                get_place_details(session, int(place_id))
                if isinstance(place_id, int)
                else None
            )
            if details is None:
                state.clear()
                state.update(_new_manage())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _manage_list_content(session, state)
            else:
                text = _admin_request_text(details)
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ К карточке", callback_data="plm_back")]]
                )
        elif phase == "create_input":
            text = (
                "➕ Новое место\n\n"
                "Введите название нового места. Сначала проверим, нет ли уже "
                "похожего места в справочнике."
            )
            if state.get("candidate"):
                text += f"\n\nТекущее название: «{_h(state['candidate'])}»"
            if state.get("manage_mode"):
                text += (
                    "\n\nУ нового места сразу появится стандартный шаблон "
                    "анонса. Анонс не запускается."
                )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "◀️ Назад",
                            callback_data=(
                                "plm_back" if state.get("manage_mode") else "pl_back"
                            ),
                        )
                    ]
                ]
            )
        elif phase == "create_check":
            if not state.get("candidate"):
                state.clear()
                state.update(_new_hub())
                text, keyboard = _hub_content()
            else:
                text, keyboard = _create_check_content(session, state)
        elif phase == "create_city_input":
            text = (
                "🏙 В каком городе площадка?\n\n"
                f"Название: «{_h(str(state.get('candidate') or ''))}»\n\n"
                "Введите город обычным текстом (например, Москва или "
                "Санкт-Петербург). Город виден в списке площадок и помогает "
                "найти место поиском."
            )
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "◀️ Назад",
                            callback_data=(
                                "plm_back" if state.get("manage_mode") else "pl_back"
                            ),
                        )
                    ]
                ]
            )
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
            pending_alias = _pending_alias_text(state)
            if details is None:
                state.clear()
                state.update(_new_hub())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _hub_content()
            elif pending_alias is None:
                # U3b: после рестарта/старого снимка WIP название могло не
                # сохраниться. Предупреждать не о чем — возвращаемся на
                # ближайший живой родительский экран вместо исключения.
                _drop_alias_warning_keys(state)
                state["phase"] = "confirm"
                state.setdefault("return_phase", "all_places")
                state.setdefault("return_page", 0)
                notice = notice or (
                    "Название для добавления не сохранилось. Выберите его "
                    "заново в карточке места."
                )
                text = _card_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🔤 Выбрать альтернативное название",
                                callback_data="pl_names:0",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "◀️ Вернуться к списку", callback_data="pl_back"
                            )
                        ],
                    ]
                )
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


# --- Poster 2.0 S2: выбор площадки для немедленного старта анонса -------------

PICK_PAGE_SIZE = 8
PICK_STALE_TEXT = "Кнопка устарела: откройте «Создать для площадки» заново."


def _pick_content(session: Session, state: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Список зарегистрированных площадок; тап по названию сразу начинает анонс."""
    offset = _page_value(state, "pick_page")
    places, total = list_places(session, offset=offset, limit=PICK_PAGE_SIZE)
    if not places and total > 0 and offset > 0:
        offset = 0
        places, total = list_places(session, offset=0, limit=PICK_PAGE_SIZE)
    state["pick_page"] = offset

    if total == 0:
        text = (
            "📍 Создать для площадки\n\n"
            "В справочнике пока нет площадок. Добавьте первую — анонс "
            "начнётся сразу по её стандартному шаблону."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Новая площадка", callback_data="pl_pick_add"
                    )
                ],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")],
            ]
        )
        return text, keyboard

    text = (
        "📍 Создать для площадки\n\n"
        "Выберите площадку — анонс начнётся сразу по её стандартному "
        "шаблону, без лишних подтверждений.\n\n"
    )
    for number, item in enumerate(places, start=offset + 1):
        text += f"{number}. {_h(_place_title(item.canonical_name, item.city))}\n"
    text += "\n" + _page_note(offset, len(places), total, PICK_PAGE_SIZE)

    rows: list[list[InlineKeyboardButton]] = []
    for item in places:
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name("🚀 ", _place_title(item.canonical_name, item.city)),
                    callback_data=f"pl_pick:{item.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"pl_pickp:{max(offset - PICK_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(places) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"pl_pickp:{offset + len(places)}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("🔎 Найти площадку", callback_data="pl_pick_search")]
    )
    rows.append(
        [InlineKeyboardButton("➕ Новая площадка", callback_data="pl_pick_add")]
    )
    rows.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _pick_search_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Результаты поиска площадки в режиме pick."""
    query = str(state.get("query") or "")
    results = search_places(session, query)
    total = len(results)
    offset = _clamp_offset(_page_value(state, "page"), total, SEARCH_PAGE_SIZE)
    state["page"] = offset
    page_results = results[offset : offset + SEARCH_PAGE_SIZE]
    if not results:
        text = (
            f"🔎 Поиск площадки: «{_h(query)}»\n\n"
            "Ничего не найдено. Площадка не создаётся автоматически — её можно "
            "добавить отдельно."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Новая площадка", callback_data="pl_pick_add"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔁 Искать снова", callback_data="pl_pick_search"
                    )
                ],
                [InlineKeyboardButton("◀️ К списку", callback_data="pl_pick_back")],
            ]
        )
        return text, keyboard

    text = f"🔎 Площадки по запросу: «{_h(query)}»\n\n"
    for number, result in enumerate(page_results, start=offset + 1):
        text += (
            f"{number}. {_h(_place_title(result.canonical_name, result.city))} — "
            f"{_result_label(result)}\n"
        )
    text += "\n" + _page_note(offset, len(page_results), total)

    rows: list[list[InlineKeyboardButton]] = []
    for result in page_results:
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(
                        "🚀 ", _place_title(result.canonical_name, result.city)
                    ),
                    callback_data=f"pl_pick:{result.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"pl_picksp:{max(offset - SEARCH_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(page_results) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"pl_picksp:{offset + len(page_results)}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton("🔁 Искать снова", callback_data="pl_pick_search")]
    )
    rows.append([InlineKeyboardButton("◀️ К списку", callback_data="pl_pick_back")])
    return text, InlineKeyboardMarkup(rows)


def _new_pick() -> dict:
    return {"phase": "pick", "pick_page": 0}


# --- Poster 2.1 R2: справочник «Управление → Площадки» ----------------------

MANAGE_STALE_TEXT = "Кнопка устарела: откройте «Управление → Площадки» заново."

MANAGE_LIST_PHASES = frozenset(
    {"manage_list", "manage_search_input", "manage_search_results", "manage_card"}
)

# Фазы, которые существуют только в справочнике «Управление → Площадки».
# Нужны, чтобы обычный раздел «Места» не открывался по сохранённой сессии
# справочника и чтобы «назад» справочника не отвечал «Кнопка устарела».
MANAGE_ONLY_PHASES = MANAGE_LIST_PHASES | {"manage_admin_request"}


def _new_manage() -> dict:
    """Состояние справочника площадок (отдельно от выбора для анонса)."""
    return {"phase": "manage_list", "page": 0}


def _drop_manage_mode(state: dict) -> None:
    """
    Сбрасывает режим справочника при выходе в обычный сценарий «Места».

    Иначе `manage_mode` из «Управление → Площадки → Добавить место» протекал
    в общие экраны: «◀️ Назад» отправляла `plm_back` вместо `pl_back` и
    уводила в справочник.
    """
    state.pop("manage_mode", None)


def _manage_list_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Список площадок справочника: пагинация как в «Все места»."""
    offset = _page_value(state)
    places, total = list_places(session, offset=max(offset, 0), limit=PLACES_PAGE_SIZE)
    if not places and total > 0 and offset > 0:
        offset = 0
        places, total = list_places(session, offset=0, limit=PLACES_PAGE_SIZE)
    state["page"] = offset

    if total == 0:
        text = (
            "📍 Площадки\n\n"
            "В справочнике пока нет площадок. Добавьте первую — у неё сразу "
            "появится стандартный шаблон анонса."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Добавить место", callback_data="plm_add")],
                [InlineKeyboardButton("◀️ Назад", callback_data="manage")],
            ]
        )
        return text, keyboard

    text = f"📍 Площадки — {total}\n\n"
    for index, item in enumerate(places, start=offset + 1):
        text += f"{index}. {_h(_place_title(item.canonical_name, item.city))}\n"
    text += "\n" + _page_note(offset, len(places), total, PLACES_PAGE_SIZE)
    text += (
        "\nТап по площадке открывает карточку справочника: название, "
        "альтернативные названия, адрес и ссылка."
    )

    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(places, start=offset + 1):
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(
                        f"{index}. ",
                        _place_title(item.canonical_name, item.city),
                    ),
                    callback_data=f"plm_open:{item.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"plm_page:{max(offset - PLACES_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(places) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"plm_page:{offset + len(places)}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔎 Найти место", callback_data="plm_search")])
    rows.append([InlineKeyboardButton("➕ Добавить место", callback_data="plm_add")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="manage")])
    return text, InlineKeyboardMarkup(rows)


def _manage_search_content(
    session: Session, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Результаты поиска в справочнике: только открытие карточки."""
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
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Добавить место", callback_data="plm_add")],
                [InlineKeyboardButton("🔁 Искать снова", callback_data="plm_search")],
                [InlineKeyboardButton("◀️ К списку", callback_data="plm_back")],
            ]
        )
        return text, keyboard

    text = f"🔎 Результаты поиска: «{_h(query)}»\n\n"
    for index, result in enumerate(page_results, start=offset + 1):
        text += (
            f"{index}. {_h(_place_title(result.canonical_name, result.city))} — "
            f"{_result_label(result)}\n"
        )
    text += "\n" + _page_note(offset, len(page_results), total)

    rows: list[list[InlineKeyboardButton]] = []
    for index, result in enumerate(page_results, start=offset + 1):
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(
                        f"{index}. ",
                        _place_title(result.canonical_name, result.city),
                    ),
                    callback_data=f"plm_open:{result.place_id}",
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"plm_spage:{max(offset - SEARCH_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(page_results) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"plm_spage:{offset + len(page_results)}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔁 Искать снова", callback_data="plm_search")])
    rows.append([InlineKeyboardButton("◀️ К списку", callback_data="plm_back")])
    return text, InlineKeyboardMarkup(rows)


def _manage_card_content(
    session: Session, details: PlaceDetails, *, manages: bool, superadmin: bool
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Карточка места справочника: данные и действия по актуальным правам.

    `manages`/`superadmin` вычислены по БД в момент отрисовки: callback не
    передаёт роль, а видимость кнопки — только удобство.
    """
    alternatives = [item for item in details.names if not item.is_canonical]
    lines = [f"📍 {_h(details.canonical_name)}", ""]
    lines.append(f"Альтернативные названия: {len(alternatives)}")
    # Poster 2.0 U1a: город виден в карточке; у старых мест его нет.
    lines.append(f"Город: {_h(details.city) if details.city else '—'}")
    lines.append(f"Адрес: {_h(details.address) if details.address else 'не указан'}")
    lines.append(f"Ссылка: {_safe_link(details.url) if details.url else 'не указана'}")
    lines.append("")
    if manages:
        lines.append("Вы можете редактировать данные места и его стандартный шаблон.")
    else:
        lines.append(
            "Это просмотр справочника: редактирование доступно администратору "
            "места и суперадминистратору Poster."
        )

    rows: list[list[InlineKeyboardButton]] = []
    if manages:
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Редактировать",
                    callback_data=f"plm_edit:{details.place_id}",
                )
            ]
        )
    if superadmin:
        rows.append(
            [
                InlineKeyboardButton(
                    "👥 Администраторы",
                    callback_data=f"plm_admins:{details.place_id}",
                )
            ]
        )
    if not manages:
        rows.append(
            [
                InlineKeyboardButton(
                    "🛡 Я админ места",
                    callback_data=f"plm_admin_request:{details.place_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ К списку", callback_data="plm_back")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def open_place_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    «📍 Создать для площадки»: список площадок первым экраном.

    Поиск и добавление — отдельные кнопки, а не замена списка. Состояние
    раздела сбрасывается: это вход в создание анонса, а не справочник.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = _new_pick()
    save_place_session(user_id, state)
    await _render_phase(update, context, state, drop_input=True)


async def handle_pick_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пагинация списка площадок в режиме pick."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "pick":
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    offset = _parse_offset(query.data or "", "pl_pickp:")
    if offset is None:
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["pick_page"] = offset
    await _render_phase(update, context, state)


async def handle_pick_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Поиск площадки в режиме pick — отдельное действие."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "pick",
        "pick_search_input",
        "pick_search_results",
    }:
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "pick_search_input"
    state["page"] = 0
    await _render_phase(update, context, state)


async def handle_pick_search_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Пагинация результатов поиска площадок в режиме pick."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "pick_search_results":
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    offset = _parse_offset(query.data or "", "pl_picksp:")
    if offset is None:
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["page"] = offset
    await _render_phase(update, context, state)


async def handle_pick_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Возврат из поиска площадок к полному списку."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "pick_search_input",
        "pick_search_results",
    }:
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state.clear()
    state.update(_new_pick())
    await _render_phase(update, context, state)


def _parse_offset(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    try:
        return max(int(data[len(prefix) :]), 0)
    except (TypeError, ValueError):
        return None


async def handle_pick_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«➕ Новая площадка» в режиме pick: создание с автозапуском анонса."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "pick",
        "pick_search_input",
        "pick_search_results",
    }:
        await query.answer(PICK_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state.clear()
    state.update({"phase": "create_input", "pick_mode": True})
    await _render_phase(update, context, state, drop_input=True)


async def _auto_start_pick_announcement(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place_id: int
) -> None:
    """
    Автозапуск анонса после создания площадки (Poster 2.0 S2).

    Создание идёт в обработчике справочника, а конструктор — отдельный
    ConversationHandler, поэтому состояние диалога выставляется явно.
    """
    from handlers.announcement_wizard import (
        remember_conversation_state,
        start_place_announcement_pick,
    )

    state = await start_place_announcement_pick(update, context, place_id)
    remember_conversation_state(update, context, state)


# --- Poster 2.1 R2: справочник «Управление → Площадки» ----------------------


async def open_places_manage(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «Управление → Площадки»: справочник, а НЕ выбор места для анонса.

    Сохранённая фаза pick не восстанавливается: раздел всегда открывается
    списком, тап по площадке ведёт в карточку, а не в конструктор (дефект №26).
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = load_place_session(user_id)
    if not isinstance(state, dict) or state.get("phase") not in MANAGE_LIST_PHASES:
        state = _new_manage()
    await _render_phase(update, context, state, drop_input=True)


async def handle_manage_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Пагинация списка площадок справочника."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "manage_list":
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    offset = _parse_offset(query.data or "", "plm_page:")
    if offset is None:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["page"] = offset
    await _render_phase(update, context, state)


async def handle_manage_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Поиск в справочнике — отдельное действие, место не создаётся."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in MANAGE_LIST_PHASES:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "manage_search_input"
    state["page"] = 0
    state.pop("query", None)
    await _render_phase(update, context, state)


async def handle_manage_search_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Пагинация результатов поиска справочника."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "manage_search_results":
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    offset = _parse_offset(query.data or "", "plm_spage:")
    if offset is None:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["page"] = offset
    await _render_phase(update, context, state)


def _manage_visible_place_ids(session: Session, state: dict) -> set[int] | None:
    """
    Места, кнопки которых показаны в текущей фазе справочника.

    Для выдачи поиска список пересчитывается: устаревшая кнопка не откроет
    место, которого в актуальном результате уже нет. Для полного списка
    (`manage_list`) и карточки достаточно существования места.
    """
    if state.get("phase") != "manage_search_results":
        return None
    query = state.get("query")
    if not isinstance(query, str) or not query:
        return set()
    return {item.place_id for item in search_places(session, query)}


async def handle_manage_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «Назад» справочника: карточка/поиск → список, список → «Управление».

    Фазы карточки, заявки на админа места и создания из справочника входят в
    allow-list: у каждого экрана раздела должна быть рабочая навигация, а не
    ответ «Кнопка устарела» и тупик (дефекты №26).
    """
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    phase = state.get("phase") if state is not None else None
    allowed = MANAGE_ONLY_PHASES | {"create_input", "create_city_input"}
    if (
        state is None
        or phase not in allowed
        or (
            phase in {"create_input", "create_city_input"}
            and not state.get("manage_mode")
        )
    ):
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    if phase == "manage_list":
        # «Назад» из списка справочника — выход в «Управление»; open_manage
        # сам отвечает на callback, поэтому второй answer здесь не нужен.
        from handlers.main_menu import open_manage

        await open_manage(update, context)
        return
    await query.answer()
    if phase == "manage_admin_request":
        # «◀️ К карточке»: место то же, возвращаем карточку без сброса id.
        state["phase"] = "manage_card"
        await _render_phase(update, context, state)
        return
    state.clear()
    state.update(_new_manage())
    await _render_phase(update, context, state)


async def handle_manage_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Тап по площадке в справочнике открывает КАРТОЧКУ, не анонс."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "manage_list",
        "manage_search_results",
    }:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("plm_open:") :])
    except (TypeError, ValueError):
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    session = SessionLocal()
    try:
        visible = _manage_visible_place_ids(session, state)
        details = get_place_details(session, place_id)
    finally:
        session.close()
    if visible is not None and place_id not in visible:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    if details is None:
        await query.answer()
        state.clear()
        state.update(_new_manage())
        await _render_phase(
            update, context, state, notice="Место не существует или удалено."
        )
        return
    await query.answer()
    state["phase"] = "manage_card"
    state["place_id"] = place_id
    await _render_phase(update, context, state)


async def handle_manage_edit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «✏️ Редактировать»: переход в существующее меню правки кабинета места.

    Права проверяются заново по БД (IDOR): чужое место не открывает
    административный экран даже подделанной кнопкой.
    """
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "manage_card":
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("plm_edit:") :])
    except (TypeError, ValueError):
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    if state.get("place_id") != place_id:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        allowed = can_manage_place(session, user_id, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer("Место не существует или удалено.", show_alert=True)
        return
    if not allowed:
        await query.answer(
            "Редактирование доступно администратору места.", show_alert=True
        )
        return
    # Ответ на callback даёт сам переход: повторный `answer` на тот же
    # callback Telegram отклоняет, и ошибка выглядела бы как сбой экрана.
    from handlers.admin import open_place_admin_editor

    await open_place_admin_editor(update, context, place_id)


async def handle_manage_admins(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«👥 Администраторы»: только суперадмин — карточка места кабинета."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "manage_card":
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("plm_admins:") :])
    except (TypeError, ValueError):
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    if state.get("place_id") != place_id:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    user_id = ctx.user(update).id
    if not is_superadmin(user_id):
        await query.answer(
            "Назначение администраторов доступно только суперадминистратору.",
            show_alert=True,
        )
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer("Место не существует или удалено.", show_alert=True)
        return
    # Ответ на callback даёт переход в кабинет: второй `answer` на тот же
    # callback Telegram отклоняет.
    from handlers.admin import open_place_admin_card

    await open_place_admin_card(update, context, place_id)


async def handle_manage_admin_request(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«🛡 Я админ места»: только объяснение, роль не меняется."""
    query = ctx.query(update)
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") != "manage_card":
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("plm_admin_request:") :])
    except (TypeError, ValueError):
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    if state.get("place_id") != place_id:
        await query.answer(MANAGE_STALE_TEXT, show_alert=True)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        manages = can_manage_place(session, user_id, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer("Место не существует или удалено.", show_alert=True)
        return
    if manages:
        await query.answer("У вас уже есть права на управление этим местом.")
        await _render_phase(update, context, state)
        return
    await query.answer()
    state["phase"] = "manage_admin_request"
    await _render_phase(update, context, state)


async def handle_manage_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«➕ Добавить место» из справочника: создание без автозапуска анонса."""
    state = load_place_session(ctx.user(update).id)
    if state is None or state.get("phase") not in (
        MANAGE_LIST_PHASES | {"manage_search_input"}
    ):
        await _stale(update, MANAGE_STALE_TEXT)
        return
    await ctx.query(update).answer()
    state.clear()
    state.update({"phase": "create_input", "manage_mode": True})
    await _render_phase(update, context, state)


async def open_places(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в раздел: кнопка меню, /places или восстановление после рестарта."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    # «Места» и кабинет/кирпичики — взаимоисключающие ветки: старый
    # административный ввод или конструктор не должен перехватывать текст.
    clear_admin_session(ctx.user(update).id)
    clear_brick_session(ctx.user(update).id)
    clear_template_session(ctx.user(update).id)
    state = load_place_session(ctx.user(update).id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_hub()
    elif state.get("phase") in MANAGE_ONLY_PHASES:
        # Сохранённая фаза справочника не возобновляется в обычном разделе.
        state = _new_hub()
    else:
        # Обычный раздел «Места» — не справочник: режим управления сбрасывается,
        # иначе кнопка «◀️ Назад» уводила бы в `plm_back`/справочник.
        _drop_manage_mode(state)
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


async def _create_place_from_state(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    city: str,
    cleanup_ids: list[int] | None = None,
) -> None:
    """
    Создаёт место с городом и продолжает сценарий по режиму входа (U1a).

    Город спрашивается ПОСЛЕ проверки дублей и ДО создания строки: место без
    города больше не появляется (инвариант владельца). Ошибка валидации
    оставляет пользователя в фазе ввода города, ошибка БД — тоже, но с
    отдельным сообщением: экран не должен ломаться.
    """
    candidate = state.get("candidate")
    if not isinstance(candidate, str) or not candidate:
        state.clear()
        state.update(_new_hub())
        await _render_phase(
            update, context, state, notice="Название места потеряно. Начните заново."
        )
        return
    pick_mode = bool(state.get("pick_mode"))
    manage_mode = bool(state.get("manage_mode"))

    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        place = create_place(session, candidate, city=city, created_by_user_id=user.id)
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
        # Город не принят: остаёмся в фазе ввода, данные не теряются.
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
    if pick_mode:
        # Poster 2.0 S2: создание площадки из «Создать для площадки» сразу
        # открывает анонс по её стандартному шаблону. Вопрос о городе уже
        # задан здесь; анонс не спрашивает его повторно.
        await _auto_start_pick_announcement(update, context, place_id)
        return
    if manage_mode:
        # Poster 2.1 R2: создание из справочника открывает КАРТОЧКУ места.
        # Стандартный шаблон уже создан в `create_place`; анонс не стартует.
        state.clear()
        state.update(_new_manage())
        state["phase"] = "manage_card"
        state["place_id"] = place_id
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "✅ Место добавлено вместе со стандартным шаблоном. "
                "Оно сразу доступно всем пользователям."
            ),
            cleanup_ids=cleanup_ids,
        )
        return
    await _render_phase(
        update,
        context,
        state,
        notice="✅ Место добавлено. Оно сразу доступно всем пользователям.",
        cleanup_ids=cleanup_ids,
    )


async def handle_create_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «✅ Это новое место»: переход к вопросу о городе (Poster 2.0 U1a).

    Место создаётся не сразу: сначала пользователь отвечает на простой
    текстовый вопрос о городе. Дубль-проверка и подтверждение названия при
    этом уже пройдены — назад в «create_check» кнопка не возвращает.
    """
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
    cleanup = _failed_take(state)
    state["phase"] = "create_city_input"
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=cleanup,
        drop_input=True,
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
    elif phase == "manage_search_results":
        state["phase"] = "manage_search_input"
    elif phase == "manage_search_input":
        state["phase"] = "manage_list"
        state.pop("query", None)
    elif phase == "manage_admin_request":
        state["phase"] = "manage_card"
    elif phase in {"search_input", "all_places"}:
        state.clear()
        state.update(_new_hub())
    elif phase in {"create_input", "create_check", "create_city_input"}:
        if state.get("manage_mode"):
            state.clear()
            state.update(_new_manage())
        else:
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

    if phase == "manage_search_input":
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
        state["phase"] = "manage_search_results"
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

    if phase == "pick_search_input":
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
        state["phase"] = "pick_search_results"
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

    if phase == "create_city_input":
        try:
            city = validate_place_city(raw)
        except PlaceNameError as exc:
            _failed_add(state, update)
            await _render_phase(update, context, state, notice=str(exc))
            return
        cleanup = _failed_take(state)
        message = getattr(update, "message", None)
        if message is not None and message.message_id is not None:
            cleanup.append(int(message.message_id))
        await _create_place_from_state(
            update,
            context,
            state,
            city=city,
            cleanup_ids=cleanup,
        )
        # Город уже сохранён: вопрос и ответ убираются со экрана.
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

    Режим pick (Poster 2.0 S2) — вход «📍 Создать для площадки»: список
    площадок, поиск, добавление; тап по названию немедленно начинает анонс.
    Точка входа тапа (`pl_pick:<id>`) зарегистрирована в
    `announcement_wizard_handlers` как entry_point диалога конструктора.
    """
    return [
        CommandHandler("places", open_places),
        CallbackQueryHandler(open_places, pattern=r"^places$"),
        # Poster 2.0 S2: выбор площадки для немедленного старта анонса.
        CallbackQueryHandler(open_place_picker, pattern=r"^pick_place$"),
        CallbackQueryHandler(handle_pick_page, pattern=r"^pl_pickp:\d+$"),
        CallbackQueryHandler(handle_pick_search, pattern=r"^pl_pick_search$"),
        CallbackQueryHandler(handle_pick_search_page, pattern=r"^pl_picksp:\d+$"),
        CallbackQueryHandler(handle_pick_back, pattern=r"^pl_pick_back$"),
        CallbackQueryHandler(handle_pick_add, pattern=r"^pl_pick_add$"),
        # Poster 2.1 R2: справочник «Управление → Площадки» (без анонса).
        # Точка входа `manage_places` зарегистрирована в main_menu_handlers.
        CallbackQueryHandler(handle_manage_page, pattern=r"^plm_page:\d+$"),
        CallbackQueryHandler(handle_manage_search, pattern=r"^plm_search$"),
        CallbackQueryHandler(handle_manage_search_page, pattern=r"^plm_spage:\d+$"),
        CallbackQueryHandler(handle_manage_back, pattern=r"^plm_back$"),
        CallbackQueryHandler(handle_manage_open, pattern=r"^plm_open:\d+$"),
        CallbackQueryHandler(handle_manage_edit, pattern=r"^plm_edit:\d+$"),
        CallbackQueryHandler(handle_manage_admins, pattern=r"^plm_admins:\d+$"),
        CallbackQueryHandler(
            handle_manage_admin_request, pattern=r"^plm_admin_request:\d+$"
        ),
        CallbackQueryHandler(handle_manage_add, pattern=r"^plm_add$"),
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
