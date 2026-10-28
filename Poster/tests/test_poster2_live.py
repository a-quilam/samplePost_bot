# tests/test_poster2_live.py
"""
Poster 2.0 S3 — живой анонс, календарь и время.

Проверяется фактическое поведение через настоящее приложение, FakeBot и
временную SQLite (не реальная `post_bot.db`, не сеть Telegram):

- верхний блок — настоящий анонс, отправленный ДО нижнего вопроса;
- после каждого ответа верх обновляется редактированием, а не новым постом;
- пустой анонс получает техническую `.`, литеральный `//` — нет;
- `.` не попадает в значения кирпичиков;
- рестарт восстанавливает блок без нового снимка и без дублей;
- на экране нет кнопки «Готово»;
- шутка живёт только в служебном экране.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select
from telegram.error import BadRequest

try:
    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from models import AnnouncementValue, TemplateSnapshot
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


def _seed_template(h, *, body: str, header: str = "", footer: str = ""):
    """Личный шаблон + один кирпичик; анонс стартует с входа S2."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="live")
        create_personal_brick(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            code="title",
            title="Название",
            value_type="short_text",
            rules={},
        )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="Живой",
            header=header,
            body=body,
            footer=footer,
        )
        session.commit()
        return template.template_id


def _latest_draft(h):
    with h.session_factory() as session:
        drafts = list_announcement_drafts(
            session, actor_telegram_user_id=DEFAULT_USER_ID
        )
    if not drafts:
        raise AssertionError("черновик не создан")
    return drafts[0]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
        )


def _live_records(h) -> list[dict]:
    """Сообщения/правки живого блока: HTML-предпросмотр без клавиатуры."""
    records = []
    for call in h.calls:
        if call.get("reply_markup") is not None:
            continue
        if call.get("disable_web_page_preview") is not False:
            continue
        if call["type"] not in {"message", "edit_message"}:
            continue
        records.append(call)
    return records


def _live_ids(h) -> tuple[int, ...]:
    """Сохранённые message_id живого блока черновика (порядок сверху вниз)."""
    return tuple(_details(h, _latest_draft(h).draft_id).live_message_ids)


def _live_text(h) -> str:
    records = _live_records(h)
    if not records:
        raise AssertionError("живой блок не отправлен")
    return records[-1].get("text") or ""


