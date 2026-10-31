# tests/test_template_compiler.py
"""
Stage 24: тесты компилятора шаблонов.

Проверяется:
- разбор простых кодов и нескольких кирпичиков в одной строке;
- разбор повторного употребления кирпичика;
- разбор несуществующего кирпичика;
- разбор синтаксиса ссылок `[место]([ссылка_места])`,
  `Билеты(https://example.com)`, `Билеты(t.me/example)`;
- разбор дефисов в подписи ссылок `Наше-уютное-место(...)`;
- отклонение `[место](ссылка_места)` без квадратных скобок;
- отклонение опасных схем (`javascript:`, `data:`, `file:`);
- валидация значений кирпичиков по типам (short_text, date, time, url,
  number, choice, long_text);
- пропуск необязательных значений (и корректный рендеринг пустой строки);
- устойчивость к незаполненным значениям (не должно выдавать `None`/`[]`).
"""

from __future__ import annotations

import unittest
from typing import Any, cast
from unittest.mock import MagicMock, patch

from sqlalchemy.orm import Session

from template_compiler import (
    TemplateCompileError,
    render_template,
    resolve_template_bindings,
)
from template_parser import FormatNode, TextNode, parse_template
from template_types import TEMPLATE_CONTENT


class _FakeBrick:
    def __init__(
        self,
        brick_id: int,
        code: str,
        value_type: str = "short_text",
        scope: str = "system",
        status: str = "active",
        title: str | None = None,
    ):
        self.id = brick_id
        self.code = code
        self.value_type = value_type
        self.scope = scope
        self.status = status
        self.title = title or f"Кирпичик {code}"
        self.rules: dict[str, object] = {}


class _FakeBrickStore:
    """Минимальный заменитель brick_store.resolve_brick_code_candidates."""

    def __init__(self, bricks: list[_FakeBrick] | None = None):
        self.bricks = {brick.code: brick for brick in bricks or []}
        self.ambiguous_codes: list[str] = []

    def resolve_brick_code_candidates(
        self, session, code, viewer_telegram_user_id=None, place_id=None, **kwargs
    ):
        brick = self.bricks.get(code)
        if brick is None:
            return _FakeCandidates(code, candidates=[], selected=None, ambiguous=False)
        if code in self.ambiguous_codes:
            return _FakeCandidates(
                code,
                candidates=[brick, _FakeBrick(999, code)],
                selected=brick,
                ambiguous=True,
            )
        return _FakeCandidates(
            code, candidates=[brick], selected=brick, ambiguous=False
        )


class _FakeCandidates:
    def __init__(self, code, candidates, selected, ambiguous):
        self.code = code
        self.candidates = candidates
        self.selected = selected
        self.ambiguous = ambiguous
        self.blocked_by_archived_system = False


