# log_config.py
"""
Настройка логирования: консоль + файл с ротацией.

Подключается в bot.py; модуль не импортирует telegram и config, поэтому
поведение покрыто юнит-тестами (tests/test_stage4.py, tests/test_stage14_logging.py).

Уровень задаётся переменной окружения LOG_LEVEL (по умолчанию INFO).
Файл логов — <каталог модуля>/logs/bot.log с ротацией: до 5 МБ × 5 старых
копий. Путь привязан к каталогу проекта, а не к текущей рабочей директории:
запуск из любого каталога пишет в Poster/logs/, случайные logs/ не создаются.
Каталог можно переопределить параметром setup_logging(log_dir=...).
Токен бота вычищается из сообщений и трассировок (SecretScrubFilter).
"""

import logging
import os
import traceback
from logging.handlers import RotatingFileHandler

# Каталог логов — рядом с модулем (как BASE_DIR в database.py, Stage 12):
# не зависит от того, из какого каталога запущен бот.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE_NAME = "bot.log"

# Единый структурированный формат строки лога
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ротация: 5 МБ на файл, 5 старых копий (bot.log.1 … bot.log.5)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# Маркер «нашего» хендлера — чтобы setup_logging() был идемпотентным
_MARKER_ATTR = "_post_bot_log"

# Чем заменять найденные секреты в журналах
SECRET_PLACEHOLDER = "***"


class SecretScrubFilter(logging.Filter):
    """
    Вычищает значения-секреты (токен бота) из сообщений и трассировок (§5).

    Токен попадает в лог через URL запросов Telegram API (например, при
    LOG_LEVEL=DEBUG у httpx/telegram) и может оказаться в тексте исключения
    (logger.error(..., exc_info=True)). Фильтр ставится на ХЕНДЛЕРЫ: фильтры
    логгера не действуют на пропагированные записи. Работает идемпотентно
    (повторная зачистка уже очищенного текста — no-op).
    """

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._secrets = [s for s in (secrets or []) if s]

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, SECRET_PLACEHOLDER)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        # Сообщение (msg + args) — единая строка без секретов
        record.msg = self._scrub(record.getMessage())
        record.args = None
        # Трассировка: готовый exc_text Formatter.format() использует
        # вместо повторного форматирования exc_info
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)
        elif record.exc_info:
            record.exc_text = self._scrub(
                "".join(traceback.format_exception(*record.exc_info))
            )
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)
        return True


def resolve_level(name: str | None) -> int:
    """
    Уровень логирования из строки (LOG_LEVEL).
    Пустое/неизвестное значение -> INFO (типовая защита от опечаток в .env).
    """
    if not name:
        return logging.INFO
    level = getattr(logging, str(name).upper(), None)
    return level if isinstance(level, int) else logging.INFO


def _build_scrub_filter() -> SecretScrubFilter:
    """
    Фильтр зачистки секретов для хендлеров.

    Токен читается напрямую из окружения (config НЕ импортируется — модуль
    остаётся независимым и тестируется без настоящего токена): config.py
    делает load_dotenv() при импорте, а bot.py импортирует config до
    setup_logging(), поэтому к этому моменту токен уже в переменных
    окружения. Если токена нет — фильтр ничего не меняет.
    """
    return SecretScrubFilter([os.getenv("TELEGRAM_BOT_TOKEN", "")])


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
        console.addFilter(_build_scrub_filter())
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
        file_handler.addFilter(_build_scrub_filter())
        setattr(file_handler, _MARKER_ATTR, True)
        root.addHandler(file_handler)

    return root
