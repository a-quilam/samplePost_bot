# tests/test_stage27a_announcement_store.py
"""
Stage 27A: сервис адаптивного мастера анонсов.

Проверяются выбор места/названия, снимок шаблона, значения по brick_id,
дополнительные кирпичики, рестарт, optimistic locking и изоляция авторов.
Реальная `post_bot.db`, `.env` и Telegram не используются.
"""

from __future__ import annotations

import unittest
from datetime import date

from sqlalchemy import func, select, text

from announcement_store import (
    add_announcement_extra,
    build_announcement_questions,
    build_announcement_render_payload,
    choose_announcement_template,
    clear_announcement_value,
    confirm_announcement_place,
    get_announcement_draft,
    list_announcement_drafts,
    move_announcement_extra,
    remove_announcement_extra,
    save_announcement_value,
    set_announcement_stage,
    start_announcement_wizard,
)
from announcement_wizard import (
    EXTRA_POSITION_END,
    EXTRA_POSITION_START,
    AnnouncementActorError,
    AnnouncementBrickError,
    AnnouncementConflictError,
    AnnouncementError,
    AnnouncementNotFoundError,
    AnnouncementStageError,
    AnnouncementValueError,
    WizardStage,
)
from brick_store import create_personal_brick, update_personal_brick
from models import Place
from place_store import add_alternative_name
from template_parser import BrickNode, FormatNode, LinkNode
from template_store import (
    create_personal_template,
    get_accessible_template,
    update_template_body,
    update_template_header,
)
from tests.stage23_helpers import OWNER_TG
from tests.stage27_helpers import (
    OTHER_TG,
    AnnouncementDatabaseTestCase,
)

TODAY = date(2026, 5, 1)


def brick_nodes(tokens) -> list[BrickNode]:
    """
    Все токены-кирпичики секции по порядку, включая вложенные в разметку.

    Эталон §6.1.1 оборачивает шапку в `**…**`, поэтому первый элемент
    секции — `FormatNode`, а не `BrickNode`. Тест не должен зависеть от
    того, обёрнут ли токен жирным: он ищет кирпичики рекурсивно.
    """
    found: list[BrickNode] = []
    for token in tokens:
        if isinstance(token, BrickNode):
            found.append(token)
        elif isinstance(token, LinkNode):
            found.extend(brick_nodes(token.label))
            if isinstance(token.url, BrickNode):
                found.append(token.url)
        elif isinstance(token, FormatNode):
            found.extend(brick_nodes(token.children))
    return found


