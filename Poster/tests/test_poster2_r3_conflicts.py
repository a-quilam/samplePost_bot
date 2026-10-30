# tests/test_poster2_r3_conflicts.py
"""
R3 (Poster 2.1): глобальные кирпичики, конфликты кодов и задачи разрешения.

Проверяется требование владельца, а не только реализация:

1. Глобальное предложение при существующем МЕСТНОМ носителе того же кода
   одобряется (не `error`), системный кирпичик выигрывает для НОВЫХ привязок,
   по каждому месту создаётся задача конфликта, существующие снимки и версии
   шаблонов не изменяются.
2. Гонка двух решений одной задачи: ровно одно применяется, второе получает
   явный «уже решено», дублей уведомлений нет (CAS + BEGIN IMMEDIATE).
3. Идемпотентность: повторное одобрение и повторный прогон не создают
   дублей задач и уведомлений.
4. Задача переживает перезапуск процесса: состояние только в БД.
5. Обязательность: активная задача видна в карточке места; оба исхода
   работают (переименование и переключение шаблона на глобальный кирпичик).
6. Негатив: не-админ места (IDOR), forged callback, stale, 2+ местных
   носителей одного кода.

Все базы — временные SQLite (`tests.stage22a_helpers`); реальная
`post_bot.db` не открывается.
"""

from __future__ import annotations

import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from brick_proposal_store import (
    PROPOSAL_STATUS_APPROVED,
    approve_brick_proposal,
    create_brick_proposal,
)
from brick_store import (
    CONFLICT_STATUS_OPEN,
    CONFLICT_STATUS_RENAMED,
    CONFLICT_STATUS_SWITCHED,
    BrickConflictError,
    BrickPermissionError,
    count_open_conflicts_for_place,
    create_code_conflicts,
    create_personal_brick,
    create_place_brick,
    get_conflict,
    get_system_brick,
    list_pending_conflict_notifications,
    list_place_conflicts,
    mark_conflict_notification,
    resolve_conflict_by_rename,
    resolve_conflict_by_switch,
)
from migrations import LATEST_VERSION
from models import (
    Brick,
    BrickConflict,
    BrickConflictNotification,
    PlaceAdmin,
    TemplateVersionBrick,
    utcnow,
)
from place_store import create_place
from template_store import get_standard_template, update_template_section
from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
from tests.stage22a_helpers import BrickDatabaseTestCase
from user_store import get_or_create_user

INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR

AUTHOR_TG = 8_100_001
SUPERADMIN_TG = 8_100_002
OUTSIDER_TG = 8_100_003


class ConflictModelTests(BrickDatabaseTestCase):
    """Требование 1: модель конфликта и неизменность снимков."""

    def _author_with_place(self, *, code: str, tg: int = 8_101_001):
        author = self.make_user(tg, username=f"author{tg}")
        place = self.make_place(f"Место {tg}")
        self.make_place_admin(place, author)
        personal = create_place_brick(
            session=self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Местная цена",
            value_type="short_text",
            rules={},
        )
        return author, place, personal

    def _approve_global(self, author, code: str):
        personal = self.make_user(author.telegram_user_id + 500_000)
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=personal.telegram_user_id,
            code=code,
            title="Личный источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=personal.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        return approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )

    def test_global_approval_wins_and_creates_conflict_task(self) -> None:
        author, place, local = self._author_with_place(code="цена_кода")

        result = self._approve_global(author, "цена_кода")

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertIsNotNone(result.brick)
        system = get_system_brick(self.session, "цена_кода")
        assert system is not None
        self.assertEqual(system.scope, "system")
        # Местный кирпичик не переписан и не переименован.
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        self.assertEqual(local_row.code, "цена_кода")
        self.assertEqual(local_row.status, "active")
        # Ровно одна задача по месту.
        conflicts = list_place_conflicts(self.session, self.SUPERADMIN_TG, place.id)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status, CONFLICT_STATUS_OPEN)
        self.assertEqual(conflicts[0].local_brick_id, local.id)
        self.assertEqual(conflicts[0].system_brick_id, system.id)

    def test_global_code_wins_for_new_bindings(self) -> None:
        author, place, local = self._author_with_place(code="цена_новая")
        result = self._approve_global(author, "цена_новая")
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        system = get_system_brick(self.session, "цена_новая")
        assert system is not None

        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        updated = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content="[цена_новая]",
        )
        self.assertEqual(updated.status, "updated", updated.message)
        assert updated.template is not None
        bindings = {
            binding.code: binding.brick_id
            for binding in updated.template.current_version.bindings
        }
        self.assertEqual(bindings["цена_новая"], system.id)

    def test_existing_snapshots_and_versions_are_not_rewritten(self) -> None:
        author, place, local = self._author_with_place(code="цена_снимок")
        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        updated = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content="[цена_снимок]",
        )
        assert updated.template is not None
        version_id = updated.template.current_version.version_id
        before = list(
            self.session.scalars(
                select(TemplateVersionBrick).where(
                    TemplateVersionBrick.template_version_id == version_id
                )
            ).all()
        )
        # У стандартного шаблона есть встроенные привязки (название, контакт…),
        # поэтому проверяем наличие местного кирпичика, а не единственность.
        self.assertIn(local.id, [row.brick_id for row in before])

        result = self._approve_global(author, "цена_снимок")
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)

        self.session.expire_all()
        after = list(
            self.session.scalars(
                select(TemplateVersionBrick).where(
                    TemplateVersionBrick.template_version_id == version_id
                )
            ).all()
        )
        self.assertEqual(
            [(row.brick_id, row.code) for row in after],
            [(row.brick_id, row.code) for row in before],
        )


