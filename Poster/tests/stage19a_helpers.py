# tests/stage19a_helpers.py
"""
Общие фикстуры Stage 19A: только временные файлы SQLite.

Ни один помощник не открывает настоящую Poster/post_bot.db: путь реальной
базы известен лишь для проверки «отпечатка» до/после тестов.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from database import apply_sqlite_pragmas

APP_DIR = Path(__file__).resolve().parent.parent
REAL_DB_PATH = APP_DIR / "post_bot.db"

OLD_DRAFT = {
    "id": 7,
    "user_id": 249601025,
    "title": "Старый черновик",
    "date": "17.11.2026",
    "time_start": "19:00",
    "time_end": "21:00",
    "place_name": "Старое место",
    "place_url": "https://example.com/old",
    "text": "Текст <b>поста</b>",
    "contact": "@old_contact",
    "image": "file-id-1",
    "photos": '["file-id-1", "file-id-2"]',
    "formats": '{"title": "<b>Старый черновик</b>"}',
    "created_at": "2026-01-01 10:00:00",
    "updated_at": "2026-01-02 11:00:00",
}

OLD_WIP_PAYLOAD = {
    "title": "Незавершённый пост",
    "date": "24.10.2026",
    "photos": ["wip-file-1"],
    "current_step": 3,
    "formats": {},
}


def temp_db_path(prefix: str = "poster-19a-") -> str:
    """Путь к новой временной базе в отдельном каталоге."""
    directory = tempfile.mkdtemp(prefix=prefix)
    return os.path.join(directory, "test.db")


def make_engine(path: str) -> Engine:
    """SQLite-движок как в приложении (WAL, busy_timeout, foreign_keys)."""
    engine = create_engine(
        "sqlite:///" + path,
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", apply_sqlite_pragmas)
    return engine


def create_old_schema(path: str, *, ancient: bool = False) -> None:
    """
    Создаёт схему Poster до Stage 19A и фиктивные пользовательские данные.

    current: схема ровно как уходящая в прошлое (drafts с photos/formats/
    updated_at; user_states с payload).
    ancient: совсем старая drafts без photos/formats/updated_at — проверяет
    легаси-бэкфилл `_migrate`, который идёт после нумерованных миграций.
    """
    connection = sqlite3.connect(path)
    try:
        new_columns = (
            ""
            if ancient
            else """
                photos TEXT,
                formats TEXT,
                updated_at DATETIME,
            """
        )
        connection.executescript(f"""
            CREATE TABLE drafts (
                id INTEGER NOT NULL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title VARCHAR(255),
                date VARCHAR(50),
                time_start VARCHAR(50),
                time_end VARCHAR(50),
                place_name VARCHAR(255),
                place_url VARCHAR(255),
                text TEXT,
                contact VARCHAR(255),
                image VARCHAR(255),
                {new_columns}
                created_at DATETIME NOT NULL
            );
            CREATE INDEX ix_drafts_id ON drafts (id);
            CREATE INDEX ix_drafts_user_id ON drafts (user_id);

            CREATE TABLE user_states (
                user_id INTEGER NOT NULL PRIMARY KEY,
                chat_id INTEGER,
                screen_message_id INTEGER,
                payload TEXT,
                updated_at DATETIME
            );
            """)
        columns = [
            column
            for column in OLD_DRAFT
            if not (
                ancient
                and column
                in {
                    "photos",
                    "formats",
                    "updated_at",
                }
            )
        ]
        placeholders = ", ".join("?" for _ in columns)
        connection.execute(
            f"INSERT INTO drafts ({', '.join(columns)}) VALUES ({placeholders})",
            [OLD_DRAFT[column] for column in columns],
        )
        connection.execute(
            "INSERT INTO user_states "
            "(user_id, chat_id, screen_message_id, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                OLD_DRAFT["user_id"],
                111222333,
                3781,
                json.dumps(OLD_WIP_PAYLOAD, ensure_ascii=False),
                "2026-01-03 12:00:00",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def run_init_db_subprocess(path: str) -> None:
    """
    Полный init_db в отдельном процессе — как старт бота, но на temp-базе.

    Отдельный процесс исключает влияние уже созданного в тестах движка и
    заодно проверяет, что миграции переживают перезапуск приложения.
    """
    env = os.environ.copy()
    env["DATABASE_URL"] = "sqlite:///" + path
    env.setdefault(
        "TELEGRAM_BOT_TOKEN", "123456789:FIXTUREFAKETOKENforTestsOnly00000000"
    )
    result = subprocess.run(
        [sys.executable, "-c", "from database import init_db; init_db()"],
        cwd=str(APP_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(
            "init_db упал на временной базе:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def connect(path: str) -> sqlite3.Connection:
    """Прямое подключение к временной базе (read/write внутри теста)."""
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_names(path: str) -> set[str]:
    connection = connect(path)
    try:
        return {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()
