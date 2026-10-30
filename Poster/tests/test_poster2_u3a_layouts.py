# tests/test_poster2_u3a_layouts.py
"""
Poster 2.2 U3a, дефект №13: общие шаблоны раскладки inline-клавиатур.

Проверяется:

- `utils.keyboard_layouts` как самостоятельный модуль: нарезка строк
  (включая неполную последнюю), `per_row <= 0` → `ValueError`, `None`
  не оставляет пустых слотов, `pair`/`nav_row`/`danger_row`/`list_rows`
  ведут себя одинаково при полном и частичном наборе кнопок;
- матрица реальных экранов: шаг фото — ровно одна строка
  «◀️ Назад | ⏭ Пропустить», список кирпичиков — по строке на кирпичик
  («название + 📨»), пагинация — одной строкой.

Шаблоны чистые: бизнес-логики в них нет, поэтому юнит-часть не требует
приложения. Матрица экранов идёт через `build_application`, FakeBot и
временную SQLite.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from utils import keyboard_layouts

try:
    from brick_store import create_personal_brick
    from handlers import wizard_photos
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OWNER_TG = 2_160_101


def _btn(label: str, data: str | None = None):
    return keyboard_layouts.button(label, data if data is not None else label)


def _labels(rows: list[list[Any]]) -> list[list[str]]:
    """Текстовая матрица клавиатуры: строки → подписи кнопок."""
    return [[button.text for button in row] for row in rows]


def _datas(rows: list[list[Any]]) -> list[list[str | None]]:
    return [[button.callback_data for button in row] for row in rows]


class KeyboardLayoutsUnitTests(unittest.TestCase):
    """Юнит-контракт шаблонов: без БД, сети и приложения."""

    def test_rows_of_splits_evenly(self) -> None:
        rows = keyboard_layouts.rows_of([_btn(f"b{index}") for index in range(8)], 4)
        self.assertEqual([len(row) for row in rows], [4, 4])
        self.assertEqual(_labels(rows)[0], ["b0", "b1", "b2", "b3"])

    def test_rows_of_keeps_short_last_row(self) -> None:
        rows = keyboard_layouts.rows_of([_btn(f"b{index}") for index in range(5)], 3)
        self.assertEqual([len(row) for row in rows], [3, 2])
        self.assertEqual(_labels(rows)[1], ["b3", "b4"])

    def test_rows_of_single_item(self) -> None:
        rows = keyboard_layouts.rows_of([_btn("only")], 2)
        self.assertEqual(_labels(rows), [["only"]])

    def test_rows_of_empty_input_has_no_rows(self) -> None:
        self.assertEqual(keyboard_layouts.rows_of([], 4), [])

    def test_rows_of_rejects_non_positive_per_row(self) -> None:
        for per_row in (0, -1, -12):
            with self.subTest(per_row=per_row):
                with self.assertRaises(ValueError):
                    keyboard_layouts.rows_of([_btn("x")], per_row)

    def test_rows_of_skips_none_without_empty_slots(self) -> None:
        rows = keyboard_layouts.rows_of([_btn("a"), None, _btn("b"), None], 2)
        self.assertEqual(_labels(rows), [["a", "b"]])

    def test_rows_of_accepts_generator(self) -> None:
        rows = keyboard_layouts.rows_of((_btn(f"g{index}") for index in range(3)), 2)
        self.assertEqual(_labels(rows), [["g0", "g1"], ["g2"]])

    def test_pair_is_single_row(self) -> None:
        rows = keyboard_layouts.pair(_btn("left"), _btn("right"))
        self.assertEqual(_labels(rows), [["left", "right"]])

    def test_pair_drops_missing_side(self) -> None:
        self.assertEqual(_labels(keyboard_layouts.pair(_btn("left"), None)), [["left"]])
        self.assertEqual(
            _labels(keyboard_layouts.pair(None, _btn("right"))), [["right"]]
        )

    def test_pair_of_nothing_is_empty(self) -> None:
        self.assertEqual(keyboard_layouts.pair(None, None), [])

    def test_list_rows_keeps_action_on_the_right(self) -> None:
        rows = keyboard_layouts.list_rows(
            [(_btn("Кирпичик"), _btn("📨")), (_btn("Второй"), _btn("📨"))]
        )
        self.assertEqual(_labels(rows), [["Кирпичик", "📨"], ["Второй", "📨"]])

    def test_list_rows_without_action_has_one_button(self) -> None:
        rows = keyboard_layouts.list_rows([(_btn("Архивный"), None)])
        self.assertEqual(_labels(rows), [["Архивный"]])

    def test_list_rows_skips_row_without_main_button(self) -> None:
        rows = keyboard_layouts.list_rows([(None, _btn("📨")), (_btn("Живой"), None)])
        self.assertEqual(_labels(rows), [["Живой"]])

    def test_list_rows_empty_input(self) -> None:
        self.assertEqual(keyboard_layouts.list_rows([]), [])

    def test_nav_row_both_sides(self) -> None:
        rows = keyboard_layouts.nav_row(_btn("◀️", "p0"), _btn("▶️", "p8"))
        self.assertEqual(_datas(rows), [["p0", "p8"]])

    def test_nav_row_single_side_and_empty(self) -> None:
        self.assertEqual(_labels(keyboard_layouts.nav_row(None, _btn("▶️"))), [["▶️"]])
        self.assertEqual(_labels(keyboard_layouts.nav_row(_btn("◀️"), None)), [["◀️"]])
        self.assertEqual(keyboard_layouts.nav_row(None, None), [])

    def test_danger_row_is_its_own_row(self) -> None:
        self.assertEqual(
            _labels(keyboard_layouts.danger_row(_btn("🗑 Отозвать"))), [["🗑 Отозвать"]]
        )
        self.assertEqual(keyboard_layouts.danger_row(None), [])

    def test_button_builds_inline_button(self) -> None:
        built = keyboard_layouts.button("📨", "bp_start:7")
        self.assertEqual((built.text, built.callback_data), ("📨", "bp_start:7"))

    def test_helpers_do_not_mutate_input_rows(self) -> None:
        left, right = _btn("left"), _btn("right")
        rows = keyboard_layouts.pair(left, right)
        rows[0].append(_btn("extra"))
        self.assertEqual((left.text, right.text), ("left", "right"))


class PhotoStepMatrixTests(unittest.TestCase):
    """Матрица шага фото: ровно «◀️ Назад | ⏭ Пропустить» одной строкой."""

    def test_photo_step_keyboard_matrix(self) -> None:
        markup = wizard_photos.photo_step_keyboard(
            SimpleNamespace(draft_id=11, revision=3)
        )
        rows = [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]
        self.assertEqual(
            rows,
            [
                [
                    ("◀️ Назад", "aw_photo_back:11:3"),
                    ("⏭ Пропустить", "aw_photo_none:11:3"),
                ]
            ],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrickListMatrixTests(unittest.TestCase):
    """Матрица хаба: строка кирпичика = «название + 📨», пагинация отдельно."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [patch("config.SUPERADMIN_USER_IDS", ())]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_personal(self, h: Harness, code: str, title: str):
        with h.session_factory() as session:
            get_or_create_user(
                session,
                OWNER_TG,
                username="owner2160",
                full_name="Владелец 2160",
            )
            return create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code=code,
                title=title,
                value_type="short_text",
            )

    def keyboard_rows(self, h: Harness) -> list[list[tuple[str, str | None]]]:
        record = h.screen()
        assert record is not None, "экрана ещё не было"
        markup = record.get("reply_markup")
        assert markup is not None, "у экрана нет клавиатуры"
        return [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]

    @scenario_test
    async def test_hub_rows_are_name_action_pairs(self, h: Harness) -> None:
        first = self.make_personal(h, "раскладка_1", "Первый")
        second = self.make_personal(h, "раскладка_2", "Второй")
        await h.text("/bricks", user_id=OWNER_TG)
        rows = self.keyboard_rows(h)

        brick_rows = [
            row
            for row in rows
            if any("bp_start" in (data or "") for _label, data in row)
        ]
        self.assertEqual(len(brick_rows), 2, f"ожидались две строки кирпичиков: {rows}")
        for row in brick_rows:
            self.assertEqual(len(row), 2, f"строка должна быть парой: {row}")
            self.assertEqual(row[1][0], "📨")
        self.assertEqual(
            [data for _label, data in brick_rows[0]][1], f"bp_start:{first.id}"
        )
        self.assertEqual(
            [data for _label, data in brick_rows[1]][1], f"bp_start:{second.id}"
        )

    @scenario_test
    async def test_hub_has_no_shared_propose_button(self, h: Harness) -> None:
        self.make_personal(h, "раскладка_3", "Третий")
        await h.text("/bricks", user_id=OWNER_TG)
        datas = [data for row in self.keyboard_rows(h) for _label, data in row]
        self.assertNotIn("bp_start", datas, "общая кнопка предложения вернулась")
        self.assertTrue(
            [data for data in datas if (data or "").startswith("bp_start:")],
            "в строке кирпичика нет кнопки предложения",
        )


if __name__ == "__main__":
    unittest.main()
