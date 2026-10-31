# tests/test_poster2_u4b_acceptance_tester.py
"""
Poster 2.2 U4b, независимая приёмка сценария владельца №9.

Проверяется ФАКТИЧЕСКОЕ поведение через настоящее приложение
(`bot.build_application`), FakeBot и временную SQLite. Реальная
`post_bot.db`, `.env` и сеть Telegram не используются.

Сценарий владельца: администратор площадки получает ссылку, ДРУГОЙ
пользователь открывает её, и конструктор запускается сразу для правильной
площадки (без выбора места и шаблона).

Отдельно зафиксированы расхождения, найденные при приёмке (исправлены в
U4b-fix, регрессия — `tests/test_poster2_u4b_defects_tester.py`):

- повторный `/start` с тем же токеном создавал ВТОРОЙ черновик той же
  площадки (п.6 приёмки: «двойной /start — без дублей черновиков»);
- потеря прав между проверкой в обработчике и повторной проверкой в
  хранилище при отрисовке экрана ссылки показывала «внутреннюю ошибку»
  вместо понятного отказа (гонка прав).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

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

    ACCEPTANCE_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    ACCEPTANCE_IMPORT_ERROR = str(exc)

# Значение совпадает с `handlers.announcement_wizard.AW_QUESTIONS`.
AW_QUESTIONS = 2

OTHER_TG = 9_960_001
INVITED_TG = 9_960_002
PLAIN_TG = 9_960_003
SUPER_TG = 9_960_004

MAIN_MENU_BUTTONS = [
    "pick_place",
    "pick_template",
    "my_announcements",
    "manage",
    "help",
]


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    admin_tg: int = DEFAULT_USER_ID,
    admin: bool = True,
) -> int:
    """Площадка со стандартным шаблоном; `admin=True` выдаёт роль админа."""
    with h.session_factory() as session:
        user = get_or_create_user(session, admin_tg, username=f"u{admin_tg}")
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


def _seed_user(h, telegram_user_id: int) -> None:
    with h.session_factory() as session:
        get_or_create_user(session, telegram_user_id, username=f"u{telegram_user_id}")
        session.commit()


def _invites(h) -> list:
    with h.session_factory() as session:
        return list(
            session.scalars(select(PlaceInviteLink).order_by(PlaceInviteLink.id)).all()
        )


def _active_tokens(h) -> list[str]:
    return [row.token for row in _invites(h) if row.revoked_at is None]


def _drafts(h, telegram_user_id: int):
    with h.session_factory() as session:
        return list(
            list_announcement_drafts(session, actor_telegram_user_id=telegram_user_id)
        )


def _callbacks(h, prefix: str = "") -> list[str]:
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


async def _open_invite_screen(h, place_id: int, *, user_id: int = DEFAULT_USER_ID):
    """Карточка места → «🔗 Ссылка для создания анонса» → токен."""
    await h.text("/start", user_id=user_id)
    await h.callback("manage_places", user_id=user_id)
    await h.callback(f"plm_open:{place_id}", user_id=user_id)
    await h.callback(f"plm_invite:{place_id}", user_id=user_id)
    return _token_from_screen(h)


@unittest.skipIf(ACCEPTANCE_IMPORT_ERROR, ACCEPTANCE_IMPORT_ERROR or "")
class OwnerScenarioAcceptanceTests(unittest.TestCase):
    """Сценарий владельца №9: ссылка, приглашённый, конструктор сразу."""

    @scenario_test
    async def test_link_screen_has_explanation_and_disabled_preview(self, h):
        place_id = _seed_place(h, name="Дом культуры")

        token = await _open_invite_screen(h, place_id)

        text = h.screen_text()
        self.assertIn("Ссылка для создания анонса", text)
        self.assertIn("Дом культуры", text)
        self.assertIn(f"https://t.me/{h.bot.username}?start={token}", text)
        # Объяснение: права администратора ссылка не даёт.
        self.assertIn("не даёт прав", text)
        record = h.screen()
        assert record is not None
        preview = record.get("link_preview_options")
        self.assertIsNotNone(preview, "экран обязан отключать превью ссылки")
        self.assertTrue(getattr(preview, "is_disabled", False))

    @scenario_test
    async def test_other_user_gets_wizard_for_exact_place(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        text = h.screen_text()
        self.assertIn("Вопрос 1", text)
        self.assertNotIn("Выберите площадку", text)
        self.assertNotIn("Выберите шаблон", text)
        self.assertEqual(
            h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME), AW_QUESTIONS
        )
        drafts = _drafts(h, INVITED_TG)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].place_id, place_id)
        self.assertEqual(drafts[0].origin, ORIGIN_PLACE)
        self.assertIsNotNone(drafts[0].template_snapshot)
        self.assertEqual(drafts[0].template_snapshot.source_kind, "standard")
        # Диалог живой: следующий ответ принимается.
        await h.text("Мой концерт", user_id=INVITED_TG)
        self.assertNotIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_new_user_gets_profile_and_starter_template(self, h):
        place_id = _seed_place(h)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)

        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == INVITED_TG)
            )
            self.assertIsNotNone(user, "профиль создаётся и по ссылке")
            from template_store import list_personal_templates

            templates = list_personal_templates(session, INVITED_TG)
        self.assertEqual(len(templates), 1, "стартовый шаблон выдан один раз")

    @scenario_test
    async def test_existing_wip_survives_and_stays_openable(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        other_place = _seed_place(h, name="Клуб", admin_tg=OTHER_TG)
        # Незавершённый черновик по другой площадке, уже с введённым значением.
        await h.text("/start", user_id=INVITED_TG)
        await h.callback("pick_place", user_id=INVITED_TG)
        await h.callback(f"pl_pick:{other_place}", user_id=INVITED_TG)
        await h.text("Прежний черновик", user_id=INVITED_TG)
        wip = _drafts(h, INVITED_TG)
        self.assertEqual(len(wip), 1)
        wip_id, wip_revision = wip[0].draft_id, wip[0].revision

        token = await _open_invite_screen(h, place_id)
        await h.text(f"/start {token}", user_id=INVITED_TG)

        drafts = _drafts(h, INVITED_TG)
        self.assertEqual(len(drafts), 2, "прежний черновик не уничтожается")
        old = next(draft for draft in drafts if draft.draft_id == wip_id)
        self.assertEqual(old.revision, wip_revision, "прежний черновик не перезаписан")
        self.assertTrue(
            [value for value in old.values if value.payload],
            "введённое значение прежнего черновика сохранено",
        )
        new = next(draft for draft in drafts if draft.draft_id != wip_id)
        self.assertEqual(new.place_id, place_id)

        await h.text("/start", user_id=INVITED_TG)
        await h.callback("my_announcements", user_id=INVITED_TG)
        self.assertIn(f"aw_open:{wip_id}", _callbacks(h, "aw_open:"))

    @scenario_test
    async def test_link_grants_no_admin_rights(self, h):
        place_id = _seed_place(h)
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        self.assertIn("Вопрос 1", h.screen_text())

        await h.text("/start", user_id=INVITED_TG)
        await h.callback("manage_places", user_id=INVITED_TG)
        await h.callback(f"plm_open:{place_id}", user_id=INVITED_TG)
        self.assertEqual(_callbacks(h, "plm_edit:"), [], "чужое место не редактируется")
        self.assertEqual(_callbacks(h, "plm_invite:"), [], "ссылку не перевыпускает")

        # Подделанное нажатие: отказ, ничего не создаётся.
        invite_rows_before = len(_invites(h))
        await h.callback(f"plm_invite:{place_id}", user_id=INVITED_TG)
        self.assertTrue(h.answer_texts(), "подделанная кнопка должна получить отказ")
        self.assertEqual(len(_invites(h)), invite_rows_before)

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
            "ссылка не делает пользователя администратором площадки",
        )

    @scenario_test
    async def test_token_does_not_expose_place_id_or_secrets(self, h):
        place_id = _seed_place(h)
        token = await _open_invite_screen(h, place_id)

        self.assertNotEqual(token, str(place_id))
        # Подстроку "1" искать нельзя: одиночная цифра случайно встречается
        # в 22-символьном токене. Значимо другое — payload не является
        # номером места и не содержит его в виде «pl:1»/«1:place».
        self.assertFalse(token.isdigit(), "payload не перебирается как номер места")
        self.assertNotIn(":", token, "payload — непрозрачный идентификатор")
        self.assertGreaterEqual(len(token), 16)
        self.assertLessEqual(len(token), 22)
        text = h.screen_text()
        self.assertNotIn(f":{place_id}", text)
        self.assertNotIn("token", text.lower())

        # Числовой payload, равный place_id, конструктор не открывает.
        await h.text(f"/start {place_id}", user_id=INVITED_TG)
        self.assertIn("Ссылка не работает", h.screen_text())
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))
        self.assertEqual(_drafts(h, INVITED_TG), [])

    @scenario_test
    async def test_revoked_link_is_refused(self, h):
        place_id = _seed_place(h)
        old_token = await _open_invite_screen(h, place_id)

        await h.callback(f"plm_invite_regen:{place_id}", user_id=DEFAULT_USER_ID)
        new_token = _token_from_screen(h)
        self.assertNotEqual(old_token, new_token)

        await h.text(f"/start {old_token}", user_id=INVITED_TG)
        self.assertIn("Ссылка не работает", h.screen_text())
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))
        self.assertEqual(_drafts(h, INVITED_TG), [])

        await h.text(f"/start {new_token}", user_id=INVITED_TG)
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_deleted_place_link_is_refused(self, h):
        place_id = _seed_place(h)
        token = await _open_invite_screen(h, place_id)

        # Удаление вне приложения (обслуживание базы): строка ссылки остаётся
        # сиротой, отсечь её обязана проверка существования места.
        import sqlite3

        connection = sqlite3.connect(str(h.engine.url.database))
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM places WHERE id = ?", (place_id,))
            connection.commit()
        finally:
            connection.close()

        await h.text(f"/start {token}", user_id=INVITED_TG)
        self.assertIn("Ссылка не работает", h.screen_text())
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))
        self.assertEqual(_drafts(h, INVITED_TG), [])

    @scenario_test
    async def test_forged_payload_refused_with_menu_and_no_internal_error(self, h):
        _seed_place(h)
        self.assertEqual(_invites(h), [])

        for payload in ("Z" * 22, "0123456789abcdef", "%00", "../etc/passwd", "x"):
            await h.text(f"/start {payload}", user_id=INVITED_TG)
            text = h.screen_text()
            self.assertIn("Ссылка не работает", text, payload)
            self.assertNotIn("внутренняя ошибка", text.lower(), payload)
            self.assertEqual(
                _callbacks(h), MAIN_MENU_BUTTONS, f"payload {payload!r} → меню"
            )
            self.assertIsNone(
                h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME)
            )
        self.assertEqual(_drafts(h, INVITED_TG), [])
        self.assertEqual(_invites(h), [], "подделанный payload ссылок не создаёт")


@unittest.skipIf(ACCEPTANCE_IMPORT_ERROR, ACCEPTANCE_IMPORT_ERROR or "")
class IssuanceRightsTests(unittest.TestCase):
    """Ссылку выдаёт только управляющий площадкой; суперадмин — любую."""

    def setUp(self) -> None:
        self.enterContext(patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)))
        self.enterContext(patch("config.SUPERADMIN_CONTACT_URL", None))

    @scenario_test
    async def test_plain_user_cannot_issue(self, h):
        place_id = _seed_place(h)
        _seed_user(h, PLAIN_TG)

        await h.text("/start", user_id=PLAIN_TG)
        await h.callback("manage_places", user_id=PLAIN_TG)
        await h.callback(f"plm_open:{place_id}", user_id=PLAIN_TG)
        self.assertEqual(_callbacks(h, "plm_invite:"), [])
        await h.callback(f"plm_invite:{place_id}", user_id=PLAIN_TG)

        self.assertEqual(_invites(h), [], "обычный пользователь ссылку не выдаёт")
        self.assertNotIn("?start=", h.screen_text())
        # Перевыпуск тоже закрыт.
        await h.callback(f"plm_invite_regen:{place_id}", user_id=PLAIN_TG)
        self.assertEqual(_invites(h), [])

    @scenario_test
    async def test_admin_of_another_place_cannot_issue(self, h):
        foreign_place = _seed_place(h, name="Чужая площадка")
        own_place = _seed_place(h, name="Своя площадка", admin_tg=OTHER_TG)

        await h.text("/start", user_id=OTHER_TG)
        await h.callback("manage_places", user_id=OTHER_TG)
        await h.callback(f"plm_open:{foreign_place}", user_id=OTHER_TG)
        await h.callback(f"plm_invite:{foreign_place}", user_id=OTHER_TG)
        self.assertEqual(_invites(h), [], "чужую ссылку админ не выдаёт")

        await h.callback("plm_back", user_id=OTHER_TG)
        await h.callback(f"plm_open:{own_place}", user_id=OTHER_TG)
        await h.callback(f"plm_invite:{own_place}", user_id=OTHER_TG)
        self.assertEqual(_active_tokens(h), [_token_from_screen(h)])
        self.assertEqual([row.place_id for row in _invites(h)], [own_place])

    @scenario_test
    async def test_superadmin_can_issue_for_any_place(self, h):
        place_id = _seed_place(h)

        token = await _open_invite_screen(h, place_id, user_id=SUPER_TG)

        self.assertEqual(_active_tokens(h), [token])
        await h.text(f"/start {token}", user_id=INVITED_TG)
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(_drafts(h, INVITED_TG)[0].place_id, place_id)


@unittest.skipIf(ACCEPTANCE_IMPORT_ERROR, ACCEPTANCE_IMPORT_ERROR or "")
class IdempotencyAcceptanceTests(unittest.TestCase):
    """Повторный вход в раздел не плодит ссылки; перевыпуск отзывает прежнюю."""

    @scenario_test
    async def test_repeat_visit_reuses_same_link(self, h):
        place_id = _seed_place(h)
        first = await _open_invite_screen(h, place_id)

        await h.callback(f"plm_card:{place_id}", user_id=DEFAULT_USER_ID)
        await h.callback(f"plm_invite:{place_id}", user_id=DEFAULT_USER_ID)
        second = _token_from_screen(h)

        self.assertEqual(first, second)
        self.assertEqual(len(_invites(h)), 1, "повторный вход не создаёт дублей")

    @scenario_test
    async def test_double_tap_on_invite_button(self, h):
        place_id = _seed_place(h)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"plm_invite:{place_id}")
        first = _token_from_screen(h)
        await h.callback(f"plm_invite:{place_id}")  # двойное нажатие

        self.assertEqual(_token_from_screen(h), first)
        self.assertEqual(len(_invites(h)), 1)

    @scenario_test
    async def test_regenerate_keeps_audit_and_revokes_old(self, h):
        place_id = _seed_place(h)
        old_token = await _open_invite_screen(h, place_id)

        await h.callback(f"plm_invite_regen:{place_id}")
        new_token = _token_from_screen(h)

        self.assertNotEqual(old_token, new_token)
        rows = _invites(h)
        self.assertEqual(len(rows), 2, "отозванная ссылка остаётся аудитом")
        self.assertEqual(
            [row.token for row in rows if row.revoked_at is not None], [old_token]
        )
        self.assertEqual(_active_tokens(h), [new_token])
        # Отзыв виден и после рестарта процесса.
        await h.restart()
        await h.callback(f"plm_invite_regen:{place_id}", message_id=2)
        self.assertNotEqual(_token_from_screen(h), new_token)


@unittest.skipIf(ACCEPTANCE_IMPORT_ERROR, ACCEPTANCE_IMPORT_ERROR or "")
class StartRegressionTests(unittest.TestCase):
    """/start без payload — прежний вход; tf_ и тексты не перехвачены."""

    @scenario_test
    async def test_start_without_payload_opens_main_menu(self, h):
        await h.text("/start")

        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertEqual(_callbacks(h), MAIN_MENU_BUTTONS)
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

        # И обычный вход в конструктор по площадке работает как прежде.
        place_id = _seed_place(h)
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))
        await h.callback(f"pl_pick:{place_id}")
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_transfer_payload_is_not_intercepted(self, h):
        await h.text("/start tf_999_deadbeefdeadbeefdeadbeef", user_id=INVITED_TG)
        text = h.screen_text()
        self.assertNotIn("Вопрос 1", text)
        self.assertNotIn("Ссылка не работает", text)
        self.assertIsNone(h.dialog_state(user_id=INVITED_TG, name=CONVERSATION_NAME))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
