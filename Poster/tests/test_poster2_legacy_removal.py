# tests/test_poster2_legacy_removal.py
"""
Poster 2.0, S5: удаление легаси-мастера поста.

Проверяется, что девятишаговый мастер удалён целиком, а не спрятан:

- в живом коде нет его символов и текстов («Новый мастер»,
  «Готово — получить пост», `POST_CREATION`, `POST_STEPS`,
  `build_post_summary`);
- легаси-входа (`create_post`/`restore_wip`) нет в меню и в `bot.py`; в
  `announcement_wizard.py` допустим только обработчик устаревшей кнопки;
- файлов `handlers/post_creation.py`, `handlers/drafts.py`,
  `handlers/callbacks.py` нет;
- устаревший callback `create_post` не запускает мастер и не трогает
  живые сессии;
- миграция 17 архивирует `drafts`, идемпотентна и не задевает живые
  таблицы;
- `import bot` собирается без удалённых модулей.

Все сценарии — FakeBot, `Application.process_update` и временная SQLite.
Настоящий Telegram, `.env` и `Poster/post_bot.db` не используются.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import io
import json
import os
import shutil
import tokenize
import unittest
from pathlib import Path

from base import Base
from migrations import LATEST_VERSION, run_migrations
from place_store import create_place
from tests.integration_harness import HARNESS_IMPORT_ERROR, scenario_test
from tests.stage19a_helpers import (
    APP_DIR,
    connect,
    make_engine,
    table_names,
    temp_db_path,
)
from user_store import get_or_create_user

# Каталоги, которые не являются живым кодом.
SKIP_DIRS = {
    ".venv",
    "__pycache__",
    "logs",
    "backups",
    "tests",
    "docs",
    ".git",
}

LEGACY_FILES = (
    "handlers/post_creation.py",
    "handlers/drafts.py",
    "handlers/callbacks.py",
)

# Символы/тексты удалённого мастера: их не должно быть нигде в живом коде.
FORBIDDEN_EVERYWHERE = (
    "POST_CREATION",
    "POST_STEPS",
    "build_post_summary",
    "Готово — получить пост",
    "Новый мастер",
)

# Легаси-вход: кнопка/команда запуска старого мастера. В
# `announcement_wizard.py` остаётся только обработчик устаревшей кнопки,
# который уводит в меню, — это семантика критерия приёмки.
LEGACY_ENTRY_TOKENS = ("create_post", "restore_wip")
LEGACY_ENTRY_ALLOWED = {"handlers/announcement_wizard.py"}


def live_python_files() -> list[Path]:
    """Все .py живого кода: без тестов, документации и служебных каталогов."""
    result: list[Path] = []
    for root, dirs, files in os.walk(APP_DIR):
        dirs[:] = [
            name for name in dirs if name not in SKIP_DIRS and not name.startswith(".")
        ]
        for name in files:
            if name.endswith(".py"):
                result.append(Path(root) / name)
    return sorted(result)


def rel(path: Path) -> str:
    return path.relative_to(APP_DIR).as_posix()


def code_only(path: Path) -> str:
    """
    Исходник без комментариев и docstring'ов.

    Комментарий «легаси удалён в S5» — не легаси-вход и не текст интерфейса,
    поэтому критерий смотрит на исполняемый код и строковые литералы-значения.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    skip_lines: set[int] = set()
    # Комментарии.
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            skip_lines.add(token.start[0])
    # Docstring'и модуля/классов/функций.
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            end = getattr(first, "end_lineno", first.lineno) or first.lineno
            skip_lines.update(range(first.lineno, end + 1))
    return "\n".join(
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in skip_lines
    )


def answer_text(h) -> str:
    """Текст ответов на callback (alert/toast), как его увидел бы пользователь."""
    parts: list[str] = []
    for call in h.answers():
        args = call.get("args") or ()
        kwargs = call.get("kwargs") or {}
        parts.extend(str(item) for item in args)
        parts.extend(f"{key}={value}" for key, value in kwargs.items())
        if call.get("text"):
            parts.append(str(call["text"]))
    return "\n".join(parts)


def module_exists(module: str) -> bool:
    """True, если модуль существует и импортируется."""
    try:
        spec = importlib.util.find_spec(module)
    except ModuleNotFoundError:
        return False
    if spec is None:
        return False
    importlib.import_module(module)
    return True


