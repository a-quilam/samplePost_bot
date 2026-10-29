# tests/test_stage20_admin_logic.py
"""
Stage 20A: единица-логика ролей, кабинета и атомарных назначений.

Проверяется настоящая SQLite во временных файлах: частичный уникальный
индекс «не более одного действующего админа», конкурентные операции,
аудит-история и конфигурация ролей. Настоящая post_bot.db и Telegram не
используются.
"""

from __future__ import annotations

import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from migrations import run_migrations
from models import PlaceAdmin, User, utcnow
from permissions import (
    can_access_admin_panel,
    can_manage_place,
    has_any_place_admin_role,
    is_place_admin,
    is_superadmin,
)
from place_admin_store import (
    AdminActorError,
    AdminPermissionError,
    UserSearchError,
    assign_place_admin,
    get_active_place_admin,
    list_places_for_administration,
    replace_place_admin,
    revoke_place_admin,
    search_known_users,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user


class StoreFixtureBase(unittest.TestCase):
    """
    Общий каркас: временная база, пользователи и место.

    Роль суперадминистратора патчится на весь тест: слой store обязан
    перепроверять конфигурацию при каждом вызове, а тест проверяет как
    доступ, так и явные отказы (patch с пустым списком).
    """

    SUPERADMIN_TG = 900100
    PLACE_ADMIN_TG = 900200
    ORDINARY_TG = 900300
    TARGET_TG = 900400

    def setUp(self) -> None:
        self._config_patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (self.SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._config_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-20a-logic-")
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
        self.place_admin = self._user(
            self.PLACE_ADMIN_TG, username="placeadmin", full_name="Местный Админ"
        )
        self.ordinary = self._user(
            self.ORDINARY_TG, username="guest", full_name="Обычный Гость"
        )
        self.target = self._user(
            self.TARGET_TG, username="newbie", full_name="Новый Человек"
        )
        self.place = self._place("Кофейня «Локал»")

    def _user(
        self, telegram_user_id: int, *, username: str | None, full_name: str | None
    ) -> User:
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )

    def _place(self, name: str):
        from place_store import create_place

        return create_place(
            self.session,
            name,
            city="Тестовый город",
            created_by_user_id=self.ordinary.id,
        )

    def _active_admins(self) -> list[PlaceAdmin]:
        return list(
            self.session.scalars(
                select(PlaceAdmin).where(PlaceAdmin.revoked_at.is_(None))
            ).all()
        )

    def _history_rows(self) -> list[PlaceAdmin]:
        return list(
            self.session.scalars(select(PlaceAdmin).order_by(PlaceAdmin.id)).all()
        )

    def _assign(self, telegram_user_id: int) -> None:
        result = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=telegram_user_id,
        )
        self.assertEqual(result.status, "assigned")

    def _deny_config(self):
        """Открыто отключает роль суперадминистратора внутри теста."""
        return patch("config.SUPERADMIN_USER_IDS", ())


class ConfigParsingTests(unittest.TestCase):
    """Строгий парсинг конфигурации: без пустых сегментов и username."""

    def test_user_ids_empty_and_valid_values(self) -> None:
        from config import parse_superadmin_user_ids

        self.assertEqual(parse_superadmin_user_ids(None), ())
        self.assertEqual(parse_superadmin_user_ids(""), ())
        self.assertEqual(parse_superadmin_user_ids("   "), ())
        self.assertEqual(parse_superadmin_user_ids(" 12 , 34 "), (12, 34))
        self.assertEqual(parse_superadmin_user_ids("12,34,12"), (12, 34))

    def test_user_ids_rejects_bad_values_without_echoing_secrets(self) -> None:
        from config import ConfigError, parse_superadmin_user_ids

        bad_values = ("12,,34", ",", "12,abc", "0", "@user", "12,−5", "12;34")
        for value in bad_values:
            with self.assertRaises(ConfigError, msg=value):
                parse_superadmin_user_ids(value)

    def test_contact_url_accepts_only_absolute_http_or_https(self) -> None:
        from config import parse_superadmin_contact_url

        self.assertIsNone(parse_superadmin_contact_url(None))
        self.assertIsNone(parse_superadmin_contact_url("   "))
        self.assertEqual(
            parse_superadmin_contact_url(" https://t.me/poster_admin "),
            "https://t.me/poster_admin",
        )
        self.assertEqual(
            parse_superadmin_contact_url("http://example.com/a"),
            "http://example.com/a",
        )

    def test_contact_url_rejects_bad_schemes_and_spaces(self) -> None:
        from config import ConfigError, parse_superadmin_contact_url

        bad_values = ("t.me/user", "ftp://x", "https://a b", "x" * 2049)
        for value in bad_values:
            with self.assertRaises(ConfigError, msg=value[:32]):
                parse_superadmin_contact_url(value)


