# tests/test_stage19a_schema.py
"""
Stage 19A: минимальная схема пользователей и мест и её ограничения.

Проверяются настоящие ограничения SQLite (UNIQUE/CHECK/FK/частичный
уникальный индекс), а не только проверки Python. База — временный файл.
"""

from __future__ import annotations

import os
import shutil
import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from migrations import run_migrations
from models import Draft, Place, PlaceName, User
from place_store import (
    PlaceNameError,
    clean_display_name,
    create_place,
    normalize_place_name,
)
from tests.stage19a_helpers import make_engine, table_names, temp_db_path
from user_store import get_or_create_user


class PlaceSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = temp_db_path()
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

    def _user(self, telegram_id: int = 100500, username: str | None = "anna") -> User:
        return get_or_create_user(
            self.session, telegram_id, username=username, full_name="Anna"
        )

    # --- пользователи -------------------------------------------------------

    def test_telegram_user_id_is_unique_in_database(self) -> None:
        self._user(100500)

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO users "
                        "(telegram_user_id, created_at) "
                        "VALUES (100500, CURRENT_TIMESTAMP)"
                    )
                )

        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(User)).all()), 1)

    def test_get_or_create_user_is_upsert_without_duplicates(self) -> None:
        first = self._user(100500, username="@anna")
        self.assertEqual(first.username, "anna")

        second = get_or_create_user(self.session, 100500, username=None)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.username, "anna")  # пустое значение не затёрло

        other = get_or_create_user(self.session, 200600, username="other")
        self.assertNotEqual(other.id, first.id)
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(User)).all()), 2)

    def test_invalid_telegram_ids_are_rejected(self) -> None:
        for value in (0, -1, True, "100"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    get_or_create_user(self.session, value)  # type: ignore[arg-type]

    # --- места и названия ---------------------------------------------------

    def test_cyrillic_place_gets_canonical_normalized_name(self) -> None:
        user = self._user()

        place = create_place(
            self.session,
            "Ёлка-Кафе «Тёплый Дом»",
            created_by_user_id=user.id,
        )

        self.assertIsNotNone(place.id)
        self.assertEqual(place.created_by_user_id, user.id)
        name = self.session.scalars(
            select(PlaceName).where(PlaceName.place_id == place.id)
        ).one()
        self.assertEqual(name.name, "Ёлка-Кафе «Тёплый Дом»")
        self.assertEqual(name.name_search, "елка кафе теплый дом")
        self.assertEqual(name.is_canonical, 1)

    def test_similar_but_different_places_stay_independent(self) -> None:
        create_place(self.session, "Кофейня «Дом»")
        create_place(self.session, "кофейня дом")

        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 2)
            names = session.scalars(select(PlaceName)).all()
        self.assertEqual(len(names), 2)
        self.assertEqual({name.name_search for name in names}, {"кофейня дом"})

    def test_empty_name_is_rejected_before_database(self) -> None:
        for value in ("", "   ", "«»---"):
            with self.subTest(value=value):
                with self.assertRaises(PlaceNameError):
                    create_place(self.session, value)
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 0)

    def test_database_check_rejects_empty_name_and_search(self) -> None:
        place = create_place(self.session, "Нормальное место")

        for name, search, canonical in (
            ("", "x", 0),
            ("   ", "x", 0),
            ("Место", "", 0),
            ("Место", "  ", 0),
            ("Место", "место", 2),
            ("Место", "место", -1),
        ):
            with self.subTest(name=name, search=search, canonical=canonical):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        connection.execute(
                            text(
                                "INSERT INTO place_names "
                                "(place_id, name, name_search, is_canonical, "
                                "created_at) VALUES (:place, :name, :search, "
                                ":canonical, CURRENT_TIMESTAMP)"
                            ),
                            {
                                "place": place.id,
                                "name": name,
                                "search": search,
                                "canonical": canonical,
                            },
                        )

    def test_only_one_canonical_name_per_place(self) -> None:
        place = create_place(self.session, "Театр")

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO place_names "
                        "(place_id, name, name_search, is_canonical, created_at) "
                        "VALUES (:place, 'Второе имя', 'второе имя', 1, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"place": place.id},
                )

        # Альтернативное (неканоническое) название — допустимо: частичный
        # уникальный индекс ограничивает только is_canonical=1.
        with self.Session() as session:
            session.add(
                PlaceName(
                    place_id=place.id,
                    name="Второе имя",
                    name_search="второе имя",
                    is_canonical=0,
                )
            )
            session.commit()
            names = session.scalars(
                select(PlaceName).where(PlaceName.place_id == place.id)
            ).all()
        self.assertEqual(len(names), 2)
        self.assertEqual(sum(name.is_canonical for name in names), 1)

    def test_duplicate_normalized_name_inside_place_conflicts(self) -> None:
        place = create_place(self.session, "Кофейня «Дом»")

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO place_names "
                        "(place_id, name, name_search, is_canonical, created_at) "
                        "VALUES (:place, 'КОФЕЙНЯ ДОМ', 'кофейня дом', 0, "
                        "CURRENT_TIMESTAMP)"
                    ),
                    {"place": place.id},
                )

    def test_same_normalized_name_in_different_places_is_allowed(self) -> None:
        first = create_place(self.session, "Кофейня «Дом»")
        second = create_place(self.session, "Кофейня Дом")

        with self.Session() as session:
            rows = session.scalars(
                select(PlaceName).where(PlaceName.name_search == "кофейня дом")
            ).all()
        self.assertEqual({row.place_id for row in rows}, {first.id, second.id})

    # --- внешние ключи и каскады -------------------------------------------

    def test_foreign_keys_are_enforced(self) -> None:
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("PRAGMA foreign_keys")).scalar(), 1
            )

        with self.assertRaises(IntegrityError):
            with self.engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO place_names "
                        "(place_id, name, name_search, is_canonical, created_at) "
                        "VALUES (999999, 'Сирота', 'сирота', 1, CURRENT_TIMESTAMP)"
                    )
                )

        with self.assertRaises(IntegrityError):
            create_place(
                self.session,
                "Место с несуществующим создателем",
                created_by_user_id=999999,
            )
        with self.Session() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 0)
            self.assertEqual(len(session.scalars(select(PlaceName)).all()), 0)

    def test_delete_user_sets_creator_null_and_place_delete_cascades(self) -> None:
        user = self._user()
        place = create_place(
            self.session, "Место для удаления", created_by_user_id=user.id
        )
        name = self.session.scalars(
            select(PlaceName).where(PlaceName.place_id == place.id)
        ).one()

        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM users WHERE id = :id"), {"id": user.id}
            )
        self.session.expire_all()
        refreshed = self.session.get(Place, place.id)
        if refreshed is None:  # pragma: no cover — защита от неверного состояния
            self.fail("Место исчезло после удаления пользователя")
        self.assertIsNone(refreshed.created_by_user_id)
        refreshed_name = self.session.get(PlaceName, name.id)
        if refreshed_name is None:  # pragma: no cover — защита от неверного состояния
            self.fail("Название исчезло после удаления пользователя")
        self.assertIsNone(refreshed_name.created_by_user_id)

        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM places WHERE id = :id"), {"id": place.id}
            )
        with self.Session() as session:
            self.assertEqual(
                len(
                    session.scalars(
                        select(PlaceName).where(PlaceName.place_id == place.id)
                    ).all()
                ),
                0,
            )

    # --- нормализация и Unicode --------------------------------------------

    def test_normalization_is_unicode_and_case_insensitive(self) -> None:
        self.assertEqual(normalize_place_name("Й"), normalize_place_name("И\u0306"))
        self.assertEqual(normalize_place_name("Ёлка"), normalize_place_name("елка"))
        self.assertEqual(normalize_place_name("Ａ"), normalize_place_name("a"))
        self.assertEqual(clean_display_name("  Кофейня\t«Дом»  "), "Кофейня «Дом»")

    # --- конкурентные операции ---------------------------------------------

    def test_concurrent_user_upsert_creates_single_row(self) -> None:
        def upsert(_: int) -> int:
            with self.Session() as session:
                user = get_or_create_user(session, 777000, username="racer")
                return user.id

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(upsert, range(8)))

        self.assertEqual(len(set(ids)), 1)
        with self.Session() as session:
            users = session.scalars(
                select(User).where(User.telegram_user_id == 777000)
            ).all()
        self.assertEqual(len(users), 1)

    def test_concurrent_place_creation_keeps_all_rows(self) -> None:
        def create(_: int) -> int:
            with self.Session() as session:
                place = create_place(session, "Одновременное место")
                return place.id

        with ThreadPoolExecutor(max_workers=6) as pool:
            place_ids = list(pool.map(create, range(6)))

        self.assertEqual(len(set(place_ids)), 6)
        with self.Session() as session:
            places = session.scalars(select(Place)).all()
            canonical = session.scalars(
                select(PlaceName).where(PlaceName.is_canonical == 1)
            ).all()
        self.assertEqual(len(places), 6)
        self.assertEqual(len(canonical), 6)

    # --- границы Stage 19A ---------------------------------------------------

    def test_no_future_tables_and_photos_stay_draft_data(self) -> None:
        from base import Base

        Base.metadata.create_all(bind=self.engine)
        tables = set(Base.metadata.tables)
        forbidden = {
            "bricks",
            "templates",
            "template_versions",
            "draft_values",
            "brick_proposals",
        }
        self.assertFalse(forbidden & tables)
        self.assertFalse({name for name in table_names(self.path) if "brick" in name})
        self.assertIn("photos", Draft.__table__.columns)
        self.assertFalse(
            any(
                column_name.startswith("brick")
                for column_name in Draft.__table__.columns.keys()
            )
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
