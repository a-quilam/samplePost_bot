# tests/test_stage12_reliability.py
"""
Stage 12, §3–§6: дефекты, найденные независимым аудитом Stage 11.

Каждый тест воспроизводит РЕАЛЬНЫЙ дефект (найденный чтением кода, а не
претензией к стилю) и фиксирует обязательный инвариант:

1. Прерванная отправка превью: частично отправленные временные сообщения
   не «осиротеют» — они запоминаются и удаляются при следующей замене
   (превью не накапливаются бесконечно).
2. Прерванная финальная отправка (длинный пост в несколько частей):
   повтор досылает ТОЛЬКО недостаающую часть — без дублей уже
   отправленного и без его удаления.
3. Рестарт на экране превью: превью до рестарта заменяется, а не копится
   (preview_messages входят в WIP-снимок).
4–5. Рестарт + прямой вход («Создать пост» / «Черновик N») БЕЗ /start:
   существующий экран редактируется, а не создаётся заново; ref в БД
   не затирается.

Тесты идут без сети, токена и настоящей БД (FakeBot + временный SQLite).
"""

import unittest

try:
    from handlers.post_creation import POST_CREATION, POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        call_text,
        goto_preview,
        is_post_message,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)

# Длинный текст: больше лимита Telegram (4000) — превью/пост из 2+ частей
LONG_TEXT = "Очень важное событие с разметкой. " * 140  # 4760 символов
LONG_TEXT_EDITED = "Совсем другая длинная строка события. " * 140


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PartialSendTests(unittest.TestCase):
    """§6: сбои посередине отправки — превью и готовый пост."""

    async def _to_long_preview(self, h) -> None:
        """Диалог до превью с длинным текстом (2 части превью)."""
        await h.goto_step(step_index("text"))
        await h.text(LONG_TEXT)
        for _ in range(len(POST_STEPS) - 1 - step_index("text")):
            await h.text("Пропустить")
        self.assertGreater(len(h.post_records()), 1, "длинный текст должен разбиться")

    @scenario_test
    async def test_partial_preview_failure_is_cleaned_next_replace(self, h):
        """1. Прерванное превью: частично отправленная часть не осиротеет."""
        await self._to_long_preview(h)
        preview_ids_before = list(h.user_data().get("preview_messages") or [])
        self.assertGreaterEqual(len(preview_ids_before), 2, "части превью запомнены")

        # Правка текста: новое превью падает на ВТОРОЙ части
        await h.callback("edit_post")
        await h.callback("edit_text")
        h.bot.fail_after["message"] = 1  # первая часть уйдёт, вторая сорвётся
        await h.text(LONG_TEXT_EDITED)

        partial_id = h.bot._next_message_id  # id частично отправленной части
        h.bot.fail_after.pop("message", None)

        # Следующее успешное превью должно убрать ВСЁ старое, включая часть
        # от прерванной отправки — иначе временные сообщения накапливаются
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Новый заголовок")

        self.assertIn(
            partial_id,
            h.deleted_ids(),
            "частично отправленное превью не удалено — осиротевшее временное "
            "сообщение накапливается (дефект Stage 12 A)",
        )
        for old_id in preview_ids_before:
            self.assertIn(old_id, h.deleted_ids(), "старое превью заменено")
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог жив")

    @scenario_test
    async def test_interrupted_final_send_resumes_without_duplicates(self, h):
        """2. Прерванный «Готово»: повтор досылает только недостаающее."""
        await self._to_long_preview(h)
        preview_texts = [call_text(c) for c in h.post_records()]
        self.assertGreaterEqual(len(preview_texts), 2, "превью из нескольких частей")

        h.bot.fail_after["message"] = 1
        await h.callback("finish")
        first_final_id = h.bot._next_message_id  # первая часть поста отправлена
        h.bot.fail_after.pop("message", None)

        # Сбой не врёт об успехе и не теряет данные
        self.assertIn("Не удалось отправить готовый пост", h.screen_text())
        self.assertTrue(h.user_data().get("text"), "данные поста на месте")
        state = h.user_states()[0]
        self.assertTrue(state.payload, "снимок WIP в БД цел")

        marker = len(h.calls)
        await h.callback("finish")  # повторная попытка

        self.assertIn("Пост готов.", h.screen_text())
        retry_texts = [call_text(c) for c in h.calls[marker:] if is_post_message(c)]
        self.assertEqual(
            retry_texts,
            preview_texts[1:],
            "повтор досылает ТОЛЬКО недостаающую часть (без дубля первой — "
            "дефект Stage 12 B)",
        )
        self.assertNotIn(
            first_final_id, h.deleted_ids(), "уже отправленная часть не удаляется"
        )
        self.assertIsNone(h.user_states()[0].payload, "пост завершён — снимок чист")
        self.assertEqual(h.dialog_state(), None, "цикл завершён")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RestartScreenTests(unittest.TestCase):
    """§5: рестарт — экран редактируется, превью и ref не теряются."""

    @scenario_test
    async def test_preview_from_before_restart_is_replaced(self, h):
        """3. Рестарт на превью: превью до рестарта удаляется при замене."""
        await goto_preview(h, title="Превью до рестарта")
        old_preview_id = h.bot._next_message_id  # единственная часть превью
        self.assertEqual(len(h.post_records()), 1)

        await h.restart()
        await h.callback("restore_wip")

        self.assertIn(
            old_preview_id,
            h.deleted_ids(),
            "превью до рестарта не заменено — временное сообщение остаётся "
            "навсегда (дефект Stage 12 C)",
        )
        self.assertIn("Пост готов к действию.", h.screen_text())
        self.assertEqual(len(h.post_records()), 2, "новое превью показано")
        self.assertEqual(h.dialog_state(), POST_CREATION)

    async def _draft_and_screen(self, h) -> int:
        """Черновик + запомненный ref экрана; возвращает id экрана в БД."""
        await goto_preview(h, title="Черновик для рестарта")
        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1)
        return h.user_states()[0].screen_message_id

    @scenario_test
    async def test_create_post_entry_reuses_screen_after_restart(self, h):
        """4. Рестарт + прямое «Создать пост» (без /start): тот же экран."""
        await h.start_dialog()
        old_screen_id = h.user_states()[0].screen_message_id
        sends_before = len([s for s in h.screens() if s["type"] == "message"])

        await h.restart()
        await h.text("✏️ Создать пост")

        sends_after = len([s for s in h.screens() if s["type"] == "message"])
        self.assertEqual(
            sends_after,
            sends_before,
            "после рестарта экран должен редактироваться, а не создаваться "
            "заново (дефект Stage 12 D)",
        )
        self.assertEqual(
            h.user_states()[0].screen_message_id,
            old_screen_id,
            "ref экрана в БД не должен затираться",
        )
        self.assertTrue(h.screen_text().startswith("Шаг 1 из 9"))

    @scenario_test
    async def test_open_draft_entry_reuses_screen_after_restart(self, h):
        """5. Рестарт + открытие черновика напрямую: тот же экран, без копий."""
        old_screen_id = await self._draft_and_screen(h)
        sends_before = len([s for s in h.screens() if s["type"] == "message"])

        await h.restart()
        await h.callback("editdraft_1")

        sends_after = len([s for s in h.screens() if s["type"] == "message"])
        self.assertEqual(
            sends_after,
            sends_before,
            "открытие черновика после рестарта не должно плодить экраны "
            "(дефект Stage 12 D)",
        )
        self.assertEqual(
            h.user_states()[0].screen_message_id,
            old_screen_id,
            "ref экрана в БД не должен затираться",
        )
        self.assertIn("Пост готов к действию.", h.screen_text())
        self.assertEqual(len(h.drafts()), 1, "черновик не задублирован")


if __name__ == "__main__":
    unittest.main()
