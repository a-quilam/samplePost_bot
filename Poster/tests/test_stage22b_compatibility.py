# tests/test_stage22b_compatibility.py
"""
Stage 22B: переход со схемы 6 на 7 и сохранность личных данных.

Только временная копия схемы Stage 22A. Реальная Poster/post_bot.db не
открывается. Проверяются WAL-aware backup, ревизия существующих строк,
журнал сессий и повторный запуск.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from base import Base
from brick_types import rules_from_json
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Brick, Place, PlaceName, UserState, utcnow
from place_store import normalize_place_name
from tests.stage19a_helpers import connect, make_engine, temp_db_path
from user_store import get_or_create_user

# Схема легаси-таблицы `drafts` (Stage 21): модели `Draft` больше нет (S5),
# поэтому создаём и заполняем её сырым SQL, как её оставила бы старая база.
LEGACY_DRAFTS_DDL = """
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
        photos TEXT,
        formats TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME
    )
"""


def journal(path: str | Path) -> list[tuple[int, str]]:
    connection = connect(str(path))
    try:
        return [
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def scalar(path: str | Path, sql: str, params: tuple[object, ...] = ()):
    connection = connect(str(path))
    try:
        row = connection.execute(sql, params).fetchone()
        return None if row is None else tuple(row)
    finally:
        connection.close()


class Stage22BCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("config.SUPERADMIN_USER_IDS", ()))
        self.enterContext(patch("config.SUPERADMIN_CONTACT_URL", None))
        self.path = temp_db_path("poster-22b-compat-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        # Ровно Stage 22A: миграции 1–6 и legacy-таблицы, но без журнала
        # сессий Stage 22B. Bricks уже создан миграцией 6, поэтому его тоже
        # не отдаём create_all (иначе create_all добавил бы ревизию раньше).
        run_migrations(self.engine, DEFAULT_MIGRATIONS[:6])
        stage23_tables = {
            "templates",
            "template_versions",
            "template_version_bricks",
            "template_snapshots",
            "template_snapshot_bricks",
            "template_sessions",
            "announcement_drafts",
            "announcement_values",
            "announcement_extras",
        }
        # R3: таблицы конфликтов создаёт миграция 19, а не create_all —
        # иначе имитация старой схемы получила бы их раньше миграции.
        stage23_tables |= {
            "brick_conflicts",
            "brick_conflict_notifications",
        }
        legacy_tables = [
            table
            for name, table in Base.metadata.tables.items()
            if name not in {"brick_sessions", "brick_proposals"} | stage23_tables
        ]
        Base.metadata.create_all(bind=self.engine, tables=legacy_tables)
        with self.engine.begin() as connection:
            connection.execute(text(LEGACY_DRAFTS_DDL))
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

    def backup_paths(self) -> list[Path]:
        return sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))

    def test_stage22a_database_migrates_to_seven_and_preserves_rows(self) -> None:
        user = get_or_create_user(self.session, 9_440_001, username="owner")
        # Stage 22A ещё не знает шаблонов: создаём место напрямую, без
        # сервиса Stage 23, который требует таблиц версии 8.
        place = Place(created_by_user_id=user.id, created_at=utcnow())
        self.session.add(place)
        self.session.flush()
        self.session.add(
            PlaceName(
                place_id=place.id,
                name="Старый клуб",
                name_search=normalize_place_name("Старый клуб"),
                is_canonical=1,
                created_by_user_id=user.id,
                created_at=utcnow(),
            )
        )
        wip = UserState(
            user_id=user.id,
            chat_id=777,
            payload='{"current_step": 3, "data": {"title": "WIP"}}',
        )
        self.session.add(wip)
        self.session.commit()
        payload = wip.payload
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO drafts (user_id, title, text, image, photos, "
                    "formats, created_at) VALUES (:user_id, 'Черновик до "
                    "Stage 22B', 'Описание', 'photo-1', '[\"photo-1\"]', "
                    "'[\"Афиша\"]', :created_at)"
                ),
                {"user_id": user.id, "created_at": utcnow().isoformat(sep=" ")},
            )
        # revision ещё нет: вставляем напрямую в схему 6.
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO bricks "
                "(code, title, type, scope, source, owner_user_id, rules_json, "
                "status, created_by_user_id, created_at) VALUES "
                "('старый_код', 'Старый личный кирпичик', 'number', 'personal', "
                "'ask', ?, '{\"min\": 0}', 'active', ?, '2026-01-01 00:00:00')",
                (user.id, user.id),
            )
        self.session.close()

        self.assertEqual(journal(self.path)[-1], (6, "bricks_registry"))
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        copies = self.backup_paths()
        self.assertEqual(len(copies), 1, copies)
        backup = copies[0]
        self.assertEqual(journal(backup)[-1], (6, "bricks_registry"))
        self.assertIsNone(
            scalar(
                backup,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='brick_sessions'",
            )
        )
        backup_db = connect(str(backup))
        try:
            backup_columns = {
                str(row["name"])
                for row in backup_db.execute("PRAGMA table_info(bricks)")
            }
        finally:
            backup_db.close()
        self.assertNotIn("revision", backup_columns)
        self.assertEqual(
            scalar(backup, "SELECT code FROM bricks WHERE id=10"),
            ("старый_код",),
        )
        backup_connection = sqlite3.connect(backup)
        try:
            self.assertEqual(
                backup_connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            backup_connection.close()

        self.assertIn((17, "archive_legacy_drafts"), journal(self.path))
        self.assertIn((18, "builtin_templates_v3"), journal(self.path))
        self.assertEqual(
            journal(self.path)[-1], (LATEST_VERSION, "brick_proposal_description")
        )
        # Миграция 17 переименовала легаси-таблицу: строка прежнего черновика
        # читается из архива без потерь.
        archived = scalar(
            self.path,
            "SELECT title, image, photos, formats FROM legacy_drafts_backup",
        )
        assert archived is not None
        self.assertEqual(archived[0], "Черновик до Stage 22B")
        self.assertEqual(archived[1], "photo-1")
        self.assertEqual(archived[2], '["photo-1"]')
        self.assertEqual(archived[3], '["Афиша"]')
        with self.Session() as session:
            stored_state = session.get(UserState, user.id)
            assert stored_state is not None
            self.assertEqual(stored_state.payload, payload)
            brick = session.scalar(select(Brick).where(Brick.code == "старый_код"))
            assert brick is not None
            self.assertEqual(brick.id, 10)
            self.assertEqual(brick.title, "Старый личный кирпичик")
            self.assertEqual(brick.revision, 1)
            self.assertEqual(
                rules_from_json(brick.value_type, brick.rules_json), {"min": 0}
            )
            self.assertEqual(brick.owner_user_id, user.id)
            self.assertIsNotNone(session.get(Place, place.id))

    def test_migration_seven_is_idempotent_and_sessions_survive_restart(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO brick_sessions "
                "(telegram_user_id, payload, updated_at) "
                "VALUES (?, ?, '2026-01-01 00:00:00')",
                (9_440_002, '{"phase":"hub","page":0}'),
            )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(len(journal(self.path)), LATEST_VERSION)
        self.assertEqual(
            scalar(
                self.path,
                "SELECT payload FROM brick_sessions WHERE telegram_user_id=?",
                (9_440_002,),
            ),
            ('{"phase":"hub","page":0}',),
        )


if __name__ == "__main__":
    unittest.main()
