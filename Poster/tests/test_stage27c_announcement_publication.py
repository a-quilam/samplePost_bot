# tests/test_stage27c_announcement_publication.py
"""
Stage 27C: сервис фотографий и честная модель публикации.

Проверяются отдельная коллекция вложений, заморозка готового HTML/фото,
продолжение после сбоя, запрет правки после частичного успеха и защита от
повторной публикации. Реальная база не открывается.
"""

from __future__ import annotations

import json
from datetime import date

from sqlalchemy import text

from announcement_store import (
    MAX_ANNOUNCEMENT_PHOTOS,
    PUBLICATION_STATUS_FAILED,
    PUBLICATION_STATUS_PARTIAL,
    PUBLICATION_STATUS_SENDING,
    PUBLICATION_STATUS_SENT,
    add_announcement_photo,
    begin_announcement_publication,
    choose_announcement_template,
    clear_announcement_photos,
    confirm_announcement_place,
    get_announcement_draft,
    mark_announcement_publication_failed,
    mark_announcement_publication_sent,
    record_announcement_publication_progress,
    remove_announcement_photo,
    reset_failed_announcement_publication,
    resume_announcement_publication,
    save_announcement_value,
    set_announcement_stage,
)
from announcement_wizard import (
    AnnouncementConflictError,
    AnnouncementPublicationError,
    AnnouncementStageError,
    AnnouncementValueError,
    WizardStage,
)
from template_store import update_template_content
from tests.stage27_helpers import OTHER_TG, OWNER_TG, AnnouncementDatabaseTestCase

TODAY = date(2026, 5, 1)


class AnnouncementPublicationTestCase(AnnouncementDatabaseTestCase):
    """Готовый личный шаблон и анонс, доведённый до обзора."""

    def ready_draft(self, *, body: str = "Событие [main]"):
        brick_id = self.make_personal_brick(self.owner, "main")
        template = self.make_personal_template(
            self.owner,
            "Личный анонс",
            content="\n".join(p for p in ("", body, "") if p),
        )
        draft = self.start_draft()
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=template.template_id,
            expected_revision=confirmed.revision,
        )
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
            brick_id=brick_id,
            value="Значение анонса",
            expected_revision=chosen.revision,
            today=TODAY,
        )
        return (
            set_announcement_stage(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=saved.draft_id,
                stage=WizardStage.PREVIEW,
                expected_revision=saved.revision,
            ),
            template,
            brick_id,
        )


class AnnouncementPhotoServiceTests(AnnouncementPublicationTestCase):
    def test_add_is_idempotent_and_remove_clear_keep_other_data(self) -> None:
        draft, _template, brick_id = self.ready_draft()
        first = add_announcement_photo(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            file_id="photo-a",
            expected_revision=draft.revision,
        )
        self.assertEqual(first.photos, ("photo-a",))
        self.assertEqual(first.revision, draft.revision + 1)

        duplicate = add_announcement_photo(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            file_id="photo-a",
            expected_revision=first.revision,
        )
        self.assertEqual(duplicate.revision, first.revision)
        self.assertEqual(duplicate.photos, ("photo-a",))

        second = add_announcement_photo(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            file_id="photo-b",
            expected_revision=first.revision,
        )
        self.assertEqual(second.photos, ("photo-a", "photo-b"))
        second_value = second.value_for(brick_id)
        draft_value = draft.value_for(brick_id)
        assert second_value is not None
        assert draft_value is not None
        self.assertEqual(second_value.payload, draft_value.payload)

        removed = remove_announcement_photo(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            index=0,
            expected_revision=second.revision,
        )
        self.assertEqual(removed.photos, ("photo-b",))
        cleared = clear_announcement_photos(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=removed.revision,
        )
        self.assertEqual(cleared.photos, ())

    def test_photo_limit_and_stale_revision(self) -> None:
        draft, _template, _brick = self.ready_draft()
        photos = [f"photo-{index}" for index in range(MAX_ANNOUNCEMENT_PHOTOS)]
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE announcement_drafts SET photos = :photos WHERE id = :id"),
                {"photos": json.dumps(photos), "id": draft.draft_id},
            )
        self.session.expire_all()
        full = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        with self.assertRaises(AnnouncementValueError):
            add_announcement_photo(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                file_id="one-too-many",
                expected_revision=full.revision,
            )
        with self.assertRaises(AnnouncementConflictError):
            add_announcement_photo(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                file_id="stale",
                expected_revision=full.revision + 1,
            )

    def test_other_actor_cannot_see_or_change_photos(self) -> None:
        draft, _template, _brick = self.ready_draft()
        with self.assertRaises(Exception):
            add_announcement_photo(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                file_id="foreign",
                expected_revision=draft.revision,
            )
        with self.assertRaises(Exception):
            get_announcement_draft(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
            )
        self.assertEqual(draft.photos, ())


