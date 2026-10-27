# handlers/templates.py
"""
Telegram-интерфейс раздела «Мои шаблоны» (Stage 25).

Пользователь создаёт личные шаблоны, открывает карточку, редактирует шапку,
тело и подвал обычным сообщением, смотрит предпросмотр, доступные кирпичики
и сбрасывает содержимое к встроенному стандарту. Стандартный шаблон места
открывается из административного кабинета и доступен только действующему
администратору места или суперадминистратору.

Границы этапа:
- согласование кирпичиков — Stage 26;
- адаптивный мастер постов — Stage 27;
- фотографии — отдельные вложения, не кирпичики.

Безопасность и UX:
- владелец всегда берётся из Telegram update, template_id — только из callback;
- сервисный слой повторно проверяет владельца, права, ревизию и кирпичики;
- текст принимается только в явных фазах `template_sessions`;
- один экран `utils.screen`; черновик не смешивается с постом, местами,
  кирпичиками и админкой;
- устаревшая кнопка не перезаписывает чужие изменения, а введённый текст
  сохраняется для повторной попытки;
- имя шаблона живёт только в сессии и никогда не попадает в callback_data.

Callback-префикс `tp_` уникален; в данных только id/фаза/индекс, без JSON.
"""

from __future__ import annotations

import logging
from typing import Any, TypeGuard

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

import utils.screen as screen
import utils.tg_context as ctx
from admin_state import clear_admin_session, load_admin_session
from brick_state import clear_brick_session
from brick_store import list_available_bricks
from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_TYPE_TITLES,
)
from database import SessionLocal
from models import User
from permissions import can_access_admin_panel, can_manage_place
from place_state import clear_place_session
from place_store import get_place_details
from template_compiler import (
    TemplateBindings,
    TemplateCompileError,
    resolve_template_bindings,
)
from template_state import (
    clear_template_session,
    load_template_session,
    save_template_session,
)
from template_store import (
    TemplateActorError,
    TemplateDetails,
    TemplateError,
    TemplatePermissionError,
    create_personal_template,
    get_accessible_template,
    get_builtin_template,
    get_standard_template,
    list_personal_templates,
    reset_personal_template,
    reset_standard_template,
    update_template_section,
)
from template_types import (
    TEMPLATE_KIND_PERSONAL,
    TEMPLATE_KIND_STANDARD,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
    TEMPLATE_SECTIONS,
    TemplateValidationError,
    validate_template_name,
    validate_template_sections,
)
from user_store import get_or_create_user
from utils.tg_html import escape_html

logger = logging.getLogger(__name__)

# --- Константы ---------------------------------------------------------------

PAGE_SIZE = 8
BRICK_PAGE_SIZE = 8
BUTTON_TITLE_LIMIT = 42
TEXT_PREVIEW_LIMIT = 1200
ERROR_TEXT_LIMIT = 1400

PHASE_HUB = "hub"
# Poster 2.0 S2: список личных шаблонов как вход в анонс. Тап по названию
# сразу начинает конструктор, «✏️» открывает карточку для правки.
PHASE_PICK = "pick"
PHASE_CARD = "card"
PHASE_SECTION_EDIT = "section_edit"
PHASE_CONFIRM_SAVE = "confirm_save"
PHASE_SECTION_ERROR = "section_error"
PHASE_RESOLVE_AMBIGUITY = "resolve_ambiguity"
PHASE_PREVIEW = "preview"
PHASE_RESET_CONFIRM = "reset_confirm"
PHASE_BRICK_LIST = "brick_list"
PHASE_HELP = "help"
PHASE_AWAIT_NAME = "await_new_name"
PHASE_CREATE_CHOICE = "create_choice"
PHASE_CREATE_CONFIRM = "create_confirm"
PHASE_SAVING = "saving"
PHASE_CREATE_SAVING = "create_saving"

# Фазы, в которых сценарий принимает обычный текст. Все остальные фазы —
# экранные: текст в них не должен попадать в шаблон.
TEXT_PHASES = frozenset(
    {
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
        PHASE_AWAIT_NAME,
    }
)

_TEMPLATE_PHASES = frozenset(
    {
        PHASE_CARD,
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
        PHASE_PREVIEW,
        PHASE_RESET_CONFIRM,
        PHASE_BRICK_LIST,
        PHASE_HELP,
        PHASE_SAVING,
    }
)

_CREATE_PHASES = frozenset(
    {PHASE_AWAIT_NAME, PHASE_CREATE_CHOICE, PHASE_CREATE_CONFIRM, PHASE_CREATE_SAVING}
)

PHASES = frozenset({PHASE_HUB, PHASE_PICK}) | _TEMPLATE_PHASES | _CREATE_PHASES

_TEXT_INPUT_ERROR = "Текст не принят. Исправьте его и отправьте новой полной секцией."

STALE_TEXT = "Кнопка устарела: откройте шаблон заново."
DENIED_TEXT = "Шаблон недоступен."

SECTION_TITLES = {
    TEMPLATE_SECTION_HEADER: "шапка",
    TEMPLATE_SECTION_BODY: "тело",
    TEMPLATE_SECTION_FOOTER: "подвал",
}
SECTION_GENITIVE = {
    TEMPLATE_SECTION_HEADER: "шапки",
    TEMPLATE_SECTION_BODY: "тела",
    TEMPLATE_SECTION_FOOTER: "подвала",
}
SECTION_SHORT = {
    "h": TEMPLATE_SECTION_HEADER,
    "b": TEMPLATE_SECTION_BODY,
    "f": TEMPLATE_SECTION_FOOTER,
}
SECTION_SHORT_BY_NAME = {value: key for key, value in SECTION_SHORT.items()}
SECTION_KEYS = {
    TEMPLATE_SECTION_HEADER: "header",
    TEMPLATE_SECTION_BODY: "body",
    TEMPLATE_SECTION_FOOTER: "footer",
}
SCOPE_TITLES = {
    BRICK_SCOPE_SYSTEM: "системный",
    BRICK_SCOPE_PLACE: "место",
    BRICK_SCOPE_PERSONAL: "личный",
}
ERROR_KINDS = frozenset(
    {
        "syntax",
        "unknown",
        "stale",
        "conflict",
        "forbidden",
        "invalid",
        "validation",
        "unexpected",
    }
)


# --- Общие помощники ---------------------------------------------------------


def _h(value: object) -> str:
    """Экранирует пользовательский текст для parse_mode=HTML."""
    return escape_html(str(value))


