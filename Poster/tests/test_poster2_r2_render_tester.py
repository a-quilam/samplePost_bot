# tests/test_poster2_r2_render_tester.py
"""
R2 (Poster 2.1) — независимая проверка эталона BUILTIN v3 и выдачи
стартового шаблона.

Написано тестировщиком по требованиям владельца (Часть III), без опоры на
внутренние константы продукта: эталон анонса задан здесь ЛИТЕРАЛЬНО, а
сравнение идёт побайтово по реальному HTML, который уходит в Telegram.

Проверяется:
- стартовый шаблон нового пользователя (/start) рендерится ровно в эталон:
  жирный заголовок, пустая строка, жирная строка «дата // время», пустая
  строка, жирная КЛИКАБЕЛЬНАЯ площадка, нежирный адрес следующей строкой,
  пустая строка, описание, пустая строка, контакт;
- пропущенные значения: разделитель `//` сохраняется буквально (и в дате,
  и во времени, и в анонсе «только заголовок»);
- полностью пустой результат → ровно «.» (и заглушка не застревает в
  значениях кирпичиков);
- сквозной путь анонса по стандарту места даёт тот же самый текст.

Реальная `post_bot.db`, `.env`, сеть и Telegram не используются: временная
SQLite (tests.integration_harness) и доменные сервисы на ней.
"""

from __future__ import annotations

import unittest

from sqlalchemy import select

try:
    from announcement_store import (
        build_announcement_preview,
        choose_announcement_template,
        confirm_announcement_place,
        save_announcement_value,
        start_announcement_wizard,
    )
    from brick_store import get_system_brick
    from models import AnnouncementValue
    from place_store import create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        list_personal_templates,
        render_template_version,
    )
    from template_types import INITIAL_PERSONAL_TEMPLATE_NAME
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)


# Эталон владельца для встроенного стандарта v4 (Poster 2.0 U1a): ровно этот
# текст должен оказаться в Telegram. Жирный заголовок, пустая строка, жирное
# «дата // время» и сразу под ним жирная кликабельная площадка (без пустой
# строки), нежирный адрес строкой ниже, пустая строка, описание, пустая
# строка, контакт.
OWNER_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "@contact"
)

# Ожидаемая структура по строкам: (номер строки, HTML внутри строки,
# жирность строки целиком).
OWNER_LINES = (
    (0, "Летний концерт", True),
    (1, "", None),
    (2, "12.07.2027 // 19:00", True),
    (3, '<a href="https://example.com/place">Дом культуры</a>', True),
    (4, "ул. Ленина, 1", False),
    (5, "", None),
    (6, "Большой зал, вход свободный.", False),
    (7, "", None),
    (8, "@contact", False),
)


def _values(session) -> dict[int, str]:
    """Значения ровно по восьми системным кирпичикам эталона."""
    raw = {
        "название": "Летний концерт",
        "дата": "12.07.2027",
        "время_начала": "19:00",
        "место": "Дом культуры",
        "ссылка_места": "https://example.com/place",
        "адрес": "ул. Ленина, 1",
        "описание": "Большой зал, вход свободный.",
        "контакт": "@contact",
    }
    result: dict[int, str] = {}
    for code, value in raw.items():
        brick = get_system_brick(session, code)
        if brick is None:  # pragma: no cover — системный реестр обязателен
            raise AssertionError(f"нет системного кирпичика {code!r}")
        result[brick.id] = value
    return result


