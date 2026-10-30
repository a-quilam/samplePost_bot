# tests/test_poster2_delivery.py
"""
Poster 2.0 S4 §5.3–§5.6: единая доставка и финализация анонса.

Первая часть — `delivery.py` на минимальной заглушке бота: одна подпись у
последнего фотосегмента, `.` без фото и пустая подпись с фото, прогресс
только после подтверждённого ответа Telegram, досылка недостающего,
`RetryAfter` (один повтор), `Forbidden`, падение между успехом API и
записью прогресса.

Вторая часть — сквозные сценарии через настоящее приложение, FakeBot и
временную SQLite: живой медиаблок становится готовым постом без второй
отправки, рестарт не дублирует, прошлая версия удаляется только после
успеха, проблемный экран лимита сохраняет данные и возвращает 4096 по
«Без фотографий», неудачное удаление не ломает поток.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Any

from telegram.error import BadRequest, Forbidden, RetryAfter

from delivery import (
    EMPTY_CAPTION_TEXT,
    delivery_fingerprint,
    is_forbidden,
    is_not_found,
    is_not_modified,
    is_retry_after,
    send_announcement_post,
)

try:
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import CONVERSATION_NAME
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


# —— минимальная заглушка Telegram для unit-тестов доставки ----------------
@dataclass
class FakeMessage:
    message_id: int


@dataclass
class StubBot:
    """
    Bot только с методами доставки, которые зовёт `send_announcement_post`.

    `errors` — очередь исключений по типу вызова: пустая очередь = успех.
    Каждый успешный вызов записывается в `calls`, как у настоящего Telegram
    (подтверждённый ответ приходит ровно на успех).
    """

    calls: list[dict[str, Any]] = field(default_factory=list)
    errors: dict[str, list[BaseException]] = field(default_factory=dict)
    _next_id: int = 500

    def _record(self, kind: str, kwargs: dict[str, Any]) -> FakeMessage:
        queue = self.errors.get(kind)
        if queue:
            raise queue.pop(0)
        self._next_id += 1
        self.calls.append({"type": kind, **kwargs})
        return FakeMessage(self._next_id)

    async def send_message(self, **kwargs: Any) -> FakeMessage:
        return self._record("message", kwargs)

    async def send_photo(self, **kwargs: Any) -> FakeMessage:
        return self._record("photo", kwargs)

    async def send_media_group(self, **kwargs: Any) -> list[FakeMessage]:
        message = self._record("media_group", kwargs)
        return [message]


def _captions(call: dict[str, Any]) -> list[str | None]:
    return [getattr(item, "caption", None) for item in call["media"]]


class DeliveryUnitsTests(unittest.TestCase):
    """Поведение `send_announcement_post` без БД и без сети."""

    def test_text_without_photos_is_one_message(self) -> None:
        bot = StubBot()
        progress: dict = {}
        ids = _run(send_announcement_post(bot, 777, "Анонс", [], progress=progress))
        self.assertEqual(len(ids), 1)
        self.assertEqual([c["type"] for c in bot.calls], ["message"])
        self.assertEqual(bot.calls[0]["text"], "Анонс")
        self.assertEqual(progress["step"], 1)
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["ids"], ids)

    def test_single_photo_gets_the_caption(self) -> None:
        bot = StubBot()
        _run(send_announcement_post(bot, 777, "Анонс", ["p1"]))
        self.assertEqual([c["type"] for c in bot.calls], ["photo"])
        self.assertEqual(bot.calls[0]["caption"], "Анонс")

    def test_only_the_last_photo_segment_has_the_caption(self) -> None:
        bot = StubBot()
        photos = [f"p{index}" for index in range(10)]
        _run(send_announcement_post(bot, 777, "Анонс", photos))
        (group,) = bot.calls
        self.assertEqual(group["type"], "media_group")
        self.assertEqual(
            [item.media for item in group["media"]],
            photos,
        )
        self.assertEqual(
            _captions(group),
            [None] * 9 + ["Анонс"],
            "подпись разрешена только у последнего фотосегмента",
        )

    def test_media_group_keeps_message_id_of_every_photo(self) -> None:
        """Настоящий Bot API отвечает по сообщению на каждое фото группы."""

        class GroupBot(StubBot):
            async def send_media_group(self, **kwargs: Any) -> list[FakeMessage]:
                self.calls.append({"type": "media_group", **kwargs})
                count = len(kwargs.get("media") or ())
                self._next_id += count
                return [
                    FakeMessage(self._next_id - count + index + 1)
                    for index in range(count)
                ]

        bot = GroupBot()
        progress: dict = {}
        photos = [f"p{index}" for index in range(3)]
        ids = _run(send_announcement_post(bot, 777, "Анонс", photos, progress=progress))
        self.assertEqual(len(ids), 3, "id каждого сообщения группы, а не только первое")
        self.assertEqual(progress["ids"], ids)
        self.assertEqual(progress["step"], 1, "одна операция — одна группа")

        # Продолжение не переотправляет подтверждённую группу: все три id
        # уже принадлежат доставленной версии.
        bot.calls.clear()
        again = _run(
            send_announcement_post(bot, 777, "Анонс", photos, progress=progress)
        )
        self.assertEqual(again, ids)
        self.assertEqual(bot.calls, [])

    def test_tail_of_one_photo_goes_as_send_photo_with_caption(self) -> None:
        bot = StubBot()
        photos = [f"p{index}" for index in range(11)]
        _run(send_announcement_post(bot, 777, "Анонс", photos))
        self.assertEqual(
            [c["type"] for c in bot.calls],
            ["media_group", "photo"],
            "sendMediaGroup не принимает группу из одного элемента",
        )
        self.assertEqual(_captions(bot.calls[0]), [None] * 10)
        self.assertEqual(bot.calls[1]["caption"], "Анонс")
        self.assertEqual(bot.calls[1]["photo"], "p10")

    def test_empty_text_with_photos_has_no_caption(self) -> None:
        bot = StubBot()
        _run(send_announcement_post(bot, 777, "", ["p1"]))
        self.assertIsNone(bot.calls[0]["caption"])
        self.assertFalse(bot.calls[0].get("parse_mode"))

    def test_placeholder_dot_is_sent_only_without_photos(self) -> None:
        bot = StubBot()
        _run(send_announcement_post(bot, 777, ".", []))
        self.assertEqual(bot.calls[0]["text"], ".")

    def test_progress_is_recorded_only_after_confirmed_reply(self) -> None:
        bot = StubBot(errors={"photo": [Forbidden("Forbidden: bot was blocked")]})
        progress: dict = {}
        with self.assertRaises(Forbidden):
            _run(send_announcement_post(bot, 777, "Анонс", ["p1"], progress=progress))
        self.assertEqual(progress["ids"], [], "падение не считается успехом")
        self.assertEqual(progress["step"], 0)
        self.assertEqual(bot.calls, [])

    def test_forbidden_is_not_retried(self) -> None:
        bot = StubBot(errors={"message": [Forbidden("Forbidden: bot was blocked")]})
        with self.assertRaises(Forbidden):
            _run(send_announcement_post(bot, 777, "Анонс", []))
        self.assertEqual(bot.calls, [])

    def test_retry_after_waits_once_and_succeeds(self) -> None:
        bot = StubBot(errors={"photo": [RetryAfter(retry_after=0)]})
        ids = _run(send_announcement_post(bot, 777, "Анонс", ["p1"]))
        self.assertEqual(len(ids), 1, "RetryAfter повторяется ровно один раз")
        self.assertEqual([c["type"] for c in bot.calls], ["photo"])

    def test_retry_after_twice_gives_up_honestly(self) -> None:
        bot = StubBot(
            errors={"photo": [RetryAfter(retry_after=0), RetryAfter(retry_after=0)]}
        )
        with self.assertRaises(RetryAfter):
            _run(send_announcement_post(bot, 777, "Анонс", ["p1"]))
        self.assertEqual(bot.calls, [])

    def test_partial_progress_resends_only_the_missing_part(self) -> None:
        bot = StubBot()
        photos = [f"p{index}" for index in range(11)]
        progress = {
            "fingerprint": delivery_fingerprint("Анонс", photos),
            "step": 1,
            "ids": [101],
            "total": 2,
        }
        ids = _run(
            send_announcement_post(
                bot, 777, "Анонс", photos, progress=progress, on_progress=None
            )
        )
        self.assertEqual(ids, [101, 501], "подтверждённый шаг не повторяется")
        self.assertEqual([c["type"] for c in bot.calls], ["photo"])
        self.assertEqual(bot.calls[0]["photo"], "p10")

    def test_progress_write_failure_keeps_confirmed_ids(self) -> None:
        """Падение между успехом API и записью в БД не даёт дубля."""
        bot = StubBot()
        progress: dict = {}
        attempts: list[int] = []

        def on_progress(snapshot: dict) -> None:
            attempts.append(len(snapshot["ids"]))
            if len(attempts) == 2:
                raise RuntimeError("БД недоступна")

        photos = [f"p{index}" for index in range(11)]
        with self.assertRaises(RuntimeError):
            _run(
                send_announcement_post(
                    bot,
                    777,
                    "Анонс",
                    photos,
                    progress=progress,
                    on_progress=on_progress,
                )
            )
        # Первая группа подтверждена и уже в памяти прогресса, несмотря на сбой
        # записи; текст ушёл подписью хвоста — повтору остаётся только он.
        self.assertEqual(progress["step"], 1)
        self.assertEqual(len(progress["ids"]), 1)

        bot.calls.clear()
        ids = _run(send_announcement_post(bot, 777, "Анонс", photos, progress=progress))
        self.assertEqual(len(ids), 2)
        self.assertEqual([c["type"] for c in bot.calls], ["photo"])

    def test_changed_content_restarts_delivery(self) -> None:
        bot = StubBot()
        progress = {"fingerprint": "старый", "step": 1, "ids": [101], "total": 2}
        ids = _run(send_announcement_post(bot, 777, "Анонс", ["p1"], progress=progress))
        self.assertEqual(ids, [501], "новый payload не наследует старые id")
        self.assertEqual(len(bot.calls), 1)

    def test_fingerprint_normalizes_empty_text(self) -> None:
        # Пустая подпись и служебный маркер — одно и то же содержимое:
        # живой блок и замороженный payload не должны расходиться.
        self.assertEqual(
            delivery_fingerprint("", ["p1"]),
            delivery_fingerprint(EMPTY_CAPTION_TEXT, ["p1"]),
        )
        # Литеральная `.` — обычный видимый текст: правка `.` → пусто
        # обязана доставить новую версию, а не считаться неизменённой.
        self.assertNotEqual(
            delivery_fingerprint("", ["p1"]),
            delivery_fingerprint(".", ["p1"]),
        )
        self.assertNotEqual(
            delivery_fingerprint("Анонс", ["p1"]),
            delivery_fingerprint("Анонс", ["p2"]),
        )

    def test_error_predicates(self) -> None:
        self.assertTrue(is_retry_after(RetryAfter(retry_after=3)))
        self.assertTrue(is_forbidden(Forbidden("Forbidden: bot was blocked")))
        self.assertTrue(is_not_modified(BadRequest("Message is not modified")))
        self.assertTrue(is_not_found(BadRequest("Message to edit not found")))
        self.assertFalse(is_not_found(BadRequest("Message is not modified")))


def _run(awaitable):
    """Запускает одну корутину доставки в отдельном цикле (unit-тесты)."""
    import asyncio

    return asyncio.run(awaitable)


# —— сквозные сценарии -----------------------------------------------------
def _stored(h, draft_id: int):
    from tests.integration_harness import DEFAULT_USER_ID

    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementDeliveryScenarioTests(unittest.TestCase):
    """Живой блок, финализация по fingerprint, сбои и проблемный экран."""

    async def _to_photo_step(
        self, h, *, body: str | None = None, value: str | None = "Значение"
    ):
        """
        Доводит мастер до шага фото: анонс ещё НЕ доставлен.

        `value=None` — кирпичик пропускается (пустой видимый текст анонса).
        """
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _latest_draft,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h, body=body)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        if value is None:
            skip = next(
                (
                    data
                    for _label, data in h.screen_buttons()
                    if data and data.startswith("aw_qskip")
                ),
                None,
            )
            if skip is None:
                raise AssertionError(f"Нет кнопки пропуска: {h.screen_buttons()}")
            await h.callback(skip)
        else:
            await h.text(value)
        if "Фотографии анонса" not in h.screen_text():
            raise AssertionError(f"Не шаг фото: {h.screen_text()}")
        return _latest_draft(h)

    @scenario_test
    async def test_live_block_becomes_the_ready_post_without_resending(self, h) -> None:
        from tests.test_stage27c_announcement_ui import _post_types

        await h.text("/start")
        draft = await self._to_photo_step(h)
        live_ids = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(live_ids, "живой блок текстового анонса сохранён")
        self.assertEqual(h.posts(), [], "живой блок не считается готовым постом")

        # Партия фото завершает анонс: живой медиаблок становится готовым
        # постом, второй такой же не отправляется.
        await h.photo("live-photo")
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_total, 1)
        self.assertEqual(sent.publication_photos, ("live-photo",))
        photos = _post_types(h, {"photo"})
        self.assertEqual(len(photos), 1, "ровно одна отправка фото")
        self.assertEqual(photos[0]["photo"], "live-photo")
        self.assertIn("Значение", photos[0]["caption"] or "")
        # Прежний текстовый блок убран только после подтверждённой доставки.
        self.assertTrue(live_ids <= set(h.deleted_ids()))
        # Порядок в чате: фото выше служебного экрана.
        new_ids = set(sent.delivered_message_ids)
        self.assertTrue(new_ids)
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)
        self.assertLess(max(new_ids), screen_id)

    @scenario_test
    async def test_empty_text_with_photo_is_delivered_as_media(self, h) -> None:
        """Дефект S4-1: пустой текст + фото — доставляется фото, а не `.`."""
        from tests.test_stage27c_announcement_ui import _post_types

        await h.text("/start")
        draft = await self._to_photo_step(h, body="[main]", value=None)
        live_ids = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(live_ids, "текстовый блок «.» показан до фото")
        self.assertEqual(h.posts(), [], "до фото готового поста нет")

        await h.photo("empty-photo")
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("empty-photo",))
        self.assertNotEqual(
            sent.publication_text, ".", "заглушка не должна попасть в payload"
        )

        photos = _post_types(h, {"photo", "media_group"})
        self.assertEqual(len(photos), 1, "фото доставлено ровно один раз")
        self.assertEqual(photos[0]["type"], "photo")
        self.assertEqual(photos[0]["photo"], "empty-photo")
        self.assertIsNone(photos[0]["caption"], "подписи-точки у фото нет")
        self.assertFalse(photos[0]["parse_mode"], "пустой parse_mode не нужен")

        # Готовым постом стало фото, а прежний текстовый «.» удалён после успеха.
        self.assertTrue(sent.delivered_message_ids)
        self.assertFalse(
            set(sent.delivered_message_ids) & live_ids,
            "id готового поста — это фото, а не прежний текстовый блок",
        )
        self.assertTrue(live_ids <= set(h.deleted_ids()))

    @scenario_test
    async def test_restart_after_delivery_does_not_duplicate(self, h) -> None:
        from handlers.announcement_wizard import AW_PREVIEW
        from tests.test_stage27c_announcement_ui import _callback

        await h.text("/start")
        draft = await self._to_photo_step(h)
        await h.photo("restart-photo")
        posts_before = len(h.posts())
        self.assertEqual(posts_before, 1)

        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertEqual(len(h.posts()), posts_before, "рестарт не отправил дубль")
        self.assertIn("✅ Анонс готов.", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_forbidden_keeps_data_and_shows_honest_screen(self, h) -> None:
        from tests.test_stage27c_announcement_ui import _callback, _callbacks

        await h.text("/start")
        draft = await self._to_photo_step(h)
        h.bot.errors_by_type["photo"] = Forbidden("Forbidden: bot was blocked")
        await h.photo("blocked-photo")

        failed = _stored(h, draft.id)
        self.assertEqual(failed.publication_status, "failed")
        self.assertEqual(failed.publication_step, 0)
        self.assertEqual(failed.photos, ("blocked-photo",), "данные не потеряны")
        self.assertIn("Forbidden", h.screen_text())
        self.assertIn(
            "Повторить отправку", " ".join(label for label, _data in h.screen_buttons())
        )
        self.assertTrue(_callbacks(h, "aw_pub_resume"))

        h.bot.errors_by_type.pop("photo", None)
        await h.callback(_callback(h, "aw_pub_resume"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("blocked-photo",))

    @scenario_test
    async def test_failed_deletion_does_not_break_delivery(self, h) -> None:
        await h.text("/start")
        draft = await self._to_photo_step(h)
        old_ids = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(old_ids)
        h.bot.errors_by_type["delete_message"] = BadRequest(
            "Bad Request: message can't be deleted"
        )
        await h.photo("kept-photo")

        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertIn("✅ Анонс готов.", h.screen_text())
        # Удаление не удалось — анонс всё равно доставлен, а старый блок
        # остался в чате: это известное ограничение, а не сбой доставки.
        self.assertEqual(sent.publication_photos, ("kept-photo",))
        h.bot.errors_by_type.pop("delete_message", None)

    @scenario_test
    async def test_length_problem_screen_keeps_data_and_input(self, h) -> None:
        from tests.test_stage27c_announcement_ui import _callback

        await h.text("/start")
        # Длинный кирпичик (не литерал шаблона): сокращать нужно именно его.
        long_value = "Длинный текст анонса " * 150
        draft = await self._to_photo_step(h, body="[main]", value=long_value)
        photo_update = await h.photo("over-limit-photo")

        screen = h.screen_text()
        self.assertIn("не помещается", screen)
        self.assertIn("из 1024", screen)
        self.assertIn("Самые длинные кирпичики", screen)
        labels = [label for label, _data in h.screen_buttons()]
        self.assertTrue(any("Изменить другой кирпичик" in label for label in labels))
        self.assertTrue(any("Без фотографий" in label for label in labels))
        # Данные и фото сохранены, ввод не удалён.
        stored = _stored(h, draft.id)
        self.assertEqual(stored.photos, ("over-limit-photo",))
        self.assertIsNone(stored.publication_status)
        self.assertNotIn(photo_update.message.message_id, h.deleted_ids())

        # Сокращение кирпичика применяет ожидавшее фото автоматически.
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Короткий анонс")
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("over-limit-photo",))

    @scenario_test
    async def test_no_photos_from_problem_screen_restores_4096(self, h) -> None:
        from tests.test_stage27c_announcement_ui import _callback, _post_types

        await h.text("/start")
        long_value = "Длинный текст анонса " * 150
        draft = await self._to_photo_step(h, body="[main]", value=long_value)
        await h.photo("dropped-photo")
        self.assertIn("из 1024", h.screen_text())

        await h.callback(_callback(h, "aw_photo_drop"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ())
        self.assertEqual(sent.photos, ())
        self.assertEqual(_post_types(h, {"photo", "media_group"}), [])
        self.assertGreater(len((h.posts()[-1] or "")), 1024)
        self.assertEqual(h.posts()[-1], sent.publication_text)
