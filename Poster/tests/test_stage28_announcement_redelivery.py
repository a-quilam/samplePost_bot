# tests/test_stage28_announcement_redelivery.py
"""
Stage 28: миграция 14 — проверяемая замена доставленного анонса.

Проверяется только временная SQLite. Миграции 1–13 не меняются, версия 14
добавляет ровно один разрешённый переход `sent -> sending` с явным
переносом адресов прошлой версии; всё остальное по-прежнему запрещено.
Копия базы перед миграцией делается через Backup API (видит WAL).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from migrations import (
    DEFAULT_MIGRATIONS,
    LATEST_VERSION,
    BackupError,
    Migration,
    MigrationError,
    run_migrations,
)
from tests.stage19a_helpers import (
    OLD_DRAFT,
    OLD_WIP_PAYLOAD,
    connect,
    create_old_schema,
    make_engine,
    temp_db_path,
)

# Контрольные суммы миграций 1–13 на момент Stage 28. Любое изменение
# применённой миграции запрещено: правки живут только в новой версии.
CHECKSUMS_1_13 = (
    "d33c1e23cddb66603110c837e1c7995b90a9d9ab0c5121e3f8b044ba8a4b8104",
    "b2ea9aa221dc1f87abe0d49b2a788f13530f8f71a11486376f9e69e6e219bf14",
    "4bb747c3c4358886f42c9a043fc25dacd50f77dad50a3c787391c7974dc97d88",
    "5d5a4dffb273850e6b971302c9a2aaed0b2e8d120f07fe422ae04503334bc002",
    "7bf1c92fe209b86748bb574c3e8559ede066a34b109314e48fad5df10dd2839d",
    "0d385a8b4a7a7ab0cf35c450214b510b15e7bb4cbc650abca77adf2a9deb46a8",
    "16c694b4d73cf524e9d9bf880aebf2ba953dee85bec7917528ee4f6cc3316f94",
    "5ccd8ef0d28b402a55e04b0f5cb0041a03bc6e9bb4ff289b6d5a86c0c84e1ade",
    "f76c82096f0f6d1fc04391ac8e680ee4864818128cb90ad9ea86d484da5289e4",
    "10db4ccda3015aa5dbc296da6f6b2a030593911044c44cbad4f39a6adae6ed0f",
    "4c6c37c6624d7a824263afc910e984299b6c2ef0eb5290d30867175690291ed0",
    "f6c5279686aaf0588c2c7ed815d42dc21e508b4dfd87dbd5d8e98041420cd979",
    "e610dde59357d3c0a169edbf0e64f3691b2a40dd10e63606ffc529c403d70426",
)

SENT_PROGRESS = {"fingerprint": "fp-old", "step": 1, "ids": [501, 502], "total": 1}


def _journal(path: str) -> list[tuple[int, str, str]]:
    connection = connect(path)
    try:
        return [
            (int(row["version"]), str(row["name"]), str(row["checksum"]))
            for row in connection.execute(
                "SELECT version, name, checksum FROM schema_migrations "
                "ORDER BY version"
            )
        ]
    finally:
        connection.close()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_signature(path: str) -> list[tuple[str, str, str]]:
    connection = connect(path)
    try:
        return [
            (str(row["type"]), str(row["name"]), str(row["sql"]))
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
    finally:
        connection.close()


class AnnouncementRedeliveryMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-28-redelivery-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def test_migrations_one_to_seventeen_keep_legacy_data_and_checksums(self) -> None:
        create_old_schema(self.path)

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(LATEST_VERSION, 24)

        journal = _journal(self.path)
        self.assertEqual(
            [(version, name) for version, name, _ in journal],
            [(migration.version, migration.name) for migration in DEFAULT_MIGRATIONS],
        )
        # Записанные контрольные суммы — ровно код текущих миграций.
        self.assertEqual(
            [checksum for _, _, checksum in journal],
            [migration.checksum for migration in DEFAULT_MIGRATIONS],
        )
        # 1–13 не изменились даже побайтно по контрольной сумме DDL.
        self.assertEqual(
            tuple(checksum for _, _, checksum in journal[:13]), CHECKSUMS_1_13
        )
        # 14–17 — ровно код текущих миграций: ни переписаны, ни подменены.
        self.assertEqual(
            [checksum for _, _, checksum in journal[13:]],
            [migration.checksum for migration in DEFAULT_MIGRATIONS[13:]],
        )

        connection = connect(self.path)
        try:
            # S5: `drafts` переименована миграцией 17 — прежняя строка
            # читается из архива без потерь.
            draft = connection.execute(
                "SELECT * FROM legacy_drafts_backup WHERE id = ?",
                (OLD_DRAFT["id"],),
            ).fetchone()
            state = connection.execute(
                "SELECT payload FROM user_states WHERE user_id = ?",
                (OLD_DRAFT["user_id"],),
            ).fetchone()
        finally:
            connection.close()
        assert draft is not None and state is not None
        self.assertEqual(draft["title"], OLD_DRAFT["title"])
        self.assertEqual(draft["photos"], OLD_DRAFT["photos"])
        self.assertEqual(draft["formats"], OLD_DRAFT["formats"])
        self.assertEqual(json.loads(state["payload"]), OLD_WIP_PAYLOAD)

        # Копия — состояние ДО миграции, а не после.
        copies = sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))
        self.assertEqual(len(copies), 1, copies)
        self.assertGreater(copies[0].stat().st_size, 0)
        self.assertEqual(copies[0].stat().st_mode & 0o777, 0o600)
        # Копия — другое состояние, чем мигрированная база.
        self.assertNotEqual(_sha256(copies[0]), _sha256(Path(self.path)))
        connection = connect(str(copies[0]))
        try:
            names = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
        self.assertNotIn("announcement_drafts", names)
        self.assertNotIn("schema_migrations", names)

    def test_migration_fourteen_does_not_change_columns_or_indexes(self) -> None:
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:13]), 13)
        before = _schema_signature(self.path)

        # Проверяется именно версия 14, поэтому список миграций усечён:
        # схему меняют также 15/16, и они не предмет этого теста.
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:14]), 14)
        after = _schema_signature(self.path)

        self.assertEqual(
            [item for item in before if item[0] != "trigger"],
            [item for item in after if item[0] != "trigger"],
        )
        # Меняется только один триггер.
        before_triggers = {name: sql for kind, name, sql in before if kind == "trigger"}
        after_triggers = {name: sql for kind, name, sql in after if kind == "trigger"}
        self.assertEqual(set(before_triggers), set(after_triggers))
        changed = {
            name
            for name in before_triggers
            if before_triggers[name] != after_triggers[name]
        }
        self.assertEqual(changed, {"trg_announcement_drafts_published_immutable"})
        self.assertIn(
            "previous_ids",
            after_triggers["trg_announcement_drafts_published_immutable"],
        )

    def test_failed_fourteen_rolls_back_and_keeps_old_trigger(self) -> None:
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:13]), 13)
        broken = Migration(
            version=14,
            name="announcement_redelivery",
            statements=(
                *DEFAULT_MIGRATIONS[13].statements,
                "INSERT INTO missing_table (x) VALUES (1)",
            ),
        )

        with self.assertRaises(MigrationError):
            run_migrations(self.engine, (*DEFAULT_MIGRATIONS[:13], broken))

        self.assertEqual(
            [version for version, _, _ in _journal(self.path)], list(range(1, 14))
        )
        triggers = {
            name: sql
            for kind, name, sql in _schema_signature(self.path)
            if kind == "trigger"
        }
        # Старый триггер восстановлен транзакцией: перехода из `sent` нет.
        self.assertNotIn(
            "previous_ids", triggers["trg_announcement_drafts_published_immutable"]
        )

        # Исправленная версия применяется поверх откатившейся попытки,
        # затем доводятся 15/16 текущего кода.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            [version for version, _, _ in _journal(self.path)],
            list(range(1, LATEST_VERSION + 1)),
        )


class AnnouncementRedeliveryTriggerTests(unittest.TestCase):
    """Триггер версии 14: ровно один проверяемый переход из `sent`."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-28-trigger-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        run_migrations(self.engine)
        self.connection = sqlite3.connect(self.path)
        self.addCleanup(self.connection.close)
        self.connection.execute("PRAGMA foreign_keys=OFF")
        self.connection.execute(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, 1001, '2026-01-01 00:00:00')"
        )
        self.connection.execute(
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (1, 1, '2026-01-01 00:00:00')"
        )
        self.connection.commit()
        self.draft_id = self._sent_draft()

    def _sent_draft(self) -> int:
        self.connection.execute(
            """
            INSERT INTO announcement_drafts (
                author_user_id, place_id, place_display_name, stage, revision,
                created_at, updated_at, publication_status, publication_text,
                publication_photos, publication_progress,
                publication_started_at, published_at
            ) VALUES (1, 1, 'Дом культуры', 'completed', 5,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'sent',
                'Готовый текст', '[]', ?, '2026-01-01 00:00:00',
                '2026-01-01 00:00:00')
            """,
            (json.dumps(SENT_PROGRESS),),
        )
        self.connection.commit()
        return int(
            self.connection.execute(
                "SELECT max(id) FROM announcement_drafts"
            ).fetchone()[0]
        )

    def _rebuild(self, *, previous: object | None, status: str = "sending") -> None:
        progress: dict[str, object] = {}
        if previous is not None:
            progress["previous_ids"] = previous
        self.connection.execute(
            """
            UPDATE announcement_drafts SET
                publication_status = ?,
                publication_progress = ?,
                published_at = NULL,
                publication_started_at = '2026-02-01 00:00:00',
                publication_error = NULL,
                revision = revision + 1
            WHERE id = ?
            """,
            (status, json.dumps(progress), self.draft_id),
        )
        self.connection.commit()

    def test_sent_can_rebuild_only_with_old_ids_in_previous_ids(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self._rebuild(previous=None)
        with self.assertRaises(sqlite3.IntegrityError):
            self._rebuild(previous=[999])
        with self.assertRaises(sqlite3.IntegrityError):
            # Старые id нельзя выдать за id текущей отправки.
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_progress = ? WHERE id = ?",
                (
                    json.dumps({**SENT_PROGRESS, "previous_ids": [501, 502]}),
                    self.draft_id,
                ),
            )
            self.connection.commit()

        self._rebuild(previous=[501, 502])
        row = self.connection.execute(
            "SELECT publication_status, published_at, revision, "
            "publication_progress FROM announcement_drafts WHERE id = ?",
            (self.draft_id,),
        ).fetchone()
        self.assertEqual(row[0], "sending")
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 6)
        self.assertEqual(json.loads(row[3])["previous_ids"], [501, 502])
        self.assertNotIn("ids", json.loads(row[3]))

    def test_other_transitions_from_sent_stay_forbidden(self) -> None:
        for status in ("failed", "partial", "sent"):
            with self.subTest(status=status):
                with self.assertRaises(sqlite3.IntegrityError):
                    self._rebuild(previous=[501, 502], status=status)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_text = 'Другое' "
                "WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET published_at = "
                "'2026-03-01 00:00:00' WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE announcement_drafts SET publication_progress = '{}' "
                "WHERE id = ?",
                (self.draft_id,),
            )
            self.connection.commit()


