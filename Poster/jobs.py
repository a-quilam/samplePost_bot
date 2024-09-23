# jobs.py

import logging
from datetime import datetime, time, timedelta  # Корректный импорт

from sqlalchemy.orm import Session

from database import SessionLocal
from models import Draft

from telegram.ext import Application

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

async def remove_old_drafts(context):
    """
    Фоновая задача для удаления черновиков, которым больше месяца.
    """
    logger.info("Запуск фоновой задачи: удаление старых черновиков.")
    
    # Создаём сессию базы данных
    session: Session = SessionLocal()
    
    try:
        # Определяем дату, до которой черновики считаются старыми (30 дней назад)
        cutoff_date = datetime.utcnow() - timedelta(days=30)
        
        # Запрос на выбор черновиков, созданных до cutoff_date
        old_drafts = session.query(Draft).filter(Draft.created_at < cutoff_date).all()
        
        if not old_drafts:
            logger.info("Нет черновиков, подлежащих удалению.")
            return
        
        # Количество черновиков, которые будут удалены
        count = len(old_drafts)
        
        # Удаление старых черновиков
        for draft in old_drafts:
            session.delete(draft)
        
        # Фиксация изменений в базе данных
        session.commit()
        
        logger.info(f"Удалено {count} черновиков, которым больше месяца.")
    except Exception as e:
        logger.error(f"Ошибка при удалении черновиков: {e}")
        session.rollback()
    finally:
        # Закрываем сессию
        session.close()

def setup_jobs(application: Application):
    """
    Настройка фоновых задач для бота.
    
    :param application: Экземпляр Telegram Application
    """
    # Планируем задачу на ежедневное выполнение в 00:00 UTC
    application.job_queue.run_daily(
        remove_old_drafts,
        time=time(hour=0, minute=0),  # Корректный вызов time
        name="remove_old_drafts"
    )
    logger.info("Фоновая задача 'remove_old_drafts' успешно настроена.")
