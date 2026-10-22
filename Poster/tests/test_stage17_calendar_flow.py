# tests/test_stage17_calendar_flow.py
"""
Stage 17: кнопочный календарь даты — сценарии через Application.process_update.

Проверяется НАСТОЯЩАЯ цепочка обработчиков (bot.build_application) поверх
FakeBot и временной SQLite:
- три кнопки на шаге «Дата» и в промпте правки даты;
- «На этой неделе» / «На следующей неделе» (пн–вс, без прошедших дней);
- «Другая» → два двухнедельных диапазона → недели → дни;
- «Дальше» → пары месяцев, в том числе остаток ноября после диапазонов;
- месяц → недели месяца → дни (без выхода за месяц и горизонт);
- возврат на каждом уровне, изменение ТОЛЬКО существующего экрана;
- текстовый ввод при открытом календаре (дата и ошибка формата);
- WIP, рестарт на промежуточном уровне, правка даты из превью;
- устаревшие, повторные и подделанные callback;
- переход через полночь: прошедшая дата не сохраняется;
- сквозной сценарий: создание → календарь → черновик → открытие →
  правка даты кнопками → сохранение.

Запуск из каталога Poster:
    python -m unittest tests.test_stage17_calendar_flow -v
"""

import json
import unittest

from telegram.error import BadRequest

try:
    from handlers.post_creation import EDIT_FIELD, POST_CREATION, POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        frozen_today,
        goto_preview,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    """Индекс шага по ключу поля."""
    return next(i for i, step in enumerate(POST_STEPS) if step["key"] == key)


def step_title(key: str) -> str:
    """Заголовок экрана шага («Шаг N из 9 — Название»)."""
    index = step_index(key)
    return f"Шаг {index + 1} из {len(POST_STEPS)} — {POST_STEPS[index]['label']}"


def screen_sends(h) -> list[dict]:
    """Только СОЗДАННЫЕ сообщения экрана (не правки)."""
    return [call for call in h.screens() if call["type"] == "message"]


def buttons(h) -> list[tuple[str, str | None]]:
    """Кнопки текущего экрана."""
    return h.screen_buttons()


def datas(h) -> list[str | None]:
    """callback_data кнопок текущего экрана."""
    return [data for _, data in buttons(h)]


def last_answer(h) -> str:
    """Текст последнего ответа на callback (тост/алерт)."""
    return str(h.answers()[-1].get("text") or "")


