# tests/test_stage23_templates_snapshots.py
"""
Stage 23: снимки шаблонов — неизменяемость, права и источники.

Проверяется, что снимок копирует ровно ту версию, для которой создан, не
зависит от последующих правок шаблона, читается только владельцем и
разруливается ключом идемпотентности. Реальная post_bot.db не открывается.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from models import TemplateSnapshot
from template_store import (
    TemplateConflictError,
    TemplateNotFoundError,
    create_template_snapshot,
    get_template_snapshot,
    list_template_snapshots,
    update_template_body,
    update_template_header,
)
from template_types import BUILTIN_TEMPLATE_VERSION
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    SUPERADMIN_TG,
    TemplateDatabaseTestCase,
)


class TemplateSnapshotTests(TemplateDatabaseTestCase):
    """Снимки личных и стандартных шаблонов."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.other = self.make_user(OTHER_TG, username="other")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="super")

    def test_personal_snapshot_copies_all_sections_and_bindings(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "снапкод")
        created = self.make_personal_template(
            self.owner,
            "Снапшотный",
            header="Заголовок [название]",
            body="Тело [снапкод]",
            footer="Подвал",
        )
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.assertEqual(snapshot.source_template_id, created.template_id)
        self.assertEqual(snapshot.source_template_version, 1)
        self.assertEqual(snapshot.source_kind, "personal")
        self.assertEqual(snapshot.source_name, "Снапшотный")
        self.assertIsNone(snapshot.source_place_id)
        self.assertEqual(snapshot.header, "Заголовок [название]")
        self.assertEqual(snapshot.body, "Тело [снапкод]")
        self.assertEqual(snapshot.footer, "Подвал")
        self.assertEqual(snapshot.brick_ids, tuple(sorted((1, brick_id))))

    def test_standard_snapshot_copies_builtin_version(self) -> None:
        from template_store import get_standard_template

        place = self.make_place("Снапшотное место")
        standard = get_standard_template(self.session, OWNER_TG, place.id)
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.template_id,
        )
        self.assertEqual(snapshot.source_kind, "standard")
        self.assertEqual(snapshot.source_place_id, place.id)
        self.assertIsNone(snapshot.source_name)
        self.assertEqual(snapshot.brick_ids, standard.current_version.brick_ids)
        self.assertEqual(snapshot.builtin_version, BUILTIN_TEMPLATE_VERSION)
        del standard

    def test_snapshot_is_immutable_after_template_edits(self) -> None:
        created = self.make_personal_template(self.owner, "Мутируемый")
        old_header = created.current_version.header
        old_body = created.current_version.body
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.make_place_admin(self.make_place("Другое место"), self.other)

        # Меняем текущую версию шаблона после создания снимка.
        update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Новый заголовок",
        )
        update_template_body(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=2,
            content="Новый текст",
        )

        refreshed = get_template_snapshot(self.session, OWNER_TG, snapshot.snapshot_id)
        assert refreshed is not None
        self.assertEqual(refreshed.header, old_header)
        self.assertEqual(refreshed.body, old_body)
        self.assertEqual(refreshed.source_template_version, 1)

    def test_idempotency_key_returns_existing_snapshot(self) -> None:
        created = self.make_personal_template(self.owner, "Идемпотентный")
        first = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            idempotency_key="ключ-1",
        )
        second = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            idempotency_key="ключ-1",
        )
        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(len(list_template_snapshots(self.session, OWNER_TG)), 1)

        # Тот же ключ для другого шаблона — конфликт.
        other = self.make_personal_template(self.owner, "Другой")
        with self.assertRaises(TemplateConflictError):
            create_template_snapshot(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=other.template_id,
                idempotency_key="ключ-1",
            )

    def test_expected_revision_mismatch_is_conflict(self) -> None:
        created = self.make_personal_template(self.owner, "Конфликтный")
        update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Правка",
        )
        with self.assertRaises(TemplateConflictError):
            create_template_snapshot(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
                expected_revision=1,
            )
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=2,
        )
        self.assertEqual(snapshot.source_template_version, 2)

    def test_snapshot_access_is_owner_only(self) -> None:
        created = self.make_personal_template(self.owner, "Тайный")
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.assertIsNone(
            get_template_snapshot(self.session, OTHER_TG, snapshot.snapshot_id)
        )
        self.assertIsNone(
            get_template_snapshot(self.session, SUPERADMIN_TG, snapshot.snapshot_id)
        )
        self.assertEqual(list_template_snapshots(self.session, OTHER_TG), [])

        self.assertIsNotNone(
            get_template_snapshot(self.session, OWNER_TG, snapshot.snapshot_id)
        )
        rows = list_template_snapshots(self.session, OWNER_TG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].snapshot_id, snapshot.snapshot_id)

    def test_unknown_or_inaccessible_template_snapshot_fails(self) -> None:
        created = self.make_personal_template(self.owner, "Скрытый")
        with self.assertRaises(TemplateNotFoundError):
            create_template_snapshot(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                template_id=created.template_id,
            )
        with self.assertRaises(TemplateNotFoundError):
            create_template_snapshot(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id + 1000,
            )

    def test_snapshot_rows_are_never_touching_legacy_drafts(self) -> None:
        created = self.make_personal_template(self.owner, "Чистый")
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        rows = self.session.scalars(select(TemplateSnapshot)).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, snapshot.snapshot_id)
        self.assertIsNone(rows[0].idempotency_key)
        self.assertIsNone(rows[0].builtin_version)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
