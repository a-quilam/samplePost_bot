# admin_state.py
"""
Долговременное состояние административного кабинета (Stage 20A).

Оно отделено от WIP-поста и от сценария «Места»:
- содержит только фазу, выбранное место и кандидата в администраторы;
- не хранит роли: права проверяются по конфигурации суперадмина и
  таблице `place_admins` при каждом действии;
- переживает рестарт, чтобы подтверждение назначения не потерялось;
- очищается при переходе в другой раздел, /start, /cancel и главное меню.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import SessionLocal
from models import AdminSession, utcnow

logger = logging.getLogger(__name__)


def load_admin_session(telegram_user_id: int) -> dict | None:
    """Читает JSON-состояние кабинета; битое значение считается отсутствующим."""
    session = SessionLocal()
    try:
        row = session.get(AdminSession, telegram_user_id)
        if row is None:
            return None
        try:
            parsed = json.loads(row.payload)
        except (TypeError, ValueError):
            logger.warning(
                "Повреждённое состояние кабинета пользователя %s проигнорировано.",
                telegram_user_id,
            )
            return None
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:  # noqa: BLE001 — состояние не должно ломать бота
        logger.error("Не удалось прочитать состояние кабинета: %s", e)
        return None
    finally:
        session.close()


def save_admin_session(telegram_user_id: int, payload: dict) -> bool:
    """
    Сохраняет состояние кабинета (UPSERT).

    Возвращает True только после подтверждённого commit: вызывающий код
    удаляет пользовательский ввод лишь после успешного сохранения.
    """
    session = SessionLocal()
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
        statement = (
            sqlite_insert(AdminSession)
            .values(
                telegram_user_id=telegram_user_id,
                payload=encoded,
                updated_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[AdminSession.telegram_user_id],
                set_={"payload": encoded, "updated_at": utcnow()},
            )
        )
        session.execute(statement)
        session.commit()
        return True
    except Exception as e:  # noqa: BLE001 — экран остаётся рабочим
        session.rollback()
        logger.error("Не удалось сохранить состояние кабинета: %s", e)
        return False
    finally:
        session.close()


def clear_admin_session(telegram_user_id: int) -> None:
    """Удаляет только интерфейсное состояние кабинета; роли не трогаются."""
    session = SessionLocal()
    try:
        row = session.scalar(
            select(AdminSession).where(
                AdminSession.telegram_user_id == telegram_user_id
            )
        )
        if row is not None:
            session.delete(row)
            session.commit()
    except Exception as e:  # noqa: BLE001 — навигация не должна падать
        session.rollback()
        logger.error("Не удалось очистить состояние кабинета: %s", e)
    finally:
        session.close()


def has_admin_session(telegram_user_id: int) -> bool:
    """Есть ли сохранённое состояние административного кабинета."""
    return load_admin_session(telegram_user_id) is not None