class AnnouncementPublicationServiceTests(AnnouncementPublicationTestCase):
    def test_begin_freezes_html_and_photos_and_blocks_content_edits(self) -> None:
        draft, template, brick_id = self.ready_draft(body="СТАРЫЙ ТЕКСТ [main]")
        with_photos = add_announcement_photo(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            file_id="photo-1",
            expected_revision=draft.revision,
        )
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=with_photos.draft_id,
            expected_revision=with_photos.revision,
            today=TODAY,
        )
        self.assertEqual(
            publication.draft.publication_status, PUBLICATION_STATUS_SENDING
        )
        self.assertIn("СТАРЫЙ ТЕКСТ", publication.text)
        self.assertEqual(publication.photos, ("photo-1",))
        self.assertEqual(publication.progress, {})
        self.assertIsNotNone(publication.draft.publication_started_at)

        # Исходный шаблон можно менять, но замороженный payload — нет.
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template.template_id,
            expected_revision=template.revision,
            content="НОВЫЙ ТЕКСТ [main]",
        )
        resumed = resume_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=publication.draft.draft_id,
            expected_revision=publication.draft.revision,
        )
        self.assertEqual(resumed.text, publication.text)
        self.assertNotIn("НОВЫЙ ТЕКСТ", resumed.text)

        with self.assertRaises(AnnouncementPublicationError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=publication.draft.draft_id,
                brick_id=brick_id,
                value="Другое",
                expected_revision=resumed.draft.revision,
                today=TODAY,
            )
        with self.assertRaises(AnnouncementPublicationError):
            add_announcement_photo(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=publication.draft.draft_id,
                file_id="photo-2",
                expected_revision=resumed.draft.revision,
            )
        with self.assertRaises(AnnouncementPublicationError):
            set_announcement_stage(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=publication.draft.draft_id,
                stage=WizardStage.EXTRAS,
                expected_revision=resumed.draft.revision,
            )

    def test_failed_before_first_operation_can_reset_to_editing(self) -> None:
        draft, _template, brick_id = self.ready_draft()
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            today=TODAY,
        )
        progressed = record_announcement_publication_progress(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=publication.draft.draft_id,
            progress={
                "fingerprint": "fp",
                "step": 0,
                "ids": [],
                "total": 1,
            },
        )
        self.assertEqual(progressed.publication_step, 0)
        failed = mark_announcement_publication_failed(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=progressed.draft_id,
            error="Telegram API: timeout",
        )
        self.assertEqual(failed.publication_status, PUBLICATION_STATUS_FAILED)
        self.assertTrue(failed.publication_can_reset)
        self.assertFalse(failed.publication_sent)
        with self.assertRaises(AnnouncementPublicationError):
            mark_announcement_publication_sent(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=failed.draft_id,
            )
        reset = reset_failed_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=failed.draft_id,
            expected_revision=failed.revision,
        )
        self.assertIsNone(reset.publication_status)
        self.assertIsNone(reset.publication_text)
        self.assertEqual(reset.publication_photos, ())
        self.assertEqual(reset.publication_progress, {})
        self.assertEqual(reset.stage, WizardStage.PREVIEW)
        editable = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=reset.draft_id,
            brick_id=brick_id,
            value="После сброса",
            expected_revision=reset.revision,
            today=TODAY,
        )
        stored_value = editable.value_for(brick_id)
        self.assertIsNotNone(stored_value)
        assert stored_value is not None
        self.assertEqual(stored_value.payload, {"text": "После сброса"})

    def test_partial_cannot_reset_and_resume_skips_confirmed_step(self) -> None:
        draft, _template, _brick = self.ready_draft()
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            today=TODAY,
        )
        progressed = record_announcement_publication_progress(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=publication.draft.draft_id,
            progress={
                "fingerprint": "fp",
                "step": 1,
                "ids": [501],
                "total": 2,
            },
        )
        failed = mark_announcement_publication_failed(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=progressed.draft_id,
            error="Telegram API: second message failed",
        )
        self.assertEqual(failed.publication_status, PUBLICATION_STATUS_PARTIAL)
        self.assertFalse(failed.publication_can_reset)
        with self.assertRaises(AnnouncementPublicationError):
            reset_failed_announcement_publication(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=failed.draft_id,
                expected_revision=failed.revision,
            )

        resumed = resume_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=failed.draft_id,
            expected_revision=failed.revision,
        )
        self.assertEqual(resumed.progress["step"], 1)
        self.assertEqual(resumed.progress["ids"], [501])
        sent = mark_announcement_publication_sent(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=resumed.draft.draft_id,
        )
        self.assertEqual(sent.publication_status, PUBLICATION_STATUS_SENT)
        self.assertEqual(sent.stage, WizardStage.COMPLETED)
        self.assertIsNotNone(sent.published_at)
        # Повторная фиксация успеха идемпотентна и не создаёт новый revision.
        again = mark_announcement_publication_sent(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=sent.draft_id,
        )
        self.assertEqual(again.revision, sent.revision)
        # Stage 28: доставленный анонс можно пересобрать. Прошлые адреса не
        # теряются — они переносятся в `previous_ids`, а `ids` новой отправки
        # появится только после её подтверждения.
        rebuilt = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=sent.draft_id,
            expected_revision=sent.revision,
            today=TODAY,
        )
        self.assertEqual(rebuilt.draft.publication_status, PUBLICATION_STATUS_SENDING)
        self.assertIsNone(rebuilt.draft.published_at)
        self.assertEqual(rebuilt.draft.previous_message_ids, (501,))
        self.assertEqual(rebuilt.draft.delivered_message_ids, ())
        self.assertEqual(rebuilt.draft.publication_text, sent.publication_text)
        self.assertEqual(rebuilt.draft.publication_photos, ())

    def test_begin_requires_ready_preview_and_rejects_stale_revision(self) -> None:
        draft = self.start_at_questions()
        with self.assertRaises(AnnouncementStageError):
            begin_announcement_publication(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
                today=TODAY,
            )
        ready, _template, _brick = self.ready_draft()
        with self.assertRaises(AnnouncementConflictError):
            begin_announcement_publication(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=ready.draft_id,
                expected_revision=ready.revision - 1,
                today=TODAY,
            )

    def test_progress_validation_and_status_guards(self) -> None:
        draft, _template, _brick = self.ready_draft()
        with self.assertRaises(AnnouncementPublicationError):
            record_announcement_publication_progress(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                progress={
                    "fingerprint": "fp",
                    "step": 0,
                    "ids": [],
                    "total": 1,
                },
            )
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            today=TODAY,
        )
        for bad in (
            {"fingerprint": "", "step": 0, "ids": [], "total": 1},
            {"fingerprint": "fp", "step": -1, "ids": [], "total": 1},
            {"fingerprint": "fp", "step": 2, "ids": [], "total": 1},
            {"fingerprint": "fp", "step": 0, "ids": ["x"], "total": 1},
        ):
            with self.assertRaises(AnnouncementValueError):
                record_announcement_publication_progress(
                    self.session,
                    actor_telegram_user_id=OWNER_TG,
                    draft_id=publication.draft.draft_id,
                    progress=bad,
                )


