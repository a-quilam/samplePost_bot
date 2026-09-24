# handlers/jobs.py

import logging
from datetime import datetime, time, timedelta

from sqlalchemy.orm import Session
from telegram.ext import Application, ContextTypes

from database import SessionLocal
from models import Draft, PostApproval

logger = logging.getLogger(__name__)


def sync_remove_old_drafts() -> None:
    session: Session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old = session.query(Draft).filter(Draft.created_at < cutoff).all()
        for draft in old:
            # Удаляем и запись согласования — иначе она «осиротеет» без поста
            approval = (
                session.query(PostApproval)
                .filter(PostApproval.draft_id == draft.id)
                .first()
            )
            if approval is not None:
                session.delete(approval)
            session.delete(draft)
        session.commit()
        logger.info(f"Удалено {len(old)} старых черновиков.")
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
