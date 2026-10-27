# tests/test_stage22a_brick_service.py
"""
Stage 22A: сервисный слой реестра кирпичиков без Telegram.

Проверяются авторизация, видимость, приватность, уникальность кодов,
детерминированное разрешение имён, источники значений и конкурентность.
"""

from __future__ import annotations

import inspect
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select, text

from brick_store import (
    BrickActorError,
    BrickConflictError,
    BrickNotFoundError,
    BrickPermissionError,
    can_view_brick,
    check_code_availability,
    create_personal_brick,
    create_place_brick,
    create_system_brick,
    get_system_brick,
    get_visible_brick,
    list_available_bricks,
    list_personal_bricks,
    list_place_bricks,
    list_system_bricks,
    resolve_brick_by_code,
    resolve_brick_value,
)
from brick_types import BrickRulesError, BrickValidationError, BrickValueStatus
from models import Brick, PlaceName
from place_store import add_alternative_name
from tests.stage22a_helpers import BrickDatabaseTestCase


class BrickServiceTests(BrickDatabaseTestCase):
    def count_bricks(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(Brick)) or 0)

    def test_system_reads_exclude_archived_but_keep_reserved_code(self) -> None:
        # 9 кирпичиков миграции 6 + `[адрес]` миграции 15.
        self.assertEqual(len(list_system_bricks(self.session)), 10)
        title = get_system_brick(self.session, " НАЗВАНИЕ ")
        self.assertIsNotNone(title)
        assert title is not None
        self.assertEqual((title.id, title.code), (1, "название"))
        self.assertIsNone(get_system_brick(self.session, "нет такого"))
        self.assertIsNone(get_system_brick(self.session, "a[b"))

        with self.engine.begin() as connection:
            connection.execute(text("UPDATE bricks SET status='archived' WHERE id=1"))
        self.session.expire_all()
        self.assertIsNone(get_system_brick(self.session, "название"))
        archived = get_system_brick(self.session, "название", include_archived=True)
        self.assertIsNotNone(archived)
        assert archived is not None
        self.assertFalse(archived.is_active)
        self.assertEqual(len(list_system_bricks(self.session)), 9)
        self.assertIsNone(resolve_brick_by_code(self.session, "название"))

    def test_personal_creation_normalizes_code_and_never_accepts_foreign_owner(
        self,
    ) -> None:
        user = self.make_user(2001, username="owner")
        row = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="  ЦЕНА ",
            title="  Моя цена  ",
            value_type="number",
            rules={"min": 0, "integer": True},
        )
        self.assertEqual(row.code, "цена")
        self.assertEqual(row.title, "Моя цена")
        self.assertEqual(row.scope, "personal")
        self.assertEqual(row.owner_user_id, user.id)
        self.assertEqual(row.created_by_user_id, user.id)
        self.assertIsNone(row.place_id)
        self.assertEqual(row.rules, {"integer": True, "min": 0})
        params = inspect.signature(create_personal_brick).parameters
        self.assertNotIn("owner_user_id", params)
        self.assertNotIn("scope", params)
        with self.assertRaises(BrickActorError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=999_999,
                code="чужая",
                title="Чужая",
                value_type="short_text",
            )

    def test_place_creation_requires_place_admin_or_superadmin(self) -> None:
        admin = self.make_user(2002)
        ordinary = self.make_user(2003)
        superadmin = self.make_user(self.SUPERADMIN_TG)
        place = self.make_place("Дом культуры")
        other_place = self.make_place("Библиотека")
        self.make_place_admin(place, admin)

        with self.assertRaises(BrickPermissionError):
            create_place_brick(
                self.session,
                actor_telegram_user_id=ordinary.telegram_user_id,
                place_id=place.id,
                code="меню",
                title="Меню",
                value_type="short_text",
            )
        self.assertEqual(
            self.session.scalar(
                select(func.count()).select_from(Brick).where(Brick.code == "меню")
            ),
            0,
        )
        created = create_place_brick(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            code="меню",
            title="Меню",
            value_type="short_text",
        )
        self.assertEqual(created.scope, "place")
        self.assertEqual(created.place_id, place.id)
        self.assertIsNone(created.owner_user_id)
        self.assertEqual(created.created_by_user_id, admin.id)

        super_created = create_place_brick(
            self.session,
            actor_telegram_user_id=superadmin.telegram_user_id,
            place_id=other_place.id,
            code="меню",
            title="Меню другого места",
            value_type="short_text",
        )
        self.assertEqual(super_created.place_id, other_place.id)
        with self.assertRaises(BrickNotFoundError):
            create_place_brick(
                self.session,
                actor_telegram_user_id=admin.telegram_user_id,
                place_id=999_999,
                code="нет",
                title="Нет",
                value_type="short_text",
            )

    def test_system_creation_is_superadmin_only_and_new_code_is_reserved(self) -> None:
        ordinary = self.make_user(2004)
        superadmin = self.make_user(self.SUPERADMIN_TG)
        with self.assertRaises(BrickPermissionError):
            create_system_brick(
                self.session,
                actor_telegram_user_id=ordinary.telegram_user_id,
                code="глобальный",
                title="Глобальный",
                value_type="short_text",
            )
        created = create_system_brick(
            self.session,
            actor_telegram_user_id=superadmin.telegram_user_id,
            code="глобальный",
            title="Глобальный",
            value_type="choice",
            rules={"choices": ["да", "нет"]},
        )
        self.assertEqual(created.scope, "system")
        self.assertGreater(created.id, 10)
        self.assertEqual(len(list_system_bricks(self.session)), 11)
        with self.assertRaises(BrickConflictError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=ordinary.telegram_user_id,
                code="глобальный",
                title="Подмена",
                value_type="short_text",
            )
        with self.assertRaises(BrickConflictError):
            create_system_brick(
                self.session,
                actor_telegram_user_id=superadmin.telegram_user_id,
                code="глобальный",
                title="Дубль",
                value_type="short_text",
            )

    def test_code_availability_reports_invalid_forbidden_reserved_and_duplicate(
        self,
    ) -> None:
        user = self.make_user(2005)
        place = self.make_place("Место проверки кода")
        self.make_place_admin(place, user)
        invalid = check_code_availability(
            self.session, "a", scope="personal", owner_user_id=user.id
        )
        self.assertFalse(invalid.available)
        self.assertEqual(invalid.reason, "invalid")
        forbidden = check_code_availability(
            self.session, "ФОТО", scope="personal", owner_user_id=user.id
        )
        self.assertFalse(forbidden.available)
        self.assertEqual(forbidden.reason, "forbidden")
        reserved = check_code_availability(
            self.session, "название", scope="personal", owner_user_id=user.id
        )
        self.assertFalse(reserved.available)
        self.assertEqual(reserved.reason, "system_reserved")
        self.assertTrue(
            check_code_availability(
                self.session, "цена", scope="personal", owner_user_id=user.id
            ).available
        )
        create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="цена",
            title="Цена",
            value_type="number",
        )
        duplicate = check_code_availability(
            self.session, "цена", scope="personal", owner_user_id=user.id
        )
        self.assertFalse(duplicate.available)
        self.assertEqual(duplicate.reason, "duplicate")
        in_system = check_code_availability(self.session, "цена", scope="system")
        self.assertFalse(in_system.available)
        self.assertEqual(in_system.reason, "duplicate")
        create_place_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
            code="цена",
            title="Цена места",
            value_type="number",
        )
        place_duplicate = check_code_availability(
            self.session, "цена", scope="place", place_id=place.id
        )
        self.assertFalse(place_duplicate.available)
        self.assertEqual(place_duplicate.reason, "duplicate")

    def test_private_personal_brick_isolation_and_foreign_id_rejection(self) -> None:
        owner = self.make_user(2006)
        other = self.make_user(2007)
        row = create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code="заметка",
            title="Личная заметка",
            value_type="long_text",
        )
        self.assertIsNotNone(
            get_visible_brick(
                self.session, row.id, viewer_telegram_user_id=owner.telegram_user_id
            )
        )
        self.assertIsNone(
            get_visible_brick(
                self.session, row.id, viewer_telegram_user_id=other.telegram_user_id
            )
        )
        self.assertFalse(
            can_view_brick(
                self.session, row.id, viewer_telegram_user_id=other.telegram_user_id
            )
        )
        self.assertEqual(
            resolve_brick_by_code(
                self.session, "заметка", viewer_telegram_user_id=other.telegram_user_id
            ),
            None,
        )
        other_rows = list_available_bricks(
            self.session, viewer_telegram_user_id=other.telegram_user_id
        )
        self.assertNotIn(row.id, [item.id for item in other_rows])
        self.assertEqual(list_personal_bricks(self.session, other.telegram_user_id), [])
        with self.assertRaises(BrickActorError):
            list_personal_bricks(self.session, 999_999)

    def test_place_visibility_requires_the_selected_place(self) -> None:
        admin = self.make_user(2008)
        viewer = self.make_user(2009)
        place = self.make_place("Театр")
        other_place = self.make_place("Музей")
        self.make_place_admin(place, admin)
        row = create_place_brick(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            code="сцена",
            title="Сцена",
            value_type="short_text",
        )
        self.assertIsNotNone(
            get_visible_brick(
                self.session,
                row.id,
                viewer_telegram_user_id=viewer.telegram_user_id,
                place_id=place.id,
            )
        )
        self.assertIsNone(
            get_visible_brick(
                self.session, row.id, viewer_telegram_user_id=viewer.telegram_user_id
            )
        )
        self.assertIsNone(
            get_visible_brick(
                self.session,
                row.id,
                viewer_telegram_user_id=viewer.telegram_user_id,
                place_id=other_place.id,
            )
        )
        in_place = list_available_bricks(
            self.session,
            viewer_telegram_user_id=viewer.telegram_user_id,
            place_id=place.id,
        )
        self.assertIn(row.id, [item.id for item in in_place])
        in_other = list_available_bricks(
            self.session,
            viewer_telegram_user_id=viewer.telegram_user_id,
            place_id=other_place.id,
        )
        self.assertNotIn(row.id, [item.id for item in in_other])
        self.assertEqual(
            [item.id for item in list_place_bricks(self.session, place.id)], [row.id]
        )
        with self.assertRaises(BrickNotFoundError):
            list_available_bricks(self.session, place_id=999_999)

    def test_search_ordering_type_filter_archive_and_foreign_isolation(self) -> None:
        admin = self.make_user(2010)
        place = self.make_place("Концертный зал")
        other = self.make_user(2011)
        self.make_place_admin(place, admin)
        place_row = create_place_brick(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            code="цена",
            title="Стоимость участия",
            value_type="number",
        )
        personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            code="цена",
            title="Моя цена",
            value_type="number",
        )
        notes = create_personal_brick(
            self.session,
            actor_telegram_user_id=admin.telegram_user_id,
            code="заметки",
            title="Личные заметки",
            value_type="long_text",
        )
        foreign = create_personal_brick(
            self.session,
            actor_telegram_user_id=other.telegram_user_id,
            code="чужая",
            title="Чужая заметка",
            value_type="long_text",
        )

        available = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
        )
        scope_order = {"system": 0, "place": 1, "personal": 2}
        keys = [(scope_order[item.scope], item.code, item.id) for item in available]
        self.assertEqual(keys, sorted(keys))
        ids = [item.id for item in available]
        self.assertIn(place_row.id, ids)
        self.assertIn(personal.id, ids)
        self.assertIn(notes.id, ids)
        self.assertNotIn(foreign.id, ids)

        by_code = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            query="ЦЕНА",
        )
        self.assertEqual([item.scope for item in by_code], ["place", "personal"])
        by_title = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            query="стоимость",
        )
        self.assertEqual([item.id for item in by_title], [place_row.id])
        numbers = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            value_type="number",
        )
        self.assertEqual({item.id for item in numbers}, {place_row.id, personal.id})

        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE bricks SET status='archived' WHERE id=:id"),
                {"id": personal.id},
            )
        self.session.expire_all()
        active = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
        )
        self.assertNotIn(personal.id, [item.id for item in active])
        archived = list_available_bricks(
            self.session,
            viewer_telegram_user_id=admin.telegram_user_id,
            place_id=place.id,
            include_archived=True,
        )
        self.assertIn(personal.id, [item.id for item in archived])

    def test_personal_and_place_listings_are_scoped(self) -> None:
        user = self.make_user(2012)
        other = self.make_user(2013)
        place = self.make_place("Площадка")
        self.make_place_admin(place, user)
        own = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="своё",
            title="Своё",
            value_type="short_text",
        )
        create_personal_brick(
            self.session,
            actor_telegram_user_id=other.telegram_user_id,
            code="чужое",
            title="Чужое",
            value_type="short_text",
        )
        place_row = create_place_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            place_id=place.id,
            code="местное",
            title="Местное",
            value_type="short_text",
        )
        self.assertEqual(
            [
                item.id
                for item in list_personal_bricks(self.session, user.telegram_user_id)
            ],
            [own.id],
        )
        self.assertEqual(
            [item.id for item in list_place_bricks(self.session, place.id)],
            [place_row.id],
        )
        with self.assertRaises(BrickNotFoundError):
            list_place_bricks(self.session, 999_999)

    def test_resolution_precedence_is_place_then_personal_and_per_viewer(self) -> None:
        first = self.make_user(2014)
        second = self.make_user(2015)
        place = self.make_place("Сцена")
        self.make_place_admin(place, first)
        first_personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=first.telegram_user_id,
            code="цена",
            title="Цена первой",
            value_type="number",
        )
        second_personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=second.telegram_user_id,
            code="цена",
            title="Цена второй",
            value_type="number",
        )
        place_row = create_place_brick(
            self.session,
            actor_telegram_user_id=first.telegram_user_id,
            place_id=place.id,
            code="цена",
            title="Цена места",
            value_type="number",
        )
        with_place = resolve_brick_by_code(
            self.session,
            "цена",
            viewer_telegram_user_id=first.telegram_user_id,
            place_id=place.id,
        )
        self.assertIsNotNone(with_place)
        assert with_place is not None
        self.assertEqual(with_place.scope, "place")
        self.assertEqual(with_place.id, place_row.id)
        personal_first = resolve_brick_by_code(
            self.session,
            "цена",
            viewer_telegram_user_id=first.telegram_user_id,
        )
        self.assertIsNotNone(personal_first)
        assert personal_first is not None
        self.assertEqual(personal_first.id, first_personal.id)
        personal_second = resolve_brick_by_code(
            self.session,
            "цена",
            viewer_telegram_user_id=second.telegram_user_id,
        )
        self.assertIsNotNone(personal_second)
        assert personal_second is not None
        self.assertEqual(personal_second.id, second_personal.id)
        second_with_place = resolve_brick_by_code(
            self.session,
            "цена",
            viewer_telegram_user_id=second.telegram_user_id,
            place_id=place.id,
        )
        self.assertIsNotNone(second_with_place)
        assert second_with_place is not None
        self.assertEqual(second_with_place.id, place_row.id)
        system = resolve_brick_by_code(self.session, "название")
        self.assertIsNotNone(system)
        assert system is not None
        self.assertEqual(system.id, 1)
        self.assertIsNone(
            resolve_brick_by_code(self.session, "цена", viewer_telegram_user_id=999_999)
        )

    def test_duplicate_human_titles_are_allowed_but_codes_are_not(self) -> None:
        first = self.make_user(2016)
        second = self.make_user(2017)
        one = create_personal_brick(
            self.session,
            actor_telegram_user_id=first.telegram_user_id,
            code="цена",
            title="Цена",
            value_type="number",
        )
        two = create_personal_brick(
            self.session,
            actor_telegram_user_id=first.telegram_user_id,
            code="стоимость",
            title="Цена",
            value_type="number",
        )
        three = create_personal_brick(
            self.session,
            actor_telegram_user_id=second.telegram_user_id,
            code="цена",
            title="Цена",
            value_type="number",
        )
        self.assertEqual(one.title, two.title)
        self.assertEqual(two.title, three.title)
        with self.assertRaises(BrickConflictError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=first.telegram_user_id,
                code="цена",
                title="Другое название",
                value_type="number",
            )

    def test_invalid_definition_does_not_insert_and_session_recovers(self) -> None:
        user = self.make_user(2018)
        before = self.count_bricks()
        with self.assertRaises(BrickValidationError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                code="a",
                title="Короткий код",
                value_type="short_text",
            )
        with self.assertRaises(BrickValidationError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                code="нормальный",
                title="   ",
                value_type="short_text",
            )
        with self.assertRaises(BrickRulesError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                code="другой",
                title="Правила",
                value_type="choice",
                rules={},
            )
        with self.assertRaises(BrickValidationError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                code="третий",
                title="Тип",
                value_type="any",
            )
        self.assertEqual(self.count_bricks(), before)
        recovered = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="после_ошибки",
            title="После ошибки",
            value_type="short_text",
        )
        self.assertEqual(recovered.code, "после_ошибки")

    def test_concurrent_same_code_creates_exactly_one_definition(self) -> None:
        user = self.make_user(2019)
        barrier = Barrier(4)

        def attempt(_: int) -> int | None:
            with self.Session() as session:
                barrier.wait()
                try:
                    row = create_personal_brick(
                        session,
                        actor_telegram_user_id=user.telegram_user_id,
                        code="гонка",
                        title="Гонка",
                        value_type="short_text",
                    )
                    return row.id
                except BrickConflictError:
                    return None

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        self.assertEqual(len([value for value in results if value is not None]), 1)
        with self.engine.connect() as connection:
            count: int = int(
                connection.execute(
                    text("SELECT count(*) FROM bricks WHERE code='гонка'")
                ).scalar_one()
            )
        self.assertEqual(count, 1)

    def test_failed_create_rolls_back_and_same_session_can_retry(self) -> None:
        user = self.make_user(2020)
        create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="занято",
            title="Занято",
            value_type="short_text",
        )
        with self.assertRaises(BrickConflictError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=user.telegram_user_id,
                code="занято",
                title="Ещё раз",
                value_type="short_text",
            )
        retried = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="свободно",
            title="Свободно",
            value_type="short_text",
        )
        self.assertEqual(retried.code, "свободно")

    def test_place_source_resolves_known_values_and_missing_fields(self) -> None:
        user = self.make_user(2021)
        place = self.make_place(
            "Дом культуры",
            created_by_user_id=user.id,
            address="улица Ленина, 1",
            url="https://example.com/place",
        )
        alternative = add_alternative_name(
            self.session, place.id, "ДК", created_by_user_id=user.id
        )
        self.assertEqual(alternative.status, "created")
        name_brick = get_system_brick(self.session, "место")
        address_brick = get_system_brick(self.session, "адрес_места")
        url_brick = get_system_brick(self.session, "ссылка_места")
        assert name_brick and address_brick and url_brick
        self.assertEqual(
            resolve_brick_value(self.session, name_brick, place_id=place.id).payload,
            {"text": "Дом культуры"},
        )
        self.assertEqual(
            resolve_brick_value(
                self.session,
                name_brick,
                place_id=place.id,
                selected_place_name_id=alternative.name_id,
            ).payload,
            {"text": "ДК"},
        )
        self.assertEqual(
            resolve_brick_value(self.session, address_brick, place_id=place.id).payload,
            {"text": "улица Ленина, 1"},
        )
        self.assertEqual(
            resolve_brick_value(self.session, url_brick, place_id=place.id).payload,
            {"url": "https://example.com/place"},
        )
        self.assertEqual(
            resolve_brick_value(self.session, name_brick).status,
            BrickValueStatus.MISSING,
        )
        missing_place = self.make_place("Без контактов")
        self.assertEqual(
            resolve_brick_value(
                self.session, address_brick, place_id=missing_place.id
            ).status,
            BrickValueStatus.MISSING,
        )
        self.assertEqual(
            resolve_brick_value(
                self.session, url_brick, place_id=missing_place.id
            ).status,
            BrickValueStatus.MISSING,
        )
        other_place = self.make_place("Другое")
        other_name = self.session.scalar(
            select(PlaceName).where(PlaceName.place_id == other_place.id)
        )
        assert other_name is not None
        self.assertEqual(
            resolve_brick_value(
                self.session,
                name_brick,
                place_id=place.id,
                selected_place_name_id=other_name.id,
            ).status,
            BrickValueStatus.INVALID,
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE places SET url='javascript:alert(1)' WHERE id=:id"),
                {"id": place.id},
            )
        self.session.expire_all()
        self.assertEqual(
            resolve_brick_value(self.session, url_brick, place_id=place.id).status,
            BrickValueStatus.INVALID,
        )

    def test_ask_and_context_sources_distinguish_all_value_states(self) -> None:
        user = self.make_user(2022)
        title = get_system_brick(self.session, "название")
        assert title is not None
        self.assertEqual(
            resolve_brick_value(self.session, title).status,
            BrickValueStatus.NEEDS_INPUT,
        )
        self.assertEqual(
            resolve_brick_value(
                self.session, title, raw_value="", allow_empty=True
            ).status,
            BrickValueStatus.EMPTY_OPTIONAL,
        )
        self.assertEqual(
            resolve_brick_value(self.session, title, raw_value="Название").status,
            BrickValueStatus.VALID,
        )
        self.assertEqual(
            resolve_brick_value(self.session, title, raw_value="").status,
            BrickValueStatus.INVALID,
        )
        context_brick = create_personal_brick(
            self.session,
            actor_telegram_user_id=user.telegram_user_id,
            code="контекст",
            title="Контекст",
            value_type="number",
            source="context",
            rules={"min": 0},
        )
        self.assertEqual(
            resolve_brick_value(self.session, context_brick).status,
            BrickValueStatus.MISSING,
        )
        self.assertEqual(
            resolve_brick_value(
                self.session, context_brick, context_values={"контекст": "10"}
            ).payload,
            {"number": 10},
        )
        self.assertEqual(
            resolve_brick_value(
                self.session, context_brick, context_values={"контекст": "abc"}
            ).status,
            BrickValueStatus.INVALID,
        )
        self.assertEqual(
            resolve_brick_value(
                self.session,
                context_brick,
                context_values={"контекст": ""},
                allow_empty=True,
            ).status,
            BrickValueStatus.EMPTY_OPTIONAL,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
