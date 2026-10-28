# tests/test_stage5.py
"""
Инфраструктурные тесты (Stage 5, эксплуатационная часть).

После удаления легаси-мастера (Poster 2.0 S5) здесь остались только
проверки общей инфраструктуры:

- models.utcnow(): наивный UTC вместо устаревшего datetime.utcnow;
- PRAGMA SQLite (busy timeout, WAL, synchronous) — конкурентный доступ;
- errors.py: уведомление пользователя об ошибке + троттлинг,
  подавление «message is not modified»;
- главное меню Poster 2.0: пять кнопок и справка /help.

Девятишаговый мастер, его WIP, split_long_text/send_post, TTL и пагинация
черновиков удалены вместе с легаси-кодом: доставка живёт в delivery.py
(tests/test_poster2_delivery.py), список анонсов — в «📂 Мои анонсы»
(tests/test_poster2_menu.py).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import asyncio
import os
import tempfile
import unittest
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  — регистрация таблиц в Base.metadata
from base import Base
from database import SQLITE_BUSY_TIMEOUT_SECONDS, apply_sqlite_pragmas
from models import User, utcnow

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    import utils.screen as screen_module
    from handlers.main_menu import help_command, main_menu_keyboard

    HANDLERS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HANDLERS_IMPORT_ERROR = str(e)


def make_session_factory():
    """Фабрика сессий над отдельной БД в памяти."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


class _ScreenBot:
    """Заглушка бота: записывает отрисовки экрана (send/edit/delete)."""

    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append({"type": "message", **kwargs})

    async def edit_message_text(self, **kwargs):
        self.calls.append({"type": "edit_message", **kwargs})

    async def delete_message(self, *args, **kwargs):
        # cleanup_messages зовёт позиционно: delete_message(chat_id, message_id)
        record = {
            "type": "delete_message",
            "chat_id": args[0] if args else kwargs.get("chat_id"),
            **kwargs,
        }
        if len(args) > 1:
            record["message_id"] = args[1]
        self.calls.append(record)


class UtcnowTests(unittest.TestCase):
    """models.utcnow(): наивный UTC, без DeprecationWarning."""

    def test_naive_utc_close_to_now(self):
        value = utcnow()
        self.assertIsNone(value.tzinfo)
        self.assertLess(abs((datetime_now_utc() - value).total_seconds()), 5)

    def test_no_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            models.utcnow()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(deprecations, [])

    def test_model_created_at_naive(self):
        session_factory, engine = make_session_factory()
        session = session_factory()
        try:
            user = User(telegram_user_id=1, full_name="Тест")
            session.add(user)
            session.commit()
            self.assertIsNone(user.created_at.tzinfo)
        finally:
            session.close()
            engine.dispose()

    def test_update_refreshes_updated_at(self):
        # A2: правка строки обновляет updated_at (onupdate модели)
        session_factory, engine = make_session_factory()
        session = session_factory()
        try:
            user = User(telegram_user_id=1, full_name="Старое")
            session.add(user)
            session.commit()
            self.assertIsNotNone(user.updated_at)

            user.full_name = "Новое"
            session.commit()
            session.refresh(user)
            self.assertLess(
                abs((datetime_now_utc() - user.updated_at).total_seconds()), 5
            )
        finally:
            session.close()
            engine.dispose()


def datetime_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


