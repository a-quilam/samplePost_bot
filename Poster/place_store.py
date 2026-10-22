# place_store.py
"""
Минимальный слой справочника мест (Stage 19A).

Здесь только то, что нужно фундаменту: нормализация названия и атомарное
создание места вместе с каноническим названием. Поиска, альтернативных
названий, администраторов и шаблонов на этом этапе нет.

Сознательное правило: похожие названия НЕ объединяются. Два пользователя
могут создать «Кофейня Дом» и «Кофейня «Дом»» как два разных места —
решение о слиянии/альтернативном названии принимается явно в будущем.
"""

from __future__ import annotations

import unicodedata

from sqlalchemy.orm import Session

from models import Place, PlaceName, utcnow


class PlaceNameError(ValueError):
    """Название места пустое или не содержит букв/цифр."""


def clean_display_name(name: str) -> str:
    """
    Отображаемое название: NFKC, trim, сжатые пробелы.

    Регистр, пунктуация и эмодзи сохраняются — это то, что увидит человек.
    """
    if not isinstance(name, str):
        raise PlaceNameError("Название места должно быть строкой")
    normalized = unicodedata.normalize("NFKC", name)
    return " ".join(normalized.split())


def normalize_place_name(name: str) -> str:
    """
    Нормализованное представление названия для сравнения и уникальности.

    NFKC → casefold (в т.ч. регистр кириллицы) → «ё» → «е» →
    пунктуация и пробельные символы становятся пробелами → пробелы
    сжимаются. Результат обязан быть непустым: название из одних кавычек
    или дефисов недопустимо.

    Пример: «Кофейня «Дом»» → «кофейня дом»; «Ёлка-Кафе» → «елка кафе».
    """
    display = clean_display_name(name)
    folded = display.casefold().replace("ё", "е")
    chars: list[str] = []
    for char in folded:
        if char.isspace() or unicodedata.category(char).startswith("P"):
            chars.append(" ")
        else:
            chars.append(char)
    normalized = " ".join("".join(chars).split())
    if not normalized:
        raise PlaceNameError(
            "Название места должно содержать хотя бы одну букву или цифру"
        )
    return normalized


def create_place(
    session: Session,
    canonical_name: str,
    *,
    created_by_user_id: int | None = None,
) -> Place:
    """
    Создаёт место и его каноническое название в одной транзакции.

    Возвращает сохранённый `Place` с заполненным `id`. При любой ошибке
    (включая нарушение внешнего ключа создателя) транзакция откатывается:
    место без названия или название без места не остаются.

    `created_by_user_id` — внутренний `users.id`, а не Telegram user ID.
    """
    display_name = clean_display_name(canonical_name)
    if not display_name:
        raise PlaceNameError("Название места не может быть пустым")
    name_search = normalize_place_name(display_name)

    now = utcnow()
    place = Place(created_by_user_id=created_by_user_id, created_at=now)
    session.add(place)
    try:
        session.flush()  # нужен place.id для названия
        session.add(
            PlaceName(
                place_id=place.id,
                name=display_name,
                name_search=name_search,
                is_canonical=1,
                created_by_user_id=created_by_user_id,
                created_at=now,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return place
