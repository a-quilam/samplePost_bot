# tests/test_poster2_u3a_bricks_tester.py
"""
U3a №8: независимые проверки тестировщика — по требованию владельца,
без опоры на константы и структуру тестов разработчика.

Проверяется только наблюдаемое поведение: что человек видит и нажимает.

1. На хабе «Мои кирпичики» нет кнопки «предложить» вне строки кирпичика:
   каждая активная строка состоит из названия и компактной кнопки «📨».
2. Архивных и чужих кирпичиков в списке предложения нет.
3. Кнопка «📨» строки сразу открывает «Куда предложить?» — без повторного
   выбора кирпичика; «назад» возвращает в хаб.
4. Старая кнопка из истории чата (без id) не ломает раздел: появляется
   подсказка, экран остаётся хабом, предложение не создаётся.
5. При пустом списке кирпичиков предложения нет и текст не обещает кнопку,
   которой нет.
6. Пустой список «Мои предложения» отправляет к кнопке в строке кирпичика.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from brick_store import archive_personal_brick, create_personal_brick
    from models import BrickProposal
    from place_store import create_place
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина уходит в skip-сообщение
    IMPORT_ERROR = str(exc)


OWNER = 2_160_301
STRANGER = 2_160_302

ROW_MARK = "📨"


def rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    record = h.screen()
    assert record is not None, "экрана не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def datas(h: Harness) -> list[str]:
    return [data or "" for _label, data in h.screen_buttons()]


def alerts(h: Harness) -> list[str]:
    out: list[str] = []
    for record in h.answers():
        args = tuple(record.get("args") or ())
        text = record.get("text") or (
            args[0] if args and isinstance(args[0], str) else None
        )
        if text:
            out.append(str(text))
    return out


def proposals(h: Harness) -> int:
    with h.session_factory() as session:
        return len(session.query(BrickProposal).all())


@unittest.skipIf(
    IMPORT_ERROR is not None, f"интеграционное окружение недоступно: {IMPORT_ERROR}"
)
class HubRowOnlyTesterCase(unittest.TestCase):
    """Точка входа одна — строка кирпичика."""

    def setUp(self) -> None:
        patcher = patch("config.SUPERADMIN_USER_IDS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    def user(self, h: Harness, tg: int) -> int:
        with h.session_factory() as session:
            return get_or_create_user(
                session, tg, username=f"u{tg}", full_name=f"Юзер {tg}"
            ).id

    def brick(self, h: Harness, code: str, title: str = "Кирпичик", tg: int = OWNER):
        self.user(h, tg)
        with h.session_factory() as session:
            return create_personal_brick(
                session,
                actor_telegram_user_id=tg,
                code=code,
                title=title,
                value_type="short_text",
            )

    @scenario_test
    async def test_every_active_row_is_a_name_plus_button_pair(
        self, h: Harness
    ) -> None:
        made = [
            self.brick(h, "т_строка_1", "Первый"),
            self.brick(h, "т_строка_2", "Второй"),
        ]
        await h.text("/bricks", user_id=OWNER)

        found = [
            row
            for row in rows(h)
            if any(d and d.startswith("bp_start:") for _l, d in row)
        ]
        self.assertEqual(len(found), len(made), f"строки: {rows(h)}")
        for row, brick in zip(found, made, strict=True):
            self.assertEqual(len(row), 2, f"не пара: {row}")
            self.assertEqual(row[1], (ROW_MARK, f"bp_start:{brick.id}"))
            self.assertTrue(row[0][0].startswith(brick.title), row[0][0])
        # Ни одной кнопки предложения вне строк.
        self.assertNotIn("bp_start", datas(h))

    @scenario_test
    async def test_archived_and_foreign_bricks_are_not_rows(self, h: Harness) -> None:
        mine = self.brick(h, "т_мой", "Мой")
        gone = self.brick(h, "т_архив", "В архив")
        foreign = self.brick(h, "т_чужой", "Чужой", tg=STRANGER)
        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER,
                brick_id=gone.id,
                expected_revision=gone.revision,
            )
        await h.text("/bricks", user_id=OWNER)
        listed = " ".join(datas(h))
        self.assertIn(f"bp_start:{mine.id}", listed)
        self.assertNotIn(f"bp_start:{gone.id}", listed)
        self.assertNotIn(f"bp_start:{foreign.id}", listed)

    @scenario_test
    async def test_row_button_skips_brick_choice_and_back_returns_hub(
        self, h: Harness
    ) -> None:
        brick = self.brick(h, "т_вход", "Вход")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)

        screen = h.screen_text()
        self.assertIn("Куда предложить", screen)
        self.assertNotIn("Выберите кирпичик", screen)
        self.assertIn("Вход", screen)

        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertNotIn("Куда предложить", h.screen_text())

    @scenario_test
    async def test_full_path_from_row_creates_one_proposal(self, h: Harness) -> None:
        with h.session_factory() as session:
            place_id = create_place(session, "Зал тестера", city="Город").id
        brick = self.brick(h, "т_путь", "Путь")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)
        await h.callback("bp_target:place", user_id=OWNER)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER)
        self.assertIn("Проверьте предложение", h.screen_text())
        await h.callback("bp_send", user_id=OWNER)
        self.assertEqual(proposals(h), 1)
        self.assertIn("Предложение отправлено", h.screen_text())

    @scenario_test
    async def test_old_button_from_chat_history_is_harmless(self, h: Harness) -> None:
        self.brick(h, "т_старая", "Старая")
        await h.text("/bricks", user_id=OWNER)
        before = h.screen_text()

        await h.callback("bp_start", user_id=OWNER)

        self.assertEqual(h.screen_text(), before, "экран ушёл из хаба")
        self.assertTrue(alerts(h), "нет ответа на устаревшую кнопку")
        self.assertFalse(
            [text for text in alerts(h) if "внутренняя ошибка" in text.lower()]
        )
        self.assertEqual(proposals(h), 0)
        self.assertNotIn("bp_target:place", datas(h))

    @scenario_test
    async def test_empty_hub_promises_nothing_it_cannot_do(self, h: Harness) -> None:
        await h.text("/bricks", user_id=OWNER)
        screen = h.screen_text()
        self.assertNotIn("bp_start", datas(h))
        self.assertNotIn("«📨» в его строке", screen)
        self.assertIn("Создайте первый", screen)

        await h.callback("bp_start", user_id=OWNER)
        self.assertTrue(alerts(h))
        self.assertNotIn("Куда предложить", h.screen_text())
        self.assertEqual(proposals(h), 0)

    @scenario_test
    async def test_empty_proposal_list_sends_to_row_button(self, h: Harness) -> None:
        self.brick(h, "т_история", "История")
        await h.text("/bricks", user_id=OWNER)
        await h.callback("bp_mine", user_id=OWNER)
        screen = h.screen_text()
        self.assertIn("Мои предложения", screen)
        self.assertIn("«📨»", screen)
        self.assertIn("строке кирпичика", screen)
        self.assertNotIn("Предложить кирпичик", screen)


if __name__ == "__main__":
    unittest.main()
