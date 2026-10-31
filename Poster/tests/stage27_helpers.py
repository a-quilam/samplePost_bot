# tests/stage27_helpers.py
"""
Общие временные фикстуры Stage 27A (адаптивный мастер анонсов).

Реальная `post_bot.db` не открывается: каждый тест поднимает собственный
SQLite-файл и применяет миграции 1–12. Помощники создают пользователей,
место с альтернативным названием, стандартный/личный шаблон и доводят
анонс до нужного этапа через публичный сервис, а не прямыми INSERT.
"""

from __future__ import annotations

from announcement_store import (
    choose_announcement_template,
    confirm_announcement_place,
    start_announcement_wizard,
)
from brick_store import create_place_brick
from models import Place
from place_store import add_alternative_name
from template_store import (
    TemplateDetails,
    get_standard_template,
    update_template_content,
)
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    SUPERADMIN_TG,
    VISITOR_TG,
    TemplateDatabaseTestCase,
)

__all__ = [
    "AnnouncementDatabaseTestCase",
    "OTHER_TG",
    "OWNER_TG",
    "SUPERADMIN_TG",
    "VISITOR_TG",
]


class AnnouncementDatabaseTestCase(TemplateDatabaseTestCase):
    """Временная база Stage 27A с готовым владельцем и местом."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="announcement-owner")
        self.other = self.make_user(OTHER_TG, username="announcement-other")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="announcement-super")
        self.visitor = self.make_user(VISITOR_TG, username="announcement-visitor")

        self.place = self.make_place(
            "Дом культуры",
            created_by_user_id=self.owner.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        self.make_place_admin(self.place, self.owner)
        self.alternative = add_alternative_name(
            self.session,
            self.place.id,
            "ДК «Тестовый»",
            created_by_user_id=self.owner.id,
        )
        assert self.alternative.name_id is not None
        self.standard = self.standard_template(self.place)

    # --- сущности ----------------------------------------------------------

    def standard_template(self, place: Place) -> TemplateDetails:
        return get_standard_template(self.session, OWNER_TG, place.id)

    def make_place_brick(
        self,
        code: str,
        *,
        value_type: str = "short_text",
        rules: dict[str, object] | None = None,
    ) -> int:
        definition = create_place_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=self.place.id,
            code=code,
            title=f"Кирпичик {code}",
            value_type=value_type,
            rules=rules,
        )
        return definition.id

    def update_template_body(self, template: TemplateDetails, body: str) -> None:
        """Меняет весь текст шаблона (H2: секций больше нет)."""
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template.template_id,
            expected_revision=template.revision,
            content=body,
        )
        self.session.expire_all()

    # --- анонсы ------------------------------------------------------------

    def start_draft(
        self,
        *,
        place_id: int | None = None,
        place_name_id: int | None = None,
        actor_tg: int = OWNER_TG,
        idempotency_key: object | None = None,
    ):
        return start_announcement_wizard(
            self.session,
            actor_telegram_user_id=actor_tg,
            place_id=self.place.id if place_id is None else place_id,
            place_name_id=place_name_id,
            idempotency_key=idempotency_key,
        )

    def start_at_questions(
        self,
        *,
        template_id: int | None = None,
        place_name_id: int | None = None,
        actor_tg: int = OWNER_TG,
    ):
        draft = self.start_draft(
            place_name_id=place_name_id,
            actor_tg=actor_tg,
        )
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=actor_tg,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        return choose_announcement_template(
            self.session,
            actor_telegram_user_id=actor_tg,
            draft_id=confirmed.draft_id,
            template_id=(
                self.standard.template_id if template_id is None else template_id
            ),
            expected_revision=confirmed.revision,
        )
