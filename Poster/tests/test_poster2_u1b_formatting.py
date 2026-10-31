# tests/test_poster2_u1b_formatting.py
"""
Poster 2.0 U1b: форматирование Telegram сохраняется на всём пути значения.

Сценарии владельца (все через настоящее приложение, FakeBot и временную
SQLite — реальная `post_bot.db`, `.env` и сеть Telegram не используются):

1. после ввода форматированного ответа живой блок и следующий вопрос
   сохраняют разметку;
2. возврат к уже заполненному кирпичику показывает значение с
   форматированием (его видно и можно скопировать);
3. рестарт процесса не теряет разметку;
4. завершение анонса (без фото и с фото) отдаёт её в тексте/подписи;
5. альбом: разметка в подписи последнего сегмента медиагруппы;
6. редактирование значения показывает прежнее форматирование и сохраняет
   новое;
7. спецсимволы `a < b & c` не экранируются дважды;
8. вложенные entity (жирный + ссылка) дают естественную вложенность тегов.

Отдельно фиксируется требование «никаких превью ссылок»: и живой блок, и
готовый анонс уходят с `LinkPreviewOptions(is_disabled=True)`, а не с
включённым предпросмотром.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import date

from sqlalchemy import select
from telegram import MessageEntity

try:
    from announcement_store import build_announcement_fields, get_announcement_draft
    from delivery import FINAL_LINK_PREVIEW, LIVE_LINK_PREVIEW
    from handlers.announcement_wizard import CONVERSATION_NAME
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
        _open_edit,
        _reopen_question,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )
    from utils.tg_html import escape_html

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def ent(type_: str, offset: int, length: int, **kwargs) -> MessageEntity:
    return MessageEntity(type=type_, offset=offset, length=length, **kwargs)


def _live_records(h) -> list[dict]:
    """Сообщения/правки живого блока: HTML-предпросмотр без клавиатуры."""
    return [
        call
        for call in h.calls
        if call.get("reply_markup") is None and is_live_record(call)
    ]


def _live_text(h) -> str:
    records = _live_records(h)
    if not records:
        raise AssertionError("живой блок не отправлен")
    return records[-1].get("text") or ""


def _screen_history(h) -> str:
    """Все тексты экрана: значение могло быть показано на любом из них."""
    return "\n".join(h.screen_texts())


def _stored_payload(h, draft_id: int, brick_id: int) -> dict:
    with h.session_factory() as session:
        row = session.scalar(
            select(AnnouncementValue).where(
                AnnouncementValue.draft_id == draft_id,
                AnnouncementValue.brick_id == brick_id,
            )
        )
    if row is None or row.value_json is None:
        raise AssertionError("значение кирпичика не сохранено")
    import json

    return json.loads(row.value_json)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FormattingCaptureTests(unittest.TestCase):
    """Ввод → хранение → живой блок → следующий вопрос."""

    @scenario_test
    async def test_formatted_answer_survives_into_live_block_and_storage(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Форматирование",
            specs=[
                ("title", "Название", "short_text", {}),
                ("desc", "Описание", "long_text", {}),
            ],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        text = "Привет, мир!"
        await h.text(
            text,
            entities=[
                ent(MessageEntity.BOLD, 0, 6),
                ent(MessageEntity.ITALIC, 8, 3),
            ],
        )

        # Живой блок: разметка сохранена, двойного экранирования нет.
        live = _live_text(h)
        self.assertIn("<b>Привет</b>", live)
        self.assertIn("<i>мир</i>", live)
        self.assertNotIn("&lt;b&gt;", live)

        draft = _latest_draft(h)
        payload = _stored_payload(h, draft.id, bricks["title"])
        self.assertEqual(payload["text"], text)
        self.assertEqual(payload["html"], "<b>Привет</b>, <i>мир</i>!")

        # Следующий вопрос (второй кирпичик) — экран, а не новый пост.
        self.assertIn("Вопрос 2 из 2", h.screen_text())

    @scenario_test
    async def test_plain_text_stores_no_html_key(self, h):
        """Простой текст не плодит `html`: старая схема payload не меняется."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Просто текст")
        draft = _latest_draft(h)
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(payload, {"text": "Просто текст"})

    @scenario_test
    async def test_utf16_offsets_with_emoji_and_cyrillic(self, h):
        """Эмодзи (2 код-юнита UTF-16) не сдвигает границы форматирования."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        text = "🎉 Праздник 🎈 сегодня"
        # "🎉" — 2 единицы, затем " " и "Праздник" (8) → смещение 3..11
        await h.text(text, entities=[ent(MessageEntity.BOLD, 3, 8)])
        self.assertIn("🎉 <b>Праздник</b> 🎈 сегодня", _live_text(h))
        draft = _latest_draft(h)
        self.assertEqual(
            _stored_payload(h, draft.id, bricks["main"])["html"],
            "🎉 <b>Праздник</b> 🎈 сегодня",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FormattingScreenTests(unittest.TestCase):
    """Возврат и редактирование показывают сохранённое форматирование."""

    @scenario_test
    async def test_return_to_filled_brick_shows_stored_formatting(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Возврат",
            specs=[
                ("title", "Название", "short_text", {}),
                ("desc", "Описание", "long_text", {}),
            ],
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text(
            "Театр",
            entities=[ent(MessageEntity.BOLD, 0, 5)],
        )

        # Возврат ко первому вопросу: значение показывается с разметкой.
        await h.callback(_callback(h, "aw_qback"))
        screen = h.screen()["text"]
        self.assertIn("Текущее значение: <b>Театр</b>", screen)
        self.assertNotIn("&lt;b&gt;", screen)

    @scenario_test
    async def test_edit_screen_shows_old_formatting_and_saves_new(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Старое", entities=[ent(MessageEntity.UNDERLINE, 0, 6)])
        await _no_photos(h)
        # U2b: категории правки живут на экране «Изменить».
        await _open_edit(h)

        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        self.assertIn("Текущее значение: <u>Старое</u>", h.screen()["text"])

        await h.text("Новое", entities=[ent(MessageEntity.STRIKETHROUGH, 0, 5)])
        draft = _latest_draft(h)
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(payload["text"], "Новое")
        self.assertEqual(payload["html"], "<s>Новое</s>")
        # Доставленный анонс меняется явной кнопкой (U2b: она на экране
        # «Изменить») — с той же разметкой.
        await _open_edit(h)
        await h.callback(_callback(h, "aw_apply"))
        self.assertEqual(h.posts()[-1], "<s>Новое</s>")

    @scenario_test
    async def test_extra_brick_screen_shows_stored_formatting(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Доп. поле",
            specs=[("main", "Основное", "short_text", {})],
            content="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное")
        await _no_photos(h)
        await _open_edit(h)

        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exadd"))
        await h.callback(_callback(h, "aw_exbrick"))
        # H2: секций и позиций больше нет.
        await h.text("Доп", entities=[ent(MessageEntity.SPOILER, 0, 3)])
        self.assertEqual(
            _stored_payload(h, _latest_draft(h).id, bricks["main"])["text"], "Главное"
        )
        # Открытие заполненного доп. кирпичика снова показывает разметку.
        await h.callback(_callback(h, "aw_exedit"))
        self.assertIn("<tg-spoiler>Доп</tg-spoiler>", h.screen()["text"])
        self.assertNotIn("&lt;tg-spoiler&gt;", h.screen()["text"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FormattingPersistenceTests(unittest.TestCase):
    """Рестарт, финал без фото, финал с фото и альбом."""

    @scenario_test
    async def test_restart_restores_formatting_in_live_block_and_screen(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("До рестарта", entities=[ent(MessageEntity.BOLD, 0, 2)])
        draft = _latest_draft(h)

        await h.restart()
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")

        self.assertIn("<b>До</b> рестарта", _live_text(h))
        # U2b: после рестарта анонс доставлен, категории правки — на экране
        # «Изменить».
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        self.assertIn("Текущее значение: <b>До</b> рестарта", h.screen()["text"])

    @scenario_test
    async def test_final_text_without_photos_keeps_formatting(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Финал",
            specs=[("main", "Основное", "short_text", {})],
            content="Анонс: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Сегодня", entities=[ent(MessageEntity.BOLD, 0, 7)])
        await _no_photos(h)

        delivered = h.posts()[-1] or ""
        self.assertEqual(delivered, "Анонс: <b>Сегодня</b>")
        # U5: пост без фото — это живой блок, доведённый правкой до финального
        # HTML, поэтому у доставленного сообщения несколько вызовов. Превью
        # ссылок выключено в каждом из них (а не только в `send_message`).
        previews = h.delivered_link_previews()
        self.assertTrue(previews, "доставленный анонс не найден")
        for options in previews:
            self.assertIsNotNone(options)
            self.assertTrue(options.is_disabled)

    @scenario_test
    async def test_single_photo_caption_keeps_formatting(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Фото",
            specs=[("main", "Основное", "short_text", {})],
            content="Анонс: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Вечер", entities=[ent(MessageEntity.ITALIC, 0, 5)])
        await h.photo("u1b-photo")

        photo_record = [r for r in h.post_records() if r["type"] == "photo"][-1]
        self.assertEqual(photo_record["caption"], "Анонс: <i>Вечер</i>")
        self.assertEqual(photo_record["parse_mode"], "HTML")

    @scenario_test
    async def test_album_caption_keeps_formatting(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Альбом",
            specs=[("main", "Основное", "short_text", {})],
            content="Анонс: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Тур", entities=[ent(MessageEntity.UNDERLINE, 0, 3)])
        await h.photo("u1b-0", media_group_id="u1b-album")
        await h.photo("u1b-1", media_group_id="u1b-album")
        await asyncio.sleep(0.5)

        groups = [r for r in h.post_records() if r["type"] == "media_group"]
        self.assertEqual(len(groups), 1)
        captions = [getattr(item, "caption", None) for item in groups[0]["media"]]
        self.assertEqual(captions, [None, "Анонс: <u>Тур</u>"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FormattingSafetyTests(unittest.TestCase):
    """Спецсимволы, вложенность и запрет превью ссылок."""

    @scenario_test
    async def test_special_characters_are_not_escaped_twice(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Символы",
            specs=[("main", "Основное", "short_text", {})],
            content="a < b & c: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        value = "x < y & z"
        await h.text(value, entities=[ent(MessageEntity.BOLD, 0, len(value))])
        await _no_photos(h)

        delivered = h.posts()[-1] or ""
        self.assertEqual(
            delivered,
            "a &lt; b &amp; c: <b>x &lt; y &amp; z</b>",
            "статический текст и значение экранируются ровно один раз",
        )
        self.assertNotIn("&amp;lt;", delivered)
        self.assertNotIn("&amp;amp;", delivered)

    @scenario_test
    async def test_nested_bold_and_link_is_natural(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        text = "Билеты на сайте"
        await h.text(
            text,
            entities=[
                ent(MessageEntity.BOLD, 0, len(text)),
                ent(
                    MessageEntity.TEXT_LINK,
                    10,
                    4,
                    url="https://example.com/tickets",
                ),
            ],
        )
        self.assertEqual(
            _live_text(h),
            '<b>Билеты на <a href="https://example.com/tickets">сайт</a>е</b>',
        )

    @scenario_test
    async def test_no_link_preview_anywhere(self, h):
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Ссылки",
            specs=[("main", "Основное", "short_text", {})],
            content="Сайт: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text(
            "https://example.com/page",
            entities=[ent(MessageEntity.URL, 0, 24)],
        )
        await _no_photos(h)

        for record in _live_records(h):
            self.assertIs(
                record.get("link_preview_options"),
                LIVE_LINK_PREVIEW,
                "живой блок обязан отключать превью ссылок",
            )
        # U5: готовый пост без фото — доведённый живой блок, поэтому проверяется
        # всё содержимое доставленного сообщения, а не одна запись отправки.
        delivered_previews = h.delivered_link_previews()
        self.assertTrue(delivered_previews, "готовый анонс не отправлен")
        for options in delivered_previews:
            self.assertIsNotNone(options)
            self.assertTrue(options.is_disabled)
        for record in h.post_records():
            if record["type"] != "message" or is_live_record(record):
                continue
            self.assertIs(
                record.get("link_preview_options"),
                FINAL_LINK_PREVIEW,
                "готовый анонс обязан отключать превью ссылок",
            )
        for record in h.screens():
            self.assertIsNotNone(
                record.get("link_preview_options"),
                "экран не должен включать предпросмотр",
            )
            self.assertTrue(record["link_preview_options"].is_disabled)

    @scenario_test
    async def test_foreign_html_in_database_is_sanitized_on_read(self, h):
        """Подделанный HTML (ручная правка БД) не доходит до отправки."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Нормально")
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
                    "text": "Нормально",
                    "html": '<script>alert(1)</script><b onclick="x">Норм</b>',
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
        self.assertNotIn("<script>", field.raw_html)
        self.assertNotIn("onclick", field.raw_html)
        self.assertIn("<b>Норм</b>", field.raw_html)

        await _reopen_question(h)
        screen = h.screen()["text"]
        self.assertNotIn("<script>", screen)
        self.assertNotIn("onclick", screen)

        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        self.assertIsNotNone(details.template_snapshot)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME) is not None, True)

    @scenario_test
    async def test_plain_value_stays_escaped_on_screen(self, h):
        """Значение без entities не получает `html`: экран экранирует как раньше."""
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("<b>ответ</b>")
        # Значение без entities экранируется ровно один раз.
        self.assertEqual(_live_text(h), "&lt;b&gt;ответ&lt;/b&gt;")
        await _reopen_question(h)
        screen = h.screen()["text"]
        self.assertIn("Текущее значение: &lt;b&gt;ответ&lt;/b&gt;", screen)
        self.assertEqual(escape_html("<b>ответ</b>"), "&lt;b&gt;ответ&lt;/b&gt;")


if __name__ == "__main__":
    unittest.main()
