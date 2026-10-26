# tests/test_stage27c_announcement_ui.py
"""
Stage 27C: сквозные сценарии фото и публикации адаптивного мастера.

Все Update проходят через настоящее приложение/FakeBot, БД временная.
Проверяются оба вида шаблонов, 0/1/много фото, рестарт, двойной callback,
сбой API, частичная отправка, продолжение и защита от дублей.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from sqlalchemy import select

try:
    from announcement_store import (
        begin_announcement_publication,
        get_announcement_draft,
    )
    from announcement_wizard import WizardStage
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from models import AnnouncementDraft, User
    from template_store import get_standard_template
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
        _draft_by_id,
        _latest_draft,
        _preview,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


async def _personal_preview(
    h,
    *,
    body: str | None = None,
    specs: list[tuple[str, str, str, dict[str, object]]] | None = None,
):
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        body=body,
        specs=specs,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Значение")
    await _preview(h)
    draft = _latest_draft(h)
    return draft, bricks


async def _standard_preview(h):
    place_id, _alias_id = _seed_place(h)
    with h.session_factory() as session:
        standard_id = get_standard_template(
            session, DEFAULT_USER_ID, place_id
        ).template_id
    await _start_to_template(h, place_id)
    await h.callback(_callback(h, "aw_template", f":{standard_id}"))
    for _ in range(20):
        skips = _callbacks(h, "aw_qskip")
        if not skips:
            break
        await h.callback(skips[0])
    await _preview(h)
    return _latest_draft(h)


async def _open_photos(h) -> None:
    await h.callback(_callback(h, "aw_photos"))
    if "Фотографии анонса" not in h.screen_text():
        raise AssertionError(f"Не экран фотографий: {h.screen_text()}")


def _post_types(h, types: set[str]) -> list[dict]:
    return [call for call in h.calls if call["type"] in types]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementPublicationStandardTests(unittest.TestCase):
    @scenario_test
    async def test_standard_zero_photos_success_and_repeated_confirm(self, h) -> None:
        await h.text("/start")
        draft = await _standard_preview(h)
        self.assertIn("Фотографии: 0", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_start"))
        self.assertEqual(h.post_records(), [])

        await h.callback(_callback(h, "aw_pub_start"))
        self.assertIn("Публикация анонса", h.screen_text())
        self.assertIn("Вложений: 0", h.screen_text())
        confirm_data = _callback(h, "aw_pub_confirm")
        posts_before = len(h.post_records())
        await h.callback(confirm_data)

        with h.session_factory() as session:
            stored = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.stage, WizardStage.COMPLETED)
        self.assertIsNotNone(stored.published_at)
        self.assertEqual(stored.publication_photos, ())
        self.assertIn("Дом культуры", stored.publication_text or "")
        self.assertNotIn("[фото]", stored.publication_text or "")
        self.assertIn("Анонс опубликован", h.screen_text())
        self.assertEqual(len(h.post_records()), posts_before + 1)
        self.assertIn("Дом культуры", h.posts()[-1] or "")

        # Старый callback подтверждения не публикует второй раз.
        published_at = stored.published_at
        posts_after_success = len(h.post_records())
        await h.callback(confirm_data)
        self.assertEqual(len(h.post_records()), posts_after_success)
        with h.session_factory() as session:
            again = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(again.published_at, published_at)
        self.assertEqual(again.publication_status, "sent")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementPublicationPhotoTests(unittest.TestCase):
    @scenario_test
    async def test_photos_view_replace_restart_and_publish(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_preview(h)
        self.assertEqual(h.post_records(), [])

        await _open_photos(h)
        first_update = await h.photo("photo-1", media_group_id="album-1")
        await h.photo("photo-2", media_group_id="album-1")
        stored = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(stored.photos)), ("photo-1", "photo-2"))
        revision = stored.revision
        deleted_inputs = [
            call["args"][1]
            for call in h.calls
            if call["type"] == "delete_message" and len(call["args"]) > 1
        ]
        self.assertIn(first_update.message.message_id, deleted_inputs)

        # Повторный Update того же file_id не создаёт второе вложение.
        await h.photo("photo-1")
        duplicate = _draft_by_id(h, draft.id)
        self.assertEqual(duplicate.revision, revision)
        self.assertEqual(tuple(json.loads(duplicate.photos)), ("photo-1", "photo-2"))

        # Рестарт теряет user_data, но фото и анонс остаются в SQLite.
        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertIn("Фотографии: 2", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

        # Удаление одного фото и добавление нового — замена без фото-кирпичиков.
        await _open_photos(h)
        await h.callback(_callback(h, "aw_photo_del"))
        after_delete = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(after_delete.photos)), ("photo-2",))
        await h.photo("photo-3")
        after_add = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(after_add.photos)), ("photo-2", "photo-3"))

        # Реальный предпросмотр вложений идёт тем же отправителем.
        await h.callback(_callback(h, "aw_photo_show"))
        self.assertIn("временный предпросмотр", h.screen_text())
        preview_ids = list(h.user_data().get("preview_messages") or [])
        self.assertTrue(preview_ids)
        self.assertTrue(any(c["type"] == "media_group" for c in h.calls))

        await h.callback(_callback(h, "aw_preview"))
        await h.callback(_callback(h, "aw_pub_start"))
        confirm_data = _callback(h, "aw_pub_confirm")
        posts_before = len(h.post_records())
        await h.callback(confirm_data)

        with h.session_factory() as session:
            sent = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("photo-2", "photo-3"))
        self.assertEqual(sent.photos, ("photo-2", "photo-3"))
        self.assertIn("Значение", sent.publication_text or "")
        self.assertNotIn("[фото]", sent.publication_text or "")
        new_posts = h.post_records()[posts_before:]
        self.assertEqual(len(new_posts), 1)
        self.assertEqual(new_posts[0]["type"], "media_group")
        self.assertEqual(
            [getattr(item, "media", None) for item in new_posts[0]["media"]],
            ["photo-2", "photo-3"],
        )
        self.assertIn("Значение", h.posts()[-1] or "")
        deleted = [call for call in h.calls if call["type"] == "delete_message"]
        self.assertTrue(
            all(any(pid in call["args"] for call in deleted) for pid in preview_ids)
        )

        # Повторное подтверждение после успеха не отправляет снова.
        posts_after = len(h.post_records())
        await h.callback(confirm_data)
        self.assertEqual(len(h.post_records()), posts_after)

    @scenario_test
    async def test_single_photo_caption_failure_is_partial_and_resumes(self, h) -> None:
        await h.text("/start")
        long_body = ("Длинный текст анонса " * 150) + "[main]"
        draft, _bricks = await _personal_preview(h, body=long_body)
        await _open_photos(h)
        await h.photo("long-photo")
        await h.callback(_callback(h, "aw_preview"))

        h.bot.fail_after["message"] = 0
        await h.callback(_callback(h, "aw_pub_start"))
        confirm_data = _callback(h, "aw_pub_confirm")
        await h.callback(confirm_data)

        with h.session_factory() as session:
            partial = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(partial.publication_status, "partial")
        self.assertEqual(partial.publication_step, 1)
        self.assertEqual(len(partial.publication_progress["ids"]), 1)
        self.assertFalse(partial.publication_can_reset)
        self.assertIn("отправлен частично", h.screen_text())
        self.assertFalse(_callbacks(h, "aw_pub_reset"))

        h.bot.fail_after.clear()
        photos_before = len(_post_types(h, {"photo"}))
        await h.callback(_callback(h, "aw_pub_resume"))
        with h.session_factory() as session:
            sent = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(sent.publication_status, "sent")
        # Фото из подтверждённого шага не отправлялось второй раз.
        self.assertEqual(len(_post_types(h, {"photo"})), photos_before)
        self.assertIn("Длинный текст анонса", h.posts()[-1] or "")

    @scenario_test
    async def test_eleven_photos_use_two_groups_and_partial_resume(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_preview(h)
        await _open_photos(h)
        for index in range(11):
            await h.photo(f"group-photo-{index}")
        await h.callback(_callback(h, "aw_preview"))
        with h.session_factory() as session:
            stored = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(len(stored.photos), 11)

        h.bot.fail_after["media_group"] = 1
        await h.callback(_callback(h, "aw_pub_start"))
        await h.callback(_callback(h, "aw_pub_confirm"))
        with h.session_factory() as session:
            partial = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(partial.publication_status, "partial")
        self.assertEqual(partial.publication_step, 1)
        self.assertEqual(partial.publication_total, 2)
        self.assertFalse(partial.publication_can_reset)

        h.bot.fail_after.clear()
        await h.callback(_callback(h, "aw_pub_resume"))
        with h.session_factory() as session:
            sent = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(sent.publication_status, "sent")
        groups = _post_types(h, {"media_group"})
        # Одна группа до сбоя и одна после resume; первая не повторялась.
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            [f"group-photo-{index}" for index in range(10)],
        )
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[1]["media"]],
            ["group-photo-10"],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementPublicationFailureTests(unittest.TestCase):
    @scenario_test
    async def test_failed_before_first_operation_can_return_to_editing(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_preview(h)
        h.bot.fail_after["message"] = 0
        await h.callback(_callback(h, "aw_pub_start"))
        await h.callback(_callback(h, "aw_pub_confirm"))

        with h.session_factory() as session:
            failed = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(failed.publication_status, "failed")
        self.assertEqual(failed.publication_step, 0)
        self.assertTrue(failed.publication_can_reset)
        self.assertIn("Ни одна операция", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_reset"))

        await h.callback(_callback(h, "aw_pub_reset"))
        with h.session_factory() as session:
            reset = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertIsNone(reset.publication_status)
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertIn("можно изменить", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_start"))

        h.bot.fail_after.clear()
        await h.callback(_callback(h, "aw_pub_start"))
        await h.callback(_callback(h, "aw_pub_confirm"))
        with h.session_factory() as session:
            sent = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(sent.publication_status, "sent")
        self.assertIn("Анонс опубликован", h.screen_text())

    @scenario_test
    async def test_restart_during_sending_resumes_frozen_payload(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_preview(h)
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            begin_announcement_publication(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                expected_revision=details.revision,
                today=date.today(),
            )

        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("Отправка не завершена", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_resume"))
        self.assertFalse(_callbacks(h, "aw_pub_start"))

        await h.callback(_callback(h, "aw_pub_resume"))
        with h.session_factory() as session:
            sent = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(sent.publication_status, "sent")
        self.assertIn("Анонс опубликован", h.screen_text())

    @scenario_test
    async def test_foreign_callback_cannot_start_or_publish(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_preview(h)
        await h.callback(_callback(h, "aw_pub_start"))
        confirm_data = _callback(h, "aw_pub_confirm")

        with h.session_factory() as session:
            other = session.scalar(
                select(User).where(User.telegram_user_id == OTHER_TG)
            )
            if other is None:
                other = User(
                    telegram_user_id=OTHER_TG,
                    username="foreign-stage27c",
                    full_name="Чужой",
                )
                session.add(other)
                session.commit()

        posts_before = len(h.post_records())
        await h.callback(confirm_data, user_id=OTHER_TG)
        self.assertEqual(len(h.post_records()), posts_before)
        with h.session_factory() as session:
            stored = session.get(AnnouncementDraft, draft.id)
        self.assertIsNone(stored.publication_status)
        self.assertNotEqual(stored.stage, WizardStage.COMPLETED)
