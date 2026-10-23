# handlers/admin.py
"""
Административный кабинет Stage 20A: роли, места и назначение администраторов.

Безопасность:
- актор всегда берётся из `update.effective_user.id`, а не из callback;
- суперадминистратор проверяется по `SUPERADMIN_USER_IDS` при каждом действии;
- администратор места проверяется по актуальной строке `place_admins`;
- callback передаёт только id места/получателя, но не подтверждает роль;
- назначение/замена/отзыв дополнительно проверяют права и ожидаемое состояние
  в слое `place_admin_store`, а инвариант «не более одного админа» обеспечивает
  частичный уникальный индекс SQLite;
- состояние `admin_sessions` хранит только навигацию и является подсказкой,
  а не источником полномочий.

Текстовый ввод принимается только в явной фазе поиска получателя. Обработчик
зарегистрирован после мастера поста и «Мест», поэтому не перехватывает их
поля; при входе в кабинет сценарий «Места» очищается.
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape as html_escape

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

from admin_state import (
    clear_admin_session,
    load_admin_session,
    save_admin_session,
)
from database import SessionLocal
from permissions import (
    can_access_admin_panel,
    can_manage_place,
    has_any_place_admin_role,
    is_superadmin,
)
from place_admin_store import (
    ADMIN_PLACES_PAGE_SIZE,
    AdminMutationResult,
    AdminPermissionError,
    AdminUser,
    UserSearchError,
    assign_place_admin,
    get_active_place_admin,
    list_places_for_administration,
    replace_place_admin,
    revoke_place_admin,
    search_known_users,
)
from place_state import clear_place_session
from place_store import PlaceDetails, get_place_details
from user_store import get_or_create_user
from utils import screen
from utils import tg_context as ctx

logger = logging.getLogger(__name__)

BUTTON_TEXT_LIMIT = 60
STALE_TEXT = "Кнопка устарела: откройте административный кабинет заново."
DENIED_TEXT = "Раздел доступен только администраторам Poster."
DENIED_ALERT = "Доступ к административному разделу закрыт."

TEXT_INPUT_PHASES = {"assign_search"}

PHASES = {
    "places",
    "place",
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
}

SUPER_ONLY_PHASES = {
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
}


def _h(value: object) -> str:
    return html_escape(str(value))


def _short(value: object, limit: int = 100) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _safe_link(url: str) -> str:
    if url.lower().startswith(("http://", "https://")):
        return f'<a href="{_h(url)}">{_h(url)}</a>'
    return _h(url)


def _button_name(prefix: str, text: str) -> str:
    available = max(BUTTON_TEXT_LIMIT - len(prefix), 1)
    if len(text) <= available:
        return prefix + text
    return prefix + text[: max(available - 1, 1)] + "…"


def _user_label(user: AdminUser, *, with_id: bool = False) -> str:
    if user.full_name:
        base = user.full_name
        if user.username:
            base += f" (@{user.username})"
    elif user.username:
        base = f"@{user.username}"
    else:
        base = f"ID {user.telegram_user_id}"
    if with_id:
        base += f"\nПостоянный Telegram user ID: {user.telegram_user_id}"
    return base


def _date_label(value: datetime | None) -> str:
    if value is None:
        return "—"
    try:
        return value.strftime("%d.%m.%Y %H:%M UTC")
    except (AttributeError, ValueError):  # pragma: no cover — неожиданный тип
        return "—"


def _new_state() -> dict:
    return {"phase": "places", "page": 0}


def _page_value(state: dict) -> int:
    try:
        value = int(state.get("page") or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _clamp_offset(offset: int, total: int, page_size: int) -> int:
    if total <= 0:
        return 0
    max_offset = ((total - 1) // page_size) * page_size
    return min(max(offset, 0), max_offset)


def _page_note(offset: int, count: int, total: int) -> str:
    if total <= 0:
        return ""
    return (
        f"Показано {offset + 1}–{offset + count} из {total}.\n"
        f"Страница {offset // ADMIN_PLACES_PAGE_SIZE + 1}."
    )


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


def _ensure_user(session: Session, update: Update) -> None:
    telegram_user = ctx.user(update)
    get_or_create_user(
        session,
        telegram_user.id,
        username=getattr(telegram_user, "username", None),
        full_name=getattr(telegram_user, "full_name", None),
    )


def _access_kind(session: Session, telegram_user_id: int) -> str | None:
    if is_superadmin(telegram_user_id):
        return "superadmin"
    if has_any_place_admin_role(session, telegram_user_id):
        return "place_admin"
    return None


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
    """Сохраняет состояние, рисует экран и только потом чистит ввод."""
    user_id = ctx.user(update).id
    body = f"{notice}\n\n{text}" if notice else text
    saved = save_admin_session(user_id, state)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(context, user_data, chat_id, body, keyboard)
    screen.save_screen_ref(user_id, user_data, chat_id)
    if saved:
        if cleanup_ids:
            await screen.cleanup_messages(
                context.bot, chat_id, sorted(set(cleanup_ids))
            )
        if drop_input:
            await screen.drop_user_input(update, context)
    return saved


async def _render_denied(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закрывает кабинет; выдаёт только главное меню, без административных кнопок."""
    user_id = ctx.user(update).id
    clear_admin_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    await screen.render_screen(
        context,
        user_data,
        ctx.chat(update).id,
        DENIED_TEXT,
        InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
        ),
    )
    screen.save_screen_ref(user_id, user_data, ctx.chat(update).id)


