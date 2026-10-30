# tests/test_poster2_u2a_calendar_verify_tester.py
"""
Poster 2.2 U2a: независимая приёмка компактного календаря (взгляд тестировщика).

Файл написан отдельно от тестов разработчика (`test_poster2_u2a_calendar*`):
свои даты, свои фикстуры, проверка ФАКТИЧЕСКОГО поведения через настоящее
приложение, FakeBot и временный SQLite.

Проверяется по требованиям владельца:
1) воскресенье: ровно 8 быстрых дат (сегодня + 7), окно скользящее;
2) матрица первого экрана даты: 2×4 быстрых даты, одна строка из двух
   диапазонов, «📅 Выбрать месяц», внизу ровно «◀️ Назад» | «⏭ Пропустить»;
   старых кнопок и дублей callback_data нет;
3) конец месяца/года и високосный февраль: окно переходит корректно;
4) смена месяца посередине окна: 8 различных упорядоченных дат;
5) раскрытие диапазона: 7 дат на том же экране, возврат к быстрым датам;
6) месяцы: сетка на горизонт, дни месяца, стрелки в пределах горизонта,
   выбор дня сохраняется сразу;
7) негативы: legacy-callback, двойные нажатия, «Назад», «Пропустить»,
   даты вне горизонта, чужой пользователь;
8) понедельник и воскресенье дают одинаковую структуру экрана.

Дефекты, воспроизведённые внизу файла (`DefectReproductionTests`), НЕ
исправляются тестировщиком: они фиксируют ожидаемое поведение и падают,
пока дефект жив.

Запуск из каталога Poster:
    python -m unittest tests.test_poster2_u2a_calendar_verify_tester -v
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

# Чужой пользователь: не владелец черновика.
FOREIGN_USER_ID = 4242

SUNDAY = date(2026, 11, 1)


def _seed(h, *, rules: dict | None = None) -> int:
    """Личный шаблон «[title] — [day]»: заголовок, затем дата."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="verify-u2a")

        def _ensure(code: str, title: str, value_type: str, extra: dict) -> None:
            try:
                create_personal_brick(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    code=code,
                    title=title,
                    value_type=value_type,
                    rules=extra,
                )
            except Exception:  # noqa: BLE001 — кирпичик уже создан в этом тесте
                session.rollback()

        _ensure("title", "Заголовок", "short_text", {})
        _ensure("day", "Дата", "date", rules or {})
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="Приёмка U2a",
            header="",
            body="[title] — [day]",
            footer="",
        )
        session.commit()
        return template.template_id


async def _open(h, *, rules: dict | None = None) -> None:
    template_id = _seed(h, rules=rules)
    await h.text("/start")
    await h.callback(f"tp_pick:{template_id}")
    await h.text("Приёмка")


def _data(h, prefix: str) -> list[str]:
    return [
        value
        for _label, value in h.screen_buttons()
        if value and value.startswith(prefix)
    ]


def _first(h, prefix: str) -> str:
    values = _data(h, prefix)
    if not values:
        raise AssertionError(f"нет кнопки {prefix!r}; есть {h.screen_buttons()!r}")
    return values[0]


def _prefix(h) -> str:
    return _first(h, "aw_calday").split(":", 1)[1].rsplit(":", 1)[0]


def _days(h) -> list[date]:
    return [
        date.fromisoformat(value.rsplit(":", 1)[1]) for value in _data(h, "aw_calday")
    ]


def _rows(h) -> list[list[tuple[str, str | None]]]:
    markup = h.screen().get("reply_markup")
    if markup is None:
        raise AssertionError("у экрана нет клавиатуры")
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _labels(h) -> list[str]:
    return [label for label, _value in h.screen_buttons()]


def _answers(h) -> list[str]:
    return [str(item.get("text") or "") for item in h.answers()]


def _screen_sends(h) -> int:
    return len([call for call in h.screens() if call["type"] == "message"])


def _saved(h, *, today: date) -> dict:
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


