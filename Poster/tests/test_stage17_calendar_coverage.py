# tests/test_stage17_calendar_coverage.py
"""
Stage 17: полнота кнопочного календаря через настоящий интерфейс.

Тесты проходят через Application.process_update (FakeBot + временная SQLite)
и проверяют не только чистые функции, но и то, что РИСУЕТ бот:

- каждая дата горизонта [сегодня, сегодня + 12 календарных месяцев]
  достижима кнопками (объединение всех уровней интерфейса);
- каждая такая дата реально принимается обработчиком;
- каждая неделя/месяц/диапазон не содержит чужих и повторных дат;
- подписи кнопок соответствуют датам и русским дням недели;
- callback-данные короткие, не содержат пользовательского текста и
  укладываются в лимит Telegram (64 байта);
- навигация меняет ТОЛЬКО существующий экран и отвечает на каждый callback
  ровно один раз; пустых экранов и лишних сообщений нет.

Запуск из каталога Poster:
    python -m unittest tests.test_stage17_calendar_coverage -v
"""

import re
import unittest
from datetime import date, timedelta

try:
    from handlers.post_creation import POST_CREATION, POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        frozen_today,
        scenario_test,
    )
    from utils import calendar_view

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)

DAY_LABEL_RE = re.compile(r"^\d{2}\.\d{2} \((пн|вт|ср|чт|пт|сб|вс)\)$")


def day_dates(h) -> list[date]:
    """Даты из кнопок текущего экрана (по callback cal_day:ISO)."""
    result = []
    for _, data in h.screen_buttons():
        if data and data.startswith("cal_day:"):
            result.append(date.fromisoformat(data.split(":", 1)[1]))
    return result


def callbacks(h) -> list[str]:
    """Все непустые callback-данные текущего экрана."""
    return [data for _, data in h.screen_buttons() if data]


def parse_month(data: str) -> tuple[int, int]:
    year, month = data.split(":", 1)[1].split("-")
    return int(year), int(month)


def screen_sends(h) -> list[dict]:
    return [call for call in h.screens() if call["type"] == "message"]


