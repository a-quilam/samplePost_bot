# models.py

from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from base import Base  # Импортируем Base из base.py


class Draft(Base):
    __tablename__ = 'drafts'

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    title = Column(String(255), nullable=True)
    date = Column(String(50), nullable=True)
    time_start = Column(String(50), nullable=True)
    time_end = Column(String(50), nullable=True)
    place_name = Column(String(255), nullable=True)
    place_url = Column(String(255), nullable=True)
    text = Column(Text, nullable=True)
    contact = Column(String(255), nullable=True)
    image = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<Draft(id={self.id}, user_id={self.user_id}, title={self.title})>"


class ResponsiblePerson(Base):
    __tablename__ = 'responsible_persons'
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    telegram_id = Column(Integer, unique=True, nullable=False)

    def __repr__(self):
        return f"<ResponsiblePerson(id={self.id}, name={self.name}, telegram_id={self.telegram_id})>"
