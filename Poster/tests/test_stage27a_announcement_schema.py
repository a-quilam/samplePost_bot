# tests/test_stage27a_announcement_schema.py
"""
Stage 27A: схема отдельного состояния адаптивного мастера.

Проверяются миграция 12, независимые таблицы, ограничения/триггеры и
сохранность прежних данных: `drafts` уходит в архив миграцией 17 (S5),
`user_states` не трогается. Настоящая база не открывается.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from json import loads

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import UserState
from tests.stage19a_helpers import (
    OLD_DRAFT,
    OLD_WIP_PAYLOAD,
    create_old_schema,
    make_engine,
    temp_db_path,
)
from tests.stage27_helpers import AnnouncementDatabaseTestCase


class AnnouncementWizardMigrationTests(AnnouncementDatabaseTestCase):
    """Миграция 12 и ограничения новых таблиц."""

    def test_latest_migration_is_seventeen_and_follows_brick_proposals(self) -> None:
        self.assertEqual(LATEST_VERSION, 17)
        self.assertEqual(
            [(migration.version, migration.name) for migration in DEFAULT_MIGRATIONS],
            [
                (1, "users_places_place_names"),
                (2, "place_contacts_and_ui_state"),
                (3, "place_admins_and_admin_ui_state"),
                (4, "place_edit_log"),
                (5, "admin_transfers"),
                (6, "bricks_registry"),
                (7, "brick_management"),
                (8, "templates_versions_snapshots"),
                (9, "template_binding_codes"),
                (10, "template_sessions"),
                (11, "brick_proposals"),
                (12, "announcement_wizard"),
                (13, "announcement_photos_publication"),
                (14, "announcement_redelivery"),
                (15, "brick_description_and_address"),
                (16, "announcement_draft_origin"),
                (17, "archive_legacy_drafts"),
            ],
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

    def test_new_tables_columns_indexes_and_triggers_exist(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "announcement_drafts",
                    "announcement_values",
                    "announcement_extras",
                }
                <= tables
            )

            draft_columns = {
                str(row[1]): row
                for row in connection.execute("PRAGMA table_info(announcement_drafts)")
            }
            self.assertTrue(
                {
                    "id",
                    "author_user_id",
                    "place_id",
                    "place_name_id",
                    "place_display_name",
                    "place_address",
                    "place_url",
                    "template_id",
                    "template_version",
                    "template_snapshot_id",
                    "stage",
                    "revision",
                    "idempotency_key",
                    "photos",
                    "publication_status",
                    "publication_text",
                    "publication_photos",
                    "publication_progress",
                    "publication_error",
                    "publication_started_at",
                    "published_at",
                    "created_at",
                    "updated_at",
                }
                <= set(draft_columns)
            )
            self.assertEqual(int(draft_columns["revision"][4]), 1)
            self.assertEqual(str(draft_columns["stage"][4]), "'place'")
            self.assertEqual(int(draft_columns["place_address"][3]), 0)
            self.assertEqual(int(draft_columns["place_url"][3]), 0)

            value_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(announcement_values)")
            }
            self.assertEqual(
                value_columns,
                {
                    "id",
                    "draft_id",
                    "brick_id",
                    "state",
                    "value_json",
                    "created_at",
                    "updated_at",
                },
            )
            extra_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(announcement_extras)")
            }
            self.assertEqual(
                extra_columns,
                {
                    "id",
                    "draft_id",
                    "brick_id",
                    "section",
                    "position",
                    "occurrence",
                    "created_at",
                    "updated_at",
                },
            )

            indexes = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertTrue(
                {
                    "ix_announcement_drafts_author_updated",
                    "ux_announcement_drafts_snapshot",
                    "ux_announcement_drafts_idempotency",
                    "ix_announcement_drafts_publication_status",
                    "ix_announcement_values_brick",
                    "ix_announcement_extras_draft",
                    "ix_announcement_extras_brick",
                }
                <= indexes
            )
            triggers = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            }
            self.assertTrue(
                {
                    "trg_announcement_drafts_revision_increases",
                    "trg_announcement_drafts_identity_immutable",
                    "trg_announcement_drafts_template_immutable",
                    "trg_announcement_values_identity_immutable",
                    "trg_announcement_extras_identity_immutable",
                    "trg_announcement_drafts_media_json_insert",
                    "trg_announcement_drafts_media_json_update",
                    "trg_announcement_drafts_publication_status_insert",
                    "trg_announcement_drafts_publication_status_update",
                    "trg_announcement_drafts_publication_shape_insert",
                    "trg_announcement_drafts_publication_shape",
                    "trg_announcement_drafts_published_immutable",
                }
                <= triggers
            )
        finally:
            connection.close()

    def test_revision_must_increase_and_identity_cannot_change(self) -> None:
        draft = self.start_draft()
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET revision=revision-1 "
                        "WHERE id=:draft_id"
                    ),
                    {"draft_id": draft.draft_id},
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET author_user_id=:other_id "
                        "WHERE id=:draft_id"
                    ),
                    {"other_id": self.other.id, "draft_id": draft.draft_id},
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET place_id=:other_place_id "
                        "WHERE id=:draft_id"
                    ),
                    {
                        "other_place_id": self.place.id + 1,
                        "draft_id": draft.draft_id,
                    },
                )

        # Отображаемое название можно обновить только вместе с revision;
        # триггер защищает идентичность, а не вычисленный текст карточки.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE announcement_drafts "
                    "SET place_display_name='Другое', revision=revision+1 "
                    "WHERE id=:draft_id"
                ),
                {"draft_id": draft.draft_id},
            )

    def test_snapshot_and_value_identities_are_immutable(self) -> None:
        draft = self.start_at_questions()
        assert draft.template_snapshot is not None
        snapshot_id = draft.template_snapshot.snapshot_id

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts "
                        "SET template_snapshot_id=:snapshot_id+1, "
                        "revision=revision+1 WHERE id=:draft_id"
                    ),
                    {"snapshot_id": snapshot_id, "draft_id": draft.draft_id},
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO announcement_values "
                        "(draft_id, brick_id, state, value_json, created_at, "
                        "updated_at) VALUES "
                        "(:draft_id, 1, 'valid', 'not-json', "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    ),
                    {"draft_id": draft.draft_id},
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO announcement_extras "
                        "(draft_id, brick_id, section, position, occurrence, "
                        "created_at, updated_at) VALUES "
                        "(:draft_id, 2, 'middle', 'end', 1, "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    ),
                    {"draft_id": draft.draft_id},
                )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO announcement_values "
                        "(draft_id, brick_id, state, value_json, created_at, "
                        "updated_at) VALUES "
                        "(:draft_id, 1, 'empty_optional', '{}', "
                        "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                    ),
                    {"draft_id": draft.draft_id},
                )


class OldDraftCompatibilityTests(unittest.TestCase):
    """Старые черновики и WIP не конвертируются в анонс.

    Миграция 12 создаёт параллельные таблицы, а не переносит данные. В S5
    легаси-таблица `drafts` переименована миграцией 17: содержимое остаётся
    доступным в `legacy_drafts_backup` и в анонс не попадает.
    """

    def setUp(self) -> None:
        self.path = temp_db_path("poster-27a-old-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        create_old_schema(self.path)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def test_old_draft_and_wip_survive_without_content_migration(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.session.close)

        state = self.session.get(UserState, OLD_DRAFT["user_id"])
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(loads(state.payload or ""), OLD_WIP_PAYLOAD)

        with self.engine.connect() as connection:
            archived = connection.execute(
                text("SELECT title, image FROM legacy_drafts_backup WHERE id = :id"),
                {"id": OLD_DRAFT["id"]},
            ).fetchone()
            drafts_table = connection.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='drafts'"
                )
            ).fetchone()
            self.assertIsNone(drafts_table, "легаси-таблица переименована")
            self.assertIsNotNone(archived, "прежний черновик читается из архива")
            assert archived is not None
            self.assertEqual(archived[0], OLD_DRAFT["title"])
            self.assertEqual(archived[1], OLD_DRAFT["image"])
            self.assertEqual(
                connection.execute(
                    text("SELECT count(*) FROM announcement_drafts")
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    text("SELECT count(*) FROM announcement_values")
                ).scalar_one(),
                0,
            )
            self.assertEqual(
                connection.execute(
                    text("SELECT count(*) FROM announcement_extras")
                ).scalar_one(),
                0,
            )


if __name__ == "__main__":
    unittest.main()
