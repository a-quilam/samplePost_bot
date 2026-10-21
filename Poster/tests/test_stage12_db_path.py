# tests/test_stage12_db_path.py
"""
Stage 12, §2: путь к SQLite не зависит от рабочей директории.

Регрессия «случайная пустая БД в корне репозитория»: раньше
database.py использовал относительный `sqlite:///./post_bot.db`, и
запуск из корня/из IDE/из launchd создавал посторонний пустой файл.
Теперь default путь якорится каталогом модуля Poster/.

Тесты (без сети, без токена, без настоящей пользовательской БД):
- subprocess импортирует database из разных cwd;
- URL одинаков и указывает в Poster/;
- в рабочей директории ничего не создаётся;
- настоящая Poster/post_bot.db не изменяется.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

POSTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(POSTER_DIR)
EXPECTED_URL = "sqlite:///" + os.path.join(POSTER_DIR, "post_bot.db")

PRINT_URL = "import database; print(database.SQLALCHEMY_DATABASE_URL)"


def _run(code: str, cwd: str) -> subprocess.CompletedProcess:
    """python -c из произвольной директории; database импортируется из Poster/."""
    env = os.environ.copy()
    env["DATABASE_URL"] = ""  # пустое значение = путь по умолчанию
    env["PYTHONPATH"] = POSTER_DIR
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class DatabasePathFromAnyCwdTests(unittest.TestCase):
    """Один и тот же default URL из Poster/, корня и произвольной папки."""

    def _assert_default_url(self, cwd: str) -> None:
        result = _run(PRINT_URL, cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            EXPECTED_URL,
            f"cwd={cwd}: URL обязан указывать в каталог Poster/",
        )

    def test_url_from_poster_dir(self):
        self._assert_default_url(POSTER_DIR)

    def test_url_from_repo_root(self):
        self._assert_default_url(REPO_DIR)

    def test_url_from_arbitrary_temp_dir(self):
        with tempfile.TemporaryDirectory(prefix="poster_cwd_") as tmp:
            self._assert_default_url(tmp)

    def test_import_from_repo_root_creates_no_db_file(self):
        # Именно так в прошлый раз появился пустой post_bot.db в корне:
        # импорт database из корня не должен создавать файлов в cwd
        root_copy = Path(REPO_DIR) / "post_bot.db"
        existed_before = root_copy.exists()
        result = _run(PRINT_URL, REPO_DIR)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not existed_before:
            self.assertFalse(
                root_copy.exists(),
                "импорт database из корня создал посторонний post_bot.db",
            )

    def test_import_from_temp_dir_creates_no_db_file(self):
        with tempfile.TemporaryDirectory(prefix="poster_cwd_") as tmp:
            result = _run(PRINT_URL, tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                os.listdir(tmp),
                [],
                "в рабочей директории не должно появиться новых файлов",
            )

    def test_real_working_db_untouched(self):
        # Настоящая пользовательская база: существует и не пересоздаётся
        # импортами database из других директорий. В чистом checkout
        # (CI) рабочей базы нет — проверка применима только к машине
        # разработчика; эквивалент для CI — тесты пути с временной БД.
        real_db = Path(POSTER_DIR) / "post_bot.db"
        if not real_db.exists():
            self.skipTest(
                "рабочая база Poster/post_bot.db отсутствует "
                "(чистое окружение — проверка неприменима)"
            )
        size_before = real_db.stat().st_size
        self.assertGreater(size_before, 0, "рабочая база не пуста")

        for cwd in (REPO_DIR, POSTER_DIR):
            result = _run(PRINT_URL, cwd)
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(real_db.stat().st_size, size_before, "рабочая база изменялась")

    def test_database_url_override_wins_over_default(self):
        # Явно настроенный DATABASE_URL сохраняет приоритет (Docker и тесты)
        env = os.environ.copy()
        env["DATABASE_URL"] = "sqlite:////tmp/stage12_override.db"
        env["PYTHONPATH"] = POSTER_DIR
        result = subprocess.run(
            [sys.executable, "-c", PRINT_URL],
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sqlite:////tmp/stage12_override.db")


if __name__ == "__main__":
    unittest.main()
