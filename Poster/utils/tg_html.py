# utils/tg_html.py
"""
Безопасный HTML: форматирование Telegram (MessageEntity) → HTML и разбиение.

Внутреннее представление форматируемых полей поста — безопасный HTML:
обычный текст экранируется, разметка исходного сообщения воспроизводится
стандартными тегами Telegram. Один и тот же HTML уходит в превью, в готовый
пост и хранится в черновике (колонка Draft.formats) — двойного экранирования
нет, превью дословно совпадает с финальной отправкой.

Смещения MessageEntity заданы в 16-битных код-юнитах UTF-16 (как в
Telegram), поэтому разбор ведётся по utf-16-байтам — так же, как в
`Message._parse_html` python-telegram-bot, — а не по символам Python:
эмодзи и прочие широкие символы не сдвигают границы форматирования.

Разница с `Message._parse_html` PTB (сознательная): значения атрибутов
(href, emoji-id, language) экранируются — иначе кавычка в ссылке ломает
всю разметку сообщения; spoilter и pre+language пишутся в документированном
Telegram виде.
"""

from collections.abc import Sequence
from html import escape as _escape

from telegram import MessageEntity


def escape_html(text: str) -> str:
    """
    HTML-экранирование текста и значений атрибутов: &, <, >, ", '.

    Один экранировщик для текстовых узлов и атрибутов: значение в кавычках
    обязано быть экранировано, а Telegram одинаково разворачивает сущности
    и в тексте, и в атрибутах.
    """
    return _escape(text, quote=True)