class SqlitePragmaTests(unittest.TestCase):
    """PRAGMA: busy timeout + WAL — защита от «database is locked»."""

    def _engine_with_pragmas(self, url):
        engine = create_engine(url)
        from sqlalchemy import event

        event.listen(engine, "connect", apply_sqlite_pragmas)
        return engine

    def test_wal_and_timeouts_on_file_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_pragmas(
                f"sqlite:///{os.path.join(tmp, 'check.db')}"
            )
            try:
                with engine.connect() as connection:
                    busy = connection.execute(text("PRAGMA busy_timeout")).scalar()
                    journal = connection.execute(text("PRAGMA journal_mode")).scalar()
                    sync = connection.execute(text("PRAGMA synchronous")).scalar()
                self.assertEqual(busy, SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
                self.assertEqual(journal, "wal")
                # 1 = NORMAL
                self.assertEqual(sync, 1)
            finally:
                engine.dispose()

    def test_in_memory_db_does_not_error(self):
        # :memory: не поддерживает WAL — PRAGMA не должна ронять соединение
        engine = self._engine_with_pragmas("sqlite:///:memory:")
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()


class ErrorNoticeTests(unittest.TestCase):
    """errors.py: ответ пользователю при ошибках, троттлинг, подавление."""

    def setUp(self):
        import errors

        self.errors = errors
        errors._last_error_notice.clear()
        self._orig_notice = errors._send_error_notice
        self.notice = AsyncMock(return_value=True)
        errors._send_error_notice = self.notice

    def tearDown(self):
        self.errors._send_error_notice = self._orig_notice
        self.errors._last_error_notice.clear()

    @staticmethod
    def _update(chat_id=555):
        from datetime import datetime

        from telegram import Chat, Message, Update

        return Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime(2026, 1, 1),
                chat=Chat(id=chat_id, type="private"),
                text="тест",
            ),
        )

    def test_not_modified_is_not_notified(self):
        from telegram.error import BadRequest

        context = SimpleNamespace(
            error=BadRequest("Message is not modified: нажатие повторено")
        )
        with self.assertLogs(self.errors.logger, level="INFO"):
            asyncio.run(self.errors.error_handler(self._update(), context))
        self.notice.assert_not_awaited()

    def test_other_error_notifies_user_with_chat_id(self):
        context = SimpleNamespace(error=RuntimeError("boom"))
        with self.assertLogs(self.errors.logger, level="ERROR"):
            asyncio.run(self.errors.error_handler(self._update(), context))
        self.notice.assert_awaited_once()
        self.assertEqual(self.notice.await_args.args[1], 555)

    def test_non_update_object_does_not_crash(self):
        context = SimpleNamespace(error=RuntimeError("boom"))
        with self.assertLogs(self.errors.logger, level="ERROR"):
            asyncio.run(self.errors.error_handler(object(), context))
        self.notice.assert_not_awaited()

    def test_throttles_repeated_notices_per_chat(self):
        # Реальная реализация троттлинга (мок в setUp подменён обратно)
        self.errors._send_error_notice = self._orig_notice
        message = SimpleNamespace(reply_text=AsyncMock())

        first = asyncio.run(self.errors._send_error_notice(message, 42))
        second = asyncio.run(self.errors._send_error_notice(message, 42))

        self.assertTrue(first)
        self.assertFalse(second)
        message.reply_text.assert_awaited_once()


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class MainMenuKeyboardTests(unittest.TestCase):
    """Главное меню: inline-клавиатура экрана + справка /help."""

    def test_keyboard_structure(self):
        from telegram import InlineKeyboardMarkup

        keyboard = main_menu_keyboard()
        self.assertIsInstance(keyboard, InlineKeyboardMarkup)
        texts = [button.text for row in keyboard.inline_keyboard for button in row]
        # Poster 2.0 S2: главное меню — ровно пять кнопок.
        self.assertEqual(
            texts,
            [
                "📍 Создать для площадки",
                "📝 Создать по личному шаблону",
                "📂 Мои анонсы",
                "⚙️ Управление",
                "❓ Помощь",
            ],
        )
        callbacks = [
            button.callback_data for row in keyboard.inline_keyboard for button in row
        ]
        self.assertEqual(
            callbacks,
            [
                "pick_place",
                "pick_template",
                "my_announcements",
                "manage",
                "help",
            ],
        )

    def test_help_lists_commands(self):
        bot = _ScreenBot()
        session_factory, engine = make_session_factory()
        self.addCleanup(engine.dispose)
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=AsyncMock()),
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=777),
        )
        with patch.object(screen_module, "SessionLocal", session_factory):
            asyncio.run(help_command(update, SimpleNamespace(user_data={}, bot=bot)))
        # справка рисуется на экране управления (send/edit), не reply-сообщением
        self.assertEqual(len(bot.calls), 1)
        text = bot.calls[0]["text"]
        for command in ("/start", "/new_wizard", "/drafts", "/cancel", "/help"):
            self.assertIn(command, text)
        # Poster 2.0 S2: справка описывает кнопки нового меню и то, что
        # готовый анонс приходит в личный чат.
        self.assertIn("Создать для площадки", text)
        self.assertIn("Создать по личному шаблону", text)
        self.assertIn("Мои анонсы", text)
        self.assertIn("Управление", text)
        self.assertIn("готовый пост в этот чат", text)


if __name__ == "__main__":
    unittest.main()
