# user_store.py
"""
Минимальный слой пользователей (Stage 19A).

Пользователь идентифицируется постоянным Telegram user ID; username —
только справочное поле, которое обновляется при каждом обращении.
Функции сами управляют транзакцией: вызывающий код не обязан помнить
о commit/rollback.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from models import User, utcnow


def _clean_optional(value: str | None, *, strip_at: bool = False) -> str | None:
    """Trim и пустая строка -> None; у username снимается ведущий «@»."""
    if value is None:
        return None
    cleaned = value.strip()
    if strip_at:
        cleaned = cleaned.lstrip("@")
    return cleaned or None


def get_or_create_user(
    session: Session,
    telegram_user_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
) -> User:
    """
    Возвращает пользователя по Telegram user ID, создавая его при первом
    обращении. Повторный вызов обновляет известные username/имя и
    `last_seen_at`; пустые значения не затирают ранее сохранённые.

    Атомарность: SQLite UPSERT по уникальному `telegram_user_id` — два
    конкурентных вызова не создают дубль и не падают на гонке.
    """
    if isinstance(telegram_user_id, bool) or not isinstance(telegram_user_id, int):
        raise ValueError("telegram_user_id должен быть целым числом")
    if telegram_user_id <= 0:
        raise ValueError("telegram_user_id должен быть положительным")

    clean_username = _clean_optional(username, strip_at=True)
    clean_full_name = _clean_optional(full_name)
    now = utcnow()

    statement = sqlite_insert(User).values(
        telegram_user_id=telegram_user_id,
        username=clean_username,
        full_name=clean_full_name,
        created_at=now,
        updated_at=now,
        last_seen_at=now,
    )
    update_values: dict[str, Any] = {"updated_at": now, "last_seen_at": now}
    if clean_username is not None:
        update_values["username"] = clean_username
    if clean_full_name is not None:
        update_values["full_name"] = clean_full_name
    statement = statement.on_conflict_do_update(
        index_elements=[User.telegram_user_id],
        set_=update_values,
    )

    session.execute(statement)
    session.commit()

    user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:  # pragma: no cover — защита от неверного состояния БД
        raise RuntimeError("UPSERT пользователя не создал и не нашёл строку")
    return user
