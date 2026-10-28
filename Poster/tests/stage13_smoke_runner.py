# tests/stage13_smoke_runner.py
"""
Процесс «штатного запуска» бота (Stage 13, §1 и §4).

Запускается как скрипт ИЗ ЛЮБОГО рабочего каталога и проверяет без сети
и без настоящего токена:

- импорт всех модулей проекта;
- конфигурацию с фиктивным TELEGRAM_BOT_TOKEN (env приходит от теста);
- bot.main(): сборку Application, регистрацию обработчиков, init_db
  с миграциями на ВРЕМЕННОЙ базе (DATABASE_URL из env);
- настройку polling и явный allowed_updates;
- отсутствие обращений к сети (socket заблокирован на уровне Python).

Логирование не настраивается (setup_logging подменён): файлы логов
привязаны к каталогу проекта (Poster/logs/) и принадлежат действующему
боту — смоук не пишет в действующие журналы; сам setup_logging проверен
unit-тестами Stage 4 и Stage 14.

В stdout печатается строка SMOKE_REPORT=<json>, её читает
tests/test_stage13_smoke.py.
"""

import json
import sys
from pathlib import Path

# Каталог Poster — в sys.path независимо от рабочей директории
POSTER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(POSTER_DIR))


def main() -> int:
    # —— сеть заблокирована ДО любого кода продукта ——
    import socket

    def _no_network(*_args, **_kwargs):
        raise RuntimeError("сеть недоступна: smoke-тест обходится без Telegram API")

    socket.socket.connect = _no_network  # type: ignore[method-assign]
    socket.create_connection = _no_network
    socket.getaddrinfo = _no_network

    from telegram import Update
    from telegram.ext import (
        CallbackQueryHandler,
        ConversationHandler,
        MessageHandler,
    )

    import bot as bot_module
    import database

    # Логи в тестовом процессе не настраиваем: не пишем в действующие
    # журналы пользователя (Poster/logs/)
    bot_module.setup_logging = lambda *args, **kwargs: None  # type: ignore[assignment]

    # run_polling подменяется: реальный опрос Telegram не запускается,
    # но ВСЕ аргументы запуска фиксируются для проверки allowed_updates
    captured: dict = {}

    def _fake_run_polling(self, *args, **kwargs):
        captured.update(kwargs)
        captured["polled"] = True

    import telegram.ext

    telegram.ext.Application.run_polling = _fake_run_polling  # type: ignore[method-assign]

    # —— штатный запуск: сборка, обработчики, БД + миграции, polling ——
    bot_module.main()

    # Отчёт собирается по свежему приложению (main не возвращает его)
    app = bot_module.build_application()
    handlers = app.handlers[0]
    from sqlalchemy import inspect

    tables = sorted(inspect(database.engine).get_table_names())

    report = {
        "polled_without_network": bool(captured.get("polled")),
        "allowed_updates": captured.get("allowed_updates"),
        "allowed_updates_is_all_types": captured.get("allowed_updates")
        == Update.ALL_TYPES,
        "handler_count": len(handlers),
        "has_conversation": any(isinstance(h, ConversationHandler) for h in handlers),
        "callback_handlers": sum(isinstance(h, CallbackQueryHandler) for h in handlers),
        "message_handlers": sum(isinstance(h, MessageHandler) for h in handlers),
        "error_handlers": len(app.error_handlers),
        "database_url": database.SQLALCHEMY_DATABASE_URL,
        "db_tables": tables,
        "db_has_announcement_drafts": "announcement_drafts" in tables,
        "db_has_legacy_drafts_backup": "legacy_drafts_backup" in tables,
        "db_has_user_states": "user_states" in tables,
    }
    print("SMOKE_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
