# place_store.py
"""
Справочник мест Stage 19B: нормализация, поиск, карточка и названия.

Правила этапа:
- поиск идёт по каноническим и альтернативным названиям, ранжируется
  (точное → альтернативное → частичное → похожее);
- похожие названия разных мест НЕ сливаются автоматически: пользователь
  явно выбирает существующее место или подтверждает создание нового;
- альтернативное название уникально внутри одного места (это гарантирует
  UNIQUE в SQLite), но может совпадать/быть похожим у разных мест —
  тогда интерфейс предупреждает и требует явного решения;
- все операции создания/добавления сами управляют транзакцией.

Функции возвращают простые неизменяемые снимки, а не ORM-объекты: после
закрытия сессии данные карточки остаются пригодными для отрисовки.
"""

from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Place, PlaceName, utcnow

# Предел интерфейса: название должно помещаться в карточку/кнопку вместе с
# остальными полями без молчаливого обрезания. Превышение — явная ошибка.
MAX_PLACE_NAME_LENGTH = 300

# Достаточно строгий порог «очень похожего» названия для предупреждения.
SIMILARITY_THRESHOLD = 0.8


class PlaceNameError(ValueError):
    """Название места пустое, слишком длинное или без букв/цифр."""


@dataclass(frozen=True)
class PlaceListItem:
    """Строка списка мест (без внутренних служебных полей в интерфейсе)."""

    place_id: int
    canonical_name_id: int
    canonical_name: str


@dataclass(frozen=True)
class PlaceNameItem:
    """Один вариант названия места."""

    id: int
    name: str
    is_canonical: bool


@dataclass(frozen=True)
class PlaceDetails:
    """Снимок карточки места для экрана."""

    place_id: int
    canonical_name_id: int
    canonical_name: str
    names: tuple[PlaceNameItem, ...]
    address: str | None
    url: str | None


@dataclass(frozen=True)
class SearchResult:
    """Результат поиска с объяснением, почему место найдено."""

    place_id: int
    canonical_name: str
    matched_name_id: int
    matched_name: str
    category: str
    score: float


@dataclass(frozen=True)
class NameConflict:
    """Похожее название у ДРУГОГО места (не слияние, только предупреждение)."""

    place_id: int
    canonical_name: str
    matched_name_id: int
    matched_name: str
    exact: bool


@dataclass(frozen=True)
class AlternativeNameResult:
    """Итог добавления альтернативы."""

    status: str  # created / duplicate / conflict / place_missing
    display_name: str
    name_id: int | None = None
    conflicts: tuple[NameConflict, ...] = ()


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
    сжимаются. Результат обязан быть непустым и содержать хотя бы один
    буквенно-цифровой символ: название из одних кавычек, дефисов или
    эмодзи недопустимо.

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
    if not normalized or not any(char.isalnum() for char in normalized):
        raise PlaceNameError(
            "Название места должно содержать хотя бы одну букву или цифру"
        )
    return normalized


def _validated_name(name: str, *, what: str = "Название места") -> str:
    """Отображаемое название с проверкой непустоты, смысла и длины."""
    display = clean_display_name(name)
    if not display:
        raise PlaceNameError(f"{what} не может быть пустым")
    if len(display) > MAX_PLACE_NAME_LENGTH:
        raise PlaceNameError(
            f"{what} слишком длинное: максимум {MAX_PLACE_NAME_LENGTH} "
            f"символов, получено {len(display)}. Сократите название."
        )
    normalize_place_name(display)  # проверка «хотя бы одна буква/цифра»
    return display


def validate_place_name(name: str, *, what: str = "Название места") -> str:
    """
    Публичная проверка пользовательского названия.

    Возвращает очищенное отображаемое значение; ошибка `PlaceNameError`
    содержит понятное человеку объяснение (пустое/длинное/без букв).
    """
    return _validated_name(name, what=what)


def _optional_text(value: str | None) -> str | None:
    """Необязательный контакт: пустое значение хранится как NULL."""
    if value is None:
        return None
    cleaned = " ".join(str(value).split())
    return cleaned or None


def create_place(
    session: Session,
    canonical_name: str,
    *,
    created_by_user_id: int | None = None,
    address: str | None = None,
    url: str | None = None,
) -> Place:
    """
    Создаёт место и его каноническое название в одной транзакции.

    Возвращает сохранённый `Place` с заполненным `id`. При любой ошибке
    (включая нарушение внешнего ключа создателя) транзакция откатывается:
    место без названия или название без места не остаются.

    `created_by_user_id` — внутренний `users.id`, а не Telegram user ID.
    Адрес/ссылка необязательны: в Stage 19B их заполняет не Telegram-флоу,
    но карточка умеет показывать уже известные контакты.
    """
    display_name = _validated_name(canonical_name)
    name_search = normalize_place_name(display_name)

    now = utcnow()
    place = Place(
        created_by_user_id=created_by_user_id,
        address=_optional_text(address),
        url=_optional_text(url),
        created_at=now,
    )
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


