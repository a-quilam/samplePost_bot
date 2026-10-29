# tests/test_poster2_r2_migration_tester.py
"""
R2 (Poster 2.1) — независимая проверка миграции 18 (BUILTIN v3).

Требование владельца:
- обновляются ТОЛЬКО нетронутые копии прежнего эталона v2 (содержимое
  побайтово = старый эталон v2 + маркер `builtin_version = 2`);
- имя стартовой личной копии «Мой шаблон» меняется на «Дефолтный шаблон»
  только в той же транзакции, что и текст (переименования без обновления
  не бывает);
- отредактированные человеком копии не трогаются;
- повторный запуск ничего не меняет;
- FK/integrity целы, старые версии и снимки не переписаны.

Эталоны v2/v3 записаны здесь ЛИТЕРАЛЬНО (не импортом констант продукта),
чтобы миграция проверялась независимо от текущего `template_types`.
Временная SQLite: реальная `post_bot.db` не открывается.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import unittest

from sqlalchemy.orm import sessionmaker

from migrations import (
    DEFAULT_MIGRATIONS,
    LATEST_VERSION,
    MigrationError,
    run_migrations,
)
from tests.stage19a_helpers import make_engine, temp_db_path

# Прежний эталон v2 (Poster 2.0 S1): именно это лежит в нетронутых копиях.
V2_HEADER = "**[название]**"
V2_BODY = (
    "[дата] // [время_начала]\n"
    "[место]([ссылка_места])\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
V2_FOOTER = "[контакт]"

# Эталон v3 (Часть III) — что должно оказаться у нетронутых копий.
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

# Эталон v4 (Poster 2.0 U1a): дата/время идут сразу под заголовком, тело —
# только описание, подвал — только контакт. Именно это получают нетронутые
# копии после миграции 22.
V4_HEADER = "**[название]**\n\n**[дата] // [время_начала]**\n**[место]([ссылка_места])**\n[адрес]"
V4_BODY = "\n[описание]"
V4_FOOTER = "\n[контакт]"

OLD_INITIAL_NAME = "Мой шаблон"
NEW_INITIAL_NAME = "Дефолтный шаблон"

OWNER_TG = 9_812_001
OTHER_TG = 9_812_002

# Восемь системных кодов эталона: привязки обязаны быть полными.
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


def _sql(path: str, statement: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement, parameters).fetchall()
    finally:
        connection.close()


def _write(path: str, statement: str, parameters: tuple = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


def _seed_personal(
    path: str,
    template_id: int,
    *,
    name: str,
    header: str = V2_HEADER,
    body: str = V2_BODY,
    footer: str = V2_FOOTER,
    builtin: int | None = 2,
    owner_user_id: int = 1,
    revision: int = 1,
) -> None:
    """Личная копия владельца с одной текущей версией."""
    _write(
        path,
        "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
        "created_by_user_id, created_at, updated_at) "
        "VALUES (?, 'personal', ?, ?, ?, 1, '2026-01-01 00:00:00', "
        "'2026-01-01 00:00:00')",
        (template_id, owner_user_id, name, revision),
    )
    _write(
        path,
        "INSERT INTO template_versions (id, template_id, version, header, body, "
        "footer, created_by_user_id, created_at, builtin_version) "
        "VALUES (?, ?, ?, ?, ?, ?, 1, '2026-01-01 00:00:00', ?)",
        (template_id, template_id, revision, header, body, footer, builtin),
    )
    _bind_version(path, template_id)


def _bind_version(path: str, template_id: int) -> None:
    """Привязки всех версий шаблона — по коду из `bricks`, как их пишет сервис."""
    _write(
        path,
        "INSERT INTO template_version_bricks (template_version_id, brick_id, code) "
        "SELECT v.id, b.id, b.code FROM template_versions AS v "
        "JOIN bricks AS b ON b.scope = 'system' "
        "AND b.code IN (SELECT code FROM (SELECT 'название' AS code UNION ALL "
        "SELECT 'дата' UNION ALL SELECT 'время_начала' UNION ALL "
        "SELECT 'место' UNION ALL SELECT 'ссылка_места' UNION ALL "
        "SELECT 'адрес' UNION ALL SELECT 'описание' UNION ALL "
        "SELECT 'контакт')) "
        "WHERE v.template_id = ?",
        (template_id,),
    )


class _Base(unittest.TestCase):
    """Временная база: миграции 1–17, затем заполнение «настоящими» копиями."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-r2-migration-tester-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:17]), 17)
        _write(
            self.path,
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, ?, '2026-01-01 00:00:00')",
            (OWNER_TG,),
        )
        _write(
            self.path,
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (2, ?, '2026-01-01 00:00:00')",
            (OTHER_TG,),
        )
        _write(
            self.path,
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (1, 1, '2026-01-01 00:00:00')",
        )
        self._seed()

    def _seed(self) -> None:
        """Копии прежнего эталона: нетронутые, тронутые и переименованные."""
        # 1. Нетронутая стартовая личная копия с маркером v2 — обновляется
        #    и переименовывается.
        _seed_personal(self.path, 1, name=OLD_INITIAL_NAME)
        # 2. Нетронутая стартовая копия без маркера (так её выдавал старый
        #    сервис: builtin_version IS NULL) — тоже обновляется.
        _seed_personal(self.path, 2, name=OLD_INITIAL_NAME, builtin=None)
        # 3. Тронутая человеком шапка — не трогаем, имя тоже.
        _seed_personal(
            self.path, 3, name=OLD_INITIAL_NAME, header="**[название] — моё**"
        )
        # 4. Тронутое тело — не трогаем.
        _seed_personal(
            self.path,
            4,
            name="Рабочий",
            body=V2_BODY + "\n\nмоя подпись",
        )
        # 5. Тронутый подвал — не трогаем.
        _seed_personal(self.path, 5, name="Рабочий", footer=V2_FOOTER + " · мой")
        # 6. Переименованная, но нетронутая по тексту копия — не трогаем.
        _seed_personal(self.path, 6, name="Копия для концертов")
        # 7. Нетронутый стандарт места — обновляется, имя остаётся NULL.
        _write(
            self.path,
            "INSERT INTO templates (id, kind, place_id, revision, created_at, "
            "updated_at) VALUES (7, 'standard', 1, 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (7, 7, 1, ?, ?, ?, NULL, '2026-01-01 00:00:00', 2)",
            (V2_HEADER, V2_BODY, V2_FOOTER),
        )
        _bind_version(self.path, 7)
        # 8. Личная копия: текущая версия v2, но ревизия уже указывает на
        #    более новую (правка человека). Не трогаем.
        _write(
            self.path,
            "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
            "created_by_user_id, created_at, updated_at) "
            "VALUES (8, 'personal', 2, ?, 2, 2, "
            "'2026-01-01 00:00:00', '2026-01-02 00:00:00')",
            (OLD_INITIAL_NAME,),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (80, 8, 1, ?, ?, ?, 2, '2026-01-01 00:00:00', 2)",
            (V2_HEADER, V2_BODY, V2_FOOTER),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (81, 8, 2, '**[название]** — правка', ?, ?, 2, "
            "'2026-01-02 00:00:00', NULL)",
            (V2_BODY, V2_FOOTER),
        )
        _bind_version(self.path, 8)

    def migrate(self) -> int:
        return run_migrations(self.engine)

    def templates(self) -> list[tuple]:
        return _sql(
            self.path, "SELECT id, kind, name, revision FROM templates ORDER BY id"
        )

    def versions(self, template_id: int) -> list[tuple]:
        return _sql(
            self.path,
            "SELECT version, header, body, footer, builtin_version "
            "FROM template_versions WHERE template_id = ? ORDER BY version",
            (template_id,),
        )


class MigrationEighteenContentTests(_Base):
    """Кто получает новую версию и кто — нет."""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(self.migrate(), LATEST_VERSION)

    def test_latest_version_is_twenty(self) -> None:
        # R3 добавил миграцию 19, R4 — миграцию 20; миграция 18 остаётся
        # на своём месте, а последняя в журнале — 20.
        self.assertEqual(LATEST_VERSION, 22)
        versions = _sql(
            self.path,
            "SELECT version, name FROM schema_migrations ORDER BY version",
        )
        self.assertIn((18, "builtin_templates_v3"), versions)
        self.assertEqual(versions[-1], (22, "builtin_templates_v4"))

    def test_untouched_initial_copy_gets_v4_and_new_name(self) -> None:
        rows = self.versions(1)
        self.assertEqual(len(rows), 3, "обе прежние версии остаются в истории")
        self.assertEqual(rows[0], (1, V2_HEADER, V2_BODY, V2_FOOTER, 2))
        self.assertEqual(rows[1], (2, V3_HEADER, V3_BODY, V3_FOOTER, 3))
        self.assertEqual(rows[2], (3, V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 1"),
            [(NEW_INITIAL_NAME, 3)],
        )

    def test_untouched_initial_copy_without_marker_is_updated(self) -> None:
        rows = self.versions(2)
        self.assertEqual([row[0] for row in rows], [1, 2, 3])
        self.assertEqual(rows[1][1:], (V3_HEADER, V3_BODY, V3_FOOTER, 3))
        self.assertEqual(rows[2][1:], (V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 2"),
            [(NEW_INITIAL_NAME, 3)],
        )

    def test_untouched_standard_is_updated_and_keeps_null_name(self) -> None:
        rows = self.versions(7)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][1:], (V3_HEADER, V3_BODY, V3_FOOTER, 3))
        self.assertEqual(rows[2][1:], (V4_HEADER, V4_BODY, V4_FOOTER, 4))
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 7"),
            [(None, 3)],
        )

    def test_edited_copies_are_not_touched(self) -> None:
        for template_id, expected_name in (
            (3, OLD_INITIAL_NAME),
            (4, "Рабочий"),
            (5, "Рабочий"),
        ):
            with self.subTest(template_id=template_id):
                rows = self.versions(template_id)
                self.assertEqual(len(rows), 1, "новой версии быть не должно")
                self.assertEqual(rows[0][4], 2)
                self.assertEqual(
                    _sql(
                        self.path,
                        "SELECT name, revision FROM templates WHERE id = ?",
                        (template_id,),
                    ),
                    [(expected_name, 1)],
                )

    def test_renamed_untouched_copy_is_not_touched(self) -> None:
        self.assertEqual(len(self.versions(6)), 1)
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 6"),
            [("Копия для концертов", 1)],
        )

    def test_rename_never_happens_without_content_update(self) -> None:
        """Имя «Мой шаблон» остаётся, если текст не обновился."""
        statuses = dict(
            (row[0], row)
            for row in _sql(self.path, "SELECT id, name, revision FROM templates")
        )
        # 3 — тронутая копия с именем «Мой шаблон»: имя сохранилось.
        self.assertEqual(statuses[3][1], OLD_INITIAL_NAME)
        self.assertEqual(statuses[3][2], 1)
        # 8 — копия с новейшей правкой и именем «Мой шаблон».
        self.assertEqual(statuses[8][1], OLD_INITIAL_NAME)
        self.assertEqual(statuses[8][2], 2)
        # Нет ни одного шаблона, переименованного без новой версии.
        renamed_without_version = _sql(
            self.path,
            "SELECT t.id FROM templates AS t WHERE t.name = ? " "AND t.revision = 1",
            (NEW_INITIAL_NAME,),
        )
        self.assertEqual(renamed_without_version, [])

    def test_non_current_version_is_not_used_as_candidate(self) -> None:
        """У копии 8 текущая версия — 2 (правка), а v2-текст лежит в v1."""
        rows = self.versions(8)
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(rows[1][4], None)
        self.assertEqual(
            _sql(self.path, "SELECT revision FROM templates WHERE id = 8"), [(2,)]
        )

    def test_new_version_bindings_are_complete_and_resolved_by_code(self) -> None:
        for template_id, new_version in (
            (1, 2),
            (1, 3),
            (2, 2),
            (2, 3),
            (7, 2),
            (7, 3),
        ):
            with self.subTest(template_id=template_id):
                bindings = _sql(
                    self.path,
                    "SELECT b.code, b.scope, vb.brick_id, b.id "
                    "FROM template_version_bricks AS vb "
                    "JOIN template_versions AS v ON v.id = vb.template_version_id "
                    "JOIN bricks AS b ON b.id = vb.brick_id "
                    "WHERE v.template_id = ? AND v.version = ?",
                    (template_id, new_version),
                )
                self.assertEqual({row[0] for row in bindings}, EXPECTED_CODES)
                self.assertEqual({row[1] for row in bindings}, {"system"})
                self.assertTrue(
                    all(row[2] == row[3] for row in bindings),
                    "brick_id берётся из bricks по коду, а не подставляется числом",
                )

    def test_snapshots_are_not_rewritten(self) -> None:
        self.assertEqual(
            _sql(self.path, "SELECT count(*) FROM template_snapshots"), [(0,)]
        )


