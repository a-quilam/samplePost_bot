# tests/test_poster2_u1a_place_city.py
"""
U1a — город площадки: создание, редактирование, списки и поиск.

Проверяется:

- `create_place` без города / с пустым городом / городом длиннее лимита —
  явная ошибка валидации, место не появляется;
- город нормализуется (NFKC, сжатые пробелы) и сохраняется в `places.city`;
- `set_place_city`/`delete_place_city` меняют поле и пишут ровно одну
  строку аудита `city_set`/`city_delete` с прежним и новым значением;
  устаревшее ожидаемое значение не перезаписывает чужую правку;
- `list_places`/`search_places` показывают «Название · Город», у старых
  мест без города — только название; совпадение по городу — отдельная
  категория результата;
- два места с одинаковым названием в разных городах различимы.

Реальная `post_bot.db`, сеть и Telegram не используются: каждый тест
поднимает временный SQLite-файл и применяет миграции (1–22).
"""

from __future__ import annotations

import os
import shutil
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

import handlers.places as places_handlers
from migrations import run_migrations
from models import Place, PlaceEditLog, PlaceName
from place_edit_store import (
    PlaceEditPermissionError,
    delete_place_city,
    set_place_city,
)
from place_store import (
    MAX_PLACE_CITY_LENGTH,
    PlaceNameError,
    create_place,
    get_place_details,
    list_places,
    search_places,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user

SUPERADMIN_TG = 9_842_001
OTHER_USER_TG = 9_842_002


class PlaceCityTestCase(unittest.TestCase):
    """Временная база с суперадмином и без единого места."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-u1a-city-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)
        self.superadmin = get_or_create_user(
            self.session, SUPERADMIN_TG, username="city-admin"
        )
        self.other = get_or_create_user(
            self.session, OTHER_USER_TG, username="city-other"
        )

    # --- помощники --------------------------------------------------------

    def make_place(
        self,
        name: str,
        *,
        city: str = "Москва",
        address: str | None = None,
        url: str | None = None,
    ) -> Place:
        return create_place(
            self.session,
            name,
            city=city,
            created_by_user_id=self.superadmin.id,
            address=address,
            url=url,
        )

    def place_count(self) -> int:
        return len(self.session.scalars(select(Place.id)).all())

    def city_of(self, place_id: int) -> str | None:
        return self.session.get(Place, place_id).city  # type: ignore[union-attr]

    def log_rows(self) -> list[PlaceEditLog]:
        return list(
            self.session.scalars(select(PlaceEditLog).order_by(PlaceEditLog.id)).all()
        )


class CreatePlaceCityValidationTests(PlaceCityTestCase):
    """Город обязателен и ограничен по длине уже на входе сервиса."""

    def test_missing_city_is_a_validation_error(self) -> None:
        with self.assertRaises(TypeError):
            create_place(  # type: ignore[call-arg]
                self.session,
                "Дом культуры",
                created_by_user_id=self.superadmin.id,
            )
        self.assertEqual(self.session.scalars(select(Place.id)).all(), [])

    def test_empty_city_is_rejected(self) -> None:
        for raw in ("", "   ", "\u00a0\t\n"):
            with self.subTest(city=repr(raw)):
                with self.assertRaises(PlaceNameError):
                    self.make_place("Дом культуры", city=raw)
        self.assertEqual(self.session.scalars(select(Place.id)).all(), [])
        self.assertEqual(self.session.scalars(select(PlaceName.id)).all(), [])

    def test_city_without_letters_is_rejected(self) -> None:
        with self.assertRaises(PlaceNameError):
            self.make_place("Дом культуры", city="— 🏙 —")

    def test_city_longer_than_limit_is_rejected_not_truncated(self) -> None:
        too_long = "я" * (MAX_PLACE_CITY_LENGTH + 1)
        with self.assertRaises(PlaceNameError) as raised:
            self.make_place("Дом культуры", city=too_long)
        self.assertIn(str(MAX_PLACE_CITY_LENGTH), str(raised.exception))
        self.assertEqual(self.session.scalars(select(Place.id)).all(), [])

        exact = self.make_place("Дом культуры", city="я" * MAX_PLACE_CITY_LENGTH)
        self.assertEqual(self.city_of(exact.id), "я" * MAX_PLACE_CITY_LENGTH)

    def test_city_is_normalized_and_stored(self) -> None:
        # NFKC превращает NBSP в обычный пробел, затем пробелы сжимаются.
        place = self.make_place("Дом культуры", city=" \u00a0Москва\u00a0  ")
        self.assertEqual(self.city_of(place.id), "Москва")
        details = get_place_details(self.session, place.id)
        assert details is not None
        self.assertEqual(details.city, "Москва")


class PlaceCityEditingTests(PlaceCityTestCase):
    """`set_place_city`/`delete_place_city` и аудит правок."""

    def setUp(self) -> None:
        super().setUp()
        self.place = self.make_place("Дом культуры", city="Москва")

    def test_set_city_updates_value_and_writes_single_audit_row(self) -> None:
        result = set_place_city(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            city="  Санкт-Петербург ",
            expected_city="Москва",
        )
        self.assertEqual(result.status, "ok")
        self.session.commit()
        self.assertEqual(self.city_of(self.place.id), "Санкт-Петербург")
        rows = self.log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "city_set")
        self.assertEqual(rows[0].place_id, self.place.id)
        self.assertEqual(rows[0].old_value, "Москва")
        self.assertEqual(rows[0].new_value, "Санкт-Петербург")
        self.assertEqual(rows[0].actor_user_id, self.superadmin.id)

    def test_delete_city_sets_null_and_writes_city_delete(self) -> None:
        result = delete_place_city(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            expected_city="Москва",
        )
        self.assertEqual(result.status, "ok")
        self.session.commit()
        self.assertIsNone(self.city_of(self.place.id))
        self.assertEqual(
            [(row.action, row.old_value, row.new_value) for row in self.log_rows()],
            [("city_delete", "Москва", None)],
        )
        details = get_place_details(self.session, self.place.id)
        assert details is not None
        self.assertIsNone(details.city)

    def test_stale_expected_city_does_not_overwrite(self) -> None:
        set_place_city(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            city="Казань",
            expected_city="Москва",
        )
        self.session.commit()
        result = set_place_city(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            city="Тверь",
            expected_city="Москва",
        )
        self.assertEqual(result.status, "changed")
        self.assertEqual(result.value, "Казань")
        self.session.commit()
        self.assertEqual(self.city_of(self.place.id), "Казань")
        self.assertEqual(
            [row.action for row in self.log_rows()],
            ["city_set"],
            "вторая правка не записана",
        )

    def test_unchanged_city_writes_no_audit_row(self) -> None:
        result = set_place_city(
            self.session,
            actor_telegram_user_id=SUPERADMIN_TG,
            place_id=self.place.id,
            city=" Москва ",
            expected_city="Москва",
        )
        self.assertEqual(result.status, "unchanged")
        self.session.commit()
        self.assertEqual(self.log_rows(), [])

    def test_empty_city_is_rejected_by_edit_store(self) -> None:
        with self.assertRaises(PlaceNameError):
            set_place_city(
                self.session,
                actor_telegram_user_id=SUPERADMIN_TG,
                place_id=self.place.id,
                city="   ",
                expected_city="Москва",
            )
        self.session.rollback()
        self.assertEqual(self.city_of(self.place.id), "Москва")
        self.assertEqual(self.log_rows(), [])

    def test_foreign_user_cannot_change_city(self) -> None:
        with self.assertRaises(PlaceEditPermissionError):
            set_place_city(
                self.session,
                actor_telegram_user_id=OTHER_USER_TG,
                place_id=self.place.id,
                city="Тверь",
                expected_city="Москва",
            )
        self.session.rollback()
        self.assertEqual(self.city_of(self.place.id), "Москва")


class PlaceCityListsAndSearchTests(PlaceCityTestCase):
    """Списки и поиск: «Название · Город», NULL-город, отдельная категория."""

    def setUp(self) -> None:
        super().setUp()
        self.moscow = self.make_place("Дом культуры", city="Москва")
        self.tver = self.make_place("Дом культуры", city="Тверь")
        self.old = self.make_place("Старый клуб", city="Москва")
        # Старая запись без города: не backfill, city = NULL.
        self.session.execute(
            Place.__table__.update().where(Place.id == self.old.id).values(city=None)
        )
        self.session.commit()

    def _list_item(self, place_id: int):
        items, _ = list_places(self.session, offset=0, limit=100)
        return next(item for item in items if item.place_id == place_id)

    def test_list_shows_city_and_keeps_null_city_as_name_only(self) -> None:
        self.assertEqual(self._list_item(self.moscow.id).city, "Москва")
        self.assertIsNone(self._list_item(self.old.id).city)
        # Тот же формат, что печатает экран «Все места».
        self.assertEqual(
            places_handlers._place_title("Дом культуры", "Москва"),
            "Дом культуры · Москва",
        )
        self.assertEqual(
            places_handlers._place_title("Старый клуб", None), "Старый клуб"
        )
        self.assertEqual(
            places_handlers._place_title("Старый клуб", "   "), "Старый клуб"
        )

        text, _ = places_handlers._all_places_content(self.session, {})
        self.assertIn("Дом культуры · Москва", text)
        self.assertIn("Дом культуры · Тверь", text)
        self.assertIn("Старый клуб\n", text)
        self.assertNotIn("Старый клуб ·", text)

    def test_search_by_city_is_a_separate_category(self) -> None:
        results = search_places(self.session, "Тверь")
        self.assertEqual([item.place_id for item in results], [self.tver.id])
        self.assertEqual(results[0].category, "city")
        self.assertEqual(results[0].city, "Тверь")
        self.assertEqual(results[0].canonical_name, "Дом культуры")
        text, _ = places_handlers._search_results_content(
            self.session, {"query": "Тверь"}
        )
        self.assertIn("Дом культуры · Тверь", text)
        self.assertIn("совпадение по городу: «Тверь»", text)

    def test_name_match_outranks_city_match(self) -> None:
        results = search_places(self.session, "Дом культуры")
        self.assertEqual(
            {item.place_id for item in results}, {self.moscow.id, self.tver.id}
        )
        self.assertTrue(
            all(item.category == "canonical_exact" for item in results),
            "точное название важнее совпадения по городу",
        )

    def test_same_name_in_different_cities_is_distinguishable(self) -> None:
        items = [self._list_item(self.moscow.id), self._list_item(self.tver.id)]
        self.assertEqual(
            [item.canonical_name for item in items],
            ["Дом культуры", "Дом культуры"],
        )
        self.assertEqual(
            [
                places_handlers._place_title(item.canonical_name, item.city)
                for item in items
            ],
            ["Дом культуры · Москва", "Дом культуры · Тверь"],
        )
        text, _ = places_handlers._all_places_content(self.session, {})
        self.assertEqual(text.count("Дом культуры · "), 2)

    def test_empty_search_query_is_rejected_without_results(self) -> None:
        for query in ("", "   "):
            with self.subTest(query=query):
                with self.assertRaises(PlaceNameError):
                    search_places(self.session, query)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
