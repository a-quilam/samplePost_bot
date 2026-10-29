# tests/test_poster2_u1a_migrations.py
"""
U1a — миграции 21 (`place_city`) и 22 (`builtin_templates_v4`).

Проверяется фактическое поведение на ЗАПОЛНЕННОЙ базе предыдущей версии:

- миграция 20 → 21: добавочные колонки `places.city` и
  `announcement_drafts.place_city` (обе nullable), системный кирпичик
  «город» (`scope='system'`, `source='place'`, `type='short_text'`),
  прежние места/черновики не переписаны, `city` у них NULL, триггер
  резервирования кодов восстановлен и снова блокирует подмену системного
  кода, повторный запуск — no-op, `PRAGMA integrity_check`/`foreign_key_check`
  чистые;
- миграция 21 → 22: нетронутые копии эталона v3 получают ровно версию 4
  (`builtin_version = 4`), отредактированные копии (в т.ч. с
  `builtin_version IS NULL`) не трогаются, копия «Дефолтный шаблон» с
  нетронутым текстом обновляется, снимки анонсов и старые версии не
  переписываются, повторный прогон — no-op.

Эталоны v3/v4 записаны здесь ЛИТЕРАЛЬНО (без импорта констант продукта),
чтобы миграция проверялась независимо от текущего `template_types`.
Реальная `post_bot.db` не открывается: только временные файлы SQLite.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest

from brick_types import SYSTEM_BRICKS
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from tests.stage19a_helpers import make_engine, temp_db_path

# --- Эталоны встроенного шаблона (литерально, не из template_types) --------

V3_HEADER = "**[название]**"
V3_BODY = (
    "\n"
    "**[дата] // [время_начала]**\n"
    "\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
V3_FOOTER = "\n[контакт]"

V4_HEADER = "**[название]**\n\n**[дата] // [время_начала]**\n**[место]([ссылка_места])**\n[адрес]"
V4_BODY = "\n[описание]"
V4_FOOTER = "\n[контакт]"

# Восемь системных кодов обеих версий: набор привязок не меняется.
EXPECTED_CODES = {
    "название",
    "дата",
    "время_начала",
    "место",
    "ссылка_места",
    "адрес",
    "описание",
    "контакт",
}

INITIAL_TEMPLATE_NAME = "Дефолтный шаблон"

OWNER_TG = 9_841_001
OTHER_TG = 9_841_002


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
    """PRAGMA table_info в виде «имя → строка pragma»."""
    return {str(row[1]): row for row in _sql(path, f"PRAGMA table_info({table})")}


def _seed_user(path: str, user_id: int, telegram_user_id: int) -> None:
    _write(
        path,
        "INSERT INTO users (id, telegram_user_id, created_at) "
        "VALUES (?, ?, '2026-01-01 00:00:00')",
        (user_id, telegram_user_id),
    )


def _seed_place(
    path: str,
    place_id: int,
    *,
    created_by_user_id: int | None = 1,
    address: str | None = None,
    url: str | None = None,
) -> None:
    """Место схемы версии 20: колонки `city` ещё нет."""
    _write(
        path,
        "INSERT INTO places (id, created_by_user_id, address, url, created_at) "
        "VALUES (?, ?, ?, ?, '2026-01-01 00:00:00')",
        (place_id, created_by_user_id, address, url),
    )


class _Base(unittest.TestCase):
    """Временная база и помощники; подклассы задают точку старта."""

    start_version = 20

    def setUp(self) -> None:
        self.path = temp_db_path("poster-u1a-migration-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(
            run_migrations(self.engine, DEFAULT_MIGRATIONS[: self.start_version]),
            self.start_version,
        )

    def migrate(self) -> int:
        return run_migrations(self.engine)

    def journal(self) -> list[tuple[int, str]]:
        return [
            (int(row[0]), str(row[1]))
            for row in _sql(
                self.path,
                "SELECT version, name FROM schema_migrations ORDER BY version",
            )
        ]

    def assert_clean(self) -> None:
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])


# --- Версия 20 → 21 ---------------------------------------------------------


class PlaceCityMigrationTests(_Base):
    """Миграция 21 на заполненной базе версии 20."""

    start_version = 20

    def setUp(self) -> None:
        super().setUp()
        _seed_user(self.path, 1, OWNER_TG)
        # Старое место с адресом/ссылкой и без города: миграция не должна
        # ни backfill-ить его, ни терять данные.
        _seed_place(
            self.path,
            5,
            address="ул. Старая, 3",
            url="https://old.example/place",
        )
        _write(
            self.path,
            "INSERT INTO announcement_drafts "
            "(id, author_user_id, place_id, place_display_name, place_address, "
            "origin, stage, revision, created_at, updated_at) VALUES "
            "(9, 1, 5, 'Старое место', 'ул. Старая, 3', 'place', 'place', 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        )

    def test_latest_version_is_twenty_two(self) -> None:
        self.assertEqual(LATEST_VERSION, 22)
        self.assertEqual(self.migrate(), LATEST_VERSION)
        journal = self.journal()
        self.assertIn((21, "place_city"), journal)
        self.assertEqual(journal[-1], (22, "builtin_templates_v4"))

    def test_columns_are_added_nullable_and_rows_keep_null_city(self) -> None:
        self.migrate()
        place_columns = _columns(self.path, "places")
        self.assertIn("city", place_columns)
        self.assertEqual(int(place_columns["city"][3]), 0, "city обязана быть nullable")
        self.assertEqual(str(place_columns["city"][2]), "TEXT")

        draft_columns = _columns(self.path, "announcement_drafts")
        self.assertIn("place_city", draft_columns)
        self.assertEqual(int(draft_columns["place_city"][3]), 0)

        self.assertEqual(
            _sql(
                self.path,
                "SELECT address, url, city, created_by_user_id "
                "FROM places WHERE id = 5",
            ),
            [("ул. Старая, 3", "https://old.example/place", None, 1)],
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT place_address, place_url, place_city, place_display_name "
                "FROM announcement_drafts WHERE id = 9",
            ),
            [("ул. Старая, 3", None, None, "Старое место")],
        )
        self.assert_clean()

    def test_empty_city_is_rejected_by_check_constraint(self) -> None:
        self.migrate()
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE announcement_drafts SET place_city = '   ' " "WHERE id = 9"
                )
        finally:
            connection.close()
        self.assertEqual(
            _sql(self.path, "SELECT place_city FROM announcement_drafts"),
            [(None,)],
        )

    def test_city_brick_is_system_and_described_by_code(self) -> None:
        self.migrate()
        rows = _sql(
            self.path,
            "SELECT code, title, type, scope, source, status, place_id, "
            "owner_user_id FROM bricks WHERE code = 'город'",
        )
        self.assertEqual(
            rows,
            [("город", "Город", "short_text", "system", "place", "active", None, None)],
        )
        # Логический ID каталога — 11, но чтение сверяет код, а не номер.
        catalog = [
            (item.id, item.code, item.value_type, item.source)
            for item in SYSTEM_BRICKS
            if item.code == "город"
        ]
        self.assertEqual(catalog, [(11, "город", "short_text", "place")])

    def test_city_brick_gets_free_id_when_eleven_is_taken(self) -> None:
        """Пользовательский кирпичик с id=11 не перезаписывается."""
        _write(
            self.path,
            "INSERT INTO bricks "
            "(id, code, title, type, scope, source, rules_json, status, "
            "owner_user_id, created_by_user_id, created_at, revision) VALUES "
            "(11, 'мой', 'Мой', 'short_text', 'personal', 'ask', NULL, "
            "'active', 1, 1, '2026-01-01 00:00:00', 1)",
        )
        self.migrate()
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, scope FROM bricks WHERE code = 'мой'",
            ),
            [(11, "personal")],
        )
        city = _sql(
            self.path,
            "SELECT id, scope, source FROM bricks WHERE code = 'город'",
        )
        self.assertEqual(len(city), 1)
        self.assertEqual(city[0][1:], ("system", "place"))
        self.assertGreater(int(city[0][0]), 11)
        self.assert_clean()

    def test_reserved_codes_trigger_is_restored(self) -> None:
        triggers = {
            str(row[0])
            for row in _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type = 'trigger'",
            )
        }
        self.assertIn("trg_bricks_reserve_codes_insert", triggers)
        self.migrate()
        triggers = {
            str(row[0])
            for row in _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type = 'trigger'",
            )
        }
        self.assertIn("trg_bricks_reserve_codes_insert", triggers)
        # Системный код «город» остаётся зарезервированным для личных.
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO bricks "
                    "(code, title, type, scope, source, rules_json, status, "
                    "owner_user_id, created_by_user_id, created_at, revision) "
                    "VALUES ('город', 'Свой город', 'short_text', 'personal', "
                    "'ask', NULL, 'active', 1, 1, '2026-01-01 00:00:00', 1)"
                )
        finally:
            connection.close()

    def test_repeat_run_of_migration_twenty_one_is_noop(self) -> None:
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:21]), 21)
        bricks = _sql(
            self.path,
            "SELECT id, code, title, type, scope, source, status FROM bricks "
            "ORDER BY id",
        )
        places = _sql(self.path, "SELECT id, address, url, city FROM places")
        draft_columns = list(_columns(self.path, "announcement_drafts"))
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:21]), 21)
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, code, title, type, scope, source, status FROM bricks "
                "ORDER BY id",
            ),
            bricks,
        )
        self.assertEqual(
            _sql(self.path, "SELECT id, address, url, city FROM places"), places
        )
        self.assertEqual(
            list(_columns(self.path, "announcement_drafts")), draft_columns
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT count(*) FROM bricks WHERE code = 'город' AND scope = 'system'",
            ),
            [(1,)],
        )
        self.assertEqual(len(self.journal()), 21)
        self.assert_clean()


# --- Версия 21 → 22 ---------------------------------------------------------


class BuiltinTemplatesV4MigrationTests(_Base):
    """Миграция 22 обновляет только доказуемо нетронутые копии v3."""

    start_version = 21

    def setUp(self) -> None:
        super().setUp()
        _seed_user(self.path, 1, OWNER_TG)
        _seed_user(self.path, 2, OTHER_TG)
        _seed_place(self.path, 1)
        self._seed_templates()

    # --- фикстуры ---------------------------------------------------------

    def _seed_personal(
        self,
        template_id: int,
        *,
        name: str,
        header: str = V3_HEADER,
        body: str = V3_BODY,
        footer: str = V3_FOOTER,
        builtin: int | None = 3,
        owner_user_id: int = 1,
        revision: int = 1,
    ) -> None:
        _write(
            self.path,
            "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
            "created_by_user_id, created_at, updated_at) VALUES "
            "(?, 'personal', ?, ?, ?, 1, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')",
            (template_id, owner_user_id, name, revision),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, '2026-01-01 00:00:00', ?)",
            (template_id, template_id, revision, header, body, footer, builtin),
        )
        self._bind(template_id)

    def _seed_standard(self, template_id: int, *, place_id: int = 1) -> None:
        _write(
            self.path,
            "INSERT INTO templates (id, kind, place_id, revision, created_at, "
            "updated_at) VALUES (?, 'standard', ?, 1, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')",
            (template_id, place_id),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (?, ?, 1, ?, ?, ?, NULL, '2026-01-01 00:00:00', 3)",
            (template_id, template_id, V3_HEADER, V3_BODY, V3_FOOTER),
        )
        self._bind(template_id)

    def _bind(self, template_id: int) -> None:
        """Привязки всех версий — по коду из `bricks`, как их пишет сервис."""
        _write(
            self.path,
            "INSERT INTO template_version_bricks "
            "(template_version_id, brick_id, code) "
            "SELECT v.id, b.id, b.code FROM template_versions AS v "
            "JOIN bricks AS b ON b.scope = 'system' AND b.code IN "
            "('название', 'дата', 'время_начала', 'место', 'ссылка_места', "
            "'адрес', 'описание', 'контакт') WHERE v.template_id = ?",
            (template_id,),
        )

    def _seed_templates(self) -> None:
        # 1. Нетронутая копия эталона v3 с маркером и стартовым именем —
        #    обновляется по маркеру версии.
        self._seed_personal(1, name=INITIAL_TEMPLATE_NAME)
        # 2. Нетронутая копия, потерявшая маркер (NULL) и названная
        #    миграцией 18 «Дефолтный шаблон» — обновляется по эвристике имени.
        self._seed_personal(2, name=INITIAL_TEMPLATE_NAME, builtin=None)
        # 3. Тот же NULL и то же имя, но текст правлен — не трогаем.
        self._seed_personal(
            3,
            name=INITIAL_TEMPLATE_NAME,
            header="**[название]** — моё",
            builtin=None,
        )
        # 4. Нетронутый по тексту, но личный шаблон с ДРУГИМ именем и NULL:
        #    доказуемости нет — не трогаем.
        self._seed_personal(4, name="Копия для концертов", builtin=None)
        # 5. Правленая копия с маркером v3 — не трогаем.
        self._seed_personal(
            5,
            name=INITIAL_TEMPLATE_NAME,
            body=V3_BODY + "\n\nмоя подпись",
        )
        # 6. Нетронутый стандарт места — обновляется, имя остаётся NULL.
        self._seed_standard(6)
        # 7. Переименованная человеком копия с маркером: переименование —
        #    осознанная правка (прецедент миграции 18), копию не трогаем.
        self._seed_personal(7, name="Копия для концертов")
        # 8. Снимок анонса, снятый с нетронутой копии до миграции.
        _write(
            self.path,
            "INSERT INTO template_snapshots "
            "(id, owner_user_id, source_template_id, source_template_version, "
            "source_kind, header, body, footer, builtin_version, created_at) "
            "VALUES (90, 1, 1, 1, 'personal', ?, ?, ?, 3, "
            "'2026-01-01 00:00:00')",
            (V3_HEADER, V3_BODY, V3_FOOTER),
        )

    # --- помощники чтения -------------------------------------------------

    def versions(self, template_id: int) -> list[tuple]:
        return _sql(
            self.path,
            "SELECT version, header, body, footer, builtin_version "
            "FROM template_versions WHERE template_id = ? ORDER BY version",
            (template_id,),
        )

    def template_row(self, template_id: int) -> tuple:
        return _sql(
            self.path,
            "SELECT name, revision FROM templates WHERE id = ?",
            (template_id,),
        )[0]

    # --- проверки ---------------------------------------------------------

    def test_untouched_copy_gets_exact_v4_version(self) -> None:
        self.migrate()
        rows = self.versions(1)
        self.assertEqual(len(rows), 2, "v3 остаётся в истории")
        self.assertEqual(rows[0], (1, V3_HEADER, V3_BODY, V3_FOOTER, 3))
        self.assertEqual(rows[1], (2, V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(self.template_row(1), (INITIAL_TEMPLATE_NAME, 2))

    def test_untouched_default_named_copy_without_marker_is_updated(self) -> None:
        self.migrate()
        rows = self.versions(2)
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(rows[1][1:], (V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(self.template_row(2), (INITIAL_TEMPLATE_NAME, 2))

    def test_edited_default_named_copy_is_not_touched(self) -> None:
        self.migrate()
        rows = self.versions(3)
        self.assertEqual(len(rows), 1, "правленый текст не перезаписывается")
        self.assertEqual(rows[0][1], "**[название]** — моё")
        self.assertIsNone(rows[0][4])
        self.assertEqual(self.template_row(3), (INITIAL_TEMPLATE_NAME, 1))

    def test_untouched_copy_with_other_name_is_not_touched(self) -> None:
        self.migrate()
        self.assertEqual(len(self.versions(4)), 1)
        self.assertEqual(self.template_row(4), ("Копия для концертов", 1))

    def test_edited_copy_with_marker_is_not_touched(self) -> None:
        self.migrate()
        rows = self.versions(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 3)
        self.assertEqual(rows[0][2], V3_BODY + "\n\nмоя подпись")
        self.assertEqual(self.template_row(5), (INITIAL_TEMPLATE_NAME, 1))

    def test_renamed_copy_with_marker_is_not_touched(self) -> None:
        self.migrate()
        self.assertEqual(len(self.versions(7)), 1)
        self.assertEqual(self.template_row(7), ("Копия для концертов", 1))

    def test_untouched_standard_is_updated_and_keeps_null_name(self) -> None:
        self.migrate()
        rows = self.versions(6)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1:], (V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(self.template_row(6), (None, 2))

    def test_new_version_bindings_are_complete_and_resolved_by_code(self) -> None:
        self.migrate()
        for template_id in (1, 2, 6):
            with self.subTest(template_id=template_id):
                bindings = _sql(
                    self.path,
                    "SELECT b.code, b.scope, vb.brick_id, b.id "
                    "FROM template_version_bricks AS vb "
                    "JOIN template_versions AS v ON v.id = vb.template_version_id "
                    "JOIN bricks AS b ON b.id = vb.brick_id "
                    "WHERE v.template_id = ? AND v.version = 2",
                    (template_id,),
                )
                self.assertEqual({row[0] for row in bindings}, EXPECTED_CODES)
                self.assertEqual({row[1] for row in bindings}, {"system"})
                self.assertTrue(
                    all(row[2] == row[3] for row in bindings),
                    "brick_id берётся из bricks по коду, а не хардкодится",
                )
                self.assertNotIn("город", {row[0] for row in bindings})

    def test_snapshots_and_history_are_not_rewritten(self) -> None:
        self.migrate()
        self.assertEqual(
            _sql(
                self.path,
                "SELECT header, body, footer, builtin_version, "
                "source_template_version FROM template_snapshots WHERE id = 90",
            ),
            [(V3_HEADER, V3_BODY, V3_FOOTER, 3, 1)],
        )
        # Версия-предок осталась на месте и не изменилась.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT header, body, footer FROM template_versions "
                "WHERE template_id = 1 AND version = 1",
            ),
            [(V3_HEADER, V3_BODY, V3_FOOTER)],
        )
        # Ни одного шаблона без текущей версии.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT count(*) FROM templates AS t WHERE NOT EXISTS "
                "(SELECT 1 FROM template_versions AS v "
                "WHERE v.template_id = t.id AND v.version = t.revision)",
            ),
            [(0,)],
        )
        self.assert_clean()

    def test_repeat_run_is_noop(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        templates = _sql(
            self.path, "SELECT id, kind, name, revision FROM templates ORDER BY id"
        )
        versions = _sql(
            self.path,
            "SELECT id, template_id, version, header, body, footer, "
            "builtin_version FROM template_versions ORDER BY id",
        )
        bindings = _sql(
            self.path,
            "SELECT template_version_id, brick_id, code "
            "FROM template_version_bricks ORDER BY template_version_id, brick_id",
        )
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, kind, name, revision FROM templates ORDER BY id",
            ),
            templates,
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, template_id, version, header, body, footer, "
                "builtin_version FROM template_versions ORDER BY id",
            ),
            versions,
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT template_version_id, brick_id, code "
                "FROM template_version_bricks "
                "ORDER BY template_version_id, brick_id",
            ),
            bindings,
        )
        # Временная таблица-очередь не остаётся в схеме.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'migration_%'",
            ),
            [],
        )
        self.assert_clean()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
