# tests/test_stage16_input_cleanup.py
"""
Stage 16 (дефект №1): неудачный ввод не должен оставаться в чате навсегда.

Реальный инцидент: пользователь ввёл несколько некорректных дат — бот
показывал ошибку, но сообщения с ошибочным вводом оставались в чате
ПОСЛЕ того, как поле было введено корректно.

Правила (Stage 16):
- пока ввод неправильный, сообщение остаётся (пользователь видит, что
  не получилось), а его message_id запоминается в состоянии сценария;
- после того, как значение принято и надёжно сохранено (снимок WIP в БД),
  удаляются ВСЕ неудачные сообщения ЭТОГО поля + сам успешный ввод;
- при сбое сохранения состояния ничего не удаляется преждевременно;
- сообщения, не относящиеся к текущему сценарию, не трогаются;
- учтены: несколько ошибок, «Назад», пропуск поля, рестарт процесса;
- отказ Telegram при удалении не ломает диалог (best-effort).

Проверяется через настоящий Application.process_update (integration_harness).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import POST_CREATION, POST_STEPS
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    """Индекс шага по ключу поля."""
    return next(i for i, step in enumerate(POST_STEPS) if step["key"] == key)


class _BrokenSession:
    """Сессия БД, на которой любая операция падает (имитация сбоя SQLite)."""

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
class FailedInputCleanupTests(unittest.TestCase):
    """Очистка неудачного ввода после принятия корректного значения."""

    @scenario_test
    async def test_invalid_inputs_deleted_after_field_accepted(self, h):
        """Несколько ошибок → корректный ввод: всё неудачное удаляется."""
        await h.goto_step(step_index("date"))

        bad_first = await h.text("17.11.2026-г")
        bad_second = await h.text("не дата")
        self.assertNotIn(bad_first.message.message_id, h.deleted_ids())
        self.assertNotIn(bad_second.message.message_id, h.deleted_ids())

        good = await h.text("17.11.2026")

        self.assertIn(bad_first.message.message_id, h.deleted_ids())
        self.assertIn(bad_second.message.message_id, h.deleted_ids())
        self.assertIn(good.message.message_id, h.deleted_ids())
        # Поле принято: следующий шаг показан
        self.assertIn("Время начала", h.screen_text())

    @scenario_test
    async def test_invalid_input_kept_while_still_invalid(self, h):
        """Пока значение не принято, неудачные сообщения остаются в чате."""
        await h.goto_step(step_index("date"))

        bad = await h.text("вчера ночью")

        self.assertNotIn(bad.message.message_id, h.deleted_ids())
        self.assertIn("Некорректный формат", h.screen_text())

    @scenario_test
    async def test_no_deletion_when_state_save_fails(self, h):
        """Сбой БД: значение не подтверждено → ничего не удаляется."""
        import utils.screen as screen_module

        await h.goto_step(step_index("date"))
        bad = await h.text("17.11.2026-г")

        original = screen_module.SessionLocal
        screen_module.SessionLocal = lambda: _BrokenSession()
        try:
            good = await h.text("17.11.2026")
        finally:
            screen_module.SessionLocal = original

        self.assertNotIn(good.message.message_id, h.deleted_ids())
        self.assertNotIn(bad.message.message_id, h.deleted_ids())

        # БД вернулась: поле принимается заново («Назад» → дата) — тогда
        # неудачные попытки поля уходят в очистку
        await h.callback("back")  # вернулись на дату
        good_again = await h.text("17.11.2026")
        self.assertIn(good.message.message_id, h.deleted_ids())
        self.assertIn(bad.message.message_id, h.deleted_ids())
        self.assertIn(good_again.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_back_and_fix_clears_failed_attempts(self, h):
        """«Назад» + исправление: неудачные сообщения поля убираются."""
        await h.goto_step(step_index("date"))
        bad = await h.text("17.11.2026-г")

        await h.callback("back")  # вернулись на заголовок
        await h.text("Заголовок")
        self.assertNotIn(bad.message.message_id, h.deleted_ids())

        await h.text("17.11.2026")  # дата исправлена
        self.assertIn(bad.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_skip_clears_failed_attempts_of_field(self, h):
        """Пропуск поля — его решение: неудачные попытки поля убираются."""
        await h.goto_step(step_index("date"))
        bad = await h.text("17.11.2026-г")

        await h.text("Пропустить")

        self.assertIn(bad.message.message_id, h.deleted_ids())
        self.assertIn("Время начала", h.screen_text())

    @scenario_test
    async def test_restart_keeps_failed_tracking(self, h):
        """Рестарт: идентификаторы неудачного ввода переживают перезапуск."""
        # непустой пост → снимок WIP с трекингом реально сохраняется
        await h.start_dialog()
        await h.text("Заголовок для проверки")  # шаг 1 принят → дата
        bad = await h.text("17.11.2026-г")

        await h.restart()
        # после рестарта диалога нет: пользователь продолжает WIP
        await h.callback("restore_wip")

        await h.text("17.11.2026")
        self.assertIn(bad.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_foreign_messages_are_not_deleted(self, h):
        """Сообщения вне текущего сценария не удаляются чужой очисткой."""
        stray = await h.text("просто сообщение мимо бота")  # вне диалога

        await h.goto_step(step_index("date"))
        await h.text("17.11.2026-г")
        await h.text("17.11.2026")

        self.assertNotIn(stray.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_scenario_end_keeps_unresolved_attempts(self, h):
        """Выход из сценария без принятия поля: неудачное остаётся в чате."""
        await h.goto_step(step_index("date"))
        bad = await h.text("17.11.2026-г")

        await h.text("/cancel")

        self.assertNotIn(bad.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_delete_failure_does_not_break_dialog(self, h):
        """Отказ Telegram при удалении — best-effort, диалог продолжается."""
        await h.goto_step(step_index("date"))
        await h.text("17.11.2026-г")

        h.bot.fail_types.add("delete_message")
        try:
            await h.text("17.11.2026")
        finally:
            h.bot.fail_types.discard("delete_message")

        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог жив")
        self.assertIn("Время начала", h.screen_text())

    @scenario_test
    async def test_edit_mode_invalid_input_cleaned(self, h):
        """Правка поля на превью: неудачный ввод убирается после принятия."""
        await h.goto_step(step_index("date"))
        await h.text("17.11.2026")
        # пропуски оставшихся шагов (до изображения включительно) → превью
        for _ in range(len(POST_STEPS) - step_index("date") - 1):
            await h.text("Пропустить")

        await h.callback("edit_post")
        await h.callback("edit_date")
        bad = await h.text("не дата")
        await h.text("18.11.2026")

        self.assertIn(bad.message.message_id, h.deleted_ids())
        self.assertIn("18.11.2026", h.posts()[-1] or "")


if __name__ == "__main__":
    unittest.main()