def _structure(h) -> tuple:
    """Структура экрана без дат: форма строк, подписи диапазонов, навигация."""
    rows = _rows(h)
    return (
        tuple(len(row) for row in rows),
        tuple(
            label
            for row in rows
            for label, value in row
            if value and value.startswith("aw_calrange")
        ),
        tuple(
            action
            for _label, value in h.screen_buttons()
            if value
            for action in (value.split(":", 1)[0],)
        ),
    )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SundayQuickWindowTests(unittest.TestCase):
    """Требование 1: воскресенье даёт ровно 8 быстрых дат."""

    @scenario_test
    async def test_sunday_has_eight_quick_dates_starting_today(self, h):
        with frozen_today(2026, 11, 1):  # воскресенье
            await _open(h)
            days = _days(h)
            self.assertEqual(len(days), 8, f"воскресенье дало {len(days)} дат")
            self.assertEqual(
                days, calendar_view.days_between(SUNDAY, date(2026, 11, 8))
            )
            self.assertEqual(len(set(days)), 8)
            self.assertEqual(days, sorted(days))

    @scenario_test
    async def test_window_is_sliding_not_week_bound(self, h):
        """
        Понедельник и воскресенье: 8 дат и одна форма экрана, разные даты.

        Окно не привязано к календарной неделе, поэтому диапазоны скользят
        вместе с ним: сравниваются форма строк и число диапазонов в строке,
        а не их подписи (подписи как раз обязаны различаться).
        """
        with frozen_today(2026, 10, 26):  # понедельник
            await _open(h)
            monday_days = _days(h)
            monday_rows = [len(row) for row in _rows(h)]
            monday_structure = _structure(h)
        with frozen_today(2026, 11, 1):  # воскресенье, следующее за ним
            await _open(h)
            sunday_days = _days(h)
            sunday_rows = [len(row) for row in _rows(h)]
            sunday_structure = _structure(h)

        self.assertEqual(len(monday_days), 8)
        self.assertEqual(len(sunday_days), 8)
        # Пересечение окон ровно 2 дня (02.11 и 01.11..): окно скользящее.
        self.assertEqual(sunday_days[0] - monday_days[0], timedelta(days=6))

        # Форма экрана не зависит от дня недели.
        self.assertEqual(monday_rows, sunday_rows)
        self.assertEqual(monday_structure[0], sunday_structure[0])
        self.assertEqual(monday_structure[2], sunday_structure[2])

        # Диапазоны: их ровно два, они в одной строке…
        monday_ranges = monday_structure[1]
        sunday_ranges = sunday_structure[1]
        self.assertEqual(len(monday_ranges), 2)
        self.assertEqual(len(sunday_ranges), 2)
        self.assertEqual(monday_structure[0][2], 2, "диапазоны не в одной строке")
        # …а подписи скользят вместе с окном, а не повторяются.
        self.assertNotEqual(
            monday_ranges,
            sunday_ranges,
            "подписи диапазонов не сдвинулись вместе с окном",
        )
        self.assertTrue(all("–" in label for label in monday_ranges + sunday_ranges))

    @scenario_test
    async def test_every_weekday_screen_has_same_structure(self, h):
        """Требование 8: структура экрана не зависит от дня недели."""
        base = date(2026, 10, 26)  # понедельник
        for offset in range(7):
            day = base + timedelta(days=offset)
            with frozen_today(day.year, day.month, day.day):
                await _open(h)
                self.assertEqual(
                    len(_days(h)), 8, f"{day} ({day.strftime('%a')}): не 8 дат"
                )
                self.assertEqual(
                    [len(row) for row in _rows(h)],
                    [4, 4, 2, 1, 2],
                    f"{day}: форма клавиатуры первого экрана",
                )
                self.assertEqual(_days(h)[0], day)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FirstScreenMatrixTests(unittest.TestCase):
    """Требование 2: точный состав клавиатуры первого экрана даты."""

    @scenario_test
    async def test_matrix_on_sunday(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            rows = _rows(h)
            self.assertEqual(len(rows), 5, f"строк клавиатуры: {len(rows)}")
            self.assertEqual([len(row) for row in rows], [4, 4, 2, 1, 2])

            # 2 строки × 4 быстрых даты
            for row in rows[:2]:
                self.assertEqual(
                    [value.split(":", 1)[0] for _label, value in row],
                    ["aw_calday"] * 4,
                )

            # Одна строка из двух диапазонов
            range_row = rows[2]
            self.assertEqual(len(range_row), 2)
            self.assertEqual(
                [label for label, _value in range_row],
                ["09.11–15.11", "16.11–22.11"],
            )
            self.assertTrue(
                all(value.startswith("aw_calrange:") for _label, value in range_row)
            )

            # Одна кнопка выбора месяца
            self.assertEqual(rows[3], [("📅 Выбрать месяц", _first(h, "aw_calmonths"))])

            # Внизу ровно «◀️ Назад» | «⏭ Пропустить» в одной строке
            self.assertEqual(
                [label for label, _value in rows[4]],
                ["◀️ Назад", "⏭ Пропустить"],
            )
            self.assertEqual(
                [value.split(":", 1)[0] for _label, value in rows[4]],
                ["aw_qback", "aw_qskip"],
            )
            self.assertEqual(
                len({value for _label, value in h.screen_buttons()}),
                len(h.screen_buttons()),
                "дубли callback_data на первом экране",
            )

    @scenario_test
    async def test_removed_legacy_entries_are_absent(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            labels = " ".join(_labels(h))
            for removed in (
                "Текущая неделя",
                "Следующая неделя",
                "Другая дата",
                "Календарь: месяцы и диапазоны",
            ):
                self.assertNotIn(removed, labels)
            actions = {value.split(":", 1)[0] for _l, value in h.screen_buttons()}
            for legacy in ("aw_calroot", "aw_calweek", "aw_calother", "aw_calweekdays"):
                self.assertNotIn(legacy, actions)
            self.assertNotIn("aw_cal", actions, "нет голого aw_cal-входа")

    @scenario_test
    async def test_ranges_do_not_overlap_quick_window_or_each_other(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            quick = set(_days(h))
            covered = set(quick)
            seen: list[list[date]] = []
            for index in range(2):
                await h.callback(f"aw_calrange:{prefix}:{index}")
                block = _days(h)
                self.assertEqual(len(block), 7)
                self.assertEqual(len(set(block)), 7)
                self.assertFalse(covered & set(block), f"диапазон {index} пересекается")
                covered |= set(block)
                seen.append(block)
                await h.callback(f"aw_calquick:{prefix}")
            self.assertEqual(seen[1][0] - seen[0][-1], timedelta(days=1))
            self.assertEqual(len(covered), 22)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarEdgeDateTests(unittest.TestCase):
    """Требования 3 и 4: концы месяца/года, високосный февраль, стык месяцев."""

    @scenario_test
    async def test_month_and_year_boundaries(self, h):
        cases = (
            (date(2026, 10, 31), date(2026, 11, 7)),
            (date(2026, 12, 31), date(2027, 1, 7)),
            (date(2028, 2, 28), date(2028, 3, 6)),
            (date(2028, 2, 29), date(2028, 3, 7)),
        )
        for first, last in cases:
            with self.subTest(today=first):
                with frozen_today(first.year, first.month, first.day):
                    await _open(h)
                    days = _days(h)
                    self.assertEqual(len(days), 8)
                    self.assertEqual(days[0], first)
                    self.assertEqual(days[-1], last)
                    self.assertEqual(len(set(days)), 8)
                    self.assertEqual(days, sorted(days))
                    self.assertEqual([len(row) for row in _rows(h)], [4, 4, 2, 1, 2])

    @scenario_test
    async def test_window_crossing_month_start_is_ordered_and_distinct(self, h):
        with frozen_today(2026, 10, 29):
            await _open(h)
            days = _days(h)
            self.assertEqual(
                days, calendar_view.days_between(date(2026, 10, 29), date(2026, 11, 5))
            )
            self.assertEqual(len(set(days)), 8)
            self.assertEqual(days, sorted(days))

    @scenario_test
    async def test_year_crossing_range_label_names_end_year(self, h):
        with frozen_today(2026, 12, 27):
            await _open(h)
            labels = [
                label
                for label, value in h.screen_buttons()
                if value and value.startswith("aw_calrange")
            ]
            self.assertEqual(len(labels), 2)
            self.assertTrue(
                all(label.endswith(".2027") for label in labels),
                f"переход года без года в подписи: {labels}",
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RangeExpansionTests(unittest.TestCase):
    """Требование 5: раскрытие диапазона на том же экране и возврат."""

    @scenario_test
    async def test_expansion_replaces_screen_and_returns_to_quick(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            quick = set(_days(h))
            sends = _screen_sends(h)
            await h.callback(f"aw_calrange:{prefix}:0")
            block = _days(h)
            self.assertEqual(_screen_sends(h), sends, "создано новое сообщение")
            self.assertEqual(len(block), 7)
            self.assertEqual(
                block, calendar_view.days_between(date(2026, 11, 9), date(2026, 11, 15))
            )
            self.assertFalse(quick & set(block))
            self.assertIn("К быстрым датам", " ".join(_labels(h)))
            back = _first(h, "aw_calquick")
            await h.callback(back)
            self.assertEqual(_days(h), sorted(quick))
            self.assertIn("📅 Выбрать месяц", _labels(h))

    @scenario_test
    async def test_day_in_expanded_range_saves_without_confirmation(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calrange:{prefix}:1")
            chosen = _days(h)[4]
            before_text = h.screen_text()
            before_screens = len(h.screen_texts())
            await h.callback(f"aw_calday:{prefix}:{chosen.isoformat()}")
            # Дата сохраняется сразу: никакого шага подтверждения.
            self.assertEqual(
                _saved(h, today=SUNDAY)["day"], chosen.strftime("%d.%m.%Y")
            )
            self.assertNotIn(
                "подтверд", h.screen_text().lower(), "запрошено подтверждение даты"
            )
            # Экран обновился: send или edit — деталь отрисовки, важно, что
            # пользователь увидел новое состояние, а не старую сетку диапазона.
            self.assertTrue(
                len(h.screen_texts()) > before_screens
                or h.screen_text() != before_text,
                "экран не обновился после выбора даты",
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MonthNavigationTests(unittest.TestCase):
    """Требование 6: месяцы, дни месяца, стрелки в пределах горизонта."""

    @scenario_test
    async def test_month_days_and_arrows_stay_within_horizon(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            horizon = calendar_view.horizon_end(SUNDAY)
            self.assertEqual(horizon, date(2027, 11, 1))
            await h.callback(f"aw_calmonths:{prefix}")
            keys = [
                (int(value.rsplit(":", 2)[-2]), int(value.rsplit(":", 1)[-1]))
                for value in _data(h, "aw_calmonth:")
            ]
            self.assertTrue(keys, "нет сетки месяцев")
            self.assertEqual(keys, sorted(keys))
            self.assertLessEqual(len(keys), 12, "кнопок месяцев больше 12")
            horizon_key = (horizon.year, horizon.month)
            # Сетка может кончаться месяцем ДО горизонта: 13-й месяц
            # доступен стрелкой «▶️» из последнего месяца сетки.
            self.assertLessEqual(keys[-1], horizon_key)
            self.assertTrue(all(key <= horizon_key for key in keys))

            # Стрелки доводят до месяца горизонта и не уводят за него.
            for key in keys[1:]:
                await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
                self.assertIn("◀️", _labels(h))
            await h.callback(f"aw_calmonth:{prefix}:{keys[-1][0]}:{keys[-1][1]}")
            clicked = [keys[-1]]
            while "▶️" in _labels(h):
                forward = [
                    value
                    for label, value in h.screen_buttons()
                    if label == "▶️" and value
                ]
                self.assertEqual(len(forward), 1, "стрелка «▶️» без цели")
                await h.callback(forward[0])
                clicked.append(
                    (
                        int(forward[0].rsplit(":", 2)[-2]),
                        int(forward[0].rsplit(":", 1)[-1]),
                    )
                )
            self.assertEqual(clicked[-1], horizon_key, "горизонт недостижим стрелками")
            self.assertEqual(
                clicked, sorted(clicked), "стрелки увели не вперёд по месяцам"
            )
            days = _days(h)
            self.assertTrue(all(day <= horizon for day in days))
            self.assertEqual(days[-1], horizon)

            # Первый месяц сетки: назад стрелки нет.
            await h.callback(f"aw_calmonth:{prefix}:{keys[0][0]}:{keys[0][1]}")
            self.assertNotIn("◀️", _labels(h))
            self.assertIn("▶️", _labels(h))

    @scenario_test
    async def test_month_day_press_saves_immediately(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calmonths:{prefix}")
            keyboard = [
                (int(value.rsplit(":", 2)[-2]), int(value.rsplit(":", 1)[-1]))
                for value in _data(h, "aw_calmonth:")
            ]
            chosen_key = keyboard[3]
            await h.callback(f"aw_calmonth:{prefix}:{chosen_key[0]}:{chosen_key[1]}")
            day = _days(h)[-1]
            self.assertEqual((day.year, day.month), chosen_key)
            await h.callback(f"aw_calday:{prefix}:{day.isoformat()}")
            self.assertEqual(_saved(h, today=SUNDAY)["day"], day.strftime("%d.%m.%Y"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NegativeScenarioTests(unittest.TestCase):
    """Требование 7: негативы не дают внутренних ошибок и не портят данные."""

    @scenario_test
    async def test_legacy_callbacks_redirect_without_error(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=SUNDAY)
            legacy = (
                f"aw_calroot:{prefix}",
                f"aw_calroot:{prefix}:2026-11-05",
                f"aw_calweek:{prefix}:cur",
                f"aw_calweek:{prefix}:next",
                f"aw_calweek:{prefix}:unknown",
                f"aw_calother:{prefix}",
                f"aw_calweekdays:{prefix}:2026:11:0",
                f"aw_calweekdays:{prefix}:2026:11:1",
            )
            for data in legacy:
                answers_before = len(h.answers())
                await h.callback(data)
                self.assertGreater(
                    len(h.answers()), answers_before, f"нет ответа: {data}"
                )
                self.assertNotIn("внутренняя ошибка", "\n".join(_answers(h)).lower())
                self.assertIn("Вопрос 2 из 2", h.screen_text(), data)
                self.assertEqual(len(_days(h)), 8, data)
                self.assertEqual(_saved(h, today=SUNDAY), before, data)

    @scenario_test
    async def test_double_taps_do_not_duplicate_or_resave(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            sends = _screen_sends(h)
            await h.callback(f"aw_calrange:{prefix}:0")
            after_first = _screen_sends(h)
            await h.callback(f"aw_calrange:{prefix}:0")
            self.assertEqual(_screen_sends(h), after_first, "повтор создал сообщение")
            self.assertEqual(len(_days(h)), 7)

            await h.callback(f"aw_calmonths:{prefix}")
            await h.callback(f"aw_calmonths:{prefix}")
            self.assertIn("Выберите месяц", h.screen_text())

            day = date(2026, 12, 3)
            await h.callback(f"aw_calday:{prefix}:{day.isoformat()}")
            saved = _saved(h, today=SUNDAY)
            self.assertEqual(saved["day"], day.strftime("%d.%m.%Y"))
            answers = len(h.answers())
            await h.callback(f"aw_calday:{prefix}:{day.isoformat()}")
            self.assertGreater(len(h.answers()), answers)
            self.assertNotIn("внутренняя ошибка", "\n".join(_answers(h)).lower())
            self.assertEqual(_saved(h, today=SUNDAY), saved)
            self.assertGreaterEqual(_screen_sends(h), sends)

    @scenario_test
    async def test_back_and_skip_from_first_screen(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            await h.callback(_first(h, "aw_qback"))
            self.assertIn("Вопрос 1 из 2", h.screen_text())
            await h.callback(_first(h, "aw_qnext"))
            self.assertIn("Вопрос 2 из 2", h.screen_text())
            self.assertEqual(len(_days(h)), 8)

            await h.callback(_first(h, "aw_qskip"))
            self.assertIn("Фотографии анонса", h.screen_text())
            self.assertIsNone(_saved(h, today=SUNDAY)["day"])

    @scenario_test
    async def test_out_of_horizon_dates_are_rejected(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=SUNDAY)
            horizon = calendar_view.horizon_end(SUNDAY)
            for value in (
                (SUNDAY - timedelta(days=1)).isoformat(),
                (horizon + timedelta(days=1)).isoformat(),
                "2031-06-15",
            ):
                await h.callback(f"aw_calday:{prefix}:{value}")
                self.assertIn("вне допустимого диапазона", _answers(h)[-1])
                self.assertEqual(_saved(h, today=SUNDAY), before)
            # Месяц вне сетки — устаревшая кнопка, значение не пишется.
            await h.callback(f"aw_calmonth:{prefix}:2031:6")
            self.assertTrue(_answers(h)[-1])
            self.assertEqual(_saved(h, today=SUNDAY), before)
            # Последний день горизонта допустим.
            await h.callback(f"aw_calday:{prefix}:{horizon.isoformat()}")
            self.assertEqual(
                _saved(h, today=SUNDAY)["day"], horizon.strftime("%d.%m.%Y")
            )

    @scenario_test
    async def test_foreign_user_cannot_touch_calendar(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=SUNDAY)
            answers = len(h.answers())
            for data in (
                f"aw_calday:{prefix}:2026-11-03",
                f"aw_calmonths:{prefix}",
                f"aw_calrange:{prefix}:0",
                f"aw_calquick:{prefix}",
            ):
                await h.callback(data, user_id=FOREIGN_USER_ID)
            self.assertGreater(len(h.answers()), answers)
            self.assertEqual(_saved(h, today=SUNDAY), before)
            self.assertNotIn("внутренняя ошибка", "\n".join(_answers(h)).lower())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DefectReproductionTests(unittest.TestCase):
    """
    Воспроизведение найденных дефектов (ожидаемое поведение = требование).

    Эти тесты ПАДАЮТ, пока дефекты не исправлены: они фиксируют, что должен
    делать продукт, а не то, что он делает сейчас.
    """

    @scenario_test
    async def test_deffect_month_grid_is_twelve_months(self, h):
        """
        Дефект D1: требование владельца — сетка ровно 12 месяцев.

        Воспроизведение: «сегодня» = 01.11.2026 (любой день 1–8 месяца),
        «📅 Выбрать месяц». Факт: 13 кнопок месяцев (Ноябрь 2026 …
        Ноябрь 2027), первый месяц показывает дни 01.11–30.11, из которых
        22 дня уже были в быстрых датах и диапазонах.
        Ожидание: 12 кнопок месяцев; каждая дата горизонта достижима ровно
        один раз (быстрые даты + диапазоны + месяцы).
        """
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calmonths:{prefix}")
            months = _data(h, "aw_calmonth:")
            self.assertEqual(
                len(months), 12, f"сетка месяцев: {len(months)} вместо 12: {months}"
            )

    @scenario_test
    async def test_deffect_malformed_month_year_has_no_internal_error(self, h):
        """
        Дефект D2: `aw_calmonth` с годом вне диапазона `date` роняет обработчик.

        Воспроизведение: `aw_calmonth:<draft>:<rev>:<brick>:0:5` (год 0).
        Факт: ValueError «year 0 is out of range» в
        `utils/calendar_view.month_bounds` (вызов из
        `announcement_wizard._handle_calendar_callback`, строка с
        `calendar_view.month_days(key, allowed)`); пользователю приходит
        «⚠️ Произошла внутренняя ошибка», callback остаётся без ответа.
        Ожидание: как и для месяца 13 — «экран устарел», ответ на callback,
        значение не меняется, никаких внутренних ошибок.
        """
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=SUNDAY)
            for data in (
                f"aw_calmonth:{prefix}:0:5",
                f"aw_calmonth:{prefix}:-1:5",
                f"aw_calmonth:{prefix}:10000:1",
                f"aw_calmonth:{prefix}:99999:12",
            ):
                answers_before = len(h.answers())
                await h.callback(data)
                self.assertNotIn(
                    "внутренняя ошибка",
                    "\n".join(_answers(h)).lower() + h.screen_text().lower(),
                    f"{data}: пользователь увидел внутреннюю ошибку",
                )
                self.assertGreater(
                    len(h.answers()), answers_before, f"{data}: callback без ответа"
                )
                self.assertIn("Вопрос 2 из 2", h.screen_text(), data)
                self.assertEqual(_saved(h, today=SUNDAY), before, data)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MalformedCalendarKeyTests(unittest.TestCase):
    """D2: недопустимый ключ месяца отвергается до `date(...)`."""

    def test_invalid_month_key_gives_no_days(self):
        for key in (
            (0, 5),
            (-1, 5),
            (10000, 1),
            (99999, 12),
            (2026, 0),
            (2026, 13),
            (2026, -3),
        ):
            with self.subTest(key=key):
                self.assertFalse(calendar_view.is_valid_month_key(key))
                self.assertEqual(
                    calendar_view.month_days(key, (SUNDAY, date(2027, 11, 1))),
                    [],
                    f"{key}: недопустимый месяц дал дни",
                )

    def test_valid_month_key_still_works(self):
        for key in ((1, 1), (2026, 11), (9999, 12)):
            with self.subTest(key=key):
                self.assertTrue(calendar_view.is_valid_month_key(key))

    def test_month_bounds_rejects_invalid_key_with_value_error(self):
        with self.assertRaises(ValueError):
            calendar_view.month_bounds((0, 5))
        with self.assertRaises(ValueError):
            calendar_view.month_bounds((2026, 13))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MalformedCalendarCallbackTests(unittest.TestCase):
    """
    D2 (регрессия): любой подделанный календарный callback безопасен.

    Проверяются числовые части месяца, дата вне `date`-диапазона, индекс
    диапазона и битые идентификаторы: ответ на callback, «экран устарел»,
    без внутренней ошибки и без изменения сохранённого значения.
    """

    @scenario_test
    async def test_malformed_month_and_day_payloads_are_stale(self, h):
        with frozen_today(2026, 11, 1):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=SUNDAY)
            payloads = (
                f"aw_calmonth:{prefix}:0:5",
                f"aw_calmonth:{prefix}:-1:5",
                f"aw_calmonth:{prefix}:10000:1",
                f"aw_calmonth:{prefix}:99999:12",
                f"aw_calmonth:{prefix}:2026:0",
                f"aw_calmonth:{prefix}:2026:13",
                f"aw_calmonth:{prefix}:2026:-3",
                f"aw_calmonth:{prefix}:999999999999999999999:1",
                f"aw_calday:{prefix}:0000-01-01",
                f"aw_calday:{prefix}:10000-01-01",
                f"aw_calday:{prefix}:9999-12-31",
                f"aw_calday:{prefix}:2026-13-01",
                f"aw_calday:{prefix}:не-дата",
                f"aw_calday:{prefix}:",
                f"aw_calrange:{prefix}:99",
                f"aw_calrange:{prefix}:-1",
            )
            for data in payloads:
                answers_before = len(h.answers())
                await h.callback(data)
                self.assertNotIn(
                    "внутренняя ошибка",
                    "\n".join(_answers(h)).lower() + h.screen_text().lower(),
                    f"{data}: пользователь увидел внутреннюю ошибку",
                )
                self.assertGreater(
                    len(h.answers()), answers_before, f"{data}: callback без ответа"
                )
                self.assertIn("Вопрос 2 из 2", h.screen_text(), data)
                self.assertEqual(len(_days(h)), 8, data)
                self.assertEqual(_saved(h, today=SUNDAY), before, data)


if __name__ == "__main__":
    unittest.main()
