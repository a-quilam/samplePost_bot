# tests/test_stage16_photos.py
"""
Stage 16 (дефект №5): приём нескольких фотографий и альбомов.

Реальный инцидент: пользователь отправил два альбома — в черновик попало
ТОЛЬКО фото последнего альбома: новый альбом/одиночка ЗАМЕНЯЛИ собранный
набор, а не добавлялись к нему.

Правила (Stage 16):
- визард НАКАПЛИВАЕТ набор: одиночка, альбом, второй альбом и отдельные
  снимки подряд складываются в один список file_id;
- одиночка при пустом наборе сразу показывает превью (прежнее поведение);
- этап изображения завершается кнопкой «✅ Фото готово»;
- превью принимает новые фото: одиночка обновляет превью, новый альбом
  начинает сбор кнопкой;
- запоздавшие фото ЗАВЕРШЁННОГО альбома не меняют пост (правило Stage 9);
- правка поля «Изображение» ЗАМЕНЯЕТ набор (осознанная замена);
- отправка разбивает набор на медиа-группы по ≤10 (лимит Telegram);
- превью и финальный пост содержат ровно одинаковый полный набор.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import MEDIA_GROUP_LIMIT, POST_STEPS
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    """Индекс шага по ключу поля."""
    return next(i for i, step in enumerate(POST_STEPS) if step["key"] == key)


def album_media(call: dict) -> list:
    """file_id-снимков одного вызова send_media_group."""
    return [m.media for m in call["media"]]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoAccumulationTests(unittest.TestCase):
    """Накопление фото: снимки, альбомы, второй альбом, лимиты."""

    @scenario_test
    async def test_two_albums_accumulate(self, h):
        """Два альбома подряд → в наборе все три фото (инцидент Stage 16)."""
        await h.goto_step(step_index("image"))
        await h.photo("alb1-1", media_group_id="mg-1")
        await h.photo("alb1-2", media_group_id="mg-1")
        await h.photo("alb2-1", media_group_id="mg-2")

        self.assertEqual(
            h.user_data().get("photos"), ["alb1-1", "alb1-2", "alb2-1"]
        )

        await h.callback("media_done")
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(
            album_media(preview_album), ["alb1-1", "alb1-2", "alb2-1"]
        )

    @scenario_test
    async def test_several_singles_in_a_row(self, h):
        """Отдельные снимки подряд: каждый добавляет фото, превью растёт."""
        await h.goto_step(step_index("image"))
        await h.photo("solo-1")
        self.assertEqual(h.user_data().get("photos"), ["solo-1"])

        await h.photo("solo-2")
        self.assertEqual(h.user_data().get("photos"), ["solo-1", "solo-2"])
        self.assertEqual(h.user_data().get("current_step"), len(POST_STEPS))

        await h.photo("solo-3")
        self.assertEqual(
            h.user_data().get("photos"), ["solo-1", "solo-2", "solo-3"]
        )
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(album_media(preview_album), ["solo-1", "solo-2", "solo-3"])

    @scenario_test
    async def test_album_after_single(self, h):
        """Альбом после одиночной фотографии: сбор + «Фото готово»."""
        await h.goto_step(step_index("image"))
        await h.photo("solo-1")  # одиночка → превью

        await h.photo("alb-1", media_group_id="mg-a")
        self.assertIn("Фото готово", h.screen_buttons()[0][0])
        await h.photo("alb-2", media_group_id="mg-a")
        await h.callback("media_done")

        self.assertEqual(
            h.user_data().get("photos"), ["solo-1", "alb-1", "alb-2"]
        )
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(album_media(preview_album), ["solo-1", "alb-1", "alb-2"])

    @scenario_test
    async def test_single_during_album_collection_stays(self, h):
        """Одиночка посреди сбора альбома добавляется, этап не завершается."""
        await h.goto_step(step_index("image"))
        await h.photo("alb-1", media_group_id="mg-a")
        await h.photo("solo-mid")  # не преждевременно к превью
        self.assertEqual(h.user_data().get("current_step"), step_index("image"))
        await h.photo("alb-2", media_group_id="mg-a")
        await h.callback("media_done")

        self.assertEqual(
            h.user_data().get("photos"), ["alb-1", "solo-mid", "alb-2"]
        )

    @scenario_test
    async def test_media_group_limit_chunking(self, h):
        """Набор больше лимита Telegram отправляется группами ≤10."""
        await h.goto_step(step_index("image"))
        for index in range(6):
            await h.photo(f"g1-{index}", media_group_id="mg-big1")
        for index in range(6):
            await h.photo(f"g2-{index}", media_group_id="mg-big2")
        await h.callback("media_done")
        self.assertEqual(len(h.user_data().get("photos")), 12)

        groups = [c for c in h.calls if c["type"] == "media_group"]
        self.assertTrue(all(len(c["media"]) <= MEDIA_GROUP_LIMIT for c in groups))
        expected = list(h.user_data().get("photos"))
        self.assertEqual(len(expected), 12)
        preview_ids = [fid for c in groups for fid in album_media(c)]
        self.assertEqual(preview_ids, expected)

        await h.callback("finish")
        final_groups = [
            c
            for c in h.calls[h.calls.index(groups[-1]) + 1 :]
            if c["type"] == "media_group"
        ]
        final_ids = [fid for c in final_groups for fid in album_media(c)]
        self.assertEqual(final_ids, expected)
        self.assertTrue(
            all(len(c["media"]) <= MEDIA_GROUP_LIMIT for c in final_groups)
        )

    @scenario_test
    async def test_draft_saves_all_accumulated_photos(self, h):
        """В черновик попадает ВЕСЬ собранный набор file_id."""
        await h.goto_step(step_index("image"))
        await h.photo("keep-1", media_group_id="mg-1")
        await h.photo("keep-2", media_group_id="mg-1")
        await h.photo("keep-3", media_group_id="mg-2")
        await h.callback("media_done")
        await h.callback("save_draft")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(
            drafts[0].photos, '["keep-1", "keep-2", "keep-3"]'
        )

    @scenario_test
    async def test_edit_image_replaces_set(self, h):
        """Правка поля «Изображение» заменяет набор (осознанная замена)."""
        await h.goto_step(step_index("image"))
        await h.photo("old-1", media_group_id="mg-1")
        await h.photo("old-2", media_group_id="mg-1")
        await h.callback("media_done")

        await h.callback("edit_post")
        await h.callback("edit_image")
        await h.photo("new-1", media_group_id="mg-2")
        await h.photo("new-2", media_group_id="mg-2")
        await h.callback("media_done")

        self.assertEqual(h.user_data().get("photos"), ["new-1", "new-2"])
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(album_media(preview_album), ["new-1", "new-2"])

    @scenario_test
    async def test_preview_equals_final_with_accumulated_set(self, h):
        """Превью и финальный пост содержат одинаковый полный набор."""
        await h.goto_step(step_index("image"))
        await h.photo("mix-1", media_group_id="mg-1")
        await h.photo("mix-2", media_group_id="mg-1")
        await h.photo("mix-3")  # одиночка поверх альбома
        await h.callback("media_done")

        preview_groups = [c for c in h.calls if c["type"] == "media_group"]
        preview_ids = [fid for c in preview_groups for fid in album_media(c)]

        await h.callback("finish")
        final_groups = [
            c
            for c in h.calls[h.calls.index(preview_groups[-1]) + 1 :]
            if c["type"] == "media_group"
        ]
        final_ids = [fid for c in final_groups for fid in album_media(c)]
        self.assertEqual(preview_ids, final_ids)
        self.assertEqual(preview_ids, ["mix-1", "mix-2", "mix-3"])


if __name__ == "__main__":
    unittest.main()
