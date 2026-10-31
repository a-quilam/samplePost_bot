# utils/calendar_view.py
"""
Компактный календарь даты мероприятия (Poster 2.2 U2a): чистая логика.

Модуль НЕ знает про Telegram и экран: он только раскладывает разрешённый
интервал дат на уровни календаря, которые затем рисует
`handlers.wizard_questions` / `handlers.announcement_wizard`. Это позволяет
перебрать весь горизонт в тестах и доказать, что множество доступных дат не
имеет пропусков и повторов.

Правила U2a (замена календарных недель Stage 17):
- «сегодня» берётся из согласованной зоны приложения (utils.parsers.APP_TZ);
- быстрые даты — СКОЛЬЗЯЩЕЕ окно из 8 дней: сегодня + следующие 7 дней
  (не привязано к календарной неделе, в воскресенье их тоже восемь);
- дальше два диапазона по 7 дней, которые начинаются сразу после восьмого
  дня быстрого окна и не пересекаются ни с ним, ни между собой;
- выбор месяца — сетка месяцев от первой ещё не охваченной даты, но не
  больше 12 кнопок; месяц горизонта, не поместившийся в сетку (13-й),
  открывается стрелкой «▶️» из последнего месяца (`month_sequence`);
  внутри месяца дни показываются сеткой, границы — [allowed];
- горизонт — 12 календарных месяцев от сегодняшней даты включительно
  (календарное прибавление, а не фиксированные 365 дней);
- пустые интервалы не создаются: всё обрезается по допустимым датам.

Формат кнопок: быстрая дата — «дд.мм дв» (компактно для четырёх кнопок в
строке), день месяца — просто число, диапазон — «дд.мм–дд.мм». Год в
подписи диапазона появляется, только если без него диапазон неоднозначен:
при переходе через год у конечной даты («28.12–03.01.2027») и для любого
диапазона не текущего года («04.01–10.01.2027» при «сегодня» в 2026).
"""

import calendar
from datetime import date, timedelta

from utils.parsers import APP_TZ, app_today
from utils.ui_texts import tr

# Горизонт выбора: календарных месяцев от текущей даты включительно
HORIZON_MONTHS = 12

# Сетка «Выбрать месяц»: не больше 12 кнопок-месяцев. Месяц горизонта, не
# поместившийся в сетку (13-й, когда первая неохваченная дата попадает в
# первый месяц горизонта), открывается стрелкой «▶️» из последнего месяца.
MONTH_GRID_MAX = HORIZON_MONTHS

# Быстрое окно: сегодня + 7 следующих дней (ровно 8 кнопок)
QUICK_DAYS = 8

# Диапазон продолжения: 7 дней (как «неделя», но без привязки к пн/вс)
FOLLOWUP_RANGE_DAYS = 7

# Дни недели: 0 — понедельник (как date.weekday())
WEEKDAYS_SHORT = (
    tr("calendar.weekday.pn"),
    tr("calendar.weekday.vt"),
    tr("calendar.weekday.sr"),
    tr("calendar.weekday.cht"),
    tr("calendar.weekday.pt"),
    tr("calendar.weekday.sb"),
    tr("calendar.weekday.vs"),
)

# Названия месяцев в именительном падеже (кнопки «Ноябрь 2026»)
MONTH_NAMES = {
    1: tr("calendar.month.yanvar"),
    2: tr("calendar.month.fevral"),
    3: tr("calendar.month.mart"),
    4: tr("calendar.month.aprel"),
    5: tr("calendar.month.may"),
    6: tr("calendar.month.iyun"),
    7: tr("calendar.month.iyul"),
    8: tr("calendar.month.avgust"),
    9: tr("calendar.month.sentyabr"),
    10: tr("calendar.month.oktyabr"),
    11: tr("calendar.month.noyabr"),
    12: tr("calendar.month.dekabr"),
}

# Тип интервала дат: (первый день, последний день) включительно
DateRange = tuple[date, date]
# Тип месяца: (год, номер месяца 1..12)
MonthKey = tuple[int, int]

