# database.py

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from base import Base  # Импортируем Base из base.py

# Путь к базе данных SQLite
SQLALCHEMY_DATABASE_URL = "sqlite:///./handlers/drafts.db"

# Создание SQLAlchemy engine
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

# Создание конфигурированного класса Session
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Функция для создания таблиц
def init_db():
    Base.metadata.create_all(bind=engine)