def _places_content(
    session: Session,
    state: dict,
    access_kind: str,
    telegram_user_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    offset = max(_page_value(state), 0)
    items, total = list_places_for_administration(
        session,
        telegram_user_id,
        offset=offset,
        limit=ADMIN_PLACES_PAGE_SIZE,
    )
    offset = _clamp_offset(offset, total, ADMIN_PLACES_PAGE_SIZE)
    if offset != _page_value(state):
        items, total = list_places_for_administration(
            session,
            telegram_user_id,
            offset=offset,
            limit=ADMIN_PLACES_PAGE_SIZE,
        )
    state["page"] = offset

    if access_kind == "superadmin":
        heading = "🛡 Все места"
        intro = "Выберите место, чтобы назначить, заменить или снять " "администратора."
    else:
        heading = "🛡 Мои места"
        intro = "Здесь показаны только места, которыми вы управляете."

    if total == 0:
        text = f"{heading}\n\n{intro}\n\n" + (
            "В справочнике пока нет мест."
            if access_kind == "superadmin"
            else "Действующих назначений пока нет."
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
        )
        return text, keyboard

    text = f"{heading} — {total}\n\n{intro}\n\n"
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(items, start=offset + 1):
        admin_line = (
            _h(_short(_user_label(item.admin), 90))
            if item.admin is not None
            else "не назначен"
        )
        text += (
            f"{index}. {_h(_short(item.canonical_name, 100))}\n"
            f"   Администратор: {admin_line}\n"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", item.canonical_name),
                    callback_data=f"adm_place:{item.place_id}",
                )
            ]
        )
    text += "\n" + _page_note(offset, len(items), total)

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"adm_page:{max(offset - ADMIN_PLACES_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(items) < total:
        nav.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"adm_page:{offset + len(items)}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _place_text(details: PlaceDetails, state: dict, session: Session) -> str:
    active = get_active_place_admin(session, details.place_id)
    text = f"🛡 Место\n\n📍 {_h(details.canonical_name)}"
    alternatives = [item for item in details.names if not item.is_canonical]
    if alternatives:
        text += "\n\nАльтернативные названия:\n" + "\n".join(
            f"— {_h(_short(item.name, 120))}" for item in alternatives[:8]
        )
        if len(alternatives) > 8:
            text += f"\nЕщё {len(alternatives) - 8}."
    if details.address:
        text += f"\n\nАдрес: {_h(_short(details.address, 500))}"
    if details.url:
        text += f"\n\nСсылка: {_safe_link(_short(details.url, 500))}"
    if active is None:
        text += "\n\nАдминистратор: не назначен."
    else:
        text += (
            "\n\nАдминистратор: "
            f"{_h(_user_label(active.user, with_id=True))}\n"
            f"Назначен: {_h(_date_label(active.assigned_at))}"
        )
    text += (
        "\n\nВ Stage 20A кабинет управляет только ролями. "
        "Редактирование данных места — Stage 20B, передача админа — Stage 21."
    )
    return text


