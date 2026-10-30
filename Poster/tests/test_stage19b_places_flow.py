# tests/test_stage19b_places_flow.py
"""
Stage 19B: интеграционные сценарии Telegram-справочника мест.

Все сценарии идут через настоящий Application.process_update, FakeBot и
временную SQLite. Проверяются фактический текст экрана, кнопки, ответы
callback и сохранённые строки; живой Telegram и настоящая база не нужны.
"""

from __future__ import annotations

import unittest

from sqlalchemy import func, select
from telegram.error import BadRequest

try:
    from announcement_store import list_announcement_drafts
    from brick_store import create_personal_brick
    from models import AnnouncementDraft, Place, PlaceName, Template, User
    from place_state import load_place_session, save_place_session
    from place_store import normalize_place_name
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def insert_place(
    h,
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    address: str | None = None,
    url: str | None = None,
) -> tuple[int, int, list[int]]:
    """Вставляет место, каноническое и альтернативные названия напрямую."""
    session = h.session_factory()
    try:
        place = Place(address=address, url=url)
        session.add(place)
        session.flush()
        canonical = PlaceName(
            place_id=place.id,
            name=name,
            name_search=normalize_place_name(name),
            is_canonical=1,
        )
        session.add(canonical)
        session.flush()
        alias_ids: list[int] = []
        for alias in aliases:
            row = PlaceName(
                place_id=place.id,
                name=alias,
                name_search=normalize_place_name(alias),
                is_canonical=0,
            )
            session.add(row)
            session.flush()
            alias_ids.append(row.id)
        session.commit()
        return place.id, canonical.id, alias_ids
    finally:
        session.close()


def place_count(h) -> int:
    session = h.session_factory()
    try:
        return int(session.scalar(select(func.count()).select_from(Place)) or 0)
    finally:
        session.close()


def name_rows(h, place_id: int) -> list[PlaceName]:
    session = h.session_factory()
    try:
        return list(
            session.scalars(
                select(PlaceName)
                .where(PlaceName.place_id == place_id)
                .order_by(PlaceName.id)
            ).all()
        )
    finally:
        session.close()


def callback_for(h, prefix: str) -> str:
    matches = [
        data for _, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {h.screen_buttons()!r}\n{h.screen_text()}"
        )
    return matches[0]


async def open_hub(h, *, user_id: int | None = None) -> None:
    await h.callback("places", user_id=user_id)
    assert "📍 Места" in h.screen_text(), h.screen_text()


async def open_search(h, query: str) -> None:
    if load_place_session(h.user_id) is None:
        await open_hub(h)
    await h.callback("pl_search")
    assert "Введите часть названия" in h.screen_text(), h.screen_text()
    await h.text(query)


