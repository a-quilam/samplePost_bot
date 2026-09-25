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
