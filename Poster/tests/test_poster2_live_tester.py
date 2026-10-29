# tests/test_poster2_live_tester.py
"""
Poster 2.0, S3 — независимая проверка (poster-tester).

Сценарии написаны заново, независимо от tests/test_poster2_live.py:
сквозные пути через настоящее приложение, FakeBot и временную SQLite.
Реальная `post_bot.db`, `.env` и сеть не используются.

Покрытие:

- живой блок правится, а не пересылается; снимок шаблона не пересоздаётся;
- рестарт посреди вопросов: черновик, значения и адрес живого блока
  восстанавливаются без дубля верхнего блока;
- пропуск кирпичика обновляет верх и не теряет уже введённое;
- кнопка времени 18:30 и ручной ввод «20:15» сохраняют значение;
- «Без фотографий» завершает анонс: `publication_text` = фактически
  отправленному (`.` для пустого), шутки в нём нет;
- техническая `.` не попадает в `announcement_values` и исчезает при правке;
- второй анонс не меняет и не удаляет живой блок первого.

Известный дефект (S3, кейс 1) — живой блок отправляется НОВЫМ сообщением
после того, как служебный экран уже создан, поэтому в чате он оказывается
НИЖЕ экрана. Здесь он не закреплён красным тестом: воспроизведение и
ожидание описаны в отчёте poster-tester (`live_message_ids[0]` > id экрана
на самом первом входе `pl_pick`/`tp_pick`).
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

try:
    from telegram.error import BadRequest

    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from models import AnnouncementValue, PlaceAdmin, TemplateSnapshot, utcnow
    from place_store import create_place
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")
    return found[0]


def _live_records(h) -> list[dict]:
    """Живой верхний блок: HTML-предпросмотр без клавиатуры и без превью-флага True."""
    return [
        call
        for call in h.calls
        if call.get("reply_markup") is None
        and call.get("disable_web_page_preview") is False
        and call["type"] in {"message", "edit_message"}
    ]


def _live_text(h) -> str:
    records = _live_records(h)
    if not records:
        raise AssertionError("живой блок не отправлен")
    return records[-1].get("text") or ""


def _drafts(h):
    with h.session_factory() as session:
        return list_announcement_drafts(session, actor_telegram_user_id=DEFAULT_USER_ID)


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
        )


def _values(h, draft_id: int) -> dict[int, str | None]:
    with h.session_factory() as session:
        rows = list(
            session.scalars(
                select(AnnouncementValue).where(AnnouncementValue.draft_id == draft_id)
            )
        )
    return {row.brick_id: row.value_json for row in rows}


def _snapshot_count(h) -> int:
    with h.session_factory() as session:
        return len(list(session.scalars(select(TemplateSnapshot.id))))


def _seed_bricks(h, specs):
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="tester-s3")
        for code, title, kind, rules in specs:
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=title,
                value_type=kind,
                rules=rules or {},
            )
        session.commit()


def _seed_template(h, *, body: str, name: str = "Личный", specs=None):
    _seed_bricks(h, specs or [("title", "Название", "short_text", {})])
    with h.session_factory() as session:
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            header="",
            body=body,
            footer="",
        )
        session.commit()
        return template.template_id


def _seed_place(h, name: str = "Дом культуры") -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="tester-s3")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        return place.id


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RestartRecoveryTests(unittest.TestCase):
    """Рестарт посреди вопросов: черновик и живой блок восстанавливаются."""

    @scenario_test
    async def test_restart_midway_restores_live_block_without_snapshot(self, h):
        template_id = _seed_template(
            h,
            body="Анонс: [title]",
            specs=[("title", "Название", "short_text", {})],
        )
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("До рестарта")
        # Падаем на шаге фото (после последнего кирпичика) — состояние
        # черновика и живой блок уже сохранены.
        draft = _drafts(h)[0]
        snapshot_before = _snapshot_count(h)
        live_before = _details(h, draft.draft_id).live_message_ids
        self.assertEqual(len(live_before), 1)
        sends_before = len([r for r in _live_records(h) if r["type"] == "message"])

        await h.restart()
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")

        restored = _details(h, draft.draft_id)
        self.assertEqual(
            _snapshot_count(h), snapshot_before, "после рестарта создан новый снимок"
        )
        self.assertEqual(restored.live_message_ids, live_before)
        self.assertEqual(
            _values(h, draft.draft_id)[restored.template_snapshot.brick_ids[0]] or "",
            '{"text": "До рестарта"}',
        )
        # Верхний блок отредактирован, а не отправлен заново.
        sends_after = len([r for r in _live_records(h) if r["type"] == "message"])
        self.assertEqual(
            sends_after, sends_before, "после рестарта отправлен дубль живого блока"
        )
        self.assertIn("До рестарта", _live_text(h))

    @scenario_test
    async def test_restart_keeps_answers_to_every_brick(self, h):
        template_id = _seed_template(
            h,
            body="[title] / [where]",
            specs=[
                ("title", "Название", "short_text", {}),
                ("where", "Место", "short_text", {}),
            ],
        )
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Вечеринка")
        draft = _drafts(h)[0]
        live_before = _details(h, draft.draft_id).live_message_ids

        await h.restart()
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")

        restored = _details(h, draft.draft_id)
        self.assertEqual(restored.live_message_ids, live_before)
        self.assertIn("Вечеринка", _live_text(h))
        # Черновик остался на вопросах: второй кирпичик всё ещё спрашивается.
        self.assertIn("Вопрос 2 из 2", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SkipAndLiveUpdateTests(unittest.TestCase):
    """Пропуск кирпичика обновляет верх и не теряет уже введённое."""

    @staticmethod
    def _seed(h):
        return _seed_template(
            h,
            body="[title] — [where]",
            specs=[
                ("title", "Название", "short_text", {}),
                ("where", "Место", "short_text", {}),
            ],
        )

    @scenario_test
    async def test_skip_of_filled_brick_updates_live_block(self, h):
        """Пропуск заполненного кирпичика виден сверху сразу."""
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Вечеринка")
        self.assertIn("Вечеринка", _live_text(h))

        # Возвращаемся к первому кирпичику и очищаем его пропуском.
        await h.callback(_callback(h, "aw_qback"))
        await h.callback(_callback(h, "aw_qskip"))

        after_skip = _live_text(h)
        self.assertNotIn("Вечеринка", after_skip, "пропуск не убрал значение сверху")
        self.assertEqual(after_skip.strip(), "—")
        draft = _drafts(h)[0]
        stored = " ".join(v or "" for v in _values(h, draft.draft_id).values())
        self.assertNotIn("Вечеринка", stored)

    @scenario_test
    async def test_skip_of_untouched_brick_keeps_previous_value(self, h):
        """Пропуск второго кирпичика не теряет значение первого."""
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Вечеринка")
        before_skip = _live_text(h)

        await h.callback(_callback(h, "aw_qskip"))

        after_skip = _live_text(h)
        self.assertIn("Вечеринка", after_skip, "пропуск потерял введённое значение")
        # Видимый текст не изменился: значение второго поля и так пусто.
        self.assertEqual(after_skip, before_skip)
        self.assertIn("Фотографии анонса", h.screen_text())
        draft = _drafts(h)[0]
        values = _values(h, draft.draft_id)
        self.assertIn('"Вечеринка"', " ".join(v or "" for v in values.values()))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TimeEntryTests(unittest.TestCase):
    """Кнопки времени и ручной ввод сохраняют одно и то же значение."""

    @staticmethod
    def _seed(h):
        return _seed_template(
            h,
            body="[day] / [clock]",
            specs=[
                ("day", "Дата", "date", {}),
                ("clock", "Время", "time", {}),
            ],
        )

    @scenario_test
    async def test_time_preset_1830_is_saved_and_shown(self, h):
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))  # дату пропускаем

        presets = _callbacks(h, "aw_timeset")
        self.assertTrue(any(data.endswith(":18:30") for data in presets), presets)
        await h.callback([d for d in presets if d.endswith(":18:30")][0])

        draft = _drafts(h)[0]
        stored = " ".join(v or "" for v in _values(h, draft.draft_id).values())
        self.assertIn("18:30", stored, f"значение не сохранено: {stored}")
        self.assertIn("18:30", _live_text(h))

    @scenario_test
    async def test_manual_time_2015_is_saved_and_shown(self, h):
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))
        await h.text("20:15")

        draft = _drafts(h)[0]
        stored = " ".join(v or "" for v in _values(h, draft.draft_id).values())
        self.assertIn("20:15", stored, f"ручной ввод не сохранён: {stored}")
        self.assertIn("20:15", _live_text(h))

    @scenario_test
    async def test_stale_time_preset_is_rejected(self, h):
        """Устаревшая кнопка времени (чужой revision) не меняет значение."""
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))
        stale = _callback(h, "aw_timeset")
        # Значение даты меняет revision: кнопка времени устаревает.
        await h.callback(_callback(h, "aw_qback"))
        await h.callback([c for c in _callbacks(h, "aw_calday")][0])
        draft = _drafts(h)[0]
        before = _values(h, draft.draft_id)
        await h.callback(stale)
        self.assertEqual(_values(h, draft.draft_id), before)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EmptyAnnouncementDeliveryTests(unittest.TestCase):
    """Пустой анонс: `.` в отправке и в `publication_text`, шутка — только снизу."""

    @scenario_test
    async def test_photo_none_delivers_dot_and_records_it(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        self.assertEqual(_live_text(h).strip(), ".")
        await h.callback(_callback(h, "aw_qskip"))
        # Шаг фото: ни «Готово», ни доставки до явного выбора.
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertEqual(h.posts(), [])
        await h.callback(_callback(h, "aw_photo_none"))

        draft = _drafts(h)[0]
        stored = _details(h, draft.draft_id)
        self.assertEqual(stored.publication_text, ".")
        self.assertEqual(h.posts(), ["."])
        joke = ui_humor.empty_announcement_joke()
        self.assertNotIn(joke, stored.publication_text or "")
        self.assertNotIn(joke, h.posts()[0] or "")

    @scenario_test
    async def test_placeholder_absent_from_values_and_gone_after_edit(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        draft = _drafts(h)[0]
        await h.callback(_callback(h, "aw_qskip"))

        values = _values(h, draft.draft_id)
        self.assertTrue(values, "значение кирпичика не создано вовсе")
        for value in values.values():
            self.assertNotIn(".", (value or "").strip())
        self.assertEqual(_live_text(h).strip(), ".")

        # Правка кирпичика убирает точку и не оставляет её в значениях.
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("Настоящий анонс")
        self.assertIn("Настоящий анонс", _live_text(h))
        self.assertNotEqual(_live_text(h).strip(), ".")
        for value in _values(h, draft.draft_id).values():
            self.assertNotIn(".", (value or "").strip())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SecondAnnouncementTests(unittest.TestCase):
    """Новый анонс не трогает живой блок предыдущего."""

    @scenario_test
    async def test_second_announcement_does_not_touch_first_live_block(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Первый")
        await h.callback(_callback(h, "aw_photo_none"))
        first = _drafts(h)[0]
        first_ids = tuple(_details(h, first.draft_id).live_message_ids)
        first_values = _values(h, first.draft_id)
        self.assertTrue(first_ids)
        boundary = len(h.calls)

        await h.callback("main_menu")
        await h.callback("pick_template")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Второй")

        drafts = _drafts(h)
        self.assertEqual(len(drafts), 2, [d.draft_id for d in drafts])
        # Живой блок первого анонса не переписан и не удалён.
        self.assertEqual(tuple(_details(h, first.draft_id).live_message_ids), first_ids)
        self.assertEqual(_values(h, first.draft_id), first_values)
        touched = [
            call
            for call in h.calls[boundary:]
            if call["type"] == "edit_message"
            and int(call.get("message_id") or 0) in first_ids
        ]
        self.assertEqual(touched, [], "второй анонс переписал блок первого")
        self.assertNotIn(first_ids[0], h.deleted_ids())

        other = [d for d in drafts if d.draft_id != first.draft_id][0]
        self.assertNotEqual(
            tuple(_details(h, other.draft_id).live_message_ids), first_ids
        )

    @scenario_test
    async def test_place_entry_starts_live_block_immediately(self, h):
        place_id = _seed_place(h)
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        live = _live_records(h)
        self.assertTrue(live, "живой блок не появился на входе по площадке")
        self.assertIn("Вопрос 1 из", h.screen_text())
        self.assertIsNone(live[0].get("reply_markup"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiveAboveScreenOrderTests(unittest.TestCase):
    """
    Порядок «анонс сверху, служебный экран снизу» (S3 §4, регрессия дефекта).

    Telegram не переставляет сообщения, поэтому порядок проверяется по
    фактическим `message_id`: живой блок должен иметь id МЕНЬШЕ служебного
    экрана и оба должны реально присутствовать в чате.
    """

    @staticmethod
    def _alive(h) -> set[int]:
        """message_id сообщений бота, оставшихся в чате (с учётом удалений)."""
        alive: set[int] = set()
        index = 0
        for call in h.calls:
            if call["type"] in {"message", "photo", "media_group"}:
                index += 1
                alive.add(100 + index)
        for message_id in h.deleted_ids():
            alive.discard(int(message_id))
        return alive

    def _assert_live_above_screen(self, h) -> tuple[tuple[int, ...], int]:
        draft = _drafts(h)[0]
        live_ids = tuple(_details(h, draft.draft_id).live_message_ids)
        screen_id = h.screen_message_id()
        self.assertTrue(live_ids, "живой блок не сохранён в черновике")
        self.assertIsNotNone(screen_id, "служебный экран не отрисован")
        alive = self._alive(h)
        self.assertIn(live_ids[0], alive, "живой блок отсутствует в чате")
        self.assertIn(screen_id, alive, "служебный экран отсутствует в чате")
        self.assertLess(
            live_ids[0],
            screen_id,
            f"анонс ниже служебного экрана: live={live_ids} screen={screen_id}",
        )
        return live_ids, screen_id

    @scenario_test
    async def test_personal_entry_puts_live_above_screen(self, h):
        template_id = _seed_template(h, body="Анонс: [title]")
        await h.text("/start")
        entry_screen = h.screen_message_id()
        await h.callback(f"tp_pick:{template_id}")

        live_ids, screen_id = self._assert_live_above_screen(h)
        self.assertNotEqual(screen_id, entry_screen, "экран не пересоздан ниже анонса")
        self.assertIn(entry_screen, h.deleted_ids(), "старый экран остался в чате")

        # Ответ по-прежнему правит живой блок, порядок сохраняется.
        await h.text("Вечеринка")
        after_live, after_screen = self._assert_live_above_screen(h)
        self.assertEqual(after_live, live_ids, "ответ пересоздал живой блок")
        self.assertEqual(after_screen, screen_id)

    @scenario_test
    async def test_place_entry_puts_live_above_screen(self, h):
        place_id = _seed_place(h)
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        self._assert_live_above_screen(h)

    @scenario_test
    async def test_screen_recreated_from_menu_stays_below_live(self, h):
        template_id = _seed_template(h, body="Анонс: [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("До меню")
        live_before, _screen = self._assert_live_above_screen(h)

        # Пользовательская команда ниже экрана пересоздаёт служебный экран.
        await h.text_from_below("/new_wizard")
        live_after = tuple(_details(h, _drafts(h)[0].draft_id).live_message_ids)
        self.assertEqual(live_after, live_before, "меню пересоздало живой блок")
        self.assertGreater(
            h.screen_message_id(),
            live_after[0],
            "после пересоздания экран оказался выше анонса",
        )

    @scenario_test
    async def test_restart_keeps_live_id_and_screen_below(self, h):
        template_id = _seed_template(h, body="Анонс: [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("До рестарта")
        draft = _drafts(h)[0]
        live_before, _screen = self._assert_live_above_screen(h)
        sends_before = len([r for r in _live_records(h) if r["type"] == "message"])

        await h.restart()
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")

        live_after, _screen_after = self._assert_live_above_screen(h)
        self.assertEqual(live_after, live_before, "рестарт создал новый живой блок")
        self.assertEqual(
            len([r for r in _live_records(h) if r["type"] == "message"]),
            sends_before,
            "после рестарта отправлен дубль живого блока",
        )

    @scenario_test
    async def test_lost_live_message_is_recreated_above_screen(self, h):
        """Сообщение анонса удалено извне: создаётся новое, снова выше экрана."""
        template_id = _seed_template(h, body="Анонс: [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        old_live, _screen = self._assert_live_above_screen(h)

        # Telegram отвечает «Message to edit not found» на правку удалённого.
        h.bot.errors_by_type["edit_message"] = BadRequest("Message to edit not found")
        await h.text("После потери")
        h.bot.errors_by_type.pop("edit_message", None)

        new_live, screen_id = self._assert_live_above_screen(h)
        self.assertNotEqual(new_live, old_live, "живой блок не пересоздан")
        self.assertIn("После потери", _live_text(h))


if __name__ == "__main__":
    unittest.main()
