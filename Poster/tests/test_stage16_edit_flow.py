# tests/test_stage16_edit_flow.py
"""
Stage 16 (дефект №4): кнопка «✏️ Редактировать» — полная рабочая цепочка.

Реальный инцидент: нажатие «Редактировать» не давало видимого меню, а
последующий текст приводил к «Редактирование без выбранного поля — возврат
к превью»: EDIT_FIELD наступал БЕЗ выбранного поля («пустой идентификатор
поля»), меню рендерилось на сообщении-экране, отдельном от превью.

Правила (Stage 16):
- «Редактировать» открывает меню полей; EDIT_FIELD наступает ТОЛЬКО
  вместе с выбранным полем — состояние с пустым полем невозможно;
- промпт поля показывает ТЕКУЩЕЕ значение и подсказку;
- цепочка: превью → меню → поле → ввод (или «Пропустить»/неверный ввод)
  → обновлённое превью; отмена возвращает к превью без потери данных;
- повторное «Редактировать»/устаревшие кнопки отвечают честно и не
  ломают диалог; неизвестное поле не завершает диалог;
- рестарт во время правки поля восстанавливает промпт этого поля.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import EDIT_FIELD, POST_CREATION, POST_STEPS
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


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EditFlowTests(unittest.TestCase):
    """Цепочка редактирования из превью."""

    @scenario_test
    async def test_menu_lists_all_fields(self, h):
        """«Редактировать» → меню со всеми полями; состояние не EDIT_FIELD."""
        await goto_preview(h, title="Исходный заголовок")

        await h.callback("edit_post")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        buttons = h.screen_buttons()
        labels = [text for text, _ in buttons]
        for step in POST_STEPS:
            self.assertIn(step["label"], labels, f"нет поля {step['label']}")
        self.assertIn(("◀️ Назад", "cancel_edit"), buttons)
        self.assertIn("Выберите поле", h.screen_text())

    @scenario_test
    async def test_full_chain_updates_preview(self, h):
        """Цепочка: меню → поле → ввод → обновлённое превью."""
        await goto_preview(h, title="Исходный заголовок")

        await h.callback("edit_post")
        await h.callback("edit_title")
        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        prompt = h.screen_text()
        self.assertIn("Текущее значение: Исходный заголовок", prompt)
        self.assertIn("«Заголовок»", prompt)

        await h.text("Новый заголовок")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("title"), "Новый заголовок")
        self.assertIn("Новый заголовок", h.posts()[-1])
        # правка завершена: поле не «висит» в редактировании
        self.assertIsNone(h.user_data().get("edit_field"))

    @scenario_test
    async def test_invalid_input_stays_in_field_edit(self, h):
        """Неверный ввод при правке: ошибка на том же поле, значение цело."""
        await goto_preview(h, title="Заголовок")
        await h.callback("edit_post")
        await h.callback("edit_date")

        bad = await h.text("не дата")

        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        self.assertIn("Некорректный формат", h.screen_text())
        self.assertIn("Дата", h.screen_text())
        self.assertEqual(h.user_data().get("title"), "Заголовок")
        self.assertNotIn(bad.message.message_id, h.deleted_ids())

        await h.text("17.11.2026")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("date"), "17.11.2026")

    @scenario_test
    async def test_prompt_shows_current_value_and_empty(self, h):
        """Промпт показывает текущее значение; пустое поле — «не задано»."""
        await goto_preview(h, title="Заголовок")  # остальные поля пропущены

        await h.callback("edit_post")
        await h.callback("edit_title")
        self.assertIn("Текущее значение: Заголовок", h.screen_text())

        await h.callback("edit_place_name")
        self.assertIn("Текущее значение: не задано", h.screen_text())

    @scenario_test
    async def test_prompt_shows_photo_count(self, h):
        """Промпт правки изображения показывает размер набора."""
        await goto_preview(h, title="Заголовок")
        await h.photo("cnt-1", media_group_id="mg-c")
        await h.photo("cnt-2", media_group_id="mg-c")
        await h.callback("media_done")

        await h.callback("edit_post")
        await h.callback("edit_image")
        self.assertIn("Текущее значение: 2 фотографии", h.screen_text())

    @scenario_test
    async def test_cancel_edit_keeps_data_and_preview(self, h):
        """Отмена правки: данные не теряются, превью не тронуто."""
        await goto_preview(h, title="Заголовок")
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.callback("cancel_edit")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("title"), "Заголовок")
        self.assertIn("Пост готов к действию", h.screen_text())

    @scenario_test
    async def test_repeated_edit_post_shows_menu_again(self, h):
        """Повторное «Редактировать» в меню — снова меню, без блокировки."""
        await goto_preview(h, title="Заголовок")

        await h.callback("edit_post")
        await h.callback("edit_post")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn("Выберите поле", h.screen_text())

    @scenario_test
    async def test_switch_field_during_edit(self, h):
        """Кнопка другого поля во время правки — переход без потери данных."""
        await goto_preview(h, title="Заголовок")
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.callback("edit_date")  # устаревшая кнопка меню — дружелюбный переход

        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        self.assertEqual(h.user_data().get("edit_field"), "date")

        await h.text("17.11.2026")
        self.assertEqual(h.user_data().get("title"), "Заголовок", "другое поле цело")

    @scenario_test
    async def test_preview_buttons_during_edit_hint(self, h):
        """Кнопки превью во время правки: подсказка, состояние сохранено."""
        await goto_preview(h, title="Заголовок")
        await h.callback("edit_post")
        await h.callback("edit_title")

        for data in ("finish", "save_draft", "edit_post"):
            with self.subTest(data=data):
                await h.callback(data)
                self.assertEqual(h.dialog_state(), 1)
                self.assertEqual(len(h.drafts()), 0, "ничего не сохранено")
                self.assertEqual(len(h.posts()), 1, "пост не отправлен")

    @scenario_test
    async def test_unknown_field_keeps_dialog_alive(self, h):
        """Неизвестное поле не завершает диалог: ответ + возврат к превью."""
        await goto_preview(h, title="Заголовок")

        await h.callback("edit_banana")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn("Неизвестное поле", h.answers()[-1]["text"])
        self.assertIn("Пост готов к действию", h.screen_text())

    @scenario_test
    async def test_stale_edit_button_after_dialog_end(self, h):
        """Правка после завершения диалога — честный ответ, без сбоя."""
        await goto_preview(h, title="Заголовок")
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())

        await h.callback("edit_title")

        self.assertIn("Кнопка устарела", h.answers()[-1]["text"])

    @scenario_test
    async def test_restart_during_edit_restores_prompt(self, h):
        """Рестарт во время правки поля: восстанавливается промпт поля."""
        await goto_preview(h, title="Заголовок")
        await h.callback("edit_post")
        await h.callback("edit_title")

        await h.restart()
        await h.callback("restore_wip")

        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        self.assertEqual(h.user_data().get("edit_field"), "title")
        self.assertIn("Текущее значение: Заголовок", h.screen_text())

        await h.text("Восстановленный")
        self.assertEqual(h.user_data().get("title"), "Восстановленный")
        self.assertEqual(h.dialog_state(), POST_CREATION)


if __name__ == "__main__":
    unittest.main()
