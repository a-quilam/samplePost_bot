# tests/__init__.py
"""
Пакет тестов Poster.

Тесты НИКОГДА не используют настоящий токен Telegram. config.py требует
TELEGRAM_BOT_TOKEN при импорте, поэтому до импорта любого тестового модуля
в окружение подставляется фиктивный токен — если он не задан извне.
load_dotenv() не перезаписывает существующие переменные окружения, поэтому
локальный .env на тестовый процесс не влияет: набор тестов одинаков на
разработческой машине и в чистом CI-окружении без .env.

Сетевых обращений тесты не делают: Telegram Bot подменяется FakeBot
(интеграционный каркас Stage 9), polling не запускается, смоук Stage 13
блокирует сокеты на уровне Python. Настоящий post_bot.db также
недоступен: DATABASE_URL перенаправляется в одноразовый файл.
"""

import os
import tempfile

# Фиктивный токен в формате Bot API (<digits>:<symbols>): валиден для
# сборки Application, но не существует — сеть в тестах недоступна.
# Пустая строка в окружении тоже считается «не задана» — фиктивный
# токен важнее пустого значения
_FIXTURE_TOKEN = "123456789:FIXTUREFAKETOKENforTestsOnly00000000"

if not os.environ.get("TELEGRAM_BOT_TOKEN"):
    os.environ["TELEGRAM_BOT_TOKEN"] = _FIXTURE_TOKEN

# Настоящий post_bot.db тестам недоступен ВООБЩЕ: unit-тесты зовут
# обработчики напрямую (без интеграционного каркаса) и могут дойти до
# записи состояния (sync_wip → screen.save_state). DATABASE_URL
# направляется в одноразовый файл во временном каталоге — реальная БД
# пользователя не изменяется никогда. Тесты, которым важен путь БД
# (test_stage12_db_path), работают в subprocess с явным DATABASE_URL
# и от этой настройки не зависят.
if not os.environ.get("DATABASE_URL"):
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="poster-test-db-")
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(
        _TEST_DB_DIR, "test_bot.db"
    )
