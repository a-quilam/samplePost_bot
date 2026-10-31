# tests/test_poster2_u1a_verify_tester.py
"""
U1a — независимая приёмка (тестировщик, не автор кода).

Проверяется фактическое поведение по требованиям владельца:

1. город: обязателен при создании, «Название · Город» в списках, поиск по
   городу, различимость одинаковых имён, редактирование города в карточке
   места (`city_set`/`city_delete`), `[город]` вне стандартного шаблона;
2. стандартный шаблон v4: побайтовый ВИДИМЫЙ формат по тексту требования
   (жирный заголовок, пустая строка, жирные «дата // время», сразу под ними
   жирная площадка-ссылка без пустой строки, обычный адрес, пустая строка,
   описание, пустая строка, контакт) + состав секций редактора; правленый
   пользователем шаблон не перезаписывается миграцией 22;
3. автозаполнение: жирная кликабельная площадка, адрес, пустые дата/время,
   первый вопрос — первый неизвестный кирпичик, площадка без URL, свежий
   снимок после правки справочника;
4. негативы: идемпотентность миграций, integrity/foreign_key, старые места
   с `city IS NULL`, повторные callback без дублей.

Реальная `post_bot.db`, `.env` и сеть не используются.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import select

try:
    from announcement_store import (
        build_announcement_preview,
        build_announcement_questions,
        choose_announcement_template,
        confirm_announcement_place,
        start_announcement_wizard,
    )
    from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
    from models import AnnouncementDraft, Place
    from place_edit_store import set_place_city
    from place_store import create_place, get_place_details, list_places, search_places
    from template_parser import BrickNode, FormatNode, LinkNode, parse_template
    from template_types import BUILTIN_TEMPLATE_CONTENT, TEMPLATE_CONTENT
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_live_record,
        scenario_test,
    )
    from tests.stage19a_helpers import make_engine, temp_db_path
    from tests.stage23_helpers import TemplateDatabaseTestCase
    from user_store import get_or_create_user

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    IMPORT_ERROR = str(exc)


OWNER_TG = 9_845_001
OTHER_TG = 9_845_002

# --- Эталон требования 2 (литералы, не импорт из продукта) -------------------
#
# Владелец: «заголовок жирный, пустая строка, "дата // время" жирные,
# "место(ссылка)" жирная БЕЗ пустой строки между датой и местом, адрес
# обычный, пустая строка, описание, пустая строка, контакт».
# Тот же эталон зафиксирован в docs/agent_team/poster2_spec.md §6.1.1.
REQ_VISIBLE_LINES = (
    "<b>Летний концерт</b>",
    "",
    "<b>12.07.2027 // 19:00</b>",
    '<b><a href="https://example.com/place">Дом культуры</a></b>',
    "ул. Ленина, 1",
    "",
    "Большой зал, вход свободный.",
    "",
    "@contact",
)
REQ_RENDERED = "\n".join(REQ_VISIBLE_LINES)


def _walk_codes(tokens: Any) -> list[str]:
    """Коды-подписи секции (без цели ссылки)."""
    codes: list[str] = []
    for token in tokens:
        if isinstance(token, BrickNode):
            codes.append(token.code)
        elif isinstance(token, LinkNode):
            codes.extend(_walk_codes(token.label))
        elif isinstance(token, FormatNode):
            codes.extend(_walk_codes(token.children))
    return codes


def _walk_all_codes(tokens: Any) -> list[str]:
    """Все коды, включая цель условной ссылки."""
    codes: list[str] = []
    for token in tokens:
        if isinstance(token, BrickNode):
            codes.append(token.code)
        elif isinstance(token, LinkNode):
            codes.extend(_walk_all_codes(token.label))
            if isinstance(token.url, BrickNode):
                codes.append(token.url.code)
        elif isinstance(token, FormatNode):
            codes.extend(_walk_all_codes(token.children))
    return codes


# --- Требование 2: формат и секции -----------------------------------------


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class TemplateV4RequirementTests(TemplateDatabaseTestCase):
    """Побайтовый видимый формат и состав секций по требованию владельца."""

    place_city = "Москва"

    def setUp(self) -> None:
        super().setUp()
        self.owner = get_or_create_user(self.session, OWNER_TG, username="u1a-verify")
        self.place = create_place(
            self.session,
            "Дом культуры",
            city=self.place_city,
            created_by_user_id=self.owner.id,
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        self.standard = self._standard()

    def _standard(self):
        from template_store import get_standard_template

        return get_standard_template(self.session, OWNER_TG, self.place.id)

    def _values(self) -> dict[int, str]:
        from brick_store import get_system_brick

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
        values: dict[int, str] = {}
        for code, value in raw.items():
            brick = get_system_brick(self.session, code)
            assert brick is not None
            values[int(brick.id)] = value
        return values

    def _render(self) -> str:
        from template_store import render_template_version

        return render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            values_by_id=self._values(),
        )

    def test_visible_text_matches_owner_format_byte_for_byte(self) -> None:
        """
        Требование 2: пустая строка — ПОСЛЕ заголовка; дата/время и площадка
        стоят подряд, без пустой строки между ними.
        """
        rendered = self._render()
        self.assertEqual(
            rendered,
            REQ_RENDERED,
            "видимый текст стандартного шаблона не совпадает с требованием.\n"
            f"ожидание: {REQ_RENDERED!r}\nфакт:     {rendered!r}",
        )

    def test_blank_lines_positions(self) -> None:
        """Пустые строки ровно в позициях 1, 5, 7 (между блоками)."""
        lines = self._render().split("\n")
        self.assertEqual(len(lines), 9, lines)
        self.assertEqual(
            [index for index, line in enumerate(lines) if line == ""],
            [1, 5, 7],
            f"неверные пустые строки: {lines!r}",
        )
        self.assertEqual(lines[0], "<b>Летний концерт</b>")
        self.assertEqual(lines[2], "<b>12.07.2027 // 19:00</b>")
        self.assertTrue(lines[3].startswith("<b>"), lines[3])
        self.assertEqual(lines[4], "ул. Ленина, 1")

    def test_editor_text_composition(self) -> None:
        """H2: единый текст; порядок кодов прежний — шапка, описание, контакт."""
        tokens = parse_template(BUILTIN_TEMPLATE_CONTENT, section=TEMPLATE_CONTENT)
        self.assertEqual(
            _walk_codes(tokens),
            [
                "название",
                "дата",
                "время_начала",
                "место",
                "адрес",
                "описание",
                "контакт",
            ],
        )
        self.assertEqual(
            _walk_all_codes(tokens),
            [
                "название",
                "дата",
                "время_начала",
                "место",
                "ссылка_места",
                "адрес",
                "описание",
                "контакт",
            ],
        )
        # `[город]` не входит в стандартный шаблон.
        self.assertNotIn("город", _walk_all_codes(tokens))
        self.assertNotIn("город", BUILTIN_TEMPLATE_CONTENT)


# --- Требование 1/3: город в домене и автозаполнение ------------------------


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class CityDomainTests(TemplateDatabaseTestCase):
    """Город: обязательность, списки, поиск, различимость, NULL-город."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = get_or_create_user(self.session, OWNER_TG, username="u1a-city")

    def _place(self, name: str, city: str, **kwargs):
        return create_place(
            self.session, name, city=city, created_by_user_id=self.owner.id, **kwargs
        )

    def test_city_required_on_creation(self) -> None:
        from place_store import PlaceNameError

        # None проверяется намеренно: тип `str`, но значения из старых
        # вызовов/JSON могли прийти пустыми — сервис обязан отказать.
        bad_cities: tuple[Any, ...] = ("", "   ", None, "«»---")
        for bad in bad_cities:
            with self.subTest(city=bad):
                with self.assertRaises((PlaceNameError, TypeError)):
                    create_place(self.session, "Новое место", city=bad)
        self.assertEqual(
            len(self.session.scalars(select(Place)).all()),
            0,
            "место создано без города",
        )

    def test_same_name_in_different_cities_is_distinguishable_in_list(self) -> None:
        moscow = self._place("Дом культуры", "Москва")
        tver = self._place("Дом культуры", "Тверь")
        items, total = list_places(self.session, offset=0, limit=50)
        self.assertEqual(total, 2)
        pairs = {
            item.place_id: f"{item.canonical_name} · {item.city}" for item in items
        }
        self.assertNotEqual(pairs[moscow.id], pairs[tver.id])
        self.assertEqual(pairs[moscow.id], "Дом культуры · Москва")
        self.assertEqual(pairs[tver.id], "Дом культуры · Тверь")

    def test_search_finds_place_by_city(self) -> None:
        tver = self._place("Летняя эстрада", "Тверь")
        self._place("Дом культуры", "Москва")
        results = search_places(self.session, "Тверь")
        self.assertEqual([item.place_id for item in results], [tver.id])
        self.assertEqual(results[0].city, "Тверь")

    def test_legacy_place_with_null_city_is_usable(self) -> None:
        """Старое место с city=NULL: список, карточка, поиск, анонс работают."""
        place = self._place("Старый клуб", "Москва")
        self.session.execute(
            Place.__table__.update().where(Place.id == place.id).values(city=None)
        )
        self.session.commit()
        self.session.expire_all()
        details = get_place_details(self.session, place.id)
        assert details is not None
        self.assertIsNone(details.city)
        items, _ = list_places(self.session, offset=0, limit=10)
        self.assertIsNone(items[0].city)
        self.assertEqual(search_places(self.session, "Старый")[0].place_id, place.id)
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=place.id
        )
        self.assertIsNone(draft.place_city)

    def test_city_is_snapshotted_and_refreshed_before_template_is_fixed(self) -> None:
        """Правка города в справочнике до фиксации шаблона попадает в снимок."""
        place = self._place("Дом культуры", "Москва", address="ул. Ленина, 1")
        standard = self._standard(place.id)
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=place.id
        )
        draft = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        with patch("config.SUPERADMIN_USER_IDS", (OWNER_TG,)):
            set_place_city(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                place_id=place.id,
                city="Тверь",
                expected_city="Москва",
            )
        fixed = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            template_id=standard.template_id,
            expected_revision=draft.revision,
            expected_template_revision=standard.revision,
        )
        self.assertEqual(fixed.place_city, "Тверь")

    def _standard(self, place_id: int):
        from template_store import get_standard_template

        return get_standard_template(self.session, OWNER_TG, place_id)

    def test_city_brick_can_be_used_in_custom_template(self) -> None:
        """`[город]` доступен автору шаблона и рендерится значением места."""
        from template_store import create_personal_template, render_template_version

        self._place("Дом культуры", "Москва")
        template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="С городом",
            content="\n".join(p for p in ("**[название]**", "Город: [город]", "") if p),
        )
        from brick_store import get_system_brick

        brick = get_system_brick(self.session, "город")
        assert brick is not None
        rendered = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=template.template_id,
            values_by_id={int(brick.id): "Москва"},
        )
        self.assertIn("Город: Москва", rendered)


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class AutofillTests(TemplateDatabaseTestCase):
    """Требование 3: автозаполнение живого анонса по площадке."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = get_or_create_user(self.session, OWNER_TG, username="u1a-auto")
        self.place = create_place(
            self.session,
            "Дом культуры",
            city="Москва",
            created_by_user_id=self.owner.id,
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        from template_store import get_standard_template

        self.standard = get_standard_template(self.session, OWNER_TG, self.place.id)

    def _started(self):
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=self.place.id
        )
        draft = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        return choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            template_id=self.standard.template_id,
            expected_revision=draft.revision,
            expected_template_revision=self.standard.revision,
        )

    def test_live_announcement_autofils_place_before_first_question(self) -> None:
        fixed = self._started()
        preview = build_announcement_preview(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=fixed.draft_id
        )
        html = preview.html
        self.assertIn(
            '<b><a href="https://example.com/place">Дом культуры</a></b>', html
        )
        self.assertIn("ул. Ленина, 1", html)
        # Дата/время пусты: мастер не выдумывает значения, а строка с одними
        # пустыми кирпичиками скрывается целиком — висячего `//` нет.
        self.assertNotIn("2026", html)
        self.assertNotIn(" // ", html)
        self.assertEqual(
            html.split("\n"),
            [
                '<b><a href="https://example.com/place">Дом культуры</a></b>',
                "ул. Ленина, 1",
            ],
        )
        # Город в текст не попадает.
        self.assertNotIn("Москва", html)
        # Первый вопрос — первый неизвестный кирпичик, не площадка и не город.
        questions = build_announcement_questions(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=fixed.draft_id,
        )
        self.assertTrue(questions, "вопросов нет")
        self.assertEqual(questions[0].code, "название")
        codes = [question.code for question in questions]
        self.assertNotIn("место", codes, "площадка не должна спрашиваться")
        self.assertNotIn("адрес", codes, "адрес не должен спрашиваться")
        self.assertNotIn("город", codes, "город не должен спрашиваться")

    def test_place_without_url_keeps_bold_name_and_address(self) -> None:
        place = create_place(
            self.session,
            "Клуб без ссылки",
            city="Тверь",
            created_by_user_id=self.owner.id,
            address="ул. Мира, 2",
        )
        from template_store import get_standard_template

        standard = get_standard_template(self.session, OWNER_TG, place.id)
        draft = start_announcement_wizard(
            self.session, actor_telegram_user_id=OWNER_TG, place_id=place.id
        )
        draft = confirm_announcement_place(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            expected_revision=draft.revision,
        )
        fixed = choose_announcement_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            template_id=standard.template_id,
            expected_revision=draft.revision,
            expected_template_revision=standard.revision,
        )
        preview = build_announcement_preview(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=fixed.draft_id
        )
        self.assertIn("<b>Клуб без ссылки</b>", preview.html)
        self.assertNotIn("<a href=", preview.html, "битая ссылка без URL")
        self.assertIn("ул. Мира, 2", preview.html)


# --- Требование 4: негативы и идемпотентность -------------------------------


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class MigrationNegativesTests(unittest.TestCase):
    """Свежая база: все миграции, повторный прогон, целостность, старые строки."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-u1a-verify-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def _sql(self, statement: str, parameters: tuple = ()) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    def test_migrations_are_idempotent_and_clean(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        snapshot = {
            "journal": self._sql(
                "SELECT version FROM schema_migrations ORDER BY version"
            ),
            "bricks": self._sql("SELECT id, code FROM bricks ORDER BY id"),
            "places": self._sql("SELECT id, city FROM places"),
        }
        # Повторный прогон ничего не меняет и не дублирует кирпичик «город».
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            self._sql("SELECT version FROM schema_migrations ORDER BY version"),
            snapshot["journal"],
        )
        self.assertEqual(
            self._sql("SELECT id, code FROM bricks ORDER BY id"), snapshot["bricks"]
        )
        self.assertEqual(
            self._sql("SELECT count(*) FROM bricks WHERE code = 'город'"), [(1,)]
        )
        self.assertEqual(self._sql("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self._sql("PRAGMA foreign_key_check"), [])

    def test_city_columns_are_nullable_and_old_rows_survive(self) -> None:
        # База версии 20 со старым местом без города, затем миграции 21–22.
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:20]), 20)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO places (id, created_by_user_id, address, url, created_at) "
                "VALUES (7, NULL, 'старый адрес', NULL, '2026-01-01 00:00:00')"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            self._sql("SELECT id, address, city FROM places WHERE id = 7"),
            [(7, "старый адрес", None)],
            "миграция не должна backfill-ить город",
        )
        self.assertEqual(self._sql("PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(self._sql("PRAGMA foreign_key_check"), [])


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class UserTemplateNotOverwrittenTests(unittest.TestCase):
    """Правленый пользователем шаблон не перезаписывается миграцией 22."""

    V3_HEADER = "**[название]**"
    V3_BODY = "\n**[дата] // [время_начала]**\n\n**[место]([ссылка_места])**\n[адрес]\n\n[описание]"
    V3_FOOTER = "\n[контакт]"

    @classmethod
    def _joined(cls, header: str, body: str, footer: str) -> str:
        """Формула backfill миграции 24: секции через "\n" только между непустыми."""
        if not (header.strip() or body.strip() or footer.strip()):
            return header + body + footer
        text = header.strip()
        if text and (body.strip() or footer.strip()):
            text += "\n"
        text += body
        if body.strip() and footer.strip():
            text += "\n"
        return text + footer

    def setUp(self) -> None:
        self.path = temp_db_path("poster-u1a-verify-m22-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:21]), 21)
        self._write(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, ?, '2026-01-01 00:00:00')",
            (OWNER_TG,),
        )

    def _write(self, statement: str, parameters: tuple = ()) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(statement, parameters)
            connection.commit()
        finally:
            connection.close()

    def _sql(self, statement: str, parameters: tuple = ()) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()

    def _seed_personal(
        self, template_id: int, *, name: str, header: str, body: str, footer: str
    ) -> None:
        """Строка версии v3 до миграции 24 — секции, а не `content`."""
        self._write(
            "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
            "created_by_user_id, created_at, updated_at) VALUES "
            "(?,'personal',1,?,1,1,'2026-01-01 00:00:00','2026-01-01 00:00:00')",
            (template_id, name),
        )
        self._write(
            "INSERT INTO template_versions (id, template_id, version, header, body, "
            "footer, created_by_user_id, created_at, builtin_version) VALUES "
            "(?, ?, 1, ?, ?, ?, 1, '2026-01-01 00:00:00', 3)",
            (template_id, template_id, header, body, footer),
        )

    def test_edited_copy_is_not_overwritten(self) -> None:
        edited_body = self.V3_BODY + "\n\nмоя подпись"
        self._seed_personal(
            1,
            name="Дефолтный шаблон",
            header=self.V3_HEADER,
            body=edited_body,
            footer=self.V3_FOOTER,
        )
        untouched_id = 2
        self._seed_personal(
            untouched_id,
            name="Дефолтный шаблон",
            header=self.V3_HEADER,
            body=self.V3_BODY,
            footer=self.V3_FOOTER,
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

        self.assertEqual(
            self._sql(
                "SELECT content, builtin_version, revision FROM template_versions AS v "
                "JOIN templates AS t ON t.id = v.template_id WHERE t.id = 1"
            ),
            [(self._joined(self.V3_HEADER, edited_body, self.V3_FOOTER), 3, 1)],
            "правленый шаблон перезаписан миграцией",
        )
        # Контроль: нетронутая копия обновлена до v4 (миграция работает).
        rows = self._sql(
            "SELECT version, builtin_version, content FROM template_versions "
            "WHERE template_id = ? ORDER BY version",
            (untouched_id,),
        )
        self.assertEqual(rows[-1][1], 4)
        self.assertEqual(rows[-1][2], BUILTIN_TEMPLATE_CONTENT)

    def test_snapshot_of_edited_copy_is_not_rewritten(self) -> None:
        self._seed_personal(
            3,
            name="Мой шаблон",
            header=self.V3_HEADER,
            body=self.V3_BODY,
            footer=self.V3_FOOTER,
        )
        self._write(
            "INSERT INTO template_snapshots (id, owner_user_id, source_template_id, "
            "source_template_version, source_kind, header, body, footer, "
            "builtin_version, created_at) VALUES "
            "(50, 1, 3, 1, 'personal', ?, ?, ?, 3, '2026-01-01 00:00:00')",
            (self.V3_HEADER, self.V3_BODY, self.V3_FOOTER),
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            self._sql(
                "SELECT content, extended_content FROM template_snapshots "
                "WHERE id = 50"
            ),
            [(self._joined(self.V3_HEADER, self.V3_BODY, self.V3_FOOTER), None)],
        )
        self.assertEqual(
            self._sql("SELECT revision FROM templates WHERE id = 3"),
            [(1,)],
            "шаблон с другим именем и текстом v3 не должен обновляться",
        )


# --- UI-сценарии через настоящее приложение ---------------------------------


def _callbacks(h, prefix: str = "") -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class PlaceCityUiTests(unittest.TestCase):
    """Создание места: вопрос о городе текстом, место без города не создаётся."""

    @scenario_test
    async def test_city_is_asked_as_plain_text_and_blocks_creation(self, h):
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_add")
            await h.text("Дом культуры")
            await h.callback("pl_create_confirm")

        question = h.screen_text()
        self.assertIn("В каком городе площадка", question)
        self.assertIn("Дом культуры", question)
        with h.session_factory() as session:
            self.assertEqual(
                len(session.scalars(select(Place)).all()),
                0,
                "место создано до ответа о городе",
            )

        # Пустой/пробельный город — ошибка, место по-прежнему не создано.
        await h.text("   ")
        self.assertIn("Город не может быть пустым", h.screen_text())
        with h.session_factory() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 0)

        await h.text("Москва")
        with h.session_factory() as session:
            places = list(session.scalars(select(Place)).all())
            self.assertEqual(len(places), 1)
            self.assertEqual(places[0].city, "Москва")

    @scenario_test
    async def test_back_button_on_city_question_works(self, h):
        """
        На экране вопроса о городе есть кнопка «◀️ Назад». Требование:
        кнопка навигации работает, а не отвечает «Кнопка устарела».
        """
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_add")
            await h.text("Клуб для отмены")
            await h.callback("pl_create_confirm")
            self.assertIn("В каком городе площадка", h.screen_text())
            self.assertIn("plm_back", _callbacks(h))
            await h.callback("plm_back")
            answer = h.answers()[-1] if h.answers() else {}
        self.assertIsNone(
            answer.get("text"),
            f"«Назад» на вопросе о городе: {answer.get('text')!r}; "
            f"экран = {h.screen_text()[:200]!r}",
        )
        self.assertIn(
            "📍 Площадки",
            h.screen_text(),
            "«Назад» не вернул в список площадок",
        )
        with h.session_factory() as session:
            self.assertEqual(len(session.scalars(select(Place)).all()), 0)

    @scenario_test
    async def test_repeat_confirm_does_not_create_duplicate_place(self, h):
        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback("plm_add")
            await h.text("Уникальное место")
            await h.callback("pl_create_confirm")
            await h.text("Москва")
            place_id = None
            with h.session_factory() as session:
                places = list(session.scalars(select(Place)).all())
                self.assertEqual(len(places), 1)
                place_id = places[0].id
            # Устаревшее повторное нажатие «это новое место» не создаёт дубль.
            await h.callback("pl_create_confirm")
            await h.text("Москва")
        with h.session_factory() as session:
            places = list(session.scalars(select(Place)).all())
        self.assertEqual([p.id for p in places], [place_id])

    @scenario_test
    async def test_place_lists_show_name_and_city(self, h):
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="u1a-ui")
            create_place(
                session, "Дом культуры", city="Москва", created_by_user_id=user.id
            )
            tver = create_place(
                session, "Дом культуры", city="Тверь", created_by_user_id=user.id
            )
            tver_id = tver.id

        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_all")
        text = h.screen_text()
        self.assertIn("Дом культуры · Москва", text)
        self.assertIn("Дом культуры · Тверь", text)

        await h.callback("pl_search")
        await h.text("Тверь")
        self.assertIn("Дом культуры · Тверь", h.screen_text())
        self.assertIn("совпадение по городу", h.screen_text())
        self.assertIn(f"pl_open:{tver_id}", _callbacks(h, "pl_open:"))

    @scenario_test
    async def test_city_can_be_edited_from_place_card(self, h):
        """
        Требование 1 (дефект 2) в редакции H3-C: город виден в карточке места
        и меняется из «Управление → Площадки → карточка → Редактировать».

        Удаление города из интерфейса убрано владельцем: у названия и города
        корзины нет, поэтому сбросить город в NULL через UI нельзя. Проверка
        `city_set` сохранена полностью — она и была сутью требования.
        """
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="u1a-edit")
            place = create_place(
                session, "Дом культуры", city="Москва", created_by_user_id=user.id
            )
            place_id = place.id

        def _city() -> str | None:
            with h.session_factory() as session:
                details = get_place_details(session, place_id)
                assert details is not None
                return details.city

        with patch("config.SUPERADMIN_USER_IDS", (DEFAULT_USER_ID,)):
            await h.text("/start")
            await h.callback("manage_places")
            await h.callback(f"plm_open:{place_id}")
            card = h.screen_text()
            self.assertIn("Москва", card, "карточка не показывает город")

            await h.callback(f"plm_edit:{place_id}")
            menu = h.screen_text()
            callbacks = _callbacks(h, "adm_edit_")
            self.assertIn("Москва", menu, "меню правки не показывает город")
            self.assertIn(
                f"adm_edit_city:{place_id}",
                callbacks,
                f"нет кнопки изменения города: callback'и = {callbacks}",
            )
            self.assertNotIn(
                f"adm_edit_city_delete:{place_id}",
                callbacks,
                "у города не должно быть удаления (H3-C)",
            )
            city_buttons = [
                (label, data)
                for label, data in h.screen_buttons()
                if data and "city" in data
            ]
            self.assertEqual(
                [label for label, _data in city_buttons],
                ["🏙 Изменить город"],
                f"у города должна быть только правка: {city_buttons}",
            )

            # `city_set` через UI: выбор действия → ввод → подтверждение.
            await h.callback(f"adm_edit_city:{place_id}")
            self.assertIn("Изменить город", h.screen_text())
            self.assertIn("Москва", h.screen_text(), "экран ввода не показывает город")
            await h.text("Тверь")
            confirm = h.screen_text()
            self.assertIn("Сохранить новый город", confirm)
            self.assertIn("Тверь", confirm)
            await h.callback("adm_edit_confirm")
            self.assertIn("✅ Город места обновлён.", h.screen_text())

            # Удаления города в меню нет: старый callback недоступен.
            menu = h.screen_text()
            self.assertIn("Тверь", menu, "меню правки не обновило город")
            await h.callback(f"adm_edit_city_delete:{place_id}")
            self.assertNotIn("Подтвердите удаление города", h.screen_text())
            self.assertIn("Тверь", h.screen_text())

        self.assertEqual(_city(), "Тверь", "город не должен сбрасываться через UI")
        with h.session_factory() as session:
            from models import PlaceEditLog

            actions = [
                row.action
                for row in session.scalars(
                    select(PlaceEditLog)
                    .where(PlaceEditLog.place_id == place_id)
                    .order_by(PlaceEditLog.id)
                ).all()
            ]
        self.assertEqual(actions, ["city_set"], actions)

    @scenario_test
    async def test_official_place_picker_shows_city_in_list_and_search(self, h):
        """
        Требование 1: «в справочнике и результатах поиска показывать город
        рядом с площадкой», «два одинаковых имени в разных городах различимы».

        Официальный вход — «📍 Создать для площадки» (список площадок из
        меню), затем «🔎 Найти площадку» и текстовый запрос по городу.
        """
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="u1a-aw")
            moscow = create_place(
                session, "Дом культуры", city="Москва", created_by_user_id=user.id
            )
            tver = create_place(
                session, "Дом культуры", city="Тверь", created_by_user_id=user.id
            )
            moscow_id, tver_id = moscow.id, tver.id

        await h.text("/start")
        await h.callback("pick_place")

        # 1. Список площадок: город рядом с названием, и в тексте, и в кнопках.
        pick_text = h.screen_text()
        labels = [
            label for label, _data in h.screen_buttons() if "Дом культуры" in label
        ]
        self.assertIn("Дом культуры · Москва", pick_text, pick_text)
        self.assertIn("Дом культуры · Тверь", pick_text, pick_text)
        self.assertEqual(
            len(set(labels)),
            2,
            f"одинаковые подписи кнопок не различимы: {labels}",
        )
        self.assertTrue(
            any("Дом культуры · Тверь" in label for label in labels),
            f"в подписи кнопки нет города: {labels}",
        )
        self.assertIn(f"pl_pick:{tver_id}", _callbacks(h, "pl_pick:"))
        self.assertIn(f"pl_pick:{moscow_id}", _callbacks(h, "pl_pick:"))

        # 2. Поиск по городу в том же официальном входе.
        await h.callback("pl_pick_search")
        await h.text("Тверь")
        search_text = h.screen_text()
        self.assertIn("Дом культуры · Тверь", search_text, search_text)
        self.assertNotIn("Дом культуры · Москва", search_text, search_text)
        self.assertIn("совпадение по городу", search_text, search_text)
        search_calls = _callbacks(h, "pl_pick:")
        self.assertIn(f"pl_pick:{tver_id}", search_calls)
        self.assertNotIn(f"pl_pick:{moscow_id}", search_calls)


