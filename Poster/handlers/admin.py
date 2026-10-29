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

Текстовый ввод принимается только в явных фазах кабинета: поиск получателя
назначения (Stage 20A) и поля редактирования места (Stage 20B). Обработчик
зарегистрирован после мастера поста и «Мест», поэтому не перехватывает их
поля; при входе в кабинет сценарий «Места» очищается.
"""

from __future__ import annotations

import logging
from datetime import datetime
from html import escape as html_escape
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
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
from admin_transfer_store import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    TransferUser,
    TransferView,
    approve_transfer_recipient,
    cancel_admin_transfer,
    confirm_admin_transfer,
    create_admin_transfer,
    find_known_recipients,
    get_pending_transfer_for_place,
    get_transfer_view,
    mark_transfer_notified,
    normalize_transfer_username,
    open_transfer_invitation,
    reject_admin_transfer,
    transfer_status_label,
)
from brick_proposal_store import (
    PROPOSAL_PAGE_SIZE,
    PROPOSAL_SCOPE_SYSTEM,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_WITHDRAWN,
    BrickProposalSnapshot,
    ProposalActorError,
    ProposalPermissionError,
    approve_brick_proposal,
    count_pending_for_place,
    count_pending_place_proposals,
    count_pending_system_proposals,
    get_admin_proposal,
    list_pending_for_place,
    list_pending_place_proposals,
    list_pending_system_proposals,
    reject_brick_proposal,
)
from brick_state import clear_brick_session
from brick_store import (
    BrickConflictError,
    BrickConflictView,
    BrickPermissionError,
    ConflictResolutionResult,
    count_open_conflicts_for_place,
    get_conflict,
    list_pending_conflict_notifications,
    list_place_conflicts,
    mark_conflict_notification,
    resolve_conflict_by_rename,
    resolve_conflict_by_switch,
)
from brick_types import (
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_TITLES,
)
from database import SessionLocal
from models import User
from permissions import (
    can_access_admin_panel,
    can_manage_place,
    has_any_place_admin_role,
    is_place_admin,
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
from place_edit_store import (
    AliasRow,
    EditMutationResult,
    PlaceEditPermissionError,
    PlaceEditValueError,
    add_place_alias,
    clean_address_text,
    delete_place_address,
    delete_place_alias,
    delete_place_url,
    list_place_aliases,
    normalize_place_url,
    rename_canonical_place_name,
    rename_place_alias,
    set_place_address,
    set_place_url,
)
from place_state import clear_place_session
from place_store import (
    PlaceDetails,
    PlaceNameError,
    find_name_conflicts,
    get_place_details,
    normalize_place_name,
    validate_place_name,
)
from template_state import clear_template_session
from user_store import get_or_create_user
from utils import screen
from utils import tg_context as ctx

logger = logging.getLogger(__name__)

BUTTON_TEXT_LIMIT = 60
STALE_TEXT = "Кнопка устарела: откройте административный кабинет заново."
DENIED_TEXT = "Раздел доступен только администраторам Poster."
DENIED_ALERT = "Доступ к административному разделу закрыт."

PHASES = {
    "places",
    "place",
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
    "edit_menu",
    "edit_name_input",
    "edit_name_confirm",
    "edit_aliases",
    "edit_alias_add_input",
    "edit_alias_add_confirm",
    "edit_alias_rename_input",
    "edit_alias_rename_confirm",
    "edit_alias_delete_confirm",
    "edit_address_input",
    "edit_address_confirm",
    "edit_address_delete_confirm",
    "edit_url_input",
    "edit_url_confirm",
    "edit_url_delete_confirm",
    "transfer_username_input",
    "transfer_candidates",
    "transfer_confirm_known",
    "transfer_confirm_invite",
    "transfer_outgoing",
    "proposal_global_list",
    "proposal_places_list",
    "proposal_card",
    "proposal_approve_confirm",
    "proposal_reject_confirm",
    "conflicts_list",
    "conflict_card",
    "conflict_rename_input",
    "conflict_switch_confirm",
}

_SUPER_ONLY_PHASES = {
    "assign_search",
    "assign_results",
    "confirm_assign",
    "confirm_replace",
    "confirm_revoke",
    "proposal_global_list",
}
SUPER_ONLY_PHASES = _SUPER_ONLY_PHASES


def _h(value: object) -> str:
    return html_escape(str(value))


def _short(value: object, limit: int = 100) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


# Экран Telegram ограничен 4096 символами. Длинный адрес хранится полностью;
# на экране показываем начало с ЯВНОЙ пометкой, а не молчаливое «…».
DISPLAY_VALUE_LIMIT = 1500


def _long_value(value: object, limit: int = DISPLAY_VALUE_LIMIT) -> str:
    """HTML-безопасный фрагмент длинного значения без потери данных в БД."""
    text = str(value)
    if len(text) <= limit:
        return _h(text)
    return (
        _h(text[:limit])
        + f"\n… Показаны первые {limit} символов. Полное значение сохранено."
    )


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


def _transfer_user_label(user: TransferUser, *, with_id: bool = False) -> str:
    """Подпись получателя в экранах передачи (username — не идентификатор)."""
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


def _transfer_username_content(
    details: PlaceDetails, active_admin: AdminUser | None
) -> tuple[str, InlineKeyboardMarkup]:
    initiator = (
        _h(_user_label(active_admin, with_id=True))
        if active_admin is not None
        else "текущий администратор"
    )
    text = (
        "🔁 Передача администрации места\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Инициатор: {initiator}\n\n"
        "Передача лишит вас полномочий на это место только после того, как "
        "получатель подтвердит предложение в своём аккаунте. До этого вы "
        "остаётесь администратором и можете отменить передачу.\n\n"
        "Введите @username получателя. Username — только подсказка: если "
        "человек уже известен боту, вы выберете его по постоянному Telegram "
        "user ID. Если нет — бот создаст одноразовую ссылку-приглашение."
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")]]
    )
    return text, keyboard


def _transfer_candidates_content(
    details: PlaceDetails, candidates: tuple[TransferUser, ...], state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    username = _h(str(state.get("transfer_username") or ""))
    text = (
        "👤 Найдено несколько известных пользователей\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Username: @{username}\n\n"
        "Одинаковый username не позволяет надёжно выбрать человека. "
        "Укажите конкретного получателя по постоянному Telegram user ID:"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for index, user in enumerate(candidates, start=1):
        text += (
            f"\n{index}. {_h(_short(_transfer_user_label(user), 100))}\n"
            f"   Telegram user ID: {user.telegram_user_id}"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"{index}. ", _transfer_user_label(user)),
                    callback_data=f"adm_transfer_pick:{user.telegram_user_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")])
    return text, InlineKeyboardMarkup(rows)


def _transfer_known_confirm_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup] | None:
    target_tg = state.get("transfer_target_telegram_user_id")
    if not isinstance(target_tg, int):
        return None
    target_row = session.scalar(select(User).where(User.telegram_user_id == target_tg))
    if target_row is None:
        return None
    target = TransferUser(
        user_id=target_row.id,
        telegram_user_id=target_row.telegram_user_id,
        username=target_row.username,
        full_name=target_row.full_name,
    )
    active = get_active_place_admin(session, details.place_id)
    text = (
        "📨 Подтвердите создание предложения\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"От: {_h(_user_label(active.user, with_id=True)) if active else '—'}\n"
        f"Получатель: {_h(_transfer_user_label(target, with_id=True))}\n\n"
        "Получатель уже известен боту, поэтому предложение будет привязано к "
        "его постоянному Telegram user ID, а не к username. Права перейдут "
        "только после его подтверждения; до этого вы остаётесь "
        "администратором. Предложение действует 24 часа."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Создать предложение", callback_data="adm_transfer_confirm"
                )
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel")],
        ]
    )


def _transfer_invite_confirm_content(
    details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    username = _h(str(state.get("transfer_username") or ""))
    text = (
        "🔗 Получатель пока не известен боту\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Username: @{username}\n\n"
        "По одному username нельзя достоверно определить Telegram user ID: "
        "username можно сменить, а освободившийся — занять другому человеку. "
        "Автоматической передачи по username не будет.\n\n"
        "Бот создаст одноразовую ссылку-приглашение на 24 часа. Открывший её "
        "пользователь станет лишь кандидатом: вы увидите его постоянный "
        "Telegram user ID и должны будете явно утвердить его. Только после "
        "этого получатель сможет подтвердить передачу.\n\n"
        "Права до подтверждения получателя остаются у вас."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 Создать ссылку-приглашение",
                    callback_data="adm_transfer_confirm",
                )
            ],
            [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel")],
        ]
    )


def _transfer_expiry_label(view: TransferView) -> str:
    return _date_label(view.expires_at)


def _transfer_outgoing_content(
    session: Session,
    details: PlaceDetails,
    state: dict,
    *,
    invite_url: str | None = None,
) -> tuple[str, InlineKeyboardMarkup] | None:
    view = get_pending_transfer_for_place(session, details.place_id)
    if view is None:
        return None

    text = (
        "📨 Передача администрации\n\n"
        f"📍 {_h(details.canonical_name)}\n"
        f"Статус: {_h(transfer_status_label(view.status))}\n"
        f"Действует до: {_h(_transfer_expiry_label(view))}\n"
        f"Инициатор: {_h(_transfer_user_label(view.from_user))}\n"
    )
    if view.to_user is not None:
        text += (
            "Получатель: " f"{_h(_transfer_user_label(view.to_user, with_id=True))}\n"
        )
    else:
        text += f"Username в приглашении: @{_h(view.to_username or '—')}\n"
        if view.opened_by is not None:
            text += (
                "Ссылку открыл: "
                f"{_h(_transfer_user_label(view.opened_by, with_id=True))}\n\n"
                "Это ещё не передача прав. Проверьте, что открывший — нужный "
                "вам человек, и утвердите его как получателя."
            )
        else:
            text += (
                "\nСсылку пока никто не открыл. Отправьте её получателю сами: "
                "бот не может первым написать незнакомому человеку."
            )

    if invite_url:
        text += (
            "\n\n🔗 Одноразовая ссылка-приглашение:\n"
            f"{_safe_link(invite_url)}\n\n"
            "Ссылка показывается только сейчас и хранится в базе лишь в виде "
            "хеша. Если вы её потеряли, отмените предложение и создайте новое."
        )
    elif view.has_invite_token and view.to_user is None:
        text += (
            "\n\nСсылка-приглашение была показана один раз при создании. "
            "Восстановить её из базы нельзя; если она потеряна, отмените "
            "предложение и создайте новое."
        )

    if view.awaiting_recipient:
        text += (
            "\n\nПолучатель известен, но ещё не подтвердил передачу. "
            "Права остаются у вас."
        )
        if view.notified_at is None:
            text += " Доставка уведомления получателю не подтверждена."
    elif view.awaiting_initiator_approval:
        text += "\n\nОжидается ваше утверждение кандидата."
    else:
        text += "\n\nПрава остаются у вас до подтверждения получателем."

    rows: list[list[InlineKeyboardButton]] = []
    if view.awaiting_initiator_approval:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Утвердить получателя",
                    callback_data=f"adm_transfer_approve:{view.transfer_id}",
                )
            ]
        )
    if view.awaiting_recipient:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔔 Уведомить получателя ещё раз",
                    callback_data=f"adm_transfer_notify:{view.transfer_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                "🔄 Обновить статус",
                callback_data=f"adm_transfer_refresh:{details.place_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "❌ Отменить передачу",
                callback_data=f"adm_transfer_cancel:{view.transfer_id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")])
    return text, InlineKeyboardMarkup(rows)


def _transfer_offer_text(view: TransferView) -> str:
    return (
        "📨 Вам предлагают администрирование места\n\n"
        f"📍 {_h(view.place_name)}\n"
        f"От: {_h(_transfer_user_label(view.from_user, with_id=True))}\n"
        f"Действует до: {_h(_transfer_expiry_label(view))}\n\n"
        "Если вы подтвердите, вы станете администратором этого места, а "
        "отправитель потеряет полномочия. До подтверждения права остаются "
        "у отправителя. Подтверждение действует только в вашем аккаунте "
        "Telegram."
    )


def _transfer_offer_keyboard(view: TransferView) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Подтверждаю",
                    callback_data=f"adm_transfer_accept:{view.transfer_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Отклонить",
                    callback_data=f"adm_transfer_decline:{view.transfer_id}",
                )
            ],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
        ]
    )


def _incoming_transfer_content(
    status: str, view: TransferView | None, viewer_telegram_user_id: int
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран получателя после открытия ссылки или нажатия кнопки."""
    menu = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
    )
    if view is None or status == "invalid_invite":
        return (
            "🔗 Ссылка-приглашение недействительна\n\n"
            "Проверьте, что ссылка скопирована полностью и не изменена. "
            "Попросите инициатора создать новое приглашение.",
            menu,
        )
    if status == "self":
        return (
            "🔁 Это ваша собственная ссылка-приглашение\n\n"
            "Передать админку самому себе нельзя. Отправьте ссылку другому "
            "человеку.",
            menu,
        )
    if status == "opened_by_other":
        return (
            "🔗 Ссылка уже открыта другим аккаунтом\n\n"
            "Приглашение нельзя перепривязать к другому человеку. Попросите "
            "инициатора отменить его и создать новую ссылку.",
            menu,
        )
    if status == "not_recipient":
        return (
            "🔒 Предложение адресовано другому пользователю\n\n"
            "Подтвердить передачу может только получатель, выбранный "
            "инициатором.",
            menu,
        )
    if status == "approved" or (
        view.status == STATUS_PENDING
        and view.to_user is not None
        and view.to_user.telegram_user_id == viewer_telegram_user_id
    ):
        return _transfer_offer_text(view), _transfer_offer_keyboard(view)
    if view.status == STATUS_PENDING and view.opened_by is not None:
        if view.opened_by.telegram_user_id == viewer_telegram_user_id:
            return (
                "✅ Приглашение открыто\n\n"
                f"📍 {_h(view.place_name)}\n"
                f"От: {_h(_transfer_user_label(view.from_user, with_id=True))}\n\n"
                "Это ещё не передача прав. Инициатор должен увидеть ваш "
                "постоянный Telegram user ID и явно утвердить вас как "
                "получателя. После этого вы сможете подтвердить передачу.\n\n"
                "До подтверждения администратором остаётся отправитель.",
                menu,
            )
        return (
            "🔗 Ссылка уже открыта другим аккаунтом\n\n"
            "Приглашение нельзя перепривязать к другому человеку. Попросите "
            "инициатора создать новую ссылку.",
            menu,
        )
    if view.status == STATUS_CONFIRMED:
        return (
            "✅ Передача уже завершена\n\n"
            f"📍 {_h(view.place_name)}\n\n"
            "Это предложение больше нельзя подтвердить повторно.",
            menu,
        )
    if view.status == STATUS_EXPIRED:
        return (
            "⌛ Срок приглашения истёк\n\n"
            f"📍 {_h(view.place_name)}\n\n"
            "Передача не состоялась; администратором остаётся отправитель. "
            "Можно попросить его создать новое предложение.",
            menu,
        )
    if view.status == STATUS_CANCELLED:
        return (
            "❌ Передача отменена инициатором\n\n"
            f"📍 {_h(view.place_name)}\n\n"
            "Полномочия не изменились.",
            menu,
        )
    if view.status == STATUS_REJECTED:
        return (
            "❌ Предложение отклонено\n\n"
            f"📍 {_h(view.place_name)}\n\n"
            "Полномочия не изменились. Можно создать новое предложение.",
            menu,
        )
    if view.status == STATUS_SUPERSEDED:
        return (
            "⚠️ Предложение недействительно\n\n"
            f"📍 {_h(view.place_name)}\n\n"
            "Администратор места изменился или назначение было отозвано. "
            "Передача не состоится.",
            menu,
        )
    return (
        "📨 Предложение передачи\n\n"
        f"📍 {_h(view.place_name)}\n\n"
        "Сейчас это предложение нельзя подтвердить.",
        menu,
    )


