# place_state.py
"""
Долговременное состояние сценария «Места» (Stage 19B).

Оно намеренно отделено от WIP-поста в `user_states`:
- поход в справочник не должен менять незавершённый пост;
- снимок ограничен фазой и выбранными id, а не копией справочника;
- ключ — Telegram user ID, потому что это состояние интерфейса.

Строка `place_sessions` переживает рестарт. Очищается при /start, /cancel,
возврате в главное меню и переходе в другой раздел.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import SessionLocal
from models import PlaceSession, utcnow

logger = logging.getLogger(__name__)


def load_place_session(telegram_user_id: int) -> dict | None:
    """Читает JSON-состояние сценария; битое значение считается отсутствующим."""
    session = SessionLocal()
    try:
        row = session.get(PlaceSession, telegram_user_id)
        if row is None:
            return None
        try:
            parsed = json.loads(row.payload)
        except (TypeError, ValueError):
            logger.warning(
                "Повреждённое состояние мест пользователя %s проигнорировано.",
                telegram_user_id,
            )
            return None
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:  # noqa: BLE001 — состояние не должно ломать бота
        logger.error("Не удалось прочитать состояние мест: %s", e)
        return None
    finally:
        session.close()


def save_place_session(telegram_user_id: int, payload: dict) -> bool:
    """
    Сохраняет состояние сценария (UPSERT).

    Возвращает True только после подтверждённого commit: вызывающий код
    удаляет пользовательский ввод лишь после успешного сохранения.
    """
    session = SessionLocal()
    try:
        statement = (
            sqlite_insert(PlaceSession)
            .values(
                telegram_user_id=telegram_user_id,
                payload=json.dumps(payload, ensure_ascii=False),
                updated_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[PlaceSession.telegram_user_id],
                set_={
                    "payload": json.dumps(payload, ensure_ascii=False),
                    "updated_at": utcnow(),
                },
            )
        )
        session.execute(statement)
        session.commit()
        return True
    except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
        session.rollback()
        logger.error("Не удалось сохранить состояние мест: %s", e)
        return False
    finally:
        session.close()


def clear_place_session(telegram_user_id: int) -> None:
    """Удаляет только интерфейсное состояние; доменные места не трогаются."""
    session = SessionLocal()
    try:
        row = session.scalar(
            select(PlaceSession).where(
                PlaceSession.telegram_user_id == telegram_user_id
            )
        )
        if row is not None:
            session.delete(row)
            session.commit()
    except Exception as e:  # noqa: BLE001 — навигация не должна падать
        session.rollback()
        logger.error("Не удалось очистить состояние мест: %s", e)
    finally:
        session.close()


def has_place_session(telegram_user_id: int) -> bool:
    """Есть ли активное/сохранённое состояние сценария «Места»."""
    return load_place_session(telegram_user_id) is not None