def _place_keyboard(
    details: PlaceDetails,
    session: Session,
    *,
    access_kind: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if access_kind == "superadmin":
        active = get_active_place_admin(session, details.place_id)
        if active is None:
            rows.append(
                [
                    InlineKeyboardButton(
                        "👤 Назначить администратора",
                        callback_data=f"adm_assign:{details.place_id}",
                    )
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🔁 Заменить администратора",
                        callback_data=f"adm_replace:{details.place_id}",
                    )
                ]
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        "🚫 Снять администратора",
                        callback_data=f"adm_revoke:{details.place_id}",
                    )
                ]
            )
    rows.append([InlineKeyboardButton("◀️ К списку мест", callback_data="adm_places")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


def _assign_search_text(details: PlaceDetails, state: dict) -> str:
    mode = str(state.get("mode") or "assign")
    action = "замены администратора" if mode == "replace" else "назначения"
    return (
        f"👤 Поиск получателя для {action}\n\n"
        f"📍 {_h(details.canonical_name)}\n\n"
        "Введите @username, часть имени или точный постоянный Telegram user ID. "
        "Найти можно только пользователя, который уже взаимодействовал с "
        "Poster.\n\n"
        "Если получатель ещё не запускал бота по этой ссылке, попросите его "
        "сначала открыть Poster и нажать /start."
    )


def _assign_results_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    query = str(state.get("query") or "")
    results = search_known_users(session, query)
    text = (
        f"👤 Получатель\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Запрос: «{_h(_short(query, 100))}»\n\n"
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    if not results.users:
        text += (
            "Пользователь не найден среди известных боту. Если получатель "
            "ещё не запускал Poster, автоматически определить его user ID "
            "нельзя: попросите его открыть бота и нажать /start, затем "
            "повторите поиск."
        )
    else:
        if results.total == len(results.users):
            text += f"Найдено: {results.total}.\n\n"
        else:
            text += (
                f"Найдено: {results.total}; показаны первые "
                f"{len(results.users)}. Уточните запрос.\n\n"
            )
        for index, user in enumerate(results.users, start=1):
            label = _short(_user_label(user), 100)
            text += (
                f"{index}. {_h(label)}\n"
                f"   Telegram user ID: {user.telegram_user_id}\n"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        _button_name(f"{index}. ", _user_label(user)),
                        callback_data=f"adm_pick:{user.telegram_user_id}",
                    )
                ]
            )
    keyboard.append(
        [InlineKeyboardButton("🔁 Искать снова", callback_data="adm_search_again")]
    )
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")])
    return text, InlineKeyboardMarkup(keyboard)


def _confirmation_content(
    session: Session, details: PlaceDetails, state: dict, phase: str
) -> tuple[str, InlineKeyboardMarkup] | None:
    active = get_active_place_admin(session, details.place_id)
    target: AdminUser | None = None
    if phase != "confirm_revoke":
        target_value = state.get("target_telegram_user_id")
        if not isinstance(target_value, int):
            return None
        target_row = search_known_users(session, str(target_value))
        if not target_row.users:
            return None
        target = target_row.users[0]

    if phase == "confirm_assign":
        if active is not None or target is None:
            return None
        title = "Подтвердите назначение"
        action_line = "Назначить этого пользователя администратором места?"
        text = (
            f"{title}\n\n"
            f"📍 {_h(details.canonical_name)}\n"
            f"Получатель: {_h(_user_label(target, with_id=True))}\n\n"
            f"{action_line}"
        )
    elif phase == "confirm_replace":
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            return None
        if target is None:
            return None
        title = "Подтвердите замену администратора"
        text = (
            f"{title}\n\n"
            f"📍 {_h(details.canonical_name)}\n"
            f"Сейчас: {_h(_user_label(active.user, with_id=True))}\n"
            f"Новый администратор: {_h(_user_label(target, with_id=True))}\n\n"
            "Текущий администратор будет снят, новый назначен в одной "
            "транзакции."
        )
    else:
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            return None
        title = "Подтвердите снятие администратора"
        text = (
            f"{title}\n\n"
            f"📍 {_h(details.canonical_name)}\n"
            f"Администратор: {_h(_user_label(active.user, with_id=True))}\n\n"
            "После снятия у места не останется действующего админа."
        )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить", callback_data="adm_confirm")],
            [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel_confirm")],
        ]
    )
    return text, keyboard


