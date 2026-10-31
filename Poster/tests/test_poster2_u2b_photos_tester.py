# tests/test_poster2_u2b_photos_tester.py
"""
Poster 2.0 U2b, независимая проверка шага фото (poster-tester).

Сценарии написаны заново, без опоры на `tests/test_poster2_u2b_photos.py`:
настоящее приложение, FakeBot, временная SQLite. Проверяются только
негативные и граничные случаи:

- чужой пользователь не может добавить фото в чужой черновик;
- повторный callback «⏭ Пропустить» не создаёт второй анонс;
- подделанный callback с чужим/несуществующим `draft_id` отвергается;
- устаревшая кнопка шага не меняет состояние;
- повтор одного и того же `file_id` не дублирует снимок и не даёт второй пост.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import asyncio
import unittest

try:
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import User
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        OTHER_TG,
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _details(h, draft_id: int, telegram_user_id: int = DEFAULT_USER_ID):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )


async def _to_photo(h) -> int:
    """Доводит автора до шага фото; возвращает id черновика."""
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Значение")
    if "Фотографии анонса" not in h.screen_text():
        raise AssertionError(f"не шаг фото: {h.screen_text()}")
    return _latest_draft(h).id


def _ensure_other_user(h) -> None:
    """Создаёт второго пользователя: его callback'и/фото не должны пройти."""
    from sqlalchemy import select

    with h.session_factory() as session:
        existing = session.scalar(select(User).where(User.telegram_user_id == OTHER_TG))
        if existing is None:
            session.add(
                User(
                    telegram_user_id=OTHER_TG,
                    username="foreign-u2b",
                    full_name="Чужой",
                )
            )
            session.commit()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ForeignPhotoTests(unittest.TestCase):
    """Чужой пользователь не может пополнить чужой черновик."""

    @scenario_test
    async def test_foreign_photo_is_refused(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        before = _details(h, draft_id)
        posts_before = h.posts()

        _ensure_other_user(h)
        await h.photo("foreign-photo", user_id=OTHER_TG)
        await asyncio.sleep(0.3)

        after = _details(h, draft_id)
        self.assertEqual(after.photos, before.photos, "чужие фото не приняты")
        self.assertEqual(after.revision, before.revision)
        self.assertIsNone(after.publication_status)
        self.assertEqual(h.posts(), posts_before)
        self.assertNotIn("✅ Анонс готов.", h.screen_text())

    @scenario_test
    async def test_foreign_skip_does_not_finish_the_draft(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        skip = _callback(h, "aw_photo_none")

        _ensure_other_user(h)
        await h.callback(skip, user_id=OTHER_TG)

        stored = _details(h, draft_id)
        self.assertIsNone(stored.publication_status, "чужой callback не завершает")
        self.assertEqual(h.posts(), [])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DoubleCallbackTests(unittest.TestCase):
    """Повторные и подделанные callback'и шага фото."""

    @scenario_test
    async def test_double_skip_creates_one_announcement(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        skip = _callback(h, "aw_photo_none")

        await h.callback(skip)
        posts_after_first = h.posts()
        self.assertEqual(len(posts_after_first), 1)
        stored = _details(h, draft_id)
        version_ids = stored.delivered_message_ids

        await h.callback(skip)
        self.assertEqual(h.posts(), posts_after_first, "второго анонса нет")
        after = _details(h, draft_id)
        self.assertEqual(after.delivered_message_ids, version_ids)
        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertIn("✅ Анонс готов.", h.screen_text())

    @scenario_test
    async def test_forged_skip_with_unknown_draft_is_rejected(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        stored = _details(h, draft_id)

        await h.callback(f"aw_photo_none:{draft_id}:{stored.revision}")
        posts_after_first = h.posts()
        skip_of_first = f"aw_photo_none:{draft_id}:{stored.revision}"
        await h.callback(skip_of_first)

        self.assertEqual(h.posts(), posts_after_first)
        await h.callback("aw_photo_none:999999:1")
        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertEqual(len(h.posts()), len(posts_after_first))
        self.assertIsNotNone(_details(h, draft_id).publication_status)

    @scenario_test
    async def test_forged_back_with_wrong_revision_does_nothing(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        stored = _details(h, draft_id)
        posts_before = h.posts()

        await h.callback(f"aw_photo_back:{draft_id}:{stored.revision + 5}")

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertEqual(h.posts(), posts_before, "подделанная кнопка не отправляет")
        after = _details(h, draft_id)
        self.assertEqual(after.revision, stored.revision)
        self.assertIsNone(after.publication_status)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DuplicatePhotoTests(unittest.TestCase):
    """Повтор Update'а с тем же `file_id` не дублирует снимок."""

    @scenario_test
    async def test_same_file_id_twice_gives_one_photo(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)

        await h.photo("dup-1", media_group_id="mg-dup")
        await h.photo("dup-1", media_group_id="mg-dup")
        await asyncio.sleep(0.4)

        stored = _details(h, draft_id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("dup-1",))
        # Один снимок уходит не медиагруппой (Telegram её не принимает), а
        # sendPhoto; важно, что пост ровно один и снимок в нём один.
        posts = h.post_records()
        self.assertEqual(len(posts), 1, "повтор Update'а не создал второй пост")
        self.assertEqual(posts[0]["type"], "photo")
        self.assertEqual(posts[0]["photo"], "dup-1")
        self.assertEqual(len(h.posts()), 1)

    @scenario_test
    async def test_same_file_id_after_delivery_creates_no_new_version(self, h) -> None:
        await h.text("/start")
        draft_id = await _to_photo(h)
        await h.callback(_callback(h, "aw_photo_none"))
        stored = _details(h, draft_id)
        version_ids = stored.delivered_message_ids
        posts_before = h.posts()

        await h.callback(_callback(h, "aw_edit"))
        await h.callback(_callback(h, "aw_photos"))
        await h.photo("late-1")
        # H3: фото к доставленному анонсу применяется сразу, без «Применить».
        self.assertFalse(_callbacks(h, "aw_apply"))
        await h.photo("late-1")

        after = _details(h, draft_id)
        self.assertEqual(after.publication_status, "sent")
        self.assertEqual(after.photos, ("late-1",))
        self.assertEqual(len(h.posts()), len(posts_before) + 1)
        self.assertEqual(tuple(after.previous_message_ids), version_ids)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME) is not None, True)
