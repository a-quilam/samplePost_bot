# template_parser.py
"""
Stage 24: парсер шаблонов анонсов.

Разбирает три текстовые секции шаблона в независимое от Telegram и SQLite
дерево токенов. Здесь нет обращений к базе данных, пользователей и сетевых
запросов: определения кирпичиков и значения передаются внутрь явно.

Грамматика (уровень лексера/парсера):

- `[код]` — токен кирпичика: код заменяется на постоянный brick_id
  конкретной версии;
- `[[` и `]]` — экранированные буквальные скобки (иначе `[` всегда
  начинает кирпичик или ссылку);
- `ТЕКСТ(URL)` или `[место]([ссылка_места])` — гиперссылка; видимый текст
  идёт до `(`, адрес — внутри; скобки и служебные обозначения в готовом
  сообщении не выводятся;
- `**жирный**`, `*курсив*`, `__подчёркивание__`, `~~зачёркнутый~~` —
  Telegram-форматирование (с поддержкой вложенности);
- прочие символы, включая переводы строк и Unicode, сохраняются дословно.

Неизвестные конструкции (в том числе `[неизвестный]` и `[место](текст)`)
не заменяются пустой строкой: парсер выдаёт `TemplateParseError` с секцией,
позицией и причиной. Форматирование выводится в виде `MessageEntity`
Telegram со смещениями в 16-битных код-юнитах UTF-16, как требует Telegram.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape as _escape

BRICK_PATTERN = re.compile(r"\[(?P<code>[^\[\]\(\)]+)\]")
LINK_URL_PATTERN = re.compile(r"\((?P<url>[^()]+)\)")
FORMAT_PATTERN = re.compile(r"(?P<marker>\*\*|~~|__|\*)")

ALLOWED_URL_SCHEMES = ("http", "https")


class TemplateParseError(ValueError):
    """Синтаксическая ошибка шаблона с понятной локализацией."""

    def __init__(
        self,
        message: str,
        *,
        section: str | None = None,
        offset: int | None = None,
        code: str | None = None,
    ) -> None:
        self.section = section
        self.offset = offset
        self.code = code
        suffix = ""
        if section is not None:
            suffix += f" (секция {section})"
        if offset is not None:
            suffix += f", позиция {offset + 1}"
        if code:
            suffix += f", кирпичик «{code}»"
        super().__init__(f"{message}{suffix}")


@dataclass(frozen=True)
class TextNode:
    """Обычный текст, уже очищенный от разметки; без экранирования HTML."""

    text: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"Text({self.text!r})"


@dataclass
class BrickNode:
    """Токен кирпичика: код используется при привязке, ID — при исполнении."""

    code: str
    brick_id: int | None = None
    expected_type: str | None = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"Brick({self.code!r}, id={self.brick_id})"


@dataclass(frozen=True)
class LinkNode:
    """
    Гиперссылка: текст (`label`) и URL (`url`).

    `url` может быть готовым абсолютным адресом (`https://…`) либо вложенным
    токеном кирпичика. Внутри скобок `( )` допустим ровно один кирпичик
    либо строка URL.
    """

    label: tuple["TemplateToken", ...]
    url: str | BrickNode
    url_brick: BrickNode | None = None
    normalized_url: str | None = None

    def __repr__(self) -> str:  # pragma: no cover
        return f"Link(label={self.label!r}, url={self.url!r})"


@dataclass(frozen=True)
class FormatNode:
    """Форматирование Telegram: marker задаёт тип, дети — вложенные узлы."""

    marker: str
    children: tuple["TemplateToken", ...] = field(default_factory=tuple)

    def __repr__(self) -> str:  # pragma: no cover
        return f"Format({self.marker!r}, children={len(self.children)})"


TemplateToken = TextNode | BrickNode | LinkNode | FormatNode


def parse_template(text: str, *, section: str | None = None) -> list[TemplateToken]:
    """
    Разбирает текст одной секции в последовательность узлов.

    Бросает `TemplateParseError` при неизвестных или незакрытых конструкциях;
    частично корректное дерево не возвращается.
    """
    if not isinstance(text, str):
        raise TemplateParseError("Текст секции должен быть строкой.", section=section)
    tokens = _lex(text, section=section)
    return _apply_formatting(tokens, section=section)


ESCAPE_MAP = {
    "*": "*",
    "_": "_",
    "~": "~",
    "(": "(",
    ")": ")",
    "\\": "\\",
}


def _lex(text: str, *, section: str | None) -> list[LexerToken]:
    """
    Лексер: превращает сырой текст в плоский список узлов и маркеров.

    Экранирование: `\\*`, `\\_`, `\\~`, `\\(`, `\\)`, `\\\\` дают литеральные
    символы; `[[` и `]]` дают буквальные квадратные скобки. Форматирование
    (`**`, `*`, `__`, `~~`) здесь выделяется, но ещё не сворачивается —
    вложенность решает `_apply_formatting`.
    """
    tokens: list[LexerToken] = []
    buffer: list[str] = []
    offset = 0
    length = len(text)

    def flush() -> None:
        if buffer:
            tokens.append(TextNode("".join(buffer)))
            buffer.clear()

    while offset < length:
        char = text[offset]

        # Экранирование обратным слэшем: литеральный спецсимвол.
        if char == "\\" and offset + 1 < length:
            escaped = text[offset + 1]
            if escaped in ESCAPE_MAP:
                buffer.append(ESCAPE_MAP[escaped])
                offset += 2
                continue
            buffer.append(char)
            offset += 1
            continue

        # `[[` и `]]` — экранированные квадратные скобки.
        if char == "[" and offset + 1 < length and text[offset + 1] == "[":
            buffer.append("[")
            offset += 2
            continue
        if char == "]" and offset + 1 < length and text[offset + 1] == "]":
            buffer.append("]")
            offset += 2
            continue

        # Маркеры форматирования (длинные раньше коротких: `**` раньше `*`).
        matched = False
        for marker in ("**", "__", "~~", "*"):
            if text.startswith(marker, offset):
                flush()
                tokens.append(FormatMarker(marker=marker))
                offset += len(marker)
                matched = True
                break
        if matched:
            continue

        # Кирпичик `[код]`.
        if char == "[":
            match = BRICK_PATTERN.match(text, offset)
            if match is None:
                raise TemplateParseError(
                    "Незакрытая квадратная скобка «[» без кирпичика.",
                    section=section,
                    offset=offset,
                )
            flush()
            tokens.append(BrickNode(code=match.group("code")))
            offset = match.end()
            continue

        # Начало гиперссылки: `(...)` сразу после слова/кирпичика.
        if char == "(" and (
            (buffer and not buffer[-1].isspace())
            or (tokens and isinstance(tokens[-1], (BrickNode, FormatNode)))
        ):
            end = _find_close_paren(text, offset)
            if end is None:
                # Нет закрывающей скобки — это просто текст со скобкой.
                buffer.append(char)
                offset += 1
                continue
            inner = text[offset + 1 : end]
            flush()
            # Подпись-кирпичик явно указывает на намерение сделать ссылку:
            # невалидный адрес для неё — ошибка, а не проза.
            is_brick_label = bool(tokens) and isinstance(tokens[-1], BrickNode)
            try:
                url_token = _parse_link_url(inner, section=section, offset=offset)
            except TemplateParseError:
                # Опасные схемы всегда отклоняются, независимо от подписи.
                if _has_dangerous_scheme(inner) or is_brick_label:
                    raise
                # Двоеточие подразумевает попытку схемы URL: это не проза,
                # а невалидный адрес — ошибка выходит наружу.
                if ":" in inner:
                    raise
                # Обычный текст в скобках после слова — проза, а не ссылка.
                buffer.append(text[offset : end + 1])
                offset = end + 1
                continue
            label = _pop_link_label(tokens)
            tokens.append(LinkNode(label=label, url=url_token, normalized_url=None))
            offset = end + 1
            continue

        buffer.append(char)
        offset += 1

    flush()
    return tokens


_DANGEROUS_SCHEMES = ("javascript:", "data:", "file:", "vbscript:")


def _has_dangerous_scheme(content: str) -> bool:
    """Проверяет, начинается ли содержимое скобок с опасной схемы URL."""
    return content.strip().lower().startswith(_DANGEROUS_SCHEMES)


def _find_close_paren(text: str, start: int) -> int | None:
    """Ищет закрывающую «)» с учётом экранирования «\\)»."""
    offset = start + 1
    while offset < len(text):
        char = text[offset]
        if char == "\\" and offset + 1 < len(text):
            offset += 2
            continue
        if char == ")":
            return offset
        offset += 1
    return None


def _pop_link_label(tokens: list[LexerToken]) -> tuple[TemplateToken, ...]:
    """
    Забирает последний узел как подпись ссылки.

    Дефисы в литеральной подписи заменяются пробелами (`Наше-уютное-место`
    → «Наше уютное место»), но только в этой конструкции; дефисы в обычном
    тексте и значениях кирпичиков не трогаются. Пробелы перед «(» не
    распознаются как ссылка.
    """
    if not tokens:
        return ()
    last = tokens[-1]
    if isinstance(last, TextNode):
        if last.text.endswith((" ", "\n", "\t")):
            return ()
        label = TextNode(last.text.replace("-", " "))
        tokens.pop()
        return (label,)
    if isinstance(last, BrickNode):
        tokens.pop()
        return (last,)
    return ()


def _parse_link_url(inner: str, *, section: str | None, offset: int) -> str | BrickNode:
    """
    Разбирает содержимое `(...)` — адрес гиперссылки.

    Допустимо либо ровно один кирпичик `[код]`, либо литеральный URL
    (с нормализацией `t.me/...` и `example.com` до `https://...`).
    """
    stripped = inner.strip()
    if not stripped:
        raise TemplateParseError(
            "Пустой адрес внутри «( )».",
            section=section,
            offset=offset,
        )
    brick = BRICK_PATTERN.fullmatch(stripped)
    if brick is not None:
        return BrickNode(code=brick.group("code"))
    if "[" in stripped or "]" in stripped:
        raise TemplateParseError(
            "Некорректный адрес ссылки: обрывочная квадратная скобка.",
            section=section,
            offset=offset,
        )
    return _normalize_url(stripped, section=section, offset=offset)


def _normalize_url(raw: str, *, section: str | None, offset: int) -> str:
    """
    Нормализует адрес: схемы `t.me`, домены без `://` превращаются в HTTPS.

    Опасные схемы (`javascript:`, `data:`, `file:`) отклоняются.
    Путь, query и фрагмент сохраняются дословно.
    """
    value = raw.strip()
    if not value:
        raise TemplateParseError(
            "Пустой адрес гиперссылки.", section=section, offset=offset
        )
    lowered = value.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:")):
        raise TemplateParseError(
            "Схема ссылки запрещена.",
            section=section,
            offset=offset,
        )
    if lowered.startswith(("http://", "https://")):
        return value
    if lowered.startswith("t.me/") or lowered.startswith("tg://resolve?"):
        return f"https://{value}" if not lowered.startswith("tg://") else value
    # Домен без схемы — подставляем https, не добавляем www.
    if re.match(r"^[a-z0-9\-]+(\.[a-z0-9\-]+)+", lowered) or lowered.startswith("www."):
        return f"https://{value}"
    raise TemplateParseError(
        f"Некорректный URL ссылки: {value!r}.",
        section=section,
        offset=offset,
    )


def _apply_formatting(
    tokens: list[LexerToken], *, section: str | None
) -> list[TemplateToken]:
    """
    Сворачивает маркеры `**…**`, `*…*`, `__…__`, `~~…~~` в FormatNode.

    Реализация: ищем первую пару одинаковых маркеров; дети рекурсивно
    обрабатываются тем же алгоритмом, что даёт вложенность (`**жирный
    с *курсивом***`). Незакрытый маркер остаётся Literal-текстом —
    парсер не молча удаляет неизвестные конструкции.
    """
    markers = ("**", "__", "~~", "*")
    for marker in markers:
        while True:
            occurrences = [
                i
                for i, t in enumerate(tokens)
                if isinstance(t, FormatMarker) and t.marker == marker
            ]
            if len(occurrences) < 2:
                break
            start, end = occurrences[0], occurrences[1]

            # Проверяем, есть ли сразу после закрывающего маркера ещё один
            # маркер другого вида, который мог бы закрыть незакрытый маркер
            # внутри текущего содержимого. Пример: `**жирный *курсивом***`
            # разбирается как `**` + [жирный, `*`, курсивом] + `**` + `*`.
            # Завершающий `*` закрывает курсив внутри жирного.
            trailing_index: int | None = None
            if end + 1 < len(tokens):
                next_token = tokens[end + 1]
                if isinstance(next_token, FormatMarker) and next_token.marker != marker:
                    inner_matches = [
                        t
                        for t in tokens[start + 1 : end]
                        if isinstance(t, FormatMarker) and t.marker == next_token.marker
                    ]
                    if len(inner_matches) == 1:
                        trailing_index = end + 1

            if trailing_index is not None:
                # Присоединяем завершающий маркер к внутреннему содержимому,
                # чтобы рекурсия закрыла вложенный маркер.
                inner = tokens[start + 1 : end] + [tokens[trailing_index]]
                node = FormatNode(
                    marker=marker,
                    children=tuple(_apply_formatting(inner, section=section)),
                )
                tokens = tokens[:start] + [node] + tokens[trailing_index + 1 :]
                continue
            inner = tokens[start + 1 : end]
            node = FormatNode(
                marker=marker, children=tuple(_apply_formatting(inner, section=section))
            )
            tokens = tokens[:start] + [node] + tokens[end + 1 :]
    # Незакрытые маркеры остаются литеральным текстом: `*`, `__` и т.д.
    return [TextNode(t.marker) if isinstance(t, FormatMarker) else t for t in tokens]


@dataclass(frozen=True)
class FormatMarker:
    """Разделённый маркер форматирования: `**`, `*`, `__` или `~~`."""

    marker: str

    def __repr__(self) -> str:  # pragma: no cover
        return f"Marker({self.marker!r})"


# Токены лексера: готовые узлы плюс промежуточные маркеры форматирования.
LexerToken = TemplateToken | FormatMarker


def render_to_plain_text(tokens: list[TemplateToken]) -> str:
    """Собирает видимый текст готового анонса без HTML и entities."""
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, TextNode):
            parts.append(token.text)
        elif isinstance(token, BrickNode):
            parts.append(f"[{token.code}]")  # до привязки — исходный токен
        elif isinstance(token, LinkNode):
            parts.append("".join(render_to_plain_text(list(token.label))))
        elif isinstance(token, FormatNode):
            parts.append("".join(render_to_plain_text(list(token.children))))
    return "".join(parts)


def render_to_html(tokens: list[TemplateToken]) -> str:
    """
    Сериализует дерево в безопасный HTML Telegram.

    Кириллица, эмодзи и символы за пределами BMP сохраняются дословно;
    HTML-специальные символы экранируются один раз.
    """
    parts: list[str] = []
    for token in tokens:
        if isinstance(token, TextNode):
            parts.append(_escape(token.text, quote=True))
        elif isinstance(token, BrickNode):
            parts.append(_escape(f"[{token.code}]", quote=True))
        elif isinstance(token, LinkNode):
            label = "".join(render_to_html(list(token.label)))
            url = token.normalized_url
            if url is None:
                if isinstance(token.url, str):
                    url = token.url
                elif isinstance(token.url, BrickNode):
                    # До привязки показываем исходный код кирпичика.
                    url = f"[{token.url.code}]"
                else:
                    url = ""
            parts.append(f'<a href="{_escape(url, quote=True)}">{label}</a>')
        elif isinstance(token, FormatNode):
            inner = "".join(render_to_html(list(token.children)))
            tag = _format_tag(token.marker)
            parts.append(f"<{tag}>{inner}</{tag}>")
    return "".join(parts)


def _format_tag(marker: str) -> str:
    return {
        "**": "b",
        "*": "i",
        "__": "u",
        "~~": "s",
    }.get(marker, "span")


def utf16_length(text: str) -> int:
    """Длина текста в код-юнитах UTF-16 — единицы смещений Telegram."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2