async def _render_phase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
) -> bool:
    """Единая отрисовка фазы; каждый раз проверяет актуальные права."""
    user_id = ctx.user(update).id
    phase = str(state.get("phase") or "")
    session = SessionLocal()
    try:
        access_kind = _access_kind(session, user_id)
        if access_kind is None:
            session.close()
            await _render_denied(update, context)
            return False

        if phase not in PHASES:
            state.clear()
            state.update(_new_state())
            phase = "places"
        if phase in SUPER_ONLY_PHASES and access_kind != "superadmin":
            state.clear()
            state.update(_new_state())
            phase = "places"
            notice = notice or "Действие доступно только суперадминистратору."

        place_id = state.get("place_id")
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )

        if phase == "places":
            text, keyboard = _places_content(session, state, access_kind, user_id)
        elif phase == "place":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not can_manage_place(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or "Доступ к этому месту не подтверждён."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text = _place_text(details, state, session)
                keyboard = _place_keyboard(details, session, access_kind=access_kind)
        elif phase == "assign_search":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                text = _assign_search_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")]]
                )
        elif phase == "assign_results":
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not state.get("query"):
                state["phase"] = "assign_search"
                text = _assign_search_text(details, state)
                keyboard = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")]]
                )
            else:
                text, keyboard = _assign_results_content(session, details, state)
        else:  # confirm_*
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                confirmation = _confirmation_content(session, details, state, phase)
                if confirmation is None:
                    state["phase"] = "place"
                    notice = notice or (
                        "Ситуация изменилась: обновите выбор администратора."
                    )
                    text = _place_text(details, state, session)
                    keyboard = _place_keyboard(
                        details, session, access_kind=access_kind
                    )
                else:
                    text, keyboard = confirmation
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


