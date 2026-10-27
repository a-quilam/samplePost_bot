# tests/test_stage21_transfer_logic.py
"""
Stage 21: единица-логика передачи администратора места.

Проверяется настоящая SQLite во временных файлах и замороженное время:
права инициатора, привязка известного получателя к постоянному user ID,
хеширование одноразового токена, первый кандидат, утверждение инициатором,
границы 24 часов, атомарное подтверждение, откат при сбое, конкурентность,
инвалидация после Stage 20A и отсутствие обрезания длинного адреса.
Настоящая post_bot.db и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from admin_transfer_store import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_REJECTED,
    STATUS_SUPERSEDED,
    TransferActorError,
    TransferPermissionError,
    TransferValueError,
    approve_transfer_recipient,
    cancel_admin_transfer,
    confirm_admin_transfer,
    create_admin_transfer,
    find_known_recipients,
    get_pending_transfer_for_place,
    get_transfer_view,
    hash_invite_token,
    normalize_transfer_username,
    open_transfer_invitation,
    reject_admin_transfer,
)
from migrations import run_migrations
from models import AdminTransfer, Place, PlaceAdmin, User
from permissions import is_place_admin
from place_admin_store import (
    assign_place_admin,
    get_active_place_admin,
    replace_place_admin,
    revoke_place_admin,
)
from place_edit_store import set_place_address
from place_store import create_place
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user


class TransferFixture(unittest.TestCase):
    """Временная БД, место, три пользователя и действующий админ."""

    SUPERADMIN_TG = 920100
    INITIATOR_TG = 920200
    TARGET_TG = 920300
    OTHER_TG = 920400
    THIRD_TG = 920500
    NO_USERNAME_TG = 920600
    START = datetime(2026, 10, 23, 12, 0, 0)

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (self.SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.now = self.START
        self._time_patcher = patch(
            "admin_transfer_store.utcnow", side_effect=lambda: self.now
        )
        self._time_patcher.start()
        self.addCleanup(self._time_patcher.stop)

        self.path = temp_db_path("poster-21-logic-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

        self.superadmin = self._user(
            self.SUPERADMIN_TG, username="boss", full_name="Супер Админ"
        )
        self.initiator = self._user(
            self.INITIATOR_TG, username="oldadmin", full_name="Старый Админ"
        )
        self.target = self._user(
            self.TARGET_TG, username="newadmin", full_name="Новый Админ"
        )
        self.other = self._user(
            self.OTHER_TG, username="guestuser", full_name="Другой Гость"
        )
        self.third = self._user(
            self.THIRD_TG, username="thirdadmin", full_name="Третий Админ"
        )
        self.no_username = self._user(
            self.NO_USERNAME_TG, username=None, full_name="Без Username"
        )
        self.place = create_place(
            self.session, "Кофейня «Передача»", created_by_user_id=self.other.id
        )
        self._assign(self.INITIATOR_TG)

    # —— помощники ---------------------------------------------------------

    def _user(
        self,
        telegram_user_id: int,
        *,
        username: str | None,
        full_name: str | None,
    ) -> User:
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )

    def _assign(self, telegram_user_id: int) -> None:
        result = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=telegram_user_id,
        )
        self.assertEqual(result.status, "assigned")

    def _advance(self, delta: timedelta) -> None:
        self.now += delta
        self.session.expire_all()

    def _create_known(self, *, username: str = "newadmin"):
        return create_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            place_id=self.place.id,
            username=username,
            recipient_telegram_user_id=self.TARGET_TG,
        )

    def _create_invite(self, *, username: str = "stranger"):
        return create_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            place_id=self.place.id,
            username=username,
        )

    def _active(self) -> PlaceAdmin | None:
        active = get_active_place_admin(self.session, self.place.id)
        if active is None:
            return None
        return self.session.get(PlaceAdmin, active.assignment_id)

    def _history(self) -> list[PlaceAdmin]:
        self.session.expire_all()
        return list(
            self.session.scalars(
                select(PlaceAdmin)
                .where(PlaceAdmin.place_id == self.place.id)
                .order_by(PlaceAdmin.id)
            ).all()
        )

    def _transfer_row(self, transfer_id: int) -> AdminTransfer:
        self.session.expire_all()
        row = self.session.get(AdminTransfer, transfer_id)
        assert row is not None
        return row


class UsernameAndLookupTests(TransferFixture):
    """Username — подсказка, а не идентификатор прав."""

    def test_normalizes_at_and_validates_telegram_shape(self) -> None:
        self.assertEqual(normalize_transfer_username("  @newadmin "), "newadmin")
        for bad in ("", " ", "@", "abc", "1abcde", "with space", "a" * 33, "юзер"):
            with self.assertRaises(TransferValueError, msg=bad):
                normalize_transfer_username(bad)

    def test_exact_case_insensitive_lookup_without_creating_users(self) -> None:
        before = len(list(self.session.scalars(select(User)).all()))
        found = find_known_recipients(self.session, "@NEWADMIN")
        self.assertEqual([item.telegram_user_id for item in found], [self.TARGET_TG])
        self.assertEqual(find_known_recipients(self.session, "newadminx"), ())
        self.assertEqual(
            find_known_recipients(self.session, "newadmin")[0].username, "newadmin"
        )
        self.assertEqual(len(list(self.session.scalars(select(User)).all())), before)

    def test_duplicate_username_returns_all_candidates(self) -> None:
        self._user(921111, username="newadmin", full_name="Другой Новый")
        found = find_known_recipients(self.session, "newadmin")
        self.assertEqual(
            {item.telegram_user_id for item in found},
            {self.TARGET_TG, 921111},
        )

    def test_username_alone_never_binds_known_user(self) -> None:
        result = self._create_invite(username="newadmin")
        self.assertEqual(result.status, "created_invite")
        assert result.transfer is not None
        self.assertIsNone(result.transfer.to_user)
        self.assertIsNotNone(result.invite_token)


class CreateTransferTests(TransferFixture):
    """Создание предложения и срок ровно 24 часа."""

    def test_only_active_admin_of_exact_place_can_create(self) -> None:
        for actor in (
            self.OTHER_TG,
            self.SUPERADMIN_TG,  # глобальная роль не является передаваемой
        ):
            with self.assertRaises(TransferPermissionError, msg=actor):
                create_admin_transfer(
                    self.session,
                    actor_telegram_user_id=actor,
                    place_id=self.place.id,
                    username="newadmin",
                    recipient_telegram_user_id=self.TARGET_TG,
                )
        self.assertIsNone(get_pending_transfer_for_place(self.session, self.place.id))

    def test_unregistered_initiator_raises_actor_error(self) -> None:
        with self.assertRaises(TransferActorError):
            create_admin_transfer(
                self.session,
                actor_telegram_user_id=929999,
                place_id=self.place.id,
                username="newadmin",
                recipient_telegram_user_id=self.TARGET_TG,
            )

    def test_known_recipient_creation_binds_id_and_keeps_rights(self) -> None:
        result = self._create_known()
        self.assertEqual(result.status, "created_known")
        self.assertIsNone(result.invite_token)
        assert result.transfer is not None
        self.assertEqual(result.transfer.to_user.telegram_user_id, self.TARGET_TG)
        self.assertEqual(result.transfer.from_user.telegram_user_id, self.INITIATOR_TG)
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(row.status, STATUS_PENDING)
        self.assertIsNone(row.token_hash)
        self.assertEqual(row.to_user_id, self.target.id)
        self.assertEqual(row.from_user_id, self.initiator.id)
        active_assignment = self.session.scalar(
            select(PlaceAdmin).where(
                PlaceAdmin.place_id == self.place.id,
                PlaceAdmin.revoked_at.is_(None),
            )
        )
        assert active_assignment is not None
        self.assertEqual(row.from_assignment_id, active_assignment.id)
        self.assertEqual(row.expires_at - row.created_at, timedelta(hours=24))
        self.assertEqual(row.created_at, self.START)
        self.assertEqual(row.expires_at, self.START + timedelta(hours=24))
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))

    def test_known_recipient_without_username_is_supported(self) -> None:
        result = create_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            place_id=self.place.id,
            username="no_username",
            recipient_telegram_user_id=self.NO_USERNAME_TG,
        )
        self.assertEqual(result.status, "created_known")
        assert result.transfer is not None
        assert result.transfer.to_user is not None
        self.assertEqual(result.transfer.to_user.telegram_user_id, self.NO_USERNAME_TG)

    def test_invite_stores_only_sha256_and_never_raw_token(self) -> None:
        result = self._create_invite()
        self.assertEqual(result.status, "created_invite")
        token = result.invite_token
        assert token is not None and result.transfer is not None
        self.assertGreaterEqual(len(token), 40)
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertIsNone(row.to_user_id)
        self.assertEqual(row.token_hash, hash_invite_token(token))
        self.assertNotEqual(row.token_hash, token)
        self.assertNotIn(token, row.to_username or "")
        self.assertTrue(row.token_hash and len(row.token_hash) == 64)

    def test_self_transfer_by_username_and_by_id_is_rejected(self) -> None:
        self.assertEqual(self._create_invite(username="oldadmin").status, "self")
        result = create_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            place_id=self.place.id,
            username="newadmin",
            recipient_telegram_user_id=self.INITIATOR_TG,
        )
        self.assertEqual(result.status, "self")
        self.assertIsNone(get_pending_transfer_for_place(self.session, self.place.id))

    def test_one_pending_per_place_and_lazy_expiry_allows_new(self) -> None:
        first = self._create_known()
        self.assertEqual(first.status, "created_known")
        second = self._create_known()
        self.assertEqual(second.status, "active_exists")

        self._advance(timedelta(hours=23, minutes=59, seconds=59))
        self.assertEqual(self._create_known().status, "active_exists")
        self._advance(timedelta(seconds=1))  # ровно 24:00:00
        third = self._create_known()
        self.assertEqual(third.status, "created_known")
        assert first.transfer is not None
        self.assertEqual(
            self._transfer_row(first.transfer.transfer_id).status, STATUS_EXPIRED
        )

    def test_database_partial_unique_index_blocks_second_pending(self) -> None:
        first = self._create_known()
        assert first.transfer is not None
        assignment = self.session.scalar(
            select(PlaceAdmin).where(
                PlaceAdmin.place_id == self.place.id,
                PlaceAdmin.revoked_at.is_(None),
            )
        )
        assert assignment is not None
        self.session.add(
            AdminTransfer(
                place_id=self.place.id,
                from_user_id=self.initiator.id,
                from_assignment_id=assignment.id,
                to_user_id=self.other.id,
                to_username="guestuser",
                status=STATUS_PENDING,
                created_at=self.now,
                expires_at=self.now + timedelta(hours=24),
            )
        )
        with self.assertRaises(IntegrityError):
            self.session.flush()
        self.session.rollback()
        pending_row = self.session.scalar(
            select(AdminTransfer).where(
                AdminTransfer.place_id == self.place.id,
                AdminTransfer.status == STATUS_PENDING,
            )
        )
        assert pending_row is not None
        self.assertEqual(
            pending_row.id,
            first.transfer.transfer_id,
        )


class ConfirmKnownRecipientTests(TransferFixture):
    """Подтверждение известным получателем: атомарность и права."""

    def _confirmed(self):
        result = self._create_known()
        assert result.transfer is not None
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        return result, confirm

    def test_success_revokes_old_and_creates_new_atomically(self) -> None:
        result, confirm = self._confirmed()
        self.assertEqual(confirm.status, "confirmed")
        assert result.transfer is not None
        transfer = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(transfer.status, STATUS_CONFIRMED)
        self.assertEqual(transfer.confirmed_at, self.START)
        self.assertEqual(transfer.closed_at, self.START)

        active = self._active()
        assert active is not None
        self.assertEqual(active.user_id, self.target.id)
        self.assertEqual(active.assigned_by_user_id, self.initiator.id)
        self.assertIsNone(active.revoked_at)
        history = self._history()
        self.assertEqual(len(history), 2)
        old = history[0]
        self.assertEqual(old.user_id, self.initiator.id)
        self.assertEqual(old.revoked_at, self.START)
        self.assertEqual(old.revoked_by_user_id, self.target.id)
        self.assertTrue(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))

    def test_repeat_confirmation_is_idempotent(self) -> None:
        result, first = self._confirmed()
        self.assertEqual(first.status, "confirmed")
        assert result.transfer is not None
        second = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(second.status, "already_confirmed")
        self.assertEqual(len(self._history()), 2)
        self.assertEqual(
            len([row for row in self._history() if row.revoked_at is None]),
            1,
        )

    def test_other_user_cannot_confirm(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        for actor in (self.OTHER_TG, self.INITIATOR_TG):
            denied = confirm_admin_transfer(
                self.session,
                actor_telegram_user_id=actor,
                transfer_id=result.transfer.transfer_id,
            )
            self.assertEqual(denied.status, "not_recipient")
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertEqual(len(self._history()), 1)

    def test_username_change_does_not_change_recipient_identity(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        self._user(self.TARGET_TG, username="renamed_user", full_name="Новое Имя")
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "confirmed")
        active = self._active()
        assert active is not None
        self.assertEqual(active.user_id, self.target.id)

    def test_confirmation_at_23_59_59_is_valid(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        self._advance(timedelta(hours=23, minutes=59, seconds=59))
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "confirmed")

    def test_confirmation_at_exactly_24h_is_rejected(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        self._advance(timedelta(hours=24))
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "expired")
        transfer = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(transfer.status, STATUS_EXPIRED)
        self.assertEqual(transfer.closed_at, self.now)
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))

    def test_expiry_is_lazy_and_requires_no_scheduler(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        self._advance(timedelta(hours=24))
        # Без чтения/действия статус в БД ещё pending: фоновых задач нет.
        status_before = self.session.scalar(
            select(AdminTransfer.status).where(
                AdminTransfer.id == result.transfer.transfer_id
            )
        )
        self.assertEqual(status_before, STATUS_PENDING)
        view = get_transfer_view(self.session, result.transfer.transfer_id)
        assert view is not None
        self.assertEqual(view.status, STATUS_EXPIRED)

    def test_confirm_after_admin_replaced_is_superseded(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        replaced = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.THIRD_TG,
            expected_admin_telegram_user_id=self.INITIATOR_TG,
        )
        self.assertEqual(replaced.status, "replaced")
        self.assertEqual(
            self._transfer_row(result.transfer.transfer_id).status, STATUS_SUPERSEDED
        )
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "superseded")
        self.assertTrue(is_place_admin(self.session, self.THIRD_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))

    def test_confirm_after_revoke_is_superseded(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        revoked = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.INITIATOR_TG,
        )
        self.assertEqual(revoked.status, "revoked")
        self.assertEqual(
            self._transfer_row(result.transfer.transfer_id).status, STATUS_SUPERSEDED
        )
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "superseded")

    def test_reassigning_same_admin_does_not_revive_old_offer(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.THIRD_TG,
            expected_admin_telegram_user_id=self.INITIATOR_TG,
        )
        replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.INITIATOR_TG,
            expected_admin_telegram_user_id=self.THIRD_TG,
        )
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "superseded")
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))

    def test_stage20a_superadmin_powers_survive_transfer(self) -> None:
        _, confirm = self._confirmed()
        self.assertEqual(confirm.status, "confirmed")
        replaced = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.THIRD_TG,
            expected_admin_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(replaced.status, "replaced")
        self.assertTrue(is_place_admin(self.session, self.THIRD_TG, self.place.id))

    def test_failed_commit_rolls_back_transfer_and_roles(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        with patch.object(
            self.session, "commit", side_effect=RuntimeError("сбой commit")
        ):
            with self.assertRaises(RuntimeError):
                confirm_admin_transfer(
                    self.session,
                    actor_telegram_user_id=self.TARGET_TG,
                    transfer_id=result.transfer.transfer_id,
                )
        self.session.expire_all()
        transfer = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(transfer.status, STATUS_PENDING)
        self.assertIsNone(transfer.confirmed_at)
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        self.assertEqual(len(self._history()), 1)

    def test_lost_race_reports_already_confirmed_not_superseded(self) -> None:
        """
        Проигравший гонку получает фактический исход, а не устаревший «superseded».

        Детерминированное воспроизведение флейка: вторая сессия читает
        предложение ДО чужого подтверждения (устаревший `row.status` в
        identity map) и заходит в ветку «администратор изменился». Раньше она
        безусловно возвращала `superseded` и игнорировала то, что условное
        закрытие pending уже не сработало — победитель подтвердил предложение.
        """
        result = self._create_known()
        assert result.transfer is not None
        transfer_id = result.transfer.transfer_id

        stale = self.Session()
        self.addCleanup(stale.close)
        # Устаревшая копия строки: статус прочитан до победы конкурента.
        # Ссылку держим: identity map SQLAlchemy хранит объекты слабо, и без
        # неё `session.get` перечитал бы уже подтверждённую строку.
        stale_row = stale.get(AdminTransfer, transfer_id)
        assert stale_row is not None
        self.assertEqual(stale_row.status, STATUS_PENDING)
        # Конкурент подтверждает предложение в другой сессии.
        with self.Session() as other:
            winner = confirm_admin_transfer(
                other,
                actor_telegram_user_id=self.TARGET_TG,
                transfer_id=transfer_id,
            )
        self.assertEqual(winner.status, "confirmed")

        loser = confirm_admin_transfer(
            stale,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=transfer_id,
        )
        self.assertEqual(
            loser.status,
            "already_confirmed",
            "фактический исход важнее устаревшего снимка строки",
        )
        self.assertTrue(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        self.assertEqual(len(self._history()), 2, "права не менялись повторно")

    def test_concurrent_confirmation_has_single_winner(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        barrier = threading.Barrier(2)

        def worker() -> str:
            session = self.Session()
            try:
                barrier.wait(timeout=10)
                outcome = confirm_admin_transfer(
                    session,
                    actor_telegram_user_id=self.TARGET_TG,
                    transfer_id=result.transfer.transfer_id,
                )
                return outcome.status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: worker(), range(2)))
        self.assertIn("confirmed", statuses)
        self.assertTrue(
            set(statuses) <= {"confirmed", "already_confirmed", "conflict"},
            statuses,
        )
        self.assertEqual(statuses.count("confirmed"), 1)
        self.session.expire_all()
        active = list(
            self.session.scalars(
                select(PlaceAdmin).where(
                    PlaceAdmin.place_id == self.place.id,
                    PlaceAdmin.revoked_at.is_(None),
                )
            ).all()
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].user_id, self.target.id)
        self.assertEqual(len(self._history()), 2)


class CancelRejectTests(TransferFixture):
    """Отмена инициатором и отказ получателя не меняют права."""

    def test_only_initiator_can_cancel_and_then_new_offer_is_possible(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        denied = cancel_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(denied.status, "not_initiator")
        cancelled = cancel_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(
            self._transfer_row(result.transfer.transfer_id).status, STATUS_CANCELLED
        )
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertEqual(self._create_known().status, "created_known")

    def test_cancel_after_confirmation_is_impossible(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        again = cancel_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(again.status, STATUS_CONFIRMED)

    def test_recipient_rejection_keeps_rights_and_allows_new_offer(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        denied = reject_admin_transfer(
            self.session,
            actor_telegram_user_id=self.OTHER_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(denied.status, "not_recipient")
        rejected = reject_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(
            self._transfer_row(result.transfer.transfer_id).status, STATUS_REJECTED
        )
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        self.assertEqual(self._create_known().status, "created_known")

    def test_confirmation_after_rejection_is_rejected(self) -> None:
        result = self._create_known()
        assert result.transfer is not None
        reject_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, STATUS_REJECTED)


class InviteFlowTests(TransferFixture):
    """Неизвестный получатель: токен, первый кандидат, утверждение."""

    def _opened_invite(self):
        result = self._create_invite()
        assert result.transfer is not None and result.invite_token is not None
        opened = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        return result, opened

    def test_invalid_and_tampered_token_do_not_bind_candidate(self) -> None:
        result = self._create_invite()
        assert result.transfer is not None and result.invite_token is not None
        for token in (
            "",
            "wrong",
            result.invite_token[:-1] + ("A" if result.invite_token[-1] != "A" else "B"),
        ):
            opened = open_transfer_invitation(
                self.session,
                transfer_id=result.transfer.transfer_id,
                token=token,
                actor_telegram_user_id=self.TARGET_TG,
            )
            self.assertEqual(opened.status, "invalid_invite", msg=token)
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertIsNone(row.opened_by_user_id)
        self.assertIsNone(row.to_user_id)

    def test_token_is_bound_to_its_transfer_id(self) -> None:
        first = self._create_invite()
        assert first.transfer is not None and first.invite_token is not None
        # Второе место с собственной передачей не нужно: чужой id не совпадёт.
        opened = open_transfer_invitation(
            self.session,
            transfer_id=first.transfer.transfer_id + 100,
            token=first.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(opened.status, "invalid_invite")

    def test_first_opener_is_only_candidate_and_gets_no_rights(self) -> None:
        result, opened = self._opened_invite()
        self.assertEqual(opened.status, "opened")
        assert result.transfer is not None
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(row.opened_by_user_id, self.target.id)
        self.assertEqual(row.opened_at, self.START)
        self.assertIsNone(row.to_user_id)
        self.assertTrue(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        before_approval = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(before_approval.status, "not_recipient")

    def test_second_account_cannot_rebind_and_same_account_reopens(self) -> None:
        result, opened = self._opened_invite()
        self.assertEqual(opened.status, "opened")
        assert result.transfer is not None and result.invite_token is not None
        other = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.OTHER_TG,
        )
        self.assertEqual(other.status, "opened_by_other")
        again = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(again.status, "already_opened")
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(row.opened_by_user_id, self.target.id)

    def test_initiator_opening_own_link_does_not_bind_self(self) -> None:
        result = self._create_invite()
        assert result.transfer is not None and result.invite_token is not None
        opened = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.INITIATOR_TG,
        )
        self.assertEqual(opened.status, "self")
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertIsNone(row.opened_by_user_id)

    def test_initiator_must_explicitly_approve_candidate(self) -> None:
        result, _ = self._opened_invite()
        assert result.transfer is not None
        denied = approve_transfer_recipient(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(denied.status, "not_initiator")
        approved = approve_transfer_recipient(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(approved.status, "approved")
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertEqual(row.to_user_id, self.target.id)
        self.assertEqual(row.opened_by_user_id, self.target.id)
        self.assertFalse(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        confirm = confirm_admin_transfer(
            self.session,
            actor_telegram_user_id=self.TARGET_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(confirm.status, "confirmed")
        self.assertTrue(is_place_admin(self.session, self.TARGET_TG, self.place.id))
        self.assertFalse(is_place_admin(self.session, self.INITIATOR_TG, self.place.id))

    def test_approval_requires_opened_candidate(self) -> None:
        result = self._create_invite()
        assert result.transfer is not None
        approved = approve_transfer_recipient(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        self.assertEqual(approved.status, "not_opened")

    def test_invite_open_at_exactly_24h_is_expired(self) -> None:
        result = self._create_invite()
        assert result.transfer is not None and result.invite_token is not None
        self._advance(timedelta(hours=24))
        opened = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(opened.status, STATUS_EXPIRED)
        row = self._transfer_row(result.transfer.transfer_id)
        self.assertIsNone(row.opened_by_user_id)
        self.assertEqual(row.status, STATUS_EXPIRED)

    def test_cancel_invalidates_invite_and_token(self) -> None:
        result = self._create_invite()
        assert result.transfer is not None and result.invite_token is not None
        cancel_admin_transfer(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        opened = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(opened.status, STATUS_CANCELLED)
        self.assertIsNone(
            self._transfer_row(result.transfer.transfer_id).opened_by_user_id
        )

    def test_approved_invite_cannot_be_opened_by_other(self) -> None:
        result, _ = self._opened_invite()
        assert result.transfer is not None and result.invite_token is not None
        approve_transfer_recipient(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            transfer_id=result.transfer.transfer_id,
        )
        other = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.OTHER_TG,
        )
        self.assertEqual(other.status, "not_recipient")
        target = open_transfer_invitation(
            self.session,
            transfer_id=result.transfer.transfer_id,
            token=result.invite_token,
            actor_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(target.status, "approved")


class AddressAuditTests(TransferFixture):
    """Stage 21: 500 символов у адреса были произвольным лимитом."""

    def test_long_address_is_stored_without_truncation(self) -> None:
        long_address = "улица " + ("Очень длинный адрес " * 300)
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.INITIATOR_TG,
            place_id=self.place.id,
            address=long_address,
            expected_address=None,
        )
        self.assertEqual(result.status, "ok")
        self.session.expire_all()
        place = self.session.get(Place, self.place.id)
        assert place is not None
        self.assertEqual(place.address, " ".join(long_address.split()))
        self.assertGreater(len(place.address or ""), 500)
        self.assertFalse((place.address or "").endswith("…"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
