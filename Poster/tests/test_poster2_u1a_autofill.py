# tests/test_poster2_u1a_autofill.py
"""
U1a — автозаполнение анонса из справочника и перечитывание снимка.

Проверяется через доменный сервис на временной SQLite (без сети и
Telegram):

- у площадки есть ссылка, адрес и город: после фиксации шаблона живой
  рендер содержит жирное КЛИКАБЕЛЬНОЕ название площадки (`<b><a href=…>`),
  адрес отдельной строкой и ссылку из справочника; дата/время пусты —
  мастер не выдумывает значения;
- город попадает в снимок черновика (`place_city`), но НЕ в живой текст:
  `[город]` не входит в стандартный шаблон;
- первый вопрос мастера — первый неизвестный кирпичик шаблона (название),
  а не площадка и не город: эти данные уже известны из справочника;
- площадка БЕЗ ссылки: название остаётся жирным, но без `<a>`, адрес
  по-прежнему виден;
- снимок площадки перечитывается в момент выбора шаблона: адрес,
  обновлённый в справочнике ПОСЛЕ старта анонса, попадает в живой пост,
  а не остаётся старым значением из момента создания черновика.

Реальная `post_bot.db` и `.env` не открываются.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

from announcement_store import (
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_questions,
    choose_announcement_template,
    confirm_announcement_place,
    get_announcement_draft,
    start_announcement_wizard,
)
from models import Place
from place_store import create_place
from template_store import get_standard_template
from tests.stage23_helpers import TemplateDatabaseTestCase
from user_store import get_or_create_user

OWNER_TG = 9_844_001


class AutofillTestCase(TemplateDatabaseTestCase):
    """Временная база, владелец и площадка с полным набором данных."""

    place_name = "Дом культуры"
    place_address = "ул. Ленина, 1"
    place_url: str | None = "https://example.com/place"
    place_city = "Москва"

    def setUp(self) -> None:
        super().setUp()
        self.owner = get_or_create_user(self.session, OWNER_TG, username="u1a-live")
        self.place = create_place(
            self.session,
            self.place_name,
            city=self.place_city,
            created_by_user_id=self.owner.id,
            address=self.place_address,
            url=self.place_url,
        )
        self.standard = get_standard_template(self.session, OWNER_TG, self.place.id)

    # --- помощники --------------------------------------------------------

    def start_place_draft(self):
        return start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=self.place.id,
        )

    def confirm(self, draft):
        return confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )

    def choose(self, draft):
        """Подтверждает место и фиксирует шаблон."""
        confirmed = self.confirm(draft)
        return choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=self.standard.template_id,
            expected_revision=confirmed.revision,
        )

    def edit_place(self, **values: object) -> None:
        """Правит карточку справочника напрямую, минуя UI."""
        place = self.session.get(Place, self.place.id)
        assert place is not None
        for key, value in values.items():
            setattr(place, key, value)
        self.session.commit()
        self.session.expire_all()

    def preview(self, draft_id: int):
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft_id,
        )

    def fields(self, draft_id: int):
        return {
            field.code: field
            for field in build_announcement_fields(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft_id,
            )
        }


class PlaceAutofillTests(AutofillTestCase):
    """Живой рендер берёт название/адрес/ссылку из справочника."""

    def test_place_data_is_rendered_and_dates_stay_empty(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        preview = self.preview(chosen.draft_id)
        html = preview.html

        # Название площадки — жирное и кликабельное (HTML, не только текст).
        self.assertIn(
            '<b><a href="https://example.com/place">Дом культуры</a></b>', html
        )
        # Адрес отдельной нежирной строкой сразу под площадкой.
        self.assertIn("\nул. Ленина, 1\n", html)
        # Город в текст не попадает: `[город]` не входит в стандарт v4.
        self.assertNotIn("Москва", html)

        # Дата/время пусты: мастер не подставляет выдуманные значения.
        lines = html.split("\n")
        self.assertEqual(lines[2], "<b> // </b>")

        states = self.fields(chosen.draft_id)
        for code in ("название", "дата", "время_начала"):
            with self.subTest(code=code):
                self.assertTrue(states[code].is_missing, states[code].status)
        # Площадка/адрес/ссылка известны из карточки, а не «заполнены».
        for code, expected in (
            ("место", self.place_name),
            ("адрес", self.place_address),
            ("ссылка_места", self.place_url),
        ):
            with self.subTest(code=code):
                self.assertTrue(states[code].is_known, states[code].status)
                self.assertEqual(states[code].raw_value, expected)
        self.assertEqual(preview.visible_text.count("ул. Ленина, 1"), 1)

    def test_first_question_is_not_place_or_city(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
        )
        self.assertTrue(questions)
        codes = [question.code for question in questions]
        self.assertEqual(codes[0], "название", codes)
        self.assertNotIn("место", codes)
        self.assertNotIn("город", codes)
        self.assertNotIn("адрес", codes)
        self.assertNotIn("ссылка_места", codes)

    def test_city_is_stored_in_draft_snapshot(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        self.assertEqual(chosen.place_city, self.place_city)
        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
        )
        self.assertEqual(stored.place_city, "Москва")
        self.assertEqual(stored.place_address, self.place_address)
        self.assertEqual(stored.place_url, self.place_url)

    def test_snapshot_rereads_address_changed_before_template_choice(self) -> None:
        draft = self.start_place_draft()
        confirmed = self.confirm(draft)
        self.assertEqual(confirmed.place_address, "ул. Ленина, 1")

        # Админ правит карточку между подтверждением места и фиксацией
        # шаблона: снимок черновика уже содержит прежний адрес.
        self.edit_place(address="ул. Новая, 9", city="Тверь")
        self.assertEqual(confirmed.place_address, "ул. Ленина, 1")

        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=self.standard.template_id,
            expected_revision=confirmed.revision,
        )
        self.assertEqual(chosen.place_address, "ул. Новая, 9")
        self.assertEqual(chosen.place_city, "Тверь")
        html = self.preview(chosen.draft_id).html
        self.assertIn("\nул. Новая, 9\n", html)
        self.assertNotIn("ул. Ленина, 1", html)

    def test_address_removed_from_directory_disappears_from_live_text(self) -> None:
        draft = self.start_place_draft()
        confirmed = self.confirm(draft)
        self.edit_place(address=None, url=None)

        chosen = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=confirmed.draft_id,
            template_id=self.standard.template_id,
            expected_revision=confirmed.revision,
        )
        self.assertIsNone(chosen.place_address)
        self.assertIsNone(chosen.place_url)
        html = self.preview(chosen.draft_id).html
        self.assertIn("<b>Дом культуры</b>", html)
        self.assertNotIn("<a ", html)
        self.assertNotIn("ул. Ленина, 1", html)


class PlaceWithoutUrlAutofillTests(AutofillTestCase):
    """Площадка без ссылки: жирное название без `<a>`, адрес виден."""

    place_url = None

    def test_name_stays_bold_without_anchor_and_address_is_visible(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        html = self.preview(chosen.draft_id).html

        self.assertIn("<b>Дом культуры</b>", html)
        self.assertNotIn("<a ", html)
        self.assertNotIn("</a>", html)
        self.assertIn("\nул. Ленина, 1\n", html)

    def test_link_brick_is_asked_but_place_and_address_are_known(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        fields = self.fields(chosen.draft_id)
        self.assertTrue(fields["место"].is_known)
        self.assertTrue(fields["адрес"].is_known)
        # Ссылки нет в справочнике — по шаблону это вопрос, а не пустая ссылка.
        self.assertTrue(fields["ссылка_места"].is_missing)
        questions = [
            question.code
            for question in build_announcement_questions(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=chosen.draft_id,
            )
        ]
        self.assertEqual(questions[0], "название")
        self.assertIn("ссылка_места", questions)
        self.assertNotIn("место", questions)


class SnapshotIsNotALinkTests(AutofillTestCase):
    """Снимок остаётся снимком: правка справочника после выбора не влияет."""

    def test_later_directory_edits_do_not_change_the_live_text(self) -> None:
        draft = self.start_place_draft()
        chosen = self.choose(draft)
        before = self.preview(chosen.draft_id).html

        place = self.session.get(Place, self.place.id)
        assert place is not None
        place.address = "ул. Поздняя, 77"
        place.url = "https://example.com/other"
        self.session.commit()

        self.assertEqual(self.preview(chosen.draft_id).html, before)
        stored = get_announcement_draft(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=chosen.draft_id,
        )
        self.assertEqual(stored.place_address, "ул. Ленина, 1")
        self.assertEqual(stored.place_url, "https://example.com/place")
        self.assertEqual(self.session.scalars(select(Place.id)).all(), [self.place.id])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
