# tests/test_stage16_time_comma.py
"""
Stage 16 (дефект №3): время с запятой как разделитель «ЧЧ,ММ».

Реальный инцидент: пользователь вводил «21,30» — бот отвечал
«Некорректный формат», хотя часы с двоеточием и точкой принимались.
Разделитель уже произвольен в остальных формах (ЧЧ:ММ, ЧЧ.ММ, ЧЧММ,
«ЧЧ ММ») — запятая добавляется тем же правилом.

Опасные формы с запятой остаются отказами: «1,2», «18,3», «18,300»,
«18,,30», «в18,30» — произвольная строка с запятыми часами не становится.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import POST_STEPS
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from utils.parsers import parse_time_flexible

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
class TimeCommaParserTest(unittest.TestCase):
    """Запятая — равноправный разделитель часов и минут."""

    def assert_time(self, text: str, expected: str) -> None:
        self.assertEqual(parse_time_flexible(text), expected, text)

    def test_comma_separator_follows_evening_rule(self):
        """«ЧЧ,ММ» подчиняется единому вечернему правилу — как ЧЧ:ММ."""
        self.assert_time("18,30", "18:30")
        self.assert_time("7,30", "19:30")
        self.assert_time("9,30", "21:30")
        self.assert_time("12,30", "12:30")
        self.assert_time("0,30", "00:30")
        self.assert_time("23,59", "23:59")

    def test_comma_with_explicit_period(self):
        """Явный период имеет приоритет — как у остальных разделителей."""
        self.assert_time("9,30 утра", "09:30")
        self.assert_time("12,30 ночи", "00:30")
        self.assert_time("6,15 вечера", "18:15")

    def test_other_separators_still_work(self):
        """Поддержка двоеточия, точки, компактной и пробельной форм сохранена."""
        self.assert_time("18:30", "18:30")
        self.assert_time("18.30", "18:30")
        self.assert_time("1830", "18:30")
        self.assert_time("18 30", "18:30")
        self.assert_time("пол двенадцатого", "23:30")

    def test_dangerous_comma_forms_stay_invalid(self):
        """Произвольная строка с запятыми часами не становится."""
        for text in ("1,2", "18,3", "18,300", "18,,30", "в18,30", "18,30,45", ",30", "18,"):
            self.assertIsNone(parse_time_flexible(text), text)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TimeCommaWizardTests(unittest.TestCase):
    """Визард принимает время с запятой и сохраняет нормализованно."""

    @scenario_test
    async def test_time_start_with_comma_accepted(self, h):
        """«18,30» на шаге времени начала → 18:30 в состоянии и на экране."""
        await h.goto_step(step_index("time_start"))
        await h.text("18,30")

        self.assertEqual(h.user_data().get("time_start"), "18:30")
        self.assertIn("Время окончания", h.screen_text())

    @scenario_test
    async def test_evening_rule_with_comma(self, h):
        """«9,30» → вечернее правило → 21:30 (как «9:30»)."""
        await h.goto_step(step_index("time_start"))
        await h.text("9,30")

        self.assertEqual(h.user_data().get("time_start"), "21:30")

    @scenario_test
    async def test_invalid_comma_form_reports_error(self, h):
        """«1,2» — ошибка формата; экран шага остаётся, ввод не удалён."""
        await h.goto_step(step_index("time_start"))
        bad = await h.text("1,2")

        self.assertIn("Некорректный формат", h.screen_text())
        self.assertIn("Время начала", h.screen_text())
        self.assertIsNone(h.user_data().get("time_start"))
        self.assertNotIn(bad.message.message_id, h.deleted_ids())


if __name__ == "__main__":
    unittest.main()
