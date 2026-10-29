# tests/test_poster2_r2_templates.py
"""
Poster 2.1 R2: BUILTIN v3, миграция 18 и «Дефолтный шаблон».

Проверяется:

- точный рендер стандартного анонса по эталону владельца (Часть III):
  жирный заголовок, пустая строка, жирное «дата // время», пустая строка,
  жирная кликабельная площадка, нежирный адрес строкой ниже, пустая строка,
  описание, пустая строка, контакт;
- буквальный `//` при пропущенных кирпичиках и `.` при полностью пустом
  анонсе;
- миграция 18 на заполненной временной БД: нетронутые копии (личная
  «Мой шаблон» и стандарт места) получают новую версию v3, история и снимки
  не переписываются, тронутые человеком и переименованные копии остаются
  как есть, повторный запуск ничего не меняет;
- выдача стартового личного шаблона новому пользователю: имя «Дефолтный
  шаблон» и содержимое v3;
- «Восстановить стандарт» после миграции создаёт версию `builtin_version=3`.

Настоящая `post_bot.db`, `.env`, Telegram и сеть не используются: каждый
тест поднимает собственный временный SQLite-файл.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest

from sqlalchemy.orm import sessionmaker

from brick_store import get_system_brick, list_system_bricks
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Template, TemplateSnapshot
from template_compiler import (
    PLACEHOLDER_TEXT,
    render_announcement,
    render_template,
)
from template_parser import BrickNode, FormatNode, LinkNode, parse_template
from template_store import (
    create_template_snapshot,
    ensure_initial_personal_template,
    get_personal_template,
    get_standard_template,
    list_template_versions,
    reset_personal_template,
    update_template_header,
)
from template_types import (
    BUILTIN_TEMPLATE,
    BUILTIN_TEMPLATE_VERSION,
    INITIAL_PERSONAL_TEMPLATE_NAME,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage23_helpers import OWNER_TG
from user_store import get_or_create_user

# Эталон владельца (Часть III): ровно этот текст должен получиться в Telegram.
EXPECTED_ANNOUNCEMENT = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    "\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "@contact"
)

# Эталон v2 внутри миграции 18 (проверяется независимо от текущих констант).
V2_HEADER = "**[название]**"
V2_BODY = (
    "[дата] // [время_начала]\n"
    "[место]([ссылка_места])\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
V2_FOOTER = "[контакт]"


def _bind(tokens, definitions_by_code) -> None:
    """Проставляет brick_id по коду — как это делает компилятор версии."""
    for node in tokens:
        if isinstance(node, BrickNode):
            node.brick_id = definitions_by_code[node.code].id
        elif isinstance(node, LinkNode):
            _bind(list(node.label), definitions_by_code)
            if isinstance(node.url, BrickNode):
                node.url.brick_id = definitions_by_code[node.url.code].id
        elif isinstance(node, FormatNode):
            _bind(list(node.children), definitions_by_code)


class BuiltinTemplateV3RenderTests(unittest.TestCase):
    """Рендер BUILTIN v3 по эталону владельца — побайтово."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-2-r2-render-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        run_migrations(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)
        self.definitions = {item.id: item for item in list_system_bricks(self.session)}
        self.by_code = {item.code: item for item in list_system_bricks(self.session)}
        self.tokens = {
            section: parse_template(text, section=section)
            for section, text in (
                (TEMPLATE_SECTION_HEADER, BUILTIN_TEMPLATE.header),
                (TEMPLATE_SECTION_BODY, BUILTIN_TEMPLATE.body),
                (TEMPLATE_SECTION_FOOTER, BUILTIN_TEMPLATE.footer),
            )
        }
        for tokens in self.tokens.values():
            _bind(tokens, self.by_code)

    def _values(self) -> dict[int, object]:
        return {
            self.by_code["название"].id: "Летний концерт",
            self.by_code["дата"].id: "12.07.2027",
            self.by_code["время_начала"].id: "19:00",
            self.by_code["место"].id: "Дом культуры",
            self.by_code["ссылка_места"].id: "https://example.com/place",
            self.by_code["адрес"].id: "ул. Ленина, 1",
            self.by_code["описание"].id: "Большой зал, вход свободный.",
            self.by_code["контакт"].id: "@contact",
        }

    def test_version_is_three(self) -> None:
        self.assertEqual(BUILTIN_TEMPLATE.version, 3)
        self.assertEqual(BUILTIN_TEMPLATE_VERSION, 3)

    def test_render_matches_owner_reference_exactly(self) -> None:
        rendered = render_template(self.tokens, self._values(), self.definitions)
        self.assertEqual(rendered, EXPECTED_ANNOUNCEMENT)

    def test_render_announcement_uses_same_layout(self) -> None:
        rendered = render_announcement(
            self.tokens, self._values(), self.definitions, has_photos=False
        )
        self.assertEqual(rendered, EXPECTED_ANNOUNCEMENT)

    def test_skipped_bricks_keep_literal_separator(self) -> None:
        """`//` — буквальная статика: пропуски кирпичиков её не удаляют."""
        values = {self.by_code["название"].id: "Только название"}
        rendered = render_template(self.tokens, values, self.definitions)
        self.assertIn("//", rendered)
        self.assertIn("\n\n", rendered)
        # Литеральный `//` делает анонс видимым: заглушка не подставляется.
        self.assertEqual(
            render_announcement(self.tokens, values, self.definitions),
            rendered,
        )

    def test_completely_empty_template_is_placeholder(self) -> None:
        """Без секций видимого текста нет — ровно `.`."""
        self.assertEqual(render_announcement({}, {}, {}), PLACEHOLDER_TEXT)
        # С фото точка не нужна: пустая подпись вместо заглушки.
        self.assertEqual(render_announcement({}, {}, {}, has_photos=True), "")