def _short(value: object, limit: int = BUTTON_TITLE_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _clip(value: object, limit: int = TEXT_PREVIEW_LIMIT) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (на экране сокращено; в шаблоне сохранено полностью)"


def _display(value: object, limit: int = TEXT_PREVIEW_LIMIT) -> str:
    """Отображает текст секции, не теряя факт пустоты/пробелов."""
    if not isinstance(value, str):
        return "— нет —"
    if value == "":
        return "— пусто —"
    if not value.strip():
        return "— только пробелы —"
    return _h(_clip(value, limit))


def _positive_int(value: object) -> TypeGuard[int]:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


def _non_negative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _page_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _parse_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _callback_id(data: str, prefix: str) -> int | None:
    if not data.startswith(prefix):
        return None
    return _parse_int(data[len(prefix) :])


def _place_name(session: Session, place_id: int | None) -> str | None:
    if not _positive_int(place_id):
        return None
    details = get_place_details(session, int(place_id))
    return details.canonical_name if details is not None else None


def _localize_sections(text: str) -> str:
    """Переводит английские имена секций из ошибок Stage 24 в русские."""
    result = str(text)
    for section, title in SECTION_TITLES.items():
        result = result.replace(f"(секция {section})", f"(секция «{title}»)")
        result = result.replace(f"секция {section}", f"секция «{title}»")
    return result


def _compile_error_text(exc: BaseException) -> str:
    return _localize_sections(str(exc))


def _chosen_map(state: dict) -> dict[str, int] | None:
    raw = state.get("chosen_brick_ids")
    if not isinstance(raw, dict):
        return None
    result: dict[str, int] = {}
    for code, brick_id in raw.items():
        if isinstance(code, str) and code and _positive_int(brick_id):
            result[code] = int(brick_id)
    return result or None


def _valid_failed_inputs(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]


# --- Состояние ---------------------------------------------------------------


def _new_pick(page: int = 0) -> dict:
    """Состояние входа «📝 Создать по личному шаблону» (Poster 2.0 S2).

    Отдельная фаза списка: тап по названию начинает анонс. Кнопки создания и
    правки ведут в общий раздел шаблонов — данные те же, меняется только вход.
    """
    return {"phase": PHASE_PICK, "page": _page_value(page)}


def _new_hub(page: int = 0) -> dict:
    return {"phase": PHASE_HUB, "page": _page_value(page)}


def _card_state(template: TemplateDetails, *, origin: str, page: int = 0) -> dict:
    return {
        "phase": PHASE_CARD,
        "template_id": template.template_id,
        "revision": template.revision,
        "origin": origin,
        "place_id": template.place_id,
        "page": _page_value(page),
    }


def _template_origin(template: TemplateDetails) -> str:
    return "admin" if template.kind == TEMPLATE_KIND_STANDARD else "personal"


def _valid_state(state: object) -> TypeGuard[dict[str, Any]]:
    """Проверяет только структуру payload, не права и не существование строк."""
    if not isinstance(state, dict):
        return False
    phase = state.get("phase")
    if phase == PHASE_SAVING:
        phase = PHASE_CONFIRM_SAVE
    elif phase == PHASE_CREATE_SAVING:
        phase = PHASE_CREATE_CONFIRM
    if phase not in PHASES:
        return False

    if phase in {PHASE_HUB, PHASE_PICK}:
        raw_page = state.get("page", 0)
        return _non_negative_int(raw_page)

    if phase == PHASE_AWAIT_NAME:
        failed = state.get("failed_inputs", [])
        if failed is None:
            failed = []
        return isinstance(failed, list)

    if phase in _CREATE_PHASES:
        name = state.get("name")
        if not isinstance(name, str) or not name.strip():
            return False
        if phase == PHASE_CREATE_CHOICE:
            return True
        if state.get("mode") not in {"standard", "empty"}:
            return False
        for key in ("create_header", "create_body", "create_footer"):
            if not isinstance(state.get(key, ""), str):
                return False
        error = state.get("error_message")
        return error is None or isinstance(error, str)

    # Фазы существующего шаблона.
    if not _positive_int(state.get("template_id")):
        return False
    if not _positive_int(state.get("revision")):
        return False
    if state.get("origin") not in {"personal", "admin"}:
        return False
    place_id = state.get("place_id")
    if place_id is not None and not _positive_int(place_id):
        return False

    if phase in {
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
        PHASE_HELP,
    }:
        if state.get("section") not in TEMPLATE_SECTIONS:
            return False
        if not isinstance(state.get("draft_text"), str):
            return False

    if phase == PHASE_SECTION_ERROR:
        if state.get("error_kind") not in ERROR_KINDS:
            return False
        if not isinstance(state.get("error_message"), str):
            return False

    if phase == PHASE_RESOLVE_AMBIGUITY:
        if not isinstance(state.get("pending_ambiguities"), list):
            return False

    if phase == PHASE_BRICK_LIST:
        if not _non_negative_int(state.get("brick_offset", 0)):
            return False
        if state.get("back_phase") not in {
            PHASE_CARD,
            PHASE_SECTION_EDIT,
            PHASE_CONFIRM_SAVE,
            PHASE_SECTION_ERROR,
            PHASE_RESOLVE_AMBIGUITY,
        }:
            return False

    if phase == PHASE_HELP:
        if state.get("back_phase") != PHASE_SECTION_EDIT:
            return False

    return True


# --- Доступ и загрузка -------------------------------------------------------


def _ensure_user(session: Session, update: Update) -> User:
    telegram_user = ctx.user(update)
    return get_or_create_user(
        session,
        telegram_user.id,
        username=getattr(telegram_user, "username", None),
        full_name=getattr(telegram_user, "full_name", None),
    )


def _load_editable(
    session: Session, telegram_user_id: int, template_id: object
) -> TemplateDetails | None:
    """
    Шаблон, который пользователь действительно может редактировать.

    Для личного достаточно владельца (это проверяет сервис). Для стандарта
    UI дополнительно требует действующего администратора места: сервис
    повторит проверку при сохранении и сбросе.
    """
    if not _positive_int(template_id):
        return None
    template = get_accessible_template(session, telegram_user_id, int(template_id))
    if template is None:
        return None
    if template.kind == TEMPLATE_KIND_STANDARD:
        if template.place_id is None:
            return None
        if not can_manage_place(session, telegram_user_id, template.place_id):
            return None
    elif template.kind != TEMPLATE_KIND_PERSONAL:
        return None
    return template


# --- Экраны: список и карточка ----------------------------------------------


def _hub_content(
    templates: list[TemplateDetails], page: int
) -> tuple[str, InlineKeyboardMarkup]:
    total = len(templates)
    if total == 0:
        text = (
            "📄 Мои шаблоны\n\n"
            "У вас пока нет личных шаблонов. Шаблон определяет, как будет "
            "выглядеть текст анонса. Его можно использовать для разных мест."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Создать шаблон", callback_data="tp_create")],
                [InlineKeyboardButton("◀️ Назад", callback_data="main_menu")],
            ]
        )
        return text, keyboard

    page = min(_page_value(page), (total - 1) // PAGE_SIZE)
    offset = page * PAGE_SIZE
    page_items = templates[offset : offset + PAGE_SIZE]
    lines = [f"📄 Мои шаблоны\n\nВсего: {total}."]
    for index, template in enumerate(page_items, start=offset + 1):
        lines.append(f"{index}. {_h(template.display_name)} · v{template.revision}")
    if total > PAGE_SIZE:
        lines.append(f"Показаны {offset + 1}–{offset + len(page_items)} из {total}.")
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                _short(f"📄 {template.display_name}"),
                callback_data=f"tp_open:{template.template_id}",
            )
        ]
        for template in page_items
    ]
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("◀️", callback_data=f"tp_page:{page - 1}")
        )
    if offset + PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton("▶️", callback_data=f"tp_page:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("➕ Создать шаблон", callback_data="tp_create")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _pick_content(
    templates: list[TemplateDetails], page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Экран «📝 Создать по личному шаблону» (Poster 2.0 S2).

    Тап по названию сразу начинает анонс по этому шаблону — без карточки и
    подтверждений. Правка и создание остаются в том же разделе: «✏️» рядом с
    названием открывает карточку, «➕ Создать шаблон» — мастер создания.
    """
    total = len(templates)
    if total == 0:
        text = (
            "📝 Создать по личному шаблону\n\n"
            "У вас пока нет личных шаблонов. Шаблон определяет, как будет "
            "выглядеть текст анонса, и подходит для разных площадок.\n\n"
            "Создайте шаблон — и сразу сможете начать по нему анонс."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("➕ Создать шаблон", callback_data="tp_create")],
                [InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")],
            ]
        )
        return text, keyboard

    page = min(_page_value(page), (total - 1) // PAGE_SIZE)
    offset = page * PAGE_SIZE
    page_items = templates[offset : offset + PAGE_SIZE]
    lines = [
        "📝 Создать по личному шаблону",
        "",
        "Выберите шаблон — анонс начнётся сразу, без лишних подтверждений. "
        "Правка шаблона — кнопкой «✏️» рядом с названием.",
        "",
        f"Всего шаблонов: {total}.",
    ]
    for index, template in enumerate(page_items, start=offset + 1):
        lines.append(f"{index}. {_h(template.display_name)} · v{template.revision}")
    if total > PAGE_SIZE:
        lines.append(f"Показаны {offset + 1}–{offset + len(page_items)} из {total}.")

    rows: list[list[InlineKeyboardButton]] = []
    for template in page_items:
        rows.append(
            [
                InlineKeyboardButton(
                    _short(f"🚀 {template.display_name}"),
                    callback_data=f"tp_pick:{template.template_id}",
                ),
                InlineKeyboardButton(
                    "✏️", callback_data=f"tp_open:{template.template_id}"
                ),
            ]
        )
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton("◀️", callback_data=f"tp_pickpage:{page - 1}")
        )
    if offset + PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton("▶️", callback_data=f"tp_pickpage:{page + 1}")
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("➕ Создать шаблон", callback_data="tp_create")])
    rows.append([InlineKeyboardButton("◀️ Главное меню", callback_data="main_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _card_back_button(template: TemplateDetails) -> InlineKeyboardButton:
    if template.kind == TEMPLATE_KIND_STANDARD and template.place_id is not None:
        return InlineKeyboardButton(
            "◀️ К месту", callback_data=f"tp_back_place:{template.place_id}"
        )
    return InlineKeyboardButton("◀️ К списку", callback_data="tp_hub")


def _card_content(
    template: TemplateDetails, *, place_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    version = template.current_version
    kind_title = (
        "Стандартный шаблон места"
        if template.kind == TEMPLATE_KIND_STANDARD
        else "Личный шаблон"
    )
    lines = [
        f"📄 <b>{_h(template.display_name)}</b>",
        f"Тип: {kind_title}",
        f"Версия: {template.revision}",
    ]
    if template.kind == TEMPLATE_KIND_STANDARD and place_name:
        lines.append(f"Место: {_h(place_name)}")
    lines.extend(
        [
            "",
            f"📌 Шапка:\n{_display(version.header, 700)}",
            "",
            f"📝 Тело:\n{_display(version.body, 900)}",
            "",
            f"📌 Подвал:\n{_display(version.footer, 700)}",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✏️ Шапка",
                    callback_data=f"tp_edit_h:{template.template_id}",
                ),
                InlineKeyboardButton(
                    "✏️ Тело",
                    callback_data=f"tp_edit_b:{template.template_id}",
                ),
                InlineKeyboardButton(
                    "✏️ Подвал",
                    callback_data=f"tp_edit_f:{template.template_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "👁 Предпросмотр",
                    callback_data=f"tp_preview:{template.template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 Доступные кирпичики",
                    callback_data=f"tp_bricks:{template.template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "♻️ Сбросить к стандартному",
                    callback_data=f"tp_reset:{template.template_id}",
                )
            ],
            [_card_back_button(template)],
        ]
    )
    return "\n".join(lines), keyboard


def _preview_content(
    template: TemplateDetails, *, place_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    version = template.current_version
    lines = [
        f"👁 Предпросмотр: {_h(template.display_name)}",
    ]
    if template.kind == TEMPLATE_KIND_STANDARD and place_name:
        lines.append(f"Место: {_h(place_name)}")
    lines.extend(
        [
            f"Версия: {template.revision}",
            "",
            f"📌 Шапка:\n{_display(version.header)}",
            "",
            f"📝 Тело:\n{_display(version.body)}",
            "",
            f"📌 Подвал:\n{_display(version.footer)}",
            "",
        ]
    )
    if version.bindings:
        lines.append("🔗 Привязки кирпичиков:")
        for binding in version.bindings:
            label = (
                f"• [{_h(binding.code)}] — {_h(binding.title)} "
                f"({SCOPE_TITLES.get(binding.scope, binding.scope)})"
            )
            if not binding.is_active:
                label += " · архив"
            lines.append(label)
    else:
        lines.append("🔗 Привязок кирпичиков нет.")
    lines.extend(
        [
            "",
            "Это структура шаблона. Значения кирпичиков подставятся при "
            "создании анонса.",
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "◀️ К шаблону",
                    callback_data=f"tp_open:{template.template_id}",
                )
            ]
        ]
    )
    return "\n".join(lines), keyboard


def _reset_confirm_content(
    template: TemplateDetails,
) -> tuple[str, InlineKeyboardMarkup]:
    kind_title = (
        "стандартного шаблона места"
        if template.kind == TEMPLATE_KIND_STANDARD
        else "личного шаблона"
    )
    text = (
        "♻️ Сбросить к стандартному?\n\n"
        f"Шаблон: «{_h(template.display_name)}»\n"
        f"Текущая версия: {template.revision}\n\n"
        f"Содержимое {kind_title} будет заменено встроенным стандартом Poster. "
        "Будет создана новая версия; история версий и снимки не изменятся. "
        "Черновики постов не затрагиваются."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Подтвердить сброс",
                    callback_data=f"tp_reset_yes:{template.template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ Отмена",
                    callback_data=f"tp_open:{template.template_id}",
                )
            ],
        ]
    )
    return text, keyboard


# --- Экраны: редактирование секции ------------------------------------------


def _section_edit_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    section = state["section"]
    title = SECTION_GENITIVE[section]
    draft = state.get("draft_text", "")
    text = (
        f"✏️ Редактирование {title}\n\n"
        f"Текст секции (черновик):\n{_display(draft)}"
        "\n\n"
        "Отправьте новым сообщением ПОЛНЫЙ текст секции. Переводы строк, "
        "пробелы и Unicode сохраняются как есть.\n\n"
        "Кирпичики записываются в квадратных скобках: [код]. Список доступных "
        "кодов — по кнопке ниже. Служебные скобки [[ и ]] выводятся как текст.\n"
        "Форматирование: **жирный**, *курсив*, __подчёркнутый__, "
        "~~зачёркнутый~~. Ссылка: Текст(https://example.com).\n"
        "Фотографии в шаблон не входят."
    )
    template_id = state["template_id"]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📋 Доступные кирпичики",
                    callback_data=f"tp_bricks:{template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "❓ Подсказка по оформлению",
                    callback_data=(
                        f"tp_help:{SECTION_SHORT_BY_NAME[section]}:{template_id}"
                    ),
                )
            ],
            [InlineKeyboardButton("🧹 Очистить секцию", callback_data="tp_clear")],
            [
                InlineKeyboardButton(
                    "◀️ К шаблону (отменить правку)",
                    callback_data=f"tp_open:{template_id}",
                )
            ],
        ]
    )
    return text, keyboard


def _section_back_button(state: dict) -> InlineKeyboardButton:
    if state.get("origin") == "admin" and _positive_int(state.get("place_id")):
        return InlineKeyboardButton(
            "◀️ К месту",
            callback_data=f"tp_back_place:{state['place_id']}",
        )
    return InlineKeyboardButton("◀️ К списку", callback_data="tp_hub")


def _error_header(kind: str) -> str:
    return {
        "stale": "⚠️ Шаблон изменился в другом окне",
        "conflict": "⚠️ Не удалось сохранить из-за одновременной записи",
        "forbidden": "⚠️ Нет прав на изменение шаблона",
        "validation": "⚠️ Текст не сохранён",
        "syntax": "⚠️ Синтаксическая ошибка",
        "unknown": "⚠️ Неизвестный кирпичик",
        "invalid": "⚠️ Текст не прошёл проверку",
        "unexpected": "⚠️ Не удалось сохранить изменения",
    }.get(kind, "⚠️ Текст не сохранён")


def _section_error_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    kind = str(state.get("error_kind") or "unexpected")
    message = str(state.get("error_message") or "Повторите попытку.")
    draft = state.get("draft_text", "")
    lines = [
        _error_header(kind),
        "",
        _h(_clip(message, ERROR_TEXT_LIMIT)),
        "",
        "Ваш текст сохранён:" if draft else "Секция сейчас пуста.",
    ]
    if draft:
        lines.append(_display(draft))
    lines.append("")
    if kind == "stale":
        lines.append(
            "Новая версия не создана: чужие изменения не перезаписаны. "
            "Обновите версию, проверьте текст и подтвердите сохранение."
        )
    elif kind == "conflict":
        lines.append(
            "Данные не потеряны. Повторите сохранение; при повторной "
            "неоднозначности выберите кирпичик кнопками."
        )
    elif kind == "forbidden":
        lines.append("Изменение невозможно: права на шаблон больше не действуют.")
    else:
        lines.append(_TEXT_INPUT_ERROR)

    template_id = state["template_id"]
    rows: list[list[InlineKeyboardButton]] = []
    if kind == "stale":
        rows.append(
            [
                InlineKeyboardButton(
                    "🔄 Обновить версию и повторить", callback_data="tp_refresh"
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")]
        )
    elif kind == "conflict":
        rows.append(
            [
                InlineKeyboardButton(
                    "💾 Повторить сохранение", callback_data="tp_retry_save"
                )
            ]
        )
        rows.append(
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")]
        )
    elif kind == "forbidden":
        rows.append(
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")]
        )
        rows.append([_section_back_button(state)])
    else:
        rows.append(
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "📋 Доступные кирпичики",
                    callback_data=f"tp_bricks:{template_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    "◀️ К шаблону",
                    callback_data=f"tp_open:{template_id}",
                )
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _access_error_content(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    draft = state.get("draft_text", "")
    lines = [
        "⚠️ Шаблон больше недоступен",
        "",
        "Права изменились или шаблон удалён. Текст ниже сохранён в сессии, "
        "но записать его в этот шаблон уже нельзя.",
        "",
        "Ваш текст:" if draft else "Секция сейчас пуста.",
    ]
    if draft:
        lines.append(_display(draft))
    return "\n".join(lines), InlineKeyboardMarkup([[_section_back_button(state)]])


def _chosen_rows(state: dict) -> list[str]:
    chosen = _chosen_map(state)
    if not chosen:
        return []
    return [f"[{_h(code)}]" for code in sorted(chosen)]


def _confirm_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    section = state["section"]
    title = SECTION_GENITIVE[section]
    draft = state.get("draft_text", "")
    lines = [
        f"✅ Предпросмотр и подтверждение сохранения ({title})",
        "",
        _display(draft),
        "",
        "Остальные секции не меняются. Новая версия создастся только после "
        "нажатия «Сохранить».",
    ]
    chosen = _chosen_rows(state)
    if chosen:
        lines.extend(["", "Явно выбраны кирпичики: " + ", ".join(chosen) + "."])
    template_id = state["template_id"]
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💾 Сохранить", callback_data="tp_save")],
            [InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")],
            [
                InlineKeyboardButton(
                    "📋 Доступные кирпичики",
                    callback_data=f"tp_bricks:{template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    "◀️ К шаблону",
                    callback_data=f"tp_open:{template_id}",
                )
            ],
        ]
    )
    return "\n".join(lines), keyboard


def _ambiguties(state: dict) -> list[dict[str, Any]]:
    raw = state.get("pending_ambiguities")
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for group in raw:
        if not isinstance(group, dict):
            continue
        code = group.get("code")
        candidates = group.get("candidates")
        if not isinstance(code, str) or not isinstance(candidates, list):
            continue
        clean_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            brick_id = candidate.get("brick_id")
            if not _positive_int(brick_id):
                continue
            clean_candidates.append(candidate)
        if not clean_candidates:
            continue
        sections = [
            section
            for section in group.get("sections", [])
            if section in TEMPLATE_SECTIONS
        ]
        result.append(
            {
                "code": code,
                "sections": sections,
                "candidates": clean_candidates,
            }
        )
    return result


def _ambiguity_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    groups = _ambiguties(state)
    lines = [
        "❓ Неоднозначный кирпичик",
        "",
        "Один и тот же код встречается у нескольких доступных кирпичиков. "
        "Выберите конкретный вариант: в шаблон сохранится его brick_id, а не "
        "повторное угадывание кода.",
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for group_index, group in enumerate(groups):
        code = str(group["code"])
        sections = ", ".join(
            SECTION_TITLES.get(section, section) for section in group["sections"]
        )
        lines.extend(
            [
                "",
                f"Код [{_h(code)}]" + (f" (секции: {sections})" if sections else ""),
            ]
        )
        for candidate_index, candidate in enumerate(group["candidates"]):
            scope = SCOPE_TITLES.get(
                str(candidate.get("scope")), str(candidate.get("scope"))
            )
            title = _h(str(candidate.get("title") or "Кирпичик"))
            lines.append(f"{candidate_index + 1}. {title} · {scope}")
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(
                            f"{candidate_index + 1}. "
                            f"{candidate.get('title') or 'Кирпичик'} · {scope}"
                        ),
                        callback_data=(f"tp_pick:{group_index}:{candidate_index}"),
                    )
                ]
            )
    template_id = state["template_id"]
    rows.append([InlineKeyboardButton("✏️ Исправить текст", callback_data="tp_retry")])
    rows.append(
        [
            InlineKeyboardButton(
                "📋 Доступные кирпичики",
                callback_data=f"tp_bricks:{template_id}",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton("◀️ К шаблону", callback_data=f"tp_open:{template_id}")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# --- Экраны: кирпичики и справка --------------------------------------------


def _brick_list_content(
    session: Session,
    template: TemplateDetails,
    telegram_user_id: int,
    offset: int,
) -> tuple[str, InlineKeyboardMarkup, int]:
    if template.kind == TEMPLATE_KIND_STANDARD:
        viewer = None
        place_id = template.place_id
        scope_hint = (
            "Для стандартного шаблона доступны системные кирпичики и "
            "кирпичики этого места."
        )
    else:
        viewer = telegram_user_id
        place_id = None
        scope_hint = (
            "Для личного шаблона доступны системные кирпичики и ваши личные "
            "кирпичики."
        )
    bricks = list_available_bricks(
        session,
        viewer_telegram_user_id=viewer,
        place_id=place_id,
    )
    total = len(bricks)
    max_offset = ((total - 1) // BRICK_PAGE_SIZE) * BRICK_PAGE_SIZE if total else 0
    offset = min(max(0, offset), max_offset)
    page_items = bricks[offset : offset + BRICK_PAGE_SIZE]
    lines = [
        f"📋 Доступные кирпичики\n\n{scope_hint}",
        f"Всего: {total}.",
    ]
    if page_items:
        lines.append("")
        for brick in page_items:
            type_title = BRICK_TYPE_TITLES.get(brick.value_type, brick.value_type)
            scope = SCOPE_TITLES.get(brick.scope, brick.scope)
            lines.append(
                f"• [{_h(brick.code)}] — {_h(brick.title)} " f"· {type_title} · {scope}"
            )
        if total > BRICK_PAGE_SIZE:
            lines.append(
                f"\nПоказаны {offset + 1}–{offset + len(page_items)} из {total}."
            )
    else:
        lines.append("\nНет доступных кирпичиков.")
    navigation: list[InlineKeyboardButton] = []
    if offset > 0:
        navigation.append(
            InlineKeyboardButton(
                "◀️",
                callback_data=f"tp_bpage:{max(0, offset - BRICK_PAGE_SIZE)}",
            )
        )
    if offset + BRICK_PAGE_SIZE < total:
        navigation.append(
            InlineKeyboardButton(
                "▶️", callback_data=f"tp_bpage:{offset + BRICK_PAGE_SIZE}"
            )
        )
    rows: list[list[InlineKeyboardButton]] = []
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="tp_back")])
    return "\n".join(lines), InlineKeyboardMarkup(rows), offset


def _help_content() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "❓ Оформление текста шаблона\n\n"
        "• [код] — подставить кирпичик, например [название] или [дата].\n"
        "• [[ и ]] — вывести квадратные скобки как обычный текст.\n"
        "• **жирный**, *курсив*, __подчёркнутый__, ~~зачёркнутый~~.\n"
        "• Ссылка: Текст(https://example.com) или Текст([ссылка_места]).\n"
        "• Служебные символы экранируются обратной косой чертой: "
        "\\* \\_ \\~ \\( \\) \\[ \\].\n\n"
        "Текст отправляется целиком: переводы строк, отступы и Unicode "
        "сохраняются. Фотографии — отдельные вложения поста, в шаблон они "
        "не входят."
    )
    return text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Назад", callback_data="tp_back")]]
    )


# --- Экраны: создание --------------------------------------------------------


def _create_name_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    current = state.get("name")
    lines = ["➕ Создание шаблона"]
    if isinstance(current, str) and current.strip():
        lines.extend(["", f"Текущее название: «{_h(current)}»"])
    lines.extend(["", "Введите название нового шаблона:"])
    return "\n".join(lines), InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀️ Отмена", callback_data="tp_cancel")]]
    )


def _create_choice_content(
    name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"Шаблон «{_h(name)}»\n\n"
        "Выберите начальное содержимое:\n"
        "• «На основе стандартного» — встроенный текст Poster с системными "
        "кирпичиками;\n"
        "• «Пустой шаблон» — минимальная заготовка, которую вы заполните "
        "сами."
    )
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📄 На основе стандартного", callback_data="tp_cstd"
                )
            ],
            [InlineKeyboardButton("📄 Пустой шаблон", callback_data="tp_cempty")],
            [InlineKeyboardButton("✏️ Изменить название", callback_data="tp_cname")],
            [InlineKeyboardButton("◀️ Отмена", callback_data="tp_cancel")],
        ]
    )


def _create_confirm_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    name = str(state.get("name") or "")
    mode = str(state.get("mode") or "")
    header = str(state.get("create_header") or "")
    body = str(state.get("create_body") or "")
    footer = str(state.get("create_footer") or "")
    mode_title = (
        "на основе стандартного"
        if mode == "standard"
        else "пустой шаблон (минимальная заготовка)"
    )
    lines = [
        "📄 Проверьте новый шаблон",
        "",
        f"Название: «{_h(name)}»",
        f"Основа: {mode_title}",
        "",
        f"📌 Шапка:\n{_display(header)}",
        "",
        f"📝 Тело:\n{_display(body)}",
        "",
        f"📌 Подвал:\n{_display(footer)}",
    ]
    if mode == "empty":
        lines.extend(
            [
                "",
                "Совсем пустой шаблон база не принимает, поэтому в теле "
                "остаётся минимальный кирпичик [название]. Его можно заменить "
                "или дополнить после создания.",
            ]
        )
    error = state.get("error_message")
    if isinstance(error, str) and error:
        lines.extend(["", f"⚠️ {_h(_clip(error, ERROR_TEXT_LIMIT))}"])
    lines.extend(["", "Подтвердите создание?"])
    return "\n".join(lines), InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💾 Создать", callback_data="tp_csave")],
            [InlineKeyboardButton("◀️ К выбору основы", callback_data="tp_cback")],
            [InlineKeyboardButton("✏️ Изменить название", callback_data="tp_cname")],
            [InlineKeyboardButton("◀️ Отмена", callback_data="tp_cancel")],
        ]
    )


# --- Экранное ядро -----------------------------------------------------------


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
    Сохраняет состояние, рисует экран и только потом чистит временный ввод.

    Если запись сессии не удалась, пользовательский текст не удаляется:
    его можно отправить ещё раз.
    """
    user_id = ctx.user(update).id
    body = f"{notice}\n\n{text}" if notice else text
    saved = save_template_session(user_id, state)
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


async def _render_now(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    keyboard: InlineKeyboardMarkup,
) -> None:
    """Рисует экран, не создавая строку template_sessions (для отмены)."""
    user_id = ctx.user(update).id
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(context, user_data, chat_id, text, keyboard)
    screen.save_screen_ref(user_id, user_data, chat_id)


async def _render_hub(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: Session,
    telegram_user_id: int,
    state: dict,
    *,
    notice: str | None = None,
) -> bool:
    templates = list_personal_templates(session, telegram_user_id)
    text, keyboard = _hub_content(templates, _page_value(state.get("page")))
    return await _show(update, context, state, text, keyboard, notice=notice)


async def _render_phase(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    cleanup_ids: list[int] | None = None,
    drop_input: bool = False,
    check_revision: bool = True,
) -> bool:
    """Единая точка отрисовки фазы, включая восстановление после рестарта."""
    if not _valid_state(state):
        if not isinstance(state, dict):
            state = _new_hub()
        else:
            page = state.get("page")
            state.clear()
            state.update(_new_hub(_page_value(page)))
        notice = notice or "Черновик шаблона повреждён — раздел открыт заново."

    phase = state.get("phase")
    if phase == PHASE_SAVING:
        state["phase"] = phase = PHASE_CONFIRM_SAVE
        notice = notice or (
            "Сохранение не было подтверждено. Проверьте текст и повторите."
        )
    elif phase == PHASE_CREATE_SAVING:
        state["phase"] = phase = PHASE_CREATE_CONFIRM
        notice = notice or (
            "Создание не было подтверждено. Проверьте, не создан ли шаблон "
            "ранее, и повторите при необходимости."
        )

    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)

        if phase == PHASE_HUB:
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, _page_value(state.get("page")))
        elif phase == PHASE_PICK:
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _pick_content(templates, _page_value(state.get("page")))
        elif phase in _CREATE_PHASES:
            if phase == PHASE_AWAIT_NAME:
                text, keyboard = _create_name_content(state)
            elif phase == PHASE_CREATE_CHOICE:
                text, keyboard = _create_choice_content(str(state.get("name")))
            else:
                text, keyboard = _create_confirm_content(state)
        elif phase in _TEMPLATE_PHASES:
            template = _load_editable(
                session, telegram_user_id, state.get("template_id")
            )
            if template is not None and template.kind == TEMPLATE_KIND_STANDARD:
                if state.get("origin") != "admin":
                    template = None
            if template is None:
                if phase == PHASE_SECTION_ERROR:
                    text, keyboard = _access_error_content(state)
                else:
                    page = state.get("page") if isinstance(state, dict) else 0
                    state.clear()
                    state.update(_new_hub(_page_value(page)))
                    notice = notice or "Шаблон не найден или больше недоступен."
                    templates = list_personal_templates(session, telegram_user_id)
                    text, keyboard = _hub_content(
                        templates, _page_value(state.get("page"))
                    )
            else:
                state["origin"] = _template_origin(template)
                state["place_id"] = template.place_id
                place_name = _place_name(session, template.place_id)
                if phase == PHASE_CARD:
                    state["revision"] = template.revision
                    text, keyboard = _card_content(template, place_name=place_name)
                elif phase == PHASE_PREVIEW:
                    state["revision"] = template.revision
                    text, keyboard = _preview_content(template, place_name=place_name)
                elif phase == PHASE_RESET_CONFIRM:
                    state["revision"] = template.revision
                    text, keyboard = _reset_confirm_content(template)
                elif phase == PHASE_SECTION_EDIT:
                    if check_revision and template.revision != state.get("revision"):
                        notice = notice or (
                            "Шаблон изменился в другом окне. Текст сохранён; "
                            "при сохранении потребуется обновить версию."
                        )
                    text, keyboard = _section_edit_content(state)
                elif phase == PHASE_CONFIRM_SAVE:
                    if check_revision and template.revision != state.get("revision"):
                        state["phase"] = PHASE_SECTION_ERROR
                        state["error_kind"] = "stale"
                        state["error_message"] = (
                            "Шаблон уже изменился в другом окне. "
                            "Ваш текст не записан."
                        )
                        text, keyboard = _section_error_content(state)
                    else:
                        text, keyboard = _confirm_content(state)
                elif phase == PHASE_SECTION_ERROR:
                    text, keyboard = _section_error_content(state)
                elif phase == PHASE_RESOLVE_AMBIGUITY:
                    if check_revision and template.revision != state.get("revision"):
                        state["phase"] = PHASE_SECTION_ERROR
                        state["error_kind"] = "stale"
                        state["error_message"] = (
                            "Шаблон уже изменился в другом окне. "
                            "Явный выбор кирпичика не сохранён."
                        )
                        text, keyboard = _section_error_content(state)
                    elif not _ambiguties(state):
                        state["phase"] = PHASE_CONFIRM_SAVE
                        text, keyboard = _confirm_content(state)
                    else:
                        text, keyboard = _ambiguity_content(state)
                elif phase == PHASE_BRICK_LIST:
                    offset = _page_value(state.get("brick_offset", 0))
                    text, keyboard, actual_offset = _brick_list_content(
                        session, template, telegram_user_id, offset
                    )
                    state["brick_offset"] = actual_offset
                elif phase == PHASE_HELP:
                    text, keyboard = _help_content()
                else:  # pragma: no cover — защита от будущих фаз
                    state.clear()
                    state.update(_new_hub())
                    text, keyboard = _hub_content([], 0)
                    notice = notice or "Раздел открыт заново."
        else:  # pragma: no cover — _valid_state уже отсекает
            state.clear()
            state.update(_new_hub())
            text, keyboard = _hub_content([], 0)
            notice = notice or "Раздел открыт заново."
    except Exception as exc:  # noqa: BLE001 — экран не должен падать
        logger.exception("Не удалось отрисовать раздел шаблонов: %s", exc)
        text = (
            "⚠️ Не удалось загрузить раздел шаблонов.\n\n"
            "Черновик сохранён. Попробуйте повторить или вернитесь в меню."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Повторить", callback_data="tp_retry_render")],
                [InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")],
            ]
        )
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


