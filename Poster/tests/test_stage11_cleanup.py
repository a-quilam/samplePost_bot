# tests/test_stage11_cleanup.py
"""
Stage 11 (коммит D): очистка временного ввода пользователя.

Правило: временное сообщение пользователя (команда, текст шага, фото,
альбом) удаляется ТОЛЬКО после подтверждённого сохранения данных
(поле принято в user_data + снимок WIP записан в БД). Ошибка очистки —
best-effort: диалог не ломается. Готовые посты, превью, экран управления
и чужие сообщения не удаляются.

Проверяются реальные последовательности через Application.process_update
(см. tests/integration_harness) и количество delete_message.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from telegram.error import BadRequest

    from handlers.post_creation import POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    """Индекс шага по ключу поля."""
    return next(i for i, step in enumerate(POST_STEPS) if step["key"] == key)


# Валидные значения всех восьми текстовых полей (пропуски/фото — отдельно)
VALID_VALUES = {
    "title": "Концерт рок-группы",
    "date": "17.11.2026",
    "time_start": "19:00",
    "time_end": "21:30",
    "place_name": "Большой зал",
    "text": "Ждём всех!",
    "contact": "@rock_contact",
    "place_url": "https://example.com/hall",
}


IMAGE_STEP = None if INTEGRATION_IMPORT_ERROR else step_index("image")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CommandCleanupTests(unittest.TestCase):
    """Команда /start: меню на экране, сообщение команды убирается."""

    @scenario_test
    async def test_start_message_deleted_after_menu_rendered(self, h):
        update = await h.text("/start")

        self.assertIn("Здравствуйте", h.screen_text())
        self.assertEqual(
            h.deleted_ids(),
            [update.message.message_id],
            "удаляется ровно сообщение с командой /start",
        )

    @scenario_test
    async def test_menu_screen_and_posts_are_not_deleted(self, h):
        await goto_preview(h)
        posts_before = len(h.post_records())
        deletes_before = len(h.deletes())
        screen_id = h.screen().get("message_id")

        update = await h.text("/start")

        # превью (пост) и экран не удаляются — только сообщение /start
        self.assertEqual(len(h.post_records()), posts_before)
        self.assertEqual(h.deleted_ids()[deletes_before:], [update.message.message_id])
        if screen_id is not None:
            self.assertNotIn(screen_id, h.deleted_ids(), "экран не удаляется")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TextInputCleanupTests(unittest.TestCase):
    """Текстовый ввод шага: удаляется после сохранения, остаётся при ошибке."""

    @scenario_test
    async def test_accepted_text_is_deleted(self, h):
        await h.start_dialog()

        update = await h.text("Концерт рок-группы")

        self.assertEqual(
            h.deleted_ids(),
            [update.message.message_id],
            "принятое поле → сообщение ввода убрано",
        )
        self.assertEqual(h.user_data().get("title"), "Концерт рок-группы")

    @scenario_test
    async def test_each_wizard_input_is_deleted_once(self, h):
        await h.start_dialog()
        for index, step in enumerate(POST_STEPS):
            if step["key"] == "image":
                update = await h.photo("cleanup-photo-1")
            else:
                update = await h.text(VALID_VALUES[step["key"]])
            self.assertEqual(
                h.deleted_ids()[-1],
                update.message.message_id,
                f"на шаге {step['key']} ввод убран ровно один раз",
            )

    @scenario_test
    async def test_invalid_text_is_not_deleted(self, h):
        await h.goto_step(1)  # шаг «Дата» — поле с валидатором
        before = list(h.deleted_ids())

        update = await h.text("не дата")

        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertEqual(h.deleted_ids(), before, "лишних удалений нет")
        self.assertEqual(h.user_data().get("current_step"), 1)
        self.assertIn("Некорректный формат", h.screen_text())

    @scenario_test
    async def test_text_at_preview_is_not_deleted(self, h):
        await goto_preview(h)
        before = list(h.deleted_ids())

        update = await h.text("просто сообщение")

        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertEqual(h.deleted_ids(), before)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoCleanupTests(unittest.TestCase):
    """Фото и альбом: исходники уходят в очистку только после сохранения."""

    @scenario_test
    async def test_single_photo_deleted_after_saved(self, h):
        await h.goto_step(IMAGE_STEP)
        deletes_before = len(h.deleted_ids())

        update = await h.photo("cleanup-single-1")

        # file_id сохранён, превью показано — исходник убран
        self.assertEqual(h.deleted_ids()[deletes_before:], [update.message.message_id])
        self.assertEqual(h.user_data().get("photos"), ["cleanup-single-1"])
        self.assertIn("Пост готов к действию", h.screen_text())

    @scenario_test
    async def test_album_sources_deleted_only_after_done(self, h):
        await h.goto_step(IMAGE_STEP)
        deletes_before = len(h.deleted_ids())

        first = await h.photo("al-1", media_group_id="mg-clean")
        second = await h.photo("al-2", media_group_id="mg-clean")
        # сбор не завершён: исходники НЕ удаляются (гонка запоздавшего фото)
        self.assertEqual(h.deleted_ids()[deletes_before:], [])

        await h.callback("media_done")

        # все file_id сохранены и сбор завершён → исходники альбома убраны
        self.assertEqual(
            h.deleted_ids()[deletes_before:],
            [first.message.message_id, second.message.message_id],
        )
        self.assertEqual(h.user_data().get("photos"), ["al-1", "al-2"])

    @scenario_test
    async def test_late_album_photo_is_not_deleted(self, h):
        await h.goto_step(IMAGE_STEP)
        await h.photo("al-1", media_group_id="mg-late")
        await h.callback("media_done")
        posts_before = len(h.post_records())

        late = await h.photo("al-2", media_group_id="mg-late")

        # запоздавшее фото отклонено (данные не сохранены) — не удаляется
        self.assertNotIn(late.message.message_id, h.deleted_ids())
        self.assertEqual(h.user_data().get("photos"), ["al-1"])
        self.assertEqual(len(h.post_records()), posts_before)

    @scenario_test
    async def test_rejected_photo_is_not_deleted(self, h):
        # фото на шаге «Заголовок»: подсказка на экране, ввод не сохранён
        await h.start_dialog()

        update = await h.photo("stray-photo")

        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertIn("Фото можно добавить", h.screen_text())
        self.assertNotIn("photos", h.user_data())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CleanupResilienceTests(unittest.TestCase):
    """Очистка не ломает диалог: сбой БД и сбой удаления — best-effort."""

    @scenario_test
    async def test_db_failure_keeps_user_input(self, h):
        from handlers.post_creation import POST_CREATION

        await h.start_dialog()
        # Реальный сбой: таблица состояний недоступна → снимок WIP не записан
        with h.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE user_states")

        update = await h.text("Ценный текст")

        # данные приняты, но снимок не сохранён → сообщение НЕ удаляется
        self.assertEqual(h.user_data().get("title"), "Ценный текст")
        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог жив")

    @scenario_test
    async def test_delete_failure_keeps_dialog_and_data(self, h):
        from handlers.post_creation import POST_CREATION

        await h.start_dialog()
        # Telegram вернул ошибку на удаление (сообщение уже удалено/старое)
        h.bot.errors_by_type["delete_message"] = BadRequest(
            "message to delete not found"
        )

        update = await h.text("Концерт")
        h.bot.errors_by_type.pop("delete_message", None)

        # удаление не удалось — но данные приняты и диалог жив
        self.assertEqual(h.user_data().get("title"), "Концерт")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn("Шаг 2 из", h.screen_text())
        self.assertNotIn(update.message.message_id, h.deleted_ids())