async def _deep_link(context: ContextTypes.DEFAULT_TYPE, payload: str) -> str | None:
    """Ссылка t.me/<bot>?start=...; username бота не секрет."""
    username = getattr(context.bot, "username", None)
    if not username:
        try:
            me = await context.bot.get_me()
            username = getattr(me, "username", None)
        except Exception as e:  # noqa: BLE001 — без username ссылку не показать
            logger.error("Не удалось получить username бота для ссылки: %s", e)
            return None
    if not username:
        return None
    return f"https://t.me/{username}?start={quote(payload, safe='')}"


async def _send_user_notification(
    context: ContextTypes.DEFAULT_TYPE,
    telegram_user_id: int,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
) -> bool:
    """
    Best-effort личное уведомление. True — Telegram подтвердил доставку.

    Токен приглашения в уведомлениях не передаётся. Ошибка доставки не
    отменяет предложение и не выдаёт/не отнимает прав.
    """
    try:
        await context.bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return True
    except Exception as e:  # noqa: BLE001 — сбой Telegram не ломает операцию
        logger.warning(
            "Не удалось отправить уведомление пользователю %s: %s",
            telegram_user_id,
            e,
        )
        return False


async def _notify_recipient(
    context: ContextTypes.DEFAULT_TYPE, view: TransferView
) -> bool:
    if view.to_user is None:
        return False
    return await _send_user_notification(
        context,
        view.to_user.telegram_user_id,
        _transfer_offer_text(view),
        _transfer_offer_keyboard(view),
    )


async def _notify_initiator(
    context: ContextTypes.DEFAULT_TYPE,
    view: TransferView,
    text: str,
) -> None:
    await _send_user_notification(context, view.from_user.telegram_user_id, text)


async def _edit_notification_message(
    query: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup | None = None
) -> None:
    """Обновляет сообщение, на кнопке которого нажали; при сбое — без падения."""
    try:
        await query.edit_message_text(
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:  # noqa: BLE001 — устаревшее сообщение не должно падать
        logger.info("Не удалось обновить сообщение передачи: %s", e)


def _new_state() -> dict:
    return {"phase": "places", "page": 0}


def _page_value(state: dict, key: str = "page") -> int:
    try:
        value = int(state.get(key) or 0)
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
        empty_rows: list[list[InlineKeyboardButton]] = []
        if access_kind == "superadmin":
            try:
                system_pending = count_pending_system_proposals(
                    session, telegram_user_id
                )
                places_pending = count_pending_place_proposals(
                    session, telegram_user_id
                )
            except ProposalPermissionError:
                system_pending = 0
                places_pending = 0
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        f"🌍 Глобальные предложения ({system_pending})",
                        callback_data="adm_bprop_global",
                    )
                ]
            )
            empty_rows.append(
                [
                    InlineKeyboardButton(
                        f"📍 Предложения по местам ({places_pending})",
                        callback_data="adm_bprop_places",
                    )
                ]
            )
        empty_rows.append(
            [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]
        )
        return text, InlineKeyboardMarkup(empty_rows)

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
    if access_kind == "superadmin":
        try:
            system_pending = count_pending_system_proposals(session, telegram_user_id)
            places_pending = count_pending_place_proposals(session, telegram_user_id)
        except ProposalPermissionError:
            system_pending = 0
            places_pending = 0
        rows.append(
            [
                InlineKeyboardButton(
                    f"🌍 Глобальные предложения ({system_pending})",
                    callback_data="adm_bprop_global",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"📍 Предложения по местам ({places_pending})",
                    callback_data="adm_bprop_places",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _place_text(
    details: PlaceDetails,
    state: dict,
    session: Session,
    *,
    viewer_telegram_user_id: int | None = None,
) -> str:
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
        text += f"\n\nАдрес: {_long_value(details.address)}"
    if details.url:
        text += f"\n\nСсылка: {_safe_link(details.url)}"
    open_conflicts = count_open_conflicts_for_place(session, details.place_id)
    if open_conflicts:
        text += (
            "\n\n⚔️ Активные задачи конфликтов кодов: "
            f"{open_conflicts}. Их нужно разрешить до правки привязок: "
            "кирпичик места или глобальный кирпичик."
        )
    if active is None:
        text += "\n\nАдминистратор: не назначен."
    else:
        text += (
            "\n\nАдминистратор: "
            f"{_h(_user_label(active.user, with_id=True))}\n"
            f"Назначен: {_h(_date_label(active.assigned_at))}"
        )
    if active is not None and viewer_telegram_user_id == active.user.telegram_user_id:
        pending = get_pending_transfer_for_place(session, details.place_id)
        if pending is not None:
            text += (
                "\n\n📨 Передача администрации: "
                f"{_h(transfer_status_label(pending.status))} до "
                f"{_h(_transfer_expiry_label(pending))}."
            )
    text += (
        "\n\nРедактирование названия, альтернативных названий, адреса и "
        "ссылки доступно из этого кабинета."
    )
    return text


def _place_keyboard(
    details: PlaceDetails,
    session: Session,
    *,
    access_kind: str,
    viewer_telegram_user_id: int | None = None,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                "✏️ Редактировать место",
                callback_data=f"adm_edit_place:{details.place_id}",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                "📄 Стандартный шаблон",
                callback_data=f"tp_std:{details.place_id}",
            )
        ]
    )
    if viewer_telegram_user_id is not None:
        try:
            pending_proposals = count_pending_for_place(
                session, viewer_telegram_user_id, details.place_id
            )
        except ProposalPermissionError:
            pending_proposals = 0
        rows.append(
            [
                InlineKeyboardButton(
                    f"🧱 Предложения кирпичиков ({pending_proposals})",
                    callback_data=f"adm_bprop_place:{details.place_id}",
                )
            ]
        )
        open_conflicts = count_open_conflicts_for_place(session, details.place_id)
        rows.append(
            [
                InlineKeyboardButton(
                    f"⚔️ Конфликты кодов ({open_conflicts})",
                    callback_data=f"adm_bconflicts:{details.place_id}",
                )
            ]
        )
    active = get_active_place_admin(session, details.place_id)
    if active is not None and viewer_telegram_user_id == active.user.telegram_user_id:
        rows.append(
            [
                InlineKeyboardButton(
                    "🔁 Передать админку",
                    callback_data=f"adm_transfer:{details.place_id}",
                )
            ]
        )
    if access_kind == "superadmin":
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


_PROPOSAL_STATUS_UI = {
    PROPOSAL_STATUS_PENDING: "⏳ Ожидает решения",
    PROPOSAL_STATUS_APPROVED: "✅ Одобрено",
    PROPOSAL_STATUS_REJECTED: "❌ Отклонено",
    PROPOSAL_STATUS_WITHDRAWN: "↩️ Отозвано автором",
}


def _proposal_status_label(status: str) -> str:
    return _PROPOSAL_STATUS_UI.get(status, status)


def _proposal_settings_text(proposal: BrickProposalSnapshot) -> str:
    rules = proposal.rules
    if proposal.value_type == BRICK_TYPE_NUMBER:
        parts: list[str] = []
        if rules.get("integer"):
            parts.append("только целые числа")
        if "min" in rules:
            parts.append(f"минимум {rules['min']}")
        if "max" in rules:
            parts.append(f"максимум {rules['max']}")
        if "suffix" in rules:
            parts.append(f"подпись «{rules['suffix']}»")
        return "; ".join(parts) + "." if parts else "Без ограничений."
    if proposal.value_type == BRICK_TYPE_CHOICE:
        raw_choices = rules.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []
        labels = ", ".join(str(item) for item in choices) or "список пуст"
        multiple = (
            "можно выбрать несколько" if rules.get("multiple") else "один вариант"
        )
        return f"{labels}; {multiple}."
    return "Без дополнительных настроек."


def _proposal_target_label(proposal: BrickProposalSnapshot) -> str:
    if proposal.scope == PROPOSAL_SCOPE_SYSTEM:
        return "вся система"
    return f"место «{proposal.place_name or f'#{proposal.place_id}'}»"


def _proposal_page_note(offset: int, count: int, total: int) -> str:
    if total <= 0:
        return ""
    pages = max((total + PROPOSAL_PAGE_SIZE - 1) // PROPOSAL_PAGE_SIZE, 1)
    page = offset // PROPOSAL_PAGE_SIZE + 1
    return (
        f"Показано {offset + 1}–{offset + count} из {total}. "
        f"Страница {page} из {pages}."
    )


def _proposal_admin_list_content(
    session: Session,
    telegram_user_id: int,
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    phase = state.get("phase")
    offset = _page_value(state, "proposal_offset")
    place_filter = state.get("proposal_place_filter")
    if phase == "proposal_global_list":
        heading = "🌍 Глобальные предложения"
        intro = (
            "Предложения новых кирпичиков для всей системы. Рассматривать "
            "их может только суперадминистратор."
        )
        proposals, total = list_pending_system_proposals(
            session,
            telegram_user_id,
            offset=offset,
            limit=PROPOSAL_PAGE_SIZE,
        )
        if total and offset >= total:
            offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
            proposals, total = list_pending_system_proposals(
                session,
                telegram_user_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
        page_prefix = "adm_bprop_global_page"
        back_label = "◀️ К списку мест"
    else:
        if isinstance(place_filter, int) and not isinstance(place_filter, bool):
            details = get_place_details(session, place_filter)
            if details is None:
                state.clear()
                state.update(_new_state())
                raise ProposalPermissionError("Место не существует или было удалено.")
            heading = "🧱 Предложения кирпичиков"
            intro = (
                f"Ожидающие предложения для места «{_h(details.canonical_name)}». "
                "Другие места здесь не показываются."
            )
            proposals, total = list_pending_for_place(
                session,
                telegram_user_id,
                details.place_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
            if total and offset >= total:
                offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
                proposals, total = list_pending_for_place(
                    session,
                    telegram_user_id,
                    details.place_id,
                    offset=offset,
                    limit=PROPOSAL_PAGE_SIZE,
                )
            back_label = "◀️ К месту"
        else:
            heading = "📍 Предложения по местам"
            intro = (
                "Все ожидающие предложения для существующих мест. "
                "Глобальные системные предложения показаны в отдельном разделе."
            )
            proposals, total = list_pending_place_proposals(
                session,
                telegram_user_id,
                offset=offset,
                limit=PROPOSAL_PAGE_SIZE,
            )
            if total and offset >= total:
                offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
                proposals, total = list_pending_place_proposals(
                    session,
                    telegram_user_id,
                    offset=offset,
                    limit=PROPOSAL_PAGE_SIZE,
                )
            back_label = "◀️ К списку мест"
        page_prefix = "adm_bprop_places_page"
    state["proposal_offset"] = offset
    text = f"{heading} — {total}\n\n{intro}"
    if not total:
        text += (
            "\n\nОжидающих предложений нет. Отозванные и решённые "
            "предложения в очередь не попадают."
        )
    else:
        text += "\n\n" + _proposal_page_note(offset, len(proposals), total)
        for index, proposal in enumerate(proposals, start=offset + 1):
            text += (
                f"\n{index}. <b>{_h(proposal.title)}</b> "
                f"<code>[{_h(proposal.code)}]</code>\n"
                f"   Автор: {_h(_short(proposal.author_label, 100))}\n"
                f"   Область: {_h(_proposal_target_label(proposal))}\n"
                f"   Отправлено: {_h(_date_label(proposal.created_at))}\n"
            )
    rows = [
        [
            InlineKeyboardButton(
                _button_name(
                    f"{index}. ",
                    f"{proposal.title} — [{proposal.code}]",
                ),
                callback_data=f"adm_bprop_open:{proposal.id}",
            )
        ]
        for index, proposal in enumerate(proposals, start=offset + 1)
    ]
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=f"{page_prefix}:{max(offset - PROPOSAL_PAGE_SIZE, 0)}",
            )
        )
    if offset + len(proposals) < total:
        navigation.append(
            InlineKeyboardButton(
                "Вперёд ➡️",
                callback_data=f"{page_prefix}:{offset + PROPOSAL_PAGE_SIZE}",
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(back_label, callback_data="adm_bprop_back")])
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_description_html(value: object) -> str:
    """
    Описание в карточке предложения (R4 №29): моноширинно, «—» при отсутствии.

    Текст задаёт человек, поэтому он экранируется перед обёрткой в `<code>`.
    Старые pending-предложения без описания показывают «—».
    """
    text = " ".join(str(value or "").split())
    if not text:
        return "—"
    return f"<code>{_h(text)}</code>"


def _proposal_admin_card_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    parts = [
        "🧱 <b>Предложение кирпичика</b>",
        "",
        # R4 №29: все семь полей карточки — название, код, тип, описание,
        # автор, цель и статус — в одном месте и в одном порядке.
        f"Название: <b>{_h(proposal.title)}</b>",
        f"Обозначение: <code>[{_h(proposal.code)}]</code>",
        f"Тип: {_h(BRICK_TYPE_TITLES.get(proposal.value_type, proposal.value_type))}",
        f"Описание: {_proposal_description_html(proposal.description)}",
        f"Автор: {_h(proposal.author_label)}",
        f"Цель: {_h(_proposal_target_label(proposal))}",
        f"Статус: {_h(_proposal_status_label(proposal.status))}",
        f"Настройки: {_h(_proposal_settings_text(proposal))}",
        f"Отправлено: {_h(_date_label(proposal.created_at))}",
    ]
    if proposal.decided_at is not None:
        parts.append(f"Решение: {_h(_date_label(proposal.decided_at))}")
    if proposal.status == PROPOSAL_STATUS_APPROVED:
        parts.append(
            f"Создан отдельный кирпичик #{proposal.approved_brick_id}. "
            "Исходный личный кирпичик автора не изменён."
        )
    elif proposal.status == PROPOSAL_STATUS_REJECTED:
        if proposal.decision_note:
            parts.append(f"Причина: {_h(proposal.decision_note)}")
        else:
            parts.append("Предложение отклонено без изменения личного кирпичика.")
    elif proposal.status == PROPOSAL_STATUS_WITHDRAWN:
        parts.append("Предложение отозвано автором. Решение уже невозможно.")
    else:
        parts.extend(
            [
                "",
                "Снимок названия, кода, типа и настроек зафиксирован при "
                "отправке. Правки и архивация личного кирпичика на него не "
                "влияют. Код будет повторно проверен при одобрении.",
            ]
        )
    rows: list[list[InlineKeyboardButton]] = []
    if proposal.status == PROPOSAL_STATUS_PENDING:
        rows.append(
            [
                InlineKeyboardButton(
                    "✅ Одобрить",
                    callback_data=f"adm_bprop_approve:{proposal.id}",
                ),
                InlineKeyboardButton(
                    "❌ Отклонить",
                    callback_data=f"adm_bprop_reject:{proposal.id}",
                ),
            ]
        )
    rows.append([InlineKeyboardButton("◀️ К очереди", callback_data="adm_bprop_back")])
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def _proposal_approve_confirm_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "✅ <b>Одобрить предложение кирпичика?</b>\n\n"
        f"Название: {_h(proposal.title)}\n"
        f"Обозначение: <code>[{_h(proposal.code)}]</code>\n"
        f"Область: {_h(_proposal_target_label(proposal))}\n"
        f"Автор: {_h(proposal.author_label)}\n\n"
        "Будет создан отдельный кирпичик в целевой области. Исходный личный "
        "кирпичик, его владелец и существующие шаблоны не изменятся. При "
        "подтверждении права, статус, область, тип, правила и занятость кода "
        "проверяются ещё раз в одной транзакции. При конфликте код не будет "
        "переименован молча."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да, одобрить",
                    callback_data=f"adm_bprop_approve_yes:{proposal.id}",
                )
            ],
            [InlineKeyboardButton("◀️ Нет, назад", callback_data="adm_bprop_back")],
        ]
    )


