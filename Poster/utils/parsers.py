# utils/parsers.py
"""
Гибкий разбор даты и времени (Stage 8).

Внутренние форматы хранения: дата — ДД.ММ.ГГГГ, время — ЧЧ:ММ.
Относительные даты считаются в согласованной временной зоне приложения
(APP_TZ), а не в произвольной зоне сервера.

ПРАВИЛА ВРЕМЕНИ (вечер по умолчанию, особые случаи 0 и 12):
- «цифровое с минутами» — ЧЧ:ММ и ЧЧ.ММ, а также 4-значная компактная
  запись (ЧЧММ) — БУКВАЛЬНО, без сдвига: 18:30 → 18:30, 12:30 → 12:30,
  1840 → 18:40, 0900 → 09:00, 0000 → 00:00;
- «разговорное» — час без минут («7», «18»), 3 цифры («930» = 9:30),
  запись пробелом («9 30»), «полседьмого», «четверть восьмого»,
  «без четверти семь» — ВЕЧЕРНЕЕ ПРАВИЛО: 1–11 → +12, 0 → 00,
  12 → 00 (12 означает полночь);
- «утра»/«дня» — час без сдвига: 7 утра → 07:00, 12 утра → 12:00,
  12 дня → 12:00;
- «вечера»/«ночи» — час <12 → +12, час 12 → минус 12:
  7 вечера → 19:00, 12:30 ночи → 00:30, 12 ночи → 00:00.
Неоднозначное не угадывается (например, «18:30 вечера» → ошибка).

ПРАВИЛА ДАТЫ:
- ДД.ММ.ГГГГ / ДД.ММ.ГГ / ДД.ММ (год текущий), разделители . / -;
- компактные ДДММ и ДДММГГ;
- «Д Д месяц[ ГГГГ]» (января…декабря, янв…дек);
- «сегодня», «завтра», «послезавтра», «через неделю», «через N дней»;
- дни недели: «в пятницу» — ближайшая строго после сегодняшнего дня,
  «в следующую пятницу» — пятница следующей календарной недели;
  если сегодня пятница — оба дают пятницу следующей недели;
- двухзначный год ГГ трактуется как 20ГГ (правило проекта);
- существование даты проверяется через datetime (31 февраля → ошибка).
"""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from utils.lotto import substitute_lotto

# Согласованная временная зона приложения (относительные даты)
APP_TZ = ZoneInfo("Europe/Moscow")

# Месяцы в родительном падеже + распространённые сокращения
_MONTHS: dict[str, int] = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}

# Дни недели в форме «в пятницу» (винительный) → номер (0 — понедельник)
_WEEKDAYS: dict[str, int] = {
    "понедельник": 0,
    "вторник": 1,
    "среду": 2,
    "четверг": 3,
    "пятницу": 4,
    "субботу": 5,
    "воскресенье": 6,
}

# Числительные-часы (именный падеж): «без четверти семь», «восемь вечера»
_HOUR_WORDS_NOMINATIVE: dict[str, int] = {
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}

# Числительные-часы в родительном падеже: «полседьмого», «четверть восьмого»
_HOUR_WORDS_GENITIVE: dict[str, int] = {
    "первого": 1,
    "второго": 2,
    "третьего": 3,
    "четвертого": 4,
    "пятого": 5,
    "шестого": 6,
    "седьмого": 7,
    "восьмого": 8,
    "девятого": 9,
    "десятого": 10,
    "одиннадцатого": 11,
    "двенадцатого": 12,
}

# Минуты в родительном падеже для «без четверти семь» / «без пятнадцати семь»
_MINUTES_GENITIVE: dict[str, int] = {
    "четверти": 15,
    "полчаса": 30,
    "пяти": 5,
    "десяти": 10,
    "пятнадцати": 15,
    "двадцати": 20,
}

_NOMINATIVE_RE = re.compile(r"^(\d{1,2}|[а-яё]+)$")


def _hour_from_token(token: str, table: dict[str, int]) -> int | None:
    """Час из цифры или слова по словарю; None — не распознано."""
    if token.isdigit():
        value = int(token)
        return value if value <= 23 else None
    return table.get(token)


