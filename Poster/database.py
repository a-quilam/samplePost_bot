# database.py

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from base import Base

SQLALCHEMY_DATABASE_URL = "sqlite:///./post_bot.db"

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate(bind) -> None:
    """
    Минимальные миграции SQLite (добавление новых колонок, без изменения данных).

    create_all создаёт только отсутствующие таблицы и НЕ добавляет колонки в
    уже существующие — поэтому новые колонки дописываем через ALTER TABLE.
    Операция идемпотентна: выполняется только если колонки ещё нет.
    """
    inspector = inspect(bind)
    if 'drafts' in inspector.get_table_names():
        columns = {column['name'] for column in inspector.get_columns('drafts')}
        if 'photos' not in columns:
            with bind.begin() as connection:
                connection.execute(text('ALTER TABLE drafts ADD COLUMN photos TEXT'))


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
