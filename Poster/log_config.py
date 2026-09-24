# log_config.py
"""
Настройка логирования: консоль + файл с ротацией.

Подключается в bot.py; модуль не импортирует telegram и config, поэтому
поведение покрыто юнит-тестами (tests/test_stage4.py).

Уровень задаётся переменной окружения LOG_LEVEL (по умолчанию INFO).
Файл логов — logs/bot.log с ротацией: до 5 МБ × 5 старых копий.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE_NAME = "bot.log"

# Единый структурированный формат строки лога
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ротация: 5 МБ на файл, 5 старых копий (bot.log.1 … bot.log.5)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Маркер «нашего» хендлера — чтобы setup_logging() был идемпотентным
_MARKER_ATTR = "_post_bot_log"


def resolve_level(name: str | None) -> int:
    """
    Уровень логирования из строки (LOG_LEVEL).
    Пустое/неизвестное значение -> INFO (типовая защита от опечаток в .env).
    """
    if not name:
        return logging.INFO
    level = getattr(logging, str(name).upper(), None)
    return level if isinstance(level, int) else logging.INFO


def setup_logging(
    level: int | None = None, log_dir: str | None = None
) -> logging.Logger:
    """
    Настраивает корневой логгер: консоль + файл с ротацией.

    level — явный уровень; по умолчанию берётся из переменной LOG_LEVEL.
    log_dir — каталог для файла логов (по умолчанию LOG_DIR).
    Идемпотентно: повторный вызов не добавляет дублирующих хендлеров.
    """
    if level is None:
        level = resolve_level(os.getenv("LOG_LEVEL"))
    directory = log_dir or LOG_DIR
    os.makedirs(directory, exist_ok=True)
    log_path = os.path.abspath(os.path.join(directory, LOG_FILE_NAME))

    root = logging.getLogger()
    root.setLevel(level)

    # httpx логирует каждый запрос на api.telegram.org, а в URL запросов
    # Telegram API попадает токен бота — INFO-уровень httpx не пишем в лог.
    # Ошибки сети при этом останутся (WARNING+), их же пишет telegram.ext.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    marked = [h for h in root.handlers if getattr(h, _MARKER_ATTR, False)]

    if not any(not isinstance(h, RotatingFileHandler) for h in marked):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        setattr(console, _MARKER_ATTR, True)
        root.addHandler(console)

    if not any(
        isinstance(h, RotatingFileHandler) and h.baseFilename == log_path
        for h in marked
    ):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT))
        setattr(file_handler, _MARKER_ATTR, True)
        root.addHandler(file_handler)

    return root