class RolePermissionTests(StoreFixtureBase):
    """Разрешения: конфигурация — единственный источник суперадмин-роли."""

    def test_is_superadmin_reads_live_config_only(self) -> None:
        self.assertTrue(is_superadmin(self.SUPERADMIN_TG))
        with self._deny_config():
            self.assertFalse(is_superadmin(self.SUPERADMIN_TG))
        self.assertFalse(is_superadmin(self.ORDINARY_TG))
        self.assertFalse(is_superadmin(None))
        self.assertFalse(is_superadmin(True))
        self.assertFalse(is_superadmin(-1))

    def test_place_admin_role_requires_active_row_and_place(self) -> None:
        self.assertFalse(
            is_place_admin(self.session, self.PLACE_ADMIN_TG, self.place.id)
        )

        self._assign(self.PLACE_ADMIN_TG)

        self.assertTrue(
            is_place_admin(self.session, self.PLACE_ADMIN_TG, self.place.id)
        )
        self.assertTrue(has_any_place_admin_role(self.session, self.PLACE_ADMIN_TG))
        self.assertTrue(can_access_admin_panel(self.session, self.PLACE_ADMIN_TG))
        self.assertTrue(
            can_manage_place(self.session, self.PLACE_ADMIN_TG, self.place.id)
        )

        # чужое место админу не принадлежит
        other = self._place("Другая кофейня")
        self.assertFalse(is_place_admin(self.session, self.PLACE_ADMIN_TG, other.id))
        self.assertFalse(can_manage_place(self.session, self.PLACE_ADMIN_TG, other.id))

        # отбор роли закрывает доступ немедленно
        revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertFalse(has_any_place_admin_role(self.session, self.PLACE_ADMIN_TG))
        self.assertFalse(can_access_admin_panel(self.session, self.PLACE_ADMIN_TG))

    def test_ordinary_user_has_no_access(self) -> None:
        self.assertFalse(can_access_admin_panel(self.session, self.ORDINARY_TG))
        self.assertFalse(
            can_manage_place(self.session, self.ORDINARY_TG, self.place.id)
        )


class SearchKnownUsersTests(StoreFixtureBase):
    """Поиск получателя среди уже известных боту пользователей."""

    def test_search_rejects_empty_and_long_queries(self) -> None:
        for bad in ("", "   ", "x" * 101):
            with self.assertRaises(UserSearchError):
                search_known_users(self.session, bad)
        self.assertEqual(search_known_users(self.session, "u" * 100).total, 0)

    def test_search_numeric_id_is_exact_and_never_creates_user(self) -> None:
        found = search_known_users(self.session, str(self.TARGET_TG))
        self.assertEqual(found.total, 1)
        self.assertEqual(found.users[0].telegram_user_id, self.TARGET_TG)
        self.assertEqual(found.users[0].username, "newbie")

        prefix = str(self.TARGET_TG)[:3]
        self.assertEqual(search_known_users(self.session, prefix).total, 0)

        before = len(list(self.session.scalars(select(User)).all()))
        self.assertEqual(search_known_users(self.session, "987654321").total, 0)
        after = len(list(self.session.scalars(select(User)).all()))
        self.assertEqual(before, after)

    def test_search_matches_username_and_name_with_ranking(self) -> None:
        exact = search_known_users(self.session, "@newbie")
        self.assertEqual(exact.total, 1)
        self.assertEqual(exact.users[0].telegram_user_id, self.TARGET_TG)

        self.assertEqual(search_known_users(self.session, "newbie").total, 1)
        self.assertEqual(search_known_users(self.session, "нов").total, 1)
        self.assertEqual(search_known_users(self.session, "ЧЕЛОВ").total, 1)

        # неизвестный username не превращается в пользователя
        self.assertEqual(search_known_users(self.session, "@ghost").total, 0)