class ConflictIdempotencyTests(BrickDatabaseTestCase):
    """Требование 2-3: идемпотентность задач и уведомлений."""

    def _setup(self, *, tg: int):
        author = self.make_user(tg, username=f"a{tg}")
        place = self.make_place(f"Место {tg}")
        self.make_place_admin(place, author)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="код_идемп",
            title="Местный",
            value_type="short_text",
            rules={},
        )
        source_owner = self.make_user(tg + 1, username=f"s{tg}")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code="код_идемп",
            title="Источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        return author, place, proposal

    def test_repeat_approval_does_not_duplicate_tasks_or_notifications(self) -> None:
        author, place, proposal = self._setup(tg=8_110_001)
        first = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(first.status, PROPOSAL_STATUS_APPROVED, first.message)

        second = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(second.status, "already_decided")

        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            1,
        )
        self.assertEqual(
            int(
                self.session.scalar(
                    select(func.count()).select_from(BrickConflictNotification)
                )
                or 0
            ),
            1,
        )
        # Доставленное уведомление повторно не отправляется.
        pending = list_pending_conflict_notifications(self.session)
        self.assertEqual(len(pending), 1)
        self.assertTrue(
            mark_conflict_notification(
                self.session, pending[0].notification_id, delivered=True
            )
        )
        self.assertEqual(list_pending_conflict_notifications(self.session), [])
        self.assertFalse(
            mark_conflict_notification(
                self.session, pending[0].notification_id, delivered=True
            )
        )

    def test_repeated_create_code_conflicts_is_noop(self) -> None:
        author, place, proposal = self._setup(tg=8_110_002)
        first = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(first.status, PROPOSAL_STATUS_APPROVED, first.message)
        system = get_system_brick(self.session, "код_идемп")
        assert system is not None
        again = create_code_conflicts(
            self.session,
            system_brick_id=system.id,
            code="код_идемп",
        )
        self.session.commit()
        self.assertEqual(again, [])
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            1,
        )