def _values(h, draft_id: int) -> dict[int, str | None]:
    """Сырые значения кирпичиков: `value_json`, а не готовый текст."""
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


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiveBlockOrderTests(unittest.TestCase):
    """Верхний блок — настоящий анонс; он всегда выше служебного экрана."""

    @scenario_test
    async def test_live_block_is_above_the_question_screen(self, h):
        template_id = _seed_template(h, body="Постоянный текст [title]")
        await h.text("/start")
        entry_screen = h.screen_message_id()
        await h.callback(f"tp_pick:{template_id}")

        self.assertIn("Вопрос 1 из 1", h.screen_text())
        live_ids = _live_ids(h)
        self.assertEqual(len(live_ids), 1, "верхний блок не отправлен")
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)
        # Фактический порядок в чате: сообщение анонса выше служебного экрана.
        self.assertLess(live_ids[0], screen_id, "анонс оказался ниже экрана")
        # Экран входа, созданный ДО анонса, пересоздан ниже (не остался сверху).
        self.assertIn(entry_screen, h.deleted_ids())
        self.assertNotEqual(screen_id, entry_screen)

    @scenario_test
    async def test_new_wizard_recreated_screen_stays_below_live_block(self, h):
        template_id = _seed_template(h, body="Текст [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Значение")
        live_ids = _live_ids(h)
        sends_before = len([r for r in _live_records(h) if r["type"] == "message"])

        # Команда приходит НОВЫМ сообщением ниже экрана — экран пересоздаётся.
        await h.text("/new_wizard")
        screen_id = h.screen_message_id()
        self.assertLess(live_ids[0], screen_id, "пересозданный экран выше анонса")
        # Живой блок не пересоздавался: новых сообщений анонса нет.
        sends_after = len([r for r in _live_records(h) if r["type"] == "message"])
        self.assertEqual(sends_after, sends_before, "создан дубль живого блока")
        self.assertEqual(_live_ids(h), live_ids)

    @scenario_test
    async def test_recreated_live_block_stays_above_the_screen(self, h):
        """Живой блок пришлось отправить заново — экран уходит под него."""
        template_id = _seed_template(h, body="Текст [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        old_live = _live_ids(h)[0]

        # Telegram «потерял» сообщение анонса: правка невозможна.
        h.bot.errors_by_type["edit_message"] = BadRequest("message to edit not found")
        await h.text("Новое значение")
        h.bot.errors_by_type.pop("edit_message", None)

        live_ids = _live_ids(h)
        self.assertNotEqual(live_ids[0], old_live, "адрес анонса не обновился")
        self.assertLess(
            live_ids[0], h.screen_message_id(), "новый анонс оказался ниже экрана"
        )
        self.assertIn("Новое значение", _live_text(h))

    @scenario_test
    async def test_live_block_has_no_keyboard(self, h):
        template_id = _seed_template(h, body="Текст [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        for record in _live_records(h):
            self.assertIsNone(record.get("reply_markup"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiveBlockUpdateTests(unittest.TestCase):
    """После каждого ответа верхний блок меняется тем же сообщением."""

    @scenario_test
    async def test_answer_edits_live_block_and_keeps_one_message(self, h):
        template_id = _seed_template(h, body="Анонс: [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        before = _live_text(h)
        self.assertIn("Анонс", before)

        await h.text("Вечеринка")
        after = _live_text(h)
        self.assertIn("Вечеринка", after)

        edits = [r for r in _live_records(h) if r["type"] == "edit_message"]
        self.assertTrue(edits, "верхний блок не обновлён редактированием")
        # Ни одного нового сообщения живого блока после первого: дублей нет.
        sends = [r for r in _live_records(h) if r["type"] == "message"]
        self.assertEqual(len(sends), 1, "создан лишний живой блок")

    @scenario_test
    async def test_live_ids_are_stored_in_draft(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        draft = _latest_draft(h)
        details = _details(h, draft.draft_id)
        self.assertEqual(len(details.live_message_ids), 1)
        self.assertEqual(details.revision, draft.revision)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceholderTests(unittest.TestCase):
    """Техническая `.` и буквальная статика шаблона."""

    @scenario_test
    async def test_empty_announcement_gets_placeholder_dot(self, h):
        # Шаблон состоит только из кирпичика, значение не задано: видимого
        # текста нет вовсе — live-блок обязан нести техническую `.`.
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        self.assertEqual(_live_text(h).strip(), ".")

    @scenario_test
    async def test_literal_slashes_are_not_replaced_by_placeholder(self, h):
        # Литеральный `//` — непустой видимый текст: заглушки быть не должно.
        template_id = _seed_template(h, body="//")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        self.assertEqual(_live_text(h).strip(), "//")

    @scenario_test
    async def test_static_slashes_survive_empty_date_and_time(self, h):
        """Литерал `//` остаётся, даже когда `[дата]`/`[время]` пусты."""
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="literal")
            for code, title, kind in (
                ("day", "Дата", "date"),
                ("clock", "Время", "time"),
            ):
                create_personal_brick(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    code=code,
                    title=title,
                    value_type=kind,
                    rules={},
                )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Слэши",
                header="",
                body="[day] // [clock]",
                footer="",
            )
            session.commit()
            template_id = template.template_id

        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        self.assertIn("//", _live_text(h))
        await h.callback(_callback(h, "aw_qskip"))
        await h.callback(_callback(h, "aw_qskip"))
        # Литералы шаблона не удаляются: заглушки `.` нет.
        self.assertIn("//", _live_text(h))
        self.assertNotEqual(_live_text(h).strip(), ".")

    @scenario_test
    async def test_skipped_bricks_keep_static_text_without_placeholder(self, h):
        template_id = _seed_template(h, body="Событие | [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))
        self.assertEqual(_live_text(h).strip(), "Событие |")

    @scenario_test
    async def test_placeholder_never_lands_in_announcement_values(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        draft = _latest_draft(h)
        await h.callback(_callback(h, "aw_qskip"))

        values = _values(h, draft.draft_id)
        for value in values.values():
            self.assertNotEqual((value or "").strip(), ".")
        self.assertIn(".", _live_text(h))

    @scenario_test
    async def test_placeholder_disappears_after_value(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        self.assertEqual(_live_text(h).strip(), ".")
        await h.text("Настоящий заголовок")
        self.assertIn("Настоящий заголовок", _live_text(h))
        self.assertNotEqual(_live_text(h).strip(), ".")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RestartTests(unittest.TestCase):
    """Рестарт восстанавливает живой блок без нового снимка и дублей."""

    @scenario_test
    async def test_restart_restores_live_block_without_new_snapshot(self, h):
        template_id = _seed_template(h, body="Анонс [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("До рестарта")
        draft = _latest_draft(h)
        snapshots_before = _snapshot_count(h)
        live_before = _details(h, draft.draft_id).live_message_ids

        await h.restart()

        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")

        self.assertEqual(_snapshot_count(h), snapshots_before, "создан новый снимок")
        restored = _details(h, draft.draft_id)
        self.assertEqual(restored.live_message_ids, live_before)
        self.assertIn("До рестарта", _live_text(h))
        sends = [r for r in _live_records(h) if r["type"] == "message"]
        self.assertEqual(len(sends), 1, "после рестарта отправлен дубль анонса")
        # Порядок сохраняется и после рестарта: анонс выше служебного экрана.
        self.assertLess(live_before[0], h.screen_message_id())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CalendarAndTimeShortcutTests(unittest.TestCase):
    """Календарь сразу на вопросе даты; кнопки времени сохраняют значение."""

    @staticmethod
    def _seed(h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="s3")
            for code, title, kind in (
                ("day", "Дата", "date"),
                ("clock", "Время", "time"),
                ("title", "Название", "short_text"),
            ):
                create_personal_brick(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    code=code,
                    title=title,
                    value_type=kind,
                    rules={},
                )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Дата и время",
                header="",
                body="[title] [day] [clock]",
                footer="",
            )
            session.commit()
            return template.template_id

    @scenario_test
    async def test_date_question_shows_week_days_immediately(self, h):
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Заголовок")

        # Календарь уже на вопросе: дни текущей недели без отдельного входа.
        self.assertIn("Вопрос 2 из 3", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_calday"))

    @scenario_test
    async def test_time_preset_button_saves_value_and_edits_live_block(self, h):
        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        answer = await h.text("Заголовок")
        await h.callback(_callback(h, "aw_qskip"))  # дата не задана

        self.assertIn("Вопрос 3 из 3", h.screen_text())
        presets = [data for data in _callbacks(h, "aw_timeset")]
        self.assertTrue(any(data.endswith(":18:00") for data in presets))

        live_before = _live_text(h)
        await h.callback([d for d in presets if d.endswith(":18:00")][0])

        # Значение сохранено, верхний блок обновлён, ввод удалён.
        draft = _latest_draft(h)
        saved = _values(h, draft.draft_id)
        self.assertTrue(any("18:00" in (value or "") for value in saved.values()))
        self.assertIn("18:00", _live_text(h))
        self.assertNotEqual(_live_text(h), live_before)
        self.assertIn(answer.effective_message.message_id, h.deleted_ids())
        self.assertIn("Фотографии анонса", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoStepTests(unittest.TestCase):
    """Последний кирпичик ведёт на шаг фото; «Без фотографий» — финал."""

    @scenario_test
    async def test_last_brick_opens_photo_step_without_finish_button(self, h):
        template_id = _seed_template(h, body="Анонс [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Значение")

        self.assertIn("Фотографии анонса", h.screen_text())
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("📎 Добавить фото", labels)
        self.assertIn("🚫 Без фотографий", labels)
        self.assertFalse(_callbacks(h, "aw_send"))
        # До выбора пользователя анонс ещё не отправлен.
        self.assertEqual(h.posts(), [])

    @scenario_test
    async def test_no_photos_finishes_the_announcement(self, h):
        template_id = _seed_template(h, body="Анонс [title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Значение")
        await h.callback(_callback(h, "aw_photo_none"))

        self.assertIn("Готовый анонс отправлен", h.screen_text())
        posts = h.posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("Значение", posts[0] or "")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NoFinalizeButtonTests(unittest.TestCase):
    """Кнопки «Готово»/«Сформировать» нет ни на одном экране конструктора."""

    @scenario_test
    async def test_no_finish_button_on_question_screens(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        forbidden = ("Готово, получить пост", "Сформировать", "Опубликовать")
        for label, _data in h.screen_buttons():
            for word in forbidden:
                self.assertNotIn(word, label)
        # `✅ Готово` для множественного выбора — не завершение анонса:
        # на экране единственного короткого поля её вообще нет.
        self.assertFalse(_callbacks(h, "aw_finish"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorTests(unittest.TestCase):
    """Шутка живёт только в служебном экране и никогда — в анонсе."""

    @scenario_test
    async def test_joke_is_not_in_the_live_announcement(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        joke = ui_humor.empty_announcement_joke()
        self.assertTrue(joke)
        self.assertNotIn(joke, _live_text(h))
        self.assertEqual(_live_text(h).strip(), ".")

    @scenario_test
    async def test_joke_appears_in_the_service_screen_of_empty_announce(self, h):
        # Шаблон из одного кирпичика: после пропуска видимого текста нет.
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))

        joke = ui_humor.get_humor_line(announcement_is_empty=True)
        self.assertIsNotNone(joke)
        # Нижний экран шага фото — единственное место шутки.
        self.assertIn(joke, h.screen_text())
        # В верхнем блоке остаётся техническая `.`, шутки там нет.
        self.assertNotIn(joke, _live_text(h))
        self.assertEqual(_live_text(h).strip(), ".")

    @scenario_test
    async def test_joke_absent_when_announcement_has_text(self, h):
        template_id = _seed_template(h, body="[title]")
        joke = ui_humor.empty_announcement_joke()
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Настоящий анонс")
        self.assertNotIn(joke, h.screen_text())
        self.assertIn("Настоящий анонс", h.screen_text() + _live_text(h))

    @scenario_test
    async def test_joke_never_reaches_delivered_post(self, h):
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))
        await h.callback(_callback(h, "aw_photo_none"))

        joke = ui_humor.empty_announcement_joke()
        posts = h.posts()
        self.assertTrue(posts)
        for text in posts:
            self.assertNotIn(joke, text or "")
        self.assertNotIn(joke, _live_text(h))

    @scenario_test
    async def test_empty_announcement_publishes_placeholder_not_joke(self, h):
        """§5.6: `publication_text` — реально отправленное, т.е. `.`, без шутки."""
        template_id = _seed_template(h, body="[title]")
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_callback(h, "aw_qskip"))
        await h.callback(_callback(h, "aw_photo_none"))

        draft = _details(h, _latest_draft(h).draft_id)
        self.assertEqual(draft.publication_text, ".")
        joke = ui_humor.empty_announcement_joke()
        self.assertNotIn(joke, draft.publication_text or "")
        self.assertNotIn(joke, h.posts()[0] or "")
        # Буквальный `//` заглушку не получает.
        self.assertNotEqual(draft.publication_text.strip(), "//")

    def test_hook_returns_stable_short_phrase(self) -> None:
        self.assertTrue(ui_humor.humor_enabled())
        first = ui_humor.empty_announcement_joke()
        self.assertIsNotNone(first)
        assert first is not None  # для mypy: дальше нужна строка
        self.assertEqual(first, ui_humor.empty_announcement_joke())
        self.assertEqual(ui_humor.get_humor_line(announcement_is_empty=True), first)
        self.assertIsNone(ui_humor.get_humor_line(announcement_is_empty=False))
        self.assertLessEqual(len(first), 200)


if __name__ == "__main__":
    unittest.main()
