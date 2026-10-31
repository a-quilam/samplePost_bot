# tests/test_poster2_231_place_keyboard_layout.py
"""
Poster 2.3.1 E4: компактная клавиатура карточки места.

Проверяется раскладка `_place_keyboard` снаружи, как у владельца:
- действия собраны парами по смыслу, не больше двух кнопок в строке;
- навигация «К списку мест» | «В меню» — последняя строка;
- состав действий и их callback-данные не изменились по сравнению с
  прежней компоновкой (права по-прежнему решают видимость).

Сценарии идут через настоящий `Application.process_update` и временную
SQLite; прямые доменные вызовы используются только для подготовки данных.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from sqlalchemy import select

    from models import PlaceAdmin, User
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from tests.test_stage20_admin_flow import ensure_user, insert_place

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)

SUPER_TG = 700100
PLACE_ADMIN_TG = 700200


def _rows(h) -> list[list[tuple[str, str | None]]]:
    """Структура клавиатуры: строки и кнопки, как их видит пользователь."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _callbacks(h) -> list[str | None]:
    return [data for _label, data in h.screen_buttons()]


def add_place_admin(h, place_id: int, telegram_user_id: int) -> None:
    """Делает пользователя действующим администратором места."""
    session = h.session_factory()
    try:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        assert user is not None, f"нет пользователя {telegram_user_id}"
        session.add(PlaceAdmin(place_id=place_id, user_id=user.id))
        session.commit()
    finally:
        session.close()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceKeyboardLayoutTests(unittest.TestCase):
    """E4: карточка места компактна и не теряет действий."""

    @scenario_test
    async def test_superadmin_without_admin_gets_pairs(self, h) -> None:
        place_id = insert_place(h, "Место без админа")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)

            self.assertLessEqual(
                max(len(row) for row in _rows(h)), 2, f"строка шире пары: {_rows(h)}"
            )
            rows = _rows(h)
            self.assertEqual(
                [data for _label, data in rows[0]],
                [f"adm_edit_place:{place_id}", f"tp_std:{place_id}"],
            )
            self.assertEqual(
                [data for _label, data in rows[1]],
                [f"adm_bprop_place:{place_id}", f"adm_bconflicts:{place_id}"],
            )
            self.assertEqual(
                [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
            )
            # Назначение — одиночное действие: рядом с ним ничего не стоит.
            self.assertEqual(
                [data for _label, data in rows[-2]], [f"adm_assign:{place_id}"]
            )
            self.assertIn(f"adm_assign:{place_id}", _callbacks(h))

    @scenario_test
    async def test_superadmin_with_admin_pairs_replace_and_revoke(self, h) -> None:
        place_id = insert_place(h, "Место с админом")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)

            rows = _rows(h)
            self.assertEqual(
                [data for _label, data in rows[-2]],
                [f"adm_replace:{place_id}", f"adm_revoke:{place_id}"],
            )
            self.assertEqual(
                [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
            )
            self.assertNotIn(f"adm_assign:{place_id}", _callbacks(h))

    @scenario_test
    async def test_superadmin_who_is_admin_pairs_transfer_and_replace(self, h) -> None:
        """Суперадмин и админ места сразу: «Передать» | «Заменить», «Снять» ниже."""
        place_id = insert_place(h, "Место суперадмина")
        ensure_user(h, SUPER_TG, full_name="Супер Админ")
        add_place_admin(h, place_id, SUPER_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=SUPER_TG)
            await h.callback(f"adm_place:{place_id}", user_id=SUPER_TG)

            rows = _rows(h)
            self.assertEqual(
                [data for _label, data in rows[-3]],
                [f"adm_transfer:{place_id}", f"adm_replace:{place_id}"],
            )
            self.assertEqual(
                [data for _label, data in rows[-2]], [f"adm_revoke:{place_id}"]
            )
            self.assertEqual(
                [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
            )
            self.assertLessEqual(max(len(row) for row in rows), 2)

    @scenario_test
    async def test_place_admin_keeps_transfer_and_counters(self, h) -> None:
        place_id = insert_place(h, "Своё место")
        ensure_user(h, PLACE_ADMIN_TG, full_name="Местный Админ")
        add_place_admin(h, place_id, PLACE_ADMIN_TG)
        await h.text("/admin", user_id=PLACE_ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=PLACE_ADMIN_TG)

        rows = _rows(h)
        self.assertLessEqual(
            max(len(row) for row in rows), 2, f"строка шире пары: {rows}"
        )
        self.assertEqual(
            [data for _label, data in rows[1]],
            [f"adm_bprop_place:{place_id}", f"adm_bconflicts:{place_id}"],
        )
        self.assertEqual(
            [data for _label, data in rows[-2]], [f"adm_transfer:{place_id}"]
        )
        self.assertEqual(
            [data for _label, data in rows[-1]], ["adm_places", "main_menu"]
        )
        callbacks = _callbacks(h)
        for forbidden in ("adm_assign", "adm_replace", "adm_revoke"):
            self.assertFalse(
                [data for data in callbacks if (data or "").startswith(forbidden)],
                f"лишнее действие {forbidden}: {callbacks}",
            )
