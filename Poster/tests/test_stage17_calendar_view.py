# tests/test_stage17_calendar_view.py
"""
Stage 17: календарная логика кнопочного выбора даты.

Проверяется ЧИСТЫЙ модуль utils.calendar_view — без Telegram и базы:
- формат кнопок «дд.мм (дв)» и диапазонов (включая переходы месяца/года);
- текущая и следующая календарные недели (пн–вс);
- два двухнедельных диапазона и стык с месяцами без пропусков;
- пары календарных месяцев, начиная с первой неохваченной даты;
- горизонт ровно 12 календарных месяцев (високосные годы, концы месяцев);
- месяцы с 28, 29, 30 и 31 днём, 29 февраля, конец месяца и Новый год;
- ПОЛНОТА множества дат: объединение всех кнопочных уровней равно всему
  разрешённому интервалу [сегодня, сегодня + 12 месяцев] — без пропусков,
  повторов внутри уровня и дат за пределами горизонта.

Запуск из каталога Poster:
    python -m unittest tests.test_stage17_calendar_view -v
"""

import re
import unittest
from datetime import date, timedelta

from utils.calendar_view import (
    MONTH_NAMES,
    WEEKDAYS_SHORT,
    add_months,
    add_months_key,
    app_today_value,
    current_week,
    days_between,
    days_in_month,
    first_uncovered,
    format_day,
    format_month,
    format_range,
    horizon_end,
    month_bounds,
    month_pairs,
    month_weeks,
    next_week,
    other_ranges,
    pair_index,
    weeks_covering,
)

DAY_LABEL_RE = re.compile(r"^\d{2}\.\d{2} \((пн|вт|ср|чт|пт|сб|вс)\)$")


def collect_calendar_days(today: date) -> list[date]:
    """
    Все даты, достижимые кнопками календаря для указанного «сегодня».

    Обходит ровно те уровни, которые рисует интерфейс:
    текущая неделя, следующая неделя, два диапазона (недели → дни),
    все пары месяцев (месяцы → недели → дни).
    """
    horizon = horizon_end(today)
    days: list[date] = []

    start, end = current_week(today)
    days.extend(days_between(start, end))

    start, end = next_week(today)
    days.extend(days_between(start, end))

    for range_start, range_end in other_ranges(today):
        for week_start, week_end in weeks_covering(range_start, range_end):
            days.extend(days_between(week_start, week_end))

    for pair in month_pairs(today, horizon):
        for key in pair:
            for week_start, week_end in month_weeks(key, (today, horizon)):
                days.extend(days_between(week_start, week_end))

    return days


class FormattingTests(unittest.TestCase):
    """Формат кнопок: строгий для дня, однозначный для диапазонов."""

    def test_day_buttons_are_strict(self):
        self.assertEqual(format_day(date(2026, 11, 17)), "17.11 (вт)")
        self.assertEqual(format_day(date(2026, 10, 22)), "22.10 (чт)")
        self.assertEqual(format_day(date(2026, 10, 25)), "25.10 (вс)")
        self.assertEqual(format_day(date(2027, 1, 1)), "01.01 (пт)")

    def test_every_weekday_has_russian_two_letter_label(self):
        # 2026-10-19 — понедельник: семь дней подряд дают все семь подписей
        labels = [format_day(date(2026, 10, 19) + timedelta(days=i)) for i in range(7)]
        self.assertEqual(
            labels,
            [
                "19.10 (пн)",
                "20.10 (вт)",
                "21.10 (ср)",
                "22.10 (чт)",
                "23.10 (пт)",
                "24.10 (сб)",
                "25.10 (вс)",
            ],
        )
        self.assertEqual(WEEKDAYS_SHORT, ("пн", "вт", "ср", "чт", "пт", "сб", "вс"))

    def test_range_same_month_year(self):
        self.assertEqual(
            format_range(date(2026, 11, 2), date(2026, 11, 8)), "02.11–08.11"
        )
        self.assertEqual(
            format_range(date(2026, 11, 2), date(2026, 11, 15)), "02.11–15.11"
        )

    def test_range_crosses_month(self):
        self.assertEqual(
            format_range(date(2026, 10, 26), date(2026, 11, 1)), "26.10–01.11"
        )

    def test_range_crosses_year_has_years(self):
        self.assertEqual(
            format_range(date(2026, 12, 28), date(2027, 1, 10)),
            "28.12.2026–10.01.2027",
        )

    def test_single_day_range(self):
        self.assertEqual(format_range(date(2026, 11, 30), date(2026, 11, 30)), "30.11")

    def test_month_names_are_russian_with_year(self):
        self.assertEqual(format_month((2026, 11)), "Ноябрь 2026")
        self.assertEqual(format_month((2027, 1)), "Январь 2027")
        self.assertEqual(MONTH_NAMES[12], "Декабрь")


