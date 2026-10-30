# tests/test_stage17_calendar_view.py
"""
Календарная логика кнопочного выбора даты (актуализировано в U2a).

Проверяется ЧИСТЫЙ модуль utils.calendar_view — без Telegram и базы:
- формат подписей: быстрая дата «29.10 чт», день месяца, диапазон, месяц;
- быстрое окно ровно из восьми дат (сегодня + 7), скользящее и не
  привязанное к календарной неделе;
- два диапазона по семь дней сразу после окна, без пересечений и пропусков;
- сетка месяцев от первой неохваченной даты до горизонта;
- дни месяца в границах месяца и горизонта;
- горизонт ровно 12 календарных месяцев (високосные годы, концы месяцев);
- ПОЛНОТА множества дат: объединение всех кнопочных уровней равно всему
  разрешённому интервалу [сегодня, сегодня + 12 месяцев] — без пропусков,
  без повторов.

Запуск из каталога Poster:
    python -m unittest tests.test_stage17_calendar_view -v
"""

import re
import unittest
from datetime import date, timedelta

from utils.calendar_view import (
    FOLLOWUP_RANGE_DAYS,
    HORIZON_MONTHS,
    MONTH_GRID_MAX,
    MONTH_NAMES,
    QUICK_DAYS,
    WEEKDAYS_SHORT,
    add_months,
    add_months_key,
    app_today_value,
    days_between,
    days_in_month,
    first_uncovered,
    followup_ranges,
    format_month,
    format_month_day,
    format_quick_day,
    format_range,
    horizon_end,
    month_bounds,
    month_days,
    month_grid,
    month_sequence,
    quick_days,
)

DAY_LABEL_RE = re.compile(r"^\d{2}\.\d{2} (пн|вт|ср|чт|пт|сб|вс)$")


def collect_calendar_days(today: date) -> list[date]:
    """
    Все даты, достижимые кнопками календаря для указанного «сегодня».

    Обходит ровно те уровни, которые рисует интерфейс: быстрые даты,
    два семидневных диапазона, все месяцы горизонта (дни месяца). Сетка
    месяцев ограничена 12 кнопками, поэтому месяц горизонта, не попавший в
    неё, открывается стрелкой «▶️» — здесь он берётся из `month_sequence`.
    """
    horizon = horizon_end(today)
    days: list[date] = []

    days.extend(day for day in quick_days(today) if day <= horizon)
    for start, end in followup_ranges(today):
        days.extend(day for day in days_between(start, end) if day <= horizon)
    for key in month_sequence(max(first_uncovered(today), today), horizon):
        days.extend(month_days(key, (today, horizon)))

    return days


