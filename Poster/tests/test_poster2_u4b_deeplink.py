# tests/test_poster2_u4b_deeplink.py
"""
Poster 2.2 U4b: ссылка «создать анонс этой площадки» (`/start <token>`).

Проверяется через настоящее приложение `bot.build_application`, FakeBot и
временную SQLite (tests.integration_harness): реальный Telegram, `.env` и
`post_bot.db` не используются.

Покрытие:

- ссылка из карточки места: админ площадки видит её, превью отключено,
  username бота берётся у Telegram, токен — случайный (не `place_id`);
- `/start <token>` открывает конструктор ЭТОЙ площадки сразу: без выбора
  места и без выбора шаблона, и диалог продолжает принимать ответы;
- новый пользователь получает профиль и стартовый личный шаблон;
- повторный `/start` тем же токеном продолжает ТОТ ЖЕ черновик площадки с
  введёнными данными (без дублей), а прежний WIP по ДРУГОЙ площадке
  остаётся в «Мои анонсы» и не удаляется;
- после отправки анонса ссылка открывает новый черновик той же площадки;
- отозванная ссылка, удалённая площадка и подделанный/неизвестный токен
  дают один понятный отказ и главное меню (это не ошибка);
- `/start` без payload — прежний вход в меню (регресс);
- ссылка не даёт прав: у приглашённого нет ни редактирования площадки, ни
  административного кабинета;
- повторный вход в раздел ссылки не создаёт дублей, перевыпуск отзывает
  прежний токен.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

try:
    from announcement_store import ORIGIN_PLACE, list_announcement_drafts
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import PlaceAdmin, PlaceInviteLink, User, utcnow
    from place_store import create_place
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)

OTHER_TG = 8_930_002
INVITED_TG = 8_930_003

# Значение совпадает с `handlers.announcement_wizard.AW_QUESTIONS`.
AW_QUESTIONS = 2


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = True,
):
    """Место со стандартным шаблоном; `admin=True` выдаёт роль администратора."""
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        if admin:
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
        return list(
            session.scalars(select(PlaceInviteLink).order_by(PlaceInviteLink.id)).all()
        )


def _active_invites(h):
    return [row for row in _invites(h) if row.revoked_at is None]


def _token_from_screen(h) -> str:
    text = h.screen_text()
    marker = "?start="
    if marker not in text:
        raise AssertionError(f"В экране нет ссылки: {text!r}")
    return text.split(marker, 1)[1].split()[0].strip()


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


async def _open_invite_screen(h, place_id: int, user_id: int = DEFAULT_USER_ID) -> str:
    """Открывает карточку места и раздел ссылки, возвращает токен."""
    await h.text("/start", user_id=user_id)
    await h.callback("manage_places", user_id=user_id)
    await h.callback(f"plm_open:{place_id}", user_id=user_id)
    await h.callback(f"plm_invite:{place_id}", user_id=user_id)
    return _token_from_screen(h)


@unittest.skipIf(INTEGRATION_IMPORT_ERROR, INTEGRATION_IMPORT_ERROR or "")
class PlaceInviteLinkTests(unittest.TestCase):
    """Выдача ссылки: права, идемпотентность, перевыпуск, превью."""

    @scenario_test
    async def test_card_shows_link_with_preview_disabled_and_bot_username(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)

        token = await _open_invite_screen(h, place_id)

        text = h.screen_text()
        self.assertIn("Ссылка для создания анонса", text)
        self.assertIn("Дом культуры", text)
        self.assertIn(f"https://t.me/{h.bot.username}?start={token}", text)
        # Превью ссылки отключено у самого сообщения-экрана.
        record = h.screen()
        assert record is not None
        preview = record.get("link_preview_options")
        self.assertIsNotNone(preview, "экран должен отключать превью ссылки")
        self.assertTrue(getattr(preview, "is_disabled", False))

    @scenario_test
    async def test_token_is_random_and_not_place_id(self, h):
        place_id = _seed_place(h, admin=True)
        token = await _open_invite_screen(h, place_id)

        self.assertNotEqual(token, str(place_id))
        self.assertNotIn(f":{place_id}", token)
        self.assertGreaterEqual(len(token), 16)
        self.assertLessEqual(len(token), 22)
        self.assertTrue(
            all(char.isalnum() or char in {"_", "-"} for char in token),
            token,
        )
        # Достаточно случайный: две площадки получают разные токены.
        second_place = _seed_place(h, name="Клуб", admin=True)
        second_token = await _open_invite_screen(h, second_place)
        self.assertNotEqual(token, second_token)

    @scenario_test
    async def test_repeated_visit_does_not_duplicate_link(self, h):
        place_id = _seed_place(h, admin=True)
        first = await _open_invite_screen(h, place_id)
        await h.callback(f"plm_card:{place_id}")
        await h.callback(f"plm_invite:{place_id}")
        second = _token_from_screen(h)

        self.assertEqual(first, second)
        self.assertEqual(len(_active_invites(h)), 1)
        self.assertEqual(len(_invites(h)), 1)

    @scenario_test
    async def test_regenerate_revokes_previous_token(self, h):
        place_id = _seed_place(h, admin=True)
        old_token = await _open_invite_screen(h, place_id)

        await h.callback(_callback(h, "plm_invite_regen:"))
        new_token = _token_from_screen(h)

        self.assertNotEqual(old_token, new_token)
        self.assertIn("Прежняя ссылка больше не работает", h.screen_text())
        rows = _invites(h)
        self.assertEqual(len(rows), 2, "отозванная ссылка остаётся аудитом")
        self.assertEqual(len(_active_invites(h)), 1)
        revoked = [row for row in rows if row.revoked_at is not None]
        self.assertEqual([row.token for row in revoked], [old_token])

        # Старый токен больше не открывает конструктор.
        await h.text(f"/start {old_token}", user_id=INVITED_TG)
        self.assertIn("Ссылка не работает", h.screen_text())
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))

    @scenario_test
    async def test_link_does_not_grant_admin_rights(self, h):
        place_id = _seed_place(h, admin=True)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        self.assertIn("Вопрос 1", h.screen_text())

        # Прав не появилось: чужое место не редактируется, кабинет закрыт.
        await h.text("/start", user_id=INVITED_TG)
        await h.callback("manage_places", user_id=INVITED_TG)
        await h.callback(f"plm_open:{place_id}", user_id=INVITED_TG)
        self.assertEqual(_callbacks(h, "plm_edit:"), [])
        self.assertEqual(_callbacks(h, "plm_invite:"), [])
        await h.callback("/admin", user_id=INVITED_TG)
        await h.text("/admin", user_id=INVITED_TG)
        self.assertNotIn("Кабинет суперадмина", h.screen_text())
        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == INVITED_TG)
            )
            assert user is not None
            roles = list(
                session.scalars(
                    select(PlaceAdmin).where(PlaceAdmin.user_id == user.id)
                ).all()
            )
        self.assertEqual(
            [role for role in roles if role.revoked_at is None],
            [],
            "ссылка не создаёт администратора площадки",
        )


@unittest.skipIf(INTEGRATION_IMPORT_ERROR, INTEGRATION_IMPORT_ERROR or "")
class PlaceInviteDeepLinkTests(unittest.TestCase):
    """Открытие ссылки: конструктор сразу, без выбора места и шаблона."""

    @scenario_test
    async def test_valid_link_opens_wizard_without_pickers(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        text = h.screen_text()
        self.assertIn("Вопрос 1", text)
        self.assertNotIn("Выберите площадку", text)
        self.assertNotIn("Выберите шаблон", text)
        self.assertEqual(
            h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME), AW_QUESTIONS
        )
        # Нет промежуточного экрана выбора: первый же ответ — вопрос.
        drafts = self._drafts(h, INVITED_TG)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].origin, ORIGIN_PLACE)
        self.assertEqual(drafts[0].place_id, place_id)
        self.assertIsNotNone(drafts[0].template_snapshot)
        self.assertEqual(drafts[0].template_snapshot.source_kind, "standard")

        # Диалог живой: следующий ответ принимается.
        await h.text("Мой концерт", user_id=INVITED_TG)
        self.assertNotIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_new_user_gets_profile_and_initial_template(self, h):
        place_id = _seed_place(h, admin=True)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == INVITED_TG)
            )
            self.assertIsNotNone(user, "профиль создаётся и по ссылке")
            from template_store import list_personal_templates

            templates = list_personal_templates(session, INVITED_TG)
        self.assertEqual(len(templates), 1, "стартовый шаблон выдаётся один раз")

    @scenario_test
    async def test_existing_wip_is_kept_and_new_draft_opens(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)
        other_place = _seed_place(h, name="Клуб", admin=True)
        # Прежний черновик пользователя (обычный вход по площадке).
        await h.text("/start", user_id=INVITED_TG)
        await h.callback("pick_place", user_id=INVITED_TG)
        await h.callback(f"pl_pick:{other_place}", user_id=INVITED_TG)
        self.assertIn("Вопрос 1", h.screen_text())
        wip = self._drafts(h, INVITED_TG)
        self.assertEqual(len(wip), 1)
        wip_id = wip[0].draft_id

        token = await _open_invite_screen(h, place_id)
        await h.text(f"/start {token}", user_id=INVITED_TG)

        drafts = self._drafts(h, INVITED_TG)
        self.assertEqual(len(drafts), 2, "прежний WIP не удаляется")
        ids = {draft.draft_id for draft in drafts}
        self.assertIn(wip_id, ids)
        new_draft = next(draft for draft in drafts if draft.draft_id != wip_id)
        self.assertEqual(new_draft.place_id, place_id)

        # Прежний черновик доступен в «Мои анонсы».
        await h.text("/start", user_id=INVITED_TG)
        await h.callback("my_announcements", user_id=INVITED_TG)
        self.assertIn(f"aw_open:{wip_id}", _callbacks(h, "aw_open:"))

    @scenario_test
    async def test_second_start_same_token_continues_same_draft(self, h):
        """Повторный `/start` тем же токеном продолжает тот же черновик."""
        place_id = _seed_place(h, name="Дом культуры", admin=True)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        await h.text("Мой концерт", user_id=INVITED_TG)
        first = self._drafts(h, INVITED_TG)
        self.assertEqual(len(first), 1)
        first_id = first[0].draft_id
        self.assertTrue(
            [value for value in first[0].values if value.payload],
            "ответ должен сохраниться в черновике",
        )

        await h.text(f"/start {token}", user_id=INVITED_TG)

        drafts = self._drafts(h, INVITED_TG)
        self.assertEqual(
            [draft.draft_id for draft in drafts],
            [first_id],
            "повторная ссылка не должна создавать второй черновик площадки",
        )
        text = h.screen_text()
        self.assertIn("Вопрос 1", text, "продолжение начинается с введённого поля")
        self.assertIn("Текущее значение: Мой концерт", text)
        self.assertNotIn("Значение пока не задано", text)
        self.assertEqual(
            h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME), AW_QUESTIONS
        )
        # Черновик живой: следующий ответ по-прежнему сохраняется.
        await h.text("03.04.2027", user_id=INVITED_TG)
        self.assertNotIn("Вопрос 1", h.screen_text())
        self.assertEqual(len(self._drafts(h, INVITED_TG)), 1)

    @scenario_test
    async def test_link_after_sent_announcement_opens_new_draft(self, h):
        """Завершённый анонс не «продолжается» ссылкой — открывается новый."""
        place_id = _seed_place(h, name="Дом культуры", admin=True)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        for _step in range(12):
            skips = _callbacks(h, "aw_qskip:")
            nones = _callbacks(h, "aw_photo_none:")
            if skips:
                await h.callback(skips[0], user_id=INVITED_TG)
            elif nones:
                await h.callback(nones[0], user_id=INVITED_TG)
            else:
                break
        sent = self._drafts(h, INVITED_TG)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].publication_status, "sent")

        await h.text(f"/start {token}", user_id=INVITED_TG)

        drafts = self._drafts(h, INVITED_TG)
        self.assertEqual(len(drafts), 2, "после отправки ссылка открывает новый анонс")
        new = next(draft for draft in drafts if draft.draft_id != sent[0].draft_id)
        self.assertNotEqual(new.publication_status, "sent")
        self.assertEqual(new.place_id, place_id)
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_unknown_and_forged_tokens_show_safe_message(self, h):
        _seed_place(h, admin=True)
        for payload in (
            "Z" * 22,
            "0123456789abcdef",
            "not-a-token",
            "x",
        ):
            await h.text(f"/start {payload}", user_id=INVITED_TG)
            self.assertIn(
                "Ссылка не работает",
                h.screen_text(),
                f"payload {payload!r} должен дать безопасный отказ",
            )
            self.assertIsNone(
                h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME)
            )
            self.assertIn("pick_place", _callbacks(h, ""))
        self.assertEqual(self._drafts(h, INVITED_TG), [])

    @scenario_test
    async def test_deleted_place_link_is_invalid(self, h):
        place_id = _seed_place(h, admin=True)
        token = await _open_invite_screen(h, place_id)

        # Площадку удаляют вне приложения (обслуживание базы): строка ссылки
        # остаётся «сиротой», и именно проверка существования места не даёт
        # открыть по ней конструктор. Соединение без foreign_keys — как
        # ручная правка базы.
        import sqlite3

        connection = sqlite3.connect(str(h.engine.url.database))
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM places WHERE id = ?", (place_id,))
            connection.commit()
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM place_invite_links WHERE token = ?", (token,)
                ).fetchone(),
                "ссылка осталась в базе — проверка обязана отсечь сироту",
            )
        finally:
            connection.close()

        await h.text(f"/start {token}", user_id=INVITED_TG)
        self.assertIn("Ссылка не работает", h.screen_text())
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))

    @scenario_test
    async def test_start_without_payload_still_opens_main_menu(self, h):
        await h.text("/start")

        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertEqual(
            [data for _label, data in h.screen_buttons()],
            ["pick_place", "pick_template", "my_announcements", "manage", "help"],
        )
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_transfer_payload_still_goes_to_transfer_handler(self, h):
        # Чужое пространство имён `tf_` не перехватывается ссылкой.
        await h.text("/start tf_999_deadbeefdeadbeefdeadbeef", user_id=INVITED_TG)
        self.assertNotIn("Вопрос 1", h.screen_text())
        self.assertTrue(
            "передач" in h.screen_text().lower()
            or "приглашение" in h.screen_text().lower(),
            h.screen_text(),
        )

    # —— вспомогательное ——

    def _session(self, h):
        return h.session_factory()

    def _drafts(self, h, telegram_user_id: int):
        with h.session_factory() as session:
            return list_announcement_drafts(
                session, actor_telegram_user_id=telegram_user_id
            )