def _seed_version_seventeen(path: str) -> None:
    """
    Заполняет базу версии 17 копиями прежнего эталона (v2).

    Пишутся реальные пользователь/место и пять `templates`:

    1. нетронутый личный «Мой шаблон» (v2) — обновляется и переименовывается;
    2. личный, переименованный человеком, но нетронутый по содержимому —
       не обновляется (правило «только доказуемо нетронутые»);
    3. личный «Мой шаблон» с изменённой шапкой — не обновляется;
    4. нетронутый стандарт места — содержимое обновляется, имя (NULL) прежнее;
    5. стартовая личная копия без маркера версии (`builtin_version IS NULL`):
       так её создавал `ensure_initial_personal_template` до R2; имя и все три
       секции совпадают с эталоном — копия обновляется и переименовывается.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, ?, '2026-01-01 00:00:00')",
            (OWNER_TG,),
        )
        connection.execute(
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (1, 1, '2026-01-01 00:00:00')"
        )
        personal = (
            (1, "Мой шаблон", V2_HEADER, V2_BODY, V2_FOOTER, 2),
            (2, "Рабочий шаблон", V2_HEADER, V2_BODY, V2_FOOTER, 2),
            (3, "Мой шаблон", "**[название]** — моя правка", V2_BODY, V2_FOOTER, 2),
            (5, "Мой шаблон", V2_HEADER, V2_BODY, V2_FOOTER, None),
        )
        for template_id, name, header, body, footer, builtin in personal:
            connection.execute(
                "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
                "created_by_user_id, created_at, updated_at) "
                "VALUES (?, 'personal', 1, ?, 1, 1, "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (template_id, name),
            )
            connection.execute(
                "INSERT INTO template_versions (id, template_id, version, header, "
                "body, footer, created_by_user_id, created_at, builtin_version) "
                "VALUES (?, ?, 1, ?, ?, ?, 1, '2026-01-01 00:00:00', ?)",
                (template_id, template_id, header, body, footer, builtin),
            )
        connection.execute(
            "INSERT INTO templates (id, kind, place_id, revision, created_at, "
            "updated_at) VALUES (4, 'standard', 1, 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO template_versions (id, template_id, version, header, body, "
            "footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (4, 4, 1, ?, ?, ?, NULL, '2026-01-01 00:00:00', 2)",
            (V2_HEADER, V2_BODY, V2_FOOTER),
        )
        connection.commit()
    finally:
        connection.close()


class _MigrationEighteenTestCase(unittest.TestCase):
    """Временная база версии 17 с нетронутыми и тронутыми копиями."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-2-r2-migration-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:17]), 17)
        _seed_version_seventeen(self.path)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)

    def _rows(self, statement: str, parameters: tuple = ()) -> list[tuple]:
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(statement, parameters).fetchall()
        finally:
            connection.close()