class LegacyCodeGoneTests(unittest.TestCase):
    """Живой код не содержит символов и текстов удалённого мастера."""

    def test_forbidden_symbols_and_texts_are_absent(self) -> None:
        offenders: list[str] = []
        for path in live_python_files():
            text = code_only(path)
            for token in FORBIDDEN_EVERYWHERE:
                if token in text:
                    offenders.append(f"{rel(path)}: {token}")
        self.assertEqual(offenders, [], "остатки легаси-мастера в живом коде")

    def test_legacy_entry_absent_outside_stale_callback_handler(self) -> None:
        offenders: list[str] = []
        for path in live_python_files():
            name = rel(path)
            if name in LEGACY_ENTRY_ALLOWED:
                continue
            text = code_only(path)
            for token in LEGACY_ENTRY_TOKENS:
                if token in text:
                    offenders.append(f"{name}: {token}")
        self.assertEqual(
            offenders,
            [],
            "легаси-вход (кнопка/команда) допустим только как обработчик "
            "устаревшей кнопки в announcement_wizard.py",
        )

    def test_stale_entry_in_wizard_is_only_a_navigation_answer(self) -> None:
        """В мастере остались только ветки «кнопка устарела», не запуск."""
        path = APP_DIR / "handlers/announcement_wizard.py"
        source = path.read_text(encoding="utf-8")
        occurrences = [
            (number, line.strip())
            for number, line in enumerate(source.splitlines(), start=1)
            if any(token in line for token in LEGACY_ENTRY_TOKENS)
            and not line.strip().startswith("#")
        ]
        self.assertTrue(occurrences, "обработчик устаревшей кнопки должен остаться")
        for number, line in occurrences:
            with self.subTest(line=number):
                self.assertTrue(
                    line.startswith('if data == "') or line.startswith('r"'),
                    f"легаси-вход в строке {number}: {line}",
                )
        # Ни команд, ни кнопок с легаси-именем.
        self.assertNotIn('CommandHandler("create_post"', source)
        self.assertNotIn('CommandHandler("restore_wip"', source)
        self.assertNotIn("Шаг 1 из 9", source)

    def test_legacy_handler_modules_are_deleted(self) -> None:
        for name in LEGACY_FILES:
            with self.subTest(module=name):
                self.assertFalse(
                    (APP_DIR / name).exists(), f"{name} должен быть удалён в S5"
                )
                module = name[: -len(".py")].replace("/", ".")
                self.assertFalse(
                    module_exists(module), f"модуль {module} всё ещё импортируется"
                )

    def test_bot_imports_without_legacy_modules(self) -> None:
        source = (APP_DIR / "bot.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        for name in LEGACY_FILES:
            module = name[: -len(".py")].replace("/", ".")
            self.assertNotIn(module, imported)
            self.assertNotIn(module, code_only(APP_DIR / "bot.py"))
        # Реальная сборка приложения (без запуска polling).
        import bot as bot_module

        self.assertTrue(callable(bot_module.build_application))


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {HARNESS_IMPORT_ERROR}",
)
class LegacyCallbackTests(unittest.TestCase):
    """Устаревший callback не запускает мастер и не трогает сессии."""

    @staticmethod
    async def _enter_wizard(h) -> None:
        """Входит в конструктор через площадку: только так активен диалог."""
        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id, username="legacy")
            place = create_place(
                session, "Театр S5", city="Тестовый город", created_by_user_id=user.id
            )
            session.commit()
            place_id = place.id
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        assert h.dialog_state() is not None

    @scenario_test
    async def test_create_post_callback_closes_wizard_and_shows_menu(self, h):
        await self._enter_wizard(h)

        await h.callback("create_post")

        self.assertIsNone(h.dialog_state(), "легаси-мастер не запущен")
        self.assertNotIn("Шаг", h.screen_text())
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertIn("устарела", answer_text(h))
        self.assertIn("pick_place", [data for _, data in h.screen_buttons()])

    @scenario_test
    async def test_create_post_does_not_touch_user_state(self, h):
        await self._enter_wizard(h)
        before = [(row.user_id, row.screen_message_id) for row in h.user_states()]
        await h.callback("create_post")
        after = [(row.user_id, row.screen_message_id) for row in h.user_states()]
        self.assertEqual(after, before, "user_states не переписана")

    @scenario_test
    async def test_restore_wip_points_to_my_announcements(self, h):
        await self._enter_wizard(h)

        await h.callback("restore_wip")

        self.assertIsNone(h.dialog_state())
        self.assertIn("Мои анонсы", answer_text(h))


