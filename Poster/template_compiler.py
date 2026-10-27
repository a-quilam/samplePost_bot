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

3.  **Единый рендер анонса** (`render_announcement`): тот же результат плюс
    решение о технической заглушке `.`. Poster 2.0 использует его и для
    живого верхнего блока (S3), и для итоговой отправки (S4): один сборщик,
    одно поведение.

Статический текст шаблона выводится буквально: никакой постобработки,
«чистки сирот» и удаления литералов (`//` остаётся `//`, даже если обе
подстановки пусты). Техническая `.` — это решение уровня сообщения, а не
значения кирпичика, поэтому она добавляется здесь, а не в `values_by_id`.

Границы этапа:

- Telegram-редактор шаблонов — Stage 25;
- согласование новых кирпичиков — Stage 26;
- подключение снимков к мастеру постов — Stage 27;
- живой/итоговый рендер Poster 2.0 — S3.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from announcement_wizard import visible_announcement_text
from brick_store import BrickDefinition, resolve_brick_code_candidates
from brick_types import (
    validate_brick_value,
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
from utils.tg_html import utf16_len
from utils.validators import normalize_url

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
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> tuple[dict[str, int], list[AmbiguousCode], list[str]]:
    """
    Внутренняя привязка одного текста: заполняет `brick_id` у `BrickNode`.

    Дважды повторяющиеся коды одного варианта не создают дублирующих
    привязок; каждая привязка однозначно указывает на конкретный brick_id.

    `chosen_brick_ids` — явный выбор пользователя для неоднозначных кодов.
    Он используется только для кодов, по которым найдено несколько
    кандидатов, и только если выбранный brick_id есть среди кандидатов.
    Для однозначных кодов выбор вызывающего кода игнорируется — так
    невозможно обойти проверку, передав произвольный список ID.
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
                explicit = (chosen_brick_ids or {}).get(code)
                if explicit is not None:
                    match = next(
                        (row for row in candidates.candidates if row.id == explicit),
                        None,
                    )
                    if match is not None:
                        resolved[code] = explicit
                        node.brick_id = explicit
                        return
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
    chosen_brick_ids: Mapping[str, int] | None = None,
) -> TemplateBindings:
    """
    Разбирает три секции и возвращает привязки для сохранения версии.

    Секции обрабатываются в порядке header → body → footer, чтобы порядок
    ошибок был предсказуемым. Список `unknown_codes` содержит неизвестные
    коды, для которых не найдено ни одного доступного кирпичика.

    `chosen_brick_ids` — явный выбор пользователя для неоднозначных кодов:
    отображение «код → brick_id». Выбор применяется только если выбранный
    brick_id действительно является кандидатом для этого кода; для
    однозначных кодов выбор вызывающего кода игнорируется.
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
            chosen_brick_ids=chosen_brick_ids,
        )
        for code, brick_id in resolved.items():
            bindings.resolved_brick_ids.setdefault(code, brick_id)
        bindings.ambiguous_codes.extend(ambiguous)
        bindings.unknown_codes.extend(unknown)
    return bindings


def _brick_text(value: Any, definition: BrickDefinition) -> str:
    """
    Стабильный текст значения кирпичика с строгой валидацией.

    Все семь типов проверяются через `validate_brick_value` — значение
    не подменяется молча на short_text, ошибки данных не превращаются
    в правдоподобный, но некорректный анонс.
    """
    result = validate_brick_value(definition.value_type, value, rules=definition.rules)
    if not result.is_valid:
        raise TemplateCompileError(
            f"Значение кирпичика «{definition.code}» некорректно: {result.error}"
        )
    payload = result.payload or {}
    for key in ("text", "date", "time", "url", "number", "choice"):
        if key in payload:
            return str(payload[key])
    if "choices" in payload:
        choices = payload["choices"]
        if isinstance(choices, (list, tuple)):
            return ", ".join(str(item) for item in choices)
    return str(payload) if payload else ""


def _safe_url(value: Any, *, code: str = "") -> str:
    """
    Безопасный адрес гиперссылки из значения кирпичика.

    Проверяет схему http/https, отклоняет `javascript:`, `data:`, `file:`
    и произвольный текст. Некорректный адрес — ошибка, а не правдоподобный
    анонс с битой ссылкой.
    """
    text = str(value) if value is not None else ""
    normalized = normalize_url(text)
    if normalized is None:
        raise TemplateCompileError(
            f"Значение кирпичика «{code}» не является корректной "
            f"http(s)-ссылкой: {text!r}."
        )
    return normalized


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
) -> str:
    """
    Визуализирует дерево одной секции, подставляя значения кирпичиков.

    Значения валидируются по типу кирпичика (`validate_brick_value`):
    дата/время/число/выбор/текст/ссылка — все семь типов. URL дополнительно
    нормализуется и проверяется на схему непосредственно перед созданием
    гиперссылки. Отсутствующее значение пропускает токен (необязательное
    поле); некорректное значение — диагностируемая ошибка, а не вывод
    `None`/`[]` в готовом анонсе.
    """
    parts: list[str] = []

    def _visit(node: Any) -> str | None:
        if isinstance(node, TextNode):
            return _escape_html(node.text)
        if isinstance(node, BrickNode):
            if node.brick_id is None:
                raise TemplateCompileError(
                    "Токен кирпичика без привязки brick_id: "
                    "версия шаблона повреждена."
                )
            definition = definitions_by_id.get(node.brick_id)
            if definition is None:
                raise TemplateCompileError(
                    f"Определение кирпичика #{node.brick_id} не найдено: "
                    "нельзя подменить тип значения на short_text."
                )
            if node.brick_id not in values_by_id:
                return None  # Необязательное значение пропускается.
            value = values_by_id[node.brick_id]
            if value is None or (isinstance(value, str) and not value.strip()):
                return None
            return _escape_html(_brick_text(value, definition))
        if isinstance(node, LinkNode):
            label = _visit_token_list(node.label)
            if not label:
                return None
            if isinstance(node.url, BrickNode):
                if node.url.brick_id is None:
                    raise TemplateCompileError(
                        "URL ссылки без привязки brick_id: "
                        "версия шаблона повреждена."
                    )
                url_definition = definitions_by_id.get(node.url.brick_id)
                if url_definition is None:
                    raise TemplateCompileError(
                        f"Определение кирпичика #{node.url.brick_id} не найдено."
                    )
                if node.url.brick_id not in values_by_id:
                    # Не выводим пустые/битые ссылки; подпись тоже скрываем.
                    return None
                url_value = values_by_id[node.url.brick_id]
                if not url_value or (
                    isinstance(url_value, str) and not url_value.strip()
                ):
                    return None
                url = _safe_url(url_value, code=url_definition.code)
            else:
                url = _normalize_render_url(node.url)
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


