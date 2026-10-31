# tests/test_poster2_h2_template_content.py
"""
Poster 2.3 H2 (часть 1): единый `content` шаблона и необязательный
`extended_content`.

Четыре независимые проверки контракта H2 на временных SQLite (реальная
`post_bot.db`, сеть и Telegram не используются):

1. **Golden байт-в-байт.** Прежний эталон v4 состоял из трёх частей
   (`header`/`body`/`footer`); их склейка по формуле миграции 24 побайтово
   совпадает с `BUILTIN_TEMPLATE_CONTENT`, а рендер этой строки совпадает с
   литеральным golden-HTML для матрицы: все значения заполнены / пустое
   описание / пустой контакт / оба пустых. То есть перенос секций в одну
   строку не меняет ни одного символа готового анонса.

2. **Миграция pre-24 → 24.** Заполненная база версии 23 (строки версий с
   секциями, снимок, привязки, черновик и extras с `section`/`position`)
   переводится миграцией 24: `content` собирается по backfill-формуле,
   `extended_content IS NULL`, значения extras целы, колонок секций и
   позиции больше нет, история/привязки/триггеры сохранены, повторный
   запуск — no-op. Схема после миграции совпадает со свежим `create_all`.

3. **Скрытие пустых brick-строк.** Строка, в которой есть только токены
   кирпичиков, отрендерившиеся пустыми, исчезает целиком (вместе с висячим
   `//` и разметкой `<b></b>`). Литеральные строки и пустые строки вне
   brick-строк сохраняются буквально.

4. **`extended_content` при выбранных extras.** Анонс без дополнительных
   кирпичиков рендерит `content`, с дополнительными — `extended_content`
   (extras дописываются строками в его конец). Удаление extras возвращает
   канонический текст; значения кирпичиков самого шаблона при этом целы.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest
from typing import Any

from sqlalchemy import text as sql_text

from announcement_store import (
    add_announcement_extra,
    build_announcement_preview,
    build_announcement_render_payload,
    remove_announcement_extra,
    save_announcement_value,
)
from base import Base
from brick_store import get_system_brick
from brick_types import SYSTEM_BRICKS
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from template_compiler import render_announcement, render_template
from template_parser import BrickNode, FormatNode, LinkNode, parse_template
from template_store import create_personal_template
from template_types import (
    BUILTIN_TEMPLATE_CONTENT,
    BUILTIN_TEMPLATE_EXTENDED_CONTENT,
    TEMPLATE_CONTENT,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage27_helpers import OWNER_TG, AnnouncementDatabaseTestCase

# --- Эталон v4 (литералы, без импорта констант продукта) --------------------

REFERENCE_HEADER = (
    "**[название]**\n"
    "\n"
    "**[дата] // [время_начала]**\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]"
)
REFERENCE_BODY = "\n[описание]"
REFERENCE_FOOTER = "\n[контакт]"


# Формула backfill миграции 24: разделитель только между непустыми (по
# `.strip()`) соседними частями. Для эталона v4 тело и подвал уже начинаются
# с перевода строки, поэтому склейка равна прежней трёхчастной записи.
def _join(header: str, body: str, footer: str) -> str:
    """Склейка прежних секций в единый `content` по формуле миграции 24."""
    if not (header.strip() or body.strip() or footer.strip()):
        return header + body + footer
    text = header.strip()
    if text and (body.strip() or footer.strip()):
        text += "\n"
    text += body
    if body.strip() and footer.strip():
        text += "\n"
    return text + footer


# --- Golden-HTML: ровно то, что увидит Telegram -----------------------------

GOLDEN_ALL_FILLED = (
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
# Пустое описание: его строка скрывается вместе с соседней пустой, поэтому
# между адресом и контактом остаётся ровно один пустой разделитель шаблона.
GOLDEN_EMPTY_DESCRIPTION = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "@contact"
)
# Пустой контакт: скрыта его строка, текст заканчивается описанием.
GOLDEN_EMPTY_CONTACT = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный."
)
GOLDEN_EMPTY_BOTH = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1"
)

FILLED_VALUES = {
    "название": "Летний концерт",
    "дата": "12.07.2027",
    "время_начала": "19:00",
    "место": "Дом культуры",
    "ссылка_места": "https://example.com/place",
    "адрес": "ул. Ленина, 1",
    "описание": "Большой зал, вход свободный.",
    "контакт": "@contact",
}

SYSTEM_BY_CODE = {item.code: item for item in SYSTEM_BRICKS}
BINDINGS = {code: SYSTEM_BY_CODE[code].id for code in FILLED_VALUES}


def _definitions() -> dict[int, Any]:
    """Определения системных кирпичиков: ID → описание для компилятора."""
    return {definition.id: definition for definition in SYSTEM_BRICKS}


def _bound_tokens(text: str) -> list[Any]:
    """Токены текста с brick_id из постоянных системных привязок."""
    result: list[Any] = []
    for token in parse_template(text):
        if isinstance(token, BrickNode):
            result.append(BrickNode(code=token.code, brick_id=BINDINGS[token.code]))
        elif isinstance(token, LinkNode):
            url = token.url
            if isinstance(url, BrickNode):
                url = BrickNode(code=url.code, brick_id=BINDINGS[url.code])
            result.append(
                LinkNode(label=tuple(_bound_tokens_children(token.label)), url=url)
            )
        elif isinstance(token, FormatNode):
            result.append(
                FormatNode(
                    marker=token.marker,
                    children=tuple(_bound_tokens_children(token.children)),
                )
            )
        else:
            result.append(token)
    return result


def _bound_tokens_children(children: Any) -> list[Any]:
    """Дети разметки/подписи ссылки с сохранением структуры узлов."""
    result: list[Any] = []
    for token in children:
        if isinstance(token, BrickNode):
            result.append(BrickNode(code=token.code, brick_id=BINDINGS[token.code]))
        elif isinstance(token, LinkNode):
            url = token.url
            if isinstance(url, BrickNode):
                url = BrickNode(code=url.code, brick_id=BINDINGS[url.code])
            result.append(
                LinkNode(label=tuple(_bound_tokens_children(token.label)), url=url)
            )
        elif isinstance(token, FormatNode):
            result.append(
                FormatNode(
                    marker=token.marker,
                    children=tuple(_bound_tokens_children(token.children)),
                )
            )
        else:
            result.append(token)
    return result


def _values(**overrides: str) -> dict[int, Any]:
    """Значения по brick_id; пустые строки не передаются (это «не задано»)."""
    return {
        BINDINGS[code]: value
        for code, value in {**FILLED_VALUES, **overrides}.items()
        if value
    }


class TemplateContentGoldenTests(unittest.TestCase):
    """Склейка прежних секций и рендер единого `content` — байт-в-байт."""

    def test_v4_sections_join_equals_builtin_content(self) -> None:
        joined = _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)
        self.assertEqual(joined, BUILTIN_TEMPLATE_CONTENT)
        # Прежний рендер склеивал непустые секции через "\n" — результат
        # совпадает с единым текстом H2 побайтово.
        self.assertEqual(
            joined,
            "\n".join((REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)),
        )

    def test_extended_content_is_absent_in_builtin(self) -> None:
        self.assertIsNone(BUILTIN_TEMPLATE_EXTENDED_CONTENT)

    def _render(self, text: str, values: dict[int, Any]) -> str:
        return render_template(
            {TEMPLATE_CONTENT: _bound_tokens(text)},
            values,
            _definitions(),
        )

    def test_golden_matrix_matches_joined_content_byte_for_byte(self) -> None:
        """
        Матрица H2: рендер склейки секций = рендер литерала = golden-эталон.

        Случаи — все значения заполнены, пустое описание, пустой контакт,
        оба пустых. Ожидаемый HTML записан здесь литерально: смена поведения
        «пустой рендер» → «скрытие пустых brick-строк» в H2 осознанная и
        утверждена, поэтому пустая строка не оставляет висячих разделителей.
        """
        joined = _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER)
        matrix: tuple[tuple[str, dict[str, str], str], ...] = (
            ("all filled", {}, GOLDEN_ALL_FILLED),
            ("empty description", {"описание": ""}, GOLDEN_EMPTY_DESCRIPTION),
            ("empty contact", {"контакт": ""}, GOLDEN_EMPTY_CONTACT),
            ("both empty", {"описание": "", "контакт": ""}, GOLDEN_EMPTY_BOTH),
        )
        for name, overrides, golden in matrix:
            with self.subTest(case=name):
                values = _values(**overrides)
                from_join = self._render(joined, values)
                from_literal = self._render(BUILTIN_TEMPLATE_CONTENT, values)
                self.assertEqual(from_join, from_literal, "склейка = литерал")
                self.assertEqual(from_join, golden, "golden байт-в-байт")

    def test_empty_builtin_render_is_empty_and_gets_placeholder(self) -> None:
        tokens = {TEMPLATE_CONTENT: _bound_tokens(BUILTIN_TEMPLATE_CONTENT)}
        self.assertEqual(render_template(tokens, {}, _definitions()), "")
        self.assertEqual(
            render_announcement(tokens, {}, _definitions()),
            ".",
            "пустой видимый текст без фото → техническая «.»",
        )


# --- Миграция 24 ------------------------------------------------------------

_H2_TABLES = (
    "template_versions",
    "template_version_bricks",
    "template_snapshots",
    "template_snapshot_bricks",
    "announcement_extras",
)

OWNER_TG_MIGRATION = 9_845_001


def _write(path: str, statement: str, parameters: tuple = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _sql(path: str, statement: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement, parameters).fetchall()
    finally:
        connection.close()


def _columns(path: str, table: str) -> dict[str, tuple]:
    return {str(row[1]): row for row in _sql(path, f"PRAGMA table_info({table})")}


def _schema(path: str) -> dict[str, tuple]:
    """
    Колонки/индексы/FK таблиц H2 как они видны SQLite.

    Триггеры сюда не входят: их создаёт миграция, а `create_all` — нет;
    неизменяемость истории проверяется отдельными запросами ниже.
    """
    connection = sqlite3.connect(path)
    try:
        result: dict[str, tuple] = {}
        for table in _H2_TABLES:
            columns = [
                (str(row[1]), str(row[2]).upper(), int(row[3]), row[4])
                for row in connection.execute(f"PRAGMA table_info({table})")
            ]
            indexes = sorted(
                (str(row[1]), int(row[2]), str(row[3]))
                for row in connection.execute(f"PRAGMA index_list({table})")
            )
            foreign_keys = sorted(
                (str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6]))
                for row in connection.execute(f"PRAGMA foreign_key_list({table})")
            )
            result[table] = (columns, indexes, foreign_keys)
        return result
    finally:
        connection.close()


class TemplateContentMigrationTests(unittest.TestCase):
    """Заполненная база версии 23 → миграция 24 без потери данных."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-h2-migration-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(
            run_migrations(self.engine, DEFAULT_MIGRATIONS[:23]),
            23,
            "миграция 24 ещё не применена",
        )
        self._seed()

    # --- фикстуры ---------------------------------------------------------

    def _seed(self) -> None:
        _write(
            self.path,
            "INSERT INTO users (id, telegram_user_id, username, created_at) "
            "VALUES (1, ?, 'h2-owner', '2026-01-01 00:00:00')",
            (OWNER_TG_MIGRATION,),
        )
        _write(
            self.path,
            "INSERT INTO places (id, created_by_user_id, address, url, created_at) "
            "VALUES (1, 1, 'ул. Ленина, 1', 'https://example.com/place', "
            "'2026-01-01 00:00:00')",
        )
        _write(
            self.path,
            "INSERT INTO templates (id, kind, place_id, revision, created_at, "
            "updated_at) VALUES (1, 'standard', 1, 3, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        )
        versions = (
            # id, version, header, body, footer, builtin_version
            (
                1,
                1,
                "**[название]**",
                "\n**[дата] // [время_начала]**\n\n"
                "**[место]([ссылка_места])**\n[адрес]\n\n[описание]",
                "\n[контакт]",
                3,
            ),
            (2, 2, REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER, 4),
            (3, 3, "только шапка", "", "", None),
            (4, 4, "", "только тело", "", None),
            # Все секции из пробелов: прежний CHECK разрешал такую строку.
            (5, 5, " ", "", "", None),
        )
        for version_id, version, header, body, footer, builtin in versions:
            _write(
                self.path,
                "INSERT INTO template_versions (id, template_id, version, "
                "header, body, footer, created_by_user_id, created_at, "
                "builtin_version) VALUES (?, 1, ?, ?, ?, ?, 1, "
                "'2026-01-01 00:00:00', ?)",
                (version_id, version, header, body, footer, builtin),
            )
        _write(
            self.path,
            "INSERT INTO template_version_bricks (template_version_id, "
            "brick_id, code) SELECT 1, id, code FROM bricks "
            "WHERE scope = 'system' AND code IN "
            "('название', 'дата', 'время_начала')",
        )
        _write(
            self.path,
            "INSERT INTO template_snapshots (id, owner_user_id, "
            "source_template_id, source_template_version, source_kind, "
            "source_name, source_place_id, source_owner_user_id, header, body, "
            "footer, builtin_version, idempotency_key, created_at) "
            "VALUES (1, 1, 1, 2, 'standard', NULL, 1, NULL, ?, ?, ?, 4, "
            "'h2-snapshot', '2026-01-01 00:00:00')",
            (REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER),
        )
        _write(
            self.path,
            "INSERT INTO template_snapshot_bricks (template_snapshot_id, "
            "brick_id, code) SELECT 1, id, code FROM bricks "
            "WHERE scope = 'system' AND code IN ('название', 'место')",
        )
        _write(
            self.path,
            "INSERT INTO announcement_drafts (id, author_user_id, place_id, "
            "place_display_name, origin, template_id, template_version, "
            "template_snapshot_id, stage, revision, created_at, updated_at) "
            "VALUES (1, 1, 1, 'Дом культуры', 'place', 1, 2, 1, 'extras', 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        )
        extras = (
            (1, 8, "header", "start"),  # [описание] в начало шапки
            (2, 9, "footer", "end"),  # [контакт] в конец подвала
        )
        for extra_id, brick_id, section, position in extras:
            _write(
                self.path,
                "INSERT INTO announcement_extras (id, draft_id, brick_id, "
                "section, position, occurrence, created_at, updated_at) "
                "VALUES (?, 1, ?, ?, ?, 1, '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:00')",
                (extra_id, brick_id, section, position),
            )

    def _migrate(self) -> int:
        return run_migrations(self.engine)

    def _content_rows(self) -> list[tuple]:
        return _sql(
            self.path,
            "SELECT id, template_id, version, content, extended_content, "
            "builtin_version FROM template_versions ORDER BY id",
        )

    # --- проверки ---------------------------------------------------------

    def test_pre_migration_schema_has_sections(self) -> None:
        """Воспроизведение до миграции: секции и позиция extras ещё есть."""
        version_columns = _columns(self.path, "template_versions")
        self.assertEqual(
            [name for name in ("header", "body", "footer") if name in version_columns],
            ["header", "body", "footer"],
        )
        self.assertNotIn("content", version_columns)
        extra_columns = _columns(self.path, "announcement_extras")
        self.assertIn("section", extra_columns)
        self.assertIn("position", extra_columns)

    def test_content_is_backfilled_and_extended_is_null(self) -> None:
        self.assertEqual(self._migrate(), LATEST_VERSION)
        rows = self._content_rows()
        self.assertEqual([row[0] for row in rows], [1, 2, 3, 4, 5])
        expected = {
            1: _join(
                "**[название]**",
                "\n**[дата] // [время_начала]**\n\n"
                "**[место]([ссылка_места])**\n[адрес]\n\n[описание]",
                "\n[контакт]",
            ),
            2: _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER),
            3: "только шапка",
            4: "только тело",
            5: " ",
        }
        for row in rows:
            with self.subTest(version_id=row[0]):
                self.assertEqual(row[3], expected[row[0]], "content по backfill")
                self.assertIsNone(row[4], "extended_content не изобретается")
        # Номер версии и метка встроенного эталона не переписаны.
        self.assertEqual(
            [(row[2], row[5]) for row in rows],
            [(1, 3), (2, 4), (3, None), (4, None), (5, None)],
        )
        # Заполненный эталон v4 не изменился ни на символ.
        self.assertEqual(rows[1][3], BUILTIN_TEMPLATE_CONTENT)

    def test_snapshot_contents_are_backfilled(self) -> None:
        self._migrate()
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, content, extended_content, builtin_version, "
                "idempotency_key, source_template_version "
                "FROM template_snapshots",
            ),
            [
                (
                    1,
                    _join(REFERENCE_HEADER, REFERENCE_BODY, REFERENCE_FOOTER),
                    None,
                    4,
                    "h2-snapshot",
                    2,
                )
            ],
        )

    def test_extras_lose_sections_and_keep_values(self) -> None:
        self._migrate()
        columns = _columns(self.path, "announcement_extras")
        for removed in ("section", "position"):
            self.assertNotIn(removed, columns, "H2: выбора секции/позиции нет")
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, draft_id, brick_id, occurrence FROM "
                "announcement_extras ORDER BY id",
            ),
            [(1, 1, 8, 1), (2, 1, 9, 1)],
            "значения extras перенесены без потерь",
        )
        # Связи и привязки не пересобраны: те же строки, те же коды.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT template_version_id, code FROM template_version_bricks "
                "ORDER BY template_version_id, code",
            ),
            [(1, "время_начала"), (1, "дата"), (1, "название")],
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT template_snapshot_id, code FROM template_snapshot_bricks "
                "ORDER BY code",
            ),
            [(1, "место"), (1, "название")],
        )

    def test_history_stays_immutable_after_rebuild(self) -> None:
        self._migrate()
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE template_versions SET content = 'подмена' WHERE id = 2"
                )
        finally:
            connection.close()
        content_column = _columns(self.path, "template_versions")["content"]
        self.assertEqual(
            (content_column[2], content_column[3], content_column[4]),
            ("TEXT", 1, None),
            "content TEXT NOT NULL",
        )

    def test_repeat_run_is_noop_and_integrity_is_clean(self) -> None:
        self._migrate()
        before = self._content_rows()
        self.assertEqual(self._migrate(), LATEST_VERSION)
        self.assertEqual(self._content_rows(), before, "повтор не меняет текст")
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE name LIKE '%backup%' "
                "AND name NOT LIKE 'legacy_%'",
            ),
            [],
            "временные копии миграции удалены",
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT version, name FROM schema_migrations ORDER BY version "
                "DESC LIMIT 1",
            ),
            [(24, "template_content_and_extras_without_sections")],
        )

    def test_post_migration_schema_equals_fresh_create_all(self) -> None:
        self._migrate()
        fresh_path = temp_db_path("poster-h2-fresh-")
        self.addCleanup(shutil.rmtree, os.path.dirname(fresh_path), True)
        fresh_engine = make_engine(fresh_path)
        self.addCleanup(fresh_engine.dispose)
        Base.metadata.create_all(fresh_engine)
        self.assertEqual(
            _schema(self.path),
            _schema(fresh_path),
            "колонки, индексы и FK после миграции = модели",
        )