def _set_section_error(state: dict, kind: str, message: str) -> None:
    state["phase"] = PHASE_SECTION_ERROR
    state["error_kind"] = kind
    state["error_message"] = message
    state.pop("pending_ambiguities", None)


def _pending_ambiguities(bindings: TemplateBindings) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    by_code: dict[str, dict[str, Any]] = {}
    for ambiguous in bindings.ambiguous_codes:
        group = by_code.get(ambiguous.code)
        if group is None:
            group = {
                "code": ambiguous.code,
                "sections": [],
                "candidates": [
                    {
                        "brick_id": candidate.brick_id,
                        "title": candidate.title,
                        "scope": candidate.scope,
                        "status": candidate.status,
                    }
                    for candidate in ambiguous.candidates
                ],
            }
            by_code[ambiguous.code] = group
            groups.append(group)
        if ambiguous.section not in group["sections"]:
            group["sections"].append(ambiguous.section)
    return groups


async def _validate_and_show(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
    *,
    notice: str | None = None,
    drop_input: bool = False,
) -> bool:
    """
    Проверяет черновик Stage 24 и показывает подтверждение/ошибку/выбор.

    Никаких доменных записей здесь нет: версия появится только после
    отдельного нажатия «Сохранить».
    """
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        template = _load_editable(session, telegram_user_id, state.get("template_id"))
        if template is None:
            page = _page_value(state.get("page"))
            state.clear()
            state.update(_new_hub(page))
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, page)
            return await _show(
                update,
                context,
                state,
                text,
                keyboard,
                notice="Шаблон не найден или больше недоступен.",
            )
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            state.clear()
            state.update(_new_hub())
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, state["page"])
            return await _show(
                update,
                context,
                state,
                text,
                keyboard,
                notice="Шаблон доступен только администратору места.",
            )
        state["origin"] = _template_origin(template)
        state["place_id"] = template.place_id

        if template.revision != state.get("revision"):
            _set_section_error(
                state,
                "stale",
                "Шаблон уже изменился в другом окне. Ваш текст не записан. "
                "Обновите версию и повторите проверку.",
            )
        else:
            current = template.current_version
            sections = {
                TEMPLATE_SECTION_HEADER: current.header,
                TEMPLATE_SECTION_BODY: current.body,
                TEMPLATE_SECTION_FOOTER: current.footer,
            }
            sections[state["section"]] = state.get("draft_text", "")
            try:
                checked_header, checked_body, checked_footer = (
                    validate_template_sections(
                        sections[TEMPLATE_SECTION_HEADER],
                        sections[TEMPLATE_SECTION_BODY],
                        sections[TEMPLATE_SECTION_FOOTER],
                    )
                )
            except TemplateValidationError as exc:
                _set_section_error(state, "validation", str(exc))
            else:
                compiled = resolve_template_bindings(
                    session,
                    {
                        TEMPLATE_SECTION_HEADER: checked_header,
                        TEMPLATE_SECTION_BODY: checked_body,
                        TEMPLATE_SECTION_FOOTER: checked_footer,
                    },
                    actor_telegram_user_id=telegram_user_id,
                    place_id=template.place_id,
                    chosen_brick_ids=_chosen_map(state),
                )
                if compiled.parse_errors:
                    message = "\n".join(
                        _compile_error_text(error) for error in compiled.parse_errors
                    )
                    _set_section_error(state, "syntax", message)
                elif compiled.unknown_codes:
                    codes = ", ".join(f"[{code}]" for code in compiled.unknown_codes)
                    _set_section_error(
                        state,
                        "unknown",
                        f"Неизвестные кирпичики: {codes}. Проверьте код в "
                        "«Доступных кирпичиках» или исправьте текст.",
                    )
                elif compiled.ambiguous_codes:
                    state["phase"] = PHASE_RESOLVE_AMBIGUITY
                    state["pending_ambiguities"] = _pending_ambiguities(compiled)
                    state.pop("error_kind", None)
                    state.pop("error_message", None)
                else:
                    state["phase"] = PHASE_CONFIRM_SAVE
                    state.pop("pending_ambiguities", None)
                    state.pop("error_kind", None)
                    state.pop("error_message", None)
    except TemplateCompileError as exc:
        _set_section_error(state, "invalid", _compile_error_text(exc))
    except Exception as exc:  # noqa: BLE001 — база/сервис не должны ломать экран
        logger.exception("Не удалось проверить шаблон: %s", exc)
        _set_section_error(
            state,
            "unexpected",
            "Не удалось проверить текст из-за временной ошибки. "
            "Черновик сохранён; повторите попытку.",
        )
    finally:
        session.close()

    # Содержимое выбирается по итоговой фазе после закрытия сессии.
    if state.get("phase") == PHASE_CONFIRM_SAVE:
        text, keyboard = _confirm_content(state)
    elif state.get("phase") == PHASE_RESOLVE_AMBIGUITY:
        text, keyboard = _ambiguity_content(state)
    else:
        text, keyboard = _section_error_content(state)
    return await _show(
        update,
        context,
        state,
        text,
        keyboard,
        notice=notice,
        drop_input=drop_input,
    )


