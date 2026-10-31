# utils/current_value.py
"""
Единый компонент «Текущее значение» (Poster 2.3 H3, требование D).

Раньше каждый экран показывал сохранённое значение по-своему: карточки
кирпичиков оборачивали его в `<code>`, шаблоны резали по 1200 символов
«для красоты», места сокращали адрес до 1500 символов. Автор не мог
отличить значение от служебной подписи и не видел полный текст.

Здесь один визуальный блок:

    Текущее значение:
    ────────────
    <сохранённое значение>
    ────────────

Правила:

- значение уже прошло allow-list (`SafeHtml`/`escape_html`) и вставляется
  БЕЗ повторного экранирования: форматирование сохраняется;
- `<code>` вокруг значения не добавляется: это подпись, а не технический
  фрагмент;
- значение не обрезается «для красоты». Единственное ограничение —
  физический предел сообщения Telegram (4096 символов): тогда показывается
  начало, валидное по разметке, и ЯВНАЯ пометка, что значение сохранено
  полностью. Молчаливой потери нет.

Модуль не знает бизнес-логики: на вход приходят готовая подпись и
отформатированное значение, на выходе — строка для parse_mode=HTML.
"""

from __future__ import annotations

from collections.abc import Callable

from utils.tg_html import escape_html, split_html, utf16_len
from utils.ui_texts import tr

# Разделитель значения: визуальная рамка без разметки Telegram, поэтому
# его вид одинаков в любом клиенте и не занимает заметную часть лимита.
SEPARATOR = "────────────"

# Предел сырого HTML значения на одном экране. Ниже лимита Telegram (4096)
# намеренно: вокруг значения ещё подпись, разделители и подсказка. Это не
# косметическое сокращение, а защита от отказа Telegram отправить экран.
VALUE_LIMIT = 3600

# Физический предел одного сообщения Telegram: длиннее экран не отправится
# вовсе, поэтому бюджет значений считается от него.
SCREEN_LIMIT = 4096

# Запас на неточность оценки обвязки (заголовок со разметкой, лишний перевод
# строки): лучше показать на пару символов меньше, чем не отправить экран.
SCREEN_SAFETY = 64

# Рамка одного блока значения: подпись, две линии-разделителя и переводы
# строк. Считается в бюджете даже у самого значения.
BLOCK_CHROME = 96

# Пометка о сокращении встаёт в каждый сокращённый блок; её место тоже
# резервируется заранее, иначе два «полных» значения с пометками выносят
# экран за предел Telegram.
NOTE_RESERVE = 128

# Меньше этого значение не сокращаем: несколько символов пользы не несут.
MIN_VALUE_LIMIT = 200

# Пометка о том, что показано только начало значения. Полное значение
# остаётся в базе и в анонсе — об этом сказано прямо.
TRUNCATED_NOTE = tr("screen.module.pokazano_nachalo_znacheniya_ekran")

EMPTY = tr("templates.value_block.pusto")


def _clip_safe(value: str, limit: int) -> tuple[str, bool]:
    """
    Первая валидная по разметке часть значения и признак сокращения.

    `split_html` не рвёт теги и ссылки-сущности и закрывает открытые теги,
    поэтому часть остаётся корректным HTML. Короткое значение возвращается
    как есть — ни один символ не теряется.
    """
    if utf16_len(value) <= limit:
        return value, False
    chunks = split_html(value, limit)
    return (chunks[0] if chunks else ""), True


def current_value_block(
    label: str,
    value_html: str,
    *,
    limit: int = VALUE_LIMIT,
) -> str:
    """
    Блок «подпись + разделители + значение» для экрана.

    `value_html` — уже безопасный HTML (allow-list или `escape_html`);
    повторного экранирования нет, иначе автор увидел бы `&lt;b&gt;`.

    `limit <= 0` — «места на экране нет»: блок всё равно рисуется, но
    значение заменяется отметкой `EMPTY`. Так бюджет экрана измеряет
    обвязку без содержимого значений.
    """
    visible = value_html if value_html.strip() else EMPTY
    if limit <= 0:
        shown, truncated = EMPTY, False
    else:
        shown, truncated = _clip_safe(visible, int(limit))
    lines = [f"{escape_html(label)}:", SEPARATOR, shown, SEPARATOR]
    if truncated:
        lines.append(TRUNCATED_NOTE)
    return "\n".join(lines)


def current_value_text(
    label: str,
    value: object,
    *,
    limit: int = VALUE_LIMIT,
) -> str:
    """То же для простого текста: значение экранируется один раз здесь."""
    text = "" if value is None else str(value)
    return current_value_block(label, escape_html(text), limit=limit)


