# template_compiler.py
"""
Stage 24: компилятор шаблонов анонсов.

Разделяет две операции:

1.  **Разбор и связывание** (`resolve_template_bindings`): находит токены
    `[код]`, проверяет грамматику и определяет доступных кандидатов.
    Возвращает привязки к постоянным `brick_id` для сохранения версии.
    При ошибке частично корректная версия не сохраняется.

2.  **Выполнение** (`render_template`): собирает итоговый текст анонса из
    связанной версии или snapshot'а и значений кирпичиков. Обозначения не
    разрешаются заново: используются сохранённые brick_id.

Границы этапа:

- Telegram-редактор шаблонов — Stage 25;
- согласование новых кирпичиков — Stage 26;
- подключение снимков к мастеру постов — Stage 27.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from brick_store import BrickDefinition, resolve_brick_code_candidates
from brick_types import (
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_URL,
)
from template_parser import (
    BrickNode,
    FormatNode,
    LinkNode,
    TemplateParseError,
    TextNode,
    parse_template,
)
from template_types import (
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)

_SECTION_ORDER = (
    TEMPLATE_SECTION_HEADER,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
)


class TemplateCompileError(ValueError):
    """Ошибка разбора, привязки или исполнения шаблона."""


class TemplateBindingConflictError(TemplateCompileError):
    """Неоднозначный `[код]` требует явного выбора brick_id пользователем."""


@dataclass(frozen=True)
class BindingCandidate:
    """Один кирпичик-кандидат для неоднозначного кода."""

    brick_id: int
    code: str
    title: str
    scope: str
    status: str

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"Candidate(id={self.brick_id}, code={self.code!r}, scope={self.scope!r})"
        )


@dataclass(frozen=True)
class AmbiguousCode:
    """
    Секция + код, по которым найдено несколько доступных кирпичиков.

    Редактор Stage 25 обязан показать список кандидатов и сохранить выбранный
    `brick_id` в привязках версии.
    """

    section: str
    code: str
    candidates: tuple[BindingCandidate, ...] = ()

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AmbiguousCode(section={self.section!r}, code={self.code!r}, "
            f"candidates={len(self.candidates)})"
        )


@dataclass
class TemplateBindings:
    """Результат разбора и привязки одной версии шаблона."""

    resolved_brick_ids: dict[str, int] = field(default_factory=dict)
    ambiguous_codes: list[AmbiguousCode] = field(default_factory=list)
    unknown_codes: list[str] = field(default_factory=list)
    parse_errors: list[TemplateParseError] = field(default_factory=list)
    tokens_by_section: dict[str, list[Any]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            not self.ambiguous_codes
            and not self.unknown_codes
            and not self.parse_errors
        )


def _binding_candidate(definition: BrickDefinition) -> BindingCandidate:
    return BindingCandidate(
        brick_id=definition.id,
        code=definition.code,
        title=definition.title,
        scope=definition.scope,
        status=definition.status,
    )


def _resolve_codes_in_section(
    session: Session,
    section: str,
    tokens: Sequence[Any],
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> tuple[dict[str, int], list[AmbiguousCode], list[str]]:
    """
    Внутренняя привязка одного текста: заполняет `brick_id` у `BrickNode`.

    Дважды повторяющиеся коды одного варианта не создают дублирующих
    привязок; каждая привязка однозначно указывает на конкретный brick_id.
    """
    resolved: dict[str, int] = {}
    ambiguous: list[AmbiguousCode] = []
    unknown: list[str] = []

    def _visit(node: Any) -> None:
        if isinstance(node, BrickNode):
            code = node.code
            if code in resolved:
                node.brick_id = resolved[code]
                return
            candidates = resolve_brick_code_candidates(
                session,
                code,
                viewer_telegram_user_id=actor_telegram_user_id,
                place_id=place_id,
            )
            if candidates.blocked_by_archived_system:
                unknown.append(code)
                return
            if candidates.ambiguous:
                ambiguous.append(
                    AmbiguousCode(
                        section=section,
                        code=code,
                        candidates=tuple(
                            _binding_candidate(row) for row in candidates.candidates
                        ),
                    )
                )
                return
            if candidates.selected is None:
                unknown.append(code)
                return
            resolved[code] = candidates.selected.id
            node.brick_id = candidates.selected.id
        elif isinstance(node, LinkNode):
            _visit_token_list(node.label)
            if isinstance(node.url, BrickNode):
                _visit(node.url)
        elif hasattr(node, "children"):
            _visit_token_list(getattr(node, "children"))

    def _visit_token_list(children: Iterable[Any]) -> None:
        for child in children:
            _visit(child)

    for token in tokens:
        _visit(token)
    return resolved, ambiguous, unknown


def resolve_template_bindings(
    session: Session,
    sections: Mapping[str, str],
    *,
    actor_telegram_user_id: int | None,
    place_id: int | None,
) -> TemplateBindings:
    """
    Разбирает три секции и возвращает привязки для сохранения версии.

    Секции обрабатываются в порядке header → body → footer, чтобы порядок
    ошибок был предсказуемым. Список `unknown_codes` содержит неизвестные
    коды, для которых не найдено ни одного доступного кирпичика.
    """
    bindings = TemplateBindings()
    for section in _SECTION_ORDER:
        raw = sections.get(section)
        if raw is None:
            continue
        try:
            bindings.tokens_by_section[section] = parse_template(raw, section=section)
        except TemplateParseError as exc:
            bindings.parse_errors.append(exc)
            continue
    if bindings.parse_errors:
        return bindings

    for section, tokens in bindings.tokens_by_section.items():
        resolved, ambiguous, unknown = _resolve_codes_in_section(
            session,
            section,
            tokens,
            actor_telegram_user_id=actor_telegram_user_id,
            place_id=place_id,
        )
        for code, brick_id in resolved.items():
            bindings.resolved_brick_ids.setdefault(code, brick_id)
        bindings.ambiguous_codes.extend(ambiguous)
        bindings.unknown_codes.extend(unknown)
    return bindings


def _format_brick_value(value: Any, value_type: str) -> str:
    """
    Приводит значение кирпичика к стабильному тексту для конечного анонса.

    Даты и время рендерятся по правилам Stage 17 (`дд.мм.гггг` / `чч:мм`).
    Логические и пустые значения выводятся как пустая строка.
    """
    if value is None:
        return ""
    if value_type == BRICK_TYPE_DATE:
        return str(value)
    if value_type == BRICK_TYPE_TIME:
        return str(value)
    if value_type in (BRICK_TYPE_NUMBER, BRICK_TYPE_SHORT_TEXT, BRICK_TYPE_URL):
        return str(value)
    if value_type == BRICK_TYPE_LONG_TEXT:
        return str(value)
    if value_type == BRICK_TYPE_CHOICE:
        return str(value)
    return str(value)


_FORMAT_TAGS = {"**": "b", "*": "i", "__": "u", "~~": "s"}


def _escape_html(text: str, *, quote: bool = False) -> str:
    """Экранирует HTML-спецсимволы для безопасного вывода в Telegram."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        if quote
        else text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _section_html(
    tokens: Sequence[Any],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
    *,
    skip_missing: bool = True,
) -> str:
    """
    Визуализирует дерево одной секции, подставляя значения кирпичиков.

    `skip_missing=True` удаляет токены кирпичиков, для которых значение
    отсутствует (вместо вывода `None`/`[]`). Тексты экранируются один раз;
    форматирование превращается в теги Telegram (`<b>`, `<i>`, `<u>`, `<s>`).
    """
    parts: list[str] = []

    def _visit(node: Any) -> str | None:
        if isinstance(node, TextNode):
            return _escape_html(node.text)
        if isinstance(node, BrickNode):
            if node.brick_id is None:
                return None
            value = values_by_id.get(node.brick_id)
            if value is None or value == "":
                return None
            definition = definitions_by_id.get(node.brick_id)
            value_type = definition.value_type if definition else BRICK_TYPE_SHORT_TEXT
            return _escape_html(_format_brick_value(value, value_type))
        if isinstance(node, LinkNode):
            label = _visit_token_list(node.label)
            if not label:
                return None
            if isinstance(node.url, BrickNode):
                if node.url.brick_id is None:
                    return None
                url_value = values_by_id.get(node.url.brick_id)
                if not url_value:
                    # Не выводим пустые/битые ссылки; подпись тоже скрываем.
                    return None
                url = _format_brick_value(url_value, BRICK_TYPE_URL)
            else:
                url = node.url
            if not url:
                return None
            return f'<a href="{_escape_html(url, quote=True)}">{label}</a>'
        if isinstance(node, FormatNode):
            inner = _visit_token_list(node.children)
            tag = _FORMAT_TAGS.get(node.marker)
            if tag:
                return f"<{tag}>{inner}</{tag}>"
            return inner
        if isinstance(node, str):
            return _escape_html(node)
        return None

    def _visit_token_list(children: Iterable[Any]) -> str:
        inner: list[str] = []
        for child in children:
            rendered = _visit(child)
            if rendered is not None:
                inner.append(rendered)
        return "".join(inner)

    for token in tokens:
        rendered = _visit(token)
        if rendered is not None:
            parts.append(rendered)
    return "".join(parts)


def render_template(
    tokens_by_section: Mapping[str, Sequence[Any]],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> str:
    """
    Собирает итоговый HTML-анонс из уже связанных секций.

    Не выполняет повторного разбора: привязки и структура уже сохранены.
    """
    sections: list[str] = []
    for section in _SECTION_ORDER:
        tokens = tokens_by_section.get(section)
        if not tokens:
            continue
        html = _section_html(
            tokens,
            values_by_id,
            definitions_by_id,
        )
        if html:
            sections.append(html)
    return "\n".join(sections)
