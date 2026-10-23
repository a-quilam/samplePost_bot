# tests/test_stage23_templates_compatibility.py
"""
Stage 23: совместимость привязок кирпичиков.

Проверяется, что службы привязок и совместимости шаблонов решают свои задачи
только с постоянными brick_id: права владельца, архивность и переносы между
местами. Реальная post_bot.db не открывается.
"""

from __future__ import annotations

import unittest

from brick_store import archive_personal_brick
from template_store import (
    TemplateError,
    list_template_brick_ids,
    update_template_body,
    validate_template_compatibility,
)
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    TemplateDatabaseTestCase,
)


class TemplateCompatibilityTests(TemplateDatabaseTestCase):
    """Права кирпичиков: системные/личные/местные и архивность."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.other = self.make_user(OTHER_TG, username="other")

    def test_standard_template_compatible_with_system_and_own_place_bricks(
        self,
    ) -> None:
        place = self.make_place("Место совместимости")
        standard = self.make_standard_template(place)
        compatibility = validate_template_compatibility(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.template_id,
        )
        self.assertTrue(compatibility.compatible)
        self.assertEqual(compatibility.issues, ())
        self.assertEqual(
            tuple(binding.brick_id for binding in compatibility.bindings),
            standard.current_version.brick_ids,
        )

    def test_other_place_standard_is_incompatible(self) -> None:
        first = self.make_place("Первое место")
        second = self.make_place("Второе место")
        standard = self.make_standard_template(first)
        compatibility = validate_template_compatibility(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.template_id,
            selected_place_id=second.id,
        )
        self.assertFalse(compatibility.compatible)
        self.assertTrue(any("другому месту" in issue for issue in compatibility.issues))

    def test_personal_template_uses_only_owner_and_system_bricks(self) -> None:
        system_id = 1  # системный кирпичик «название»
        brick_id = self.make_personal_brick(self.owner, "личный")
        created = self.make_personal_template(
            self.owner,
            "Личный перенос",
            body="Тело [личный] [название]",
            brick_ids=(system_id, brick_id),
        )
        compatibility = validate_template_compatibility(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.assertTrue(compatibility.compatible)
        self.assertEqual(compatibility.issues, ())

    def test_foreign_owner_brick_causes_incompatible(self) -> None:
        brick = self.make_personal_brick(self.other, "чужой")
        with self.assertRaises(TemplateError):
            self.make_personal_template(
                self.owner,
                "Привязка к чужому",
                body="Тело чужого [чужой]",
                brick_ids=(brick,),
            )

    def test_archived_brick_is_flagged_but_stays_bound(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "архивный")
        created = self.make_personal_template(
            self.owner,
            "Архивный шаблон",
            body="Тело [архивный]",
            brick_ids=(brick_id,),
        )
        self.assertTrue(
            validate_template_compatibility(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
            ).compatible
        )
        archive_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=brick_id,
            expected_revision=1,
        )
        compatibility = validate_template_compatibility(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.assertFalse(compatibility.compatible)
        self.assertTrue(any("в архиве" in issue for issue in compatibility.issues))
        # Привязка не исчезает после архивирования.
        self.assertEqual(created.current_version.brick_ids, (brick_id,))
        self.assertEqual(
            list_template_brick_ids(self.session, OWNER_TG, created.template_id),
            (brick_id,),
        )

    def test_update_template_body_binds_new_ids_by_brick_id(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "новый")
        created = self.make_personal_template(
            self.owner, "Обновляемый", body="Старый текст"
        )
        update_template_body(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Новый текст [новый]",
            brick_ids=(brick_id,),
        )
        from template_store import list_template_brick_ids

        self.assertEqual(
            list_template_brick_ids(self.session, OWNER_TG, created.template_id),
            (brick_id,),
        )
        self.assertEqual(
            validate_template_compatibility(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
            ).compatible,
            True,
        )

    def test_unknown_brick_id_in_bindings_is_rejected(self) -> None:
        with self.assertRaises(TemplateError):
            self.make_personal_template(
                self.owner, "Некорректные привязки", brick_ids=(99999,)
            )

    def test_foreign_brick_cannot_be_bound_on_create(self) -> None:
        foreign = self.make_personal_brick(self.other, "чужой_код")
        with self.assertRaises(TemplateError):
            self.make_personal_template(
                self.owner, "Чужие привязки", brick_ids=(foreign,)
            )

    def test_cannot_update_template_with_foreign_brick(self) -> None:
        created = self.make_personal_template(self.owner, "Собственный")
        foreign = self.make_personal_brick(self.other, "чужой_код")
        from template_store import update_template_header

        with self.assertRaises(TemplateError):
            update_template_header(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
                expected_revision=1,
                content="Подмена",
                brick_ids=(foreign,),
            )
        created = self.make_personal_template(self.owner, "Собственный 2")

    def test_place_scoped_template_cannot_use_foreign_place_brick(self) -> None:
        place = self.make_place("Локальное место")
        other_place = self.make_place("Другое место")
        foreign = self.make_personal_brick(self.other, "чужой_код")
        with self.assertRaises(TemplateError):
            self.make_personal_template(
                self.owner,
                "Личный шаблон",
                body="Тело [чужой_код]",
                brick_ids=(foreign,),
            )
        del place, other_place, foreign


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
