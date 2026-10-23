# tests/test_stage23_template_schema.py
"""
Stage 23: настоящие ограничения SQLite для шаблонов, версий и снимков.

Временный файл, миграции 1–8, реальные UNIQUE/CHECK/FK/триггеры. Проверяется,
что историю нельзя переписать, стандарт нельзя удалить, а привязка живёт по
постоянному brick_id. Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from migrations import LATEST_VERSION
from models import Place, Template, TemplateVersion
from tests.stage19a_helpers import connect
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    TemplateDatabaseTestCase,
)


class TemplateSchemaTests(TemplateDatabaseTestCase):
    """Схема миграции 8: таблицы, индексы, триггеры и backfill."""

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

    def _template_id_for_place(self, place_id: int) -> int:
        row = self.session.execute(
            text(
                "SELECT id FROM templates "
                "WHERE kind = 'standard' AND place_id = :pid"
            ),
            {"pid": place_id},
        ).one()
        return int(row[0])

    # --- журнал миграций ---------------------------------------------------

    def test_migration_eight_applies_and_is_idempotent(self) -> None:
        self.assertEqual(self.migrations_applied, LATEST_VERSION)
        self.assertEqual(LATEST_VERSION, 8)
        self.assertIn((8, "templates_versions_snapshots"), self.journal())

        tables = set(self.engine.dialect.get_table_names(self.engine.connect()))
        self.assertTrue(
            {
                "templates",
                "template_versions",
                "template_version_bricks",
                "template_snapshots",
                "template_snapshot_bricks",
            }
            <= tables
        )

    def test_place_table_names_and_schema_journal(self) -> None:
        connection = connect(self.path)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' " "ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
        names = {str(row[0]) for row in rows}
        self.assertTrue(
            {
                "trg_templates_identity_immutable",
                "trg_templates_revision_increases",
                "trg_templates_standard_no_delete",
                "trg_template_versions_no_update",
                "trg_template_versions_no_delete",
                "trg_template_version_bricks_no_update",
                "trg_template_version_bricks_no_delete",
                "trg_template_snapshots_no_update",
                "trg_template_snapshots_no_delete",
                "trg_template_snapshot_bricks_no_update",
                "trg_template_snapshot_bricks_no_delete",
            }
            <= names
        )

    # --- backfill стандартных шаблонов -------------------------------------

    def test_existing_places_get_standard_template_with_builtin_version(
        self,
    ) -> None:
        place = self.make_place("Старое кафе", created_by_user_id=None)
        template_id = self._template_id_for_place(place.id)

        row = self.session.execute(
            text(
                "SELECT header, body, footer, builtin_version "
                "FROM template_versions WHERE template_id = :tid"
            ),
            {"tid": template_id},
        ).one()
        self.assertEqual(row[0], "[название]")
        self.assertIn("[дата]", str(row[1]))
        self.assertIn("[описание]", str(row[1]))
        self.assertEqual(row[3], 1)

        bricks = self.session.execute(
            text(
                "SELECT brick_id FROM template_version_bricks "
                "WHERE template_version_id = ("
                "SELECT id FROM template_versions WHERE template_id = :tid)"
                " ORDER BY brick_id"
            ),
            {"tid": template_id},
        ).fetchall()
        self.assertEqual([int(item[0]) for item in bricks], [1, 2, 3, 4, 5, 6, 7, 8, 9])

    # --- ограничения формы шаблона ----------------------------------------

    def test_unique_standard_per_place_partial_index(self) -> None:
        place = self.make_place("Первое место")

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "INSERT INTO templates (kind, place_id, owner_user_id, "
                    "name, revision, created_at, updated_at) VALUES "
                    "('standard', :pid, NULL, NULL, 1, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"pid": place.id},
            )
        self.session.rollback()

        # Личный шаблон не попадает в частичный индекс стандарта.
        self.session.execute(
            text(
                "INSERT INTO templates (kind, place_id, owner_user_id, "
                "name, revision, created_at, updated_at) VALUES "
                "('personal', NULL, :owner, :name, 1, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {
                "owner": self.make_user(OWNER_TG).id,
                "name": "Личный",
            },
        )
        self.session.commit()
        rows = self.session.execute(
            text("SELECT id, kind FROM templates ORDER BY id")
        ).fetchall()
        self.assertEqual([str(row[1]) for row in rows], ["standard", "personal"])

    def test_templates_shape_check_forbids_broken_kinds(self) -> None:
        user = self.make_user(OWNER_TG)
        cases: list[dict[str, Any]] = [
            {"kind": "standard", "place_id": None},
            {
                "kind": "standard",
                "place_id": self.make_place("Сломанный стандарт").id,
                "owner_user_id": user.id,
            },
            {"kind": "personal", "owner_user_id": user.id, "name": ""},
            {"kind": "personal", "owner_user_id": user.id, "name": "   "},
        ]
        for case in cases:
            with self.subTest(case=case):
                values: dict[str, Any] = {
                    "kind": "personal",
                    "place_id": None,
                    "owner_user_id": None,
                    "name": "Личный",
                    "revision": 1,
                }
                values.update(case)
                with self.assertRaises((IntegrityError, OperationalError)):
                    self.session.execute(
                        text(
                            "INSERT INTO templates (kind, place_id, "
                            "owner_user_id, name, revision, created_at, "
                            "updated_at) VALUES (:kind, :place_id, "
                            ":owner_user_id, :name, :revision, "
                            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ),
                        values,
                    )
                self.session.rollback()

    # --- неизменяемость и ревизии -----------------------------------------

    def test_template_identity_and_revision_triggers(self) -> None:
        place = self.make_place("Триггерное место")
        template_id = self._template_id_for_place(place.id)
        other_place = self.make_place("Другое место")

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text("UPDATE templates SET place_id = :other WHERE id = :tid"),
                {"other": other_place.id, "tid": template_id},
            )
        self.session.rollback()

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "UPDATE templates SET kind = 'personal', "
                    "owner_user_id = :owner WHERE id = :tid"
                ),
                {"owner": self.make_user(OWNER_TG).id, "tid": template_id},
            )
        self.session.rollback()

        # Ревизия должна только расти.
        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text("UPDATE templates SET revision = 1 WHERE id = :tid"),
                {"tid": template_id},
            )
        self.session.rollback()
        self.session.execute(
            text("UPDATE templates SET revision = 2 WHERE id = :tid"),
            {"tid": template_id},
        )
        self.session.commit()
        row = self.session.get(Template, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)

    def test_versions_and_bindings_are_immutable(self) -> None:
        place = self.make_place("История место")
        template_id = self._template_id_for_place(place.id)
        version_row = self.session.execute(
            text(
                "SELECT id, header FROM template_versions "
                "WHERE template_id = :tid ORDER BY version"
            ),
            {"tid": template_id},
        ).one()
        version_id, original_header = int(version_row[0]), str(version_row[1])

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "UPDATE template_versions SET header = 'подмена' " "WHERE id = :vid"
                ),
                {"vid": version_id},
            )
        self.session.rollback()

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text("DELETE FROM template_versions WHERE id = :vid"),
                {"vid": version_id},
            )
        self.session.rollback()

        binding = self.session.execute(
            text(
                "SELECT template_version_id, brick_id "
                "FROM template_version_bricks WHERE template_version_id = :vid "
                "ORDER BY brick_id LIMIT 1"
            ),
            {"vid": version_id},
        ).one()

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "UPDATE template_version_bricks SET brick_id = 9 "
                    "WHERE template_version_id = :vid AND brick_id = :bid"
                ),
                {"vid": binding[0], "bid": binding[1]},
            )
        self.session.rollback()

        refreshed = self.session.get(TemplateVersion, version_id)
        assert refreshed is not None
        self.assertEqual(refreshed.header, original_header)

    def test_standard_template_cannot_be_deleted_directly(self) -> None:
        place = self.make_place("Удаление стандарта")
        template_id = self._template_id_for_place(place.id)
        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text("DELETE FROM templates WHERE id = :tid"),
                {"tid": template_id},
            )
        self.session.rollback()
        self.assertIsNotNone(self.session.get(Template, template_id))

    def test_deleting_place_cascades_template_and_versions(self) -> None:
        place = self.make_place("Каскадное место")
        template_id = self._template_id_for_place(place.id)
        self.session.delete(place)
        self.session.commit()

        self.assertIsNone(self.session.get(Place, place.id))
        self.assertIsNone(self.session.get(Template, template_id))
        count = self.session.execute(
            text("SELECT count(*) FROM template_versions " "WHERE template_id = :tid"),
            {"tid": template_id},
        ).scalar()
        self.assertEqual(count, 0)

    def test_snapshots_are_immutable_and_idempotency_unique(self) -> None:
        from template_store import create_template_snapshot

        user = self.make_user(OWNER_TG)
        place = self.make_place("Снимочное место")
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self._template_id_for_place(place.id),
            idempotency_key="первый-снимок",
        )

        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "UPDATE template_snapshots SET header = 'подмена' "
                    "WHERE id = :sid"
                ),
                {"sid": snapshot.snapshot_id},
            )
        self.session.rollback()
        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text("DELETE FROM template_snapshots WHERE id = :sid"),
                {"sid": snapshot.snapshot_id},
            )
        self.session.rollback()

        # Ключ идемпотентности не может быть пустым или только пробелами.
        with self.assertRaises((IntegrityError, OperationalError)):
            self.session.execute(
                text(
                    "INSERT INTO template_snapshots (owner_user_id, "
                    "source_template_id, source_template_version, source_kind, "
                    "header, body, footer, idempotency_key, created_at) "
                    "VALUES (:owner, :tid, 1, 'standard', 'x', 'y', 'z', "
                    "'   ', CURRENT_TIMESTAMP)"
                ),
                {"owner": user.id, "tid": snapshot.source_template_id},
            )
        self.session.rollback()

    def test_personal_template_versions_can_share_place_ids_in_snapshots(
        self,
    ) -> None:
        """Снимок личного шаблона не требует места; FK источника работает."""
        from template_store import create_template_snapshot

        owner = self.make_user(OWNER_TG)
        self.make_user(OTHER_TG)
        personal = self.make_personal_template(owner, "Личный снимок")
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=personal.template_id,
        )
        self.assertIsNone(snapshot.source_place_id)
        self.assertEqual(snapshot.source_kind, "personal")
        self.assertEqual(snapshot.source_owner_user_id, owner.id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
