# handlers/jobs.py

import logging
from datetime import time, timedelta, datetime
from telegram.ext import ContextTypes
from sqlalchemy.orm import Session
from database import SessionLocal
from models import Draft

logger = logging.getLogger(__name__)

def sync_remove_old_drafts():
    session: Session = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(days=30)
        old = session.query(Draft).filter(Draft.created_at < cutoff).all()
        for draft in old:
            session.delete(draft)
        session.commit()
        logger.info(f"Удалено {len(old)} старых черновиков.")
    except Exception as e:
        logger.error(f"Ошибка удаления черновиков: {e}")
        session.rollback()
    finally:
        session.close()

async def remove_old_drafts(context: ContextTypes.DEFAULT_TYPE):
    sync_remove_old_drafts()

def setup_jobs(application):
    application.job_queue.run_daily(remove_old_drafts, time=time(hour=0, minute=0), name="remove_old_drafts")
