# tests/test_stage10_html.py
"""
Тесты Stage 10, часть 1: безопасный HTML (utils/tg_html).

- entities_to_html: каждый тип форматирования Telegram, вложенные и
  комбинированные entities, смещения UTF-16 (эмодзи), экранирование
  текста и значений атрибутов, битые смещения, сверка с text_html
  python-telegram-bot на общих типах и документированные отличия;
- utf16_len: единицы смещений Telegram, а не символы Python;
- split_html: теги и ссылки-сущности не рвутся, открытые теги
  закрываются/переоткрываются, видимый текст не теряется и не дублируется,
  предпочтение границы после '\\n', лимит считается в UTF-16.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import html
import re
import unittest
from datetime import datetime

from telegram import Chat, Message, MessageEntity, User

from utils.tg_html import entities_to_html, escape_html, split_html, utf16_len


def make_message(text: str, entities: list[MessageEntity]) -> Message:
    """Сообщение-фикстура для сверки с Message.text_html (без бота)."""
    return Message(
        message_id=1,
        date=datetime(2026, 1, 1, 12, 0),
        chat=Chat(id=1, type="private"),
        from_user=User(id=1, first_name="Тест", is_bot=False),
        text=text,
        entities=entities,
    )


def visible(html_text: str) -> str:
    """Видимый текст HTML (теги сняты, сущности развёрнуты)."""
    return html.unescape(re.sub(r"<[^>]*>", "", html_text))


def ent(type_: str, offset: int, length: int, **kwargs) -> MessageEntity:
    return MessageEntity(type=type_, offset=offset, length=length, **kwargs)


def assert_well_formed(case: unittest.TestCase, chunk: str) -> None:
    """Часть — валидный HTML: пары тегов сходятся, обрывков нет."""
    stack: list[str] = []
    for match in re.finditer(r"<[^>]*>", chunk):
        tag = match.group(0)
        if tag.startswith("</"):
            name = tag[2:-1].split()[0]
            if not stack:
                case.fail(f"закрывающий тег без открывающего: ...{chunk[-60:]!r}")
            case.assertEqual(
                stack.pop(), name, f"несогласованный тег: ...{chunk[-60:]!r}"
            )
        elif not tag.endswith("/>"):
            stack.append(tag[1:-1].split()[0])
    case.assertEqual(stack, [], f"незакрытые теги: ...{chunk[-60:]!r}")

    # После удаления целых тегов не должно оставаться обрывков '<', '>' и '&'
    residue = re.sub(r"<[^>]*>", "", chunk)
    for ref in ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;"):
        residue = residue.replace(ref, "")
    case.assertNotIn("<", residue, f"обрывок тега: {chunk[-60:]!r}")
    case.assertNotIn(">", residue, f"обрывок тега: {chunk[-60:]!r}")
    case.assertNotIn("&", residue, f"обрывок сущности: {chunk[-60:]!r}")
    case.assertNotIn(";", residue, f"обрывок сущности: {chunk[-60:]!r}")


class EscapeHtmlTests(unittest.TestCase):
    """escape_html: один экранировщик для текста и атрибутов."""

    def test_escapes_specials(self):
        self.assertEqual(
            escape_html("a < b & \"c\" 'd'"),
            "a &lt; b &amp; &quot;c&quot; &#x27;d&#x27;",
        )

    def test_plain_text_unchanged(self):
        self.assertEqual(escape_html("Обычный текст 123"), "Обычный текст 123")


class Utf16LenTests(unittest.TestCase):
    """utf16_len: длина в единицах смещений Telegram (UTF-16)."""

    def test_ascii_and_cyrillic_one_unit(self):
        self.assertEqual(utf16_len("abc"), 3)
        self.assertEqual(utf16_len("привет"), 6)

    def test_emoji_is_two_units(self):
        self.assertEqual(utf16_len("😀"), 2)
        self.assertEqual(utf16_len("a😀b"), 4)

    def test_empty(self):
        self.assertEqual(utf16_len(""), 0)


class EntitiesToHtmlTests(unittest.TestCase):
    """Конвертер entities → безопасный HTML."""

    def convert(self, text: str, *entities: MessageEntity) -> str:
        return entities_to_html(text, list(entities))

    def test_bold(self):
        self.assertEqual(
            self.convert("жирный", ent(MessageEntity.BOLD, 0, 6)), "<b>жирный</b>"
        )

    def test_italic(self):
        self.assertEqual(
            self.convert("курсив", ent(MessageEntity.ITALIC, 0, 6)), "<i>курсив</i>"
        )

    def test_underline(self):
        self.assertEqual(
            self.convert("подчёркнутый", ent(MessageEntity.UNDERLINE, 0, 12)),
            "<u>подчёркнутый</u>",
        )

    def test_strikethrough(self):
        self.assertEqual(
            self.convert("зачёркнутый", ent(MessageEntity.STRIKETHROUGH, 0, 11)),
            "<s>зачёркнутый</s>",
        )

    def test_code(self):
        self.assertEqual(
            self.convert("print()", ent(MessageEntity.CODE, 0, 7)),
            "<code>print()</code>",
        )

    def test_pre_without_language(self):
        self.assertEqual(
            self.convert("блок кода", ent(MessageEntity.PRE, 0, 9)),
            "<pre>блок кода</pre>",
        )

    def test_pre_with_documented_language_class(self):
        # Вид по документации Telegram; PTB пишет class="без language-"
        self.assertEqual(
            self.convert("print()", ent(MessageEntity.PRE, 0, 7, language="python")),
            '<pre><code class="language-python">print()</code></pre>',
        )

    def test_spoiler(self):
        self.assertEqual(
            self.convert("спойлер", ent(MessageEntity.SPOILER, 0, 7)),
            "<tg-spoiler>спойлер</tg-spoiler>",
        )

    def test_text_link(self):
        self.assertEqual(
            self.convert(
                "ссылка",
                ent(MessageEntity.TEXT_LINK, 0, 6, url="https://example.com"),
            ),
            '<a href="https://example.com">ссылка</a>',
        )

    def test_text_link_without_url_falls_back_to_text(self):
        # url обязателен для ссылки; без неё — просто текст, не битый HTML
        self.assertEqual(
            self.convert("ссылка", ent(MessageEntity.TEXT_LINK, 0, 6)), "ссылка"
        )

    def test_url_entity_becomes_explicit_link(self):
        text = "https://t.me/x"
        self.assertEqual(
            self.convert(text, ent(MessageEntity.URL, 0, 14)),
            '<a href="https://t.me/x">https://t.me/x</a>',
        )

    def test_text_mention(self):
        user = User(id=42, first_name="Иван", is_bot=False)
        self.assertEqual(
            self.convert("Иван", ent(MessageEntity.TEXT_MENTION, 0, 4, user=user)),
            '<a href="tg://user?id=42">Иван</a>',
        )

    def test_blockquote(self):
        self.assertEqual(
            self.convert("цитата", ent(MessageEntity.BLOCKQUOTE, 0, 6)),
            "<blockquote>цитата</blockquote>",
        )

    def test_expandable_blockquote(self):
        self.assertEqual(
            self.convert("цитата", ent(MessageEntity.EXPANDABLE_BLOCKQUOTE, 0, 6)),
            "<blockquote expandable>цитата</blockquote>",
        )

    def test_custom_emoji(self):
        emoji_id = "5368324170671202286"
        self.assertEqual(
            self.convert(
                "😀",
                ent(MessageEntity.CUSTOM_EMOJI, 0, 2, custom_emoji_id=emoji_id),
            ),
            f'<tg-emoji emoji-id="{emoji_id}">😀</tg-emoji>',
        )

    def test_unknown_type_keeps_text_without_tags(self):
        # Вспомогательные типы разметки Telegram не имеют — текст сохраняем
        self.assertEqual(
            self.convert("@ivan", ent(MessageEntity.MENTION, 0, 5)), "@ivan"
        )
        self.assertEqual(self.convert("#тег", ent(MessageEntity.HASHTAG, 0, 4)), "#тег")

    def test_no_entities_escapes_plain_text(self):
        self.assertEqual(entities_to_html("a < b & c", None), "a &lt; b &amp; c")
        self.assertEqual(entities_to_html("a < b", []), "a &lt; b")

    def test_empty_text(self):
        self.assertEqual(entities_to_html("", None), "")

    def test_nested_entities(self):
        # Жирный целиком, курсив внутри него
        text = "жирный курсив"
        result = self.convert(
            text,
            ent(MessageEntity.BOLD, 0, 13),
            ent(MessageEntity.ITALIC, 7, 6),
        )
        self.assertEqual(result, "<b>жирный <i>курсив</i></b>")

    def test_combined_entities_with_gap(self):
        # Два отдельных фрагмента: жирный и курсив через пробел
        text = "жирный и курсив"
        result = self.convert(
            text,
            ent(MessageEntity.BOLD, 0, 6),
            ent(MessageEntity.ITALIC, 9, 6),
        )
        self.assertEqual(result, "<b>жирный</b> и <i>курсив</i>")

    def test_equal_range_entities_nest(self):
        text = "старт"
        result = self.convert(
            text,
            ent(MessageEntity.ITALIC, 0, 5),
            ent(MessageEntity.BOLD, 0, 5),
        )
        self.assertEqual(result, "<i><b>старт</b></i>")

    def test_emoji_offsets_counted_in_utf16(self):
        # Эмодзи занимает 2 код-юнита: смещение жирного — 2, а не 1
        text = "😀жирный"
        result = self.convert(text, ent(MessageEntity.BOLD, 2, 6))
        self.assertEqual(result, "😀<b>жирный</b>")

    def test_emoji_after_entity(self):
        text = "привет😀"
        result = self.convert(text, ent(MessageEntity.BOLD, 0, 6))
        self.assertEqual(result, "<b>привет</b>😀")

    def test_text_is_escaped_everywhere(self):
        text = "<b>&</b>"
        result = self.convert(text, ent(MessageEntity.BOLD, 0, 8))
        self.assertEqual(result, "<b>&lt;b&gt;&amp;&lt;/b&gt;</b>")

    def test_code_content_is_escaped(self):
        result = self.convert("a<b", ent(MessageEntity.CODE, 0, 3))
        self.assertEqual(result, "<code>a&lt;b</code>")

    def test_href_attribute_is_escaped(self):
        # Кавычка и амперсанд в ссылке не ломают атрибут: у PTB href уходит
        # как есть — это одно из сознательных отличий нашего конвертера
        url = 'https://x.com/a?b=1&c="2"'
        result = self.convert("текст", ent(MessageEntity.TEXT_LINK, 0, 5, url=url))
        self.assertEqual(
            result, '<a href="https://x.com/a?b=1&amp;c=&quot;2&quot;">текст</a>'
        )

    def test_broken_offsets_ignored_text_preserved(self):
        # Длина выходит за конец текста — entity не применяется, текст цел
        result = self.convert("привет", ent(MessageEntity.BOLD, 4, 100))
        self.assertEqual(result, "привет")

    def test_negative_offset_ignored(self):
        result = self.convert("привет", ent(MessageEntity.BOLD, -1, 3))
        self.assertEqual(result, "привет")

    def test_matches_ptb_text_html_bold_and_escaping(self):
        # Сверка с эталоном python-telegram-bot на общих типах
        text = "до <b> & жирный конец"
        message = make_message(text, [ent(MessageEntity.BOLD, 9, 6)])
        self.assertEqual(entities_to_html(text, message.entities), message.text_html)

    def test_matches_ptb_text_html_link_code_blockquote(self):
        text = "см. ссылка и код конец"
        entities = [
            ent(MessageEntity.TEXT_LINK, 4, 6, url="https://example.com"),
            ent(MessageEntity.CODE, 13, 3),
        ]
        message = make_message(text, entities)
        self.assertEqual(entities_to_html(text, message.entities), message.text_html)

        text = "цитата здесь"
        message = make_message(text, [ent(MessageEntity.BLOCKQUOTE, 0, 12)])
        self.assertEqual(entities_to_html(text, message.entities), message.text_html)

    def test_matches_ptb_text_html_with_emoji_offsets(self):
        text = "😀жирный конец"
        message = make_message(text, [ent(MessageEntity.BOLD, 2, 6)])
        self.assertEqual(entities_to_html(text, message.entities), message.text_html)


class SplitHtmlTests(unittest.TestCase):
    """split_html: длинный HTML частями без потерь и разрывов тегов."""

    def check_common(self, html_text: str, chunks: list[str], limit: int) -> None:
        """Общие инварианты: лимит, валидность частей, полнота видимого текста."""
        self.assertGreater(len(chunks), 1, "длинный текст должен разойтись")
        for chunk in chunks:
            self.assertLessEqual(
                utf16_len(chunk), limit, f"часть длиннее лимита: ...{chunk[-40:]!r}"
            )
            assert_well_formed(self, chunk)
        self.assertEqual(visible("".join(chunks)), visible(html_text))

    def test_short_html_unchanged_exactly(self):
        text = "<b>короткий</b> и <i>тоже</i>"
        self.assertEqual(split_html(text, 100), [text])

    def test_short_text_unchanged(self):
        self.assertEqual(split_html("короткий текст", 100), ["короткий текст"])

    def test_invalid_limit_raises(self):
        with self.assertRaises(ValueError):
            split_html("x", 0)

    def test_plain_long_text_joins_exactly(self):
        # Без тегов части — точные подстроки: склейка даёт исходный текст
        text = "Важное событие начнётся в 14:00 (вход свободный)! " * 300
        chunks = split_html(text, 4000)
        self.check_common(text, chunks, 4000)
        self.assertEqual("".join(chunks), text)

    def test_bold_run_tags_reopened_and_balanced(self):
        html_text = "".join(
            f"<b>текст {index:04d} довольно длинный. </b>" for index in range(200)
        )
        chunks = split_html(html_text, 100)
        self.check_common(html_text, chunks, 100)

    def test_cut_inside_bold_reopens_tag(self):
        html_text = "<b>" + "х" * 200 + "</b>"
        chunks = split_html(html_text, 50)
        self.check_common(html_text, chunks, 50)
        for chunk in chunks[1:]:
            self.assertTrue(chunk.startswith("<b>"), chunk[:40])

    def test_nested_tags_balanced_on_split(self):
        html_text = "<b>жирный <i>и курсив</i> текст</b> " * 60
        chunks = split_html(html_text, 70)
        self.check_common(html_text, chunks, 70)

    def test_entity_refs_never_torn(self):
        html_text = "AT&amp; " * 300
        chunks = split_html(html_text, 10)
        for chunk in chunks:
            residue = chunk.replace("&amp;", "")
            self.assertNotIn("&", residue, f"сущность разорвана: {chunk!r}")
        self.check_common(html_text, chunks, 10)

    def test_boundary_prefers_newline(self):
        # Границы встают ровно после переводов строк, когда они есть
        html_text = "a" * 95 + "\n" + "b" * 95 + "\n" + "c" * 95
        chunks = split_html(html_text, 100)
        self.assertEqual(chunks, ["a" * 95 + "\n", "b" * 95 + "\n", "c" * 95])

    def test_blockquote_cut_between_open_and_close(self):
        html_text = "<blockquote>" + "ц" * 120 + "</blockquote>"
        chunks = split_html(html_text, 60)
        self.check_common(html_text, chunks, 60)
        self.assertTrue(chunks[0].startswith("<blockquote>"))
        self.assertTrue(chunks[0].endswith("</blockquote>"))

    def test_emoji_chunks_respect_utf16_limit(self):
        html_text = "😀" * 3000
        chunks = split_html(html_text, 100)
        self.check_common(html_text, chunks, 100)
        # Смайлик — один токен: части склеиваются в исходный текст
        self.assertEqual("".join(chunks), html_text)

    def test_link_cut_reopens_with_attributes(self):
        html_text = '<a href="https://example.com/page">' + "т" * 150 + "</a>"
        chunks = split_html(html_text, 80)
        self.check_common(html_text, chunks, 80)
        for chunk in chunks:
            self.assertTrue(chunk.startswith('<a href="https://example.com/page">'))
            self.assertTrue(chunk.endswith("</a>"))


if __name__ == "__main__":
    unittest.main()
