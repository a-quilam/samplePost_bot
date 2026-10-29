# tests/test_stage26_brick_proposals_schema.py
"""
Stage 26: схема предложений кирпичиков и её настоящие SQLite-ограничения.

Временная база, миграции 1–11. Проверяются CHECK/FK/индексы/триггеры:
неизменяемый снимок, окончательность решения, запрет удаления истории,
права источника и узкое исключение для продвижения личного кода в системный.
Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from brick_proposal_store import (
    create_brick_proposal,
    reject_brick_proposal,
)
from brick_store import create_personal_brick, create_system_brick
from migrations import LATEST_VERSION
from tests.stage19a_helpers import connect
from tests.stage22a_helpers import BrickDatabaseTestCase
from user_store import get_or_create_user


class BrickProposalSchemaTests(BrickDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        get_or_create_user(
            self.session,
            self.SUPERADMIN_TG,
            username="stage26super",
            full_name="Stage 26 Superadmin",
        )

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

    def make_source(self, telegram_user_id: int = 9_926_101, code: str = "свой_код"):
        user = self.make_user(telegram_user_id)
        brick = create_personal_brick(
            self.session,
            actor_telegram_user_id=telegram_user_id,
            code=code,
            title="Личный кирпичик",
            value_type="short_text",
        )
        return user, brick

    def make_pending_place(self):
        user, brick = self.make_source()
        place = self.make_place("Зал предложений")
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            source_brick_id=brick.id,
            scope="place",
            place_id=place.id,
        )
        return user, brick, place, proposal

    def insert_raw(self, values: dict[str, object]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join(f":{name}" for name in values)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO brick_proposals ({columns}) VALUES ({placeholders})"
                ),
                values,
            )

    def test_migration_12_follows_brick_proposals_and_has_constraints(self) -> None:
        self.assertEqual(LATEST_VERSION, 22)
        # Миграция 11 остаётся на своём месте; 15/16 добавлены Poster 2.0 S1,
        # 17 — S5, 18 — Poster 2.1 R2, 19 — Poster 2.1 R3, 20 — R4,
        # 21/22 — Poster 2.0 U1a; порядок журнала не переставлен.
        self.assertEqual(
            self.journal()[-12:],
            [
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
                (21, "place_city"),
                (22, "builtin_templates_v4"),
            ],
        )
        connection = connect(self.path)
        try:
            columns = {
                str(row["name"]): row
                for row in connection.execute("PRAGMA table_info(brick_proposals)")
            }
            self.assertTrue(
                {
                    "id",
                    "author_user_id",
                    "scope",
                    "place_id",
                    "source_brick_id",
                    "title",
                    "code",
                    "type",
                    "source",
                    "rules_json",
                    "status",
                    "created_at",
                    "decided_at",
                    "decided_by_user_id",
                    "approved_brick_id",
                    "decision_note",
                }
                <= set(columns)
            )
            self.assertNotIn("expires_at", columns)
            self.assertNotIn("ttl", columns)
            indexes = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='brick_proposals'"
                )
            }
            self.assertTrue(
                {
                    "ix_brick_proposals_author_status_created",
                    "ix_brick_proposals_place_status_created",
                    "ix_brick_proposals_status_scope_created",
                    "ix_brick_proposals_source_brick",
                    "ux_brick_proposals_approved_brick",
                }
                <= indexes
            )
            triggers = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='brick_proposals'"
                )
            }
            self.assertEqual(
                triggers,
                {
                    "trg_brick_proposals_snapshot_immutable",
                    "trg_brick_proposals_single_transition",
                    "trg_brick_proposals_decision_immutable",
                    "trg_brick_proposals_no_delete",
                    "trg_brick_proposals_source_owner",
                    "trg_brick_proposals_approval_link",
                },
            )
        finally:
            connection.close()

    def test_constraints_reject_bad_scope_target_status_type_and_code(self) -> None:
        user, brick = self.make_source()
        place = self.make_place("Место")
        base: dict[str, object] = {
            "author_user_id": user.id,
            "scope": "place",
            "place_id": place.id,
            "source_brick_id": brick.id,
            "title": "Снимок",
            "code": "код",
            "type": "short_text",
            "source": "ask",
            "rules_json": None,
            "status": "pending",
            "created_at": "2026-01-01 00:00:00",
        }
        bad_variants: list[dict[str, object]] = [
            {"scope": "personal", "place_id": None},
            {"scope": "place", "place_id": None},
            {"scope": "system", "place_id": place.id},
            {"status": "expired"},
            {"type": "unknown"},
            {"source": "unknown"},
            {"code": "фото"},
            {"code": "с пробелом"},
            {"code": "[рамки]"},
            {"title": "   "},
            {"rules_json": "[]"},
            {"rules_json": "not json"},
        ]
        for override in bad_variants:
            with self.subTest(override=override), self.assertRaises(IntegrityError):
                self.insert_raw({**base, **override})

    def test_decision_shape_requires_audit_and_approved_brick_link(self) -> None:
        user, brick, place, proposal = self.make_pending_place()
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET status='approved' " "WHERE id=:id"
                    ),
                    {"id": proposal.id},
                )
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET status='rejected' " "WHERE id=:id"
                    ),
                    {"id": proposal.id},
                )
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET status='withdrawn', "
                        "decided_at=:now, decided_by_user_id=:uid, "
                        "decision_note='причина' WHERE id=:id"
                    ),
                    {
                        "now": "2026-01-01 00:00:00",
                        "uid": user.id,
                        "id": proposal.id,
                    },
                )

    def test_snapshot_columns_are_immutable_after_insert(self) -> None:
        user, brick, place, proposal = self.make_pending_place()
        other_brick = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="другой_источник",
            title="Другой источник",
            value_type="short_text",
        )
        columns = {
            "author_user_id": user.id + 1000,
            "scope": "system",
            "place_id": None,
            "source_brick_id": other_brick.id,
            "title": "Другое",
            "code": "другой",
            "type": "number",
            "source": "context",
            "rules_json": '{"min": 1}',
        }
        for column, value in columns.items():
            with self.subTest(column=column), self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            f"UPDATE brick_proposals SET {column}=:value "
                            "WHERE id=:id"
                        ),
                        {"value": value, "id": proposal.id},
                    )

    def test_single_transition_and_audit_fields_are_final(self) -> None:
        user, _brick, _place, proposal = self.make_pending_place()
        rejected = reject_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
            note="Не подходит",
        )
        self.assertEqual(rejected.status, "rejected")
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET decision_note='Другая' "
                        "WHERE id=:id"
                    ),
                    {"id": proposal.id},
                )
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET status='withdrawn' " "WHERE id=:id"
                    ),
                    {"id": proposal.id},
                )

    def test_history_cannot_be_deleted_physically(self) -> None:
        _user, _brick, _place, proposal = self.make_pending_place()
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text("DELETE FROM brick_proposals WHERE id=:id"),
                    {"id": proposal.id},
                )

    def test_source_must_be_active_owned_personal_brick(self) -> None:
        owner, brick = self.make_source()
        stranger = self.make_user(9_926_102)
        place = self.make_place("Место источника")
        base = {
            "author_user_id": stranger.id,
            "scope": "place",
            "place_id": place.id,
            "source_brick_id": brick.id,
            "title": "Снимок",
            "code": "код2",
            "type": "short_text",
            "source": "ask",
            "rules_json": None,
            "status": "pending",
            "created_at": "2026-01-01 00:00:00",
        }
        with self.assertRaises(IntegrityError):
            self.insert_raw(base)

        system_brick = create_system_brick(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            code="sys26",
            title="Системный 26",
            value_type="short_text",
        )
        with self.assertRaises(IntegrityError):
            self.insert_raw(
                {
                    **base,
                    "author_user_id": owner.id,
                    "source_brick_id": system_brick.id,
                    "code": "sys26",
                }
            )

        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE bricks SET status='archived' WHERE id=:id"),
                {"id": brick.id},
            )
        with self.assertRaises(IntegrityError):
            self.insert_raw(
                {
                    **base,
                    "author_user_id": owner.id,
                    "code": "код3",
                }
            )

    def test_approval_link_requires_separate_matching_active_brick(self) -> None:
        user, brick, place, proposal = self.make_pending_place()
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "UPDATE brick_proposals SET status='approved', "
                        "decided_at=:now, decided_by_user_id=:uid, "
                        "approved_brick_id=:bid WHERE id=:id"
                    ),
                    {
                        "now": "2026-01-01 00:00:00",
                        "uid": user.id,
                        "bid": brick.id,
                        "id": proposal.id,
                    },
                )

    def insert_system_raw(self, code: str, title: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO bricks "
                    "(code, title, type, scope, source, place_id, owner_user_id, "
                    "rules_json, status, revision, created_by_user_id, "
                    "created_at, updated_at) VALUES "
                    "(:code, :title, 'short_text', 'system', 'ask', NULL, NULL, "
                    "NULL, 'active', 1, NULL, '2026-01-01 00:00:00', NULL)"
                ),
                {"code": code, "title": title},
            )

    def test_system_reservation_exception_requires_matching_pending_proposal(
        self,
    ) -> None:
        user, brick = self.make_source(code="промо26")
        with self.assertRaises(IntegrityError):
            self.insert_system_raw("промо26", "Прямой системный")
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            source_brick_id=brick.id,
            scope="system",
        )
        self.assertEqual(proposal.scope, "system")
        self.insert_system_raw("промо26", "Личный кирпичик")
        system = self.session.execute(
            text("SELECT id FROM bricks WHERE scope='system' AND code='промо26'")
        ).first()
        self.assertIsNotNone(system)

    def test_promoted_source_stays_editable_but_new_duplicate_is_blocked(self) -> None:
        user, source = self.make_source(code="промо27")
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        approved = self.session.execute(
            text("SELECT 1 FROM brick_proposals WHERE id=:id AND status='pending'"),
            {"id": proposal.id},
        ).first()
        self.assertIsNotNone(approved)
        # Разрешаем только через транзакционный сервис, поэтому здесь
        # проверяем, что обычный UPDATE названия будущего источника не
        # ломается после появления системного дубля.
        from brick_proposal_store import approve_brick_proposal

        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(result.status, "approved")
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE bricks SET title='Новое название' WHERE id=:id"),
                {"id": source.id},
            )
        stranger = self.make_user(9_926_199)
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO bricks "
                        "(code, title, type, scope, source, place_id, "
                        "owner_user_id, rules_json, status, revision, "
                        "created_by_user_id, created_at, updated_at) "
                        "VALUES ('промо27', 'Новый личный', 'short_text', "
                        "'personal', 'ask', NULL, :owner, NULL, 'active', 1, "
                        ":owner, '2026-01-01 00:00:00', NULL)"
                    ),
                    {"owner": stranger.id},
                )
        with self.engine.begin() as connection:
            with self.assertRaises(IntegrityError):
                connection.execute(
                    text("UPDATE bricks SET code='название' WHERE id=:id"),
                    {"id": source.id},
                )


if __name__ == "__main__":
    unittest.main()