async def open_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в кабинет: /admin, кнопка меню или восстановление после рестарта."""
    query = getattr(update, "callback_query", None)
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        allowed = can_access_admin_panel(session, user_id)
    except Exception as e:  # noqa: BLE001 — отказ безопаснее открытия
        logger.error("Не удалось проверить доступ в кабинет: %s", e)
        allowed = False
    finally:
        session.close()

    if not allowed:
        clear_admin_session(user_id)
        if query is not None:
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        await _render_denied(update, context)
        await screen.drop_user_input(update, context)
        return

    if query is not None:
        await query.answer()
    clear_place_session(user_id)
    state = load_admin_session(user_id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_state()
    await _render_phase(update, context, state, drop_input=True)


async def handle_places_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("adm_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_open_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_place:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        allowed = can_access_admin_panel(session, ctx.user(update).id)
        manages = allowed and can_manage_place(session, ctx.user(update).id, place_id)
        details = get_place_details(session, place_id) if manages else None
    finally:
        session.close()
    if details is None:
        await query.answer("Место недоступно.", show_alert=True)
        return
    await query.answer()
    state["place_id"] = place_id
    state["phase"] = "place"
    await _render_phase(update, context, state)


async def handle_assign(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_assign:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        if get_place_details(session, place_id) is None:
            await query.answer("Место не найдено.", show_alert=True)
            return
        if get_active_place_admin(session, place_id) is not None:
            await query.answer(
                "У места уже есть администратор. Используйте замену.",
                show_alert=True,
            )
            return
    finally:
        session.close()
    await query.answer()
    state["mode"] = "assign"
    state.pop("expected_admin_telegram_user_id", None)
    state["phase"] = "assign_search"
    await _render_phase(update, context, state)


async def handle_replace(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_replace:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None or active is None:
        await query.answer(
            "Администратор уже изменился. Обновите карточку.", show_alert=True
        )
        return
    await query.answer()
    state["mode"] = "replace"
    state["expected_admin_telegram_user_id"] = active.user.telegram_user_id
    state["phase"] = "assign_search"
    await _render_phase(update, context, state)


async def handle_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    data = query.data or ""
    try:
        place_id = int(data[len("adm_revoke:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    if state.get("place_id") != place_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None or active is None:
        await query.answer(
            "Администратор уже изменился. Обновите карточку.", show_alert=True
        )
        return
    await query.answer()
    state["expected_admin_telegram_user_id"] = active.user.telegram_user_id
    state["phase"] = "confirm_revoke"
    await _render_phase(update, context, state)


async def handle_search_again(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"assign_results", "assign_search"}:
        await _stale(update)
        return
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "assign_search"
    state.pop("query", None)
    await _render_phase(update, context, state)


async def handle_pick_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "assign_results":
        await _stale(update)
        return
    data = query.data or ""
    try:
        telegram_user_id = int(data[len("adm_pick:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    query_text = state.get("query")
    place_id = state.get("place_id")
    if not isinstance(query_text, str) or not isinstance(place_id, int):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        results = search_known_users(session, query_text)
        visible_ids = {user.telegram_user_id for user in results.users}
        if telegram_user_id not in visible_ids:
            await _stale(update)
            return
        details = get_place_details(session, place_id)
        active = get_active_place_admin(session, place_id)
    finally:
        session.close()
    if details is None:
        await query.answer("Место не существует.", show_alert=True)
        return

    mode = state.get("mode") or "assign"
    if mode == "replace":
        expected = state.get("expected_admin_telegram_user_id")
        if active is None or active.user.telegram_user_id != expected:
            await query.answer(
                "Администратор уже изменился. Выберите действие заново.",
                show_alert=True,
            )
            state["phase"] = "place"
            await _render_phase(update, context, state)
            return
    elif active is not None:
        await query.answer(
            "У места уже появился администратор. Выберите замену.",
            show_alert=True,
        )
        state["phase"] = "place"
        await _render_phase(update, context, state)
        return

    await query.answer()
    state["target_telegram_user_id"] = telegram_user_id
    state["phase"] = "confirm_replace" if mode == "replace" else "confirm_assign"
    await _render_phase(update, context, state)


def _mutation_notice(result: AdminMutationResult) -> str:
    if result.status == "assigned":
        return "✅ Администратор назначен."
    if result.status == "replaced":
        return "✅ Администратор заменён."
    if result.status == "revoked":
        return "✅ Администратор снят."
    if result.status == "already_admin":
        return "Этот пользователь уже управляет этим местом."
    if result.status == "has_admin":
        return (
            "Назначение не выполнено: у места уже есть администратор. "
            "Используйте замену."
        )
    if result.status == "user_missing":
        return "Получатель больше не известен боту. Выберите другого."
    if result.status == "place_missing":
        return "Место не существует или было удалено."
    return (
        "Данные изменились во время подтверждения. Действие не выполнено; "
        "обновите выбор."
    )


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    user = ctx.user(update)
    if not is_superadmin(user.id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "confirm_assign",
        "confirm_replace",
        "confirm_revoke",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    await query.answer()

    phase = state.get("phase")
    target = state.get("target_telegram_user_id")
    expected = state.get("expected_admin_telegram_user_id")
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        if phase == "confirm_revoke":
            if not isinstance(expected, int):
                await _stale(update)
                return
            result = revoke_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                expected_admin_telegram_user_id=expected,
            )
        elif phase == "confirm_replace":
            if not isinstance(target, int) or not isinstance(expected, int):
                await _stale(update)
                return
            result = replace_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                target_telegram_user_id=target,
                expected_admin_telegram_user_id=expected,
            )
        else:
            if not isinstance(target, int):
                await _stale(update)
                return
            result = assign_place_admin(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                target_telegram_user_id=target,
            )
    except AdminPermissionError:
        clear_admin_session(user.id)
        await _render_denied(update, context)
        return
    except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
        logger.error("Не удалось изменить администратора места: %s", e)
        state["phase"] = "place"
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось изменить администратора из-за ошибки базы данных.",
        )
        return
    finally:
        session.close()

    if result.status == "place_missing":
        state.clear()
        state.update(_new_state())
    else:
        state["phase"] = "place"
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
    ):
        state.pop(key, None)
    await _render_phase(update, context, state, notice=_mutation_notice(result))


async def handle_places_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Возврат из карточки/поиска к списку мест."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in PHASES - {"places"}:
        await _stale(update)
        return
    await query.answer()
    state.clear()
    state.update(_new_state())
    await _render_phase(update, context, state)


async def handle_cancel_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена поиска/подтверждения: возврат к карточке места."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "assign_search",
        "assign_results",
        "confirm_assign",
        "confirm_replace",
        "confirm_revoke",
    }:
        await _stale(update)
        return
    await query.answer()
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
    ):
        state.pop(key, None)
    if isinstance(state.get("place_id"), int):
        state["phase"] = "place"
    else:
        state.clear()
        state.update(_new_state())
    await _render_phase(update, context, state)


async def handle_cancel_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Отмена на экране подтверждения без изменения прав."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "confirm_assign",
        "confirm_replace",
        "confirm_revoke",
    }:
        await _stale(update)
        return
    await query.answer()
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
    ):
        state.pop(key, None)
    state["phase"] = "place"
    await _render_phase(update, context, state)


async def handle_search_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Текст в фазе поиска получателя (только суперадминистратор)."""
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "assign_search":
        return
    if not is_superadmin(ctx.user(update).id):
        clear_admin_session(ctx.user(update).id)
        await _render_denied(update, context)
        return

    await screen.reanchor_screen(update, context)
    raw = ctx.message(update).text or ""
    query = " ".join(raw.split())
    _failed_add(state, update)
    session = SessionLocal()
    try:
        search_known_users(session, query)
    except UserSearchError as e:
        await _render_phase(update, context, state, notice=str(e))
        return
    finally:
        session.close()

    state["query"] = query
    state["phase"] = "assign_results"
    cleanup = _failed_take(state)
    current = getattr(update, "message", None)
    current_id = getattr(current, "message_id", None)
    if current_id is not None:
        cleanup.append(int(current_id))
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=cleanup,
        drop_input=True,
    )


