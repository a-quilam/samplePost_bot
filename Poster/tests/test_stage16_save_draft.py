# tests/test_stage16_save_draft.py
"""
Stage 16 (дефект №6): сохранение в черновики — видимый результат и рабочие
следующие действия.

Реальный инцидент: после «Сохранить в черновики» пользователь не видел
результата (сообщение-экран уходило выше по чату), а кнопки под превью
отвечали «устарело» — создавалось ощущение, что сохранение не сработало.

Правила (Stage 16):
- после подтверждённого сохранения виден экран успеха («Черновик сохранён»)
  с кнопками следующих действий — «Черновики», «Создать пост», помощь;
  каждая кнопка действительно работает (глобальные обработчики);
- черновик сохраняется ровно один раз: повторный callback устаревший
  и не создаёт дубль, не блокирует новый диалог;
- при ошибке SQLite НЕ показывается успех, данные не теряются;
- сценарий создания завершается корректно (состояние None, данные поста
  сброшены, ref экрана сохранён для дальнейшей навигации).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import POST_CREATION
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


class _BrokenSession:
    """Сессия БД, на которой коммит падает (имитация сбоя SQLite)."""

    def __init__(self):
        self._real = None

    def get(self, *args, **kwargs):
        raise RuntimeError("имитация сбоя SQLite")

    def query(self, *args, **kwargs):
        raise RuntimeError("имитация сбоя SQLite")

    def add(self, *args, **kwargs):
        raise RuntimeError("имитация сбоя SQLite")

    def commit(self):
        raise RuntimeError("имитация сбоя SQLite")

    def rollback(self):
        pass

    def close(self):
        pass


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SaveDraftScreenTests(unittest.TestCase):
    """Экран успеха после сохранения в черновики."""

    @scenario_test
    async def test_success_screen_with_working_actions(self, h):
        """Экран успеха: кнопки «Черновики» и «Создать пост» действительно работают."""
        await goto_preview(h, title="Готовый пост")
        await h.callback("save_draft")

        self.assertIn("Пост сохранён в черновики", h.screen_text())
        buttons = h.screen_buttons()
        self.assertIn(("📂 Черновики", "drafts"), buttons)
        self.assertIn(("✏️ Создать пост", "create_post"), buttons)

        # «Черновики» после сохранения — список реально показан
        await h.callback("drafts")
        self.assertIsNone(h.dialog_state())
        self.assertIn("Ваши черновики", h.screen_text())

        # «Создать пост» после сохранения — новый диалог реально начинается
        await h.callback("create_post")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn("Заголовок", h.screen_text())

    @scenario_test
    async def test_help_action_works_after_save(self, h):
        """Кнопка помощи на экране успеха работает."""
        await goto_preview(h, title="Пост")
        await h.callback("save_draft")

        await h.callback("help")

        self.assertIsNone(h.dialog_state())
        # справка реально показана на экране
        self.assertIn("Как работать", h.screen_text())

    @scenario_test
    async def test_repeated_save_saves_exactly_once(self, h):
        """Повторный callback сохранения: дубля нет, ответ честный."""
        await goto_preview(h, title="Один раз")
        await h.callback("save_draft")
        drafts_before = len(h.drafts())
        self.assertEqual(drafts_before, 1)

        await h.callback("save_draft")

        self.assertEqual(len(h.drafts()), drafts_before, "дубликата быть не должно")
        self.assertIsNone(h.dialog_state())
        self.assertIn("устарела", h.answers()[-1]["text"])

    @scenario_test
    async def test_new_dialog_after_save_is_independent(self, h):
        """После сохранения новый диалог работает и создаёт второй черновик."""
        await goto_preview(h, title="Первый")
        await h.callback("save_draft")

        await h.start_dialog()
        await h.text("Второй пост")
        for _ in range(7):  # остальные шаги пропускаем до превью
            await h.text("Пропустить")
        await h.callback("save_draft")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 2, "каждый диалог — свой черновик")
        titles = {draft.title for draft in drafts}
        self.assertEqual(titles, {"Первый", "Второй пост"})

    @scenario_test
    async def test_db_error_shows_failure_and_keeps_data(self, h):
        """Ошибка БД: нет ложного успеха, данные целы, повтор срабатывает."""
        import handlers.post_creation as post_creation_module

        await goto_preview(h, title="Ценный пост")

        original = post_creation_module.SessionLocal
        post_creation_module.SessionLocal = lambda: _BrokenSession()
        try:
            await h.callback("save_draft")
        finally:
            post_creation_module.SessionLocal = original

        self.assertIn("ошибка", h.screen_text().lower())
        self.assertNotIn("сохранён", h.screen_text().lower())
        self.assertEqual(len(h.drafts()), 0)
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог жив")
        self.assertEqual(h.user_data().get("title"), "Ценный пост", "данные целы")

        # БД вернулась: повторное сохранение работает
        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1)
        self.assertIn("Пост сохранён в черновики", h.screen_text())

    @scenario_test
    async def test_state_and_data_cleared_after_save(self, h):
        """Сценарий завершён корректно: состояние None, данные поста сброшены."""
        await goto_preview(h, title="На проверку")
        await h.callback("save_draft")

        self.assertIsNone(h.dialog_state())
        self.assertNotIn("title", h.user_data())
        self.assertNotIn("photos", h.user_data())
        # ref экрана сохранён — навигация продолжает работать
        self.assertIsNotNone(h.screen_message_id())

    @scenario_test
    async def test_empty_post_reports_and_keeps_dialog(self, h):
        """Пустой пост: честный отказ, черновик не создаётся."""
        await h.start_dialog()
        for _ in range(9):  # все девять шагов пропускаем
            await h.text("Пропустить")
        await h.callback("save_draft")

        self.assertIn("Пост пуст", h.screen_text())
        self.assertEqual(len(h.drafts()), 0)
        self.assertEqual(h.dialog_state(), POST_CREATION)


if __name__ == "__main__":
    unittest.main()