class FormattingTests(unittest.TestCase):
    """Формат подписей: компактная быстрая дата и однозначный диапазон."""

    def test_quick_day_labels_are_compact(self):
        self.assertEqual(format_quick_day(date(2026, 10, 29)), "29.10 чт")
        self.assertEqual(format_quick_day(date(2026, 11, 1)), "01.11 вс")
        self.assertEqual(format_quick_day(date(2027, 1, 1)), "01.01 пт")

    def test_every_weekday_has_russian_two_letter_label(self):
        labels = [
            format_quick_day(date(2026, 10, 19) + timedelta(days=i)) for i in range(7)
        ]
        self.assertEqual(
            labels,
            [
                "19.10 пн",
                "20.10 вт",
                "21.10 ср",
                "22.10 чт",
                "23.10 пт",
                "24.10 сб",
                "25.10 вс",
            ],
        )
        self.assertEqual(WEEKDAYS_SHORT, ("пн", "вт", "ср", "чт", "пт", "сб", "вс"))

    def test_month_day_is_just_number(self):
        self.assertEqual(format_month_day(date(2026, 11, 1)), "1")
        self.assertEqual(format_month_day(date(2026, 11, 30)), "30")

    def test_range_same_month_year(self):
        self.assertEqual(
            format_range(date(2026, 11, 6), date(2026, 11, 12), date(2026, 10, 29)),
            "06.11–12.11",
        )

    def test_range_crosses_month(self):
        self.assertEqual(
            format_range(date(2026, 10, 30), date(2026, 11, 5), date(2026, 10, 29)),
            "30.10–05.11",
        )

    def test_range_crosses_year_names_end_year(self):
        self.assertEqual(
            format_range(date(2026, 12, 28), date(2027, 1, 3), date(2026, 12, 27)),
            "28.12–03.01.2027",
        )

    def test_range_next_year_without_crossing_names_year(self):
        # 27.12.2026 → диапазоны уже в 2027: без года подпись неоднозначна.
        self.assertEqual(
            format_range(date(2027, 1, 4), date(2027, 1, 10), date(2026, 12, 27)),
            "04.01–10.01.2027",
        )
        self.assertEqual(
            format_range(date(2027, 1, 11), date(2027, 1, 17), date(2026, 12, 27)),
            "11.01–17.01.2027",
        )

    def test_range_of_current_year_has_no_year(self):
        self.assertEqual(
            format_range(date(2026, 11, 6), date(2026, 11, 12), date(2026, 12, 27)),
            "06.11–12.11",
            "диапазон текущего года не должен обрастать годом",
        )

    def test_single_day_range(self):
        self.assertEqual(
            format_range(date(2026, 11, 30), date(2026, 11, 30), date(2026, 10, 29)),
            "30.11",
        )
        self.assertEqual(
            format_range(date(2027, 1, 4), date(2027, 1, 4), date(2026, 12, 27)),
            "04.01.2027",
        )

    def test_month_names_are_russian_with_year(self):
        self.assertEqual(format_month((2026, 11)), "Ноябрь 2026")
        self.assertEqual(format_month((2027, 1)), "Январь 2027")
        self.assertEqual(MONTH_NAMES[12], "Декабрь")


class QuickWindowTests(unittest.TestCase):
    """Быстрое окно: ровно восемь дат в любом дне недели."""

    def test_quick_window_has_exactly_eight_days(self):
        today = date(2026, 10, 29)  # четверг, пример задания
        days = quick_days(today)
        self.assertEqual(len(days), QUICK_DAYS)
        self.assertEqual(
            [format_quick_day(day) for day in days],
            [
                "29.10 чт",
                "30.10 пт",
                "31.10 сб",
                "01.11 вс",
                "02.11 пн",
                "03.11 вт",
                "04.11 ср",
                "05.11 чт",
            ],
        )

    def test_sunday_still_has_eight_quick_days(self):
        """Старый current_week в воскресенье давал один день — регрессия."""
        sunday = date(2026, 10, 25)
        days = quick_days(sunday)
        self.assertEqual(len(days), 8)
        self.assertEqual(days[0], sunday)
        self.assertEqual(days[-1], date(2026, 11, 1))

    def test_monday_quick_window_is_not_calendar_week(self):
        monday = date(2026, 10, 19)
        days = quick_days(monday)
        self.assertEqual(len(days), 8)
        self.assertEqual(days[-1], date(2026, 10, 26))

    def test_quick_window_crosses_month_end(self):
        days = quick_days(date(2026, 10, 28))
        self.assertEqual(days[0], date(2026, 10, 28))
        self.assertEqual(days[-1], date(2026, 11, 4))

    def test_quick_window_crosses_year_end(self):
        days = quick_days(date(2026, 12, 29))
        self.assertEqual(days[0], date(2026, 12, 29))
        self.assertEqual(days[-1], date(2027, 1, 5))

    def test_quick_window_leap_february(self):
        days = quick_days(date(2028, 2, 25))
        self.assertIn(date(2028, 2, 29), days)
        self.assertEqual(days[-1], date(2028, 3, 3))


