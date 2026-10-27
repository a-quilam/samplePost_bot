# tests/test_stage27b_announcement_fields.py
"""
Stage 27B: сервисные помощники полей, готовности и предпросмотра.

Telegram-слой не должен заново определять вопросы по шаблону. Эти тесты
фиксируют единый источник полей (`build_announcement_fields`), разделение
известных/пропущенных/заполненных значений и сборку предпросмотра по снимку.
"""

from __future__ import annotations

import unittest
from datetime import date

from announcement_store import (
    FIELD_STATUS_KNOWN,
    FIELD_STATUS_MISSING,
    FIELD_STATUS_SKIPPED,
    FIELD_STATUS_VALID,
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_questions,
    save_announcement_value,
)
from announcement_wizard import (
    evaluate_announcement_readiness,
    visible_announcement_text,
)
from template_store import create_personal_template
from tests.stage27_helpers import OWNER_TG, AnnouncementDatabaseTestCase

TODAY = date(2026, 5, 1)


class FieldStateTests(AnnouncementDatabaseTestCase):
    """Поля снимка и вопросы совпадают по brick_id и статусам."""

    def test_fields_cover_snapshot_once_and_keep_known_place_values(self) -> None:
        draft = self.start_at_questions()
        snapshot = draft.template_snapshot
        assert snapshot is not None

        fields = build_announcement_fields(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )

        brick_ids = [field.brick_id for field in fields]
        self.assertEqual(len(brick_ids), len(set(brick_ids)))
        self.assertEqual(set(brick_ids), set(snapshot.brick_ids))
        self.assertEqual(
            [question.brick_id for question in questions],
            [
                field.brick_id
                for field in fields
                if field.status == FIELD_STATUS_MISSING
            ],
        )

        known = {
            field.code: field for field in fields if field.status == FIELD_STATUS_KNOWN
        }
        self.assertEqual(known["место"].raw_value, "Дом культуры")
        self.assertEqual(known["адрес"].raw_value, "ул. Тестовая, 1")
        self.assertEqual(known["ссылка_места"].raw_value, "https://example.com/place")

    def test_filled_and_explicitly_skipped_are_distinct(self) -> None:
        draft = self.start_at_questions()
        fields = build_announcement_fields(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        short_fields = [field for field in fields if field.value_type == "short_text"]
        filled_field, skipped_field = short_fields[0], short_fields[1]

        revision = draft.revision
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=filled_field.brick_id,
            value="Значение",
            expected_revision=revision,
            today=TODAY,
        )
        revision = saved.revision
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=skipped_field.brick_id,
            value="",
            expected_revision=revision,
            today=TODAY,
        )

        by_id = {
            field.brick_id: field
            for field in build_announcement_fields(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=saved.draft_id,
                today=TODAY,
            )
        }
        self.assertEqual(by_id[filled_field.brick_id].status, FIELD_STATUS_VALID)
        self.assertEqual(by_id[filled_field.brick_id].raw_value, "Значение")
        self.assertEqual(by_id[skipped_field.brick_id].status, FIELD_STATUS_SKIPPED)


class ReadinessTests(unittest.TestCase):
    """
    Готовность после S3: пропуск кирпичика не блокирует переход.

    Решение владельца (§5.6, S3 п.8): анонс, где пропущены **все**
    кирпичики, готов — заглушка `.` подставляется при сборке сообщения.
    `ready` теперь означает «можно идти дальше», а не «текст непустой»;
    видимый текст остаётся доступным вызывающему слою.
    """

    def test_empty_text_is_ready_and_has_no_visible_text(self) -> None:
        result = evaluate_announcement_readiness(
            " <b></b> ",
            snapshot_brick_count=0,
            filled_value_count=0,
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.visible_text, "")
        self.assertEqual(result.issues, ())

    def test_static_template_without_bricks_can_be_ready(self) -> None:
        result = evaluate_announcement_readiness(
            "<b>Постоянный текст</b>",
            snapshot_brick_count=0,
            filled_value_count=0,
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.visible_text, "Постоянный текст")
        self.assertEqual(result.issues, ())

    def test_template_without_any_filled_value_is_ready(self) -> None:
        result = evaluate_announcement_readiness(
            "",
            snapshot_brick_count=2,
            filled_value_count=0,
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.issues, ())

    def test_one_filled_value_is_enough_for_optional_fields(self) -> None:
        result = evaluate_announcement_readiness(
            "<b>Текст</b>",
            snapshot_brick_count=2,
            filled_value_count=1,
        )
        self.assertTrue(result.ready)
        self.assertEqual(visible_announcement_text("<i>Текст</i>"), "Текст")


class PreviewTests(AnnouncementDatabaseTestCase):
    """Предпросмотр собирается из снимка, а не из текущего шаблона."""

    def test_preview_reports_missing_then_builds_value(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "заголовок")
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Только заголовок",
            header="",
            body="Постоянный текст [заголовок]",
            footer="",
        )
        draft = self.start_at_questions(template_id=template.template_id)
        self.session.expire_all()

        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        # S3: анонс с пропущенным (но предусмотренным шаблоном) полем готов;
        # диагностика остаётся в `missing`, а не блокирует переход.
        self.assertTrue(preview.ready)
        self.assertEqual(preview.issues, ())
        self.assertEqual(
            [question.brick_id for question in preview.missing], [brick_id]
        )
        self.assertIn("Постоянный текст", preview.visible_text)

        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_id,
            value="Заголовок",
            expected_revision=draft.revision,
            today=TODAY,
        )
        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=saved.draft_id,
            today=TODAY,
        )
        self.assertTrue(preview.ready)
        self.assertIn("Заголовок", preview.visible_text)
        self.assertEqual(preview.missing, ())
        self.assertTrue(
            any(field.status == FIELD_STATUS_VALID for field in preview.fields)
        )

    def test_static_template_has_no_questions_and_is_ready(self) -> None:
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Статический",
            header="",
            body="Только постоянный текст",
            footer="",
        )
        draft = self.start_at_questions(template_id=template.template_id)
        self.session.expire_all()

        self.assertEqual(
            build_announcement_questions(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                today=TODAY,
            ),
            [],
        )
        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        self.assertTrue(preview.ready)
        self.assertEqual(preview.visible_text, "Только постоянный текст")
        self.assertEqual(preview.fields, ())


if __name__ == "__main__":
    unittest.main()
