# handlers/templates.py
"""
Telegram-интерфейс раздела «Мои шаблоны» (Stage 25).

Пользователь создаёт личные шаблоны, открывает карточку, редактирует единый
текст (и, если он есть, расширенный текст) обычным сообщением, смотрит
предпросмотр, доступные кирпичики и сбрасывает содержимое к встроенному
стандарту. Poster 2.3 H2: прежние «шапка/тело/подвал» больше не существуют;
полная переделка экранов — часть 2, здесь поток работает минимально. Стандартный шаблон места
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

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
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
from models import AnnouncementDraft, Template, TemplateSnapshot, User
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
    get_personal_template,
    get_standard_template,
    list_personal_templates,
    reset_personal_template,
    reset_standard_template,
    update_template_content,
)
from template_types import (
    TEMPLATE_CONTENT,
    TEMPLATE_EXTENDED_CONTENT,
    TEMPLATE_KIND_PERSONAL,
    TEMPLATE_KIND_STANDARD,
    TEMPLATE_TEXTS,
    TemplateValidationError,
    validate_template_content,
    validate_template_name,
)
from user_store import get_or_create_user
from utils import current_value
from utils.tg_html import escape_html
from utils.ui_texts import tr, tr_safe

logger = logging.getLogger(__name__)

# --- Константы ---------------------------------------------------------------

PAGE_SIZE = 8
BRICK_PAGE_SIZE = 8
BUTTON_TITLE_LIMIT = 42
# H3 (требование D): превью значения больше не режется «для красоты».
# Единственный предел — физический лимит сообщения Telegram; он живёт в
# общем компоненте «Текущее значение» (utils/current_value.py).
ERROR_TEXT_LIMIT = 1400

PHASE_HUB = "hub"
# Poster 2.0 S2: список личных шаблонов как вход в анонс. Тап по названию
# сразу начинает конструктор, «✏️» открывает карточку для правки.
# R2b, дефект №25: фаза живёт ТОЛЬКО у входа «📝 Создать по личному
# шаблону» (`pick_template`). «Управление → Шаблоны» и `/templates`
# открывают hub управления и никогда не запускают конструктор.
PHASE_PICK = "pick"
PHASE_CARD = "card"
PHASE_SECTION_EDIT = "section_edit"
PHASE_CONFIRM_SAVE = "confirm_save"
PHASE_SECTION_ERROR = "section_error"
PHASE_RESOLVE_AMBIGUITY = "resolve_ambiguity"
PHASE_PREVIEW = "preview"
PHASE_RESET_CONFIRM = "reset_confirm"
# Poster 2.1 R2: удаление личного шаблона — только после подтверждения и
# только если у шаблона нет неизменяемой истории (снимки/анонсы).
PHASE_DELETE_CONFIRM = "delete_confirm"
PHASE_BRICK_LIST = "brick_list"
PHASE_HELP = "help"
PHASE_AWAIT_NAME = "await_new_name"
PHASE_CREATE_CHOICE = "create_choice"
PHASE_CREATE_CONFIRM = "create_confirm"
PHASE_SAVING = "saving"
PHASE_CREATE_SAVING = "create_saving"
PHASE_DELETING = "deleting"

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
        PHASE_DELETE_CONFIRM,
        PHASE_BRICK_LIST,
        PHASE_HELP,
        PHASE_SAVING,
        PHASE_DELETING,
    }
)

_CREATE_PHASES = frozenset(
    {PHASE_AWAIT_NAME, PHASE_CREATE_CHOICE, PHASE_CREATE_CONFIRM, PHASE_CREATE_SAVING}
)

PHASES = frozenset({PHASE_HUB, PHASE_PICK}) | _TEMPLATE_PHASES | _CREATE_PHASES

_TEXT_INPUT_ERROR = tr("templates.module.tekst_prinyat_ispravte_ego")

STALE_TEXT = tr("errors.stale.templates")
DENIED_TEXT = tr("templates.module.shablon_nedostupen")

TEXT_GENITIVE = {
    TEMPLATE_CONTENT: tr("templates.module.teksta"),
    TEMPLATE_EXTENDED_CONTENT: tr("templates.module.rasshirennogo_teksta"),
}
# Отличает «второй текст не трогаем» от `None` = «расширения нет».
_UNSET_TEXT = object()
SCOPE_TITLES = {
    BRICK_SCOPE_SYSTEM: tr("wizard.module.sistemnyy"),
    BRICK_SCOPE_PLACE: tr("wizard.module.mesto"),
    BRICK_SCOPE_PERSONAL: tr("wizard.module.lichnyy"),
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


def _clip(value: object, limit: int = ERROR_TEXT_LIMIT) -> str:
    """
    Текст ошибки без разметки, ограниченный по длине.

    Это не «текущее значение» автора, а техническое сообщение, поэтому
    пометка компонента («полное значение сохранено») здесь была бы ложной:
    ошибка целиком лежит в логе.
    """
    text = _h(value)
    if len(text) <= limit:
        return text
    return text[:limit] + tr("templates.clip.soobschenie_sokrascheno")


def _value_block(
    label: str, value: object, *, limit: int = current_value.VALUE_LIMIT
) -> str:
    """
    Блок «Текущее значение»: подпись, разделители, сохранённый текст.

    Общий компонент (utils/current_value.py) сохраняет разметку SafeHtml,
    не добавляет `<code>` и не режет текст эстетически — только по
    физическому лимиту Telegram, с явной пометкой. Здесь значение только
    экранируется: обрезку и пометку делает сам компонент, иначе пометка
    задвоилась бы.

    `limit` приходит из общего бюджета экрана (utils/current_value.py):
    на экране с двумя значениями каждому достаётся половина остатка, иначе
    два «полных» блока вместе не влезают в 4096.
    """
    if not isinstance(value, str):
        return current_value.current_value_block(
            label, tr("templates.value_block.otsutstvuet"), limit=limit
        )
    if value == "":
        shown = tr("templates.value_block.pusto")
    elif not value.strip():
        shown = tr("templates.value_block.tolko_probely")
    else:
        shown = _h(value)
    return current_value.current_value_block(label, shown, limit=limit)


def _fit_value_screen(build, blocks: int) -> str:
    """
    Текст экрана с бюджетом для `blocks` значений.

    Сначала экран собирается с пустыми значениями: это обвязка (заголовок,
    подписи, подсказки, привязки). Остаток лимита Telegram делится между
    значениями (utils/current_value.value_limits), затем экран собирается
    окончательно. Так даже два огромных значения не выносят сообщение за
    4096, и у каждого сокращённого есть честная пометка.
    """
    limits = current_value.value_limits(build([0] * blocks), blocks)
    return current_value.clip_screen(build(limits))


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


def _compile_error_text(exc: BaseException) -> str:
    """Текст ошибки Stage 24 без переименований: секций в шаблоне больше нет."""
    return str(exc)


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
    elif phase == PHASE_DELETING:
        phase = PHASE_DELETE_CONFIRM
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
        if not isinstance(state.get("create_content", ""), str):
            return False
        extended = state.get("create_extended")
        if extended is not None and not isinstance(extended, str):
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
        if state.get("text_name") not in TEMPLATE_TEXTS:
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
        text = tr("templates.hub.moi_shablony_u_poka")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.templates.sozdat_shablon"),
                        callback_data="tp_create",
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.nav.back"), callback_data="main_menu"
                    )
                ],
            ]
        )
        return text, keyboard

    page = min(_page_value(page), (total - 1) // PAGE_SIZE)
    offset = page * PAGE_SIZE
    page_items = templates[offset : offset + PAGE_SIZE]
    lines = [tr("templates.hub.moi_shablony_vsego", total=total)]
    for index, template in enumerate(page_items, start=offset + 1):
        lines.append(f"{index}. {_h(template.display_name)} · v{template.revision}")
    if total > PAGE_SIZE:
        lines.append(
            tr(
                "templates.hub.pokazany",
                value=offset + 1,
                value_2=offset + len(page_items),
                total=total,
            )
        )
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
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.templates.sozdat_shablon"), callback_data="tp_create"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.back"), callback_data="main_menu")]
    )
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
        text = tr("templates.pick.sozdat_lichnomu_shablonu_u")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.templates.sozdat_shablon"),
                        callback_data="tp_create",
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.nav.main_menu"), callback_data="main_menu"
                    )
                ],
            ]
        )
        return text, keyboard

    page = min(_page_value(page), (total - 1) // PAGE_SIZE)
    offset = page * PAGE_SIZE
    page_items = templates[offset : offset + PAGE_SIZE]
    lines = [
        tr("buttons.main.create_template"),
        "",
        tr("templates.pick.vyberite_shablon_anons_nachnetsya"),
        "",
        tr("templates.pick.vsego_shablonov", total=total),
    ]
    for index, template in enumerate(page_items, start=offset + 1):
        lines.append(f"{index}. {_h(template.display_name)} · v{template.revision}")
    if total > PAGE_SIZE:
        lines.append(
            tr(
                "templates.hub.pokazany",
                value=offset + 1,
                value_2=offset + len(page_items),
                total=total,
            )
        )

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
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.templates.sozdat_shablon"), callback_data="tp_create"
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(tr("buttons.nav.main_menu"), callback_data="main_menu")]
    )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _card_back_button(template: TemplateDetails) -> InlineKeyboardButton:
    if template.kind == TEMPLATE_KIND_STANDARD and template.place_id is not None:
        return InlineKeyboardButton(
            tr("admin.proposal_admin_list.k_mestu"),
            callback_data=f"tp_back_place:{template.place_id}",
        )
    return InlineKeyboardButton(tr("buttons.places.k_spisku"), callback_data="tp_hub")


def _card_content(
    template: TemplateDetails, *, place_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    version = template.current_version
    kind_title = (
        tr("wizard.selected_template.standartnyy_shablon_mesta")
        if template.kind == TEMPLATE_KIND_STANDARD
        else tr("wizard.selected_template.lichnyy_shablon")
    )
    has_extended = version.extended_content is not None

    def build(limits: list[int]) -> str:
        lines = [
            f"📄 <b>{_h(template.display_name)}</b>",
            tr("templates.build.tip", kind_title=kind_title),
            tr("templates.build.versiya", revision=template.revision),
        ]
        if template.kind == TEMPLATE_KIND_STANDARD and place_name:
            lines.append(tr_safe("templates.build.mesto", place_name=_h(place_name)))
        lines.extend(
            [
                "",
                _value_block(
                    tr("templates.build.tekst"), version.content, limit=limits[0]
                ),
            ]
        )
        if has_extended:
            lines.extend(
                [
                    "",
                    _value_block(
                        tr("templates.build.rasshirennyy_tekst"),
                        version.extended_content,
                        limit=limits[1],
                    ),
                ]
            )
        return "\n".join(lines)

    text = _fit_value_screen(build, blocks=2 if has_extended else 1)
    edit_row = [
        InlineKeyboardButton(
            tr("buttons.templates.tekst"),
            callback_data=f"tp_edit:{template.template_id}",
        )
    ]
    if has_extended:
        edit_row.append(
            InlineKeyboardButton(
                tr("buttons.templates.rasshirennyy"),
                callback_data=f"tp_edit_ext:{template.template_id}",
            )
        )
    keyboard = InlineKeyboardMarkup(
        [
            edit_row,
            # H3 (требование E): просмотр текста и список кодов — два
            # связанных действия «посмотреть», они стоят рядом.
            [
                InlineKeyboardButton(
                    tr("buttons.templates.predprosmotr"),
                    callback_data=f"tp_preview:{template.template_id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.kirpichiki"),
                    callback_data=f"tp_bricks:{template.template_id}",
                ),
            ],
            # Опасное действие — отдельной строкой: его нельзя нажать
            # случайно рядом с просмотром.
            [
                InlineKeyboardButton(
                    tr("buttons.templates.vosstanovit_standart"),
                    callback_data=f"tp_reset:{template.template_id}",
                )
            ],
            [_card_back_button(template)],
        ]
    )
    rows = [list(row) for row in keyboard.inline_keyboard]
    if template.kind == TEMPLATE_KIND_PERSONAL:
        # Стандарт места не удаляется: место не может остаться без шаблона
        # (это гарантирует триггер схемы). У личного удаление — в конце списка.
        rows.insert(
            -1,
            [
                InlineKeyboardButton(
                    tr("buttons.templates.udalit_shablon"),
                    callback_data=f"tp_delete:{template.template_id}",
                )
            ],
        )
    return text, InlineKeyboardMarkup(rows)


def _preview_content(
    template: TemplateDetails, *, place_name: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    version = template.current_version
    has_extended = version.extended_content is not None

    def build(limits: list[int]) -> str:
        lines = [
            tr_safe(
                "templates.build.predprosmotr", display_name=_h(template.display_name)
            ),
        ]
        if template.kind == TEMPLATE_KIND_STANDARD and place_name:
            lines.append(tr_safe("templates.build.mesto", place_name=_h(place_name)))
        lines.extend(
            [
                tr("templates.build.versiya", revision=template.revision),
                "",
                _value_block(
                    tr("templates.build.tekst"), version.content, limit=limits[0]
                ),
            ]
        )
        if has_extended:
            lines.extend(
                [
                    "",
                    _value_block(
                        tr("templates.build.rasshirennyy_tekst"),
                        version.extended_content,
                        limit=limits[1],
                    ),
                ]
            )
        lines.append("")
        if version.bindings:
            lines.append(tr("templates.build.privyazki_kirpichikov"))
            for binding in version.bindings:
                label = (
                    f"• [{_h(binding.code)}] — {_h(binding.title)} "
                    f"({SCOPE_TITLES.get(binding.scope, binding.scope)})"
                )
                if not binding.is_active:
                    label += tr("templates.build.arhiv")
                lines.append(label)
        else:
            lines.append(tr("templates.build.privyazok_kirpichikov_net"))
        lines.extend(
            [
                "",
                tr("templates.build.eto_struktura_shablona_znacheniya"),
            ]
        )
        return "\n".join(lines)

    text = _fit_value_screen(build, blocks=2 if has_extended else 1)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.templates.k_shablonu"),
                    callback_data=f"tp_open:{template.template_id}",
                )
            ]
        ]
    )
    return text, keyboard


def _reset_confirm_content(
    template: TemplateDetails,
) -> tuple[str, InlineKeyboardMarkup]:
    kind_title = (
        tr("templates.reset_confirm.standartnogo_shablona_mesta")
        if template.kind == TEMPLATE_KIND_STANDARD
        else tr("templates.reset_confirm.lichnogo_shablona")
    )
    text = tr_safe(
        "templates.reset_confirm.vosstanovit_standart_shablon",
        display_name=_h(template.display_name),
        revision=template.revision,
        kind_title=kind_title,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.templates.podtverdit_vosstanovlenie"),
                    callback_data=f"tp_reset_yes:{template.template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"),
                    callback_data=f"tp_open:{template.template_id}",
                )
            ],
        ]
    )
    return text, keyboard


# --- Экраны: редактирование секции ------------------------------------------


def _delete_confirm_content(
    template: TemplateDetails,
) -> tuple[str, InlineKeyboardMarkup]:
    text = tr_safe(
        "templates.delete_confirm.udalit_shablon_shablon_tekuschaya",
        display_name=_h(template.display_name),
        revision=template.revision,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.templates.udalit"),
                    callback_data=f"tp_delete_yes:{template.template_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"),
                    callback_data=f"tp_open:{template.template_id}",
                )
            ],
        ]
    )
    return text, keyboard


DELETE_BLOCKED_TEXT = tr("templates.module.shablon_nelzya_udalit_nemu")


def _template_usage(session: Session, template_id: int) -> tuple[int, int]:
    """
    Сколько неизменяемых следов ссылается на шаблон.

    Снимок в анонсе (`template_snapshots`) и сам анонс
    (`announcement_drafts`) держат шаблон FK `ON DELETE RESTRICT`. Пока хотя
    бы один след есть, физическое удаление запрещено: иначе пострадает
    история анонсов.
    """
    snapshots = int(
        session.scalar(
            select(func.count(TemplateSnapshot.id)).where(
                TemplateSnapshot.source_template_id == template_id
            )
        )
        or 0
    )
    drafts = int(
        session.scalar(
            select(func.count(AnnouncementDraft.id)).where(
                AnnouncementDraft.template_id == template_id
            )
        )
        or 0
    )
    return snapshots, drafts


def _delete_personal_template(
    session: Session,
    *,
    telegram_user_id: int,
    template_id: int,
    expected_revision: int,
) -> str:
    """
    Удаляет личный шаблон без истории версий/снимков/анонсов.

    Возвращает исход: `deleted`, `stale`, `blocked`, `not_found`.
    Вся проверка и удаление — в одной транзакции: чужая правка, снимок или
    анонс не дают удалить шаблон по устаревшему экрану.
    """
    template = get_personal_template(session, telegram_user_id, template_id)
    if template is None:
        return "not_found"
    if template.revision != expected_revision:
        return "stale"
    snapshots, drafts = _template_usage(session, template_id)
    if snapshots or drafts:
        return "blocked"
    try:
        result = session.execute(
            delete(Template).where(
                Template.id == template_id,
                Template.kind == TEMPLATE_KIND_PERSONAL,
                Template.revision == expected_revision,
            )
        )
    except IntegrityError:
        session.rollback()
        return "blocked"
    if getattr(result, "rowcount", 0) != 1:
        session.rollback()
        return "stale"
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return "blocked"
    return "deleted"


def _text_edit_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    text_name = state["text_name"]
    title = TEXT_GENITIVE[text_name]
    draft = state.get("draft_text", "")

    def build(limits: list[int]) -> str:
        # Бюджет значения считается от обвязки ЭТОГО экрана: заголовок и
        # подсказка про формат входят в 4096 вместе с блоком значения.
        return tr_safe(
            "templates.build.redaktirovanie_otpravte_novym",
            title=title,
            value_block=_value_block(
                tr("templates.build.tekst_chernovik"), draft, limit=limits[0]
            ),
        )

    text = _fit_value_screen(build, blocks=1)
    template_id = state["template_id"]
    keyboard = InlineKeyboardMarkup(
        [
            # H3 (требование E): два справочных действия рядом.
            [
                InlineKeyboardButton(
                    tr("buttons.templates.kirpichiki"),
                    callback_data=f"tp_bricks:{template_id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.podskazka"),
                    callback_data=f"tp_help:{template_id}",
                ),
            ],
            # Очистка — отдельной строкой: необратимо для черновика.
            [
                InlineKeyboardButton(
                    tr("buttons.templates.ochistit"), callback_data="tp_clear"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.templates.k_shablonu_otmenit_pravku"),
                    callback_data=f"tp_open:{template_id}",
                )
            ],
        ]
    )
    return text, keyboard


def _section_back_button(state: dict) -> InlineKeyboardButton:
    if state.get("origin") == "admin" and _positive_int(state.get("place_id")):
        return InlineKeyboardButton(
            tr("admin.proposal_admin_list.k_mestu"),
            callback_data=f"tp_back_place:{state['place_id']}",
        )
    return InlineKeyboardButton(tr("buttons.places.k_spisku"), callback_data="tp_hub")


def _error_header(kind: str) -> str:
    return {
        "stale": tr("templates.error_header.shablon_izmenilsya_drugom_okne"),
        "conflict": tr("templates.error_header.ne_udalos_sohranit_odnovremennoy"),
        "forbidden": tr("templates.error_header.net_prav_izmenenie_shablona"),
        "validation": tr("templates.error_header.tekst_sohranen"),
        "syntax": tr("templates.error_header.sintaksicheskaya_oshibka"),
        "unknown": tr("templates.error_header.neizvestnyy_kirpichik"),
        "invalid": tr("templates.error_header.tekst_proshel_proverku"),
        "unexpected": tr("templates.error_header.ne_udalos_sohranit_izmeneniya"),
    }.get(kind, tr("templates.error_header.tekst_sohranen"))


def _section_error_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    kind = str(state.get("error_kind") or "unexpected")
    message = str(
        state.get("error_message") or tr("templates.section_error.povtorite_popytku")
    )
    draft = state.get("draft_text", "")

    def build(limits: list[int]) -> str:
        lines = [
            _error_header(kind),
            "",
            _clip(message, ERROR_TEXT_LIMIT),
            "",
        ]
        lines.append(
            _value_block(
                tr("templates.build.vash_tekst_sohranen"), draft, limit=limits[0]
            )
            if draft
            else tr("templates.build.sekciya_seychas_pusta")
        )
        lines.append("")
        if kind == "stale":
            lines.append(tr("templates.build.novaya_versiya_sozdana_chuzhie"))
        elif kind == "conflict":
            lines.append(tr("templates.build.dannye_poteryany_povtorite"))
        elif kind == "forbidden":
            lines.append(tr("templates.build.izmenenie_nevozmozhno_prava"))
        else:
            lines.append(_TEXT_INPUT_ERROR)
        return "\n".join(lines)

    # Текст ошибки занимает экран вместе со значением: бюджет считается от
    # фактической обвязки, иначе черновик на 5000 символов вынесет экран.
    text = _fit_value_screen(build, blocks=1)

    template_id = state["template_id"]
    rows: list[list[InlineKeyboardButton]] = []
    if kind == "stale":
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.obnovit_versiyu_povtorit"),
                    callback_data="tp_refresh",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.ispravit_tekst"), callback_data="tp_retry"
                )
            ]
        )
    elif kind == "conflict":
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.povtorit_sohranenie"),
                    callback_data="tp_retry_save",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.ispravit_tekst"), callback_data="tp_retry"
                )
            ]
        )
    elif kind == "forbidden":
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.ispravit_tekst"), callback_data="tp_retry"
                )
            ]
        )
        rows.append([_section_back_button(state)])
    else:
        # H3 (требование E): «поправить» и «посмотреть коды» — действия над
        # тем же текстом, поэтому стоят в одной строке.
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.ispravit"), callback_data="tp_retry"
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.kirpichiki"),
                    callback_data=f"tp_bricks:{template_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    tr("buttons.templates.k_shablonu"),
                    callback_data=f"tp_open:{template_id}",
                )
            ]
        )
    return text, InlineKeyboardMarkup(rows)


def _access_error_content(state: dict) -> tuple[str, InlineKeyboardMarkup]:
    draft = state.get("draft_text", "")

    def build(limits: list[int]) -> str:
        lines = [
            tr("templates.build.shablon_bolshe_nedostupen"),
            "",
            tr("templates.build.prava_izmenilis_shablon_udalen"),
            "",
        ]
        lines.append(
            _value_block(tr("templates.build.vash_tekst"), draft, limit=limits[0])
            if draft
            else tr("templates.build.sekciya_seychas_pusta")
        )
        return "\n".join(lines)

    text = _fit_value_screen(build, blocks=1)
    return text, InlineKeyboardMarkup([[_section_back_button(state)]])


def _chosen_rows(state: dict) -> list[str]:
    chosen = _chosen_map(state)
    if not chosen:
        return []
    return [f"[{_h(code)}]" for code in sorted(chosen)]


def _confirm_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    text_name = state["text_name"]
    title = TEXT_GENITIVE[text_name]
    draft = state.get("draft_text", "")

    def build(limits: list[int]) -> str:
        lines = [
            tr_safe("templates.build.predprosmotr_podtverzhdenie", title=title),
            "",
            _value_block(tr("templates.build.tekst_chernovik"), draft, limit=limits[0]),
            "",
            tr("templates.build.vtoroy_tekst_shablona_menyaetsya"),
        ]
        chosen = _chosen_rows(state)
        if chosen:
            lines.extend(
                [
                    "",
                    tr("templates.build.yavno_vybrany_kirpichiki")
                    + ", ".join(chosen)
                    + ".",
                ]
            )
        return "\n".join(lines)

    text = _fit_value_screen(build, blocks=1)
    template_id = state["template_id"]
    keyboard = InlineKeyboardMarkup(
        [
            # H3 (требование E): пара «принять / поправить» — оба действия
            # относятся к показанному выше черновику.
            [
                InlineKeyboardButton(
                    tr("buttons.bricks.sohranit"), callback_data="tp_save"
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.ispravit"), callback_data="tp_retry"
                ),
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.templates.kirpichiki"),
                    callback_data=f"tp_bricks:{template_id}",
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.k_shablonu"),
                    callback_data=f"tp_open:{template_id}",
                ),
            ],
        ]
    )
    return text, keyboard


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
        result.append({"code": code, "candidates": clean_candidates})
    return result


def _ambiguity_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    groups = _ambiguties(state)
    lines = [
        tr("templates.ambiguity.neodnoznachnyy_kirpichik"),
        "",
        tr("templates.ambiguity.odin_tot_zhe_kod"),
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for group_index, group in enumerate(groups):
        code = str(group["code"])
        lines.extend(["", tr_safe("templates.ambiguity.kod", code=_h(code))])
        for candidate_index, candidate in enumerate(group["candidates"]):
            scope = SCOPE_TITLES.get(
                str(candidate.get("scope")), str(candidate.get("scope"))
            )
            title = _h(
                str(candidate.get("title") or tr("templates.ambiguity.kirpichik"))
            )
            lines.append(f"{candidate_index + 1}. {title} · {scope}")
            rows.append(
                [
                    InlineKeyboardButton(
                        _short(
                            f"{candidate_index + 1}. "
                            f"{candidate.get('title') or tr('templates.ambiguity.kirpichik')}"
                            f" · {scope}"
                        ),
                        callback_data=(f"tp_pick:{group_index}:{candidate_index}"),
                    )
                ]
            )
    template_id = state["template_id"]
    # H3 (требование E): выбор варианта уже сделан выше; ниже — пара
    # «поправить / посмотреть коды» и отдельно выход к шаблону.
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.templates.ispravit"), callback_data="tp_retry"
            ),
            InlineKeyboardButton(
                tr("buttons.templates.kirpichiki"),
                callback_data=f"tp_bricks:{template_id}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                tr("buttons.templates.k_shablonu"),
                callback_data=f"tp_open:{template_id}",
            )
        ]
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
        scope_hint = tr("templates.brick_list.dlya_standartnogo_shablona")
    else:
        viewer = telegram_user_id
        place_id = None
        scope_hint = tr("templates.brick_list.dlya_lichnogo_shablona_dostupny")
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
        tr("templates.brick_list.dostupnye_kirpichiki", scope_hint=scope_hint),
        tr("templates.brick_list.vsego", total=total),
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
                tr(
                    "templates.brick_list.pokazany",
                    value=offset + 1,
                    value_2=offset + len(page_items),
                    total=total,
                )
            )
    else:
        lines.append(tr("templates.brick_list.net_dostupnyh_kirpichikov"))
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
    rows.append([InlineKeyboardButton(tr("buttons.nav.back"), callback_data="tp_back")])
    return "\n".join(lines), InlineKeyboardMarkup(rows), offset


def _help_content() -> tuple[str, InlineKeyboardMarkup]:
    text = tr("templates.help.oformlenie_teksta_shablona_kod")
    return text, InlineKeyboardMarkup(
        [[InlineKeyboardButton(tr("buttons.nav.back"), callback_data="tp_back")]]
    )


# --- Экраны: создание --------------------------------------------------------


def _create_name_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    current = state.get("name")
    lines = [tr("templates.create_name.sozdanie_shablona")]
    if isinstance(current, str) and current.strip():
        lines.extend(
            [
                "",
                tr_safe(
                    "templates.create_name.tekuschee_nazvanie", current=_h(current)
                ),
            ]
        )
    lines.extend(["", tr("templates.create_name.vvedite_nazvanie_novogo_shablona")])
    # Название вводит человек, и оно может быть любой длины: экран всё равно
    # обязан уложиться в лимит Telegram (с явной пометкой о сокращении).
    return current_value.clip_screen("\n".join(lines)), InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"), callback_data="tp_cancel"
                )
            ]
        ]
    )


def _create_choice_content(
    name: str,
) -> tuple[str, InlineKeyboardMarkup]:
    text = current_value.clip_screen(
        tr_safe("templates.create_choice.shablon_vyberite_nachalnoe", name=_h(name))
    )
    return text, InlineKeyboardMarkup(
        [
            # H3 (требование E): два варианта основы — один выбор, одна строка.
            [
                InlineKeyboardButton(
                    tr("buttons.templates.standartnyy"), callback_data="tp_cstd"
                ),
                InlineKeyboardButton(
                    tr("buttons.templates.pustoy"), callback_data="tp_cempty"
                ),
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.bricks.nazvanie"), callback_data="tp_cname"
                ),
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"), callback_data="tp_cancel"
                ),
            ],
        ]
    )


def _create_confirm_content(
    state: dict,
) -> tuple[str, InlineKeyboardMarkup]:
    name = str(state.get("name") or "")
    mode = str(state.get("mode") or "")
    content = str(state.get("create_content") or "")
    extended = state.get("create_extended")
    mode_title = (
        tr("templates.create_confirm.na_osnove_standartnogo")
        if mode == "standard"
        else tr("templates.create_confirm.pustoy_shablon_minimalnaya")
    )
    has_extended = isinstance(extended, str)
    error = state.get("error_message")

    def build(limits: list[int]) -> str:
        lines = [
            tr("templates.build.proverte_novyy_shablon"),
            "",
            tr_safe("templates.build.nazvanie", name=_h(name)),
            tr("templates.build.osnova", mode_title=mode_title),
            "",
            _value_block(tr("templates.build.tekst"), content, limit=limits[0]),
        ]
        if isinstance(extended, str):
            lines.extend(
                [
                    "",
                    _value_block(
                        tr("templates.build.rasshirennyy_tekst"),
                        extended,
                        limit=limits[1],
                    ),
                ]
            )
        if mode == "empty":
            lines.extend(
                [
                    "",
                    tr("templates.build.sovsem_pustoy_shablon_baza"),
                ]
            )
        if isinstance(error, str) and error:
            lines.extend(["", f"⚠️ {_clip(error, ERROR_TEXT_LIMIT)}"])
        lines.extend(["", tr("templates.build.podtverdite_sozdanie")])
        return "\n".join(lines)

    text = _fit_value_screen(build, blocks=2 if has_extended else 1)
    return text, InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    tr("buttons.templates.sozdat"), callback_data="tp_csave"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.templates.k_vyboru_osnovy"), callback_data="tp_cback"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.templates.izmenit_nazvanie"), callback_data="tp_cname"
                )
            ],
            [
                InlineKeyboardButton(
                    tr("buttons.admin.otmena_2"), callback_data="tp_cancel"
                )
            ],
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
        notice = notice or tr("templates.render_phase.chernovik_shablona_povrezhden")

    phase = state.get("phase")
    if phase == PHASE_SAVING:
        state["phase"] = phase = PHASE_CONFIRM_SAVE
        notice = notice or (tr("templates.render_phase.sohranenie_bylo_podtverzhdeno"))
    elif phase == PHASE_CREATE_SAVING:
        state["phase"] = phase = PHASE_CREATE_CONFIRM
        notice = notice or (tr("templates.render_phase.sozdanie_bylo_podtverzhdeno"))
    elif phase == PHASE_DELETING:
        state["phase"] = phase = PHASE_DELETE_CONFIRM
        notice = notice or (tr("templates.render_phase.udalenie_bylo_podtverzhdeno"))

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
                    notice = notice or tr(
                        "templates.render_phase.shablon_nayden_bolshe_nedostupen"
                    )
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
                elif phase == PHASE_DELETE_CONFIRM:
                    state["revision"] = template.revision
                    text, keyboard = _delete_confirm_content(template)
                elif phase == PHASE_SECTION_EDIT:
                    if check_revision and template.revision != state.get("revision"):
                        notice = notice or (
                            tr("templates.render_phase.shablon_izmenilsya_drugom_okne")
                        )
                    text, keyboard = _text_edit_content(state)
                elif phase == PHASE_CONFIRM_SAVE:
                    if check_revision and template.revision != state.get("revision"):
                        state["phase"] = PHASE_SECTION_ERROR
                        state["error_kind"] = "stale"
                        state["error_message"] = tr(
                            "templates.render_phase.shablon_izmenilsya_drugom_okne_2"
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
                        state["error_message"] = tr(
                            "templates.render_phase.shablon_izmenilsya_drugom_okne_3"
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
                    notice = notice or tr("templates.render_phase.razdel_otkryt_zanovo")
        else:  # pragma: no cover — _valid_state уже отсекает
            state.clear()
            state.update(_new_hub())
            text, keyboard = _hub_content([], 0)
            notice = notice or tr("templates.render_phase.razdel_otkryt_zanovo")
    except Exception as exc:  # noqa: BLE001 — экран не должен падать
        logger.exception("Не удалось отрисовать раздел шаблонов: %s", exc)
        text = tr("templates.render_phase.ne_udalos_zagruzit_razdel")
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        tr("buttons.templates.povtorit"),
                        callback_data="tp_retry_render",
                    )
                ],
                [
                    InlineKeyboardButton(
                        tr("buttons.admin.glavnoe_menyu"), callback_data="main_menu"
                    )
                ],
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
                notice=tr("templates.render_phase.shablon_nayden_bolshe_nedostupen"),
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
                notice=tr("templates.validate_and_show.shablon_dostupen_tolko"),
            )
        state["origin"] = _template_origin(template)
        state["place_id"] = template.place_id

        if template.revision != state.get("revision"):
            _set_section_error(
                state,
                "stale",
                tr("templates.validate_and_show.shablon_izmenilsya_drugom_okne"),
            )
        else:
            current = template.current_version
            texts = {
                TEMPLATE_CONTENT: current.content,
                TEMPLATE_EXTENDED_CONTENT: current.extended_content,
            }
            texts[state["text_name"]] = state.get("draft_text", "")
            try:
                checked_content, checked_extended = validate_template_content(
                    texts[TEMPLATE_CONTENT],
                    texts[TEMPLATE_EXTENDED_CONTENT],
                )
            except TemplateValidationError as exc:
                _set_section_error(state, "validation", str(exc))
            else:
                compile_texts = {TEMPLATE_CONTENT: checked_content}
                if checked_extended is not None:
                    compile_texts[TEMPLATE_EXTENDED_CONTENT] = checked_extended
                compiled = resolve_template_bindings(
                    session,
                    compile_texts,
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
                        tr(
                            "templates.validate_and_show.neizvestnye_kirpichiki_proverte",
                            codes=codes,
                        ),
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
            tr("templates.validate_and_show.ne_udalos_proverit_tekst"),
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
    """
    Вход в раздел управления шаблонами: кнопка меню, /templates, рестарт.

    R2b, дефект №25: «Управление → Шаблоны» и `/templates` — только
    управление. Сохранённая фаза `pick` (вход в создание анонса) здесь НЕ
    возобновляется: раздел открывается списком управления, а создать анонс
    можно только из главного меню через «📝 Создать по личному шаблону».
    Незавершённая правка/сохранение шаблона возобновляется как раньше.
    """
    query = getattr(update, "callback_query", None)
    if query is not None:
        await query.answer()
    await screen.reanchor_screen(update, context)
    telegram_user_id = ctx.user(update).id
    clear_place_session(telegram_user_id)
    clear_admin_session(telegram_user_id)
    clear_brick_session(telegram_user_id)

    state = load_template_session(telegram_user_id)
    if not _valid_state(state) or state.get("phase") == PHASE_PICK:
        state = _new_hub(
            _page_value(state.get("page") if isinstance(state, dict) else 0)
        )
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
        PHASE_DELETE_CONFIRM,
        PHASE_SECTION_ERROR,
        PHASE_BRICK_LIST,
        PHASE_HELP,
        # U3b: экран ввода названия нового шаблона — живой экран создания;
        # «К списку» ведёт в список шаблонов, а не отвечает «устарело».
        PHASE_AWAIT_NAME,
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
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


async def handle_edit_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Открывает редактор текста шаблона.

    Poster 2.3 H2: `tp_edit:<id>` правит канонический `content`,
    `tp_edit_ext:<id>` — `extended_content`. Разделения на секции нет.
    """
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CARD:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    data = query.data or ""
    # tp_edit:<id> | tp_edit_ext:<id>
    prefix, _, raw_id = data.partition(":")
    text_name = (
        TEMPLATE_EXTENDED_CONTENT if prefix == "tp_edit_ext" else TEMPLATE_CONTENT
    )
    try:
        template_id = int(raw_id)
    except (TypeError, ValueError):
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
            return
        if template.kind == TEMPLATE_KIND_STANDARD and state.get("origin") != "admin":
            await query.answer(DENIED_TEXT, show_alert=True)
            return
        await query.answer()
        current = template.current_version
        if text_name == TEMPLATE_EXTENDED_CONTENT and (
            current.extended_content is None
        ):
            await query.answer(
                tr("alerts.templates.u_shablona_net_rasshirennogo"), show_alert=True
            )
            return
        state.update(
            {
                "phase": PHASE_SECTION_EDIT,
                "template_id": template.template_id,
                "revision": template.revision,
                "origin": _template_origin(template),
                "place_id": template.place_id,
                "text_name": text_name,
                "draft_text": getattr(current, text_name) or "",
                "chosen_brick_ids": {},
            }
        )
        state.pop("error_kind", None)
        state.pop("error_message", None)
        state.pop("pending_ambiguities", None)
        text, keyboard = _text_edit_content(state)
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
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
            alert = tr("templates.render_phase.shablon_nayden_bolshe_nedostupen")
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
            notice = tr("templates.handle_reset_yes.sbros_vypolnen_shablon_izmenilsya")
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
                    tr(
                        "templates.handle_reset_yes.shablon_sbroshen_vstroennomu",
                        revision=current.revision,
                    )
                    if result.status == "updated"
                    else tr("templates.handle_reset_yes.shablon_sovpadaet_vstroennym")
                )
            elif result.status == "stale":
                current = result.template or get_accessible_template(
                    session, telegram_user_id, template_id
                )
                if current is None:
                    alert = tr("alerts.templates.shablon_nayden")
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
                    notice = tr(
                        "templates.handle_reset_yes.sbros_vypolnen_shablon_izmenilsya"
                    )
            elif result.status == "forbidden":
                alert = result.message or tr(
                    "templates.handle_reset_yes.nedostatochno_prav"
                )
                new_state = _new_hub(_page_value(state.get("page")))
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, new_state["page"])
                notice = result.message or tr(
                    "templates.handle_reset_yes.prava_shablon_bolshe_deystvuyut"
                )
            elif result.status == "not_found":
                new_state = _new_hub(_page_value(state.get("page")))
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, new_state["page"])
                notice = tr("templates.handle_reset_yes.shablon_bolshe_suschestvuet")
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
                notice = result.message or tr(
                    "templates.handle_reset_yes.sbros_vypolnen"
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось сбросить шаблон: %s", exc)
        alert = tr("templates.handle_reset_yes.ne_udalos_sbrosit_shablon")
    finally:
        session.close()

    if alert is not None:
        await query.answer(alert, show_alert=True)
        return
    await query.answer()
    if new_state is not None and keyboard is not None:
        await _show(update, context, new_state, text, keyboard, notice=notice)


# --- Обработчики: удаление личного шаблона ----------------------------------


async def handle_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """«🗑 Удалить шаблон» в карточке: только подтверждение, без удаления."""
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CARD:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_delete:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    telegram_user_id = ctx.user(update).id
    session = SessionLocal()
    try:
        template = _load_editable(session, telegram_user_id, template_id)
        if template is None:
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
            return
        if template.kind != TEMPLATE_KIND_PERSONAL:
            await query.answer(
                tr("alerts.templates.standartnyy_shablon_udalit_nelzya"),
                show_alert=True,
            )
            return
        await query.answer()
        state.update(
            {
                "phase": PHASE_DELETE_CONFIRM,
                "revision": template.revision,
                "origin": _template_origin(template),
                "place_id": template.place_id,
            }
        )
        text, keyboard = _delete_confirm_content(template)
    finally:
        session.close()
    await _show(update, context, state, text, keyboard)


async def handle_delete_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Подтверждённое удаление личного шаблона.

    Снимки и анонсы не удаляются и не переписываются: если они есть,
    пользователь получает явный исход «удалить нельзя» и остаётся в карточке.
    """
    query = ctx.query(update)
    telegram_user_id = ctx.user(update).id
    state = load_template_session(telegram_user_id)
    if not _valid_state(state) or state.get("phase") != PHASE_DELETE_CONFIRM:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    template_id = _callback_id(query.data or "", "tp_delete_yes:")
    if template_id is None or template_id != state.get("template_id"):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    # Защита от двойного нажатия: повторный callback увидит другую фазу.
    state["phase"] = PHASE_DELETING
    save_template_session(telegram_user_id, state)

    session = SessionLocal()
    notice: str | None = None
    alert: str | None = None
    new_state: dict | None = None
    text = ""
    keyboard: InlineKeyboardMarkup | None = None
    try:
        outcome = _delete_personal_template(
            session,
            telegram_user_id=telegram_user_id,
            template_id=template_id,
            expected_revision=int(state.get("revision") or 0),
        )
        page = _page_value(state.get("page"))
        if outcome == "deleted":
            new_state = _new_hub(page)
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, page)
            notice = tr("templates.handle_delete_yes.shablon_udalen")
        elif outcome == "not_found":
            new_state = _new_hub(page)
            templates = list_personal_templates(session, telegram_user_id)
            text, keyboard = _hub_content(templates, page)
            notice = tr("templates.handle_delete_yes.shablon_udalen_2")
        elif outcome == "stale":
            template = get_personal_template(session, telegram_user_id, template_id)
            if template is None:
                new_state = _new_hub(page)
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, page)
                notice = tr("templates.handle_delete_yes.shablon_udalen_2")
            else:
                new_state = _card_state(
                    template,
                    origin=_template_origin(template),
                    page=page,
                )
                text, keyboard = _card_content(
                    template, place_name=_place_name(session, template.place_id)
                )
                notice = tr("templates.handle_delete_yes.udalenie_vypolneno_shablon")
        else:  # blocked
            template = get_personal_template(session, telegram_user_id, template_id)
            if template is None:
                new_state = _new_hub(page)
                templates = list_personal_templates(session, telegram_user_id)
                text, keyboard = _hub_content(templates, page)
                notice = tr("templates.handle_delete_yes.shablon_udalen_2")
            else:
                new_state = _card_state(
                    template,
                    origin=_template_origin(template),
                    page=page,
                )
                text, keyboard = _card_content(
                    template, place_name=_place_name(session, template.place_id)
                )
                notice = DELETE_BLOCKED_TEXT
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось удалить шаблон: %s", exc)
        alert = tr("templates.handle_delete_yes.ne_udalos_udalit_shablon")
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
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
    template_id = _callback_id(query.data or "", "tp_help:")
    if template_id is None:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if template_id != state.get("template_id"):
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
    if phase == PHASE_AWAIT_NAME:
        # U3b: экран ввода названия нового шаблона — живой экран создания.
        # «Назад» не должен отвечать «Кнопка устарела»: возвращаемся в
        # список шаблонов (как «Отмена»), создание не подтверждено.
        await query.answer()
        page = _page_value(state.get("page"))
        state.clear()
        state.update(_new_hub(page))
        await _render_phase(
            update,
            context,
            state,
            notice=tr("templates.handle_back.sozdanie_shablona_podtverzhdeno"),
        )
        return
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
        notice=tr("templates.handle_clear.tekst_ochischen_proverte_rezultat"),
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
    text, keyboard = _text_edit_content(state)
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
            await query.answer(tr("alerts.templates.shablon_nayden"), show_alert=True)
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
            tr(
                "templates.handle_refresh.versiya_obnovlena_tekst_sohranen",
                state_revision=state["revision"],
            )
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
    title = str(candidate.get("title") or tr("templates.handle_pick.kirpichik"))
    await _validate_and_show(
        update,
        context,
        state,
        notice=tr(
            "templates.handle_pick.vybran_kirpichik_proveryaem_tekst", title=title
        ),
    )


# --- Обработчики: сохранение секции -----------------------------------------


async def handle_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = ctx.query(update)
    state = load_template_session(ctx.user(update).id)
    if not _valid_state(state) or state.get("phase") != PHASE_CONFIRM_SAVE:
        await query.answer(STALE_TEXT, show_alert=True)
        return
    if state.get("text_name") not in TEMPLATE_TEXTS or not isinstance(
        state.get("draft_text"), str
    ):
        await query.answer(STALE_TEXT, show_alert=True)
        return
    await query.answer()
    telegram_user_id = ctx.user(update).id
    template_id = int(state["template_id"])
    text_name = state["text_name"]
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
        # Меняется только выбранный текст; второй остаётся прежним.
        template = _load_editable(session, telegram_user_id, template_id)
        current = template.current_version if template is not None else None
        if current is None:
            # Права могли отозвать или шаблон удалить между проверкой и
            # сохранением. Не подменяем это «временной ошибкой»: сервис сам
            # вернёт `forbidden`/`not_found`, а черновик останется в сессии.
            content = draft_text
            extended_content = _UNSET_TEXT
        else:
            content = draft_text if text_name == TEMPLATE_CONTENT else current.content
            extended_content = (
                draft_text
                if text_name == TEMPLATE_EXTENDED_CONTENT
                else current.extended_content
            )
        update_kwargs: dict[str, Any] = {}
        if extended_content is not _UNSET_TEXT:
            update_kwargs["extended_content"] = extended_content
        result = update_template_content(
            session,
            actor_telegram_user_id=telegram_user_id,
            template_id=template_id,
            expected_revision=int(state["revision"]),
            content=content,
            chosen_brick_ids=_chosen_map(state),
            **update_kwargs,
        )
        if result.ok and result.template is not None:
            # `result.template` — шаблон (`TemplateDetails`), а `current` —
            # его версия (`TemplateVersionDetails`). Карточку строим по
            # шаблону: ей нужны place_id и номер текущей ревизии.
            saved = result.template
            new_state = _card_state(
                saved,
                origin=_template_origin(saved),
                page=_page_value(state.get("page")),
            )
            text, keyboard = _card_content(
                saved, place_name=_place_name(session, saved.place_id)
            )
            notice = (
                tr("templates.handle_save.sohraneno_versii", revision=saved.revision)
                if result.status == "updated"
                else tr("templates.handle_save.soderzhimoe_izmenilos_novaya")
            )
        elif result.status == "stale":
            stale_current = result.template or get_accessible_template(
                session, telegram_user_id, template_id
            )
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "stale"
            state["error_message"] = tr(
                "templates.handle_save.shablon_izmenilsya_drugom_okne"
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
                tr("templates.handle_save.prava_izmenenie_shablona_bolshe")
            )
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "invalid":
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "invalid"
            state["error_message"] = result.message or tr(
                "templates.handle_save.tekst_proshel_proverku"
            )
            state.pop("pending_ambiguities", None)
            new_state = state
            text, keyboard = _section_error_content(state)
        elif result.status == "conflict":
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "conflict"
            state["error_message"] = result.message or (
                tr("templates.handle_save.ne_udalos_sohranit_odnovremennoy")
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
            notice = tr("templates.handle_save.shablon_bolshe_suschestvuet")
        else:  # pragma: no cover — будущие статусы сервиса
            state["phase"] = PHASE_SECTION_ERROR
            state["error_kind"] = "unexpected"
            state["error_message"] = result.message or tr(
                "templates.handle_save.ne_udalos_sohranit"
            )
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
        state["error_message"] = tr(
            "templates.handle_save.ne_udalos_sohranit_vremennoy"
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
    for key in ("create_content", "create_extended", "mode"):
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
        content = builtin.content
        extended = builtin.extended_content
    else:
        # Backend запрещает пустой текст. Минимальная безопасная заготовка
        # сохраняет пользователю «пустой» старт.
        content, extended = tr("templates.create_from.nazvanie"), None
    state.update(
        {
            "phase": PHASE_CREATE_CONFIRM,
            "mode": mode,
            "create_content": content,
            "create_extended": extended,
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
            content=state.get("create_content", ""),
            extended_content=state.get("create_extended"),
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
        notice = tr("templates.handle_create_save.lichnyy_shablon_sozdan")
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
        state["error_message"] = tr(
            "templates.handle_create_save.ne_udalos_sozdat_shablon"
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
    await query.answer(tr("alerts.templates.otmeneno"))
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
        "create_content",
        "create_extended",
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
                tr("alerts.templates.standartnyy_shablon_dostupen_tolko"),
                show_alert=True,
            )
            return
        template = get_standard_template(session, telegram_user_id, place_id)
        if template is None:
            await query.answer(
                tr("alerts.templates.u_mesta_net_standartnogo"), show_alert=True
            )
            return
        await query.answer()
        state = _card_state(template, origin="admin")
        text, keyboard = _card_content(
            template, place_name=_place_name(session, place_id)
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось открыть стандартный шаблон: %s", exc)
        await query.answer(
            tr("alerts.templates.ne_udalos_otkryt_standartnyy"),
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
        CallbackQueryHandler(handle_edit_text, pattern=r"^tp_edit(_ext)?:\d+$"),
        CallbackQueryHandler(handle_preview, pattern=r"^tp_preview:\d+$"),
        CallbackQueryHandler(handle_reset, pattern=r"^tp_reset:\d+$"),
        CallbackQueryHandler(handle_reset_yes, pattern=r"^tp_reset_yes:\d+$"),
        CallbackQueryHandler(handle_delete, pattern=r"^tp_delete:\d+$"),
        CallbackQueryHandler(handle_delete_yes, pattern=r"^tp_delete_yes:\d+$"),
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
        CallbackQueryHandler(handle_help, pattern=r"^tp_help:\d+$"),
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
