# tests/test_p0_allowed_updates.py
"""
P0-регрессия: запуск бота должен ЯВНО передавать allowed_updates.

Telegram Bot API хранит параметр allowed_updates между вызовами getUpdates
(«If not specified, the previous setting will be used»). Если run_polling()
вызван без allowed_updates, PTB не включает параметр в запрос, и бот
унаследует любую прошлую серверную подписку. Инцидент P0: подписка стала
«только callback_query» — бот формально работал без ошибок, но перестал
получать сообщения (/start и весь текстовый ввод).

Тест не ходит в сеть и не трогает БД: main() вызывается с подменёнными
build_application/init_db/setup_logging.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest
from unittest.mock import MagicMock, patch

# Пакет tests подставляет фиктивный токен в окружение (tests/__init__.py):
# discovery грузит модули как top-level, а этот модуль — первый по
# алфавиту и импортирует bot/config до того, как пакет кто-то тронет.
# Импорт стоит ДО try с `import bot` намеренно — порядок значим.
import tests  # noqa: F401

try:
    from telegram import Update

    import bot as bot_module

    IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    IMPORT_ERROR = str(e)


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class AllowedUpdatesAtStartupTest(unittest.TestCase):
    """run_polling должен получать полный allowed_updates явно."""

    def _run_main_capturing_polling(self) -> dict:
        """Вызывает bot.main() с моками, возвращает kwargs run_polling."""
        app = MagicMock()
        with (
            patch.object(bot_module, "setup_logging"),
            patch.object(bot_module, "logger"),
            patch.object(bot_module, "init_db"),
            patch.object(bot_module, "build_application", return_value=app),
        ):
            bot_module.main()

        app.run_polling.assert_called_once()
        return app.run_polling.call_args.kwargs

    def test_run_polling_receives_explicit_allowed_updates(self) -> None:
        """Без явного allowed_updates бот унаследует прошлую подписку (P0)."""
        kwargs = self._run_main_capturing_polling()
        self.assertIn(
            "allowed_updates",
            kwargs,
            "run_polling вызван без allowed_updates — бот унаследует "
            "прошлую серверную подписку (инцидент P0)",
        )

    def test_allowed_updates_is_full_update_type_list(self) -> None:
        """Подписка должна покрывать и сообщения, и callback-кнопки."""
        kwargs = self._run_main_capturing_polling()
        allowed = kwargs["allowed_updates"]
        self.assertEqual(Update.ALL_TYPES, allowed)
        self.assertIn("message", allowed)
        self.assertIn("callback_query", allowed)


if __name__ == "__main__":
    unittest.main()