def utf16_len(text: str) -> int:
    """Длина строки в 16-битных код-юнитах UTF-16 — единицах смещений Telegram."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def entities_to_html(text: str, entities: Sequence[MessageEntity] | None) -> str:
    """
    Текст сообщения с entities → безопасный HTML.

    Без entities — просто экранированный текст. Entities с битыми смещениями
    (нулевая длина, выход за конец текста) игнорируются: текст остаётся
    как есть, содержимое не теряется.
    """
    if not text or not entities:
        return escape_html(text)

    utf16_text = text.encode("utf-16-le", "surrogatepass")
    total_units = len(utf16_text) // 2
    sliced: dict[MessageEntity, str] = {}
    for entity in entities:
        if entity.length <= 0 or entity.offset < 0:
            continue
        if entity.offset + entity.length > total_units:
            continue
        start = entity.offset * 2
        sliced[entity] = utf16_text[start : start + entity.length * 2].decode(
            "utf-16-le"
        )

    if not sliced:
        return escape_html(text)
    return _parse_html(text, sliced)


def _parse_html(
    message_text: str,
    entities: dict[MessageEntity, str],
    *,
    offset: int = 0,
) -> str:
    """
    Рекурсивная сборка HTML: сортировка по смещению, вложенность entities,
    срезы по UTF-16 — алгоритм PTB `Message._parse_html`, но со
    экранированием значений атрибутов.
    """
    utf16_text = message_text.encode("utf-16-le", "surrogatepass")
    html_text = ""
    last_offset = 0
    sorted_entities = sorted(entities.items(), key=lambda item: item[0].offset)
    parsed: list[MessageEntity] = []

    for entity, entity_text in sorted_entities:
        if entity in parsed:
            continue
        nested = {
            nested_entity: nested_text
            for nested_entity, nested_text in sorted_entities
            if nested_entity.offset >= entity.offset
            and nested_entity.offset + nested_entity.length
            <= entity.offset + entity.length
            and nested_entity != entity
        }
        parsed.extend(nested.keys())

        if nested:
            inner = _parse_html(entity_text, nested, offset=entity.offset)
        else:
            inner = escape_html(entity_text)

        # Текст между предыдущей entities и текущей тоже экранируем
        html_text += escape_html(
            utf16_text[last_offset * 2 : (entity.offset - offset) * 2].decode(
                "utf-16-le"
            )
        )
        html_text += _wrap(entity, inner, entity_text)
        last_offset = entity.offset - offset + entity.length

    html_text += escape_html(utf16_text[last_offset * 2 :].decode("utf-16-le"))
    return html_text


def _wrap(entity: MessageEntity, inner: str, raw_text: str) -> str:
    """
    Тег для конкретного типа entity. raw_text — исходный текст entity
    (нужен URL как href). Вспомогательные типы без разметки Telegram
    (mention, hashtag, email, date_time, ...) возвращают просто текст —
    содержимое сохраняется.
    """
    kind = entity.type
    if kind == MessageEntity.BOLD:
        return f"<b>{inner}</b>"
    if kind == MessageEntity.ITALIC:
        return f"<i>{inner}</i>"
    if kind == MessageEntity.UNDERLINE:
        return f"<u>{inner}</u>"
    if kind == MessageEntity.STRIKETHROUGH:
        return f"<s>{inner}</s>"
    if kind == MessageEntity.SPOILER:
        return f"<tg-spoiler>{inner}</tg-spoiler>"
    if kind == MessageEntity.CODE:
        return f"<code>{inner}</code>"
    if kind == MessageEntity.PRE:
        if entity.language:
            # Документированный Telegram вид pre с языком
            language = escape_html(entity.language)
            return f'<pre><code class="language-{language}">' f"{inner}</code></pre>"
        return f"<pre>{inner}</pre>"
    if kind == MessageEntity.TEXT_LINK and entity.url:
        return f'<a href="{escape_html(entity.url)}">{inner}</a>'
    if kind == MessageEntity.URL:
        # Явная ссылка вместо надежды на автоссылку: так URL остаётся
        # кликабельным независимо от того, как клиент разберёт HTML
        return f'<a href="{escape_html(raw_text)}">{inner}</a>'
    if kind == MessageEntity.TEXT_MENTION and entity.user:
        return f'<a href="tg://user?id={entity.user.id}">{inner}</a>'
    if kind == MessageEntity.BLOCKQUOTE:
        return f"<blockquote>{inner}</blockquote>"
    if kind == MessageEntity.EXPANDABLE_BLOCKQUOTE:
        return f"<blockquote expandable>{inner}</blockquote>"
    if kind == MessageEntity.CUSTOM_EMOJI and entity.custom_emoji_id:
        emoji_id = escape_html(str(entity.custom_emoji_id))
        return f'<tg-emoji emoji-id="{emoji_id}">{inner}</tg-emoji>'
    return inner


# —— Разбиение длинного HTML на части ————————————————————————————————————

# Максимум сущности-ссылки вида "&...;" (наши экранировки не длиннее 7):
# дальше точку с запятой считаем обычным текстом, чтобы не «съедать»
# половины абзаца при неэкранированном '&'.
_MAX_ENTITY_SPAN = 12


def split_html(text: str, limit: int) -> list[str]:
    """
    Разбивает HTML на части длиной не более limit (код-юнитов UTF-16).

    Гарантии:
    — теги и ссылки-сущности (&...;) не разрываются никогда: разрез идёт
      только между ними и внутри обычного текста (эмодзи не делятся);
    — теги, открытые на границе, закрываются в конце части и переоткрываются
      в начале следующей — каждая часть остаётся валидным HTML;
    — видимый текст не теряется и не дублируется;
    — граница предпочитается сразу после '\\n'; если переводов строки нет,
      части наполняются до предела по символам.

    Лимит считается по «сырому» HTML: он не короче видимого текста, поэтому
    части укладываются в лимит и после разбора Telegram. Редкий случай
    «один токен больше лимита» (гипердлинный атрибут) не лечится — такой
    токен уходит целиком.
    """
    if limit <= 0:
        raise ValueError("Лимит части должен быть положительным")
    if utf16_len(text) <= limit:
        return [text]

    tokens = _tokenize(text)
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    # Открытые теги: (имя, полный открывающий тег) — полный тег нужен,
    # чтобы переоткрыть тег в следующей части вместе с атрибутами
    stack: list[tuple[str, str]] = []
    # Удобные границы разреза после '\\n': (позиция в current,
    # длина по этой позиции, стек открытых тегов в этот момент)
    marks: list[tuple[int, int, list[tuple[str, str]]]] = []

    def fits(token: str, new_stack: list[tuple[str, str]]) -> bool:
        return current_len + utf16_len(token) + _closes_len(new_stack) <= limit

    def flush() -> bool:
        """
        Выгружает заполненную часть, предпочитая границу после '\\n'.

        Возвращает True при срезе по границе (содержимое частично остаётся
        в новой части) и False при полной выгрузке. Стек не переносится и не
        сбрасывается: он описывает состояние ВХОДА на точке разреза, а
        переоткрытые теги новой части воспроизводят состояние границы.
        """
        nonlocal current, current_len, marks
        for position, length_at, snapshot in reversed(marks):
            if position <= 0:
                continue
            reopened = [tag for _, tag in snapshot]
            reopened_len = sum(utf16_len(tag) for tag in reopened)
            rest_len = current_len - length_at
            # Часть 1 (префикс + закрытие тегов, открытых на границе)
            # влезает всегда — инвариант выполнялся в момент появления
            # границы. Часть 2 должна уложиться вместе с закрытиями стека.
            if reopened_len + rest_len + _closes_len(stack) <= limit:
                chunks.append("".join(current[:position]) + _closes(snapshot))
                current = reopened + current[position:]
                current_len = reopened_len + rest_len
                marks = [
                    (
                        len(reopened) + (pos - position),
                        reopened_len + (ln - length_at),
                        snap,
                    )
                    for (pos, ln, snap) in marks
                    if pos > position
                ]
                return True
        # Свободной границы нет: выгружаем всё, а открытые теги переоткрываем
        # в новой части — во входе впереди ещё есть их закрывающие теги
        chunks.append("".join(current) + _closes(stack))
        reopened = [tag for _, tag in stack]
        current = reopened
        current_len = sum(utf16_len(tag) for tag in reopened)
        marks = []
        return False

    for token in tokens:
        while current and not fits(token, _after(stack, token)):
            if not flush():
                # Всё выгружено (с переоткрытием): токен встаёт в новую
                # часть — повторная выгрузка дала бы пустые теги-пары
                break
        stack = _after(stack, token)
        current.append(token)
        current_len += utf16_len(token)
        if token == "\n":
            marks.append((len(current), current_len, list(stack)))

    if current:
        chunks.append("".join(current) + _closes(stack))
    return chunks


def _tokenize(text: str) -> list[str]:
    """
    Разбивает HTML на токены: теги, ссылки-сущности (&...;) и одиночные
    символы. Токен не разрывается при сплите никогда.
    """
    tokens: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "<":
            end = _scan_tag(text, index)
            if end is not None:
                tokens.append(text[index:end])
                index = end
                continue
            # Одиночный '<' (в нашем HTML не встречается — текст экранирован)
            tokens.append(char)
            index += 1
        elif char == "&":
            semi = text.find(";", index + 1)
            if semi != -1 and semi - index <= _MAX_ENTITY_SPAN:
                tokens.append(text[index : semi + 1])
                index = semi + 1
            else:
                tokens.append(char)
                index += 1
        else:
            tokens.append(char)
            index += 1
    return tokens


def _scan_tag(text: str, start: int) -> int | None:
    """Индекс символа после '>', если тег корректен (кавычки атрибутов учтены)."""
    quote = ""
    index = start + 1
    while index < len(text):
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == ">":
            return index + 1
        index += 1
    return None


def _after(stack: list[tuple[str, str]], token: str) -> list[tuple[str, str]]:
    """Стек открытых тегов после применения токена (текст тег не меняет)."""
    if not (token.startswith("<") and token.endswith(">")):
        return stack
    if token.startswith("</"):
        return _pop(stack, token[2:-1].strip())
    if token.startswith("<!") or token.endswith("/>"):
        return stack
    inner = token[1:-1].strip()
    if not inner:
        return stack
    return stack + [(inner.split()[0], token)]


def _pop(stack: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    """Закрывает тег с данным именем (и всё, что в нём осталось открытым)."""
    for index in range(len(stack) - 1, -1, -1):
        if stack[index][0] == name:
            return stack[:index]
    return stack  # лишний закрывающий тег — игнорируем


def _closes(stack: list[tuple[str, str]]) -> str:
    """Закрывающие теги стека в порядке закрытия."""
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _closes_len(stack: list[tuple[str, str]]) -> int:
    """Длина _closes в код-юнитах UTF-16."""
    return sum(utf16_len(f"</{name}>") for name, _ in reversed(stack))