class MigrationEighteenTests(_MigrationEighteenTestCase):
    """Миграция 18 обновляет только доказуемо нетронутые копии."""

    def test_untouched_personal_copy_is_updated_and_renamed(self) -> None:
        self.assertEqual(LATEST_VERSION, 19)
        template = self.session.get(Template, 1)
        assert template is not None
        self.assertEqual(template.name, "Дефолтный шаблон")
        self.assertEqual(template.revision, 2)
        versions = list_template_versions(self.session, OWNER_TG, 1)
        self.assertEqual([item.version for item in versions], [1, 2])
        # Новая версия — ровно BUILTIN v3.
        self.assertEqual(versions[1].header, BUILTIN_TEMPLATE.header)
        self.assertEqual(versions[1].body, BUILTIN_TEMPLATE.body)
        self.assertEqual(versions[1].footer, BUILTIN_TEMPLATE.footer)
        self.assertEqual(versions[1].builtin_version, 3)
        self.assertEqual(
            {binding.code for binding in versions[1].bindings},
            {
                "название",
                "дата",
                "время_начала",
                "место",
                "ссылка_места",
                "адрес",
                "описание",
                "контакт",
            },
        )
        # Старая версия v2 не переписана (иммутабельность истории).
        self.assertEqual(versions[0].header, V2_HEADER)
        self.assertEqual(versions[0].body, V2_BODY)
        self.assertEqual(versions[0].footer, V2_FOOTER)
        self.assertEqual(versions[0].builtin_version, 2)

    def test_standard_copy_is_updated_with_previous_name(self) -> None:
        template = self.session.get(Template, 4)
        assert template is not None
        self.assertIsNone(template.name)
        self.assertEqual(template.revision, 2)
        current = get_standard_template(self.session, OWNER_TG, 1).current_version
        self.assertEqual(current.version, 2)
        self.assertEqual(current.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(current.footer, BUILTIN_TEMPLATE.footer)
        self.assertEqual(current.builtin_version, 3)

    def test_edited_copy_is_not_touched(self) -> None:
        template = self.session.get(Template, 3)
        assert template is not None
        self.assertEqual(template.name, "Мой шаблон")
        self.assertEqual(template.revision, 1)
        versions = list_template_versions(self.session, OWNER_TG, 3)
        self.assertEqual([item.version for item in versions], [1])
        self.assertEqual(versions[0].header, "**[название]** — моя правка")
        self.assertEqual(versions[0].builtin_version, 2)

    def test_renamed_but_untouched_copy_is_not_updated(self) -> None:
        """Правило этапа: обновляется только полностью нетронутая копия."""
        template = self.session.get(Template, 2)
        assert template is not None
        self.assertEqual(template.name, "Рабочий шаблон")
        self.assertEqual(template.revision, 1)
        versions = list_template_versions(self.session, OWNER_TG, 2)
        self.assertEqual([item.version for item in versions], [1])
        self.assertEqual(versions[0].body, V2_BODY)
        self.assertEqual(versions[0].builtin_version, 2)

    def test_untouched_initial_copy_without_marker_is_updated(self) -> None:
        """Стартовая копия v2 без маркера — тоже доказуемо нетронутая."""
        template = self.session.get(Template, 5)
        assert template is not None
        self.assertEqual(template.name, "Дефолтный шаблон")
        self.assertEqual(template.revision, 2)
        versions = list_template_versions(self.session, OWNER_TG, 5)
        self.assertEqual([item.version for item in versions], [1, 2])
        self.assertIsNone(versions[0].builtin_version)
        self.assertEqual(versions[1].body, BUILTIN_TEMPLATE.body)
        self.assertEqual(versions[1].builtin_version, 3)

    def test_user_copy_already_v3_keeps_old_name(self) -> None:
        """Копия v3 с именем «Мой шаблон» — создана человеком, не трогаем."""
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
                "created_by_user_id, created_at, updated_at) "
                "VALUES (100, 'personal', 1, 'Мой шаблон', 1, 1, "
                "'2026-01-02 00:00:00', '2026-01-02 00:00:00')"
            )
            connection.execute(
                "INSERT INTO template_versions (id, template_id, version, header, "
                "body, footer, created_by_user_id, created_at, builtin_version) "
                "VALUES (100, 100, 1, ?, ?, ?, 1, '2026-01-02 00:00:00', 3)",
                (
                    BUILTIN_TEMPLATE.header,
                    BUILTIN_TEMPLATE.body,
                    BUILTIN_TEMPLATE.footer,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        template = self.session.get(Template, 100)
        assert template is not None
        self.assertEqual(template.name, "Мой шаблон")
        self.assertEqual(template.revision, 1)

    def test_repeat_run_changes_nothing(self) -> None:
        before = self._rows(
            "SELECT id, template_id, version, header, body, footer, builtin_version "
            "FROM template_versions ORDER BY id"
        )
        revisions = self._rows("SELECT id, name, revision FROM templates ORDER BY id")
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            self._rows(
                "SELECT id, template_id, version, header, body, footer, "
                "builtin_version FROM template_versions ORDER BY id"
            ),
            before,
        )
        self.assertEqual(
            self._rows("SELECT id, name, revision FROM templates ORDER BY id"),
            revisions,
        )
        self.assertEqual(
            self._rows(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'migration_%'"
            ),
            [],
        )

    def test_integrity_and_foreign_keys_after_migration(self) -> None:
        self.assertEqual(self._rows("PRAGMA foreign_key_check"), [])
        self.assertEqual(self._rows("PRAGMA integrity_check"), [("ok",)])
        # У каждой версии нет чужих/потерянных привязок.
        orphan = self._rows(
            "SELECT count(*) FROM template_version_bricks AS vb "
            "WHERE NOT EXISTS (SELECT 1 FROM template_versions AS v "
            "WHERE v.id = vb.template_version_id)"
        )
        self.assertEqual(orphan, [(0,)])

    def test_snapshots_are_not_rewritten(self) -> None:
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=1,
            expected_revision=2,
        )
        row = self.session.get(TemplateSnapshot, snapshot.snapshot_id)
        assert row is not None
        self.assertEqual(row.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(self._rows("SELECT count(*) FROM template_snapshots"), [(1,)])


class ResetToBuiltinAfterMigrationTests(_MigrationEighteenTestCase):
    """«Восстановить стандарт» после v3 создаёт версию с `builtin_version=3`."""

    def test_personal_reset_creates_v3_version(self) -> None:
        updated = update_template_header(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=1,
            expected_revision=2,
            content="Своя шапка",
        )
        self.assertEqual(updated.status, "updated")
        result = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=1,
            expected_revision=3,
            confirm=True,
        )
        self.assertEqual(result.status, "updated")
        assert result.template is not None
        current = result.template.current_version
        self.assertEqual(current.version, 4)
        self.assertEqual(current.header, BUILTIN_TEMPLATE.header)
        self.assertEqual(current.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(current.footer, BUILTIN_TEMPLATE.footer)
        self.assertEqual(current.builtin_version, BUILTIN_TEMPLATE_VERSION)

    def test_untouched_personal_copy_is_unchanged_on_reset(self) -> None:
        """Копия уже совпадает с v3 — повторный сброс версию не плодит."""
        result = reset_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=1,
            expected_revision=2,
            confirm=True,
        )
        self.assertEqual(result.status, "unchanged")
        self.assertEqual(
            self._rows("SELECT count(*) FROM template_versions WHERE template_id=1"),
            [(2,)],
        )

    def test_reset_is_selectable_for_both_kinds(self) -> None:
        """Карточки личного шаблона и стандарта места дают одно действие сброса."""
        personal = get_personal_template(self.session, OWNER_TG, 1)
        standard = get_standard_template(self.session, OWNER_TG, 1)
        assert personal is not None
        self.assertEqual(personal.kind, "personal")
        self.assertEqual(standard.kind, "standard")


