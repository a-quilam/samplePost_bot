# tests/test_poster2_delivery_tester.py
"""
Poster 2.0, S4 — независимая проверка доставки/финализации (poster-tester).

Сценарии написаны заново, независимо от `tests/test_poster2_delivery.py` и
`tests/test_poster2_limits.py`: настоящее приложение (`build_application`),
FakeBot и временная SQLite. Реальная `post_bot.db`, `.env` и сеть не
используются.

Покрытие:

- альбом из 3 фото: одна медиагруппа, подпись только у последнего сегмента,
  автофинал, `published_at`, экран ниже блока, ввод удалён;
- рестарт посреди альбома: фото сохранены и не дублируются, следующий снимок
  завершает анонс одной медиагруппой;
- `RetryAfter` в середине медиагруппы: после паузы досылается только
  недостающее, прогресс закрыт, итог `sent`;
- два анонса подряд: финал второго не трогает доставленный первый;
- пустой анонс без фото → `.`; пустой анонс с фото → фото без подписи-точки.

`test_empty_announcement_with_photo_is_delivered_as_media` — регрессия
дефекта S4-1 (см. отчёт poster-tester): при пустом видимом тексте фотография
не отправлялась вовсе, а «готовым постом» объявлялось прежнее текстовое
сообщение «.». Дефект исправлен: фото доставляется без подписи-точки, прежний
текстовый блок удаляется после успеха.
"""

from __future__ import annotations

import asyncio
import functools
import unittest

from telegram.error import RetryAfter

try:
    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from models import PlaceAdmin, utcnow
    from place_store import create_place
    from template_store import create_personal_template
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        FakeBot,
        Harness,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


DEFAULT_TG = 7


# —— вспомогательные функции сценариев -------------------------------------


