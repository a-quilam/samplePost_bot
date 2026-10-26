# tests/test_stage27a_announcement_wizard.py
"""
Stage 27A: чистая доменная логика адаптивных вопросов.

Проверяются порядок вопросов, повторяющиеся brick_id, значения места,
различие «нет значения»/«допустимое пустое», все семь типов и диагностика
повреждённого/несовместимого снимка. Telegram и реальная БД не нужны.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from announcement_wizard import (
    AnnouncementBrickError,
    AnnouncementSnapshotError,
    AnnouncementTemplateSnapshot,
    AnnouncementValueError,
    SnapshotBinding,
    answer_payload_to_raw,
    build_questions,
    place_context_values,
    visible_brick_issue,
)
from brick_store import BrickDefinition
from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_URL,
    BrickValueResult,
    BrickValueStatus,
)
from template_types import (
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)

created = datetime(2026, 1, 1)


def definition(
    brick_id: int,
    code: str,
    value_type: str = BRICK_TYPE_SHORT_TEXT,
    *,
    scope: str = BRICK_SCOPE_SYSTEM,
    source: str = "ask",
    place_id: int | None = None,
    owner_user_id: int | None = None,
    rules: dict[str, object] | None = None,
    status: str = "active",
) -> BrickDefinition:
    return BrickDefinition(
        id=brick_id,
        code=code,
        title=f"Кирпичик {code}",
        value_type=value_type,
        scope=scope,
        source=source,
        place_id=place_id,
        owner_user_id=owner_user_id,
        rules=rules or {},
        status=status,
        revision=1,
        created_by_user_id=None,
        created_at=created,
        updated_at=None,
    )


def snapshot(
    *,
    header: str = "",
    body: str = "",
    footer: str = "",
    bindings: dict[str, int],
    snapshot_id: int = 1,
) -> AnnouncementTemplateSnapshot:
    return AnnouncementTemplateSnapshot(
        snapshot_id=snapshot_id,
        source_template_id=10,
        source_template_version=3,
        source_kind="personal",
        source_place_id=None,
        sections={
            TEMPLATE_SECTION_HEADER: header,
            TEMPLATE_SECTION_BODY: body,
            TEMPLATE_SECTION_FOOTER: footer,
        },
        bindings=tuple(
            SnapshotBinding(code=code, brick_id=brick_id)
            for code, brick_id in bindings.items()
        ),
    )


class QuestionOrderTests(unittest.TestCase):
    """Детерминированный порядок и один вопрос на brick_id."""

    def test_header_then_body_then_footer_and_repeats_once(self) -> None:
        view = snapshot(
            header="Ш [head]",
            body="[body] [head]",
            footer="[foot] [body]",
            bindings={"head": 10, "body": 11, "foot": 12},
        )
        definitions = {
            10: definition(10, "head"),
            11: definition(11, "body"),
            12: definition(12, "foot"),
        }

        questions = build_questions(view, definitions)

        self.assertEqual([item.brick_id for item in questions], [10, 11, 12])
        self.assertEqual(
            [item.section for item in questions],
            [
                TEMPLATE_SECTION_HEADER,
                TEMPLATE_SECTION_BODY,
                TEMPLATE_SECTION_FOOTER,
            ],
        )
        self.assertTrue(all(not item.required for item in questions))
        self.assertTrue(all(item.allow_empty for item in questions))

    def test_bricks_outside_snapshot_are_ignored(self) -> None:
        view = snapshot(body="[inside]", bindings={"inside": 20})
        definitions = {
            20: definition(20, "inside"),
            21: definition(21, "outside"),
        }
        questions = build_questions(
            view,
            definitions,
            context_values={21: "лишнее"},
            answers={21: BrickValueResult(status=BrickValueStatus.VALID, payload={})},
        )
        self.assertEqual([item.brick_id for item in questions], [20])

    def test_known_valid_place_values_are_not_asked(self) -> None:
        view = snapshot(
            header="[место] [адрес_места] [ссылка_места]",
            bindings={"место": 5, "адрес_места": 6, "ссылка_места": 7},
        )
        definitions = {
            5: definition(5, "место", source="place"),
            6: definition(6, "адрес_места", source="place"),
            7: definition(7, "ссылка_места", BRICK_TYPE_URL, source="place"),
        }
        values = place_context_values(
            display_name="Дом культуры",
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        self.assertEqual(build_questions(view, definitions, place_values=values), [])

        # Отсутствующий адрес/ссылка не подменяются пустой строкой.
        without_contacts = place_context_values(
            display_name="Дом культуры", address=None, url=None
        )
        self.assertEqual(
            [
                item.brick_id
                for item in build_questions(
                    view, definitions, place_values=without_contacts
                )
            ],
            [6, 7],
        )

    def test_invalid_known_value_is_explained_in_question(self) -> None:
        view = snapshot(body="[ссылка_места]", bindings={"ссылка_места": 7})
        definitions = {7: definition(7, "ссылка_места", BRICK_TYPE_URL)}
        questions = build_questions(
            view,
            definitions,
            context_values={7: "ftp://example.com/place"},
        )
        self.assertEqual(len(questions), 1)
        self.assertIsNotNone(questions[0].context_error)
        self.assertIn("http", questions[0].context_error or "")

    def test_missing_and_valid_empty_answers_are_distinct(self) -> None:
        view = snapshot(body="[note]", bindings={"note": 30})
        definitions = {30: definition(30, "note")}

        self.assertEqual(len(build_questions(view, definitions)), 1)
        self.assertEqual(
            build_questions(
                view,
                definitions,
                answers={30: BrickValueResult(status=BrickValueStatus.EMPTY_OPTIONAL)},
            ),
            [],
        )
        self.assertEqual(
            build_questions(
                view,
                definitions,
                answers={
                    30: BrickValueResult(
                        status=BrickValueStatus.VALID,
                        payload={"text": "заполнено"},
                    )
                },
            ),
            [],
        )
        self.assertEqual(
            build_questions(view, definitions, context_values={30: "   "}),
            [],
        )
        with self.assertRaises(AnnouncementValueError):
            build_questions(
                view,
                definitions,
                answers={
                    30: BrickValueResult(
                        status=BrickValueStatus.INVALID, error="битое значение"
                    )
                },
            )

    def test_all_seven_brick_types_are_questions(self) -> None:
        types = [
            BRICK_TYPE_SHORT_TEXT,
            BRICK_TYPE_LONG_TEXT,
            BRICK_TYPE_DATE,
            BRICK_TYPE_TIME,
            BRICK_TYPE_URL,
            BRICK_TYPE_NUMBER,
            BRICK_TYPE_CHOICE,
        ]
        codes = [f"тип{index}" for index in range(1, 8)]
        view = snapshot(
            body=" ".join(f"[{code}]" for code in codes),
            bindings={code: index for index, code in enumerate(codes, start=101)},
        )
        definitions = {
            index: definition(
                index,
                code,
                value_type,
                rules=(
                    {"choices": ["A", "B"]} if value_type == BRICK_TYPE_CHOICE else {}
                ),
            )
            for index, code, value_type in zip(
                range(101, 108), codes, types, strict=True
            )
        }
        questions = build_questions(
            view,
            definitions,
            today=datetime(2026, 5, 1).date(),
        )
        self.assertEqual([item.value_type for item in questions], types)
        self.assertEqual([item.brick_id for item in questions], list(range(101, 108)))


class SnapshotBindingTests(unittest.TestCase):
    """Привязки снимка не разрешаются заново по тексту."""

    def test_binding_wins_over_another_definition_with_same_code(self) -> None:
        view = snapshot(body="[событие]", bindings={"событие": 40})
        first = definition(40, "событие")
        second = definition(41, "событие")
        questions = build_questions(view, {40: first, 41: second})
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].brick_id, 40)
        self.assertEqual(questions[0].title, "Кирпичик событие")

    def test_code_cannot_have_two_different_bindings(self) -> None:
        view = AnnouncementTemplateSnapshot(
            snapshot_id=1,
            source_template_id=1,
            source_template_version=1,
            source_kind="personal",
            source_place_id=None,
            sections={TEMPLATE_SECTION_BODY: "[код]"},
            bindings=(
                SnapshotBinding(code="код", brick_id=40),
                SnapshotBinding(code="код", brick_id=41),
            ),
        )
        with self.assertRaises(AnnouncementSnapshotError):
            build_questions(
                view,
                {40: definition(40, "код"), 41: definition(41, "код")},
            )

    def test_unbound_or_unparsable_snapshot_is_diagnostic(self) -> None:
        view = snapshot(body="[код] [другой]", bindings={"код": 40})
        with self.assertRaises(AnnouncementSnapshotError):
            build_questions(view, {40: definition(40, "код")})

        broken = snapshot(body="[незакрытый", bindings={"код": 40})
        with self.assertRaises(AnnouncementSnapshotError):
            build_questions(broken, {40: definition(40, "код")})

    def test_missing_archived_and_invalid_scope_are_diagnostic(self) -> None:
        view = snapshot(body="[код]", bindings={"код": 40})
        with self.assertRaises(AnnouncementBrickError):
            build_questions(view, {})
        with self.assertRaises(AnnouncementBrickError):
            build_questions(view, {40: definition(40, "код", status="archived")})
        with self.assertRaises(AnnouncementBrickError):
            build_questions(
                view,
                {40: definition(40, "код", scope=BRICK_SCOPE_PLACE, place_id=None)},
            )

    def test_visibility_checks_place_and_personal_scope(self) -> None:
        system = definition(1, "система", scope=BRICK_SCOPE_SYSTEM)
        place = definition(2, "место", scope=BRICK_SCOPE_PLACE, place_id=5)
        personal = definition(3, "личное", scope=BRICK_SCOPE_PERSONAL, owner_user_id=7)
        self.assertIsNone(visible_brick_issue(system, place_id=5, owner_user_id=7))
        self.assertIsNone(visible_brick_issue(place, place_id=5, owner_user_id=7))
        self.assertIn(
            "другому месту",
            visible_brick_issue(place, place_id=6, owner_user_id=7) or "",
        )
        self.assertIsNone(visible_brick_issue(personal, place_id=5, owner_user_id=7))
        self.assertIn(
            "другому пользователю",
            visible_brick_issue(personal, place_id=5, owner_user_id=8) or "",
        )
        self.assertIn(
            "архиве",
            visible_brick_issue(
                definition(4, "старое", status="archived"),
                place_id=5,
                owner_user_id=7,
            )
            or "",
        )

    def test_payload_to_raw_covers_all_value_families(self) -> None:
        self.assertEqual(
            answer_payload_to_raw(
                BrickValueResult(
                    status=BrickValueStatus.VALID, payload={"text": "текст"}
                )
            ),
            "текст",
        )
        self.assertEqual(
            answer_payload_to_raw(
                BrickValueResult(
                    status=BrickValueStatus.VALID, payload={"date": "01.05.2026"}
                )
            ),
            "01.05.2026",
        )
        self.assertEqual(
            answer_payload_to_raw(
                BrickValueResult(
                    status=BrickValueStatus.VALID, payload={"choices": ["A", "B"]}
                )
            ),
            ["A", "B"],
        )
        self.assertIsNone(
            answer_payload_to_raw(
                BrickValueResult(status=BrickValueStatus.EMPTY_OPTIONAL)
            )
        )


if __name__ == "__main__":
    unittest.main()