class ConflictResolutionTests(BrickDatabaseTestCase):
    """Требование 3-5: оба исхода, права, stale, 2+ носителей."""

    def _conflict(
        self, *, tg: int, code: str = "цена_р3", bind_before_approval: bool = False
    ):
        author = self.make_user(tg, username=f"a{tg}")
        place = self.make_place(f"Место {tg}")
        self.make_place_admin(place, author)
        local = create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Местная цена",
            value_type="short_text",
            rules={},
        )
        if bind_before_approval:
            # Версия ДО одобрения глобального: код ещё разрешается в местный
            # кирпичик. Именно её затем переключает исход «на глобальный».
            template = get_standard_template(
                self.session, author.telegram_user_id, place.id
            )
            prepared = update_template_section(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                template_id=template.template_id,
                expected_revision=template.revision,
                section="body",
                content=f"[{code}]",
            )
            assert prepared.template is not None
        source_owner = self.make_user(tg + 1, username=f"s{tg}")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code=code,
            title="Источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        conflicts = list_place_conflicts(
            self.session, author.telegram_user_id, place.id
        )
        self.assertEqual(len(conflicts), 1)
        return author, place, local, conflicts[0]

    def test_rename_closes_task_and_keeps_brick_id(self) -> None:
        author, place, local, conflict = self._conflict(tg=8_120_001)

        result = resolve_conflict_by_rename(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
            new_code="цена_новая_р3",
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, CONFLICT_STATUS_RENAMED)
        row = self.session.get(Brick, local.id)
        assert row is not None
        self.assertEqual(row.code, "цена_новая_р3")
        self.assertEqual(row.revision, 2)
        self.assertEqual(
            list_place_conflicts(self.session, author.telegram_user_id, place.id),
            [],
        )
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)

    def test_switch_updates_template_bindings_only_for_new_version(self) -> None:
        author, place, local, conflict = self._conflict(
            tg=8_120_002, bind_before_approval=True
        )
        old_version_id = get_standard_template(
            self.session, author.telegram_user_id, place.id
        ).current_version.version_id
        system = get_system_brick(self.session, "цена_р3")
        assert system is not None

        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, CONFLICT_STATUS_SWITCHED)
        self.assertTrue(result.template_updated)
        current = get_standard_template(self.session, author.telegram_user_id, place.id)
        bindings = {
            binding.code: binding.brick_id
            for binding in current.current_version.bindings
        }
        self.assertEqual(bindings["цена_р3"], system.id)
        # Старая версия не переписана.
        self.session.expire_all()
        old_bindings = list(
            self.session.scalars(
                select(TemplateVersionBrick).where(
                    TemplateVersionBrick.template_version_id == old_version_id
                )
            ).all()
        )
        self.assertIn(local.id, [row.brick_id for row in old_bindings])
        self.assertNotIn(system.id, [row.brick_id for row in old_bindings])
        # Местный кирпичик сохранён.
        self.assertIsNotNone(self.session.get(Brick, local.id))

    def test_outsider_cannot_resolve_task(self) -> None:
        _author, place, _local, conflict = self._conflict(tg=8_120_003)
        outsider = self.make_user(OUTSIDER_TG, username="outsider")
        self.make_place(f"Чужое место {OUTSIDER_TG}")
        self.make_place_admin(self.make_place("Ещё место"), outsider)
        self.assertIsNone(
            get_conflict(self.session, OUTSIDER_TG, conflict.id),
            "задача чужого места не раскрывается",
        )
        with self.assertRaises(BrickPermissionError):
            resolve_conflict_by_rename(
                self.session,
                actor_telegram_user_id=OUTSIDER_TG,
                conflict_id=conflict.id,
                new_code="чужой_код",
            )
        with self.assertRaises(BrickPermissionError):
            resolve_conflict_by_switch(
                self.session,
                actor_telegram_user_id=OUTSIDER_TG,
                conflict_id=conflict.id,
            )
        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_OPEN)

    def test_rename_rejects_busy_code(self) -> None:
        author, place, local, conflict = self._conflict(tg=8_120_004)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="занятый_код",
            title="Занятый",
            value_type="short_text",
            rules={},
        )

        # Контракт продукта: невалидный код приходит как BrickConflictError
        # (обёртка над BrickValidationError), а не «молча» игнорируется.
        with self.assertRaises(BrickConflictError):
            resolve_conflict_by_rename(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                conflict_id=conflict.id,
                new_code="   ",
            )
        result = resolve_conflict_by_rename(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
            new_code="занятый_код",
        )
        self.assertEqual(result.status, "conflict")
        row = self.session.get(Brick, local.id)
        assert row is not None
        self.assertEqual(row.code, "цена_р3")

    def test_stale_conflict_closes_when_local_brick_changed(self) -> None:
        author, place, local, conflict = self._conflict(tg=8_120_005)
        row = self.session.get(Brick, local.id)
        assert row is not None
        row.code = "переименован_вручную"
        self.session.commit()

        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )

        self.assertEqual(result.status, "superseded")
        fresh = self.session.get(BrickConflict, conflict.id)
        assert fresh is not None
        self.assertEqual(fresh.status, "superseded")
        self.assertEqual(
            list_place_conflicts(self.session, author.telegram_user_id, place.id),
            [],
        )

    def test_resolved_task_history_is_immutable_and_kept(self) -> None:
        """
        Решение сохраняется как история: актор и время записаны, терминальный
        статус и снимок задачи не переписываются, удаление запрещено.
        """
        author, place, _local, conflict = self._conflict(tg=8_120_011)
        result = resolve_conflict_by_rename(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
            new_code="история_р3",
        )
        self.assertTrue(result.ok, result.message)

        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_RENAMED)
        self.assertEqual(row.resolved_by_user_id, author.id)
        self.assertIsNotNone(row.resolved_at)
        resolved_at = row.resolved_at

        # Закрытая задача остаётся в истории места и не считается активной.
        history = list_place_conflicts(
            self.session, author.telegram_user_id, place.id, include_closed=True
        )
        self.assertEqual([view.id for view in history], [conflict.id])
        self.assertEqual(history[0].status, CONFLICT_STATUS_RENAMED)
        self.assertEqual(history[0].resolved_at, resolved_at)

        # Триггеры: решение терминально, аудит и снимок неизменяемы, delete нет.
        attempts = (
            "UPDATE brick_conflicts SET status = 'open', resolved_at = NULL, "
            "resolved_by_user_id = NULL WHERE id = :id",
            "UPDATE brick_conflicts SET resolved_by_user_id = NULL WHERE id = :id",
            "UPDATE brick_conflicts SET code = 'подмена' WHERE id = :id",
            "DELETE FROM brick_conflicts WHERE id = :id",
        )
        for statement in attempts:
            with self.engine.connect() as connection:
                with self.assertRaises(IntegrityError, msg=statement):
                    connection.execute(text(statement), {"id": conflict.id})
        # Ни одна попытка не изменила строку.
        self.session.expire_all()
        fresh = self.session.get(BrickConflict, conflict.id)
        assert fresh is not None
        self.assertEqual(fresh.status, CONFLICT_STATUS_RENAMED)
        self.assertEqual(fresh.code, "цена_р3")
        self.assertEqual(fresh.resolved_by_user_id, author.id)

    def test_personal_holder_gets_no_task_and_keeps_code(self) -> None:
        """Личный носитель того же кода не теряет данные и не получает задачу."""
        author = self.make_user(8_120_006, username="first")
        place = self.make_place("Место смешанного кода")
        self.make_place_admin(place, author)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="смешанный_код",
            title="Местный",
            value_type="short_text",
            rules={},
        )
        personal_owner = self.make_user(8_120_007, username="personal")
        personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=personal_owner.telegram_user_id,
            code="смешанный_код",
            title="Личный",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=personal_owner.telegram_user_id,
            source_brick_id=personal.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        # Задача только по месту: у личного кирпичика нет администратора места.
        self.assertEqual([view.place_id for view in result.conflicts], [place.id])
        personal_row = self.session.get(Brick, personal.id)
        assert personal_row is not None
        self.assertEqual(personal_row.code, "смешанный_код")
        self.assertEqual(personal_row.status, "active")

    def test_two_places_with_same_code_each_get_task(self) -> None:
        first_owner = self.make_user(8_120_008, username="first2")
        second_owner = self.make_user(8_120_009, username="second2")
        first_place = self.make_place("Место A")
        second_place = self.make_place("Место B")
        self.make_place_admin(first_place, first_owner)
        self.make_place_admin(second_place, second_owner)
        for place, owner in ((first_place, first_owner), (second_place, second_owner)):
            create_place_brick(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                place_id=place.id,
                code="два_места",
                title="Местный",
                value_type="short_text",
                rules={},
            )
        source_owner = self.make_user(8_120_010, username="author-two")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code="два_места",
            title="Источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(len(result.conflicts), 2)
        self.assertEqual(
            {view.place_id for view in result.conflicts},
            {first_place.id, second_place.id},
        )
        # По одному уведомлению каждому админу места.
        self.assertEqual(
            int(
                self.session.scalar(
                    select(func.count()).select_from(BrickConflictNotification)
                )
                or 0
            ),
            2,
        )


