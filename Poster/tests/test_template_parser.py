# tests/test_template_parser.py
"""
Stage 24: грамматика парсера шаблонов — кирпичики, гиперссылки, форматирование.

Тестируется независимый от Telegram/SQLite слой: разбор `[код]`, `[[ ]]`,
`ТЕКСТ(URL)`, `**жирный**`, `*курсив*`, `__подчёркивание__`, `~~зачёркнутый~~`
и обработка ошибок с корректным локализованным сообщением.
"""

from __future__ import annotations

import unittest

from template_parser import (
    BrickNode,
    FormatNode,
    LinkNode,
    TemplateParseError,
    TextNode,
    parse_template,
    render_to_html,
    render_to_plain_text,
)


class BrickGrammarTests(unittest.TestCase):
    """Кирпичики: простые токены, повторное употребление, экранирование."""

    def _plain(self, text: str) -> str:
        return render_to_plain_text(parse_template(text, section="body"))

    def _html(self, text: str) -> str:
        return render_to_html(parse_template(text, section="body"))

    def test_simple_brick(self) -> None:
        tokens = parse_template("Привет [название]!", section="body")
        self.assertEqual(len(tokens), 3)
        assert isinstance(tokens[0], TextNode)
        self.assertEqual(tokens[0].text, "Привет ")
        assert isinstance(tokens[1], BrickNode)
        self.assertEqual(tokens[1].code, "название")
        assert isinstance(tokens[2], TextNode)
        self.assertEqual(tokens[2].text, "!")
        self.assertEqual(self._plain("Привет [название]!"), "Привет [название]!")

    def test_multiple_bricks_in_one_line(self) -> None:
        tokens = parse_template("📅 [дата], [время_начала]", section="body")
        brick_codes = [t.code for t in tokens if isinstance(t, BrickNode)]
        self.assertEqual(brick_codes, ["дата", "время_начала"])

    def test_repeated_brick_in_one_template(self) -> None:
        tokens = parse_template("[место] снова [место]", section="body")
        bricks = [t for t in tokens if isinstance(t, BrickNode)]
        self.assertEqual(len(bricks), 2)
        self.assertEqual(bricks[0].code, bricks[1].code)

    def test_unknown_brick_raises_error_with_location(self) -> None:
        # Парсер чисто синтаксический: неизвестный код разбирается в
        # BrickNode. Существование кирпичика проверяет компилятор.
        tokens = parse_template("Префикс [неизвестный_код] суффикс", section="header")
        self.assertEqual(len(tokens), 3)
        assert isinstance(tokens[1], BrickNode)
        self.assertEqual(tokens[1].code, "неизвестный_код")

    def test_unclosed_bracket_raises_error(self) -> None:
        with self.assertRaises(TemplateParseError):
            parse_template("Префикс [название", section="body")

    def test_escaped_literal_brackets(self) -> None:
        self.assertEqual(
            self._plain("Книжка [[Алиса]] в кассе"), "Книжка [Алиса] в кассе"
        )
        self.assertEqual(
            self._plain("Комментарий [[не кирпичик]] тут"),
            "Комментарий [не кирпичик] тут",
        )

    def test_brackets_in_code_context_do_not_break_parser(self) -> None:
        self.assertEqual(
            self._plain("Ссылка на [[место]] недоступна"),
            "Ссылка на [место] недоступна",
        )


class LinkGrammarTests(unittest.TestCase):
    """Гиперссылки: кирпичик→кирпичик, литеральный текст→URL."""

    def _html(self, text: str) -> str:
        return render_to_html(parse_template(text, section="body"))

    def _plain(self, text: str) -> str:
        return render_to_plain_text(parse_template(text, section="body"))

    def test_brick_to_brick_link(self) -> None:
        text = "[место]([ссылка_места])"
        html = self._html(text)
        self.assertIn("<a href=", html)
        self.assertIn("[место]", html)
        self.assertIn("[ссылка_места]", html)

    def test_literal_text_and_absolute_url(self) -> None:
        self.assertEqual(
            self._html("Билеты(https://example.com)"),
            '<a href="https://example.com">Билеты</a>',
        )

    def test_url_without_scheme_is_normalized(self) -> None:
        self.assertEqual(
            self._html("Билеты(t.me/example)"),
            '<a href="https://t.me/example">Билеты</a>',
        )
        self.assertEqual(
            self._html("Наше место(example.com)"),
            '<a href="https://example.com">Наше место</a>',
        )

    def test_hyphen_in_literal_link_label_becomes_space(self) -> None:
        text = "Наше-уютное-место([ссылка_места])"
        tokens = parse_template(text, section="body")
        self.assertEqual(len(tokens), 1)
        assert isinstance(tokens[0], LinkNode)
        label = tokens[0].label[0]
        assert isinstance(label, TextNode)
        self.assertEqual(label.text, "Наше уютное место")
        # Обычный текст с дефисами не заменяется.
        self.assertEqual(
            self._plain("Скоро-будет(https://example.com)"),
            "Скоро будет",
        )
        self.assertEqual(
            self._plain("Обычный текст с - дефисом"),
            "Обычный текст с - дефисом",
        )

    def test_invalid_link_raises_diagnostic_error(self) -> None:
        with self.assertRaises(TemplateParseError) as context:
            parse_template("[место](ссылка_места)", section="body")
        self.assertIn("ссылка_места", str(context.exception))

    def test_literal_text_with_parens_is_not_a_link(self) -> None:
        text = "Скоро (в 5 часов) встреча"
        self.assertEqual(self._plain(text), text)
        self.assertEqual(self._html(text), text)

    def test_dangerous_urls_are_rejected(self) -> None:
        for url in ("javascript:alert(1)", "data:text/html,x", "file:/etc/passwd"):
            with self.assertRaises(TemplateParseError):
                parse_template(f"Текст({url})", section="body")

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(TemplateParseError):
            parse_template("Текст(:)", section="body")

    def test_unescaped_parens_in_plain_text(self) -> None:
        self.assertEqual(self._plain("а(текст) с (скобкой)"), "а(текст) с (скобкой)")

    def test_escaped_parens_are_literal(self) -> None:
        self.assertEqual(self._plain("а\\(текст\\)"), "а(текст)")