class FollowupRangeTests(unittest.TestCase):
    """Два диапазона по семь дней: без пересечений и пропусков."""

    def test_ranges_example(self):
        ranges = followup_ranges(date(2026, 10, 29))
        self.assertEqual(
            ranges,
            [
                (date(2026, 11, 6), date(2026, 11, 12)),
                (date(2026, 11, 13), date(2026, 11, 19)),
            ],
        )
        self.assertEqual(
            [format_range(*item, today=date(2026, 10, 29)) for item in ranges],
            ["06.11–12.11", "13.11–19.11"],
        )

    def test_ranges_are_six_seven_day_blocks(self):
        self.assertEqual(FOLLOWUP_RANGE_DAYS, 7)
        for start, end in followup_ranges(date(2026, 10, 29)):
            self.assertEqual((end - start).days, 6)

    def test_quick_window_and_ranges_are_contiguous_and_disjoint(self):
        for today in (
            date(2026, 10, 19),  # понедельник
            date(2026, 10, 25),  # воскресенье
            date(2026, 10, 31),  # конец месяца
            date(2026, 12, 31),  # конец года
            date(2028, 2, 29),  # високосный день
        ):
            with self.subTest(today=today):
                quick = quick_days(today)
                ranges = followup_ranges(today)
                blocks = [quick] + [days_between(*item) for item in ranges]
                self.assertEqual(len(quick), 8)
                self.assertEqual([len(block) for block in blocks], [8, 7, 7])
                for earlier, later in zip(blocks, blocks[1:]):
                    self.assertEqual(
                        earlier[-1] + timedelta(days=1),
                        later[0],
                        "между уровнями не должно быть пропуска",
                    )
                seen: list[date] = [day for block in blocks for day in block]
                self.assertEqual(len(seen), len(set(seen)), "диапазоны пересекаются")

    def test_first_uncovered_is_next_day_after_second_range(self):
        today = date(2026, 10, 29)
        ranges = followup_ranges(today)
        self.assertEqual(first_uncovered(today), ranges[1][1] + timedelta(days=1))
        self.assertEqual(first_uncovered(today), date(2026, 11, 20))

    def test_ranges_cross_year_boundary(self):
        ranges = followup_ranges(date(2026, 12, 26))
        self.assertEqual(ranges[0][0], date(2027, 1, 3))
        self.assertEqual(ranges[1][1], date(2027, 1, 16))