# Границы, которые принимает datetime.date: ими ограничены подделанные
# callback'и `aw_calmonth` (год 0, 10000 или 99999 ронял обработчик).
MIN_YEAR = 1
MAX_YEAR = 9999


def days_in_month(year: int, month: int) -> int:
    """Число дней в календарном месяце (с учётом високосности)."""
    return calendar.monthrange(year, month)[1]


def is_valid_month_key(key: MonthKey) -> bool:
    """
    Год и месяц в границах, которые принимает `datetime.date`.

    Callback'и приходят от пользователя, поэтому `aw_calmonth` с годом 0,
    -1 или 99999 нельзя пропускать в `month_bounds`: `date(...)` бросил бы
    ValueError уже внутри отрисовки, и пользователь увидел бы внутреннюю
    ошибку вместо «экран устарел».
    """
    year, month = key
    return MIN_YEAR <= year <= MAX_YEAR and 1 <= month <= 12


def month_bounds(key: MonthKey) -> DateRange:
    """
    Первый и последний день календарного месяца.

    Недопустимый месяц (год вне 1..9999 или номер вне 1..12) отвергается
    здесь же: подделанный callback не должен доходить до `date(...)`.
    """
    if not is_valid_month_key(key):
        raise ValueError(f"month out of range: {key!r}")
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


def quick_days(today: date, allowed: DateRange | None = None) -> list[date]:
    """
    Быстрое окно: ровно восемь дат — сегодня и следующие семь дней.

    Окно скользящее: оно не зависит от дня недели, поэтому в воскресенье
    в нём те же восемь дат, а не один сегодняшний день. Если правила
    кирпичика сдвигают начало допустимого интервала вперёд (`min_date`),
    окно привязывается к первой доступной дате: дат по-прежнему восемь.
    """
    first = selection_anchor(today, allowed)
    last = first + timedelta(days=QUICK_DAYS - 1)
    if allowed is not None:
        last = min(last, allowed[1])
    return days_between(first, last)


def followup_ranges(today: date, allowed: DateRange | None = None) -> list[DateRange]:
    """
    Два диапазона по семь дней сразу после быстрого окна.

    Для 29.10.2026 быстрые даты — 29.10…05.11, поэтому диапазоны
    06.11–12.11 и 13.11–19.11: они не пересекаются ни с окном, ни между
    собой, и между ними нет пропуска. Окно и диапазоны считаются от одного
    якоря (`today` или первого допустимого дня), поэтому не пересекаются и
    при ограничении `min_date`. Пустые диапазоны отбрасываются.
    """
    first = selection_anchor(today, allowed) + timedelta(days=QUICK_DAYS)
    second = first + timedelta(days=FOLLOWUP_RANGE_DAYS)
    last = timedelta(days=FOLLOWUP_RANGE_DAYS - 1)
    ranges: list[DateRange] = []
    for start, end in ((first, first + last), (second, second + last)):
        if allowed is not None:
            start = max(start, allowed[0])
            end = min(end, allowed[1])
            if start > end:
                continue
        ranges.append((start, end))
    return ranges


def selection_anchor(today: date, allowed: DateRange | None = None) -> date:
    """
    Первый день быстрого окна: сегодня, но не раньше `allowed[0]`.

    Если правила кирпичика сдвигают начало (`min_date`), всё календарное
    меню строится от первого доступного дня — иначе окно и диапазоны
    пересекались бы с запрещёнными датами.
    """
    return today if allowed is None else max(today, allowed[0])


def selection_seam(today: date, allowed: DateRange | None = None) -> date:
    """
    Первая дата, не покрытая быстрым окном и двумя диапазонами.

    21 день покрыт кнопками быстрого выбора (8 + 7 + 7), значит выбор
    месяца начинается со следующего дня, и стык «диапазоны → месяцы»
    непрерывен.
    """
    return selection_anchor(today, allowed) + timedelta(
        days=QUICK_DAYS + 2 * FOLLOWUP_RANGE_DAYS
    )


def first_uncovered(today: date) -> date:
    """
    Первая дата после быстрого окна и двух диапазонов (без учёта правил).

    Обёртка над `selection_seam` для чистых тестов и подписей: 21 день
    покрыт кнопками быстрого выбора.
    """
    return selection_seam(today)


