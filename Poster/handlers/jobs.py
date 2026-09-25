# handlers/jobs.py

import logging
from datetime import time, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session
from telegram.ext import Application, ContextTypes

from database import SessionLocal
from models import Draft, utcnow

logger = logging.getLogger(__name__)

# Сколько дней черновик считается неиспользуемым
DRAFT_TTL_DAYS = 30


def sync_remove_old_drafts() -> None:
    """
    Удаляет черновики старше DRAFT_TTL_DAYS как неиспользуемые.
    """
    session: Session = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(days=DRAFT_TTL_DAYS)
        # Срок жизни — от последнего изменения (недавно правленный старый
        # черновик не удаляется); у записей без updated_at (NULL) — от создания.
        removed = (
            session.query(Draft)
            .filter(func.coalesce(Draft.updated_at, Draft.created_at) < cutoff)
            .delete(synchronize_session=False)
        )
        session.commit()
        logger.info(f"Удалено {removed} старых черновиков.")
    except Exception as e:
        logger.error(f"Ошибка удаления черновиков: {e}")
        session.rollback()
    finally:
        session.close()


async def remove_old_drafts(context: ContextTypes.DEFAULT_TYPE) -> None:
    sync_remove_old_drafts()


def setup_jobs(application: Application) -> None:
    # job_queue есть только с пакетом python-telegram-bot[job-queue] (APScheduler)
    if application.job_queue is None:
        raise RuntimeError(
            "job_queue недоступен: установите python-telegram-bot[job-queue]"
        )
    application.job_queue.run_daily(
        remove_old_drafts, time=time(hour=0, minute=0), name="remove_old_drafts"
    )
