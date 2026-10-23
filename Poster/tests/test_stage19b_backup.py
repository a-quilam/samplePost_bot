# tests/test_stage19b_backup.py
"""
Stage 19B: автоматическая резервная копия SQLite перед миграциями.

Проверяется только временная база. Копия делается через SQLite Backup API
(корректно видит WAL), при сбое миграция не начинается, готовые копии
никогда не удаляются приложением.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text

from migrations import DEFAULT_MIGRATIONS, BackupError, MigrationError, run_migrations
from tests.stage19a_helpers import (
    OLD_DRAFT,
    OLD_WIP_PAYLOAD,
    connect,
    create_old_schema,
    make_engine,
    temp_db_path,
)


def backups(path: str) -> list[Path]:
    return sorted(Path(path).parent.glob(Path(path).name + ".backup-*"))


def journal(path: str) -> list[tuple[int, str]]:
    connection = connect(path)
    try:
        return [
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def scalar(path: str, sql: str, parameters: tuple = ()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, parameters).fetchone()
    finally:
        connection.close()


class MigrationBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-19b-backup-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def test_new_database_does_not_need_backup(self) -> None:
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine)
        self.assertEqual(backups(self.path), [])

    def test_legacy_database_is_backed_up_before_first_migration(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)

        run_migrations(engine)

        copies = backups(self.path)
        self.assertEqual(len(copies), 1, copies)
        copy = copies[0]
        self.assertGreater(copy.stat().st_size, 0)
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        # Копия — состояние ДО миграции: пользовательские данные на месте,
        # новых таблиц и журнала ещё нет.
        self.assertEqual(
            scalar(
                str(copy), "SELECT title FROM drafts WHERE id = ?", (OLD_DRAFT["id"],)
            ),
            (OLD_DRAFT["title"],),
        )
        self.assertIsNone(
            scalar(
                str(copy),
                "SELECT name FROM sqlite_master WHERE type='table' AND name='places'",
            )
        )
        with sqlite3.connect(copy) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        self.assertEqual(
            [version for version, _ in journal(self.path)], [1, 2, 3, 4, 5, 6, 7, 8]
        )

    def test_second_migration_is_backed_up_before_it_runs(self) -> None:
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine, DEFAULT_MIGRATIONS[:1])
        self.assertEqual(backups(self.path), [])
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO places (id, created_at) VALUES (7, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO place_names "
                    "(id, place_id, name, name_search, is_canonical, created_at) "
                    "VALUES (11, 7, 'Театр', 'театр', 1, CURRENT_TIMESTAMP)"
                )
            )

        run_migrations(engine)

        copies = backups(self.path)
        self.assertEqual(len(copies), 1)
        copy = copies[0]
        self.assertEqual(
            [version for version, _ in journal(str(copy))],
            [1],
        )
        self.assertIsNone(
            scalar(
                str(copy),
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='place_sessions'",
            )
        )
        self.assertEqual(
            scalar(str(copy), "SELECT name FROM place_names WHERE place_id = 7"),
            ("Театр",),
        )
        self.assertEqual(
            [version for version, _ in journal(self.path)], [1, 2, 3, 4, 5, 6, 7, 8]
        )
        self.assertEqual(
            scalar(self.path, "SELECT name FROM place_names WHERE place_id = 7"),
            ("Театр",),
        )
        self.assertEqual(
            scalar(self.path, "SELECT address FROM places WHERE id = 7"),
            (None,),
        )

    def test_backup_captures_committed_data_still_in_wal(self) -> None:
        create_old_schema(self.path)
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO drafts (user_id, title, created_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (4242, "Черновик из WAL"),
        )
        writer.commit()
        wal = Path(self.path + "-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)

        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine)

        copies = backups(self.path)
        self.assertEqual(len(copies), 1)
        self.assertEqual(
            scalar(
                str(copies[0]),
                "SELECT title FROM drafts WHERE user_id = ?",
                (4242,),
            ),
            ("Черновик из WAL",),
        )

    def test_failed_backup_prevents_migration(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)

        with patch(
            "migrations.create_database_backup",
            side_effect=BackupError("имитация сбоя копирования"),
        ):
            with self.assertRaises(BackupError):
                run_migrations(engine)

        connection = connect(self.path)
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("places", tables)
        self.assertNotIn("schema_migrations", tables)
        self.assertEqual(
            scalar(
                self.path, "SELECT title FROM drafts WHERE id = ?", (OLD_DRAFT["id"],)
            ),
            (OLD_DRAFT["title"],),
        )
        self.assertEqual(backups(self.path), [])

    def test_backup_does_not_delete_existing_user_copies(self) -> None:
        create_old_schema(self.path)
        manual = Path(self.path + ".user-backup")
        manual.write_bytes(b"user-owned backup")
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)

        run_migrations(engine)

        self.assertTrue(manual.exists())
        self.assertEqual(manual.read_bytes(), b"user-owned backup")
        self.assertEqual(len(backups(self.path)), 1)

    def test_backup_contains_wip_and_can_be_restored(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine)
        copy = backups(self.path)[0]

        restored = temp_db_path("poster-19b-restored-")
        self.addCleanup(shutil.rmtree, os.path.dirname(restored), True)
        shutil.copy2(copy, restored)

        self.assertEqual(
            scalar(
                restored,
                "SELECT payload FROM user_states WHERE user_id = ?",
                (OLD_DRAFT["user_id"],),
            ),
            (json.dumps(OLD_WIP_PAYLOAD, ensure_ascii=False),),
        )
        self.assertIsNone(
            scalar(
                restored,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='places'",
            )
        )
        # Восстановленная копия — обычная старая база: следующий запуск снова
        # создаст новую копию и применит миграции.
        restored_engine = make_engine(restored)
        self.addCleanup(restored_engine.dispose)
        run_migrations(restored_engine)
        self.assertEqual(
            [version for version, _ in journal(restored)], [1, 2, 3, 4, 5, 6, 7, 8]
        )
        self.assertEqual(len(backups(restored)), 1)

    def test_second_run_at_latest_version_does_not_create_copy(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine)
        first = backups(self.path)
        self.assertEqual(len(first), 1)

        run_migrations(engine)

        self.assertEqual(backups(self.path), first)

    def test_temp_file_failure_is_reported_as_backup_error(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)

        with patch("migrations.tempfile.mkstemp", side_effect=OSError("нет места")):
            with self.assertRaises(BackupError) as caught:
                run_migrations(engine)

        self.assertIn("копи", str(caught.exception).lower())
        connection = connect(self.path)
        try:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("places", tables)
        self.assertNotIn("schema_migrations", tables)

    def test_backup_failure_message_is_explicit(self) -> None:
        create_old_schema(self.path)
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        with patch(
            "migrations.create_database_backup",
            side_effect=MigrationError("нельзя продолжить без копии"),
        ):
            with self.assertRaises(MigrationError) as caught:
                run_migrations(engine)
        self.assertIn("копи", str(caught.exception).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
