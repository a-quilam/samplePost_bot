# tests/test_poster2_u2a_calendar.py
"""
Poster 2.2 U2a: компактный календарь даты (исправление №3).

Проверяется требуемое владельцем поведение:
- первый экран даты — inline-календарь сразу на вопросе;
- ровно восемь быстрых дат (сегодня + 7), скользящее окно;
- два семидневных диапазона сразу после окна, ровно в одной строке;
- нажатие диапазона раскрывает его 7 дат на месте, есть возврат к быстрым;
- «📅 Выбрать месяц» → сетка 12 месяцев → дни месяца сеткой, стрелки в
  пределах горизонта;
- выбор даты без подтверждения; пропуск и назад;
- старые callback (aw_calroot/aw_calweek/aw_calother) безопасны;
- СОСТАВ И РАСПОЛОЖЕНИЕ строк inline-клавиатуры (матрица).

Все сценарии идут через настоящее приложение, FakeBot и временный SQLite.

Запуск из каталога Poster:
    python -m unittest tests.test_poster2_u2a_calendar -v
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

try:
    from announcement_store import build_announcement_fields, list_announcement_drafts
    from brick_store import create_personal_brick
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        frozen_today,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import calendar_view

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

# Пример владельца: сегодня 29.10.2026.
TODAY = date(2026, 10, 29)

QUICK_LABELS = [
    "29.10 чт",
    "30.10 пт",
    "31.10 сб",
    "01.11 вс",
    "02.11 пн",
    "03.11 вт",
    "04.11 ср",
    "05.11 чт",
]


def _seed(h, *, day_rules: dict | None = None) -> int:
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="u2a")
        for code, title, value_type, rules in (
            ("title", "Заголовок", "short_text", {}),
            ("day", "Дата", "date", day_rules or {}),
        ):
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=title,
                value_type=value_type,
                rules=rules,
            )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="С датой",
            content="\n".join(p for p in ("", "[title] — [day]", "") if p),
        )
        session.commit()
        return template.template_id


async def _open_date_question(h, *, day_rules: dict | None = None):
    template_id = _seed(h, day_rules=day_rules)
    await h.text("/start")
    await h.callback(f"tp_pick:{template_id}")
    await h.text("Концерт")
    if "Вопрос 2 из 2" not in h.screen_text():
        raise AssertionError(f"не вопрос даты: {h.screen_text()[:120]!r}")


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")
    return found[0]


def _field_prefix(h) -> str:
    return _callback(h, "aw_calday").split(":", 1)[1].rsplit(":", 1)[0]


def _day_buttons(h) -> list[date]:
    return [
        date.fromisoformat(data.rsplit(":", 1)[1])
        for data in _callbacks(h, "aw_calday")
    ]


def _keyboard(h) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры текущего экрана: строки и кнопки."""

    markup = h.screen().get("reply_markup")
    assert markup is not None
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _answer_text(h) -> str:
    return str(h.answers()[-1].get("text") or "")


def _saved_day(h, *, today: date):
    with h.session_factory() as session:
        draft = list_announcement_drafts(
            session, actor_telegram_user_id=DEFAULT_USER_ID
        )[0]
        fields = build_announcement_fields(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft.draft_id,
            today=today,
        )
    return next(field.raw_value for field in fields if field.code == "day")


