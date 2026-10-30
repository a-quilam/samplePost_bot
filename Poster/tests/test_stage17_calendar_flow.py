# tests/test_stage17_calendar_flow.py
"""
Stage 17 (актуализировано в Poster 2.0 S5): кнопочный календарь даты
в конструкторе анонса.

Легаси-мастер удалён: календарь живёт в едином конструкторе, поэтому
сценарии идут через настоящее приложение (`bot.build_application`), FakeBot
и временную SQLite, а дата берётся из кирпичика типа `date`.

Проверяется:
- календарь сразу на вопросе даты (дни текущей недели, недели, «другая
  дата», месяцы, полный экран календаря);
- «На этой неделе» / «Следующей» — ровно дни календарной недели;
- «Другая дата» → двухнедельные диапазоны → дни и возвраты;
- месяцы → недели месяца → дни (без выхода за месяц и горизонт);
- выбранный день сохраняется в кирпичик, живой блок обновляется;
- текстовый ввод при открытом календаре, ошибка формата не теряет календарь;
- прошедшая/внегоризонтная/подделанная дата отклоняется, значение не пишется;
- рестарт на промежуточном экране восстанавливает вопрос с календарём;
- навигация меняет только существующий экран, один answer на callback.

Запуск из каталога Poster:
    python -m unittest tests.test_stage17_calendar_flow -v
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

try:
    from announcement_store import (
        build_announcement_fields,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        frozen_today,
        is_live_record,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import calendar_view

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

TODAY = date(2026, 10, 22)


def _seed_date_announcement(h, *, title: str = "Концерт") -> int:
    """Личный шаблон «[title] — [day]»: вопрос заголовка, затем даты."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="calendar")
        for code, brick_title, value_type in (
            ("title", "Заголовок", "short_text"),
            ("day", "Дата", "date"),
        ):
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=brick_title,
                value_type=value_type,
                rules={},
            )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="С датой",
            header="",
            body="[title] — [day]",
            footer="",
        )
        session.commit()
        return template.template_id


async def _open_date_question(h, *, title: str = "Концерт"):
    """Открывает анонс и доходит до вопроса даты (2 из 2)."""
    template_id = _seed_date_announcement(h, title=title)
    await h.text("/start")
    await h.callback(f"tp_pick:{template_id}")
    await h.text(title)
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
    """`draft_id:revision:brick_id` из callback'а дня."""
    return _callback(h, "aw_calday").split(":", 1)[1].rsplit(":", 1)[0]


def _day_buttons(h) -> list[date]:
    return [
        date.fromisoformat(data.rsplit(":", 1)[1])
        for data in _callbacks(h, "aw_calday")
    ]


def _answer_text(h) -> str:
    return str(h.answers()[-1].get("text") or "")


def _saved_values(h, *, today: date) -> dict[str, object]:
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
    return {field.code: field.raw_value for field in fields}


