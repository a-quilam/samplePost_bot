# models.py

import json
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String, Text
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
