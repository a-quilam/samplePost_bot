# tests/test_stage19b_store.py
"""
Stage 19B: слой справочника мест — поиск, карточка и альтернативные названия.

Проверяется настоящая SQLite (временный файл): уникальность внутри места,
конкурентная вставка, отсутствие неявных слияний и ранжирование результатов.
"""

from __future__ import annotations

import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from migrations import run_migrations
from models import Place, PlaceName
from place_store import (
    MAX_PLACE_NAME_LENGTH,
    PlaceNameError,
    add_alternative_name,
    create_place,
    find_name_conflicts,
    get_place_details,
    list_places,
    normalize_place_name,
    place_name_similarity,
    search_places,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user


class PlaceSearchStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-19b-store-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.user = get_or_create_user(
            self.session, 700001, username="tester", full_name="Тест"
        )

    def _create(self, name: str, *, address: str | None = None, url: str | None = None):
        return create_place(
            self.session,
            name,
            created_by_user_id=self.user.id,
            address=address,
            url=url,
        )

    def _names(self, place_id: int) -> list[PlaceName]:
        return list(
            self.session.scalars(
                select(PlaceName)
                .where(PlaceName.place_id == place_id)
                .order_by(PlaceName.id)
            ).all()
        )

    def test_list_places_is_paginated_and_has_canonical_name(self) -> None:
        created = [self._create(f"Место {index:03d}") for index in range(25)]

        first, total = list_places(self.session, offset=0, limit=8)
        second, second_total = list_places(self.session, offset=8, limit=8)
        last, _ = list_places(self.session, offset=24, limit=8)

        self.assertEqual(total, 25)
        self.assertEqual(second_total, 25)
        self.assertEqual(len(first), 8)
        self.assertEqual(len(second), 8)
        self.assertEqual(len(last), 1)
        self.assertEqual(first[0].place_id, created[0].id)
        self.assertEqual(first[0].canonical_name, "Место 000")
        self.assertEqual(
            {item.place_id for item in first} & {item.place_id for item in second},
            set(),
        )

    def test_exact_canonical_alias_partial_and_similar_ranking(self) -> None:
        house = self._create("Кофейня «Дом»")
        add_alternative_name(
            self.session,
            house.id,
            "Дом у моря",
            created_by_user_id=self.user.id,
        )
        tea = self._create("Кофейня Дома")
        theatre = self._create("Театр")

        exact = search_places(self.session, "кофейня дом")
        self.assertEqual(exact[0].place_id, house.id)
        self.assertEqual(exact[0].category, "canonical_exact")

        alias = search_places(self.session, "ДОМ У МОРЯ")
        self.assertEqual(alias[0].place_id, house.id)
        self.assertEqual(alias[0].category, "alias_exact")
        self.assertEqual(alias[0].matched_name, "Дом у моря")

        partial = search_places(self.session, "коф")
        self.assertEqual({item.place_id for item in partial}, {house.id, tea.id})
        self.assertTrue(
            all(item.category.endswith("_partial") for item in partial), partial
        )

        similar = search_places(self.session, "Кофейня Дома")
        # «Кофейня Дома» — точное название второго места, а не только похожее
        self.assertEqual(similar[0].place_id, tea.id)
        self.assertEqual(similar[0].category, "canonical_exact")

        fuzzy = search_places(self.session, "Кофейня Доммм")
        self.assertEqual(fuzzy[0].place_id, house.id)
        self.assertEqual(fuzzy[0].category, "similar")
        self.assertGreaterEqual(
            place_name_similarity("кофейня доммм", "кофейня дом"), 0.8
        )
        self.assertNotIn(theatre.id, {item.place_id for item in fuzzy})

    def test_normalization_covers_case_yo_spaces_punctuation_and_unicode(self) -> None:
        place = self._create("Ёлка-Кафе «Тёплый Дом»")

        for query in (
            "ёлка кафе тёплый дом",
            "ЕЛКА—КАФЕ, ТЕПЛЫЙ   ДОМ",
            "  елка-кафе тёплый—дом ",
        ):
            with self.subTest(query=query):
                results = search_places(self.session, query)
                self.assertEqual(results[0].place_id, place.id)
                self.assertEqual(results[0].category, "canonical_exact")

    def test_alternative_name_search_does_not_merge_places(self) -> None:
        first = self._create("Клуб «Север»")
        second = self._create("Бар «Север»")
        add_alternative_name(
            self.session,
            second.id,
            "Северный клуб",
            created_by_user_id=self.user.id,
        )

        results = search_places(self.session, "север")
        ids = {item.place_id for item in results}
        self.assertEqual(ids, {first.id, second.id})
        self.assertEqual(len(results), 2)
        # Поиск не создаёт новых мест и не меняет число строк.
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 2)

    def test_two_places_with_same_normalized_name_are_both_returned(self) -> None:
        first = self._create("Кофейня Дом")
        second = self._create("кофейня дом")

        results = search_places(self.session, "кофейня дом")
        self.assertEqual([item.place_id for item in results], [first.id, second.id])
        self.assertTrue(all(item.category == "canonical_exact" for item in results))

    def test_place_details_exposes_optional_contacts_and_names(self) -> None:
        place = self._create(
            "Театр",
            address="ул. Садовая, 1",
            url="https://example.com/theatre",
        )
        add_alternative_name(
            self.session, place.id, "Драмтеатр", created_by_user_id=self.user.id
        )

        details = get_place_details(self.session, place.id)
        self.assertIsNotNone(details)
        assert details is not None
        self.assertEqual(details.canonical_name, "Театр")
        self.assertEqual(details.address, "ул. Садовая, 1")
        self.assertEqual(details.url, "https://example.com/theatre")
        self.assertEqual([item.name for item in details.names], ["Театр", "Драмтеатр"])
        self.assertEqual(sum(item.is_canonical for item in details.names), 1)

        bare = self._create("Без контактов")
        bare_details = get_place_details(self.session, bare.id)
        assert bare_details is not None
        self.assertIsNone(bare_details.address)
        self.assertIsNone(bare_details.url)

    def test_missing_place_returns_none_and_conflicts_ignore_target_place(self) -> None:
        first = self._create("Кофейня Дом")
        second = self._create("Кофейня Дома")

        self.assertIsNone(get_place_details(self.session, 999999))
        conflicts = find_name_conflicts(
            self.session, "Кофейня Дома", exclude_place_id=second.id
        )
        self.assertEqual([item.place_id for item in conflicts], [first.id])
        self.assertFalse(conflicts[0].exact)

    def test_empty_and_too_long_names_are_rejected_explicitly(self) -> None:
        for value in ("", "   ", "«»---", "😀😀", "+++"):
            with self.subTest(value=value):
                with self.assertRaises(PlaceNameError):
                    self._create(value)

        too_long = "Я" * (MAX_PLACE_NAME_LENGTH + 1)
        with self.assertRaises(PlaceNameError) as caught:
            self._create(too_long)
        self.assertIn(str(MAX_PLACE_NAME_LENGTH), str(caught.exception))

        place = self._create("Нормальное место")
        with self.assertRaises(PlaceNameError):
            add_alternative_name(
                self.session,
                place.id,
                too_long,
                created_by_user_id=self.user.id,
            )
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 1)
            self.assertEqual(len(session.scalars(select(PlaceName)).all()), 1)

    def test_create_place_and_canonical_name_are_atomic(self) -> None:
        with self.assertRaises(Exception):
            create_place(
                self.session,
                "Атомарное место",
                created_by_user_id=999999,  # нет такого users.id → FK
            )
        self.session.rollback()
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 0)
            self.assertEqual(len(session.scalars(select(PlaceName)).all()), 0)

    def test_similarity_threshold_is_stable(self) -> None:
        self.assertGreaterEqual(
            place_name_similarity("кофейня дом", "кофейня дома"), 0.8
        )
        self.assertGreaterEqual(place_name_similarity("театр", "театрр"), 0.8)
        self.assertLess(place_name_similarity("кофейня дом", "автосервис"), 0.8)


class AlternativeNameStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path("poster-19b-alt-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.user = get_or_create_user(self.session, 700002, username="aliaser")

    def _place(self, name: str) -> Place:
        return create_place(self.session, name, created_by_user_id=self.user.id)

    def _name_count(self, place_id: int) -> int:
        return len(
            self.session.scalars(
                select(PlaceName).where(PlaceName.place_id == place_id)
            ).all()
        )

    def test_alias_is_added_immediately_and_visibility_is_shared(self) -> None:
        place = self._place("Театр")

        result = add_alternative_name(
            self.session, place.id, "  Драм-Театр  ", created_by_user_id=self.user.id
        )

        self.assertEqual(result.status, "created")
        self.assertIsNotNone(result.name_id)
        details = get_place_details(self.session, place.id)
        assert details is not None
        self.assertEqual([item.name for item in details.names], ["Театр", "Драм-Театр"])
        self.assertEqual(details.names[1].is_canonical, False)

    def test_duplicate_inside_one_place_is_rejected(self) -> None:
        place = self._place("Кофейня «Дом»")
        first = add_alternative_name(
            self.session, place.id, "Кофейня Дом", created_by_user_id=self.user.id
        )
        self.assertEqual(first.status, "duplicate")
        self.assertEqual(self._name_count(place.id), 1)

        second = add_alternative_name(
            self.session, place.id, "КОФЕЙНЯ-ДОМ", created_by_user_id=self.user.id
        )
        self.assertEqual(second.status, "duplicate")
        self.assertEqual(self._name_count(place.id), 1)

    def test_exact_conflict_with_other_place_requires_explicit_force(self) -> None:
        first = self._place("Клуб")
        other = self._place("Клуб Север")

        blocked = add_alternative_name(
            self.session, first.id, "Клуб Север", created_by_user_id=self.user.id
        )
        self.assertEqual(blocked.status, "conflict")
        self.assertEqual(
            [conflict.place_id for conflict in blocked.conflicts], [other.id]
        )
        self.assertEqual(self._name_count(first.id), 1)

        forced = add_alternative_name(
            self.session,
            first.id,
            "Клуб Север",
            created_by_user_id=self.user.id,
            force=True,
        )
        self.assertEqual(forced.status, "created")
        self.assertEqual(self._name_count(first.id), 2)
        # Второе место не изменилось и не слилось с первым.
        self.assertEqual(self._name_count(other.id), 1)

    def test_very_similar_name_of_other_place_warns(self) -> None:
        first = self._place("Кофейня Дом")
        other = self._place("Кофейня Дома")

        result = add_alternative_name(
            self.session, first.id, "Кофейня Домаа", created_by_user_id=self.user.id
        )

        self.assertEqual(result.status, "conflict")
        self.assertEqual([item.place_id for item in result.conflicts], [other.id])
        self.assertFalse(result.conflicts[0].exact)
        self.assertEqual(self._name_count(first.id), 1)

    def test_concurrent_same_alias_creates_exactly_one_row(self) -> None:
        place = self._place("Конкурентное место")

        def add(_: int) -> tuple[str, int | None]:
            with self.Session() as session:
                result = add_alternative_name(
                    session,
                    place.id,
                    "Один и тот же вариант",
                    created_by_user_id=self.user.id,
                )
                return result.status, result.name_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(add, range(8)))

        self.assertEqual(sum(1 for status, _ in outcomes if status == "created"), 1)
        self.assertEqual(sum(1 for status, _ in outcomes if status == "duplicate"), 7)
        with self.Session() as session:
            rows = session.scalars(
                select(PlaceName).where(
                    PlaceName.place_id == place.id,
                    PlaceName.name_search
                    == normalize_place_name("Один и тот же вариант"),
                )
            ).all()
        self.assertEqual(len(rows), 1)

    def test_missing_place_is_reported_without_orphan_row(self) -> None:
        result = add_alternative_name(
            self.session, 999999, "Сирота", created_by_user_id=self.user.id
        )
        self.assertEqual(result.status, "place_missing")
        with self.Session() as session:
            self.assertEqual(
                len(
                    session.scalars(
                        select(PlaceName).where(PlaceName.name_search == "сирота")
                    ).all()
                ),
                0,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
