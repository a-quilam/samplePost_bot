# models.py

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

    def __repr__(self) -> str:
        return f"<Draft(id={self.id}, user_id={self.user_id}, title={self.title})>"


class ResponsiblePerson(Base):
    __tablename__ = "responsible_persons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<ResponsiblePerson(id={self.id}, name={self.name}, "
            f"telegram_id={self.telegram_id})>"
        )


class PostApproval(Base):
    """
    Состояние согласования поста.

    Минимальное изменение схемы: НОВАЯ таблица — её создаёт create_all
    автоматически, существующие таблицы и данные не изменяются
    (без ALTER TABLE и миграций). На пост — ровно одна запись (unique draft_id),
    поэтому повторное нажатие кнопки не создаёт дублей.
    """

    __tablename__ = "post_approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    draft_id: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=True, index=True
    )
    responsible_telegram_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    # Возможные значения: 'assigned' -> 'approved' -> 'published' | 'declined'
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="assigned")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PostApproval(draft_id={self.draft_id}, "
            f"responsible={self.responsible_telegram_id}, status={self.status})>"
        )
