# tests/stage23_helpers.py
"""
Общие временные базы Stage 23 (шаблоны, версии, снимки).

Настоящая `post_bot.db` не открывается: каждый тест поднимает собственный
временный SQLite-файл, применяет нумерованные миграции (1–11) и получает
Session-фабрику. Здесь же — готовые фикстуры пользователей, мест, админов,
личных кирпичиков и шаблонов, чтобы каждый тест-файл описывал только сценарий.
"""

from __future__ import annotations

import os
import shutil
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy.orm import Session, sessionmaker

from brick_store import create_personal_brick
from migrations import run_migrations
from models import Place, PlaceAdmin, User, utcnow
from place_store import create_place
from template_store import (
    TemplateDetails,
    create_personal_template,
    create_standard_template_for_place,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user

__all__ = [
    "TemplateDatabaseTestCase",
    "OTHER_TG",
    "OWNER_TG",
    "SUPERADMIN_TG",
    "VISITOR_TG",
]

OWNER_TG = 23_910_001
OTHER_TG = 23_910_002
SUPERADMIN_TG = 23_910_003
VISITOR_TG = 23_910_004


class TemplateDatabaseTestCase(unittest.TestCase):
    """Временная база Stage 23: миграции 1–11, чистая Session на каждый тест."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-23-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.migrations_applied = run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

    # --- базовые сущности --------------------------------------------------

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
        """Создаёт место вместе с каноническим названием и стандартом."""
        return create_place(
            self.session,
            name,
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

    def make_standard_template(
        self, place: Place, *, created_by_user_id: int | None = None
    ) -> TemplateDetails:
        return create_standard_template_for_place(
            self.session,
            place.id,
            created_by_user_id=created_by_user_id,
        )

    def make_personal_template(
        self,
        owner: User,
        name: str,
        *,
        header: str = "Заголовок",
        body: str = "Текст [название]",
        footer: str = "Подвал",
        chosen_brick_ids: dict[str, int] | None = None,
    ) -> TemplateDetails:
        return create_personal_template(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            name=name,
            header=header,
            body=body,
            footer=footer,
            chosen_brick_ids=chosen_brick_ids,
        )

    def make_personal_brick(self, owner: User, code: str) -> int:
        definition = create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            title=f"Кирпичик {code}",
            code=code,
            value_type="short_text",
        )
        return definition.id
