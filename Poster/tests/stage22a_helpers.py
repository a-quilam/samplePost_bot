# tests/stage22a_helpers.py
"""
Общие временные базы Stage 22A. Настоящая post_bot.db не открывается.
"""

from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from migrations import run_migrations
from models import Place, PlaceAdmin, User, utcnow
from place_store import create_place
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user


class BrickDatabaseTestCase(unittest.TestCase):
    SUPERADMIN_TG = 9_922_001

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (self.SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-22a-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

    def make_user(self, telegram_user_id: int, *, username: str | None = None) -> User:
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=f"Пользователь {telegram_user_id}",
        )

    def make_place(
        self,
        name: str,
        *,
        created_by_user_id: int | None = None,
        address: str | None = None,
        url: str | None = None,
    ) -> Place:
        return create_place(
            self.session,
            name,
            city="Тестовый город",
            created_by_user_id=created_by_user_id,
            address=address,
            url=url,
        )

    def make_place_admin(self, place: Place, user: User) -> PlaceAdmin:
        row = PlaceAdmin(
            place_id=place.id,
            user_id=user.id,
            assigned_by_user_id=None,
            assigned_at=utcnow(),
        )
        self.session.add(row)
        self.session.commit()
        return row

    def backup_paths(self) -> list[Path]:
        return sorted(Path(self.path).parent.glob(Path(self.path).name + ".backup-*"))