class ConflictRaceTests(BrickDatabaseTestCase):
    """Требование 2: два одновременных решения — ровно одно применяется."""

    def _conflict(self):
        author = self.make_user(8_130_001, username="race-author")
        place = self.make_place("Место гонки")
        self.make_place_admin(place, author)
        local = create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="гонка_р3",
            title="Местный",
            value_type="short_text",
            rules={},
        )
        source_owner = self.make_user(8_130_002, username="race-source")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code="гонка_р3",
            title="Источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        conflicts = list_place_conflicts(
            self.session, author.telegram_user_id, place.id
        )
        return author, place, local, conflicts[0]

    def test_two_concurrent_switches_have_single_winner(self) -> None:
        author, _place, _local, conflict = self._conflict()

        barrier = Barrier(2)

        def worker() -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                return resolve_conflict_by_switch(
                    session,
                    actor_telegram_user_id=author.telegram_user_id,
                    conflict_id=conflict.id,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: worker(), range(2)))

        self.assertEqual(statuses.count(CONFLICT_STATUS_SWITCHED), 1, statuses)
        # Проигравший ВСЕГДА получает честное «уже решено»: ни `error` от
        # UNIQUE(template_id, version), ни ложный `superseded`.
        self.assertEqual(
            [status for status in statuses if status != CONFLICT_STATUS_SWITCHED],
            ["already_resolved"],
            statuses,
        )
        self.session.expire_all()
        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_SWITCHED)

    def test_concurrent_rename_and_switch_only_one_applies(self) -> None:
        author, place, local, conflict = self._conflict()
        barrier = Barrier(2)

        def rename_worker() -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                return resolve_conflict_by_rename(
                    session,
                    actor_telegram_user_id=author.telegram_user_id,
                    conflict_id=conflict.id,
                    new_code="переименованный_р3",
                ).status
            finally:
                session.close()

        def switch_worker() -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                return resolve_conflict_by_switch(
                    session,
                    actor_telegram_user_id=author.telegram_user_id,
                    conflict_id=conflict.id,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(rename_worker), pool.submit(switch_worker)]
            statuses = [future.result() for future in futures]

        winners = [
            status
            for status in statuses
            if status in {CONFLICT_STATUS_RENAMED, CONFLICT_STATUS_SWITCHED}
        ]
        self.assertEqual(len(winners), 1, statuses)
        # Проигравший получает `already_resolved`, а не ложный `superseded`
        # (переименование победителя не делает задачу «протухшей»).
        self.assertEqual(
            [status for status in statuses if status not in winners],
            ["already_resolved"],
            statuses,
        )
        self.session.expire_all()
        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertNotEqual(row.status, CONFLICT_STATUS_OPEN)
        self.assertEqual(
            list_place_conflicts(self.session, author.telegram_user_id, place.id),
            [],
        )
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        # Ни один исход не теряет данные: кирпичик существует.
        self.assertEqual(local_row.status, "active")

    def test_preopened_card_race_never_reports_false_superseded(self) -> None:
        """
        Карточка задачи открыта в обоих сеансах ДО решения.

        Снимок задачи уже лежит в identity map каждой сессии; проигравший
        обязан перечитать состояние под блокировкой записи и ответить
        `already_resolved`, а не объявлять ложный `superseded` из-за
        переименования победителя.
        """
        author, _place, _local, conflict = self._conflict()
        author_tg = author.telegram_user_id
        conflict_id = conflict.id
        barrier = Barrier(2)

        def act(kind: str) -> str:
            session = self.Session()
            try:
                # Открытие карточки: снимок читается до гонки.
                view = get_conflict(session, author_tg, conflict_id)
                assert view is not None and view.is_open
                barrier.wait(timeout=10)
                if kind == "switch":
                    return resolve_conflict_by_switch(
                        session,
                        actor_telegram_user_id=author_tg,
                        conflict_id=conflict_id,
                    ).status
                return resolve_conflict_by_rename(
                    session,
                    actor_telegram_user_id=author_tg,
                    conflict_id=conflict_id,
                    new_code="из_открытой_карточки",
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, "switch"), pool.submit(act, "rename")]
            statuses = [future.result() for future in futures]

        winners = [
            status
            for status in statuses
            if status in {CONFLICT_STATUS_RENAMED, CONFLICT_STATUS_SWITCHED}
        ]
        self.assertEqual(len(winners), 1, statuses)
        self.assertEqual(
            [status for status in statuses if status not in winners],
            ["already_resolved"],
            statuses,
        )

    def test_resolution_holds_write_lock_before_reading_status(self) -> None:
        """
        Решение сериализуется блокировкой записи ДО чтения статуса задачи.

        Без неё проигравший читает `open` из старого снимка (и, если победитель
        переименовал кирпичик, ложно объявляет `superseded`). Проверка
        детерминированная: пока решение не закоммичено, второе соединение не
        может начать запись.
        """
        author, _place, _local, conflict = self._conflict()
        author_tg = author.telegram_user_id
        conflict_id = conflict.id
        observed: dict[str, bool] = {}

        def probe(session, **kwargs):
            # Точка после `BEGIN IMMEDIATE` и до записи версии шаблона.
            other = sqlite3.connect(self.path, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                observed["blocked"] = False
                other.rollback()
            except sqlite3.OperationalError:
                observed["blocked"] = True
            finally:
                other.close()
            return original_rebind(session, **kwargs)

        from brick_store import _rebind_place_template_to_system

        original_rebind = _rebind_place_template_to_system
        with patch("brick_store._rebind_place_template_to_system", side_effect=probe):
            result = resolve_conflict_by_switch(
                self.session,
                actor_telegram_user_id=author_tg,
                conflict_id=conflict_id,
            )

        self.assertEqual(result.status, CONFLICT_STATUS_SWITCHED, result.message)
        self.assertTrue(
            observed.get("blocked"),
            "решение обязано держать блокировку записи: иначе два сеанса "
            "читают статус одновременно и проигравший получает ложный исход",
        )

    def test_integrity_error_after_concurrent_win_is_reported_as_resolved(self) -> None:
        """
        IntegrityError по версии шаблона не должен выглядеть как `error`.

        Если гонку выиграл другой сеанс (задача уже закрыта), конфликт номера
        версии — следствие чужого решения: честный ответ `already_resolved`,
        а не «ошибка базы». Сценарий детерминированный: победитель
        фиксируется отдельным соединением, после чего наша вставка версии
        падает с UNIQUE.
        """
        author, place, _local, conflict = self._conflict()
        author_tg = author.telegram_user_id
        author_id = author.id
        conflict_id = conflict.id

        def winner_commits_then_unique_conflict(session, **kwargs):
            # Победитель — другой процесс: он закрыл задачу и закоммитил.
            other = sqlite3.connect(self.path)
            try:
                other.execute(
                    "UPDATE brick_conflicts "
                    "SET status = 'switched', resolved_at = '2026-01-01 00:00:00', "
                    "resolved_by_user_id = ? WHERE id = ?",
                    (author_id, conflict_id),
                )
                other.commit()
            finally:
                other.close()
            raise IntegrityError(
                "INSERT INTO template_versions", {}, Exception("UNIQUE constraint")
            )

        with (
            patch("brick_store._begin_immediate_write", lambda session: None),
            patch(
                "brick_store._rebind_place_template_to_system",
                side_effect=winner_commits_then_unique_conflict,
            ),
        ):
            result = resolve_conflict_by_switch(
                self.session,
                actor_telegram_user_id=author_tg,
                conflict_id=conflict_id,
            )

        self.assertEqual(result.status, "already_resolved", result.message)
        self.assertNotEqual(result.status, "error")
        self.session.expire_all()
        row = self.session.get(BrickConflict, conflict_id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_SWITCHED)

    def test_rename_integrity_error_after_concurrent_win_is_not_error(self) -> None:
        """Тот же контракт для исхода «переименовать»."""
        author, _place, local, conflict = self._conflict()
        author_tg = author.telegram_user_id
        author_id = author.id
        conflict_id = conflict.id

        def winner_commits_then_unique_conflict(session, **kwargs):
            other = sqlite3.connect(self.path)
            try:
                other.execute(
                    "UPDATE brick_conflicts "
                    "SET status = 'renamed', resolved_at = '2026-01-01 00:00:00', "
                    "resolved_by_user_id = ? WHERE id = ?",
                    (author_id, conflict_id),
                )
                other.commit()
            finally:
                other.close()
            raise IntegrityError("UPDATE bricks", {}, Exception("UNIQUE constraint"))

        with (
            patch("brick_store._begin_immediate_write", lambda session: None),
            patch(
                "brick_store._rename_place_brick",
                side_effect=winner_commits_then_unique_conflict,
            ),
        ):
            result = resolve_conflict_by_rename(
                self.session,
                actor_telegram_user_id=author_tg,
                conflict_id=conflict_id,
                new_code="после_гонки",
            )

        self.assertEqual(result.status, "already_resolved", result.message)
        row = self.session.get(Brick, local.id)
        assert row is not None
        # Переименование не применилось: местный кирпичик держит прежний код.
        self.assertEqual(row.code, "гонка_р3")


class ConflictSameTemplateRaceTests(BrickDatabaseTestCase):
    """
    Два РАЗНЫХ конфликта одного места переключаются одновременно.

    Оба исхода должны примениться: сериализация не даёт вставить две версии
    шаблона с одинаковым номером (`UNIQUE(template_id, version)`), а номер
    читается уже под блокировкой записи.
    """

    def test_two_conflicts_of_one_place_each_get_own_template_version(self) -> None:
        author = self.make_user(8_150_001, username="same-tpl")
        place = self.make_place("Место двух кодов")
        self.make_place_admin(place, author)
        codes = ("код_первый", "код_второй")
        for code in codes:
            create_place_brick(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                place_id=place.id,
                code=code,
                title="Местный",
                value_type="short_text",
                rules={},
            )
        # Привязки к местным кирпичикам создаются ДО одобрения глобальных.
        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        prepared = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content=f"x [{codes[0]}] y [{codes[1]}]",
        )
        self.assertEqual(prepared.status, "updated", prepared.message)
        revision_before = get_standard_template(
            self.session, author.telegram_user_id, place.id
        ).revision

        source_owner = self.make_user(8_150_002, username="same-src")
        for code in codes:
            source = create_personal_brick(
                self.session,
                actor_telegram_user_id=source_owner.telegram_user_id,
                code=code,
                title="Источник",
                value_type="short_text",
                rules={},
            )
            proposal = create_brick_proposal(
                self.session,
                actor_telegram_user_id=source_owner.telegram_user_id,
                source_brick_id=source.id,
                scope="system",
            )
            self.make_user(self.SUPERADMIN_TG, username="boss")
            approved = approve_brick_proposal(
                self.session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                proposal_id=proposal.id,
            )
            self.assertEqual(
                approved.status, PROPOSAL_STATUS_APPROVED, approved.message
            )

        author_tg = author.telegram_user_id
        conflict_ids = [
            view.id for view in list_place_conflicts(self.session, author_tg, place.id)
        ]
        self.assertEqual(len(conflict_ids), 2)
        barrier = Barrier(2)

        def worker(conflict_id: int) -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                return resolve_conflict_by_switch(
                    session,
                    actor_telegram_user_id=author_tg,
                    conflict_id=conflict_id,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(worker, conflict_ids))

        # Оба исхода применились; ни один не упал в `error` из-за номера версии.
        self.assertEqual(statuses, [CONFLICT_STATUS_SWITCHED] * 2, statuses)
        self.session.expire_all()
        current = get_standard_template(self.session, author_tg, place.id)
        self.assertEqual(current.revision, revision_before + 2)
        system_ids = {
            brick.id
            for brick in (get_system_brick(self.session, code) for code in codes)
            if brick is not None
        }
        self.assertEqual(len(system_ids), 2)
        bindings = {
            binding.code: binding.brick_id
            for binding in current.current_version.bindings
        }
        for code in codes:
            self.assertIn(bindings[code], system_ids)
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)