class AdminTextHandler(MessageHandler):
    """Текст принимается только в явной фазе поиска получателя кабинета."""

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_admin_session(user.id)
        if not isinstance(state, dict):
            return None
        if state.get("phase") not in TEXT_INPUT_PHASES:
            return None
        return matched


async def handle_admin_stale(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Любая необработанная кнопка adm_* — устаревшая, прав не меняет."""
    await _stale(update)


def admin_handlers() -> list[BaseHandler]:
    """
    Обработчики кабинета Stage 20A.

    Регистрируются после мастера поста и «Мест», но до общего возврата в меню:
    callback-и админки не пересекаются с их шаблонами. Текстовый обработчик
    активен только в фазе поиска получателя.
    """
    return [
        CommandHandler("admin", open_admin),
        CallbackQueryHandler(open_admin, pattern=r"^admin_panel$"),
        CallbackQueryHandler(handle_places_page, pattern=r"^adm_page:\d+$"),
        CallbackQueryHandler(handle_open_place, pattern=r"^adm_place:\d+$"),
        CallbackQueryHandler(handle_assign, pattern=r"^adm_assign:\d+$"),
        CallbackQueryHandler(handle_replace, pattern=r"^adm_replace:\d+$"),
        CallbackQueryHandler(handle_revoke, pattern=r"^adm_revoke:\d+$"),
        CallbackQueryHandler(handle_pick_user, pattern=r"^adm_pick:\d+$"),
        CallbackQueryHandler(handle_search_again, pattern=r"^adm_search_again$"),
        CallbackQueryHandler(handle_cancel_confirm, pattern=r"^adm_cancel_confirm$"),
        CallbackQueryHandler(handle_cancel_flow, pattern=r"^adm_cancel$"),
        CallbackQueryHandler(handle_places_back, pattern=r"^adm_places$"),
        CallbackQueryHandler(handle_confirm, pattern=r"^adm_confirm$"),
        AdminTextHandler(filters.TEXT & ~filters.COMMAND, handle_search_text),
        CallbackQueryHandler(handle_admin_stale, pattern=r"^adm_"),
    ]