class MonthGridTests(unittest.TestCase):
    """Сетка месяцев и дни месяца в пределах горизонта."""

    def test_month_grid_has_twelve_months(self):
        today = date(2026, 10, 29)
        horizon = horizon_end(today)
        grid = month_grid(first_uncovered(today), horizon)
        self.assertEqual(len(grid), HORIZON_MONTHS)
        self.assertEqual(grid[0], (2026, 11))
        self.assertEqual(grid[-1], (2027, 10))

    def test_month_grid_is_capped_at_twelve(self):
        """Месяц горизонта, не попавший в 12 кнопок, берётся из sequence."""
        today = date(2026, 10, 1)
        horizon = horizon_end(today)
        seam = first_uncovered(today)
        sequence = month_sequence(seam, horizon)
        grid = month_grid(seam, horizon)
        self.assertEqual(len(sequence), 13, "в последовательности 13 месяцев")
        self.assertEqual(len(grid), MONTH_GRID_MAX)
        self.assertEqual(grid, sequence[:MONTH_GRID_MAX])
        self.assertEqual(sequence[-1], (horizon.year, horizon.month))

    def test_month_grid_covers_seam_or_continues_by_arrow(self):
        for today in (
            date(2026, 10, 22),
            date(2026, 10, 1),
            date(2026, 12, 31),
            date(2028, 2, 29),
        ):
            with self.subTest(today=today):
                horizon = horizon_end(today)
                seam = first_uncovered(today)
                grid = month_grid(seam, horizon)
                sequence = month_sequence(seam, horizon)
                first_month_start, _ = month_bounds(grid[0])
                self.assertLessEqual(first_month_start, seam)
                # Сетка — начало последовательности, хвост доступен стрелкой.
                self.assertEqual(grid, sequence[: len(grid)])
                last_month_start, last_month_end = month_bounds(sequence[-1])
                self.assertLessEqual(last_month_start, horizon)
                self.assertLessEqual(horizon, last_month_end)
                self.assertLessEqual(len(grid), MONTH_GRID_MAX)

    def test_month_grid_is_empty_when_seam_is_past_horizon(self):
        today = date(2026, 10, 29)
        self.assertEqual(month_grid(first_uncovered(today), today), [])

    def test_month_days_stay_inside_month_and_bounds(self):
        today = date(2026, 10, 29)
        horizon = horizon_end(today)
        for key in month_grid(first_uncovered(today), horizon):
            month_start, month_end = month_bounds(key)
            days = month_days(key, (today, horizon))
            self.assertTrue(days, f"пустой месяц {key}")
            for day in days:
                self.assertEqual((day.year, day.month), key)
                self.assertGreaterEqual(day, month_start)
                self.assertLessEqual(day, month_end)
                self.assertTrue(today <= day <= horizon)

    def test_month_days_clipped_by_horizon(self):
        today = date(2026, 10, 29)
        horizon = horizon_end(today)
        self.assertEqual(horizon, date(2027, 10, 29))
        days = month_days((2027, 10), (today, horizon))
        self.assertEqual(days[-1], horizon)
        self.assertEqual(len(days), 29)

    def test_month_lengths(self):
        self.assertEqual(days_in_month(2027, 2), 28)
        self.assertEqual(days_in_month(2028, 2), 29)
        self.assertEqual(days_in_month(2026, 4), 30)
        self.assertEqual(days_in_month(2026, 10), 31)

    def test_leap_february_has_29th(self):
        today = date(2028, 1, 1)
        days = month_days((2028, 2), (today, horizon_end(today)))
        self.assertIn(date(2028, 2, 29), days)
        self.assertEqual(format_quick_day(date(2028, 2, 29)), "29.02 вт")


class HorizonTests(unittest.TestCase):
    """Граница 12 календарных месяцев — прибавление месяцев, а не 365 дней."""

    def test_horizon_example(self):
        self.assertEqual(horizon_end(date(2026, 10, 29)), date(2027, 10, 29))
        self.assertEqual(horizon_end(date(2026, 10, 22)), date(2027, 10, 22))

    def test_horizon_keeps_day_of_month(self):
        self.assertEqual(horizon_end(date(2026, 1, 31)), date(2027, 1, 31))
        self.assertEqual(horizon_end(date(2026, 8, 31)), date(2027, 8, 31))
        self.assertEqual(horizon_end(date(2026, 11, 30)), date(2027, 11, 30))

    def test_horizon_clamps_leap_day(self):
        # 29.02.2028 + 12 месяцев: 29.02.2029 не существует → 28.02.2029
        self.assertEqual(horizon_end(date(2028, 2, 29)), date(2029, 2, 28))

    def test_add_months_clamps_to_month_end(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))
        self.assertEqual(add_months(date(2028, 1, 31), 1), date(2028, 2, 29))
        self.assertEqual(add_months(date(2026, 3, 31), 1), date(2026, 4, 30))
        self.assertEqual(add_months(date(2026, 12, 15), 1), date(2027, 1, 15))

    def test_add_months_key_wraps_year(self):
        self.assertEqual(add_months_key((2026, 12), 1), (2027, 1))
        self.assertEqual(add_months_key((2026, 1), -1), (2025, 12))
        self.assertEqual(add_months_key((2026, 11), 14), (2028, 1))

    def test_horizon_is_inclusive_and_bounded(self):
        today = date(2026, 10, 22)
        horizon = horizon_end(today)
        days = days_between(today, horizon)
        self.assertEqual(days[0], today)
        self.assertEqual(days[-1], horizon)
        self.assertEqual(len(days), 366)  # 2027 не високосный: 365 + 1 день
        self.assertNotIn(horizon + timedelta(days=1), days)

    def test_actual_today_comes_from_app_timezone(self):
        """Календарь и текстовый парсер используют одно «сегодня» (APP_TZ)."""
        from datetime import datetime

        from utils.parsers import APP_TZ, app_today

        self.assertEqual(app_today_value(), app_today())
        self.assertEqual(app_today(), datetime.now(APP_TZ).date())
        self.assertEqual(str(APP_TZ), "Europe/Moscow")


