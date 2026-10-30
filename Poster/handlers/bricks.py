# handlers/bricks.py
"""
Раздел «Мои кирпичики» (Stage 22B).

Личный кирпичик — типизированное определение поля для будущих шаблонов
анонса. На этом этапе пользователь создаёт, смотрит, меняет, архивирует и
восстанавливает определения; в шаблоны и посты они ещё не вставляются.

Безопасность и UX:
- владелец всегда берётся из Telegram update, а brick_id — только из callback;
- сервисный слой повторно проверяет принадлежность, статус и ревизию;
- один экран `utils.screen`; черновик определения хранится в `brick_sessions`
  и не смешивается с постом, «Местами» или админкой;
- до явного «Сохранить» строки `bricks` не существует;
- тип после создания неизменяем; архив мягкий, код остаётся занятым;
- устаревшая кнопка не меняет данные и получает понятный ответ.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from decimal import Decimal, InvalidOperation
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

from admin_state import clear_admin_session
from brick_proposal_store import (
    PROPOSAL_PAGE_SIZE,
    PROPOSAL_SCOPE_PLACE,
    PROPOSAL_SCOPE_SYSTEM,
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_WITHDRAWN,
    BrickProposalSnapshot,
    ProposalActorError,
    ProposalError,
    create_brick_proposal,
    get_own_proposal,
    list_own_proposals,
    withdraw_brick_proposal,
)
from brick_state import load_brick_session, save_brick_session
from brick_store import (
    BrickActorError,
    BrickConflictError,
    archive_personal_brick,
    check_code_availability,
    count_personal_bricks,
    create_personal_brick,
    get_personal_brick,
    list_personal_bricks,
    restore_personal_brick,
    update_personal_brick,
)
from brick_types import (
    BRICK_STATUS_ACTIVE,
    BRICK_STATUS_ARCHIVED,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_URL,
    BRICK_TYPES,
    BrickValidationError,
    clean_brick_description,
    clean_brick_title,
    ensure_brick_type,
    parse_rules,
    validate_brick_code,
)
from database import SessionLocal
from models import User
from place_state import clear_place_session
from place_store import get_place_details, list_places
from template_state import clear_template_session
from user_store import get_or_create_user
from utils import keyboard_layouts, screen
from utils import tg_context as ctx

logger = logging.getLogger(__name__)

STALE_TEXT = "Кнопка устарела: откройте раздел заново."
# U3a №8: предложение запускается только из строки конкретного кирпичика.
# Так отвечает устаревшая общая кнопка `bp_start` без id из старых сообщений.
PROPOSAL_ROW_ONLY_HINT = "Выберите кирпичик в списке: «📨» справа от его названия."
# U3a: у нового пользователя кирпичиков нет — ссылаться на «📨» не на что,
# подсказка ведёт к кнопке, которая на экране действительно есть.
PROPOSAL_CREATE_HINT = "Сначала создайте кирпичик кнопкой «➕ Создать кирпичик»."
PAGE_SIZE = 8
_BUTTON_TITLE_LIMIT = 48

TYPE_UI_TITLES = {
    BRICK_TYPE_SHORT_TEXT: "Короткий текст",
    BRICK_TYPE_LONG_TEXT: "Большой текст",
    BRICK_TYPE_DATE: "Дата",
    BRICK_TYPE_TIME: "Время",
    BRICK_TYPE_URL: "Ссылка",
    BRICK_TYPE_NUMBER: "Число",
    BRICK_TYPE_CHOICE: "Выбор из вариантов",
}

TYPE_UI_HINTS = {
    BRICK_TYPE_SHORT_TEXT: "Одна строка: имя, роль, короткая подпись.",
    BRICK_TYPE_LONG_TEXT: "Несколько строк: описание, примечание, программа.",
    BRICK_TYPE_DATE: "Дата события; при вводе будет использован календарь.",
    BRICK_TYPE_TIME: "Время события; используется существующий разбор времени.",
    BRICK_TYPE_URL: "Ссылка; проверяется и нормализуется как адрес.",
    BRICK_TYPE_NUMBER: "Число с необязательными границами и подписью.",
    BRICK_TYPE_CHOICE: "Один или несколько вариантов из вашего списка.",
}

_TEXT_PHASES = frozenset(
    {
        "title_input",
        "code_input",
        "description_input",
        "number_min_input",
        "number_max_input",
        "number_suffix_input",
        "choice_input",
    }
)
TEXT_PHASES = _TEXT_PHASES
_WIZARD_PHASES = frozenset(
    {
        *_TEXT_PHASES,
        "type_select",
        "number_settings",
        "choice_settings",
        "preview",
        "edit_menu",
    }
)
_PROPOSAL_PHASES = frozenset(
    {
        "proposal_select_brick",
        "proposal_scope",
        "proposal_select_place",
        "proposal_preview",
        "proposal_list",
        "proposal_card",
        "proposal_withdraw_confirm",
    }
)
# Число шагов мастера предложения (R4 №24: человек видит «шаг N из M»).
# U3a №8: шаг выбора кирпичика убран — мастер стартует из строки хаба,
# поэтому шагов три: область → место → проверка.
PROPOSAL_STEPS = 3
PHASES = frozenset(
    {
        "hub",
        "archive",
        "card",
        "archive_confirm",
        *_WIZARD_PHASES,
        *_PROPOSAL_PHASES,
    }
)
# Экранные фазы: ввод текста не ожидается, экран живёт кнопками. Свободный
# текст здесь не должен затирать раздел подсказкой главного меню (R4).
SCREEN_PHASES = frozenset(
    {
        "hub",
        "archive",
        "card",
        "archive_confirm",
        *_PROPOSAL_PHASES,
    }
)


# --- Состояние --------------------------------------------------------------


def _new_hub(*, page: int = 0, archive_page: int = 0) -> dict:
    return {"phase": "hub", "page": page, "archive_page": archive_page}


def _page_value(value: object) -> int:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(result, 0)


def _empty_draft() -> dict:
    return {
        "title": "",
        "code": "",
        # S5: инструкция автору анонса, видимая на экране вопроса.
        "description": "",
        "value_type": "",
        "rules": {},
    }


def _new_create_state(*, page: int = 0, archive_page: int = 0) -> dict:
    return {
        "phase": "title_input",
        "mode": "create",
        "draft": _empty_draft(),
        "page": page,
        "archive_page": archive_page,
    }


def _draft(state: dict) -> dict:
    draft = state.get("draft")
    if not isinstance(draft, dict):
        draft = _empty_draft()
        state["draft"] = draft
    if not isinstance(draft.get("title"), str):
        draft["title"] = ""
    if not isinstance(draft.get("code"), str):
        draft["code"] = ""
    if not isinstance(draft.get("description"), str):
        # Старые черновики в `brick_sessions` не знают об описании (S5).
        draft["description"] = ""
    if draft.get("value_type") not in BRICK_TYPES:
        draft["value_type"] = ""
    if not isinstance(draft.get("rules"), dict):
        draft["rules"] = {}
    return draft


def _valid_wizard_state(state: dict) -> bool:
    phase = state.get("phase")
    if phase not in _WIZARD_PHASES:
        return False
    if state.get("mode") not in {"create", "edit"}:
        return False
    draft = state.get("draft")
    if not isinstance(draft, dict):
        return False
    if (
        not isinstance(draft.get("title"), str)
        or not isinstance(draft.get("code"), str)
        or not isinstance(draft.get("description"), str)
    ):
        return False
    value_type = draft.get("value_type")
    if (
        phase not in {"title_input", "code_input", "description_input", "type_select"}
        and value_type not in BRICK_TYPES
    ):
        return False
    if value_type and value_type not in BRICK_TYPES:
        return False
    return isinstance(draft.get("rules"), dict)


def _saved_page(state: dict) -> tuple[int, int]:
    return _page_value(state.get("page")), _page_value(state.get("archive_page"))


def _failed_add(state: dict, update: Update) -> None:
    message = getattr(update, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return
    ids = state.setdefault("failed_inputs", [])
    if message_id not in ids:
        ids.append(int(message_id))


def _failed_take(state: dict) -> list[int]:
    values = state.pop("failed_inputs", None) or []
    result: list[int] = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            continue
    return result


# --- Пользователь и сервисные проверки -------------------------------------


def _ensure_user(session: Session, update: Update) -> User:
    telegram_user = ctx.user(update)
    return get_or_create_user(
        session,
        telegram_user.id,
        username=getattr(telegram_user, "username", None),
        full_name=getattr(telegram_user, "full_name", None),
    )


def _code_available(
    session: Session,
    *,
    user_id: int,
    code: str,
    exclude_brick_id: int | None = None,
):
    return check_code_availability(
        session,
        code,
        scope="personal",
        owner_user_id=user_id,
        exclude_brick_id=exclude_brick_id,
    )


def _suggest_code(session: Session, user_id: int, title: str) -> str:
    """
    Предлагает свободный человекочитаемый код из названия.

    Пользователь видит предложение и может его изменить. Занятые/системные
    коды не подставляются молча: предложение уже проверено и показано.
    """
    normalized = unicodedata.normalize("NFKC", title).casefold()
    pieces: list[str] = []
    for char in normalized:
        if char.isalnum() or char in "_-":
            pieces.append(char)
        else:
            pieces.append("_")
    base = re.sub(r"_+", "_", "".join(pieces)).strip("_-")
    if len(base) < 2:
        base = "мой_кирпичик"
    if len(base) > 64:
        base = base[:64].rstrip("_-")
    if len(base) < 2:
        base = "мой_кирпичик"

    candidate = base
    counter = 2
    while True:
        availability = _code_available(session, user_id=user_id, code=candidate)
        if availability.available:
            return candidate
        suffix = f"_{counter}"
        stem = base[: 64 - len(suffix)].rstrip("_-")
        candidate = f"{stem}{suffix}"
        counter += 1
        if counter > 999:
            return "мой_кирпичик"


# --- Форматирование ---------------------------------------------------------


def _h(value: object) -> str:
    return html_escape(str(value), quote=False)


def _short(value: object, limit: int = _BUTTON_TITLE_LIMIT) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _button_title(brick) -> str:
    return _short(f"{brick.title} — [{brick.code}]")


def _type_title(value_type: str) -> str:
    return TYPE_UI_TITLES.get(value_type, value_type)


def _description_text(value: object) -> str:
    """Описание для экранов: экранируется и переносится, без «дыр»."""
    text = " ".join(str(value or "").split())
    return text or "нет"


def _description_html(value: object) -> str:
    """
    Описание кирпичика моноширинно (Poster 2.1 R4 №27).

    Текст целиком задаёт человек, поэтому он экранируется, а затем
    оборачивается в `<code>`. Пустое описание остаётся коротким «нет» без
    тега: это не описание, а отметка об отсутствии.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return "нет"
    return f"<code>{_h(text)}</code>"


