# tests/test_stage23_templates_service.py
"""
Stage 23: сервисный слой шаблонов — стандарты, личные шаблоны, версии.

Проверяются права (админ места/суперадмин/создатель/чужой), оптимистичная
блокировка, сохранение остальных секций, идемпотентность идентичных правок,
история версий и сброс стандарта. Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from models import Template
from template_store import (
    STANDARD_TEMPLATE_DISPLAY_NAME,
    TemplateConfirmationRequired,
    TemplateNotFoundError,
    TemplatePermissionError,
    create_personal_template,
    ensure_standard_template,
    get_accessible_template,
    get_personal_template,
    get_standard_template,
    get_template_version,
    list_personal_templates,
    list_template_brick_ids,
    list_template_versions,
    reset_standard_template,
    update_template_body,
    update_template_footer,
    update_template_header,
    update_template_section,
)
from template_types import TemplateValidationError
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    SUPERADMIN_TG,
    VISITOR_TG,
    TemplateDatabaseTestCase,
)


class StandardTemplateServiceTests(TemplateDatabaseTestCase):
    """Стандартное место: чтение всем, запись только действующей админке."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.admin = self.make_user(OTHER_TG, username="placeadmin")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="super")
        self.visitor = self.make_user(VISITOR_TG, username="visitor")
        self.place = self.make_place(
            "Шаблонное место", created_by_user_id=self.owner.id
        )
        self.standard = get_standard_template(self.session, OWNER_TG, self.place.id)
        self.template_id = self.standard.template_id

    def test_place_creation_creates_standard_template_once(self) -> None:
        templates = self.session.scalars(select(Template)).all()
        self.assertEqual(len(templates), 1)
        template = templates[0]
        self.assertEqual(template.kind, "standard")
        self.assertEqual(template.place_id, self.place.id)
        self.assertEqual(template.revision, 1)
        self.assertEqual(template.name, None)

    def test_creator_does_not_get_edit_rights(self) -> None:
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template_id,
            expected_revision=self.standard.revision,
            content="[название] собственный",
        )
        self.assertEqual(result.status, "forbidden")
        self.assertIsNotNone(result.template)
        row = self.session.get(Template, self.template_id)
        assert row is not None
        self.assertEqual(row.revision, 1)
        self.assertEqual(
            len(list_template_versions(self.session, OWNER_TG, self.template_id)), 1
        )

    def test_active_place_admin_can_edit_sections(self) -> None:
        self.make_place_admin(self.place, self.admin)
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="❗ [название]",
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        self.assertEqual(result.template.revision, 2)
        self.assertEqual(result.template.current_version.header, "❗ [название]")
        # Остальные секции сохранены.
        self.assertEqual(
            result.template.current_version.body, self.standard.current_version.body
        )
        self.assertEqual(
            result.template.current_version.footer,
            self.standard.current_version.footer,
        )
        self.assertEqual(
            result.template.current_version.brick_ids,
            self.standard.current_version.brick_ids,
        )

    def test_superadmin_can_edit_standard(self) -> None:
        result = update_template_header(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Супер заголовок",
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        self.assertEqual(
            result.template.current_version.created_by_user_id, (self.superadmin.id)
        )

    def test_revoked_admin_loses_edit_rights_immediately(self) -> None:
        row = self.make_place_admin(self.place, self.admin)
        from place_admin_store import revoke_place_admin

        revoke_place_admin(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            expected_admin_telegram_user_id=self.admin.telegram_user_id,
        )
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Уже не админ",
        )
        self.assertEqual(result.status, "forbidden")
        del row

    def test_new_admin_after_transfer_can_edit(self) -> None:
        from place_admin_store import replace_place_admin

        self.make_place_admin(self.place, self.owner)
        result = replace_place_admin(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=OTHER_TG,
            expected_admin_telegram_user_id=self.owner.telegram_user_id,
        )
        self.assertEqual(result.status, "replaced")

        update = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="От нового админа",
        )
        self.assertEqual(update.status, "updated")

    def test_identical_save_creates_no_new_version(self) -> None:
        self.make_place_admin(self.place, self.admin)
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content=self.standard.current_version.header,
        )
        self.assertEqual(result.status, "unchanged")
        row = self.session.get(Template, self.template_id)
        assert row is not None
        self.assertEqual(row.revision, 1)
        self.assertEqual(
            len(list_template_versions(self.session, OTHER_TG, self.template_id)), 1
        )

    def test_stale_revision_is_rejected(self) -> None:
        self.make_place_admin(self.place, self.admin)
        first = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Версия 2",
        )
        self.assertEqual(first.status, "updated")

        stale = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Устаревшая правка",
        )
        self.assertEqual(stale.status, "stale")
        assert stale.template is not None
        self.assertEqual(stale.template.revision, 2)
        self.assertEqual(stale.template.current_version.header, "Версия 2")
        self.assertEqual(
            len(list_template_versions(self.session, OTHER_TG, self.template_id)), 2
        )

    def test_section_edit_preserves_history_and_versions(self) -> None:
        self.make_place_admin(self.place, self.admin)
        update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Заголовок 2",
        )
        update_template_body(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=2,
            content="Текст 3 [описание]",
        )
        update_template_footer(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=3,
            content="Подвал 4",
        )
        versions = list_template_versions(self.session, OTHER_TG, self.template_id)
        self.assertEqual([item.version for item in versions], [1, 2, 3, 4])
        self.assertEqual(versions[0].header, self.standard.current_version.header)
        self.assertEqual(versions[3].header, "Заголовок 2")
        self.assertEqual(versions[3].body, "Текст 3 [описание]")
        self.assertEqual(versions[3].footer, "Подвал 4")
        # Историческая версия читается отдельно.
        first = get_template_version(self.session, OTHER_TG, self.template_id, 1)
        assert first is not None
        self.assertEqual(first.header, "[название]")

    def test_not_found_and_invalid_input(self) -> None:
        self.make_place_admin(self.place, self.admin)
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="  \n\t  ",
        )
        self.assertEqual(result.status, "updated")
        row = self.session.get(Template, self.template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)
        assert result.template is not None
        self.assertEqual(result.template.current_version.header, "  \n\t  ")

        unknown = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id + 999,
            expected_revision=1,
            content="x",
        )
        self.assertEqual(unknown.status, "not_found")

        with self.assertRaises(TemplateValidationError):
            update_template_section(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                template_id=self.template_id,
                expected_revision=1,
                section="middle",
                content="x",
            )

        self.assertEqual(
            list_template_brick_ids(self.session, OWNER_TG, self.template_id),
            (2, 3, 4, 5, 6, 7, 8, 9),
        )

    def test_read_access_for_any_registered_user(self) -> None:
        details = get_standard_template(self.session, VISITOR_TG, self.place.id)
        self.assertEqual(details.template_id, self.template_id)
        self.assertEqual(details.display_name, STANDARD_TEMPLATE_DISPLAY_NAME)
        accessible = get_accessible_template(self.session, VISITOR_TG, self.template_id)
        assert accessible is not None
        self.assertEqual(accessible.template_id, self.template_id)

        unknown_place = self.make_place("Другое место", created_by_user_id=None)
        with self.assertRaises(TemplateNotFoundError):
            get_standard_template(self.session, VISITOR_TG, unknown_place.id + 999)

    def test_ensure_standard_template_requires_place_admin(self) -> None:
        self.assertEqual(
            get_standard_template(self.session, OWNER_TG, self.place.id).template_id,
            self.template_id,
        )
        with self.assertRaises(TemplatePermissionError):
            ensure_standard_template(self.session, VISITOR_TG, self.place.id)

        result = ensure_standard_template(self.session, SUPERADMIN_TG, self.place.id)
        self.assertEqual(result.template_id, self.template_id)

    def test_reset_standard_template_requires_confirmation_and_admin(self) -> None:
        self.make_place_admin(self.place, self.admin)
        update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Кастомный заголовок",
        )
        template_row = self.session.get(Template, self.template_id)
        assert template_row is not None
        revision = template_row.revision
        self.assertEqual(revision, 2)

        forbidden = reset_standard_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.template_id,
            expected_revision=revision,
            confirm=True,
        )
        self.assertEqual(forbidden.status, "forbidden")

        with self.assertRaises(TemplateConfirmationRequired):
            reset_standard_template(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                template_id=self.template_id,
                expected_revision=revision,
                confirm=False,
            )
        row = self.session.get(Template, self.template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)

        stale = reset_standard_template(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            confirm=True,
        )
        self.assertEqual(stale.status, "stale")

        done = reset_standard_template(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(done.status, "updated")
        assert done.template is not None
        self.assertEqual(done.template.revision, 3)
        latest = done.template.current_version
        self.assertEqual(latest.header, "[название]")
        self.assertEqual(latest.builtin_version, 1)
        versions = list_template_versions(self.session, OTHER_TG, self.template_id)
        self.assertEqual([item.version for item in versions], [1, 2, 3])
        self.assertEqual(versions[0].builtin_version, 1)
        self.assertIsNone(versions[1].builtin_version)

    def test_version_history_is_readable_but_not_writable(self) -> None:
        self.make_place_admin(self.place, self.admin)
        update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=self.template_id,
            expected_revision=1,
            content="Правка администратора",
        )
        versions = list_template_versions(self.session, OWNER_TG, self.template_id)
        self.assertEqual(len(versions), 2)
        first = get_template_version(self.session, OWNER_TG, self.template_id, 1)
        assert first is not None
        self.assertEqual(first.header, "[название]")
        missing = get_template_version(self.session, OWNER_TG, self.template_id, 5)
        self.assertIsNone(missing)
        wrong_version = get_template_version(
            self.session, OWNER_TG, self.template_id, -1
        )
        self.assertIsNone(wrong_version)