def _all_callback_data(rows) -> list[str]:
    return [data for row in rows for _label, data in row if data]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FirstScreenMatrixTests(unittest.TestCase):
    """Состав и расположение строк первого экрана даты (точная матрица)."""

    @scenario_test
    async def test_first_screen_matrix(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            rows = _keyboard(h)

            day_data = [
                f"aw_calday:{prefix}:{(TODAY + timedelta(days=index)).isoformat()}"
                for index in range(8)
            ]
            expected = [
                [
                    (QUICK_LABELS[0], day_data[0]),
                    (QUICK_LABELS[1], day_data[1]),
                    (QUICK_LABELS[2], day_data[2]),
                    (QUICK_LABELS[3], day_data[3]),
                ],
                [
                    (QUICK_LABELS[4], day_data[4]),
                    (QUICK_LABELS[5], day_data[5]),
                    (QUICK_LABELS[6], day_data[6]),
                    (QUICK_LABELS[7], day_data[7]),
                ],
                [
                    ("06.11–12.11", f"aw_calrange:{prefix}:0"),
                    ("13.11–19.11", f"aw_calrange:{prefix}:1"),
                ],
                [("📅 Выбрать месяц", f"aw_calmonths:{prefix}")],
                [
                    ("◀️ Назад", f"aw_qback:{prefix}"),
                    ("⏭ Пропустить", f"aw_qskip:{prefix}"),
                ],
            ]
            self.assertEqual(rows, expected)
            self.assertEqual(len(rows), 5, "ровно пять строк на первом экране")
            self.assertEqual([len(row) for row in rows], [4, 4, 2, 1, 2])

            data = _all_callback_data(rows)
            self.assertEqual(len(data), len(set(data)), "дубли callback_data")
            for removed in (
                "aw_calweek",
                "aw_calother",
                "aw_calroot",
                "aw_calweekdays",
            ):
                self.assertFalse(
                    [item for item in data if item.startswith(removed)],
                    f"на первом экране остался дубль {removed}",
                )
            labels = [label for row in rows for label, _ in row]
            for banned in (
                "Текущая неделя",
                "Следующая неделя",
                "Другая дата",
                "Календарь:",
            ):
                self.assertFalse(
                    [label for label in labels if banned in label],
                    f"на первом экране осталась кнопка {banned!r}",
                )

    @scenario_test
    async def test_expanded_range_matrix_and_return(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)

            await h.callback(f"aw_calrange:{prefix}:1")
            rows = _keyboard(h)
            self.assertEqual(len(rows), 3, "7 дней = 4 + 3, затем возврат")
            self.assertEqual([len(row) for row in rows], [4, 3, 1])
            self.assertEqual(
                _day_buttons(h),
                calendar_view.days_between(date(2026, 11, 13), date(2026, 11, 19)),
            )
            self.assertEqual(
                rows[-1],
                [("◀️ К быстрым датам", f"aw_calquick:{prefix}")],
            )
            # Никаких дублей-входов на раскрытом диапазоне.
            self.assertFalse(_callbacks(h, "aw_calmonths"))
            self.assertFalse(_callbacks(h, "aw_calroot"))

            await h.callback(f"aw_calquick:{prefix}")
            self.assertEqual(
                _keyboard(h)[0][0], (QUICK_LABELS[0], _keyboard(h)[0][0][1])
            )
            self.assertEqual(len(_day_buttons(h)), 8)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuickWindowTests(unittest.TestCase):
    """Восемь быстрых дат в любой день недели, включая воскресенье."""

    @scenario_test
    async def test_monday_has_eight_quick_days(self, h):
        with frozen_today(2026, 10, 26):
            await _open_date_question(h)
            monday = date(2026, 10, 26)
            self.assertEqual(
                _day_buttons(h), [monday + timedelta(days=i) for i in range(8)]
            )
            self.assertEqual(len(_day_buttons(h)), 8)

    @scenario_test
    async def test_sunday_still_has_eight_quick_days(self, h):
        """Старый current_week в воскресенье оставлял один день — регрессия."""
        with frozen_today(2026, 11, 1):
            await _open_date_question(h)
            sunday = date(2026, 11, 1)
            days = _day_buttons(h)
            self.assertEqual(len(days), 8)
            self.assertEqual(days, [sunday + timedelta(days=i) for i in range(8)])
            self.assertEqual(days[-1], date(2026, 11, 8))

    def test_quick_window_crosses_month_end(self):
        with frozen_today(2026, 10, 28):
            days = calendar_view.quick_days(date(2026, 10, 28))
            self.assertEqual(len(days), 8)
            self.assertEqual(days[-1], date(2026, 11, 4))

    def test_quick_window_crosses_year_end(self):
        days = calendar_view.quick_days(date(2026, 12, 29))
        self.assertEqual(days[0], date(2026, 12, 29))
        self.assertEqual(days[-1], date(2027, 1, 5))

    def test_quick_window_in_leap_february(self):
        days = calendar_view.quick_days(date(2028, 2, 25))
        self.assertIn(date(2028, 2, 29), days)
        self.assertEqual(len(days), 8)

    def test_ranges_have_no_overlap_or_gap(self):
        for today in (
            date(2026, 10, 26),
            date(2026, 11, 1),
            date(2026, 10, 31),
            date(2026, 12, 31),
            date(2028, 2, 29),
        ):
            with self.subTest(today=today):
                quick = calendar_view.quick_days(today)
                ranges = calendar_view.followup_ranges(today)
                self.assertEqual(len(quick), 8)
                self.assertEqual(len(ranges), 2)
                for start, end in ranges:
                    self.assertEqual((end - start).days, 6)
                covered = set(quick)
                for start, end in ranges:
                    block = calendar_view.days_between(start, end)
                    self.assertFalse(covered & set(block), "пересечение уровней")
                    covered |= set(block)
                ordered = sorted(covered)
                self.assertEqual(len(ordered), 22)
                self.assertEqual(
                    (ordered[-1] - ordered[0]).days,
                    21,
                    "между первым и последним днём нет пропусков",
                )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MonthPickerTests(unittest.TestCase):
    """«Выбрать месяц» → 12 месяцев → дни месяца сеткой."""

    @scenario_test
    async def test_twelve_month_grid(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calmonths:{prefix}")

            keys = [
                (int(data.rsplit(":", 2)[-2]), int(data.rsplit(":", 1)[-1]))
                for data in _callbacks(h, "aw_calmonth:")
            ]
            self.assertEqual(len(keys), 12)
            self.assertEqual(keys[0], (2026, 11))
            self.assertEqual(keys[-1], (2027, 10))
            labels = [
                label
                for label, data in h.screen_buttons()
                if data and data.startswith("aw_calmonth:")
            ]
            self.assertEqual(
                labels,
                [calendar_view.format_month(key) for key in keys],
            )

    @scenario_test
    async def test_month_days_are_a_grid_with_arrows(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calmonth:{prefix}:2026:11")

            rows = _keyboard(h)
            day_rows = [
                row for row in rows if row and row[0][1].startswith("aw_calday")
            ]
            self.assertEqual(len(day_rows), 5, "ноябрь 2026 — 30 дней по 7 в строке")
            self.assertTrue(all(len(row) == 7 for row in day_rows[:-1]))
            self.assertEqual(len(day_rows[-1]), 2)
            self.assertEqual(_day_buttons(h)[0], date(2026, 11, 1))
            self.assertEqual(_day_buttons(h)[-1], date(2026, 11, 30))
            labels = [label for row in day_rows for label, _ in row]
            self.assertEqual(labels, [str(day) for day in range(1, 31)])
            self.assertIn("📅 Месяцы", [label for label, _ in h.screen_buttons()])
            self.assertIn(
                "◀️ К быстрым датам", [label for label, _ in h.screen_buttons()]
            )

    @scenario_test
    async def test_month_arrows_stay_within_horizon(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)

            await h.callback(f"aw_calmonth:{prefix}:2026:11")
            self.assertNotIn("◀️", [label for label, _ in h.screen_buttons()])
            self.assertIn("▶️", [label for label, _ in h.screen_buttons()])

            await h.callback(f"aw_calmonth:{prefix}:2027:10")
            self.assertIn("◀️", [label for label, _ in h.screen_buttons()])
            self.assertNotIn("▶️", [label for label, _ in h.screen_buttons()])
            self.assertEqual(_day_buttons(h)[-1], calendar_view.horizon_end(TODAY))

    @scenario_test
    async def test_month_day_choice_needs_no_confirmation(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calmonth:{prefix}:2027:3")
            chosen = date(2027, 3, 15)
            await h.callback(f"aw_calday:{prefix}:{chosen.isoformat()}")

            self.assertEqual(_saved_day(h, today=TODAY), "15.03.2027")
            self.assertIn("Фотографии анонса", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuickChoiceTests(unittest.TestCase):
    """Выбор, пропуск и старые callback."""

    @scenario_test
    async def test_quick_day_choice_needs_no_confirmation(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            chosen = TODAY + timedelta(days=5)
            await h.callback(f"aw_calday:{_field_prefix(h)}:{chosen.isoformat()}")
            self.assertEqual(_saved_day(h, today=TODAY), "03.11.2026")
            self.assertIn("Фотографии анонса", h.screen_text())

    @scenario_test
    async def test_skip_advances_without_date(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            await h.callback(_callback(h, "aw_qskip"))
            self.assertIn("Фотографии анонса", h.screen_text())
            self.assertIsNone(_saved_day(h, today=TODAY))

    @scenario_test
    async def test_range_day_choice_and_back(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calrange:{prefix}:0")
            self.assertIn(f"aw_calquick:{prefix}", _callbacks(h, "aw_calquick"))
            chosen = date(2026, 11, 9)
            await h.callback(f"aw_calday:{prefix}:{chosen.isoformat()}")
            self.assertEqual(_saved_day(h, today=TODAY), "09.11.2026")

    @scenario_test
    async def test_old_callbacks_are_safe(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            before = _saved_day(h, today=TODAY)
            for data in (
                f"aw_calroot:{prefix}",
                f"aw_calweek:{prefix}:cur",
                f"aw_calweek:{prefix}:next",
                f"aw_calother:{prefix}",
                f"aw_calweekdays:{prefix}:2026:11:0",
            ):
                await h.callback(data)
                self.assertTrue(_answer_text(h), f"нет ответа на {data}")
                self.assertIn("Вопрос 2 из 2", h.screen_text())
                self.assertEqual(len(_day_buttons(h)), 8)
                self.assertEqual(_saved_day(h, today=TODAY), before)
                self.assertNotIn("внутрен", _answer_text(h).lower())

    @scenario_test
    async def test_horizon_rule_limits_quick_days_and_ranges(self, h):
        """Ограничение горизонта: за max_date в календаре нет дат."""
        with frozen_today(2026, 10, 29):
            max_date = (TODAY + timedelta(days=10)).strftime("%d.%m.%Y")
            await _open_date_question(h, day_rules={"max_date": max_date})
            prefix = _field_prefix(h)
            limit = TODAY + timedelta(days=10)

            self.assertEqual(len(_day_buttons(h)), 8)
            # Второй диапазон целиком за max_date — его кнопки нет.
            self.assertEqual(_callbacks(h, "aw_calrange"), [f"aw_calrange:{prefix}:0"])
            await h.callback(f"aw_calrange:{prefix}:0")
            self.assertEqual(
                [day for day in _day_buttons(h) if day > limit],
                [],
                "даты за горизонтом",
            )
            # «Выбрать месяц» не предлагается: месяцев впереди нет.
            self.assertFalse(_callbacks(h, "aw_calmonths"))

    @scenario_test
    async def test_date_day_choice_outside_horizon_is_rejected(self, h):
        with frozen_today(2026, 10, 29):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            horizon = calendar_view.horizon_end(TODAY)
            await h.callback(
                f"aw_calday:{prefix}:{(horizon + timedelta(days=1)).isoformat()}"
            )
            self.assertIn("вне допустимого диапазона", _answer_text(h))
            self.assertIsNone(_saved_day(h, today=TODAY))
            await h.callback(f"aw_calday:{prefix}:{horizon.isoformat()}")
            self.assertEqual(_saved_day(h, today=TODAY), horizon.strftime("%d.%m.%Y"))


if __name__ == "__main__":
    unittest.main()