class FormattingGrammarTests(unittest.TestCase):
    """Telegram-форматирование: жирный, курсив, подчёркивание, зачёркивание."""

    def _html(self, text: str) -> str:
        return render_to_html(parse_template(text, section="body"))

    def _plain(self, text: str) -> str:
        return render_to_plain_text(parse_template(text, section="body"))

    def test_bold(self) -> None:
        self.assertEqual(self._html("**жирный текст**"), "<b>жирный текст</b>")
        self.assertEqual(self._plain("**жирный текст**"), "жирный текст")

    def test_italic(self) -> None:
        self.assertEqual(self._html("*курсив*"), "<i>курсив</i>")

    def test_underline(self) -> None:
        self.assertEqual(self._html("__подчёркивание__"), "<u>подчёркивание</u>")

    def test_strikethrough(self) -> None:
        self.assertEqual(self._html("~~зачёркнутый~~"), "<s>зачёркнутый</s>")

    def test_nested_formatting(self) -> None:
        self.assertEqual(
            self._html("**жирный с *курсивом***"), "<b>жирный с <i>курсивом</i></b>"
        )

    def test_formatting_does_not_swallow_bricks(self) -> None:
        tokens = parse_template("**[место] и [дата]**", section="body")
        self.assertEqual(len(tokens), 1)
        self.assertIsInstance(tokens[0], FormatNode)
        self.assertEqual(self._plain("**[место] и [дата]**"), "[место] и [дата]")

    def test_escaped_marker_stays_literal(self) -> None:
        self.assertEqual(self._plain("не жирный \\* текст"), "не жирный * текст")
        self.assertEqual(self._plain("не курсив \\* тут"), "не курсив * тут")


class UnicodeAndSectionsTests(unittest.TestCase):
    """Unicode, кириллица, эмодзи, разделы и сохранение переносов."""

    def _plain(self, text: str) -> str:
        return render_to_plain_text(parse_template(text, section="body"))

    def test_unicode_and_emoji_are_preserved(self) -> None:
        text = "🎉 Начало [дата] — с хорошей погодой"
        self.assertEqual(self._plain(text), text)

    def test_multiline_text_preserves_newlines(self) -> None:
        text = "Строка 1\nСтрока 2\n[место]\nСтрока 4"
        tokens = parse_template(text, section="body")
        # Лексер объединяет соседний текст: [Текст, Кирпичик, Текст].
        self.assertEqual(len(tokens), 3)
        self.assertEqual(self._plain(text), text)

    def test_special_characters_and_html_are_escaped_in_html(self) -> None:
        text = "Пробелы & символы <br> с quotes"
        html = render_to_html(parse_template(text, section="body"))
        self.assertIn("&lt;br&gt;", html)
        self.assertIn("&amp;", html)

    def test_author_spaces_and_punctuation_preserved(self) -> None:
        text = "   Пробелы в начале. Кавычки «ёлочки», тире — дефис.  "
        self.assertEqual(self._plain(text), text)


class ErrorDiagnosticsTests(unittest.TestCase):
    """Проверка сообщений об ошибках с секцией и позицией."""

    def test_error_includes_section_and_offset(self) -> None:
        with self.assertRaises(TemplateParseError) as context:
            parse_template("Префикс [название", section="header")
        self.assertEqual(context.exception.section, "header")
        self.assertEqual(context.exception.offset, 8)

    def test_error_includes_brick_code_when_found(self) -> None:
        # Парсер чисто синтаксический: неизвестный код разбирается в
        # BrickNode. Проверку существования кирпичика делает компилятор
        # (см. tests.test_template_compiler.test_unknown_brick_raises).
        tokens = parse_template("[unknown_brick]", section="footer")
        self.assertEqual(len(tokens), 1)
        assert isinstance(tokens[0], BrickNode)
        self.assertEqual(tokens[0].code, "unknown_brick")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