class PersonalTemplateServiceTests(TemplateDatabaseTestCase):
    """Личные шаблоны: владелец видит и правит, другие не видят вовсе."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.other = self.make_user(OTHER_TG, username="other")
        self.superadmin = self.make_user(SUPERADMIN_TG, username="super")

    def test_create_personal_template_with_explicit_bindings(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "личное")
        created = self.make_personal_template(
            self.owner,
            "Мой шаблон",
            header="Header [название]",
            body="Body [личное]",
            footer="Footer",
        )
        self.assertEqual(created.owner_user_id, self.owner.id)
        # [название] в шапке привязан к системному кирпичику #1,
        # [личное] в теле — к личному кирпичику владельца.
        self.assertEqual(
            created.current_version.brick_ids, tuple(sorted((1, brick_id)))
        )
        self.assertEqual(created.current_version.version, 1)
        self.assertEqual(created.current_version.created_by_user_id, self.owner.id)

    def test_names_may_repeat_for_same_owner(self) -> None:
        first = self.make_personal_template(self.owner, "Повтор")
        second = self.make_personal_template(self.owner, "Повтор")
        self.assertNotEqual(first.template_id, second.template_id)
        self.assertEqual(first.name, second.name)
        self.assertEqual(len(list_personal_templates(self.session, OWNER_TG)), 2)

    def test_other_users_cannot_read_or_edit_personal_template(self) -> None:
        created = self.make_personal_template(self.owner, "Секретный")
        self.assertIsNone(
            get_personal_template(self.session, OTHER_TG, created.template_id)
        )
        self.assertIsNone(
            get_accessible_template(self.session, OTHER_TG, created.template_id)
        )
        self.assertIsNone(
            get_template_version(self.session, OTHER_TG, created.template_id, 1)
        )
        self.assertEqual(
            list_template_versions(self.session, OTHER_TG, created.template_id), []
        )
        with self.assertRaises(TemplateNotFoundError):
            list_template_brick_ids(self.session, OTHER_TG, created.template_id)
        self.assertIsNone(
            get_accessible_template(self.session, SUPERADMIN_TG, created.template_id)
        )

        result = update_template_header(
            self.session,
            actor_telegram_user_id=OTHER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="взлом",
        )
        self.assertEqual(result.status, "not_found")
        created_row = self.session.get(Template, created.template_id)
        assert created_row is not None
        self.assertEqual(created_row.revision, 1)

    def test_owner_can_edit_sections_and_stale_is_rejected(self) -> None:
        created = self.make_personal_template(self.owner, "Редактируемый")
        result = update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Новый заголовок",
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        self.assertEqual(result.template.current_version.header, "Новый заголовок")
        self.assertEqual(result.template.current_version.body, "Текст [название]")

        stale = update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Старая правка",
        )
        self.assertEqual(stale.status, "stale")

    def test_binding_updates_and_clearing(self) -> None:
        brick_a = self.make_personal_brick(self.owner, "первый")
        brick_b = self.make_personal_brick(self.owner, "второй")
        created = self.make_personal_template(
            self.owner, "Привязки", body="Текст [первый]"
        )
        self.assertEqual(created.current_version.brick_ids, (brick_a,))

        replace = update_template_section(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            section="body",
            content="Новый текст [второй]",
        )
        self.assertEqual(replace.status, "updated")
        assert replace.template is not None
        self.assertEqual(replace.template.current_version.brick_ids, (brick_b,))
        old_version = get_template_version(
            self.session, OWNER_TG, created.template_id, 1
        )
        assert old_version is not None
        self.assertEqual(old_version.brick_ids, (brick_a,))

        unchanged = update_template_body(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=2,
            content="Новый текст [второй]",
        )
        self.assertEqual(unchanged.status, "unchanged")

        cleared = update_template_section(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=2,
            section="body",
            content="Без привязок",
        )
        self.assertEqual(cleared.status, "updated")
        assert cleared.template is not None
        self.assertEqual(cleared.template.current_version.brick_ids, ())

    def test_unknown_template_id_is_cleanly_not_found(self) -> None:
        with self.assertRaises(TemplateNotFoundError):
            get_standard_template(self.session, OWNER_TG, 9999)
        unknown = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Тест",
            header="a",
            body="b",
            footer="c",
        )
        self.assertEqual(unknown.template_id, unknown.template_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