class CalendarStructureTests(unittest.TestCase):
    """Недели, диапазоны и пары месяцев для 22.10.2026 (пример задания)."""

    TODAY = date(2026, 10, 22)  # четверг

    def test_current_week_starts_today(self):
        start, end = current_week(self.TODAY)
        self.assertEqual(start, date(2026, 10, 22))
        self.assertEqual(end, date(2026, 10, 25))

    def test_next_week_is_full_calendar_week(self):
        start, end = next_week(self.TODAY)
        self.assertEqual(start, date(2026, 10, 26))
        self.assertEqual(end, date(2026, 11, 1))

    def test_other_ranges_example(self):
        ranges = other_ranges(self.TODAY)
        self.assertEqual(
            ranges,
            [
                (date(2026, 11, 2), date(2026, 11, 15)),
                (date(2026, 11, 16), date(2026, 11, 29)),
            ],
        )
        self.assertEqual(
            [format_range(*value) for value in ranges], ["02.11–15.11", "16.11–29.11"]
        )

    def test_first_uncovered_is_next_day_after_ranges(self):
        self.assertEqual(first_uncovered(self.TODAY), date(2026, 11, 30))

    def test_month_pairs_example(self):
        pairs = month_pairs(self.TODAY, horizon_end(self.TODAY))
        self.assertEqual(
            pairs,
            [
                [(2026, 11), (2026, 12)],
                [(2027, 1), (2027, 2)],
                [(2027, 3), (2027, 4)],
                [(2027, 5), (2027, 6)],
                [(2027, 7), (2027, 8)],
                [(2027, 9), (2027, 10)],
            ],
        )
        self.assertEqual(pair_index(self.TODAY, (2026, 11)), 0)
        self.assertEqual(pair_index(self.TODAY, (2026, 12)), 0)
        self.assertEqual(pair_index(self.TODAY, (2027, 1)), 1)
        self.assertEqual(pair_index(self.TODAY, (2027, 10)), 5)
        self.assertIsNone(pair_index(self.TODAY, (2026, 10)))

    def test_november_still_contains_thirtieth(self):
        """Стык диапазонов и месяцев: 30.11 не потеряно, первая пара — ноябрь."""
        horizon = horizon_end(self.TODAY)
        key = (2026, 11)
        weeks = month_weeks(key, (self.TODAY, horizon))
        days = [day for week in weeks for day in days_between(*week)]
        self.assertIn(date(2026, 11, 30), days)
        self.assertEqual(month_pairs(self.TODAY, horizon)[0][0], key)

    def test_current_week_monday_and_sunday(self):
        monday = date(2026, 10, 19)
        self.assertEqual(current_week(monday), (monday, date(2026, 10, 25)))
        sunday = date(2026, 10, 25)
        self.assertEqual(current_week(sunday), (sunday, sunday))
        self.assertEqual(next_week(sunday), (date(2026, 10, 26), date(2026, 11, 1)))

    def test_wednesday_current_week(self):
        wednesday = date(2026, 10, 21)
        self.assertEqual(current_week(wednesday), (wednesday, date(2026, 10, 25)))

    def test_ranges_are_contiguous_with_weeks_and_months(self):
        """Швы навигации: каждая следующая ступень начинается со следующего дня."""
        week_start, week_end = current_week(self.TODAY)
        next_start, next_end = next_week(self.TODAY)
        ranges = other_ranges(self.TODAY)
        self.assertEqual(week_end + timedelta(days=1), next_start)
        self.assertEqual(next_end + timedelta(days=1), ranges[0][0])
        self.assertEqual(ranges[0][1] + timedelta(days=1), ranges[1][0])
        self.assertEqual(ranges[1][1] + timedelta(days=1), first_uncovered(self.TODAY))
        first_month_start, _ = month_bounds(
            month_pairs(self.TODAY, horizon_end(self.TODAY))[0][0]
        )
        self.assertLessEqual(first_month_start, first_uncovered(self.TODAY))

    def test_month_weeks_do_not_leave_month(self):
        horizon = horizon_end(self.TODAY)
        for key in ((2026, 11), (2026, 12), (2027, 2)):
            month_start, month_end = month_bounds(key)
            for week_start, week_end in month_weeks(key, (self.TODAY, horizon)):
                self.assertGreaterEqual(week_start, month_start)
                self.assertLessEqual(week_end, month_end)
                self.assertLessEqual(week_start, week_end)