async def open_card(h, query: str, place_id: int) -> None:
    await open_search(h, query)
    await h.callback(f"pl_open:{place_id}")
    assert "✅ Подтвердить" in h.screen_text(), h.screen_text()
    assert ("◀️ Вернуться к списку", "pl_back") in h.screen_buttons()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacesNavigationTests(unittest.TestCase):
    @scenario_test
    async def test_main_menu_has_places_and_hub_has_required_actions(self, h):
        await h.text("/start")
        # Poster 2.0 S2: площадки открываются из «⚙️ Управление».
        self.assertIn(("📍 Создать для площадки", "pick_place"), h.screen_buttons())
        self.assertIn(("⚙️ Управление", "manage"), h.screen_buttons())

        await open_hub(h)
        buttons = h.screen_buttons()
        self.assertIn(("🔎 Найти место", "pl_search"), buttons)
        self.assertIn(("📚 Все места", "pl_all"), buttons)
        self.assertIn(("➕ Добавить место", "pl_add"), buttons)
        self.assertIn(("◀️ Назад", "main_menu"), buttons)

        await h.callback("main_menu")
        self.assertIn("Выберите действие", h.screen_text())

    @scenario_test
    async def test_places_command_opens_section(self, h):
        await h.text("/places")
        self.assertIn("📍 Места", h.screen_text())
        self.assertIn(("🔎 Найти место", "pl_search"), h.screen_buttons())

    @scenario_test
    async def test_empty_places_list_is_not_a_dead_end(self, h):
        await open_hub(h)
        await h.callback("pl_all")
        self.assertIn("пока нет мест", h.screen_text())
        self.assertIn(("➕ Добавить место", "pl_add"), h.screen_buttons())
        self.assertIn(("◀️ Назад", "main_menu"), h.screen_buttons())
        self.assertEqual(place_count(h), 0)

    @scenario_test
    async def test_small_list_of_five_places_fits_without_pagination(self, h):
        for index in range(5):
            insert_place(h, f"Малое место {index}")

        await open_hub(h)
        await h.callback("pl_all")

        for index in range(5):
            self.assertIn(f"Малое место {index}", h.screen_text())
        self.assertIn("Показано 1–5 из 5.", h.screen_text())
        self.assertFalse(
            any(data and data.startswith("pl_page:") for _, data in h.screen_buttons())
        )

    @scenario_test
    async def test_long_names_keep_search_screen_within_telegram_limit(self, h):
        long_names = []
        for index in range(8):
            prefix = f"{index}. Театр "
            canonical = prefix + "Я" * (300 - len(prefix))
            long_names.append(canonical)
            insert_place(h, canonical, aliases=(f"Сцена {index}",))

        await open_search(h, "Театр")

        text = h.screen_text()
        self.assertLessEqual(len(text), 4096)
        for canonical in long_names:
            self.assertIn(canonical, text)
        self.assertEqual(
            len(
                [data for _, data in h.screen_buttons() if data.startswith("pl_open:")]
            ),
            8,
        )

    @scenario_test
    async def test_all_places_paginates_500_without_showing_technical_ids(self, h):
        session = h.session_factory()
        try:
            for index in range(500):
                session.add(
                    Place(
                        address=None,
                        url=None,
                    )
                )
            session.flush()
            places = list(session.scalars(select(Place).order_by(Place.id)).all())
            for index, place in enumerate(places):
                session.add(
                    PlaceName(
                        place_id=place.id,
                        name=f"Место {index:03d}",
                        name_search=f"место {index:03d}",
                        is_canonical=1,
                    )
                )
            session.commit()
        finally:
            session.close()

        await open_hub(h)
        await h.callback("pl_all")
        text = h.screen_text()
        self.assertIn("500", text)
        self.assertIn("Место 000", text)
        opens = [
            data
            for _, data in h.screen_buttons()
            if data and data.startswith("pl_open:")
        ]
        self.assertEqual(len(opens), 8)
        next_data = callback_for(h, "pl_page:")
        self.assertNotIn("#1", text)
        self.assertNotIn("place_id", text)
        self.assertNotIn("ID:", text)

        await h.callback(next_data)
        self.assertIn("Место 008", h.screen_text())
        self.assertNotEqual(text, h.screen_text())
        self.assertEqual(
            len([d for _, d in h.screen_buttons() if d and d.startswith("pl_open:")]), 8
        )

    @scenario_test
    async def test_card_hides_empty_address_and_link_and_shows_known_ones(self, h):
        first, _, _ = insert_place(h, "Место без контактов")
        second, _, _ = insert_place(
            h,
            "Место с контактами",
            address="ул. Театральная, 5",
            url="https://example.com/place",
        )

        await open_card(h, "без контактов", first)
        self.assertNotIn("Адрес", h.screen_text())
        self.assertNotIn("Ссылка", h.screen_text())

        await h.callback("pl_back")
        await open_search(h, "с контактами")
        await h.callback(f"pl_open:{second}")
        text = h.screen_text()
        self.assertIn("Адрес", text)
        self.assertIn("ул. Театральная, 5", text)
        self.assertIn("Ссылка", text)
        self.assertIn("https://example.com/place", text)

    @scenario_test
    async def test_search_ranks_canonical_alias_partial_and_similar(self, h):
        house, _, _ = insert_place(h, "Кофейня «Дом»", aliases=("Дом у моря",))
        second, _, _ = insert_place(h, "Кофейня Дома")

        await open_search(h, "Кофейня Дом")
        text = h.screen_text()
        self.assertIn("Результаты поиска: «Кофейня Дом»", text)
        self.assertIn("точное название", text)
        self.assertIn("Кофейня Дома", text)
        self.assertEqual(callback_for(h, "pl_open:"), f"pl_open:{house}")

        await open_search(h, "ДОМ У МОРЯ")
        self.assertIn("альтернативное название", h.screen_text())
        self.assertIn(f"pl_open:{house}", [d for _, d in h.screen_buttons()])

        await open_search(h, "коф")
        self.assertIn("частичное совпадение", h.screen_text())
        ids = {
            data
            for _, data in h.screen_buttons()
            if data and data.startswith("pl_open:")
        }
        self.assertEqual(ids, {f"pl_open:{house}", f"pl_open:{second}"})

        await open_search(h, "Кофейня Доммм")
        self.assertIn("похожее название", h.screen_text())
        self.assertIn(f"pl_open:{house}", [d for _, d in h.screen_buttons()])

    @scenario_test
    async def test_search_handles_unicode_yo_case_spaces_and_punctuation(self, h):
        place, _, _ = insert_place(h, "Ёлка-Кафе «Тёплый Дом»")

        await open_search(h, "  ЕЛКА—КАФЕ, ТЕПЛЫЙ   ДОМ ")
        self.assertIn(f"pl_open:{place}", [d for _, d in h.screen_buttons()])
        self.assertIn("точное название", h.screen_text())

    @scenario_test
    async def test_search_does_not_create_place_when_nothing_found(self, h):
        await open_search(h, "Совсем новое место")
        self.assertIn("Ничего не найдено", h.screen_text())
        self.assertIn(("➕ Добавить место", "pl_add"), h.screen_buttons())
        self.assertEqual(place_count(h), 0)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceCreationFlowTests(unittest.TestCase):
    @scenario_test
    async def test_new_place_requires_check_and_explicit_confirmation(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        self.assertIn("Введите название нового места", h.screen_text())
        input_update = await h.text("Новый Театр")

        text = h.screen_text()
        self.assertIn("Проверьте, нет ли уже этого места в базе", text)
        self.assertIn("Новый Театр", text)
        self.assertIn(("✅ Это новое место", "pl_create_confirm"), h.screen_buttons())
        self.assertIn(("✏️ Исправить название", "pl_create_edit"), h.screen_buttons())
        self.assertEqual(place_count(h), 0, "до подтверждения место не создаётся")

        # Poster 2.0 U1a: место создаётся только после ответа о городе.
        await h.callback("pl_create_confirm")
        self.assertIn("В каком городе площадка", h.screen_text())
        self.assertEqual(place_count(h), 0, "город ещё не указан")
        await h.text("Москва")
        self.assertEqual(place_count(h), 1)
        self.assertIn("✅ Место добавлено", h.screen_text())
        self.assertIn("Новый Театр", h.screen_text())
        self.assertIn("✅ Подтвердить", h.screen_text())

        session = h.session_factory()
        try:
            place = session.scalars(select(Place)).one()
            names = list(
                session.scalars(
                    select(PlaceName).where(PlaceName.place_id == place.id)
                ).all()
            )
        finally:
            session.close()
        self.assertEqual(len(names), 1)
        self.assertEqual(names[0].name, "Новый Театр")
        self.assertEqual(names[0].is_canonical, 1)
        self.assertIn(input_update.message.message_id, h.deleted_ids())

        await h.callback(callback_for(h, "pl_confirm:"))
        self.assertIn("Место выбрано", h.screen_text())
        self.assertIn("Новый Театр", h.screen_text())
        session = h.session_factory()
        try:
            # Поток мест не создаёт анонсов: конструктор живёт отдельно.
            self.assertEqual(
                session.scalar(select(func.count()).select_from(AnnouncementDraft)),
                0,
            )
        finally:
            session.close()
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "confirmed")
        self.assertNotIn("draft", state)

    @scenario_test
    async def test_back_from_new_place_card_returns_to_all_places(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Новое место для списка")
        await h.callback("pl_create_confirm")
        await h.text("Москва")

        await h.callback("pl_back")
        self.assertIn("📚 Все места", h.screen_text())
        self.assertIn("Новое место для списка", h.screen_text())
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "all_places")
        self.assertEqual(place_count(h), 1)

    @scenario_test
    async def test_new_place_is_immediately_available_to_another_user(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Общее место")
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        await h.callback(callback_for(h, "pl_confirm:"))
        await h.callback("main_menu")

        await open_hub(h, user_id=8)
        await h.callback("pl_all", user_id=8)
        self.assertIn("Общее место", h.screen_text())
        self.assertIn("pl_open:", str(h.screen_buttons()))

    @scenario_test
    async def test_creating_place_does_not_grant_admin_or_expose_template_ui(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Место для всех")
        await h.callback("pl_create_confirm")
        await h.text("Москва")

        session = h.session_factory()
        try:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(User)) or 0, 1
            )
            # Stage 23: стандартный шаблон создан атомарно, но UI шаблонов
            # появится только на Stage 25. Poster 2.0 S2 добавляет второй
            # шаблон — стартовый личный, выданный при первом контакте, —
            # поэтому считаем именно стандартные шаблоны места.
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(Template)
                    .where(Template.kind == "standard")
                )
                or 0,
                1,
            )
        finally:
            session.close()
        self.assertNotIn("администратор", h.screen_text().lower())
        self.assertNotIn("шаблон", h.screen_text().lower())
        from base import Base

        self.assertFalse({"draft_values"} & set(Base.metadata.tables))
        self.assertIn("templates", set(Base.metadata.tables))

    @scenario_test
    async def test_duplicate_check_offers_existing_and_alias_requires_confirmation(
        self, h
    ):
        place_id, _, _ = insert_place(h, "Кофейня Дом")

        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Дом")
        self.assertIn("Кофейня Дом", h.screen_text())
        existing_button = callback_for(h, "pl_open:")
        self.assertEqual(existing_button, f"pl_open:{place_id}")

        await h.callback(existing_button)
        text = h.screen_text()
        self.assertIn("Вы выбрали существующее место", text)
        self.assertIn("сохранить введённое название как альтернативное", text.lower())
        self.assertEqual(len(name_rows(h, place_id)), 1, "алиас не добавлен молча")

        await h.callback("pl_save_candidate")
        rows = name_rows(h, place_id)
        self.assertEqual([row.name for row in rows], ["Кофейня Дом", "Дом"])
        self.assertEqual(place_count(h), 1)
        self.assertIn("Дом", h.screen_text())
        self.assertIn("Кофейня Дом", h.screen_text())

    @scenario_test
    async def test_user_can_refuse_alias_after_choosing_existing(self, h):
        place_id, _, _ = insert_place(h, "Кофейня Дом")

        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Дом")
        await h.callback(callback_for(h, "pl_open:"))
        await h.callback("pl_skip_candidate")

        self.assertEqual(len(name_rows(h, place_id)), 1)
        self.assertIn("✅ Подтвердить", h.screen_text())
        await h.callback(callback_for(h, "pl_confirm:"))
        self.assertIn("Место выбрано", h.screen_text())

    @scenario_test
    async def test_exact_duplicate_does_not_create_second_place_without_intent(self, h):
        place_id, _, _ = insert_place(h, "Кофейня Дом")

        await open_hub(h)
        await h.callback("pl_add")
        await h.text("кофейня дом")
        self.assertIn("точное совпадение", h.screen_text().lower())
        await h.callback(callback_for(h, "pl_open:"))
        self.assertIn("уже совпадает", h.screen_text().lower())
        self.assertEqual(place_count(h), 1)
        self.assertEqual(len(name_rows(h, place_id)), 1)

    @scenario_test
    async def test_explicit_confirmation_allows_two_similar_places(self, h):
        insert_place(h, "Кофейня Дом")

        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Кофейня Дом")
        await h.callback("pl_create_confirm")
        await h.text("Москва")

        self.assertEqual(place_count(h), 2)
        self.assertIn("✅ Место добавлено", h.screen_text())
        # Оба места остаются независимыми; никакого слияния нет.
        self.assertEqual(len(name_rows(h, 1)), 1)

    @scenario_test
    async def test_edit_candidate_before_creation(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Опечатка")
        await h.callback("pl_create_edit")
        self.assertIn("Введите название нового места", h.screen_text())
        await h.text("Исправленное место")
        self.assertIn("Исправленное место", h.screen_text())
        self.assertEqual(place_count(h), 0)
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        self.assertEqual(place_count(h), 1)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AlternativeNameFlowTests(unittest.TestCase):
    @scenario_test
    async def test_add_alternative_and_select_it_without_changing_place(self, h):
        place_id, canonical_id, _ = insert_place(h, "Театр")

        await open_card(h, "Театр", place_id)
        await h.callback("pl_names:0")
        self.assertIn("Названия места", h.screen_text())
        self.assertIn(("➕ Добавить название", "pl_addname"), h.screen_buttons())

        await h.callback("pl_addname")
        self.assertIn("Введите альтернативное название", h.screen_text())
        await h.text("Драмтеатр")
        self.assertIn("Драмтеатр", h.screen_text())
        self.assertIn("Театр", h.screen_text())

        rows = name_rows(h, place_id)
        self.assertEqual([row.name for row in rows], ["Театр", "Драмтеатр"])
        self.assertEqual(rows[1].is_canonical, 0)
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["place_id"], place_id)
        self.assertEqual(state["display_name_id"], rows[1].id)
        self.assertEqual(place_count(h), 1)

        await h.callback("pl_names:0")
        await h.callback(f"pl_name:{canonical_id}")
        self.assertIn("✅ Подтвердить", h.screen_text())
        self.assertIn("Театр", h.screen_text())
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["display_name_id"], canonical_id)
        self.assertEqual(state["place_id"], place_id)

        await h.callback(callback_for(h, "pl_confirm:"))
        self.assertIn("Место выбрано", h.screen_text())
        self.assertIn("Театр", h.screen_text())

    @scenario_test
    async def test_duplicate_alternative_rejected_and_failed_input_cleaned(self, h):
        place_id, _, _ = insert_place(h, "Театр", aliases=("Драм-Театр",))

        await open_card(h, "Театр", place_id)
        await h.callback("pl_names:0")
        await h.callback("pl_addname")
        failed = await h.text("драм-театр")
        self.assertIn("уже есть", h.screen_text().lower())
        self.assertNotIn(failed.message.message_id, h.deleted_ids())
        self.assertEqual(len(name_rows(h, place_id)), 2)

        await h.text("Новая сцена")
        self.assertEqual(
            [row.name for row in name_rows(h, place_id)][-1], "Новая сцена"
        )
        self.assertIn(failed.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_conflict_with_other_place_requires_explicit_choice(self, h):
        first, _, _ = insert_place(h, "Клуб")
        second, _, _ = insert_place(h, "Клуб Север")

        await open_card(h, "Клуб", first)
        await h.callback("pl_names:0")
        await h.callback("pl_addname")
        await h.text("Клуб Север")

        text = h.screen_text().lower()
        self.assertIn("похоже", text)
        self.assertIn("клуб север", text)
        self.assertIn("другом месте", text)
        self.assertEqual(len(name_rows(h, first)), 1)
        self.assertEqual(
            ("✅ Да, добавить", "pl_alias_force") in h.screen_buttons(), True
        )
        self.assertIn(("❌ Нет, отмена", "pl_alias_cancel"), h.screen_buttons())

        await h.callback("pl_alias_cancel")
        self.assertEqual(len(name_rows(h, first)), 1)
        self.assertEqual(place_count(h), 2)

        await h.callback("pl_names:0")
        await h.callback("pl_addname")
        await h.text("Клуб Север")
        await h.callback("pl_alias_force")
        self.assertEqual(len(name_rows(h, first)), 2)
        self.assertEqual(len(name_rows(h, second)), 1)
        self.assertEqual(place_count(h), 2)

    @scenario_test
    async def test_names_screen_paginates_without_losing_full_list_access(self, h):
        place_id, _, _ = insert_place(
            h,
            "Место с вариантами",
            aliases=tuple(f"Вариант {index:02d}" for index in range(15)),
        )
        await open_card(h, "Место с вариантами", place_id)
        await h.callback("pl_names:0")
        self.assertIn("Вариант 00", h.screen_text())
        self.assertIn("pl_names:6", str(h.screen_buttons()))
        await h.callback("pl_names:6")
        self.assertIn("Вариант 06", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacesReliabilityTests(unittest.TestCase):
    @scenario_test
    async def test_restart_resumes_create_check_and_screen(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Место после рестарта")
        self.assertIn("Проверьте, нет ли уже", h.screen_text())

        await h.restart()
        await h.callback("places")

        self.assertIn("Проверьте, нет ли уже", h.screen_text())
        self.assertIn("Место после рестарта", h.screen_text())
        self.assertEqual(place_count(h), 0)
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        self.assertEqual(place_count(h), 1)

    @scenario_test
    async def test_broken_page_value_in_saved_state_does_not_break_screen(self, h):
        insert_place(h, "Место с битой страницей")
        save_place_session(
            h.user_id,
            {"phase": "all_places", "page": "не число"},
        )

        await h.callback("places")
        self.assertIn("📚 Все места", h.screen_text())
        self.assertIn("Место с битой страницей", h.screen_text())
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["page"], 0)

    @scenario_test
    async def test_restart_during_search_input_accepts_text_after_restart(self, h):
        place_id, _, _ = insert_place(h, "Театр")
        await open_hub(h)
        await h.callback("pl_search")
        self.assertIn("Введите часть названия", h.screen_text())

        await h.restart()
        await h.text("Театр")

        self.assertIn("Результаты поиска", h.screen_text())
        self.assertIn(f"pl_open:{place_id}", [d for _, d in h.screen_buttons()])

    @scenario_test
    async def test_deleted_screen_is_recreated_and_ref_saved(self, h):
        await open_hub(h)
        old_screen = h.screen_message_id()
        h.bot.errors_by_type["edit_message"] = BadRequest("Message to edit not found")
        try:
            await h.callback("pl_all")
        finally:
            h.bot.errors_by_type.clear()

        self.assertIn("пока нет мест", h.screen_text())
        self.assertNotEqual(h.screen_message_id(), old_screen)
        self.assertIsNotNone(h.screen_message_id())

    @scenario_test
    async def test_old_search_result_button_is_stale_after_new_query(self, h):
        first_id, _, _ = insert_place(h, "Первый театр")
        insert_place(h, "Второй клуб")

        await open_search(h, "Первый театр")
        old_button = callback_for(h, "pl_open:")
        self.assertEqual(old_button, f"pl_open:{first_id}")

        await h.callback("pl_search")
        await h.text("Второй клуб")
        self.assertIn("Второй клуб", h.screen_text())

        await h.callback(old_button)
        self.assertIn("Второй клуб", h.screen_text())
        self.assertNotIn("Место выбрано", h.screen_text())
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "search_results")
        self.assertEqual(state["query"], "Второй клуб")
        self.assertEqual(place_count(h), 2)
        self.assertTrue(
            any("устарел" in str(call.get("text", "")).lower() for call in h.answers())
        )

    @scenario_test
    async def test_forged_and_stale_callbacks_do_not_change_data(self, h):
        place_id, _, _ = insert_place(h, "Театр")
        await open_card(h, "Театр", place_id)

        await h.callback("pl_confirm:999999")
        self.assertEqual(place_count(h), 1)
        self.assertIn("✅ Подтвердить", h.screen_text())
        self.assertTrue(
            any("устарел" in str(call.get("text", "")).lower() for call in h.answers())
        )

        await h.callback("pl_names:0")
        await h.callback("pl_name:999999")
        state = load_place_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "names")

        await h.callback("pl_back")
        await h.callback(callback_for(h, "pl_confirm:"))
        self.assertIn("Место выбрано", h.screen_text())
        confirmation = (
            callback_for(h, "pl_confirm:")
            if any(d and d.startswith("pl_confirm:") for _, d in h.screen_buttons())
            else f"pl_confirm:{place_id}"
        )
        await h.callback(confirmation)
        self.assertIn("Место выбрано", h.screen_text())
        self.assertEqual(place_count(h), 1)

    @scenario_test
    async def test_callback_without_active_place_session_is_stale(self, h):
        await h.text("/start")
        before = h.screen_text()
        await h.callback("pl_all")
        self.assertEqual(h.screen_text(), before)
        self.assertTrue(h.answers())

    @scenario_test
    async def test_cancel_and_start_clear_place_state(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Отменяемое место")
        self.assertIsNotNone(load_place_session(h.user_id))

        await h.text("/cancel")
        self.assertIn("Сценарий выбора места отменён", h.screen_text())
        self.assertIsNone(load_place_session(h.user_id))
        self.assertEqual(place_count(h), 0)

        await open_hub(h)
        await h.callback("pl_search")
        self.assertIsNotNone(load_place_session(h.user_id))
        await h.text("/start")
        self.assertIn("Выберите действие", h.screen_text())
        self.assertIsNone(load_place_session(h.user_id))

        await h.text("Обычный текст вне места")
        self.assertNotIn("Введите часть названия", h.screen_text())

    @scenario_test
    async def test_place_deleted_between_search_and_confirm_is_reported(self, h):
        place_id, _, _ = insert_place(h, "Исчезнувшее место")
        await open_card(h, "Исчезнувшее место", place_id)

        session = h.session_factory()
        try:
            place = session.get(Place, place_id)
            assert place is not None
            session.delete(place)
            session.commit()
        finally:
            session.close()

        await h.callback(f"pl_confirm:{place_id}")
        self.assertIn("не существует", h.screen_text().lower())
        self.assertIn("📍 Места", h.screen_text())
        self.assertEqual(place_count(h), 0)

    @scenario_test
    async def test_invalid_and_too_long_input_kept_until_fixed(self, h):
        too_long = "Я" * 5000
        await open_hub(h)
        await h.callback("pl_add")
        failed = await h.text(too_long)
        self.assertIn("длин", h.screen_text().lower())
        self.assertEqual(place_count(h), 0)
        self.assertNotIn(failed.message.message_id, h.deleted_ids())

        await h.text("Короткое имя")
        self.assertIn("Проверьте, нет ли уже", h.screen_text())
        self.assertIn(failed.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_repeated_create_and_confirm_callbacks_are_safe(self, h):
        await open_hub(h)
        await h.callback("pl_add")
        await h.text("Однократное место")
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        # Устаревшая кнопка подтверждения названия больше не создаёт место.
        await h.callback("pl_create_confirm")
        self.assertEqual(place_count(h), 1)
        await h.callback(callback_for(h, "pl_confirm:"))
        session = h.session_factory()
        try:
            confirmation_data = f"pl_confirm:{session.query(Place).one().id}"
        finally:
            session.close()
        await h.callback(confirmation_data)
        self.assertEqual(place_count(h), 1)
        self.assertIn("Место выбрано", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacesIsolationTests(unittest.TestCase):
    """
    Поток мест изолирован от конструктора анонса (Poster 2.0 S5).

    Легаси-WIP и черновики девятишагового мастера удалены; проверяется
    сохранность живого анонса: значения, фото и запись `announcement_drafts`
    не меняются, анонс можно продолжить после ухода в места.
    """

    @staticmethod
    def _seed_announcement(h) -> tuple[int, str]:
        """Личный шаблон и начатый анонс; возвращает (draft_id, title)."""
        title = "Анонс до мест"
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="places")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="ttl",
                title="Заголовок",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Для мест",
                header="",
                body="[ttl]",
                footer="",
            )
            session.commit()
            template_id = template.template_id
        return template_id, title

    async def _start_announcement(self, h) -> int:
        template_id, title = self._seed_announcement(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text(title)
        with h.session_factory() as session:
            drafts = list_announcement_drafts(
                session, actor_telegram_user_id=DEFAULT_USER_ID
            )
        self.assertEqual(len(drafts), 1, "анонс не создан")
        return drafts[0].draft_id

    @scenario_test
    async def test_place_flow_does_not_touch_existing_announcement(self, h):
        draft_id = await self._start_announcement(h)
        with h.session_factory() as session:
            before = session.get(AnnouncementDraft, draft_id)
            before_values = before.revision
            before_stage = before.stage

        await open_hub(h)
        await h.callback("pl_all")
        await h.callback("pl_add")
        await h.text("Место рядом с анонсом")
        await h.callback("pl_create_confirm")
        await h.text("Москва")
        await h.callback("main_menu")

        with h.session_factory() as session:
            after = session.get(AnnouncementDraft, draft_id)
            self.assertIsNotNone(after, "анонс не удалён потоком мест")
            self.assertEqual(after.revision, before_values)
            self.assertEqual(after.stage, before_stage)
            self.assertEqual(
                len(
                    list_announcement_drafts(
                        session, actor_telegram_user_id=DEFAULT_USER_ID
                    )
                ),
                1,
                "дублей анонса не появилось",
            )

    @scenario_test
    async def test_place_flow_keeps_announcement_continuable(self, h):
        draft_id = await self._start_announcement(h)
        await open_hub(h)
        await h.callback("pl_search")
        await h.text("Дом")
        await h.callback("main_menu")

        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft_id}")
        # Одиночное поле уже заполнено: анонс открывается обзором, а не
        # вопросом — данные не потеряны.
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertIn("Анонс до мест", h.screen_text())
        self.assertEqual(place_count(h), 0)

    @scenario_test
    async def test_place_text_does_not_intercept_active_wizard(self, h):
        await self._start_announcement(h)
        # Шаг фото: текст не должен уходить в поиск мест.
        await h.callback(
            [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("aw_photo_none")
            ][0]
        )
        before = place_count(h)
        await h.text("Кофейня «Дом»")

        self.assertEqual(place_count(h), before, "место не создано из текста")
        self.assertIn("✅ Анонс готов", h.screen_text())

    @scenario_test
    async def test_create_post_does_not_start_legacy_wizard_or_clear_place(self, h):
        """S5: `/create_post` не запускает мастер и не сбрасывает контекст мест."""
        await open_hub(h)
        await h.callback("pl_search")
        await h.text("/create_post")

        self.assertIsNotNone(load_place_session(h.user_id), "сессия места сохранена")
        self.assertIsNone(h.dialog_state(name="announcement_wizard"))
        self.assertNotIn("Шаг 1 из 9", h.screen_text())
        self.assertEqual(place_count(h), 0)

    @scenario_test
    async def test_places_during_wizard_leaves_dialog_and_keeps_announcement(self, h):
        draft_id = await self._start_announcement(h)

        await h.callback("places")
        self.assertIsNone(h.dialog_state(name="announcement_wizard"))
        self.assertIn("📍 Места", h.screen_text())
        with h.session_factory() as session:
            drafts = list_announcement_drafts(
                session, actor_telegram_user_id=DEFAULT_USER_ID
            )
        self.assertEqual([draft.draft_id for draft in drafts], [draft_id])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
