# tests/test_stage19a_migrations.py
"""
Stage 19A: нумерованные миграции SQLite.

Тесты работают только с временными файлами. Настоящая post_bot.db не
открывается на запись; отдельный тест сверяет её отпечаток до и после
полной инициализации временной базы.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import create_engine

import database
from migrations import (
    DEFAULT_MIGRATIONS,
    LATEST_VERSION,
    IncompatibleSchemaError,
    Migration,
    MigrationError,
    run_migrations,
)
from tests.stage19a_helpers import (
    OLD_DRAFT,
    OLD_WIP_PAYLOAD,
    REAL_DB_PATH,
    connect,
    create_old_schema,
    make_engine,
    run_init_db_subprocess,
    table_names,
    temp_db_path,
)


def _journal(path: str) -> list[tuple[int, str]]:
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


def _sha256(path) -> str | None:
    """Отпечаток файла; None, если файла нет (чистый CI-чекаут)."""
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MigrationMechanismTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path()
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def test_new_database_gets_schema_and_journal(self) -> None:
        version = run_migrations(self.engine)

        self.assertEqual(version, LATEST_VERSION)
        tables = table_names(self.path)
        self.assertTrue({"users", "places", "place_names"} <= tables)
        self.assertIn("schema_migrations", tables)
        self.assertEqual(
            _journal(self.path),
            [
                (1, "users_places_place_names"),
                (2, "place_contacts_and_ui_state"),
            ],
        )

    def test_second_run_is_idempotent(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        # Повторный запуск не падает на CREATE TABLE (миграции не применяются
        # заново) и не дублирует записи журнала.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(len(_journal(self.path)), len(DEFAULT_MIGRATIONS))

    def test_restart_reconnects_to_same_schema_version(self) -> None:
        run_migrations(self.engine)
        self.engine.dispose()

        restarted = make_engine(self.path)
        self.addCleanup(restarted.dispose)
        self.assertEqual(run_migrations(restarted), LATEST_VERSION)
        self.assertEqual(len(_journal(self.path)), len(DEFAULT_MIGRATIONS))

    def test_old_schema_drafts_survive_migration(self) -> None:
        create_old_schema(self.path)

        run_migrations(self.engine)

        connection = connect(self.path)
        try:
            row = connection.execute(
                "SELECT title, date, time_start, time_end, place_name, "
                "place_url, text, contact, image, photos, formats, created_at "
                "FROM drafts WHERE id = ?",
                (OLD_DRAFT["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        for key in (
            "title",
            "date",
            "time_start",
            "time_end",
            "place_name",
            "place_url",
            "text",
            "contact",
            "image",
            "photos",
            "formats",
            "created_at",
        ):
            self.assertEqual(row[key], OLD_DRAFT[key], key)

    def test_old_wip_survives_migration(self) -> None:
        create_old_schema(self.path)

        run_migrations(self.engine)

        connection = connect(self.path)
        try:
            row = connection.execute(
                "SELECT screen_message_id, payload FROM user_states "
                "WHERE user_id = ?",
                (OLD_DRAFT["user_id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["screen_message_id"], 3781)
        self.assertEqual(json.loads(row["payload"]), OLD_WIP_PAYLOAD)

    def test_full_init_on_empty_database(self) -> None:
        run_init_db_subprocess(self.path)

        tables = table_names(self.path)
        self.assertTrue(
            {"users", "places", "place_names", "drafts", "user_states"} <= tables
        )
        self.assertEqual(
            _journal(self.path),
            [
                (1, "users_places_place_names"),
                (2, "place_contacts_and_ui_state"),
            ],
        )

        # Повторный старт (как рестарт процесса) не переприменяет миграции.
        run_init_db_subprocess(self.path)
        self.assertEqual(len(_journal(self.path)), 2)

    def test_full_init_upgrades_ancient_drafts_columns(self) -> None:
        create_old_schema(self.path, ancient=True)

        run_init_db_subprocess(self.path)

        connection = connect(self.path)
        try:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(drafts)")
            }
            row = connection.execute(
                "SELECT title, date, text, photos, formats, updated_at "
                "FROM drafts WHERE id = ?",
                (OLD_DRAFT["id"],),
            ).fetchone()
            payload = connection.execute(
                "SELECT payload FROM user_states WHERE user_id = ?",
                (OLD_DRAFT["user_id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertTrue({"photos", "formats", "updated_at"} <= columns)
        self.assertEqual(row["title"], OLD_DRAFT["title"])
        self.assertEqual(row["date"], OLD_DRAFT["date"])
        self.assertEqual(row["text"], OLD_DRAFT["text"])
        self.assertIsNone(row["photos"])  # старые записи не переписываются
        self.assertIsNone(row["formats"])
        self.assertEqual(json.loads(payload["payload"]), OLD_WIP_PAYLOAD)

    def test_failed_run_rolls_back_all_pending_migrations(self) -> None:
        good = Migration(1, "good", ("CREATE TABLE t_good (id INTEGER PRIMARY KEY)",))
        bad = Migration(
            2,
            "bad",
            (
                "CREATE TABLE t_bad (id INTEGER PRIMARY KEY)",
                "INSERT INTO missing_table (x) VALUES (1)",
            ),
        )

        with self.assertRaises(MigrationError) as caught:
            run_migrations(self.engine, [good, bad])
        self.assertIn("2", str(caught.exception))
        self.assertIn("bad", str(caught.exception))

        tables = table_names(self.path)
        self.assertNotIn("t_good", tables)
        self.assertNotIn("t_bad", tables)
        if "schema_migrations" in tables:
            self.assertEqual(_journal(self.path), [])

        fixed = Migration(2, "bad", ("CREATE TABLE t_bad (id INTEGER PRIMARY KEY)",))
        self.assertEqual(run_migrations(self.engine, [good, fixed]), 2)
        self.assertEqual([version for version, _ in _journal(self.path)], [1, 2])
        self.assertTrue({"t_good", "t_bad"} <= table_names(self.path))

    def test_failure_keeps_earlier_committed_versions(self) -> None:
        first = Migration(1, "first", ("CREATE TABLE t_first (id INTEGER)",))
        second_bad = Migration(
            2,
            "second",
            ("CREATE TABLE t_second (id INTEGER)", "INSERT INTO nope VALUES (1)"),
        )
        self.assertEqual(run_migrations(self.engine, [first]), 1)

        with self.assertRaises(MigrationError):
            run_migrations(self.engine, [first, second_bad])

        self.assertEqual([version for version, _ in _journal(self.path)], [1])
        self.assertNotIn("t_second", table_names(self.path))

        second_fixed = Migration(2, "second", ("CREATE TABLE t_second (id INTEGER)",))
        self.assertEqual(run_migrations(self.engine, [first, second_fixed]), 2)
        self.assertEqual([version for version, _ in _journal(self.path)], [1, 2])

    def test_unknown_newer_version_is_incompatible(self) -> None:
        run_migrations(self.engine)
        connection = connect(self.path)
        try:
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum) "
                "VALUES (999, 'future', 'unknown')"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine)
        self.assertIn("999", str(caught.exception))

    def test_journal_gap_is_incompatible(self) -> None:
        first = Migration(1, "first", ("CREATE TABLE g1 (id INTEGER)",))
        second = Migration(2, "second", ("CREATE TABLE g2 (id INTEGER)",))
        run_migrations(self.engine, [first, second])

        connection = connect(self.path)
        try:
            connection.execute("DELETE FROM schema_migrations WHERE version = 1")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine, [first, second])
        self.assertIn("непрерывн", str(caught.exception))

    def test_edited_applied_migration_is_incompatible(self) -> None:
        first = Migration(1, "first", ("CREATE TABLE c1 (id INTEGER)",))
        run_migrations(self.engine, [first])

        edited = Migration(1, "first", ("CREATE TABLE c1 (id INTEGER, extra TEXT)",))
        with self.assertRaises(IncompatibleSchemaError) as caught:
            run_migrations(self.engine, [edited])
        self.assertIn("контрольная сумма", str(caught.exception).lower())

    def test_non_file_sqlite_is_rejected(self) -> None:
        memory = create_engine("sqlite:///:memory:")
        self.addCleanup(memory.dispose)

        with self.assertRaises(MigrationError) as caught:
            run_migrations(memory)
        self.assertIn("файлов", str(caught.exception).lower())

    def test_concurrent_migration_runs_are_safe(self) -> None:
        def run_once(_: int) -> int:
            return run_migrations(self.engine)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(run_once, range(6)))

        self.assertEqual(results, [LATEST_VERSION] * 6)
        self.assertEqual(len(_journal(self.path)), len(DEFAULT_MIGRATIONS))
        self.assertTrue({"users", "places", "place_names"} <= table_names(self.path))

    def test_real_database_is_not_configured_or_touched(self) -> None:
        configured_path = database.engine.url.database
        self.assertIsNotNone(configured_path)
        self.assertNotEqual(
            os.path.realpath(str(configured_path)),
            os.path.realpath(str(REAL_DB_PATH)),
        )
        self.assertIn("poster-test-db-", str(configured_path))

        before_hash = _sha256(REAL_DB_PATH)
        before_stat = REAL_DB_PATH.stat() if REAL_DB_PATH.exists() else None

        temp_path = temp_db_path()
        try:
            run_init_db_subprocess(temp_path)
        finally:
            shutil.rmtree(os.path.dirname(temp_path), True)

        self.assertEqual(_sha256(REAL_DB_PATH), before_hash)
        if before_stat is not None:
            after_stat = REAL_DB_PATH.stat()
            self.assertEqual(after_stat.st_size, before_stat.st_size)
            self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
