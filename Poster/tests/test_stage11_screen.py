# tests/test_stage11_screen.py
"""
Stage 11: инфраструктура единого интерфейсного сообщения (utils.screen).

Проверяет без Telegram и без Application:
- render_screen: первый рендер создаёт экран, повторный правит его;
- устаревший/удалённый экран → новый (без ошибки для пользователя);
- «message is not modified» проглатывается;
- track/сleanup: best-effort очистка не ломает сценарий;
- save_state/load_state/clear_payload: WIP-снимок переживает «рестарт»
  (данные читаются из новой сессии, т.е. не из памяти процесса).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import asyncio
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

try:
    from telegram.error import BadRequest, NetworkError

    from tests.integration_harness import (
        FIXTURE_DATE,
        HARNESS_IMPORT_ERROR,
        FakeBot,
        call_text,
    )
    from utils import screen

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ScreenRenderTests(unittest.TestCase):
    """Отрисовка экрана: создаёт, правит, восстанавливает."""

    def setUp(self) -> None:
        self.bot = FakeBot()
        self.user_data: dict = {}
        # render_screen нужны bot и user_data — остальное контексту не нужно
        self.context: Any = SimpleNamespace(bot=self.bot)
        self.chat_id = 777

    def render(self, text: str, markup=None) -> None:
        asyncio.run(
            screen.render_screen(
                self.context, self.user_data, self.chat_id, text, markup
            )
        )

    def test_first_render_sends_screen_and_remembers_it(self) -> None:
        self.render("Главное меню")

        sends = [c for c in self.bot.calls if c["type"] == "message"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(call_text(sends[0]), "Главное меню")
        self.assertTrue(sends[0]["link_preview_options"].is_disabled)
        ref = screen.screen_ref(self.user_data)
        self.assertIsNotNone(ref)
        assert ref is not None
        self.assertEqual(ref["chat_id"], self.chat_id)
        # FakeBot выдаёт message_id, начиная с 101
        self.assertEqual(ref["message_id"], 101)

    def test_second_render_edits_existing_screen(self) -> None:
        self.render("Главное меню")
        self.render("Шаг 1/9 — Заголовок")

        kinds = [c["type"] for c in self.bot.calls]
        self.assertEqual(kinds, ["message", "edit_message"])
        edit = self.bot.calls[1]
        self.assertEqual(call_text(edit), "Шаг 1/9 — Заголовок")
        self.assertTrue(edit["link_preview_options"].is_disabled)

    def test_deleted_screen_creates_new_without_error(self) -> None:
        self.render("Главное меню")
        self.bot.errors_by_type["edit_message"] = BadRequest(
            "Message to edit not found"
        )
        self.render("Шаг 1/9 — Заголовок")

        kinds = [c["type"] for c in self.bot.calls]
        # edit брошен → второй send с новым message_id
        self.assertEqual(kinds, ["message", "message"])
        ref = screen.screen_ref(self.user_data)
        assert ref is not None
        self.assertEqual(ref["message_id"], 102)

    def test_message_not_modified_is_success(self) -> None:
        self.render("Главное меню")
        self.bot.errors_by_type["edit_message"] = BadRequest(
            "Message is not modified: specified new message content and reply "
            "markup are exactly the same"
        )
        self.render("Главное меню")

        kinds = [c["type"] for c in self.bot.calls]
        # повторное нажатие кнопки не порождает нового сообщения
        self.assertEqual(kinds, ["message"])

    def test_network_error_on_edit_falls_back_to_new_screen(self) -> None:
        self.render("Главное меню")
        self.bot.errors_by_type["edit_message"] = NetworkError("network down")
        self.render("Шаг 1/9 — Заголовок")

        kinds = [c["type"] for c in self.bot.calls]
        self.assertEqual(kinds, ["message", "message"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TempMessagesTests(unittest.TestCase):
    """Трекинг и best-effort очистка временных сообщений."""

    def setUp(self) -> None:
        self.bot = FakeBot()
        self.user_data: dict = {}
        self.context: Any = SimpleNamespace(bot=self.bot)

    def _message(self, message_id: int):
        from telegram import Chat, Message, User

        message = Message(
            message_id=message_id,
            date=FIXTURE_DATE,
            chat=Chat(id=777, type="private"),
            from_user=User(id=1, first_name="u", is_bot=False),
            text="ввод",
        )
        message.set_bot(self.bot)  # type: ignore[arg-type]
        return message

    def test_track_temp_deduplicates(self) -> None:
        screen.track_temp(self.user_data, self._message(5))
        screen.track_temp(self.user_data, self._message(5))
        screen.track_temp(self.user_data, self._message(6))
        self.assertEqual(self.user_data["temp_messages"], [5, 6])

    def test_track_temp_ignores_none(self) -> None:
        screen.track_temp(self.user_data, None)
        # ключ не создаётся вовсе — удалять нечего
        self.assertNotIn("temp_messages", self.user_data)

    def test_cleanup_temps_deletes_and_clears(self) -> None:
        screen.track_temp(self.user_data, self._message(5))
        screen.track_temp(self.user_data, self._message(6))

        asyncio.run(screen.cleanup_temps(self.context, 777, self.user_data))

        deleted = [
            c["args"][1] for c in self.bot.calls if c["type"] == "delete_message"
        ]
        self.assertEqual(deleted, [5, 6])
        # список очищен — повторная очистка ничего не удалит
        self.assertNotIn("temp_messages", self.user_data)

    def test_cleanup_survives_delete_failure(self) -> None:
        screen.track_temp(self.user_data, self._message(5))
        screen.track_temp(self.user_data, self._message(6))
        # первое удаление падает (например, сообщение уже удалено)
        self.bot.errors_by_type["delete_message"] = BadRequest(
            "message to delete not found"
        )

        asyncio.run(screen.cleanup_temps(self.context, 777, self.user_data))

        # сценарий не прерван, второе сообщение тоже попытка удалить;
        # список очищен — очистка идемпотентна
        self.assertNotIn("temp_messages", self.user_data)

    def test_track_preview_replaces_previous(self) -> None:
        screen.track_preview(self.user_data, [1, 2])
        screen.track_preview(self.user_data, [3])
        self.assertEqual(self.user_data["preview_messages"], [3])

    def test_cleanup_preview_only_previews(self) -> None:
        screen.track_temp(self.user_data, self._message(5))
        screen.track_preview(self.user_data, [9])

        asyncio.run(screen.cleanup_preview(self.context, 777, self.user_data))

        deleted = [
            c["args"][1] for c in self.bot.calls if c["type"] == "delete_message"
        ]
        self.assertEqual(deleted, [9])
        self.assertEqual(self.user_data["temp_messages"], [5])

    def test_replace_preview_deletes_old_and_tracks_new(self) -> None:
        screen.track_preview(self.user_data, [1, 2])
        screen.track_temp(self.user_data, self._message(7))

        asyncio.run(screen.replace_preview(self.context, 777, self.user_data, [3]))

        deleted = [
            c["args"][1] for c in self.bot.calls if c["type"] == "delete_message"
        ]
        self.assertEqual(deleted, [1, 2], "старое превью удалено, новое цело")
        self.assertEqual(self.user_data["preview_messages"], [3])
        self.assertEqual(self.user_data["temp_messages"], [7], "ввод не тронут")

    def test_replace_preview_first_time_keeps_new(self) -> None:
        # превью ещё не было — ничего не удаляется, новое запоминается
        asyncio.run(screen.replace_preview(self.context, 777, self.user_data, [4]))

        self.assertEqual(self.user_data["preview_messages"], [4])
        self.assertFalse([c for c in self.bot.calls if c["type"] == "delete_message"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DropUserInputTests(unittest.TestCase):
    """drop_user_input: удаляется только ввод ПОСЛЕ подтверждённого сохранения."""

    def setUp(self) -> None:
        self.bot = FakeBot()
        self.context: Any = SimpleNamespace(bot=self.bot)

    @staticmethod
    def _update(message, *, callback_query=None):
        return SimpleNamespace(message=message, callback_query=callback_query)

    @staticmethod
    def _user_message(message_id: int = 41, *, is_bot: bool = False):
        return SimpleNamespace(
            message_id=message_id,
            from_user=SimpleNamespace(id=7, is_bot=is_bot),
            chat=SimpleNamespace(id=777),
        )

    def _deleted(self) -> list[int]:
        return [c["args"][1] for c in self.bot.calls if c["type"] == "delete_message"]

    def test_user_text_message_is_deleted(self) -> None:
        update = self._update(self._user_message(41))

        asyncio.run(screen.drop_user_input(update, self.context))

        self.assertEqual(self._deleted(), [41])

    def test_callback_context_is_not_touched(self) -> None:
        # сообщение в callback-контексте — сообщение бота (экран/пост):
        # удалять его нельзя
        update = self._update(self._user_message(42), callback_query=object())

        asyncio.run(screen.drop_user_input(update, self.context))

        self.assertEqual(self._deleted(), [])

    def test_bot_message_is_not_deleted(self) -> None:
        update = self._update(self._user_message(43, is_bot=True))

        asyncio.run(screen.drop_user_input(update, self.context))

        self.assertEqual(self._deleted(), [])

    def test_missing_message_and_chat_are_safe(self) -> None:
        # заглушки/неполные объекты не должны ронять очистку
        asyncio.run(screen.drop_user_input(SimpleNamespace(), self.context))
        asyncio.run(
            screen.drop_user_input(
                self._update(SimpleNamespace(message_id=44)), self.context
            )
        )
        self.assertEqual(self._deleted(), [])

    def test_delete_failure_does_not_raise(self) -> None:
        self.bot.errors_by_type["delete_message"] = BadRequest(
            "message to delete not found"
        )
        update = self._update(self._user_message(45))

        asyncio.run(screen.drop_user_input(update, self.context))  # не падает

        self.assertEqual(self.bot.calls, [])  # вызов был, но исключение проглочено


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class UserStatePersistenceTests(unittest.TestCase):
    """WIP-снимок в user_states: сохранение, восстановление, очистка."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from base import Base

        self._tmp = tempfile.TemporaryDirectory(prefix="poster_screen_")
        db_path = Path(self._tmp.name) / "screen.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self._patcher = patch.object(screen, "SessionLocal", self.factory)
        self._patcher.start()
        self.user_data: dict = {}

    def tearDown(self) -> None:
        self._patcher.stop()
        self.engine.dispose()
        self._tmp.cleanup()

    def test_save_load_roundtrip(self) -> None:
        screen.remember_screen(self.user_data, 777, 42)
        payload = {"title": "Тест", "current_step": 2, "formats": {}}
        screen.save_state(1, self.user_data, 777, payload)

        ref, loaded = screen.load_state(1)
        # чтение «из БД», а не из памяти: значения совпадают
        self.assertEqual(ref, {"chat_id": 777, "message_id": 42})
        self.assertEqual(loaded, payload)

    def test_empty_state_returns_none(self) -> None:
        ref, payload = screen.load_state(123)
        self.assertIsNone(ref)
        self.assertIsNone(payload)

    def test_clear_payload_keeps_screen_ref(self) -> None:
        screen.remember_screen(self.user_data, 777, 42)
        screen.save_state(1, self.user_data, 777, {"title": "Тест"})
        screen.clear_payload(1)

        ref, payload = screen.load_state(1)
        self.assertEqual(ref, {"chat_id": 777, "message_id": 42})
        self.assertIsNone(payload)

    def test_hydrate_screen_ref_restores_to_user_data(self) -> None:
        screen.remember_screen(self.user_data, 777, 42)
        screen.save_state(1, self.user_data, 777, None)

        fresh_data: dict = {}
        ref = screen.hydrate_screen_ref(fresh_data, 1)
        self.assertEqual(ref, {"chat_id": 777, "message_id": 42})
        self.assertEqual(fresh_data["screen"], ref)

    def test_corrupt_payload_is_none(self) -> None:
        session = self.factory()
        try:
            from models import UserState

            session.add(UserState(user_id=1, payload="{не json"))
            session.commit()
        finally:
            session.close()
        ref, payload = screen.load_state(1)
        self.assertIsNone(payload)


if __name__ == "__main__":
    unittest.main()
