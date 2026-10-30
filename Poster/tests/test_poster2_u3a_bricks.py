# tests/test_poster2_u3a_bricks.py
"""
Poster 2.2 U3a, дефект №8: «предложить кирпичик» только из строки кирпичика.

Проверяется поведение хаба «Мои кирпичики»:

- общих кнопок «📨 Предложить кирпичик» нет; у каждой строки активного
  кирпичика есть компактная «📨» с `bp_start:<id>`;
- архивный кирпичик кнопки не получает;
- устаревшая общая кнопка `bp_start` без id из старого сообщения не даёт
  «внутренней ошибки»: приходит подсказка, хаб перерисовывается, мастер
  не запускается и состояние не меняется;
- `bp_start:<id>` сразу открывает экран области (шаг выбора кирпичика
  пропущен), а «назад» возвращает в хаб;
- при пустом списке предложения нет, а подсказка не ссылается на
  несуществующую кнопку «📨»: и в алерте устаревшей кнопки, и в пустом
  «📤 Мои предложения» ведёт к «➕ Создать кирпичик»;
- чужие и архивные id через `bp_start:<id>` безопасны.

Только настоящее приложение (`build_application`), FakeBot и временная
SQLite: реальная `post_bot.db` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import select

try:
    from brick_proposal_store import PROPOSAL_STATUS_PENDING, create_brick_proposal
    from brick_store import archive_personal_brick, create_personal_brick
    from handlers.bricks import (
        PROPOSAL_ROW_ONLY_HINT,
        PROPOSAL_STEPS,
    )
    from models import BrickProposal
    from place_store import create_place
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OWNER_TG = 2_160_201
OTHER_TG = 2_160_202

HUB_TITLE = "Мои кирпичики"
CRUMB = "🧱 Кирпичики → 📨 Предложения"
EMPTY_HUB_HINT = "Чтобы предложить кирпичик"


def _datas(h: Harness) -> list[str]:
    return [data or "" for _label, data in h.screen_buttons()]


def _labels(h: Harness) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _alerts(h: Harness) -> list[str]:
    out: list[str] = []
    for record in h.answers():
        text = record.get("text")
        if text is None:
            args = tuple(record.get("args") or ())
            text = args[0] if args and isinstance(args[0], str) else None
        if text:
            out.append(text)
    return out


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrickRowEntryTests(unittest.TestCase):
    """Хаб и вход в предложение: одна дорога — строка кирпичика."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", ()),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- фикстуры ---------------------------------------------------------

    def ensure_user(self, h: Harness, telegram_user_id: int) -> int:
        with h.session_factory() as session:
            user = get_or_create_user(
                session,
                telegram_user_id,
                username=f"user{telegram_user_id}",
                full_name=f"Пользователь {telegram_user_id}",
            )
            return user.id

    def make_personal(
        self,
        h: Harness,
        code: str,
        *,
        title: str = "Личный кирпичик",
        owner_tg: int = OWNER_TG,
    ):
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            return create_personal_brick(
                session,
                actor_telegram_user_id=owner_tg,
                code=code,
                title=title,
                value_type="short_text",
            )

    def make_place(self, h: Harness, name: str) -> int:
        with h.session_factory() as session:
            place = create_place(session, name, city="Тестовый город")
            return place.id

    def archive(self, h: Harness, brick) -> None:
        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=brick.id,
                expected_revision=brick.revision,
            )

    def proposal_count(self, h: Harness) -> int:
        with h.session_factory() as session:
            return len(session.scalars(select(BrickProposal)).all())

    async def open_hub(self, h: Harness, user_id: int = OWNER_TG) -> None:
        await h.text("/bricks", user_id=user_id)

    # --- матрица хаба -----------------------------------------------------

    @scenario_test
    async def test_hub_rows_have_compact_action_and_no_shared_button(
        self, h: Harness
    ) -> None:
        first = self.make_personal(h, "строка_1", title="Первый")
        second = self.make_personal(h, "строка_2", title="Второй")
        await self.open_hub(h)

        self.assertIn(HUB_TITLE, h.screen_text())
        # Общей кнопки «предложить» больше нет — ни без id, ни с ним.
        self.assertNotIn("bp_start", _datas(h))

        rows = _rows(h)
        brick_rows = [
            row for row in rows if any(d and d.startswith("bp_start:") for _l, d in row)
        ]
        self.assertEqual(
            [[data for _label, data in row] for row in brick_rows],
            [
                [f"bk_open:{first.id}", f"bp_start:{first.id}"],
                [f"bk_open:{second.id}", f"bp_start:{second.id}"],
            ],
        )
        for row in brick_rows:
            self.assertEqual(len(row), 2, f"строка кирпичика не пара: {row}")
            self.assertEqual(row[1][0], "📨")
            self.assertIn(row[0][0], ("Первый — [строка_1]", "Второй — [строка_2]"))

        # Базовые выходы хаба не потерялись.
        self.assertIn("bk_create", _datas(h))
        self.assertIn("bp_mine", _datas(h))
        self.assertIn("main_menu", _datas(h))

    @scenario_test
    async def test_archived_brick_row_has_no_propose_button(self, h: Harness) -> None:
        active = self.make_personal(h, "активный_8", title="Активный")
        archived = self.make_personal(h, "архивный_8", title="Архивный")
        self.archive(h, archived)
        await self.open_hub(h)

        datas = _datas(h)
        self.assertIn(f"bp_start:{active.id}", datas)
        self.assertNotIn(f"bp_start:{archived.id}", datas)
        self.assertNotIn(f"bk_open:{archived.id}", datas)
        self.assertIn(f"bk_open:{active.id}", datas)

    @scenario_test
    async def test_legacy_shared_button_is_redirected_without_error(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, "легаси_8", title="Легаси")
        await self.open_hub(h)
        text_before = h.screen_text()
        screens_before = len(h.screens())
        calls_before = len(h.calls)

        await h.callback("bp_start", user_id=OWNER_TG)

        # Никакой «внутренней ошибки»: подсказка выбирать строку.
        self.assertFalse(
            [text for text in h.answer_texts() if "внутренняя ошибка" in text.lower()],
            "legacy bp_start привёл к внутренней ошибке",
        )
        self.assertIn(PROPOSAL_ROW_ONLY_HINT, _alerts(h))
        # Хаб остаётся хабом: перерисовка не обязана менять сообщение
        # (Telegram отвечает «message is not modified» на идентичный текст).
        self.assertGreaterEqual(len(h.screens()), screens_before)
        self.assertEqual(h.screen_text(), text_before)
        self.assertNotIn(CRUMB, h.screen_text())
        self.assertNotIn("Куда предложить", h.screen_text())
        self.assertNotIn("bp_target:place", _datas(h))
        # Ни одного лишнего сообщения и ни одного предложения.
        new_calls = h.calls[calls_before:]
        self.assertFalse(
            [call for call in new_calls if call["type"] == "message"],
            "legacy bp_start отправил лишнее сообщение",
        )
        self.assertEqual(self.proposal_count(h), 0)

        # После подсказки обычный вход строкой работает.
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)
        self.assertIn("Куда предложить", h.screen_text())
        self.assertIn(f"шаг 1 из {PROPOSAL_STEPS}", h.screen_text())

    @scenario_test
    async def test_row_button_opens_scope_step_without_brick_choice(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, "строка_вход_8", title="Вход")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)

        text = h.screen_text()
        self.assertIn(CRUMB, text)
        self.assertIn("Куда предложить", text)
        self.assertNotIn("Выберите кирпичик", text)
        self.assertIn("Вход", text)
        datas = _datas(h)
        self.assertIn("bp_target:place", datas)
        self.assertIn("bp_target:system", datas)
        self.assertIn("bp_back", datas)

        # «Назад» из области ведёт в хаб, а не в повторный выбор.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertIn(f"bp_start:{source.id}", _datas(h))

    @scenario_test
    async def test_row_button_place_scope_reaches_preview_and_sends(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал строки 8")
        source = self.make_personal(h, "строка_место_8", title="Место")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:place", user_id=OWNER_TG)
        self.assertIn(f"bp_place:{place_id}", _datas(h))
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        self.assertIn("Проверьте предложение", h.screen_text())
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIn("Предложение отправлено", h.screen_text())
        with h.session_factory() as session:
            row = session.scalar(select(BrickProposal))
            assert row is not None
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)
            self.assertEqual(row.scope, "place")
            self.assertEqual(row.place_id, place_id)
            self.assertEqual(row.source_brick_id, source.id)

    @scenario_test
    async def test_row_button_system_scope_reaches_preview_and_sends(
        self, h: Harness
    ) -> None:
        source = self.make_personal(h, "строка_система_8", title="Система")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)
        await h.callback("bp_target:system", user_id=OWNER_TG)
        self.assertIn("вся система", h.screen_text())
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIn("Предложение отправлено", h.screen_text())
        with h.session_factory() as session:
            row = session.scalar(select(BrickProposal))
            assert row is not None
            self.assertEqual(row.scope, "system")
            self.assertIsNone(row.place_id)

    @scenario_test
    async def test_row_button_for_foreign_and_unknown_id_is_safe(
        self, h: Harness
    ) -> None:
        self.make_personal(h, "свой_8", title="Свой")
        foreign = self.make_personal(h, "чужой_8", owner_tg=OTHER_TG)
        await self.open_hub(h)

        await h.callback(f"bp_start:{foreign.id}", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertIn("не найден", h.screen_text().lower())
        self.assertNotIn("Куда предложить", h.screen_text())

        await h.callback("bp_start:999999", user_id=OWNER_TG)
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertIn("не найден", h.screen_text().lower())
        self.assertEqual(self.proposal_count(h), 0)

    @scenario_test
    async def test_empty_hub_does_not_offer_proposal_and_text_matches_buttons(
        self, h: Harness
    ) -> None:
        await self.open_hub(h)
        text = h.screen_text()
        datas = _datas(h)

        self.assertIn(HUB_TITLE, text)
        self.assertNotIn("bp_start", datas)
        self.assertNotIn(EMPTY_HUB_HINT, text, "подсказка про несуществующую кнопку")
        self.assertIn("Создайте первый", text)
        self.assertIn("bk_create", datas)

        # Legacy-кнопка при пустом списке: подсказка, хаб, никакого мастера.
        await h.callback("bp_start", user_id=OWNER_TG)
        self.assertNotIn(PROPOSAL_ROW_ONLY_HINT, _alerts(h))
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertNotIn("Сначала создайте личный кирпичик", h.screen_text())
        self.assertEqual(self.proposal_count(h), 0)

    @scenario_test
    async def test_legacy_shared_button_on_empty_hub_points_to_create(
        self, h: Harness
    ) -> None:
        """U3a: на пустом хабе кнопки «📨» нет — подсказка ведёт к созданию."""
        await self.open_hub(h)
        self.assertNotIn("bp_start", _datas(h))

        await h.callback("bp_start", user_id=OWNER_TG)

        alerts = _alerts(h)
        self.assertTrue(alerts, "нет ответа на устаревшую кнопку")
        hint = " ".join(alerts)
        self.assertIn("Создать кирпичик", hint)
        self.assertNotIn("📨", hint, "подсказка о несуществующей кнопке «📨»")
        self.assertIn("bk_create", _datas(h))
        self.assertIn(HUB_TITLE, h.screen_text())
        self.assertNotIn("Куда предложить", h.screen_text())
        self.assertEqual(self.proposal_count(h), 0)

    @scenario_test
    async def test_my_proposals_empty_state_points_to_row_button(
        self, h: Harness
    ) -> None:
        self.make_personal(h, "без_предложений_8", title="Без предложений")
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)

        text = h.screen_text()
        self.assertIn("Мои предложения", text)
        self.assertIn("«📨» в строке кирпичика", text)
        self.assertNotIn("Предложить кирпичик", text)
        self.assertEqual(_datas(h).count("bp_start"), 0)

    @scenario_test
    async def test_my_proposals_without_bricks_points_to_create(
        self, h: Harness
    ) -> None:
        """U3a: без кирпичиков «📨» в списке нет — подсказка ведёт к созданию."""
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)

        text = h.screen_text()
        self.assertIn("Мои предложения", text)
        self.assertIn("Создайте кирпичик кнопкой «➕ Создать кирпичик»", text)
        self.assertNotIn("«📨»", text, "подсказка о несуществующей кнопке «📨»")
        self.assertEqual(self.proposal_count(h), 0)

    @scenario_test
    async def test_hub_with_proposal_keeps_history_entry(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал истории 8")
        source = self.make_personal(h, "история_8", title="История")
        self.ensure_user(h, OWNER_TG)
        with h.session_factory() as session:
            create_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                source_brick_id=source.id,
                scope="place",
                place_id=place_id,
            )
        await self.open_hub(h)
        self.assertIn("bp_mine", _datas(h))
        # Кнопка строки ведёт в мастер, история — отдельным входом.
        self.assertIn(f"bp_start:{source.id}", _datas(h))
        self.assertNotIn("bp_start", _datas(h))


if __name__ == "__main__":
    unittest.main()