class MonthBoundaryTests(unittest.TestCase):
    """Месяцы с 28, 29, 30 и 31 днём, 29 февраля и конец месяца."""

    def test_month_lengths(self):
        self.assertEqual(days_in_month(2027, 2), 28)
        self.assertEqual(days_in_month(2028, 2), 29)
        self.assertEqual(days_in_month(2026, 4), 30)
        self.assertEqual(days_in_month(2026, 10), 31)

    def test_february_regular_2027_weeks(self):
        weeks = month_weeks((2027, 2), (date(2026, 1, 1), date(2030, 1, 1)))
        self.assertEqual(
            [(start.day, end.day) for start, end in weeks],
            [(1, 7), (8, 14), (15, 21), (22, 28)],
        )

    def test_february_leap_2028_contains_29th(self):
        today = date(2028, 1, 1)
        horizon = horizon_end(today)
        weeks = month_weeks((2028, 2), (today, horizon))
        days = [day for week in weeks for day in days_between(*week)]
        self.assertIn(date(2028, 2, 29), days)
        self.assertEqual(days[-1], date(2028, 2, 29))
        self.assertEqual(format_day(date(2028, 2, 29)), "29.02 (вт)")

    def test_april_30_days_weeks(self):
        weeks = month_weeks((2026, 4), (date(2026, 1, 1), date(2027, 1, 1)))
        self.assertEqual(
            [(start.day, end.day) for start, end in weeks],
            [(1, 5), (6, 12), (13, 19), (20, 26), (27, 30)],
        )

    def test_november_first_partial_week_is_one_day(self):
        """1 ноября 2026 — воскресенье: неделя месяца не уходит в октябрь."""
        weeks = month_weeks((2026, 11), (date(2026, 1, 1), date(2027, 12, 31)))
        self.assertEqual(weeks[0], (date(2026, 11, 1), date(2026, 11, 1)))
        self.assertEqual(format_range(*weeks[0]), "01.11")
        self.assertEqual(weeks[-1], (date(2026, 11, 30), date(2026, 11, 30)))

    def test_december_crosses_year_and_is_clipped(self):
        weeks = month_weeks((2026, 12), (date(2026, 1, 1), date(2027, 12, 31)))
        self.assertEqual(weeks[-1], (date(2026, 12, 28), date(2026, 12, 31)))
        self.assertEqual(format_range(*weeks[-1]), "28.12–31.12")

    def test_horizon_month_last_week_is_clipped(self):
        """Октябрь 2027 доступен только по 22.10 — недели не выходят за горизонт."""
        today = date(2026, 10, 22)
        horizon = horizon_end(today)
        self.assertEqual(horizon, date(2027, 10, 22))
        weeks = month_weeks((2027, 10), (today, horizon))
        self.assertEqual(weeks[0], (date(2027, 10, 1), date(2027, 10, 3)))
        self.assertEqual(weeks[-1], (date(2027, 10, 18), date(2027, 10, 22)))
        self.assertEqual(format_range(*weeks[-1]), "18.10–22.10")

    def test_year_boundary_ranges(self):
        today = date(2026, 12, 15)  # вторник
        ranges = other_ranges(today)
        self.assertEqual(ranges[0][0], date(2026, 12, 28))
        self.assertEqual(ranges[0][1], date(2027, 1, 10))
        self.assertEqual(format_range(*ranges[0]), "28.12.2026–10.01.2027")
        self.assertEqual(first_uncovered(today), date(2027, 1, 25))

    def test_last_pair_can_have_one_month(self):
        """Если месяц горизонта — первый в паре, пустой второй не показывается."""
        today = date(2026, 10, 31)
        horizon = horizon_end(today)
        self.assertEqual(horizon, date(2027, 10, 31))
        pairs = month_pairs(today, horizon)
        self.assertEqual(pairs[0][0], (2026, 12))
        self.assertEqual(pairs[-1], [(2027, 10)])
        self.assertEqual(len(pairs), 6)
        self.assertEqual(pair_index(today, (2027, 10)), 5)

    def test_single_day_month_after_horizon_is_not_shown(self):
        """Месяц, полностью лежащий за горизонтом, не попадает в пары."""
        today = date(2026, 10, 22)
        horizon = horizon_end(today)
        months = [key for pair in month_pairs(today, horizon) for key in pair]
        self.assertEqual(months[0], (2026, 11))
        self.assertEqual(months[-1], (2027, 10))
        self.assertNotIn((2027, 11), months)


