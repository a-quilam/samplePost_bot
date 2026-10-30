# tests/test_poster2_u4b_defects_tester.py
"""
Poster 2.2 U4b: регрессия исправленных дефектов независимой приёмки (U4b-fix).

Оба сценария были найдены приёмкой (красные тесты под `expectedFailure`) и
исправлены; теперь это обычные регрессионные тесты.

Дефект 1 (п.6 приёмки «двойной /start — без дублей черновиков»).
    Повторный `/start` с тем же токеном создавал ВТОРОЙ черновик той же
    площадки и заново открывал «Вопрос 1» с пустыми значениями: первый
    черновик (с уже введёнными данными) оставался в «Мои анонсы» отдельной
    строкой. Причина: `open_invite_payload` → `start_place_announcement_pick`
    каждый раз генерировал новый `idempotency_key` в `_fresh_ui()`.
    Исправление: ссылка на площадку продолжает её незавершённый черновик
    (`resume_place_announcement`), сохраняя введённые данные.

Дефект 2 (п.4 приёмки «без внутренней ошибки»).
    Если права управляющего отозваны между проверкой в
    `handle_manage_invite` и повторной проверкой в `ensure_invite` при
    отрисовке экрана, пользователь получал «⚠️ Произошла внутренняя
    ошибка»: `PlaceInvitePermissionError` не классифицирован в
    `errors.expected_domain_errors()`. Исправление: оба исключения слоя
    ссылок попадают в доменный fallback.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

try:
    from announcement_store import list_announcement_drafts
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import PlaceAdmin, PlaceInviteLink, utcnow
    from place_store import create_place
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    DEFECTS_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    DEFECTS_IMPORT_ERROR = str(exc)

INVITED_TG = 9_970_002


def _seed_place(h, *, name: str = "Дом культуры") -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, DEFAULT_USER_ID, username="admin")
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


def _invites(h) -> list:
    with h.session_factory() as session:
        return list(session.scalars(select(PlaceInviteLink)).all())


def _drafts(h, telegram_user_id: int):
    with h.session_factory() as session:
        return list(
            list_announcement_drafts(session, actor_telegram_user_id=telegram_user_id)
        )


def _token_from_screen(h) -> str:
    text = h.screen_text()
    marker = "?start="
    if marker not in text:
        raise AssertionError(f"В экране нет ссылки: {text!r}")
    return text.split(marker, 1)[1].split()[0].strip()


async def _open_invite_screen(h, place_id: int) -> str:
    await h.text("/start")
    await h.callback("manage_places")
    await h.callback(f"plm_open:{place_id}")
    await h.callback(f"plm_invite:{place_id}")
    return _token_from_screen(h)


@unittest.skipIf(DEFECTS_IMPORT_ERROR, DEFECTS_IMPORT_ERROR or "")
class DuplicateDraftOnSecondStartTests(unittest.TestCase):
    """Дефект 1: повторный /start с тем же токеном плодил черновики."""

    @scenario_test
    async def test_double_start_same_token_does_not_duplicate_draft(self, h):
        """
        Воспроизведение:
          1. админ выдаёт ссылку на площадку;
          2. приглашённый открывает её и вводит значение («Мой концерт»);
          3. Telegram повторяет update или человек нажимает ту же ссылку снова.

        Ожидание (и оно же закрепляется тестом): конструктор продолжает ТОТ
        ЖЕ черновик этой площадки — ровно одна запись в «Мои анонсы»,
        введённое значение на экране.
        """
        place_id = _seed_place(h, name="Дом культуры")
        token = await _open_invite_screen(h, place_id)

        await h.text(f"/start {token}", user_id=INVITED_TG)
        await h.text("Мой концерт", user_id=INVITED_TG)
        first = _drafts(h, INVITED_TG)
        self.assertEqual(len(first), 1)
        first_id = first[0].draft_id

        await h.text(f"/start {token}", user_id=INVITED_TG)

        drafts = _drafts(h, INVITED_TG)
        self.assertEqual(
            [draft.draft_id for draft in drafts],
            [first_id],
            "повторный /start с тем же токеном не должен создавать второй черновик",
        )
        self.assertNotIn(
            "Значение пока не задано",
            h.screen_text(),
            "введённое значение не должно теряться при повторном открытии ссылки",
        )
        self.assertIn("Текущее значение: Мой концерт", h.screen_text())


@unittest.skipIf(DEFECTS_IMPORT_ERROR, DEFECTS_IMPORT_ERROR or "")
class LostRightsDuringRenderTests(unittest.TestCase):
    """Дефект 2: гонка прав давала «внутреннюю ошибку» вместо отказа."""

    @scenario_test
    async def test_lost_rights_do_not_show_internal_error(self, h):
        """
        Воспроизведение: администратор открывает карточку площадки, права
        отзывают ровно между проверкой в `handle_manage_invite` и повторной
        проверкой в `ensure_invite` при отрисовке экрана ссылки.

        Ожидание (и оно же закрепляется тестом): понятный отказ (alert
        доменного fallback'а), ссылка не создаётся, никакой «внутренней
        ошибки».
        """
        import place_invite_store

        place_id = _seed_place(h)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        with patch.object(
            place_invite_store, "can_manage_place", lambda *a, **k: False
        ):
            await h.callback(f"plm_invite:{place_id}")

        replies = " ".join(h.reply_texts())
        self.assertNotIn("внутренняя ошибка", replies.lower())
        self.assertEqual(_invites(h), [], "ссылка не должна создаваться без прав")
        self.assertTrue(h.answer_texts(), "пользователь должен получить отказ в alert")
        self.assertNotIn("?start=", h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))


@unittest.skipIf(DEFECTS_IMPORT_ERROR, DEFECTS_IMPORT_ERROR or "")
class ExpectedDomainErrorsTests(unittest.TestCase):
    """Классификация исключений слоя ссылок: все — доменные, не сбои."""

    def test_all_place_invite_errors_are_domain_errors(self):
        import errors
        import place_invite_store

        expected = errors.expected_domain_errors()
        for cls in (
            place_invite_store.PlaceInviteError,
            place_invite_store.PlaceInvitePermissionError,
        ):
            self.assertIn(cls, expected, f"{cls.__name__} не в доменном fallback")
            self.assertTrue(
                errors.is_expected_domain_error(cls("проверка")),
                f"{cls.__name__} должен обрабатываться доменным fallback'ом",
            )

    def test_permission_error_is_not_internal_error_text(self):
        import errors

        self.assertNotIn("внутренняя", errors.domain_notice_text().lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
