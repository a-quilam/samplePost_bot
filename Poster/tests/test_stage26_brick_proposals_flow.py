# tests/test_stage26_brick_proposals_flow.py
"""
Stage 26: 40 интеграционных сценариев предложений кирпичиков.

Каждый сценарий проходит через настоящий `Application.process_update`
из `bot.build_application()`, временную SQLite и FakeBot. Постоянная
`Poster/post_bot.db`, реальные WIP, шаблоны, фотографии и Telegram не
используются.
"""

from __future__ import annotations

import unittest
from datetime import datetime
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select

from admin_transfer_store import confirm_admin_transfer, create_admin_transfer
from brick_proposal_store import (
    PROPOSAL_STATUS_PENDING,
    approve_brick_proposal,
    create_brick_proposal,
    reject_brick_proposal,
    withdraw_brick_proposal,
)
from brick_store import (
    BrickConflictError,
    create_personal_brick,
    create_place_brick,
    list_available_bricks,
    update_personal_brick,
)
from models import (
    AnnouncementDraft,
    Brick,
    BrickProposal,
    Place,
    PlaceAdmin,
    TemplateVersionBrick,
    UserState,
    utcnow,
)
from place_store import create_place
from template_store import create_personal_template
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    Harness,
    scenario_test,
)
from user_store import get_or_create_user