# --- Скрытие пустых brick-строк --------------------------------------------

HIDING_TEXT = (
    "Афиша\n"
    "\n"
    "[название]\n"
    "\n"
    "литерал // без токенов\n"
    "\n"
    "\n"
    "**[дата] // [время_начала]**\n"
    "\n"
    "Подпись"
)


class EmptyBrickLineHidingTests(unittest.TestCase):
    """
    Строка с brick-токенами, отрендерившимися пустыми, скрывается целиком.

    H2 (утверждённый контракт): висячий литерал `//` и пустая разметка
    `<b></b>` не должны попадать в готовый анонс. Строки без токенов
    выводятся буквально, включая литеральные `//` и пустые строки.
    """

    def _render(self, text: str, values: dict[int, Any]) -> str:
        return render_template(
            {TEMPLATE_CONTENT: _bound_tokens(text)},
            values,
            _definitions(),
        )

    def test_brick_only_lines_disappear_without_dangling_literals(self) -> None:
        rendered = self._render(HIDING_TEXT, {})
        self.assertNotIn("[название]", rendered)
        self.assertNotIn("//", rendered.replace("литерал // без токенов", ""))
        self.assertNotIn("<b></b>", rendered)
        # Пустая brick-строка не оставляет «дыру»: вместе с ней уходит одна
        # соседняя пустая строка, остальные пустые строки сохраняются.
        self.assertEqual(rendered, "Афиша\n\nлитерал // без токенов\n\n\nПодпись")

    def test_literal_line_keeps_its_own_separator(self) -> None:
        """Литеральный `//` в строке без кирпичиков — не «сирота»."""
        rendered = self._render("а\n//\n\nб", {})
        self.assertEqual(rendered, "а\n//\n\nб")
        # Видимый текст непустой → заглушка «.» не подставляется.
        tokens = {TEMPLATE_CONTENT: _bound_tokens("//")}
        self.assertEqual(
            render_announcement(tokens, {}, _definitions()),
            "//",
            "литеральный `//` остаётся текстом анонса",
        )

    def test_filled_brick_line_is_kept_with_its_literal(self) -> None:
        rendered = self._render(
            HIDING_TEXT,
            {
                BINDINGS["название"]: "Название",
                BINDINGS["дата"]: "01.01.2027",
                BINDINGS["время_начала"]: "19:00",
            },
        )
        self.assertEqual(
            rendered,
            "Афиша\n\nНазвание\n\nлитерал // без токенов\n\n\n"
            "<b>01.01.2027 // 19:00</b>\n\nПодпись",
        )


