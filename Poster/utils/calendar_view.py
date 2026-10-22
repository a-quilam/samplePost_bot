# utils/calendar_view.py
"""
Кнопочный календарь даты мероприятия (Stage 17): чистая календарная логика.

Модуль НЕ знает про Telegram и экран: он только раскладывает разрешённый
интервал дат на уровни меню, которые затем рисует handlers.post_creation.
Это позволяет перебрать весь горизонт в тестах и доказать, что множество
доступных дат не имеет пропусков и повторов.

Правила Stage 17:
- «сегодня» берётся из согласованной зоны приложения (utils.parsers.APP_TZ);
- текущая неделя — с СЕГОДНЯ до воскресенья (прошедшие дни не показываются);
- следующая неделя — понедельник–воскресенье следующей календарной недели;
- «Другая» — два двухнедельных диапазона, начиная с понедельника после
  следующей недели; далее — пары календарных месяцев;
- горизонт — 12 календарных месяцев от сегодняшней даты включительно
  (календарное прибавление, а не фиксированные 365 дней);
- первая пара месяцев начинается с месяца, содержащего первую ещё не
  охваченную дату, поэтому стык «два диапазона → месяцы» не теряет дней;
- недели и месяцы обрезаются по горизонту; пустые интервалы не создаются.

Формат кнопок дня — строго «дд.мм (дв)» с русскими двухбуквенными днями
недели. Диапазоны показываются как «дд.мм–дд.мм», при переходе через год
годы добавляются («29.12.2026–11.01.2027»); одиночный день — «дд.мм».
"""

import calendar
from datetime import date, timedelta

from utils.parsers import APP_TZ, app_today

# Горизонт выбора: календарных месяцев от текущей даты включительно
HORIZON_MONTHS = 12

# Дни недели: 0 — понедельник (как date.weekday())
WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")

# Названия месяцев в именительном падеже (кнопки «Ноябрь 2026»)
MONTH_NAMES = {
    1: "Январь",
    2: "Февраль",
    3: "Март",
    4: "Апрель",
    5: "Май",
    6: "Июнь",
    7: "Июль",
    8: "Август",
    9: "Сентябрь",
    10: "Октябрь",
    11: "Ноябрь",
    12: "Декабрь",
}

# Тип интервала дат: (первый день, последний день) включительно
DateRange = tuple[date, date]
# Тип месяца: (год, номер месяца 1..12)
MonthKey = tuple[int, int]


def days_in_month(year: int, month: int) -> int:
    """Число дней в календарном месяце (с учётом високосности)."""
    return calendar.monthrange(year, month)[1]


def month_bounds(key: MonthKey) -> DateRange:
    """Первый и последний день календарного месяца."""
    year, month = key
    return date(year, month, 1), date(year, month, days_in_month(year, month))


def add_months_key(key: MonthKey, months: int) -> MonthKey:
    """Сдвиг месяца на N календарных месяцев (без дней)."""
    year, month = key
    total = year * 12 + (month - 1) + months
    shifted_year, shifted_month = divmod(total, 12)
    return shifted_year, shifted_month + 1


def add_months(value: date, months: int) -> date:
    """
    Календарное прибавление месяцев с прижатием дня к концу месяца.

    31.01 + 1 месяц → 28/29.02, 29.02.2028 + 12 месяцев → 28.02.2029.
    Именно так считается граница горизонта: это «та же дата через
    12 календарных месяцев», а не 365 дней.
    """
    year, month = add_months_key((value.year, value.month), months)
    day = min(value.day, days_in_month(year, month))
    return date(year, month, day)


def horizon_end(today: date) -> date:
    """Последняя доступная дата: сегодня + 12 календарных месяцев включительно."""
    return add_months(today, HORIZON_MONTHS)


def days_between(start: date, end: date) -> list[date]:
    """Все даты интервала включительно (пустой список, если start > end)."""
    if start > end:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def current_week(today: date) -> DateRange:
    """
    Оставшиеся дни текущей календарной недели: сегодня — воскресенье.

    Прошедшие дни не показываются: в понедельник интервал — вся неделя,
    в воскресенье — только сегодняшний день.
    """
    return today, today + timedelta(days=6 - today.weekday())


def next_week(today: date) -> DateRange:
    """Следующая календарная неделя: понедельник–воскресенье."""
    monday = today - timedelta(days=today.weekday())
    start = monday + timedelta(days=7)
    return start, start + timedelta(days=6)


def other_ranges(today: date) -> list[DateRange]:
    """
    Два двухнедельных диапазона кнопки «Другая».

    Первый начинается с понедельника после следующей календарной недели
    и длится 14 дней; второй — следующие 14 дней. Для 22.10.2026:
    02.11–15.11 и 16.11–29.11.
    """
    monday = today - timedelta(days=today.weekday())
    first_start = monday + timedelta(days=14)
    second_start = monday + timedelta(days=28)
    return [
        (first_start, first_start + timedelta(days=13)),
        (second_start, second_start + timedelta(days=13)),
    ]