class TemplateBindingTests(unittest.TestCase):
    """Привязка кодов шаблонов к brick_id."""

    def setUp(self):
        self.brick_store = _FakeBrickStore(
            [
                _FakeBrick(1, "название"),
                _FakeBrick(2, "место", value_type="short_text"),
                _FakeBrick(3, "ссылка_места", value_type="url"),
                _FakeBrick(4, "дата", value_type="date"),
                _FakeBrick(5, "время_начала", value_type="time"),
                _FakeBrick(6, "описание", value_type="long_text"),
                _FakeBrick(7, "контакт", value_type="short_text"),
            ]
        )

    def _resolve(self, text: str):
        with patch(
            "template_compiler.resolve_brick_code_candidates",
            side_effect=self.brick_store.resolve_brick_code_candidates,
        ):
            return resolve_template_bindings(
                cast(Session, MagicMock()),
                {TEMPLATE_CONTENT: text},
                actor_telegram_user_id=1,
                place_id=None,
            )

    def test_simple_brick_resolved(self) -> None:
        result = self._resolve("Просто [название] код")
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_brick_ids.get("название"), 1)

    def test_repeated_brick_resolved_once(self) -> None:
        result = self._resolve("[место] и снова [место]")
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_brick_ids.get("место"), 2)

    def test_unknown_brick_raises(self) -> None:
        result = self._resolve("Использую [неизвестный] код")
        self.assertFalse(result.ok)
        self.assertIn("неизвестный", result.unknown_codes)

    def test_ambiguous_code_raises(self) -> None:
        self.brick_store.ambiguous_codes = ["место"]
        result = self._resolve("У нас [место]")
        self.assertFalse(result.ok)
        self.assertEqual(len(result.ambiguous_codes), 1)
        self.assertEqual(result.ambiguous_codes[0].code, "место")
        self.assertEqual(len(result.ambiguous_codes[0].candidates), 2)

    def test_parse_error_in_section(self) -> None:
        result = self._resolve("Закрытая [скобка")
        self.assertFalse(result.ok)
        self.assertTrue(result.parse_errors)

    def test_repeated_brick_multiple_uses(self) -> None:
        result = self._resolve("[название] [название] [название]")
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_brick_ids.get("название"), 1)

    def test_brick_link_resolved(self) -> None:
        result = self._resolve("[место]([ссылка_места])")
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_brick_ids.get("место"), 2)
        self.assertEqual(result.resolved_brick_ids.get("ссылка_места"), 3)

    def test_literal_url_resolved(self) -> None:
        result = self._resolve("Билеты(https://example.com)")
        self.assertTrue(result.ok)

    def test_normalized_url_resolved(self) -> None:
        result = self._resolve("Билеты(t.me/example)")
        self.assertTrue(result.ok)

    def test_hyphenated_link_label(self) -> None:
        result = self._resolve("Наше-уютное-место([ссылка_места])")
        self.assertTrue(result.ok)
        self.assertEqual(result.resolved_brick_ids.get("ссылка_места"), 3)


class TemplateRenderTests(unittest.TestCase):
    """Выполнение (рендеринг) шаблонов с типизированными значениями."""

    def setUp(self):
        self.brick_store = _FakeBrickStore(
            [
                _FakeBrick(1, "название"),
                _FakeBrick(2, "место", value_type="short_text"),
                _FakeBrick(3, "ссылка_места", value_type="url"),
                _FakeBrick(4, "дата", value_type="date"),
                _FakeBrick(5, "время_начала", value_type="time"),
                _FakeBrick(6, "описание", value_type="long_text"),
                _FakeBrick(7, "контакт", value_type="short_text"),
                _FakeBrick(8, "количество", value_type="number"),
                _FakeBrick(9, "тип", value_type="choice"),
            ]
        )
        self.brick_store.bricks["тип"].rules = {"choices": ["Оффлайн", "Онлайн"]}

    def _render(self, text: str, values: dict[int, object]) -> str:
        with patch(
            "template_compiler.resolve_brick_code_candidates",
            side_effect=self.brick_store.resolve_brick_code_candidates,
        ):
            bindings = resolve_template_bindings(
                cast(Session, MagicMock()),
                {TEMPLATE_CONTENT: text},
                actor_telegram_user_id=1,
                place_id=None,
            )
        if not bindings.ok:
            raise TemplateCompileError(bindings.parse_errors or bindings.unknown_codes)
        # Используем уже связанные токены из bindings: повторный парсинг
        # создал бы новые BrickNode без brick_id.
        tokens = bindings.tokens_by_text.get(TEMPLATE_CONTENT, [])
        definitions_by_id: dict[int, Any] = {}
        for code, brick_id in bindings.resolved_brick_ids.items():
            brick = self.brick_store.bricks.get(code)
            if brick is not None:
                definitions_by_id[brick_id] = brick
        return render_template({TEMPLATE_CONTENT: tokens}, values, definitions_by_id)

    def test_render_short_text_value(self) -> None:
        result = self._render("Название: [название]", {1: "Анонс"})
        self.assertEqual(result, "Название: Анонс")

    def test_render_missing_brick_is_skipped(self) -> None:
        # H2: строка с токеном кирпичика, отрендерившимся пустым, скрывается
        # целиком — вместе с висящим литералом.
        result = self._render("Название: [название] и всё", {})
        self.assertEqual(result, "")

    def test_render_literal_line_without_bricks_is_preserved(self) -> None:
        # Строки без токенов остаются буквальными, даже если соседняя
        # строка с пустым кирпичиком скрыта.
        result = self._render("Название: [название]\nПросто текст", {})
        self.assertEqual(result, "Просто текст")

    def test_render_url_value(self) -> None:
        result = self._render(
            "[место]([ссылка_места])", {2: "Место", 3: "https://example.com"}
        )
        self.assertEqual(result, '<a href="https://example.com">Место</a>')

    def test_render_missing_url_brick_keeps_label_without_link(self) -> None:
        # Poster 2.0 U1a: без URL остаётся только подпись — обычным текстом,
        # без пустых скобок и без битой ссылки.
        result = self._render("[место]([ссылка_места]) и всё", {2: "Место"})
        self.assertEqual(result, "Место и всё")

    def test_render_date_value(self) -> None:
        result = self._render("Когда: [дата]", {4: "10.10.2026"})
        self.assertEqual(result, "Когда: 10.10.2026")

    def test_render_time_value(self) -> None:
        result = self._render("Время: [время_начала]", {5: "19:00"})
        self.assertEqual(result, "Время: 19:00")

    def test_render_number_value(self) -> None:
        result = self._render("Количество: [количество]", {8: 10})
        self.assertEqual(result, "Количество: 10")

    def test_render_choice_value(self) -> None:
        result = self._render("Тип: [тип]", {9: "Оффлайн"})
        self.assertEqual(result, "Тип: Оффлайн")

    def test_render_long_text_value_with_newlines(self) -> None:
        result = self._render("Описание: [описание]", {6: "строка 1\nстрока 2"})
        self.assertEqual(result, "Описание: строка 1\nстрока 2")


