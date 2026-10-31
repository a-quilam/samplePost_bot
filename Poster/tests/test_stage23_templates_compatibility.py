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
from template_compiler import TemplateCompileError
from template_store import (
    list_template_brick_ids,
    update_template_content,
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
        self.make_personal_brick(self.owner, "личный")
        created = self.make_personal_template(
            self.owner,
            "Личный перенос",
            content="\n".join(p for p in ("", "Тело [личный] [название]", "") if p),
        )
        compatibility = validate_template_compatibility(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        self.assertTrue(compatibility.compatible)
        self.assertEqual(compatibility.issues, ())

    def test_foreign_owner_brick_causes_incompatible(self) -> None:
        self.make_personal_brick(self.other, "чужой")
        with self.assertRaises(TemplateCompileError):
            self.make_personal_template(
                self.owner,
                "Привязка к чужому",
                content="\n".join(p for p in ("", "Тело чужого [чужой]", "") if p),
            )

    def test_archived_brick_is_flagged_but_stays_bound(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "архивный")
        created = self.make_personal_template(
            self.owner,
            "Архивный шаблон",
            content="\n".join(p for p in ("", "Тело [архивный]", "") if p),
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
            self.owner,
            "Обновляемый",
            content="\n".join(p for p in ("", "Старый текст", "") if p),
        )
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Новый текст [новый]",
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

    def test_unknown_brick_code_in_text_is_rejected(self) -> None:
        with self.assertRaises(TemplateCompileError):
            self.make_personal_template(
                self.owner,
                "Некорректные привязки",
                content="\n".join(p for p in ("", "Текст [несуществующий]", "") if p),
            )

    def test_foreign_brick_cannot_be_bound_on_create(self) -> None:
        self.make_personal_brick(self.other, "чужой_код")
        with self.assertRaises(TemplateCompileError):
            self.make_personal_template(
                self.owner,
                "Чужие привязки",
                content="\n".join(p for p in ("", "Тело [чужой_код]", "") if p),
            )

    def test_cannot_update_template_with_foreign_brick(self) -> None:
        created = self.make_personal_template(self.owner, "Собственный")
        self.make_personal_brick(self.other, "чужой_код")
        result = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Подмена [чужой_код]",
        )
        self.assertEqual(result.status, "invalid")
        self.assertIn("чужой_код", result.message or "")

    def test_place_scoped_template_cannot_use_foreign_place_brick(self) -> None:
        place = self.make_place("Локальное место")
        other_place = self.make_place("Другое место")
        self.make_personal_brick(self.other, "чужой_код")
        with self.assertRaises(TemplateCompileError):
            self.make_personal_template(
                self.owner,
                "Личный шаблон",
                content="\n".join(p for p in ("", "Тело [чужой_код]", "") if p),
            )
        del place, other_place


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
