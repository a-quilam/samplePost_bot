# tests/test_stage22a_brick_schema.py
"""
Stage 22A: реальные ограничения SQLite для реестра кирпичиков.

Временный файл, миграции 1–6, настоящие UNIQUE/CHECK/FK/триггеры. Реальная
Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brick_store import list_system_bricks
from brick_types import SYSTEM_BRICKS
from migrations import LATEST_VERSION, run_migrations
from tests.stage19a_helpers import connect
from tests.stage22a_helpers import BrickDatabaseTestCase


class BrickSchemaTests(BrickDatabaseTestCase):
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

    def insert_brick(
        self,
        *,
        code: str = "пользовательский",
        title: str = "Пользовательское поле",
        value_type: str = "short_text",
        scope: str = "personal",
        source: str = "ask",
        place_id: int | None = None,
        owner_user_id: int | None = None,
        rules_json: str | None = None,
        status: str = "active",
        created_by_user_id: int | None = None,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO bricks
                        (code, title, type, scope, source, place_id,
                         owner_user_id, rules_json, status, created_by_user_id,
                         created_at)
                    VALUES
                        (:code, :title, :value_type, :scope, :source, :place_id,
                         :owner_user_id, :rules_json, :status,
                         :created_by_user_id, :created_at)
                    """),
                {
                    "code": code,
                    "title": title,
                    "value_type": value_type,
                    "scope": scope,
                    "source": source,
                    "place_id": place_id,
                    "owner_user_id": owner_user_id,
                    "rules_json": rules_json,
                    "status": status,
                    "created_by_user_id": created_by_user_id,
                    "created_at": "2026-10-23 12:00:00",
                },
            )

    def test_migrations_seed_exactly_ten_stable_system_bricks(self) -> None:
        self.assertEqual(
            self.journal(),
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
                (18, "builtin_templates_v3"),
                (19, "brick_code_conflicts"),
                (20, "brick_proposal_description"),
            ],
        )
        # 9 кирпичиков миграции 6 + системный `[адрес]` миграции 15
        # (Poster 2.0 S1). Каталог и БД обязаны совпадать по коду и порядку.
        definitions = list_system_bricks(self.session)
        self.assertEqual(len(definitions), 10)
        self.assertEqual([item.id for item in definitions], list(range(1, 11)))
        self.assertEqual(
            [item.code for item in definitions],
            [item.code for item in SYSTEM_BRICKS],
        )
        for actual, expected in zip(definitions, SYSTEM_BRICKS, strict=True):
            # На чистой базе миграция 15 вставляет `[адрес]` сразу за
            # миграцией 6, поэтому ID совпадают с каталогом. В существующей
            # базе номер мог быть другим (`MAX(id)+1`), поэтому основная
            # сверка — по коду.
            self.assertEqual(actual.code, expected.code)
            self.assertEqual(actual.title, expected.title)
            self.assertEqual(actual.value_type, expected.value_type)
            self.assertEqual(actual.source, expected.source)
            self.assertEqual(actual.rules, dict(expected.rules))
            self.assertEqual(actual.scope, "system")
            self.assertIsNone(actual.place_id)
            self.assertIsNone(actual.owner_user_id)
            self.assertIsNone(actual.created_by_user_id)

    def test_repeated_initialization_does_not_duplicate_or_renumber(self) -> None:
        before = [
            (item.id, item.code, item.title, item.created_at)
            for item in list_system_bricks(self.session)
        ]
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        after = [
            (item.id, item.code, item.title, item.created_at)
            for item in list_system_bricks(self.session)
        ]
        self.assertEqual(after, before)
        with self.engine.connect() as connection:
            count: int = int(
                connection.execute(
                    text("SELECT count(*) FROM bricks WHERE scope='system'")
                ).scalar_one()
            )
        self.assertEqual(count, 10)
        self.assertEqual(len(self.journal()), LATEST_VERSION)

    def test_restart_does_not_overwrite_a_controlled_system_definition(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE bricks SET title='Название (изменено)' WHERE id=1")
            )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.session.expire_all()
        changed = list_system_bricks(self.session)[0]
        self.assertEqual(changed.id, 1)
        self.assertEqual(changed.code, "название")
        self.assertEqual(changed.title, "Название (изменено)")

    def test_real_unique_indexes_are_per_scope_not_per_title(self) -> None:
        first_user = self.make_user(1001)
        second_user = self.make_user(1002)
        first_place = self.make_place("Первое место")
        second_place = self.make_place("Второе место")

        self.insert_brick(owner_user_id=first_user.id, code="цена")
        self.insert_brick(
            owner_user_id=first_user.id,
            code="другая",
            title="Цена",
        )
        self.insert_brick(owner_user_id=second_user.id, code="цена")
        self.insert_brick(scope="place", place_id=first_place.id, code="цена")
        self.insert_brick(scope="place", place_id=second_place.id, code="цена")

        with self.assertRaises(IntegrityError):
            self.insert_brick(owner_user_id=first_user.id, code="цена")
        with self.assertRaises(IntegrityError):
            self.insert_brick(scope="place", place_id=first_place.id, code="цена")
        with self.assertRaises(IntegrityError):
            self.insert_brick(
                scope="system", code="название", title="Ещё одно название"
            )

    def test_system_codes_are_globally_reserved_in_both_directions(self) -> None:
        user = self.make_user(1003)
        place = self.make_place("Место")
        for scope, place_id, owner in (
            ("personal", None, user.id),
            ("place", place.id, None),
        ):
            with self.subTest(scope=scope), self.assertRaises(IntegrityError):
                self.insert_brick(
                    scope=scope,
                    code="название",
                    place_id=place_id,
                    owner_user_id=owner,
                )

        self.insert_brick(owner_user_id=user.id, code="будущий_системный")
        with self.assertRaises(IntegrityError):
            self.insert_brick(scope="system", code="будущий_системный")

    def test_update_to_reserved_system_code_is_rejected(self) -> None:
        user = self.make_user(1004)
        self.insert_brick(owner_user_id=user.id, code="свободный")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE bricks SET code='название' " "WHERE code='свободный'")
                )

    def test_scope_owner_and_type_checks_are_real(self) -> None:
        user = self.make_user(1005)
        place = self.make_place("Место проверок")
        invalid_cases: list[dict[str, Any]] = [
            {"scope": "system", "owner_user_id": user.id},
            {"scope": "place", "owner_user_id": user.id},
            {"scope": "place", "place_id": place.id, "owner_user_id": user.id},
            {"scope": "personal"},
            {"scope": "personal", "place_id": place.id, "owner_user_id": user.id},
            {"scope": "unknown"},
            {"scope": "personal", "owner_user_id": user.id, "value_type": "any"},
            {"scope": "personal", "owner_user_id": user.id, "value_type": "photo"},
            {"scope": "personal", "owner_user_id": user.id, "value_type": "media"},
            {"scope": "personal", "owner_user_id": user.id, "source": "personal"},
            {"scope": "personal", "owner_user_id": user.id, "status": "draft"},
            {"scope": "personal", "owner_user_id": user.id, "code": "a"},
            {"scope": "personal", "owner_user_id": user.id, "code": "a" * 65},
            {"scope": "personal", "owner_user_id": user.id, "code": "a b"},
            {"scope": "personal", "owner_user_id": user.id, "code": "a[b"},
            {"scope": "personal", "owner_user_id": user.id, "code": "фото"},
            {"scope": "personal", "owner_user_id": user.id, "title": "  "},
            {"scope": "place", "place_id": 999999},
            {"scope": "personal", "owner_user_id": 999999},
        ]
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(IntegrityError):
                self.insert_brick(**values)

    def test_rules_json_must_be_a_valid_json_object(self) -> None:
        user = self.make_user(1006)
        valid = ('{"min": 0, "integer": true}',)
        self.insert_brick(
            owner_user_id=user.id,
            code="число",
            value_type="number",
            rules_json=valid[0],
        )
        for rules in ("{bad", "[]", "null", "1", '"text"'):
            with self.subTest(rules=rules), self.assertRaises(IntegrityError):
                self.insert_brick(
                    owner_user_id=user.id,
                    code=f"плохие_{abs(hash(rules))}",
                    value_type="short_text",
                    rules_json=rules,
                )

    def test_foreign_keys_cascade_and_creator_is_set_null(self) -> None:
        owner = self.make_user(1007)
        creator = self.make_user(1008)
        place = self.make_place("Место каскадов", created_by_user_id=creator.id)
        self.insert_brick(
            owner_user_id=owner.id,
            code="личный",
            created_by_user_id=creator.id,
        )
        self.insert_brick(
            scope="place",
            place_id=place.id,
            code="местный",
            created_by_user_id=creator.id,
        )

        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM users WHERE id=:id"), {"id": creator.id}
            )
        with self.engine.connect() as connection:
            creator_count: int | None = connection.execute(
                text("SELECT created_by_user_id FROM bricks WHERE code='местный'")
            ).scalar_one()
            personal_count: int = int(
                connection.execute(
                    text("SELECT count(*) FROM bricks WHERE code='личный'")
                ).scalar_one()
            )
        self.assertIsNone(creator_count)
        self.assertEqual(personal_count, 1)

        with self.engine.begin() as connection:
            connection.execute(text("DELETE FROM users WHERE id=:id"), {"id": owner.id})
            connection.execute(
                text("DELETE FROM places WHERE id=:id"), {"id": place.id}
            )
        with self.engine.connect() as connection:
            remaining: int = int(
                connection.execute(
                    text(
                        "SELECT count(*) FROM bricks "
                        "WHERE code IN ('личный', 'местный')"
                    )
                ).scalar_one()
            )
        self.assertEqual(remaining, 0)

    def test_system_rows_are_immutable_and_cannot_be_deleted(self) -> None:
        operations = [
            "DELETE FROM bricks WHERE id=1",
            "UPDATE bricks SET id=101 WHERE id=1",
            "UPDATE bricks SET code='переименовано' WHERE id=1",
            "UPDATE bricks SET scope='personal', owner_user_id=1 WHERE id=1",
            "UPDATE bricks SET place_id=1 WHERE id=1",
        ]
        for statement in operations:
            with self.subTest(statement=statement), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(text(statement))

    def test_id_is_immutable_for_ordinary_bricks_too(self) -> None:
        user = self.make_user(1009)
        self.insert_brick(owner_user_id=user.id, code="обычный")
        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text("UPDATE bricks SET id=500 WHERE code='обычный'")
                )

    def test_archived_system_code_stays_reserved(self) -> None:
        user = self.make_user(1010)
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE bricks SET status='archived' WHERE id=1"))
        with self.assertRaises(IntegrityError):
            self.insert_brick(owner_user_id=user.id, code="название")

    def test_availability_search_indexes_and_trigger_names_exist(self) -> None:
        connection = connect(self.path)
        try:
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='bricks'"
                )
            }
            triggers = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='bricks'"
                )
            }
            plan = connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT id FROM bricks WHERE scope='system' AND status='active'"
            ).fetchall()
        finally:
            connection.close()
        self.assertTrue(
            {
                "ux_bricks_system_code",
                "ux_bricks_place_code",
                "ux_bricks_personal_code",
                "ix_bricks_scope_status_code",
                "ix_bricks_place_status",
                "ix_bricks_owner_status",
                "ix_bricks_type_status",
                "ix_bricks_code",
                "ix_bricks_title",
            }
            <= indexes
        )
        self.assertTrue(
            {
                "trg_bricks_id_immutable",
                "trg_bricks_system_code_immutable",
                "trg_bricks_system_owner_immutable",
                "trg_bricks_system_no_delete",
                "trg_bricks_reserve_codes_insert",
                "trg_bricks_reserve_codes_update",
            }
            <= triggers
        )
        self.assertIn(
            "ix_bricks_scope_status_code", " ".join(str(row["detail"]) for row in plan)
        )

    def test_legacy_drafts_table_gone_and_no_brick_columns(self) -> None:
        """S5: `drafts` удалена из моделей; кирпичики живут отдельной схемой."""
        with self.engine.connect() as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        self.assertIn("bricks", tables)
        self.assertNotIn("drafts", tables, "легаси-таблицы нет в живой схеме")
        self.assertNotIn("legacy_drafts_backup", tables, "свежая база: архива нет")
        self.assertNotIn("draft_values", tables)
        self.assertIn("templates", tables)
        self.assertIn("template_versions", tables)
        self.assertIn("template_snapshots", tables)
        self.assertIn("brick_proposals", tables)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