class ConflictRestartTests(BrickDatabaseTestCase):
    """Требование 4: состояние задачи переживает перезапуск процесса."""

    def test_task_survives_new_session_and_new_engine(self) -> None:
        author = self.make_user(8_140_001, username="restart")
        place = self.make_place("Место рестарта")
        self.make_place_admin(place, author)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="рестарт_р3",
            title="Местный",
            value_type="short_text",
            rules={},
        )
        source_owner = self.make_user(8_140_002, username="restart-src")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code="рестарт_р3",
            title="Источник",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        conflict_id = result.conflicts[0].id
        # Значение нужно после close(): ORM-объект тогда отсоединён.
        author_tg = author.telegram_user_id
        self.session.close()

        # Имитация рестарта: новый engine/session на том же файле БД.
        engine = self.make_engine_copy()
        session = self.Session(bind=engine)
        try:
            view = get_conflict(session, author_tg, conflict_id)
            assert view is not None
            self.assertEqual(view.status, CONFLICT_STATUS_OPEN)
            resolved = resolve_conflict_by_switch(
                session,
                actor_telegram_user_id=author_tg,
                conflict_id=conflict_id,
            )
            self.assertTrue(resolved.ok, resolved.message)
        finally:
            session.close()
            engine.dispose()

    def make_engine_copy(self):
        from sqlalchemy import create_engine

        return create_engine(f"sqlite:///{self.path}")