def unique(values: list[date]) -> list[date]:
    return list(dict.fromkeys(values))


TODAYS = (
    date(2026, 10, 22),  # четверг из примера
    date(2026, 10, 19),  # понедельник
    date(2026, 10, 21),  # среда
    date(2026, 10, 25),  # воскресенье
    date(2026, 10, 31),  # конец месяца
    date(2026, 12, 31),  # канун Нового года
    date(2027, 1, 1),  # Новый год
    date(2028, 2, 29),  # високосный день
    date(2026, 11, 30),  # 30-дневный месяц
    date(2027, 2, 28),  # 28-дневный февраль
    date(2029, 12, 31),  # ещё один переход года
)


class CompletenessTests(unittest.TestCase):
    """
    Главный тест полноты: объединение всех уровней календаря — ровно весь
    разрешённый интервал. Ни одна дата не пропущена и не выходит за границы.
    """

    TODAYS = TODAYS

    def test_full_horizon_has_no_gaps(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                expected = days_between(today, horizon)
                actual = collect_calendar_days(today)
                self.assertEqual(
                    sorted(set(actual)),
                    expected,
                    "каждая дата горизонта должна быть достижима",
                )
                # ни одной даты вне [сегодня, горизонт]
                self.assertTrue(all(today <= day <= horizon for day in actual))
                self.assertNotIn(horizon + timedelta(days=1), actual)
                self.assertNotIn(today - timedelta(days=1), actual)

    def test_each_view_has_no_duplicate_days(self):
        """Внутри одного уровня дата не дублируется (повторы ломали бы выбор)."""
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                views: list[tuple[str, list[date]]] = [
                    ("quick", quick_days(today)),
                ]
                for index, (start, end) in enumerate(followup_ranges(today)):
                    views.append((f"range_{index}", days_between(start, end)))
                for key in month_grid(max(first_uncovered(today), today), horizon):
                    views.append(
                        (f"month_{key[0]}_{key[1]}", month_days(key, (today, horizon)))
                    )
                # Месяц горизонта, не попавший в 12 кнопок, — тоже экран дней.
                tail = month_sequence(max(first_uncovered(today), today), horizon)[
                    MONTH_GRID_MAX:
                ]
                for key in tail:
                    views.append(
                        (f"month_{key[0]}_{key[1]}", month_days(key, (today, horizon)))
                    )
                for name, days in views:
                    self.assertTrue(days, f"{name}: пустой интервал")
                    self.assertEqual(
                        len(days), len(unique(days)), f"{name}: повтор даты"
                    )

    def test_every_day_button_label_matches_calendar(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                for day in unique(collect_calendar_days(today)):
                    label = format_quick_day(day)
                    self.assertRegex(label, DAY_LABEL_RE)
                    self.assertEqual(label.split(" ")[1], WEEKDAYS_SHORT[day.weekday()])

    def test_no_empty_month_or_range(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                for start, end in followup_ranges(today):
                    self.assertLessEqual(start, end)
                    self.assertEqual(len(days_between(start, end)), 7)
                for key in month_sequence(max(first_uncovered(today), today), horizon):
                    month_start, month_end = month_bounds(key)
                    self.assertLessEqual(month_start, horizon)
                    self.assertGreaterEqual(month_end, today)
                    self.assertTrue(month_days(key, (today, horizon)))


if __name__ == "__main__":
    unittest.main()