class MigrationEighteenRepeatTests(_Base):
    """Повторный запуск и целостность."""

    def test_repeat_run_changes_nothing(self) -> None:
        self.assertEqual(self.migrate(), LATEST_VERSION)
        templates = self.templates()
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
        self.assertEqual(self.templates(), templates)
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
                "FROM template_version_bricks ORDER BY template_version_id, brick_id",
            ),
            bindings,
        )
        # Временная таблица-очередь не остаётся в схеме.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'migration_%'",
            ),
            [],
        )

    def test_integrity_and_foreign_keys_are_clean(self) -> None:
        self.migrate()
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])
        orphans = _sql(
            self.path,
            "SELECT count(*) FROM template_version_bricks AS vb "
            "WHERE NOT EXISTS (SELECT 1 FROM template_versions AS v "
            "WHERE v.id = vb.template_version_id)",
        )
        self.assertEqual(orphans, [(0,)])
        dangling = _sql(
            self.path,
            "SELECT count(*) FROM templates AS t WHERE NOT EXISTS "
            "(SELECT 1 FROM template_versions AS v "
            "WHERE v.template_id = t.id AND v.version = t.revision)",
        )
        self.assertEqual(dangling, [(0,)], "у каждого шаблона есть текущая версия")

    def test_missing_system_brick_blocks_update_but_keeps_db_clean(self) -> None:
        """Неполные привязки недопустимы: копия не обновляется вовсе."""
        _write(
            self.path,
            "UPDATE bricks SET status = 'archived' "
            "WHERE scope = 'system' AND code = 'контакт'",
        )
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(len(self.versions(1)), 1, "нет версии с неполными привязками")
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 1"),
            [(OLD_INITIAL_NAME, 1)],
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])