def _seed(h, *, body: str = "[title]", brick: str = "title") -> int:
    """Личный кирпичик + личный шаблон; возвращает id шаблона."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_TG, username="tester-s4")
        create_personal_brick(
            session,
            actor_telegram_user_id=DEFAULT_TG,
            code=brick,
            title="Заголовок",
            value_type="short_text",
            rules={},
        )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_TG,
            name="Тестовый шаблон",
            header="",
            body=body,
            footer="",
        )
        session.commit()
        return template.template_id


def _seed_place(h) -> int:
    """Площадка с админом — для ветки входа «по площадке»."""
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_TG, username="tester-s4")
        place = create_place(
            session,
            "Дом культуры",
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        return place.id


def _draft(h):
    """Самый свежий черновик автора."""
    with h.session_factory() as session:
        return list_announcement_drafts(session, actor_telegram_user_id=DEFAULT_TG)[0]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=DEFAULT_TG, draft_id=draft_id
        )


def _button(h, prefix: str) -> str:
    for _label, data in h.screen_buttons():
        if data and data.startswith(prefix):
            return data
    raise AssertionError(f"нет кнопки {prefix!r}: {h.screen_buttons()}")


def _maybe_button(h, prefix: str) -> str | None:
    for _label, data in h.screen_buttons():
        if data and data.startswith(prefix):
            return data
    return None


async def _to_photo_step(h, template_id: int, *, value: str | None = None) -> None:
    """
    Проходит вопросы до шага фото.

    `value` — ответ на каждый кирпичик; None — вопросы пропускаются
    (пустой видимый текст анонса).
    """
    await h.text("/start")
    await h.callback(f"tp_pick:{template_id}")
    for _ in range(12):
        if "Фотографии анонса" in h.screen_text():
            return
        skip = _maybe_button(h, "aw_qskip")
        if skip is not None and value is None:
            await h.callback(skip)
            continue
        await h.text(value or "Значение")
    raise AssertionError(f"шаг фото не достигнут: {h.screen_text()}")


def _media_groups(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "media_group"]


def _photos_sent(h) -> list[dict]:
    return [call for call in h.calls if call["type"] == "photo"]


def _group_media(call: dict) -> list[str | None]:
    return [getattr(item, "media", None) for item in call["media"]]


def _group_captions(call: dict) -> list[str | None]:
    return [getattr(item, "caption", None) for item in call["media"]]


class RetryOnceFakeBot(FakeBot):
    """
    FakeBot, который ОДИН раз отвечает `RetryAfter` на выбранный метод.

    Воспроизводит flood control Telegram посреди отправки: первый вызов
    падает, продукт обязан подождать и повторить только недостающее.
    """

    def __init__(self, kind: str = "media_group", retry_after: int = 0) -> None:
        super().__init__()
        self.kind = kind
        self.retry_after = retry_after
        self.failures = 0

    async def _maybe_retry_after(self, kind: str) -> None:
        if kind == self.kind and self.failures > 0:
            self.failures -= 1
            raise RetryAfter(retry_after=self.retry_after)

    async def send_media_group(self, *args, **kwargs):
        await self._maybe_retry_after("media_group")
        return await super().send_media_group(*args, **kwargs)

    async def send_photo(self, *args, **kwargs):
        await self._maybe_retry_after("photo")
        return await super().send_photo(*args, **kwargs)


def retry_scenario(kind: str):
    """Декоратор async-теста с FakeBot, падающим `RetryAfter` один раз."""

    def decorator(method):
        @functools.wraps(method)
        def wrapper(self) -> None:
            async def run() -> None:
                harness = Harness()
                bot = RetryOnceFakeBot(kind)
                harness.bot = bot
                await harness.start()
                try:
                    await method(self, harness, bot)
                finally:
                    await harness.close()

            asyncio.run(run())

        return wrapper

    return decorator


def scenario_test(method):
    """Обычный сценарий на чистом FakeBot."""

    @functools.wraps(method)
    def wrapper(self) -> None:
        async def run() -> None:
            harness = Harness()
            await harness.start()
            try:
                await method(self, harness)
            finally:
                await harness.close()

        asyncio.run(run())

    return wrapper


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AlbumAndRestartTests(unittest.TestCase):
    """Альбом целиком, финал одной медиагруппой, рестарт без дублей."""

    @scenario_test
    async def test_album_of_three_finalizes_once(self, h) -> None:
        template_id = _seed(h)
        await _to_photo_step(h, template_id, value="Значение")
        draft = _draft(h)
        self.assertIsNone(_details(h, draft.draft_id).publication_status)

        uploads = []
        for index in range(3):
            update = await h.photo(f"alb-{index}", media_group_id="mg-three")
            uploads.append(update.message.message_id)
        self.assertIsNone(
            _details(h, draft.draft_id).publication_status,
            "до конца партии анонс не должен быть готов",
        )
        await asyncio.sleep(0.5)

        sent = _details(h, draft.draft_id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("alb-0", "alb-1", "alb-2"))
        self.assertIsNotNone(sent.published_at)
        self.assertTrue(sent.delivered_message_ids)

        groups = _media_groups(h)
        self.assertEqual(len(groups), 1, "ровно одна медиагруппа на весь альбом")
        self.assertEqual(_group_media(groups[0]), ["alb-0", "alb-1", "alb-2"])
        self.assertEqual(
            _group_captions(groups[0]),
            [None, None, "Значение"],
            "текст — подписью только последнего сегмента",
        )
        self.assertEqual(_photos_sent(h), [], "хвоста из одного фото здесь нет")

        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)
        self.assertGreater(
            screen_id,
            max(sent.delivered_message_ids),
            "служебный экран должен идти ниже анонса",
        )
        for message_id in uploads:
            self.assertIn(message_id, h.deleted_ids())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_restart_mid_album_keeps_photos_and_next_photo_finalizes(
        self, h
    ) -> None:
        from handlers import wizard_photos

        template_id = _seed(h)
        await _to_photo_step(h, template_id, value="Значение")
        draft = _draft(h)

        await h.photo("mid-0", media_group_id="mg-mid")
        await h.photo("mid-1", media_group_id="mg-mid")
        # Рестарт до срабатывания паузы: отложенное закрытие партии теряется.
        pending = h.user_data().get(wizard_photos.PENDING_TASK_KEY)
        if pending is not None:
            pending.cancel()
        before = _details(h, draft.draft_id)
        self.assertEqual(before.photos, ("mid-0", "mid-1"))
        self.assertIsNone(before.publication_status)

        await h.restart()
        await asyncio.sleep(0.3)
        self.assertEqual(
            _details(h, draft.draft_id).photos,
            ("mid-0", "mid-1"),
            "фото пережили рестарт",
        )
        self.assertEqual(_media_groups(h), [], "рестарт не отправил дубль")

        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")
        photos_button = _maybe_button(h, "aw_photos")
        if photos_button is not None:
            await h.callback(photos_button)
        await h.photo("mid-2")
        await asyncio.sleep(0.4)

        sent = _details(h, draft.draft_id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("mid-0", "mid-1", "mid-2"))
        groups = _media_groups(h)
        self.assertEqual(len(groups), 1, "прежние фото не отправлены повторно")
        self.assertEqual(_group_media(groups[0]), ["mid-0", "mid-1", "mid-2"])
        self.assertIn("Значение", _group_captions(groups[0])[-1] or "")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RetryAfterMidGroupTests(unittest.TestCase):
    """Flood control в середине медиагруппы."""

    @retry_scenario("media_group")
    async def test_retry_after_mid_media_group(self, h, bot) -> None:
        template_id = _seed(h)
        await _to_photo_step(h, template_id, value="Значение")
        draft = _draft(h)
        live_ids = set(_details(h, draft.draft_id).live_message_ids)
        self.assertTrue(live_ids)

        # Первый send_media_group (живой блок) проходит, следующий — падает
        # один раз: Telegram просит подождать посреди доставки.
        bot.failures = 0
        await h.photo("ra-0", media_group_id="mg-ra")
        bot.failures = 1
        await h.photo("ra-1", media_group_id="mg-ra")
        await asyncio.sleep(0.5)

        self.assertEqual(bot.failures, 0, "RetryAfter действительно произошёл")
        sent = _details(h, draft.draft_id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("ra-0", "ra-1"))
        self.assertEqual(
            sent.publication_progress.get("step"),
            sent.publication_progress.get("total"),
            "прогресс закрыт полностью",
        )
        self.assertEqual(len(sent.delivered_message_ids), 1)
        self.assertEqual(
            int(sent.publication_progress["ids"][0]),
            sent.delivered_message_ids[0],
            "в прогрессе именно подтверждённый id",
        )
        # Падение НЕ записало прогресс: в чате ровно один успешный вызов
        # группы, дублей фото нет.
        groups = _media_groups(h)
        self.assertEqual(len(groups), 1, "после паузы отправлена ровно одна группа")
        self.assertEqual(_group_media(groups[0]), ["ra-0", "ra-1"])
        self.assertEqual(_group_captions(groups[0]), [None, "Значение"])
        self.assertEqual(
            len([c for c in h.post_records() if c["type"] in ("photo", "media_group")]),
            1,
            "повторной отправки фото нет",
        )
        self.assertTrue(
            live_ids <= set(h.deleted_ids()),
            "прошлый блок заменён только после успеха новой версии",
        )
        self.assertIn("Готовый анонс отправлен", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SecondAnnouncementTests(unittest.TestCase):
    """Финал второго анонса не трогает доставленный первый."""

    @scenario_test
    async def test_second_announcement_final_keeps_first(self, h) -> None:
        template_id = _seed(h)
        await _to_photo_step(h, template_id, value="Первый анонс")
        await h.photo("first-photo")
        await asyncio.sleep(0.3)

        first = _draft(h)
        first_state = _details(h, first.draft_id)
        self.assertEqual(first_state.publication_status, "sent")
        first_ids = set(first_state.delivered_message_ids)
        self.assertTrue(first_ids)
        self.assertEqual(len(h.post_records()), 1)

        await _to_photo_step(h, template_id, value="Второй анонс")
        await h.photo("second-0", media_group_id="mg-second")
        await h.photo("second-1", media_group_id="mg-second")
        await asyncio.sleep(0.5)

        second = _draft(h)
        self.assertNotEqual(second.draft_id, first.draft_id)
        second_state = _details(h, second.draft_id)
        self.assertEqual(second_state.publication_status, "sent")
        self.assertEqual(second_state.publication_photos, ("second-0", "second-1"))
        second_ids = set(second_state.delivered_message_ids)
        self.assertTrue(second_ids)
        self.assertFalse(
            second_ids & first_ids, "id версий разных анонсов не смешиваются"
        )

        first_after = _details(h, first.draft_id)
        self.assertEqual(first_after.publication_status, "sent")
        self.assertEqual(first_after.publication_photos, ("first-photo",))
        self.assertEqual(set(first_after.delivered_message_ids), first_ids)
        self.assertFalse(
            first_ids & set(h.deleted_ids()),
            "доставленный первый анонс нельзя удалять финалом второго",
        )
        self.assertEqual(len(h.post_records()), 2)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EmptyAnnouncementTests(unittest.TestCase):
    """Пустой видимый текст: `.` без фото, фото без подписи-точки."""

    @scenario_test
    async def test_empty_without_photos_sends_dot(self, h) -> None:
        template_id = _seed(h)
        await _to_photo_step(h, template_id, value=None)
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertEqual(h.posts(), [], "до выбора фото поста нет")

        await h.callback(_button(h, "aw_photo_none"))
        sent = _details(h, _draft(h).draft_id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_text, ".")
        self.assertEqual(h.posts(), ["."])
        self.assertEqual(sent.publication_photos, ())

    @scenario_test
    async def test_empty_announcement_with_photo_is_delivered_as_media(self, h) -> None:
        """Дефект S4-1: фото при пустом тексте уходит БЕЗ подписи-точки.

        В чате появляется сообщение с фото «empty-photo» без подписи,
        `publication_photos == ("empty-photo",)`, `delivered_message_ids`
        указывают на это фото, `publication_text` не «.». Раньше фотография
        не отправлялась вовсе, а готовым постом объявлялся прежний «.».
        """
        template_id = _seed(h)
        await _to_photo_step(h, template_id, value=None)
        draft = _draft(h)
        live_ids = set(_details(h, draft.draft_id).live_message_ids)
        self.assertTrue(live_ids)

        await h.photo("empty-photo")
        await asyncio.sleep(0.3)

        sent = _details(h, draft.draft_id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("empty-photo",))

        media = _photos_sent(h) + _media_groups(h)
        self.assertTrue(
            media,
            "фото не отправлено: в чате только прежнее текстовое «.»",
        )
        first = media[0]
        if first["type"] == "photo":
            self.assertEqual(first["photo"], "empty-photo")
            self.assertIsNone(
                first.get("caption"), "подписи-точки у фото быть не должно"
            )
        else:
            self.assertEqual(_group_media(first), ["empty-photo"])
            self.assertEqual(_group_captions(first), [None])

        captions: list[str] = []
        for call in _photos_sent(h):
            if call.get("caption"):
                captions.append(call["caption"])
        for call in _media_groups(h):
            captions.extend(item for item in _group_captions(call) if item)
        self.assertEqual(captions, [], "точка не должна уходить подписью")

        self.assertNotEqual(sent.publication_text, ".")
        self.assertTrue(
            set(sent.delivered_message_ids)
            and not set(sent.delivered_message_ids) & live_ids,
            "готовым постом должно стать фото, а не прежний текстовый блок",
        )
