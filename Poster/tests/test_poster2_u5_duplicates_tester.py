# tests/test_poster2_u5_duplicates_tester.py
"""
Poster 2.2 U5: независимая приёмка — текстовый анонс без фото не дублируется.

Сценарии задания:
1. Последний кирпичик → шаг фото → «⏭ Пропустить»: в чате одна копия текста,
   она выше экрана «✅ Анонс готов.» с ровно двумя кнопками.
2. Завершение сразу после последнего кирпичика (кнопка «Отправить» в обзоре,
   минуя шаг фото): тоже одна копия.
3. «Изменить» → правка → «Применить изменения»: старая копия заменена,
   второй копии нет.
4. Повторное завершение без правки: «уже отправлена», новой отправки нет.
5. Фото-путь (1 фото и альбом): по-прежнему одна копия поста, фото на месте.
6. Рестарт доставленного текстового анонса: экран завершения цел, дублей нет.

Дефект U5-D1 (очистка устаревшего живого блока не выполнялась на пути
«Повторить отправку») исправлен: снимок `live_message_ids` снимается до
продолжения публикации, и устаревший блок удаляется после подтверждённой
доставки — как в обычном fallback'е `_finish_and_deliver`.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются: только FakeBot
и временная SQLite из `tests/integration_harness.py`.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest, Forbidden

try:
    from announcement_store import get_announcement_draft
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (  # noqa: F401
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    U5_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    U5_IMPORT_ERROR = str(exc)

TEXT = "Значение"
UPDATED = "Обновлено"


def _stored(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _chat_messages(h) -> dict[int, tuple[str, str]]:
    """
    Вид содержимого и итоговый текст сообщений бота, оставшихся в чате.

    Живой блок анонса без фото создаётся `send_message`, а окончательный вид
    получает правкой, поэтому искать копию по записям `send_message` нельзя:
    берётся фактическое содержимое FakeBot без удалённых сообщений. Вид
    («text»/«caption») нужен, чтобы не путать текст анонса с подписью фото:
    у фото-поста подпись — это штатное представление того же анонса.
    """
    deleted = set(h.deleted_ids())
    store = getattr(h.bot, "_message_content", {}) or {}
    return {
        int(message_id): (str(content[0]), str(content[1]))
        for (_chat_id, message_id), content in store.items()
        if int(message_id) not in deleted and content[1] is not None
    }


def _chat_texts(h) -> dict[int, str]:
    """Итоговые тексты сообщений без учёта вида содержимого."""
    return {message_id: text for message_id, (_kind, text) in _chat_messages(h).items()}


def _copies(h, text: str) -> list[int]:
    """Отдельные ТЕКСТОВЫЕ сообщения с ровно таким текстом (не подписи фото)."""
    return sorted(
        message_id
        for message_id, (kind, shown) in _chat_messages(h).items()
        if kind == "text" and shown == text
    )


def _assert_single_copy(
    test: unittest.TestCase, h, draft_id: int, text: str = TEXT
) -> int:
    """Одна копия текста, она доставлена и лежит ВЫШЕ служебного экрана."""
    stored = _stored(h, draft_id)
    test.assertEqual(stored.publication_status, "sent")
    test.assertTrue(stored.delivered_message_ids, "доставленных адресов нет")
    copies = _copies(h, text)
    test.assertEqual(len(copies), 1, f"копий текста {text!r}: {copies}")
    message_id = copies[0]
    test.assertIn(message_id, set(stored.delivered_message_ids))
    screen_id = h.screen_message_id()
    test.assertIsNotNone(screen_id, "экран завершения не показан")
    test.assertLess(message_id, screen_id, "анонс должен быть выше экрана")
    # В чате лежит ровно тот текст, который записан как доставленный.
    test.assertEqual(_chat_texts(h)[message_id], stored.publication_text)
    return message_id


async def _to_photo_step(h, *, value: str = TEXT):
    """Мастер доведён до шага фото (текст ещё не доставлен)."""
    await h.text("/start")
    place_id, _alias = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    if "Фотографии анонса" not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()}")
    return _latest_draft(h)


@unittest.skipIf(
    U5_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {U5_IMPORT_ERROR}",
)
class TextAnnouncementNoDuplicatesTests(unittest.TestCase):
    """П.1–4 задания: одна копия текста без фото, замена при правке."""

    @scenario_test
    async def test_last_brick_then_skip_gives_single_copy_above_screen(self, h) -> None:
        """П.1: последний кирпичик → шаг фото → «⏭ Пропустить»."""
        await h.text("/start")
        draft = await _to_photo_step(h)
        self.assertIn(
            "⏭ Пропустить",
            [label for label, _data in h.screen_buttons()],
            "на шаге фото нет кнопки пропуска",
        )
        live_ids = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(live_ids, "живой блок сохранён до финала")

        await _no_photos(h)

        message_id = _assert_single_copy(self, h, draft.id)
        # Экран завершения: фиксированный текст и ровно две кнопки.
        self.assertIn("✅ Анонс готов.", h.screen_text())
        self.assertEqual(
            [label for label, _data in h.screen_buttons()],
            ["✏️ Изменить", "🏠 В меню"],
        )
        # Постом стал сам живой блок: второй отправки не было.
        self.assertEqual(set(_stored(h, draft.id).delivered_message_ids), live_ids)
        self.assertEqual(message_id, max(live_ids))
        self.assertNotIn(message_id, h.deleted_ids())
        self.assertEqual(len(h.posts()), 1, f"постов: {h.posts()}")

    @scenario_test
    async def test_send_right_after_last_brick_gives_single_copy(self, h) -> None:
        """П.2: обзор сразу после кирпичика, «Отправить» без шага фото."""
        draft = await _to_photo_step(h)
        live_ids = set(_stored(h, draft.id).live_message_ids)
        await h.callback("main_menu")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("👁 Обзор анонса", h.screen_text())
        sends_before = len(
            [call for call in h.calls if call["type"] in {"photo", "media_group"}]
        )

        await h.callback(_callback(h, "aw_send"))

        message_id = _assert_single_copy(self, h, draft.id)
        self.assertEqual(set(_stored(h, draft.id).delivered_message_ids), live_ids)
        self.assertIn(message_id, live_ids)
        self.assertEqual(
            len([call for call in h.calls if call["type"] in {"photo", "media_group"}]),
            sends_before,
            "вместо промоушена отправлен новый пост",
        )
        self.assertEqual(len(h.posts()), 1)

    @scenario_test
    async def test_edit_and_apply_replaces_the_copy(self, h) -> None:
        """П.3: «Изменить» → правка → «Применить изменения»."""
        await h.text("/start")
        draft = await _to_photo_step(h)
        await _no_photos(h)
        first = _assert_single_copy(self, h, draft.id)

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text(UPDATED)
        await _open_edit(h)
        self.assertIn(
            "🔄 Применить изменения",
            [label for label, _data in h.screen_buttons()],
        )
        await h.callback(_callback(h, "aw_apply"))

        updated = _assert_single_copy(self, h, draft.id, text=UPDATED)
        stored = _stored(h, draft.id)
        self.assertNotEqual(updated, first, "новая версия не отличает��я от старой")
        self.assertEqual(tuple(stored.delivered_message_ids), (updated,))
        self.assertEqual(tuple(stored.previous_message_ids), (first,))
        self.assertIn(first, h.deleted_ids(), "старая копия не удалена")
        self.assertEqual(_copies(h, TEXT), [], "осталась старая версия текста")
        self.assertEqual(len(h.posts()), 2, "ожидаем ровно две доставленные версии")

    @scenario_test
    async def test_repeated_finish_does_not_send_another_copy(self, h) -> None:
        """П.4: повторное завершение без правки — «уже отправлена»."""
        await h.text("/start")
        draft = await _to_photo_step(h)
        await _no_photos(h)
        first = _assert_single_copy(self, h, draft.id)
        posts_before = len(h.posts())

        await h.callback("main_menu")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertEqual(
            [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_send")
            ],
            [],
            "у доставленного анонса не должно быть кнопки отправки",
        )
        stored = _stored(h, draft.id)
        sends_before = len(
            [
                call
                for call in h.calls
                if call["type"] in {"message", "photo", "media_group"}
            ]
        )
        await h.callback(f"aw_send:{draft.id}:{stored.revision}")

        self.assertEqual(_assert_single_copy(self, h, draft.id), first)
        self.assertEqual(set(_stored(h, draft.id).delivered_message_ids), {first})
        new_sends = [
            call
            for call in h.calls
            if call["type"] in {"message", "photo", "media_group"}
        ][sends_before:]
        # Повторное завершение не отправляет пост: только правки экрана.
        self.assertEqual(new_sends, [], "повторное завершение отправило сообщение")
        self.assertEqual(len(h.posts()), posts_before)
        self.assertIn(
            "уже отправлена",
            " ".join(text for text in h.answer_texts() if text),
            "нет сообщения «уже отправлена»",
        )


@unittest.skipIf(
    U5_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {U5_IMPORT_ERROR}",
)
class PhotoAndRestartSingleCopyTests(unittest.TestCase):
    """П.5–6 задания: фото-путь и рестарт доставленного текстового анонса."""

    def _media_calls(self, h) -> list[dict]:
        return [call for call in h.calls if call["type"] in {"photo", "media_group"}]

    @scenario_test
    async def test_single_photo_keeps_one_post_and_photo(self, h) -> None:
        """П.5: одно фото — одна копия поста, подпись с текстом."""
        await h.text("/start")
        draft = await _to_photo_step(h)
        live_ids = set(_stored(h, draft.id).live_message_ids)

        await h.photo("single-u5")

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("single-u5",))
        photos = [call for call in self._media_calls(h) if call["type"] == "photo"]
        self.assertEqual(len(photos), 1, "фото отправлено повторно/потеряно")
        self.assertEqual(
            photos[0].get("caption"),
            stored.publication_text,
            "подпись поста не совпадает с текстом анонса",
        )
        self.assertEqual(_copies(h, TEXT), [], "текст остался отдельной копией")
        self.assertTrue(
            live_ids <= set(h.deleted_ids()),
            "прежний текстовый блок не заменён медиаблоком",
        )
        screen_id = h.screen_message_id()
        assert screen_id is not None
        message_id = self._only_post_id(h, stored)
        self.assertLess(message_id, screen_id, "пост должен быть выше экрана")
        self.assertEqual(len(h.posts()), 1)

    @scenario_test
    async def test_album_keeps_one_post_and_both_photos(self, h) -> None:
        """П.5: альбом — один пост, обе фотографии в чате."""
        await h.text("/start")
        draft = await _to_photo_step(h)

        await h.photo("alb-u5-1", media_group_id="mg-u5")
        await h.photo("alb-u5-2", media_group_id="mg-u5")
        await asyncio.sleep(0.4)

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("alb-u5-1", "alb-u5-2"))
        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "альбом отправлен больше одного раза")
        media = groups[0]["media"]
        self.assertEqual(
            [getattr(item, "media", None) for item in media],
            ["alb-u5-1", "alb-u5-2"],
        )
        self.assertEqual(
            [getattr(item, "caption", None) for item in media],
            [None, stored.publication_text],
            "подпись альбома не совпадает с текстом анонса",
        )
        self.assertEqual(_copies(h, TEXT), [], "текст ушёл отдельной копией")
        self.assertEqual(len(h.posts()), 1)

    @scenario_test
    async def test_restart_after_delivery_keeps_single_copy(self, h) -> None:
        """П.6: рестарт, «Мои анонсы», экран завершения — без дублей."""
        await h.text("/start")
        draft = await _to_photo_step(h)
        await _no_photos(h)
        first = _assert_single_copy(self, h, draft.id)
        sends_before = len(self._media_calls(h))

        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        self.assertIn("📂 Мои анонсы", h.screen_text())
        self.assertIn(
            "готовый анонс отправлен",
            h.screen_text(),
            "в списке нет доставленного анонса",
        )
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))

        self.assertIn("✅ Анонс готов.", h.screen_text())
        self.assertEqual(
            [label for label, _data in h.screen_buttons()],
            ["✏️ Изменить", "🏠 В меню"],
        )
        self.assertEqual(_assert_single_copy(self, h, draft.id), first)
        self.assertEqual(len(self._media_calls(h)), sends_before)
        self.assertNotIn(first, h.deleted_ids())

        # «Изменить» после рестарта открывает компактный экран правки.
        await _open_edit(h)
        self.assertIn("✏️ Редактирование анонса", h.screen_text())

    def _only_post_id(self, h, stored) -> int:
        ids = list(stored.delivered_message_ids)
        self.assertEqual(len(ids), 1, f"доставлено сообщений: {ids}")
        return int(ids[0])


@unittest.skipIf(
    U5_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {U5_IMPORT_ERROR}",
)
class U5DuplicateAfterResumeTests(unittest.TestCase):
    """Дефект U5-D1: путь «Повторить отправку» не убирает устаревший блок."""

    @scenario_test
    async def test_stale_live_block_is_cleaned_on_resume(self, h) -> None:
        """
        Рестарт → Telegram недоступен на завершении (правка блока, пересборка
        и отправка падают) → Telegram восстановился → «Повторить отправку».

        Ожидание: в чате одна копия текста (устаревший живой блок первой
        попытки удалён, как в обычном fallback'е `_run_announcement_publication`).
        Факт: живой блок остаётся, рядом появляется доставленный пост — две
        одинаковые копии, `live_message_ids` указывают на первую.
        """
        await h.text("/start")
        draft = await _to_photo_step(h)
        stale_live = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(stale_live)

        # Рестарт: user_data пуст — промоушен нужно доказывать правкой блока.
        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("👁 Обзор анонса", h.screen_text())

        # Сбой Telegram ровно на завершении: ни правка блока, ни пересборка,
        # ни отправка поста не подтверждены.
        h.bot.errors_by_type["edit_message"] = BadRequest("Message to edit not found")
        h.bot.errors_by_type["message"] = Forbidden("Forbidden: bot was blocked")
        await h.callback(_callback(h, "aw_send"))
        self.assertEqual(
            _stored(h, draft.id).publication_status,
            "failed",
            "сбой доставки должен быть зафиксирован",
        )
        self.assertEqual(_stored(h, draft.id).delivered_message_ids, ())

        # Telegram снова работает; пользователь повторяет отправку.
        h.bot.errors_by_type.clear()
        await h.callback("main_menu")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        await h.callback(_callback(h, "aw_pub_resume"))

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        copies = _copies(h, TEXT)
        self.assertEqual(
            len(copies),
            1,
            f"после «Повторить отправку» копий текста {len(copies)}: {copies}; "
            f"устаревший блок {sorted(stale_live)}",
        )
        self.assertTrue(
            stale_live <= set(h.deleted_ids()),
            "устаревший живой блок не удалён после подтверждённой доставки",
        )
        self.assertEqual(stored.live_message_ids, ())

    @scenario_test
    async def test_stale_live_block_is_cleaned_on_resume_without_restart(
        self, h
    ) -> None:
        """
        То же без рестарта, но промоушен невозможен (нет доказанной
        синхронизации): fallback-доставка падает, затем повтор succeeds.

        Ожидание: одна копия. Факт: устаревший блок остаётся.
        """
        await h.text("/start")
        draft = await _to_photo_step(h)
        stale_live = set(_stored(h, draft.id).live_message_ids)
        self.assertTrue(stale_live)

        # Промоушен невозможен, отправка первого прохода падает.
        h.bot.errors_by_type["message"] = Forbidden("Forbidden: bot was blocked")
        with patch(
            "handlers.announcement_wizard._promote_live_post",
            new=AsyncMock(return_value=None),
        ):
            await _no_photos(h)
        self.assertEqual(_stored(h, draft.id).publication_status, "failed")

        h.bot.errors_by_type.clear()
        await h.callback(_callback(h, "aw_pub_resume"))

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        copies = _copies(h, TEXT)
        self.assertEqual(
            len(copies),
            1,
            f"после повтора копий текста {len(copies)}: {copies}; "
            f"устаревший блок {sorted(stale_live)}",
        )
        self.assertTrue(stale_live <= set(h.deleted_ids()))
        self.assertEqual(stored.live_message_ids, ())