# --- Обработчики: вход и навигация ------------------------------------------


async def open_templates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вход в раздел: кнопка меню, /templates или продолжение после рестарта."""
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    telegram_user_id = ctx.user(update).id
    clear_place_session(telegram_user_id)
    clear_admin_session(telegram_user_id)
    clear_brick_session(telegram_user_id)

    state = load_template_session(telegram_user_id)
    if not _valid_state(state):
        state = _new_hub()
    await _render_phase(update, context, state, drop_input=True)


async def open_template_picker(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    «📝 Создать по личному шаблону»: список личных шаблонов первым экраном.

    Раздел тот же, что «Мои шаблоны» (`/templates`), но вход сразу
    ориентирован на запуск анонса: тап по названию начинает конструктор.
    Правка и создание остаются доступны здесь же.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    telegram_user_id = ctx.user(update).id
    clear_place_session(telegram_user_id)
    clear_admin_session(telegram_user_id)
    clear_brick_session(telegram_user_id)
    state = _new_pick()
    await _render_phase(update, context, state, drop_input=True)


async def handle_pick_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пагинация списка выбора личного шаблона."""
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_PICK:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    page = _callback_id(query.data or "", "tp_pickpage:")
    if page is None:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["page"] = max(0, page)
    await _render_phase(update, context, state)


