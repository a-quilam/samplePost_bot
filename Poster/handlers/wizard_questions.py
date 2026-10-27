# handlers/wizard_questions.py
"""
Poster 2.0 S3: вопросы, календарь и время конструктора анонса.

Здесь живёт **вид** вопроса: текст, подсказки по типам, сетка календаря и
быстрые кнопки времени. Оркестрация диалога (сохранение значения, переход к
следующему вопросу, обработка callback) осталась в
`handlers/announcement_wizard.py` — модуль не тянет Telegram-обработчики и
не хранит состояние, поэтому его можно тестировать отдельно.

Поведение вынесено из `announcement_wizard.py` без изменений (S3 п.1):
вопрос «Вопрос N из M», календарь с неделями/месяцами, подсказки типов.
Новое в S3: компактная сетка календаря выводится **сразу** на вопросе даты
и быстрые кнопки времени 18:00 / 18:30 / 19:00 рядом с ручным вводом.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from telegram import InlineKeyboardButton

from announcement_store import (
    FIELD_STATUS_MISSING,
    FIELD_STATUS_SKIPPED,
    FIELD_STATUS_VALID,
    AnnouncementFieldState,
)
from brick_types import (
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_TITLES,
    BRICK_TYPE_URL,
)
from utils import calendar_view
from utils.parsers import app_today
from utils.tg_html import escape_html

# Быстрые значения времени (S3 п.5): самые частые начала вечеринок/встреч.
# Ручной ввод остаётся: кнопки — сокращение, а не замена.
TIME_PRESETS = ("18:00", "18:30", "19:00")

# Сколько дней показывать в компактной сетке «текущая неделя».
CALENDAR_DAYS_PER_ROW = 4


def h(value: object) -> str:
    """HTML-экранирование значения для текста экрана."""
    return escape_html(str(value if value is not None else ""))


def field_status_text(field: AnnouncementFieldState) -> str:
    if field.status == FIELD_STATUS_VALID:
        return "заполнено"
    if field.status == FIELD_STATUS_SKIPPED:
        return "пропущено"
    if field.status == FIELD_STATUS_MISSING:
        return "не заполнено"
    return "из справочника"


def format_value(value: object | None, *, limit: int = 800) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        text = ", ".join(str(item) for item in value)
    else:
        text = str(value)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def field_value_text(field: AnnouncementFieldState) -> str:
    raw = format_value(field.raw_value)
    if (
        field.value_type == BRICK_TYPE_NUMBER
        and raw
        and isinstance(field.rules.get("suffix"), str)
        and field.rules["suffix"]
    ):
        return f"{raw} {field.rules['suffix']}"
    return raw


def number_rules_hint(rules: dict[str, Any]) -> str:
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


def date_rules_hint(rules: dict[str, Any]) -> str:
    parts = []
    if rules.get("min_date"):
        parts.append(f"не раньше {rules['min_date']}")
    if rules.get("max_date"):
        parts.append(f"не позже {rules['max_date']}")
    if rules.get("horizon_months") is not None:
        parts.append(f"горизонт {rules['horizon_months']} мес.")
    return "; ".join(parts)


def type_hint(field: AnnouncementFieldState) -> str:
    value_type = field.value_type
    rules = dict(field.rules or {})
    if value_type == BRICK_TYPE_SHORT_TEXT:
        return "Ожидается короткий текст одним сообщением."
    if value_type == BRICK_TYPE_LONG_TEXT:
        return "Можно отправить несколько строк одним сообщением."
    if value_type == BRICK_TYPE_DATE:
        extra = date_rules_hint(rules)
        base = "Выберите дату календарём или отправьте её текстом, например 31.12.2026."
        return f"{base} Ограничения: {extra}" if extra else base
    if value_type == BRICK_TYPE_TIME:
        presets = ", ".join(TIME_PRESETS)
        return "Отправьте время, например 18:30 или 9:00. Быстрый выбор: " f"{presets}."
    if value_type == BRICK_TYPE_URL:
        return (
            "Отправьте ссылку: https://…, t.me/… или домен. Опасные схемы "
            "не принимаются."
        )
    if value_type == BRICK_TYPE_NUMBER:
        extra = number_rules_hint(rules)
        base = "Отправьте число цифрами, например 12."
        return f"{base} Ограничения: {extra}." if extra else base
    if value_type == BRICK_TYPE_CHOICE:
        if rules.get("multiple"):
            return "Отметьте один или несколько вариантов кнопками, затем «Готово»."
        return "Выберите один вариант кнопкой."
    title = BRICK_TYPE_TITLES.get(value_type, value_type)
    return f"Введите значение типа «{title}»."


def choice_options(field: AnnouncementFieldState) -> list[str]:
    raw = field.rules.get("choices")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(item) for item in raw]


def selected_choices(field: AnnouncementFieldState) -> list[str]:
    raw = field.raw_value
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def question_text(
    field: AnnouncementFieldState,
    index: int,
    total: int,
    *,
    editing: bool = False,
    error: str | None = None,
    notice: str | None = None,
) -> str:
    blocks = [
        f"🧱 Вопрос {index + 1} из {total}: {h(field.title)}",
        type_hint(field),
        "Поле необязательное: его можно пропустить.",
    ]
    if field.status == FIELD_STATUS_VALID:
        value = field_value_text(field)
        blocks.append(f"Текущее значение: {h(value)}")
    elif field.status == FIELD_STATUS_SKIPPED:
        blocks.append("Сейчас поле пропущено. Можно заполнить его или перейти дальше.")
    elif field.status == FIELD_STATUS_MISSING:
        blocks.append("Значение пока не задано.")
    if error:
        blocks.append(f"⚠️ {h(error)}")
    if notice:
        blocks.append(h(notice))
    return "\n\n".join(blocks)


def parse_rule_date(value: object) -> date | None:
    try:
        from datetime import datetime

        return datetime.strptime(str(value), "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def calendar_bounds(rules: dict[str, Any]) -> tuple[date, date]:
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
    minimum = parse_rule_date(rules.get("min_date"))
    maximum = parse_rule_date(rules.get("max_date"))
    if minimum is not None:
        start = max(start, minimum)
    if maximum is not None:
        end = min(end, maximum)
    return start, end


def clip_range(
    value: calendar_view.DateRange, allowed: calendar_view.DateRange
) -> calendar_view.DateRange | None:
    start = max(value[0], allowed[0])
    end = min(value[1], allowed[1])
    if start > end:
        return None
    return start, end


def calendar_title(field: AnnouncementFieldState) -> str:
    value = field_value_text(field)
    lines = [
        # Заголовок кирпичика задаёт человек (автор/админ места) и попадает
        # в экран с parse_mode=HTML — экранируем, как в вопросах.
        f"📅 {h(field.title)}",
        "Выберите дату кнопками. Дату также можно отправить текстом.",
    ]
    if value:
        lines.append(f"Текущее значение: {h(value)}")
    hint = date_rules_hint(dict(field.rules))
    if hint:
        lines.append(f"Ограничения: {hint}.")
    return "\n\n".join(lines)


def day_rows(
    days: list[date],
    prefix: str,
    *,
    per_row: int = CALENDAR_DAYS_PER_ROW,
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


def calendar_shortcut_rows(
    field: AnnouncementFieldState,
    prefix: str,
    *,
    today: date | None = None,
) -> list[list[InlineKeyboardButton]]:
    """
    Компактная сетка календаря прямо на вопросе даты (S3 п.4).

    Сверху — дни текущей недели кнопками (`aw_calday`), затем недели,
    «другая дата» и месяцы. Callback'и прежние, поэтому ручная навигация и
    экран `aw_calroot` работают как раньше; отдельного нажатия «открыть
    календарь» больше не требуется.
    """
    start, end = calendar_bounds(dict(field.rules))
    if start > end:
        return []
    current_day = today or app_today()
    allowed = (start, end)
    rows: list[list[InlineKeyboardButton]] = []

    current = clip_range(calendar_view.current_week(current_day), allowed)
    if current is not None:
        rows.extend(day_rows(calendar_view.days_between(*current), prefix))
        rows.append(
            [
                InlineKeyboardButton(
                    f"Текущая неделя: {calendar_view.format_range(*current)}",
                    callback_data=f"aw_calweek:{prefix}:cur",
                )
            ]
        )
    following = clip_range(calendar_view.next_week(current_day), allowed)
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
            clip_range(item, allowed)
            for item in calendar_view.other_ranges(current_day)
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
    pairs = calendar_view.month_pairs(current_day, end)
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
    # Полный экран календаря остаётся доступным (месяцы/диапазоны) — но уже
    # как дополнительная навигация, а не единственный вход: дни текущей
    # недели видны сразу выше.
    rows.append(
        [
            InlineKeyboardButton(
                "📅 Календарь: месяцы и диапазоны",
                callback_data=f"aw_calroot:{prefix}",
            )
        ]
    )
    return rows


def time_preset_rows(
    prefix: str, *, presets: tuple[str, ...] = TIME_PRESETS
) -> list[list[InlineKeyboardButton]]:
    """Кнопки быстрого времени: `aw_timeset:<draft>:<rev>:<brick>:<value>`."""
    return [
        [
            InlineKeyboardButton(value, callback_data=f"aw_timeset:{prefix}:{value}")
            for value in presets
        ]
    ]


def _short(value: object, limit: int = 60) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
