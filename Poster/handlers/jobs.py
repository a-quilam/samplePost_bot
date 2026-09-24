# handlers/jobs.py

import logging
from datetime import time, timedelta

from sqlalchemy.orm import Session
from telegram.ext import Application, ContextTypes

from approval import STATUS_APPROVED, STATUS_ASSIGNED
from database import SessionLocal
from models import Draft, PostApproval, utcnow

logger = logging.getLogger(__name__)

# Сколько дней черновик считается неиспользуемым
DRAFT_TTL_DAYS = 30


def sync_remove_old_drafts() -> None:
    """
    Удаляет черновики старше DRAFT_TTL_DAYS.

    Посты с АКТИВНЫМ согласованием (assigned/approved) не трогаем: ответственный
    уже получил уведомление и работает с постом — исчезновение посреди цикла
    сломало бы сценарий (кнопки ответили бы «Пост не найден»).
    Declined/published и посты без согласования удаляются как неиспользуемые.
    """
    session: Session = SessionLocal()
    try:
        cutoff = utcnow() - timedelta(days=DRAFT_TTL_DAYS)
        old = session.query(Draft).filter(Draft.created_at < cutoff).all()
        skipped = 0
        removed = 0
        for draft in old:
            # Удаляем и запись согласования — иначе она «осиротеет» без поста
            approval = (
                session.query(PostApproval)
                .filter(PostApproval.draft_id == draft.id)
                .first()
            )
            if approval is not None and approval.status in (
                STATUS_ASSIGNED,
                STATUS_APPROVED,
            ):
                skipped += 1  # идёт согласование/публикация — оставляем
                continue
            if approval is not None:
                session.delete(approval)
            session.delete(draft)
            removed += 1
        session.commit()
        logger.info(
            f"Удалено {removed} старых черновиков (пропущено {skipped} на согласовании)."
        )
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
