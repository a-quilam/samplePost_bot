# database.py

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from base import Base

# .env может переопределить файл БД (в Docker: sqlite:////app/data/post_bot.db).
# Пустое значение или отсутствие переменной = путь по умолчанию.
load_dotenv()
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./post_bot.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # check_same_thread нужен только для SQLite (мультипоточные обработчики PTB)
    connect_args=(
        {"check_same_thread": False}
        if SQLALCHEMY_DATABASE_URL.startswith("sqlite")
        else {}
    ),
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _migrate(bind) -> None:
    """
    Минимальные миграции SQLite (добавление новых колонок, без изменения данных).

    create_all создаёт только отсутствующие таблицы и НЕ добавляет колонки в
    уже существующие — поэтому новые колонки дописываем через ALTER TABLE.
    Операция идемпотентна: выполняется только если колонки ещё нет.
    """
    inspector = inspect(bind)
    if "drafts" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("drafts")}
        if "photos" not in columns:
            with bind.begin() as connection:
                connection.execute(text("ALTER TABLE drafts ADD COLUMN photos TEXT"))


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