class MigrationEighteenRollbackTests(_Base):
    """Сбой шага откатывает миграцию целиком: данных не теряем."""

    def test_failed_step_rolls_back_without_partial_changes(self) -> None:
        before_templates = self.templates()
        before_versions = _sql(
            self.path,
            "SELECT id, template_id, version, builtin_version "
            "FROM template_versions ORDER BY id",
        )
        # Искусственный отказ на последнем смысловом шаге миграции.
        _write(
            self.path,
            "CREATE TRIGGER trg_tester_block_m18 BEFORE UPDATE ON templates "
            "BEGIN SELECT RAISE(ABORT, 'tester rollback'); END",
        )
        with self.assertRaises(MigrationError):
            self.migrate()
        self.assertEqual(
            _sql(self.path, "SELECT max(version) FROM schema_migrations"), [(17,)]
        )
        self.assertEqual(self.templates(), before_templates)
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, template_id, version, builtin_version "
                "FROM template_versions ORDER BY id",
            ),
            before_versions,
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name = 'migration_18_targets'",
            ),
            [],
        )
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])

        # После снятия причины миграция применяется как обычно.
        _write(self.path, "DROP TRIGGER trg_tester_block_m18")
        self.assertEqual(self.migrate(), LATEST_VERSION)
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 1"),
            [(NEW_INITIAL_NAME, 3)],
        )


class MigrationEighteenRenderTests(_Base):
    """Обновлённая копия рендерится ровно в новый эталон владельца."""

    def test_updated_copy_renders_owner_reference(self) -> None:
        self.migrate()
        from brick_store import list_system_bricks
        from template_store import render_template_version

        session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(session.close)
        by_code = {item.code: item for item in list_system_bricks(session)}
        values = {
            by_code["название"].id: "Летний концерт",
            by_code["дата"].id: "12.07.2027",
            by_code["время_начала"].id: "19:00",
            by_code["место"].id: "Дом культуры",
            by_code["ссылка_места"].id: "https://example.com/place",
            by_code["адрес"].id: "ул. Ленина, 1",
            by_code["описание"].id: "Большой зал, вход свободный.",
            by_code["контакт"].id: "@contact",
        }
        rendered = render_template_version(
            session,
            actor_telegram_user_id=OWNER_TG,
            template_id=1,
            values_by_id=values,
        )
        self.assertEqual(
            rendered,
            "<b>Летний концерт</b>\n"
            "\n"
            "<b>12.07.2027 // 19:00</b>\n"
            '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
            "ул. Ленина, 1\n"
            "\n"
            "Большой зал, вход свободный.\n"
            "\n"
            "@contact",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
