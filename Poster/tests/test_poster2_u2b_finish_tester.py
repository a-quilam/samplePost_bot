# tests/test_poster2_u2b_finish_tester.py
"""
Poster 2.0 U2b, независимая проверка экрана завершения (poster-tester).

Сценарии написаны заново (не опираются на `tests/test_poster2_u2b_finish.py`)
и проверяют только негативные/граничные случаи:

- подделанный `aw_edit` с чужим `draft_id` или ревизией отклоняется;
- чужой пользователь не открывает и не применяет правку чужого анонса (IDOR);
- двойное нажатие «Изменить» не создаёт второй экран и не отправляет копию;
- H3: чужая кнопка не может заменить версию (кнопки «Применить изменения»
  больше нет, поэтому проверяется чужой `aw_qedit`/`aw_edit`);
- повторное открытие экранов правки не даёт второй пост;
- устаревшая кнопка экрана правки не меняет готовый анонс.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest

try:
    from sqlalchemy import select

    from announcement_store import get_announcement_draft
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
        _no_photos,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _ensure_other_user(h) -> None:
    """Второй пользователь: его callback'и не должны управлять чужим анонсом."""
    with h.session_factory() as session:
        if session.scalar(select(User).where(User.telegram_user_id == OTHER_TG)):
            return
        session.add(
            User(
                telegram_user_id=OTHER_TG,
                username="foreign-u2b-finish",
                full_name="Чужой",
            )
        )
        session.commit()


async def _delivered(h, *, value: str = "Значение"):
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    await _no_photos(h)
    draft = _latest_draft(h)
    return draft, bricks


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ForeignDeliveredTests(unittest.TestCase):
    """IDOR: чужой пользователь не управляет готовым анонсом."""

    @scenario_test
    async def test_foreign_edit_callback_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        edit = _callback(h, "aw_edit")
        _ensure_other_user(h)

        await h.callback(edit, user_id=OTHER_TG)

        # Черновик не изменился, экран завершения у автора не подменён
        # чужой правкой.
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.revision, draft.revision)
        self.assertFalse(_callbacks(h, "aw_qmanage"), "правка не открылась")
        self.assertTrue(_callbacks(h, "aw_edit"))

    @scenario_test
    async def test_foreign_edit_callback_cannot_change_the_version(self, h) -> None:
        """H3: версия меняется сохранением, поэтому чужой ввод — главный риск."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        version_ids = _details(h, draft.id).delivered_message_ids
        posts_before = h.posts()

        await h.callback(_callback(h, "aw_edit"))
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Своё изменение")
        stored = _details(h, draft.id)
        self.assertIn("Своё изменение", stored.publication_text or "")
        posts_sent_by_author = h.posts()

        # Чужой пользователь пытается открыть правку чужого анонса по
        # актуальной ревизии и записать своё значение.
        _ensure_other_user(h)
        await h.callback(f"aw_edit:{draft.id}:{stored.revision}", user_id=OTHER_TG)
        self.assertFalse(_callbacks(h, "aw_qmanage"), "чужая правка не открылась")
        await h.text("Чужое изменение", user_id=OTHER_TG)

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertNotIn("Чужое изменение", stored.publication_text or "")
        self.assertIn("Своё изменение", stored.publication_text or "")
        self.assertEqual(h.posts(), posts_sent_by_author)
        self.assertTrue(set(version_ids) <= set(h.deleted_ids()))

        # Негатив: без чужого вмешательства постов ровно два.
        self.assertEqual(len(posts_before), 1)
        self.assertEqual(len(h.posts()), 2)

    @scenario_test
    async def test_foreign_open_of_draft_does_not_deliver(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()
        _ensure_other_user(h)

        await h.callback(f"aw_open:{draft.id}", user_id=OTHER_TG)

        self.assertEqual(h.posts(), posts_before, "чужих постов не появилось")
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ForgedDeliveredCallbackTests(unittest.TestCase):
    """Подделанные и повторные callback'и экрана завершения."""

    @scenario_test
    async def test_forged_edit_with_unknown_draft_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()

        await h.callback(f"aw_edit:{draft.id + 500}:{draft.revision}")

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertFalse(_callbacks(h, "aw_qmanage"), "чужая правка не открылась")
        self.assertEqual(h.posts(), posts_before)

    @scenario_test
    async def test_forged_edit_with_wrong_revision_is_rejected(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        await h.callback(f"aw_edit:{draft.id}:{draft.revision + 7}")

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertFalse(_callbacks(h, "aw_qmanage"))
        self.assertTrue(_callbacks(h, "aw_edit"))

    @scenario_test
    async def test_double_edit_keeps_one_screen_and_no_new_post(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        posts_before = h.posts()
        sends_before = len([c for c in h.screens() if c["type"] == "message"])
        edit = _callback(h, "aw_edit")

        await h.callback(edit)
        first_text = h.screen_text()
        await h.callback(edit)

        self.assertEqual(h.screen_text(), first_text, "экран не задвоился")
        self.assertEqual(
            len([c for c in h.screens() if c["type"] == "message"]),
            sends_before,
            "второго сообщения экрана нет",
        )
        self.assertEqual(h.posts(), posts_before)

    @scenario_test
    async def test_reopening_edit_screens_creates_no_second_post(self, h) -> None:
        """H3: применения по кнопке нет — и повторное открытие не плодит пост."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        version_ids = _details(h, draft.id).delivered_message_ids
        posts_before = h.posts()

        for _attempt in range(2):
            await h.callback(_callback(h, "aw_edit"))
            await h.callback(_callback(h, "aw_photos"))
            await h.callback("main_menu")
            await h.text("/start")
            await h.callback("my_announcements")
            await h.callback(f"aw_open:{draft.id}")

        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(tuple(stored.delivered_message_ids), version_ids)
        self.assertEqual(stored.publication_status, "sent")
        self.assertFalse(_callbacks(h, "aw_apply"))

    @scenario_test
    async def test_stale_edit_from_old_screen_changes_nothing(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        # Кнопка «Изменить» с экрана завершения запоминается до правки.
        stale_edit = _callback(h, "aw_edit")
        await h.callback(stale_edit)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Новое значение")
        posts_before = h.posts()

        await h.callback(stale_edit)

        self.assertIn("Данные изменились", h.answer_texts()[-1])
        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        # Устаревшая кнопка не откатила и не удвоила сохранённую версию.
        self.assertEqual(stored.publication_text, "Новое значение")
        self.assertEqual(len(h.posts()), 2)
