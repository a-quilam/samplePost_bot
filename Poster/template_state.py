# template_state.py
"""
Долговременное состояние раздела «Мои шаблоны» (Stage 25).

Состояние редактора шаблонов отделено от:
- WIP-поста (`user_states`);
- справочника мест (`place_sessions`);
- административного кабинета (`admin_sessions`);
- конструктора кирпичиков (`brick_sessions`).

Ключ — Telegram user ID: это интерфейсная навигация, а не доменная связь.
Payload содержит фазу, ID шаблона, редактируемую секцию, исходную revision,
черновой текст и выбранные brick_id при неоднозначности. Строка переживает
перезапуск процесса, поэтому незавершённое редактирование можно продолжить.
При переходе в другой раздел или /cancel состояние очищается.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from database import SessionLocal
from models import TemplateSession, utcnow

logger = logging.getLogger(__name__)

# Фазы, в которых сценарий ждёт пользовательский текст. Остальные фазы —
# экранные: текст вне их не должен попадать в редактор шаблонов.
_TEXT_PHASES = frozenset(
    {
        "section_edit",
        "confirm_save",
        "section_error",
        "resolve_ambiguity",
        "await_new_name",
    }
)

# Фазы, которые считаются «browsing» (только экран, без ожидания ввода).
_BROWSE_PHASES = frozenset(
    {
        "hub",
        "card",
        "preview",
        "reset_confirm",
        "brick_list",
        "help",
        "create_choice",
        "create_confirm",
    }
)

# Незавершённые сценарии, которые нельзя молча потерять при навигации.
_DRAFT_PHASES = _TEXT_PHASES | frozenset({"saving", "create_saving"})


def load_template_session(telegram_user_id: int) -> dict | None:
    """Читает JSON-состояние; битое значение считается отсутствующим."""
    session = SessionLocal()
    try:
        row = session.get(TemplateSession, telegram_user_id)
        if row is None:
            return None
        try:
            parsed = json.loads(row.payload)
        except (TypeError, ValueError):
            logger.warning(
                "Повреждённое состояние шаблонов пользователя %s " "проигнорировано.",
                telegram_user_id,
            )
            return None
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 — состояние не должно ломать бота
        logger.error("Не удалось прочитать состояние шаблонов: %s", exc)
        return None
    finally:
        session.close()


def save_template_session(telegram_user_id: int, payload: dict) -> bool:
    """
    Сохраняет состояние (UPSERT).

    True — только после подтверждённого commit. Вызывающий удаляет
    пользовательский ввод лишь после успешной записи.
    """
    session = SessionLocal()
    try:
        encoded = json.dumps(payload, ensure_ascii=False)
        statement = (
            sqlite_insert(TemplateSession)
            .values(
                telegram_user_id=telegram_user_id,
                payload=encoded,
                updated_at=utcnow(),
            )
            .on_conflict_do_update(
                index_elements=[TemplateSession.telegram_user_id],
                set_={"payload": encoded, "updated_at": utcnow()},
            )
        )
        session.execute(statement)
        session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — экран остаётся рабочим
        session.rollback()
        logger.error("Не удалось сохранить состояние шаблонов: %s", exc)
        return False
    finally:
        session.close()


def clear_template_session(telegram_user_id: int) -> None:
    """Удаляет только интерфейсное состояние; доменные шаблоны не трогает."""
    session = SessionLocal()
    try:
        row = session.scalar(
            select(TemplateSession).where(
                TemplateSession.telegram_user_id == telegram_user_id
            )
        )
        if row is not None:
            session.delete(row)
            session.commit()
    except Exception as exc:  # noqa: BLE001 — навигация не должна падать
        session.rollback()
        logger.error("Не удалось очистить состояние шаблонов: %s", exc)
    finally:
        session.close()


def has_template_session(telegram_user_id: int) -> bool:
    """Есть ли сохранённое состояние раздела (в любой фазе)."""
    return load_template_session(telegram_user_id) is not None


def has_template_draft(telegram_user_id: int) -> bool:
    """
    Есть ли незавершённый редактор секции или создания.

    Просмотр списка, карточки или предпросмотра не считается черновиком.
    """
    state = load_template_session(telegram_user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") in _DRAFT_PHASES


def is_text_phase(telegram_user_id: int) -> bool:
    """Находится ли пользователь в фазе, которая принимает текстовый ввод."""
    state = load_template_session(telegram_user_id)
    if not isinstance(state, dict):
        return False
    return state.get("phase") in _TEXT_PHASES