class LegacyArchiveMigrationTests(unittest.TestCase):
    """Миграция 17: архив, идемпотентность и сохранность живых таблиц."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-s5-legacy-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

    def _init_like_production(self) -> None:
        """Ровно `init_db()`: миграции, затем недостающие таблицы моделей."""
        run_migrations(self.engine)
        Base.metadata.create_all(bind=self.engine)

    def _create_legacy_schema(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""
                CREATE TABLE drafts (
                    id INTEGER NOT NULL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    title VARCHAR(255),
                    text TEXT,
                    image VARCHAR(255),
                    photos TEXT,
                    formats TEXT,
                    created_at DATETIME NOT NULL
                )
                """)
            connection.exec_driver_sql(
                "INSERT INTO drafts (id, user_id, title, text, image, photos, "
                "formats, created_at) VALUES "
                "(1, 42, 'Прежний черновик', 'Текст', 'file-1', "
                "'[\"file-1\"]', '{\"title\": \"x\"}', '2026-01-01 10:00:00')"
            )

    def test_migration_seventeen_archives_readable_copy(self) -> None:
        self._create_legacy_schema()

        self._init_like_production()

        tables = table_names(self.path)
        self.assertNotIn("drafts", tables)
        self.assertIn("legacy_drafts_backup", tables)
        connection = connect(self.path)
        try:
            row = connection.execute(
                "SELECT title, text, image, photos, formats, created_at "
                "FROM legacy_drafts_backup WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["title"], "Прежний черновик")
        self.assertEqual(row["text"], "Текст")
        self.assertEqual(row["image"], "file-1")
        self.assertEqual(json.loads(row["photos"]), ["file-1"])
        self.assertEqual(json.loads(row["formats"]), {"title": "x"})
        self.assertEqual(row["created_at"], "2026-01-01 10:00:00")

    def test_migration_seventeen_is_idempotent(self) -> None:
        self._create_legacy_schema()
        self._init_like_production()
        versions = self._journal()
        # Poster 2.1 R2 добавила миграцию 18: архив остаётся на месте,
        # последней в журнале теперь идёт обновление встроенного шаблона.
        self.assertIn((17, "archive_legacy_drafts"), versions)
        self.assertEqual(versions[-1], (LATEST_VERSION, "place_invite_links"))

        # Повторные запуски (рестарты) архив не переименовывают и не теряют.
        for _ in range(2):
            self._init_like_production()
        self.assertEqual(self._journal(), versions)
        self.assertEqual(
            self._scalar("SELECT count(*) FROM legacy_drafts_backup"), (1,)
        )

    def test_live_tables_are_untouched_by_archive(self) -> None:
        self._create_legacy_schema()
        self._init_like_production()

        tables = table_names(self.path)
        for table in (
            "users",
            "places",
            "place_names",
            "place_admins",
            "templates",
            "bricks",
            "announcement_drafts",
            "user_states",
        ):
            with self.subTest(table=table):
                self.assertIn(table, tables)
        # Живые таблицы созданы пустыми: архив их не заполняет.
        self.assertEqual(self._scalar("SELECT count(*) FROM announcement_drafts"), (0,))
        self.assertEqual(self._scalar("SELECT count(*) FROM user_states"), (0,))

    def test_fresh_database_has_no_archive(self) -> None:
        self._init_like_production()
        tables = table_names(self.path)
        self.assertNotIn("drafts", tables)
        self.assertNotIn("legacy_drafts_backup", tables)
        self.assertIn((17, "archive_legacy_drafts"), self._journal())
        self.assertEqual(self._journal()[-1], (LATEST_VERSION, "place_invite_links"))

    def _journal(self) -> list[tuple[int, str]]:
        connection = connect(self.path)
        try:
            return [
                (int(row["version"]), str(row["name"]))
                for row in connection.execute(
                    "SELECT version, name FROM schema_migrations ORDER BY version"
                )
            ]
        finally:
            connection.close()

    def _scalar(self, sql: str):
        connection = connect(self.path)
        try:
            row = connection.execute(sql).fetchone()
            return None if row is None else tuple(row)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