def _normalize_render_url(raw: str) -> str:
    """
    Нормализует литеральный URL гиперссылки перед созданием `<a href>`.

    Опасные схемы и произвольный текст отклоняются; корректные адреса
    приводятся к https://.
    """
    value = str(raw).strip()
    lowered = value.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        raise TemplateCompileError(f"Схема ссылки запрещена: {value!r}.")
    if lowered.startswith(("http://", "https://")):
        return value
    normalized = normalize_url(value)
    if normalized is None:
        raise TemplateCompileError(f"Некорректный адрес гиперссылки: {value!r}.")
    return normalized


def render_template(
    tokens_by_section: Mapping[str, Sequence[Any]],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
) -> str:
    """
    Собирает итоговый HTML-анонс из уже связанных секций.

    Не выполняет повторного разбора: привязки и структура уже сохранены.
    Статический текст секций выводится буквально — сиротские литералы
    (`//`, разделители) не удаляются и не «чистятся».
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


# Техническая заглушка Telegram: пустое сообщение отправить нельзя.
PLACEHOLDER_TEXT = "."

# Лимиты Telegram на один анонс — по ВИДИМОМУ тексту после разбора
# разметки (S4 §5.1). Сырые HTML-теги символами сообщения не считаются.
ANNOUNCEMENT_TEXT_LIMIT = 4096
ANNOUNCEMENT_CAPTION_LIMIT = 1024


def visible_text_length(rendered_html: str) -> int:
    """
    Длина видимого текста в единицах Telegram (UTF-16 code units).

    Считается после разбора разметки: `visible_announcement_text` снимает
    теги и раскрывает HTML-сущности, поэтому `&amp;` — один символ, а не
    пять, и ограничение совпадает с фактическим сообщением.
    """
    return utf16_len(visible_announcement_text(rendered_html))


def announcement_text_limit(*, has_photos: bool) -> int:
    """Предел текста: без фото 4096, с фото (подпись) — 1024."""
    return ANNOUNCEMENT_CAPTION_LIMIT if has_photos else ANNOUNCEMENT_TEXT_LIMIT


@dataclass(frozen=True)
class AnnouncementLengthCheck:
    """
    Результат измерения готового текста (S4 §5.1).

    `length` — видимые символы после разбора entities; `limit` — предел
    Telegram для выбранного вида сообщения. Автосокращения нет: превышение
    показывается пользователю отдельным экраном, данные сохраняются.
    """

    length: int
    limit: int
    has_photos: bool

    @property
    def over(self) -> bool:
        return self.length > self.limit

    @property
    def excess(self) -> int:
        return max(0, self.length - self.limit)


def check_announcement_length(
    rendered_html: str, *, has_photos: bool
) -> AnnouncementLengthCheck:
    """Измеряет готовый анонс против лимита 4096/1024 без его изменения."""
    return AnnouncementLengthCheck(
        length=visible_text_length(rendered_html),
        limit=announcement_text_limit(has_photos=has_photos),
        has_photos=has_photos,
    )


def render_announcement(
    tokens_by_section: Mapping[str, Sequence[Any]],
    values_by_id: Mapping[int, Any],
    definitions_by_id: Mapping[int, BrickDefinition],
    *,
    has_photos: bool = False,
) -> str:
    """
    Единый рендер анонса для живого блока и итоговой отправки.

    Отличие от `render_template` — только техническая заглушка:

    - видимый текст непустой → рендер как есть (статика буквальна);
    - видимый текст пуст и фото нет → ровно ``.``;
    - видимый текст пуст, но есть фото → пустая строка: фото уходит без
      подписи, точка не нужна.

    Заглушка не является значением кирпичика: `values_by_id` не меняется,
    поэтому `.` не попадает в `announcement_values` и исчезает при правке.
    Анонс из одного литерального `//` непустой и заглушку не получает.
    """
    html = render_template(tokens_by_section, values_by_id, definitions_by_id)
    if visible_announcement_text(html):
        return html
    if has_photos:
        return ""
    return PLACEHOLDER_TEXT