def month_sequence(seam: date, end: date) -> list[MonthKey]:
    """
    Все месяцы от месяца первой неохваченной даты до месяца горизонта.

    Это полная последовательность для стрелок «◀️»/«▶️» и проверки
    достижимости: горизонт длится 12 календарных месяцев, но начинается он
    не с 1-го числа, поэтому при `seam` в начале месяца в последовательности
    оказывается 13 месяцев.
    """
    if seam > end:
        return []
    current = (seam.year, seam.month)
    last = (end.year, end.month)
    months: list[MonthKey] = []
    while current <= last:
        months.append(current)
        current = add_months_key(current, 1)
    return months


def month_grid(seam: date, end: date) -> list[MonthKey]:
    """
    Сетка месячного выбора: не больше 12 кнопок-месяцев.

    Кнопок не больше `MONTH_GRID_MAX`: если первая неохваченная дата попадает
    в первый месяц горизонта (окно и диапазоны не переходят в следующий
    месяц), месяцев в последовательности 13. Месяц горизонта при этом не
    теряется: он открывается стрелкой «▶️» из последнего месяца сетки
    (`month_sequence`), а не отдельной кнопкой.
    """
    return month_sequence(seam, end)[:MONTH_GRID_MAX]


def month_days(key: MonthKey, allowed: DateRange) -> list[date]:
    """
    Дни выбранного месяца в допустимом интервале.

    Обрезаются и по границам месяца, и по [allowed]: при выборе месяца
    нельзя уйти в соседний месяц или за горизонт 12 месяцев.

    Недопустимый ключ месяца даёт пустой список, а не исключение: экран
    показывает «устарел», а не внутреннюю ошибку.
    """
    if not is_valid_month_key(key):
        return []
    month_start, month_end = month_bounds(key)
    return days_between(max(month_start, allowed[0]), min(month_end, allowed[1]))


def format_quick_day(value: date) -> str:
    """
    Быстрая дата: «29.10 чт».

    День недели дан двумя буквами без скобок — подпись остаётся короткой,
    и в строке гарантированно помещаются четыре кнопки.
    """
    return f"{value.day:02d}.{value.month:02d} {WEEKDAYS_SHORT[value.weekday()]}"


def format_month_day(value: date) -> str:
    """День месяца в сетке месяца: только число («1», «29»)."""
    return str(value.day)


def format_range(start: date, end: date, today: date | None = None) -> str:
    """
    Подпись диапазона: «06.11–12.11», одиночный день — «30.11».

    Год добавляется только там, где без него подпись стала бы неоднозначной:
    - диапазон пересекает границу года — год у конечной даты
      («28.12–03.01.2027»);
    - диапазон целиком (или одиночный день) не в текущем году — год у
      конечной даты («04.01–10.01.2027» при «сегодня» в 2026).
    Так в обычном случае подпись остаётся короткой («06.11–12.11»).
    Год конечной даты достаточен: диапазон не длиннее семи дней и не может
    растянуться больше чем на два года.
    """
    if today is None:
        today = app_today_value()
    same_year = start.year == end.year
    needs_year = not same_year or start.year != today.year
    tail = f".{end.year}" if needs_year else ""
    if start == end:
        return f"{start.day:02d}.{start.month:02d}{tail}"
    return f"{start.day:02d}.{start.month:02d}–{end.day:02d}.{end.month:02d}{tail}"


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
    "FOLLOWUP_RANGE_DAYS",
    "HORIZON_MONTHS",
    "MAX_YEAR",
    "MIN_YEAR",
    "MONTH_GRID_MAX",
    "MONTH_NAMES",
    "QUICK_DAYS",
    "WEEKDAYS_SHORT",
    "add_months",
    "add_months_key",
    "app_today_value",
    "days_between",
    "days_in_month",
    "first_uncovered",
    "followup_ranges",
    "format_month",
    "format_month_day",
    "format_quick_day",
    "format_range",
    "horizon_end",
    "is_valid_month_key",
    "month_bounds",
    "month_days",
    "month_grid",
    "month_sequence",
    "quick_days",
    "selection_anchor",
    "selection_seam",
]
