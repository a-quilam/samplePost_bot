# place_edit_store.py
"""
Административное редактирование данных места (Stage 20B).

Правила этапа:
- редактировать каноническое название, альтернативные названия, адрес и
  ссылку может только суперадминистратор или действующий администратор
  ИМЕННО этого места; роль проверяется заново внутри каждой операции по
  Telegram user ID — данные из callback/состояния экрана не принимаются;
- каждое изменение атомарно: проверка актуального значения → изменение →
  строка аудита `place_edit_log` → commit. Если исходное значение изменил
  другой администратор, изменение отклоняется (`changed`) — потерянных
  обновлений нет;
- альтернативные названия: добавление/переименование/удаление по тем же
  правилам Stage 19B (уникальность внутри места, предупреждение о похожих
  названиях ДРУГИХ мест). Удалить каноническое название этим слоем нельзя;
- адрес и ссылка — необязательные поля: отсутствие представлено NULL,
  а не пустой строкой; интерфейс показывает «Не указан»/«Не указана»;
- город (Poster 2.0 U1a) — необязательное в схеме поле, обязательное при
  создании новой площадки; редактирование идёт теми же правилами аудита
  (`city_set`/`city_delete`);
- ссылка нормализуется в валидный http(s)-URL, пригодный для гиперссылки;
  `javascript:`, `file:`, `data:` и прочие схемы отклоняются;
- аудит не содержит секретов и сырых сообщений: только разобранные
  значения полей места и внутренние идентификаторы.

Функции сами управляют транзакцией, как в place_admin_store.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models import Place, PlaceEditLog, PlaceName, User, utcnow
from permissions import can_manage_place
from place_store import (
    NameConflict,
    find_name_conflicts,
    normalize_place_name,
    validate_place_city,
    validate_place_name,
)

logger = logging.getLogger(__name__)

# Ссылка ограничена разумным размером гиперссылки. Для адреса жёсткого
# лимита хранения нет: SQLite TEXT и карточка не должны терять введённый
# текст. Длинное значение на экране показывается явным фрагментом с пометкой
# «полное значение сохранено» (см. handlers/admin.py).
MAX_PLACE_URL_LENGTH = 500

# Действия аудита: ровно одна строка на успешное изменение.
ACTION_RENAME_CANONICAL = "rename_canonical"
ACTION_ALIAS_ADD = "alias_add"
ACTION_ALIAS_RENAME = "alias_rename"
ACTION_ALIAS_DELETE = "alias_delete"
ACTION_ADDRESS_SET = "address_set"
ACTION_ADDRESS_DELETE = "address_delete"
ACTION_URL_SET = "url_set"
ACTION_URL_DELETE = "url_delete"
# Poster 2.0 U1a: город площадки. У города нет «удаления» в UI по умолчанию
# (он обязателен для новых мест), но аудит различает установку и сброс.
ACTION_CITY_SET = "city_set"
ACTION_CITY_DELETE = "city_delete"


class PlaceEditValueError(ValueError):
    """Значение поля пустое, слишком длинное или не пригодно для места."""


class PlaceEditPermissionError(PermissionError):
    """Операция запрещена: вызывающий не управляет этим местом."""


class PlaceEditActorError(RuntimeError):
    """Администратор не зарегистрирован в таблице users."""


@dataclass(frozen=True)
class EditMutationResult:
    """
    Результат мутации без ORM-объектов.

    status:
    - ok            — изменение применено, строка аудита записана;
    - changed       — исходное значение уже не то (конкурентное изменение),
                      изменение НЕ выполнено;
    - duplicate     — такое название уже есть у места (нет изменения);
    - unchanged     — значение совпадает с текущим (нет изменения);
    - conflict      — новое название похоже на название другого места,
                      сохранение требует явного подтверждения;
    - not_found     — строки названия больше нет (удалена/чужое место);
    - canonical     — попытка удалить/переименовать альтернативный слой,
                      затрагивая каноническое название;
    - place_missing — место не существует.
    """

    status: str
    value: str | None = None
    old_value: str | None = None
    name_id: int | None = None
    conflicts: tuple[NameConflict, ...] = ()


@dataclass(frozen=True)
class AliasRow:
    """Снимок альтернативного названия для экрана кабинета."""

    name_id: int
    name: str
    is_canonical: bool


def list_place_aliases(session: Session, place_id: int) -> tuple[AliasRow, ...]:
    """Альтернативные названия места (без канонического), в порядке id."""
    rows = session.execute(
        select(PlaceName.id, PlaceName.name)
        .where(PlaceName.place_id == place_id, PlaceName.is_canonical == 0)
        .order_by(PlaceName.id)
    ).all()
    return tuple(
        AliasRow(name_id=int(row[0]), name=str(row[1]), is_canonical=False)
        for row in rows
    )


def _actor_id_for_place(
    session: Session, actor_telegram_user_id: int, place_id: int
) -> int:
    """
    Внутренний users.id актора, допущенного к редактированию места.

    Роль проверяется по ACTUAL строке `place_admins` и конфигурации
    суперадминистраторов — не по данным интерфейса.
    """
    if not can_manage_place(session, actor_telegram_user_id, place_id):
        raise PlaceEditPermissionError(
            "Редактирование места доступно только его администратору или суперадминистратору."
        )
    actor = session.scalar(
        select(User.id).where(User.telegram_user_id == actor_telegram_user_id)
    )
    if actor is None:
        raise PlaceEditActorError(
            "Актор редактирования не зарегистрирован в таблице пользователей."
        )
    return int(actor)


def _log_edit(
    session: Session,
    *,
    actor_user_id: int,
    place_id: int,
    action: str,
    old_value: str | None,
    new_value: str | None,
) -> None:
    """Строка аудита в той же транзакции, что и само изменение."""
    session.add(
        PlaceEditLog(
            place_id=place_id,
            actor_user_id=actor_user_id,
            action=action,
            old_value=old_value,
            new_value=new_value,
            created_at=utcnow(),
        )
    )


# --- Валидация и нормализация полей -------------------------------------------


def clean_address_text(raw: str) -> str:
    """
    Адрес: NFKC, trim, сжатые пробелы. Пустое значение — ошибка с явной
    подсказкой использовать кнопку удаления.

    Искусственного ограничения длины нет: адрес хранится в TEXT полностью.
    Нормализация пробелов — осознанное правило отображения адреса, а не
    молчаливое обрезание; длина текста не уменьшается.
    """
    if not isinstance(raw, str):
        raise PlaceEditValueError("Адрес должен быть строкой")
    normalized = unicodedata.normalize("NFKC", raw)
    cleaned = " ".join(normalized.split())
    if not cleaned:
        raise PlaceEditValueError(
            "Адрес пустой. Если адреса нет, используйте кнопку «Удалить адрес»."
        )
    return cleaned


_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def normalize_place_url(raw: str) -> str:
    """
    Нормализация ссылки места в валидный http(s)-URL.

    Правила:
    - пробелы по краям срезаются; ссылки с внутренними пробелами отклоняются;
    - `t.me/x`, `example.com`, `www.example.com/path` получают `https://`;
    - http:// и https:// сохраняются как есть (путь, query, фрагмент — тоже);
    - схема задаётся пользователем автоматически ТОЛЬКО как https,
      `www.` автоматически не добавляется;
    - `javascript:`, `file:`, `data:` и другие непригодные для гиперссылки
      схемы отклоняются явно;
    - пустое значение и длиннее 500 символов — ошибка;
    - имя кирпичика `ссылка_места` не превращается в URL: без точки и с
      подчёркиванием в «домене» значение отклоняется.
    """
    if not isinstance(raw, str):
        raise PlaceEditValueError("Ссылка должна быть строкой")
    cleaned = raw.strip()
    if not cleaned:
        raise PlaceEditValueError(
            "Ссылка пустая. Удалить ссылку можно кнопкой «Удалить ссылку»."
        )
    if len(cleaned) > MAX_PLACE_URL_LENGTH:
        raise PlaceEditValueError(
            f"Ссылка слишком длинная: максимум {MAX_PLACE_URL_LENGTH} символов."
        )
    if " " in cleaned:
        raise PlaceEditValueError("Ссылка не должна содержать пробелов.")

    candidate = cleaned if _SCHEME_RE.match(cleaned) else f"https://{cleaned}"
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise PlaceEditValueError(
            "Поддерживаются только ссылки http:// и https:// (например, https://t.me/poster)."
        )
    host = parsed.hostname or ""
    if not host:
        raise PlaceEditValueError(
            "В ссылке нет адреса сайта: укажите домен, например example.com."
        )
    if "_" in host:
        raise PlaceEditValueError(
            "Ссылка не распознана как адрес сайта. Укажите домен, например t.me/poster."
        )
    if not re.search(r"\.[A-Za-z0-9-]+$", host):
        raise PlaceEditValueError(
            "В адресе сайта нет доменной части: укажите домен с точкой, например example.com."
        )
    return candidate


# --- Альтернативные названия --------------------------------------------------


def _validated_alias_name(name: str) -> str:
    """Отображаемое альтернативное название по правилам Stage 19B."""
    return validate_place_name(name, what="Альтернативное название")


def add_place_alias(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    name: str,
    force: bool = False,
) -> EditMutationResult:
    """
    Добавляет альтернативное название администрируемого места.

    Правила как у обычного пользователя Stage 19B: дубликат внутри места
    (`duplicate`), предупреждение о похожих названиях других мест
    (`conflict` без записи). Записывает аудит `alias_add`.
    """
    display = _validated_alias_name(name)
    needle = normalize_place_name(display)
    actor_id = _actor_id_for_place(session, actor_telegram_user_id, place_id)
    if session.get(Place, place_id) is None:
        return EditMutationResult(status="place_missing")

    existing = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id, PlaceName.name_search == needle
        )
    )
    if existing is not None:
        return EditMutationResult(
            status="duplicate", value=existing.name, name_id=existing.id
        )

    conflicts = tuple(find_name_conflicts(session, display, exclude_place_id=place_id))
    if conflicts and not force:
        return EditMutationResult(status="conflict", value=display, conflicts=conflicts)

    created = PlaceName(
        place_id=place_id,
        name=display,
        name_search=needle,
        is_canonical=0,
        created_by_user_id=actor_id,
        created_at=utcnow(),
    )
    session.add(created)
    _log_edit(
        session,
        actor_user_id=actor_id,
        place_id=place_id,
        action=ACTION_ALIAS_ADD,
        old_value=None,
        new_value=display,
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise
    session.commit()
    return EditMutationResult(status="ok", value=display, name_id=created.id)


def rename_place_alias(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    name_id: int,
    new_name: str,
    expected_name: str,
    force: bool = False,
) -> EditMutationResult:
    """
    Переименовывает альтернативное название с защитой от потерянных
    обновлений: строка должна по-прежнему содержать `expected_name`.

    Каноническое название этим путём не переименовывается (`canonical`);
    дубликат внутри места отклоняется; предупреждение о похожих названиях
    других мест требует `force`. Аудит: `alias_rename`.
    """
    display = _validated_alias_name(new_name)
    needle = normalize_place_name(display)
    actor_id = _actor_id_for_place(session, actor_telegram_user_id, place_id)
    if session.get(Place, place_id) is None:
        return EditMutationResult(status="place_missing")

    row = session.scalar(
        select(PlaceName).where(PlaceName.id == name_id, PlaceName.place_id == place_id)
    )
    if row is None:
        return EditMutationResult(status="not_found", value=display)
    if row.is_canonical:
        return EditMutationResult(status="canonical", value=row.name)
    if row.name != expected_name:
        return EditMutationResult(status="changed", value=row.name)

    if row.name_search == needle:
        return EditMutationResult(status="unchanged", value=row.name, name_id=row.id)

    duplicate = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id,
            PlaceName.name_search == needle,
            PlaceName.id != row.id,
        )
    )
    if duplicate is not None:
        return EditMutationResult(
            status="duplicate", value=duplicate.name, name_id=duplicate.id
        )

    conflicts = tuple(find_name_conflicts(session, display, exclude_place_id=place_id))
    if conflicts and not force:
        return EditMutationResult(status="conflict", value=display, conflicts=conflicts)

    row.name = display
    row.name_search = needle
    _log_edit(
        session,
        actor_user_id=actor_id,
        place_id=place_id,
        action=ACTION_ALIAS_RENAME,
        old_value=expected_name,
        new_value=display,
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise
    session.commit()
    return EditMutationResult(status="ok", value=display, name_id=row.id)


def delete_place_alias(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    name_id: int,
    expected_name: str,
) -> EditMutationResult:
    """
    Удаляет ровно одно альтернативное название места.

    Каноническое название удалить нельзя (`canonical`); если строка
    уже удалена или принадлежит другому месту — `not_found`; если текст
    изменился с момента открытия экрана — `changed`. Аудит: `alias_delete`.
    """
    actor_id = _actor_id_for_place(session, actor_telegram_user_id, place_id)
    if session.get(Place, place_id) is None:
        return EditMutationResult(status="place_missing")

    row = session.scalar(
        select(PlaceName).where(PlaceName.id == name_id, PlaceName.place_id == place_id)
    )
    if row is None:
        return EditMutationResult(status="not_found")
    if row.is_canonical:
        return EditMutationResult(status="canonical", value=row.name)
    if row.name != expected_name:
        return EditMutationResult(status="changed", value=row.name)

    session.delete(row)
    _log_edit(
        session,
        actor_user_id=actor_id,
        place_id=place_id,
        action=ACTION_ALIAS_DELETE,
        old_value=expected_name,
        new_value=None,
    )
    session.commit()
    return EditMutationResult(status="ok", old_value=expected_name, name_id=name_id)


# --- Каноническое название ----------------------------------------------------


def rename_canonical_place_name(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    new_name: str,
    expected_name: str,
    force: bool = False,
) -> EditMutationResult:
    """
    Изменяет каноническое название места.

    Правила Stage 20B:
    - название валидируется по алгоритму Stage 19B; дубликат внутри места
      (`duplicate`) и предупреждение о похожих названиях других мест
      (`conflict` до `force=True`) работают как у альтернативы;
    - если новое название уже является альтернативой этого места, статусы
      меняются атомарно: альтернатива становится канонической, прежнее
      каноническое — альтернативным (в БД всегда ровно один canonical);
    - если новое название новое, прежнее каноническое сохраняется как
      альтернативное название этого же места — старые варианты поиска
      продолжают работать;
    - защита от потерянных обновлений: строка канонического названия
      должна по-прежнему содержать `expected_name`, иначе `changed`;
    - audit `rename_canonical`: старое и новое отображаемые названия.
    """
    display = _validated_alias_name(new_name)
    needle = normalize_place_name(display)
    actor_id = _actor_id_for_place(session, actor_telegram_user_id, place_id)
    if session.get(Place, place_id) is None:
        return EditMutationResult(status="place_missing")

    canonical = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id, PlaceName.is_canonical == 1
        )
    )
    if canonical is None:
        return EditMutationResult(status="place_missing")
    if canonical.name != expected_name:
        return EditMutationResult(status="changed", value=canonical.name)

    if canonical.name_search == needle:
        return EditMutationResult(
            status="unchanged", value=canonical.name, name_id=canonical.id
        )

    alias = session.scalar(
        select(PlaceName).where(
            PlaceName.place_id == place_id,
            PlaceName.name_search == needle,
            PlaceName.id != canonical.id,
        )
    )
    if alias is not None:
        # Новое название уже есть у места: альтернатива становится канонической,
        # прежнее каноническое — альтернативным. Оба UPDATE в одной транзакции:
        # частичный уникальный индекс не даёт двух канонических названий.
        _log_edit(
            session,
            actor_user_id=actor_id,
            place_id=place_id,
            action=ACTION_RENAME_CANONICAL,
            old_value=canonical.name,
            new_value=alias.name,
        )
        canonical.is_canonical = 0
        alias.is_canonical = 1
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise
        session.commit()
        return EditMutationResult(status="ok", value=alias.name, name_id=alias.id)

    conflicts = tuple(find_name_conflicts(session, display, exclude_place_id=place_id))
    if conflicts and not force:
        return EditMutationResult(status="conflict", value=display, conflicts=conflicts)

    old_name = canonical.name
    old_needle = canonical.name_search
    canonical.name = display
    canonical.name_search = needle
    session.add(
        PlaceName(
            place_id=place_id,
            name=old_name,
            name_search=old_needle,
            is_canonical=0,
            created_by_user_id=actor_id,
            created_at=utcnow(),
        )
    )
    _log_edit(
        session,
        actor_user_id=actor_id,
        place_id=place_id,
        action=ACTION_RENAME_CANONICAL,
        old_value=old_name,
        new_value=display,
    )
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise
    session.commit()
    return EditMutationResult(status="ok", value=display, name_id=canonical.id)


# --- Адрес и ссылка ------------------------------------------------------------


_OPTIONAL_FIELD_ACTIONS = {
    "address": (ACTION_ADDRESS_SET, ACTION_ADDRESS_DELETE),
    "url": (ACTION_URL_SET, ACTION_URL_DELETE),
    "city": (ACTION_CITY_SET, ACTION_CITY_DELETE),
}


def _apply_optional_field(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    field: str,  # "address" | "url" | "city"
    new_value: str | None,
    expected_value: str | None,
) -> EditMutationResult:
    """
    Условное изменение необязательного поля места (адрес, ссылка или город).

    Защита от потерянных обновлений: поле должно по-прежнему содержать
    `expected_value` — то значение, которое администратор видел на экране.
    Если другой администратор успел его изменить, возвращается `changed`
    с текущим значением, и пользователь обновляет карточку.

    Установка и удаление значения (NULL) симметричны; отсутствие поля в БД —
    это NULL, никаких пустых строк или фиктивных «Не указано». Аудит:
    address_set/address_delete, url_set/url_delete или city_set/city_delete.
    """
    actor_id = _actor_id_for_place(session, actor_telegram_user_id, place_id)
    place = session.get(Place, place_id)
    if place is None:
        return EditMutationResult(status="place_missing")
    current = getattr(place, field)
    if current != expected_value:
        return EditMutationResult(status="changed", value=current)
    if current == new_value:
        return EditMutationResult(status="unchanged", value=current)

    setattr(place, field, new_value)
    set_action, delete_action = _OPTIONAL_FIELD_ACTIONS[field]
    action = set_action if new_value is not None else delete_action
    _log_edit(
        session,
        actor_user_id=actor_id,
        place_id=place_id,
        action=action,
        old_value=current,
        new_value=new_value,
    )
    session.commit()
    return EditMutationResult(status="ok", value=new_value, old_value=current)


def set_place_address(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    address: str,
    expected_address: str | None,
) -> EditMutationResult:
    """Устанавливает или меняет адрес места. Адрес — текст, а не URL."""
    cleaned = clean_address_text(address)
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="address",
        new_value=cleaned,
        expected_value=expected_address,
    )


def delete_place_address(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    expected_address: str | None,
) -> EditMutationResult:
    """Удаляет адрес места: поле возвращается к NULL, место сохраняется."""
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="address",
        new_value=None,
        expected_value=expected_address,
    )


def set_place_url(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    url: str,
    expected_url: str | None,
) -> EditMutationResult:
    """Устанавливает или меняет ссылку места; значение нормализуется в http(s)-URL."""
    normalized = normalize_place_url(url)
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="url",
        new_value=normalized,
        expected_value=expected_url,
    )


def delete_place_url(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    expected_url: str | None,
) -> EditMutationResult:
    """Удаляет ссылку места: поле возвращается к NULL, место сохраняется."""
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="url",
        new_value=None,
        expected_value=expected_url,
    )


def set_place_city(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    city: str,
    expected_city: str | None,
) -> EditMutationResult:
    """
    Устанавливает или меняет город места (Poster 2.0 U1a).

    Город нормализуется по тем же правилам, что и название (NFKC, сжатые
    пробелы, непустое значение), и ограничен `MAX_PLACE_CITY_LENGTH`.
    Аудит: `city_set`.
    """
    cleaned = validate_place_city(city)
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="city",
        new_value=cleaned,
        expected_value=expected_city,
    )


def delete_place_city(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
    expected_city: str | None,
) -> EditMutationResult:
    """
    Сбрасывает город места в NULL.

    Нужен для старых записей и для исправления ошибочно указанного города:
    обязательность города проверяется при СОЗДАНИИ места, а не при каждой
    правке. Аудит: `city_delete`.
    """
    return _apply_optional_field(
        session,
        actor_telegram_user_id=actor_telegram_user_id,
        place_id=place_id,
        field="city",
        new_value=None,
        expected_value=expected_city,
    )