@unittest.skipIf(IMPORT_ERROR is not None, f"окружение недоступно: {IMPORT_ERROR}")
class AnnouncementFromPlaceUiTests(unittest.TestCase):
    """Анонс по зарегистрированной площадке: живой блок и первый вопрос."""

    @scenario_test
    async def test_live_block_autofills_and_asks_no_city_question(self, h):
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="u1a-live2")
            place = create_place(
                session,
                "Дом культуры",
                city="Москва",
                created_by_user_id=user.id,
                address="ул. Ленина, 1",
                url="https://example.com/place",
            )
            place_id = place.id

        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn("Дом культуры · Москва", h.screen_text())
        await h.callback(f"pl_pick:{place_id}")

        live = [call for call in h.calls if is_live_record(call)]
        self.assertTrue(live, "живой анонс не отправлен")
        html = live[-1].get("text") or ""
        self.assertIn(
            '<b><a href="https://example.com/place">Дом культуры</a></b>', html
        )
        self.assertIn("ул. Ленина, 1", html)
        self.assertNotIn("Москва", html, "город попал в живой анонс")
        screen = h.screen_text()
        self.assertIn("Вопрос 1 из", screen)
        self.assertNotIn("Город", screen, "мастер спрашивает город")
        self.assertNotIn("площадк", screen.lower(), "мастер спрашивает площадку")

        # Повторный callback не создаёт второй черновик/снимок.
        await h.callback(f"pl_pick:{place_id}")
        with h.session_factory() as session:
            drafts = list(session.scalars(select(AnnouncementDraft)).all())
            self.assertEqual(len(drafts), 1, [d.id for d in drafts])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