def list_places(
    session: Session, *, offset: int = 0, limit: int = 8
) -> tuple[list[PlaceListItem], int]:
    """
    Страница мест в порядке создания и общее число доступных мест.

    Запрос учитывает только места с каноническим названием: карточка без
    названия невозможна по прикладному контракту Stage 19A.
    """
    base = (
        select(Place.id, PlaceName.id, PlaceName.name)
        .join(PlaceName, PlaceName.place_id == Place.id)
        .where(PlaceName.is_canonical == 1)
    )
    total = int(session.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = session.execute(
        base.order_by(Place.id).offset(max(offset, 0)).limit(max(limit, 1))
    ).all()
    return (
        [
            PlaceListItem(
                place_id=int(row[0]),
                canonical_name_id=int(row[1]),
                canonical_name=str(row[2]),
            )
            for row in rows
        ],
        total,
    )


def get_place_details(session: Session, place_id: int) -> PlaceDetails | None:
    """Карточка места; None — если места нет или нарушен контракт названий."""
    place = session.get(Place, place_id)
    if place is None:
        return None
    names = list(
        session.scalars(
            select(PlaceName)
            .where(PlaceName.place_id == place_id)
            .order_by(PlaceName.is_canonical.desc(), PlaceName.id)
        ).all()
    )
    canonical = next((name for name in names if name.is_canonical), None)
    if canonical is None:
        return None
    return PlaceDetails(
        place_id=place.id,
        canonical_name_id=canonical.id,
        canonical_name=canonical.name,
        names=tuple(
            PlaceNameItem(
                id=name.id, name=name.name, is_canonical=bool(name.is_canonical)
            )
            for name in names
        ),
        address=place.address or None,
        url=place.url or None,
    )


def get_place_name(
    session: Session, place_id: int, name_id: int
) -> PlaceNameItem | None:
    """Вариант названия, принадлежащий именно этому месту."""
    row = session.scalar(
        select(PlaceName).where(PlaceName.id == name_id, PlaceName.place_id == place_id)
    )
    if row is None:
        return None
    return PlaceNameItem(id=row.id, name=row.name, is_canonical=bool(row.is_canonical))


def _trigrams(value: str) -> set[str]:
    """Множество 3-грамм с граничными пробелами (для Jaccard)."""
    padded = f"  {value} "
    return {padded[index : index + 3] for index in range(len(padded) - 2)}


def place_name_similarity(left: str, right: str) -> float:
    """
    Похожесть двух названий в [0, 1].

    Комбинируются Jaccard 3-грамм (устойчив к перестановке слов) и
    SequenceMatcher (ловит опечатки в коротких названиях). Берётся максимум:
    оба варианта независимо полезны, ложных слияний нет — это только
    предупреждение пользователю.
    """
    first = normalize_place_name(left)
    second = normalize_place_name(right)
    if first == second:
        return 1.0
    first_grams = _trigrams(first)
    second_grams = _trigrams(second)
    union = first_grams | second_grams
    jaccard = len(first_grams & second_grams) / len(union) if union else 0.0
    sequence = difflib.SequenceMatcher(None, first, second).ratio()
    return max(jaccard, sequence)


def _normalized_search_query(query: str) -> str:
    """Проверка поискового ввода: он тоже не должен быть пустым/огромным."""
    display = clean_display_name(query)
    if not display:
        raise PlaceNameError("Введите часть названия места")
    if len(display) > MAX_PLACE_NAME_LENGTH:
        raise PlaceNameError(
            f"Слишком длинный поисковый запрос: максимум "
            f"{MAX_PLACE_NAME_LENGTH} символов."
        )
    return normalize_place_name(display)


_RANK = {
    "canonical_exact": 0,
    "alias_exact": 1,
    "canonical_partial": 2,
    "alias_partial": 3,
    "similar": 4,
}


def search_places(session: Session, query: str) -> list[SearchResult]:
    """
    Ранжированный поиск по каноническим и альтернативным названиям.

    Для каждого места остаётся лучший вариант названия. Точное каноническое
    название важнее точной альтернативы, затем идут частичные совпадения и
    похожие названия. Места с одинаковыми названиями остаются отдельными.
    """
    needle = _normalized_search_query(query)
    places = list(session.scalars(select(Place).order_by(Place.id)).all())
    names = list(session.scalars(select(PlaceName).order_by(PlaceName.id)).all())
    names_by_place: dict[int, list[PlaceName]] = {}
    canonical_by_place: dict[int, PlaceName] = {}
    for name in names:
        names_by_place.setdefault(name.place_id, []).append(name)
        if name.is_canonical:
            canonical_by_place[name.place_id] = name

    best_by_place: dict[int, SearchResult] = {}
    for place in places:
        canonical = canonical_by_place.get(place.id)
        if canonical is None:
            continue
        for name in names_by_place.get(place.id, []):
            is_canonical = bool(name.is_canonical)
            category: str | None = None
            score = 0.0
            if name.name_search == needle:
                category = "canonical_exact" if is_canonical else "alias_exact"
                score = 1.0
            elif needle in name.name_search:
                category = "canonical_partial" if is_canonical else "alias_partial"
                # Совпадение с начала названия полезнее совпадения в середине.
                score = 0.99 if name.name_search.startswith(needle) else 0.9
            else:
                similarity = place_name_similarity(needle, name.name_search)
                if similarity >= SIMILARITY_THRESHOLD:
                    category = "similar"
                    score = similarity
            if category is None:
                continue
            candidate = SearchResult(
                place_id=place.id,
                canonical_name=canonical.name,
                matched_name_id=name.id,
                matched_name=name.name,
                category=category,
                score=score,
            )
            current = best_by_place.get(place.id)
            if current is None or (
                _RANK[candidate.category],
                -candidate.score,
            ) < (
                _RANK[current.category],
                -current.score,
            ):
                best_by_place[place.id] = candidate

    results = list(best_by_place.values())
    results.sort(
        key=lambda item: (
            _RANK[item.category],
            -item.score,
            item.canonical_name.casefold(),
            item.place_id,
        )
    )
    return results


def find_name_conflicts(
    session: Session, name: str, *, exclude_place_id: int | None = None
) -> list[NameConflict]:
    """
    Совпадения/очень похожие названия у других мест.

    Одинаковая нормализованная строка — `exact=True`; иначе срабатывает
    порог похожести. Для каждого места возвращается одно лучшее совпадение.
    """
    display = _validated_name(name)
    needle = normalize_place_name(display)
    rows = session.execute(
        select(PlaceName, Place.id)
        .join(Place, Place.id == PlaceName.place_id)
        .order_by(PlaceName.place_id, PlaceName.id)
    ).all()
    canonical_by_place = {
        row[0].place_id: row[0] for row in rows if row[0].is_canonical
    }
    best: dict[int, NameConflict] = {}
    for name_row, place_id in rows:
        if exclude_place_id is not None and place_id == exclude_place_id:
            continue
        canonical = canonical_by_place.get(place_id)
        if canonical is None:
            continue
        exact = name_row.name_search == needle
        if not exact:
            if (
                place_name_similarity(needle, name_row.name_search)
                < SIMILARITY_THRESHOLD
            ):
                continue
        conflict = NameConflict(
            place_id=place_id,
            canonical_name=canonical.name,
            matched_name_id=name_row.id,
            matched_name=name_row.name,
            exact=exact,
        )
        current = best.get(place_id)
        if current is None or (conflict.exact and not current.exact):
            best[place_id] = conflict
    return [best[place_id] for place_id in sorted(best)]


def add_alternative_name(
    session: Session,
    place_id: int,
    name: str,
    *,
    created_by_user_id: int | None = None,
    force: bool = False,
) -> AlternativeNameResult:
    """
    Добавляет альтернативное название после проверок.

    - пустое/слишком длинное/бессмысленное имя — `PlaceNameError`;
    - уже есть у этого места (в т.ч. каноническое) — `duplicate`;
    - очень похоже на другое место и `force=False` — `conflict`, строка НЕ
      добавляется; интерфейс показывает предупреждение и спрашивает явно;
    - место исчезло — `place_missing`.

    Конкурентная вставка защищена UNIQUE `(place_id, name_search)` и
    SQLite UPSERT: даже при одновременном добавлении остаётся одна строка.
    """
    display = _validated_name(name, what="Альтернативное название")
    needle = normalize_place_name(display)

    if session.get(Place, place_id) is None:
        return AlternativeNameResult(status="place_missing", display_name=display)

    existing = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id,
            PlaceName.name_search == needle,
        )
    )
    if existing is not None:
        return AlternativeNameResult(
            status="duplicate",
            display_name=existing.name,
            name_id=existing.id,
        )

    conflicts = tuple(find_name_conflicts(session, display, exclude_place_id=place_id))
    if conflicts and not force:
        return AlternativeNameResult(
            status="conflict",
            display_name=display,
            conflicts=conflicts,
        )

    statement = (
        sqlite_insert(PlaceName)
        .values(
            place_id=place_id,
            name=display,
            name_search=needle,
            is_canonical=0,
            created_by_user_id=created_by_user_id,
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["place_id", "name_search"])
    )
    try:
        result = session.execute(statement)
        created = getattr(result, "rowcount", 0) == 1
        session.commit()
    except IntegrityError:
        session.rollback()
        if session.get(Place, place_id) is None:
            return AlternativeNameResult(status="place_missing", display_name=display)
        raise
    except Exception:
        session.rollback()
        raise

    row = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id,
            PlaceName.name_search == needle,
        )
    )
    if row is None:  # pragma: no cover — защита от неверного состояния БД
        raise RuntimeError("UPSERT названия не создал и не нашёл строку")
    if not created:
        return AlternativeNameResult(
            status="duplicate", display_name=row.name, name_id=row.id
        )
    return AlternativeNameResult(
        status="created", display_name=row.name, name_id=row.id
    )