class PlaceSelectionTests(AnnouncementDatabaseTestCase):
    """Место выбирается первым; альтернативное название не создаёт место."""

    def test_canonical_and_alternative_names_keep_one_place(self) -> None:
        before = self.session.scalar(select(Place).where(Place.id == self.place.id))
        self.assertIsNotNone(before)

        canonical = self.start_draft()
        self.assertEqual(canonical.place_id, self.place.id)
        self.assertEqual(canonical.place_display_name, "Дом культуры")
        self.assertEqual(canonical.place_address, "ул. Тестовая, 1")
        self.assertEqual(canonical.place_url, "https://example.com/place")

        alternative = self.start_draft(place_name_id=self.alternative.name_id)
        self.assertEqual(alternative.place_id, self.place.id)
        self.assertEqual(alternative.place_name_id, self.alternative.name_id)
        self.assertEqual(alternative.place_display_name, "ДК «Тестовый»")
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(Place)),
            1,
        )

    def test_missing_address_and_url_stay_null(self) -> None:
        bare = self.make_place("Место без контактов")
        draft = self.start_draft(place_id=bare.id)
        self.assertEqual(draft.place_display_name, "Место без контактов")
        self.assertIsNone(draft.place_address)
        self.assertIsNone(draft.place_url)

    def test_invalid_place_foreign_name_and_unknown_actor_are_rejected(self) -> None:
        other_place = self.make_place("Другое место")
        foreign_name = add_alternative_name(
            self.session, other_place.id, "Чужое альтернативное"
        )
        self.assertIsNotNone(foreign_name.name_id)
        with self.assertRaises(AnnouncementNotFoundError):
            self.start_draft(place_id=999_999)
        with self.assertRaises(AnnouncementNotFoundError):
            self.start_draft(place_name_id=foreign_name.name_id)
        with self.assertRaises(AnnouncementNotFoundError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                place_id=other_place.id,
                place_name_id=self.alternative.name_id,
            )
        with self.assertRaises(AnnouncementActorError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=999_999,
                place_id=self.place.id,
            )

    def test_idempotency_key_returns_same_draft_and_rejects_other_choice(self) -> None:
        first = self.start_draft(idempotency_key="ключ-места")
        second = self.start_draft(idempotency_key="ключ-места")
        self.assertEqual(first.draft_id, second.draft_id)
        self.assertEqual(first.revision, second.revision)

        with self.assertRaises(AnnouncementConflictError):
            self.start_draft(
                place_name_id=self.alternative.name_id,
                idempotency_key="ключ-места",
            )
        other_place = self.make_place("Другое место")
        with self.assertRaises(AnnouncementConflictError):
            self.start_draft(place_id=other_place.id, idempotency_key="ключ-места")

    def test_confirmation_refreshes_contacts_and_honours_revision(self) -> None:
        draft = self.start_draft()
        self.session.execute(
            text(
                "UPDATE places SET address=NULL, url='https://new.example/place' "
                "WHERE id=:place_id"
            ),
            {"place_id": self.place.id},
        )
        self.session.commit()
        self.session.expire_all()

        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        self.assertEqual(confirmed.stage, WizardStage.TEMPLATE)
        self.assertIsNone(confirmed.place_address)
        self.assertEqual(confirmed.place_url, "https://new.example/place")
        self.assertEqual(confirmed.revision, draft.revision + 1)

        with self.assertRaises(AnnouncementConflictError):
            confirm_announcement_place(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                expected_revision=draft.revision,
            )

    def test_selected_alternative_name_is_validated_against_place(self) -> None:
        other_place = self.make_place("Чужое имя")
        alt = add_alternative_name(self.session, other_place.id, "Чужое альтернативное")
        self.assertIsNotNone(alt.name_id)
        with self.assertRaises(AnnouncementNotFoundError):
            self.start_draft(place_name_id=alt.name_id)