def _apply_evening(hour: int, minute: int) -> tuple[int, int] | None:
    """Вечернее правило для разговорного времени (без явного времени суток)."""
    if hour == 0 or hour == 12:
        return 0, minute
    if 1 <= hour <= 11:
        return hour + 12, minute
    if 13 <= hour <= 23:
        return hour, minute
    return None


def _apply_period(hour: int, minute: int, period: str | None) -> tuple[int, int] | None:
    """Правило времени суток; period — утра/дня/вечера/ночи или None."""
    if period is None:
        return _apply_evening(hour, minute)
    if period in ("утра", "дня"):
        # Явное утро/день: час без сдвига (12 утра → 12:00)
        return (hour, minute) if 0 <= hour <= 12 else None
    # вечера / ночи
    if 1 <= hour <= 11:
        return hour + 12, minute
    if hour == 12:
        return 0, minute
    return None  # 13+ с «вечера/ночи» — не угадываем


def _finish(hour: int, minute: int) -> str | None:
    """Сборка ЧЧ:ММ с проверкой границ."""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_time_flexible(text: str) -> str | None:
    """
    Гибкий разбор времени. Возвращает «ЧЧ:ММ» или None.

    См. модульный docstring: цифровая запись — буквально, разговорная —
    с вечерним правилом, явные утро/день/вечер/ночь — по правилу периода.
    """
    raw = substitute_lotto(text or "").strip()
    if not raw:
        return None

    period: str | None = None
    m = re.search(r"\s+(утра|дня|вечера|ночи)$", raw)
    if m:
        period = m.group(1)
        raw = raw[: m.start()].strip()

    def _resolve(hour: int, minute: int, *, literal: bool = False) -> str | None:
        # Цифровая запись (literal) — без вечернего сдвига; сдвиг только при
        # явном времени суток («12:30 ночи» → 00:30). Разговорная — вечером.
        applied: tuple[int, int] | None
        if literal and period is None:
            applied = (hour, minute)
        else:
            applied = _apply_period(hour, minute, period)
        if applied is None:
            return None
        return _finish(*applied)

    # Цифровое с разделителем «:» или «.» — буквально
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", raw)
    if m:
        return _resolve(int(m.group(1)), int(m.group(2)), literal=True)

    # Компактные 4 цифры ЧЧММ — буквально
    m = re.fullmatch(r"(\d{4})", raw)
    if m:
        return _resolve(int(raw[:2]), int(raw[2:]), literal=True)

    # 3 цифры «930» — разговорный час с минутами
    m = re.fullmatch(r"(\d)(\d{2})", raw)
    if m:
        return _resolve(int(m.group(1)), int(m.group(2)))

    # Запись пробелом «18 30» — разговорный час с минутами
    m = re.fullmatch(r"(\d{1,2}) (\d{2})", raw)
    if m:
        return _resolve(int(m.group(1)), int(m.group(2)))

    # «пол седьмого» / «пол12» / «половина седьмого» — (час − 1):30
    m = re.fullmatch(r"(?:пол|половина)\s*([а-яё]+|\d{1,2})", raw)
    if m:
        hour = _hour_from_token(m.group(1), _HOUR_WORDS_GENITIVE)
        if hour is not None and 1 <= hour <= 12:
            return _resolve(hour - 1, 30)
        return None

    # «четверть восьмого» — (час − 1):15
    m = re.fullmatch(r"четверть\s+([а-яё]+|\d{1,2})", raw)
    if m:
        hour = _hour_from_token(m.group(1), _HOUR_WORDS_GENITIVE)
        if hour is not None and 1 <= hour <= 12:
            return _resolve(hour - 1, 15)
        return None

    # «без четверти семь» / «без 15 пять» / «без пятнадцати семь» — час − минуты
    m = re.fullmatch(
        r"без\s+(четверть|полчаса|\d{1,2}|[а-яё]+)\s+([а-яё]+|\d{1,2})", raw
    )
    if m:
        minus_token = m.group(1)
        if minus_token == "четверть":
            minus = 15
        elif minus_token == "полчаса":
            minus = 30
        elif minus_token.isdigit():
            minus = int(minus_token)
        else:
            minus = _MINUTES_GENITIVE.get(minus_token, -1)
        hour = _hour_from_token(m.group(2), _HOUR_WORDS_NOMINATIVE)
        if hour is None or not 0 <= minus <= 59:
            return None
        applied = _apply_period(hour, 0, period)
        if applied is None:
            return None
        total = applied[0] * 60 + applied[1] - minus
        if total < 0:
            return None
        return _finish(total // 60, total % 60)

    # Час без минут: «7», «18», «0» (после пасхалки — и «топор» → «7»)
    m = _NOMINATIVE_RE.fullmatch(raw)
    if m:
        hour = _hour_from_token(m.group(1), _HOUR_WORDS_NOMINATIVE)
        if hour is not None:
            return _resolve(hour, 0)

    return None


def _validate_ymd(year: int, month: int, day: int) -> date | None:
    """Существование даты через datetime (високосные годы, 31 февраля)."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_flexible(text: str, today: date | None = None) -> str | None:
    """
    Гибкий разбор даты. Возвращает «ДД.ММ.ГГГГ» или None.

    today подаётся в тестах; по умолчанию — текущая дата в APP_TZ.
    """
    if today is None:
        today = datetime.now(APP_TZ).date()
    raw = substitute_lotto(text or "").strip()
    if not raw:
        return None

    parsed: date | None = None

    # Относительные даты
    if raw == "сегодня":
        parsed = today
    elif raw == "завтра":
        parsed = today + timedelta(days=1)
    elif raw == "послезавтра":
        parsed = today + timedelta(days=2)
    elif raw == "через неделю":
        parsed = today + timedelta(days=7)
    else:
        m = re.fullmatch(r"через (\d{1,4}) (?:дн|дня|дней|день)", raw)
        if m:
            delta = int(m.group(1))
            if 1 <= delta <= 3650:
                parsed = today + timedelta(days=delta)

    # Дни недели: «в пятницу» / «в следующую пятницу»
    if parsed is None:
        m = re.fullmatch(
            r"(?:в|во)\s+(?:следующ(?:ий|ую|ее)\s+)?"
            r"(понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)",
            raw,
        )
        if m:
            weekday = _WEEKDAYS[m.group(1)]
            if raw.split()[1] in ("следующий", "следующую", "следующее"):
                # Пятница (и любой день) следующей календарной недели
                monday = today - timedelta(days=today.weekday())
                parsed = monday + timedelta(days=7 + weekday)
            else:
                # Ближайший такой день строго после сегодняшнего
                delta = (weekday - today.weekday()) % 7 or 7
                parsed = today + timedelta(days=delta)

    # Числовые формы: ДД.ММ[.ГГГГ|ГГ], разделители . / -
    if parsed is None:
        m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2}|\d{4}))?", raw)
        if m:
            year_part = m.group(3)
            if year_part is None:
                year = today.year
            elif len(year_part) == 2:
                year = 2000 + int(year_part)  # правило проекта: ГГ → 20ГГ
            else:
                year = int(year_part)
            parsed = _validate_ymd(year, int(m.group(2)), int(m.group(1)))

    # Компактные формы: ДДММ и ДДММГГ
    if parsed is None:
        m = re.fullmatch(r"(\d{2})(\d{2})", raw)
        if m:
            parsed = _validate_ymd(today.year, int(m.group(2)), int(m.group(1)))
        else:
            m = re.fullmatch(r"(\d{2})(\d{2})(\d{2})", raw)
            if m:
                parsed = _validate_ymd(
                    2000 + int(m.group(3)), int(m.group(2)), int(m.group(1))
                )

    # «17 ноября» / «17 ноя» / «17 ноября 2026»
    if parsed is None:
        m = re.fullmatch(
            r"(\d{1,2}) (января|февраля|марта|апреля|мая|июня|июля|августа"
            r"|сентября|октября|ноября|декабря"
            r"|янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек)\.?"
            r"(?: (\d{4}))?",
            raw,
        )
        if m:
            year = int(m.group(3)) if m.group(3) else today.year
            parsed = _validate_ymd(year, _MONTHS[m.group(2)], int(m.group(1)))

    if parsed is None:
        return None
    return parsed.strftime("%d.%m.%Y")
