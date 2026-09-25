# database.py

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from base import Base

# .env может переопределить файл БД (в Docker: sqlite:////app/data/post_bot.db).
# Пустое значение или отсутствие переменной = путь по умолчанию.
load_dotenv()
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL") or "sqlite:///./post_bot.db"

# Busy timeout (сек): обработчики PTB и job-пул работают с БД из разных
# потоков/задач — ждём освобождения блокировки вместо мгновенного
# «database is locked».
SQLITE_BUSY_TIMEOUT_SECONDS = 30


def apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """
    PRAGMA на каждом новом соединении с SQLite:

    - busy_timeout — ждём блокировку (см. SQLITE_BUSY_TIMEOUT_SECONDS);
    - journal_mode=WAL — чтение не блокирует запись (параллельные job и
      обработчики), данные переживают сбои;
    - synchronous=NORMAL — рекомендуемый для WAL уровень надёжности/скорости.

    Для не-SQLite БД (и :memory:) функция не применяется.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # check_same_thread нужен только для SQLite (мультипоточные обработчики PTB)
    connect_args=(
        {"check_same_thread": False}
        if SQLALCHEMY_DATABASE_URL.startswith("sqlite")
        else {}
    ),
)
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    event.listen(engine, "connect", apply_sqlite_pragmas)

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
        with bind.begin() as connection:
            if "photos" not in columns:
                connection.execute(text("ALTER TABLE drafts ADD COLUMN photos TEXT"))
            if "updated_at" not in columns:
                # NULL у существующих записей — TTL для них считает срок
                # по created_at (см. handlers.jobs.sync_remove_old_drafts).
                connection.execute(
                    text("ALTER TABLE drafts ADD COLUMN updated_at DATETIME")
                )


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