class TemplateSelectionTests(AnnouncementDatabaseTestCase):
    """Снимок копирует версию и привязки, но не живые правки шаблона."""

    def test_standard_snapshot_copies_exact_sections_and_bindings(self) -> None:
        draft = self.start_at_questions()
        snapshot = draft.template_snapshot
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source_template_id, self.standard.template_id)
        self.assertEqual(
            snapshot.source_template_version,
            self.standard.current_version.version,
        )
        self.assertEqual(snapshot.source_kind, "standard")
        self.assertEqual(snapshot.source_place_id, self.place.id)
        self.assertEqual(
            snapshot.sections["header"], self.standard.current_version.header
        )
        self.assertEqual(snapshot.sections["body"], self.standard.current_version.body)
        self.assertEqual(
            snapshot.sections["footer"], self.standard.current_version.footer
        )
        self.assertEqual(
            set(snapshot.brick_ids),
            set(self.standard.current_version.brick_ids),
        )
        self.assertEqual(draft.template_id, self.standard.template_id)
        self.assertEqual(draft.template_version, self.standard.current_version.version)

    def test_source_template_edits_do_not_change_started_announcement(self) -> None:
        draft = self.start_at_questions()
        assert draft.template_snapshot is not None
        old_header = draft.template_snapshot.sections["header"]
        old_body = draft.template_snapshot.sections["body"]
        old_ids = draft.template_snapshot.brick_ids

        update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            expected_revision=self.standard.revision,
            content="Новая шапка",
        )
        refreshed = get_accessible_template(
            self.session, OWNER_TG, self.standard.template_id
        )
        assert refreshed is not None
        update_template_body(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            expected_revision=refreshed.revision,
            content="Новое тело без кирпичиков",
        )

        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        assert stored.template_snapshot is not None
        self.assertEqual(stored.template_snapshot.sections["header"], old_header)
        self.assertEqual(stored.template_snapshot.sections["body"], old_body)
        self.assertEqual(stored.template_snapshot.brick_ids, old_ids)
        self.assertEqual(stored.template_version, 1)
        self.assertEqual(stored.revision, draft.revision)

    def test_personal_template_of_owner_is_allowed_but_other_user_template_is_not(
        self,
    ) -> None:
        own = self.make_personal_template(self.owner, "Мой шаблон")
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
            template_id=own.template_id,
            expected_revision=confirmed.revision,
        )
        self.assertEqual(chosen.template_id, own.template_id)
        self.assertIsNotNone(chosen.template_snapshot)
        assert chosen.template_snapshot is not None
        self.assertEqual(chosen.template_snapshot.source_kind, "personal")

        foreign = self.make_personal_template(self.other, "Чужой шаблон")
        second = self.start_draft()
        confirmed_second = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=second.draft_id,
            expected_revision=second.revision,
        )
        with self.assertRaises(AnnouncementNotFoundError):
            choose_announcement_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=confirmed_second.draft_id,
                template_id=foreign.template_id,
                expected_revision=confirmed_second.revision,
            )
        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed_second.draft_id,
        )
        self.assertEqual(stored.stage, WizardStage.TEMPLATE)
        self.assertIsNone(stored.template_id)

    def test_standard_of_another_place_is_rejected_without_snapshot(self) -> None:
        other_place = self.make_place("Другое место")
        other_standard = self.standard_template(other_place)
        draft = self.start_draft()
        confirmed = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        with self.assertRaises(AnnouncementStageError):
            choose_announcement_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=confirmed.draft_id,
                template_id=other_standard.template_id,
                expected_revision=confirmed.revision,
            )
        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
        )
        self.assertIsNone(stored.template_snapshot)
        self.assertEqual(stored.stage, WizardStage.TEMPLATE)

    def test_choose_before_place_confirmation_and_replace_are_rejected(self) -> None:
        draft = self.start_draft()
        with self.assertRaises(AnnouncementStageError):
            choose_announcement_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                template_id=self.standard.template_id,
                expected_revision=draft.revision,
            )

        chosen = self.start_at_questions()
        again = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
            template_id=self.standard.template_id,
            expected_revision=chosen.revision,
        )
        self.assertIsNotNone(again.template_snapshot)
        self.assertIsNotNone(chosen.template_snapshot)
        assert again.template_snapshot is not None
        assert chosen.template_snapshot is not None
        self.assertEqual(
            again.template_snapshot.snapshot_id, chosen.template_snapshot.snapshot_id
        )
        self.assertEqual(again.revision, chosen.revision)

        own = self.make_personal_template(self.owner, "Другой шаблон")
        with self.assertRaises(AnnouncementConflictError):
            choose_announcement_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
                template_id=own.template_id,
                expected_revision=chosen.revision,
            )

    def test_snapshot_bindings_win_over_later_code_edits(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "старый")
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Шаблон с кодом",
            header="",
            body="[старый]",
            footer="",
        )
        draft = self.start_at_questions(template_id=template.template_id)
        result = update_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=brick_id,
            expected_revision=1,
            code="новый",
            title="Новое имя",
            rules={},
        )
        self.assertEqual(result.status, "updated")

        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        assert stored.template_snapshot is not None
        self.assertEqual(stored.template_snapshot.bindings[0].code, "старый")
        self.assertEqual(stored.template_snapshot.bindings[0].brick_id, brick_id)
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].brick_id, brick_id)
        self.assertEqual(questions[0].code, "старый")


