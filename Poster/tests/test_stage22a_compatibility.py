# tests/test_stage22a_compatibility.py
"""
Stage 22A: миграция со схемы Stage 21 и сохранность прежних сценариев.

Проверяются только временные базы: версия 5 → 6, WIP и фото прежнего
черновика, права администраторов и передача админки. Легаси-таблица
`drafts` удалена из моделей (Poster 2.0 S5), поэтому архив проверяется
сырым SQL: миграция 17 переименовывает её в `legacy_drafts_backup`.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from admin_transfer_store import (
    confirm_admin_transfer,
    create_admin_transfer,
    get_transfer_view,
)
from base import Base
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Place, PlaceAdmin, PlaceName, UserState, utcnow
from permissions import (
    can_access_admin_panel,
    can_manage_place,
    has_any_place_admin_role,
    is_place_admin,
    is_superadmin,
)
from place_admin_store import get_active_place_admin
from place_store import normalize_place_name
from tests.stage19a_helpers import connect, make_engine, temp_db_path
from user_store import get_or_create_user

LEGACY_DRAFTS_DDL = """
    CREATE TABLE drafts (
        id INTEGER NOT NULL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title VARCHAR(255),
        date VARCHAR(50),
        time_start VARCHAR(50),
        time_end VARCHAR(50),
        place_name VARCHAR(255),
        place_url VARCHAR(255),
        text TEXT,
        contact VARCHAR(255),
        image VARCHAR(255),
        photos TEXT,
        formats TEXT,
        created_at DATETIME NOT NULL,
        updated_at DATETIME
    )