class ConflictMigrationTests(unittest.TestCase):
    """Требование 7: миграция 19 на заполненной БД."""

    def setUp(self) -> None:
        import os
        import shutil

        from tests.stage19a_helpers import make_engine, temp_db_path

        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (9_900_001,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = temp_db_path("poster-r3-migration-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    def test_latest_version_is_twenty_three(self) -> None:
        # U1a добавил 21 (`place_city`) и 22 (`builtin_templates_v4`)
        # поверх R4-овской 20; все применяются штатно.
        self.assertEqual(LATEST_VERSION, 23)

    def test_migration_19_on_filled_database_keeps_data(self) -> None:
        from sqlalchemy import text

        from migrations import DEFAULT_MIGRATIONS, run_migrations

        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:18]), 18)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, telegram_user_id, created_at) "
                    "VALUES (1, 77001, '2026-01-01 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO places (id, created_by_user_id, created_at) "
                    "VALUES (1, 1, '2026-01-01 00:00:00')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO bricks (id, code, title, type, scope, source, "
                    "rules_json, status, place_id, created_by_user_id, "
                    "created_at, updated_at, revision) VALUES "
                    "(500, 'миграция_р3', 'Местный', 'short_text', 'place', "
                    "'ask', NULL, 'active', 1, 1, '2026-01-01 00:00:00', "
                    "'2026-01-01 00:00:00', 1)"
                )
            )

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        with self.engine.begin() as connection:
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                _first_cell(connection, "PRAGMA integrity_check"),
                "ok",
            )
            # Существующие данные не тронуты.
            self.assertEqual(
                connection.exec_driver_sql(
                    "SELECT code, scope, revision FROM bricks WHERE id = 500"
                ).fetchone(),
                ("миграция_р3", "place", 1),
            )
            # Новые таблицы пусты и готовы к работе.
            self.assertEqual(
                _first_cell(connection, "SELECT count(*) FROM brick_conflicts"),
                0,
            )
            self.assertEqual(
                _first_cell(
                    connection, "SELECT count(*) FROM brick_conflict_notifications"
                ),
                0,
            )
        # Идемпотентность: повторный запуск ничего не меняет.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        with self.engine.begin() as connection:
            self.assertEqual(
                _first_cell(connection, "SELECT count(*) FROM brick_conflicts"),
                0,
            )


