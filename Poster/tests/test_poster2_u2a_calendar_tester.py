# tests/test_poster2_u2a_calendar_tester.py
"""
Poster 2.2 U2a: независимые тесты компактного календаря (взгляд тестировщика).

Написаны отдельно от тестов разработчика: свои фикстуры, свои проверки,
другие даты. Цель — поймать дефекты, которые «свои» тесты могут не увидеть:

- календарь на вопросе даты и на экране значения дополнительного кирпичика;
- ровно восемь быстрых дат в каждый день недели (в т.ч. воскресенье);
- два диапазона по 7 дней в одной строке, без пересечений и пропусков;
- раскрытие диапазона заменяет экран (без нового сообщения) и возвращает;
- 12-месячная сетка, дни месяца, стрелки внутри горизонта;
- нет кнопок-дублей и устаревших подписей на первом экране;
- устаревшие callback из старых сообщений отвечают без внутренней ошибки;
- выбор дня не требует подтверждения; пропуск работает;
- callback_data укладываются в лимит Telegram.

Запуск из каталога Poster:
    python -m unittest tests.test_poster2_u2a_calendar_tester -v
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
        inline_buttons,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import calendar_view

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _seed(h, *, rules: dict | None = None) -> int:
    """Личный шаблон «[title] — дата...»: заголовок, затем даты."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="tester-u2a")

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
            name="Тест U2a",
            content="\n".join(p for p in ("", "[title] — [day]", "") if p),
        )
        session.commit()
        return template.template_id


async def _open(h, *, rules: dict | None = None) -> None:
    template_id = _seed(h, rules=rules)
    await h.text("/start")
    await h.callback(f"tp_pick:{template_id}")
    await h.text("Тест")


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


def _answer(h) -> str:
    return str(h.answers()[-1].get("text") or "")


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


