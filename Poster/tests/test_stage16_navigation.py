# tests/test_stage16_navigation.py
"""
Stage 16 (дефект №7): навигация после сохранения черновика и пересоздание
экрана у точки ввода пользователя.

Реальный инцидент: после «Сохранить в черновики» пользователь набирал
«Черновики» или «Создать пост» внизу чата — бот отвечал МОЛЧА (update
попадал в stale_dialog_action) либо ответ уходил к СТАРОМУ сообщению-экрану
выше по чату и не был виден. Ничего из меню не работало.

Правила (Stage 16):
- навигация пользовательским СООБЩЕНИЕМ (команда или текст кнопки меню)
  пересоздаёт экран рядом с этим сообщением: старое сообщение-экран
  удаляется best-effort, ref очищается в user_data и в БД (WIP-payload
  сохраняется), ответ виден внизу чата;
- callback-навигация (кнопки на самом экране) экран НЕ пересоздаёт —
  правка на месте, сообщений не накапливается;
- сообщения НЕ новее экрана (штатная последовательность) не пересоздают
  экран;
- навигация сообщением работает и после рестарта (ref из БД).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import POST_CREATION, POST_STEPS
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


def last_render(h, marker: str) -> dict | None:
    """Последний рендер экрана, чей текст содержит marker (send или edit)."""
    for call in reversed(h.calls):
        if call["type"] in ("message", "edit_message"):
            if marker in (call.get("text") or ""):
                return call
    return None


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NavigationAfterSaveTests(unittest.TestCase):
    """Навигация текстом/командой НИЖЕ экрана."""

    @scenario_test
    async def test_drafts_button_below_screen_works(self, h):
        """Сценарий из инцидента: «Черновики» внизу чата после сохранения."""
        await goto_preview(h, title="Готовый пост")
        await h.callback("save_draft")
        old_screen = h.screen_message_id()

        await h.text_from_below("📝 Черновики")

        self.assertIn("Ваши черновики", h.screen_text())
        render = last_render(h, "Ваши черновики")
        self.assertEqual(
            render["type"], "message", "ответ должен отправиться НОВЫМ сообщением внизу"
        )
        self.assertIn(old_screen, h.deleted_ids(), "старый экран должен уйти в очистку")
        self.assertNotEqual(h.screen_message_id(), old_screen, "ref указывает на новый")

    @scenario_test
    async def test_create_post_button_below_screen_starts_dialog(self, h):
        """«Создать пост» внизу чата — диалог начинается рядом с вводом."""
        await goto_preview(h, title="Пост")
        await h.callback("save_draft")
        old_screen = h.screen_message_id()

        await h.text_from_below("✏️ Создать пост")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn("Заголовок", h.screen_text())
        render = last_render(h, "Заголовок")
        self.assertEqual(render["type"], "message")
        self.assertIn(old_screen, h.deleted_ids())

    @scenario_test
    async def test_commands_below_screen_work(self, h):
        """/start, /help, /drafts, /cancel внизу чата — каждый даёт видимый ответ."""
        for command, marker in (
            ("/start", "Выберите действие"),
            ("/help", "Как работать"),
            ("/drafts", "черновик"),
            ("/cancel", "Сейчас нет активного создания поста"),
        ):
            with self.subTest(command=command):
                await h.text("/start")  # штатный путь: экран создан на месте
                old_screen = h.screen_message_id()

                await h.text_from_below(command)

                self.assertIn(marker, h.screen_text(), f"нет ответа на {command}")
                render = last_render(h, marker)
                self.assertEqual(render["type"], "message")
                self.assertIn(old_screen, h.deleted_ids())

    @scenario_test
    async def test_text_below_without_dialog_shows_hint_near_input(self, h):
        """Обычный текст вне диалога внизу чата — подсказка рядом с вводом."""
        await h.text("/start")
        old_screen = h.screen_message_id()

        await h.text_from_below("какой-то текст")

        self.assertIn("Воспользуйтесь кнопками меню", h.screen_text())
        render = last_render(h, "Воспользуйтесь кнопками меню")
        self.assertEqual(render["type"], "message")
        self.assertIn(old_screen, h.deleted_ids())

    @scenario_test
    async def test_button_navigation_keeps_screen_in_place(self, h):
        """Callback-навигация: правка на месте, без удаления и дублей."""
        await h.text("/start")
        old_screen = h.screen_message_id()

        await h.callback("drafts")

        self.assertIn("черновик", h.screen_text())
        render = last_render(h, "черновик")
        self.assertEqual(render["type"], "edit_message")
        self.assertNotIn(old_screen, h.deleted_ids(), "экран не должен удаляться")
        self.assertEqual(h.screen_message_id(), old_screen, "ref не меняется")

    @scenario_test
    async def test_message_above_screen_does_not_reanchor(self, h):
        """Сообщение НЕ новее экрана — экран не пересоздаётся."""
        await h.text("/start")
        old_screen = h.screen_message_id()

        await h.text("/drafts")  # id сообщения меньше id экрана

        self.assertIn("черновик", h.screen_text())
        render = last_render(h, "черновик")
        self.assertEqual(render["type"], "edit_message")
        self.assertNotIn(old_screen, h.deleted_ids())

    @scenario_test
    async def test_drafts_from_dialog_saves_post_near_input(self, h):
        """/drafts сообщением из диалога: пост не теряется — уходит в черновики."""
        await h.start_dialog()
        await h.text("Пост для переживания")  # шаг 1 принят
        await h.text_from_below("/drafts")  # выход из диалога сообщением ниже

        self.assertIsNone(h.dialog_state())
        self.assertIn("черновик", h.screen_text())
        # пост не потерян пересозданием экрана — он в черновиках
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "Пост для переживания")

    @scenario_test
    async def test_reanchor_preserves_wip_payload(self, h):
        """Пересоздание экрана не трогает снимок WIP: «Продолжить пост» работает."""
        await h.start_dialog()
        await h.text("Заголовок переживания")  # снимок WIP в БД
        await h.restart()

        await h.text_from_below("/start")  # re-anchor: ref в БД очищен

        self.assertIn("Продолжить пост", str(h.screen_buttons()))
        await h.callback("restore_wip")
        self.assertEqual(h.user_data().get("title"), "Заголовок переживания")
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_navigation_below_after_restart(self, h):
        """После рестарта ref известен только БД — навигация сообщением работает."""
        await h.text("/start")
        old_screen = h.screen_message_id()

        await h.restart()
        await h.text_from_below("/start")

        self.assertIn("Выберите действие", h.screen_text())
        render = last_render(h, "Выберите действие")
        self.assertEqual(render["type"], "message")
        self.assertIn(old_screen, h.deleted_ids(), "протухший экран удалён")
        self.assertNotEqual(h.screen_message_id(), old_screen)

    @scenario_test
    async def test_wizard_input_does_not_reanchor(self, h):
        """Ввод полей визарда НЕ пересоздаёт экран (он правится на месте)."""
        await h.text("/start")
        await h.text("✏️ Создать пост")  # штатный вход: экран визарда
        old_screen = h.screen_message_id()

        await h.text("Заголовок поста")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        render = last_render(h, "Шаг 2 из 9")
        self.assertEqual(render["type"], "edit_message")
        self.assertNotIn(old_screen, h.deleted_ids())


if __name__ == "__main__":
    unittest.main()