class ValueTests(AnnouncementDatabaseTestCase):
    """Значения сохраняются по brick_id и проверяются валидаторами."""

    def test_standard_questions_skip_known_place_values_and_follow_order(self) -> None:
        draft = self.start_at_questions()
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        # Эталон §6.1.1 содержит 1,2,3,5,7,8,9,10. Место/адрес/ссылка
        # подставлены из карточки площадки, поэтому спрашиваются только
        # остальные кирпичики — в порядке шапка → тело → подвал.
        self.assertEqual([item.brick_id for item in questions], [1, 2, 3, 8, 9])
        self.assertTrue(all(item.source != "place" for item in questions))

    def test_missing_place_contacts_produce_questions(self) -> None:
        bare = self.make_place("Место без контактов")
        bare_standard = self.standard_template(bare)
        draft = self.start_draft(place_id=bare.id)
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
            template_id=bare_standard.template_id,
            expected_revision=confirmed.revision,
        )
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
            today=TODAY,
        )
        # Площадка без адреса и ссылки: название места известно из карточки,
        # а `ссылка_места` (7) и `адрес` (10) контекста не имеют и становятся
        # обычными вопросами. Порядок — первое появление в снимке.
        self.assertEqual([item.brick_id for item in questions], [1, 2, 3, 7, 10, 8, 9])

    def test_save_valid_invalid_and_empty_values(self) -> None:
        draft = self.start_at_questions()
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=2,
            value="01.05.2026",
            expected_revision=draft.revision,
            today=TODAY,
        )
        value = saved.value_for(2)
        self.assertIsNotNone(value)
        assert value is not None
        self.assertEqual(value.payload, {"date": "01.05.2026"})
        self.assertEqual(saved.revision, draft.revision + 1)

        with self.assertRaises(AnnouncementValueError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=2,
                value="32.13.2026",
                today=TODAY,
            )
        unchanged = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(unchanged.revision, saved.revision)
        unchanged_value = unchanged.value_for(2)
        self.assertIsNotNone(unchanged_value)
        assert unchanged_value is not None
        self.assertEqual(unchanged_value.payload, {"date": "01.05.2026"})

        emptied = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=3,
            value="   ",
            expected_revision=saved.revision,
            today=TODAY,
        )
        empty_value = emptied.value_for(3)
        self.assertIsNotNone(empty_value)
        assert empty_value is not None
        self.assertTrue(empty_value.is_empty_optional)
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        self.assertNotIn(3, [item.brick_id for item in questions])

        cleared = clear_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=3,
            expected_revision=emptied.revision,
        )
        self.assertIsNone(cleared.value_for(3))
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            today=TODAY,
        )
        self.assertIn(3, [item.brick_id for item in questions])

    def test_value_outside_snapshot_or_archived_is_rejected(self) -> None:
        draft = self.start_at_questions()
        with self.assertRaises(AnnouncementBrickError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=999_999,
                value="x",
                expected_revision=draft.revision,
                today=TODAY,
            )

        personal = self.make_personal_brick(self.owner, "архивный")
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Архивный шаблон",
            header="",
            body="[архивный]",
            footer="",
        )
        archived_draft = self.start_at_questions(template_id=template.template_id)
        self.session.execute(
            text("UPDATE bricks SET status='archived' WHERE id=:brick_id"),
            {"brick_id": personal},
        )
        self.session.commit()
        with self.assertRaises(AnnouncementBrickError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=archived_draft.draft_id,
                brick_id=personal,
                value="x",
                expected_revision=archived_draft.revision,
                today=TODAY,
            )

    def test_all_seven_brick_types_are_saved_and_rendered(self) -> None:
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
        for code, value_type, rules, _ in specs:
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
            name="Все типы",
            header="",
            body=" ".join(f"[{code}]" for code, _, _, _ in specs),
            footer="",
        )
        draft = self.start_at_questions(template_id=template.template_id)
        revision = draft.revision
        for code, _, _, value in specs:
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
            stored_value = saved.value_for(brick_ids[code])
            self.assertIsNotNone(stored_value)
            assert stored_value is not None
            self.assertEqual(stored_value.status.value, "valid")

        self.assertEqual(
            build_announcement_questions(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                today=TODAY,
            ),
            [],
        )
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(payload.values_by_id[brick_ids["сумма"]], 42)
        self.assertEqual(payload.values_by_id[brick_ids["вариант"]], "A")
        self.assertEqual(payload.values_by_id[brick_ids["срок"]], "01.05.2026")

    def test_missing_is_not_same_as_empty_optional(self) -> None:
        brick_id = self.make_personal_brick(self.owner, "необязательный")
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Пустое",
            header="",
            body="[необязательный]",
            footer="",
        )
        draft = self.start_at_questions(template_id=template.template_id)
        self.assertEqual(
            len(
                build_announcement_questions(
                    self.session,
                    actor_telegram_user_id=OWNER_TG,
                    draft_id=draft.draft_id,
                )
            ),
            1,
        )
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_id,
            value="",
            expected_revision=draft.revision,
        )
        empty_value = saved.value_for(brick_id)
        self.assertIsNotNone(empty_value)
        assert empty_value is not None
        self.assertTrue(empty_value.is_empty_optional)
        self.assertEqual(
            build_announcement_questions(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
            ),
            [],
        )