class InitialPersonalTemplateV3Tests(unittest.TestCase):
    """Новый пользователь сразу получает «Дефолтный шаблон» с текстом v3."""

    def test_initial_template_name_and_content(self) -> None:
        path = temp_db_path("poster-2-r2-initial-")
        self.addCleanup(shutil.rmtree, os.path.dirname(path), True)
        engine = make_engine(path)
        self.addCleanup(engine.dispose)
        run_migrations(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False)()
        self.addCleanup(session.close)
        get_or_create_user(session, OWNER_TG, username="r2", full_name="R2 Owner")

        created = ensure_initial_personal_template(
            session, actor_telegram_user_id=OWNER_TG
        )
        self.assertEqual(INITIAL_PERSONAL_TEMPLATE_NAME, "Дефолтный шаблон")
        self.assertEqual(created.name, "Дефолтный шаблон")
        version = created.current_version
        self.assertEqual(version.header, BUILTIN_TEMPLATE.header)
        self.assertEqual(version.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(version.footer, BUILTIN_TEMPLATE.footer)
        # Стартовая копия — не «сброс»: маркер построенной версии пуст.
        self.assertIsNone(version.builtin_version)
        # Привязки: системные коды резолвятся по коду, а не хардкодом ID.
        expected = {
            code: get_system_brick(session, code).id  # type: ignore[union-attr]
            for code in (
                "название",
                "дата",
                "время_начала",
                "место",
                "ссылка_места",
                "адрес",
                "описание",
                "контакт",
            )
        }
        self.assertEqual(
            {binding.code: binding.brick_id for binding in version.bindings},
            expected,
        )


class BuiltinBindingCatalogTests(unittest.TestCase):
    """BUILTIN v3 использует те же восемь системных кодов, что и v2."""

    def test_builtin_header_has_no_trailing_newline(self) -> None:
        self.assertFalse(BUILTIN_TEMPLATE.header.endswith("\n"))
        self.assertTrue(BUILTIN_TEMPLATE.body.startswith("\n"))
        self.assertTrue(BUILTIN_TEMPLATE.footer.startswith("\n"))

    def test_bindings_are_codes_of_builtin_text(self) -> None:
        combined = BUILTIN_TEMPLATE.header + BUILTIN_TEMPLATE.body
        combined += BUILTIN_TEMPLATE.footer
        for binding in BUILTIN_TEMPLATE.bindings:
            with self.subTest(code=binding.code):
                self.assertIn(f"[{binding.code}]", combined)