class AssignPlaceAdminTests(StoreFixtureBase):
    """Назначение: права, состояние места, аудит."""

    def test_assign_requires_superadmin_role(self) -> None:
        for actor in (self.PLACE_ADMIN_TG, self.ORDINARY_TG):
            with self._deny_config():
                with self.assertRaises(AdminPermissionError):
                    assign_place_admin(
                        self.session,
                        actor_telegram_user_id=actor,
                        place_id=self.place.id,
                        target_telegram_user_id=self.TARGET_TG,
                    )
            # роль другого суперадмина не даёт доступа чужому актору
            with self.assertRaises(AdminPermissionError):
                assign_place_admin(
                    self.session,
                    actor_telegram_user_id=actor,
                    place_id=self.place.id,
                    target_telegram_user_id=self.TARGET_TG,
                )
        self.assertEqual(self._history_rows(), [])

    def test_assign_unregistered_actor_raises_actor_error(self) -> None:
        ghost_tg = 900999
        with patch("config.SUPERADMIN_USER_IDS", (ghost_tg,)):
            actor = self.session.scalar(
                select(User).where(User.telegram_user_id == ghost_tg)
            )
            self.assertIsNone(actor)
            with self.assertRaises(AdminActorError):
                assign_place_admin(
                    self.session,
                    actor_telegram_user_id=ghost_tg,
                    place_id=self.place.id,
                    target_telegram_user_id=self.TARGET_TG,
                )
        self.assertEqual(self._history_rows(), [])

    def test_assign_reports_missing_place_and_user(self) -> None:
        missing_place = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=999999,
            target_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(missing_place.status, "place_missing")

        missing_user = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=123456789,
        )
        self.assertEqual(missing_user.status, "user_missing")
        self.assertEqual(self._history_rows(), [])

    def test_assign_success_and_audit_fields(self) -> None:
        before = utcnow()
        result = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(result.status, "assigned")
        self.assertIsNotNone(result.admin)
        assert result.admin is not None
        self.assertEqual(result.admin.user.telegram_user_id, self.PLACE_ADMIN_TG)
        self.assertGreaterEqual(result.admin.assigned_at, before)

        active = get_active_place_admin(self.session, self.place.id)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.user.telegram_user_id, self.PLACE_ADMIN_TG)
        self.assertEqual(active.assigned_at, result.admin.assigned_at)

        rows = self._history_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertIsNone(row.revoked_at)
        self.assertEqual(row.assigned_by_user_id, self.superadmin.id)
        self.assertEqual(row.user_id, self.place_admin.id)
        self.assertIsNone(row.revoked_by_user_id)

    def test_assign_repeat_is_reported_and_other_target_refused(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)

        repeat = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(repeat.status, "already_admin")
        self.assertEqual(len(self._history_rows()), 1)

        conflict = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(conflict.status, "has_admin")
        self.assertEqual(len(self._history_rows()), 1)
        self.assertEqual(len(self._active_admins()), 1)