async def handle_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_HUB:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    page = _callback_id(query.data or "", "tp_page:")
    if page is None:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["page"] = max(0, page)
    await _render_phase(update, context, state)


async def handle_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    phase = state.get("phase")
    if phase not in {
        PHASE_HUB,
        PHASE_CARD,
        PHASE_PREVIEW,
        PHASE_RESET_CONFIRM,
        PHASE_SECTION_ERROR,
        PHASE_BRICK_LIST,
        PHASE_HELP,
    }:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    page = _page_value(state.get("page"))
    state.clear()
    state.update(_new_hub(page))
    await _render_phase(update, context, state)


async def handle_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_open:")
    if template_id is None:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if state.get("phase") not in {PHASE_HUB, PHASE_PICK} and template_id != state.get(
        "template_id"
    ):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        new_state = _card_state(
            template,
            origin=_template_origin(template),
            page=_page_value(state.get("page")),
        )
        text, keyboard = _card_content(
            template, place_name=_place_name(session, template.place_id)
        )
    finally:
        session.close()
    await _show(update, context, new_state, text, keyboard)


async def handle_edit_section(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CARD:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    # tp_edit_h:<id> | tp_edit_b:<id> | tp_edit_f:<id>
    try:
        prefix, raw_id = data.split(":", 1)
        short = prefix.rsplit("_", 1)[-1]
        section = SECTION_SHORT[short]
        template_id = int(raw_id)
    except (KeyError, TypeError, ValueError):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        current = template.current_version
        state.update(
            {
                "phase": PHASE_SECTION_EDIT,
                "template_id": template.template_id,
                "revision": template.revision,
                "origin": _template_origin(template),
                "place_id": template.place_id,
                "section": section,
                "draft_text": getattr(current, section),
                "chosen_brick_ids": {},
            }
        )
        state.pop("error_kind", None)
        state.pop("error_message", None)
        state.pop("pending_ambiguities", None)
        text, keyboard = _section_edit_content(state)
    finally:
        session.close()
    await _show(update, context, state, text, keyboard)


async def handle_preview(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CARD:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_preview:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        state.update(
            {
                "phase": PHASE_PREVIEW,
                "revision": template.revision,
                "origin": _template_origin(template),
                "place_id": template.place_id,
            }
        )
        text, keyboard = _preview_content(
            template, place_name=_place_name(session, template.place_id)
        )
    finally:
        session.close()
    await _show(update, context, state, text, keyboard)


# --- Обработчики: сброс ------------------------------------------------------


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CARD:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_reset:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        state.update(
            {
                "phase": PHASE_RESET_CONFIRM,
                "revision": template.revision,
                "origin": _template_origin(template),
                "place_id": template.place_id,
            }
        )
        text, keyboard = _reset_confirm_content(template)
    finally:
        session.close()
    await _show(update, context, state, text, keyboard)


async def handle_reset_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_RESET_CONFIRM:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_reset_yes:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    new_state: dict | None = None
    text = ""
    keyboard: InlineKeyboardMarkup | None = None
    notice: str | None = None
    alert: str | None = None
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            alert = "Шаблон не найден или больше недоступен."
        elif template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            alert = DENIED_TEXT
        elif template.revision != state.get("revision"):
            new_state = _card_state(
                template,
                origin=_template_origin(template),
                page=_page_value(state.get("page")),
            )
            text, keyboard = _card_content(
                template, place_name=_place_name(session, template.place_id)
            )
            notice = (
                "Сброс не выполнен: шаблон уже изменился в другом окне. "
                "Проверьте актуальную версию и повторите."
            )
        else:
            reset = (
                reset_standard_template
                if template.kind == TEMPLATE_KIND_STANDARD
                else reset_personal_template
            )
            result = reset(
                session,
                actor_telegram_user_id=telegram_user_id,
                template_id=template_id,
                expected_revision=int(state["revision"]),
                confirm=True,
            )
            current = result.template
            if current is None:
                current = get_accessible_template(
                    session, telegram_user_id, template_id
                )
            if result.ok and current is not None:
                new_state = _card_state(
                    current,
                    origin=_template_origin(current),
                    page=_page_value(state.get("page")),
                )
                text, keyboard = _card_content(
                    current,
                    place_name=_place_name(session, current.place_id),
                )
                notice = (
                    "Шаблон сброшен к встроенному стандарту. "
                    f"Текущая версия: {current.revision}."
                    if result.status == "updated"
                    else "Шаблон уже совпадает со встроенным стандартом; "
                    "новая версия не создана."
                )
            elif result.status == "stale":
                current = result.template or get_accessible_template(
                    session, telegram_user_id, template_id
                )
                if current is None:
                    alert = "Шаблон не найден."
                else:
                    new_state = _card_state(
                        current,
                        origin=_template_origin(current),
                        page=_page_value(state.get("page")),
                    )
                    text, keyboard = _card_content(
                        current,
                        place_name=_place_name(session, current.place_id),
                    )
                    notice = (
                        "Сброс не выполнен: шаблон уже изменился в другом "
                        "окне. Проверьте актуальную версию и повторите."
                    )
            elif result.status == "forbidden":
                alert = result.message or "Недостаточно прав."
                new_state = _new_hub(_page_value(state.get("page")))
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, new_state["page"])
                notice = result.message or "Права на шаблон больше не действуют."
            elif result.status == "not_found":
                new_state = _new_hub(_page_value(state.get("page")))
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, new_state["page"])
                notice = "Шаблон больше не существует."
            else:
                new_state = _card_state(
                    template,
                    origin=_template_origin(template),
                    page=_page_value(state.get("page")),
                )
                text, keyboard = _card_content(
                    template,
                    place_name=_place_name(session, template.place_id),
                )
                notice = result.message or "Сброс не выполнен."
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось сбросить шаблон: %s", exc)
        alert = "Не удалось сбросить шаблон. Повторите попытку."
    finally:
        session.close()

    if alert is not None:
        await query.answer(alert, show_alert=True)
        return
    await query.answer()
    if new_state is not None and keyboard is not None:
        await _show(update, context, new_state, text, keyboard, notice=notice)


