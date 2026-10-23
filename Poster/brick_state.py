# brick_state.py
"""
Долговременное состояние раздела «Мои кирпичики» (Stage 22B).

Состояние конструктора отделено от:
- WIP-поста (`user_states`) — создание кирпичика не меняет пост;
- справочника мест (`place_sessions`);
- административного кабинета (`admin_sessions`).

Ключ — Telegram user ID: это интерфейсная навигация, а не доменная связь.
Payload содержит только фазу, черновик определения и выбранные страницы.
Строка переживает перезапуск процесса, поэтому незавершённый мастер можно
продолжить с того же экрана. При переходе в другой раздел или /cancel она
очищается: незавершённый черновик не перехватывает чужой текстовый ввод.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import SessionLocal
from models import BrickSession, utcnow

logger = logging.getLogger(__name__)

# Фазы, в которых сценарий ждёт пользовательский текст. Остальные фазы —
# экранные: текст вне их не должен попадать в конструктор.
_BROWSE_PHASES = frozenset({"hub", "archive", "card", "archive_confirm"})


def load_brick_session(telegram_user_id: int) -> dict | None:
    """Читает JSON-состояние; битое значение считается отсутствующим."""
    session = SessionLocal()
    try:
        row = session.get(BrickSession, telegram_user_id)
        if row is None:
            return None
        try:
            parsed = json.loads(row.payload)
        except (TypeError, ValueError):
            logger.warning(
                "Повреждённое состояние кирпичиков пользователя %s " "проигнорировано.",
                telegram_user_id,
            )
            return None
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 — состояние не должно ломать бота
        logger.error("Не удалось прочитать состояние кирпичиков: %s", exc)
        return None
    finally:
        session.close()


def save_brick_session(telegram_user_id: int, payload: dict) -> bool:
    """
    Сохраняет состояние (UPSERT).

    True — только после подтверждённого commit. Вызывающий удаляет
    пользовательский ввод лишь после успешной записи.
    """
    session = SessionLocal()
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
        statement = (
            sqlite_insert(BrickSession)
            .values(
                telegram_user_id=telegram_user_id,
                payload=encoded,
                updated_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[BrickSession.telegram_user_id],
                set_={"payload": encoded, "updated_at": utcnow()},
            )
        )
        session.execute(statement)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — экран остаётся рабочим
        session.rollback()
        logger.error("Не удалось сохранить состояние кирпичиков: %s", exc)
        return False
    finally:
        session.close()


def clear_brick_session(telegram_user_id: int) -> None:
    """Удаляет только интерфейсное состояние; доменные кирпичики не трогает."""
    session = SessionLocal()
    try:
        row = session.scalar(
            select(BrickSession).where(
                BrickSession.telegram_user_id == telegram_user_id
            )
        )
        if row is not None:
            session.delete(row)
            session.commit()
    except Exception as exc:  # noqa: BLE001 — навигация не должна падать
        session.rollback()
        logger.error("Не удалось очистить состояние кирпичиков: %s", exc)
    finally:
        session.close()


def has_brick_session(telegram_user_id: int) -> bool:
    """Есть ли сохранённое состояние раздела (в любой фазе)."""
    return load_brick_session(telegram_user_id) is not None


def has_brick_draft(telegram_user_id: int) -> bool:
    """
    Есть ли незавершённый мастер создания/редактирования.

    Просмотр списка, архива или карточки не считается черновиком: /cancel
    не должен сообщать об отмене, если пользователь просто открыл раздел.
    """
    state = load_brick_session(telegram_user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") not in _BROWSE_PHASES