"""


def journal(path: str | Path) -> list[tuple[int, str]]:
    connection = connect(str(path))
    try:
        return [
            (int(row["version"]), str(row["name"]))
            for row in connection.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            )
        ]
    finally:
        connection.close()


def scalar(path: str | Path, sql: str, params: tuple[object, ...] = ()) -> tuple | None:
    connection = connect(str(path))
    try:
        row = connection.execute(sql, params).fetchone()
        return None if row is None else tuple(row)
    finally:
        connection.close()


class Stage22ACompatibilityTests(unittest.TestCase):
    SUPERADMIN_TG = 9_333_001

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (self.SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-22a-compat-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        # Ровно схема Stage 21: миграции 1–5 и legacy-таблицы create_all,
        # но ещё без bricks.
        run_migrations(self.engine, DEFAULT_MIGRATIONS[:5])
        stage23_tables = {
            "templates",
            "template_versions",
            "template_version_bricks",
            "template_snapshots",
            "template_snapshot_bricks",
            "template_sessions",
            "announcement_drafts",
            "announcement_values",
            "announcement_extras",
        }
        # R3: таблицы конфликтов создаёт миграция 19, а не create_all —
        # иначе имитация старой схемы получила бы их раньше миграции.
        stage23_tables |= {
            "brick_conflicts",
            "brick_conflict_notifications",
        }
        legacy_tables = [
            table
            for name, table in Base.metadata.tables.items()
            if name
            not in {"bricks", "brick_sessions", "brick_proposals"} | stage23_tables
        ]
        Base.metadata.create_all(bind=self.engine, tables=legacy_tables)
        # Модели `Draft` больше нет (S5): схему прежней таблицы создаём сырым
        # SQL, как её оставила бы установка Stage 21.
        with self.engine.begin() as connection:
            connection.execute(text(LEGACY_DRAFTS_DDL))
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

    def backup_paths(self) -> list[Path]:
        return sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))

    def make_user(self, telegram_user_id: int, username: str | None = None):
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=f"Пользователь {telegram_user_id}",
        )

    def make_place(self, name: str, *, created_by_user_id: int | None = None):
        # Схема Stage 21 ещё не знает шаблонов, поэтому создаём её строки
        # напрямую, не вызывая сервис Stage 23.
        place = Place(
            created_by_user_id=created_by_user_id,
            address="улица Ленина, 1",
            url="https://example.com/place",
            created_at=utcnow(),
        )
        self.session.add(place)
        self.session.flush()
        self.session.add(
            PlaceName(
                place_id=place.id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=1,
                created_by_user_id=created_by_user_id,
                created_at=utcnow(),
            )
        )
        self.session.commit()
        return place

    def make_admin(self, place_id: int, user_id: int) -> PlaceAdmin:
        row = PlaceAdmin(
            place_id=place_id,
            user_id=user_id,
            assigned_at=utcnow(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def test_stage21_database_migrates_and_preserves_drafts_wip_and_photos(
        self,
    ) -> None:
        user = self.make_user(9_330_101, "author")
        place = self.make_place("Театр до миграции", created_by_user_id=user.id)
        admin = self.make_admin(place.id, user.id)
        photos = ["file-photo-1", "file-photo-2"]
        formats = {"title": "<b>Старый заголовок</b>"}
        wip = {
            "title": "Незавершённый пост",
            "date": "24.10.2026",
            "photos": ["wip-photo-1"],
            "current_step": 4,
        }
        expected_photos = json.dumps(photos, ensure_ascii=False)
        expected_formats = json.dumps(formats, ensure_ascii=False)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO drafts (id, user_id, title, date, time_start, "
                    "time_end, place_name, place_url, text, contact, image, "
                    "photos, formats, created_at, updated_at) VALUES "
                    "(7, :user_id, 'Старый черновик', '17.11.2026', '19:00', "
                    "'21:00', 'Старое место', 'https://example.com/old', "
                    "'Текст <b>поста</b>', '@old_contact', 'file-photo-1', "
                    ":photos, :formats, :created_at, :created_at)"
                ),
                {
                    "user_id": user.telegram_user_id,
                    "photos": expected_photos,
                    "formats": expected_formats,
                    "created_at": utcnow().isoformat(sep=" "),
                },
            )
        state = UserState(
            user_id=user.telegram_user_id,
            chat_id=user.telegram_user_id,
            screen_message_id=55,
            payload=json.dumps(wip, ensure_ascii=False),
            updated_at=utcnow(),
        )
        self.session.add(state)
        self.session.commit()
        expected_payload = state.payload
        self.session.close()

        self.assertEqual(
            journal(self.path),
            [
                (1, "users_places_place_names"),
                (2, "place_contacts_and_ui_state"),
                (3, "place_admins_and_admin_ui_state"),
                (4, "place_edit_log"),
                (5, "admin_transfers"),
            ],
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

        copies = self.backup_paths()
        self.assertEqual(len(copies), 1, copies)
        backup = copies[0]
        self.assertEqual(
            journal(backup),
            [
                (1, "users_places_place_names"),
                (2, "place_contacts_and_ui_state"),
                (3, "place_admins_and_admin_ui_state"),
                (4, "place_edit_log"),
                (5, "admin_transfers"),
            ],
        )
        self.assertIsNone(
            scalar(
                backup,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bricks'",
            )
        )
        self.assertEqual(
            scalar(backup, "SELECT photos FROM drafts WHERE id=7"),
            (expected_photos,),
        )
        self.assertEqual(
            scalar(
                backup,
                "SELECT payload FROM user_states WHERE user_id=?",
                (user.telegram_user_id,),
            ),
            (expected_payload,),
        )
        backup_connection = sqlite3.connect(backup)
        try:
            self.assertEqual(
                backup_connection.execute("PRAGMA integrity_check").fetchone()[0],
                "ok",
            )
        finally:
            backup_connection.close()

        self.assertIn((17, "archive_legacy_drafts"), journal(self.path))
        self.assertIn((18, "builtin_templates_v3"), journal(self.path))
        self.assertEqual(
            journal(self.path)[-1], (LATEST_VERSION, "brick_proposal_description")
        )
        # Миграция 17 переименовала легаси-таблицу: строки прежнего черновика
        # читаются из архива как есть, `drafts` в рабочей схеме нет.
        self.assertIsNone(
            scalar(
                self.path,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='drafts'",
            )
        )
        archived = scalar(
            self.path,
            "SELECT title, image, photos, formats FROM legacy_drafts_backup "
            "WHERE id=7",
        )
        assert archived is not None
        self.assertEqual(archived[0], "Старый черновик")
        self.assertEqual(archived[1], "file-photo-1")
        self.assertEqual(archived[2], expected_photos)
        self.assertEqual(archived[3], expected_formats)
        self.assertEqual(json.loads(archived[2] or "[]"), photos)
        with self.Session() as session:
            stored_state = session.get(UserState, user.telegram_user_id)
            assert stored_state is not None
            self.assertEqual(stored_state.payload, expected_payload)
            self.assertEqual(json.loads(stored_state.payload or "{}"), wip)
            stored_place = session.get(Place, place.id)
            assert stored_place is not None
            self.assertEqual(stored_place.address, "улица Ленина, 1")
            self.assertEqual(stored_place.url, "https://example.com/place")
            stored_admin = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.id == admin.id)
            )
            self.assertIsNotNone(stored_admin)
            assert stored_admin is not None
            self.assertIsNone(stored_admin.revoked_at)
        self.assertEqual(
            scalar(
                self.path,
                "SELECT count(*) FROM bricks WHERE scope='system'",
            ),
            # 9 кирпичиков миграции 6 + `[адрес]` миграции 15.
            (10,),
        )
        self.assertIsNone(
            scalar(
                self.path,
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='draft_values'",
            )
        )

    def test_admin_rights_still_work_after_brick_migration(self) -> None:
        admin = self.make_user(9_330_201, "placeadmin")
        ordinary = self.make_user(9_330_202, "ordinary")
        superadmin = self.make_user(self.SUPERADMIN_TG, "boss")
        place = self.make_place("Зал с правами")
        assignment = self.make_admin(place.id, admin.id)
        self.session.close()

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        with self.Session() as session:
            self.assertTrue(is_place_admin(session, admin.telegram_user_id, place.id))
            self.assertTrue(can_manage_place(session, admin.telegram_user_id, place.id))
            self.assertFalse(
                is_place_admin(session, ordinary.telegram_user_id, place.id)
            )
            self.assertFalse(
                can_manage_place(session, ordinary.telegram_user_id, place.id)
            )
            self.assertTrue(is_superadmin(superadmin.telegram_user_id))
            self.assertTrue(is_superadmin(self.SUPERADMIN_TG))
            self.assertTrue(can_access_admin_panel(session, admin.telegram_user_id))
            self.assertTrue(has_any_place_admin_role(session, admin.telegram_user_id))
            self.assertFalse(
                has_any_place_admin_role(session, ordinary.telegram_user_id)
            )
            active = get_active_place_admin(session, place.id)
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active.user.telegram_user_id, admin.telegram_user_id)
            self.assertEqual(active.assignment_id, assignment.id)
            active_row = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.id == assignment.id)
            )
            assert active_row is not None
            active_row.revoked_at = utcnow()
            session.commit()
        with self.Session() as session:
            self.assertFalse(is_place_admin(session, admin.telegram_user_id, place.id))
            self.assertIsNone(get_active_place_admin(session, place.id))

    def test_transfer_created_on_stage21_completes_after_migration(self) -> None:
        initiator = self.make_user(9_330_301, "oldadmin")
        recipient = self.make_user(9_330_302, "newadmin")
        place = self.make_place("Театр передачи")
        assignment = self.make_admin(place.id, initiator.id)
        created = create_admin_transfer(
            self.session,
            actor_telegram_user_id=initiator.telegram_user_id,
            place_id=place.id,
            username="newadmin",
            recipient_telegram_user_id=recipient.telegram_user_id,
        )
        self.assertEqual(created.status, "created_known")
        assert created.transfer is not None
        transfer_id = created.transfer.transfer_id
        self.session.close()

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        with self.Session() as session:
            pending = get_transfer_view(session, transfer_id)
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending.status, "pending")
            self.assertIsNotNone(pending.to_user)
            assert pending.to_user is not None
            self.assertEqual(
                pending.to_user.telegram_user_id, recipient.telegram_user_id
            )
            result = confirm_admin_transfer(
                session,
                actor_telegram_user_id=recipient.telegram_user_id,
                transfer_id=transfer_id,
            )
            self.assertEqual(result.status, "confirmed")
            confirmed = get_transfer_view(session, transfer_id)
            self.assertIsNotNone(confirmed)
            assert confirmed is not None
            self.assertEqual(confirmed.status, "confirmed")
            active = get_active_place_admin(session, place.id)
            self.assertIsNotNone(active)
            assert active is not None
            self.assertEqual(active.user.telegram_user_id, recipient.telegram_user_id)
            old_assignment = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.id == assignment.id)
            )
            assert old_assignment is not None
            self.assertIsNotNone(old_assignment.revoked_at)
            self.assertTrue(
                is_place_admin(session, recipient.telegram_user_id, place.id)
            )
            self.assertFalse(
                is_place_admin(session, initiator.telegram_user_id, place.id)
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