OWNER_TG = 26_700_001
ADMIN_TG = 26_700_002
SUPER_TG = 26_700_003
OTHER_TG = 26_700_004
NEW_ADMIN_TG = 26_700_005


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class BrickProposalFlowTests(unittest.TestCase):
    """Пользовательские и административные сценарии Stage 26."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- фикстуры ---------------------------------------------------------

    def ensure_user(
        self, h: Harness, telegram_user_id: int, username: str | None = None
    ) -> int:
        with h.session_factory() as session:
            user = get_or_create_user(
                session,
                telegram_user_id,
                username=username or f"user{telegram_user_id}",
                full_name=f"Пользователь {telegram_user_id}",
            )
            return user.id

    def make_place(
        self,
        h: Harness,
        name: str,
        *,
        admin_tg: int | None = None,
    ) -> int:
        with h.session_factory() as session:
            creator = None
            if admin_tg is not None:
                creator = get_or_create_user(
                    session,
                    admin_tg,
                    username=f"admin{admin_tg}",
                    full_name=f"Админ {admin_tg}",
                )
            place = create_place(
                session,
                name,
                city="Тестовый город",
                created_by_user_id=creator.id if creator is not None else None,
            )
            if creator is not None:
                session.add(
                    PlaceAdmin(
                        place_id=place.id,
                        user_id=creator.id,
                        assigned_by_user_id=None,
                        assigned_at=utcnow(),
                    )
                )
                session.commit()
            return place.id

    def make_personal(
        self,
        h: Harness,
        owner_tg: int,
        *,
        code: str,
        title: str = "Личный кирпичик",
        value_type: str = "short_text",
        rules: dict | None = None,
    ):
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            return create_personal_brick(
                session,
                actor_telegram_user_id=owner_tg,
                code=code,
                title=title,
                value_type=value_type,
                rules=rules,
            )

    def make_place_brick(
        self,
        h: Harness,
        actor_tg: int,
        place_id: int,
        *,
        code: str,
        title: str = "Кирпичик места",
    ):
        self.ensure_user(h, actor_tg)
        with h.session_factory() as session:
            return create_place_brick(
                session,
                actor_telegram_user_id=actor_tg,
                place_id=place_id,
                code=code,
                title=title,
                value_type="short_text",
            )

    def direct_proposal(
        self,
        h: Harness,
        owner_tg: int,
        source_id: int,
        *,
        scope: str = "place",
        place_id: int | None = None,
    ) -> int:
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=owner_tg,
                source_brick_id=source_id,
                scope=scope,
                place_id=place_id,
            )
            return proposal.id

    def proposal_row(self, h: Harness, proposal_id: int) -> BrickProposal:
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            return row

    def proposal_status(self, h: Harness, proposal_id: int) -> str:
        return self.proposal_row(h, proposal_id).status

    def brick_count(self, h: Harness) -> int:
        with h.session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Brick)) or 0)

    def brick_by_code(
        self,
        h: Harness,
        code: str,
        *,
        scope: str | None = None,
    ) -> Brick | None:
        with h.session_factory() as session:
            statement = select(Brick).where(Brick.code == code)
            if scope is not None:
                statement = statement.where(Brick.scope == scope)
            return session.scalar(statement)

    def source_row(self, h: Harness, brick_id: int) -> Brick:
        with h.session_factory() as session:
            row = session.get(Brick, brick_id)
            assert row is not None
            return row

    def available_codes(
        self,
        h: Harness,
        viewer_tg: int,
        *,
        place_id: int | None = None,
    ) -> set[str]:
        with h.session_factory() as session:
            return {
                brick.code
                for brick in list_available_bricks(
                    session,
                    viewer_telegram_user_id=viewer_tg,
                    place_id=place_id,
                )
            }

    def make_template_with_source(
        self,
        h: Harness,
        owner_tg: int,
        source_id: int,
        code: str,
    ) -> int:
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            template = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="Шаблон Stage 26",
                header="Заголовок",
                body=f"Поле [{code}]",
                footer="Подвал",
                chosen_brick_ids={code: source_id},
            )
            return template.current_version.version_id

    def template_bindings(
        self, h: Harness, version_id: int
    ) -> list[TemplateVersionBrick]:
        with h.session_factory() as session:
            return list(
                session.scalars(
                    select(TemplateVersionBrick).where(
                        TemplateVersionBrick.template_version_id == version_id
                    )
                ).all()
            )

    # --- экран и навигация ------------------------------------------------

    def callbacks(self, h: Harness, prefix: str = "") -> list[str]:
        found = [
            callback
            for _label, callback in h.screen_buttons()
            if callback and callback.startswith(prefix)
        ]
        self.assertTrue(found, f"нет кнопки {prefix!r}: {h.screen_buttons()!r}")
        return found

    def button_labels(self, h: Harness) -> list[str]:
        return [label for label, _callback in h.screen_buttons()]

    def last_alert(self, h: Harness) -> str | None:
        if not h.answers():
            return None
        record = h.answers()[-1]
        args = record.get("args") or ()
        if args:
            return str(args[0])
        return record.get("text")

    async def open_author(self, h: Harness, user_id: int = OWNER_TG) -> None:
        await h.text("/bricks", user_id=user_id)

    async def open_my(self, h: Harness, user_id: int = OWNER_TG) -> None:
        await self.open_author(h, user_id)
        await h.callback("bp_mine", user_id=user_id)

    async def open_own_card(
        self, h: Harness, proposal_id: int, user_id: int = OWNER_TG
    ) -> None:
        await self.open_my(h, user_id)
        await h.callback(f"bp_open:{proposal_id}", user_id=user_id)

    async def open_admin(self, h: Harness, user_id: int = ADMIN_TG) -> None:
        await h.text("/admin", user_id=user_id)

    async def open_place_card(
        self, h: Harness, place_id: int, user_id: int = ADMIN_TG
    ) -> None:
        await self.open_admin(h, user_id)
        await h.callback(f"adm_place:{place_id}", user_id=user_id)

    async def open_place_queue(
        self, h: Harness, place_id: int, user_id: int = ADMIN_TG
    ) -> None:
        await self.open_place_card(h, place_id, user_id)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=user_id)

    async def open_place_proposal(
        self,
        h: Harness,
        place_id: int,
        proposal_id: int,
        user_id: int = ADMIN_TG,
    ) -> None:
        await self.open_place_queue(h, place_id, user_id)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=user_id)

    async def open_global_queue(self, h: Harness) -> None:
        await self.open_admin(h, SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)

    async def approve_place_via_ui(
        self,
        h: Harness,
        place_id: int,
        proposal_id: int,
        user_id: int = ADMIN_TG,
    ) -> None:
        await self.open_place_proposal(h, place_id, proposal_id, user_id)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=user_id)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=user_id)

    # --- журнал уведомлений ---------------------------------------------

    def author_messages(self, h: Harness, author_tg: int = OWNER_TG) -> list[dict]:
        return [
            call
            for call in h.calls
            if call["type"] == "message" and int(call.get("chat_id") or 0) == author_tg
        ]

    # =====================================================================
    # Пользовательские сценарии (1–20)
    # =====================================================================

    @scenario_test
    async def test_01_hub_offers_propose_and_my_proposals(self, h: Harness) -> None:
        await self.open_author(h)
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertIn("bp_start", self.callbacks(h, "bp_start"))
        self.assertIn("bp_mine", self.callbacks(h, "bp_mine"))

    @scenario_test
    async def test_02_user_without_bricks_is_offered_existing_constructor(
        self, h: Harness
    ) -> None:
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        self.assertIn("Сначала создайте личный кирпичик", h.screen_text())
        self.assertIn("bk_create", " ".join(self.callbacks(h)))
        self.assertEqual(self._proposal_count(h), 0)

    @scenario_test
    async def test_03_place_proposal_full_wizard_creates_pending_snapshot(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал мастера")
        source = self.make_personal(
            h,
            OWNER_TG,
            code="мастер_26",
            title="Мастер 26",
            value_type="number",
            rules={"min": 0, "suffix": "мест"},
        )
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        self.assertIn(f"bp_source:{source.id}", " ".join(self.callbacks(h)))
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        self.assertIn(f"bp_place:{place_id}", " ".join(self.callbacks(h)))
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        self.assertIn("Проверьте предложение", h.screen_text())
        self.assertIn("мастер_26", h.screen_text())
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIn("Предложение отправлено", h.screen_text())
        with h.session_factory() as session:
            row = session.scalar(select(BrickProposal))
            assert row is not None
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)
            self.assertEqual(row.scope, "place")
            self.assertEqual(row.place_id, place_id)
            self.assertEqual(row.source_brick_id, source.id)
            self.assertEqual(row.title, "Мастер 26")
            self.assertEqual(row.code, "мастер_26")
            self.assertEqual(row.value_type, "number")

    @scenario_test
    async def test_04_place_step_requires_existing_place_and_never_creates_one(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, OWNER_TG, code="без_места_26")
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        self.assertIn("нет мест", h.screen_text().lower())
        await h.callback("bp_place:999999", user_id=OWNER_TG)
        self.assertIn("не найдено", h.screen_text().lower())
        with h.session_factory() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(Place)), 0)
        self.assertEqual(self._proposal_count(h), 0)

    @scenario_test
    async def test_05_global_proposal_full_wizard_uses_system_scope(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, OWNER_TG, code="глобал_26", title="Глобал")
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:system", user_id=OWNER_TG)
        self.assertIn("вся система", h.screen_text())
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIn("Предложение отправлено", h.screen_text())
        with h.session_factory() as session:
            stored = session.scalar(select(BrickProposal))
            assert stored is not None
            self.assertEqual(stored.scope, "system")
            self.assertIsNone(stored.place_id)
            self.assertEqual(stored.status, "pending")
            self.assertEqual(stored.title, "Глобал")
            self.assertEqual(stored.code, "глобал_26")

    @scenario_test
    async def test_06_preview_shows_snapshot_and_back_navigation(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал просмотра")
        source = self.make_personal(
            h,
            OWNER_TG,
            code="просмотр_26",
            title="Просмотр",
            value_type="number",
            rules={"min": 2, "max": 9, "suffix": "шт"},
        )
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        text = h.screen_text()
        self.assertIn("Просмотр", text)
        self.assertIn("просмотр_26", text)
        self.assertIn("Число", text)
        self.assertIn("минимум 2", text)
        self.assertIn("максимум 9", text)
        self.assertIn("шт", text)
        self.assertIn("Зал просмотра", text)
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn(f"bp_place:{place_id}", " ".join(self.callbacks(h)))
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("bp_target:place", " ".join(self.callbacks(h)))
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn(f"bp_source:{source.id}", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_07_code_conflict_hides_submit_and_keeps_no_row(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал конфликта")
        self.make_place_brick(h, SUPER_TG, place_id, code="занят_ui_26", title="Занят")
        source = self.make_personal(h, OWNER_TG, code="занят_ui_26")
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        self.assertNotIn("bp_send", " ".join(self.callbacks(h)))
        self.assertIn("нельзя", h.screen_text().lower())
        self.assertEqual(self._proposal_count(h), 0)

    @scenario_test
    async def test_08_archived_and_foreign_bricks_are_not_offered(
        self, h: Harness
    ) -> None:
        archived = self.make_personal(h, OWNER_TG, code="архив_ui_26")
        self.make_personal(h, OTHER_TG, code="чужой_ui_26")
        with h.session_factory() as session:
            from brick_store import archive_personal_brick

            archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=archived.id,
                expected_revision=archived.revision,
            )
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        self.assertNotIn(f"bp_source:{archived.id}", " ".join(self.callbacks(h)))
        await h.callback(f"bp_source:{archived.id}", user_id=OWNER_TG)
        self.assertIn("архив", h.screen_text().lower())
        await h.callback("bp_source:999999", user_id=OWNER_TG)
        self.assertEqual(self._proposal_count(h), 0)

    @scenario_test
    async def test_09_pending_snapshot_survives_source_edit_and_archive(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал снимка")
        source = self.make_personal(h, OWNER_TG, code="снимок_ui_26", title="До правки")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        with h.session_factory() as session:
            updated = update_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=source.id,
                expected_revision=source.revision,
                code="после_ui_26",
                title="После правки",
                rules=None,
            )
            self.assertEqual(updated.status, "updated")
            from brick_store import archive_personal_brick

            archived = archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=source.id,
                expected_revision=2,
            )
            self.assertEqual(archived.status, "archived")
        await self.open_own_card(h, proposal_id)
        text = h.screen_text()
        self.assertIn("До правки", text)
        self.assertIn("снимок_ui_26", text)
        self.assertNotIn("после_ui_26", text)
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")

    @scenario_test
    async def test_10_my_proposals_empty_state_and_status_labels(
        self, h: Harness
    ) -> None:
        await self.open_my(h)
        self.assertIn("ещё не отправляли", h.screen_text())
        place_id = self.make_place(h, "Зал статусов")
        source = self.make_personal(h, OWNER_TG, code="статус_26")
        pending_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        rejected_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        withdrawn_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.ensure_user(h, SUPER_TG)
        with h.session_factory() as session:
            reject_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=rejected_id,
            )
            withdraw_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                proposal_id=withdrawn_id,
            )
        await self.open_my(h)
        labels = " ".join(self.button_labels(h))
        self.assertIn("Ожидает решения", labels)
        self.assertIn("Отклонено", labels)
        self.assertIn("Отозвано", labels)
        callbacks = self.callbacks(h, "bp_open:")
        self.assertEqual(
            {callback for callback in callbacks},
            {
                f"bp_open:{pending_id}",
                f"bp_open:{rejected_id}",
                f"bp_open:{withdrawn_id}",
            },
        )

    @scenario_test
    async def test_11_my_proposals_pagination_uses_eight_items(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал страниц")
        source = self.make_personal(h, OWNER_TG, code="страницы_26")
        for _index in range(9):
            self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_my(h)
        self.assertEqual(len(self.callbacks(h, "bp_open:")), 8)
        self.assertIn("bp_mpage:8", " ".join(self.callbacks(h)))
        await h.callback("bp_mpage:8", user_id=OWNER_TG)
        self.assertEqual(len(self.callbacks(h, "bp_open:")), 1)

    @scenario_test
    async def test_12_author_card_requires_withdraw_confirmation(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал подтверждения")
        source = self.make_personal(h, OWNER_TG, code="подтвердить_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_own_card(h, proposal_id)
        self.assertIn("Ожидает решения", h.screen_text())
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        self.assertIn("Отозвать предложение?", h.screen_text())
        self.assertIn("bp_withdraw_yes", " ".join(self.callbacks(h)))
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")

    @scenario_test
    async def test_13_withdraw_cancel_keeps_pending(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал отмены отзыва")
        source = self.make_personal(h, OWNER_TG, code="отмена_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_own_card(h, proposal_id)
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Ожидает решения", h.screen_text())
        self.assertIn("bp_withdraw", " ".join(self.callbacks(h)))
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")

    @scenario_test
    async def test_14_withdraw_confirm_repeat_is_stale_and_new_proposal_allowed(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал отзыва")
        source = self.make_personal(h, OWNER_TG, code="отзыв_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_own_card(h, proposal_id)
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        await h.callback("bp_withdraw_yes", user_id=OWNER_TG)
        self.assertIn("Отозвано", h.screen_text())
        self.assertEqual(self.proposal_status(h, proposal_id), "withdrawn")
        h.bot.calls.clear()
        await h.callback("bp_withdraw_yes", user_id=OWNER_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "withdrawn")
        new_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.assertNotEqual(new_id, proposal_id)
        self.assertEqual(self.proposal_status(h, new_id), "pending")

    @scenario_test
    async def test_15_foreign_author_proposal_is_private_and_unreadable(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал приватности")
        source = self.make_personal(h, OWNER_TG, code="приват_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_my(h, OTHER_TG)
        self.assertIn("ещё не отправляли", h.screen_text())
        await h.callback(f"bp_open:{proposal_id}", user_id=OTHER_TG)
        self.assertIn("не найдено", h.screen_text().lower())
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")

    @scenario_test
    async def test_16_stale_user_callbacks_do_not_change_pending(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал stale")
        source = self.make_personal(h, OWNER_TG, code="stale_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_own_card(h, proposal_id)
        h.bot.calls.clear()
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        self.assertIsNotNone(self.last_alert(h))
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIsNotNone(self.last_alert(h))
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")
        self.assertEqual(self._proposal_count(h), 1)

    @scenario_test
    async def test_17_rejected_card_has_no_actions_and_new_proposal_allowed(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал отказа")
        source = self.make_personal(h, OWNER_TG, code="отказ_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.ensure_user(h, SUPER_TG)
        with h.session_factory() as session:
            reject_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=proposal_id,
            )
        await self.open_own_card(h, proposal_id)
        self.assertIn("Отклонено", h.screen_text())
        self.assertNotIn("bp_withdraw", " ".join(self.callbacks(h)))
        new_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.assertNotEqual(new_id, proposal_id)
        self.assertEqual(self.proposal_status(h, new_id), "pending")

    @scenario_test
    async def test_18_approved_card_is_terminal_and_source_unchanged(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал одобрения", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="одобрен_ui_26", title="Одобрен")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.ensure_user(h, ADMIN_TG)
        with h.session_factory() as session:
            approve_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=proposal_id,
            )
        await self.open_own_card(h, proposal_id)
        self.assertIn("Одобрено", h.screen_text())
        self.assertNotIn("bp_withdraw", " ".join(self.callbacks(h)))
        stored = self.source_row(h, source.id)
        self.assertEqual(stored.status, "active")
        self.assertEqual(stored.code, "одобрен_ui_26")
        self.assertEqual(stored.owner_user_id, self.ensure_user(h, OWNER_TG))

    @scenario_test
    async def test_19_back_navigation_card_list_hub(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал навигации")
        source = self.make_personal(h, OWNER_TG, code="назад_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_own_card(h, proposal_id)
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои предложения", h.screen_text())
        self.assertIn(f"bp_open:{proposal_id}", " ".join(self.callbacks(h)))
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertIn("bp_start", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_20_wip_post_and_announcement_survive_proposal_and_decision(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал WIP", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="wip_ui_26")
        owner_id = self.ensure_user(h, OWNER_TG)
        with h.session_factory() as session:
            # S5: легаси-черновика больше нет — незавершённый анонс живёт в
            # `announcement_drafts`, WIP пользователя — в `user_states`.
            draft = AnnouncementDraft(
                author_user_id=owner_id,
                origin="personal",
                stage="preview",
                revision=1,
                photos='["photo-wip"]',
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            state = UserState(
                user_id=owner_id,
                chat_id=777,
                payload='{"current_step": 3, "data": {"title": "WIP"}}',
            )
            session.add_all([draft, state])
            session.commit()
            draft_id = draft.id
            payload = state.payload
        await self.open_author(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        await h.callback("bp_send", user_id=OWNER_TG)
        with h.session_factory() as session:
            proposal = session.scalar(select(BrickProposal))
            assert proposal is not None
            proposal_id = proposal.id
        await self.approve_place_via_ui(h, place_id, proposal_id, ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")
        with h.session_factory() as session:
            stored_draft = session.get(AnnouncementDraft, draft_id)
            assert stored_draft is not None
            self.assertEqual(stored_draft.stage, "preview")
            self.assertEqual(stored_draft.revision, 1)
            self.assertEqual(stored_draft.photos, '["photo-wip"]')
            stored_state = session.scalar(
                select(UserState).where(UserState.user_id == owner_id)
            )
            assert stored_state is not None
            self.assertEqual(stored_state.payload, payload)

    # =====================================================================
    # Административные сценарии (21–40)
    # =====================================================================

    @scenario_test
    async def test_21_place_card_shows_pending_count_and_place_queue(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал админа", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="очередь_ui_26")
        first = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        second = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_card(h, place_id)
        self.assertIn("(2)", " ".join(self.button_labels(h)))
        self.assertIn(f"adm_bprop_place:{place_id}", " ".join(self.callbacks(h)))
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        callbacks = self.callbacks(h, "adm_bprop_open:")
        self.assertEqual(
            set(callbacks), {f"adm_bprop_open:{first}", f"adm_bprop_open:{second}"}
        )

    @scenario_test
    async def test_22_place_queue_excludes_other_place_and_terminal_rows(
        self, h: Harness
    ) -> None:
        first = self.make_place(h, "Первый зал", admin_tg=ADMIN_TG)
        second = self.make_place(h, "Второй зал")
        source = self.make_personal(h, OWNER_TG, code="изоляция_ui_26")
        first_pending = self.direct_proposal(h, OWNER_TG, source.id, place_id=first)
        second_pending = self.direct_proposal(h, OWNER_TG, source.id, place_id=second)
        first_terminal = self.direct_proposal(h, OWNER_TG, source.id, place_id=first)
        self.ensure_user(h, SUPER_TG)
        with h.session_factory() as session:
            reject_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=first_terminal,
            )
        await self.open_place_queue(h, first)
        callbacks = self.callbacks(h, "adm_bprop_open:")
        self.assertEqual(callbacks, [f"adm_bprop_open:{first_pending}"])
        self.assertNotIn(str(second_pending), " ".join(callbacks))
        self.assertNotIn(str(first_terminal), " ".join(callbacks))

    @scenario_test
    async def test_23_place_queue_pagination_uses_eight_items(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал пагинации", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="пагинация_ui_26")
        for _index in range(9):
            self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_queue(h, place_id)
        self.assertEqual(len(self.callbacks(h, "adm_bprop_open:")), 8)
        self.assertIn("adm_bprop_places_page:8", " ".join(self.callbacks(h)))
        await h.callback("adm_bprop_places_page:8", user_id=ADMIN_TG)
        self.assertEqual(len(self.callbacks(h, "adm_bprop_open:")), 1)

    @scenario_test
    async def test_24_admin_card_shows_snapshot_author_and_settings(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал карточки", admin_tg=ADMIN_TG)
        source = self.make_personal(
            h,
            OWNER_TG,
            code="карточка_ui_26",
            title="Карточка",
            value_type="number",
            rules={"min": 0, "max": 100, "suffix": "мест"},
        )
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        text = h.screen_text()
        self.assertIn("Карточка", text)
        self.assertIn("карточка_ui_26", text)
        self.assertIn("Число", text)
        self.assertIn("минимум 0", text)
        self.assertIn("максимум 100", text)
        self.assertIn("подпись «мест»", text)
        self.assertIn("Пользователь", text)
        self.assertIn("Отправлено:", text)
        self.assertIn("Зал карточки", text)
        self.assertIn(f"adm_bprop_approve:{proposal_id}", " ".join(self.callbacks(h)))
        self.assertIn(f"adm_bprop_reject:{proposal_id}", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_25_approve_requires_confirmation_and_cancel_keeps_pending(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал approve", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="approve_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        before = self.brick_count(h)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Одобрить предложение", h.screen_text())
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")
        self.assertEqual(self.brick_count(h), before)
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertIn("Предложение кирпичика", h.screen_text())
        self.assertIn(f"adm_bprop_approve:{proposal_id}", " ".join(self.callbacks(h)))
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")

    @scenario_test
    async def test_26_reject_requires_confirmation_and_sets_rejected(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал reject", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="reject_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        await h.callback(f"adm_bprop_reject:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Отклонить предложение", h.screen_text())
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")
        await h.callback(f"adm_bprop_reject:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_reject_yes:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Отклонено", h.screen_text())
        self.assertEqual(self.proposal_status(h, proposal_id), "rejected")
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertNotIn(f"adm_bprop_open:{proposal_id}", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_27_approve_creates_separate_place_brick_without_rebinding(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал создания", admin_tg=ADMIN_TG)
        source = self.make_personal(
            h, OWNER_TG, code="создание_ui_26", title="Создание"
        )
        version_id = self.make_template_with_source(
            h, OWNER_TG, source.id, "создание_ui_26"
        )
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.approve_place_via_ui(h, place_id, proposal_id, ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")
        created = self.brick_by_code(h, "создание_ui_26", scope="place")
        assert created is not None
        self.assertNotEqual(created.id, source.id)
        self.assertEqual(created.place_id, place_id)
        self.assertIsNone(created.owner_user_id)
        source_row = self.source_row(h, source.id)
        self.assertEqual(source_row.status, "active")
        self.assertEqual(source_row.owner_user_id, self.ensure_user(h, OWNER_TG))
        bindings = self.template_bindings(h, version_id)
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0].brick_id, source.id)
        self.assertEqual(bindings[0].code, "создание_ui_26")
        self.assertIn("Одобрено", h.screen_text())

    @scenario_test
    async def test_28_approval_code_conflict_keeps_pending_and_no_duplicate(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал конфликта approve", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="конфликт_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        self.make_place_brick(
            h, ADMIN_TG, place_id, code="конфликт_ui_26", title="Появился позже"
        )
        before = self.brick_count(h)
        await self.approve_place_via_ui(h, place_id, proposal_id, ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")
        self.assertEqual(self.brick_count(h), before)
        self.assertIn("использ", h.screen_text().lower())
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertIn(f"adm_bprop_open:{proposal_id}", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_29_repeated_approve_callbacks_create_only_one_brick(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал повтора", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="повтор_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.approve_place_via_ui(h, place_id, proposal_id, ADMIN_TG)
        after_first = self.brick_count(h)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        self.assertEqual(self.brick_count(h), after_first)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")

    @scenario_test
    async def test_30_withdraw_wins_over_open_admin_card(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал гонки", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="гонка_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        before = self.brick_count(h)
        with h.session_factory() as session:
            withdraw_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                proposal_id=proposal_id,
            )
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Отозвано", h.screen_text())
        self.assertEqual(self.proposal_status(h, proposal_id), "withdrawn")
        self.assertEqual(self.brick_count(h), before)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "withdrawn")
        self.assertEqual(self.brick_count(h), before)

    @scenario_test
    async def test_31_transferred_admin_old_card_cannot_decide_and_new_admin_can(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал передачи", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="передача_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Одобрить предложение", h.screen_text())
        self.ensure_user(h, NEW_ADMIN_TG, "newadmin26")
        with h.session_factory() as session:
            created = create_admin_transfer(
                session,
                actor_telegram_user_id=ADMIN_TG,
                place_id=place_id,
                username="newadmin26",
                recipient_telegram_user_id=NEW_ADMIN_TG,
            )
            self.assertEqual(created.status, "created_known")
            assert created.transfer is not None
            confirmed = confirm_admin_transfer(
                session,
                actor_telegram_user_id=NEW_ADMIN_TG,
                transfer_id=created.transfer.transfer_id,
            )
            self.assertEqual(confirmed.status, "confirmed")
        before = self.brick_count(h)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("доступен только администраторам", h.screen_text())
        self.assertIsNotNone(self.last_alert(h))
        self.assertEqual(self.proposal_status(h, proposal_id), "pending")
        self.assertEqual(self.brick_count(h), before)
        await self.open_place_queue(h, place_id, NEW_ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=NEW_ADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=NEW_ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=NEW_ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")

    @scenario_test
    async def test_32_place_admin_cannot_open_global_or_foreign_queue(
        self, h: Harness
    ) -> None:
        own = self.make_place(h, "Своё место", admin_tg=ADMIN_TG)
        foreign = self.make_place(h, "Чужое место")
        source = self.make_personal(h, OWNER_TG, code="доступ_ui_26")
        self.direct_proposal(h, OWNER_TG, source.id, place_id=foreign)
        await self.open_place_card(h, own, ADMIN_TG)
        await h.callback(f"adm_bprop_place:{foreign}", user_id=ADMIN_TG)
        self.assertIsNotNone(self.last_alert(h))
        self.assertIn("Своё место", h.screen_text())
        await h.callback("adm_bprop_global", user_id=ADMIN_TG)
        self.assertTrue(self.last_alert(h))
        self.assertNotIn("Глобальные предложения", h.screen_text())

    @scenario_test
    async def test_33_superadmin_global_and_place_queues_are_separate(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал супер", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="сводка_ui_26")
        place_proposal = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        system_source = self.make_personal(h, OWNER_TG, code="сводка_sys_26")
        system_proposal = self.direct_proposal(
            h, OWNER_TG, system_source.id, scope="system"
        )
        await self.open_admin(h, SUPER_TG)
        await h.callback("adm_bprop_places", user_id=SUPER_TG)
        text = h.screen_text()
        self.assertIn("сводка_ui_26", text)
        self.assertNotIn("сводка_sys_26", text)
        self.assertIn(f"adm_bprop_open:{place_proposal}", " ".join(self.callbacks(h)))
        await h.callback("adm_bprop_back", user_id=SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)
        text = h.screen_text()
        self.assertIn("сводка_sys_26", text)
        self.assertNotIn("сводка_ui_26", text)
        self.assertIn(f"adm_bprop_open:{system_proposal}", " ".join(self.callbacks(h)))

    @scenario_test
    async def test_34_superadmin_approves_global_and_editor_sees_system_brick(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, OWNER_TG, code="глобал_ok_26", title="Глобал OK")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, scope="system")
        await self.open_global_queue(h)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")
        created = self.brick_by_code(h, "глобал_ok_26", scope="system")
        assert created is not None
        self.assertIn("глобал_ok_26", self.available_codes(h, OWNER_TG))
        self.assertIn("Одобрено", h.screen_text())

    @scenario_test
    async def test_35_global_promotion_keeps_source_editable_and_blocks_new_duplicate(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, OWNER_TG, code="промо_ui_26", title="Промо")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, scope="system")
        await self.open_global_queue(h)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        with h.session_factory() as session:
            updated = update_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=source.id,
                expected_revision=source.revision,
                code="промо_ui_26",
                title="Изменённый источник",
                rules=None,
            )
            self.assertEqual(updated.status, "updated")
        self.ensure_user(h, OTHER_TG)
        with h.session_factory() as session, self.assertRaises(BrickConflictError):
            create_personal_brick(
                session,
                actor_telegram_user_id=OTHER_TG,
                code="промо_ui_26",
                title="Новый дубль",
                value_type="short_text",
            )

    @scenario_test
    async def test_36_notification_failure_does_not_rollback_decision(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал сбоя уведомления", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="сбой_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        h.bot.fail_types.add("message")
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")
        self.assertIn("не доставлено", h.screen_text())
        self.assertEqual(self.author_messages(h), [])

    @scenario_test
    async def test_37_successful_decision_notifies_author_once(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал уведомления", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="уведомление_ui_26")
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_place_proposal(h, place_id, proposal_id)
        before = len(self.author_messages(h))
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=ADMIN_TG)
        messages = self.author_messages(h)
        self.assertEqual(len(messages), before + 1)
        self.assertIn("одобрено", str(messages[-1].get("text")).lower())
        self.assertIn("Автору отправлено", h.screen_text())

    @scenario_test
    async def test_38_pending_has_no_ttl_and_old_proposal_remains_decidable(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, OWNER_TG, code="старое_ui_26")
        with patch(
            "brick_proposal_store.utcnow", return_value=datetime(2011, 1, 1, 12, 0)
        ):
            proposal_id = self.direct_proposal(h, OWNER_TG, source.id, scope="system")
        self.assertEqual(
            self.proposal_row(h, proposal_id).created_at.year,
            2011,
        )
        await self.open_global_queue(h)
        self.assertIn("старое_ui_26", h.screen_text())
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        self.assertEqual(self.proposal_status(h, proposal_id), "approved")

    @scenario_test
    async def test_39_approved_place_brick_is_available_in_place_editor(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал редактора", admin_tg=ADMIN_TG)
        source = self.make_personal(
            h, OWNER_TG, code="редактор_ui_26", title="Редактор"
        )
        proposal_id = self.direct_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.approve_place_via_ui(h, place_id, proposal_id, ADMIN_TG)
        codes = self.available_codes(h, ADMIN_TG, place_id=place_id)
        self.assertIn("редактор_ui_26", codes)
        created = self.brick_by_code(h, "редактор_ui_26", scope="place")
        assert created is not None
        self.assertEqual(created.place_id, place_id)

    @scenario_test
    async def test_40_terminal_proposals_are_not_in_queue_and_have_no_actions(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал истории", admin_tg=ADMIN_TG)
        approved_source = self.make_personal(h, OWNER_TG, code="история_ok_26")
        rejected_source = self.make_personal(h, OWNER_TG, code="история_no_26")
        withdrawn_source = self.make_personal(h, OWNER_TG, code="история_out_26")
        approved = self.direct_proposal(
            h, OWNER_TG, approved_source.id, place_id=place_id
        )
        rejected = self.direct_proposal(
            h, OWNER_TG, rejected_source.id, place_id=place_id
        )
        withdrawn = self.direct_proposal(
            h, OWNER_TG, withdrawn_source.id, place_id=place_id
        )
        self.ensure_user(h, SUPER_TG)
        with h.session_factory() as session:
            approve_brick_proposal(
                session,
                actor_telegram_user_id=ADMIN_TG,
                proposal_id=approved,
            )
            reject_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=rejected,
            )
            withdraw_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                proposal_id=withdrawn,
            )
        await self.open_place_queue(h, place_id)
        self.assertFalse(
            any(
                callback and callback.startswith("adm_bprop_open:")
                for _label, callback in h.screen_buttons()
            )
        )
        await h.callback(f"adm_bprop_open:{approved}", user_id=ADMIN_TG)
        self.assertIn("Одобрено", h.screen_text())
        self.assertNotIn("adm_bprop_approve:", " ".join(self.callbacks(h)))
        self.assertNotIn("adm_bprop_reject:", " ".join(self.callbacks(h)))
        await h.callback("adm_bprop_back", user_id=ADMIN_TG)
        self.assertIn("Зал истории", h.screen_text())

    # --- внутренние проверки ---------------------------------------------

    def _proposal_count(self, h: Harness) -> int:
        with h.session_factory() as session:
            return int(
                session.scalar(select(func.count()).select_from(BrickProposal)) or 0
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
