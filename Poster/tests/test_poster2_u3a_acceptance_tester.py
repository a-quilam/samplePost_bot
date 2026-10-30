# tests/test_poster2_u3a_acceptance_tester.py
"""
U3a — независимая приёмка требований владельца (poster-tester, без кода этапа).

Проверяется только наблюдаемое поведение через настоящее приложение
(`build_application`), FakeBot и временную SQLite. Реальная `post_bot.db`,
`.env` и сеть Telegram не используются.

Покрытые требования:

1. «Мои кирпичики»: каждый активный кирпичик — ОДНА строка из ДВУХ кнопок:
   слева «Название — [код]» (открывает карточку), справа «📨» (старт
   предложения этого кирпичика). Общей кнопки «📨 Предложить кирпичик» нет.
2. `bp_start` без id (кнопка из старых сообщений): понятный ответ без
   «внутренней ошибки», человек остаётся на хабе.
3. `bp_start:<id>`: сразу шаг «куда предложить», цели «место» и «вся
   система», шаги и «назад» работают.
4. Пустой список: подсказки не ссылаются на несуществующую кнопку;
   создание кирпичика работает.
5. Раскладки: фото-шаг — ровно одна строка «Назад | Пропустить»;
   календарные быстрые дни — 4 в строке; пагинация списков — компактная
   строка ◀️▶️; опасные действия отдельной строкой.
6. Негативы: чужой/архивный id через `bp_start:<id>` — отказ без утечки;
   повторные нажатия не создают дублей.

Запуск из каталога Poster:
    .venv/bin/python -W ignore::DeprecationWarning \
        -m unittest tests.test_poster2_u3a_acceptance_tester -v
"""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

try:
    from brick_proposal_store import create_brick_proposal
    from brick_store import archive_personal_brick, create_personal_brick
    from handlers import wizard_photos
    from handlers.wizard_questions import day_rows
    from models import BrickProposal
    from place_store import create_place
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import calendar_view

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина уходит в skip-сообщение
    IMPORT_ERROR = str(exc)


OWNER = 2_162_001
STRANGER = 2_162_002

HUB_TITLE = "Мои кирпичики"
ROW_ACTION = "📨"
SHARED_LABEL = "📨 Предложить кирпичик"


def rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    """Матрица клавиатуры текущего экрана: строки → (подпись, callback)."""
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
    return list(h.answer_texts())


def proposals(h: Harness) -> int:
    with h.session_factory() as session:
        return len(session.query(BrickProposal).all())


def brick_rows(h: Harness) -> list[list[tuple[str, str | None]]]:
    """Строки списка кирпичиков (у них есть кнопка «📨» или «bk_open»)."""
    found = []
    for row in rows(h):
        if any(
            (data or "").startswith("bp_start:") or (data or "").startswith("bk_open:")
            for _label, data in row
        ):
            found.append(row)
    return found


