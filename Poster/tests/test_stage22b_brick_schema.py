# tests/test_stage22b_brick_schema.py
"""
Stage 22B: ограничения SQLite для управления личными кирпичиками.

Проверяются миграция 7, ревизия, журнал сессий и запрет отката ревизии.
Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import sqlite3
import unittest

from migrations import LATEST_VERSION, run_migrations
from tests.stage19a_helpers import connect
from tests.stage22a_helpers import BrickDatabaseTestCase


class BrickManagementSchemaTests(BrickDatabaseTestCase):
    def journal(self) -> list[tuple[int, str]]:
        connection = connect(self.path)
        try:
            return [
                (int(row["version"]), str(row["name"]))
                for row in connection.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                )
            ]
        finally:
            connection.close()

    def test_migration_seven_has_revision_and_sessions(self) -> None:
        # Миграции 7 — эпоха управления кирпичиками; 15/16 добавлены этапом
        # Poster 2.0 S1, 17 — S5, поэтому «последняя» проверяется по
        # LATEST_VERSION.
        self.assertEqual(LATEST_VERSION, 17)
        self.assertEqual(self.journal()[-1], (17, "archive_legacy_drafts"))
        connection = connect(self.path)
        try:
            brick_columns = {
                str(row["name"]): row
                for row in connection.execute("PRAGMA table_info(bricks)")
            }
            self.assertIn("revision", brick_columns)
            self.assertEqual(int(brick_columns["revision"]["notnull"]), 1)
            self.assertEqual(str(brick_columns["revision"]["dflt_value"]), "1")
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(brick_sessions)")
            }
            self.assertEqual(
                session_columns,
                {"telegram_user_id", "payload", "updated_at"},
            )
            triggers = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertIn("trg_bricks_revision_increases", triggers)
            self.assertIn("trg_bricks_reserve_codes_insert", triggers)
        finally:
            connection.close()

    def test_revision_trigger_allows_only_strict_growth(self) -> None:
        connection = connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE bricks SET revision=1 WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE bricks SET revision=0 WHERE id=1")
            connection.execute("UPDATE bricks SET revision=2 WHERE id=1")
            connection.execute("UPDATE bricks SET revision=3 WHERE id=1")
            row = connection.execute(
                "SELECT revision FROM bricks WHERE id=1"
            ).fetchone()
            self.assertEqual(int(row["revision"]), 3)
        finally:
            connection.close()

    def test_brick_sessions_payload_must_be_json_object(self) -> None:
        connection = connect(self.path)
        try:
            connection.execute(
                "INSERT INTO brick_sessions "
                "(telegram_user_id, payload, updated_at) "
                "VALUES (1, '{}', '2026-01-01 00:00:00')"
            )
            connection.execute(
                "UPDATE brick_sessions SET payload=? WHERE telegram_user_id=1",
                ('{"phase":"hub","page":0}',),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brick_sessions SET payload='[]' WHERE telegram_user_id=1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE brick_sessions SET payload='not json' "
                    "WHERE telegram_user_id=1"
                )
        finally:
            connection.close()

    def test_archived_system_code_stays_reserved_at_database_level(self) -> None:
        connection = connect(self.path)
        try:
            connection.execute("UPDATE bricks SET status='archived' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO bricks "
                    "(code, title, type, scope, source, status, revision, "
                    "created_at) VALUES "
                    "('название', 'Новая', 'short_text', 'system', 'ask', "
                    "'active', 1, '2026-01-01 00:00:00')"
                )
        finally:
            connection.close()

    def test_restart_and_repeated_migration_are_idempotent(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(len(self.journal()), LATEST_VERSION)
        self.assertEqual(self.journal()[-1], (17, "archive_legacy_drafts"))


if __name__ == "__main__":
    unittest.main()