def _format_number(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _number_rules_text(rules: dict) -> str:
    parts: list[str] = []
    if rules.get("integer"):
        parts.append("только целые числа")
    if "min" in rules:
        parts.append(f"минимум {_format_number(rules['min'])}")
    if "max" in rules:
        parts.append(f"максимум {_format_number(rules['max'])}")
    if "suffix" in rules:
        parts.append(f"подпись «{rules['suffix']}»")
    return "Без дополнительных ограничений." if not parts else "; ".join(parts) + "."


def _choice_rules_text(rules: dict) -> str:
    choices = rules.get("choices") or []
    labels = ", ".join(str(item) for item in choices) or "список пуст"
    multiple = "можно выбрать несколько" if rules.get("multiple") else "один вариант"
    return f"{labels}; {multiple}."


def _settings_text(value_type: str, rules: dict) -> str:
    if value_type in {
        BRICK_TYPE_SHORT_TEXT,
        BRICK_TYPE_LONG_TEXT,
        BRICK_TYPE_DATE,
        BRICK_TYPE_TIME,
        BRICK_TYPE_URL,
    }:
        if value_type == BRICK_TYPE_DATE:
            return (
                "Без ограничений. При будущем вводе будет использован "
                "существующий календарь Poster."
            )
        if value_type == BRICK_TYPE_TIME:
            return (
                "Без ограничений. При будущем вводе будет использован "
                "существующий разбор времени."
            )
        return "Без дополнительных настроек."
    if value_type == BRICK_TYPE_NUMBER:
        return _number_rules_text(rules)
    if value_type == BRICK_TYPE_CHOICE:
        return _choice_rules_text(rules)
    return "Без дополнительных настроек."


def _page_note(offset: int, count: int, total: int) -> str:
    if total <= 0:
        return ""
    last = offset + count
    pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = offset // PAGE_SIZE + 1
    return f"Показано {offset + 1}–{last} из {total}. Страница {page} из {pages}."


def _list_keyboard(bricks, *, offset: int, total: int, archive: bool):
    """
    Список кирпичиков: строка = «название» + компактная кнопка действия.

    №28: «предложить» доступно прямо из строки активного кирпичика — не нужно
    сначала открывать карточку. В архиве кнопка не нужна: архивный кирпичик
    предложить нельзя, поэтому строка остаётся одной кнопкой.

    U3a №13: раскладка собирается общими шаблонами `utils.keyboard_layouts`:
    «название + действие справа» и пагинация одной строкой.
    """
    prefix = "bk_apage" if archive else "bk_page"
    rows = keyboard_layouts.list_rows(
        (
            keyboard_layouts.button(_button_title(brick), f"bk_open:{brick.id}"),
            (
                keyboard_layouts.button("📨", f"bp_start:{brick.id}")
                if not archive and brick.status == BRICK_STATUS_ACTIVE
                else None
            ),
        )
        for brick in bricks
    )
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button("◀️", f"{prefix}:{max(offset - PAGE_SIZE, 0)}")
            if offset > 0
            else None
        ),
        (
            keyboard_layouts.button("▶️", f"{prefix}:{offset + PAGE_SIZE}")
            if offset + len(bricks) < total
            else None
        ),
    )
    return rows


# --- Содержимое экранов -----------------------------------------------------