class ExtraTests(AnnouncementDatabaseTestCase):
    """Дополнительные кирпичики принадлежат только одному анонсу."""

    def setUp(self) -> None:
        super().setUp()
        self.place_extra = self.make_place_brick("доп_места")
        self.personal_extra = self.make_personal_brick(self.owner, "доп_личный")
        self.other_personal = self.make_personal_brick(self.other, "чужой_доп")

    def test_add_in_three_sections_and_render_only_this_draft(self) -> None:
        draft = self.start_at_questions()
        snapshot_before = draft.template_snapshot
        assert snapshot_before is not None

        header = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.place_extra,
            section="header",
            position=EXTRA_POSITION_START,
            value="Шапка",
            expected_revision=draft.revision,
        )
        body = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.personal_extra,
            section="body",
            position=EXTRA_POSITION_END,
            value="Тело",
            expected_revision=header.revision,
        )
        # Третий кирпичик — тот же личный, но во втором анонсе он независим.
        second = self.start_at_questions()
        second_extra = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=second.draft_id,
            brick_id=self.place_extra,
            section="footer",
            position=EXTRA_POSITION_END,
            value="Подвал",
            expected_revision=second.revision,
        )

        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(len(stored.extras), 2)
        self.assertEqual(stored.stage, WizardStage.EXTRAS)
        assert stored.template_snapshot is not None
        self.assertEqual(stored.template_snapshot.sections, snapshot_before.sections)
        self.assertEqual(stored.template_snapshot.bindings, snapshot_before.bindings)

        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=body.draft_id,
        )
        header_nodes = brick_nodes(payload.tokens_by_section["header"])
        body_nodes = brick_nodes(payload.tokens_by_section["body"])
        # Дополнительный кирпичик вставлен в начало шапки перед базовыми.
        self.assertEqual(header_nodes[0].code, "доп_места")
        self.assertEqual(body_nodes[-1].code, "доп_личный")
        self.assertEqual(payload.values_by_id[self.place_extra], "Шапка")
        self.assertEqual(payload.values_by_id[self.personal_extra], "Тело")

        second_payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=second_extra.draft_id,
        )
        footer_nodes = brick_nodes(second_payload.tokens_by_section["footer"])
        self.assertEqual(footer_nodes[-1].code, "доп_места")
        self.assertNotIn(self.personal_extra, second_payload.values_by_id)

    def test_add_rejects_snapshot_duplicate_and_inaccessible_bricks(self) -> None:
        draft = self.start_at_questions()
        with self.assertRaises(AnnouncementBrickError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=1,
                section="body",
            )
        with self.assertRaises(AnnouncementBrickError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=self.other_personal,
                section="body",
            )
        with self.assertRaises(AnnouncementError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=self.place_extra,
                section="middle",
            )

        added = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.place_extra,
            section="body",
            expected_revision=draft.revision,
        )
        with self.assertRaises(AnnouncementBrickError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=self.place_extra,
                section="body",
                expected_revision=added.revision,
            )

    def test_move_and_remove_extra_keep_value_semantics(self) -> None:
        draft = self.start_at_questions()
        added = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.place_extra,
            section="header",
            position=EXTRA_POSITION_START,
            value="Значение",
            expected_revision=draft.revision,
        )
        extra = added.extras[0]
        moved = move_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            extra_id=extra.extra_id,
            section="footer",
            position=EXTRA_POSITION_END,
            expected_revision=added.revision,
        )
        self.assertEqual(moved.extras[0].section, "footer")
        self.assertEqual(moved.extras[0].position, EXTRA_POSITION_END)
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        header_nodes = brick_nodes(payload.tokens_by_section["header"])
        self.assertNotIn("доп_места", [node.code for node in header_nodes])
        footer_nodes = brick_nodes(payload.tokens_by_section["footer"])
        self.assertEqual(footer_nodes[-1].code, "доп_места")

        removed = remove_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            extra_id=extra.extra_id,
            expected_revision=moved.revision,
        )
        self.assertEqual(removed.extras, ())
        self.assertIsNone(removed.value_for(self.place_extra))
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertNotIn(self.place_extra, payload.values_by_id)

    def test_archived_extra_is_rejected(self) -> None:
        draft = self.start_at_questions()
        self.session.execute(
            text("UPDATE bricks SET status='archived' WHERE id=:brick_id"),
            {"brick_id": self.place_extra},
        )
        self.session.commit()
        with self.assertRaises(AnnouncementBrickError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=self.place_extra,
                section="body",
            )
        self.assertEqual(
            get_announcement_draft(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
            ).revision,
            draft.revision,
        )


