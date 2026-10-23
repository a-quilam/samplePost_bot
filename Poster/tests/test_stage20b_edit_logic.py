# tests/test_stage20b_edit_logic.py
"""
Stage 20B: единица-логика административного редактирования места.

Проверяется настоящая SQLite во временных файлах: строгая валидация
адреса и ссылки, защита прав (только админ этого места или суперадмин),
атомарность операций и строка аудита `place_edit_log` в той же транзакции.
Настоящая post_bot.db и Telegram не используются.
"""

from __future__ import annotations

import os
import shutil
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from migrations import run_migrations
from models import PlaceEditLog, PlaceName, User
from place_edit_store import (
    PlaceEditActorError,
    PlaceEditPermissionError,
    PlaceEditValueError,
    clean_address_text,
    delete_place_address,
    delete_place_alias,
    delete_place_url,
    list_place_aliases,
    normalize_place_url,
    rename_canonical_place_name,
    set_place_address,
    set_place_url,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from user_store import get_or_create_user


class EditFixtureBase(unittest.TestCase):
    """
    Общий каркас: временная база, место, суперадмин и админ места.

    Роль суперадминистратора патчится на весь тест: слой store обязан
    перепроверять конфигурацию при каждом вызове, а тест проверяет как
    доступ, так и явные отказы.
    """

    SUPERADMIN_TG = 900100
    PLACE_ADMIN_TG = 900200
    OTHER_ADMIN_TG = 900210
    ORDINARY_TG = 900300

    def setUp(self) -> None:
        self._config_patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (self.SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._config_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.path = temp_db_path("poster-20b-logic-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session: Session = self.Session()
        self.addCleanup(self.session.close)

        self.superadmin = self._user(
            self.SUPERADMIN_TG, username="boss", full_name="Супер Админ"
        )
        self.place_admin = self._user(
            self.PLACE_ADMIN_TG, username="local", full_name="Местный Админ"
        )
        self.other_admin = self._user(
            self.OTHER_ADMIN_TG, username="other", full_name="Чужой Админ"
        )
        self.ordinary = self._user(
            self.ORDINARY_TG, username="guest", full_name="Обычный Гость"
        )
        self.place = self._place("Кофейня «Локал»")
        from place_admin_store import assign_place_admin

        result = assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            target_telegram_user_id=self.PLACE_ADMIN_TG,
        )
        self.assertEqual(result.status, "assigned")

    def _user(
        self, telegram_user_id: int, *, username: str | None, full_name: str | None
    ) -> User:
        return get_or_create_user(
            self.session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )

    def _place(self, name: str):
        from place_store import create_place

        return create_place(self.session, name, created_by_user_id=self.ordinary.id)

    def _log_rows(self) -> list[PlaceEditLog]:
        return list(
            self.session.scalars(select(PlaceEditLog).order_by(PlaceEditLog.id)).all()
        )

    def _deny_config(self):
        """Открыто отключает роль суперадминистратора внутри теста."""
        return patch("config.SUPERADMIN_USER_IDS", ())


class AddressValidationTests(unittest.TestCase):
    """Валидация и нормализация адреса места."""

    def test_address_normalizes_and_trims(self) -> None:
        self.assertEqual(clean_address_text("  ул.\u00a0Ленина,  5  "), "ул. Ленина, 5")

    def test_address_rejects_empty_and_long(self) -> None:
        with self.assertRaises(PlaceEditValueError):
            clean_address_text("   ")
        with self.assertRaises(PlaceEditValueError):
            clean_address_text("x" * 501)
        cleaned = clean_address_text("x" * 500)
        self.assertEqual(len(cleaned), 500)


class UrlValidationTests(unittest.TestCase):
    """Валидация ссылки места: http(s), домены и опасные схемы."""

    def test_url_adds_https_and_keeps_path(self) -> None:
        self.assertEqual(normalize_place_url(" t.me/poster "), "https://t.me/poster")
        self.assertEqual(
            normalize_place_url("example.com/path?x=1"),
            "https://example.com/path?x=1",
        )

    def test_url_keeps_http_and_https(self) -> None:
        self.assertEqual(
            normalize_place_url("http://example.com"), "http://example.com"
        )
        # Регистр схемы и хоста сохраняется как введено: только отсутствующая
        # схема дополняется https://, а не наоборот.
        self.assertEqual(
            normalize_place_url("HTTPS://Example.COM/Path"),
            "HTTPS://Example.COM/Path",
        )

    def test_url_rejects_bad_schemes_and_hosts(self) -> None:
        bad_values = (
            "javascript:alert(1)",
            "file:///etc/passwd",
            "data:text/html,x",
            "ftp://example.com",
            "example_com/path",
            "",
            "   ",
        )
        for value in bad_values:
            with self.assertRaises(PlaceEditValueError, msg=value):
                normalize_place_url(value)

    def test_url_rejects_whitespace_inside(self) -> None:
        with self.assertRaises(PlaceEditValueError):
            normalize_place_url("example.com/path with space")

    def test_url_rejects_overlong(self) -> None:
        with self.assertRaises(PlaceEditValueError):
            normalize_place_url("https://" + "x" * 495 + ".com")


class EditPermissionTests(EditFixtureBase):
    """Права: только суперадмин или админ этого места."""

    def test_superadmin_can_edit(self) -> None:
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Новая, 1",
            expected_address=None,
        )
        self.assertEqual(result.status, "ok")

    def test_place_admin_can_edit_own_place(self) -> None:
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            address="ул. Новая, 1",
            expected_address=None,
        )
        self.assertEqual(result.status, "ok")

    def test_other_place_admin_cannot_edit(self) -> None:
        other_place = self._place("Бар «Другой»")
        from place_admin_store import assign_place_admin

        assign_place_admin(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=other_place.id,
            target_telegram_user_id=self.OTHER_ADMIN_TG,
        )
        with self.assertRaises(PlaceEditPermissionError):
            set_place_address(
                self.session,
                actor_telegram_user_id=self.OTHER_ADMIN_TG,
                place_id=self.place.id,
                address="ул. Чужая, 1",
                expected_address=None,
            )

    def test_ordinary_user_cannot_edit(self) -> None:
        with self.assertRaises(PlaceEditPermissionError):
            set_place_address(
                self.session,
                actor_telegram_user_id=self.ORDINARY_TG,
                place_id=self.place.id,
                address="ул. Обычная, 1",
                expected_address=None,
            )

    def test_permission_survives_missing_actor_row(self) -> None:
        # Суперадмин получает право из конфигурации (не из БД), но актор без
        # строки в `users` не может быть записан в журнал аудита — операция
        # обязана отказать с отдельным типом ошибки PlaceEditActorError.
        self.session.execute(
            User.__table__.delete().where(User.telegram_user_id == self.SUPERADMIN_TG)
        )
        self.session.commit()
        with self.assertRaises(PlaceEditActorError):
            set_place_address(
                self.session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                place_id=self.place.id,
                address="ул. Призрачная, 1",
                expected_address=None,
            )