# --- Обработчики: доступные кирпичики и справка -----------------------------


async def handle_bricks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    phase = state.get("phase")
    if phase not in {
        PHASE_CARD,
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
    }:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_bricks:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        state.update(
            {
                "phase": PHASE_BRICK_LIST,
                "back_phase": phase,
                "brick_offset": 0,
                "origin": _template_origin(template),
                "place_id": template.place_id,
            }
        )
        text, keyboard, actual_offset = _brick_list_content(
            session, template, telegram_user_id, 0
        )
        state["brick_offset"] = actual_offset
    finally:
        session.close()
    await _show(update, context, state, text, keyboard)


async def handle_bricks_page(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_BRICK_LIST:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    offset = _callback_id(query.data or "", "tp_bpage:")
    if offset is None or offset < 0:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["brick_offset"] = offset
    await _render_phase(update, context, state)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_SECTION_EDIT:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    try:
        _prefix, short, raw_id = data.split(":", 2)
        section = SECTION_SHORT[short]
        template_id = int(raw_id)
    except (KeyError, TypeError, ValueError):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if template_id != state.get("template_id") or section != state.get("section"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = PHASE_HELP
    state["back_phase"] = PHASE_SECTION_EDIT
    text, keyboard = _help_content()
    await _show(update, context, state, text, keyboard)


async def handle_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    phase = state.get("phase")
    if phase not in {PHASE_BRICK_LIST, PHASE_HELP}:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    back_phase = state.get("back_phase")
    if back_phase not in {
        PHASE_CARD,
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
    }:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = back_phase
    state.pop("back_phase", None)
    await _render_phase(update, context, state, check_revision=False)


# --- Обработчики: редактирование секции -------------------------------------


async def handle_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_SECTION_EDIT:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["draft_text"] = ""
    state["chosen_brick_ids"] = {}
    await _validate_and_show(
        update,
        context,
        state,
        notice=("Секция очищена. Проверьте результат и подтвердите сохранение."),
    )


async def handle_retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if state.get("phase") not in {
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
    }:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = PHASE_SECTION_EDIT
    state.pop("error_kind", None)
    state.pop("error_message", None)
    state.pop("pending_ambiguities", None)
    text, keyboard = _section_edit_content(state)
    await _show(update, context, state, text, keyboard)


async def handle_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if (
        not _valid_state(state)
        or state.get("phase") != PHASE_SECTION_ERROR
        or state.get("error_kind") != "stale"
    ):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, state.get("template_id"))
        if template is None:
            await query.answer("Шаблон не найден.", show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        state["revision"] = template.revision
        state["origin"] = _template_origin(template)
        state["place_id"] = template.place_id
        state.pop("error_kind", None)
        state.pop("error_message", None)
    finally:
        session.close()
    await _validate_and_show(
        update,
        context,
        state,
        notice=(
            f"Версия обновлена до {state['revision']}. "
            "Ваш текст сохранён — проверьте его и подтвердите."
        ),
    )


async def handle_retry_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if (
        not _valid_state(state)
        or state.get("phase") != PHASE_SECTION_ERROR
        or state.get("error_kind") != "conflict"
    ):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = PHASE_CONFIRM_SAVE
    state.pop("error_kind", None)
    state.pop("error_message", None)
    await _render_phase(update, context, state)


# --- Обработчики: явный выбор кирпичика -------------------------------------


async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_RESOLVE_AMBIGUITY:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    try:
        _prefix, raw_group, raw_candidate = (query.data or "").split(":", 2)
        group_index = int(raw_group)
        candidate_index = int(raw_candidate)
    except (TypeError, ValueError):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    groups = _ambiguties(state)
    if not 0 <= group_index < len(groups):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    group = groups[group_index]
    candidates = group["candidates"]
    if not 0 <= candidate_index < len(candidates):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    candidate = candidates[candidate_index]
    brick_id = candidate.get("brick_id")
    if not _positive_int(brick_id):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    chosen = dict(_chosen_map(state) or {})
    chosen[str(group["code"])] = int(brick_id)
    state["chosen_brick_ids"] = chosen
    title = str(candidate.get("title") or "кирпичик")
    await _validate_and_show(
        update,
        context,
        state,
        notice=f"Выбран кирпичик «{title}». Проверяем текст.",
    )


# --- Обработчики: сохранение секции -----------------------------------------


async def handle_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CONFIRM_SAVE:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if state.get("section") not in TEMPLATE_SECTIONS or not isinstance(
        state.get("draft_text"), str
    ):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    telegram_user_id = ctx.user(update).id
    template_id = int(state["template_id"])
    section = state["section"]
    draft_text = state["draft_text"]
    # Защита от двойного нажатия: повторный callback увидит другую фазу.
    state["phase"] = PHASE_SAVING
    save_template_session(telegram_user_id, state)

    session = SessionLocal()
    new_state: dict | None = None
    text = ""
    keyboard: InlineKeyboardMarkup | None = None
    notice: str | None = None
    alert: str | None = None
    try:
        result = update_template_section(
            session,
            actor_telegram_user_id=telegram_user_id,
            template_id=template_id,
            expected_revision=int(state["revision"]),
            section=section,
            content=draft_text,
            chosen_brick_ids=_chosen_map(state),
        )
        if result.ok and result.template is not None:
            current = result.template
            new_state = _card_state(
                current,
                origin=_template_origin(current),
                page=_page_value(state.get("page")),
            )
            text, keyboard = _card_content(
                current, place_name=_place_name(session, current.place_id)
            )
            notice = (
                f"Сохранено в версии {current.revision}."
                if result.status == "updated"
                else "Содержимое не изменилось; новая версия не создана."
            )
        elif result.status == "stale":
            stale_current = result.template or get_accessible_template(
                session, telegram_user_id, template_id
            )
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "stale"
            state["error_message"] = (
                "Шаблон уже изменился в другом окне. Ваш текст сохранён, "
                "чужие изменения не перезаписаны."
            )
            state.pop("pending_ambiguities", None)
            if stale_current is not None:
                state["origin"] = _template_origin(stale_current)
                state["place_id"] = stale_current.place_id
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "forbidden":
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "forbidden"
            state["error_message"] = result.message or (
                "Права на изменение шаблона больше не действуют."
            )
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "invalid":
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "invalid"
            state["error_message"] = _localize_sections(
                result.message or "Текст не прошёл проверку."
            )
            state.pop("pending_ambiguities", None)
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "conflict":
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "conflict"
            state["error_message"] = result.message or (
                "Не удалось сохранить из-за одновременной записи. " "Повторите попытку."
            )
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "not_found":
            page = _page_value(state.get("page"))
            state.clear()
            state.update(_new_hub(page))
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, page)
            new_state = state
            notice = "Шаблон больше не существует; изменения не сохранены."
        else:  # pragma: no cover — будущие статусы сервиса
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "unexpected"
            state["error_message"] = result.message or "Не удалось сохранить."
            new_state = state
            text, keyboard = _section_error_content(state)
    except (TemplateValidationError, TemplateCompileError) as exc:
        state["phase"] = PHASE_SECTION_ERROR
        state["error_kind"] = "invalid"
        state["error_message"] = _compile_error_text(exc)
        state.pop("pending_ambiguities", None)
        new_state = state
        text, keyboard = _section_error_content(state)
    except (TemplateActorError, TemplatePermissionError) as exc:
        state["phase"] = PHASE_SECTION_ERROR
        state["error_kind"] = "forbidden"
        state["error_message"] = str(exc)
        new_state = state
        text, keyboard = _section_error_content(state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось сохранить секцию шаблона: %s", exc)
        state["phase"] = PHASE_SECTION_ERROR
        state["error_kind"] = "conflict"
        state["error_message"] = (
            "Не удалось сохранить из-за временной ошибки. "
            "Ваш текст сохранён; повторите попытку."
        )
        new_state = state
        text, keyboard = _section_error_content(state)
    finally:
        session.close()

    if alert is not None:
        await query.answer(alert, show_alert=True)
        return
    if new_state is not None and keyboard is not None:
        await _show(update, context, new_state, text, keyboard, notice=notice)


# --- Обработчики: создание ---------------------------------------------------


async def handle_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") not in {PHASE_HUB, PHASE_PICK}:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    page = _page_value(state.get("page"))
    state.clear()
    state.update(
        {
            "phase": PHASE_AWAIT_NAME,
            "page": page,
            "name": "",
            "failed_inputs": [],
        }
    )
    text, keyboard = _create_name_content(state)
    await _show(update, context, state, text, keyboard)


async def handle_create_name_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") not in {
        PHASE_CREATE_CHOICE,
        PHASE_CREATE_CONFIRM,
    }:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = PHASE_AWAIT_NAME
    state.pop("error_message", None)
    text, keyboard = _create_name_content(state)
    await _show(update, context, state, text, keyboard)


async def handle_create_back(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CREATE_CONFIRM:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    state["phase"] = PHASE_CREATE_CHOICE
    for key in ("create_header", "create_body", "create_footer", "mode"):
        state.pop(key, None)
    state.pop("error_message", None)
    text, keyboard = _create_choice_content(str(state.get("name")))
    await _show(update, context, state, text, keyboard)


async def _create_from(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    mode: str,
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CREATE_CHOICE:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    if mode == "standard":
        builtin = get_builtin_template()
        header, body, footer = builtin.header, builtin.body, builtin.footer
    else:
        # Backend запрещает шаблон, где все три секции пусты. Минимальная
        # безопасная заготовка сохраняет пользователю «пустой» старт.
        header, body, footer = "", "[название]", ""
    state.update(
        {
            "phase": PHASE_CREATE_CONFIRM,
            "mode": mode,
            "create_header": header,
            "create_body": body,
            "create_footer": footer,
        }
    )
    state.pop("error_message", None)
    text, keyboard = _create_confirm_content(state)
    await _show(update, context, state, text, keyboard)


async def handle_create_standard(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _create_from(update, context, mode="standard")


async def handle_create_empty(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    await _create_from(update, context, mode="empty")


async def handle_create_save(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CREATE_CONFIRM:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    telegram_user_id = ctx.user(update).id
    # Защита от двойного нажатия: повторный update увидит другую фазу.
    state["phase"] = PHASE_CREATE_SAVING
    save_template_session(telegram_user_id, state)

    session = SessionLocal()
    new_state: dict | None = None
    text = ""
    keyboard: InlineKeyboardMarkup | None = None
    notice: str | None = None
    try:
        created = create_personal_template(
            session,
            actor_telegram_user_id=telegram_user_id,
            name=state.get("name"),
            header=state.get("create_header", ""),
            body=state.get("create_body", ""),
            footer=state.get("create_footer", ""),
            chosen_brick_ids=_chosen_map(state),
        )
        new_state = _card_state(
            created,
            origin="personal",
            page=_page_value(state.get("page")),
        )
        text, keyboard = _card_content(
            created, place_name=_place_name(session, created.place_id)
        )
        notice = "Личный шаблон создан."
    except (TemplateValidationError, TemplateCompileError) as exc:
        state["phase"] = PHASE_CREATE_CONFIRM
        state["error_message"] = _compile_error_text(exc)
        new_state = state
        text, keyboard = _create_confirm_content(state)
    except TemplateError as exc:
        state["phase"] = PHASE_CREATE_CONFIRM
        state["error_message"] = str(exc)
        new_state = state
        text, keyboard = _create_confirm_content(state)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось создать личный шаблон: %s", exc)
        state["phase"] = PHASE_CREATE_CONFIRM
        state["error_message"] = (
            "Не удалось создать шаблон из-за временной ошибки. "
            "Данные сохранены; повторите попытку."
        )
        new_state = state
        text, keyboard = _create_confirm_content(state)
    finally:
        session.close()

    if new_state is not None and keyboard is not None:
        await _show(update, context, new_state, text, keyboard, notice=notice)


# --- Обработчики: отмена и текст --------------------------------------------


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    telegram_user_id = ctx.user(update).id
    clear_template_session(telegram_user_id)
    await query.answer("Отменено.")
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        templates = list_personal_templates(session, telegram_user_id)
        page = _page_value(state.get("page") if isinstance(state, dict) else 0)
        text, keyboard = _hub_content(templates, page)
    finally:
        session.close()
    # Отмена действительно очищает состояние: строка template_sessions
    # не создаётся заново, пока пользователь не начнёт новый сценарий.
    await _render_now(update, context, text, keyboard)


async def _process_new_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    message = ctx.message(update)
    raw_name = message.text if message.text is not None else ""
    try:
        name = validate_template_name(raw_name)
    except TemplateValidationError as exc:
        failed = _valid_failed_inputs(state.get("failed_inputs"))
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, int) and message_id not in failed:
            failed.append(message_id)
        state["failed_inputs"] = failed
        text, keyboard = _create_name_content(state)
        await _show(
            update,
            context,
            state,
            text,
            keyboard,
            notice=str(exc),
        )
        return

    cleanup_ids = _valid_failed_inputs(state.get("failed_inputs"))
    state["name"] = name
    state["phase"] = PHASE_CREATE_CHOICE
    state["failed_inputs"] = []
    for key in (
        "create_header",
        "create_body",
        "create_footer",
        "mode",
        "error_message",
    ):
        state.pop(key, None)
    text, keyboard = _create_choice_content(name)
    await _show(
        update,
        context,
        state,
        text,
        keyboard,
        cleanup_ids=cleanup_ids,
        drop_input=True,
    )


async def _process_section_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: dict,
) -> None:
    message = ctx.message(update)
    new_text = message.text if message.text is not None else ""
    state["draft_text"] = new_text
    # Новый авторский текст требует нового явного выбора при неоднозначности.
    state["chosen_brick_ids"] = {}
    await _validate_and_show(
        update,
        context,
        state,
        drop_input=True,
    )


async def handle_template_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Принимает текст только в явных текстовых фазах редактора шаблонов."""
    telegram_user_id = ctx.user(update).id
    state = load_template_session(telegram_user_id)
    if not _valid_state(state):
        return
    phase = state.get("phase")
    if phase == PHASE_AWAIT_NAME:
        await _process_new_name(update, context, state)
    elif phase in {
        PHASE_SECTION_EDIT,
        PHASE_CONFIRM_SAVE,
        PHASE_SECTION_ERROR,
        PHASE_RESOLVE_AMBIGUITY,
    }:
        await _process_section_text(update, context, state)


# --- Стандартный шаблон из кабинета места -----------------------------------


async def open_standard_template(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Открывает стандартный шаблон места из административного кабинета."""
    query = ctx.query(update)
    place_id = _callback_id(query.data or "", "tp_std:")
    if place_id is None:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        _ensure_user(session, update)
        allowed = can_access_admin_panel(session, telegram_user_id) and (
            can_manage_place(session, telegram_user_id, place_id)
        )
        if not allowed:
            await query.answer(
                "Стандартный шаблон доступен только администратору места "
                "или суперадминистратору.",
                show_alert=True,
            )
            return
        template = get_standard_template(session, telegram_user_id, place_id)
        if template is None:
            await query.answer("У места нет стандартного шаблона.", show_alert=True)
            return
        await query.answer()
        state = _card_state(template, origin="admin")
        text, keyboard = _card_content(
            template, place_name=_place_name(session, place_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось открыть стандартный шаблон: %s", exc)
        await query.answer(
            "Не удалось открыть стандартный шаблон. Повторите попытку.",
            show_alert=True,
        )
        return
    finally:
        session.close()
    clear_place_session(telegram_user_id)
    clear_brick_session(telegram_user_id)
    await _show(update, context, state, text, keyboard)


async def handle_back_to_place(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Возврат из стандартного шаблона в карточку места админки."""
    query = ctx.query(update)
    place_id = _callback_id(query.data or "", "tp_back_place:")
    state = load_template_session(ctx.user(update).id)
    if place_id is None or not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if state.get("origin") != "admin" or state.get("place_id") != place_id:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    telegram_user_id = ctx.user(update).id
    clear_template_session(telegram_user_id)
    admin_state = load_admin_session(telegram_user_id)
    if not isinstance(admin_state, dict):
        admin_state = {"phase": "places", "page": 0}
    admin_state["phase"] = "place"
    admin_state["place_id"] = place_id
    from handlers.admin import _render_phase as render_admin_phase

    await render_admin_phase(update, context, admin_state)


# --- Регистрация -------------------------------------------------------------


class TemplateTextHandler(MessageHandler):
    """Текст принимается только в явных фазах редактора шаблонов."""

    def __init__(
        self,
        filters_arg,
        callback,
        *,
        allowed_phases: frozenset[str],
    ) -> None:
        super().__init__(filters_arg, callback)
        self.allowed_phases = frozenset(allowed_phases)

    def check_update(self, update):
        matched = super().check_update(update)
        if not matched:
            return None
        user = getattr(update, "effective_user", None)
        if user is None:
            return None
        state = load_template_session(user.id)
        if not _valid_state(state):
            return None
        if state.get("phase") not in self.allowed_phases:
            return None
        return matched


def templates_handlers() -> list[BaseHandler]:
    """Обработчики раздела «Мои шаблоны» Stage 25."""
    return [
        CommandHandler("templates", open_templates),
        CallbackQueryHandler(open_templates, pattern=r"^templates$"),
        # Poster 2.0 S2: вход «📝 Создать по личному шаблону» — список, где тап
        # по названию сразу начинает анонс. Точка старта (`tp_pick:<id>`)
        # зарегистрирована в announcement_wizard_handlers как entry_point.
        CallbackQueryHandler(open_template_picker, pattern=r"^pick_template$"),
        CallbackQueryHandler(handle_pick_page, pattern=r"^tp_pickpage:\d+$"),
        # Стандартный шаблон в кабинете места и возврат в карточку места.
        CallbackQueryHandler(open_standard_template, pattern=r"^tp_std:\d+$"),
        CallbackQueryHandler(handle_back_to_place, pattern=r"^tp_back_place:\d+$"),
        # Личный кабинет.
        CallbackQueryHandler(handle_page, pattern=r"^tp_page:\d+$"),
        CallbackQueryHandler(handle_hub, pattern=r"^tp_hub$"),
        CallbackQueryHandler(handle_open, pattern=r"^tp_open:\d+$"),
        CallbackQueryHandler(handle_edit_section, pattern=r"^tp_edit_[hbf]:\d+$"),
        CallbackQueryHandler(handle_preview, pattern=r"^tp_preview:\d+$"),
        CallbackQueryHandler(handle_reset, pattern=r"^tp_reset:\d+$"),
        CallbackQueryHandler(handle_reset_yes, pattern=r"^tp_reset_yes:\d+$"),
        CallbackQueryHandler(handle_create, pattern=r"^tp_create$"),
        CallbackQueryHandler(handle_create_standard, pattern=r"^tp_cstd$"),
        CallbackQueryHandler(handle_create_empty, pattern=r"^tp_cempty$"),
        CallbackQueryHandler(handle_create_back, pattern=r"^tp_cback$"),
        CallbackQueryHandler(handle_create_name_back, pattern=r"^tp_cname$"),
        CallbackQueryHandler(handle_create_save, pattern=r"^tp_csave$"),
        CallbackQueryHandler(handle_cancel, pattern=r"^tp_cancel$"),
        # Кирпичики, справка и общий возврат.
        CallbackQueryHandler(handle_bricks, pattern=r"^tp_bricks:\d+$"),
        CallbackQueryHandler(handle_bricks_page, pattern=r"^tp_bpage:\d+$"),
        CallbackQueryHandler(handle_help, pattern=r"^tp_help:[hbf]:\d+$"),
        CallbackQueryHandler(handle_back, pattern=r"^tp_back$"),
        # Редактирование и сохранение.
        CallbackQueryHandler(handle_clear, pattern=r"^tp_clear$"),
        CallbackQueryHandler(handle_retry, pattern=r"^tp_retry$"),
        CallbackQueryHandler(handle_refresh, pattern=r"^tp_refresh$"),
        CallbackQueryHandler(handle_retry_save, pattern=r"^tp_retry_save$"),
        CallbackQueryHandler(handle_save, pattern=r"^tp_save$"),
        CallbackQueryHandler(handle_pick, pattern=r"^tp_pick:\d+:\d+$"),
        # Повторная отрисовка после временной ошибки чтения.
        CallbackQueryHandler(_retry_render, pattern=r"^tp_retry_render$"),
        # Любая другая кнопка tp_ (старый экран) не меняет данные.
        CallbackQueryHandler(_stale_callback, pattern=r"^tp_"),
        TemplateTextHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_template_text,
            allowed_phases=TEXT_PHASES,
        ),
    ]


async def _retry_render(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    await _render_phase(update, context, state)


async def _stale_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await ctx.query(update).answer(STALE_TEXT, show_alert=True)
