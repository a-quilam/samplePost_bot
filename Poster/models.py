# models.py

import json
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from base import Base  # Импортируем Base из base.py


def utcnow() -> datetime:
    """
    Наивный (без таймзоны) текущий момент UTC.

    datetime.utcnow() устарел в Python 3.12+ и шумит DeprecationWarning;
    колонки DateTime хранят наивные значения — поэтому tzinfo срезаем.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Draft(Base):
    __tablename__ = "drafts"

    # Стиль SQLAlchemy 2.0 (Mapped/mapped_column): mypy видит int/str,
    # а не Column[...]; nullable указан явно — схема БД не меняется.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    date: Mapped[str | None] = mapped_column(String(50), nullable=True)
    time_start: Mapped[str | None] = mapped_column(String(50), nullable=True)
    time_end: Mapped[str | None] = mapped_column(String(50), nullable=True)
    place_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    place_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    contact: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # JSON-список file_id всех фотографий поста (медиа-группа).
    # У старых черновиков NULL — тогда используется image (единственная/первая),
    # поэтому существующие записи продолжают работать без миграции данных.
    photos: Mapped[str | None] = mapped_column(Text, nullable=True)
    # HTML-представления форматируемых полей (JSON: {"title": "<b>...</b>"}).
    # NULL у старых черновиков и у полей без форматирования — тогда сводка
    # показывает плоский текст поля с HTML-экранированием (легаси-поведение).
    formats: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    # Последнее изменение: TTL считает срок жизни от него, чтобы недавно
    # правленный старый черновик не удалялся. У старых записей (миграция
    # добавила колонку без данных) NULL — тогда срок считается по created_at.
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=True
    )

    def __repr__(self) -> str:
        return f"<Draft(id={self.id}, user_id={self.user_id}, title={self.title})>"


class UserState(Base):
    """
    Состояние интерфейса одного пользователя (Stage 11).

    Одна строка на пользователя: message_id постоянного экрана и JSON-снимок
    незавершённого поста (WIP). Выживает при рестарте процесса — Telegram
    хранит только last_update, поэтому после перезапуска именно отсюда
    восстанавливаются идентификатор экрана и незавершённые данные.

    user_data (PTB) остаётся «горячим» источником во время диалога; таблица —
    долговременный снимок для восстановления, а не источник истины.
    """

    __tablename__ = "user_states"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # message_id постоянного экрана управления (NULL — экрана ещё не было)
    screen_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # JSON-снимок WIP: поля поста, formats, photos, current_step и т.п.
    # (NULL — незавершённого поста нет)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"<UserState(user_id={self.user_id}, "
            f"screen_message_id={self.screen_message_id})>"
        )


# --- Справочник мест: пользователи и места (Stage 19A) -----------------------


class User(Base):
    """
    Участник бота, известный локальной базе.

    `id` — внутренний суррогатный ключ для будущих связей (администраторы
    мест, владельцы шаблонов и кирпичиков). `telegram_user_id` — постоянный
    идентификатор Telegram, единственный внешний идентификатор пользователя:
    username меняется и не является ключом.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_user_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=True
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<User(id={self.id}, telegram_user_id={self.telegram_user_id}, "
            f"username={self.username})>"
        )


class Place(Base):
    """
    Место справочника (Stage 19A/19B).

    Названия живут отдельно (`place_names`): каноническое — строка с
    `is_canonical=1`, альтернативные — соседние строки. Администратор и
    стандартный шаблон появятся отдельными таблицами на следующих этапах;
    создатель места не получает административных прав.
    """

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Необязательные контакты. В Stage 19B их нельзя заполнить через бота;
    # карточка показывает поля только при наличии данных (без пустых строк).
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<Place(id={self.id}, created_by={self.created_by_user_id})>"


class PlaceName(Base):
    """
    Название места: каноническое или альтернативное.

    Ограничения БД:
    - `name` и `name_search` не пустые после trim (CHECK);
    - `is_canonical` — только 0/1;
    - в одном месте не бывает двух одинаковых нормализованных названий
      (UNIQUE `place_id` + `name_search`);
    - у места не больше одного канонического названия (частичный
      уникальный индекс). Наличие хотя бы одного названия обеспечивает
      прикладной слой (`place_store.create_place`), а не триггер.
    """

    __tablename__ = "place_names"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    place_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("places.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    name_search: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    is_canonical: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(trim(name)) > 0", name="ck_place_names_name_not_empty"),
        CheckConstraint(
            "length(trim(name_search)) > 0", name="ck_place_names_search_not_empty"
        ),
        CheckConstraint("is_canonical IN (0, 1)", name="ck_place_names_is_canonical"),
        UniqueConstraint("place_id", "name_search", name="uq_place_names_place_search"),
        Index(
            "ux_place_names_one_canonical",
            "place_id",
            unique=True,
            sqlite_where=text("is_canonical = 1"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<PlaceName(id={self.id}, place_id={self.place_id}, "
            f"name={self.name}, canonical={self.is_canonical})>"
        )


class PlaceSession(Base):
    """
    Ограниченное состояние Telegram-сценария выбора/создания места.

    Хранится ОТДЕЛЬНО от WIP-поста (`user_states.payload`): навигация по
    справочнику не должна менять незавершённый пост. Ключ — Telegram user ID:
    состояние интерфейса не является доменной связью с `users.id`; строка
    создаётся при первом входе в раздел «Места» и очищается при /start,
    /cancel или возврате в главное меню. Payload — небольшой JSON (фаза,
    выбранные id, введённое название), а не копия данных справочника.
    """

    __tablename__ = "place_sessions"

    telegram_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<PlaceSession(telegram_user_id={self.telegram_user_id})>"


# --- Фотографии поста (медиа-группы) -----------------------------------------


def photos_to_json(photos: list[str] | None) -> str | None:
    """
    Сериализует список file_id в JSON для колонки Draft.photos.
    Пустой список -> None (столбец не заполняется).
    """
    file_ids = [str(p) for p in (photos or []) if p]
    return json.dumps(file_ids, ensure_ascii=False) if file_ids else None


def get_draft_photos(draft: Draft) -> list[str]:
    """
    Возвращает список file_id фотографий поста.

    Совместимость со старыми черновиками: если photos не заполнен (NULL/пусто/
    битый JSON), берём image — единственную фотографию прежнего формата.
    """
    raw = getattr(draft, "photos", None)
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list) and parsed:
            return [str(p) for p in parsed if p]
    return [draft.image] if draft.image else []


# --- Форматирование текстовых полей (безопасный HTML) ------------------------


def formats_to_json(formats: dict[str, str] | None) -> str | None:
    """
    Сериализует HTML-представления полей в JSON для колонки Draft.formats.

    Пустой набор -> None: поле без форматирования и легаси-запись хранятся
    одинаково (NULL) и оба рендерятся как плоский текст — старое и новое
    представление различаются по наличию колонки/ключей, а не по символам.
    """
    items = {str(key): str(value) for key, value in (formats or {}).items() if value}
    return json.dumps(items, ensure_ascii=False) if items else None


def get_draft_formats(draft: Draft) -> dict[str, str]:
    """
    HTML-представления полей черновика.

    Старые записи (колонка/JSON отсутствует или битый) дают {}: поле
    рендерится как плоский текст — как и до Stage 10.
    """
    raw = getattr(draft, "formats", None)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items() if value}