class AddressEditTests(EditFixtureBase):
    """Атомарная установка и удаление адреса + аудит."""

    def test_set_address_writes_audit(self) -> None:
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            address="  ул. Ленина,   5  ",
            expected_address=None,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, "ул. Ленина, 5")
        self.session.expire_all()
        self.assertEqual(self.place.address, "ул. Ленина, 5")
        rows = self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].place_id, self.place.id)
        self.assertEqual(rows[0].action, "address_set")
        self.assertIsNone(rows[0].old_value)
        self.assertEqual(rows[0].new_value, "ул. Ленина, 5")

    def test_set_address_with_wrong_expected_is_rejected(self) -> None:
        set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Первая, 1",
            expected_address=None,
        )
        self._log_rows()  # фиксируем, что первая запись есть

        # Другой админ изменил адрес, а этот работал со старым снимком.
        set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Вторая, 2",
            expected_address="ул. Первая, 1",
        )
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            address="ул. Третья, 3",
            expected_address="ул. Первая, 1",
        )
        self.assertEqual(result.status, "changed")
        self.assertEqual(result.value, "ул. Вторая, 2")
        self.session.expire_all()
        self.assertEqual(self.place.address, "ул. Вторая, 2")
        self.assertEqual(len(self._log_rows()), 2)

    def test_set_address_unchanged_writes_nothing(self) -> None:
        set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Первая, 1",
            expected_address=None,
        )
        result = set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Первая, 1",
            expected_address="ул. Первая, 1",
        )
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(len(self._log_rows()), 1)

    def test_delete_address(self) -> None:
        set_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            address="ул. Первая, 1",
            expected_address=None,
        )
        result = delete_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_address="ул. Первая, 1",
        )
        self.assertEqual(result.status, "ok")
        self.assertIsNone(result.value)
        self.session.expire_all()
        self.assertIsNone(self.place.address)
        rows = self._log_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1].action, "address_delete")
        self.assertEqual(rows[-1].old_value, "ул. Первая, 1")
        self.assertIsNone(rows[-1].new_value)

    def test_delete_missing_address_is_rejected(self) -> None:
        result = delete_place_address(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_address=None,
        )
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(len(self._log_rows()), 0)


