# tests/test_stage16_transitions.py
"""
Stage 16 (дефект №8): аудит целостности переходов между всеми 9 шагами.

Реальный инцидент (дефект №2 «пол двенадцатого»): пользователь ввёл
«пол двенадцатого» — бот не дал ВИДИМОГО перехода: экран оставался на
прежнем шаге (сообщение-экран существовало одно и уходило выше ввода).
Причины (Stage 16): не удалялся ввод, экран не пересоздавался у точки
ввода — теперь переход виден: следующий шаг рисуется рядом с вводом,
принятое сообщение убирается.

Аудит закрепляет для КАЖДОГО шага цепочки:
- значение нормализовано и сохранено в user_data;
- экран показывает СЛЕДУЮЩИЙ шаг («Шаг N+1 из 9 — <подпись>»);
- принятое сообщение ввода удалено (в чате нет мусора);
- состояние диалога не сломано;
плюс: назад/вперёд, рестарт посередине цепочки, изоляция диалога
после всех путей завершения.

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
    from utils.parsers import parse_time_flexible

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    """Индекс шага по ключу поля."""
    return next(i for i, step in enumerate(POST_STEPS) if step["key"] == key)


def step_screen_marker(step_position: int) -> str:
    """Маркер экрана шага: «Шаг N из 9 — <подпись>» (1-based позиция)."""
    step = POST_STEPS[step_position - 1]
    return f"Шаг {step_position} из {len(POST_STEPS)} — {step['label']}"


# Цепочка ввода: часть значений — «опасные» формы из инцидентов Stage 16
CHAIN = [
    ("title", "Аудит переходов", "Аудит переходов"),
    ("date", "17.11.2026", "17.11.2026"),
    ("time_start", "пол двенадцатого", "23:30"),
    ("time_end", "23,45", "23:45"),
    ("place_name", "Большой зал", "Большой зал"),
    ("text", "Проверяем переходы", "Проверяем переходы"),
    ("contact", "@audit_contact", "@audit_contact"),
    ("place_url", "https://example.com/audit", "https://example.com/audit"),
]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TransitionsAuditTests(unittest.TestCase):
    """Целостность цепочки 9 шагов."""

    @scenario_test
    async def test_full_chain_each_step_shows_next(self, h):
        """Каждый принятый шаг даёт видимый переход на следующий экран."""
        await h.start_dialog()
        for position, (key, typed, stored) in enumerate(CHAIN, start=1):
            with self.subTest(step=key, position=position):
                # экран текущего шага виден рядом с вводом
                self.assertIn(step_screen_marker(position), h.screen_text())

                sent = await h.text(typed)

                self.assertEqual(h.dialog_state(), POST_CREATION)
                self.assertEqual(h.user_data().get(key), stored, f"нормализация {key}")
                # ВИДИМЫЙ переход: следующий шаг показан сразу
                self.assertIn(step_screen_marker(position + 1), h.screen_text())
                # ввод убран из чатов — переход не «тонет» в мусоре
                self.assertIn(sent.message.message_id, h.deleted_ids())

        # изображение пропускается — достигнуто превью
        await h.text("Пропустить")
        self.assertIn("Пост готов к действию", h.screen_text())

        # превью и финальный пост совпадают
        preview = h.posts()[-1]
        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview)

    @scenario_test
    async def test_pol_dvenadtsatogo_gives_visible_transition(self, h):
        """Дефект №2: «пол двенадцатого» → 23:30 и видимый переход на шаг 4."""
        await h.goto_step(step_index("time_start"))

        await h.text("пол двенадцатого")

        self.assertEqual(h.user_data().get("time_start"), "23:30")
        self.assertIn(step_screen_marker(step_index("time_start") + 2), h.screen_text())
        # повторный ввод той же фразы на новом шаге — переход дальше
        await h.text("23,45")
        self.assertEqual(h.user_data().get("time_end"), "23:45")
        self.assertIn(step_screen_marker(step_index("time_end") + 2), h.screen_text())

    @scenario_test
    async def test_back_then_forward_keeps_chain_intact(self, h):
        """«Назад» по цепочке: экраны и данные согласованы, можно продолжить."""
        # ручная цепочка до времени окончания
        await h.start_dialog()
        await h.text("Аудит переходов")
        await h.text("17.11.2026")
        await h.text("пол двенадцатого")  # → время окончания

        await h.callback("back")  # → время начала
        self.assertIn(step_screen_marker(step_index("time_start") + 1), h.screen_text())
        self.assertEqual(h.user_data().get("time_start"), "23:30", "данные целы")

        await h.callback("back")  # → дата
        self.assertIn(step_screen_marker(step_index("date") + 1), h.screen_text())

        # продолжаем цепочку заново с даты
        await h.text("18.11.2026")
        self.assertIn(step_screen_marker(step_index("time_start") + 1), h.screen_text())
        await h.text("9,30")  # вечернее правило через запятую
        self.assertEqual(h.user_data().get("time_start"), "21:30")
        self.assertIn(step_screen_marker(step_index("time_end") + 1), h.screen_text())

    @scenario_test
    async def test_restart_mid_chain_resumes_at_same_step(self, h):
        """Рестарт посередине цепочки: тот же шаг, те же данные."""
        await h.start_dialog()
        await h.text("Аудит переходов")
        await h.text("17.11.2026")
        await h.text("пол двенадцатого")  # 23:30
        await h.text("23,45")  # → шаг «Место»

        await h.restart()
        await h.callback("restore_wip")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIn(step_screen_marker(step_index("place_name") + 1), h.screen_text())
        self.assertEqual(h.user_data().get("title"), "Аудит переходов")
        self.assertEqual(h.user_data().get("time_start"), "23:30")
        self.assertEqual(h.user_data().get("time_end"), "23:45")

        # цепочка продолжается после рестарта
        await h.text("Зал аудита")
        self.assertIn(step_screen_marker(step_index("text") + 1), h.screen_text())

    @scenario_test
    async def test_dialog_isolated_after_every_exit_path(self, h):
        """После всех путей завершения диалог не «висит»: подсказка, новый пост."""
        for exit_kind in ("cancel", "drafts", "start", "save_draft"):
            with self.subTest(exit=exit_kind):
                await goto_preview(h, title=f"Пост перед {exit_kind}")
                if exit_kind == "save_draft":
                    await h.callback("save_draft")  # кнопка на превью
                else:
                    await h.text(f"/{exit_kind}")  # команды внутри диалога
                self.assertIsNone(h.dialog_state())

                # обычный текст после завершения — подсказка, а не поле поста
                await h.text("просто текст")
                self.assertIn("Воспользуйтесь кнопками меню", h.screen_text())
                self.assertNotIn("title", h.user_data())

                # новый диалог работает изолированно
                await h.start_dialog()
                self.assertEqual(h.dialog_state(), POST_CREATION)
                self.assertIn(step_screen_marker(1), h.screen_text())
                await h.text(f"Чистый пост {exit_kind}")
                self.assertEqual(h.user_data().get("title"), f"Чистый пост {exit_kind}")

    @scenario_test
    async def test_parser_and_wizard_agree_on_time_forms(self, h):
        """Нормализация времени: визард хранит ровно то, что даёт парсер."""
        cases = ["пол двенадцатого", "9,30", "1830", "7 30", "12,30", "22:15"]
        await h.goto_step(step_index("time_start"))
        for typed in cases:
            with self.subTest(typed=typed):
                await h.text(typed)
                self.assertEqual(
                    h.user_data().get("time_start"),
                    parse_time_flexible(typed),
                    f"визард и парсер разошлись на «{typed}»",
                )
                await h.callback("back")  # вернуться на шаг времени начала


if __name__ == "__main__":
    unittest.main()