def payload_date(h) -> str | None:
    """Дата из WIP-снимка в user_states (что реально сохранено в БД)."""
    states = h.user_states()
    if not states or not states[0].payload:
        return None
    return json.loads(states[0].payload).get("date")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarRootTests(unittest.TestCase):
    """Точки входа календаря: шаг создания и правка даты."""

    @scenario_test
    async def test_date_step_has_three_calendar_buttons(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")

            self.assertTrue(h.screen_text().startswith(step_title("date")))
            self.assertEqual(
                datas(h),
                [
                    "cal_week",
                    "cal_nextweek",
                    "cal_other",
                    "skip",
                    "back",
                    "main_menu",
                ],
            )
            self.assertIn(("На этой неделе", "cal_week"), buttons(h))
            self.assertIn(("На следующей неделе", "cal_nextweek"), buttons(h))
            self.assertIn(("Другая", "cal_other"), buttons(h))
            # прежние кнопки шага сохранены
            self.assertIn(("⏭ Пропустить", "skip"), buttons(h))
            self.assertIn(("◀️ Назад", "back"), buttons(h))
            self.assertIn(("🏠 Меню", "main_menu"), buttons(h))
            self.assertEqual(len(screen_sends(h)), 1, "один экран")

    @scenario_test
    async def test_date_edit_prompt_has_calendar(self, h):
        await goto_preview(h, title="Концерт")

        await h.callback("edit_post")
        await h.callback("edit_date")

        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        self.assertIn("Текущее значение:", h.screen_text())
        self.assertIn(("На этой неделе", "cal_week"), buttons(h))
        self.assertIn(("На следующей неделе", "cal_nextweek"), buttons(h))
        self.assertIn(("Другая", "cal_other"), buttons(h))
        self.assertIn(("Пропустить", "skip"), buttons(h))

    @scenario_test
    async def test_other_requires_no_parallel_scenario(self, h):
        """Календарь не меняет сценарий: шаг и состояние те же."""
        await h.start_dialog()
        await h.text("Концерт")
        await h.callback("cal_other")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data()["current_step"], step_index("date"))
        self.assertEqual(h.user_data().get("date"), None)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CurrentAndNextWeekTests(unittest.TestCase):
    """«На этой неделе» и «На следующей неделе» для 22.10.2026."""

    @scenario_test
    async def test_thursday_current_week_example(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")

            self.assertEqual(
                buttons(h),
                [
                    ("22.10 (чт)", "cal_day:2026-10-22"),
                    ("23.10 (пт)", "cal_day:2026-10-23"),
                    ("24.10 (сб)", "cal_day:2026-10-24"),
                    ("25.10 (вс)", "cal_day:2026-10-25"),
                    ("◀️ Назад", "cal_root"),
                ],
            )
            self.assertIn("22.10–25.10", h.screen_text())
            self.assertNotIn("21.10", str(buttons(h)), "прошедший день не показан")
            self.assertEqual(len(screen_sends(h)), 1, "экран не пересоздавался")

    @scenario_test
    async def test_monday_current_week_has_seven_days(self, h):
        with frozen_today(2026, 10, 19):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")

            day_buttons = [
                value for value in buttons(h) if value[1].startswith("cal_day:")
            ]
            self.assertEqual(len(day_buttons), 7)
            self.assertEqual(day_buttons[0][0], "19.10 (пн)")
            self.assertEqual(day_buttons[-1][0], "25.10 (вс)")

    @scenario_test
    async def test_wednesday_current_week(self, h):
        with frozen_today(2026, 10, 21):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")

            labels = [text for text, data in buttons(h) if data.startswith("cal_day:")]
            self.assertEqual(
                labels,
                ["21.10 (ср)", "22.10 (чт)", "23.10 (пт)", "24.10 (сб)", "25.10 (вс)"],
            )

    @scenario_test
    async def test_sunday_current_week_has_only_today(self, h):
        with frozen_today(2026, 10, 25):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")

            self.assertEqual(
                buttons(h),
                [
                    ("25.10 (вс)", "cal_day:2026-10-25"),
                    ("◀️ Назад", "cal_root"),
                ],
            )

    @scenario_test
    async def test_next_week_all_seven_days(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_nextweek")

            self.assertEqual(
                buttons(h),
                [
                    ("26.10 (пн)", "cal_day:2026-10-26"),
                    ("27.10 (вт)", "cal_day:2026-10-27"),
                    ("28.10 (ср)", "cal_day:2026-10-28"),
                    ("29.10 (чт)", "cal_day:2026-10-29"),
                    ("30.10 (пт)", "cal_day:2026-10-30"),
                    ("31.10 (сб)", "cal_day:2026-10-31"),
                    ("01.11 (вс)", "cal_day:2026-11-01"),
                    ("◀️ Назад", "cal_root"),
                ],
            )
            self.assertIn("26.10–01.11", h.screen_text())

    @scenario_test
    async def test_select_day_from_current_week_advances(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            sends = len(screen_sends(h))
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-23")

            self.assertEqual(h.user_data()["date"], "23.10.2026")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))
            self.assertTrue(h.screen_text().startswith(step_title("time_start")))
            self.assertEqual(len(screen_sends(h)), sends, "новых экранов нет")
            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(payload_date(h), "23.10.2026")

    @scenario_test
    async def test_back_from_week_and_next_week(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.callback("cal_root")
            self.assertTrue(h.screen_text().startswith(step_title("date")))
            self.assertIn(("На этой неделе", "cal_week"), buttons(h))

            await h.callback("cal_nextweek")
            await h.callback("cal_root")
            self.assertTrue(h.screen_text().startswith(step_title("date")))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class OtherRangesTests(unittest.TestCase):
    """«Другая»: диапазоны, их недели и дни."""

    @scenario_test
    async def test_other_shows_two_ranges_and_next(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")

            self.assertEqual(
                buttons(h),
                [
                    ("02.11–15.11", "cal_range:0"),
                    ("16.11–29.11", "cal_range:1"),
                    ("Дальше", "cal_months:0"),
                    ("◀️ Назад", "cal_root"),
                ],
            )
            self.assertIn("Другая дата", h.screen_text())

    @scenario_test
    async def test_range_shows_two_weeks(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_range:0")

            self.assertEqual(
                buttons(h),
                [
                    ("02.11–08.11", "cal_rweek:0:2026-11-02"),
                    ("09.11–15.11", "cal_rweek:0:2026-11-09"),
                    ("◀️ Назад", "cal_other"),
                ],
            )
            await h.callback("cal_range:1")
            self.assertEqual(
                buttons(h),
                [
                    ("16.11–22.11", "cal_rweek:1:2026-11-16"),
                    ("23.11–29.11", "cal_rweek:1:2026-11-23"),
                    ("◀️ Назад", "cal_other"),
                ],
            )

    @scenario_test
    async def test_week_shows_seven_days_and_back(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_range:0")
            await h.callback("cal_rweek:0:2026-11-02")

            labels = [text for text, data in buttons(h) if data.startswith("cal_day:")]
            self.assertEqual(
                labels,
                [
                    "02.11 (пн)",
                    "03.11 (вт)",
                    "04.11 (ср)",
                    "05.11 (чт)",
                    "06.11 (пт)",
                    "07.11 (сб)",
                    "08.11 (вс)",
                ],
            )
            self.assertIn(("◀️ Назад", "cal_range:0"), buttons(h))

            await h.callback("cal_range:0")
            self.assertIn(("02.11–08.11", "cal_rweek:0:2026-11-02"), buttons(h))
            await h.callback("cal_other")
            self.assertIn(("02.11–15.11", "cal_range:0"), buttons(h))

    @scenario_test
    async def test_select_day_from_range(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_range:1")
            await h.callback("cal_rweek:1:2026-11-23")
            await h.callback("cal_day:2026-11-29")

            self.assertEqual(h.user_data()["date"], "29.11.2026")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))
            self.assertEqual(payload_date(h), "29.11.2026")

    @scenario_test
    async def test_year_boundary_range_is_understandable(self, h):
        with frozen_today(2026, 12, 15):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")

            self.assertIn(("28.12.2026–10.01.2027", "cal_range:0"), buttons(h))
            await h.callback("cal_range:0")
            labels = [text for text, _ in buttons(h)]
            self.assertIn("28.12.2026–03.01.2027", labels)
            self.assertIn("04.01–10.01", labels)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MonthsLevelTests(unittest.TestCase):
    """Пары месяцев, остаток ноября, недели месяцев и «Дальше»."""

    @scenario_test
    async def test_first_pair_is_november_december(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")

            self.assertEqual(
                buttons(h),
                [
                    ("Ноябрь 2026", "cal_month:2026-11"),
                    ("Декабрь 2026", "cal_month:2026-12"),
                    ("Дальше", "cal_months:1"),
                    ("◀️ Назад", "cal_other"),
                ],
            )
            self.assertIn("Выберите месяц", h.screen_text())

    @scenario_test
    async def test_november_keeps_thirtieth_after_ranges(self, h):
        """30.11 доступно: диапазоны закончились 29.11, пропуска нет."""
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")
            await h.callback("cal_month:2026-11")

            labels = [text for text, _ in buttons(h)]
            self.assertIn("30.11", labels)
            self.assertIn("23.11–29.11", labels)
            # первая неполная неделя ноября не уходит в октябрь
            self.assertIn("01.11", labels)
            self.assertNotIn("26.10–01.11", labels)

            # «Назад» ведёт к паре месяцев, а не на тот же уровень
            self.assertIn(("◀️ Назад", "cal_months:0"), buttons(h))

            await h.callback("cal_mweek:2026-11-30")
            self.assertEqual(
                buttons(h),
                [
                    ("30.11 (пн)", "cal_day:2026-11-30"),
                    ("◀️ Назад", "cal_month:2026-11"),
                ],
            )
            await h.callback("cal_day:2026-11-30")
            self.assertEqual(h.user_data()["date"], "30.11.2026")

    @scenario_test
    async def test_months_pagination_forward_and_back(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")
            await h.callback("cal_months:1")

            self.assertIn(("Январь 2027", "cal_month:2027-01"), buttons(h))
            self.assertIn(("Февраль 2027", "cal_month:2027-02"), buttons(h))
            self.assertIn(("◀️ Назад", "cal_months:0"), buttons(h))

            await h.callback("cal_months:0")
            self.assertIn(("Ноябрь 2026", "cal_month:2026-11"), buttons(h))
            self.assertIn(("◀️ Назад", "cal_other"), buttons(h))

    @scenario_test
    async def test_last_pair_has_no_next_and_clips_october(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            for index in range(1, 6):
                await h.callback(f"cal_months:{index}")

            self.assertEqual(
                buttons(h),
                [
                    ("Сентябрь 2027", "cal_month:2027-09"),
                    ("Октябрь 2027", "cal_month:2027-10"),
                    ("◀️ Назад", "cal_months:4"),
                ],
            )
            self.assertNotIn("Дальше", str(buttons(h)))

            await h.callback("cal_month:2027-10")
            labels = [text for text, _ in buttons(h)]
            self.assertEqual(labels[0], "01.10–03.10")
            self.assertEqual(labels[-2], "18.10–22.10")
            await h.callback("cal_mweek:2027-10-18")
            self.assertEqual(
                buttons(h)[:-1],
                [
                    ("18.10 (пн)", "cal_day:2027-10-18"),
                    ("19.10 (вт)", "cal_day:2027-10-19"),
                    ("20.10 (ср)", "cal_day:2027-10-20"),
                    ("21.10 (чт)", "cal_day:2027-10-21"),
                    ("22.10 (пт)", "cal_day:2027-10-22"),
                ],
            )
            self.assertNotIn("23.10 (сб)", str(buttons(h)))

    @scenario_test
    async def test_month_back_returns_to_own_pair(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:2")
            await h.callback("cal_month:2027-03")
            await h.callback("cal_months:2")
            self.assertIn(("Март 2027", "cal_month:2027-03"), buttons(h))

            await h.callback("cal_month:2027-04")
            self.assertIn(("◀️ Назад", "cal_months:2"), buttons(h))

    @scenario_test
    async def test_leap_february_2028_from_months(self, h):
        """Високосный февраль 2028: неделя 28.02–29.02 и выбор 29 февраля."""
        with frozen_today(2027, 12, 15):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")

            self.assertIn(("Январь 2028", "cal_month:2028-01"), buttons(h))
            self.assertIn(("Февраль 2028", "cal_month:2028-02"), buttons(h))
            await h.callback("cal_month:2028-02")
            labels = [text for text, _ in buttons(h)]
            self.assertIn("28.02–29.02", labels)
            await h.callback("cal_mweek:2028-02-28")
            self.assertEqual(
                buttons(h)[:-1],
                [
                    ("28.02 (пн)", "cal_day:2028-02-28"),
                    ("29.02 (вт)", "cal_day:2028-02-29"),
                ],
            )
            await h.callback("cal_day:2028-02-29")
            self.assertEqual(h.user_data()["date"], "29.02.2028")

    @scenario_test
    async def test_horizon_february_2029_is_not_leap(self, h):
        """Февраль 2029 за горизонтом: виден только 01.02 (горизонт включителен)."""
        with frozen_today(2028, 2, 1):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            # пары начинаются с месяца первой неохваченной даты — марта 2028;
            # до пары с февралём 2029 добираемся кнопкой «Дальше»
            seen_february = False
            for index in range(12):
                if index > 0 and f"cal_months:{index}" not in datas(h):
                    break
                await h.callback(f"cal_months:{index}")
                if ("Февраль 2029", "cal_month:2029-02") in buttons(h):
                    seen_february = True
                    break
            self.assertTrue(seen_february, "февраль 2029 должен быть в парах")
            await h.callback("cal_month:2029-02")
            labels = [text for text, _ in buttons(h)]
            self.assertEqual(labels[0], "01.02")
            self.assertNotIn("29.02", labels, "2029 не високосный")

    @scenario_test
    async def test_single_month_final_pair_has_no_empty_month(self, h):
        """Последняя пара из одного месяца: второй (пустой) не показывается."""
        with frozen_today(2026, 10, 31):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")
            self.assertEqual(buttons(h)[0][0], "Декабрь 2026")

            for index in range(1, 6):
                await h.callback(f"cal_months:{index}")
            self.assertEqual(
                buttons(h),
                [
                    ("Октябрь 2027", "cal_month:2027-10"),
                    ("◀️ Назад", "cal_months:4"),
                ],
            )

            await h.callback("cal_month:2027-10")
            labels = [text for text, _ in buttons(h)]
            self.assertIn("25.10–31.10", labels)
            await h.callback("cal_mweek:2027-10-25")
            self.assertEqual(
                len([data for _, data in buttons(h) if data.startswith("cal_day:")]),
                7,
            )
            await h.callback("cal_day:2027-10-31")
            self.assertEqual(h.user_data()["date"], "31.10.2027")

    @scenario_test
    async def test_forged_month_outside_horizon_rejected(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            answers_before = len(h.answers())

            await h.callback("cal_month:2027-11")

            self.assertEqual(len(h.answers()), answers_before + 1)
            self.assertIn("устарела", last_answer(h))
            self.assertEqual(h.user_data().get("date"), None)
            self.assertEqual(h.user_data()["current_step"], step_index("date"))
            self.assertTrue(h.screen_text().startswith(step_title("date")))

    @scenario_test
    async def test_forged_pair_index_rejected(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_months:99")

            self.assertIn("устарела", last_answer(h))
            self.assertEqual(h.user_data().get("date"), None)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TextInputWhileCalendarOpenTests(unittest.TestCase):
    """Текстовый ввод продолжает работать при открытом календаре."""

    @scenario_test
    async def test_text_date_accepted_while_calendar_open(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")

            # пользователь передумал и просто написал дату
            await h.text("17 ноября")

            self.assertEqual(h.user_data()["date"], "17.11.2026")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))
            self.assertTrue(h.screen_text().startswith(step_title("time_start")))

    @scenario_test
    async def test_compact_date_accepted_while_calendar_open(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.text("1711")
            self.assertEqual(h.user_data()["date"], "17.11.2026")

    @scenario_test
    async def test_invalid_text_keeps_calendar_available(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.text("не дата")

            self.assertIn("Некорректный формат", h.screen_text())
            self.assertEqual(h.user_data()["current_step"], step_index("date"))
            self.assertIn(("На этой неделе", "cal_week"), buttons(h))
            failed = h.user_data().get("failed_input_messages", {}).get("date", [])
            self.assertEqual(len(failed), 1)

            # выбор кнопкой принимает поле → неудачный ввод уходит в очистку
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-23")
            self.assertIn(failed[0], h.deleted_ids())
            self.assertNotIn("failed_input_messages", h.user_data())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersistenceAndRestartTests(unittest.TestCase):
    """WIP и рестарт на промежуточном уровне календаря."""

    @scenario_test
    async def test_restart_mid_calendar_restores_date_step(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_range:0")
            await h.callback("cal_rweek:0:2026-11-02")
            sends_before = len(screen_sends(h))

            await h.restart()
            await h.text("/start")
            self.assertIn(("▶️ Продолжить пост", "restore_wip"), buttons(h))
            await h.callback("restore_wip")

            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(h.user_data()["current_step"], step_index("date"))
            self.assertEqual(h.user_data().get("title"), "Концерт")
            self.assertTrue(h.screen_text().startswith(step_title("date")))
            self.assertIn(("На этой неделе", "cal_week"), buttons(h))
            self.assertEqual(
                len(screen_sends(h)),
                sends_before,
                "экран редактируется, не дублируется",
            )

            # после рестарта календарь продолжает работать
            await h.callback("cal_nextweek")
            await h.callback("cal_day:2026-10-30")
            self.assertEqual(h.user_data()["date"], "30.10.2026")
            self.assertEqual(payload_date(h), "30.10.2026")

    @scenario_test
    async def test_wip_keeps_selected_date_after_restart(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-24")
            self.assertEqual(payload_date(h), "24.10.2026")

            await h.restart()
            await h.callback("restore_wip")

            self.assertEqual(h.user_data()["date"], "24.10.2026")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))
            self.assertTrue(h.screen_text().startswith(step_title("time_start")))

    @scenario_test
    async def test_restart_during_date_edit_restores_calendar(self, h):
        with frozen_today(2026, 10, 22):
            await goto_preview(h, title="Концерт")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("cal_other")

            await h.restart()
            await h.callback("restore_wip")

            self.assertEqual(h.dialog_state(), EDIT_FIELD)
            self.assertEqual(h.user_data().get("edit_field"), "date")
            self.assertIn("Текущее значение:", h.screen_text())
            self.assertIn(("На этой неделе", "cal_week"), buttons(h))

            await h.callback("cal_nextweek")
            await h.callback("cal_day:2026-10-30")
            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(h.user_data()["date"], "30.10.2026")
            self.assertIn("Дата</b>: 30.10.2026", h.posts()[-1])

    @scenario_test
    async def test_restart_after_calendar_date_edit_shows_preview(self, h):
        """Завершённая правка даты кнопкой не возвращается после рестарта."""
        with frozen_today(2026, 10, 22):
            await goto_preview(h, title="Концерт")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("cal_nextweek")
            await h.callback("cal_day:2026-10-30")
            self.assertNotIn("edit_field", h.user_data())

            await h.restart()
            await h.callback("restore_wip")

            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertNotIn("edit_field", h.user_data())
            self.assertIn("Пост готов к действию", h.screen_text())
            self.assertIn("Дата</b>: 30.10.2026", h.posts()[-1])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StaleAndForgedCallbackTests(unittest.TestCase):
    """Устаревшие, повторные и подделанные callback календаря."""

    @scenario_test
    async def test_stale_calendar_after_day_selected(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-23")
            date_before = h.user_data()["date"]

            await h.callback("cal_day:2026-10-24")

            self.assertIn("устарела", last_answer(h))
            self.assertEqual(h.user_data()["date"], date_before)
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))
            self.assertIn("Время начала", h.screen_text())

    @scenario_test
    async def test_stale_calendar_after_dialog_end(self, h):
        await goto_preview(h, title="Концерт")
        await h.callback("save_draft")
        self.assertIsNone(h.dialog_state())

        await h.callback("cal_week")

        self.assertIn("Кнопка устарела", last_answer(h))
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_screen_recovery_when_edit_unavailable(self, h):
        """Экран удалён/недоступен: календарь безопасно создаёт новый экран."""
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            old_id = h.screen_message_id()
            sends = len(screen_sends(h))

            h.bot.errors_by_type["edit_message"] = BadRequest(
                "Message to edit not found"
            )
            await h.callback("cal_week")
            h.bot.errors_by_type.pop("edit_message", None)

            new_id = h.screen_message_id()
            self.assertNotEqual(new_id, old_id, "экран пересоздан")
            self.assertEqual(len(screen_sends(h)), sends + 1)
            self.assertIn("Текущая неделя", h.screen_text())

            # дальнейшая навигация и выбор даты правят уже новый экран
            await h.callback("cal_day:2026-10-23")
            self.assertEqual(h.screen_message_id(), new_id)
            self.assertEqual(h.user_data()["date"], "23.10.2026")

            # ref нового экрана сохранён в WIP: рестарт продолжает с него
            await h.restart()
            await h.callback("restore_wip")
            self.assertEqual(h.screen_message_id(), new_id)
            self.assertTrue(h.screen_text().startswith(step_title("time_start")))

    @scenario_test
    async def test_repeated_calendar_callback_is_safe(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            sends = len(screen_sends(h))
            await h.callback("cal_week")
            await h.callback("cal_week")

            self.assertEqual(len(screen_sends(h)), sends, "дубля экрана нет")
            self.assertEqual(
                h.screen_text().splitlines()[0],
                "📅 Текущая неделя: 22.10–25.10. Выберите дату:",
            )
            self.assertEqual(len(h.answers()), 2)

    @scenario_test
    async def test_forged_past_day_rejected(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_day:2026-10-21")

            self.assertIn("уже прошла", last_answer(h))
            self.assertIsNone(h.user_data().get("date"))
            self.assertEqual(h.user_data()["current_step"], step_index("date"))

    @scenario_test
    async def test_forged_day_beyond_horizon_rejected(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_day:2027-10-23")

            self.assertIn("за пределами горизонта", last_answer(h))
            self.assertIsNone(h.user_data().get("date"))
            self.assertEqual(h.user_data()["current_step"], step_index("date"))

    @scenario_test
    async def test_horizon_last_day_is_accepted(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_day:2027-10-22")

            self.assertEqual(h.user_data()["date"], "22.10.2027")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))

    @scenario_test
    async def test_malformed_callbacks_rejected_safely(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            calls = (
                "cal_day:not-a-date",
                "cal_range:9",
                "cal_rweek:0:2026-11-03",  # не начало недели диапазона
                "cal_month:2026-13",
                "cal_mweek:2026-11-15",  # не начало недели месяца
                "cal_unknown",
            )
            for data in calls:
                with self.subTest(data=data):
                    await h.callback(data)
                    self.assertTrue(last_answer(h))
                    self.assertIsNone(h.user_data().get("date"))
                    self.assertEqual(h.user_data()["current_step"], step_index("date"))
                    self.assertTrue(h.screen_text().startswith(step_title("date")))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MidnightRolloverTests(unittest.TestCase):
    """Календарь открыт до полуночи, кнопка нажата после."""

    @scenario_test
    async def test_day_that_became_past_is_not_saved(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            self.assertIn(("22.10 (чт)", "cal_day:2026-10-22"), buttons(h))

        with frozen_today(2026, 10, 23):
            await h.callback("cal_day:2026-10-22")  # вчерашняя кнопка

            self.assertIn("уже прошла", last_answer(h))
            self.assertIsNone(h.user_data().get("date"), "прошедшая дата не сохранена")
            self.assertEqual(h.user_data()["current_step"], step_index("date"))
            self.assertTrue(h.screen_text().startswith(step_title("date")))

            # корень обновлён: новая текущая неделя начинается с 23.10
            await h.callback("cal_week")
            labels = [text for text, _ in buttons(h)]
            self.assertEqual(labels[0], "23.10 (пт)")
            self.assertNotIn("22.10 (чт)", labels)

    @scenario_test
    async def test_still_valid_day_after_midnight_is_saved(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_nextweek")

        with frozen_today(2026, 10, 23):
            await h.callback("cal_day:2026-10-27")

            self.assertEqual(h.user_data()["date"], "27.10.2026")
            self.assertEqual(h.user_data()["current_step"], step_index("time_start"))

    @scenario_test
    async def test_month_boundary_after_midnight(self, h):
        with frozen_today(2026, 10, 31):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_other")
            await h.callback("cal_months:0")

        with frozen_today(2026, 11, 1):
            await h.callback("cal_day:2026-10-31")
            self.assertIn("уже прошла", last_answer(h))
            self.assertIsNone(h.user_data().get("date"))

            await h.callback("cal_day:2026-11-01")
            self.assertEqual(h.user_data()["date"], "01.11.2026")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EditDateCalendarTests(unittest.TestCase):
    """Правка даты из превью: кнопки и текстовый ввод дают одно поле."""

    @scenario_test
    async def test_edit_date_via_calendar_updates_preview(self, h):
        with frozen_today(2026, 10, 22):
            await goto_preview(h, title="Концерт")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("cal_nextweek")
            await h.callback("cal_day:2026-10-30")

            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(h.user_data()["date"], "30.10.2026")
            self.assertNotIn("edit_field", h.user_data())
            self.assertIn("Дата</b>: 30.10.2026", h.posts()[-1])
            self.assertIn("Пост готов к действию", h.screen_text())

    @scenario_test
    async def test_edit_date_text_still_works(self, h):
        with frozen_today(2026, 10, 22):
            await goto_preview(h, title="Концерт")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.text("17 ноября")

            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(h.user_data()["date"], "17.11.2026")
            self.assertIn("Дата</b>: 17.11.2026", h.posts()[-1])

    @scenario_test
    async def test_edit_date_text_while_calendar_open(self, h):
        """Правка даты текстом работает и когда открыт кнопочный календарь."""
        with frozen_today(2026, 10, 22):
            await goto_preview(h, title="Концерт")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("cal_other")
            await h.callback("cal_months:0")
            await h.text("17 ноября")

            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertEqual(h.user_data()["date"], "17.11.2026")
            self.assertNotIn("edit_field", h.user_data())
            self.assertIn("Дата</b>: 17.11.2026", h.posts()[-1])

    @scenario_test
    async def test_skip_in_date_edit_clears_field(self, h):
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-23")
            for _ in range(len(POST_STEPS) - step_index("time_start")):
                await h.text("Пропустить")
            self.assertIn("Дата</b>: 23.10.2026", h.posts()[-1])

            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("skip")

            self.assertIsNone(h.user_data().get("date"))
            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertNotIn("Дата</b>:", h.posts()[-1])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FullCalendarScenarioTests(unittest.TestCase):
    """Сквозной сценарий: создание кнопками → черновик → правка даты."""

    @scenario_test
    async def test_create_save_open_edit_date_via_calendar(self, h):
        with frozen_today(2026, 10, 22):
            await h.text("/start")
            await h.callback("create_post")
            await h.text("Концерт")

            # дата кнопками: Другая → первый диапазон → неделя → день
            await h.callback("cal_other")
            await h.callback("cal_range:0")
            await h.callback("cal_rweek:0:2026-11-02")
            await h.callback("cal_day:2026-11-02")
            self.assertEqual(h.user_data()["date"], "02.11.2026")

            # остальные шаги пропускаем → превью
            for _ in range(len(POST_STEPS) - step_index("time_start")):
                await h.text("Пропустить")
            self.assertIn("Пост готов к действию", h.screen_text())
            self.assertIn("Дата</b>: 02.11.2026", h.posts()[-1])

            # сохранение в черновики
            await h.callback("save_draft")
            self.assertIn("Пост сохранён в черновики", h.screen_text())
            self.assertIsNone(h.dialog_state())
            drafts = h.drafts()
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].date, "02.11.2026")

            # открытие черновика и правка даты кнопками
            await h.text("/drafts")
            self.assertIn("Ваши черновики", h.screen_text())
            await h.callback("editdraft_1")
            self.assertEqual(h.user_data()["date"], "02.11.2026")
            await h.callback("edit_post")
            await h.callback("edit_date")
            await h.callback("cal_nextweek")
            self.assertIn(("30.10 (пт)", "cal_day:2026-10-30"), buttons(h))
            await h.callback("cal_day:2026-10-30")

            self.assertEqual(h.posts()[-1].count("30.10.2026"), 1)
            await h.callback("save_draft")
            self.assertIn("Черновик 1 обновлён", h.screen_text())
            drafts = h.drafts()
            self.assertEqual(len(drafts), 1, "обновление без копий")
            self.assertEqual(drafts[0].date, "30.10.2026")

    @scenario_test
    async def test_calendar_day_equals_text_input_state(self, h):
        """Кнопка и текст дают одинаковое состояние данных."""
        with frozen_today(2026, 10, 22):
            await h.start_dialog()
            await h.text("Концерт")
            await h.callback("cal_week")
            await h.callback("cal_day:2026-10-23")
            via_button = {
                key: h.user_data().get(key)
                for key in ("title", "date", "current_step", "editing_draft_id")
            }
            button_payload = payload_date(h)

            # возвращаемся на шаг даты и вводим ту же дату текстом
            await h.callback("back")
            self.assertEqual(h.user_data()["current_step"], step_index("date"))
            await h.text("23.10.2026")
            via_text = {
                key: h.user_data().get(key)
                for key in ("title", "date", "current_step", "editing_draft_id")
            }

            self.assertEqual(via_button, via_text)
            self.assertEqual(via_button["date"], "23.10.2026")
            self.assertEqual(button_payload, payload_date(h))


if __name__ == "__main__":
    unittest.main()
