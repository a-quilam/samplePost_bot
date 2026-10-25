# tests/test_stage25_template_resets.py
"""
Stage 25: подтверждённый сброс личного и стандартного шаблонов.

Проверяется сервисный слой на временной SQLite: права, revision, явное
подтверждение, новая версия, неизменность истории и самостоятельных снимков.
Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from models import Template, TemplateSnapshot
from template_store import (
    TemplateConfirmationRequired,
    create_personal_template,
    create_template_snapshot,
    get_builtin_template,
    get_template_version,
    list_template_versions,
    reset_personal_template,
    reset_standard_template,
    update_template_header,
)
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    SUPERADMIN_TG,
    TemplateDatabaseTestCase,
)


class PersonalResetTests(TemplateDatabaseTestCase):
    """Сброс личного шаблона: только владелец, только с подтверждением."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.other = self.make_user(OTHER_TG, username="other")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="super")
        self.created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Свой",
            header="Своя шапка",
            body="Своё тело",
            footer="Свой подвал",
        )
        self.snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.created.template_id,
            expected_revision=1,
        )

    def test_confirmation_and_owner_are_required(self) -> None:
        with self.assertRaises(TemplateConfirmationRequired):
            reset_personal_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=self.created.template_id,
                expected_revision=1,
                confirm=False,
            )

        forbidden = reset_personal_template(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.created.template_id,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(forbidden.status, "forbidden")
        assert forbidden.template is not None
        self.assertEqual(forbidden.template.revision, 1)

        superadmin = reset_personal_template(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            template_id=self.created.template_id,
            expected_revision=1,
            confirm=True,
        )
        # Суперадмин не становится владельцем личного шаблона.
        self.assertEqual(superadmin.status, "forbidden")

    def test_reset_creates_version_and_preserves_history_and_snapshot(self) -> None:
        result = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.created.template_id,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        template = result.template
        self.assertEqual(template.revision, 2)
        builtin = get_builtin_template()
        self.assertEqual(template.current_version.header, builtin.header)
        self.assertEqual(template.current_version.body, builtin.body)
        self.assertEqual(template.current_version.footer, builtin.footer)
        self.assertEqual(template.current_version.builtin_version, 1)

        # История версий не переписана.
        versions = list_template_versions(
            self.session, OWNER_TG, self.created.template_id
        )
        self.assertEqual([item.version for item in versions], [1, 2])
        first = get_template_version(
            self.session, OWNER_TG, self.created.template_id, 1
        )
        assert first is not None
        self.assertEqual(first.header, "Своя шапка")
        self.assertEqual(first.body, "Своё тело")
        self.assertEqual(first.footer, "Свой подвал")

        # Снимок остался снимком версии 1 и не изменился.
        snapshot_row = self.session.get(TemplateSnapshot, self.snapshot.snapshot_id)
        assert snapshot_row is not None
        self.assertEqual(snapshot_row.source_template_version, 1)
        self.assertEqual(snapshot_row.header, "Своя шапка")
        self.assertEqual(snapshot_row.body, "Своё тело")
        self.assertEqual(snapshot_row.footer, "Свой подвал")

        # Второй сброс ничего не меняет и не плодит версии.
        unchanged = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.created.template_id,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(unchanged.status, "unchanged")
        assert unchanged.template is not None
        self.assertEqual(unchanged.template.revision, 2)
        versions_after = list_template_versions(
            self.session, OWNER_TG, self.created.template_id
        )
        self.assertEqual([item.version for item in versions_after], [1, 2])

    def test_stale_revision_is_not_overwritten(self) -> None:
        updated = update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.created.template_id,
            expected_revision=1,
            content="Правка из другого окна",
        )
        self.assertEqual(updated.status, "updated")
        stale = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.created.template_id,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(stale.status, "stale")
        assert stale.template is not None
        self.assertEqual(stale.template.revision, 2)
        self.assertEqual(
            stale.template.current_version.header, "Правка из другого окна"
        )

    def test_not_found_and_wrong_kind(self) -> None:
        missing = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=999_999,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(missing.status, "not_found")

        place = self.make_place("Место", created_by_user_id=self.owner.id)
        standard = self.session.scalar(
            select(Template).where(
                Template.place_id == place.id,
                Template.kind == "standard",
            )
        )
        assert standard is not None
        wrong = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.id,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(wrong.status, "not_found")


class StandardResetTests(TemplateDatabaseTestCase):
    """Сброс стандарта: администратор места, новая версия, снимки целы."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.admin = self.make_user(OTHER_TG, username="admin")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="super")
        self.place = self.make_place("Площадка", created_by_user_id=self.owner.id)
        self.make_place_admin(self.place, self.admin)
        template = self.session.scalar(
            select(Template).where(
                Template.place_id == self.place.id,
                Template.kind == "standard",
            )
        )
        assert template is not None
        self.template_id = template.id
        updated = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Правка администратора",
        )
        self.assertEqual(updated.status, "updated")
        self.snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=2,
        )

    def test_reset_creates_version_and_preserves_snapshot(self) -> None:
        result = reset_standard_template(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        self.assertEqual(result.template.revision, 3)
        builtin = get_builtin_template()
        self.assertEqual(result.template.current_version.header, builtin.header)
        self.assertEqual(result.template.current_version.body, builtin.body)
        self.assertEqual(result.template.current_version.builtin_version, 1)

        first = get_template_version(self.session, OTHER_TG, self.template_id, 1)
        second = get_template_version(self.session, OTHER_TG, self.template_id, 2)
        assert first is not None and second is not None
        self.assertEqual(first.builtin_version, 1)
        self.assertEqual(second.header, "Правка администратора")

        snapshot_row = self.session.get(TemplateSnapshot, self.snapshot.snapshot_id)
        assert snapshot_row is not None
        self.assertEqual(snapshot_row.source_template_version, 2)
        self.assertEqual(snapshot_row.header, "Правка администратора")

    def test_superadmin_can_reset_but_regular_user_cannot(self) -> None:
        forbidden = reset_standard_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template_id,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(forbidden.status, "forbidden")

        allowed = reset_standard_template(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            template_id=self.template_id,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(allowed.status, "updated")
        assert allowed.template is not None
        self.assertEqual(allowed.template.revision, 3)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