def first_uncovered(today: date) -> date:
    """
    Первая дата, не покрытая текущей/следующей неделей и двумя диапазонами.

    Стык «диапазоны → месяцы» обязан быть непрерывным: для 22.10.2026
    диапазоны заканчиваются 29.11.2026, значит первая неохваченная дата —
    30.11.2026 (с неё и начинается первая пара месяцев).
    """
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=42)


def weeks_covering(start: date, end: date) -> list[DateRange]:
    """
    Календарные недели (пн–вс), покрывающие интервал [start, end].

    Каждая неделя обрезается по границам интервала: неполная первая или
    последняя неделя не выходит за разрешённые даты. Пустых недель нет.
    """
    if start > end:
        return []
    cursor = start - timedelta(days=start.weekday())
    weeks: list[DateRange] = []
    while cursor <= end:
        week_start = max(cursor, start)
        week_end = min(cursor + timedelta(days=6), end)
        if week_start <= week_end:
            weeks.append((week_start, week_end))
        cursor += timedelta(days=7)
    return weeks


def month_weeks(key: MonthKey, allowed: DateRange) -> list[DateRange]:
    """
    Недели выбранного месяца в разрешённом интервале.

    Обрезаются и по границам месяца, и по [allowed]: при выборе месяца
    нельзя уйти в соседний месяц или за горизонт 12 месяцев.
    """
    month_start, month_end = month_bounds(key)
    start = max(month_start, allowed[0])
    end = min(month_end, allowed[1])
    return weeks_covering(start, end)


def month_pairs(today: date, horizon: date) -> list[list[MonthKey]]:
    """
    Пары календарных месяцев для кнопки «Дальше».

    Первая пара начинается с месяца, содержащего первую неохваченную дату
    (для 22.10.2026 — ноябрь и декабрь 2026). Последняя пара содержит месяц
    горизонта; если второй месяц пары уже за горизонтом, он не показывается
    (пустых месяцев и интервалов не бывает).
    """
    first = (first_uncovered(today).year, first_uncovered(today).month)
    last = (horizon.year, horizon.month)
    pairs: list[list[MonthKey]] = []
    current = first
    while current <= last:
        pair = [current]
        second = add_months_key(current, 1)
        if second <= last:
            pair.append(second)
        pairs.append(pair)
        current = add_months_key(current, 2)
    return pairs


def pair_index(today: date, key: MonthKey) -> int | None:
    """
    Индекс пары месяцев, содержащей месяц key (None — месяц до первой пары).

    Нужен для кнопки «Назад» с уровня недель месяца: возвращает к той паре,
    из которой пользователь пришёл (а не всегда к первой).
    """
    first = (first_uncovered(today).year, first_uncovered(today).month)
    delta = (key[0] - first[0]) * 12 + (key[1] - first[1])
    if delta < 0:
        return None
    return delta // 2


def format_day(value: date) -> str:
    """Кнопка конкретной даты: строго «дд.мм (дв)», например «17.11 (вт)»."""
    return f"{value.day:02d}.{value.month:02d} ({WEEKDAYS_SHORT[value.weekday()]})"


def format_range(start: date, end: date) -> str:
    """
    Подпись диапазона: «02.11–15.11», одиночный день — «30.11».

    При переходе через границу года годы добавляются, иначе диапазон
    «29.12–11.01» был бы неоднозначным.
    """
    if start == end:
        return f"{start.day:02d}.{start.month:02d}"
    if start.year != end.year:
        return (
            f"{start.day:02d}.{start.month:02d}.{start.year}–"
            f"{end.day:02d}.{end.month:02d}.{end.year}"
        )
    return f"{start.day:02d}.{start.month:02d}–{end.day:02d}.{end.month:02d}"


def format_month(key: MonthKey) -> str:
    """Название месяца по-русски с годом: «Ноябрь 2026»."""
    year, month = key
    return f"{MONTH_NAMES[month]} {year}"


def app_today_value() -> date:
    """
    «Сегодня» календаря в зоне приложения.

    Отдельная обёртка нужна, чтобы тесты замораживали время в одном месте
    (utils.parsers) и календарь с текстовым парсером видели одну дату.
    """
    return app_today()


__all__ = [
    "APP_TZ",
    "HORIZON_MONTHS",
    "MONTH_NAMES",
    "WEEKDAYS_SHORT",
    "add_months",
    "add_months_key",
    "app_today_value",
    "current_week",
    "days_between",
    "days_in_month",
    "first_uncovered",
    "format_day",
    "format_month",
    "format_range",
    "horizon_end",
    "month_bounds",
    "month_pairs",
    "month_weeks",
    "next_week",
    "other_ranges",
    "pair_index",
    "weeks_covering",
]
