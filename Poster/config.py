# config.py

import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получение токена бота из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен в .env файле.")


class ConfigError(ValueError):
    """Некорректная конфигурация; сообщение не содержит секретов."""


def parse_superadmin_user_ids(raw: str | None) -> tuple[int, ...]:
    """
    Разбирает `SUPERADMIN_USER_IDS`: постоянные Telegram user ID через запятую.

    Пустая конфигурация = суперадминистратора нет (неявного владельца не
    бывает). Username не принимается: права привязаны только к числовому ID.
    Дубликаты удаляются с сохранением порядка. Значения в тексте ошибки не
    повторяются — только номер некорректного элемента.
    """
    if raw is None or not raw.strip():
        return ()

    values: list[int] = []
    for position, part in enumerate(raw.split(","), start=1):
        item = part.strip()
        if not item:
            raise ConfigError(
                "SUPERADMIN_USER_IDS: пустой элемент в позиции "
                f"{position}; укажите числовые Telegram user ID через запятую."
            )
        if not item.isascii() or not item.isdecimal():
            raise ConfigError(
                "SUPERADMIN_USER_IDS: элемент в позиции "
                f"{position} должен быть положительным целым Telegram user ID."
            )
        value = int(item)
        if value <= 0:
            raise ConfigError(
                "SUPERADMIN_USER_IDS: элемент в позиции "
                f"{position} должен быть положительным Telegram user ID."
            )
        if value not in values:
            values.append(value)
    return tuple(values)


def parse_superadmin_contact_url(raw: str | None) -> str | None:
    """
    Проверяет необязательную ссылку на контакт суперадминистратора.

    Принимаются только абсолютные http(s)-ссылки: они безопасно выводятся в
    HTML-экране. Пустое значение = контакт не настроен. В ошибке само значение
    не повторяется.
    """
    if raw is None or not raw.strip():
        return None
    value = raw.strip()
    if len(value) > 2048:
        raise ConfigError(
            "SUPERADMIN_CONTACT_URL: ссылка слишком длинная (максимум 2048 символов)."
        )
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("SUPERADMIN_CONTACT_URL: укажите абсолютную http(s)-ссылку.")
    if any(char.isspace() for char in value):
        raise ConfigError("SUPERADMIN_CONTACT_URL: ссылка не должна содержать пробелы.")
    return value


# Постоянные Telegram user ID суперадминистраторов. Пустое значение означает,
# что административные функции закрыты; обычные функции бота работают.
SUPERADMIN_USER_IDS = parse_superadmin_user_ids(os.getenv("SUPERADMIN_USER_IDS"))

# Необязательная ссылка (например, на профиль Telegram) для кнопки
# «Я админ места». Реальный адрес задаётся владельцем в .env, не в коде.
SUPERADMIN_CONTACT_URL = parse_superadmin_contact_url(
    os.getenv("SUPERADMIN_CONTACT_URL")
)