def _proposal_reject_confirm_content(
    proposal: BrickProposalSnapshot,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "❌ <b>Отклонить предложение кирпичика?</b>\n\n"
        f"Название: {_h(proposal.title)}\n"
        f"Обозначение: <code>[{_h(proposal.code)}]</code>\n"
        f"Область: {_h(_proposal_target_label(proposal))}\n"
        f"Автор: {_h(proposal.author_label)}\n\n"
        "Предложение получит окончательный статус «отклонено». Личный "
        "кирпичик автора и шаблоны не изменятся. Автор сможет позже "
        "отправить новое предложение."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Да, отклонить",
                    callback_data=f"adm_bprop_reject_yes:{proposal.id}",
                )
            ],
            [InlineKeyboardButton("◀️ Нет, назад", callback_data="adm_bprop_back")],
        ]
    )


# --- Конфликты кодов: задачи разрешения (Poster 2.1 R3) --------------------


def _conflict_short_label(view: BrickConflictView) -> str:
    place = view.place_name or f"место #{view.place_id}"
    return f"[{view.code}] — {place}"


def _conflicts_list_content(
    session: Session,
    telegram_user_id: int,
    view: BrickConflictView | None,
    place_id: int,
) -> tuple[str, InlineKeyboardMarkup]:
    details = get_place_details(session, place_id)
    place_label = details.canonical_name if details is not None else f"#{place_id}"
    conflicts = list_place_conflicts(session, telegram_user_id, place_id)
    text = (
        "⚔️ Конфликты кодов\n\n"
        f"📍 {_h(place_label)}\n\n"
        "Глобальный кирпичик уже действует для новых привязок. Пока задача "
        "активна, местный кирпичик того же кода остаётся как есть: выберите "
        "«переименовать местный» или «переключиться на глобальный». "
        "Существующие снимки анонсов не изменяются."
    )
    if not conflicts:
        text += "\n\n✅ Активных задач нет."
    else:
        text += f"\n\nАктивных задач: {len(conflicts)}."
        for index, item in enumerate(conflicts, start=1):
            text += (
                f"\n{index}. <b>{_h(item.local_brick_title)}</b> "
                f"<code>[{_h(item.code)}]</code>"
            )
    rows: list[list[InlineKeyboardButton]] = []
    for item in conflicts:
        rows.append(
            [
                InlineKeyboardButton(
                    _button_name(f"⚔️ {item.code}: ", item.local_brick_title),
                    callback_data=f"adm_bconflict:{item.id}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("◀️ К месту", callback_data=f"adm_place:{place_id}")]
    )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _conflict_card_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    place = view.place_name or f"место #{view.place_id}"
    text = (
        "⚔️ <b>Задача разрешения конфликта</b>\n\n"
        f"Код: <code>[{_h(view.code)}]</code>\n"
        f"Место: {_h(place)}\n"
        f"Местный кирпичик: {_h(view.local_brick_title)} "
        f"(#{view.local_brick_id})\n"
        f"Глобальный кирпичик: #{view.system_brick_id}\n\n"
        "Глобальный кирпичик уже используется для НОВЫХ привязок этого кода. "
        "Местный кирпичик не удалён и не переименован: данные не потеряны.\n\n"
        "Выберите исход: переименовать местный кирпичик (привязки и снимки "
        "сохранятся) или переключить шаблон места на глобальный кирпичик "
        "(будет создана новая версия шаблона, старые версии и снимки "
        "останутся неизменными)."
    )
    rows: list[list[InlineKeyboardButton]] = []
    if view.is_open:
        rows.append(
            [
                InlineKeyboardButton(
                    "✏️ Переименовать местный",
                    callback_data=f"adm_bconflict_rename:{view.id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "🌍 Переключиться на глобальный",
                    callback_data=f"adm_bconflict_switch:{view.id}",
                )
            ]
        )
    else:
        text += "\n\n✅ Задача решена; история сохранена."
    rows.append(
        [
            InlineKeyboardButton(
                "◀️ К конфликтам",
                callback_data=f"adm_bconflicts:{view.place_id}",
            )
        ]
    )
    rows.append([InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")])
    return text, InlineKeyboardMarkup(rows)


def _conflict_rename_input_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "✏️ <b>Переименование местного кирпичика</b>\n\n"
        f"Конфликтный код: <code>[{_h(view.code)}]</code>\n"
        f"Кирпичик: {_h(view.local_brick_title)}\n\n"
        "Введите новое обозначение для местного кирпичика. Привязки шаблонов "
        "указывают на кирпичик по ID и продолжат работать; изменится только "
        "технический код в тексте шаблона."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "◀️ Назад",
                    callback_data=f"adm_bconflict:{view.id}",
                )
            ]
        ]
    )


def _conflict_switch_confirm_content(
    view: BrickConflictView,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🌍 <b>Переключиться на глобальный кирпичик?</b>\n\n"
        f"Код: <code>[{_h(view.code)}]</code>\n"
        f"Место: {_h(view.place_name or f'#{view.place_id}')}\n\n"
        "Стандартный шаблон места получит НОВУЮ версию с привязкой этого кода "
        "к глобальному кирпичику. Местный кирпичик останется в разделе "
        "кирпичиков места и не будет удалён. Прежние версии шаблона и "
        "сохранённые снимки анонсов не изменятся."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да, переключить",
                    callback_data=f"adm_bconflict_switch_yes:{view.id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ Нет, назад",
                    callback_data=f"adm_bconflict:{view.id}",
                )
            ],
        ]
    )


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


# R1, дефект №21: исход поиска получателя всегда явный. Вид запроса хранится
# в состоянии, чтобы «не найдено» объясняло, что именно не нашлось.
ASSIGN_QUERY_INVALID_TEXT = (
    "Неверный формат запроса. Введите @username, имя или точный числовой "
    "Telegram user ID — пустой ввод и одни символы не ищутся."
)

ASSIGN_QUERY_USERNAME = "username"
ASSIGN_QUERY_ID = "id"
ASSIGN_QUERY_NAME = "name"


def _normalize_assign_query(raw: str) -> tuple[str, str] | None:
    """
    Разбирает ввод в фазе `assign_search`.

    Возвращает (вид, значение) или None для пустого/несимвольного ввода —
    вызывающий показывает явную ошибку формата, а не молчит.
    """
    cleaned = " ".join(str(raw).split())
    if not cleaned:
        return None
    if cleaned.startswith("@"):
        username = cleaned[1:].strip()
        # Один «@» без имени — это не поиск, а ошибка формата.
        return (ASSIGN_QUERY_USERNAME, username) if username else None
    if cleaned.isascii() and cleaned.isdecimal():
        try:
            telegram_user_id = int(cleaned)
        except ValueError:  # pragma: no cover — isdecimal уже отсеял
            return None
        return (
            (ASSIGN_QUERY_ID, str(telegram_user_id)) if telegram_user_id > 0 else None
        )
    if not any(character.isalnum() for character in cleaned):
        return None
    return (ASSIGN_QUERY_NAME, cleaned)


def _assign_not_found_text(query: str, kind: str) -> str:
    """Явное «не найдено» с конкретным следующим шагом (R1, дефект №21)."""
    if kind == ASSIGN_QUERY_ID:
        return (
            f"Пользователь не найден: Telegram user ID "
            f"{_h(_short(query, 40))} не известен боту. Получатель должен "
            "открыть Poster и нажать /start, после чего повторите поиск."
        )
    if kind == ASSIGN_QUERY_USERNAME:
        return (
            f"Пользователь не найден: @{_h(_short(query, 100))} не известен "
            "боту. Получатель должен открыть Poster и нажать /start, после "
            "чего повторите поиск."
        )
    return (
        f"Пользователь не найден по запросу «{_h(_short(query, 100))}». Если "
        "это получатель, он должен открыть Poster и нажать /start, после чего "
        "повторите поиск по @username или точному Telegram user ID."
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
        # R1, дефект №21: у «не найдено» должен быть ЯВНЫЙ следующий шаг —
        # человек может быть просто не знаком боту (не нажимал /start).
        parsed = _normalize_assign_query(query)
        kind = str(state.get("query_kind") or (parsed[0] if parsed else ""))
        text += _assign_not_found_text(query, kind)
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


ALIAS_PAGE_SIZE = 6


def _edit_menu_content(
    session: Session, details: PlaceDetails
) -> tuple[str, InlineKeyboardMarkup]:
    """Меню редактирования данных места (Stage 20B)."""
    aliases = list_place_aliases(session, details.place_id)
    text = (
        "✏️ Редактирование места\n\n"
        f"📍 Название: {_h(details.canonical_name)}\n"
        f"Альтернативные названия: {len(aliases)}\n"
        f"Адрес: {_long_value(details.address) if details.address else 'Не указан'}\n"
        f"Ссылка: {_safe_link(details.url) if details.url else 'Не указана'}\n\n"
        "Выберите, что изменить:"
    )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                "🏷 Изменить название",
                callback_data=f"adm_edit_name:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "📝 Альтернативные названия",
                callback_data=f"adm_aliases:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Изменить адрес",
                callback_data=f"adm_edit_address:{details.place_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Изменить ссылку",
                callback_data=f"adm_edit_url:{details.place_id}",
            )
        ],
    ]
    if details.address:
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Удалить адрес",
                    callback_data=f"adm_edit_address_delete:{details.place_id}",
                )
            ]
        )
    if details.url:
        rows.append(
            [
                InlineKeyboardButton(
                    "🗑 Удалить ссылку",
                    callback_data=f"adm_edit_url_delete:{details.place_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_cancel")])
    return text, InlineKeyboardMarkup(rows)