class AnnouncementPublicationTriggerTests(AnnouncementPublicationTestCase):
    def test_invalid_media_json_status_and_shape_are_rejected(self) -> None:
        draft, _template, _brick = self.ready_draft()
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET photos='not-json', "
                        "revision=revision+1 WHERE id=:id"
                    ),
                    {"id": draft.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET photos='{}', "
                        "revision=revision+1 WHERE id=:id"
                    ),
                    {"id": draft.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET "
                        "publication_progress='[]', revision=revision+1 "
                        "WHERE id=:id"
                    ),
                    {"id": draft.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET "
                        "publication_status='unknown', revision=revision+1 "
                        "WHERE id=:id"
                    ),
                    {"id": draft.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET "
                        "publication_status='sending', revision=revision+1 "
                        "WHERE id=:id"
                    ),
                    {"id": draft.draft_id},
                )

    def test_published_payload_cannot_be_changed_or_reopened(self) -> None:
        draft, _template, _brick = self.ready_draft()
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
            today=TODAY,
        )
        sent = mark_announcement_publication_sent(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=publication.draft.draft_id,
        )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET publication_text='Другое', "
                        "revision=revision+1 WHERE id=:id"
                    ),
                    {"id": sent.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET publication_status='failed', "
                        "revision=revision+1 WHERE id=:id"
                    ),
                    {"id": sent.draft_id},
                )
        with self.assertRaises(Exception):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE announcement_drafts SET publication_progress='{}', "
                        "revision=revision+1 WHERE id=:id"
                    ),
                    {"id": sent.draft_id},
                )


