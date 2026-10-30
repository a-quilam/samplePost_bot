# utils/keyboard_layouts.py
"""
Переиспользуемые раскладки inline-клавиатур (Poster 2.2 U3a, дефект №13).

Раньше каждый экран собирал `[[InlineKeyboardButton(...)]]` вручную, и в
нескольких местах одна и та же раскладка повторялась слово в слово. Здесь
собраны пять понятных шаблонов:

- `rows_of` — несколько кнопок в строке (быстрые варианты, дни календаря);
- `pair` — две кнопки рядом («◀️ Назад» и «⏭ Пропустить»);
- `list_rows` — список объектов: название слева, компактное действие справа;
- `nav_row` — пагинация «◀️ / ▶️» (или с подписями);
- `danger_row` — опасное/необратимое действие отдельной строкой.

Модуль не знает бизнес-логики: на вход приходят готовые подписи и
callback'и, на выходе — строки для `InlineKeyboardMarkup`. Пустые/`None`
кнопки пропускаются, поэтому шаблон безопасно вызывать с необязательными
элементами (например, когда пагинации нет).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from telegram import InlineKeyboardButton

Row = list[InlineKeyboardButton]


def button(label: str, callback_data: str) -> InlineKeyboardButton:
    """Кнопка с подписью и callback — короткая запись для остальных шаблонов."""
    return InlineKeyboardButton(str(label), callback_data=str(callback_data))


def _clean(row: Iterable[InlineKeyboardButton | None]) -> Row:
    """Убирает `None` из готовой строки: условные кнопки не дают пустых слотов."""
    return [item for item in row if item is not None]


def rows_of(
    items: Sequence[InlineKeyboardButton | None] | Iterable[Any],
    per_row: int,
) -> list[Row]:
    """
    Кнопки строками по `per_row` штук в каждой.

    Последняя строка может быть неполной — это нормально и ожидаемо
    (например, 8 быстрых дат при `per_row=4` дают ровно две полные строки,
    а 5 кнопок при `per_row=3` — строки по 3 и 2). `None` пропускается,
    поэтому условные кнопки не ломают расположение. `per_row <= 0`
    считается ошибкой: молчаливая «одна кнопка в строке» скрыла бы баг.
    """
    if per_row <= 0:
        raise ValueError("per_row должен быть положительным")
    clean = [item for item in items if item is not None]
    return [clean[start : start + per_row] for start in range(0, len(clean), per_row)]


def pair(
    left: InlineKeyboardButton | None,
    right: InlineKeyboardButton | None,
) -> list[Row]:
    """
    Две кнопки в ОДНОЙ строке: типично «◀️ Назад» и «⏭ Пропустить».

    Если одна из кнопок не нужна (`None`), строка остаётся с одной —
    пустых мест в клавиатуре не появляется. Если не нужны обе, строк нет
    вовсе: вызывающий код не обязан это проверять.
    """
    row = _clean((left, right))
    return [row] if row else []


def list_rows(
    entries: Iterable[tuple[InlineKeyboardButton | None, InlineKeyboardButton | None]],
) -> list[Row]:
    """
    Список объектов: «строка = название + компактное действие справа».

    `entries` — пары (основная кнопка, действие). Действие необязательно:
    для архивных или уже обработанных объектов справа ничего нет, и строка
    остаётся одной кнопкой. Пары с пустой основной кнопкой пропускаются
    целиком — строка без названия бессмысленна.
    """
    rows: list[Row] = []
    for main, action in entries:
        if main is None:
            continue
        rows.append(_clean((main, action)))
    return rows


def nav_row(
    prev: InlineKeyboardButton | None = None,
    next_: InlineKeyboardButton | None = None,
) -> list[Row]:
    """
    Пагинация: «◀️» и «▶️» рядом, если есть обе стороны.

    Один край списка — одна стрелка; пустая пагинация не добавляет строку
    вовсе, поэтому вызывающий код может смело делать `rows += nav_row(...)`.
    """
    row = _clean((prev, next_))
    return [row] if row else []


def danger_row(entry: InlineKeyboardButton | None) -> list[Row]:
    """
    Опасное/необратимое действие — отдельной строкой над остальными.

    Шаблон существует, чтобы такие кнопки («🗑 Отозвать», «✅ Да, в архив»)
    не оказывались в одной строке с безопасной навигацией: случайное
    нажатие рядом с «Назад» не должно удалять данные.
    """
    if entry is None:
        return []
    return [[entry]]


__all__ = [
    "Row",
    "button",
    "danger_row",
    "list_rows",
    "nav_row",
    "pair",
    "rows_of",
]