class RestartAndAccessTests(AnnouncementDatabaseTestCase):
    """Рестарт, optimistic locking и запрет доступа по чужому ID."""

    def test_state_survives_new_session(self) -> None:
        draft = self.start_at_questions()
        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=2,
            value="01.05.2026",
            expected_revision=draft.revision,
            today=TODAY,
        )
        extra = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.make_place_brick("после_старта"),
            section="body",
            value="доп",
            expected_revision=saved.revision,
        )
        set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.PREVIEW,
            expected_revision=extra.revision,
        )
        self.session.close()

        restarted = self.Session()
        self.addCleanup(restarted.close)
        stored = get_announcement_draft(
            restarted,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(stored.stage, WizardStage.PREVIEW)
        stored_value = stored.value_for(2)
        self.assertIsNotNone(stored_value)
        assert stored_value is not None
        self.assertEqual(stored_value.payload, {"date": "01.05.2026"})
        self.assertEqual(len(stored.extras), 1)
        self.assertEqual(stored.extras[0].code, "после_старта")
        self.assertIsNotNone(stored.template_snapshot)
        assert stored.template_snapshot is not None
        # Эталон §6.1.1: 8 кирпичиков вместо прежних 9.
        self.assertEqual(len(stored.template_snapshot.bindings), 8)

    def test_stale_revision_cannot_overwrite(self) -> None:
        draft = self.start_at_questions()
        first = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=2,
            value="01.05.2026",
            expected_revision=draft.revision,
            today=TODAY,
        )
        with self.assertRaises(AnnouncementConflictError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=2,
                value="02.05.2026",
                expected_revision=draft.revision,
                today=TODAY,
            )
        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertEqual(stored.revision, first.revision)
        stored_value = stored.value_for(2)
        self.assertIsNotNone(stored_value)
        assert stored_value is not None
        self.assertEqual(stored_value.payload, {"date": "01.05.2026"})

        with self.assertRaises(AnnouncementConflictError):
            add_announcement_extra(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=self.make_place_brick("конфликт"),
                section="body",
                expected_revision=draft.revision,
            )
        self.assertEqual(
            get_announcement_draft(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
            ).revision,
            first.revision,
        )

    def test_other_user_cannot_read_or_mutate_draft(self) -> None:
        draft = self.start_at_questions()
        added = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.make_place_brick("чужой_доступ"),
            section="body",
            expected_revision=draft.revision,
        )
        extra_id = added.extras[0].extra_id
        calls = [
            lambda: get_announcement_draft(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
            ),
            lambda: save_announcement_value(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                brick_id=2,
                value="02.05.2026",
            ),
            lambda: clear_announcement_value(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                brick_id=2,
            ),
            lambda: add_announcement_extra(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                brick_id=self.make_place_brick("чужой_доп"),
                section="body",
            ),
            lambda: move_announcement_extra(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                extra_id=extra_id,
                section="footer",
            ),
            lambda: remove_announcement_extra(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                extra_id=extra_id,
            ),
            lambda: set_announcement_stage(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                draft_id=draft.draft_id,
                stage=WizardStage.COMPLETED,
            ),
        ]
        for call in calls:
            with self.assertRaises(AnnouncementNotFoundError):
                call()
        self.assertEqual(
            list_announcement_drafts(self.session, actor_telegram_user_id=OTHER_TG),
            [],
        )
        self.assertEqual(
            len(
                list_announcement_drafts(self.session, actor_telegram_user_id=OWNER_TG)
            ),
            1,
        )

    def test_stage_can_move_back_and_invalid_stage_is_rejected(self) -> None:
        draft = self.start_at_questions()
        extras = set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.EXTRAS,
            expected_revision=draft.revision,
        )
        preview = set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.PREVIEW,
            expected_revision=extras.revision,
        )
        self.assertEqual(preview.stage, WizardStage.PREVIEW)
        completed = set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.COMPLETED,
            expected_revision=preview.revision,
        )
        self.assertEqual(completed.stage, WizardStage.COMPLETED)
        back = set_announcement_stage(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            stage=WizardStage.QUESTIONS,
            expected_revision=completed.revision,
        )
        self.assertEqual(back.stage, WizardStage.QUESTIONS)
        with self.assertRaises(AnnouncementStageError):
            set_announcement_stage(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                stage="unknown",
                expected_revision=back.revision,
            )
        start_only = self.start_draft()
        with self.assertRaises(AnnouncementStageError):
            set_announcement_stage(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=start_only.draft_id,
                stage=WizardStage.PREVIEW,
                expected_revision=start_only.revision,
            )


if __name__ == "__main__":
    unittest.main()
