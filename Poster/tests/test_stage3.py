# tests/test_stage3.py
"""
Тесты безопасного HTML-экранирования (utils/formatter.py).

Единая отправка и сводка легаси-поста удалены вместе с мастером (S5):
доставка Poster 2.0 живёт в delivery.py и покрыта
tests/test_poster2_delivery.py. Здесь остаются только чистые функции
форматирования пользовательского текста, которые использует новый код.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

from utils.formatter import escape_markdown, format_text, replace_quotes


class FormatterGuardTests(unittest.TestCase):
    """Защита от возврата двойного экранирования (анализ БД: затронутых записей — 0)."""

    def test_format_text_does_not_add_backslashes(self):
        formatted = format_text('Встреча "в 12.00" - вход свободный')
        self.assertNotIn("\\", formatted)

    def test_escape_applied_once_at_render(self):
        self.assertEqual(escape_markdown("a_b"), "a\\_b")
        self.assertEqual(escape_markdown("5.00"), "5\\.00")


class QuoteTypoTests(unittest.TestCase):
    """B7: replace_quotes — все пары кавычек последовательно превращаются в «ёлочки»."""

    def test_single_pair(self):
        self.assertEqual(
            replace_quotes('Встреча "в 12.00" начнётся'),
            "Встреча «в 12.00» начнётся",
        )

    def test_multiple_pairs_all_converted(self):
        self.assertEqual(
            replace_quotes('"Первая" и "Вторая"'),
            "«Первая» и «Вторая»",
        )

    def test_three_pairs(self):
        self.assertEqual(
            replace_quotes('"раз", "два", "три"'),
            "«раз», «два», «три»",
        )

    def test_odd_quotes_alternate(self):
        # нечётное число кавычек: последовательность не сбивается
        self.assertEqual(replace_quotes('"а" и "б'), "«а» и «б")

    def test_text_without_quotes_unchanged(self):
        self.assertEqual(replace_quotes("Без кавычек"), "Без кавычек")


if __name__ == "__main__":
    unittest.main()
