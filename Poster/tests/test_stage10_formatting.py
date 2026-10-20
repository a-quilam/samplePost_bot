# tests/test_stage10_formatting.py

"""
Интеграционные тесты Stage 10: форматирование Telegram проходит весь путь
ввод -> черновик -> превью -> готовый пост и обратно, не теряясь.

Инвариант: превью и готовый пост собираются одной функцией и совпадают
дословно, включая разбивку длинного текста на части.
"""

import re
import unittest

try:
    from telegram import MessageEntity

    from handlers.post_creation import POST_CREATION, POST_STEPS
    from models import Draft
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        scenario_test,
    )
    from utils.tg_html import utf16_len

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


def visible(html: str) -> str:
    """Видимый текст HTML (теги убраны) — для проверки потерь при сплите."""
    return re.sub(r"<[^>]*>", "", html)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FormattedInputTests(unittest.TestCase):
    """Ввод с MessageEntity доходит до превью и готового поста."""

    async def _to_preview(self, h, index: int, text: str, entities=None) -> None:
        """Доводит диалог до превью: text (+entities) на шаге index, далее пропуски."""
        await h.goto_step(index)
        await h.text(text, entities=entities)
        for _ in range(len(POST_STEPS) - 1 - index):
            await h.text("Пропустить")

    @scenario_test
    async def test_bold_title_reaches_preview_and_final(self, h):
        await self._to_preview(
            h,
            step_index("title"),
            "Важный концерт",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=14)],
        )

        preview_record = h.calls[-1]
        self.assertEqual(preview_record["parse_mode"], "HTML")
        preview = h.posts()[-1]
        self.assertIn("• <b>Заголовок</b>: <b>Важный концерт</b>", preview)
        self.assertNotIn("&lt;b&gt;", preview, "нет двойного экранирования")

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ")
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_nested_entities_and_url_render_html(self, h):
        # Жирное начало, курсив времени и авто-URL (как шлёт Telegram:
        # текст URL-entity — сама ссылка) в одном сообщении
        text = "Начало в 19:00, ссылка: https://t.me/example"
        url = "https://t.me/example"
        await self._to_preview(
            h,
            step_index("text"),
            text,
            entities=[
                MessageEntity(type=MessageEntity.BOLD, offset=0, length=6),
                MessageEntity(type=MessageEntity.ITALIC, offset=9, length=5),
                MessageEntity(
                    type=MessageEntity.URL,
                    offset=text.index(url),
                    length=len(url),
                ),
            ],
        )

        preview = h.posts()[-1]
        self.assertIn(
            "• <b>Текст</b>: <b>Начало</b> в <i>19:00</i>, ссылка: "
            '<a href="https://t.me/example">https://t.me/example</a>',
            preview,
        )

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ")

    @scenario_test
    async def test_emoji_offsets_counted_in_utf16(self, h):
        # Эмодзи до жирного участка: смещение entity считается в UTF-16
        # (эмодзи = 2 код-юнита), иначе тег встанет не туда
        await self._to_preview(
            h,
            step_index("title"),
            "Праздник 🎉 концерт",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=12, length=7)],
        )

        preview = h.posts()[-1]
        self.assertIn("• <b>Заголовок</b>: Праздник 🎉 <b>концерт</b>", preview)

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ")

    @scenario_test
    async def test_styles_and_plain_escape_in_same_post(self, h):
        # Плоский заголовок (< и & в значении) + размеченный текст:
        # своё экранируется ровно один раз, разметка — как есть
        await h.goto_step(step_index("title"))
        await h.text("Скидка <50% & акция")
        for _ in range(step_index("text") - 1):  # шаги 1..4, до «Текст»
            await h.text("Пропустить")
        await h.text(
            "раз два три четыре",
            entities=[
                MessageEntity(type=MessageEntity.UNDERLINE, offset=0, length=3),
                MessageEntity(type=MessageEntity.STRIKETHROUGH, offset=4, length=3),
                MessageEntity(type=MessageEntity.SPOILER, offset=8, length=3),
                MessageEntity(type=MessageEntity.CODE, offset=12, length=6),
            ],
        )
        for _ in range(len(POST_STEPS) - 1 - step_index("text")):
            await h.text("Пропустить")

        preview = h.posts()[-1]
        self.assertIn("• <b>Заголовок</b>: Скидка &lt;50% &amp; акция", preview)
        self.assertIn(
            "• <b>Текст</b>: <u>раз</u> <s>два</s> "
            "<tg-spoiler>три</tg-spoiler> <code>четыре</code>",
            preview,
        )
        self.assertNotIn("&lt;u&gt;", preview, "теги не экранируются")

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ")

    @scenario_test
    async def test_edit_field_accepts_entities(self, h):
        await goto_preview(h, title="Обычный заголовок")

        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text(
            "Новый жирный",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=12)],
        )

        preview = h.posts()[-1]
        self.assertIn("• <b>Заголовок</b>: <b>Новый жирный</b>", preview)
        self.assertNotIn("Обычный заголовок", preview)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DraftRoundtripTests(unittest.TestCase):
    """Сохранение форматирования в черновик и чтение обратно."""

    @scenario_test
    async def test_formatted_draft_roundtrip_preserves_html(self, h):
        await h.goto_step(step_index("title"))
        await h.text(
            "Жирный заголовок",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=16)],
        )
        for _ in range(len(POST_STEPS) - 1):
            await h.text("Пропустить")
        preview_before = h.posts()[-1]
        self.assertIn("<b>Жирный заголовок</b>", preview_before)

        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1)
        self.assertIsNone(h.dialog_state())

        # Открытие: форматирование восстановлено, превью дословно то же
        await h.callback("editdraft_1")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(
            h.user_data().get("formats", {}).get("title"),
            "<b>Жирный заголовок</b>",
        )
        self.assertEqual(
            h.posts()[-1], preview_before, "превью после открытия не изменилось"
        )

        # Повторное сохранение обновляет ту же запись
        await h.callback("save_draft")
        self.assertEqual(len(h.drafts()), 1, "копии не плодятся")
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_legacy_draft_without_formats_opens_plain_and_finishes(self, h):
        # Черновик «до Stage 10»: колонка formats IS NULL, в значении —
        # символы, похожие на HTML
        session = h.session_factory()
        try:
            draft = Draft(user_id=h.user_id, title="Легаси <черновик> & кавычки")
            session.add(draft)
            session.commit()
            draft_id = draft.id
        finally:
            session.close()

        await h.callback(f"editdraft_{draft_id}")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertNotIn("formats", h.user_data(), "легаси: форматирования нет")

        preview = h.posts()[-1]
        self.assertIn(
            "• <b>Заголовок</b>: Легаси &lt;черновик&gt; &amp; кавычки", preview
        )
        self.assertNotIn("&amp;lt;", preview, "нет двойного экранирования")

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongFormattingTests(unittest.TestCase):
    """Длинный размеченный текст: разбивка без потерь и рваных тегов."""

    @scenario_test
    async def test_long_formatted_text_splits_and_equals_final(self, h):
        long_text = "Очень важное событие с разметкой. " * 140  # 4760 символов
        await h.goto_step(step_index("text"))
        await h.text(
            long_text,
            entities=[
                MessageEntity(type=MessageEntity.BOLD, offset=0, length=len(long_text))
            ],
        )
        for _ in range(len(POST_STEPS) - 1 - step_index("text")):
            await h.text("Пропустить")

        preview_chunks = h.posts()
        self.assertGreater(len(preview_chunks), 1, "длинный пост должен разбиться")
        for chunk in preview_chunks:
            self.assertTrue(chunk.strip(), "пустых частей не бывает")
            self.assertLessEqual(
                utf16_len(chunk), 4000, "часть укладывается в лимит Telegram"
            )
            self.assertEqual(
                chunk.count("<b>"),
                chunk.count("</b>"),
                f"разорванный тег в части: ...{chunk[-40:]!r}",
            )
        joined = "".join(visible(c) for c in preview_chunks)
        self.assertEqual(
            joined.count("Очень важное событие с разметкой."),
            140,
            "без потерь и дублей",
        )

        await h.callback("finish")
        final_chunks = h.posts()[len(preview_chunks) :]
        self.assertEqual(
            final_chunks, preview_chunks, "ПРЕВЬЮ == ГОТОВЫЙ ПОСТ (по частям)"
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoFormattingTests(unittest.TestCase):
    """Форматирование в подписях фото и альбомов."""

    @scenario_test
    async def test_formatted_caption_on_single_photo_equals_final(self, h):
        await h.goto_step(step_index("title"))
        await h.text(
            "Концерт сегодня",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=15)],
        )
        for _ in range(len(POST_STEPS) - 2):  # шаги 1..7, остаёмся на «Изображение»
            await h.text("Пропустить")
        await h.photo("fmt-photo-1")

        preview = h.calls[-1]
        self.assertEqual(preview["type"], "photo")
        self.assertEqual(preview["parse_mode"], "HTML")
        self.assertIn("• <b>Заголовок</b>: <b>Концерт сегодня</b>", preview["caption"])

        await h.callback("finish")
        finals = [c for c in h.calls if c["type"] == "photo"]
        self.assertEqual(finals[-1]["caption"], preview["caption"])
        self.assertIsNone(finals[-1].get("reply_markup"))

    @scenario_test
    async def test_album_caption_html_on_first_only_equals_final(self, h):
        await h.goto_step(step_index("text"))
        await h.text(
            "Альбом с разметкой",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=18)],
        )
        for _ in range(step_index("image") - step_index("text") - 1):
            await h.text("Пропустить")  # contact, place_url — остаёмся на «Изображение»
        await h.photo("alb-f-1", media_group_id="mg-fmt")
        await h.photo("alb-f-2", media_group_id="mg-fmt")
        await h.callback("media_done")

        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        first = preview_album["media"][0]
        self.assertEqual(first.parse_mode, "HTML")
        self.assertIn("• <b>Текст</b>: <b>Альбом с разметкой</b>", first.caption)
        self.assertIsNone(
            getattr(preview_album["media"][1], "caption", None),
            "подпись только у первого снимка",
        )

        await h.callback("finish")
        final_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(final_album["media"][0].caption, first.caption)
        self.assertIsNone(getattr(final_album["media"][1], "caption", None))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SaveErrorTests(unittest.TestCase):
    """Ошибка БД при сохранении: диалог и данные не теряются."""

    @scenario_test
    async def test_save_db_error_keeps_dialog_and_data(self, h):
        await goto_preview(h, title="Ценный текст")

        # Реальный сбой: таблица черновиков недоступна
        with h.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE drafts")

        await h.callback("save_draft")

        self.assertIn("Произошла ошибка при сохранении черновика", h.reply_texts()[-1])
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог остаётся на превью")
        self.assertEqual(h.user_data().get("title"), "Ценный текст", "данные целы")
