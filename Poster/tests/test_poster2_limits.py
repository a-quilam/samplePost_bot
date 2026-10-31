# tests/test_poster2_limits.py
"""
Poster 2.0 S4 §5.1/§5.3: лимиты текста и фотографий.

Проверяется:

- длина считается по ВИДИМОМУ тексту после разбора разметки (HTML-теги и
  сущности не съедают лимит), в единицах UTF-16 (эмодзи — 2);
- границы 1024/1025 (подпись к фото) и 4096/4097 (сообщение без фото);
- единая точка правды: `delivery` и `announcement_store` дают один лимит
  фотографий (10), 11-е фото отклоняется, принятые сохраняются;
- `media_batches`: 0 → нет партий, 1 → один `sendPhoto`, 2–10 → одна
  группа, 11 → 10 + 1 (хвост отдельным `sendPhoto`), 20 → две группы.

Тесты идут без сети и без реальной БД: только компилятор и (для шага фото)
временная SQLite через настоящее приложение.
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import MAX_ANNOUNCEMENT_PHOTOS
    from delivery import (
        ANNOUNCEMENT_CAPTION_LIMIT,
        ANNOUNCEMENT_PHOTO_LIMIT,
        ANNOUNCEMENT_TEXT_LIMIT,
        MEDIA_GROUP_LIMIT,
        media_batches,
    )
    from template_compiler import (
        announcement_text_limit,
        check_announcement_length,
        visible_text_length,
    )
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

    # Заглушки, чтобы декоратор @unittest.skipIf видел имена при сбое импорта.
    class _Stub:  # noqa: D401 — только для аннотаций ниже
        pass

    delivery = None
    MAX_ANNOUNCEMENT_PHOTOS = 0


def _plain(text: str) -> str:
    """Текст анонса без разметки — ровно `text` видимых символов."""
    return text


class VisibleTextLengthTests(unittest.TestCase):
    """Длина по видимому тексту, а не по сырому HTML."""

    def test_html_tags_are_not_counted(self) -> None:
        html = "<b>привет</b>"
        self.assertEqual(visible_text_length(html), len("привет"))
        self.assertEqual(visible_text_length("<b></b>"), 0)

    def test_entities_count_as_one_visible_character(self) -> None:
        # `&amp;` — один символ `&`, а не пять знаков исходника.
        self.assertEqual(visible_text_length("a &amp; b"), len("a & b"))
        self.assertEqual(visible_text_length("&lt;тег&gt;"), len("<тег>"))

    def test_emoji_count_in_utf16_units(self) -> None:
        # Суррогатная пара: Telegram считает 2 единицы, Python len — 1.
        self.assertEqual(visible_text_length("🎉"), 2)
        self.assertEqual(visible_text_length("a🎉"), 3)
        # Кириллица и составные эмодзи без суррогатов — по одному символу.
        self.assertEqual(visible_text_length("ёж"), 2)
        self.assertEqual(visible_text_length("❤️"), 2)


class AnnouncementTextLimitTests(unittest.TestCase):
    """Границы 1024/1025 и 4096/4097 после разбора entities."""

    def test_limits_depend_on_photos(self) -> None:
        self.assertEqual(announcement_text_limit(has_photos=False), 4096)
        self.assertEqual(announcement_text_limit(has_photos=True), 1024)
        self.assertEqual(ANNOUNCEMENT_TEXT_LIMIT, 4096)
        self.assertEqual(ANNOUNCEMENT_CAPTION_LIMIT, 1024)

    def test_caption_boundary_1024_and_1025(self) -> None:
        exactly = check_announcement_length("д" * 1024, has_photos=True)
        self.assertEqual((exactly.length, exactly.limit), (1024, 1024))
        self.assertFalse(exactly.over)
        self.assertEqual(exactly.excess, 0)

        over = check_announcement_length("д" * 1025, has_photos=True)
        self.assertEqual((over.length, over.limit), (1025, 1024))
        self.assertTrue(over.over)
        self.assertEqual(over.excess, 1)

    def test_message_boundary_4096_and_4097(self) -> None:
        exactly = check_announcement_length("д" * 4096, has_photos=False)
        self.assertFalse(exactly.over)
        over = check_announcement_length("д" * 4097, has_photos=False)
        self.assertTrue(over.over)
        self.assertEqual(over.excess, 1)

    def test_markup_does_not_consume_the_limit(self) -> None:
        # 1024 видимых символа (1020 + пробел + `&&&`), но исходник длиннее
        # из-за тегов и сущностей — граница не сдвигается.
        html = "<b>" + ("ж" * 1020) + "</b> &amp;&amp;&amp;"
        check = check_announcement_length(html, has_photos=True)
        self.assertEqual(check.length, 1024)
        self.assertFalse(check.over)

    def test_emoji_boundary_uses_utf16(self) -> None:
        # 512 эмодзи = 1024 единицы: ровно предел подписи.
        self.assertFalse(check_announcement_length("🎉" * 512, has_photos=True).over)
        # 513 эмодзи = 1026 единиц: превышение на 2.
        over = check_announcement_length("🎉" * 513, has_photos=True)
        self.assertEqual(over.length, 1026)
        self.assertEqual(over.excess, 2)

    def test_photo_limit_is_the_same_everywhere(self) -> None:
        self.assertEqual(ANNOUNCEMENT_PHOTO_LIMIT, 10)
        self.assertEqual(MAX_ANNOUNCEMENT_PHOTOS, ANNOUNCEMENT_PHOTO_LIMIT)
        self.assertEqual(MEDIA_GROUP_LIMIT, 10)


class MediaBatchesTests(unittest.TestCase):
    """Разбиение фото на вызовы Telegram (2–10 в группе, хвост 1 отдельно)."""

    def test_no_photos_means_no_batches(self) -> None:
        self.assertEqual(media_batches(0), [])

    def test_single_photo_is_one_batch_sent_as_photo(self) -> None:
        # Партия размера 1 уходит `sendPhoto`: `sendMediaGroup` её не примет.
        self.assertEqual(media_batches(1), [1])

    def test_two_to_ten_photos_are_a_single_group(self) -> None:
        for count in range(2, 11):
            self.assertEqual(media_batches(count), [count])

    def test_eleven_photos_are_group_plus_tail(self) -> None:
        self.assertEqual(media_batches(11), [10, 1])

    def test_twenty_photos_are_two_groups(self) -> None:
        self.assertEqual(media_batches(20), [10, 10])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoLimitScenarioTests(unittest.TestCase):
    """11-е фото отклоняется, 10 принятых остаются и уходят одной группой."""

    def _draft(self, h, draft_id: int):
        from announcement_store import get_announcement_draft

        with h.session_factory() as session:
            return get_announcement_draft(
                session,
                actor_telegram_user_id=7,
                draft_id=draft_id,
            )

    @scenario_test
    async def test_eleventh_photo_is_rejected_and_ten_are_kept(self, h) -> None:
        from tests.test_stage27c_announcement_ui import (
            _callbacks,
            _open_photos,
            _personal_delivered,
        )

        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        await _open_photos(h)
        for index in range(10):
            await h.photo(f"limit-photo-{index}")

        rejected = await h.photo("limit-photo-10")
        stored = self._draft(h, draft.id)
        self.assertEqual(
            stored.photos, tuple(f"limit-photo-{index}" for index in range(10))
        )
        self.assertIn("больше добавить нельзя", h.screen_text())
        # Сообщение с отклонённым снимком не удалено: автор видит причину.
        self.assertNotIn(rejected.message.message_id, h.deleted_ids())

        # H3: каждое фото применяется сразу — отдельной кнопки нет. Поэтому
        # важна не «одна отправка», а итог: последняя версия несёт все 10
        # снимков одной медиагруппой, а прошлые заменены.
        self.assertFalse(_callbacks(h, "aw_apply"))
        sent = self._draft(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_total, 1)
        self.assertEqual(
            sent.publication_photos,
            tuple(f"limit-photo-{index}" for index in range(10)),
        )
        groups = [c for c in h.calls if c["type"] == "media_group"]
        self.assertTrue(groups, "фото так и не ушли")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[-1]["media"]],
            [f"limit-photo-{index}" for index in range(10)],
        )
        # Прошлые версии заменены: в чате ровно одна актуальная группа.
        live = [message_id for message_id in sent.delivered_message_ids]
        self.assertEqual(len(live), 1)
        self.assertNotIn(live[0], h.deleted_ids())
