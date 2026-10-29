# tests/test_poster2_r3_conflicts_tester.py
"""
R3 (Poster 2.1) — независимые проверки требований владельца.

Проверяется ТРЕБОВАНИЕ, а не реализация:

1. Глобальное (системное) предложение при существующем МЕСТНОМ носителе того
   же кода одобряется; глобальный кирпичик действует для НОВЫХ привязок;
   существующие версии шаблона и снимки не переписываются.
2. Автоматически создаются задачи по местам и идемпотентные уведомления
   админам мест; повторное одобрение/повторный прогон дублей не создаёт;
   история решений сохраняется.
3. Обязательное разрешение: «переименовать местный» ИЛИ «переключиться на
   глобальный» (с привязками шаблона места в НОВОЙ версии); активная задача
   видна админу места в карточке места; после решения — не видна.
4. Миграция 19: свежая и заполненная БД, идемпотентность,
   integrity_check/foreign_key_check, существующие таблицы не перестраивались.

Негатив: не-админ места (IDOR/forged callback), stale-кнопки, личный носитель
кода (у личного нет места → нет задачи), коды без конфликтов (регресс).

Все базы — временные SQLite; реальная `post_bot.db`, `.env` и секреты не
открываются. Сценарии кабинета идут через `Application.process_update` +
FakeBot (`tests/integration_harness.py`).
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any
from unittest.mock import patch

from sqlalchemy import create_engine, func, select

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
    resolve_brick_code_candidates,
    resolve_conflict_by_rename,
    resolve_conflict_by_switch,
)
from migrations import LATEST_VERSION
from models import (
    Brick,
    BrickConflict,
    BrickConflictNotification,
    PlaceAdmin,
    TemplateSnapshotBrick,
    TemplateVersionBrick,
    utcnow,
)
from place_store import create_place
from template_store import (
    create_template_snapshot,
    get_standard_template,
    get_template_snapshot,
    render_template_snapshot,
    update_template_section,
)
from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage22a_helpers import BrickDatabaseTestCase
from user_store import get_or_create_user

INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR

AUTHOR_TG = 8_200_001
SUPERADMIN_TG = 8_200_002
SOURCE_TG = 8_200_003
OUTSIDER_TG = 8_200_004
CONTACT = None


class R3Base(BrickDatabaseTestCase):
    """Общие конструкторы: место + админ + местный носитель + источник."""

    SUPERADMIN_TG = SUPERADMIN_TG

    def make_author_place(
        self, *, tg: int, code: str, bind_before_approval: bool = False
    ) -> tuple[Any, Any, Any]:
        author = self.make_user(tg, username=f"author{tg}")
        place = self.make_place(f"Место {tg}")
        self.make_place_admin(place, author)
        local = create_place_brick(
            session=self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Местный кирпичик",
            value_type="short_text",
            rules={},
        )
        if bind_before_approval:
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
            self.assertEqual(prepared.status, "updated", prepared.message)
        return author, place, local

    def approve_global_code(self, *, code: str, tg: int) -> Any:
        source_owner = self.make_user(tg, username=f"source{tg}")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code=code,
            title="Личный источник",
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
        return approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )

    def make_conflict(self, *, tg: int, code: str, bind_before_approval: bool = True):
        author, place, local = self.make_author_place(
            tg=tg, code=code, bind_before_approval=bind_before_approval
        )
        result = self.approve_global_code(code=code, tg=tg + 1)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        conflicts = list_place_conflicts(
            self.session, author.telegram_user_id, place.id
        )
        self.assertEqual(len(conflicts), 1, "задача по месту")
        return author, place, local, conflicts[0]


# ---------------------------------------------------------------------------
# 1. Конфликтный сценарий целиком + неизменность снимков
# ---------------------------------------------------------------------------


class R3FullConflictTests(R3Base):
    def test_global_approval_wins_for_new_bindings_and_creates_task(self) -> None:
        code = "цена_полный"
        author, place, local = self.make_author_place(
            tg=8_201_001, code=code, bind_before_approval=True
        )

        # Снимок шаблона ДО одобрения: он должен остаться неизменным.
        template_before = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        snapshot_before = create_template_snapshot(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template_before.template_id,
        )
        old_version_id = template_before.current_version.version_id

        result = self.approve_global_code(code=code, tg=8_201_002)

        # (а) Одобрение прошло, глобальный кирпичик существует.
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertNotIn("error", (result.message or "").lower())
        system = get_system_brick(self.session, code)
        self.assertIsNotNone(system, "глобальный кирпичик создан")
        assert system is not None
        self.assertEqual(system.scope, "system")

        # (б) Задача создана и активна, местный кирпичик не переписан.
        conflicts = list_place_conflicts(
            self.session, author.telegram_user_id, place.id
        )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status, CONFLICT_STATUS_OPEN)
        self.assertEqual(conflicts[0].system_brick_id, system.id)
        self.assertEqual(conflicts[0].local_brick_id, local.id)
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 1)
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        self.assertEqual(local_row.code, code)
        self.assertEqual(local_row.status, "active")
        self.assertEqual(local_row.revision, 1)

        # (в) Новые привязки резолвятся на глобальный кирпичик.
        candidates = resolve_brick_code_candidates(
            self.session,
            code,
            viewer_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
        )
        assert candidates.selected is not None
        self.assertEqual(candidates.selected.id, system.id)
        self.assertEqual(candidates.selected.scope, "system")

        updated = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template_before.template_id,
            expected_revision=get_standard_template(
                self.session, author.telegram_user_id, place.id
            ).revision,
            section="body",
            content=f"текст [{code}]",
        )
        self.assertEqual(updated.status, "updated", updated.message)
        assert updated.template is not None
        bindings = {
            binding.code: binding.brick_id
            for binding in updated.template.current_version.bindings
        }
        self.assertEqual(bindings[code], system.id)

        # (г) Старая версия и снимок не изменились.
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
        snapshot_bindings = list(
            self.session.scalars(
                select(TemplateSnapshotBrick).where(
                    TemplateSnapshotBrick.template_snapshot_id
                    == snapshot_before.snapshot_id
                )
            ).all()
        )
        self.assertIn(local.id, [row.brick_id for row in snapshot_bindings])
        self.assertNotIn(system.id, [row.brick_id for row in snapshot_bindings])

    def test_global_approval_without_local_holders_creates_no_task(self) -> None:
        """Регресс: нет местных носителей — нет задач и уведомлений."""
        code = "цена_без_конфликта"
        result = self.approve_global_code(code=code, tg=8_202_001)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(list(result.conflicts), [])
        self.assertIsNotNone(get_system_brick(self.session, code))
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            0,
        )
        self.assertEqual(
            int(
                self.session.scalar(
                    select(func.count()).select_from(BrickConflictNotification)
                )
                or 0
            ),
            0,
        )
        self.assertEqual(list_pending_conflict_notifications(self.session), [])

    def test_personal_holder_gets_no_task(self) -> None:
        """Личный носитель кода: у личного нет места — задачи/уведомления нет."""
        code = "цена_личный_р3"
        owner = self.make_user(8_203_001, username="personal-owner")
        personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code=code,
            title="Личный",
            value_type="short_text",
            rules={},
        )
        result = self.approve_global_code(code=code, tg=8_203_002)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(list(result.conflicts), [])
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            0,
        )
        fresh = self.session.get(Brick, personal.id)
        assert fresh is not None
        self.assertEqual(fresh.code, code)
        self.assertEqual(fresh.status, "active")


# ---------------------------------------------------------------------------
# 2. Идемпотентность задач/уведомлений и сохранение истории
# ---------------------------------------------------------------------------


class R3IdempotencyTests(R3Base):
    def test_repeat_approval_does_not_duplicate_task_or_notification(self) -> None:
        code = "цена_идемпотентно"
        author, place, _local = self.make_author_place(tg=8_210_001, code=code)
        source_owner = self.make_user(8_210_002, username="src-idem")
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

        # Повторный «прогон» создания задач по тому же коду — no-op.
        system = get_system_brick(self.session, code)
        assert system is not None
        self.assertEqual(
            create_code_conflicts(self.session, system_brick_id=system.id, code=code),
            [],
        )
        self.session.commit()
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            1,
        )
        self.assertEqual(
            list_place_conflicts(self.session, author.telegram_user_id, place.id)[
                0
            ].status,
            CONFLICT_STATUS_OPEN,
        )

    def test_notification_delivery_is_exactly_once(self) -> None:
        _author, _place, _local, _conflict = self.make_conflict(
            tg=8_211_001, code="цена_уведомление"
        )
        pending = list_pending_conflict_notifications(self.session)
        self.assertEqual(len(pending), 1)
        notification = pending[0]
        self.assertTrue(
            mark_conflict_notification(
                self.session, notification.notification_id, delivered=True
            )
        )
        self.assertEqual(list_pending_conflict_notifications(self.session), [])
        # Повторная отметка (повторный прогон рассылки) не проходит.
        self.assertFalse(
            mark_conflict_notification(
                self.session, notification.notification_id, delivered=True
            )
        )
        row = self.session.get(BrickConflictNotification, notification.notification_id)
        assert row is not None
        self.assertEqual(row.status, "sent")
        self.assertEqual(row.attempts, 1)

    def test_resolved_task_history_is_kept_and_active_list_is_empty(self) -> None:
        author, place, _local, conflict = self.make_conflict(
            tg=8_212_001, code="цена_история"
        )
        result = resolve_conflict_by_rename(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
            new_code="цена_история_новая",
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(
            list_place_conflicts(self.session, author.telegram_user_id, place.id), []
        )
        history = list_place_conflicts(
            self.session, author.telegram_user_id, place.id, include_closed=True
        )
        self.assertEqual([view.id for view in history], [conflict.id])
        self.assertEqual(history[0].status, CONFLICT_STATUS_RENAMED)
        self.assertIsNotNone(history[0].resolved_at)
        self.assertEqual(history[0].resolved_by_user_id, author.id)
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)


# ---------------------------------------------------------------------------
# 3. Гонка двух решений одной задачи (два независимых сеанса)
# ---------------------------------------------------------------------------


class R3RaceTests(R3Base):
    def test_two_sessions_race_single_winner_other_explicitly_rejected(self) -> None:
        author, place, local, conflict = self.make_conflict(
            tg=8_220_001, code="цена_гонка"
        )
        system = get_system_brick(self.session, "цена_гонка")
        assert system is not None
        old_version_id = get_standard_template(
            self.session, author.telegram_user_id, place.id
        ).current_version.version_id
        author_tg = author.telegram_user_id
        conflict_id = conflict.id
        self.session.close()

        barrier = Barrier(2)

        def act(kind: str) -> str:
            session = self.Session()
            try:
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
                    new_code="цена_гонка_новая",
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, "switch"), pool.submit(act, "rename")]
            statuses = [future.result() for future in futures]

        applied = [
            status
            for status in statuses
            if status in {CONFLICT_STATUS_RENAMED, CONFLICT_STATUS_SWITCHED}
        ]
        # Ровно одно решение применено; проигравший получает явный отказ.
        self.assertEqual(len(applied), 1, statuses)
        loser = [status for status in statuses if status not in applied]
        self.assertEqual(len(loser), 1, statuses)
        self.assertIn(
            loser[0],
            {"already_resolved", "superseded"},
            f"проигравший должен получить явный отказ, а не молчание: {statuses}",
        )

        session = self.Session()
        try:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, applied[0])
            self.assertIsNotNone(row.resolved_at)
            self.assertEqual(row.resolved_by_user_id, row.resolved_by_user_id)
            local_row = session.get(Brick, local.id)
            assert local_row is not None
            self.assertEqual(local_row.status, "active")
            if applied[0] == CONFLICT_STATUS_RENAMED:
                self.assertEqual(local_row.code, "цена_гонка_новая")
            else:
                self.assertEqual(local_row.code, "цена_гонка")
            template = get_standard_template(session, author_tg, place.id)
            new_ids = [
                binding.brick_id for binding in template.current_version.bindings
            ]
            if applied[0] == CONFLICT_STATUS_SWITCHED:
                self.assertIn(system.id, new_ids)
                self.assertNotEqual(template.current_version.version_id, old_version_id)
            else:
                self.assertNotIn(system.id, new_ids)
                self.assertEqual(template.current_version.version_id, old_version_id)
            # Ни одна задача не осталась активной и дублей нет.
            self.assertEqual(count_open_conflicts_for_place(session, place.id), 0)
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count()).select_from(BrickConflictNotification)
                    )
                    or 0
                ),
                1,
            )
        finally:
            session.close()


# ---------------------------------------------------------------------------
# 4. Рестарт процесса (новый engine на той же БД) и доступность задачи
# ---------------------------------------------------------------------------


class R3RestartTests(R3Base):
    def test_active_task_is_visible_and_resolvable_after_engine_restart(self) -> None:
        author, place, _local, conflict = self.make_conflict(
            tg=8_230_001, code="цена_рестарт"
        )
        author_tg = author.telegram_user_id
        place_id = place.id
        conflict_id = conflict.id
        self.session.close()

        restarted_engine = create_engine(f"sqlite:///{self.path}")
        self.addCleanup(restarted_engine.dispose)
        session = self.Session(bind=restarted_engine)
        try:
            view = get_conflict(session, author_tg, conflict_id)
            self.assertIsNotNone(view, "активная задача доступна после рестарта")
            assert view is not None
            self.assertEqual(view.status, CONFLICT_STATUS_OPEN)
            self.assertEqual(view.place_id, place_id)
            self.assertEqual(count_open_conflicts_for_place(session, place_id), 1)
            result = resolve_conflict_by_switch(
                session, actor_telegram_user_id=author_tg, conflict_id=conflict_id
            )
            self.assertTrue(result.ok, result.message)
            self.assertEqual(result.status, CONFLICT_STATUS_SWITCHED)
        finally:
            session.close()


# ---------------------------------------------------------------------------
# 5. Оба исхода решения + обязательность
# ---------------------------------------------------------------------------


class R3OutcomeTests(R3Base):
    def test_rename_local_closes_task_and_keeps_bindings_by_id(self) -> None:
        author, place, local, conflict = self.make_conflict(
            tg=8_240_001, code="цена_переименовать"
        )
        old_version = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        old_version_id = old_version.current_version.version_id

        result = resolve_conflict_by_rename(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
            new_code="цена_локальная_новая",
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, CONFLICT_STATUS_RENAMED)
        fresh = self.session.get(Brick, local.id)
        assert fresh is not None
        self.assertEqual(fresh.code, "цена_локальная_новая")
        self.assertEqual(fresh.revision, 2)
        self.assertEqual(fresh.status, "active")
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)
        # Версия шаблона не создавалась: переименование не трогает шаблон.
        self.session.expire_all()
        current = get_standard_template(self.session, author.telegram_user_id, place.id)
        self.assertEqual(current.current_version.version_id, old_version_id)

    def test_switch_to_global_updates_new_template_version_only(self) -> None:
        author, place, local, conflict = self.make_conflict(
            tg=8_241_001, code="цена_переключить"
        )
        system = get_system_brick(self.session, "цена_переключить")
        assert system is not None
        before = get_standard_template(self.session, author.telegram_user_id, place.id)
        old_version_id = before.current_version.version_id
        old_revision = before.revision

        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )

        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, CONFLICT_STATUS_SWITCHED)
        self.assertTrue(result.template_updated)
        current = get_standard_template(self.session, author.telegram_user_id, place.id)
        self.assertEqual(current.revision, old_revision + 1)
        self.assertNotEqual(current.current_version.version_id, old_version_id)
        bindings = {
            binding.code: binding.brick_id
            for binding in current.current_version.bindings
        }
        self.assertEqual(bindings["цена_переключить"], system.id)
        # Старая версия не переписана, местный кирпичик сохранён.
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
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        self.assertEqual(local_row.code, "цена_переключить")
        self.assertEqual(local_row.status, "active")
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)

    def test_switch_without_template_binding_still_closes_task(self) -> None:
        """Исход работает и без привязки кода в шаблоне: задача закрывается."""
        author, place, local, conflict = self.make_conflict(
            tg=8_242_001, code="цена_без_привязки", bind_before_approval=False
        )
        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.template_updated)
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)
        self.assertIsNotNone(self.session.get(Brick, local.id))


# ---------------------------------------------------------------------------
# 6. Негатив модели: права, чужое место, stale, личный носитель
# ---------------------------------------------------------------------------


class R3NegativeTests(R3Base):
    def test_non_admin_cannot_resolve_conflict(self) -> None:
        _author, _place, _local, conflict = self.make_conflict(
            tg=8_250_001, code="цена_чужой"
        )
        outsider = self.make_user(OUTSIDER_TG, username="outsider")
        other_place = self.make_place("Чужое место")
        self.make_place_admin(other_place, outsider)

        # Задача чужого места не раскрывается даже по точному id.
        self.assertIsNone(get_conflict(self.session, OUTSIDER_TG, conflict.id))
        for action in ("rename", "switch"):
            with self.assertRaises(BrickPermissionError, msg=action):
                if action == "rename":
                    resolve_conflict_by_rename(
                        self.session,
                        actor_telegram_user_id=OUTSIDER_TG,
                        conflict_id=conflict.id,
                        new_code="чужое_имя",
                    )
                else:
                    resolve_conflict_by_switch(
                        self.session,
                        actor_telegram_user_id=OUTSIDER_TG,
                        conflict_id=conflict.id,
                    )
        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_OPEN)

    def test_superseded_task_cannot_be_resolved_twice(self) -> None:
        author, place, local, conflict = self.make_conflict(
            tg=8_251_001, code="цена_устаревшая"
        )
        # Местный носитель исчез сам (переименован вне сценария) — задача stale.
        row = self.session.get(Brick, local.id)
        assert row is not None
        row.code = "цена_переименована_вручную"
        self.session.commit()

        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertEqual(result.status, "superseded")
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)
        again = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertEqual(again.status, "already_resolved")

    def test_unknown_conflict_id_returns_not_found(self) -> None:
        author = self.make_user(8_252_001, username="nobody")
        self.make_place("Место без задач")
        self.assertIsNone(get_conflict(self.session, author.telegram_user_id, 999_999))
        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=999_999,
        )
        self.assertEqual(result.status, "not_found")
        self.assertFalse(result.ok)


# ---------------------------------------------------------------------------
# 7. UI: индикация в карточке места, оба исхода, рестарт, negatives
# ---------------------------------------------------------------------------


class R3UiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", CONTACT),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _seed(h: Any, *, code: str) -> tuple[int, int, int]:
        """Место админа, местный кирпичик кода и одобренное глобальное предложение."""
        with h.session_factory() as session:
            author = get_or_create_user(
                session, AUTHOR_TG, username="author", full_name="Автор"
            )
            get_or_create_user(
                session, SOURCE_TG, username="source", full_name="Источник"
            )
            get_or_create_user(
                session, SUPERADMIN_TG, username="boss", full_name="Босс"
            )
            place = create_place(session, "Место UI", created_by_user_id=author.id)
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=author.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            local = create_place_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                place_id=place.id,
                code=code,
                title="Местный",
                value_type="short_text",
                rules={},
            )
            # Привязка кода к местному кирпичику ДО одобрения глобального:
            # именно такую версию должен переключить исход «на глобальный».
            template = get_standard_template(session, AUTHOR_TG, place.id)
            prepared = update_template_section(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                template_id=template.template_id,
                expected_revision=template.revision,
                section="body",
                content=f"текст [{code}]",
            )
            assert prepared.status == "updated", prepared.message
            personal = create_personal_brick(
                session,
                actor_telegram_user_id=SOURCE_TG,
                code=code,
                title="Личный источник",
                value_type="short_text",
                rules={},
            )
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=SOURCE_TG,
                source_brick_id=personal.id,
                scope="system",
            )
            result = approve_brick_proposal(
                session,
                actor_telegram_user_id=SUPERADMIN_TG,
                proposal_id=proposal.id,
            )
            assert result.status == PROPOSAL_STATUS_APPROVED, result.message
            conflict_id = int(result.conflicts[0].id)
            return int(place.id), conflict_id, int(local.id)

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_place_card_shows_task_until_resolved(self, h) -> None:
        place_id, conflict_id, _local = self._seed(h, code="ui_индикация")

        await h.text("/admin", user_id=AUTHOR_TG)
        self.assertIn("Мои места", h.screen_text())
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        card = h.screen_text()
        self.assertIn("Активные задачи конфликтов", card)
        buttons = " ".join(label for label, _ in h.screen_buttons())
        self.assertIn("Конфликты кодов (1)", buttons)

        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Конфликты кодов", h.screen_text())
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Задача разрешения конфликта", h.screen_text())
        labels = " ".join(label for label, _ in h.screen_buttons())
        self.assertIn("Переименовать местный", labels)
        self.assertIn("Переключиться на глобальный", labels)

        await h.callback(f"adm_bconflict_switch:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_SWITCHED)

        # После решения индикации активной задачи в карточке нет.
        # Возврат в главное меню сбрасывает сессию кабинета (как у пользователя).
        await h.callback("main_menu", user_id=AUTHOR_TG)
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        card_after = h.screen_text()
        self.assertNotIn("Активные задачи конфликтов", card_after)
        buttons_after = " ".join(label for label, _ in h.screen_buttons())
        self.assertIn("Конфликты кодов (0)", buttons_after)
        # В списке задач тоже нет активных.
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Активных задач нет", h.screen_text())
        list_labels = " ".join(label for label, _ in h.screen_buttons())
        self.assertNotIn("adm_bconflict:", list_labels)

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_active_task_survives_harness_restart(self, h) -> None:
        place_id, conflict_id, _local = self._seed(h, code="ui_рестарт")
        await h.restart()

        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Активные задачи конфликтов", h.screen_text())
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_rename:{conflict_id}", user_id=AUTHOR_TG)
        await h.text("ui_после_рестарта", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_RENAMED)
            brick = session.get(Brick, row.local_brick_id)
            assert brick is not None
            self.assertEqual(brick.code, "ui_после_рестарта")

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_outsider_and_forged_callbacks_change_nothing(self, h) -> None:
        place_id, conflict_id, local_id = self._seed(h, code="ui_чужой_р3")
        with h.session_factory() as session:
            outsider = get_or_create_user(
                session, OUTSIDER_TG, username="outsider", full_name="Чужой"
            )
            foreign_place = create_place(
                session, "Чужое место", created_by_user_id=outsider.id
            )
            session.add(
                PlaceAdmin(
                    place_id=foreign_place.id,
                    user_id=outsider.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            foreign_place_id = int(foreign_place.id)

        # Чужой админ другого места: карточка чужого места не открывается.
        await h.text("/admin", user_id=OUTSIDER_TG)
        await h.callback(f"adm_bconflicts:{place_id}", user_id=OUTSIDER_TG)
        self.assertNotIn("Конфликты кодов", h.screen_text())
        # Подделанные кнопки решений чужой задачи — без изменений.
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=OUTSIDER_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=OUTSIDER_TG)
        await h.callback(f"adm_bconflict_rename:{conflict_id}", user_id=OUTSIDER_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_OPEN)
            brick = session.get(Brick, local_id)
            assert brick is not None
            self.assertEqual(brick.code, "ui_чужой_р3")
            self.assertEqual(count_open_conflicts_for_place(session, place_id), 1)
        self.assertIn(
            f"adm_place:{foreign_place_id}",
            " ".join(data or "" for _, data in h.screen_buttons()),
        )

    @unittest.skipIf(
        INTEGRATION_IMPORT_ERROR is not None,
        f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
    )
    @scenario_test
    async def test_stale_and_forged_phase_callbacks_do_not_resolve(self, h) -> None:
        place_id, conflict_id, local_id = self._seed(h, code="ui_stale")
        # Кнопка решения без входа в нужную фазу (подделка/старый экран).
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_rename:{conflict_id}", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_OPEN)

        # Двойное нажатие «да, переключить»: второе — stale, версия одна.
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            revision_before = get_standard_template(
                session, AUTHOR_TG, place_id
            ).revision
        await h.callback(f"adm_bconflict_switch:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_SWITCHED)
            template = get_standard_template(session, AUTHOR_TG, place_id)
            versions = [
                binding.brick_id for binding in template.current_version.bindings
            ]
            self.assertEqual(
                template.revision,
                revision_before + 1,
                "повторное нажатие не создаёт вторую версию",
            )
            self.assertIn(row.system_brick_id, versions)
            brick = session.get(Brick, local_id)
            assert brick is not None
            self.assertEqual(brick.code, "ui_stale")
        # Повторный вход в задачу показывает «уже решена» без кнопок решения.
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        labels = " ".join(label for label, _ in h.screen_buttons())
        self.assertNotIn("Переименовать местный", labels)
        self.assertNotIn("Переключиться на глобальный", labels)


# ---------------------------------------------------------------------------
# 7b. Дополнительные негативные и конкурентные сценарии (независимая проверка)
# ---------------------------------------------------------------------------


class R3TemplateIsolationTests(R3Base):
    """Снимки/анонсы неизменяемы, а новая версия получает только новый код."""

    def test_snapshot_render_unchanged_after_switch(self) -> None:
        """Снимок анонса рендерится ровно как до решения задачи."""
        author, place, local, conflict = self.make_conflict(
            tg=8_300_001, code="цена_рендер"
        )
        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
        )
        snapshot_bindings_before = sorted(
            (binding.code, binding.brick_id) for binding in snapshot.bindings
        )
        before = render_template_snapshot(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            snapshot_id=snapshot.snapshot_id,
            values_by_id={local.id: "ЛОКАЛЬНОЕ"},
        )
        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertTrue(result.ok, result.message)
        after = render_template_snapshot(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            snapshot_id=snapshot.snapshot_id,
            values_by_id={local.id: "ЛОКАЛЬНОЕ"},
        )
        self.assertIn("ЛОКАЛЬНОЕ", before)
        self.assertEqual(before, after, "снимок анонса не переписывается")
        fresh = get_template_snapshot(
            self.session, author.telegram_user_id, snapshot.snapshot_id
        )
        assert fresh is not None
        self.assertEqual(
            sorted((binding.code, binding.brick_id) for binding in fresh.bindings),
            snapshot_bindings_before,
        )
        self.assertIn(
            local.id, [brick_id for _code, brick_id in snapshot_bindings_before]
        )
        self.assertNotIn(
            conflict.system_brick_id,
            [brick_id for _code, brick_id in snapshot_bindings_before],
        )

    def test_concurrent_switches_of_two_tasks_keep_both_bindings(self) -> None:
        """Две задачи одного места решаются параллельно без потери привязок."""
        author = self.make_user(8_301_001, username="pair")
        place = self.make_place("Место двух задач")
        self.make_place_admin(place, author)
        for code in ("цена_пар1", "цена_пар2"):
            create_place_brick(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                place_id=place.id,
                code=code,
                title=f"Местный {code}",
                value_type="short_text",
                rules={},
            )
        for index, code in enumerate(("цена_пар1", "цена_пар2")):
            approved = self.approve_global_code(code=code, tg=8_302_001 + index)
            self.assertEqual(
                approved.status, PROPOSAL_STATUS_APPROVED, approved.message
            )

        conflicts = list_place_conflicts(
            self.session, author.telegram_user_id, place.id
        )
        self.assertEqual(len(conflicts), 2)
        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        prepared = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content="[цена_пар1] и [цена_пар2]",
        )
        self.assertEqual(prepared.status, "updated", prepared.message)
        expected_systems = {view.code: view.system_brick_id for view in conflicts}
        author_tg = author.telegram_user_id
        place_id = place.id
        ids = [view.id for view in conflicts]
        self.session.close()

        barrier = Barrier(2)

        def act(conflict_id: int) -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                return resolve_conflict_by_switch(
                    session, actor_telegram_user_id=author_tg, conflict_id=conflict_id
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, conflict_id) for conflict_id in ids]
            statuses = [future.result() for future in futures]
        self.assertEqual(statuses, [CONFLICT_STATUS_SWITCHED] * 2, statuses)

        session = self.Session()
        try:
            current = get_standard_template(session, author_tg, place_id)
            bindings = {
                binding.code: binding.brick_id
                for binding in current.current_version.bindings
            }
            # Обе задачи переключены: ни одна привязка не потерялась.
            for code, system_id in expected_systems.items():
                self.assertEqual(
                    bindings.get(code),
                    system_id,
                    f"привязка [{code}] потеряна или не переключена: {bindings}",
                )
            self.assertEqual(count_open_conflicts_for_place(session, place_id), 0)
        finally:
            session.close()


class R3RenameValidationTests(R3Base):
    """Переименование не обходит правила кодов и права."""

    def test_rename_rejects_reserved_codes_and_keeps_task_open(self) -> None:
        author, _place, _local, conflict = self.make_conflict(
            tg=8_310_001, code="цена_проверка_кода"
        )
        # Системные коды зарезервированы: исход не применяется, задача открыта.
        for reserved in ("дата", "адрес", "место", "название"):
            result = resolve_conflict_by_rename(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                conflict_id=conflict.id,
                new_code=reserved,
            )
            self.assertFalse(result.ok, f"{reserved}: {result.status}")
            self.assertEqual(result.status, "conflict", f"{reserved}: {result.message}")
        # Запрещённый/невалидный код отвергается исключением.
        for bad in ("фото", "x", "", "  ", "код с пробелом", 42, None):
            with self.assertRaises(BrickConflictError, msg=repr(bad)):
                resolve_conflict_by_rename(
                    self.session,
                    actor_telegram_user_id=author.telegram_user_id,
                    conflict_id=conflict.id,
                    new_code=bad,
                )
        row = self.session.get(BrickConflict, conflict.id)
        assert row is not None
        self.assertEqual(row.status, CONFLICT_STATUS_OPEN)
        self.assertIsNone(row.resolved_at)

    def test_revoked_admin_loses_task_access(self) -> None:
        """Снятие админа места закрывает доступ к задаче в карточке."""
        author, place, _local, conflict = self.make_conflict(
            tg=8_311_001, code="цена_снятый_админ"
        )
        row = self.session.scalar(
            select(PlaceAdmin).where(PlaceAdmin.place_id == place.id)
        )
        assert row is not None
        row.revoked_at = utcnow()
        self.session.commit()
        self.assertIsNone(
            get_conflict(self.session, author.telegram_user_id, conflict.id)
        )
        with self.assertRaises(BrickPermissionError):
            resolve_conflict_by_switch(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                conflict_id=conflict.id,
            )
        fresh = self.session.get(BrickConflict, conflict.id)
        assert fresh is not None
        self.assertEqual(fresh.status, CONFLICT_STATUS_OPEN)


class R3ArchiveHolderTests(R3Base):
    """Архивный местный носитель не даёт задачи; история не удаляется."""

    def test_archived_local_holder_creates_no_task(self) -> None:
        code = "цена_архивный"
        author = self.make_user(8_320_001, username="arch")
        place = self.make_place("Место архива")
        self.make_place_admin(place, author)
        local = create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Архивный",
            value_type="short_text",
            rules={},
        )
        row = self.session.get(Brick, local.id)
        assert row is not None
        row.status = "archived"
        self.session.commit()
        result = self.approve_global_code(code=code, tg=8_320_002)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(list(result.conflicts), [])
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            0,
        )
        self.assertEqual(list_pending_conflict_notifications(self.session), [])

    def test_resolved_history_cannot_be_rewritten_or_deleted(self) -> None:
        author, _place, _local, conflict = self.make_conflict(
            tg=8_321_001, code="цена_история_иммут"
        )
        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertTrue(result.ok, result.message)
        for statement in (
            f"UPDATE brick_conflicts SET status='renamed' WHERE id={conflict.id}",
            f"UPDATE brick_conflicts SET resolution_note='подмена' WHERE id={conflict.id}",
            f"DELETE FROM brick_conflicts WHERE id={conflict.id}",
        ):
            with self.assertRaises(Exception, msg=statement):
                with self.engine.begin() as connection:
                    connection.exec_driver_sql(statement)


class R3RaceApprovalTests(R3Base):
    """Одобрение одного предложения двумя сеансами: ровно один кирпичик."""

    def test_concurrent_approval_creates_single_system_brick(self) -> None:
        code = "цена_гонка_одобрения"
        author, place, _local = self.make_author_place(tg=8_330_001, code=code)
        source_owner = self.make_user(8_330_002, username="src-race")
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
        proposal_id = proposal.id
        self.session.close()

        barrier = Barrier(2)

        def act() -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                try:
                    return approve_brick_proposal(
                        session,
                        actor_telegram_user_id=self.SUPERADMIN_TG,
                        proposal_id=proposal_id,
                    ).status
                except Exception as exc:  # noqa: BLE001
                    return f"raised {type(exc).__name__}"
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act), pool.submit(act)]
            statuses = [future.result() for future in futures]
        self.assertEqual(
            sorted(statuses),
            [PROPOSAL_STATUS_APPROVED, "error"],
            f"ровно одно одобрение, второе — явный отказ: {statuses}",
        )
        session = self.Session()
        try:
            system_rows = list(
                session.scalars(
                    select(Brick).where(Brick.code == code, Brick.scope == "system")
                ).all()
            )
            self.assertEqual(len(system_rows), 1)
            self.assertEqual(
                int(
                    session.scalar(select(func.count()).select_from(BrickConflict)) or 0
                ),
                1,
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count()).select_from(BrickConflictNotification)
                    )
                    or 0
                ),
                1,
            )
        finally:
            session.close()


class R3NotificationScopeTests(R3Base):
    """Уведомления адресованы действующим админам мест."""

    def test_revoked_admin_is_not_notified(self) -> None:
        code = "цена_уведомление_снят"
        author = self.make_user(8_340_001, username="active")
        removed = self.make_user(8_340_002, username="removed")
        place = self.make_place("Место уведомлений")
        self.make_place_admin(place, author)
        # Снятая запись создаётся сразу с revoked_at: активный админ один.
        self.session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=removed.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
                revoked_by_user_id=author.id,
                revoked_at=utcnow(),
            )
        )
        self.session.commit()
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Местный",
            value_type="short_text",
            rules={},
        )
        result = self.approve_global_code(code=code, tg=8_340_003)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        pending = list_pending_conflict_notifications(self.session)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].telegram_user_id, author.telegram_user_id)
        self.assertNotEqual(pending[0].telegram_user_id, removed.telegram_user_id)

    def test_two_places_with_same_code_get_separate_tasks(self) -> None:
        code = "цена_два_места"
        first_author, first_place, first_local = self.make_author_place(
            tg=8_341_001, code=code
        )
        second_author, second_place, second_local = self.make_author_place(
            tg=8_342_001, code=code
        )
        result = self.approve_global_code(code=code, tg=8_343_001)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(len(result.conflicts), 2)
        self.assertEqual(
            {view.place_id for view in result.conflicts},
            {first_place.id, second_place.id},
        )
        # Каждый админ видит только задачу своего места.
        first_view = list_place_conflicts(
            self.session, first_author.telegram_user_id, first_place.id
        )
        self.assertEqual(len(first_view), 1)
        self.assertEqual(first_view[0].local_brick_id, first_local.id)
        with self.assertRaises(BrickPermissionError):
            list_place_conflicts(
                self.session, first_author.telegram_user_id, second_place.id
            )
        self.assertIsNone(
            get_conflict(
                self.session, first_author.telegram_user_id, result.conflicts[1].id
            )
        )
        pending = list_pending_conflict_notifications(self.session)
        self.assertEqual(
            {item.telegram_user_id for item in pending},
            {first_author.telegram_user_id, second_author.telegram_user_id},
        )
        self.assertEqual(len(pending), 2)
        self.assertNotEqual(result.conflicts[0].system_brick_id, second_local.id)


class R3ManualEditConflictTests(R3Base):
    """Ручная правка шаблона не отменяет задачу; решение остаётся возможным."""

    def test_manual_binding_to_global_then_switch_is_noop(self) -> None:
        author, place, local, conflict = self.make_conflict(
            tg=8_350_001, code="цена_ручная_правка"
        )
        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        prepared = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content="[цена_ручная_правка]",
        )
        self.assertEqual(prepared.status, "updated", prepared.message)
        before = get_standard_template(self.session, author.telegram_user_id, place.id)
        result = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflict.id,
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.status, CONFLICT_STATUS_SWITCHED)
        # Привязка уже вела на глобальный кирпичик — новых версий не создаётся.
        self.assertFalse(result.template_updated)
        after = get_standard_template(self.session, author.telegram_user_id, place.id)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(
            [binding.brick_id for binding in after.current_version.bindings],
            [binding.brick_id for binding in before.current_version.bindings],
        )
        self.assertIn(
            conflict.system_brick_id,
            [binding.brick_id for binding in after.current_version.bindings],
        )
        self.assertNotIn(
            local.id,
            [binding.brick_id for binding in after.current_version.bindings],
        )
        self.assertEqual(count_open_conflicts_for_place(self.session, place.id), 0)


# ---------------------------------------------------------------------------
# 8. Миграция 19
# ---------------------------------------------------------------------------


class R3MigrationTests(unittest.TestCase):
    """Требование 4: свежая/заполненная БД, идемпотентность, целостность."""

    def setUp(self) -> None:
        import os
        import shutil

        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (9_900_019,)),
            patch("config.SUPERADMIN_CONTACT_URL", CONTACT),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.path = temp_db_path("poster-r3-tester-migration-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)

    @staticmethod
    def _rows(connection: Any, statement: str) -> list[tuple]:
        return list(connection.exec_driver_sql(statement).fetchall())

    @staticmethod
    def _schema(connection: Any, name: str) -> str | None:
        row = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        return None if row is None else str(row[0])

    def test_latest_version_is_19(self) -> None:
        self.assertEqual(LATEST_VERSION, 20)

    def test_fresh_database_has_conflict_tables_and_integrity(self) -> None:
        from migrations import run_migrations

        self.assertEqual(run_migrations(self.engine), 20)
        with self.engine.connect() as connection:
            names = {
                str(row[0])
                for row in self._rows(
                    connection, "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("brick_conflicts", names)
            self.assertIn("brick_conflict_notifications", names)
            self.assertEqual(self._rows(connection, "PRAGMA foreign_key_check"), [])
            self.assertEqual(
                self._rows(connection, "PRAGMA integrity_check"), [("ok",)]
            )
            self.assertEqual(
                self._rows(connection, "SELECT count(*) FROM brick_conflicts"),
                [(0,)],
            )
            self.assertEqual(
                self._rows(
                    connection, "SELECT count(*) FROM brick_conflict_notifications"
                ),
                [(0,)],
            )

    def test_filled_database_upgrade_keeps_data_and_schema(self) -> None:
        from migrations import DEFAULT_MIGRATIONS, run_migrations

        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:18]), 18)
        with self.engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, 77019, '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO places (id, created_by_user_id, created_at) "
                "VALUES (1, 1, '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO place_admins "
                "(place_id, user_id, assigned_at) "
                "VALUES (1, 1, '2026-01-01 00:00:00')"
            )
            connection.exec_driver_sql(
                "INSERT INTO bricks (id, code, title, type, scope, source, "
                "rules_json, status, place_id, created_by_user_id, "
                "created_at, updated_at, revision) VALUES "
                "(500, 'миграция_19', 'Местный', 'short_text', 'place', "
                "'ask', NULL, 'active', 1, 1, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00', 1)"
            )
        old_tables = (
            "users",
            "places",
            "place_names",
            "place_admins",
            "bricks",
            "brick_proposals",
            "templates",
            "template_versions",
            "template_version_bricks",
            "template_snapshots",
            "template_snapshot_bricks",
        )
        with self.engine.connect() as connection:
            schema_before = {
                name: self._schema(connection, name) for name in old_tables
            }
            columns_before = {
                name: self._rows(connection, f"PRAGMA table_info({name})")
                for name in old_tables
            }
            bricks_before = self._rows(
                connection,
                "SELECT id, code, title, type, scope, source, status, place_id, "
                "created_by_user_id, created_at, updated_at, revision FROM bricks "
                "ORDER BY id",
            )
            users_before = self._rows(
                connection, "SELECT id, telegram_user_id FROM users ORDER BY id"
            )
            places_before = self._rows(
                connection, "SELECT id, created_by_user_id FROM places ORDER BY id"
            )

        self.assertEqual(run_migrations(self.engine), 20)

        with self.engine.connect() as connection:
            # Существующие таблицы не перестраивались (ни DDL, ни колонки).
            # Исключение — `brick_proposals`: миграция 20 добавочно приносит
            # `description`. Формулировка «одна новая колонка, остальное
            # дословно» сильнее общего сравнения и ловит лишние перестройки.
            self.assertEqual(
                {
                    name: self._schema(connection, name)
                    for name in old_tables
                    if name != "brick_proposals"
                },
                {
                    name: schema_before[name]
                    for name in old_tables
                    if name != "brick_proposals"
                },
            )
            self.assertEqual(
                {
                    name: self._rows(connection, f"PRAGMA table_info({name})")
                    for name in old_tables
                    if name != "brick_proposals"
                },
                {
                    name: columns_before[name]
                    for name in old_tables
                    if name != "brick_proposals"
                },
            )
            proposals_columns = self._rows(
                connection, "PRAGMA table_info(brick_proposals)"
            )
            self.assertEqual(
                [(row[1], row[2], row[3], row[4]) for row in proposals_columns][
                    : len(columns_before["brick_proposals"])
                ],
                [
                    (row[1], row[2], row[3], row[4])
                    for row in columns_before["brick_proposals"]
                ],
                "прежние колонки brick_proposals не переставлены",
            )
            self.assertEqual(
                [row[1] for row in proposals_columns][
                    len(columns_before["brick_proposals"]) :
                ],
                ["description"],
                "миграция 20 добавляет ровно одну колонку",
            )
            description_column = proposals_columns[-1]
            self.assertEqual(description_column[2], "TEXT")
            self.assertEqual(description_column[3], 0, "описание необязательно (NULL)")
            self.assertIsNone(
                description_column[4], "без DEFAULT: старые строки — NULL"
            )
            self.assertEqual(
                self._rows(
                    connection,
                    "SELECT id, code, title, type, scope, source, status, place_id, "
                    "created_by_user_id, created_at, updated_at, revision FROM bricks "
                    "ORDER BY id",
                ),
                bricks_before,
            )
            self.assertEqual(
                self._rows(
                    connection, "SELECT id, telegram_user_id FROM users ORDER BY id"
                ),
                users_before,
            )
            self.assertEqual(
                self._rows(
                    connection, "SELECT id, created_by_user_id FROM places ORDER BY id"
                ),
                places_before,
            )
            self.assertEqual(self._rows(connection, "PRAGMA foreign_key_check"), [])
            self.assertEqual(
                self._rows(connection, "PRAGMA integrity_check"), [("ok",)]
            )
            self.assertEqual(
                self._rows(
                    connection, "SELECT version FROM schema_migrations ORDER BY version"
                )[-1],
                (20,),
            )

    def test_migration_is_idempotent(self) -> None:
        from migrations import DEFAULT_MIGRATIONS, run_migrations

        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:18]), 18)
        self.assertEqual(run_migrations(self.engine), 20)
        with self.engine.connect() as connection:
            names_after_first = sorted(
                str(row[0])
                for row in self._rows(
                    connection, "SELECT name FROM sqlite_master WHERE type='table'"
                )
            )
            triggers_after_first = sorted(
                str(row[0])
                for row in self._rows(
                    connection, "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            )
            journal_after_first = self._rows(
                connection,
                "SELECT version, name FROM schema_migrations ORDER BY version",
            )
        # Повторный запуск ничего не меняет и не падает.
        self.assertEqual(run_migrations(self.engine), 20)
        with self.engine.connect() as connection:
            self.assertEqual(
                sorted(
                    str(row[0])
                    for row in self._rows(
                        connection, "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ),
                names_after_first,
            )
            self.assertEqual(
                sorted(
                    str(row[0])
                    for row in self._rows(
                        connection,
                        "SELECT name FROM sqlite_master WHERE type='trigger'",
                    )
                ),
                triggers_after_first,
            )
            self.assertEqual(
                self._rows(
                    connection,
                    "SELECT version, name FROM schema_migrations ORDER BY version",
                ),
                journal_after_first,
            )


# ---------------------------------------------------------------------------
# 9. Место без администратора: задача есть, уведомления нет
# ---------------------------------------------------------------------------


class R3NoAdminPlaceTests(R3Base):
    def test_task_created_for_place_without_admin_and_approval_succeeds(self) -> None:
        code = "цена_без_админа"
        owner = self.make_user(8_260_001, username="creator")
        place = self.make_place("Место без админа", created_by_user_id=owner.id)
        # Кирпичик места создаёт суперадмин, затем админ места снимается:
        # задача конфликта при этом обязана появиться (её видит суперадмин).
        self.make_user(self.SUPERADMIN_TG, username="boss")
        local = create_place_brick(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=place.id,
            code=code,
            title="Местный",
            value_type="short_text",
            rules={},
        )
        result = self.approve_global_code(code=code, tg=8_260_002)
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.conflicts[0].place_id, place.id)
        # Получателей нет — очередь пуста, но задача видна суперадмину.
        self.assertEqual(list_pending_conflict_notifications(self.session), [])
        superadmin_view = get_conflict(
            self.session, self.SUPERADMIN_TG, result.conflicts[0].id
        )
        self.assertIsNotNone(superadmin_view)
        assert superadmin_view is not None
        self.assertEqual(superadmin_view.local_brick_id, local.id)
        # Суперадмин может решить задачу вместо админа места.
        resolved = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            conflict_id=superadmin_view.id,
        )
        self.assertTrue(resolved.ok, resolved.message)


# ---------------------------------------------------------------------------
# 10. Регресс триггера: 2+ местных носителя и прямой INSERT системного кода
# ---------------------------------------------------------------------------


class R3TriggerRegressionTests(R3Base):
    def test_two_places_with_same_code_both_get_tasks(self) -> None:
        code = "цена_два_места"
        first_owner = self.make_user(8_270_001, username="first")
        second_owner = self.make_user(8_270_002, username="second")
        places = []
        for index, owner in ((1, first_owner), (2, second_owner)):
            place = self.make_place(f"Место {index}")
            self.make_place_admin(place, owner)
            create_place_brick(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                place_id=place.id,
                code=code,
                title=f"Местный {index}",
                value_type="short_text",
                rules={},
            )
            places.append((place, owner))

        result = self.approve_global_code(code=code, tg=8_270_003)

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertEqual(len(result.conflicts), 2, result.message)
        self.assertEqual(
            {view.place_id for view in result.conflicts},
            {place.id for place, _ in places},
        )
        system = get_system_brick(self.session, code)
        assert system is not None
        for place, owner in places:
            conflicts = list_place_conflicts(
                self.session, owner.telegram_user_id, place.id
            )
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].system_brick_id, system.id)
        # По одному уведомлению каждому админу места.
        recipients = sorted(
            item.telegram_user_id
            for item in list_pending_conflict_notifications(self.session)
        )
        self.assertEqual(
            recipients,
            sorted([first_owner.telegram_user_id, second_owner.telegram_user_id]),
        )

    def test_direct_system_insert_over_local_code_is_still_forbidden(self) -> None:
        """Регресс: снятое ограничение касается только одобрения предложения."""
        from brick_store import BrickConflictError, create_system_brick

        code = "цена_прямой_инсерт"
        author, _place, _local = self.make_author_place(tg=8_271_001, code=code)
        self.make_user(self.SUPERADMIN_TG, username="boss")
        with self.assertRaises(BrickConflictError):
            create_system_brick(
                self.session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                code=code,
                title="Прямой",
                value_type="short_text",
            )
        self.assertIsNone(get_system_brick(self.session, code))
        self.session.expire_all()
        self.assertEqual(
            int(
                self.session.scalar(select(func.count()).select_from(BrickConflict))
                or 0
            ),
            0,
        )
        del author


if __name__ == "__main__":
    unittest.main()