class ReplacePlaceAdminTests(StoreFixtureBase):
    """Замена: ожидаемый текущий админ и атомарная замена строк."""

    def test_replace_requires_matching_expected_admin(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)

        wrong = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.TARGET_TG,
            expected_admin_telegram_user_id=self.ORDINARY_TG,
        )
        self.assertEqual(wrong.status, "changed")
        self.assertEqual(len(self._history_rows()), 1)
        self.assertEqual(self._active_admins()[0].user_id, self.place_admin.id)

    def test_replace_reports_missing_admin_as_changed(self) -> None:
        empty = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.TARGET_TG,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(empty.status, "changed")
        self.assertEqual(self._history_rows(), [])

    def test_replace_success_atomically_swaps_rows(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        history_before = len(self._history_rows())

        result = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.TARGET_TG,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(result.status, "replaced")
        self.assertIsNotNone(result.admin)
        assert result.admin is not None
        self.assertEqual(result.admin.user.telegram_user_id, self.TARGET_TG)

        rows = self._history_rows()
        self.assertEqual(len(rows), history_before + 1)
        self.assertEqual(len(self._active_admins()), 1)
        self.assertEqual(self._active_admins()[0].user_id, self.target.id)

        old = next(row for row in rows if row.user_id == self.place_admin.id)
        self.assertIsNotNone(old.revoked_at)
        self.assertEqual(old.revoked_by_user_id, self.superadmin.id)
        new = next(row for row in rows if row.user_id == self.target.id)
        self.assertIsNone(new.revoked_at)

    def test_replace_to_same_user_is_reported_without_changes(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        same = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.PLACE_ADMIN_TG,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(same.status, "already_admin")
        self.assertEqual(len(self._history_rows()), 1)
        self.assertEqual(len(self._active_admins()), 1)

    def test_replace_denied_for_place_admin_and_ordinary(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        for actor in (self.PLACE_ADMIN_TG, self.ORDINARY_TG):
            with self.assertRaises(AdminPermissionError):
                replace_place_admin(
                    self.session,
                    actor_telegram_user_id=actor,
                    place_id=self.place.id,
                    target_telegram_user_id=self.TARGET_TG,
                    expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
                )
        self.assertEqual(len(self._active_admins()), 1)
        self.assertEqual(len(self._history_rows()), 1)

    def test_replace_reports_missing_place_and_user(self) -> None:
        missing_place = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=999999,
            target_telegram_user_id=self.TARGET_TG,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(missing_place.status, "place_missing")

        missing_user = replace_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=123456789,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(missing_user.status, "user_missing")
        self.assertEqual(self._history_rows(), [])


class RevokePlaceAdminTests(StoreFixtureBase):
    """Отзыв роли: ожидание и аудит."""

    def test_revoke_success_marks_history_and_clears_active(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)

        result = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(result.status, "revoked")
        self.assertIsNone(result.admin)
        self.assertIsNone(get_active_place_admin(self.session, self.place.id))

        rows = self._history_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0].revoked_at)
        self.assertEqual(rows[0].revoked_by_user_id, self.superadmin.id)

        # место снова свободно для нового назначения
        again = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.TARGET_TG,
        )
        self.assertEqual(again.status, "assigned")

    def test_revoke_is_idempotent_on_changed_state(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        first = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(first.status, "revoked")

        second = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(second.status, "changed")
        self.assertEqual(len(self._history_rows()), 1)

    def test_revoke_wrong_expected_admin_gives_changed(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        changed = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.ORDINARY_TG,
        )
        self.assertEqual(changed.status, "changed")
        self.assertEqual(len(self._active_admins()), 1)

    def test_revoke_denied_without_superadmin(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        for actor in (self.PLACE_ADMIN_TG, self.ORDINARY_TG):
            with self.assertRaises(AdminPermissionError):
                revoke_place_admin(
                    self.session,
                    actor_telegram_user_id=actor,
                    place_id=self.place.id,
                    expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
                )
        self.assertEqual(len(self._active_admins()), 1)
        self.assertEqual(len(self._history_rows()), 1)

    def test_revoke_reports_missing_place(self) -> None:
        result = revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=999999,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(result.status, "place_missing")


class ListPlacesForAdministrationTests(StoreFixtureBase):
    """Список кабинета: суперадмин видит всё, админ места — только своё."""

    def test_list_denies_ordinary_user(self) -> None:
        with self.assertRaises(AdminPermissionError):
            list_places_for_administration(self.session, self.ORDINARY_TG)

    def test_superadmin_sees_all_places_with_admin_state(self) -> None:
        second = self._place("Театр «Этаж»")
        items, total = list_places_for_administration(self.session, self.SUPERADMIN_TG)
        self.assertEqual(total, 2)
        self.assertEqual({item.place_id for item in items}, {self.place.id, second.id})
        self.assertEqual([item.admin for item in items], [None, None])

        self._assign(self.PLACE_ADMIN_TG)
        items, total = list_places_for_administration(self.session, self.SUPERADMIN_TG)
        first = next(item for item in items if item.place_id == self.place.id)
        self.assertIsNotNone(first.admin)
        assert first.admin is not None
        self.assertEqual(first.admin.telegram_user_id, self.PLACE_ADMIN_TG)
        self.assertEqual(total, 2)

    def test_place_admin_sees_only_own_place(self) -> None:
        self._place("Театр «Этаж»")
        self._assign(self.PLACE_ADMIN_TG)

        items, total = list_places_for_administration(self.session, self.PLACE_ADMIN_TG)
        self.assertEqual(total, 1)
        self.assertEqual([item.place_id for item in items], [self.place.id])
        # администратор этого места — сам актор
        self.assertIsNotNone(items[0].admin)
        assert items[0].admin is not None
        self.assertEqual(items[0].admin.telegram_user_id, self.PLACE_ADMIN_TG)

    def test_list_is_paginated(self) -> None:
        for index in range(10):
            self._place(f"Место {index}")
        page, total = list_places_for_administration(
            self.session, self.SUPERADMIN_TG, offset=0, limit=3
        )
        self.assertEqual(total, 11)
        self.assertEqual(len(page), 3)
        page2, _ = list_places_for_administration(
            self.session, self.SUPERADMIN_TG, offset=3, limit=3
        )
        self.assertEqual(len(page2), 3)
        self.assertEqual(
            {item.place_id for item in page} & {item.place_id for item in page2},
            set(),
        )

    def test_place_admin_list_denied_after_role_removal(self) -> None:
        self._assign(self.PLACE_ADMIN_TG)
        revoke_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        with self.assertRaises(AdminPermissionError):
            list_places_for_administration(self.session, self.PLACE_ADMIN_TG)


class ConcurrencyMutationTests(StoreFixtureBase):
    """
    Конкурентные операции на настоящей SQLite.

    Все потоки работают со своими сессиями: единственный действующий
    администратор, честные статусы и неизменённая аудит-история.
    """

    def _assign_in_fresh_session(self, telegram_user_id: int) -> None:
        session = self.Session()
        try:
            result = assign_place_admin(
                session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                place_id=self.place.id,
                target_telegram_user_id=telegram_user_id,
            )
            self.assertEqual(result.status, "assigned")
        finally:
            session.close()

    def test_simultaneous_assign_race_keeps_single_active_admin(self) -> None:
        def insert_row(telegram_user_id: int) -> str:
            session = self.Session()
            try:
                return assign_place_admin(
                    session,
                    actor_telegram_user_id=self.SUPERADMIN_TG,
                    place_id=self.place.id,
                    target_telegram_user_id=telegram_user_id,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(insert_row, telegram_user_id)
                for telegram_user_id in (self.TARGET_TG, self.PLACE_ADMIN_TG)
            ]
            statuses = [future.result() for future in futures]

        self.assertEqual(len(self._active_admins()), 1)
        self.assertIn("assigned", statuses)
        self.assertTrue(
            {"already_admin", "has_admin", "conflict"}.intersection(statuses)
        )
        # аудит содержит только победившую строку
        self.assertEqual(len(self._history_rows()), 1)

    def test_simultaneous_revoke_has_single_success(self) -> None:
        self._assign_in_fresh_session(self.PLACE_ADMIN_TG)

        def try_revoke() -> str:
            session = self.Session()
            try:
                return revoke_place_admin(
                    session,
                    actor_telegram_user_id=self.SUPERADMIN_TG,
                    place_id=self.place.id,
                    expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            statuses = list(pool.map(lambda _: try_revoke(), range(3)))

        self.assertEqual(statuses.count("revoked"), 1)
        self.assertTrue(
            {status for status in statuses} <= {"revoked", "changed", "conflict"}
        )
        self.assertEqual(len(self._history_rows()), 1)
        self.assertIsNone(get_active_place_admin(self.session, self.place.id))

    def test_simultaneous_revoke_and_assign_do_not_duplicate(self) -> None:
        self._assign_in_fresh_session(self.PLACE_ADMIN_TG)

        def do_revoke() -> str:
            session = self.Session()
            try:
                return revoke_place_admin(
                    session,
                    actor_telegram_user_id=self.SUPERADMIN_TG,
                    place_id=self.place.id,
                    expected_admin_telegram_user_id=self.PLACE_ADMIN_TG,
                ).status
            finally:
                session.close()

        def do_assign() -> str:
            session = self.Session()
            try:
                return assign_place_admin(
                    session,
                    actor_telegram_user_id=self.SUPERADMIN_TG,
                    place_id=self.place.id,
                    target_telegram_user_id=self.TARGET_TG,
                ).status
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            revoke_status = pool.submit(do_revoke).result()
            assign_status = pool.submit(do_assign).result()

        self.assertIn(revoke_status, {"revoked", "changed", "conflict"})
        self.assertIn(
            assign_status,
            {"assigned", "has_admin", "already_admin", "conflict", "changed"},
        )
        # ровно одна действующая строка в любом финале
        self.assertEqual(len(self._active_admins()), 1)
        self.assertEqual(len(self._history_rows()), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