class AnnouncementPublicationContentTests(AnnouncementDatabaseTestCase):
    def test_seven_types_and_extras_are_frozen(self) -> None:
        from announcement_store import add_announcement_extra
        from brick_store import create_personal_brick
        from template_store import create_personal_template

        specs: list[tuple[str, str, dict[str, object], str]] = [
            ("короткий", "short_text", {}, "короткий текст"),
            ("длинный", "long_text", {}, "длинный текст"),
            ("срок", "date", {}, "01.05.2026"),
            ("час", "time", {}, "18:30"),
            ("веб", "url", {}, "https://example.com/event"),
            ("сумма", "number", {"min": 1}, "42"),
            ("вариант", "choice", {"choices": ["A", "B"]}, "A"),
        ]
        brick_ids: dict[str, int] = {}
        for code, value_type, rules, _value in specs:
            brick_ids[code] = create_personal_brick(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                code=code,
                title=code,
                value_type=value_type,
                rules=rules,
            ).id
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Все типы и extras",
            content="\n".join(
                p
                for p in (
                    "ЗАГОЛОВОК",
                    " ".join(f"[{code}]" for code, _t, _r, _v in specs),
                    "ПОДВАЛ",
                )
                if p
            ),
        )
        draft = self.start_at_questions(template_id=template.template_id)
        revision = draft.revision
        for code, _value_type, _rules, value in specs:
            saved = save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=brick_ids[code],
                value=value,
                expected_revision=revision,
                today=TODAY,
            )
            revision = saved.revision

        # H2: доп. кирпичики дописываются строками в конец текста в порядке
        # добавления — секций и позиций больше нет.
        extras = [
            ("экстра_заголовок", "ЭКСТРА-ЗАГОЛОВОК"),
            ("экстра_тело", "ЭКСТРА-ТЕЛО"),
            ("экстра_подвал", "ЭКСТРА-ПОДВАЛ"),
        ]
        for code, value in extras:
            brick_id = create_personal_brick(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                code=code,
                title=code,
                value_type="short_text",
            ).id
            added = add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=brick_id,
                value=value,
                expected_revision=revision,
            )
            revision = added.revision
        preview_ready = set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.PREVIEW,
            expected_revision=revision,
        )
        publication = begin_announcement_publication(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=preview_ready.draft_id,
            expected_revision=preview_ready.revision,
            today=TODAY,
        )
        for _code, _value_type, _rules, value in specs:
            self.assertIn(value, publication.text)
        self.assertIn("ЭКСТРА-ЗАГОЛОВОК", publication.text)
        self.assertIn("ЭКСТРА-ТЕЛО", publication.text)
        self.assertIn("ЭКСТРА-ПОДВАЛ", publication.text)
        self.assertLess(
            publication.text.index("ПОДВАЛ"),
            publication.text.index("ЭКСТРА-ЗАГОЛОВОК"),
        )
        self.assertLess(
            publication.text.index("ЭКСТРА-ЗАГОЛОВОК"),
            publication.text.index("ЭКСТРА-ТЕЛО"),
        )
        self.assertLess(
            publication.text.index("ЭКСТРА-ТЕЛО"),
            publication.text.index("ЭКСТРА-ПОДВАЛ"),
        )
        self.assertNotIn("[фото]", publication.text)
        self.assertEqual(publication.photos, ())