def _hub_content(session: Session, user: User, state: dict):
    total = count_personal_bricks(session, user.telegram_user_id)
    archived_total = count_personal_bricks(
        session, user.telegram_user_id, status_filter=BRICK_STATUS_ARCHIVED
    )
    offset = _page_value(state.get("page"))
    if total and offset >= total:
        offset = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
    bricks = list_personal_bricks(
        session,
        user.telegram_user_id,
        limit=PAGE_SIZE,
        offset=offset,
    )
    state["page"] = offset

    parts = [
        "🧱 <b>Мои кирпичики</b>",
        "",
        "Кирпичик — это определение типизированного поля для будущих "
        "шаблонов анонса. Сейчас он не вставляется в посты и шаблоны.",
    ]
    if total:
        parts.extend(["", f"Активных кирпичиков: {total}."])
        parts.extend(
            [
                "",
                "Чтобы предложить кирпичик месту или всей системе, нажмите "
                "«📨» в его строке.",
            ]
        )
        if bricks:
            parts.extend(["", _page_note(offset, len(bricks), total)])
    elif archived_total:
        parts.extend(
            [
                "",
                "В активном списке пока нет кирпичиков. В архиве осталось: "
                f"{archived_total}. Откройте «🗄 Архив», чтобы восстановить "
                "нужный или создать новый кирпичик.",
            ]
        )
    else:
        parts.extend(
            [
                "",
                "У вас пока нет личных кирпичиков. Создайте первый — например, "
                "«Ведущий мероприятия» или «Формат участия».",
            ]
        )

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("➕ Создать кирпичик", callback_data="bk_create")]
    ]
    rows.append([InlineKeyboardButton("📤 Мои предложения", callback_data="bp_mine")])
    if bricks:
        rows.extend(_list_keyboard(bricks, offset=offset, total=total, archive=False))
    if archived_total:
        rows.append(
            [
                InlineKeyboardButton(
                    f"🗄 Архив ({archived_total})",
                    callback_data="bk_archlist",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def _archive_content(session: Session, user: User, state: dict):
    total = count_personal_bricks(
        session, user.telegram_user_id, status_filter=BRICK_STATUS_ARCHIVED
    )
    offset = _page_value(state.get("archive_page"))
    if total and offset >= total:
        offset = ((total - 1) // PAGE_SIZE) * PAGE_SIZE
    bricks = list_personal_bricks(
        session,
        user.telegram_user_id,
        status_filter=BRICK_STATUS_ARCHIVED,
        limit=PAGE_SIZE,
        offset=offset,
    )
    state["archive_page"] = offset
    if not total:
        return None
    parts = [
        "🗄 <b>Архив кирпичиков</b>",
        "",
        "Архивные кирпичики не показываются в активном списке, но остаются "
        "вашими. Их можно восстановить; код всё это время остаётся занятым.",
        "",
        _page_note(offset, len(bricks), total),
    ]
    rows = _list_keyboard(bricks, offset=offset, total=total, archive=True)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_hub")])
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def _card_content(brick):
    state_label = "🟢 Активный" if brick.status == BRICK_STATUS_ACTIVE else "🗄 В архиве"
    text = (
        f"🧱 <b>{_h(brick.title)}</b>\n\n"
        f"Обозначение: <code>[{_h(brick.code)}]</code>\n"
        f"Тип: {_h(_type_title(brick.value_type))}\n"
        f"Описание: {_description_html(brick.description)}\n"
        "Область: личная (видна только вам)\n"
        f"Статус: {state_label}\n"
        f"Настройки: {_h(_settings_text(brick.value_type, brick.rules))}\n\n"
        "Это только определение поля. Значение появится, когда кирпичик "
        "будет использован в шаблоне анонса; сейчас он не вставляется в "
        "посты автоматически."
    )
    if brick.status == BRICK_STATUS_ACTIVE:
        rows = [
            [InlineKeyboardButton("✏️ Изменить", callback_data="bk_edit")],
            [InlineKeyboardButton("📦 В архив", callback_data="bk_archive")],
        ]
    else:
        rows = [[InlineKeyboardButton("♻️ Восстановить", callback_data="bk_restore")]]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_back")])
    return text, InlineKeyboardMarkup(rows)


def _archive_confirm_content(brick):
    text = (
        "📦 <b>Переместить кирпичик в архив?</b>\n\n"
        f"Название: {_h(brick.title)}\n"
        f"Обозначение: <code>[{_h(brick.code)}]</code>\n\n"
        "Он исчезнет из активного списка, но останется у вас в архиве. "
        "Восстановить его можно в любой момент. Технический код останется "
        "занятым, чтобы старые и будущие ссылки не стали неоднозначными."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Да, в архив", callback_data="bk_archive_confirm"
                )
            ],
            [InlineKeyboardButton("◀️ Нет, назад", callback_data="bk_back")],
        ]
    )


_PROPOSAL_STATUS_UI = {
    PROPOSAL_STATUS_PENDING: "⏳ Ожидает решения",
    PROPOSAL_STATUS_APPROVED: "✅ Одобрено",
    PROPOSAL_STATUS_REJECTED: "❌ Отклонено",
    PROPOSAL_STATUS_WITHDRAWN: "↩️ Отозвано",
}


def _proposal_status_ui(status: str) -> str:
    return _PROPOSAL_STATUS_UI.get(status, status)


def _proposal_date(value: datetime | None) -> str:
    if not isinstance(value, datetime):
        return "—"
    return value.strftime("%d.%m.%Y %H:%M")


def _proposal_target_text(proposal: BrickProposalSnapshot) -> str:
    if proposal.scope == PROPOSAL_SCOPE_SYSTEM:
        return "вся система"
    return f"место «{proposal.place_name or f'#{proposal.place_id}'}»"


# Хлебная крошка раздела предложений (R4 №24): человек всегда видит, где он
# находится, и не путает предложения со списком кирпичиков.
PROPOSAL_CRUMB = "🧱 Кирпичики → 📨 Предложения"


def _proposal_header(title: str, step: int | None = None) -> str:
    """
    Заголовок экрана предложения с крошкой и номером шага.

    Шаг указывается только у мастер-экранов (1–4): у списка и карточки шага
    нет, и выдумывать его не нужно.
    """
    suffix = f" · шаг {step} из {PROPOSAL_STEPS}" if step is not None else ""
    return f"{PROPOSAL_CRUMB}\n\n📨 <b>{title}</b>{suffix}"


def _proposal_back_label(state: dict) -> str:
    """
    Подпись «Назад» для экрана области (R4 №24).

    U3a №8: мастер всегда запускается из строки хаба (`bp_start:<id>`), шаг
    выбора кирпичика пропущен — «назад» возвращает в хаб, а не на экран
    «выберите тот же кирпичик ещё раз». Ветка для старых сессий без метки
    входа оставлена, чтобы сохранённое состояние не врало подписью.
    """
    if state.get("proposal_entry") == "row":
        return "◀️ К кирпичикам"
    return "◀️ К выбору кирпичика"


def _proposal_page_note(offset: int, count: int, total: int) -> str:
    if total <= 0:
        return ""
    last = offset + count
    pages = max((total + PROPOSAL_PAGE_SIZE - 1) // PROPOSAL_PAGE_SIZE, 1)
    page = offset // PROPOSAL_PAGE_SIZE + 1
    return f"Показано {offset + 1}–{last} из {total}. Страница {page} из {pages}."


def _proposal_reset_hub(state: dict, notice: str) -> None:
    page, archive_page = _saved_page(state)
    state.clear()
    state.update(_new_hub(page=page, archive_page=archive_page))
    state["_proposal_notice"] = notice


def _proposal_notice_after_reset(state: dict, notice: str) -> None:
    state["_proposal_notice"] = notice


def _proposal_brick(session: Session, user: User, state: dict):
    try:
        brick_id = int(state.get("proposal_source_brick_id") or 0)
    except (TypeError, ValueError):
        return None
    if brick_id <= 0:
        return None
    return get_personal_brick(
        session,
        user.telegram_user_id,
        brick_id,
        include_archived=False,
    )


def _proposal_select_brick_content(session: Session, user: User, state: dict):
    """
    Шаг выбора личного кирпичика — совместимость со старыми сессиями.

    U3a №8: общей кнопки «📨 Предложить кирпичик» на хабе больше нет, поэтому
    этот экран из интерфейса недостижим: предложение запускается только из
    строки конкретного кирпичика (`bp_start:<id>`). Функция остаётся, чтобы
    сохранённая ранее сессия в фазе `proposal_select_brick` не превратилась
    в тупик после обновления: человек видит список и может продолжить.
    """
    total = count_personal_bricks(session, user.telegram_user_id)
    if total <= 0:
        _proposal_reset_hub(
            state,
            "Сначала создайте личный кирпичик — затем его можно будет "
            "предложить месту или всей системе.",
        )
        return None
    offset = _page_value(state.get("proposal_page"))
    if offset >= total:
        offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
    bricks = list_personal_bricks(
        session,
        user.telegram_user_id,
        limit=PROPOSAL_PAGE_SIZE,
        offset=offset,
    )
    state["proposal_page"] = offset
    text = (
        _proposal_header("Выберите кирпичик")
        + "\n\nВыберите свой личный кирпичик, который хотите предложить для "
        "места или всей системы.\n\n"
        "Будет отправлен снимок текущих названия, кода, типа и описания. "
        "Исходный личный кирпичик и его владелец не изменятся.\n\n"
        + _proposal_page_note(offset, len(bricks), total)
    )
    rows = [
        [
            keyboard_layouts.button(
                _button_title(brick),
                f"bp_source:{brick.id}",
            )
        ]
        for brick in bricks
    ]
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button(
                "◀️", f"bp_page:{max(offset - PROPOSAL_PAGE_SIZE, 0)}"
            )
            if offset > 0
            else None
        ),
        (
            keyboard_layouts.button("▶️", f"bp_page:{offset + PROPOSAL_PAGE_SIZE}")
            if offset + len(bricks) < total
            else None
        ),
    )
    rows.append([InlineKeyboardButton("◀️ К кирпичикам", callback_data="bp_back")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_scope_content(session: Session, user: User, state: dict):
    brick = _proposal_brick(session, user, state)
    if brick is None:
        _proposal_reset_hub(
            state,
            "Выбранный кирпичик больше недоступен. Начните предложение заново.",
        )
        return None
    text = (
        _proposal_header("Куда предложить кирпичик?", step=1) + "\n\n"
        f"Кирпичик: <b>{_h(brick.title)}</b>\n"
        f"Обозначение: <code>[{_h(brick.code)}]</code>\n\n"
        "Выберите область:\n"
        "• конкретное место — рассматривает действующий администратор этого "
        "места или суперадминистратор;\n"
        "• вся система — рассматривает только суперадминистратор.\n\n"
        "Новое место автоматически не создаётся."
    )
    # Две цели — рядом в одной строке: это выбор из двух равноправных
    # вариантов, а не список. «Назад» остаётся отдельной строкой ниже.
    rows = keyboard_layouts.pair(
        keyboard_layouts.button("📍 Место", "bp_target:place"),
        keyboard_layouts.button("🌍 Вся система", "bp_target:system"),
    )
    rows.append(
        [
            InlineKeyboardButton(
                _proposal_back_label(state),
                callback_data="bp_back",
            )
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _proposal_select_place_content(session: Session, user: User, state: dict):
    brick = _proposal_brick(session, user, state)
    if brick is None:
        _proposal_reset_hub(
            state,
            "Выбранный кирпичик больше недоступен. Начните предложение заново.",
        )
        return None
    offset = _page_value(state.get("proposal_place_page"))
    places, total = list_places(session, offset=offset, limit=PROPOSAL_PAGE_SIZE)
    if total and offset >= total:
        offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
        places, total = list_places(session, offset=offset, limit=PROPOSAL_PAGE_SIZE)
    state["proposal_place_page"] = offset
    text = (
        _proposal_header("Выберите место", step=2) + "\n\n"
        f"Кирпичик: <b>{_h(brick.title)}</b> "
        f"<code>[{_h(brick.code)}]</code>\n\n"
        "Выберите место из существующего справочника. Новое место здесь "
        "не создаётся."
    )
    rows = [
        [
            InlineKeyboardButton(
                _short(place.canonical_name, 56),
                callback_data=f"bp_place:{place.place_id}",
            )
        ]
        for place in places
    ]
    if not places:
        text += (
            "\n\nВ справочнике пока нет мест. Вернитесь назад и выберите "
            "всю систему."
        )
    if total:
        text += "\n\n" + _proposal_page_note(offset, len(places), total)
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=(f"bp_ppage:{max(offset - PROPOSAL_PAGE_SIZE, 0)}"),
            )
        )
    if offset + len(places) < total:
        navigation.append(
            InlineKeyboardButton(
                "▶️", callback_data=f"bp_ppage:{offset + PROPOSAL_PAGE_SIZE}"
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("◀️ К области", callback_data="bp_back")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_preview_content(session: Session, user: User, state: dict):
    brick = _proposal_brick(session, user, state)
    if brick is None:
        _proposal_reset_hub(
            state,
            "Выбранный кирпичик больше недоступен. Снимок не отправлен.",
        )
        return None
    scope = state.get("proposal_scope")
    place_id: int | None = None
    if scope == PROPOSAL_SCOPE_PLACE:
        try:
            place_id = int(state.get("proposal_place_id") or 0)
        except (TypeError, ValueError):
            place_id = 0
        details = get_place_details(session, place_id) if place_id > 0 else None
        if details is None:
            state["phase"] = "proposal_select_place"
            _proposal_notice_after_reset(
                state, "Место не найдено в справочнике. Выберите другое."
            )
            return _proposal_select_place_content(session, user, state)
        target = f"место «{_h(details.canonical_name)}»"
    elif scope == PROPOSAL_SCOPE_SYSTEM:
        place_id = None
        target = "вся система"
    else:
        state["phase"] = "proposal_scope"
        _proposal_notice_after_reset(
            state, "Область предложения не выбрана. Выберите её заново."
        )
        return _proposal_scope_content(session, user, state)

    availability = check_code_availability(
        session,
        brick.code,
        scope=scope,
        place_id=place_id,
        owner_user_id=None,
        exclude_brick_id=brick.id,
    )
    availability_text = "✅ Код в целевой области свободен."
    if not availability.available:
        availability_text = (
            "⚠️ <b>Отправить сейчас нельзя:</b> "
            f"{_h(availability.message or 'код уже занят в целевой области')}.\n"
            "Код не будет изменён автоматически. Исправьте код в личном "
            "кирпичике или выберите другую область."
        )
    text = (
        _proposal_header("Проверьте предложение", step=3) + "\n\n"
        f"Название: <b>{_h(brick.title)}</b>\n"
        f"Обозначение: <code>[{_h(brick.code)}]</code>\n"
        f"Тип: {_h(_type_title(brick.value_type))}\n"
        f"Описание: {_description_html(brick.description)}\n"
        f"Настройки: {_h(_settings_text(brick.value_type, brick.rules))}\n"
        f"Цель: {target}\n\n"
        f"{availability_text}\n\n"
        "После отправки будет сохранён неизменяемый снимок. Последующие "
        "правки или архивация личного кирпичика не изменят ожидающее "
        "предложение. Исходный кирпичик не копируется и не перепривязывается."
    )
    rows: list[list[InlineKeyboardButton]] = []
    if availability.available:
        rows.append([InlineKeyboardButton("📨 Отправить", callback_data="bp_send")])
    scope = state.get("proposal_scope")
    back_label = "◀️ К местам" if scope == PROPOSAL_SCOPE_PLACE else "◀️ К области"
    rows.append([InlineKeyboardButton(back_label, callback_data="bp_back")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_list_content(session: Session, user: User, state: dict):
    offset = _page_value(state.get("proposal_page"))
    proposals, total = list_own_proposals(
        session,
        user.telegram_user_id,
        offset=offset,
        limit=PROPOSAL_PAGE_SIZE,
    )
    if total and offset >= total:
        offset = ((total - 1) // PROPOSAL_PAGE_SIZE) * PROPOSAL_PAGE_SIZE
        proposals, total = list_own_proposals(
            session,
            user.telegram_user_id,
            offset=offset,
            limit=PROPOSAL_PAGE_SIZE,
        )
    state["proposal_page"] = offset
    text = (
        _proposal_header("Мои предложения") + "\n\n"
        "Здесь хранится история ваших предложений. Ожидающие предложения "
        "не имеют срока действия: их можно отозвать в любой момент, пока "
        "администратор не принял решение."
    )
    if not total:
        # U3a: у пользователя без активных кирпичиков кнопки «📨» нет —
        # подсказка ведёт к созданию кирпичика.
        if count_personal_bricks(session, user.telegram_user_id) > 0:
            text += (
                "\n\nВы ещё не отправляли предложений. Откройте «🧱 Кирпичики» и "
                "нажмите «📨» в строке кирпичика, который хотите предложить."
            )
        else:
            text += (
                "\n\nВы ещё не отправляли предложений. Создайте кирпичик кнопкой "
                "«➕ Создать кирпичик» в разделе «Мои кирпичики» — после этого "
                "его можно будет предложить месту или всей системе."
            )
    else:
        text += "\n\n" + _proposal_page_note(offset, len(proposals), total)
    rows = keyboard_layouts.list_rows(
        (
            keyboard_layouts.button(
                _short(
                    f"{_proposal_status_ui(proposal.status)} · "
                    f"{proposal.title} — [{proposal.code}]",
                    60,
                ),
                f"bp_open:{proposal.id}",
            ),
            None,
        )
        for proposal in proposals
    )
    rows += keyboard_layouts.nav_row(
        (
            keyboard_layouts.button(
                "◀️", f"bp_mpage:{max(offset - PROPOSAL_PAGE_SIZE, 0)}"
            )
            if offset > 0
            else None
        ),
        (
            keyboard_layouts.button("▶️", f"bp_mpage:{offset + PROPOSAL_PAGE_SIZE}")
            if offset + len(proposals) < total
            else None
        ),
    )
    rows.append([InlineKeyboardButton("◀️ К кирпичикам", callback_data="bp_back")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_description(value: object) -> str:
    """
    Описание в карточке предложения (R4 №29): моноширинно, с «—» при отсутствии.

    У предложений, отправленных до миграции 20, описания в снимке нет —
    карточка честно показывает «—», а не пустую строку.
    """
    text = " ".join(str(value or "").split())
    if not text:
        return "—"
    return f"<code>{_h(text)}</code>"


def _proposal_card_content(proposal: BrickProposalSnapshot):
    parts = [
        _proposal_header("Предложение кирпичика"),
        "",
        f"Название: <b>{_h(proposal.title)}</b>",
        f"Обозначение: <code>[{_h(proposal.code)}]</code>",
        f"Тип: {_h(_type_title(proposal.value_type))}",
        f"Описание: {_proposal_description(proposal.description)}",
        f"Автор: {_h(proposal.author_label)}",
        f"Цель: {_h(_proposal_target_text(proposal))}",
        f"Статус: {_proposal_status_ui(proposal.status)}",
        f"Настройки: {_h(_settings_text(proposal.value_type, proposal.rules))}",
        f"Отправлено: {_proposal_date(proposal.created_at)}",
    ]
    if proposal.decided_at is not None:
        parts.append(f"Решение: {_proposal_date(proposal.decided_at)}")
    if proposal.status == PROPOSAL_STATUS_APPROVED:
        parts.append(
            "Создан отдельный кирпичик в целевой области. Исходный личный "
            "кирпичик не изменён."
        )
    elif proposal.status == PROPOSAL_STATUS_REJECTED:
        if proposal.decision_note:
            parts.append(f"Причина: {_h(proposal.decision_note)}")
        else:
            parts.append("Предложение отклонено без изменения личного кирпичика.")
    elif proposal.status == PROPOSAL_STATUS_WITHDRAWN:
        parts.append("Предложение отозвано вами и больше не ожидает решения.")
    else:
        parts.append(
            "Предложение ожидает решения. Срок действия не ограничен; "
            "пока решение не принято, его можно отозвать."
        )
    parts.extend(
        [
            "",
            "Предложение хранит снимок на момент отправки. Ваши последующие "
            "правки личного кирпичика его не меняют.",
        ]
    )
    rows = keyboard_layouts.danger_row(
        keyboard_layouts.button("🗑 Отозвать", "bp_withdraw")
        if proposal.status == PROPOSAL_STATUS_PENDING
        else None
    )
    rows.append([InlineKeyboardButton("◀️ К предложениям", callback_data="bp_back")])
    return "\n".join(parts), InlineKeyboardMarkup(rows)


def _proposal_withdraw_confirm_content(proposal: BrickProposalSnapshot):
    text = (
        _proposal_header("Отозвать предложение?") + "\n\n"
        f"Название: {_h(proposal.title)}\n"
        f"Обозначение: <code>[{_h(proposal.code)}]</code>\n"
        f"Цель: {_h(_proposal_target_text(proposal))}\n\n"
        "После отзыва администратор больше не сможет его одобрить. Личный "
        "кирпичик останется у вас без изменений. Новое предложение можно "
        "будет отправить позже."
    )
    # Необратимое действие — отдельной строкой над безопасным «назад».
    rows = keyboard_layouts.danger_row(
        keyboard_layouts.button("✅ Отозвать", "bp_withdraw_yes")
    )
    rows.append([InlineKeyboardButton("◀️ К карточке", callback_data="bp_back")])
    return text, InlineKeyboardMarkup(rows)


def _proposal_content(
    session: Session, user: User, state: dict
) -> tuple[str, InlineKeyboardMarkup] | None:
    phase = state.get("phase")
    if phase == "proposal_select_brick":
        return _proposal_select_brick_content(session, user, state)
    if phase == "proposal_scope":
        return _proposal_scope_content(session, user, state)
    if phase == "proposal_select_place":
        return _proposal_select_place_content(session, user, state)
    if phase == "proposal_preview":
        return _proposal_preview_content(session, user, state)
    if phase == "proposal_list":
        return _proposal_list_content(session, user, state)
    if phase == "proposal_card":
        proposal = get_own_proposal(
            session,
            user.telegram_user_id,
            int(state.get("proposal_id") or 0),
        )
        if proposal is None:
            state["phase"] = "proposal_list"
            _proposal_notice_after_reset(
                state, "Предложение не найдено или больше вам не доступно."
            )
            return _proposal_list_content(session, user, state)
        return _proposal_card_content(proposal)
    if phase == "proposal_withdraw_confirm":
        proposal = get_own_proposal(
            session,
            user.telegram_user_id,
            int(state.get("proposal_id") or 0),
        )
        if proposal is None:
            state["phase"] = "proposal_list"
            _proposal_notice_after_reset(
                state, "Предложение не найдено или больше вам не доступно."
            )
            return _proposal_list_content(session, user, state)
        if proposal.status != PROPOSAL_STATUS_PENDING:
            state["phase"] = "proposal_card"
            _proposal_notice_after_reset(
                state,
                "Предложение уже не ожидает решения; отзыв невозможен.",
            )
            return _proposal_card_content(proposal)
        return _proposal_withdraw_confirm_content(proposal)
    return None


def _edit_menu_content(state: dict):
    draft = _draft(state)
    value_type = draft.get("value_type") or ""
    description = draft.get("description") or ""
    rows = [
        [InlineKeyboardButton("✏️ Название", callback_data="bk_edit_title")],
        [InlineKeyboardButton("🔤 Обозначение", callback_data="bk_edit_code")],
        [
            InlineKeyboardButton(
                "📝 Описание: есть" if description else "📝 Добавить описание",
                callback_data="bk_edit_description",
            )
        ],
    ]
    if value_type in {BRICK_TYPE_NUMBER, BRICK_TYPE_CHOICE}:
        rows.append(
            [InlineKeyboardButton("⚙️ Настройки", callback_data="bk_edit_settings")]
        )
    rows.append([InlineKeyboardButton("👁 К проверке", callback_data="bk_preview")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_back")])
    text = (
        "✏️ <b>Изменить кирпичик</b>\n\n"
        f"Название: {_h(draft.get('title') or '—')}\n"
        f"Обозначение: <code>[{_h(draft.get('code') or '—')}]</code>\n"
        f"Тип: {_h(_type_title(value_type))}\n"
        f"Описание: {_description_html(draft.get('description'))}\n"
        f"Настройки: {_h(_settings_text(value_type, draft.get('rules') or {}))}\n\n"
        "Тип после создания не меняется: для другого типа создайте новый "
        "кирпичик. Идентификатор сохраняется при любых изменениях."
    )
    return text, InlineKeyboardMarkup(rows)


THREE_FIELDS_HINT = (
    "Три поля кирпичика:\n"
    "• <b>Название</b> — что человек видит в списках и на экране вопроса.\n"
    "• <b>Обозначение</b> — короткая вставка <code>[код]</code> в шаблон "
    "анонса.\n"
    "• <b>Описание</b> — инструкция автору анонса: её видно при заполнении "
    "поля."
)


def _title_input_content(state: dict):
    draft = _draft(state)
    if state.get("mode") == "edit":
        text = (
            "✏️ <b>Название кирпичика</b>\n\n"
            f"Сейчас: {_h(draft.get('title') or '—')}\n\n"
            "Введите новое человекочитаемое название — то, что человек видит "
            "в списках и на экране вопроса. Оно может повторять названия "
            "других кирпичиков: однозначность обеспечивает техническое "
            "обозначение."
        )
    else:
        text = (
            "1️⃣ <b>Как назвать кирпичик?</b>\n\n"
            "Например: «Ведущий мероприятия», «Формат участия» или "
            "«Стоимость билета».\n\n"
            "Название видно в списках и на экране вопроса — это то, что "
            "прочитает человек, заполняющий анонс. Техническое обозначение и "
            "описание зададим следующими шагами."
        )
    text += "\n\n" + THREE_FIELDS_HINT
    return text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад", callback_data="bk_back")]]
    )


def _code_input_content(state: dict):
    draft = _draft(state)
    code = draft.get("code") or ""
    if state.get("mode") == "edit":
        intro = "Введите новое техническое обозначение."
    else:
        intro = (
            "Предлагаю обозначение для будущих шаблонов. Можно согласиться "
            "или отправить другое."
        )
    current = (
        f"Сейчас: <code>[{_h(code)}]</code>\n\n"
        if code
        else "Обозначение ещё не задано.\n\n"
    )
    text = (
        "2️⃣ <b>Техническое обозначение</b>\n\n"
        f"{intro}\n\n"
        f"{current}"
        "Это короткая вставка <code>[код]</code>, которую автор шаблона "
        "пишет в тексте анонса вместо самого значения. От 2 до 64 символов: "
        "буквы, цифры, «_» и «-». Без пробелов и квадратных скобок.\n\n"
        "Если код уже занят, бот объяснит конфликт и попросит другой."
    )
    rows: list[list[InlineKeyboardButton]] = []
    if code:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Оставить [{_short(code, 28)}]",
                    callback_data="bk_accept_code",
                )
            ]
        )
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_back")])
    return text, InlineKeyboardMarkup(rows)


def _description_input_content(state: dict):
    """Ввод описания: инструкция автору анонса, видимая на вопросе."""
    draft = _draft(state)
    current = draft.get("description") or ""
    shown = f"Сейчас: {_h(_short(current, 200))}\n\n" if current else ""
    text = (
        "3️⃣ <b>Описание для автора анонса</b>\n\n"
        f"{shown}"
        "Напишите подсказку тому, кто будет заполнять это поле: что именно "
        "указать и в каком виде. Например: «Имя и фамилия ведущего, без "
        "должности» или «Цена в рублях, только число».\n\n"
        "Описание видно на экране вопроса — над полем ввода. В текст "
        "анонса оно не попадает и на шаблон не влияет.\n\n"
        "Поле необязательное: без описания кирпичик работает как обычно."
    )
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("⏭ Без описания", callback_data="bk_skip_description")]
    ]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_back")])
    return text, InlineKeyboardMarkup(rows)


def _type_select_content(state: dict):
    draft = _draft(state)
    current = draft.get("value_type") or ""
    lines = [
        "4️⃣ <b>Выберите тип кирпичика</b>",
        "",
        "Тип определяет, какое значение будет допустимо в будущем. "
        "Уже сохранённый тип изменить нельзя.",
        "",
    ]
    for value_type in BRICK_TYPES:
        lines.append(
            f"• <b>{_h(TYPE_UI_TITLES[value_type])}</b> — "
            f"{_h(TYPE_UI_HINTS[value_type])}"
        )
    if current:
        lines.extend(["", f"Сейчас выбран: <b>{_h(_type_title(current))}</b>."])
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(0, len(BRICK_TYPES), 2):
        row = [
            InlineKeyboardButton(
                TYPE_UI_TITLES[value_type], callback_data=f"bk_type:{value_type}"
            )
            for value_type in BRICK_TYPES[index : index + 2]
        ]
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="bk_back")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _number_settings_content(state: dict):
    draft = _draft(state)
    rules = draft.get("rules") or {}
    integer = "да" if rules.get("integer") else "нет"
    minimum = _format_number(rules["min"]) if "min" in rules else "не задан"
    maximum = _format_number(rules["max"]) if "max" in rules else "не задан"
    suffix = f"«{rules['suffix']}»" if "suffix" in rules else "не задана"
    text = (
        "⚙️ <b>Настройки числа</b>\n\n"
        f"Только целые: <b>{integer}</b>\n"
        f"Минимум: <b>{_h(minimum)}</b>\n"
        f"Максимум: <b>{_h(maximum)}</b>\n"
        f"Подпись: <b>{_h(suffix)}</b>\n\n"
        "Все ограничения необязательны. Это настройки определения, а не "
        "значение для конкретного анонса."
    )
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                f"🔢 Только целые: {integer}", callback_data="bk_num:integer"
            )
        ],
        [InlineKeyboardButton(f"Минимум: {minimum}", callback_data="bk_num:set_min")],
        [InlineKeyboardButton(f"Максимум: {maximum}", callback_data="bk_num:set_max")],
        [InlineKeyboardButton(f"Подпись: {suffix}", callback_data="bk_num:set_suffix")],
    ]
    clear_row: list[InlineKeyboardButton] = []
    if "min" in rules:
        clear_row.append(
            InlineKeyboardButton("✖️ Минимум", callback_data="bk_num:clear_min")
        )
    if "max" in rules:
        clear_row.append(
            InlineKeyboardButton("✖️ Максимум", callback_data="bk_num:clear_max")
        )
    if "suffix" in rules:
        clear_row.append(
            InlineKeyboardButton("✖️ Подпись", callback_data="bk_num:clear_suffix")
        )
    if clear_row:
        rows.append(clear_row)
    rows.append(
        [
            InlineKeyboardButton("✅ Готово", callback_data="bk_num:done"),
            InlineKeyboardButton("◀️ Назад", callback_data="bk_back"),
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _choice_settings_content(state: dict):
    draft = _draft(state)
    rules = draft.get("rules") or {}
    choices = [str(item) for item in (rules.get("choices") or [])]
    multiple = "да" if rules.get("multiple") else "нет"
    if choices:
        lines = ["", "Варианты:", *[f"• {_h(item)}" for item in choices]]
        hint = "Нажмите вариант на клавиатуре, чтобы удалить его."
    else:
        lines = ["", "Список вариантов пока пуст."]
        hint = "Добавьте хотя бы один вариант."
    text = (
        "🎛 <b>Варианты выбора</b>\n"
        + "\n".join(lines)
        + f"\n\nМожно выбрать несколько: <b>{multiple}</b>.\n{hint}"
    )
    rows: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(choices):
        rows.append(
            [
                InlineKeyboardButton(
                    f"❌ {_short(item, 44)}",
                    callback_data=f"bk_choice_remove:{index}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton("➕ Добавить вариант", callback_data="bk_choice_add")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                f"Можно несколько: {multiple}",
                callback_data="bk_choice_multiple",
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("✅ Готово", callback_data="bk_choice_done"),
            InlineKeyboardButton("◀️ Назад", callback_data="bk_back"),
        ]
    )
    return text, InlineKeyboardMarkup(rows)


def _preview_content(state: dict):
    draft = _draft(state)
    value_type = draft.get("value_type") or ""
    mode = state.get("mode")
    title = draft.get("title") or "—"
    code = draft.get("code") or "—"
    text = (
        "👁 <b>Проверьте кирпичик</b>\n\n"
        f"Название: {_h(title)}\n"
        f"Обозначение: <code>[{_h(code)}]</code>\n"
        f"Тип: {_h(_type_title(value_type))}\n"
        f"Описание: {_description_html(draft.get('description'))}\n"
        "Область: личная (видна только вам)\n"
        f"Настройки: {_h(_settings_text(value_type, draft.get('rules') or {}))}\n\n"
        "После сохранения кирпичик появится в вашем списке. В посты и "
        "шаблоны он пока не вставляется."
    )
    if mode == "edit":
        text += "\n\nТип при редактировании не меняется."
    rows = [
        [InlineKeyboardButton("💾 Сохранить", callback_data="bk_save")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="bk_change")],
        [InlineKeyboardButton("✖️ Отмена", callback_data="bk_cancel")],
    ]
    return text, InlineKeyboardMarkup(rows)


# --- Отрисовка и сохранение состояния --------------------------------------


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
    Сохраняет состояние, рисует экран, затем best-effort чистит ввод.

    Пользовательский текст удаляется только после подтверждённой записи
    состояния. При ошибке БД экран всё равно обновляется, а ввод остаётся.
    """
    user_id = ctx.user(update).id
    body = f"{notice}\n\n{text}" if notice else text
    saved = save_brick_session(user_id, state)
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


async def _stale(update: Update, text: str = STALE_TEXT) -> None:
    await ctx.query(update).answer(text, show_alert=True)


async def _render_phase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
) -> bool:
    """Единая точка отрисовки текущей фазы, включая восстановление."""
    phase = state.get("phase")
    if phase not in PHASES:
        page, archive_page = _saved_page(state)
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
        notice = notice or "Черновик не найден — раздел открыт заново."
        phase = "hub"
    if phase in _WIZARD_PHASES and not _valid_wizard_state(state):
        page, archive_page = _saved_page(state)
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
        notice = notice or "Черновик кирпичика повреждён — начните заново."
        phase = "hub"

    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        text: str
        keyboard: InlineKeyboardMarkup
        if phase == "hub":
            text, keyboard = _hub_content(session, user, state)
        elif phase == "archive":
            content = _archive_content(session, user, state)
            if content is None:
                page, archive_page = _saved_page(state)
                state.clear()
                state.update(_new_hub(page=page, archive_page=archive_page))
                text, keyboard = _hub_content(session, user, state)
                notice = notice or "В архиве пока нет кирпичиков."
            else:
                text, keyboard = content
        elif phase == "card":
            brick = get_personal_brick(
                session,
                user.telegram_user_id,
                int(state.get("brick_id") or 0),
            )
            if brick is None:
                page, archive_page = _saved_page(state)
                state.clear()
                state.update(_new_hub(page=page, archive_page=archive_page))
                text, keyboard = _hub_content(session, user, state)
                notice = notice or "Кирпичик не найден или больше вам не доступен."
            else:
                state["revision"] = brick.revision
                text, keyboard = _card_content(brick)
        elif phase == "archive_confirm":
            brick = get_personal_brick(
                session,
                user.telegram_user_id,
                int(state.get("brick_id") or 0),
            )
            if brick is None:
                page, archive_page = _saved_page(state)
                state.clear()
                state.update(_new_hub(page=page, archive_page=archive_page))
                text, keyboard = _hub_content(session, user, state)
                notice = notice or "Кирпичик не найден."
            elif brick.status != BRICK_STATUS_ACTIVE:
                state["phase"] = "card"
                state["revision"] = brick.revision
                text, keyboard = _card_content(brick)
                notice = notice or "Кирпичик уже не активен."
            else:
                state["revision"] = brick.revision
                text, keyboard = _archive_confirm_content(brick)
        elif phase == "edit_menu":
            text, keyboard = _edit_menu_content(state)
        elif phase == "title_input":
            text, keyboard = _title_input_content(state)
        elif phase == "code_input":
            text, keyboard = _code_input_content(state)
        elif phase == "description_input":
            text, keyboard = _description_input_content(state)
        elif phase == "type_select":
            text, keyboard = _type_select_content(state)
        elif phase == "number_settings":
            text, keyboard = _number_settings_content(state)
        elif phase in {"number_min_input", "number_max_input", "number_suffix_input"}:
            draft = _draft(state)
            rules = draft.get("rules") or {}
            if phase == "number_min_input":
                current = _format_number(rules["min"]) if "min" in rules else "не задан"
                prompt = "Введите минимальное число или нажмите «Назад»."
            elif phase == "number_max_input":
                current = _format_number(rules["max"]) if "max" in rules else "не задан"
                prompt = "Введите максимальное число или нажмите «Назад»."
            else:
                current = f"«{rules['suffix']}»" if "suffix" in rules else "не задана"
                prompt = "Введите подпись, например «₽» или «чел.»."
            text = (
                "⚙️ <b>Настройка числа</b>\n\n"
                f"Сейчас: <b>{_h(current)}</b>\n\n{prompt}"
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="bk_back")]]
            )
        elif phase == "choice_settings":
            text, keyboard = _choice_settings_content(state)
        elif phase == "choice_input":
            text = (
                "🎛 <b>Новый вариант выбора</b>\n\n"
                "Отправьте название варианта, например «Очно» или «Онлайн»."
            )
            keyboard = InlineKeyboardMarkup(
                [[InlineKeyboardButton("◀️ Назад", callback_data="bk_back")]]
            )
        elif phase in _PROPOSAL_PHASES:
            content = _proposal_content(session, user, state)
            if content is None:
                text, keyboard = _hub_content(session, user, state)
            else:
                text, keyboard = content
        else:  # preview
            text, keyboard = _preview_content(state)
        generated_notice = state.pop("_proposal_notice", None)
        if generated_notice:
            notice = notice or generated_notice
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


# --- Вход и список ----------------------------------------------------------


async def open_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в раздел: кнопка меню, /bricks или возврат после рестарта."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    user_id = ctx.user(update).id
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_template_session(user_id)
    state = load_brick_session(user_id)
    if not isinstance(state, dict) or state.get("phase") not in PHASES:
        state = _new_hub()
    await _render_phase(update, context, state, drop_input=True)


async def handle_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"hub", "archive"}:
        await _stale(update)
        return
    await ctx.query(update).answer()
    if state.get("phase") == "archive":
        page, archive_page = _saved_page(state)
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
    await _render_phase(update, context, state)


async def handle_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "hub":
        await _stale(update)
        return
    await ctx.query(update).answer()
    page, archive_page = _saved_page(state)
    await _render_phase(
        update,
        context,
        _new_create_state(page=page, archive_page=archive_page),
        drop_input=True,
    )


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "hub":
        await _stale(update)
        return
    await ctx.query(update).answer()
    data = ctx.query(update).data or ""
    try:
        offset = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    state["page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_archive_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "hub":
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "archive"
    state["archive_page"] = 0
    await _render_phase(update, context, state)


async def handle_archive_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "archive":
        await _stale(update)
        return
    await ctx.query(update).answer()
    data = ctx.query(update).data or ""
    try:
        offset = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    state["archive_page"] = max(offset, 0)
    await _render_phase(update, context, state)


async def handle_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {"hub", "archive"}:
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""
    try:
        brick_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await _stale(update)
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        brick = get_personal_brick(session, user.telegram_user_id, brick_id)
    finally:
        session.close()
    if brick is None:
        if state.get("phase") == "archive":
            await _render_phase(
                update,
                context,
                state,
                notice="Кирпичик не найден. Возможно, он уже недоступен.",
            )
        else:
            await _render_phase(
                update,
                context,
                state,
                notice="Кирпичик не найден. Возможно, он уже недоступен.",
            )
        return
    state["phase"] = "card"
    state["brick_id"] = brick.id
    state["revision"] = brick.revision
    state["origin"] = "archive" if brick.status == BRICK_STATUS_ARCHIVED else "hub"
    await _render_phase(update, context, state)


# --- Предложения кирпичиков (Stage 26) -------------------------------------


def _proposal_parse_id(data: str) -> int | None:
    try:
        value = int(data.split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _proposal_parse_offset(data: str) -> int | None:
    try:
        value = int(data.split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        return None
    return max(value, 0)


def _proposal_new_state(state: dict, phase: str) -> None:
    page, archive_page = _saved_page(state)
    state.clear()
    state.update(
        {
            "phase": phase,
            "page": page,
            "archive_page": archive_page,
            "proposal_page": 0,
        }
    )


async def handle_proposal_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Вход в предложение кирпичика из строки списка (`bp_start:<id>`, №28).

    Строка хаба уже выбрала кирпичик, поэтому шаг выбора пропускается:
    человек сразу видит «куда предложить», а лишний экран «выберите то же
    самое ещё раз» был бы тупиком.

    U3a №8: общей кнопки `bp_start` без id на хабе больше нет — предложение
    запускается только из активного кирпичика. Старые сообщения с такой
    кнопкой ещё могут быть в чате, поэтому вызов без id не считается
    ошибкой: человека возвращают на хаб с подсказкой выбрать кирпичик.
    """
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "hub":
        await _stale(update)
        return
    query = ctx.query(update)
    data = query.data or ""
    source_id: int | None = None
    if data.startswith("bp_start:"):
        source_id = _proposal_parse_id(data)
        if source_id is None:
            await _stale(update)
            return
    if source_id is None:
        # Устаревшая общая кнопка: мягкий redirect на хаб, без «внутренней
        # ошибки» и без ложного мастера «выберите кирпичик заново».
        # U3a: на пустом хабе кнопки «📨» нет — подсказываем создание.
        session = SessionLocal()
        try:
            user = _ensure_user(session, update)
            has_active = count_personal_bricks(session, user.telegram_user_id) > 0
        finally:
            session.close()
        hint = PROPOSAL_ROW_ONLY_HINT if has_active else PROPOSAL_CREATE_HINT
        await query.answer(hint, show_alert=True)
        await _render_phase(update, context, state)
        return
    await query.answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        total = count_personal_bricks(session, user.telegram_user_id)
        brick = (
            get_personal_brick(
                session,
                user.telegram_user_id,
                source_id,
                include_archived=False,
            )
            if source_id is not None
            else None
        )
    finally:
        session.close()
    if total <= 0:
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Сначала создайте личный кирпичик — затем его можно будет "
                "предложить месту или всей системе."
            ),
        )
        return
    if source_id is not None and brick is None:
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Кирпичик не найден или уже в архиве. Выберите другой "
                "активный кирпичик."
            ),
        )
        return
    _proposal_new_state(state, "proposal_scope")
    # R4 №24: вход только из строки хаба, шаг выбора кирпичика пропущен —
    # «назад» на экране области ведёт в хаб.
    state["proposal_entry"] = "row"
    state["proposal_source_brick_id"] = brick.id
    state["proposal_place_page"] = 0
    await _render_phase(update, context, state, drop_input=True)