def _screens(h) -> int:
    return len([call for call in h.screens() if call["type"] == "message"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuestionScreenTests(unittest.TestCase):
    """Первый экран даты и отсутствие кнопок-дублей."""

    @scenario_test
    async def test_question_has_calendar_without_entry_button(self, h):
        with frozen_today(2026, 10, 27):  # вторник, другая дата
            await _open(h)
            text = h.screen_text()
            self.assertIn("Вопрос 2 из 2", text)
            self.assertTrue(_days(h), "на вопросе нет дней календаря")
            labels = _labels(h)
            self.assertNotIn("📅 Календарь", labels)
            self.assertNotIn("📅 Календарь: месяцы и диапазоны", labels)
            self.assertNotIn("Другая дата", labels)

    @scenario_test
    async def test_no_legacy_week_buttons(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            labels = " ".join(_labels(h))
            self.assertNotIn("Текущая неделя", labels)
            self.assertNotIn("Следующая неделя", labels)
            joined = " ".join(str(value) for value in _data(h, "aw_"))
            for legacy in ("aw_calweek", "aw_calother", "aw_calroot", "aw_calweekdays"):
                self.assertNotIn(legacy, joined)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExtraBrickCalendarTests(unittest.TestCase):
    """Календарь даты на экране значения дополнительного кирпичика."""

    @scenario_test
    async def test_extra_date_brick_has_compact_calendar(self, h):
        with frozen_today(2026, 10, 27):
            from tests.test_stage27b_announcement_ui import (
                _choose_personal,
                _no_photos,
                _open_edit,
                _seed_personal_template,
                _seed_place,
                _start_to_template,
            )

            await h.text("/start")
            place_id, _alias = _seed_place(h)
            template_id, _revision, bricks = _seed_personal_template(
                h,
                name="Доп. дата",
                specs=[
                    ("main", "Основное", "short_text", {}),
                    ("extra", "Доп. дата", "date", {}),
                ],
                content="[main]",
                # Poster 2.3.1 (E1): кандидат в доп. поля — кирпичик
                # расширенного текста шаблона.
                extended_content="[main]\n[extra]",
            )
            await _start_to_template(h, place_id)
            await _choose_personal(h, template_id)
            await h.text("Главное")
            await _no_photos(h)

            # U2b: доп. кирпичики доставленного анонса — на экране правки.
            await _open_edit(h)
            await h.callback(_first(h, "aw_extras"))
            await h.callback(_first(h, "aw_exadd"))
            suffix = f":{bricks['extra']}"
            chosen = None
            for _attempt in range(8):
                found = [
                    value
                    for _label, value in h.screen_buttons()
                    if value
                    and value.startswith("aw_exbrick")
                    and value.endswith(suffix)
                ]
                if found:
                    chosen = found[0]
                    break
                pages = [
                    value
                    for _label, value in h.screen_buttons()
                    if value and value.startswith("aw_exbpage")
                ]
                if not pages:
                    break
                await h.callback(pages[-1])
            self.assertIsNotNone(chosen, "кирпичик доп. поля не найден")
            await h.callback(str(chosen))
            # H2: выбора секции/позиции больше нет — кирпичик сразу ведёт на
            # экран значения, у даты это компактный календарь.
            self.assertIn("Выберите дату", h.screen_text())
            self.assertEqual(len(_days(h)), 8)
            self.assertEqual(len(_data(h, "aw_calrange")), 2)
            self.assertTrue(_data(h, "aw_calmonths"))
            self.assertFalse(_data(h, "aw_calroot"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class QuickWindowMatrixTests(unittest.TestCase):
    """Восемь быстрых дат и два семидневных диапазона."""

    def test_every_weekday_gives_eight_quick_dates(self):
        """Проверка на всех семи днях недели: окно не зависит от недели."""
        today0 = date(2026, 10, 26)
        for offset in range(7):
            with self.subTest(offset=offset):
                today = today0 + timedelta(days=offset)
                self.assertEqual(
                    len(calendar_view.quick_days(today)),
                    8,
                    "окно всегда восемь дат",
                )
                self.assertEqual(len(calendar_view.followup_ranges(today)), 2)

    @scenario_test
    async def test_range_row_is_single_row_of_two(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            rows = _rows(h)
            range_rows = [
                row
                for row in rows
                if any("aw_calrange" in (value or "") for _, value in row)
            ]
            self.assertEqual(len(range_rows), 1, "диапазоны должны быть в одной строке")
            self.assertEqual(len(range_rows[0]), 2)
            labels = [label for label, _value in range_rows[0]]
            self.assertEqual(labels, ["04.11–10.11", "11.11–17.11"])

    @scenario_test
    async def test_rows_after_ranges_are_month_and_nav_only(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            rows = _rows(h)
            self.assertEqual(
                rows[-2],
                [("📅 Выбрать месяц", _first(h, "aw_calmonths"))],
            )
            self.assertEqual(
                [label for label, _value in rows[-1]],
                ["◀️ Назад", "⏭ Пропустить"],
            )
            self.assertEqual(len(rows), 5)

    @scenario_test
    async def test_quick_dates_and_ranges_have_no_overlap(self, h):
        with frozen_today(2026, 10, 31):  # конец месяца
            await _open(h)
            prefix = _prefix(h)
            quick = set(_days(h))
            self.assertEqual(len(quick), 8)
            covered = set(quick)
            for index in range(2):
                await h.callback(f"aw_calrange:{prefix}:{index}")
                block = _days(h)
                self.assertEqual(len(block), 7)
                self.assertFalse(covered & set(block), f"диапазон {index} пересекается")
                covered |= set(block)
                await h.callback(f"aw_calquick:{prefix}")
            self.assertEqual(len(covered), 22)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RangeExpansionTests(unittest.TestCase):
    """Раскрытие диапазона на месте и возврат к быстрым датам."""

    @scenario_test
    async def test_range_replaces_screen_and_back_returns(self, h):
        with frozen_today(2026, 12, 20):  # диапазоны уходят в 2027 год
            await _open(h)
            prefix = _prefix(h)
            sends = _screens(h)
            await h.callback(f"aw_calrange:{prefix}:0")
            self.assertEqual(_screens(h), sends, "раскрытие не создаёт новое сообщение")
            self.assertIn("Выберите дату", h.screen_text())
            self.assertEqual(len(_days(h)), 7)
            self.assertIn("◀️ К быстрым датам", _labels(h))
            self.assertEqual(_first(h, "aw_calquick"), f"aw_calquick:{prefix}")
            await h.callback(f"aw_calquick:{prefix}")
            self.assertEqual(len(_days(h)), 8)
            self.assertIn("📅 Выбрать месяц", _labels(h))

    @scenario_test
    async def test_range_crossing_new_year_is_labeled_unambiguously(self, h):
        with frozen_today(2026, 12, 20):
            await _open(h)
            range_labels = [
                label
                for label, value in h.screen_buttons()
                if value and value.startswith("aw_calrange")
            ]
            self.assertEqual(len(range_labels), 2)
            self.assertIn(".2027", range_labels[0])
            self.assertTrue(all(label.count("–") == 1 for label in range_labels))

    @scenario_test
    async def test_day_in_expanded_range_is_saved_immediately(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calrange:{prefix}:1")
            chosen = _days(h)[3]
            await h.callback(f"aw_calday:{prefix}:{chosen.isoformat()}")
            self.assertEqual(
                _saved(h, today=date(2026, 10, 27))["day"],
                chosen.strftime("%d.%m.%Y"),
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MonthScreenTests(unittest.TestCase):
    """Месяцы: сетка, дни, стрелки, горизонт."""

    @scenario_test
    async def test_month_grid_is_twelve_and_covers_horizon(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calmonths:{prefix}")
            keys = [
                (int(value.rsplit(":", 2)[-2]), int(value.rsplit(":", 1)[-1]))
                for value in _data(h, "aw_calmonth:")
            ]
            self.assertEqual(len(keys), 12)
            horizon = calendar_view.horizon_end(date(2026, 10, 27))
            self.assertEqual(keys[-1], (horizon.year, horizon.month))
            self.assertIn("◀️ К быстрым датам", _labels(h))

    @scenario_test
    async def test_month_grid_is_empty_when_horizon_is_too_close(self, h):
        """Короткий горизонт: месяцев впереди нет — кнопки не показываются."""
        with frozen_today(2026, 10, 27):
            limit = (date(2026, 11, 5)).strftime("%d.%m.%Y")
            await _open(h, rules={"max_date": limit})
            self.assertEqual(_data(h, "aw_calmonths"), [])
            # Но быстрые даты и диапазон внутри горизонта остаются.
            self.assertEqual(len(_days(h)), 8)

    @scenario_test
    async def test_month_days_do_not_exceed_bounds(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            horizon = calendar_view.horizon_end(date(2026, 10, 27))
            await h.callback(f"aw_calmonths:{prefix}")
            keys = [
                (int(value.rsplit(":", 2)[-2]), int(value.rsplit(":", 1)[-1]))
                for value in _data(h, "aw_calmonth:")
            ]
            for key in keys:
                await h.callback(f"aw_calmonth:{prefix}:{key[0]}:{key[1]}")
                days = _days(h)
                self.assertTrue(days)
                self.assertEqual({(day.year, day.month) for day in days}, {key})
                self.assertTrue(
                    all(date(2026, 10, 27) <= day <= horizon for day in days)
                )
                await h.callback(f"aw_calmonths:{prefix}")

    @scenario_test
    async def test_month_arrows_open_neighbours_only(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            await h.callback(f"aw_calmonth:{prefix}:2027:1")
            forward = [
                value
                for value in _data(h, "aw_calmonth:")
                if not value.endswith(":2027:1")
            ]
            self.assertTrue(forward)
            await h.callback(forward[0])
            text = h.screen_text()
            self.assertTrue(
                "Февраль 2027" in text or "Декабрь 2026" in text,
                f"стрелка открыла не соседний месяц: {text[:80]!r}",
            )

    @scenario_test
    async def test_month_outside_horizon_is_stale(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=date(2026, 10, 27))
            await h.callback(f"aw_calmonth:{prefix}:2032:5")
            self.assertTrue(_answer(h))
            self.assertEqual(_saved(h, today=date(2026, 10, 27)), before)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SafetyTests(unittest.TestCase):
    """Безопасность callback'ов, пропуск и лимит callback_data."""

    @scenario_test
    async def test_all_callbacks_fit_telegram_limit(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            screens = [h.screen()]
            await h.callback(f"aw_calrange:{prefix}:0")
            screens.append(h.screen())
            await h.callback(f"aw_calquick:{prefix}")
            await h.callback(f"aw_calmonths:{prefix}")
            screens.append(h.screen())
            await h.callback(f"aw_calmonth:{prefix}:2027:6")
            screens.append(h.screen())
            for screen in screens:
                for _label, value in inline_buttons(screen.get("reply_markup")):
                    self.assertIsNotNone(value)
                    self.assertLessEqual(len(str(value).encode("utf-8")), 64)

    @scenario_test
    async def test_stale_and_malformed_calendar_callbacks(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            prefix = _prefix(h)
            before = _saved(h, today=date(2026, 10, 27))
            for data in (
                f"aw_calroot:{prefix}",
                f"aw_calweek:{prefix}:cur",
                f"aw_calother:{prefix}",
                f"aw_calrange:{prefix}:7",
                f"aw_calrange:{prefix}:x",
                f"aw_calmonth:{prefix}:2026:0",
                f"aw_calday:{prefix}:2026-02-31",
                "aw_calmonths:1",
            ):
                await h.callback(data)
                self.assertTrue(_answer(h), f"нет ответа на {data}")
                self.assertIn("Вопрос 2 из 2", h.screen_text())
                self.assertEqual(_saved(h, today=date(2026, 10, 27)), before)

    @scenario_test
    async def test_skip_from_first_screen(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            await h.callback(_first(h, "aw_qskip"))
            self.assertIn("Фотографии анонса", h.screen_text())
            self.assertIsNone(_saved(h, today=date(2026, 10, 27))["day"])

    @scenario_test
    async def test_stale_day_button_after_choice(self, h):
        with frozen_today(2026, 10, 27):
            await _open(h)
            today = date(2026, 10, 27)
            stale = _first(h, "aw_calday")
            fresh_day = today + timedelta(days=1)
            await h.callback(f"aw_calday:{_prefix(h)}:{fresh_day.isoformat()}")
            before = _saved(h, today=today)
            await h.callback(stale)
            self.assertEqual(_saved(h, today=today), before)


if __name__ == "__main__":
    unittest.main()