class UrlEditTests(EditFixtureBase):
    """Атомарная установка и удаление ссылки + аудит."""

    def test_set_url_normalizes_and_writes_audit(self) -> None:
        result = set_place_url(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            url=" t.me/poster ",
            expected_url=None,
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, "https://t.me/poster")
        self.session.expire_all()
        self.assertEqual(self.place.url, "https://t.me/poster")
        rows = self._log_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].action, "url_set")
        self.assertEqual(rows[0].new_value, "https://t.me/poster")

    def test_set_url_rejects_invalid_value(self) -> None:
        with self.assertRaises(PlaceEditValueError):
            set_place_url(
                self.session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                place_id=self.place.id,
                url="javascript:alert(1)",
                expected_url=None,
            )
        self.assertEqual(self._log_rows(), [])
        self.session.expire_all()
        self.assertIsNone(self.place.url)

    def test_delete_url(self) -> None:
        set_place_url(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            url="https://t.me/poster",
            expected_url=None,
        )
        result = delete_place_url(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            place_id=self.place.id,
            expected_url="https://t.me/poster",
        )
        self.assertEqual(result.status, "ok")
        self.session.expire_all()
        self.assertIsNone(self.place.url)
        rows = self._log_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1].action, "url_delete")


class AliasLayerTests(EditFixtureBase):
    """Альтернативные названия: удаление, список и защита канонического."""

    def test_add_and_list_aliases(self) -> None:
        from place_edit_store import add_place_alias

        result = add_place_alias(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            name="Локал",
            force=False,
        )
        self.assertEqual(result.status, "ok")
        rows = list_place_aliases(self.session, self.place.id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Локал")
        self.assertFalse(rows[0].is_canonical)

    def test_delete_alias_with_wrong_expected(self) -> None:
        from place_edit_store import add_place_alias

        added = add_place_alias(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            name="Локал",
            force=False,
        )
        self.assertEqual(added.status, "ok")
        alias = list_place_aliases(self.session, self.place.id)[0]
        result = delete_place_alias(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            name_id=alias.name_id,
            expected_name="Старое название",
        )
        self.assertEqual(result.status, "changed")

    def test_delete_canonical_is_rejected(self) -> None:
        canonical = self.session.scalar(
            select(PlaceName).where(
                PlaceName.place_id == self.place.id,
                PlaceName.is_canonical == 1,
            )
        )
        assert canonical is not None  # каноническое название создано в setUp
        result = delete_place_alias(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            name_id=canonical.id,
            expected_name=canonical.name,
        )
        self.assertEqual(result.status, "canonical")

    def test_canonical_rename_keeps_old_as_alias(self) -> None:
        result = rename_canonical_place_name(
            self.session,
            actor_telegram_user_id=self.PLACE_ADMIN_TG,
            place_id=self.place.id,
            new_name="Кафе Локал",
            expected_name="Кофейня «Локал»",
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, "Кафе Локал")
        self.session.expire_all()
        aliases = list_place_aliases(self.session, self.place.id)
        self.assertEqual([item.name for item in aliases], ["Кофейня «Локал»"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