async def collect_all_views(h, today: date) -> dict:
    """
    Обходит ВСЕ уровни календаря так, как это делает пользователь.

    Возвращает собранные даты по каждому уровню, подписи кнопок и все
    встреченные callback-данные. Используется и для проверки полноты,
    и для проверки длины callback-данных.
    """
    horizon = calendar_view.horizon_end(today)
    result: dict = {
        "day_labels": {},
        "callbacks": set(),
        "month_keys": [],
        "pair_sizes": [],
        "days_by_month": {},
        "days_by_range": {},
        "day_views": [],
    }

    def collect_view(days: list[date], view_name: str) -> None:
        labels = [
            (text, data)
            for text, data in h.screen_buttons()
            if data and data.startswith("cal_day:")
        ]
        assert len(labels) == len(days), f"{view_name}: число кнопок-дней"
        assert len(set(days)) == len(days), f"{view_name}: повтор даты"
        for (text, data), day in zip(labels, days, strict=True):
            assert DAY_LABEL_RE.match(text), f"{view_name}: формат {text!r}"
            assert data == f"cal_day:{day.isoformat()}", f"{view_name}: callback"
            assert text == calendar_view.format_day(day), f"{view_name}: подпись"
            assert today <= day <= horizon, f"{view_name}: дата вне горизонта"
            result["day_labels"][day.isoformat()] = text
            result["callbacks"].add(data)
            result["callbacks"].update(callbacks(h))
        result["day_views"].append((view_name, days))

    # —— текущая и следующая недели ————————————————————————————————
    await h.callback("cal_week")
    start, end = calendar_view.current_week(today)
    current_days = day_dates(h)
    assert current_days == calendar_view.days_between(start, end)
    collect_view(current_days, "current_week")
    await h.callback("cal_root")

    await h.callback("cal_nextweek")
    start, end = calendar_view.next_week(today)
    next_days = day_dates(h)
    assert next_days == calendar_view.days_between(start, end)
    collect_view(next_days, "next_week")
    await h.callback("cal_root")

    # —— «Другая»: диапазоны → недели → дни ————————————————————————
    await h.callback("cal_other")
    for index, (range_start, range_end) in enumerate(calendar_view.other_ranges(today)):
        await h.callback(f"cal_range:{index}")
        week_buttons = [
            (text, data)
            for text, data in h.screen_buttons()
            if data and data.startswith("cal_rweek:")
        ]
        expected_weeks = calendar_view.weeks_covering(range_start, range_end)
        assert len(week_buttons) == len(expected_weeks), "число недель диапазона"
        range_days: list[date] = []
        for (text, data), (week_start, week_end) in zip(
            week_buttons, expected_weeks, strict=True
        ):
            assert text == calendar_view.format_range(week_start, week_end)
            assert data == f"cal_rweek:{index}:{week_start.isoformat()}"
            result["callbacks"].add(data)
            await h.callback(data)
            days = day_dates(h)
            assert days == calendar_view.days_between(week_start, week_end)
            assert all(range_start <= day <= range_end for day in days)
            assert all(week_start <= day <= week_end for day in days)
            collect_view(days, f"range_{index}_week_{week_start}")
            range_days.extend(days)
            await h.callback(f"cal_range:{index}")
        assert range_days == calendar_view.days_between(range_start, range_end)
        result["days_by_range"][index] = range_days
        await h.callback("cal_other")

    # —— «Дальше»: пары месяцев → месяц → недели → дни —————————————
    await h.callback("cal_months:0")
    pair_index = 0
    while True:
        pair_months = [
            parse_month(data) for data in callbacks(h) if data.startswith("cal_month:")
        ]
        assert 1 <= len(pair_months) <= 2, "в паре один или два месяца"
        result["pair_sizes"].append(len(pair_months))
        result["month_keys"].extend(pair_months)

        for key in pair_months:
            month_data = f"cal_month:{key[0]}-{key[1]:02d}"
            result["callbacks"].add(month_data)
            await h.callback(month_data)
            assert calendar_view.format_month(key) in h.screen_text()

            month_week_buttons = [
                (text, data)
                for text, data in h.screen_buttons()
                if data and data.startswith("cal_mweek:")
            ]
            month_start, month_end = calendar_view.month_bounds(key)
            first = max(month_start, today)
            last = min(month_end, horizon)
            month_days: list[date] = []
            for text, data in month_week_buttons:
                week_start = date.fromisoformat(data.split(":", 1)[1])
                week_monday = week_start - timedelta(days=week_start.weekday())
                week_end = min(week_monday + timedelta(days=6), last)
                assert text == calendar_view.format_range(
                    week_start, week_end
                ), f"подпись недели месяца {key}"
                result["callbacks"].add(data)
                await h.callback(data)
                days = day_dates(h)
                assert days, "пустая неделя месяца"
                assert all(day.month == key[1] and day.year == key[0] for day in days)
                assert all(day >= today and day <= horizon for day in days)
                assert all(week_start <= day <= week_end for day in days)
                collect_view(days, f"month_{key[0]}_{key[1]}_week_{week_start}")
                month_days.extend(days)
                await h.callback(month_data)
            assert month_days == calendar_view.days_between(
                first, last
            ), f"месяц {key}: недели покрывают ровно свои доступные дни"
            result["days_by_month"][key] = month_days
            await h.callback(f"cal_months:{pair_index}")

        next_data = f"cal_months:{pair_index + 1}"
        if next_data in callbacks(h):
            await h.callback(next_data)
            pair_index += 1
        else:
            break

    result["pair_count"] = pair_index + 1
    return result


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarUiCoverageTests(unittest.TestCase):
    """Полнота календаря на уровне отрисованных кнопок."""

    @scenario_test
    async def test_every_horizon_date_is_rendered_exactly_by_calendar_views(self, h):
        with frozen_today(2026, 10, 22):
            today = date(2026, 10, 22)
            horizon = calendar_view.horizon_end(today)

            # длинный «враждебный» заголовок не должен попасть в callback-данные
            hostile_title = "Очень длинное название " * 20
            await h.start_dialog()
            await h.text(hostile_title)

            collected = await collect_all_views(h, today)

            expected_days = calendar_view.days_between(today, horizon)
            rendered_days = sorted(
                date.fromisoformat(iso) for iso in collected["day_labels"]
            )
            self.assertEqual(
                rendered_days,
                expected_days,
                "объединение всех уровней календаря — весь горизонт без пропусков",
            )

            # месяцы: с месяца первой неохваченной даты по месяц горизонта
            first_key = (
                calendar_view.first_uncovered(today).year,
                calendar_view.first_uncovered(today).month,
            )
            expected_months = []
            current = first_key
            while current <= (horizon.year, horizon.month):
                expected_months.append(current)
                current = calendar_view.add_months_key(current, 1)
            self.assertEqual(collected["month_keys"], expected_months)
            self.assertEqual(sum(collected["pair_sizes"]), len(expected_months))
            self.assertTrue(all(size in (1, 2) for size in collected["pair_sizes"]))
            self.assertEqual(
                collected["pair_count"],
                (len(expected_months) + 1) // 2,
            )

            # ни одной даты вне горизонта и ни одного дня без кнопки
            self.assertTrue(
                all(
                    today <= day <= horizon
                    for _, days in collected["day_views"]
                    for day in days
                )
            )
            self.assertEqual(
                set(collected["day_labels"]),
                {day.isoformat() for day in expected_days},
            )

    @scenario_test
    async def test_callback_data_is_short_and_has_no_user_text(self, h):
        with frozen_today(2026, 10, 22):
            today = date(2026, 10, 22)
            hostile_title = "Текст пользователя " * 25
            await h.start_dialog()
            await h.text(hostile_title)
            collected = await collect_all_views(h, today)

            for data in collected["callbacks"]:
                self.assertLessEqual(
                    len(data.encode("utf-8")),
                    64,
                    f"callback длиннее лимита Telegram: {data!r}",
                )
                self.assertNotIn("Текст пользователя", data)
                self.assertRegex(data, r"^cal_[a-z0-9:_-]+$")

            for iso in collected["day_labels"]:
                self.assertRegex(f"cal_day:{iso}", r"^cal_day:\d{4}-\d{2}-\d{2}$")

    @scenario_test
    async def test_every_horizon_date_is_accepted_by_handler(self, h):
        """Каждая дата горизонта реально сохраняется в поле «Дата»."""
        with frozen_today(2026, 10, 22):
            today = date(2026, 10, 22)
            horizon = calendar_view.horizon_end(today)

            await h.start_dialog()
            await h.text("Концерт")
            date_index = next(
                index for index, step in enumerate(POST_STEPS) if step["key"] == "date"
            )
            time_index = next(
                index
                for index, step in enumerate(POST_STEPS)
                if step["key"] == "time_start"
            )
            sends = len(screen_sends(h))
            answers = len(h.answers())

            day = today
            checked = 0
            while day <= horizon:
                await h.callback(f"cal_day:{day.isoformat()}")
                self.assertEqual(h.user_data().get("date"), day.strftime("%d.%m.%Y"))
                self.assertEqual(h.user_data()["current_step"], time_index)
                self.assertEqual(h.dialog_state(), POST_CREATION)
                await h.callback("back")
                self.assertEqual(h.user_data()["current_step"], date_index)
                day += timedelta(days=1)
                checked += 1

            self.assertEqual(checked, len(calendar_view.days_between(today, horizon)))
            self.assertEqual(len(h.answers()), answers + 2 * checked)
            self.assertEqual(len(screen_sends(h)), sends, "экран не пересоздавался")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ScreenIntegrityTests(unittest.TestCase):
    """Один экран, один ответ на callback, нет пустых экранов и лишних сообщений."""

    @scenario_test
    async def test_navigation_changes_existing_screen_only(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            sends = len(screen_sends(h))
            self.assertEqual(sends, 1)

            sequence = [
                "cal_week",
                "cal_root",
                "cal_nextweek",
                "cal_root",
                "cal_other",
                "cal_range:0",
                "cal_rweek:0:2026-11-02",
                "cal_range:0",
                "cal_other",
                "cal_months:0",
                "cal_months:1",
                "cal_month:2027-01",
                "cal_mweek:2027-01-04",
                "cal_month:2027-01",
                "cal_months:1",
                "cal_months:0",
                "cal_other",
                "cal_root",
            ]
            previous = (h.screen_text(), tuple(h.screen_buttons()))
            answers = len(h.answers())
            for index, data in enumerate(sequence, start=1):
                await h.callback(data)
                text = h.screen_text()
                current = (text, tuple(h.screen_buttons()))
                self.assertTrue(text.strip(), f"пустой экран после {data}")
                self.assertNotEqual(current, previous, f"экран не изменился: {data}")
                self.assertEqual(
                    len(h.answers()),
                    answers + index,
                    f"на {data} должен быть ровно один answer",
                )
                previous = current

            self.assertEqual(len(screen_sends(h)), sends, "второго экрана нет")
            self.assertEqual(h.replies(), [], "отдельных сообщений бот не слал")

    @scenario_test
    async def test_back_button_returns_to_expected_level(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")

            await h.callback("cal_week")
            self.assertIn(("◀️ Назад", "cal_root"), h.screen_buttons())
            await h.callback("cal_root")
            self.assertIn(("На этой неделе", "cal_week"), h.screen_buttons())

            await h.callback("cal_other")
            await h.callback("cal_range:0")
            self.assertIn(("◀️ Назад", "cal_other"), h.screen_buttons())
            await h.callback("cal_other")
            self.assertIn(("Дальше", "cal_months:0"), h.screen_buttons())

            await h.callback("cal_months:0")
            await h.callback("cal_month:2026-11")
            self.assertIn(("◀️ Назад", "cal_months:0"), h.screen_buttons())
            await h.callback("cal_months:0")
            self.assertIn(("Ноябрь 2026", "cal_month:2026-11"), h.screen_buttons())

            await h.callback("cal_month:2026-11")
            await h.callback("cal_mweek:2026-11-30")
            self.assertIn(("◀️ Назад", "cal_month:2026-11"), h.screen_buttons())
            await h.callback("cal_month:2026-11")
            self.assertIn(("30.11", "cal_mweek:2026-11-30"), h.screen_buttons())

            # корень: кнопка «Назад» шага возвращает к заголовку (Stage 16)
            await h.callback("cal_root")
            await h.callback("back")
            self.assertTrue(h.screen_text().startswith("Шаг 1 из"))
            self.assertEqual(h.user_data()["current_step"], 0)

    @scenario_test
    async def test_current_and_next_week_overlap_correctly(self, h):
        """Стык недель: сегодняшняя заканчивается воскресеньем, следующая — пн–вс."""
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")

            await h.callback("cal_week")
            current_labels = [
                text for text, data in h.screen_buttons() if data.startswith("cal_day:")
            ]
            await h.callback("cal_root")
            await h.callback("cal_nextweek")
            next_labels = [
                text for text, data in h.screen_buttons() if data.startswith("cal_day:")
            ]

            self.assertEqual(current_labels[-1], "25.10 (вс)")
            self.assertEqual(next_labels[0], "26.10 (пн)")
            self.assertEqual(len(set(current_labels) & set(next_labels)), 0)
            self.assertEqual(len(current_labels) + len(next_labels), 11)


if __name__ == "__main__":
    unittest.main()