class ConflictUiTests(unittest.TestCase):
    """Требование 5-6: карточка места, оба исхода и forged callback."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _prepare(self, h, *, code: str):
        with h.session_factory() as session:
            author = get_or_create_user(
                session, AUTHOR_TG, username="author", full_name="Автор"
            )
            get_or_create_user(
                session, SUPERADMIN_TG, username="boss", full_name="Босс"
            )
            place = create_place(
                session,
                "Место конфликта",
                city="Тестовый город",
                created_by_user_id=author.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=author.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            create_place_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                place_id=place.id,
                code=code,
                title="Местный",
                value_type="short_text",
                rules={},
            )
            source = create_personal_brick_source(session, AUTHOR_TG, code)
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                source_brick_id=source,
                scope="system",
            )
            return int(place.id), int(proposal.id)

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_place_card_shows_active_conflict_and_runs_outcomes(self, h):
        place_id, proposal_id = await self._prepare(h, code="ui_конфликт")
        await h.text("/admin", user_id=SUPERADMIN_TG)
        await h.callback("adm_bprop_global", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPERADMIN_TG)
        self.assertIn("✅ Одобрено", h.screen_text())

        # Своя сессия кабинета: список мест → карточка места.
        await h.text("/admin", user_id=AUTHOR_TG)
        self.assertIn("Мои места", h.screen_text())
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Активные задачи конфликтов", h.screen_text())
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Конфликты кодов", h.screen_text())

        with h.session_factory() as session:
            conflict_id = int(
                session.scalar(
                    select(BrickConflict.id).where(BrickConflict.place_id == place_id)
                )
            )
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Задача разрешения конфликта", h.screen_text())
        await h.callback(f"adm_bconflict_rename:{conflict_id}", user_id=AUTHOR_TG)
        await h.text("ui_переименован", user_id=AUTHOR_TG)
        self.assertIn("переименован", h.screen_text().lower())
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_RENAMED)

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_place_admin_gets_single_notification_about_conflict(self, h):
        """Идемпотентность уведомления: админ места получает ровно одно."""
        _place_id, proposal_id = await self._prepare(h, code="ui_уведомление")
        await h.text("/admin", user_id=SUPERADMIN_TG)
        await h.callback("adm_bprop_global", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPERADMIN_TG)

        def conflict_notices() -> list[str]:
            return [
                str(call.get("text") or "")
                for call in h.calls
                if call["type"] == "message"
                and int(call.get("chat_id") or 0) == AUTHOR_TG
                and "Конфликт кодов" in str(call.get("text") or "")
            ]

        self.assertEqual(len(conflict_notices()), 1)
        self.assertIn("ui_уведомление", conflict_notices()[0])

        with h.session_factory() as session:
            notifications = list(
                session.scalars(select(BrickConflictNotification)).all()
            )
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].status, "sent")
        self.assertEqual(notifications[0].attempts, 1)

        # Повторное открытие/подтверждение не рассылает уведомление снова.
        await h.callback("adm_bprop_approve_yes", user_id=SUPERADMIN_TG)
        self.assertEqual(len(conflict_notices()), 1)
        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count()).select_from(BrickConflictNotification)
                    )
                    or 0
                ),
                1,
            )

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_outsider_and_forged_callbacks_are_rejected(self, h):
        place_id, proposal_id = await self._prepare(h, code="ui_чужой")
        await h.text("/admin", user_id=SUPERADMIN_TG)
        await h.callback("adm_bprop_global", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPERADMIN_TG)
        with h.session_factory() as session:
            conflict_id = int(
                session.scalar(
                    select(BrickConflict.id).where(BrickConflict.place_id == place_id)
                )
            )
        # Чужой пользователь не админ места.
        await h.text("/admin", user_id=OUTSIDER_TG)
        await h.callback(f"adm_bconflicts:{place_id}", user_id=OUTSIDER_TG)
        self.assertNotIn("Конфликты кодов", h.screen_text())
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=OUTSIDER_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_OPEN)

        # Подделанная кнопка решения в чужой/неверной фазе — stale.
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_OPEN)


def _first_cell(connection: Connection, sql: str) -> Any:
    """Первая колонка первой строки запроса (для PRAGMA и count)."""
    row = connection.exec_driver_sql(sql).fetchone()
    assert row is not None
    return row[0]


def create_personal_brick_source(session, telegram_user_id: int, code: str) -> int:
    """Личный кирпичик-источник глобального предложения (не шаблон)."""
    definition = create_personal_brick(
        session,
        actor_telegram_user_id=telegram_user_id,
        code=code,
        title="Личный источник",
        value_type="short_text",
        rules={},
    )
    return int(definition.id)
