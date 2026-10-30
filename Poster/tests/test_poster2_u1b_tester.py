# tests/test_poster2_u1b_tester.py
"""
Poster 2.0 U1b — независимая проверка (poster-tester).

Сценарии написаны заново, без опоры на `tests/test_poster2_u1b_formatting.py`.
Проверяются свойства, которые ломаются ровно при одной из трёх мутаций
реализации:

1. откат точки захвата к `effective_message.text` (плоский текст) — тогда в
   payload нет `html`, а живой блок теряет `<b>`;
2. откат ветки `SafeHtml` в компиляторе — сохранённый HTML экранируется
   второй раз (`&lt;b&gt;`) и пользователь видит теги текстом;
3. возврат включённого превью ссылок — в записях FakeBot нет
   `LinkPreviewOptions(is_disabled=True)`.

Отдельно проверяется defense-in-depth: HTML из БД проходит allow-list при
чтении, и подделанная строка не доходит ни до экрана, ни до отправки.
"""

from __future__ import annotations

import re
import unittest
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import select
from telegram import MessageEntity

try:
    from announcement_store import (
        AnnouncementFieldState,
        build_announcement_fields,
    )
    from brick_store import BrickDefinition
    from brick_types import BrickValueResult, BrickValueStatus
    from delivery import FINAL_LINK_PREVIEW, LIVE_LINK_PREVIEW
    from handlers import announcement_wizard as announcement_wizard_module
    from handlers import wizard_questions as wizard_questions_module
    from models import AnnouncementValue
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_live_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils.tg_html import (
        FormattedText,
        SafeHtml,
        capture_text,
        escape_html,
        sanitize_html,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def ent(type_: str, offset: int, length: int, **kwargs) -> MessageEntity:
    return MessageEntity(type=type_, offset=offset, length=length, **kwargs)


def _live_text(h) -> str:
    for call in reversed(h.calls):
        if call.get("reply_markup") is None and is_live_record(call):
            return call.get("text") or ""
    raise AssertionError("живой блок не отправлен")


def _payload(h, draft_id: int, brick_id: int) -> dict:
    import json

    with h.session_factory() as session:
        row = session.scalar(
            select(AnnouncementValue).where(
                AnnouncementValue.draft_id == draft_id,
                AnnouncementValue.brick_id == brick_id,
            )
        )
    if row is None or row.value_json is None:
        raise AssertionError("значение не сохранено")
    return json.loads(row.value_json)


async def _reopen_first_question(h) -> None:
    """
    Возвращает пользователя к экрану первого вопроса после ответа.

    Последний ответ ведёт на шаг фотографий, поэтому к заполненному
    кирпичику возвращаются через управление полями.
    """
    await h.callback(_callback(h, "aw_qmanage"))
    await h.callback(_callback(h, "aw_qgoto"))


# —— юнит-уровень: сам шов, без приложения и БД ————————————————————————


class SafeHtmlSeamTests(unittest.TestCase):
    """Ветка `SafeHtml` — единственное место, где разметка не экранируется."""

    @staticmethod
    def _definition() -> BrickDefinition:
        return BrickDefinition(
            id=1,
            code="main",
            title="Основное",
            value_type="short_text",
            scope="personal",
            source="personal",
            place_id=None,
            owner_user_id=None,
            rules={},
            status="active",
            revision=1,
            created_by_user_id=None,
            created_at=datetime(2026, 1, 1, 12, 0),
            updated_at=None,
        )

    def _render(self, value: object) -> str:
        from template_compiler import (  # noqa: PLC0415 — локальный импорт теста
            TEMPLATE_SECTION_BODY,
            render_announcement,
        )
        from template_parser import BrickNode, parse_template

        tokens = parse_template("[main]", section=TEMPLATE_SECTION_BODY)
        for node in tokens:
            if isinstance(node, BrickNode):
                node.brick_id = 1
        return render_announcement(
            {TEMPLATE_SECTION_BODY: tokens},
            {1: value},
            {1: self._definition()},
        )

    def test_safe_html_is_not_escaped(self) -> None:
        rendered = self._render(SafeHtml("<b>Жирный</b> &amp; <i>курсив</i>"))
        self.assertEqual(rendered, "<b>Жирный</b> &amp; <i>курсив</i>")

    def test_plain_string_is_escaped(self) -> None:
        rendered = self._render("<b>Жирный</b>")
        self.assertEqual(rendered, "&lt;b&gt;Жирный&lt;/b&gt;")

    def test_sanitize_html_is_idempotent(self) -> None:
        once = sanitize_html('<b>a &amp; b</b><a href="https://e.com/?a=1&b=2">l</a>')
        twice = sanitize_html(once)
        self.assertEqual(str(once), str(twice))
        self.assertIsInstance(once, SafeHtml)

    def test_sanitize_strips_dangerous_markup(self) -> None:
        cleaned = str(
            sanitize_html(
                '<script>alert(1)</script><b onclick="steal()">ok</b>'
                '<a href="javascript:alert(1)">bad</a>'
            )
        )
        self.assertNotIn("<script>", cleaned)
        self.assertNotIn("onclick", cleaned)
        self.assertNotIn("javascript:", cleaned)
        self.assertIn("<b>ok</b>", cleaned)

    def test_sanitize_escapes_loose_specials(self) -> None:
        self.assertEqual(str(sanitize_html("a < b & c > d")), "a &lt; b &amp; c &gt; d")


class CaptureTests(unittest.TestCase):
    """`FormattedText`: plain — источник истины, html — разметка Telegram."""

    def test_plain_and_html_together(self) -> None:
        captured = capture_text(
            "Привет 😀 мир",
            [ent(MessageEntity.BOLD, 0, 6), ent(MessageEntity.ITALIC, 10, 3)],
        )
        self.assertIsInstance(captured, FormattedText)
        self.assertEqual(captured, "Привет 😀 мир")  # обычная строка
        self.assertEqual(captured.html, "<b>Привет</b> 😀 <i>мир</i>")
        self.assertEqual(len(str(captured)), len("Привет 😀 мир"))

    def test_no_entities_is_escaped_plain(self) -> None:
        captured = capture_text("a < b & c", None)
        self.assertEqual(captured, "a < b & c")
        self.assertEqual(captured.html, escape_html("a < b & c"))

    def test_utf16_offsets_do_not_shift_with_emoji(self) -> None:
        text = "😀😀 старт"
        captured = capture_text(text, [ent(MessageEntity.BOLD, 5, 5)])
        self.assertEqual(captured.html, "😀😀 <b>старт</b>")


# —— интеграция: мутация «плоский захват» должна ломать сценарий ——————


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MutationCaptureTests(unittest.TestCase):
    @scenario_test
    async def test_plain_capture_mutation_loses_entities(self, h):
        """
        Если захват вернуть к `effective_message.text`, entities теряются.

        Тест намеренно подменяет точку захвата на время одного ответа и
        показывает разницу: в payload нет `html`, в живом блоке нет `<b>`.
        Этим он фиксирует, что «зелёный» U1b-сценарий держится именно на
        `capture_message_text`.
        """
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        original = announcement_wizard_module.capture_message_text

        def flat_capture(message: object) -> FormattedText:
            # Мутация: плоский текст вместо разметки (прежнее поведение).
            return cast(FormattedText, getattr(message, "text", None) or "")

        announcement_wizard_module.capture_message_text = flat_capture
        try:
            await h.text("Жирный", entities=[ent(MessageEntity.BOLD, 0, 6)])
        finally:
            announcement_wizard_module.capture_message_text = original

        payload = _payload(h, _latest_draft(h).id, bricks["main"])
        self.assertEqual(payload, {"text": "Жирный"})
        self.assertNotIn("html", payload)
        self.assertNotIn("<b>", _live_text(h))

        # Контроль: со штатным захватом то же сообщение даёт разметку.
        await _reopen_first_question(h)
        await h.text("Жирный", entities=[ent(MessageEntity.BOLD, 0, 6)])
        payload = _payload(h, _latest_draft(h).id, bricks["main"])
        self.assertEqual(payload["html"], "<b>Жирный</b>")
        self.assertIn("<b>Жирный</b>", _live_text(h))

    @scenario_test
    async def test_long_text_multiline_formatting_survives_restart(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Многострочный",
            specs=[("main", "Основное", "long_text", {})],
            body="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        text = "Первая строка\nВторая строка"
        await h.text(
            text,
            entities=[
                ent(MessageEntity.ITALIC, 0, 13),
                ent(MessageEntity.CODE, 14, 13),
            ],
        )
        draft = _latest_draft(h)
        await h.restart()
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")

        expected = "<i>Первая строка</i>\n<code>Вторая строка</code>"
        payload = _payload(h, draft.id, bricks["main"])
        self.assertEqual(payload["text"], text)
        self.assertEqual(payload["html"], expected)
        self.assertEqual(_live_text(h), expected)

    @scenario_test
    async def test_screen_shows_raw_html_but_live_block_has_no_preview(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text(
            "https://example.com/a",
            entities=[ent(MessageEntity.URL, 0, 21)],
        )

        # Экран вопроса: allow-list'нутый HTML, без повторного экранирования.
        await _reopen_first_question(h)
        screen = h.screen()
        self.assertIn('<a href="https://example.com/a">', screen["text"])
        self.assertNotIn("&lt;a href", screen["text"])
        self.assertTrue(screen["link_preview_options"].is_disabled)

        # Живой блок и все последующие правки — без превью.
        for call in h.calls:
            if call.get("reply_markup") is not None:
                continue
            if not is_live_record(call):
                continue
            self.assertIs(call["link_preview_options"], LIVE_LINK_PREVIEW)
            self.assertTrue(call["link_preview_options"].is_disabled)

    @scenario_test
    async def test_every_delivery_has_preview_disabled(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Ссылки",
            specs=[("main", "Основное", "short_text", {})],
            body="Сайт: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("https://example.com/x", entities=[ent(MessageEntity.URL, 0, 21)])
        await _no_photos(h)

        posts = [
            call
            for call in h.post_records()
            if call["type"] == "message" and "parse_mode" in call
        ]
        self.assertTrue(posts, "готовый анонс не отправлен")
        for call in posts:
            self.assertIs(call["link_preview_options"], FINAL_LINK_PREVIEW)
            self.assertNotIn("disable_web_page_preview", call)

    @scenario_test
    async def test_corrupted_html_is_sanitized_on_read_and_render(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Исходно")
        draft = _latest_draft(h)

        import json

        with h.session_factory() as session:
            row = session.scalar(
                select(AnnouncementValue).where(
                    AnnouncementValue.draft_id == draft.id,
                    AnnouncementValue.brick_id == bricks["main"],
                )
            )
            assert row is not None
            row.value_json = json.dumps(
                {
                    "text": "Исходно",
                    "html": '<img src=x onerror="alert(1)"><b onmouseover="x">ok</b>',
                },
                ensure_ascii=False,
            )
            session.commit()

        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        field = next(item for item in fields if item.brick_id == bricks["main"])
        assert field.raw_html is not None
        # Неразрешённые теги не остаются разметкой: они выброшены (опасный
        # тег) или экранированы (некорректный вид), а опасные атрибуты
        # разрешённых тегов срезаны.
        self.assertNotIn("<img", field.raw_html)
        self.assertNotIn("<b onmouseover", field.raw_html)
        self.assertNotIn("onmouseover", field.raw_html)
        self.assertIn("<b>ok</b>", field.raw_html)
        tags = re.findall(r"<[^>]*>", field.raw_html)
        self.assertEqual(tags, ["<b>", "</b>"], f"осталась чужая разметка: {tags}")
        self.assertEqual(field.raw_value, "Исходно", "plain остаётся источником истины")

        # Служебный экран тоже не содержит опасной разметки.
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        self.assertNotIn("<img", h.screen()["text"])
        self.assertIn("<b>ok</b>", h.screen()["text"])


LONG_TEXT_LENGTH = 4000


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongValueScreenTests(unittest.TestCase):
    """
    U1b-fix, дефекты A и B: длинное форматированное значение не ломает экран.

    До фикса «Назад» на длинном bold-ответе собирал экран длиннее лимита
    Telegram (4096), реальный API отклонял правку, пользователь видел
    «⚠️ Произошла внутренняя ошибка». Сценарий проходит и для вопроса, и
    для extra-кирпичика — общий путь `stored_value_html`.
    """

    @scenario_test
    async def test_long_bold_answer_keeps_question_screen_alive(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Длинный",
            specs=[
                ("main", "Основное", "long_text", {}),
                ("second", "Второе", "short_text", {}),
            ],
            body="[main] [second]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        text = "ж" * LONG_TEXT_LENGTH
        await h.text(text, entities=[ent(MessageEntity.BOLD, 0, LONG_TEXT_LENGTH)])
        # Продолжение диалога возможно только после сохранения значения.
        payload = _payload(h, _latest_draft(h).id, bricks["main"])
        self.assertEqual(payload["text"], text)
        self.assertEqual(payload["html"], f"<b>{text}</b>")

        # Следующий вопрос и «Назад» на нём: экран первого вопроса с уже
        # сохранённым длинным значением (жалоба приёмки).
        await h.callback(_callback(h, "aw_qback"))
        screen_text = h.screen_text()
        self.assertNotIn("Произошла внутренняя ошибка", "\n".join(h.screen_texts()))
        self.assertIn("Текущее значение:", screen_text)
        self.assertLess(len(screen_text), 4096, "экран ушёл за лимит Telegram")
        # Разметка сохранена, но значение усечено и обрезано «…».
        self.assertIn("<b>", screen_text)
        self.assertNotIn("&lt;b&gt;", screen_text)
        self.assertIn("…", screen_text)
        self.assertEqual(screen_text.count("<b>"), screen_text.count("</b>"))
        value_html = screen_text.split("Текущее значение: ", 1)[1]
        visible = re.sub(r"<[^>]*>", "", value_html)
        self.assertLessEqual(
            len(visible), wizard_questions_module.DISPLAY_TEXT_LIMIT, value_html[-40:]
        )
        self.assertTrue(visible.endswith("…"))

    @staticmethod
    async def _choose_extra_brick(h, brick_id: int) -> None:
        """Выбирает extra-кирпичик, листая страницы доступных определений."""
        suffix = f":{brick_id}"
        for _attempt in range(8):
            found = [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_exbrick") and data.endswith(suffix)
            ]
            if found:
                await h.callback(found[0])
                return
            pages = [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_exbpage")
            ]
            if not pages:
                raise AssertionError(f"нет callback для кирпичика {brick_id}")
            await h.callback(pages[-1])
        raise AssertionError(f"кирпичик {brick_id} не найден на страницах")

    @scenario_test
    async def test_long_bold_value_in_extra_screen_opens(self, h):
        """Дефект B: тот же путь через extra-кирпичик."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Доп. длинный",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("extra", "Дополнительное", "long_text", {}),
            ],
            body="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное")
        await _no_photos(h)

        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exadd"))
        await self._choose_extra_brick(h, bricks["extra"])
        await h.callback(_callback(h, "aw_exsec", ":body"))
        await h.callback(_callback(h, "aw_expos", ":start"))

        text = "ж" * LONG_TEXT_LENGTH
        await h.text(text, entities=[ent(MessageEntity.BOLD, 0, LONG_TEXT_LENGTH)])
        payload = _payload(h, _latest_draft(h).id, bricks["extra"])
        self.assertEqual(payload["text"], text)
        self.assertEqual(payload["html"], f"<b>{text}</b>")

        # Открытие extra-кирпичика снова рисует экран значения.
        await h.callback(_callback(h, "aw_exedit", f":{bricks['extra']}"))
        screen_text = h.screen_text()
        self.assertNotIn("Произошла внутренняя ошибка", "\n".join(h.screen_texts()))
        self.assertIn("Текущее значение:", screen_text)
        self.assertLess(len(screen_text), 4096, "экран ушёл за лимит Telegram")
        self.assertIn("<b>", screen_text)
        self.assertNotIn("&lt;b&gt;", screen_text)
        self.assertIn("…", screen_text)
        self.assertEqual(screen_text.count("<b>"), screen_text.count("</b>"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EmptyStoredHtmlTests(unittest.TestCase):
    """
    U1b-fix, дефект C: пустой `html` в БД не обнуляет анонс.

    Значение с непустым `text` и пустым `html` (`""`/`<b></b>`) раньше давало
    пустой живой блок и готовый пост «.», хотя текст в БД есть.
    """

    @staticmethod
    def _corrupt_html(h, draft_id: int, brick_id: int, html: str) -> None:
        import json

        with h.session_factory() as session:
            row = session.scalar(
                select(AnnouncementValue).where(
                    AnnouncementValue.draft_id == draft_id,
                    AnnouncementValue.brick_id == brick_id,
                )
            )
            assert row is not None
            row.value_json = json.dumps(
                {"text": "Текст есть", "html": html}, ensure_ascii=False
            )
            session.commit()

    @scenario_test
    async def test_empty_html_keeps_live_block_and_publication(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Пустой HTML",
            specs=[("main", "Основное", "short_text", {})],
            body="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Исходно")
        draft = _latest_draft(h)
        self._corrupt_html(h, draft.id, bricks["main"], "<b></b>")

        # Перечитывание черновика: экран вопроса синхронизирует живой блок
        # из БД (значение с пустым `html`).
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        self.assertEqual(_live_text(h), "Текст есть", "анонс обнулился в живом блоке")

        # Готовый пост тоже не пустой.
        await h.callback(_callback(h, "aw_qnext"))
        await h.callback(_callback(h, "aw_preview"))
        await h.callback(_callback(h, "aw_send"))
        posts = [call for call in h.post_records() if call["type"] == "message"]
        self.assertTrue(posts, "готовый анонс не отправлен")
        self.assertEqual(posts[-1].get("text"), "Текст есть")
        from announcement_store import get_announcement_draft

        with h.session_factory() as session:
            stored = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertEqual(stored.publication_text, "Текст есть")

    @scenario_test
    async def test_empty_string_html_keeps_question_screen_value(self, h):
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Исходно")
        draft = _latest_draft(h)
        self._corrupt_html(h, draft.id, bricks["main"], "")

        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        field = next(item for item in fields if item.brick_id == bricks["main"])
        # Пустой HTML не сохраняется как источник разметки.
        self.assertIsNone(field.raw_html)
        self.assertEqual(field.raw_value, "Текст есть")
        self.assertEqual(wizard_questions_module.stored_value_html(field), "Текст есть")


class FieldStateRenderingTests(unittest.TestCase):
    """`stored_value_html`: форматирование только при наличии `raw_html`."""

    def _field(self, **kwargs: Any) -> AnnouncementFieldState:
        params: dict[str, Any] = {
            "brick_id": 1,
            "code": "main",
            "title": "Основное",
            "value_type": "short_text",
            "section": "body",
            "status": "valid",
            "raw_value": "<b>x</b>",
            "rules": {},
        }
        params.update(kwargs)
        return AnnouncementFieldState(**params)

    def test_with_raw_html_keeps_formatting(self) -> None:
        field = self._field(raw_html="<b>x</b>")
        self.assertEqual(wizard_questions_module.stored_value_html(field), "<b>x</b>")

    def test_without_raw_html_escapes(self) -> None:
        field = self._field()
        self.assertEqual(
            wizard_questions_module.stored_value_html(field), "&lt;b&gt;x&lt;/b&gt;"
        )

    def test_number_suffix_is_escaped_and_appended(self) -> None:
        field = self._field(
            value_type="number",
            raw_value=42,
            rules={"suffix": "<руб>"},
            raw_html=None,
        )
        self.assertIn("42", wizard_questions_module.stored_value_html(field))
        self.assertIn("&lt;руб&gt;", wizard_questions_module.stored_value_html(field))


class LongValueTruncationTests(unittest.TestCase):
    """
    U1b-fix, дефект A: длинное форматированное значение усекается до экрана.

    До фикса HTML-ветка возвращала `raw_html` целиком: ответ на 4000
    символов с bold давал экран длиннее 4096, Telegram отклонял отправку, и
    пользователь видел «⚠️ Произошла внутренняя ошибка».
    """

    def _field(self, **kwargs: Any) -> AnnouncementFieldState:
        params: dict[str, Any] = {
            "brick_id": 1,
            "code": "main",
            "title": "Основное",
            "value_type": "long_text",
            "section": "body",
            "status": "valid",
            "raw_value": "",
            "rules": {},
        }
        params.update(kwargs)
        return AnnouncementFieldState(**params)

    def test_long_html_value_is_truncated_like_plain(self) -> None:
        text = "ж" * 4000
        html_field = self._field(raw_value=text, raw_html=f"<b>{text}</b>")
        plain_field = self._field(raw_value=text, raw_html=None)

        html_value = wizard_questions_module.stored_value_html(html_field)
        plain_value = wizard_questions_module.stored_value_html(plain_field)

        visible = wizard_questions_module.visible_html_text(html_value)
        # Тот же видимый лимит, что и в плоской ветке, и та же «…» в конце.
        self.assertEqual(len(visible), len(plain_value))
        self.assertLessEqual(len(visible), wizard_questions_module.DISPLAY_TEXT_LIMIT)
        self.assertTrue(plain_value.endswith("…"))
        self.assertTrue(visible.endswith("…"))
        # Разметка не потеряна и не разрезана.
        self.assertTrue(html_value.startswith("<b>"))
        self.assertTrue(html_value.endswith("</b>"))
        self.assertEqual(html_value.count("<b>"), 1)
        self.assertEqual(html_value.count("</b>"), 1)
        self.assertNotIn("&lt;b&gt;", html_value)

    def test_dense_markup_does_not_exceed_screen_budget(self) -> None:
        """`<b>x</b>` на каждый символ не раздувает экран до 4096."""
        html = "<b>a</b>" * 3000
        truncated = wizard_questions_module.truncate_html(html)
        self.assertLessEqual(len(truncated), 2000)
        self.assertLessEqual(
            len(wizard_questions_module.visible_html_text(truncated)),
            wizard_questions_module.DISPLAY_TEXT_LIMIT,
        )

    def test_short_html_is_returned_untouched(self) -> None:
        field = self._field(raw_value="Жирный", raw_html="<b>Жирный</b>")
        self.assertEqual(
            wizard_questions_module.stored_value_html(field), "<b>Жирный</b>"
        )


class EmptyHtmlRenderingTests(unittest.TestCase):
    """
    U1b-fix, дефект C: HTML без видимого текста не источник значения.

    Значение с непустым `text` и пустым `html` (`""`, `<b></b>`) раньше
    рендерилось пустым: живой блок и готовый пост становились заглушкой `.`
    при непустом значении в БД.
    """

    @staticmethod
    def _definition() -> BrickDefinition:
        return BrickDefinition(
            id=1,
            code="main",
            title="Основное",
            value_type="short_text",
            scope="personal",
            source="personal",
            place_id=None,
            owner_user_id=None,
            rules={},
            status="active",
            revision=1,
            created_by_user_id=None,
            created_at=datetime(2026, 1, 1, 12, 0),
            updated_at=None,
        )

    def _render(self, value: object) -> str:
        from template_compiler import (  # noqa: PLC0415 — локальный импорт теста
            TEMPLATE_SECTION_BODY,
            render_announcement,
        )
        from template_parser import BrickNode, parse_template

        tokens = parse_template("[main]", section=TEMPLATE_SECTION_BODY)
        for node in tokens:
            if isinstance(node, BrickNode):
                node.brick_id = 1
        return render_announcement(
            {TEMPLATE_SECTION_BODY: tokens},
            {1: value},
            {1: self._definition()},
        )

    def test_empty_html_falls_back_to_plain_text(self) -> None:
        from announcement_store import _render_value

        for html in ("", "<b></b>", "<b> </b>", "<tg-spoiler></tg-spoiler>"):
            with self.subTest(html=html):
                rendered = _render_value(
                    self._definition(),
                    BrickValueResult(
                        status=BrickValueStatus.VALID,
                        payload={"text": "Текст анонса", "html": html},
                    ),
                )
                self.assertEqual(rendered, "Текст анонса")
                self.assertNotIsInstance(rendered, SafeHtml)
                self.assertEqual(self._render(rendered), "Текст анонса")

    def test_html_with_visible_text_still_wins(self) -> None:
        from announcement_store import _render_value

        rendered = _render_value(
            self._definition(),
            BrickValueResult(
                status=BrickValueStatus.VALID,
                payload={"text": "Текст", "html": "<b>Текст</b>"},
            ),
        )
        self.assertIsInstance(rendered, SafeHtml)
        self.assertEqual(self._render(rendered), "<b>Текст</b>")


class LanguageClassTests(unittest.TestCase):
    """U1b-fix, дефект D: `pre` с языком `c#`/`f#` сохраняет class."""

    def test_sharp_language_survives_sanitize(self) -> None:
        for language in ("c#", "f#"):
            with self.subTest(language=language):
                source = f'<pre><code class="language-{language}">int x;</code></pre>'
                cleaned = str(sanitize_html(source))
                self.assertIn(f'class="language-{language}"', cleaned)
                self.assertEqual(cleaned, source, "валидный язык не меняется")

    def test_other_languages_still_work(self) -> None:
        for language in ("python", "c++", "objective-c", "1c"):
            with self.subTest(language=language):
                cleaned = str(
                    sanitize_html(
                        f'<pre><code class="language-{language}">x</code></pre>'
                    )
                )
                self.assertIn(f'class="language-{language}"', cleaned)

    def test_arbitrary_class_still_dropped(self) -> None:
        cleaned = str(sanitize_html('<span class="evil">ok</span>'))
        self.assertNotIn("class=", cleaned)
        self.assertEqual(cleaned, "<span>ok</span>")


if __name__ == "__main__":
    unittest.main()
