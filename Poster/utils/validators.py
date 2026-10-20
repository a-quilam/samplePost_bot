# utils/validators.py
"""
Валидация и нормализация пользовательского ввода (Stage 8).

Дата и время разбираются гибко (utils.parsers) и хранятся в
нормализованном виде: ДД.ММ.ГГГГ и ЧЧ:ММ.
Ссылки нормализуются до полного URL (utils.parsers не нужен —
здесь urllib/re): @user → https://t.me/user, t.me/… и telegram.me/…
→ https://t.me/…, голый домен → https://домен.
Опасные схемы (javascript:, data:, file: и т.п.) и некорректные
адреса отклоняются; произвольный текст ссылкой не становится.
"""

import re
from urllib.parse import urlsplit

from utils.parsers import parse_date_flexible, parse_time_flexible


def normalize_date(date_text: str) -> str | None:
    """
    Нормализует дату к ДД.ММ.ГГГГ или None, если не распознана.
    """
    return parse_date_flexible(date_text)


def normalize_time(time_text: str) -> str | None:
    """
    Нормализует время к ЧЧ:ММ или None, если не распознано.
    """
    return parse_time_flexible(time_text)


def validate_date(date_text: str) -> bool:
    """
    Проверяет, распознаётся ли строка как дата (гибкие форматы).
    """
    return parse_date_flexible(date_text) is not None


def validate_time(time_text: str) -> bool:
    """
    Проверяет, распознаётся ли строка как время (гибкие форматы).
    """
    return parse_time_flexible(time_text) is not None


# Telegram-хосты, приводимые к каноническому t.me
_TELEGRAM_HOSTS = {"t.me", "telegram.me"}

# Домен: минимум одна точка, буквы/цифры/дефис, без краевых дефисов
_DOMAIN_RE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}$")
# Схема в начале строки («javascript:», «data:», «https:»…)
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
# @username Telegram: 5–32 знака, буква или подчёркивание в начале
_USERNAME_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{4,31})$")


def normalize_url(url_text: str) -> str | None:
    """
    Нормализует ввод к полному URL или None, если это не ссылка.

    - «@user» → «https://t.me/user»;
    - «t.me/x», «telegram.me/x» (с http(s):// или без) → «https://t.me/x»;
    - «example.com», «www.example.com» → «https://…» (хост не переписывается);
    - готовые корректные http(s)-ссылки не портятся: путь, параметры и
      якорь сохраняются дословно;
    - схемы вне http/https (javascript:, data:, file:, tg:…) → None;
    - учётные записи (user@host), пробелы, контрольные символы → None.
    """
    text = (url_text or "").strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None

    # Telegram username
    m = _USERNAME_RE.fullmatch(text)
    if m:
        return f"https://t.me/{m.group(1)}"

    scheme = "https"
    rest = text
    m = _SCHEME_RE.match(text)
    if m:
        scheme = m.group(1).lower()
        if scheme not in ("http", "https"):
            return None
        rest = text[m.end() :]
        if not rest.startswith("//"):
            # «javascript:…», «data:…», «http:без-слэша» — не угадываем
            return None
        rest = rest[2:]

    # Хост[:порт][/путь?параметры#якорь]
    m = re.match(r"^([^/?#]+)([/?#].*)?$", rest)
    if not m:
        return None
    hostpart, tail = m.group(1), m.group(2) or ""
    if "@" in hostpart:
        return None  # учётные записи запрещены

    pm = re.fullmatch(r"([A-Za-z0-9.\-]+)(?::(\d+))?", hostpart)
    if not pm:
        return None
    host, port = pm.group(1), pm.group(2)
    if port is not None and not 1 <= int(port) <= 65535:
        return None

    bare = host.lower()
    if bare.startswith("www."):
        bare = bare[4:]
    if bare in _TELEGRAM_HOSTS:
        host = "t.me"  # telegram.me и www.t.me → канонический t.me

    if not _DOMAIN_RE.fullmatch(host):
        return None  # не домен: произвольный текст, IP, localhost…

    result = f"{scheme}://{host}{(':' + port) if port else ''}{tail}"

    # Финальная проверка разбором URL
    parts = urlsplit(result)
    if not parts.netloc or not parts.hostname or "." not in parts.hostname:
        return None
    return result


def validate_url(url_text: str) -> bool:
    """
    Проверяет, является ли строка действительным URL (после нормализации).
    """
    return normalize_url(url_text) is not None
