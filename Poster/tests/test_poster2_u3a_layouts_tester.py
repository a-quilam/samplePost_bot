# tests/test_poster2_u3a_layouts_tester.py
"""
U3a №13: независимые проверки тестировщика раскладок клавиатур.

Проверяется требование «одна раскладка — одно место», без опоры на
внутреннее устройство модуля разработчика:

1. Шаблоны `utils.keyboard_layouts` дают предсказуемую геометрию:
   полные строки, неполная последняя, отсутствие пустых слотов из `None`,
   пагинация одной строкой, опасное действие отдельной строкой,
   `per_row <= 0` — ошибка, а не «тихая» одна кнопка в строке.
2. На реальных экранах раскладка совпадает с шаблоном:
   шаг фото — одна строка «Назад | Пропустить»;
   хаб кирпичиков — по паре «название + 📨» на строку;
   дни календаря — по четыре кнопки в строке.
"""

from __future__ import annotations

import unittest
from datetime import date

from utils import calendar_view, keyboard_layouts

try:
    from handlers import wizard_photos
    from handlers.wizard_questions import CALENDAR_DAYS_PER_ROW, day_rows
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина уходит в skip-сообщение
    IMPORT_ERROR = str(exc)


def b(label: str, data: str | None = None):
    return keyboard_layouts.button(label, data if data is not None else label)


class LayoutGeometryTesterTests(unittest.TestCase):
    """Геометрия шаблонов: то, что видит человек, а не структура кода."""

    def test_full_and_short_rows(self) -> None:
        rows = keyboard_layouts.rows_of([b(str(i)) for i in range(7)], 3)
        self.assertEqual(
            [[button.text for button in row] for row in rows],
            [["0", "1", "2"], ["3", "4", "5"], ["6"]],
        )

    def test_one_item_per_row_is_allowed(self) -> None:
        rows = keyboard_layouts.rows_of([b("a"), b("b")], 1)
        self.assertEqual(
            [[button.text for button in row] for row in rows], [["a"], ["b"]]
        )

    def test_zero_and_negative_raise(self) -> None:
        for bad in (0, -3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                keyboard_layouts.rows_of([b("x")], bad)

    def test_none_never_creates_empty_slot(self) -> None:
        rows = keyboard_layouts.rows_of([None, b("a"), None, b("b")], 2)
        self.assertEqual(
            [[button.text for button in row] for row in rows], [["a", "b"]]
        )

        pair = keyboard_layouts.pair(None, b("right"))
        self.assertEqual([[button.text for button in row] for row in pair], [["right"]])

        nav = keyboard_layouts.nav_row(None, None)
        self.assertEqual(nav, [])

    def test_pair_keeps_order(self) -> None:
        rows = keyboard_layouts.pair(b("◀️ Назад", "back"), b("⏭ Пропустить", "skip"))
        self.assertEqual(
            [(button.text, button.callback_data) for button in rows[0]],
            [("◀️ Назад", "back"), ("⏭ Пропустить", "skip")],
        )

    def test_list_rows_one_object_per_row(self) -> None:
        rows = keyboard_layouts.list_rows(
            [(b("one", "o1"), b("📨", "p1")), (b("two", "o2"), None)]
        )
        self.assertEqual(
            [[button.callback_data for button in row] for row in rows],
            [["o1", "p1"], ["o2"]],
        )

    def test_list_rows_without_main_skips_the_row(self) -> None:
        rows = keyboard_layouts.list_rows(
            [(None, b("📨", "p1")), (b("ok", "o1"), None)]
        )
        self.assertEqual(
            [[button.callback_data for button in row] for row in rows], [["o1"]]
        )

    def test_nav_row_is_one_row_with_both_sides(self) -> None:
        rows = keyboard_layouts.nav_row(b("◀️", "prev"), b("▶️", "next"))
        self.assertEqual(len(rows), 1)
        self.assertEqual([button.callback_data for button in rows[0]], ["prev", "next"])

    def test_danger_row_is_alone(self) -> None:
        rows = keyboard_layouts.danger_row(b("🗑 Отозвать", "del"))
        self.assertEqual(len(rows), 1)
        self.assertEqual([button.callback_data for button in rows[0]], ["del"])
        self.assertEqual(keyboard_layouts.danger_row(None), [])


class LayoutOnScreenTesterTests(unittest.TestCase):
    """Раскладки реальных экранов, собранные теми же шаблонами."""

    def test_photo_step_is_single_pair_row(self) -> None:
        from types import SimpleNamespace

        markup = wizard_photos.photo_step_keyboard(
            SimpleNamespace(draft_id=5, revision=2)
        )
        matrix = [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]
        self.assertEqual(
            matrix,
            [
                [
                    ("◀️ Назад", "aw_photo_back:5:2"),
                    ("⏭ Пропустить", "aw_photo_none:5:2"),
                ]
            ],
        )

    def test_calendar_days_use_four_per_row(self) -> None:
        days = [date(2026, 10, day) for day in range(1, 10)]
        rows = day_rows(days, "2026-10")
        self.assertEqual(len(rows), 3, f"строки: {len(rows)}")
        self.assertEqual([len(row) for row in rows], [4, 4, 1])
        self.assertEqual(CALENDAR_DAYS_PER_ROW, 4)
        self.assertEqual(
            rows[0][0].callback_data,
            "aw_calday:2026-10:2026-10-01",
        )
        self.assertEqual(rows[0][0].text, calendar_view.format_quick_day(days[0]))


@unittest.skipIf(
    IMPORT_ERROR is not None, f"интеграционное окружение недоступно: {IMPORT_ERROR}"
)
class HubLayoutTesterTests(unittest.TestCase):
    """Хаб: каждая строка кирпичика — ровно пара «название + 📨»."""

    @scenario_test
    async def test_hub_brick_rows_are_pairs(self, h: Harness) -> None:
        from brick_store import create_personal_brick
        from user_store import get_or_create_user

        with h.session_factory() as session:
            get_or_create_user(session, 2_160_401, username="u401", full_name="Юзер")
            brick = create_personal_brick(
                session,
                actor_telegram_user_id=2_160_401,
                code="т_раскладка",
                title="Раскладка",
                value_type="short_text",
            )
        await h.text("/bricks", user_id=2_160_401)
        record = h.screen()
        assert record is not None
        markup = record["reply_markup"]
        matrix = [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]
        brick_rows = [
            row
            for row in matrix
            if any(d and d.startswith("bp_start:") for _l, d in row)
        ]
        self.assertEqual(len(brick_rows), 1, f"строки: {matrix}")
        self.assertEqual(
            brick_rows[0],
            [
                ("Раскладка — [т_раскладка]", f"bk_open:{brick.id}"),
                ("📨", f"bp_start:{brick.id}"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