@unittest.skipIf(
    IMPORT_ERROR is not None, f"интеграционное окружение недоступно: {IMPORT_ERROR}"
)
class U3aAcceptanceCase(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("config.SUPERADMIN_USER_IDS", ())
        patcher.start()
        self.addCleanup(patcher.stop)

    # —— подготовка данных (только доменные функции, без UI) ——

    def user(self, h: Harness, tg: int) -> int:
        with h.session_factory() as session:
            return get_or_create_user(
                session, tg, username=f"u{tg}", full_name=f"Юзер {tg}"
            ).id

    def brick(
        self,
        h: Harness,
        code: str,
        title: str = "Кирпичик",
        *,
        tg: int = OWNER,
        value_type: str = "short_text",
    ):
        self.user(h, tg)
        with h.session_factory() as session:
            return create_personal_brick(
                session,
                actor_telegram_user_id=tg,
                code=code,
                title=title,
                value_type=value_type,
            )

    def place(self, h: Harness, name: str = "Зал приёмки") -> int:
        with h.session_factory() as session:
            return create_place(session, name, city="Город").id

    def archive(self, h: Harness, brick, *, tg: int = OWNER) -> None:
        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=tg,
                brick_id=brick.id,
                expected_revision=brick.revision,
            )

    # ==================================================================
    # Требование 1: строки хаба — пары «название | 📨», без общей кнопки
    # ==================================================================

    @scenario_test
    async def test_1_every_brick_is_exactly_one_row_of_two_buttons(
        self, h: Harness
    ) -> None:
        first = self.brick(h, "пр_первый", "Первый")
        second = self.brick(h, "пр_второй", "Второй")
        await h.text("/bricks", user_id=OWNER)

        bricks = brick_rows(h)
        self.assertEqual(len(bricks), 2, f"не по строке на кирпичик: {rows(h)}")
        # Порядок в списке — дело продукта; проверяется состав пары.
        by_open = {row[0][1]: row for row in bricks}
        for brick, title in (
            (first, "Первый — [пр_первый]"),
            (second, "Второй — [пр_второй]"),
        ):
            row = by_open.get(f"bk_open:{brick.id}")
            self.assertIsNotNone(row, f"нет строки кирпичика {title}: {bricks}")
            assert row is not None
            self.assertEqual(len(row), 2, f"строка не пара: {row}")
            self.assertEqual(row[0][0], title, "слева название и код")
            self.assertEqual(row[1], (ROW_ACTION, f"bp_start:{brick.id}"), "справа 📨")

        # Общей кнопки предложения нет ни в каком виде.
        self.assertNotIn("bp_start", datas(h))
        self.assertNotIn(SHARED_LABEL, [label for label, _d in h.screen_buttons()])

    @scenario_test
    async def test_1_left_button_opens_that_brick_card(self, h: Harness) -> None:
        brick = self.brick(h, "пр_карта", "Карта")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bk_open:{brick.id}", user_id=OWNER)
        screen = h.screen_text()
        self.assertIn("Карта", screen)
        self.assertIn("[пр_карта]", screen)
        self.assertIn("Изменить", " ".join(label for label, _d in h.screen_buttons()))

    # ==================================================================
    # Требование 2: bp_start без id из старых сообщений
    # ==================================================================

    @scenario_test
    async def test_2_legacy_start_without_id_keeps_hub_and_explains(
        self, h: Harness
    ) -> None:
        self.brick(h, "пр_старый", "Старый")
        await h.text("/bricks", user_id=OWNER)
        before = h.screen_text()
        before_rows = rows(h)

        await h.callback("bp_start", user_id=OWNER)

        self.assertEqual(h.screen_text(), before, "экран ушёл из хаба")
        self.assertEqual(rows(h), before_rows, "клавиатура хаба изменилась")
        answers = alerts(h)
        self.assertTrue(answers, "на нажатие не было ответа (спиннер)")
        self.assertFalse(
            [text for text in answers if "внутренняя ошибка" in text.lower()],
            f"показана внутренняя ошибка: {answers}",
        )
        self.assertTrue(
            any("📨" in text or "кирпичик" in text.lower() for text in answers),
            f"подсказка не объясняет, что делать: {answers}",
        )
        self.assertEqual(proposals(h), 0, "предложение создано из устаревшей кнопки")
        self.assertNotIn("bp_target:place", datas(h), "запустился мастер")

    # ==================================================================
    # Требование 3: bp_start:<id> — мастер конкретного кирпичика
    # ==================================================================

    @scenario_test
    async def test_3_start_from_row_skips_brick_choice_and_back_works(
        self, h: Harness
    ) -> None:
        brick = self.brick(h, "пр_вход", "Вход")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)

        screen = h.screen_text()
        self.assertIn("Куда предложить", screen, "нет шага выбора области")
        self.assertIn("Вход", screen, "не показан выбранный кирпичик")
        self.assertIn("шаг 1 из 3", screen)
        self.assertNotIn("Выберите кирпичик", screen, "лишний шаг выбора кирпичика")
        self.assertEqual(
            [
                data
                for _l, data in h.screen_buttons()
                if data and data.startswith("bp_target:")
            ],
            ["bp_target:place", "bp_target:system"],
        )

        await h.callback("bp_back", user_id=OWNER)
        self.assertIn(HUB_TITLE, h.screen_text(), "«назад» не вернул в хаб")
        self.assertNotIn("Куда предложить", h.screen_text())

    @scenario_test
    async def test_3_place_scope_walks_steps_and_back_chain(self, h: Harness) -> None:
        place_id = self.place(h, "Зал шагов")
        brick = self.brick(h, "пр_шаги", "Шаги")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)

        await h.callback("bp_target:place", user_id=OWNER)
        self.assertIn("Выберите место", h.screen_text())
        self.assertIn("шаг 2 из 3", h.screen_text())
        self.assertIn(f"bp_place:{place_id}", datas(h))

        await h.callback(f"bp_place:{place_id}", user_id=OWNER)
        self.assertIn("шаг 3 из 3", h.screen_text())
        self.assertIn("место «Зал шагов»", h.screen_text())
        self.assertIn("bp_send", datas(h))

        # Назад: проверка → места → область → хаб.
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Выберите место", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Куда предложить", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn(HUB_TITLE, h.screen_text())

    @scenario_test
    async def test_3_system_scope_reaches_confirmation_after_two_steps(
        self, h: Harness
    ) -> None:
        brick = self.brick(h, "пр_система", "Система")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)
        await h.callback("bp_target:system", user_id=OWNER)

        screen = h.screen_text()
        self.assertIn("Проверьте предложение", screen)
        self.assertIn("вся система", screen)
        self.assertIn("bp_send", datas(h))
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Куда предложить", h.screen_text(), "«назад» сломан")

    # ==================================================================
    # Требование 4: пустой список
    # ==================================================================

    @scenario_test
    async def test_4_empty_hub_screen_has_no_dead_offer(self, h: Harness) -> None:
        await h.text("/bricks", user_id=OWNER)
        screen = h.screen_text()
        self.assertNotIn("bp_start", datas(h), "кнопка предложения без кирпичиков")
        self.assertNotIn("«📨»", screen, "текст обещает кнопку, которой нет")
        self.assertIn("Создайте первый", screen)

        # Создание из пустого хаба работает.
        await h.callback("bk_create", user_id=OWNER)
        await h.text("Первый кирпич", user_id=OWNER)
        await h.callback("bk_accept_code", user_id=OWNER)
        await h.callback("bk_skip_description", user_id=OWNER)
        await h.callback("bk_type:short_text", user_id=OWNER)
        await h.callback("bk_save", user_id=OWNER)
        self.assertIn("создан", h.screen_text().lower())
        listed = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("bk_open:")
        ]
        self.assertEqual(len(listed), 1, "созданный кирпичик не появился в списке")

    @scenario_test
    async def test_4a_legacy_start_hint_on_empty_hub_points_to_real_actions(
        self, h: Harness
    ) -> None:
        """
        Ожидание владельца (№4): при пустом списке подсказка не ссылается на
        несуществующую кнопку. Устаревшая `bp_start` без id на пустом хабе
        сейчас отвечает «нажмите «📨» справа от названия», хотя кирпичиков
        и кнопки «📨» нет.
        """
        await h.text("/bricks", user_id=OWNER)
        await h.callback("bp_start", user_id=OWNER)
        answers = alerts(h)
        self.assertTrue(answers, "нет ответа на устаревшую кнопку")
        hint = " ".join(answers)
        has_row_button = any(data.startswith("bp_start:") for data in datas(h))
        self.assertTrue(
            "📨" not in hint or has_row_button,
            f"подсказка ссылается на отсутствующую кнопку «📨»: {hint!r}",
        )

    @scenario_test
    async def test_4b_empty_proposals_hint_matches_available_actions(
        self, h: Harness
    ) -> None:
        """
        Ожидание владельца (№4): пустой список «Мои предложения» не должен
        отправлять к кнопке «📨» в строке кирпичика, которой у нового
        пользователя нет.
        """
        await h.text("/bricks", user_id=OWNER)
        await h.callback("bp_mine", user_id=OWNER)
        screen = h.screen_text()
        self.assertIn("Мои предложения", screen)

        await h.callback("bp_back", user_id=OWNER)
        has_row_button = any(data.startswith("bp_start:") for data in datas(h))
        if "«📨»" in screen:
            self.assertTrue(
                has_row_button,
                "«Мои предложения» отправляет к «📨», которой нет ни в одном списке",
            )

    # ==================================================================
    # Требование 5: раскладки
    # ==================================================================

    def test_5_photo_step_is_exactly_one_pair_row(self) -> None:
        markup = wizard_photos.photo_step_keyboard(
            SimpleNamespace(draft_id=11, revision=4)
        )
        matrix = [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]
        self.assertEqual(
            matrix,
            [
                [
                    ("◀️ Назад", "aw_photo_back:11:4"),
                    ("⏭ Пропустить", "aw_photo_none:11:4"),
                ]
            ],
        )

    def test_5_calendar_days_are_four_per_row(self) -> None:
        days = [date(2026, 11, number) for number in range(1, 11)]
        matrix = [[button.text for button in row] for row in day_rows(days, "p")]
        self.assertEqual([len(row) for row in matrix], [4, 4, 2], f"строки: {matrix}")
        self.assertEqual(matrix[0][0], calendar_view.format_quick_day(days[0]))

    @scenario_test
    async def test_5_hub_pagination_is_one_compact_row(self, h: Harness) -> None:
        self.user(h, OWNER)
        for index in range(11):
            self.brick(h, f"пр_стр_{index}", f"Кирпич {index:02d}")
        await h.text("/bricks", user_id=OWNER)

        nav = [
            row
            for row in rows(h)
            if any((data or "").startswith("bk_page:") for _l, data in row)
        ]
        self.assertEqual(len(nav), 1, f"пагинация не одной строкой: {rows(h)}")
        self.assertEqual(
            [label for label, _data in nav[0]], ["▶️"], "на первой странице только ▶️"
        )

        await h.callback("bk_page:8", user_id=OWNER)
        nav = [
            row
            for row in rows(h)
            if any((data or "").startswith("bk_page:") for _l, data in row)
        ]
        self.assertEqual(len(nav), 1)
        self.assertEqual([label for label, _data in nav[0]], ["◀️"])
        # Страница сохраняется при возврате из мастера предложения.
        brick_data = [data for data in datas(h) if data.startswith("bp_start:")][0]
        await h.callback(brick_data, user_id=OWNER)
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn(
            "Показано 9–11 из 11",
            h.screen_text(),
            "потеряна страница списка после возврата из мастера",
        )

    @scenario_test
    async def test_5_proposal_and_place_lists_use_one_compact_nav_row(
        self, h: Harness
    ) -> None:
        brick = self.brick(h, "пр_пагин", "Пагин")
        self.place(h, "Зал пагинации")
        for index in range(9):
            self.place(h, f"Зал {index + 10}")
        # История предложений: 9 записей → пагинация списка предложений.
        with h.session_factory() as session:
            for index in range(9):
                extra = self.brick(h, f"пр_пагин_{index}", f"Пагин {index}")
                create_brick_proposal(
                    session,
                    actor_telegram_user_id=OWNER,
                    source_brick_id=extra.id,
                    scope="system",
                )
        await h.text("/bricks", user_id=OWNER)
        await h.callback("bp_start", user_id=OWNER)  # устаревшая кнопка: не старт
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)
        await h.callback("bp_target:place", user_id=OWNER)

        nav = [
            row
            for row in rows(h)
            if any((data or "").startswith("bp_ppage:") for _l, data in row)
        ]
        self.assertEqual(len(nav), 1, f"пагинация мест не одной строкой: {rows(h)}")
        self.assertEqual([label for label, _d in nav[0]], ["▶️"])
        next_page = nav[0][0][1] or ""
        self.assertTrue(next_page.startswith("bp_ppage:"))
        await h.callback(next_page, user_id=OWNER)
        self.assertIn(
            "Страница 2 из 2", h.screen_text(), "вторая страница мест не открылась"
        )

        # Список предложений: пагинация тоже одной строкой ◀️/▶️.
        await h.callback("bp_back", user_id=OWNER)
        await h.callback("bp_back", user_id=OWNER)
        await h.callback("bp_mine", user_id=OWNER)
        nav = [
            row
            for row in rows(h)
            if any((data or "").startswith("bp_mpage:") for _l, data in row)
        ]
        if len(rows(h)) and "bp_open:" in " ".join(datas(h)):
            self.assertLessEqual(len(nav), 1, f"пагинация предложений: {rows(h)}")

    @scenario_test
    async def test_5_dangerous_actions_are_on_separate_rows(self, h: Harness) -> None:
        brick = self.brick(h, "пр_опасн", "Опасный")
        await h.text("/bricks", user_id=OWNER)
        await h.callback(f"bk_open:{brick.id}", user_id=OWNER)
        card = rows(h)
        archive_row = [
            row for row in card if any("bk_archive" == (data or "") for _l, data in row)
        ]
        self.assertEqual(len(archive_row), 1)
        self.assertEqual(len(archive_row[0]), 1, f"«В архив» не один в строке: {card}")

        await h.callback("bk_archive", user_id=OWNER)
        confirm = rows(h)
        danger = [
            row
            for row in confirm
            if any((data or "") == "bk_archive_confirm" for _l, data in row)
        ]
        self.assertEqual(len(danger), 1)
        self.assertEqual(len(danger[0]), 1, f"необратимое действие рядом: {confirm}")
        # И стоит выше безопасного «назад».
        self.assertLess(
            confirm.index(danger[0]),
            max(
                index
                for index, row in enumerate(confirm)
                if any((data or "") == "bk_back" for _l, data in row)
            ),
        )

        await h.callback("bk_back", user_id=OWNER)
        # «Нет, назад» на подтверждении возвращает в карточку кирпичика,
        # а не сразу в хаб; в хаб ведёт «Назад» из самой карточки.
        self.assertIn("Опасный", h.screen_text())
        self.assertNotIn("bk_archive_confirm", datas(h))
        await h.callback("bk_back", user_id=OWNER)
        self.assertIn(HUB_TITLE, h.screen_text())

    # ==================================================================
    # Требование 6: негативы
    # ==================================================================

    @scenario_test
    async def test_6_foreign_and_archived_ids_are_refused_without_leak(
        self, h: Harness
    ) -> None:
        mine = self.brick(h, "пр_мой", "Мой")
        foreign = self.brick(h, "пр_секрет", "Секрет чужого", tg=STRANGER)
        archived = self.brick(h, "пр_архив", "Архивный")
        self.archive(h, archived)
        await h.text("/bricks", user_id=OWNER)

        for brick, expected_words in (
            (foreign, ("не найден", "архив")),
            (archived, ("не найден", "архив")),
            (SimpleNamespace(id=999_999_999), ("не найден", "архив")),
        ):
            await h.callback(f"bp_start:{brick.id}", user_id=OWNER)
            screen = h.screen_text()
            self.assertNotIn("Секрет чужого", screen, "утечка чужого названия")
            self.assertNotIn("пр_секрет", screen, "утечка чужого кода")
            self.assertIn("Мои кирпичики", screen, "потерян хаб")
            self.assertNotIn("bp_target:place", datas(h), "мастер всё же запустился")
            self.assertTrue(
                any(word in screen.lower() for word in expected_words),
                f"нет понятного отказа: {screen[:120]!r}",
            )
            self.assertEqual(proposals(h), 0, "создано предложение по чужому id")
            self.assertIn(f"bp_start:{mine.id}", datas(h))

    @scenario_test
    async def test_6_double_start_and_double_send_create_no_duplicates(
        self, h: Harness
    ) -> None:
        brick = self.brick(h, "пр_дубль", "Дубль")
        await h.text("/bricks", user_id=OWNER)

        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)
        first_screen = h.screen_text()
        await h.callback(f"bp_start:{brick.id}", user_id=OWNER)  # второе нажатие
        self.assertEqual(h.screen_text(), first_screen, "второй экран вместо одного")
        self.assertEqual(proposals(h), 0)

        await h.callback("bp_target:system", user_id=OWNER)
        await h.callback("bp_send", user_id=OWNER)
        await h.callback("bp_send", user_id=OWNER)
        await h.callback("bp_send", user_id=OWNER)
        self.assertEqual(proposals(h), 1, "повторное «Отправить» создало дубли")
        self.assertIn("Предложение отправлено", h.screen_text())


if __name__ == "__main__":
    unittest.main()
