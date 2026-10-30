# tests/test_poster2_u4b_deeplink_tester.py
"""
Poster 2.2 U4b, независимые негативные проверки ссылки «создать анонс».

Написаны «с другой стороны» от реализации: проверяют безопасность и
устойчивость, а не UX-тексты.

- IDOR выдачи: обычный пользователь, админ ДРУГОЙ площадки и незнакомец не
  получают ссылку чужой площадки; подделанная кнопка `plm_invite:<чужой id>`
  отвечает отказом и ничего не создаёт;
- суперадмин выдаёт ссылку любой площадки;
- подделанные payload'ы `/start` (пустой, мусор, токен другой площадки,
  усечённый/удлинённый токен) не открывают конструктор и не создают
  черновик;
- двойной `/start` с одним токеном дважды подряд даёт рабочий конструктор и
  не дублирует активную ссылку;
- payload ссылки не содержит `place_id` и не перебирается.

Реальная база, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

try:
    from announcement_store import ORIGIN_PLACE, list_announcement_drafts
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import PlaceAdmin, PlaceInviteLink, utcnow
    from place_invite_store import (
        PlaceInviteError,
        PlaceInvitePermissionError,
        ensure_invite,
        is_valid_token_shape,
        open_invite,
    )
    from place_store import create_place
    from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)

OWNER_TG = 8_940_001
FOREIGN_ADMIN_TG = 8_940_002
STRANGER_TG = 8_940_003
SUPERADMIN_TG = 8_940_004


def _seed_user(h, telegram_user_id: int) -> None:
    with h.session_factory() as session:
        get_or_create_user(session, telegram_user_id, username=f"u{telegram_user_id}")
        session.commit()


def _seed_place(h, name: str, *, owner_tg: int = OWNER_TG) -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, owner_tg, username=f"u{owner_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        return place.id


def _invites(h):
    with h.session_factory() as session:
        return list(session.scalars(select(PlaceInviteLink)).all())


def _active(h):
    return [row for row in _invites(h) if row.revoked_at is None]


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _token_from_screen(h) -> str:
    text = h.screen_text()
    marker = "?start="
    if marker not in text:
        raise AssertionError(f"В экране нет ссылки: {text!r}")
    return text.split(marker, 1)[1].split()[0].strip()


@unittest.skipIf(INTEGRATION_IMPORT_ERROR, INTEGRATION_IMPORT_ERROR or "")
class InviteIssuanceSecurityTests(unittest.TestCase):
    """Ссылку выдаёт только управляющий площадкой; подделка кнопки — отказ."""

    def setUp(self) -> None:
        self.enterContext(patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)))
        self.enterContext(patch("config.SUPERADMIN_CONTACT_URL", None))

    @scenario_test
    async def test_stranger_cannot_get_foreign_place_link(self, h):
        place_id = _seed_place(h, "Чужая площадка")
        _seed_user(h, STRANGER_TG)

        await h.text("/start", user_id=STRANGER_TG)
        await h.callback("manage_places", user_id=STRANGER_TG)
        await h.callback(f"plm_open:{place_id}", user_id=STRANGER_TG)
        self.assertEqual(_callbacks(h, "plm_invite:"), [])

        # Подделанная кнопка: отказ в callback'е, ссылка не создаётся.
        await h.callback(f"plm_invite:{place_id}", user_id=STRANGER_TG)
        self.assertTrue(h.answers())
        self.assertEqual(_invites(h), [])

    @scenario_test
    async def test_admin_of_another_place_cannot_get_link(self, h):
        foreign_place = _seed_place(h, "Чужая площадка")
        own_place = _seed_place(h, "Своя площадка", owner_tg=FOREIGN_ADMIN_TG)

        await h.text("/start", user_id=FOREIGN_ADMIN_TG)
        await h.callback("manage_places", user_id=FOREIGN_ADMIN_TG)
        await h.callback(f"plm_open:{foreign_place}", user_id=FOREIGN_ADMIN_TG)
        await h.callback(f"plm_invite:{foreign_place}", user_id=FOREIGN_ADMIN_TG)

        self.assertEqual(
            [row.place_id for row in _invites(h)],
            [],
            "админ другой площадки не выдаёт чужую ссылку",
        )

        # Своя площадка — можно (возврат в список, затем карточка).
        await h.callback("plm_back", user_id=FOREIGN_ADMIN_TG)
        await h.callback(f"plm_open:{own_place}", user_id=FOREIGN_ADMIN_TG)
        await h.callback(f"plm_invite:{own_place}", user_id=FOREIGN_ADMIN_TG)
        self.assertEqual([row.place_id for row in _active(h)], [own_place])

    @scenario_test
    async def test_superadmin_issues_link_for_any_place(self, h):
        place_id = _seed_place(h, "Любая площадка")

        await h.text("/start", user_id=SUPERADMIN_TG)
        await h.callback("manage_places", user_id=SUPERADMIN_TG)
        await h.callback(f"plm_open:{place_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"plm_invite:{place_id}", user_id=SUPERADMIN_TG)

        token = _token_from_screen(h)
        self.assertTrue(is_valid_token_shape(token))
        self.assertEqual([row.place_id for row in _active(h)], [place_id])

    @scenario_test
    async def test_store_refuses_foreign_actor_directly(self, h):
        place_id = _seed_place(h, "Чужая площадка")
        _seed_user(h, STRANGER_TG)

        with h.session_factory() as session:
            with self.assertRaises(PlaceInvitePermissionError):
                ensure_invite(
                    session,
                    actor_telegram_user_id=STRANGER_TG,
                    place_id=place_id,
                )
        self.assertEqual(_invites(h), [])

        with h.session_factory() as session:
            with self.assertRaises(PlaceInviteError):
                ensure_invite(
                    session,
                    actor_telegram_user_id=OWNER_TG,
                    place_id=place_id + 999,
                )


@unittest.skipIf(INTEGRATION_IMPORT_ERROR, INTEGRATION_IMPORT_ERROR or "")
class ForgedStartTests(unittest.TestCase):
    """Подделанный `/start` не открывает конструктор и не создаёт черновик."""

    @scenario_test
    async def test_empty_argument_keeps_main_menu(self, h):
        await h.text("/start ")

        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(
            [data for _label, data in h.screen_buttons()],
            ["pick_place", "pick_template", "my_announcements", "manage", "help"],
        )

    @scenario_test
    async def test_garbage_payload_is_refused(self, h):
        _seed_place(h, "Площадка")
        for payload in (
            "!!!!",
            "a" * 200,
            "../../etc/passwd",
            "%00",
            "12345",
            "Z" * 64,
        ):
            await h.text(f"/start {payload}", user_id=STRANGER_TG)
            self.assertIsNone(
                h.dialog_state(user_id=STRANGER_TG, name=CONVERSATION_NAME),
                f"payload {payload!r} не должен открывать диалог",
            )
        with h.session_factory() as session:
            self.assertEqual(
                list_announcement_drafts(session, actor_telegram_user_id=STRANGER_TG),
                [],
            )
        self.assertEqual(_invites(h), [])

    @scenario_test
    async def test_token_of_other_place_opens_only_its_place(self, h):
        first = _seed_place(h, "Первая площадка")
        second = _seed_place(h, "Вторая площадка", owner_tg=FOREIGN_ADMIN_TG)
        with h.session_factory() as session:
            first_token = ensure_invite(
                session,
                actor_telegram_user_id=OWNER_TG,
                place_id=first,
            ).token
            second_token = ensure_invite(
                session,
                actor_telegram_user_id=FOREIGN_ADMIN_TG,
                place_id=second,
            ).token
        self.assertNotEqual(first_token, second_token)

        await h.text(f"/start {second_token}", user_id=STRANGER_TG)
        with h.session_factory() as session:
            drafts = list_announcement_drafts(
                session, actor_telegram_user_id=STRANGER_TG
            )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].place_id, second)
        self.assertEqual(drafts[0].origin, ORIGIN_PLACE)

    @scenario_test
    async def test_truncated_and_extended_token_are_invalid(self, h):
        place_id = _seed_place(h, "Площадка")
        with h.session_factory() as session:
            token = ensure_invite(
                session,
                actor_telegram_user_id=OWNER_TG,
                place_id=place_id,
            ).token

        for payload in (token[:-1], token + "x", token.upper()):
            if payload == token:
                continue
            with h.session_factory() as session:
                self.assertEqual(open_invite(session, payload).status, "invalid")
            await h.text(f"/start {payload}", user_id=STRANGER_TG)
            self.assertIsNone(
                h.dialog_state(user_id=STRANGER_TG, name=CONVERSATION_NAME)
            )

    @scenario_test
    async def test_double_start_with_same_token_creates_one_link_and_works(self, h):
        place_id = _seed_place(h, "Площадка")
        with h.session_factory() as session:
            token = ensure_invite(
                session,
                actor_telegram_user_id=OWNER_TG,
                place_id=place_id,
            ).token

        await h.text(f"/start {token}", user_id=STRANGER_TG)
        self.assertIn("Вопрос 1", h.screen_text())

        # Второй старт с тем же токеном (как повторное открытие ссылки):
        # диалог переоткрывается, ссылка не дублируется.
        await h.text(f"/start {token}", user_id=STRANGER_TG)
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(len(_active(h)), 1)

    @scenario_test
    async def test_payload_has_no_place_id(self, h):
        place_id = _seed_place(h, "Площадка")
        with h.session_factory() as session:
            token = ensure_invite(
                session,
                actor_telegram_user_id=OWNER_TG,
                place_id=place_id,
            ).token
        self.assertNotEqual(token, str(place_id))
        self.assertFalse(
            token.isdigit(),
            "payload не должен быть перебираемым числовым идентификатором",
        )
        self.assertGreaterEqual(len(token), 16, "payload не короче ссылки place_id")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
