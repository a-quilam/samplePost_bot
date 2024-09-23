# handlers/jobs.py

import logging
from datetime import time, datetime, timedelta

from sqlalchemy.orm import Session
from telegram.ext import ContextTypes

from database import SessionLocal
from models import Draft

logger = logging.getLogger(__name__)

def sync_remove_old_drafts():
    session: Session = SessionLocal()

    try:
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        old_drafts = session.query(Draft).filter(Draft.created_at < cutoff_date).all()

        if not old_drafts:
            logger.info("Нет черновиков, подлежащих удалению.")
            return

        count = len(old_drafts)
        for draft in old_drafts:
            session.delete(draft)

        session.commit()
        logger.info(f"Удалено {count} черновиков, которым больше месяца.")
    except Exception as e:
        logger.error(f"Ошибка при удалении черновиков: {e}")
        session.rollback()
    finally:
        session.close()

async def remove_old_drafts(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запуск фоновой задачи: удаление старых черновиков.")
    sync_remove_old_drafts()

def setup_jobs(application):
    """
    Настройка фоновых задач для бота.

    :param application: Экземпляр Telegram Application
    """
    if application.job_queue:
        # Планируем задачу на ежедневное выполнение в 00:00 UTC
        application.job_queue.run_daily(
            remove_old_drafts,
            time=time(hour=0, minute=0),
            name="remove_old_drafts"
        )
        logger.info("Фоновая задача 'remove_old_drafts' успешно настроена.")
    else:
        logger.error("JobQueue не инициализирован. Фоновая задача не настроена.")