async def handle_proposal_mine(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "hub":
        await _stale(update)
        return
    await ctx.query(update).answer()
    _proposal_new_state(state, "proposal_list")
    await _render_phase(update, context, state, drop_input=True)


async def handle_proposal_source(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Выбор кирпичика на legacy-экране шага 1 (сохранённые старые сессии).

    U3a №8: из интерфейса этот шаг недостижим, поэтому обработчик остаётся
    только ради совместимости: сохранённая ранее сессия в фазе
    `proposal_select_brick` продолжает работать, а не превращается в тупик.
    """
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_select_brick":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    brick_id = _proposal_parse_id(query.data or "")
    if brick_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        brick = get_personal_brick(
            session,
            user.telegram_user_id,
            brick_id,
            include_archived=False,
        )
    finally:
        session.close()
    if brick is None:
        await _render_phase(
            update,
            context,
            state,
            notice=(
                "Кирпичик не найден или уже в архиве. Выберите другой "
                "активный кирпичик."
            ),
        )
        return
    state["proposal_source_brick_id"] = brick.id
    state["proposal_entry"] = "row"
    state.pop("proposal_scope", None)
    state.pop("proposal_place_id", None)
    state["proposal_place_page"] = 0
    state["phase"] = "proposal_scope"
    await _render_phase(update, context, state)


async def handle_proposal_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_scope":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""
    target = data.split(":", 1)[1] if ":" in data else ""
    if target == "place":
        state["proposal_scope"] = PROPOSAL_SCOPE_PLACE
        state["proposal_place_id"] = None
        state["proposal_place_page"] = 0
        state["phase"] = "proposal_select_place"
    elif target == "system":
        state["proposal_scope"] = PROPOSAL_SCOPE_SYSTEM
        state["proposal_place_id"] = None
        state["phase"] = "proposal_preview"
    else:
        await _stale(update)
        return
    await _render_phase(update, context, state)


async def handle_proposal_place(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_select_place":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    place_id = _proposal_parse_id(query.data or "")
    if place_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        details = get_place_details(session, place_id)
    finally:
        session.close()
    if details is None:
        await _render_phase(
            update,
            context,
            state,
            notice="Место не найдено в справочнике. Выберите другое.",
        )
        return
    state["proposal_scope"] = PROPOSAL_SCOPE_PLACE
    state["proposal_place_id"] = details.place_id
    state["phase"] = "proposal_preview"
    await _render_phase(update, context, state)


async def handle_proposal_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") not in {
        "proposal_select_brick",
        "proposal_select_place",
        "proposal_list",
    }:
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    offset = _proposal_parse_offset(query.data or "")
    if offset is None:
        await _stale(update)
        return
    phase = state.get("phase")
    if phase == "proposal_select_brick":
        state["proposal_page"] = offset
    elif phase == "proposal_select_place":
        state["proposal_place_page"] = offset
    else:
        state["proposal_page"] = offset
    await _render_phase(update, context, state)


async def handle_proposal_send(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_preview":
        await _stale(update)
        return
    await ctx.query(update).answer()
    source_brick_id = state.get("proposal_source_brick_id")
    scope = state.get("proposal_scope")
    place_id = state.get("proposal_place_id")
    if (
        not isinstance(source_brick_id, int)
        or isinstance(source_brick_id, bool)
        or scope not in {PROPOSAL_SCOPE_PLACE, PROPOSAL_SCOPE_SYSTEM}
    ):
        await _render_phase(
            update,
            context,
            state,
            notice="Данные предложения устарели. Выберите кирпичик заново.",
        )
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        snapshot = create_brick_proposal(
            session,
            actor_telegram_user_id=user.telegram_user_id,
            source_brick_id=source_brick_id,
            scope=scope,
            place_id=place_id if isinstance(place_id, int) else None,
        )
    except (ProposalError, ProposalActorError) as exc:
        await _render_phase(
            update,
            context,
            state,
            notice=str(exc),
        )
        return
    finally:
        session.close()
    _proposal_new_state(state, "proposal_card")
    state["proposal_id"] = snapshot.id
    await _render_phase(
        update,
        context,
        state,
        notice=(
            "Предложение отправлено на рассмотрение. Снимок сохранён; "
            "исходный кирпичик не изменён."
        ),
    )


async def handle_proposal_open_own(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_list":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    proposal_id = _proposal_parse_id(query.data or "")
    if proposal_id is None:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        proposal = get_own_proposal(session, user.telegram_user_id, proposal_id)
    finally:
        session.close()
    if proposal is None:
        await _render_phase(
            update,
            context,
            state,
            notice="Предложение не найдено или больше вам не доступно.",
        )
        return
    state["proposal_id"] = proposal.id
    state["phase"] = "proposal_card"
    await _render_phase(update, context, state)


async def handle_proposal_withdraw(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_card":
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "proposal_withdraw_confirm"
    await _render_phase(update, context, state)


async def handle_proposal_withdraw_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "proposal_withdraw_confirm":
        await _stale(update)
        return
    await ctx.query(update).answer()
    try:
        proposal_id = int(state.get("proposal_id") or 0)
    except (TypeError, ValueError):
        proposal_id = 0
    if proposal_id <= 0:
        await _stale(update)
        return
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        result = withdraw_brick_proposal(
            session,
            actor_telegram_user_id=user.telegram_user_id,
            proposal_id=proposal_id,
        )
    except ProposalActorError as exc:
        await _render_phase(update, context, state, notice=str(exc))
        return
    finally:
        session.close()
    if result.status == "not_found":
        state["phase"] = "proposal_list"
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Предложение не найдено.",
        )
        return
    if result.status == "error":
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Не удалось отозвать предложение.",
        )
        return
    state["phase"] = "proposal_card"
    if result.proposal is not None:
        state["proposal_id"] = result.proposal.id
    await _render_phase(
        update,
        context,
        state,
        notice=result.message or "Статус предложения обновлён.",
    )


async def handle_proposal_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") not in _PROPOSAL_PHASES:
        await _stale(update)
        return
    await ctx.query(update).answer()
    phase = state.get("phase")
    page, archive_page = _saved_page(state)
    if phase in {"proposal_select_brick", "proposal_list"}:
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
    elif phase == "proposal_scope":
        # R4 №24: из строки хаба шаг выбора кирпичика был пропущен — «назад»
        # возвращает в хаб, а не на экран повторного выбора того же кирпичика.
        if state.get("proposal_entry") == "row":
            state.clear()
            state.update(_new_hub(page=page, archive_page=archive_page))
        else:
            state["phase"] = "proposal_select_brick"
            state.pop("proposal_scope", None)
            state.pop("proposal_place_id", None)
    elif phase == "proposal_select_place":
        state["phase"] = "proposal_scope"
    elif phase == "proposal_preview":
        if state.get("proposal_scope") == PROPOSAL_SCOPE_PLACE:
            state["phase"] = "proposal_select_place"
        else:
            state["phase"] = "proposal_scope"
    elif phase == "proposal_card":
        state.pop("proposal_id", None)
        state["phase"] = "proposal_list"
    elif phase == "proposal_withdraw_confirm":
        state["phase"] = "proposal_card"
    else:
        await _stale(update)
        return
    await _render_phase(update, context, state)


async def handle_proposal_stale(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Устаревшая кнопка bp_*: отвечает, но ничего не меняет."""
    await _stale(update)


# --- Навигация назад и отмена ----------------------------------------------


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None:
        await _stale(update)
        return
    await ctx.query(update).answer()
    phase = state.get("phase")
    cleanup = _failed_take(state)
    page, archive_page = _saved_page(state)
    if phase == "archive":
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
    elif phase == "card":
        origin = state.get("origin")
        state.pop("mode", None)
        state.pop("draft", None)
        state.pop("brick_id", None)
        state.pop("revision", None)
        state.pop("failed_inputs", None)
        if origin == "archive":
            state.clear()
            state.update(_new_hub(page=page, archive_page=archive_page))
            state["phase"] = "archive"
        else:
            state.clear()
            state.update(_new_hub(page=page, archive_page=archive_page))
    elif phase == "archive_confirm":
        state["phase"] = "card"
    elif phase == "title_input":
        if state.get("mode") == "edit":
            state["phase"] = "edit_menu"
        else:
            state.clear()
            state.update(_new_hub(page=page, archive_page=archive_page))
    elif phase == "code_input":
        state["phase"] = "edit_menu" if state.get("mode") == "edit" else "title_input"
    elif phase == "description_input":
        state["phase"] = "edit_menu" if state.get("mode") == "edit" else "code_input"
    elif phase == "type_select":
        state["phase"] = "description_input"
    elif phase in {"number_settings", "choice_settings"}:
        state["phase"] = "edit_menu" if state.get("mode") == "edit" else "type_select"
    elif phase in {
        "number_min_input",
        "number_max_input",
        "number_suffix_input",
    }:
        state["phase"] = "number_settings"
    elif phase == "choice_input":
        state["phase"] = "choice_settings"
    elif phase == "edit_menu":
        state["phase"] = "card"
    elif phase == "preview":
        # На экране предпросмотра есть отдельная «Отмена»; сюда кнопка не
        # ведёт. На всякий случай трактуем как отмену без записи.
        await _cancel_preview(update, context, state, cleanup=cleanup)
        return
    elif phase == "hub":
        state.clear()
        state.update(_new_hub(page=page, archive_page=archive_page))
    else:
        await _stale(update)
        return
    await _render_phase(update, context, state, cleanup_ids=cleanup)


async def _cancel_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    cleanup: list[int] | None = None,
) -> None:
    page, archive_page = _saved_page(state)
    mode = state.get("mode")
    if mode == "edit" and state.get("brick_id"):
        brick_id = state["brick_id"]
        origin = state.get("origin") or "hub"
        state.pop("mode", None)
        state.pop("draft", None)
        state.pop("failed_inputs", None)
        state["phase"] = "card"
        state["brick_id"] = brick_id
        state["origin"] = origin
        await _render_phase(
            update,
            context,
            state,
            notice="Изменения отменены. Данные кирпичика не изменены.",
            cleanup_ids=cleanup,
        )
        return
    state.clear()
    state.update(_new_hub(page=page, archive_page=archive_page))
    await _render_phase(
        update,
        context,
        state,
        notice="Создание отменено. В базе ничего не сохранено.",
        cleanup_ids=cleanup,
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "preview":
        await _stale(update)
        return
    await ctx.query(update).answer()
    await _cancel_preview(update, context, state, cleanup=_failed_take(state))


# --- Ввод текста ------------------------------------------------------------


def _parse_number(raw: str) -> int | float:
    text = raw.strip().replace(" ", "").replace(",", ".")
    if not text:
        raise BrickValidationError("Введите число.")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise BrickValidationError(
            "Это не число. Введите число без лишних символов."
        ) from exc
    if not value.is_finite():
        raise BrickValidationError("Число должно быть конечным.")
    if value == value.to_integral_value():
        try:
            return int(value)
        except (OverflowError, ValueError) as exc:
            raise BrickValidationError("Число слишком велико.") from exc
    result = float(value)
    if result == float("inf") or result == float("-inf"):
        raise BrickValidationError("Число слишком велико.")
    return result


async def handle_brick_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Текст принимается только в явных фазах конструктора."""
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") not in TEXT_PHASES:
        return
    await screen.reanchor_screen(update, context)
    raw = (ctx.message(update).text or "").strip()
    phase = state.get("phase")
    draft = _draft(state)
    cleanup: list[int] = []
    notice: str | None = None

    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        if phase == "title_input":
            try:
                title = clean_brick_title(raw)
            except BrickValidationError as exc:
                notice = str(exc)
            else:
                draft["title"] = title
                if state.get("mode") == "create" and not draft.get("code"):
                    draft["code"] = _suggest_code(session, user.id, title)
                state["phase"] = (
                    "edit_menu" if state.get("mode") == "edit" else "code_input"
                )
                cleanup = _failed_take(state)
        elif phase == "code_input":
            exclude_id = (
                int(state.get("brick_id") or 0) if state.get("mode") == "edit" else None
            )
            try:
                validated = validate_brick_code(raw)
            except BrickValidationError as exc:
                notice = str(exc)
            else:
                available = _code_available(
                    session,
                    user_id=user.id,
                    code=validated,
                    exclude_brick_id=exclude_id or None,
                )
                if not available.available:
                    notice = (
                        available.message or "Код уже используется."
                    ) + " Введите другое обозначение."
                else:
                    draft["code"] = available.code or validated
                    state["phase"] = (
                        "edit_menu"
                        if state.get("mode") == "edit"
                        else "description_input"
                    )
                    cleanup = _failed_take(state)
        elif phase == "description_input":
            try:
                draft["description"] = clean_brick_description(raw) or ""
            except BrickValidationError as exc:
                notice = str(exc)
            else:
                state["phase"] = (
                    "edit_menu" if state.get("mode") == "edit" else "type_select"
                )
                cleanup = _failed_take(state)
        elif phase in {
            "number_min_input",
            "number_max_input",
            "number_suffix_input",
        }:
            key = {
                "number_min_input": "min",
                "number_max_input": "max",
                "number_suffix_input": "suffix",
            }[phase]
            rules = dict(draft.get("rules") or {})
            try:
                if key == "suffix":
                    if not raw:
                        raise BrickValidationError(
                            "Подпись не может быть пустой. Используйте «✖️ Подпись» "
                            "на экране настроек, чтобы убрать её."
                        )
                    rules["suffix"] = raw
                else:
                    rules[key] = _parse_number(raw)
                draft["rules"] = parse_rules(draft.get("value_type") or "", rules)
            except BrickValidationError as exc:
                notice = str(exc)
            else:
                state["phase"] = "number_settings"
                cleanup = _failed_take(state)
        elif phase == "choice_input":
            rules = dict(draft.get("rules") or {})
            choices = list(rules.get("choices") or [])
            try:
                value = " ".join(raw.split())
                if not value:
                    raise BrickValidationError("Вариант не может быть пустым.")
                if value in choices:
                    raise BrickValidationError("Такой вариант уже есть.")
                choices.append(value)
                rules["choices"] = choices
                draft["rules"] = parse_rules(draft.get("value_type") or "", rules)
            except BrickValidationError as exc:
                notice = str(exc)
            else:
                state["phase"] = "choice_settings"
                cleanup = _failed_take(state)
    finally:
        session.close()

    if notice is not None:
        _failed_add(state, update)
        await _render_phase(update, context, state, notice=notice)
        return
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=cleanup,
        drop_input=True,
    )


# --- Код, тип и настройки ---------------------------------------------------


async def handle_accept_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "code_input":
        await _stale(update)
        return
    await ctx.query(update).answer()
    draft = _draft(state)
    code = draft.get("code") or ""
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        exclude_id = (
            int(state.get("brick_id") or 0) if state.get("mode") == "edit" else None
        )
        available = _code_available(
            session,
            user_id=user.id,
            code=code,
            exclude_brick_id=exclude_id or None,
        )
    except Exception:  # noqa: BLE001 — экран должен остаться рабочим
        logger.exception("Не удалось проверить код кирпичика.")
        available = None
    finally:
        session.close()
    if available is None:
        await _render_phase(
            update,
            context,
            state,
            notice="Не удалось проверить код из-за ошибки базы. Повторите.",
            cleanup_ids=_failed_take(state),
        )
        return
    if not available.available:
        await _render_phase(
            update,
            context,
            state,
            notice=(available.message or "Код уже используется.")
            + " Введите другое обозначение.",
            cleanup_ids=_failed_take(state),
        )
        return
    draft["code"] = available.code or code
    state["phase"] = "edit_menu" if state.get("mode") == "edit" else "description_input"
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=_failed_take(state),
    )


async def handle_skip_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """«Без описания»: поле остаётся пустым, шаг идёт дальше без записи."""
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "description_input":
        await _stale(update)
        return
    await ctx.query(update).answer()
    draft = _draft(state)
    draft["description"] = ""
    state["phase"] = "edit_menu" if state.get("mode") == "edit" else "type_select"
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def handle_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "type_select":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""
    try:
        value_type = data.split(":", 1)[1]
    except IndexError:
        await _stale(update)
        return
    try:
        value_type = ensure_brick_type(value_type)
    except BrickValidationError:
        await _stale(update)
        return
    draft = _draft(state)
    if draft.get("value_type") != value_type:
        draft["rules"] = {}
    draft["value_type"] = value_type
    if value_type == BRICK_TYPE_NUMBER:
        state["phase"] = "number_settings"
    elif value_type == BRICK_TYPE_CHOICE:
        state["phase"] = "choice_settings"
    else:
        state["phase"] = "preview"
    await _render_phase(
        update,
        context,
        state,
        cleanup_ids=_failed_take(state),
    )


async def handle_number_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "number_settings":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""
    action = data.split(":", 1)[1] if ":" in data else ""
    draft = _draft(state)
    rules = dict(draft.get("rules") or {})
    if action == "integer":
        rules["integer"] = not bool(rules.get("integer"))
    elif action == "set_min":
        state["phase"] = "number_min_input"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    elif action == "set_max":
        state["phase"] = "number_max_input"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    elif action == "set_suffix":
        state["phase"] = "number_suffix_input"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    elif action == "clear_min":
        rules.pop("min", None)
    elif action == "clear_max":
        rules.pop("max", None)
    elif action == "clear_suffix":
        rules.pop("suffix", None)
    elif action == "done":
        try:
            draft["rules"] = parse_rules(draft.get("value_type") or "", rules)
        except BrickValidationError as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=str(exc),
                cleanup_ids=_failed_take(state),
            )
            return
        state["phase"] = "edit_menu" if state.get("mode") == "edit" else "preview"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    else:
        await _stale(update)
        return
    try:
        draft["rules"] = parse_rules(draft.get("value_type") or "", rules)
    except BrickValidationError as exc:
        await _render_phase(
            update,
            context,
            state,
            notice=str(exc),
            cleanup_ids=_failed_take(state),
        )
        return
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def handle_choice_settings(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "choice_settings":
        await _stale(update)
        return
    query = ctx.query(update)
    await query.answer()
    data = query.data or ""
    draft = _draft(state)
    rules = dict(draft.get("rules") or {})
    if data == "bk_choice_add":
        state["phase"] = "choice_input"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    if data == "bk_choice_multiple":
        rules["multiple"] = not bool(rules.get("multiple"))
    elif data.startswith("bk_choice_remove:"):
        try:
            index = int(data.split(":", 1)[1])
        except (IndexError, ValueError):
            await _stale(update)
            return
        choices = list(rules.get("choices") or [])
        if index < 0 or index >= len(choices):
            await _stale(update)
            return
        choices.pop(index)
        if choices:
            rules["choices"] = choices
        else:
            rules.pop("choices", None)
    elif data == "bk_choice_done":
        if not rules.get("choices"):
            await _render_phase(
                update,
                context,
                state,
                notice="Добавьте хотя бы один вариант выбора.",
                cleanup_ids=_failed_take(state),
            )
            return
        try:
            draft["rules"] = parse_rules(draft.get("value_type") or "", rules)
        except BrickValidationError as exc:
            await _render_phase(
                update,
                context,
                state,
                notice=str(exc),
                cleanup_ids=_failed_take(state),
            )
            return
        state["phase"] = "edit_menu" if state.get("mode") == "edit" else "preview"
        await _render_phase(update, context, state, cleanup_ids=_failed_take(state))
        return
    else:
        await _stale(update)
        return
    # Список вариантов можно временно опустошить или переключить multiple:
    # строгая проверка непустого choices выполняется только на «Готово» и
    # при добавлении варианта. Иначе последний вариант нельзя было бы убрать.
    draft["rules"] = rules
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


# --- Редактирование и сохранение -------------------------------------------


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "card":
        await _stale(update)
        return
    await ctx.query(update).answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        brick = get_personal_brick(
            session, user.telegram_user_id, int(state.get("brick_id") or 0)
        )
    finally:
        session.close()
    if brick is None:
        await _render_phase(
            update,
            context,
            state,
            notice="Кирпичик не найден. Обновите список.",
            cleanup_ids=_failed_take(state),
        )
        return
    if brick.status != BRICK_STATUS_ACTIVE:
        state["revision"] = brick.revision
        await _render_phase(
            update,
            context,
            state,
            notice="Архивный кирпичик нельзя редактировать. Сначала восстановите его.",
            cleanup_ids=_failed_take(state),
        )
        return
    state["mode"] = "edit"
    state["brick_id"] = brick.id
    state["revision"] = brick.revision
    state["draft"] = {
        "title": brick.title,
        "code": brick.code,
        "description": brick.description or "",
        "value_type": brick.value_type,
        "rules": dict(brick.rules),
    }
    state["phase"] = "edit_menu"
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def handle_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "edit_menu":
        await _stale(update)
        return
    query = ctx.query(update)
    data = query.data or ""
    await query.answer()
    if data == "bk_edit_title":
        state["phase"] = "title_input"
    elif data == "bk_edit_code":
        state["phase"] = "code_input"
    elif data == "bk_edit_description":
        state["phase"] = "description_input"
    elif data == "bk_edit_settings":
        draft = _draft(state)
        value_type = draft.get("value_type")
        if value_type == BRICK_TYPE_NUMBER:
            state["phase"] = "number_settings"
        elif value_type == BRICK_TYPE_CHOICE:
            state["phase"] = "choice_settings"
        else:
            await _stale(update)
            return
    elif data == "bk_preview":
        state["phase"] = "preview"
    else:
        await _stale(update)
        return
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def handle_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "preview":
        await _stale(update)
        return
    await ctx.query(update).answer()
    state["phase"] = "edit_menu" if state.get("mode") == "edit" else "title_input"
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def _handle_create_save(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    draft = _draft(state)
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        try:
            brick = create_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                code=draft.get("code") or "",
                title=draft.get("title") or "",
                value_type=draft.get("value_type") or "",
                rules=draft.get("rules") or {},
                description=draft.get("description") or None,
            )
        except (BrickConflictError, BrickValidationError, BrickActorError) as exc:
            session.rollback()
            # Код мог занять конкурентный пользователь. Если он всё ещё
            # свободен, это ошибка БД: оставляем предпросмотр, не теряя ввод.
            try:
                available = _code_available(
                    session,
                    user_id=user.id,
                    code=draft.get("code") or "",
                )
            except Exception:  # noqa: BLE001 — экран и черновик важнее запроса
                logger.exception("Не удалось перепроверить код кирпичика.")
                available = None
            if available is not None and not available.available:
                state["phase"] = "code_input"
                await _render_phase(
                    update,
                    context,
                    state,
                    notice=(available.message or str(exc))
                    + " Введите другое обозначение.",
                    cleanup_ids=_failed_take(state),
                )
            else:
                await _render_phase(
                    update,
                    context,
                    state,
                    notice=(
                        "Не удалось сохранить кирпичик из-за ошибки базы. "
                        "Данные не записаны; повторите сохранение."
                    ),
                    cleanup_ids=_failed_take(state),
                )
            return
        except Exception:  # noqa: BLE001 — не теряем черновик и экран
            session.rollback()
            logger.exception("Не удалось создать личный кирпичик.")
            await _render_phase(
                update,
                context,
                state,
                notice=(
                    "Не удалось сохранить кирпичик из-за ошибки базы. "
                    "Данные не записаны; повторите сохранение."
                ),
                cleanup_ids=_failed_take(state),
            )
            return
    finally:
        session.close()

    page, archive_page = _saved_page(state)
    state.clear()
    state.update(_new_hub(page=page, archive_page=archive_page))
    await _render_phase(
        update,
        context,
        state,
        notice=(
            f"Кирпичик «{_h(brick.title)}» создан. "
            f"Обозначение: <code>[{_h(brick.code)}]</code>."
        ),
        drop_input=True,
    )


async def _handle_edit_save(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    draft = _draft(state)
    brick_id = int(state.get("brick_id") or 0)
    expected_revision = int(state.get("revision") or 0)
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        try:
            result = update_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=brick_id,
                expected_revision=expected_revision,
                code=draft.get("code") or "",
                title=draft.get("title") or "",
                rules=draft.get("rules") or {},
                description=draft.get("description") or None,
            )
        except BrickValidationError as exc:
            session.rollback()
            await _render_phase(
                update,
                context,
                state,
                notice=str(exc),
                cleanup_ids=_failed_take(state),
            )
            return
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Не удалось изменить личный кирпичик.")
            await _render_phase(
                update,
                context,
                state,
                notice=(
                    "Не удалось сохранить изменения из-за ошибки базы. "
                    "Данные не изменены; повторите попытку."
                ),
                cleanup_ids=_failed_take(state),
            )
            return
    finally:
        session.close()

    if result.status == "updated" and result.brick is not None:
        state.pop("mode", None)
        state.pop("draft", None)
        state.pop("failed_inputs", None)
        state["phase"] = "card"
        state["brick_id"] = result.brick.id
        state["revision"] = result.brick.revision
        await _render_phase(
            update,
            context,
            state,
            notice="Изменения сохранены.",
            drop_input=True,
        )
        return
    if result.status == "conflict":
        state["phase"] = "code_input"
        await _render_phase(
            update,
            context,
            state,
            notice=(result.message or "Код уже используется.")
            + " Введите другое обозначение.",
            cleanup_ids=_failed_take(state),
        )
        return
    if result.status == "stale":
        if result.brick is not None:
            state["revision"] = result.brick.revision
        state.pop("mode", None)
        state.pop("draft", None)
        state["phase"] = "card" if result.brick is not None else "hub"
        if result.brick is None:
            state.clear()
            state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Кирпичик изменился. Откройте его заново.",
            cleanup_ids=_failed_take(state),
        )
        return
    if result.status in {"archived", "not_found"}:
        if result.brick is not None:
            state["revision"] = result.brick.revision
            state["phase"] = "card"
        else:
            state.clear()
            state.update(_new_hub())
        await _render_phase(
            update,
            context,
            state,
            notice=result.message or "Кирпичик недоступен.",
            cleanup_ids=_failed_take(state),
        )
        return
    # Неизвестный статус сервиса не должен молча менять состояние.
    await _render_phase(
        update,
        context,
        state,
        notice="Не удалось определить результат сохранения. Откройте карточку заново.",
        cleanup_ids=_failed_take(state),
    )


async def handle_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "preview":
        await _stale(update)
        return
    await ctx.query(update).answer()
    if state.get("mode") == "edit":
        await _handle_edit_save(update, context, state)
    else:
        await _handle_create_save(update, context, state)


# --- Архив и восстановление -------------------------------------------------


async def handle_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "card":
        await _stale(update)
        return
    await ctx.query(update).answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        brick = get_personal_brick(
            session, user.telegram_user_id, int(state.get("brick_id") or 0)
        )
    finally:
        session.close()
    if brick is None:
        await _render_phase(
            update,
            context,
            state,
            notice="Кирпичик не найден. Обновите список.",
            cleanup_ids=_failed_take(state),
        )
        return
    if brick.status != BRICK_STATUS_ACTIVE:
        state["revision"] = brick.revision
        await _render_phase(
            update,
            context,
            state,
            notice="Кирпичик уже находится в архиве.",
            cleanup_ids=_failed_take(state),
        )
        return
    state["revision"] = brick.revision
    state["phase"] = "archive_confirm"
    await _render_phase(update, context, state, cleanup_ids=_failed_take(state))


async def handle_archive_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "archive_confirm":
        await _stale(update)
        return
    await ctx.query(update).answer()
    brick_id = int(state.get("brick_id") or 0)
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        try:
            result = archive_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=brick_id,
                expected_revision=int(state.get("revision") or 0),
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Не удалось архивировать личный кирпичик.")
            await _render_phase(
                update,
                context,
                state,
                notice=(
                    "Не удалось переместить кирпичик в архив из-за ошибки базы. "
                    "Кирпичик не изменён."
                ),
                cleanup_ids=_failed_take(state),
            )
            return
    finally:
        session.close()

    if result.brick is not None:
        state["revision"] = result.brick.revision
        state["brick_id"] = result.brick.id
        state["origin"] = "hub"
        state["phase"] = "card"
    else:
        state.clear()
        state.update(_new_hub())
    await _render_phase(
        update,
        context,
        state,
        notice=result.message
        or (
            "Кирпичик перемещён в архив."
            if result.status in {"archived", "already_archived"}
            else "Кирпичик недоступен."
        ),
        cleanup_ids=_failed_take(state),
    )


async def handle_restore(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_brick_session(ctx.user(update).id)
    if state is None or state.get("phase") != "card":
        await _stale(update)
        return
    await ctx.query(update).answer()
    session = SessionLocal()
    try:
        user = _ensure_user(session, update)
        try:
            result = restore_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=int(state.get("brick_id") or 0),
                expected_revision=int(state.get("revision") or 0),
            )
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("Не удалось восстановить личный кирпичик.")
            await _render_phase(
                update,
                context,
                state,
                notice=(
                    "Не удалось восстановить кирпичик из-за ошибки базы. "
                    "Кирпичик остался в архиве."
                ),
                cleanup_ids=_failed_take(state),
            )
            return
    finally:
        session.close()

    if result.brick is not None:
        state["revision"] = result.brick.revision
        state["brick_id"] = result.brick.id
        state["origin"] = "hub"
        state["phase"] = "card"
    else:
        state.clear()
        state.update(_new_hub())
    await _render_phase(
        update,
        context,
        state,
        notice=result.message
        or (
            "Кирпичик восстановлен."
            if result.status in {"restored", "already_active"}
            else "Кирпичик недоступен."
        ),
        cleanup_ids=_failed_take(state),
    )


# --- Обработчики -----------------------------------------------------------


class BrickTextHandler(MessageHandler):
    """Текст принимается только в явных фазах «Моих кирпичиков»."""

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
        state = load_brick_session(user.id)
        if not isinstance(state, dict):
            return None
        if state.get("phase") not in self.allowed_phases:
            return None
        return matched


class BrickScreenTextHandler(MessageHandler):
    """
    Текст на ЭКРАННЫХ фазах раздела (R4, «чат как приложение»).

    Отдельный фильтр-класс, чтобы не смешивать две разные политики ввода:
    мастера принимают текст и сохраняют его (`BrickTextHandler`), а экраны
    списка/карточки/предложений ввода не ждут вовсе — их задача не отдать
    сообщение глобальной подсказке главного меню и не оставить диалог
    без кнопок.
    """

    def __init__(self, filters_arg, callback, *, screen_phases: frozenset[str]):
        super().__init__(filters_arg, callback)
        self.screen_phases = frozenset(screen_phases)

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_brick_session(user.id)
        if not isinstance(state, dict):
            return None
        if state.get("phase") not in self.screen_phases:
            return None
        return matched


async def handle_brick_stray_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Текст или фото в экранной фазе раздела (R4, «чат как приложение»).

    На экранах списка, карточки и предложений ввод не нужен: без этого
    обработчика глобальная подсказка главного меню затирала экран раздела,
    и человек оставался без единой кнопки — настоящий тупик. Экран
    восстанавливается, а сообщение пользователя удаляется best-effort.
    """
    state = load_brick_session(ctx.user(update).id)
    if not isinstance(state, dict) or state.get("phase") not in SCREEN_PHASES:
        return
    await screen.reanchor_screen(update, context)
    await _render_phase(
        update,
        context,
        state,
        notice="Этот экран работает кнопками — выберите действие.",
        drop_input=True,
    )


async def handle_brick_stale(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Неизвестная/устаревшая кнопка bk_*: отвечает, но данные не меняет."""
    await _stale(update)


def bricks_handlers() -> list[BaseHandler]:
    """
    Обработчики раздела «Мои кирпичики».

    Регистрируются после мастера поста, «Мест» и админки, но до главного
    меню: активные текстовые фазы получают ввод раньше глобальной подсказки.
    Callback-префикс `bk_` уникален; в данных только id/фаза/индекс, без JSON.
    """
    return [
        CommandHandler("bricks", open_bricks),
        CallbackQueryHandler(open_bricks, pattern=r"^bricks$"),
        CallbackQueryHandler(handle_hub, pattern=r"^bk_hub$"),
        CallbackQueryHandler(handle_create, pattern=r"^bk_create$"),
        CallbackQueryHandler(handle_page, pattern=r"^bk_page:\d+$"),
        CallbackQueryHandler(handle_archive_list, pattern=r"^bk_archlist$"),
        CallbackQueryHandler(handle_archive_page, pattern=r"^bk_apage:\d+$"),
        CallbackQueryHandler(handle_open, pattern=r"^bk_open:\d+$"),
        CallbackQueryHandler(handle_back, pattern=r"^bk_back$"),
        CallbackQueryHandler(handle_cancel, pattern=r"^bk_cancel$"),
        CallbackQueryHandler(handle_accept_code, pattern=r"^bk_accept_code$"),
        CallbackQueryHandler(
            handle_type,
            pattern=(r"^bk_type:(?:short_text|long_text|date|time|url|number|choice)$"),
        ),
        CallbackQueryHandler(handle_number_settings, pattern=r"^bk_num:[a-z_]+$"),
        CallbackQueryHandler(handle_choice_settings, pattern=r"^bk_choice_"),
        CallbackQueryHandler(handle_edit, pattern=r"^bk_edit$"),
        CallbackQueryHandler(
            handle_edit_field,
            pattern=(r"^bk_edit_(?:title|code|description|settings)$|^bk_preview$"),
        ),
        CallbackQueryHandler(handle_skip_description, pattern=r"^bk_skip_description$"),
        CallbackQueryHandler(handle_change, pattern=r"^bk_change$"),
        CallbackQueryHandler(handle_save, pattern=r"^bk_save$"),
        CallbackQueryHandler(handle_archive, pattern=r"^bk_archive$"),
        CallbackQueryHandler(handle_archive_confirm, pattern=r"^bk_archive_confirm$"),
        CallbackQueryHandler(handle_restore, pattern=r"^bk_restore$"),
        CallbackQueryHandler(handle_proposal_start, pattern=r"^bp_start(?::\d+)?$"),
        CallbackQueryHandler(handle_proposal_mine, pattern=r"^bp_mine$"),
        CallbackQueryHandler(handle_proposal_source, pattern=r"^bp_source:\d+$"),
        CallbackQueryHandler(
            handle_proposal_target, pattern=r"^bp_target:(?:place|system)$"
        ),
        CallbackQueryHandler(handle_proposal_place, pattern=r"^bp_place:\d+$"),
        CallbackQueryHandler(
            handle_proposal_page,
            pattern=r"^bp_(?:page|ppage|mpage):\d+$",
        ),
        CallbackQueryHandler(handle_proposal_send, pattern=r"^bp_send$"),
        CallbackQueryHandler(handle_proposal_open_own, pattern=r"^bp_open:\d+$"),
        CallbackQueryHandler(handle_proposal_withdraw, pattern=r"^bp_withdraw$"),
        CallbackQueryHandler(
            handle_proposal_withdraw_confirm, pattern=r"^bp_withdraw_yes$"
        ),
        CallbackQueryHandler(handle_proposal_back, pattern=r"^bp_back$"),
        CallbackQueryHandler(handle_proposal_stale, pattern=r"^bp_"),
        BrickTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_brick_text,
            allowed_phases=TEXT_PHASES,
        ),
        BrickScreenTextHandler(
            (filters.TEXT | filters.PHOTO) & ~filters.COMMAND,
            handle_brick_stray_text,
            screen_phases=SCREEN_PHASES,
        ),
        CallbackQueryHandler(handle_brick_stale, pattern=r"^bk_"),
    ]