class AnnouncementRedeliveryBackupTests(unittest.TestCase):
    """Копия перед миграцией 14 видит зафиксированные данные из WAL."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-28-wal-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def test_wal_data_of_version_thirteen_is_backed_up_before_fourteen(self) -> None:
        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine, DEFAULT_MIGRATIONS[:13])

        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (7, 7007, '2026-01-01 00:00:00')"
        )
        writer.execute(
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (7, 7, '2026-01-01 00:00:00')"
        )
        writer.execute(
            """
            INSERT INTO announcement_drafts (
                author_user_id, place_id, place_display_name, stage, revision,
                created_at, updated_at, publication_status, publication_text,
                publication_photos, publication_progress,
                publication_started_at, published_at
            ) VALUES (7, 7, 'Место из WAL', 'completed', 2,
                '2026-01-01 00:00:00', '2026-01-01 00:00:00', 'sent',
                'Текст из WAL', '[]', ?, '2026-01-01 00:00:00',
                '2026-01-01 00:00:00')
            """,
            (json.dumps(SENT_PROGRESS),),
        )
        writer.commit()
        wal = Path(self.path + "-wal")
        self.assertTrue(wal.exists() and wal.stat().st_size > 0)

        # Копия делается перед первой ожидающей миграцией (14) и содержит
        # версию 13 — это проверяется ниже; текущий код доходит до 16.
        self.assertEqual(run_migrations(engine), LATEST_VERSION)

        copies = sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))
        self.assertEqual(len(copies), 1, copies)
        backup = sqlite3.connect(str(copies[0]))
        self.addCleanup(backup.close)
        row = backup.execute(
            "SELECT place_display_name, publication_status, publication_progress "
            "FROM announcement_drafts WHERE author_user_id = 7"
        ).fetchone()
        self.assertEqual(row[0], "Место из WAL")
        self.assertEqual(row[1], "sent")
        self.assertEqual(json.loads(row[2])["ids"], [501, 502])
        # Копия — версия 13: журнал ещё без 14.
        self.assertEqual(
            backup.execute("SELECT max(version) FROM schema_migrations").fetchone()[0],
            13,
        )
        # Новая база: строка сохранена и переход разрешён.
        with engine.begin() as connection:
            from sqlalchemy import text

            connection.execute(
                text(
                    "UPDATE announcement_drafts SET publication_status='sending', "
                    "publication_progress=:p, published_at=NULL, "
                    "publication_started_at='2026-02-01 00:00:00', "
                    "revision=revision+1 WHERE author_user_id=7"
                ),
                {"p": json.dumps({"previous_ids": [501, 502]})},
            )

    def test_backup_failure_blocks_fourteen(self) -> None:
        from unittest.mock import patch

        engine = make_engine(self.path)
        self.addCleanup(engine.dispose)
        run_migrations(engine, DEFAULT_MIGRATIONS[:13])

        with patch(
            "migrations.create_database_backup",
            side_effect=BackupError("имитация сбоя копирования"),
        ):
            with self.assertRaises(BackupError):
                run_migrations(engine)

        self.assertEqual(
            [version for version, _, _ in _journal(self.path)], list(range(1, 14))
        )
        triggers = {
            name: sql
            for kind, name, sql in _schema_signature(self.path)
            if kind == "trigger"
        }
        self.assertNotIn(
            "previous_ids", triggers["trg_announcement_drafts_published_immutable"]
        )


if __name__ == "__main__":
    unittest.main()