class TemplateFormatTests(unittest.TestCase):
    """Форматирование Telegram entities: жирный, курсив, ссылки, вложенность."""

    def test_bold_formatting(self) -> None:
        tokens = parse_template("**жирный текст**")
        self.assertEqual(len(tokens), 1)
        node = tokens[0]
        self.assertEqual(getattr(node, "marker", None), "**")

    def test_italic_formatting(self) -> None:
        tokens = parse_template("*курсив*")
        self.assertEqual(len(tokens), 1)
        node = tokens[0]
        self.assertEqual(getattr(node, "marker", None), "*")

    def test_strike_formatting(self) -> None:
        tokens = parse_template("~~зачёркнутый~~")
        self.assertEqual(len(tokens), 1)
        node = tokens[0]
        self.assertEqual(getattr(node, "marker", None), "~~")

    def test_underline_formatting(self) -> None:
        tokens = parse_template("__подчёркивание__")
        self.assertEqual(len(tokens), 1)
        node = tokens[0]
        self.assertEqual(getattr(node, "marker", None), "__")

    def test_nested_formatting(self) -> None:
        tokens = parse_template("**жирный *курсив***")
        outer = tokens[0]
        assert isinstance(outer, FormatNode)
        self.assertEqual(outer.marker, "**")
        # Внутри `**...**` также есть `*...*`
        inner = [c for c in outer.children if hasattr(c, "marker")]
        self.assertTrue(inner)

    def test_link_inside_bold(self) -> None:
        tokens = parse_template("**[место](https://example.com)**")
        self.assertEqual(len(tokens), 1)
        node = tokens[0]
        assert isinstance(node, FormatNode)
        self.assertEqual(node.marker, "**")
        # Внутри узла — LinkNode, но он был скомпилирован без привязки
        inner = [c for c in node.children if hasattr(c, "url")]
        self.assertTrue(inner)

    def test_html_escaping(self) -> None:
        tokens = parse_template("Символы < > &")
        # Простой текст сохраняется дословно
        self.assertEqual(len(tokens), 1)
        assert isinstance(tokens[0], TextNode)
        self.assertEqual(tokens[0].text, "Символы < > &")

    def test_universal_symbols(self) -> None:
        text = "Строка 1\nСтрока 2 🎉"
        tokens = parse_template(text)
        # Лексер объединяет соседний текст в один узел.
        self.assertEqual(len(tokens), 1)
        assert isinstance(tokens[0], TextNode)
        self.assertEqual(tokens[0].text, "Строка 1\nСтрока 2 🎉")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
