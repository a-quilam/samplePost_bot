# tests/test_stage22b_brick_service.py
"""
Stage 22B: управление личными кирпичиками на сервисном слое.

Временные миграционные базы; проверяются владение, ревизии, мягкий архив,
пожизненная резервация кода, пагинация, кандидаты `[код]` и откаты ошибок.
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from brick_store import (
    BrickActorError,
    BrickConflictError,
    BrickDefinition,
    BrickMutationResult,
    archive_personal_brick,
    check_code_availability,
    count_personal_bricks,
    create_personal_brick,
    create_place_brick,
    get_personal_brick,
    list_personal_bricks,
    resolve_brick_by_code,
    resolve_brick_code_candidates,
    restore_personal_brick,
    update_personal_brick,
)
from brick_types import BrickValidationError
from models import Brick
from tests.stage22a_helpers import BrickDatabaseTestCase


class BrickManagementServiceTests(BrickDatabaseTestCase):
    def make_brick(
        self,
        telegram_user_id: int,
        *,
        code: str = "цена",
        title: str = "Цена",
        value_type: str = "short_text",
        rules: dict | None = None,
    ):
        return create_personal_brick(
            self.session,
            actor_telegram_user_id=telegram_user_id,
            code=code,
            title=title,
            value_type=value_type,
            rules=rules,
        )

    def count_bricks(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(Brick)) or 0)

    def mutation_brick(self, result: BrickMutationResult) -> BrickDefinition:
        self.assertIsNotNone(result.brick)
        assert result.brick is not None
        return result.brick

    def test_update_preserves_id_and_type_and_increments_revision(self) -> None:
        user = self.make_user(2_201)
        created = self.make_brick(
            user.telegram_user_id,
            code="цена",
            title="Цена",
            value_type="number",
            rules={"min": 0, "integer": True},
        )
        result = update_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
            code="  СТОИМОСТЬ ",
            title="  Стоимость билета  ",
            rules={"min": 100, "max": 5000, "suffix": "₽"},
        )
        self.assertEqual(result.status, "updated")
        self.assertTrue(result.ok)
        assert result.brick is not None
        self.assertEqual(self.mutation_brick(result).id, created.id)
        self.assertEqual(self.mutation_brick(result).value_type, "number")
        self.assertEqual(self.mutation_brick(result).code, "стоимость")
        self.assertEqual(self.mutation_brick(result).title, "Стоимость билета")
        self.assertEqual(
            self.mutation_brick(result).rules, {"min": 100, "max": 5000, "suffix": "₽"}
        )
        self.assertEqual(self.mutation_brick(result).revision, created.revision + 1)
        self.assertEqual(self.count_bricks(), 11)

    def test_update_rejects_foreign_system_place_and_archived_rows(self) -> None:
        owner = self.make_user(2_202)
        other = self.make_user(2_203)
        place_owner = self.make_user(2_204)
        place = self.make_place("Место с кирпичиком")
        self.make_place_admin(place, place_owner)
        own = self.make_brick(owner.telegram_user_id, code="свой", title="Свой")
        foreign = self.make_brick(other.telegram_user_id, code="чужой", title="Чужой")
        place_brick = create_place_brick(
            self.session,
            actor_telegram_user_id=place_owner.telegram_user_id,
            place_id=place.id,
            code="местный",
            title="Местный",
            value_type="short_text",
        )
        for brick_id, revision in (
            (foreign.id, foreign.revision),
            (place_brick.id, place_brick.revision),
            (1, 1),
        ):
            result = update_personal_brick(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                brick_id=brick_id,
                expected_revision=revision,
                code="подмена",
                title="Подмена",
                rules={},
            )
            self.assertEqual(result.status, "not_found")
            self.assertIsNone(result.brick)
        for mutation in (archive_personal_brick, restore_personal_brick):
            self.assertEqual(
                mutation(
                    self.session,
                    actor_telegram_user_id=owner.telegram_user_id,
                    brick_id=1,
                    expected_revision=1,
                ).status,
                "not_found",
            )

        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=own.id,
            expected_revision=own.revision,
        )
        self.assertEqual(archived.status, "archived")
        result = update_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=own.id,
            expected_revision=archived.brick.revision if archived.brick else 1,
            code="свой",
            title="Новое",
            rules={},
        )
        self.assertEqual(result.status, "archived")
        self.assertEqual(self.mutation_brick(result).code, "свой")
        self.assertEqual(self.mutation_brick(result).title, "Свой")

    def test_admin_roles_do_not_open_foreign_personal_bricks(self) -> None:
        owner = self.make_user(2_205)
        admin = self.make_user(2_206)
        superadmin = self.make_user(self.SUPERADMIN_TG)
        place = self.make_place("Дом культуры")
        self.make_place_admin(place, admin)
        created = self.make_brick(
            owner.telegram_user_id, code="закрыто", title="Закрыто"
        )
        for outsider in (admin, superadmin):
            self.assertIsNone(
                get_personal_brick(self.session, outsider.telegram_user_id, created.id)
            )
            self.assertEqual(
                list_personal_bricks(self.session, outsider.telegram_user_id), []
            )
            self.assertEqual(
                count_personal_bricks(self.session, outsider.telegram_user_id), 0
            )
            result = update_personal_brick(
                self.session,
                actor_telegram_user_id=outsider.telegram_user_id,
                brick_id=created.id,
                expected_revision=created.revision,
                code="забран",
                title="Забран",
                rules={},
            )
            self.assertEqual(result.status, "not_found")
        existing = get_personal_brick(self.session, owner.telegram_user_id, created.id)
        assert existing is not None
        self.assertEqual(existing.code, "закрыто")

    def test_code_conflict_is_explicit_and_archived_code_stays_reserved(self) -> None:
        user = self.make_user(2_207)
        first = self.make_brick(user.telegram_user_id, code="занято", title="Первый")
        second = self.make_brick(user.telegram_user_id, code="второй", title="Второй")
        conflict = update_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=second.id,
            expected_revision=second.revision,
            code="занято",
            title="Второй",
            rules={},
        )
        self.assertEqual(conflict.status, "conflict")
        self.assertIn("уже используется", conflict.message or "")
        self.session.expire_all()
        stored = self.session.get(Brick, second.id)
        assert stored is not None
        self.assertEqual(stored.code, "второй")
        self.assertEqual(stored.title, "Второй")
        self.assertEqual(stored.revision, second.revision)

        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=first.id,
            expected_revision=first.revision,
        )
        self.assertEqual(archived.status, "archived")
        availability = check_code_availability(
            self.session,
            "занято",
            scope="personal",
            owner_user_id=user.id,
        )
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "duplicate")
        with self.assertRaises(BrickConflictError):
            self.make_brick(user.telegram_user_id, code="занято", title="Новый")
        stored = self.session.get(Brick, first.id)
        assert stored is not None
        self.assertEqual(first.id, stored.id)

    def test_archive_and_restore_are_soft_idempotent_and_revision_guarded(self) -> None:
        user = self.make_user(2_208)
        created = self.make_brick(user.telegram_user_id, code="архив", title="Архив")
        first = archive_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
        )
        self.assertEqual(first.status, "archived")
        self.assertEqual(self.mutation_brick(first).id, created.id)
        self.assertEqual(self.mutation_brick(first).code, "архив")
        self.assertEqual(self.mutation_brick(first).status, "archived")
        self.assertEqual(self.mutation_brick(first).revision, created.revision + 1)
        again = archive_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=self.mutation_brick(first).revision,
        )
        self.assertEqual(again.status, "already_archived")
        self.assertEqual(
            self.mutation_brick(again).revision, self.mutation_brick(first).revision
        )

        restored = restore_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=self.mutation_brick(again).revision,
        )
        self.assertEqual(restored.status, "restored")
        self.assertEqual(self.mutation_brick(restored).id, created.id)
        self.assertEqual(self.mutation_brick(restored).code, "архив")
        self.assertEqual(self.mutation_brick(restored).status, "active")
        self.assertEqual(self.mutation_brick(restored).revision, created.revision + 2)
        active_again = restore_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=self.mutation_brick(restored).revision,
        )
        self.assertEqual(active_again.status, "already_active")
        self.assertEqual(
            self.mutation_brick(active_again).revision,
            self.mutation_brick(restored).revision,
        )

        self.assertEqual(
            archive_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=created.id,
                expected_revision=created.revision,
            ).status,
            "stale",
        )
        archived_again = archive_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=self.mutation_brick(restored).revision,
        )
        self.assertEqual(archived_again.status, "archived")
        self.assertEqual(
            restore_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=created.id,
                expected_revision=created.revision,
            ).status,
            "stale",
        )
        finally_restored = restore_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=self.mutation_brick(archived_again).revision,
        )
        self.assertEqual(finally_restored.status, "restored")
        self.session.expire_all()
        stored = self.session.get(Brick, created.id)
        assert stored is not None
        self.assertEqual(stored.status, "active")
        self.assertEqual(self.count_bricks(), 11)

    def test_stale_edit_never_overwrites_newer_revision(self) -> None:
        user = self.make_user(2_209)
        created = self.make_brick(user.telegram_user_id, code="версия", title="Версия")
        newer = update_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
            code="версия",
            title="Новая версия",
            rules={},
        )
        self.assertEqual(newer.status, "updated")
        stale = update_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
            code="версия",
            title="Потерянное обновление",
            rules={},
        )
        self.assertEqual(stale.status, "stale")
        self.assertEqual(self.mutation_brick(stale).title, "Новая версия")
        self.session.expire_all()
        stored = self.session.get(Brick, created.id)
        assert stored is not None
        self.assertEqual(stored.title, "Новая версия")

    def test_two_concurrent_editors_produce_exactly_one_winner(self) -> None:
        user = self.make_user(2_210)
        created = self.make_brick(user.telegram_user_id, code="гонка", title="Гонка")
        barrier = Barrier(2)

        def attempt(title: str) -> str:
            with self.Session() as session:
                barrier.wait()
                result = update_personal_brick(
                    session,
                    actor_telegram_user_id=user.telegram_user_id,
                    brick_id=created.id,
                    expected_revision=created.revision,
                    code="гонка",
                    title=title,
                    rules={},
                )
                return result.status

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, ["Первый", "Второй"]))
        self.assertEqual(results.count("updated"), 1, results)
        self.assertTrue(
            all(item in {"updated", "stale", "conflict"} for item in results)
        )
        self.session.expire_all()
        stored = self.session.get(Brick, created.id)
        assert stored is not None
        self.assertIn(stored.title, {"Первый", "Второй"})
        self.assertEqual(stored.revision, created.revision + 1)

    def test_validation_error_leaves_all_fields_and_revision_untouched(self) -> None:
        user = self.make_user(2_211)
        created = self.make_brick(
            user.telegram_user_id,
            code="число",
            title="Число",
            value_type="number",
            rules={"min": 0, "max": 10},
        )
        with self.assertRaises(BrickValidationError):
            update_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=created.id,
                expected_revision=created.revision,
                code="новый",
                title="Новое название",
                rules={"min": 20, "max": 10},
            )
        self.session.expire_all()
        stored = self.session.get(Brick, created.id)
        assert stored is not None
        self.assertEqual(stored.code, "число")
        self.assertEqual(stored.title, "Число")
        self.assertEqual(stored.rules_json, '{"max": 10, "min": 0}')
        self.assertEqual(stored.revision, created.revision)

    def test_operational_error_rolls_back_and_reports_conflict(self) -> None:
        user = self.make_user(2_212)
        created = self.make_brick(user.telegram_user_id, code="сбой", title="Сбой")
        error = OperationalError("UPDATE bricks", {}, Exception("disk I/O error"))
        with patch("brick_store._commit_conditional_brick_update", side_effect=error):
            result = update_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=created.id,
                expected_revision=created.revision,
                code="сбой",
                title="Не сохранилось",
                rules={},
            )
        self.assertEqual(result.status, "conflict")
        self.assertIn("Не удалось сохранить", result.message or "")
        self.session.expire_all()
        stored = self.session.get(Brick, created.id)
        assert stored is not None
        self.assertEqual(stored.title, "Сбой")
        self.assertEqual(stored.revision, created.revision)
        # После откатанной транзакции сервис снова работает.
        retried = update_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
            code="сбой",
            title="Сохранилось",
            rules={},
        )
        self.assertEqual(retried.status, "updated")

    def test_list_counts_and_pagination_are_owner_scoped(self) -> None:
        owner = self.make_user(2_213)
        other = self.make_user(2_214)
        for index in range(5):
            self.make_brick(
                owner.telegram_user_id,
                code=f"код_{index}",
                title=f"Кирпичик {index}",
            )
        archived = self.make_brick(
            owner.telegram_user_id, code="архивный", title="Архивный"
        )
        archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=archived.id,
            expected_revision=archived.revision,
        )
        self.assertEqual(count_personal_bricks(self.session, owner.telegram_user_id), 5)
        self.assertEqual(
            count_personal_bricks(
                self.session,
                owner.telegram_user_id,
                status_filter="archived",
            ),
            1,
        )
        first = list_personal_bricks(
            self.session, owner.telegram_user_id, limit=2, offset=0
        )
        second = list_personal_bricks(
            self.session, owner.telegram_user_id, limit=2, offset=2
        )
        last = list_personal_bricks(
            self.session, owner.telegram_user_id, limit=2, offset=4
        )
        self.assertEqual([row.code for row in first], ["код_0", "код_1"])
        self.assertEqual([row.code for row in second], ["код_2", "код_3"])
        self.assertEqual([row.code for row in last], ["код_4"])
        archived_rows = list_personal_bricks(
            self.session,
            owner.telegram_user_id,
            status_filter="archived",
        )
        self.assertEqual([row.code for row in archived_rows], ["архивный"])
        self.assertEqual(list_personal_bricks(self.session, other.telegram_user_id), [])
        self.assertEqual(count_personal_bricks(self.session, other.telegram_user_id), 0)
        with self.assertRaises(Exception):
            list_personal_bricks(self.session, owner.telegram_user_id, limit=0)
        with self.assertRaises(Exception):
            list_personal_bricks(self.session, owner.telegram_user_id, offset=-1)
        with self.assertRaises(Exception):
            list_personal_bricks(
                self.session, owner.telegram_user_id, status_filter="unknown"
            )

    def test_get_personal_brick_hides_other_scope_and_filters_status(self) -> None:
        owner = self.make_user(2_215)
        other = self.make_user(2_216)
        created = self.make_brick(
            owner.telegram_user_id, code="карточка", title="Карточка"
        )
        stored = get_personal_brick(self.session, owner.telegram_user_id, created.id)
        assert stored is not None
        self.assertEqual(stored.id, created.id)
        self.assertIsNone(
            get_personal_brick(self.session, other.telegram_user_id, created.id)
        )
        self.assertIsNone(get_personal_brick(self.session, owner.telegram_user_id, 1))
        archived = archive_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            brick_id=created.id,
            expected_revision=created.revision,
        )
        assert archived.brick is not None
        self.assertIsNone(
            get_personal_brick(
                self.session,
                owner.telegram_user_id,
                created.id,
                include_archived=False,
            )
        )
        stored = get_personal_brick(self.session, owner.telegram_user_id, created.id)
        assert stored is not None
        self.assertEqual(stored.status, "archived")

    def test_candidates_expose_place_personal_ambiguity_and_keep_runtime_order(
        self,
    ) -> None:
        user = self.make_user(2_217)
        other = self.make_user(2_218)
        place = self.make_place("Клуб")
        self.make_place_admin(place, user)
        personal = self.make_brick(
            user.telegram_user_id, code="стоимость", title="Личная стоимость"
        )
        place_brick = create_place_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
            code="стоимость",
            title="Стоимость места",
            value_type="number",
        )
        resolution = resolve_brick_code_candidates(
            self.session,
            " СТОИМОСТЬ ",
            viewer_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
        )
        self.assertTrue(resolution.ambiguous)
        self.assertEqual(resolution.code, "стоимость")
        self.assertEqual(
            [row.id for row in resolution.candidates], [place_brick.id, personal.id]
        )
        assert resolution.selected is not None
        self.assertEqual(resolution.selected.id, place_brick.id)
        resolved = resolve_brick_by_code(
            self.session,
            "стоимость",
            viewer_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
        )
        assert resolved is not None
        self.assertEqual(resolved.id, place_brick.id)
        self.assertEqual(
            resolve_brick_code_candidates(
                self.session,
                "стоимость",
                viewer_telegram_user_id=other.telegram_user_id,
                place_id=place.id,
            ).ambiguous,
            False,
        )
        own_resolution = resolve_brick_code_candidates(
            self.session,
            "стоимость",
            viewer_telegram_user_id=user.telegram_user_id,
            place_id=None,
        )
        assert own_resolution.selected is not None
        self.assertEqual(own_resolution.selected.id, personal.id)
        missing = resolve_brick_code_candidates(self.session, "a[b")
        self.assertIsNone(missing.code)
        self.assertEqual(missing.candidates, ())

    def test_archived_place_code_falls_back_but_archived_system_code_does_not(
        self,
    ) -> None:
        user = self.make_user(2_219)
        place = self.make_place("Зал")
        self.make_place_admin(place, user)
        personal = self.make_brick(
            user.telegram_user_id, code="сцена", title="Личная сцена"
        )
        place_brick = create_place_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
            code="сцена",
            title="Сцена места",
            value_type="short_text",
        )
        self.session.execute(
            Brick.__table__.update()
            .where(Brick.id == place_brick.id)
            .values(status="archived")
        )
        self.session.commit()
        resolution = resolve_brick_code_candidates(
            self.session,
            "сцена",
            viewer_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
        )
        assert resolution.selected is not None
        self.assertEqual(resolution.selected.id, personal.id)
        self.assertFalse(resolution.ambiguous)

        self.session.execute(
            Brick.__table__.update().where(Brick.id == 1).values(status="archived")
        )
        self.session.commit()
        system = resolve_brick_code_candidates(
            self.session,
            "название",
            viewer_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
        )
        self.assertTrue(system.blocked_by_archived_system)
        self.assertIsNone(system.selected)
        self.assertEqual(system.candidates, ())

    def test_unregistered_actor_cannot_read_or_mutate(self) -> None:
        user = self.make_user(2_220)
        created = self.make_brick(user.telegram_user_id, code="актор", title="Актор")
        with self.assertRaises(BrickActorError):
            list_personal_bricks(self.session, 999_999)
        with self.assertRaises(BrickActorError):
            update_personal_brick(
                self.session,
                actor_telegram_user_id=999_999,
                brick_id=created.id,
                expected_revision=created.revision,
                code="актор",
                title="Актор",
                rules={},
            )


if __name__ == "__main__":
    unittest.main()