def _render_personal(h, session, template_id: int, values: dict[int, str]) -> str:
    return render_template_version(
        session,
        actor_telegram_user_id=DEFAULT_USER_ID,
        template_id=template_id,
        values_by_id=values,
    )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class OwnerReferenceRenderTests(unittest.TestCase):
    """Стартовый шаблон рендерится ровно в эталон владельца."""

    @scenario_test
    async def test_new_user_template_renders_exactly_owner_reference(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            rows = list_personal_templates(session, DEFAULT_USER_ID)
            self.assertEqual(len(rows), 1, "у нового пользователя один шаблон")
            template = rows[0]
            self.assertEqual(template.name, INITIAL_PERSONAL_TEMPLATE_NAME)
            self.assertEqual(template.name, "Дефолтный шаблон")

            rendered = _render_personal(
                h, session, template.template_id, _values(session)
            )
        self.assertEqual(rendered, OWNER_REFERENCE)

    @scenario_test
    async def test_owner_reference_line_structure(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            rendered = _render_personal(
                h, session, template.template_id, _values(session)
            )
        lines = rendered.split("\n")
        self.assertEqual(len(lines), len(OWNER_LINES), f"строки: {lines!r}")
        for index, text, bold in OWNER_LINES:
            with self.subTest(line=index):
                line = lines[index]
                if bold is None:
                    self.assertEqual(line, "")
                    continue
                if bold:
                    self.assertTrue(
                        line.startswith("<b>") and line.endswith("</b>"),
                        f"строка {index} должна быть целиком жирной: {line!r}",
                    )
                    self.assertEqual(
                        line[len("<b>") : -len("</b>")],
                        text,
                        f"видимый текст строки {index}",
                    )
                else:
                    self.assertNotIn(
                        "<b>", line, f"строка {index} не должна быть жирной"
                    )
                    self.assertNotIn("</b>", line)
                    self.assertEqual(line, text)
        # Кликабельность площадки: гиперссылка внутри жирной строки.
        self.assertIn('<a href="https://example.com/place">Дом культуры</a>', lines[3])
        # Никакой «сырой» разметки шаблона в готовом тексте.
        self.assertNotIn("**", rendered)
        self.assertNotIn("[", rendered)
        self.assertNotIn("]", rendered)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SkippedBricksTests(unittest.TestCase):
    """Пропущенные значения не ломают эталон и не стирают `//`."""

    @scenario_test
    async def test_missing_date_keeps_literal_separator(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            values = _values(session)
            date_id = get_system_brick(session, "дата").id
            values.pop(date_id)
            rendered = _render_personal(h, session, template.template_id, values)
            self.assertIn("<b> // 19:00</b>", rendered)
        # v4: строка «дата // время» идёт через одну после заголовка (индекс 2).
        self.assertIn(" // ", rendered.split("\n")[2])

    @scenario_test
    async def test_missing_start_time_keeps_literal_separator(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            values = _values(session)
            time_id = get_system_brick(session, "время_начала").id
            values.pop(time_id)
            rendered = _render_personal(h, session, template.template_id, values)
            self.assertIn("<b>12.07.2027 // </b>", rendered)

    @scenario_test
    async def test_only_title_is_not_empty_and_keeps_separator(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            title_id = get_system_brick(session, "название").id
            rendered = _render_personal(
                h, session, template.template_id, {title_id: "Только заголовок"}
            )
        self.assertTrue(rendered.startswith("<b>Только заголовок</b>"))
        self.assertIn("//", rendered)
        self.assertNotEqual(rendered, ".")

    @scenario_test
    async def test_missing_address_keeps_place_line_clickable(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            values = _values(session)
            values.pop(get_system_brick(session, "адрес").id)
            rendered = _render_personal(h, session, template.template_id, values)
        self.assertIn(
            '<b><a href="https://example.com/place">Дом культуры</a></b>', rendered
        )
        self.assertIn("Большой зал, вход свободный.", rendered)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EmptyAnnouncementTests(unittest.TestCase):
    """Пустой результат — ровно «.»; заглушка не попадает в данные."""

    @scenario_test
    async def test_empty_personal_announcement_is_single_dot(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="r2-render")
            # Единственный кирпичик шаблона необязателен и не заполнен.
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Пустой",
                header="",
                body="[описание]",
                footer="",
            )
            session.commit()
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                origin="personal",
                template_id=template.template_id,
            )
            preview = build_announcement_preview(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft.draft_id
            )
            self.assertEqual(preview.html, ".")
            stored = [
                row
                for row in session.scalars(
                    select(AnnouncementValue).where(
                        AnnouncementValue.draft_id == draft.draft_id
                    )
                ).all()
            ]
            self.assertEqual(stored, [], "пустой анонс не сохраняет значений")

    @scenario_test
    async def test_empty_announcement_after_clearing_value_becomes_dot(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="r2-render")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Очищаемый",
                header="",
                body="[описание]",
                footer="",
            )
            session.commit()
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                origin="personal",
                template_id=template.template_id,
            )
            brick = get_system_brick(session, "описание")
            save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                brick_id=brick.id,
                value="Всё есть",
            )
            filled = build_announcement_preview(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft.draft_id
            )
            self.assertEqual(filled.html, "Всё есть")
            save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                brick_id=brick.id,
                value="",
            )
            cleared = build_announcement_preview(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft.draft_id
            )
            self.assertEqual(cleared.html, ".")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StandardTemplateEndToEndTests(unittest.TestCase):
    """Стандарт места в реальном анонсе даёт тот же эталон."""

    @scenario_test
    async def test_place_announcement_matches_owner_reference(self, h):
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="r2-place")
            place = create_place(
                session,
                "Дом культуры",
                city="Тестовый город",
                created_by_user_id=user.id,
                address="ул. Ленина, 1",
                url="https://example.com/place",
            )
            place_id = int(place.id)
            session.commit()
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
            )
            confirm_announcement_place(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft.draft_id
            )
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            self.assertIsNotNone(standard)
            assert standard is not None
            choose_announcement_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                template_id=standard.template_id,
            )
            for code, value in (
                ("название", "Летний концерт"),
                ("дата", "12.07.2027"),
                ("время_начала", "19:00"),
                ("описание", "Большой зал, вход свободный."),
                ("контакт", "@contact"),
            ):
                save_announcement_value(
                    session,
                    actor_telegram_user_id=DEFAULT_USER_ID,
                    draft_id=draft.draft_id,
                    brick_id=get_system_brick(session, code).id,
                    value=value,
                )
            preview = build_announcement_preview(
                session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft.draft_id
            )
            self.assertEqual(preview.html, OWNER_REFERENCE)
            # Заглушка «.» не является готовым текстом.
            self.assertNotEqual(preview.html, ".")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