# --- extras → extended_content ---------------------------------------------


class ExtendedContentRenderTests(AnnouncementDatabaseTestCase):
    """Анонс рендерит `extended_content` только при выбранных extras."""

    def setUp(self) -> None:
        super().setUp()
        # Poster 2.3.1 (E1): доп. кирпичик стоит на своей позиции в
        # расширенном тексте шаблона. Личный шаблон не видит кирпичики места,
        # поэтому кандидат — личный кирпичик владельца.
        self.extra_brick = self.make_personal_brick(self.owner, "доп_места")
        self.template = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Расширяемый",
            content="Основной текст: [название]",
            extended_content="Расширенный текст: [название]\n[доп_места]",
        )
        self.draft = self.start_at_questions(template_id=self.template.template_id)

    def _preview(self) -> str:
        return build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft.draft_id,
        ).html

    def _title_brick_id(self) -> int:
        """ID системного кирпичика «название» (обязателен в каталоге)."""
        definition = get_system_brick(self.session, "название")
        self.assertIsNotNone(definition)
        assert definition is not None
        return int(definition.id)

    def test_extras_switch_render_between_content_and_extended(self) -> None:
        draft = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft.draft_id,
            brick_id=self._title_brick_id(),
            value="Концерт",
            expected_revision=self.draft.revision,
        )
        # Без extras: расширение не выбрано, рендерится канонический текст
        # (расширенный текст снимка остаётся доступным, но не используется).
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertFalse(payload.use_extended)
        self.assertEqual(self._preview(), "Основной текст: Концерт")

        added = add_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=self.extra_brick,
            value="Дополнительно",
            expected_revision=draft.revision,
        )
        self.assertEqual([extra.code for extra in added.extras], ["доп_места"])
        payload = build_announcement_render_payload(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
        )
        self.assertTrue(payload.use_extended)
        # E1: значение extras стоит на позиции токена в расширенном тексте.
        self.assertEqual(self._preview(), "Расширенный текст: Концерт\nДополнительно")

        # Удаление extras возвращает канонический текст; значение самого
        # кирпичика extras удалено вместе с ним (документированное поведение
        # remove), а значения кирпичиков шаблона не затронуты.
        removed = remove_announcement_extra(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            extra_id=added.extras[0].extra_id,
            expected_revision=added.revision,
        )
        self.assertEqual(removed.extras, ())
        self.assertIsNone(removed.value_for(self.extra_brick))
        self.assertEqual(
            self.session.execute(
                sql_text(
                    "SELECT brick_id FROM announcement_values "
                    "WHERE draft_id = :draft ORDER BY brick_id"
                ),
                {"draft": draft.draft_id},
            ).fetchall(),
            [(self._title_brick_id(),)],
            "значение канонического кирпичика сохранено",
        )
        self.assertEqual(self._preview(), "Основной текст: Концерт")
