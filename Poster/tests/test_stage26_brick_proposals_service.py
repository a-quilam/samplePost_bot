# tests/test_stage26_brick_proposals_service.py
"""
Stage 26: сервис предложений кирпичиков без Telegram.

Временная SQLite и реальные ограничения. Проверяются снимки, права,
идемпотентность решений, транзакционное одобрение, конфликты кодов,
неизменность источника/шаблонов и отсутствие срока действия pending.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from brick_proposal_store import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    PROPOSAL_STATUS_REJECTED,
    PROPOSAL_STATUS_WITHDRAWN,
    ProposalActorError,
    ProposalConflictError,
    ProposalNotFoundError,
    ProposalPermissionError,
    ProposalValidationError,
    approve_brick_proposal,
    count_pending_for_place,
    count_pending_place_proposals,
    count_pending_system_proposals,
    create_brick_proposal,
    get_admin_proposal,
    get_own_proposal,
    list_own_proposals,
    list_pending_for_place,
    list_pending_place_proposals,
    list_pending_system_proposals,
    reject_brick_proposal,
    withdraw_brick_proposal,
)
from brick_store import (
    archive_personal_brick,
    create_personal_brick,
    create_place_brick,
    list_available_bricks,
    update_personal_brick,
)
from models import Brick, BrickProposal, TemplateVersionBrick
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    SUPERADMIN_TG,
    TemplateDatabaseTestCase,
)
from user_store import get_or_create_user


class BrickProposalServiceTests(TemplateDatabaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        get_or_create_user(
            self.session,
            SUPERADMIN_TG,
            username="stage26super",
            full_name="Stage 26 Superadmin",
        )

    def make_owner(self, telegram_user_id: int = OWNER_TG):
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=f"owner{telegram_user_id}",
            full_name=f"Owner {telegram_user_id}",
        )

    def make_source(
        self,
        owner,
        *,
        code: str = "свой",
        title: str = "Свой кирпичик",
        value_type: str = "short_text",
        rules: dict | None = None,
    ):
        return create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code=code,
            title=title,
            value_type=value_type,
            rules=rules,
        )

    def make_place_with_admin(self, admin=None):
        place = self.make_place(
            "Зал Stage 26", created_by_user_id=admin.id if admin else None
        )
        if admin is not None:
            self.make_place_admin(place, admin)
        return place

    def make_proposal(self, owner, source, *, scope="place", place=None):
        return create_brick_proposal(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            source_brick_id=source.id,
            scope=scope,
            place_id=place.id if place is not None else None,
        )

    def make_place_brick(self, **kwargs):
        return create_place_brick(self.session, **kwargs)

    def proposal_row(self, proposal_id: int) -> BrickProposal:
        row = self.session.get(BrickProposal, proposal_id)
        assert row is not None
        self.session.refresh(row)
        return row

    def brick_count(self) -> int:
        return int(len(self.session.scalars(select(Brick.id)).all()))

    def test_create_place_snapshot_is_pending_and_place_is_not_created(self) -> None:
        owner = self.make_owner()
        place = self.make_place("Существующее место")
        source = self.make_source(
            owner,
            code="цена_26",
            title="Цена 26",
            value_type="number",
            rules={"min": 0},
        )
        before_places = len(self.session.scalars(select(Brick.id)).all())
        snapshot = self.make_proposal(owner, source, place=place)
        self.assertEqual(snapshot.status, PROPOSAL_STATUS_PENDING)
        self.assertEqual(snapshot.title, "Цена 26")
        self.assertEqual(snapshot.code, "цена_26")
        self.assertEqual(snapshot.value_type, "number")
        self.assertEqual(snapshot.rules, {"min": 0})
        self.assertEqual(snapshot.place_id, place.id)
        self.assertEqual(snapshot.source_brick_id, source.id)
        self.assertIsNotNone(snapshot.created_at)
        self.assertIsNone(snapshot.decided_at)
        self.assertIsNone(snapshot.approved_brick_id)
        self.assertEqual(
            len(self.session.scalars(select(Brick.id)).all()), before_places
        )
        self.assertEqual(self.proposal_row(snapshot.id).status, "pending")

    def test_create_system_snapshot_and_keep_source(self) -> None:
        owner = self.make_owner()
        source = self.make_source(owner, code="глобальный_26", title="Глобальный")
        snapshot = self.make_proposal(owner, source, scope="system")
        self.assertEqual(snapshot.scope, "system")
        self.assertIsNone(snapshot.place_id)
        self.session.expire_all()
        stored = self.session.get(Brick, source.id)
        assert stored is not None
        self.assertEqual(stored.title, "Глобальный")
        self.assertEqual(stored.code, "глобальный_26")
        self.assertEqual(stored.scope, "personal")
        self.assertEqual(stored.owner_user_id, owner.id)

    def test_create_rejects_foreign_archived_system_and_missing_source(self) -> None:
        owner = self.make_owner()
        stranger = self.make_owner(OTHER_TG)
        place = self.make_place("Место")
        foreign = self.make_source(stranger, code="чужой_26")
        with self.assertRaises(ProposalNotFoundError):
            create_brick_proposal(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                source_brick_id=foreign.id,
                scope="place",
                place_id=place.id,
            )
        own = self.make_source(owner, code="архив_26")
        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=own.id,
            expected_revision=1,
        )
        self.assertEqual(archived.status, "archived")
        with self.assertRaises(ProposalValidationError):
            create_brick_proposal(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                source_brick_id=own.id,
                scope="place",
                place_id=place.id,
            )
        with self.assertRaises(ProposalNotFoundError):
            create_brick_proposal(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                source_brick_id=999_999,
                scope="place",
                place_id=place.id,
            )

    def test_create_rejects_bad_or_missing_place_and_scope(self) -> None:
        owner = self.make_owner()
        source = self.make_source(owner)
        for scope, place_id in (
            ("personal", None),
            ("place", None),
            ("place", 999_999),
            ("system", 1),
        ):
            with self.subTest(scope=scope, place_id=place_id):
                with self.assertRaises(
                    (ProposalValidationError, ProposalNotFoundError)
                ):
                    create_brick_proposal(
                        self.session,
                        actor_telegram_user_id=owner.telegram_user_id,
                        source_brick_id=source.id,
                        scope=scope,
                        place_id=place_id,
                    )

    def test_create_code_conflicts_and_system_source_exception(self) -> None:
        owner = self.make_owner()
        place = self.make_place("Место конфликта")
        self.make_place_brick(
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=place.id,
            code="занят_26",
            title="Занятый",
            value_type="short_text",
        )
        source = self.make_source(owner, code="занят_26")
        with self.assertRaises(ProposalConflictError):
            self.make_proposal(owner, source, place=place)
        system_source = self.make_source(owner, code="глобал_26")
        snapshot = self.make_proposal(owner, system_source, scope="system")
        self.assertEqual(snapshot.status, "pending")

    def test_snapshot_survives_source_edit_and_archive(self) -> None:
        owner = self.make_owner()
        place = self.make_place("Место снимка")
        source = self.make_source(owner, code="снимок_26", title="До правки")
        snapshot = self.make_proposal(owner, source, place=place)
        updated = update_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=source.id,
            expected_revision=source.revision,
            code="новый_код_26",
            title="После правки",
            rules=None,
        )
        self.assertEqual(updated.status, "updated")
        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=source.id,
            expected_revision=2,
        )
        self.assertEqual(archived.status, "archived")
        self.session.expire_all()
        stored = get_own_proposal(self.session, owner.telegram_user_id, snapshot.id)
        assert stored is not None
        self.assertEqual(stored.title, "До правки")
        self.assertEqual(stored.code, "снимок_26")
        self.assertEqual(stored.status, "pending")

    def test_own_proposal_list_is_private_and_paginated(self) -> None:
        owner = self.make_owner()
        stranger = self.make_owner(OTHER_TG)
        place = self.make_place("Место списка")
        own_ids = []
        for index in range(10):
            source = self.make_source(owner, code=f"свой_{index:02d}")
            own_ids.append(self.make_proposal(owner, source, place=place).id)
        foreign_source = self.make_source(stranger, code="чужой_список")
        foreign = self.make_proposal(stranger, foreign_source, place=place)
        rows, total = list_own_proposals(
            self.session, owner.telegram_user_id, offset=0, limit=8
        )
        self.assertEqual(total, 10)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row.id != foreign.id for row in rows))
        second, second_total = list_own_proposals(
            self.session, owner.telegram_user_id, offset=8, limit=8
        )
        self.assertEqual(second_total, 10)
        self.assertEqual(len(second), 2)
        ids = {row.id for row in rows} | {row.id for row in second}
        self.assertEqual(ids, set(own_ids))
        self.assertIsNone(
            get_own_proposal(self.session, owner.telegram_user_id, foreign.id)
        )

    def test_admin_read_permissions_and_scope_filters(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        stranger = get_or_create_user(
            self.session, 9_926_777, username="stranger26", full_name="Stranger"
        )
        first = self.make_place_with_admin(admin)
        second = self.make_place("Другое место")
        place_source = self.make_source(owner, code="место_26")
        place_proposal = self.make_proposal(owner, place_source, place=first)
        other_source = self.make_source(owner, code="другое_26")
        other_proposal = self.make_proposal(owner, other_source, place=second)
        system_source = self.make_source(owner, code="система_26")
        system_proposal = self.make_proposal(owner, system_source, scope="system")

        self.assertIsNotNone(
            get_admin_proposal(self.session, admin.telegram_user_id, place_proposal.id)
        )
        self.assertIsNone(
            get_admin_proposal(self.session, admin.telegram_user_id, other_proposal.id)
        )
        self.assertIsNone(
            get_admin_proposal(self.session, admin.telegram_user_id, system_proposal.id)
        )
        self.assertIsNone(
            get_admin_proposal(
                self.session, stranger.telegram_user_id, place_proposal.id
            )
        )
        self.assertIsNotNone(
            get_admin_proposal(self.session, SUPERADMIN_TG, place_proposal.id)
        )
        self.assertIsNotNone(
            get_admin_proposal(self.session, SUPERADMIN_TG, system_proposal.id)
        )

        queue, total = list_pending_for_place(
            self.session, admin.telegram_user_id, first.id
        )
        self.assertEqual(total, 1)
        self.assertEqual([row.id for row in queue], [place_proposal.id])
        with self.assertRaises(ProposalPermissionError):
            list_pending_for_place(self.session, admin.telegram_user_id, second.id)
        all_places, all_total = list_pending_place_proposals(
            self.session, SUPERADMIN_TG
        )
        self.assertEqual(all_total, 2)
        self.assertEqual(
            {row.id for row in all_places}, {place_proposal.id, other_proposal.id}
        )
        with self.assertRaises(ProposalPermissionError):
            list_pending_place_proposals(self.session, admin.telegram_user_id)
        systems, system_total = list_pending_system_proposals(
            self.session, SUPERADMIN_TG
        )
        self.assertEqual(system_total, 1)
        self.assertEqual([row.id for row in systems], [system_proposal.id])
        with self.assertRaises(ProposalPermissionError):
            list_pending_system_proposals(self.session, admin.telegram_user_id)

    def test_pending_counts_are_scoped(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        first = self.make_place_with_admin(admin)
        second = self.make_place("Второе")
        source_one = self.make_source(owner, code="count1")
        source_two = self.make_source(owner, code="count2")
        source_three = self.make_source(owner, code="count3")
        one = self.make_proposal(owner, source_one, place=first)
        self.make_proposal(owner, source_two, place=second)
        self.make_proposal(owner, source_three, scope="system")
        self.assertEqual(
            count_pending_for_place(self.session, admin.telegram_user_id, first.id),
            1,
        )
        self.assertEqual(
            count_pending_place_proposals(self.session, SUPERADMIN_TG),
            2,
        )
        self.assertEqual(
            count_pending_system_proposals(self.session, SUPERADMIN_TG),
            1,
        )
        reject_brick_proposal(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            proposal_id=one.id,
        )
        self.assertEqual(
            count_pending_for_place(self.session, admin.telegram_user_id, first.id),
            0,
        )

    def test_approve_place_creates_separate_brick_and_keeps_template_bindings(
        self,
    ) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="новый_26", title="Новый")
        template = self.make_personal_template(
            owner,
            "Шаблон 26",
            content="\n".join(p for p in ("", "Поле [новый_26]", "") if p),
        )
        version_id = template.current_version.version_id
        snapshot = self.make_proposal(owner, source, place=place)
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED)
        self.assertIsNotNone(result.brick)
        assert result.brick is not None
        self.assertNotEqual(result.brick.id, source.id)
        self.assertEqual(result.brick.scope, "place")
        self.assertEqual(result.brick.place_id, place.id)
        self.assertIsNone(result.brick.owner_user_id)
        self.assertEqual(result.brick.code, "новый_26")
        self.session.expire_all()
        source_row = self.session.get(Brick, source.id)
        assert source_row is not None
        self.assertEqual(source_row.status, "active")
        self.assertEqual(source_row.scope, "personal")
        self.assertEqual(source_row.owner_user_id, owner.id)
        binding = self.session.scalar(
            select(TemplateVersionBrick).where(
                TemplateVersionBrick.template_version_id == version_id,
                TemplateVersionBrick.brick_id == source.id,
            )
        )
        assert binding is not None
        self.assertEqual(binding.code, "новый_26")
        stored = self.proposal_row(snapshot.id)
        self.assertEqual(stored.status, "approved")
        self.assertEqual(stored.approved_brick_id, result.brick.id)

    def test_approve_system_creates_system_brick_same_code(self) -> None:
        owner = self.make_owner()
        source = self.make_source(owner, code="промо_26", title="Промо")
        snapshot = self.make_proposal(owner, source, scope="system")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, "approved")
        assert result.brick is not None
        self.assertEqual(result.brick.scope, "system")
        self.assertIsNone(result.brick.place_id)
        self.assertIsNone(result.brick.owner_user_id)
        self.assertEqual(result.brick.code, "промо_26")
        self.assertIn(
            "промо_26",
            {
                brick.code
                for brick in list_available_bricks(
                    self.session,
                    viewer_telegram_user_id=owner.telegram_user_id,
                )
            },
        )
        updated = update_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=source.id,
            expected_revision=source.revision,
            code="промо_26",
            title="Обновлённый источник",
            rules=None,
        )
        self.assertEqual(updated.status, "updated")

    def test_approve_permissions_are_checked_again(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        stranger = get_or_create_user(
            self.session, 9_926_778, username="stranger2", full_name="Stranger 2"
        )
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="права_26")
        snapshot = self.make_proposal(owner, source, place=place)
        with self.assertRaises(ProposalPermissionError):
            approve_brick_proposal(
                self.session,
                actor_telegram_user_id=stranger.telegram_user_id,
                proposal_id=snapshot.id,
            )
        self.assertEqual(self.proposal_row(snapshot.id).status, "pending")
        with self.assertRaises(ProposalActorError):
            approve_brick_proposal(
                self.session,
                actor_telegram_user_id=9_999_999,
                proposal_id=snapshot.id,
            )
        self.assertEqual(self.proposal_row(snapshot.id).status, "pending")

    def test_approve_conflict_keeps_pending_and_creates_no_brick(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="конфликт_26")
        snapshot = self.make_proposal(owner, source, place=place)
        self.make_place_brick(
            actor_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            code="конфликт_26",
            title="Появился позже",
            value_type="short_text",
        )
        before = self.brick_count()
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, "conflict")
        self.assertIsNone(result.brick)
        self.assertEqual(self.brick_count(), before)
        self.assertEqual(self.proposal_row(snapshot.id).status, "pending")

    def test_approve_uses_snapshot_after_source_archive(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="архив_снимок_26", title="Снимок")
        snapshot = self.make_proposal(owner, source, place=place)
        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=source.id,
            expected_revision=source.revision,
        )
        self.assertEqual(archived.status, "archived")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, "approved")
        assert result.brick is not None
        self.assertEqual(result.brick.code, "архив_снимок_26")
        self.assertEqual(result.brick.title, "Снимок")
        self.session.expire_all()
        archived_source = self.session.get(Brick, source.id)
        assert archived_source is not None
        self.assertEqual(archived_source.status, "archived")

    def test_repeated_decision_and_stale_transition_are_noops(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="повтор_26")
        snapshot = self.make_proposal(owner, source, place=place)
        first = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(first.status, "approved")
        before = self.brick_count()
        second = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(second.status, "already_decided")
        self.assertIsNone(second.brick)
        self.assertEqual(self.brick_count(), before)
        third = reject_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(third.status, "already_decided")
        self.assertEqual(self.proposal_row(snapshot.id).status, "approved")

    def test_reject_records_note_and_source_is_unchanged(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="отказ_26", title="Отказ")
        snapshot = self.make_proposal(owner, source, place=place)
        result = reject_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
            note="  Не   подходит  ",
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_REJECTED)
        assert result.proposal is not None
        self.assertEqual(result.proposal.decision_note, "Не подходит")
        self.session.expire_all()
        stored = self.session.get(Brick, source.id)
        assert stored is not None
        self.assertEqual(stored.status, "active")
        self.assertEqual(stored.title, "Отказ")
        self.assertEqual(stored.owner_user_id, owner.id)

    def test_withdraw_is_author_only_and_allows_new_proposal(self) -> None:
        owner = self.make_owner()
        stranger = self.make_owner(OTHER_TG)
        place = self.make_place("Место отзыва")
        source = self.make_source(owner, code="отзыв_26")
        snapshot = self.make_proposal(owner, source, place=place)
        denied = withdraw_brick_proposal(
            self.session,
            actor_telegram_user_id=stranger.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(denied.status, "not_found")
        self.assertEqual(self.proposal_row(snapshot.id).status, "pending")
        result = withdraw_brick_proposal(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_WITHDRAWN)
        repeated = withdraw_brick_proposal(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(repeated.status, "already_decided")
        second = self.make_proposal(owner, source, place=place)
        self.assertNotEqual(second.id, snapshot.id)
        self.assertEqual(second.status, "pending")

    def test_pending_without_ttl_remains_decidable_after_years(self) -> None:
        owner = self.make_owner()
        admin = self.make_owner(OTHER_TG)
        place = self.make_place_with_admin(admin)
        source = self.make_source(owner, code="без_ttl_26")
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO brick_proposals "
                "(author_user_id, scope, place_id, source_brick_id, title, "
                "code, type, source, rules_json, status, created_at) "
                "VALUES (?, 'place', ?, ?, ?, ?, 'short_text', 'ask', NULL, "
                "'pending', '2010-01-01 00:00:00')",
                (
                    owner.id,
                    place.id,
                    source.id,
                    source.title,
                    source.code,
                ),
            )
        snapshot = self.session.scalars(
            select(BrickProposal).where(BrickProposal.source_brick_id == source.id)
        ).one()
        rows, total = list_pending_for_place(
            self.session, admin.telegram_user_id, place.id
        )
        self.assertEqual(total, 1)
        self.assertEqual(rows[0].id, snapshot.id)
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            proposal_id=snapshot.id,
        )
        self.assertEqual(result.status, "approved")

    def test_get_admin_proposal_hides_foreign_existence(self) -> None:
        owner = self.make_owner()
        stranger = self.make_owner(OTHER_TG)
        place = self.make_place("Место приватности")
        source = self.make_source(owner, code="приват_26")
        snapshot = self.make_proposal(owner, source, place=place)
        self.assertIsNone(
            get_admin_proposal(self.session, stranger.telegram_user_id, snapshot.id)
        )
        self.assertIsNone(
            get_own_proposal(self.session, stranger.telegram_user_id, snapshot.id)
        )
        self.assertIsNone(
            get_own_proposal(self.session, owner.telegram_user_id, 999_999)
        )


if __name__ == "__main__":
    unittest.main()