class HorizonTests(unittest.TestCase):
    """Граница 12 календарных месяцев — прибавление месяцев, а не 365 дней."""

    def test_horizon_example(self):
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


class CompletenessTests(unittest.TestCase):
    """
    Главный тест полноты: объединение всех уровней календаря — ровно весь
    разрешённый интервал. Ни одна дата не пропущена и не выходит за границы.
    """

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
                views: list[tuple[str, list[date]]] = []
                current = days_between(*current_week(today))
                following = days_between(*next_week(today))
                views.append(("current_week", current))
                views.append(("next_week", following))
                for index, (start, end) in enumerate(other_ranges(today)):
                    for week_number, week in enumerate(weeks_covering(start, end)):
                        views.append(
                            (
                                f"range_{index}_week_{week_number}",
                                days_between(*week),
                            )
                        )
                for pair_number, pair in enumerate(month_pairs(today, horizon)):
                    for key in pair:
                        for week_number, week in enumerate(
                            month_weeks(key, (today, horizon))
                        ):
                            views.append(
                                (
                                    f"month_{key[0]}_{key[1]}_week_{week_number}",
                                    days_between(*week),
                                )
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
                    label = format_day(day)
                    self.assertRegex(label, DAY_LABEL_RE)
                    self.assertEqual(
                        label.split("(")[1][:2], WEEKDAYS_SHORT[day.weekday()]
                    )

    def test_no_empty_month_or_range(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                for start, end in other_ranges(today):
                    self.assertLessEqual(start, end)
                    self.assertTrue(weeks_covering(start, end))
                for pair in month_pairs(today, horizon):
                    self.assertIn(len(pair), (1, 2))
                    for key in pair:
                        month_start, month_end = month_bounds(key)
                        self.assertLessEqual(month_start, horizon)
                        self.assertGreaterEqual(month_end, today)
                        self.assertTrue(month_weeks(key, (today, horizon)))

    def test_month_pairs_cover_seam_after_ranges(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                pairs = month_pairs(today, horizon)
                first_month_start, _ = month_bounds(pairs[0][0])
                self.assertLessEqual(first_month_start, first_uncovered(today))
                # последний месяц пар содержит последний день горизонта
                last_month_start, last_month_end = month_bounds(pairs[-1][-1])
                self.assertLessEqual(last_month_start, horizon)
                self.assertLessEqual(horizon, last_month_end)

    def test_days_of_week_are_in_their_weeks(self):
        for today in self.TODAYS:
            with self.subTest(today=today):
                horizon = horizon_end(today)
                views = [weeks_covering(*current_week(today))]
                views.append(weeks_covering(*next_week(today)))
                for start, end in other_ranges(today):
                    views.append(weeks_covering(start, end))
                for pair in month_pairs(today, horizon):
                    for key in pair:
                        views.append(month_weeks(key, (today, horizon)))
                for weeks in views:
                    for week_start, week_end in weeks:
                        self.assertLessEqual(week_start, week_end)
                        self.assertEqual(
                            week_start + timedelta(days=(week_end - week_start).days),
                            week_end,
                        )
                        self.assertLessEqual((week_end - week_start).days, 6)


if __name__ == "__main__":
    unittest.main()