def _edit_input_content(
    details: PlaceDetails, phase: str, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Экран ввода нового значения для редактируемого поля."""
    if phase == "edit_name_input":
        text = (
            "🏷 Изменение названия\n\n"
            f"📍 Текущее название: {_h(details.canonical_name)}\n\n"
            "Введите новое каноническое название места. Старое название "
            "сохранится как альтернативное для поиска.\n\n"
            "Название не должно совпадать с другими названиями этого места и "
            "не должно быть слишком похожим на другие места — проверим на "
            "следующем шаге."
        )
    elif phase == "edit_alias_add_input":
        text = (
            "📝 Добавление альтернативного названия\n\n"
            f"📍 {_h(details.canonical_name)}\n\n"
            "Введите альтернативное название. Оно будет доступно для поиска "
            "вместе с каноническим названием."
        )
    elif phase == "edit_alias_rename_input":
        target_name = _h(str(state.get("edit_target_name") or ""))
        text = (
            "📝 Переименование альтернативного названия\n\n"
            f"📍 {_h(details.canonical_name)}\n\n"
            f"Текущее альтернативное название: {target_name}\n\n"
            "Введите новое название для этого варианта."
        )
    elif phase == "edit_address_input":
        current = _long_value(details.address) if details.address else "Не указан"
        text = (
            "🏠 Изменение адреса\n\n"
            f"📍 {_h(details.canonical_name)}\n"
            f"Текущий адрес: {current}\n\n"
            "Введите адрес места обычным текстом. Длина не ограничена "
            "искусственно: адрес сохраняется полностью."
        )
    elif phase == "edit_url_input":
        current = details.url if details.url else "Не указана"
        text = (
            "🔗 Изменение ссылки\n\n"
            f"📍 {_h(details.canonical_name)}\n"
            f"Текущая ссылка: {_safe_link(current)}\n\n"
            "Введите ссылку на страницу места (например, example.com или "
            "https://t.me/poster)."
        )
    else:
        text = "✏️ Редактирование места\n\nВведите новое значение."
    return text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Отмена", callback_data="adm_cancel")]]
    )


def _edit_confirm_content(
    session: Session, details: PlaceDetails, phase: str, state: dict
) -> tuple[str, InlineKeyboardMarkup] | None:
    """Экран подтверждения изменения данных места."""
    warning = str(state.get("edit_warning") or "")
    warning_block = f"⚠️ {warning}\n\n" if warning else ""

    if phase == "edit_address_delete_confirm":
        if not details.address:
            return None
        text = (
            "🗑 Подтвердите удаление адреса\n\n"
            f"📍 {_h(details.canonical_name)}\n\n"
            f"Текущий адрес: {_long_value(details.address)}\n\n"
            "Адрес будет удалён из карточки места. Само место сохранится."
        )
    elif phase == "edit_url_delete_confirm":
        if not details.url:
            return None
        text = (
            "🗑 Подтвердите удаление ссылки\n\n"
            f"📍 {_h(details.canonical_name)}\n\n"
            f"Текущая ссылка: {_safe_link(details.url)}\n\n"
            "Ссылка будет удалена из карточки места. Само место сохранится."
        )
    elif phase == "edit_alias_delete_confirm":
        target_name = _h(str(state.get("edit_target_name") or ""))
        text = (
            f"{warning_block}"
            "🗑 Подтвердите удаление альтернативного названия\n\n"
            f"📍 {_h(details.canonical_name)}\n\n"
            f"Будет удалено: {target_name}\n\n"
            "Каноническое название и другие альтернативные названия останутся "
            "нетронутыми."
        )
    else:
        value = state.get("edit_value")
        if not isinstance(value, str) or not value:
            return None

        if phase == "edit_name_confirm":
            if value == details.canonical_name:
                return None
            text = (
                f"{warning_block}"
                "✏️ Подтвердите изменение названия\n\n"
                f"📍 {_h(details.canonical_name)}\n\n"
                f"Новое название: {_h(value)}\n\n"
                "Старое название сохранится как альтернативное для поиска. "
                "Изменение подтверждает администратор места или суперадминистратор."
            )
        elif phase == "edit_alias_add_confirm":
            text = (
                f"{warning_block}"
                "✏️ Подтвердите добавление альтернативного названия\n\n"
                f"📍 {_h(details.canonical_name)}\n\n"
                f"Новое название: {_h(value)}\n\n"
                "Название будет доступно для поиска вместе с каноническим."
            )
        elif phase == "edit_alias_rename_confirm":
            target_name = _h(str(state.get("edit_target_name") or ""))
            text = (
                f"{warning_block}"
                "✏️ Подтвердите переименование альтернативного названия\n\n"
                f"📍 {_h(details.canonical_name)}\n\n"
                f"Текущее: {target_name}\n"
                f"Новое: {_h(value)}\n\n"
                "Название будет доступно для поиска вместе с каноническим."
            )
        elif phase == "edit_address_confirm":
            current = _long_value(details.address) if details.address else "Не указан"
            text = (
                f"{warning_block}"
                "🏠 Подтвердите изменение адреса\n\n"
                f"📍 {_h(details.canonical_name)}\n\n"
                f"Текущий адрес: {current}\n"
                f"Новый адрес: {_long_value(value)}\n\n"
                "Адрес будет виден в карточке места полностью; длинное "
                "значение на экране помечается как фрагмент."
            )
        elif phase == "edit_url_confirm":
            current = details.url if details.url else "Не указана"
            text = (
                f"{warning_block}"
                "🔗 Подтвердите изменение ссылки\n\n"
                f"📍 {_h(details.canonical_name)}\n\n"
                f"Текущая ссылка: {_safe_link(current)}\n"
                f"Новая ссылка: {_safe_link(value)}\n\n"
                "Ссылка будет видна в карточке места."
            )
        else:
            return None
    return text, InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Подтвердить", callback_data="adm_edit_confirm")],
            [InlineKeyboardButton("❌ Отмена", callback_data="adm_cancel")],
        ]
    )


def _edit_aliases_content(
    session: Session, details: PlaceDetails, state: dict
) -> tuple[str, InlineKeyboardMarkup]:
    """Список альтернативных названий с пагинацией и действиями."""
    aliases = list_place_aliases(session, details.place_id)
    offset = max(_page_value(state, "alias_page"), 0)
    offset = min(offset, max(len(aliases) - 1, 0) // ALIAS_PAGE_SIZE * ALIAS_PAGE_SIZE)
    page_items = aliases[offset : offset + ALIAS_PAGE_SIZE]

    total = len(aliases)
    if total == 0:
        text = (
            f"📝 Альтернативные названия\n\n📍 {_h(details.canonical_name)}\n\n"
            "Пока нет альтернативных названий. Добавьте первый вариант, чтобы "
            "место можно было найти по нему."
        )
    else:
        text = (
            f"📝 Альтернативные названия\n\n📍 {_h(details.canonical_name)}\n"
            f"Найдено: {total}\n\n"
        )
        text += "\n".join(
            f"{index}. {_h(_short(alias.name, 120))}"
            for index, alias in enumerate(page_items, start=offset + 1)
        )

    keyboard: list[list[InlineKeyboardButton]] = []
    for alias in page_items:
        keyboard.append(
            [
                InlineKeyboardButton(
                    _button_name("✏️", alias.name),
                    callback_data=f"adm_alias_rename:{alias.name_id}",
                ),
                InlineKeyboardButton(
                    "🗑 Удалить",
                    callback_data=f"adm_alias_delete:{alias.name_id}",
                ),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "⬅️ Назад", callback_data=f"adm_alias_page:{offset - ALIAS_PAGE_SIZE}"
            )
        )
    if offset + ALIAS_PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton(
                "Далее ➡️",
                callback_data=f"adm_alias_page:{offset + ALIAS_PAGE_SIZE}",
            )
        )
    if nav:
        keyboard.append(nav)
    keyboard.append(
        [
            InlineKeyboardButton(
                "➕ Добавить название",
                callback_data=f"adm_alias_add:{details.place_id}",
            )
        ]
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
    invite_url: str | None = None,
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
                text = _place_text(
                    details,
                    state,
                    session,
                    viewer_telegram_user_id=user_id,
                )
                keyboard = _place_keyboard(
                    details,
                    session,
                    access_kind=access_kind,
                    viewer_telegram_user_id=user_id,
                )
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
        elif phase == "edit_menu":
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
                text, keyboard = _edit_menu_content(session, details)
        elif phase in {
            "edit_name_input",
            "edit_alias_add_input",
            "edit_alias_rename_input",
            "edit_address_input",
            "edit_url_input",
        }:
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
                text, keyboard = _edit_input_content(details, phase, state)
        elif phase in {
            "edit_name_confirm",
            "edit_alias_add_confirm",
            "edit_alias_rename_confirm",
            "edit_alias_delete_confirm",
            "edit_address_confirm",
            "edit_address_delete_confirm",
            "edit_url_confirm",
            "edit_url_delete_confirm",
        }:
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
                content = _edit_confirm_content(session, details, phase, state)
                if content is None:
                    if phase.startswith("edit_alias"):
                        state["phase"] = "edit_aliases"
                        text, keyboard = _edit_aliases_content(session, details, state)
                    else:
                        state["phase"] = "edit_menu"
                        text, keyboard = _edit_menu_content(session, details)
                    notice = notice or (
                        "Ситуация изменилась: обновите данные и повторите действие."
                    )
                else:
                    text, keyboard = content
        elif phase == "edit_aliases":
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
                text, keyboard = _edit_aliases_content(session, details, state)
        elif phase in {
            "transfer_username_input",
            "transfer_candidates",
            "transfer_confirm_known",
            "transfer_confirm_invite",
            "transfer_outgoing",
        }:
            if details is None:
                state.clear()
                state.update(_new_state())
                notice = notice or "Место не существует или было удалено."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif not is_place_admin(session, user_id, details.place_id):
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    "Передать админку может только действующий администратор "
                    "этого места."
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                active = get_active_place_admin(session, details.place_id)
                active_user = active.user if active is not None else None
                if phase == "transfer_username_input":
                    text, keyboard = _transfer_username_content(details, active_user)
                elif phase == "transfer_candidates":
                    raw_ids = state.get("transfer_candidate_ids")
                    ids = (
                        [
                            int(value)
                            for value in raw_ids
                            if isinstance(value, int) and not isinstance(value, bool)
                        ]
                        if isinstance(raw_ids, list)
                        else []
                    )
                    users = (
                        {
                            user.telegram_user_id: user
                            for user in session.scalars(
                                select(User).where(User.telegram_user_id.in_(ids))
                            ).all()
                        }
                        if ids
                        else {}
                    )
                    candidates = tuple(
                        TransferUser(
                            user_id=users[tg].id,
                            telegram_user_id=users[tg].telegram_user_id,
                            username=users[tg].username,
                            full_name=users[tg].full_name,
                        )
                        for tg in ids
                        if tg in users
                    )
                    if not candidates:
                        state["phase"] = "transfer_username_input"
                        notice = notice or "Выберите получателя заново."
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = _transfer_candidates_content(
                            details, candidates, state
                        )
                elif phase == "transfer_confirm_known":
                    content = _transfer_known_confirm_content(session, details, state)
                    if content is None:
                        state["phase"] = "transfer_username_input"
                        notice = notice or "Получатель больше не известен боту."
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = content
                elif phase == "transfer_confirm_invite":
                    if not state.get("transfer_username"):
                        state["phase"] = "transfer_username_input"
                        notice = notice or "Введите username получателя заново."
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = _transfer_invite_confirm_content(
                            details, state
                        )
                else:
                    content = _transfer_outgoing_content(
                        session, details, state, invite_url=invite_url
                    )
                    if content is None:
                        state["phase"] = "transfer_username_input"
                        notice = notice or (
                            "Активной передачи нет. Введите username, чтобы "
                            "создать новое предложение."
                        )
                        text, keyboard = _transfer_username_content(
                            details, active_user
                        )
                    else:
                        text, keyboard = content
        elif phase in {"proposal_global_list", "proposal_places_list"}:
            place_filter = state.get("proposal_place_filter")
            if (
                phase == "proposal_places_list"
                and not isinstance(place_filter, int)
                and access_kind != "superadmin"
            ):
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    "Сводная очередь предложений мест доступна только "
                    "суперадминистратору."
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            else:
                try:
                    text, keyboard = _proposal_admin_list_content(
                        session, user_id, state
                    )
                except ProposalPermissionError as exc:
                    state.clear()
                    state.update(_new_state())
                    notice = notice or str(exc)
                    text, keyboard = _places_content(
                        session, state, access_kind, user_id
                    )
        elif phase in {
            "conflicts_list",
            "conflict_card",
            "conflict_rename_input",
            "conflict_switch_confirm",
        }:
            place_for_conflicts = state.get("place_id")
            conflict_id = state.get("conflict_id")
            view = (
                get_conflict(session, user_id, conflict_id)
                if isinstance(conflict_id, int) and not isinstance(conflict_id, bool)
                else None
            )
            if not isinstance(place_for_conflicts, int) or not can_manage_place(
                session, user_id, place_for_conflicts
            ):
                state.clear()
                state.update(_new_state())
                notice = notice or "Доступ к этому месту не подтверждён."
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif phase == "conflicts_list":
                text, keyboard = _conflicts_list_content(
                    session, user_id, view, place_for_conflicts
                )
            elif view is None:
                state["phase"] = "conflicts_list"
                notice = notice or "Задача конфликта недоступна."
                text, keyboard = _conflicts_list_content(
                    session, user_id, None, place_for_conflicts
                )
            elif phase == "conflict_card":
                text, keyboard = _conflict_card_content(view)
            elif phase == "conflict_rename_input":
                if not view.is_open:
                    state["phase"] = "conflict_card"
                    notice = notice or "Задача уже решена."
                    text, keyboard = _conflict_card_content(view)
                else:
                    text, keyboard = _conflict_rename_input_content(view)
            else:
                if not view.is_open:
                    state["phase"] = "conflict_card"
                    notice = notice or "Задача уже решена."
                    text, keyboard = _conflict_card_content(view)
                else:
                    text, keyboard = _conflict_switch_confirm_content(view)
        elif phase in {
            "proposal_card",
            "proposal_approve_confirm",
            "proposal_reject_confirm",
        }:
            try:
                proposal_id = int(state.get("proposal_id") or 0)
            except (TypeError, ValueError):
                proposal_id = 0
            proposal = (
                get_admin_proposal(session, user_id, proposal_id)
                if proposal_id > 0
                else None
            )
            if proposal is None:
                state.clear()
                state.update(_new_state())
                notice = notice or (
                    "Предложение недоступно: оно не найдено или полномочия "
                    "на это место больше не действуют."
                )
                text, keyboard = _places_content(session, state, access_kind, user_id)
            elif phase == "proposal_card":
                text, keyboard = _proposal_admin_card_content(proposal)
            elif proposal.status != PROPOSAL_STATUS_PENDING:
                state["phase"] = "proposal_card"
                notice = notice or "Предложение уже не ожидает решения."
                text, keyboard = _proposal_admin_card_content(proposal)
            elif phase == "proposal_approve_confirm":
                text, keyboard = _proposal_approve_confirm_content(proposal)
            else:
                text, keyboard = _proposal_reject_confirm_content(proposal)
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
                    text = _place_text(
                        details,
                        state,
                        session,
                        viewer_telegram_user_id=user_id,
                    )
                    keyboard = _place_keyboard(
                        details,
                        session,
                        access_kind=access_kind,
                        viewer_telegram_user_id=user_id,
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
    # R1, дефект №23: кабинет — вход в актуальный UI; снимаем reply-клавиатуру
    # старых версий, если она «прилипла» у клиента.
    await screen.drop_legacy_reply_keyboard(update, context)
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
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = load_admin_session(user_id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_state()
    await _render_phase(update, context, state, drop_input=True)


async def open_place_admin_editor(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place_id: int
) -> None:
    """
    Переход из карточки справочника в меню редактирования места кабинета.

    Вход в существующее меню правки (`adm_edit_name`/`adm_aliases`/
    `adm_edit_address`/`adm_edit_url`), а не второй редактор. Роль и место
    перепроверяются по БД: чужой id не открывает административный экран
    (IDOR), даже если callback подделан.
    """
    query = ctx.query(update)
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        allowed = can_access_admin_panel(session, user_id) and can_manage_place(
            session, user_id, place_id
        )
        exists = get_place_details(session, place_id) is not None
    except Exception as e:  # noqa: BLE001 — отказ безопаснее открытия
        logger.error("Не удалось проверить доступ к редактированию места: %s", e)
        allowed = False
        exists = False
    finally:
        session.close()
    if not exists:
        await query.answer("Место не существует или удалено.", show_alert=True)
        return
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    clear_place_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = _new_state()
    state["phase"] = "edit_menu"
    state["place_id"] = place_id
    save_admin_session(user_id, state)
    await _render_phase(update, context, state, drop_input=True)


async def open_place_admin_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, place_id: int
) -> None:
    """
    Карточка места кабинета из справочника: назначение/замена администратора.

    Доступна только суперадминистратору (проверяется заново здесь и в
    `_render_phase`); обычный админ места получает отказ.
    """
    query = ctx.query(update)
    user_id = ctx.user(update).id
    if not is_superadmin(user_id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    session = SessionLocal()
    try:
        exists = get_place_details(session, place_id) is not None
    finally:
        session.close()
    if not exists:
        await query.answer("Место не существует или удалено.", show_alert=True)
        return
    await query.answer()
    clear_place_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    state = _new_state()
    state["phase"] = "place"
    state["place_id"] = place_id
    save_admin_session(user_id, state)
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
    state.pop("query_kind", None)
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
        "query_kind",
    ):
        state.pop(key, None)
    await _render_phase(update, context, state, notice=_mutation_notice(result))


# --- Редактирование данных места (Stage 20B) ----------------------------------


EDIT_STATE_KEYS = (
    "edit_value",
    "edit_warning",
    "edit_expected",
    "edit_target_name_id",
    "edit_target_name",
)

EDIT_INPUT_PHASES = frozenset(
    {
        "edit_name_input",
        "edit_alias_add_input",
        "edit_alias_rename_input",
        "edit_address_input",
        "edit_url_input",
    }
)

EDIT_CANCEL_TO_MENU_PHASES = frozenset(
    {
        "edit_name_input",
        "edit_name_confirm",
        "edit_address_input",
        "edit_address_confirm",
        "edit_address_delete_confirm",
        "edit_url_input",
        "edit_url_confirm",
        "edit_url_delete_confirm",
    }
)

EDIT_CANCEL_TO_ALIASES_PHASES = frozenset(
    {
        "edit_alias_add_input",
        "edit_alias_add_confirm",
        "edit_alias_rename_input",
        "edit_alias_rename_confirm",
        "edit_alias_delete_confirm",
    }
)


# --- Передача администратора места (Stage 21) --------------------------------

TRANSFER_STATE_KEYS = (
    "transfer_username",
    "transfer_candidate_ids",
    "transfer_target_telegram_user_id",
)

TRANSFER_INPUT_PHASES = frozenset({"transfer_username_input"})

TRANSFER_CANCEL_PHASES = frozenset(
    {
        "transfer_username_input",
        "transfer_candidates",
        "transfer_confirm_known",
        "transfer_confirm_invite",
        "transfer_outgoing",
    }
)


def _clear_transfer_keys(state: dict) -> None:
    """Убирает подсказки формы передачи; на права и предложение не влияет."""
    for key in TRANSFER_STATE_KEYS:
        state.pop(key, None)


def _clear_edit_keys(state: dict) -> None:
    """Сбрасывает промежуточные значения редактирования (не роль и не место)."""
    for key in EDIT_STATE_KEYS:
        state.pop(key, None)


def _find_alias(session: Session, place_id: int, name_id: int) -> AliasRow | None:
    """Актуальная строка альтернативного названия места по id."""
    for alias in list_place_aliases(session, place_id):
        if alias.name_id == name_id:
            return alias
    return None


async def _edit_transition(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    allowed_phase: str,
) -> tuple[CallbackQuery, dict] | None:
    """
    Общий префикс обработчиков редактирования: фаза + свежая проверка прав.

    Роль проверяется по effective_user: суперадминистратор или действующий
    администратор ИМЕННО этого места. Callback не передаёт роль.
    """
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != allowed_phase:
        await _stale(update)
        return None
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        allowed = isinstance(place_id, int) and can_manage_place(
            session, ctx.user(update).id, place_id
        )
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return None
    return query, state


async def handle_edit_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «✏️ Редактировать место» в карточке места."""
    result = await _edit_transition(update, context, allowed_phase="place")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_menu"
    await _render_phase(update, context, state)


async def handle_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_name_input"
    await _render_phase(update, context, state)


async def handle_edit_address(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_address_input"
    await _render_phase(update, context, state)


async def handle_edit_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_url_input"
    await _render_phase(update, context, state)


async def handle_edit_address_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if details is None or details.address is None:
        # Адрес уже удалён или отсутствует: предлагаем обновить меню.
        await query.answer(
            "Адрес уже отсутствует: обновите меню редактирования.",
            show_alert=True,
        )
        return
    # Фиксируем текущий адрес: подтверждение сработает только если
    # другой администратор не изменил его в процессе.
    state["edit_expected"] = details.address
    state["phase"] = "edit_address_delete_confirm"
    await _render_phase(update, context, state)


async def handle_edit_url_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        details = (
            get_place_details(session, int(place_id))
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if details is None or details.url is None:
        # Ссылка уже удалена или отсутствует: предлагаем обновить меню.
        await query.answer(
            "Ссылка уже отсутствует: обновите меню редактирования.",
            show_alert=True,
        )
        return
    # Фиксируем текущую ссылку: подтверждение сработает только если
    # другой администратор не изменил её в процессе.
    state["edit_expected"] = details.url
    state["phase"] = "edit_url_delete_confirm"
    await _render_phase(update, context, state)


async def handle_aliases(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Кнопка «📝 Альтернативные названия» в меню редактирования."""
    result = await _edit_transition(update, context, allowed_phase="edit_menu")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["alias_page"] = 0
    state["phase"] = "edit_aliases"
    await _render_phase(update, context, state)


async def handle_alias_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    query, state = result
    await query.answer()
    _clear_edit_keys(state)
    state["phase"] = "edit_alias_add_input"
    await _render_phase(update, context, state)


async def handle_alias_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    state = result[1]
    data = query.data or ""
    try:
        name_id = int(data[len("adm_alias_rename:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        alias = (
            _find_alias(session, int(place_id), name_id)
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if alias is None:
        await query.answer("Название уже изменено или удалено.", show_alert=True)
        return
    await query.answer()
    _clear_edit_keys(state)
    state["edit_target_name_id"] = alias.name_id
    state["edit_target_name"] = alias.name
    state["phase"] = "edit_alias_rename_input"
    await _render_phase(update, context, state)


async def handle_alias_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    result = await _edit_transition(update, context, allowed_phase="edit_aliases")
    if result is None:
        return
    state = result[1]
    data = query.data or ""
    try:
        name_id = int(data[len("adm_alias_delete:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        alias = (
            _find_alias(session, int(place_id), name_id)
            if isinstance(place_id, int)
            else None
        )
    finally:
        session.close()
    if alias is None:
        await query.answer("Название уже изменено или удалено.", show_alert=True)
        return
    await query.answer()
    _clear_edit_keys(state)
    state["edit_target_name_id"] = alias.name_id
    state["edit_target_name"] = alias.name
    state["phase"] = "edit_alias_delete_confirm"
    await _render_phase(update, context, state)


async def handle_alias_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "edit_aliases":
        await _stale(update)
        return
    data = query.data or ""
    try:
        offset = int(data[len("adm_alias_page:") :])
    except (TypeError, ValueError):
        await _stale(update)
        return
    await query.answer()
    state["alias_page"] = max(offset, 0)
    await _render_phase(update, context, state)


def _validate_edit_input(
    session: Session,
    details: PlaceDetails,
    phase: str,
    raw: str,
    *,
    rename_name_id: int | None = None,
) -> tuple[str, str, str]:
    """
    Валидирует текстовый ввод фаз редактирования места.

    Возвращает (next_phase, value, warning). Ошибки PlaceNameError и
    PlaceEditValueError несут понятное сообщение. Похожие названия других
    мест возвращаются предупреждением — на экране подтверждения.
    """
    if phase == "edit_address_input":
        cleaned = clean_address_text(raw)
        if cleaned == details.address:
            raise PlaceEditValueError(
                "Адрес уже такой. Если адрес нужно удалить, используйте кнопку "
                "«Удалить адрес» в меню редактирования."
            )
        return "edit_address_confirm", cleaned, ""
    if phase == "edit_url_input":
        normalized = normalize_place_url(raw)
        if normalized == details.url:
            raise PlaceEditValueError(
                "Ссылка уже такая. Если ссылку нужно удалить, используйте "
                "кнопку «Удалить ссылку» в меню редактирования."
            )
        return "edit_url_confirm", normalized, ""

    what = {
        "edit_name_input": "Название места",
        "edit_alias_add_input": "Альтернативное название",
        "edit_alias_rename_input": "Альтернативное название",
    }[phase]
    display = validate_place_name(raw, what=what)
    needle = normalize_place_name(display)

    aliases = list_place_aliases(session, details.place_id)
    if phase == "edit_name_input":
        if needle == normalize_place_name(details.canonical_name):
            raise PlaceEditValueError(
                "Такое название уже является каноническим у этого места. "
                "Введите другое название."
            )
        existing_names = [alias.name for alias in aliases]
    elif phase == "edit_alias_add_input":
        existing_names = [details.canonical_name] + [alias.name for alias in aliases]
    else:
        target = None
        for alias in aliases:
            if alias.name_id == rename_name_id:
                target = alias
                break
        if target is None:
            raise PlaceEditValueError(
                "Название уже изменено или удалено другим администратором. "
                "Вернитесь к списку названий."
            )
        existing_names = [details.canonical_name] + [
            alias.name for alias in aliases if alias.name_id != rename_name_id
        ]
    for name in existing_names:
        if normalize_place_name(name) == needle:
            raise PlaceEditValueError(
                f"Такое название уже есть у этого места: «{name}». Введите другое."
            )

    conflicts = find_name_conflicts(session, display, exclude_place_id=details.place_id)
    warning = ""
    if conflicts:
        other = conflicts[0]
        warning = (
            f"Похожее название у другого места: «{other.matched_name}» "
            f"в месте «{other.canonical_name}»."
        )
        if len(conflicts) > 1:
            warning += f" Похожих мест: {len(conflicts)}."

    next_phase = {
        "edit_name_input": "edit_name_confirm",
        "edit_alias_add_input": "edit_alias_add_confirm",
        "edit_alias_rename_input": "edit_alias_rename_confirm",
    }[phase]
    return next_phase, display, warning


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст в фазах редактирования места: валидация и переход к подтверждению."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in EDIT_INPUT_PHASES:
        return
    phase = str(state.get("phase"))
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        if not isinstance(place_id, int) or not can_manage_place(
            session, user.id, place_id
        ):
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await _render_phase(
                update,
                context,
                state,
                notice="Место не существует или было удалено.",
                drop_input=True,
            )
            return
        raw = ctx.message(update).text or ""
        try:
            next_phase, value, warning = _validate_edit_input(
                session,
                details,
                phase,
                raw,
                rename_name_id=(
                    state.get("edit_target_name_id")
                    if isinstance(state.get("edit_target_name_id"), int)
                    else None
                ),
            )
        except (PlaceNameError, PlaceEditValueError) as e:
            cleanup = []
            message = getattr(update, "message", None)
            if message is not None and message.message_id is not None:
                cleanup.append(int(message.message_id))
            await _render_phase(
                update,
                context,
                state,
                notice=str(e),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
    finally:
        session.close()

    state["edit_value"] = value
    if warning:
        state["edit_warning"] = warning
    else:
        state.pop("edit_warning", None)

    # Фиксируем старое значение поля на момент ввода: подтверждение пройдёт
    # только если другой администратор не изменил поле в процессе (гонка
    # обнаруживается хранилищем по ожидаемому значению).
    expected_map = {
        "edit_name_confirm": details.canonical_name,
        "edit_address_confirm": details.address,
        "edit_url_confirm": details.url,
    }
    if next_phase in expected_map:
        state["edit_expected"] = expected_map[next_phase]
    else:
        state.pop("edit_expected", None)

    state["phase"] = next_phase
    cleanup = _failed_take(state)
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))
    await _render_phase(update, context, state, cleanup_ids=cleanup, drop_input=True)


def _edit_result_notice(
    result: EditMutationResult, phase: str
) -> tuple[str, str | None]:
    """Сообщение о результате мутации и фаза, куда вернуться."""
    alias_phase = phase.startswith("edit_alias")
    ok_phase = "edit_aliases" if alias_phase else "edit_menu"
    if result.status == "ok":
        if phase == "edit_name_confirm":
            return "✅ Название места изменено.", "place"
        if phase == "edit_alias_add_confirm":
            return "✅ Альтернативное название добавлено.", "edit_aliases"
        if phase == "edit_alias_rename_confirm":
            return "✅ Альтернативное название переименовано.", "edit_aliases"
        if phase == "edit_alias_delete_confirm":
            return "✅ Альтернативное название удалено.", "edit_aliases"
        if phase == "edit_address_confirm":
            return "✅ Адрес места обновлён.", "edit_menu"
        if phase == "edit_address_delete_confirm":
            return "✅ Адрес места удалён.", "edit_menu"
        if phase == "edit_url_confirm":
            return "✅ Ссылка места обновлена.", "edit_menu"
        return "✅ Ссылка места удалена.", "edit_menu"
    if result.status == "unchanged":
        return "Значение уже было таким. Изменений нет.", ok_phase
    if result.status == "changed":
        return (
            "Значение изменилось во время подтверждения: другой администратор "
            "обновил это поле. Откройте меню редактирования заново."
        ), ok_phase
    if result.status == "duplicate":
        return "Такое название уже есть у этого места. Введите другое.", ok_phase
    if result.status == "not_found":
        return (
            "Название уже изменено или удалено другим администратором."
        ), "edit_aliases"
    if result.status == "canonical":
        return (
            "Каноническое название изменяется отдельным действием «Изменить "
            "название»."
        ), ok_phase
    if result.status == "conflict":
        return (
            "Название похоже на название другого места. Повторите действие."
        ), ok_phase
    return "Место не существует или было удалено.", None


def _expected_field_from_state(state: dict, fresh_value: str | None) -> str | None:
    """
    Ожидаемое старое значение поля из состояния кабинета.

    Значение захватывается при входе в фазу подтверждения, поэтому гонка
    с другим администратором обнаруживается хранилищем (status "changed").
    Отсутствие ключа — состояние старой сессии: резерв на свежее значение.
    """
    if "edit_expected" not in state:
        return fresh_value
    value = state.get("edit_expected")
    if value is not None and not isinstance(value, str):
        return fresh_value
    return value


async def handle_edit_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Подтверждение изменения данных места: мутация атомарна с аудитом."""
    query = ctx.query(update)
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "edit_name_confirm",
        "edit_alias_add_confirm",
        "edit_alias_rename_confirm",
        "edit_alias_delete_confirm",
        "edit_address_confirm",
        "edit_address_delete_confirm",
        "edit_url_confirm",
        "edit_url_delete_confirm",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    phase = str(state.get("phase"))
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await query.answer()
            await _render_phase(
                update,
                context,
                state,
                notice="Место не существует или было удалено.",
            )
            return
        actor_telegram_user_id = user.id
        try:
            if phase == "edit_name_confirm":
                expected_name = _expected_field_from_state(
                    state, details.canonical_name
                )
                if expected_name is None:
                    # каноническое название всегда существует; отсутствие
                    # захвата означает старое состояние сессии — берём свежее.
                    expected_name = details.canonical_name
                result = rename_canonical_place_name(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    new_name=str(state.get("edit_value") or ""),
                    expected_name=expected_name,
                    force=True,
                )
            elif phase == "edit_alias_add_confirm":
                result = add_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name=str(state.get("edit_value") or ""),
                    force=True,
                )
            elif phase == "edit_alias_rename_confirm":
                name_id = state.get("edit_target_name_id")
                result = rename_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name_id=name_id if isinstance(name_id, int) else -1,
                    new_name=str(state.get("edit_value") or ""),
                    expected_name=str(state.get("edit_target_name") or ""),
                    force=True,
                )
            elif phase == "edit_alias_delete_confirm":
                name_id = state.get("edit_target_name_id")
                result = delete_place_alias(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    name_id=name_id if isinstance(name_id, int) else -1,
                    expected_name=str(state.get("edit_target_name") or ""),
                )
            elif phase == "edit_address_confirm":
                result = set_place_address(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    address=str(state.get("edit_value") or ""),
                    expected_address=_expected_field_from_state(state, details.address),
                )
            elif phase == "edit_address_delete_confirm":
                result = delete_place_address(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    expected_address=_expected_field_from_state(state, details.address),
                )
            elif phase == "edit_url_confirm":
                result = set_place_url(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    url=str(state.get("edit_value") or ""),
                    expected_url=_expected_field_from_state(state, details.url),
                )
            else:  # edit_url_delete_confirm
                result = delete_place_url(
                    session,
                    actor_telegram_user_id=actor_telegram_user_id,
                    place_id=place_id,
                    expected_url=_expected_field_from_state(state, details.url),
                )
        except PlaceEditPermissionError:
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
            logger.error("Не удалось изменить данные места: %s", e)
            state["phase"] = "edit_menu"
            await query.answer()
            await _render_phase(
                update,
                context,
                state,
                notice="Не удалось изменить данные места из-за ошибки базы данных.",
            )
            return
    finally:
        session.close()

    await query.answer()
    notice, next_phase = _edit_result_notice(result, phase)
    _clear_edit_keys(state)
    if result.status == "place_missing":
        state.clear()
        state.update(_new_state())
    elif next_phase is not None:
        state["phase"] = next_phase
    else:
        state["phase"] = "edit_menu"
    await _render_phase(update, context, state, notice=notice)


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
    """Отмена поиска/подтверждения/редактирования: возврат на шаг назад."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    phase = state.get("phase") if state else None
    if state is None or phase not in (
        {
            "assign_search",
            "assign_results",
            "confirm_assign",
            "confirm_replace",
            "confirm_revoke",
            "edit_menu",
            "edit_aliases",
        }
        | EDIT_CANCEL_TO_MENU_PHASES
        | EDIT_CANCEL_TO_ALIASES_PHASES
        | TRANSFER_CANCEL_PHASES
    ):
        await _stale(update)
        return
    await query.answer()
    for key in (
        "target_telegram_user_id",
        "expected_admin_telegram_user_id",
        "mode",
        "query",
        "query_kind",
    ):
        state.pop(key, None)
    if phase in TRANSFER_CANCEL_PHASES:
        # Это именно выход с экрана, а не отмена активного предложения:
        # для отмены есть отдельная кнопка, которая меняет статус в БД.
        _clear_transfer_keys(state)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase == "edit_menu":
        # Назад из меню редактирования к карточке места.
        _clear_edit_keys(state)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase == "edit_aliases":
        # R1, дефект №22: кнопка «Назад» на списке альтернативных названий
        # ведёт к карточке места. Раньше фаза не входила в allow-list, и
        # кнопка выглядела устаревшей (плюс падение при отрисовке).
        _clear_edit_keys(state)
        state.pop("alias_page", None)
        if isinstance(state.get("place_id"), int):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase in EDIT_CANCEL_TO_MENU_PHASES:
        # Возврат из ввода/подтверждения к меню редактирования.
        _clear_edit_keys(state)
        state["phase"] = "edit_menu"
    elif phase in EDIT_CANCEL_TO_ALIASES_PHASES:
        # Возврат из алиасных фаз к списку альтернативных названий.
        _clear_edit_keys(state)
        state["phase"] = "edit_aliases"
    elif isinstance(state.get("place_id"), int):
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
        "query_kind",
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
    _failed_add(state, update)
    parsed = _normalize_assign_query(raw)
    if parsed is None:
        # R1, дефект №21: пустой/несимвольный ввод — явная ошибка формата.
        # Фаза остаётся assign_search: пользователь сразу повторяет ввод.
        state.pop("query", None)
        state.pop("query_kind", None)
        await _render_phase(
            update,
            context,
            state,
            notice=ASSIGN_QUERY_INVALID_TEXT,
            cleanup_ids=_failed_take(state),
            drop_input=True,
        )
        return
    kind, value = parsed
    session = SessionLocal()
    try:
        search_known_users(session, value)
    except UserSearchError as e:
        state.pop("query", None)
        state.pop("query_kind", None)
        await _render_phase(
            update,
            context,
            state,
            notice=str(e),
            cleanup_ids=_failed_take(state),
            drop_input=True,
        )
        return
    finally:
        session.close()

    state["query"] = value
    state["query_kind"] = kind
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


async def _transfer_state_place(
    update: Update, *, allowed_phases: frozenset[str]
) -> tuple[dict, int, str] | None:
    """
    Общий guard кнопок передачи для инициатора.

    Callback подтверждает только id; фазу и место берём из сохранённого
    состояния, а действующую роль перепроверяем по БД в обработчике.
    """
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in allowed_phases:
        await _stale(update)
        return None
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return None
    return state, place_id, str(state.get("phase"))


async def handle_transfer_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Кнопка «Передать админку»: активное предложение или ввод username."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    state_place_id = state.get("place_id")
    data = str(query.data or "")
    try:
        callback_place_id = int(data.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    if not isinstance(state_place_id, int) or callback_place_id != state_place_id:
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        details = get_place_details(session, callback_place_id)
        if details is None or not is_place_admin(
            session, ctx.user(update).id, callback_place_id
        ):
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        pending = get_pending_transfer_for_place(session, callback_place_id)
        _clear_transfer_keys(state)
        state["phase"] = (
            "transfer_outgoing" if pending is not None else "transfer_username_input"
        )
    finally:
        session.close()
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Username получателя: точный поиск известного или приглашение."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in TRANSFER_INPUT_PHASES:
        return
    place_id = state.get("place_id")
    session = SessionLocal()
    try:
        if not isinstance(place_id, int) or not is_place_admin(
            session, user.id, place_id
        ):
            clear_admin_session(user.id)
            await _render_denied(update, context)
            return
        details = get_place_details(session, place_id)
        if details is None:
            state.clear()
            state.update(_new_state())
            await _render_phase(
                update,
                context,
                state,
                notice="Место не существует или было удалено.",
                drop_input=True,
            )
            return
        raw = ctx.message(update).text or ""
        try:
            username = normalize_transfer_username(raw)
        except ValueError as e:
            cleanup = []
            message = getattr(update, "message", None)
            if message is not None and message.message_id is not None:
                cleanup.append(int(message.message_id))
            await _render_phase(
                update,
                context,
                state,
                notice=str(e),
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return

        state["transfer_username"] = username
        _clear_transfer_keys_for_username(state)
        found = find_known_recipients(session, username)
        # Себя нельзя предлагать даже в списке кандидатов: кнопка вела бы
        # только к отказу store. Если других совпадений нет — явная ошибка,
        # а не приглашение на собственный username.
        candidates = tuple(
            candidate for candidate in found if candidate.telegram_user_id != user.id
        )
        if not candidates:
            if found:
                cleanup = []
                message = getattr(update, "message", None)
                if message is not None and message.message_id is not None:
                    cleanup.append(int(message.message_id))
                await _render_phase(
                    update,
                    context,
                    state,
                    notice=(
                        "Нельзя передать админку самому себе. Введите username "
                        "другого человека."
                    ),
                    cleanup_ids=cleanup,
                    drop_input=True,
                )
                return
            state["phase"] = "transfer_confirm_invite"
        elif len(candidates) == 1:
            state["transfer_target_telegram_user_id"] = candidates[0].telegram_user_id
            state["phase"] = "transfer_confirm_known"
        else:
            state["transfer_candidate_ids"] = [
                candidate.telegram_user_id for candidate in candidates
            ]
            state["phase"] = "transfer_candidates"
    finally:
        session.close()

    cleanup = []
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))
    await _render_phase(update, context, state, cleanup_ids=cleanup, drop_input=True)


def _clear_transfer_keys_for_username(state: dict) -> None:
    """Сбрасывает выбранного получателя, сохраняя введённый username."""
    state.pop("transfer_candidate_ids", None)
    state.pop("transfer_target_telegram_user_id", None)


async def handle_transfer_pick(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Выбор конкретного известного пользователя при совпадении username."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "transfer_candidates":
        await _stale(update)
        return
    raw_ids = state.get("transfer_candidate_ids")
    allowed = set(raw_ids) if isinstance(raw_ids, list) else set()
    try:
        target_tg = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    if target_tg not in allowed:
        await _stale(update)
        return
    state["transfer_target_telegram_user_id"] = target_tg
    state["phase"] = "transfer_confirm_known"
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Подтверждение создания предложения (известный или неизвестный получатель)."""
    query = ctx.query(update)
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") not in {
        "transfer_confirm_known",
        "transfer_confirm_invite",
    }:
        await _stale(update)
        return
    place_id = state.get("place_id")
    if not isinstance(place_id, int):
        await _stale(update)
        return
    phase = str(state.get("phase"))
    username = str(state.get("transfer_username") or "")
    target_tg = state.get("transfer_target_telegram_user_id")
    if phase == "transfer_confirm_known" and not isinstance(target_tg, int):
        state["phase"] = "transfer_username_input"
        await query.answer()
        await _render_phase(
            update,
            context,
            state,
            notice="Выберите получателя заново.",
        )
        return
    if not username:
        state["phase"] = "transfer_username_input"
        await query.answer()
        await _render_phase(
            update, context, state, notice="Введите username получателя заново."
        )
        return

    result = None
    try:
        session = SessionLocal()
        try:
            _ensure_user(session, update)
            if not is_place_admin(session, user.id, place_id):
                await query.answer(DENIED_ALERT, show_alert=True)
                return
            result = create_admin_transfer(
                session,
                actor_telegram_user_id=user.id,
                place_id=place_id,
                username=username,
                recipient_telegram_user_id=(
                    target_tg if phase == "transfer_confirm_known" else None
                ),
            )
        finally:
            session.close()
    except Exception as e:  # noqa: BLE001 — безопасный экран вместо падения
        logger.error("Не удалось создать предложение передачи: %s", e)
        result = None
        notice = "Не удалось создать предложение. Повторите позже."
    else:
        notice = ""

    if result is None:
        await query.answer()
        await _render_phase(update, context, state, notice=notice)
        return

    invite_url: str | None = None
    if result.status == "created_known":
        delivered = False
        if result.transfer is not None:
            delivered = await _notify_recipient(context, result.transfer)
            if delivered:
                session = SessionLocal()
                try:
                    mark_transfer_notified(session, result.transfer.transfer_id)
                except Exception as e:  # noqa: BLE001 — статус доставки не критичен
                    logger.error("Не удалось отметить доставку уведомления: %s", e)
                finally:
                    session.close()
        notice = (
            "✅ Предложение создано. Получатель получил уведомление."
            if delivered
            else (
                "✅ Предложение создано, но личное уведомление не доставлено. "
                "Откройте передачу и нажмите «Уведомить получателя ещё раз» "
                "после восстановления связи."
            )
        )
        _clear_transfer_keys(state)
        state["phase"] = "transfer_outgoing"
    elif result.status == "created_invite":
        token = result.invite_token or ""
        transfer_id = result.transfer.transfer_id if result.transfer is not None else 0
        invite_url = (
            await _deep_link(context, f"tf_{transfer_id}_{token}")
            if token and transfer_id
            else None
        )
        if invite_url is None:
            session = SessionLocal()
            try:
                if result.transfer is not None:
                    cancel_admin_transfer(
                        session,
                        actor_telegram_user_id=user.id,
                        transfer_id=result.transfer.transfer_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.error("Не удалось отменить неполное приглашение: %s", e)
            finally:
                session.close()
            _clear_transfer_keys(state)
            state["phase"] = "transfer_username_input"
            notice = (
                "Не удалось сформировать ссылку-приглашение. Предложение "
                "отменено; повторите попытку."
            )
        else:
            _clear_transfer_keys(state)
            state["phase"] = "transfer_outgoing"
            notice = (
                "✅ Ссылка-приглашение создана на 24 часа. Она показана ниже "
                "один раз."
            )
    elif result.status == "active_exists":
        _clear_transfer_keys(state)
        state["phase"] = "transfer_outgoing"
        notice = "У этого места уже есть активное предложение передачи."
    elif result.status == "self":
        notice = "Нельзя передать админку самому себе."
    elif result.status == "user_missing":
        notice = "Получатель больше не известен боту. Введите username заново."
        state["phase"] = "transfer_username_input"
    elif result.status == "place_missing":
        state.clear()
        state.update(_new_state())
        notice = "Место не существует или было удалено."
    else:
        notice = "Предложение не создано: состояние изменилось. Повторите."

    await query.answer()
    await _render_phase(update, context, state, notice=notice, invite_url=invite_url)


async def handle_transfer_refresh(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Перечитывает статус предложения и лениво применяет срок."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    session = SessionLocal()
    try:
        if not is_place_admin(session, ctx.user(update).id, place_id):
            await query.answer(DENIED_ALERT, show_alert=True)
            return
        get_pending_transfer_for_place(session, place_id)
    finally:
        session.close()
    await query.answer()
    await _render_phase(update, context, state)


async def handle_transfer_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Инициатор утверждает кандидата, открывшего одноразовую ссылку."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or view.from_user.telegram_user_id != ctx.user(update).id
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        result = approve_transfer_recipient(
            session,
            actor_telegram_user_id=ctx.user(update).id,
            transfer_id=transfer_id,
        )
    finally:
        session.close()

    if result.status != "approved":
        await query.answer()
        notice = {
            "not_opened": "Ссылку ещё никто не открыл.",
            "already_approved": "Получатель уже утверждён.",
            "not_initiator": "Утвердить получателя может только инициатор.",
            "superseded": "Предложение недействительно: администратор изменился.",
            "expired": "Срок предложения истёк.",
            "self": "Нельзя утвердить самого себя.",
        }.get(result.status, "Состояние изменилось. Обновите экран.")
        await _render_phase(update, context, state, notice=notice)
        return

    delivered = False
    if result.transfer is not None:
        delivered = await _notify_recipient(context, result.transfer)
        if delivered:
            session = SessionLocal()
            try:
                mark_transfer_notified(session, transfer_id)
            except Exception as e:  # noqa: BLE001
                logger.error("Не удалось отметить доставку уведомления: %s", e)
            finally:
                session.close()
    await query.answer()
    notice = "✅ Получатель утверждён."
    if not delivered:
        notice += (
            " Личное уведомление не доставлено; получатель может открыть "
            "ссылку повторно или вы нажмёте «Уведомить получателя ещё раз»."
        )
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_notify(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Повторная best-effort доставка уведомления уже утверждённому получателю."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or not is_place_admin(session, ctx.user(update).id, place_id)
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        if not view.awaiting_recipient:
            await query.answer(
                "Уведомление больше не требуется: состояние передачи изменилось.",
                show_alert=True,
            )
            await _render_phase(update, context, state)
            return
    finally:
        session.close()

    delivered = await _notify_recipient(context, view)
    if delivered:
        session = SessionLocal()
        try:
            mark_transfer_notified(session, transfer_id)
        except Exception as e:  # noqa: BLE001
            logger.error("Не удалось отметить доставку уведомления: %s", e)
        finally:
            session.close()
    await query.answer()
    notice = (
        "✅ Уведомление доставлено."
        if delivered
        else "Не удалось доставить уведомление. Получатель может открыть ссылку повторно."
    )
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Инициатор отменяет предложение до подтверждения; права не меняются."""
    query = ctx.query(update)
    guard = await _transfer_state_place(
        update, allowed_phases=frozenset({"transfer_outgoing"})
    )
    if guard is None:
        return
    state, place_id, _phase = guard
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    result = None
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        view = get_transfer_view(session, transfer_id)
        if (
            view is None
            or view.place_id != place_id
            or view.from_user.telegram_user_id != ctx.user(update).id
        ):
            await query.answer(STALE_TEXT, show_alert=True)
            return
        result = cancel_admin_transfer(
            session,
            actor_telegram_user_id=ctx.user(update).id,
            transfer_id=transfer_id,
        )
    finally:
        session.close()

    if result.status == "cancelled":
        recipient = None
        if result.transfer is not None:
            recipient = result.transfer.to_user or result.transfer.opened_by
        if recipient is not None:
            await _send_user_notification(
                context,
                recipient.telegram_user_id,
                (
                    "❌ Передача администрации отменена\n\n"
                    f"📍 {_h(result.transfer.place_name if result.transfer else '')}"
                    "\n\nПолномочия не изменились."
                ),
            )
        _clear_transfer_keys(state)
        state["phase"] = "place"
        notice = "✅ Передача отменена. Права остались у вас."
    else:
        notice = {
            "not_initiator": "Отменить передачу может только её инициатор.",
            "expired": "Срок предложения уже истёк.",
            "superseded": "Предложение уже недействительно: администратор изменился.",
            "cancelled": "Передача уже отменена.",
            "confirmed": "Передача уже подтверждена и не может быть отменена.",
            "rejected": "Получатель уже отклонил предложение.",
        }.get(result.status, "Состояние изменилось. Обновите экран.")
        if result.transfer is None:
            state["phase"] = "place"
        else:
            state["phase"] = "transfer_outgoing"
    await query.answer()
    await _render_phase(update, context, state, notice=notice)


async def handle_transfer_accept(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Получатель подтверждает передачу в своём Telegram-аккаунте."""
    query = ctx.query(update)
    user = ctx.user(update)
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = confirm_admin_transfer(
            session,
            actor_telegram_user_id=user.id,
            transfer_id=transfer_id,
        )
    except Exception as e:  # noqa: BLE001 — отказ безопаснее ложного подтверждения
        logger.error("Не удалось подтвердить передачу: %s", e)
        await query.answer(
            "Не удалось подтвердить передачу. Повторите позже.", show_alert=True
        )
        return
    finally:
        session.close()

    if result.status == "confirmed":
        await query.answer("Передача подтверждена")
        view = result.transfer
        if view is not None:
            text = (
                "✅ Вы теперь администратор места\n\n"
                f"📍 {_h(view.place_name)}\n\n"
                "Права прежнего администратора сняты. Его открытые экраны "
                "этого места больше не действуют: права проверяются заново."
            )
            await _edit_notification_message(
                query,
                text,
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🛡 Открыть кабинет",
                                callback_data="admin_panel",
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
            await _notify_initiator(
                context,
                view,
                (
                    "✅ Передача администрации завершена\n\n"
                    f"📍 {_h(view.place_name)}\n"
                    f"Новый администратор: "
                    f"{_h(_transfer_user_label(view.to_user)) if view.to_user else '—'}\n\n"
                    "Ваши полномочия на это место сняты."
                ),
            )
        return

    if result.status == "already_confirmed":
        await query.answer("Передача уже подтверждена")
        await _edit_notification_message(
            query,
            "✅ Это предложение уже подтверждено. Повторно права не меняются.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
            ),
        )
        return

    if result.status == "not_recipient":
        await query.answer(
            "Предложение адресовано другому пользователю.", show_alert=True
        )
        return

    await query.answer("Передача не может быть подтверждена")
    text, keyboard = _incoming_transfer_content(result.status, result.transfer, user.id)
    await _edit_notification_message(query, text, keyboard)


async def handle_transfer_decline(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Получатель отклоняет предложение; права не меняются."""
    query = ctx.query(update)
    user = ctx.user(update)
    try:
        transfer_id = int(str(query.data or "").rsplit(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = reject_admin_transfer(
            session,
            actor_telegram_user_id=user.id,
            transfer_id=transfer_id,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Не удалось отклонить передачу: %s", e)
        await query.answer(
            "Не удалось отклонить передачу. Повторите позже.", show_alert=True
        )
        return
    finally:
        session.close()

    if result.status == "rejected":
        await query.answer("Предложение отклонено")
        view = result.transfer
        await _edit_notification_message(
            query,
            (
                "❌ Вы отклонили передачу администрации\n\n"
                f"📍 {_h(view.place_name) if view else ''}\n\n"
                "Полномочия не изменились."
            ),
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")]]
            ),
        )
        if view is not None:
            await _notify_initiator(
                context,
                view,
                (
                    "❌ Получатель отклонил передачу администрации\n\n"
                    f"📍 {_h(view.place_name)}\n\n"
                    "Вы остаётесь администратором. Можно создать новое "
                    "предложение."
                ),
            )
        return

    if result.status == "not_recipient":
        await query.answer(
            "Предложение адресовано другому пользователю.", show_alert=True
        )
        return

    await query.answer("Предложение уже нельзя отклонить")
    text, keyboard = _incoming_transfer_content(result.status, result.transfer, user.id)
    await _edit_notification_message(query, text, keyboard)


async def handle_transfer_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Deep-link `/start tf_<id>_<token>`: экран получателя без прав.

    Возвращает True, если payload относится к передаче (включая некорректный):
    обычный /start тогда не должен перерисовывать главное меню.
    """
    args = getattr(context, "args", None) or []
    payload = args[0] if args else ""
    if not payload.startswith("tf_"):
        return False

    await screen.reanchor_screen(update, context)
    user = ctx.user(update)
    clear_place_session(user.id)
    clear_admin_session(user.id)

    body = payload[3:]
    transfer_id_raw, separator, token = body.partition("_")
    valid_shape = (
        bool(separator)
        and transfer_id_raw.isdigit()
        and len(transfer_id_raw) <= 18
        and 20 <= len(token) <= 128
        and all(char.isalnum() or char in {"_", "-"} for char in token)
    )
    status = "invalid_invite"
    view: TransferView | None = None
    if valid_shape:
        session = SessionLocal()
        try:
            _ensure_user(session, update)
            result = open_transfer_invitation(
                session,
                transfer_id=int(transfer_id_raw),
                token=token,
                actor_telegram_user_id=user.id,
            )
            status = result.status
            view = result.transfer
        except Exception as e:  # noqa: BLE001 — ссылка не должна ронять бота
            logger.error("Не удалось открыть приглашение передачи: %s", e)
        finally:
            session.close()

    text, keyboard = _incoming_transfer_content(status, view, user.id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user.id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(context, user_data, chat_id, text, keyboard)
    screen.save_screen_ref(user.id, user_data, chat_id)
    return True


class AdminTextHandler(MessageHandler):
    """Текст принимается только в явных фазах кабинета, заданных allowed_phases."""

    def __init__(self, filters_arg, callback, *, allowed_phases: frozenset[str]):
        super().__init__(filters_arg, callback)
        self.allowed_phases = frozenset(allowed_phases)

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
        if state.get("phase") not in self.allowed_phases:
            return None
        return matched


# --- Предложения кирпичиков: очереди и решения (Stage 26) ------------------


def _proposal_callback_id(data: str) -> int | None:
    try:
        value = int(data.rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _proposal_callback_offset(data: str) -> int | None:
    try:
        value = int(data.rsplit(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return max(value, 0)


async def handle_brick_proposals_global(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    await query.answer()
    state = _new_state()
    state.update(
        {
            "phase": "proposal_global_list",
            "proposal_offset": 0,
        }
    )
    await _render_phase(update, context, state)


async def handle_brick_proposals_places(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    if not is_superadmin(ctx.user(update).id):
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "places":
        await _stale(update)
        return
    await query.answer()
    state = _new_state()
    state.update(
        {
            "phase": "proposal_places_list",
            "proposal_offset": 0,
        }
    )
    state.pop("proposal_place_filter", None)
    await _render_phase(update, context, state)


async def handle_brick_proposals_place(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    place_id = _proposal_callback_id(str(query.data or ""))
    state_place_id = state.get("place_id")
    if (
        place_id is None
        or not isinstance(state_place_id, int)
        or state_place_id != place_id
    ):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, ctx.user(update).id, place_id)
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "proposal_places_list"
    state["proposal_offset"] = 0
    state["proposal_place_filter"] = place_id
    await _render_phase(update, context, state)


async def handle_brick_proposals_global_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_global_list":
        await _stale(update)
        return
    offset = _proposal_callback_offset(str(query.data or ""))
    if offset is None:
        await _stale(update)
        return
    await query.answer()
    state["proposal_offset"] = offset
    await _render_phase(update, context, state)


async def handle_brick_proposals_places_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_places_list":
        await _stale(update)
        return
    offset = _proposal_callback_offset(str(query.data or ""))
    if offset is None:
        await _stale(update)
        return
    await query.answer()
    state["proposal_offset"] = offset
    await _render_phase(update, context, state)


async def handle_brick_proposals_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "proposal_global_list",
        "proposal_places_list",
    }:
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer("Предложение недоступно.", show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice="Предложение не найдено или недоступно.",
        )
        return
    await query.answer()
    state["proposal_return_phase"] = state.get("phase")
    state["proposal_id"] = proposal.id
    state["phase"] = "proposal_card"
    await _render_phase(update, context, state)


async def handle_brick_proposal_approve(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_card":
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer(
            "Предложение недоступно: полномочия изменились.",
            show_alert=True,
        )
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Предложение недоступно или полномочия на это место больше "
                "не действуют."
            ),
        )
        return
    await query.answer()
    if proposal.status != PROPOSAL_STATUS_PENDING:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice="Предложение уже не ожидает решения.",
        )
        return
    state["phase"] = "proposal_approve_confirm"
    await _render_phase(update, context, state)


async def handle_brick_proposal_reject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_card":
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        proposal = get_admin_proposal(session, ctx.user(update).id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await query.answer(
            "Предложение недоступно: полномочия изменились.",
            show_alert=True,
        )
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Предложение недоступно или полномочия на это место больше "
                "не действуют."
            ),
        )
        return
    await query.answer()
    if proposal.status != PROPOSAL_STATUS_PENDING:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice="Предложение уже не ожидает решения.",
        )
        return
    state["phase"] = "proposal_reject_confirm"
    await _render_phase(update, context, state)


async def _notify_proposal_author(
    context: ContextTypes.DEFAULT_TYPE,
    proposal: BrickProposalSnapshot,
    *,
    approved: bool,
    brick_id: int | None = None,
) -> bool:
    """Best-effort уведомление после уже зафиксированного решения."""
    if approved:
        text = (
            f"✅ Ваше предложение кирпичика «{_h(proposal.title)}» "
            f"<code>[{_h(proposal.code)}]</code> одобрено.\n\n"
            f"Область: {_h(_proposal_target_label(proposal))}.\n"
            "Создан отдельный кирпичик; ваш исходный личный кирпичик и "
            "шаблоны не изменены."
        )
        if brick_id is not None:
            text += f"\nНовый кирпичик: #{brick_id}."
    else:
        text = (
            f"❌ Ваше предложение кирпичика «{_h(proposal.title)}» "
            f"<code>[{_h(proposal.code)}]</code> отклонено.\n\n"
            f"Область: {_h(_proposal_target_label(proposal))}.\n"
            "Личный кирпичик и шаблоны не изменены. Новое предложение можно "
            "отправить позже."
        )
        if proposal.decision_note:
            text += f"\nПричина: {_h(proposal.decision_note)}"
    text += "\n\nСтатус доступен в разделе «Мои кирпичики» → «Мои предложения»."
    return await _send_user_notification(
        context, proposal.author_telegram_user_id, text
    )


async def _handle_brick_proposal_decision(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    approved: bool,
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    expected_phase = (
        "proposal_approve_confirm" if approved else "proposal_reject_confirm"
    )
    if state is None or state.get("phase") != expected_phase:
        await _stale(update)
        return
    proposal_id = _proposal_callback_id(str(query.data or ""))
    if proposal_id is None or state.get("proposal_id") != proposal_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        result = (
            approve_brick_proposal(
                session,
                actor_telegram_user_id=user_id,
                proposal_id=proposal_id,
            )
            if approved
            else reject_brick_proposal(
                session,
                actor_telegram_user_id=user_id,
                proposal_id=proposal_id,
            )
        )
    except (ProposalPermissionError, ProposalActorError, ValueError) as exc:
        await query.answer(str(exc), show_alert=True)
        state.clear()
        state.update(_new_state())
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Полномочия изменились или предложение недоступно; решение "
                "не принято."
            ),
        )
        return
    finally:
        session.close()

    await query.answer()
    if result.status == "not_found":
        state.clear()
        state.update(_new_state())
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Предложение не найдено.",
        )
        return
    if result.status in {"already_decided", "stale"}:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Предложение уже рассмотрено.",
        )
        return
    if result.status in {"conflict", "error", "invalid"}:
        state["phase"] = "proposal_card"
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Решение не принято.",
        )
        return

    delivered = False
    if result.proposal is not None:
        delivered = await _notify_proposal_author(
            context,
            result.proposal,
            approved=approved,
            brick_id=result.brick.id if result.brick is not None else None,
        )
    conflict_ids = [view.id for view in result.conflicts]
    if approved and conflict_ids:
        notified, undelivered = await _notify_conflict_admins(context, conflict_ids)
        if notified and undelivered:
            notice_extra = (
                f" Администраторов мест уведомлено: {notified}, "
                f"не доставлено: {undelivered}."
            )
        elif notified:
            notice_extra = f" Администраторов мест уведомлено: {notified}."
        elif undelivered:
            notice_extra = (
                f" Уведомления администраторам мест не доставлены: {undelivered}; "
                "задача видна в карточке места."
            )
        else:
            notice_extra = (
                " Уведомления администраторам мест поставлены в очередь; "
                "если администратора нет, задача видна в карточке места."
            )
    else:
        notice_extra = ""
    state["phase"] = "proposal_card"
    if result.proposal is not None:
        state["proposal_id"] = result.proposal.id
    notice = result.message or (
        "Предложение одобрено." if approved else "Предложение отклонено."
    )
    notice += notice_extra
    if delivered:
        notice += " Автору отправлено уведомление."
    else:
        notice += (
            " Уведомление автору не доставлено; решение сохранено, статус "
            "доступен в «Мои предложения»."
        )
    await _render_phase(update, context, state, notice=notice)


async def handle_brick_proposal_approve_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle_brick_proposal_decision(update, context, approved=True)


async def handle_brick_proposal_reject_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _handle_brick_proposal_decision(update, context, approved=False)


async def handle_brick_proposals_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "proposal_global_list",
        "proposal_places_list",
        "proposal_card",
        "proposal_approve_confirm",
        "proposal_reject_confirm",
    }:
        await _stale(update)
        return
    await query.answer()
    phase = state.get("phase")
    if phase in {"proposal_global_list", "proposal_places_list"}:
        if isinstance(state.get("proposal_place_filter"), int) and isinstance(
            state.get("place_id"), int
        ):
            state["phase"] = "place"
        else:
            state.clear()
            state.update(_new_state())
    elif phase in {"proposal_approve_confirm", "proposal_reject_confirm"}:
        state["phase"] = "proposal_card"
    else:
        return_phase = state.get("proposal_return_phase")
        if return_phase not in {
            "proposal_global_list",
            "proposal_places_list",
        }:
            return_phase = (
                "proposal_places_list"
                if isinstance(state.get("proposal_place_filter"), int)
                else "proposal_global_list"
            )
        state.pop("proposal_id", None)
        state.pop("proposal_return_phase", None)
        state["phase"] = return_phase
    await _render_phase(update, context, state)


# --- Конфликты кодов: обработчики (Poster 2.1 R3) --------------------------


def _conflict_callback_id(data: str) -> int | None:
    return _proposal_callback_id(data)


async def handle_conflicts_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Кнопка «Конфликты кодов» в карточке места."""
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "place":
        await _stale(update)
        return
    place_id = _conflict_callback_id(str(query.data or ""))
    state_place_id = state.get("place_id")
    if (
        place_id is None
        or not isinstance(state_place_id, int)
        or state_place_id != place_id
    ):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        allowed = can_manage_place(session, ctx.user(update).id, place_id)
    finally:
        session.close()
    if not allowed:
        await query.answer(DENIED_ALERT, show_alert=True)
        return
    await query.answer()
    state["phase"] = "conflicts_list"
    await _render_phase(update, context, state)


async def handle_conflict_open(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "conflicts_list",
        "conflict_card",
        "conflict_rename_input",
        "conflict_switch_confirm",
    }:
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer("Задача недоступна.", show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice="Задача конфликта не найдена или полномочия изменились.",
        )
        return
    await query.answer()
    state["place_id"] = view.place_id
    state["conflict_id"] = view.id
    state["phase"] = "conflict_card"
    await _render_phase(update, context, state)


async def handle_conflict_rename(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_card":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer("Задача недоступна.", show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice="Задача конфликта не найдена или полномочия изменились.",
        )
        return
    await query.answer()
    if not view.is_open:
        await _render_phase(
            update,
            context,
            state,
            notice="Задача уже решена.",
        )
        return
    state["phase"] = "conflict_rename_input"
    await _render_phase(update, context, state)


async def handle_conflict_switch(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_card":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        view = get_conflict(session, ctx.user(update).id, conflict_id)
    finally:
        session.close()
    if view is None:
        await query.answer("Задача недоступна.", show_alert=True)
        await _render_phase(
            update,
            context,
            state,
            notice="Задача конфликта не найдена или полномочия изменились.",
        )
        return
    await query.answer()
    if not view.is_open:
        await _render_phase(update, context, state, notice="Задача уже решена.")
        return
    state["phase"] = "conflict_switch_confirm"
    await _render_phase(update, context, state)


def _conflict_result_notice(result: ConflictResolutionResult) -> str:
    if result.ok:
        return f"✅ {result.message}" if result.message else "✅ Задача решена."
    if result.status == "already_resolved":
        return "Задачу уже решили в другом окне; повторное решение не применено."
    if result.status == "superseded":
        return "Местный кирпичик уже не держит этот код; задача закрыта."
    return result.message or "Решение не принято."


async def handle_conflict_switch_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_admin_session(ctx.user(update).id)
    if state is None or state.get("phase") != "conflict_switch_confirm":
        await _stale(update)
        return
    conflict_id = _conflict_callback_id(str(query.data or ""))
    if conflict_id is None or state.get("conflict_id") != conflict_id:
        await _stale(update)
        return
    user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        try:
            result = resolve_conflict_by_switch(
                session,
                actor_telegram_user_id=user_id,
                conflict_id=conflict_id,
            )
        except (BrickPermissionError, ValueError) as exc:
            await query.answer(str(exc), show_alert=True)
            state["phase"] = "conflicts_list"
            await _render_phase(
                update,
                context,
                state,
                notice="Полномочия изменились; решение не принято.",
            )
            return
    finally:
        session.close()
    await query.answer()
    state["phase"] = "conflict_card"
    await _render_phase(
        update,
        context,
        state,
        notice=_conflict_result_notice(result),
    )


async def handle_conflict_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Новый код местного кирпичика в фазе переименования."""
    user = ctx.user(update)
    state = load_admin_session(user.id)
    if state is None or state.get("phase") != "conflict_rename_input":
        return
    conflict_id = state.get("conflict_id")
    if not isinstance(conflict_id, int) or isinstance(conflict_id, bool):
        await _stale(update)
        return
    raw = (ctx.message(update).text or "").strip()
    cleanup: list[int] = []
    message = getattr(update, "message", None)
    if message is not None and message.message_id is not None:
        cleanup.append(int(message.message_id))

    session = SessionLocal()
    try:
        _ensure_user(session, update)
        try:
            result = resolve_conflict_by_rename(
                session,
                actor_telegram_user_id=user.id,
                conflict_id=conflict_id,
                new_code=raw,
            )
        except (BrickPermissionError, BrickConflictError) as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=f"Код не принят: {exc}",
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
        except ValueError as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=f"Код не принят: {exc}",
                cleanup_ids=cleanup,
                drop_input=True,
            )
            return
    finally:
        session.close()

    if result.ok:
        state["phase"] = "conflict_card"
        await _render_phase(
            update,
            context,
            state,
            notice=_conflict_result_notice(result),
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    if result.status in {"conflict", "unchanged"}:
        await _render_phase(
            update,
            context,
            state,
            notice=_conflict_result_notice(result) + " Введите другой код.",
            cleanup_ids=cleanup,
            drop_input=True,
        )
        return
    state["phase"] = "conflict_card"
    await _render_phase(
        update,
        context,
        state,
        notice=_conflict_result_notice(result),
        cleanup_ids=cleanup,
        drop_input=True,
    )


def _conflict_notification_text(notification) -> str:
    place = notification.place_name or f"место #{notification.place_id}"
    return (
        "⚔️ <b>Конфликт кодов кирпичиков</b>\n\n"
        f"📍 {_h(place)}\n"
        f"Код: <code>[{_h(notification.code)}]</code>\n"
        f"Местный кирпичик: {_h(notification.local_brick_title)}\n\n"
        "Глобальный кирпичик с этим кодом одобрен и уже действует для новых "
        "привязок. Местный кирпичик не изменён: выберите, переименовать его "
        "или переключить шаблон места на глобальный.\n\n"
        "Откройте: /admin → «Мои места» → место → «⚔️ Конфликты кодов»."
    )


async def _notify_conflict_admins(
    context: ContextTypes.DEFAULT_TYPE,
    conflict_ids: list[int] | None = None,
) -> tuple[int, int]:
    """
    Best-effort рассылка уведомлений из очереди `brick_conflict_notifications`.

    Возвращает `(доставлено, не доставлено)`: счётчик доставки растёт только
    после подтверждённой отправки, сбой отправки учитывается отдельно, чтобы
    итоговая плашка не сообщала «уведомлено» без доставки.

    Дубли невозможны: `mark_conflict_notification` переводит только `pending`,
    повторный прогон находит пустую очередь.
    """
    session = SessionLocal()
    delivered = 0
    undelivered = 0
    try:
        notifications = list_pending_conflict_notifications(
            session, conflict_ids=conflict_ids
        )
    finally:
        session.close()
    for notification in notifications:
        ok = await _send_user_notification(
            context,
            notification.telegram_user_id,
            _conflict_notification_text(notification),
        )
        session = SessionLocal()
        try:
            if mark_conflict_notification(
                session, notification.notification_id, delivered=ok
            ):
                if ok:
                    delivered += 1
                else:
                    undelivered += 1
        finally:
            session.close()
    return delivered, undelivered


async def handle_admin_stale(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Любая необработанная кнопка adm_* — устаревшая, прав не меняет."""
    await _stale(update)


def admin_handlers() -> list[BaseHandler]:
    """
    Обработчики кабинета Stage 20A/20B.

    Регистрируются после мастера поста и «Мест», но до общего возврата в меню:
    callback-и админки не пересекаются с их шаблонами. Текстовые обработчики
    активны только в явных фазах кабинета: поиск получателя и поля
    редактирования места.
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
        # Редактирование данных места (Stage 20B).
        CallbackQueryHandler(handle_edit_open, pattern=r"^adm_edit_place:\d+$"),
        CallbackQueryHandler(handle_edit_name, pattern=r"^adm_edit_name:\d+$"),
        CallbackQueryHandler(handle_aliases, pattern=r"^adm_aliases:\d+$"),
        CallbackQueryHandler(handle_alias_add, pattern=r"^adm_alias_add:\d+$"),
        CallbackQueryHandler(handle_alias_rename, pattern=r"^adm_alias_rename:\d+$"),
        CallbackQueryHandler(handle_alias_delete, pattern=r"^adm_alias_delete:\d+$"),
        CallbackQueryHandler(handle_alias_page, pattern=r"^adm_alias_page:\d+$"),
        CallbackQueryHandler(handle_edit_address, pattern=r"^adm_edit_address:\d+$"),
        CallbackQueryHandler(
            handle_edit_address_delete,
            pattern=r"^adm_edit_address_delete:\d+$",
        ),
        CallbackQueryHandler(handle_edit_url, pattern=r"^adm_edit_url:\d+$"),
        CallbackQueryHandler(
            handle_edit_url_delete, pattern=r"^adm_edit_url_delete:\d+$"
        ),
        CallbackQueryHandler(handle_edit_confirm, pattern=r"^adm_edit_confirm$"),
        # Передача администратора места (Stage 21).
        CallbackQueryHandler(handle_transfer_open, pattern=r"^adm_transfer:\d+$"),
        CallbackQueryHandler(handle_transfer_pick, pattern=r"^adm_transfer_pick:\d+$"),
        CallbackQueryHandler(
            handle_transfer_confirm, pattern=r"^adm_transfer_confirm$"
        ),
        CallbackQueryHandler(
            handle_transfer_approve, pattern=r"^adm_transfer_approve:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_notify, pattern=r"^adm_transfer_notify:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_refresh, pattern=r"^adm_transfer_refresh:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_cancel, pattern=r"^adm_transfer_cancel:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_accept, pattern=r"^adm_transfer_accept:\d+$"
        ),
        CallbackQueryHandler(
            handle_transfer_decline, pattern=r"^adm_transfer_decline:\d+$"
        ),
        # Предложения кирпичиков (Stage 26).
        CallbackQueryHandler(
            handle_brick_proposals_global, pattern=r"^adm_bprop_global$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_global_page,
            pattern=r"^adm_bprop_global_page:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposals_places, pattern=r"^adm_bprop_places$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_places_page,
            pattern=r"^adm_bprop_places_page:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposals_place, pattern=r"^adm_bprop_place:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposals_open, pattern=r"^adm_bprop_open:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_approve, pattern=r"^adm_bprop_approve:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_reject, pattern=r"^adm_bprop_reject:\d+$"
        ),
        CallbackQueryHandler(
            handle_brick_proposal_approve_confirm,
            pattern=r"^adm_bprop_approve_yes:\d+$",
        ),
        CallbackQueryHandler(
            handle_brick_proposal_reject_confirm,
            pattern=r"^adm_bprop_reject_yes:\d+$",
        ),
        CallbackQueryHandler(handle_brick_proposals_back, pattern=r"^adm_bprop_back$"),
        # Конфликты кодов (Poster 2.1 R3).
        CallbackQueryHandler(handle_conflicts_open, pattern=r"^adm_bconflicts:\d+$"),
        CallbackQueryHandler(handle_conflict_open, pattern=r"^adm_bconflict:\d+$"),
        CallbackQueryHandler(
            handle_conflict_rename, pattern=r"^adm_bconflict_rename:\d+$"
        ),
        CallbackQueryHandler(
            handle_conflict_switch, pattern=r"^adm_bconflict_switch:\d+$"
        ),
        CallbackQueryHandler(
            handle_conflict_switch_confirm,
            pattern=r"^adm_bconflict_switch_yes:\d+$",
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_conflict_text,
            allowed_phases=frozenset({"conflict_rename_input"}),
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_search_text,
            allowed_phases=frozenset({"assign_search"}),
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_edit_text,
            allowed_phases=EDIT_INPUT_PHASES,
        ),
        AdminTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_transfer_text,
            allowed_phases=TRANSFER_INPUT_PHASES,
        ),
        CallbackQueryHandler(handle_admin_stale, pattern=r"^adm_"),
    ]