def _screen_sends(h) -> list[dict]:
    return [call for call in h.screens() if call["type"] == "message"]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarOnQuestionTests(unittest.TestCase):
    """Календарь виден сразу на вопросе даты — отдельного входа нет."""

    @scenario_test
    async def test_date_question_shows_current_week_and_navigation(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            text = h.screen_text()
            self.assertIn("Вопрос 2 из 2", text)
            self.assertIn("Выберите дату", text)

            # Дни текущей недели — кнопками, подписи календарные.
            self.assertEqual(
                _day_buttons(h),
                calendar_view.days_between(*calendar_view.current_week(TODAY)),
            )
            self.assertTrue(
                all(
                    data.startswith("aw_calday:") for data in _callbacks(h, "aw_calday")
                )
            )
            labels = [
                label
                for label, data in h.screen_buttons()
                if data and data.startswith("aw_calday")
            ]
            self.assertEqual(
                labels,
                [
                    calendar_view.format_day(day)
                    for day in calendar_view.days_between(
                        *calendar_view.current_week(TODAY)
                    )
                ],
            )
            # Следующая неделя, другая дата, месяцы и полный экран календаря.
            self.assertTrue(_callbacks(h, "aw_calweek"))
            self.assertTrue(_callbacks(h, "aw_calother"))
            self.assertTrue(_callbacks(h, "aw_calmonths"))
            self.assertTrue(_callbacks(h, "aw_calroot"))
            # Ручной ввод и пропуск сохранены.
            self.assertTrue(_callbacks(h, "aw_qskip"))
            self.assertTrue(_callbacks(h, "aw_qback"))
            # Экран один на диалог: шаги перерисовываются правкой, новый
            # send возможен только при смене сообщения-экрана.
            sends_after_date_question = len(_screen_sends(h))
            await h.callback(f"aw_calweek:{_field_prefix(h)}:cur")
            self.assertEqual(
                len(_screen_sends(h)),
                sends_after_date_question,
                "навигация календаря не создаёт новое сообщение-экран",
            )

    @scenario_test
    async def test_selected_day_is_saved_and_live_block_updates(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            chosen = TODAY + timedelta(days=3)
            await h.callback(f"aw_calday:{_field_prefix(h)}:{chosen.isoformat()}")

            self.assertEqual(
                _saved_values(h, today=TODAY)["day"], chosen.strftime("%d.%m.%Y")
            )
            self.assertIn("Фотографии анонса", h.screen_text())
            live = [
                call
                for call in h.calls
                if is_live_record(call) and call.get("reply_markup") is None
            ]
            self.assertTrue(live, "живой блок анонса не отправлен")
            self.assertIn(chosen.strftime("%d.%m.%Y"), live[-1].get("text") or "")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarNavigationTests(unittest.TestCase):
    """Недели, диапазоны и месяцы: ровно свои дни и корректные возвраты."""

    @scenario_test
    async def test_current_and_next_week_days(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)

            await h.callback(f"aw_calweek:{prefix}:cur")
            self.assertIn("Текущая неделя", h.screen_text())
            self.assertEqual(
                _day_buttons(h),
                calendar_view.days_between(*calendar_view.current_week(TODAY)),
            )
            self.assertIn(f"aw_calroot:{prefix}", _callbacks(h, "aw_calroot"))

            await h.callback(f"aw_calroot:{prefix}")
            await h.callback(f"aw_calweek:{prefix}:next")
            self.assertIn("Следующая неделя", h.screen_text())
            self.assertEqual(
                _day_buttons(h),
                calendar_view.days_between(*calendar_view.next_week(TODAY)),
            )
            # Недели стыкуются без повторных и пропущенных дней.
            current = calendar_view.days_between(*calendar_view.current_week(TODAY))
            following = calendar_view.days_between(*calendar_view.next_week(TODAY))
            self.assertEqual(current[-1] + timedelta(days=1), following[0])
            self.assertEqual(len(current) + len(following), 11)

    @scenario_test
    async def test_other_ranges_open_days_and_return(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            ranges = calendar_view.other_ranges(TODAY)
            self.assertEqual(len(ranges), 2)

            await h.callback(f"aw_calother:{prefix}")
            # callback диапазона несёт ИНДЕКС, а не дату: подписи сверяются
            # с calendar_view.other_ranges, а данные — короткие и стабильные.
            self.assertEqual(
                _callbacks(h, "aw_calrange"),
                [f"aw_calrange:{prefix}:{index}" for index in range(len(ranges))],
            )

            for index, (start, end) in enumerate(ranges):
                await h.callback(f"aw_calrange:{prefix}:{index}")
                self.assertEqual(
                    _day_buttons(h), calendar_view.days_between(start, end)
                )
                self.assertIn(f"aw_calother:{prefix}", _callbacks(h, "aw_calother"))
                await h.callback(f"aw_calother:{prefix}")

            # Возврат к вопросу из «Другой даты» — полный экран календаря.
            await h.callback(f"aw_calroot:{prefix}")
            self.assertIn(f"aw_calweek:{prefix}:cur", _callbacks(h, "aw_calweek"))

    @scenario_test
    async def test_months_open_weeks_of_month_and_return(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            horizon = calendar_view.horizon_end(TODAY)

            await h.callback(f"aw_calmonths:{prefix}")
            month_keys = [
                (int(data.rsplit(":", 2)[-2]), int(data.rsplit(":", 1)[-1]))
                for data in _callbacks(h, "aw_calmonth:")
            ]
            expected = []
            current = (
                calendar_view.first_uncovered(TODAY).year,
                calendar_view.first_uncovered(TODAY).month,
            )
            while current <= (horizon.year, horizon.month):
                expected.append(current)
                current = calendar_view.add_months_key(current, 1)
            self.assertEqual(month_keys, expected)

            key = expected[0]
            await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
            self.assertIn(
                f"{calendar_view.MONTH_NAMES[key[1]]} {key[0]}", h.screen_text()
            )
            weeks = calendar_view.month_weeks(key, (TODAY, horizon))
            self.assertTrue(weeks)
            self.assertEqual(len(_callbacks(h, "aw_calweekdays")), len(weeks))

            start, end = weeks[0]
            await h.callback(f"aw_calweekdays:{prefix}:{key[0]}:{key[1]}:0")
            self.assertEqual(_day_buttons(h), calendar_view.days_between(start, end))
            # Возврат на недели месяца и на список месяцев.
            await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
            self.assertIn("Выберите неделю", h.screen_text())
            await h.callback(f"aw_calmonths:{prefix}")
            self.assertTrue(_callbacks(h, "aw_calmonth:"))

    @scenario_test
    async def test_month_days_do_not_leave_month_or_horizon(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            horizon = calendar_view.horizon_end(TODAY)
            await h.callback(f"aw_calmonths:{prefix}")

            keys = [
                (int(data.rsplit(":", 2)[-2]), int(data.rsplit(":", 1)[-1]))
                for data in _callbacks(h, "aw_calmonth:")
            ]
            for key in keys:
                await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
                weeks = calendar_view.month_weeks(key, (TODAY, horizon))
                for week_index, (week_start, week_end) in enumerate(weeks):
                    await h.callback(
                        f"aw_calweekdays:{prefix}:{key[0]}:{key[1]}:{week_index}"
                    )
                    days = _day_buttons(h)
                    self.assertTrue(days, f"пустая неделя месяца {key}")
                    for day in days:
                        self.assertEqual((day.year, day.month), key)
                        self.assertTrue(TODAY <= day <= horizon)
                        self.assertTrue(week_start <= day <= week_end)
                    await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
                await h.callback(f"aw_calmonths:{prefix}")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarInputTests(unittest.TestCase):
    """Текстовый ввод и защита от недопустимых дат."""

    @scenario_test
    async def test_text_date_accepted_while_calendar_open(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            await h.callback(f"aw_calroot:{_field_prefix(h)}")
            typed = await h.text("31.12.2026")

            self.assertEqual(_saved_values(h, today=TODAY)["day"], "31.12.2026")
            self.assertIn("Фотографии анонса", h.screen_text())
            self.assertIn(typed.effective_message.message_id, h.deleted_ids())

    @scenario_test
    async def test_invalid_text_keeps_calendar_available(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            bad = await h.text("31.02.2026")

            self.assertIn("⚠️", h.screen_text())
            self.assertIn("Вопрос 2 из 2", h.screen_text())
            self.assertTrue(_callbacks(h, "aw_calday"), "календарь остался доступен")
            self.assertNotIn(
                bad.effective_message.message_id,
                h.deleted_ids(),
                "ошибочный ввод не удаляется",
            )
            self.assertIsNone(_saved_values(h, today=TODAY)["day"])

    @scenario_test
    async def test_past_and_beyond_horizon_days_are_rejected(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            horizon = calendar_view.horizon_end(TODAY)

            await h.callback(
                f"aw_calday:{prefix}:{(TODAY - timedelta(days=1)).isoformat()}"
            )
            self.assertIn("вне допустимого диапазона", _answer_text(h))
            self.assertIsNone(_saved_values(h, today=TODAY)["day"])

            await h.callback(
                f"aw_calday:{prefix}:{(horizon + timedelta(days=1)).isoformat()}"
            )
            self.assertIn("вне допустимого диапазона", _answer_text(h))
            self.assertIsNone(_saved_values(h, today=TODAY)["day"])

            # Последний день горизонта — допустим.
            await h.callback(f"aw_calday:{prefix}:{horizon.isoformat()}")
            self.assertEqual(
                _saved_values(h, today=TODAY)["day"], horizon.strftime("%d.%m.%Y")
            )

    @scenario_test
    async def test_malformed_and_foreign_callbacks_are_safe(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            before = _saved_values(h, today=TODAY)

            for data in (
                f"aw_calday:{prefix}:not-a-date",
                f"aw_calday:{prefix}",
                "aw_calday:1",
                f"aw_calrange:{prefix}:9",
                f"aw_calrange:{prefix}:abc",
                f"aw_calweekdays:{prefix}:2026:11:9",
                f"aw_calmonth:{prefix}:2031:5",
                f"aw_calweek:{prefix}:unknown",
            ):
                await h.callback(data)
                self.assertTrue(_answer_text(h), f"нет ответа на {data}")
                self.assertIn("Вопрос 2 из 2", h.screen_text())
                self.assertEqual(_saved_values(h, today=TODAY), before)

    @scenario_test
    async def test_stale_callback_after_value_saved(self, h):
        """Кнопка дня с прошлым revision не перезаписывает значение."""
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            stale = _callback(h, "aw_calday")
            chosen = TODAY + timedelta(days=2)
            await h.callback(f"aw_calday:{_field_prefix(h)}:{chosen.isoformat()}")
            before = _saved_values(h, today=TODAY)

            await h.callback(stale)
            self.assertEqual(_saved_values(h, today=TODAY), before)
            self.assertIn("устарел", _answer_text(h).lower())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarScreenIntegrityTests(unittest.TestCase):
    """Один экран, один ответ на callback, без пустых экранов и лишних сообщений."""

    @scenario_test
    async def test_navigation_edits_existing_screen_only(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            sends = len(_screen_sends(h))
            answers = len(h.answers())
            sequence = [
                f"aw_calroot:{prefix}",
                f"aw_calweek:{prefix}:cur",
                f"aw_calroot:{prefix}",
                f"aw_calother:{prefix}",
                f"aw_calrange:{prefix}:0",
                f"aw_calother:{prefix}",
                f"aw_calmonths:{prefix}",
                f"aw_calmonth:{prefix}:2026:11",
                f"aw_calweekdays:{prefix}:2026:11:0",
                f"aw_calmonth:{prefix}:2026:11",
                f"aw_calmonths:{prefix}",
                f"aw_calroot:{prefix}",
                f"aw_calback:{prefix}",
            ]
            for index, data in enumerate(sequence, start=1):
                await h.callback(data)
                self.assertTrue(h.screen_text().strip(), f"пустой экран после {data}")
                self.assertEqual(
                    len(h.answers()),
                    answers + index,
                    f"на {data} должен быть ровно один answer",
                )

            self.assertEqual(len(_screen_sends(h)), sends, "экран не пересоздавался")
            # Единственное отдельное сообщение — живой блок анонса.
            self.assertEqual(len(h.replies()), 1)

    @scenario_test
    async def test_back_from_calendar_returns_to_question(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calroot:{prefix}")
            await h.callback(f"aw_calback:{prefix}")

            self.assertIn("Вопрос 2 из 2", h.screen_text())
            self.assertTrue(_callbacks(h, "aw_calday"))

    @scenario_test
    async def test_restart_restores_date_question_with_calendar(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h)
            prefix = _field_prefix(h)
            await h.callback(f"aw_calweek:{prefix}:next")
            draft_id = _field_prefix(h).split(":", 1)[0]

            await h.restart()
            await h.text("/start")
            await h.callback("my_announcements")
            await h.callback(f"aw_open:{draft_id}")

            self.assertIn("Вопрос 2 из 2", h.screen_text())
            # Промежуточный уровень календаря («следующая неделя») — чистая
            # навигация, а не данные анонса: в БД он не сохраняется. После
            # рестарта вопрос даты открывается с базового уровня текущей
            # недели (как при первом заходе), а не с покинутого экрана.
            self.assertEqual(
                _day_buttons(h),
                calendar_view.days_between(*calendar_view.current_week(TODAY)),
                "после рестарта календарь открывается с базового уровня",
            )
            # Сохранённое значение поля при этом не теряется.
            self.assertIsNone(_saved_values(h, today=TODAY)["day"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarHorizonCoverageTests(unittest.TestCase):
    """
    Полнота календаря на уровне отрисованных кнопок (перенесено из
    удалённого test_stage17_calendar_coverage.py, легаси-мастер S5).

    Обходятся ВСЕ уровни так, как это делает пользователь: дни на вопросе,
    недели, два диапазона, месяцы и недели месяцев. Собираются даты,
    подписи и callback-данные.
    """

    async def _collect(self, h, today: date) -> dict:
        horizon = calendar_view.horizon_end(today)
        result: dict = {"days": set(), "callbacks": set(), "groups": []}

        def collect(name: str) -> None:
            days = _day_buttons(h)
            labels = [
                label
                for label, data in h.screen_buttons()
                if data and data.startswith("aw_calday")
            ]
            self.assertEqual(
                labels,
                [calendar_view.format_day(day) for day in days],
                f"{name}: подписи кнопок-дней",
            )
            self.assertEqual(len(days), len(set(days)), f"{name}: повтор даты")
            self.assertTrue(
                all(today <= day <= horizon for day in days),
                f"{name}: дата вне горизонта",
            )
            result["days"].update(days)
            result["groups"].append((name, days))
            result["callbacks"].update(
                data for _label, data in h.screen_buttons() if data
            )

        collect("question")
        await h.callback(_callback(h, "aw_calweek"))
        collect("current_week")
        await h.callback(_callback(h, "aw_calroot"))

        await h.callback(_callback(h, "aw_calweek"))
        prefix = _field_prefix(h)
        await h.callback(f"aw_calweek:{prefix}:next")
        collect("next_week")
        await h.callback(_callback(h, "aw_calroot"))

        await h.callback(_callback(h, "aw_calother"))
        for index in range(len(_callbacks(h, "aw_calrange"))):
            await h.callback(f"aw_calrange:{prefix}:{index}")
            collect(f"range_{index}")
            await h.callback(f"aw_calother:{prefix}")
        await h.callback(f"aw_calroot:{prefix}")

        await h.callback(f"aw_calmonths:{prefix}")
        month_keys = [
            (int(data.rsplit(":", 2)[-2]), int(data.rsplit(":", 1)[-1]))
            for data in _callbacks(h, "aw_calmonth:")
        ]
        for key in month_keys:
            await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
            week_count = len(_callbacks(h, "aw_calweekdays"))
            for week_index in range(week_count):
                await h.callback(
                    f"aw_calweekdays:{prefix}:{key[0]}:{key[1]}:{week_index}"
                )
                collect(f"month_{key[0]}_{key[1]}_week_{week_index}")
                await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
            await h.callback(f"aw_calmonths:{prefix}")
        result["month_keys"] = month_keys
        return result

    @scenario_test
    async def test_every_horizon_date_is_reachable_by_buttons(self, h):
        with frozen_today(2026, 10, 22):
            today = TODAY
            horizon = calendar_view.horizon_end(today)
            await _open_date_question(h)

            collected = await self._collect(h, today)

            expected = calendar_view.days_between(today, horizon)
            self.assertEqual(
                sorted(collected["days"]),
                expected,
                "объединение всех уровней календаря — весь горизонт без пропусков",
            )
            # Месяцы начинаются с первой неохваченной даты и доходят до
            # месяца горизонта: стык «диапазоны → месяцы» непрерывен.
            expected_months = []
            current = (
                calendar_view.first_uncovered(today).year,
                calendar_view.first_uncovered(today).month,
            )
            while current <= (horizon.year, horizon.month):
                expected_months.append(current)
                current = calendar_view.add_months_key(current, 1)
            self.assertEqual(collected["month_keys"], expected_months)

    @scenario_test
    async def test_calendar_callbacks_are_short_and_free_of_user_text(self, h):
        with frozen_today(2026, 10, 22):
            await _open_date_question(h, title="Текст пользователя " * 12)

            collected = await self._collect(h, TODAY)

            for data in collected["callbacks"]:
                self.assertLessEqual(
                    len(data.encode("utf-8")),
                    64,
                    f"callback длиннее лимита Telegram: {data!r}",
                )
                self.assertNotIn("Текст пользователя", data)
                self.assertRegex(data, r"^aw_[a-z0-9:_-]+$")


if __name__ == "__main__":
    unittest.main()