def clip_parts(value_html: str, *, limit: int) -> tuple[str, bool]:
    """
    Безопасный HTML значения, его начало и признак сокращения.

    Нужно там, где пометка о сокращении стоит отдельно от значения (заголовок
    экрана, строка списка), а не внутри блока. `limit <= 0` — блок без
    содержимого: возвращается `EMPTY` и `False`.
    """
    if limit <= 0:
        return EMPTY, False
    return _clip_safe(value_html if value_html.strip() else EMPTY, int(limit))


def clip_html(value_html: str, *, limit: int = VALUE_LIMIT) -> str:
    """
    Безопасный HTML значения, сокращённый до `limit`, с явной пометкой.

    Значение уже прошло allow-list (или `escape_html`), поэтому повторного
    экранирования нет. Если оно уложилось — возвращается как есть; иначе
    начало, валидное по разметке, и `TRUNCATED_NOTE`.
    """
    shown, truncated = clip_parts(value_html, limit=limit)
    if not truncated:
        return shown
    return f"{shown}\n{TRUNCATED_NOTE}"


def clipped_text(value: object, *, limit: int = VALUE_LIMIT) -> str:
    """
    Экранированное значение без косметической обрезки и без `<code>`.

    Нужно там, где значение стоит внутри готовой строки («Адрес: …»), а не
    отдельным блоком: рамка-разделитель разорвала бы фразу. Ограничение
    остаётся только физическим (лимит Telegram), и о нём сказано явно.
    """
    text = "" if value is None else str(value)
    return clip_html(escape_html(text), limit=limit)


def value_limits(
    screen_text: str,
    blocks: int,
    *,
    screen_limit: int = SCREEN_LIMIT,
    min_limit: int = MIN_VALUE_LIMIT,
) -> list[int]:
    """
    Бюджет каждого из `blocks` значений на экране с обвязкой `screen_text`.

    Обвязка — всё, что стоит на экране помимо значений: заголовок, подписи
    полей, подсказки, привязки шаблона. Значения делят остаток лимита
    Telegram поровну, с запасом на пометку о сокращении в каждом блоке и на
    собственную рамку блока. Иначе два «полных» значения по отдельности
    корректны, а вместе выносят сообщение за 4096, и Telegram отклоняет
    экран целиком.

    Возвращается список одинаковых лимитов: он же используется для
    измерения обвязки (значения заменяются отметкой «пусто»).
    """
    count = max(int(blocks), 1)
    headroom = (
        screen_limit
        - SCREEN_SAFETY
        - utf16_len(str(screen_text))
        - count * (BLOCK_CHROME + NOTE_RESERVE)
    )
    per_value = max(headroom // count, int(min_limit))
    return [per_value] * count


def fit_screen(
    build: Callable[[list[int]], str],
    blocks: int,
    *,
    screen_limit: int = SCREEN_LIMIT,
    min_limit: int = MIN_VALUE_LIMIT,
) -> str:
    """
    Экран с бюджетом для `blocks` значений: сначала обвязка, потом значения.

    `build(limits)` собирает текст экрана, подставляя `limits[i]` в i-е
    значение (`limit <= 0` — измерение: значение заменяется отметкой
    «пусто»). Сначала измеряется обвязка (заголовок, подписи, подсказки),
    затем остаток лимита делится между значениями.

    Последняя страховка: если обвязка сама огромна (например, имя места или
    название кирпичика в заголовке), экран всё равно укладывается в лимит
    Telegram — конец сокращается с той же явной пометкой.
    """
    count = max(int(blocks), 1)
    limits = value_limits(
        build([0] * count),
        count,
        screen_limit=screen_limit,
        min_limit=min_limit,
    )
    return clip_screen(build(limits), limit=screen_limit)


def clip_screen(text: str, *, limit: int = SCREEN_LIMIT) -> str:
    """
    Гарантирует, что текст экрана уложится в лимит Telegram.

    Нужен как страховка поверх бюджета значений: заголовок с огромным
    названием не должен приводить к отказу Telegram отправить экран.
    Сокращённый вариант остаётся валидным HTML и получает явную пометку,
    что показано только начало; данные в базе не меняются.
    """
    if limit <= 0:
        return ""
    if utf16_len(text) <= limit:
        return text
    note = "\n" + TRUNCATED_NOTE
    budget = limit - utf16_len(note)
    if budget <= 0:
        return split_html(TRUNCATED_NOTE, limit)[0]
    chunks = split_html(text, budget)
    return f"{chunks[0] if chunks else ''}{note}"


__all__ = [
    "BLOCK_CHROME",
    "EMPTY",
    "MIN_VALUE_LIMIT",
    "NOTE_RESERVE",
    "SCREEN_LIMIT",
    "SCREEN_SAFETY",
    "SEPARATOR",
    "TRUNCATED_NOTE",
    "VALUE_LIMIT",
    "clip_html",
    "clip_parts",
    "clip_screen",
    "clipped_text",
    "current_value_block",
    "current_value_text",
    "fit_screen",
    "value_limits",
]
