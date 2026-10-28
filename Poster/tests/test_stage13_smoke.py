# tests/test_stage13_smoke.py
"""
Stage 13, §1 и §4: безопасный smoke-test запуска проекта.

Проверяет штатный запуск БЕЗ живого Telegram, БЕЗ настоящего токена и
БЕЗ пользовательской базы:

- bot.main() в subprocess с фиктивным токеном и временной БД
  (tests/stage13_smoke_runner.py): сборка Application, обработчики,
  миграции, polling и явный allowed_updates — при заблокированной сети;
- запуск из каталога Poster, из корня репозитория и из произвольной
  временной директории: посторонних файлов (случайной базы, logs/)
  в рабочей директории не появляется;
- основной интерфейс через fake-bot (Harness): меню → конструктор анонса
  на временной SQLite.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

POSTER_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = POSTER_DIR.parent
SMOKE_RUNNER = Path(__file__).resolve().parent / "stage13_smoke_runner.py"

# Формат валидного, но фиктивного токена: сеть заблокирована, реального
# токена в тестах не требуется и не используется
FAKE_TOKEN = "123456789:TESTFAKETOKENabcdefghijklmnopqrstu"

REPORT_PREFIX = "SMOKE_REPORT="


def _run_smoke(cwd: str, db_dir: str) -> tuple[subprocess.CompletedProcess, dict]:
    """Запускает smoke-runner из cwd; возвращает результат и JSON-отчёт."""
    env = os.environ.copy()
    env["TELEGRAM_BOT_TOKEN"] = FAKE_TOKEN
    env["DATABASE_URL"] = f"sqlite:///{db_dir}/smoke.db"
    result = subprocess.run(
        [sys.executable, str(SMOKE_RUNNER)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = {}
    for line in result.stdout.splitlines():
        if line.startswith(REPORT_PREFIX):
            report = json.loads(line[len(REPORT_PREFIX) :])
            break
    return result, report


def _listing(directory: str) -> list[str]:
    return sorted(os.listdir(directory))


class SmokeLaunchTests(unittest.TestCase):
    """bot.main() в отдельном процессе: сеть заблокирована, БД временная."""

    def _assert_report(self, report: dict, *, cwd: str) -> None:
        self.assertTrue(
            report.get("polled_without_network"),
            f"polling настроен из {cwd} без сети",
        )
        self.assertTrue(
            report.get("allowed_updates_is_all_types"),
            "allowed_updates должен быть Update.ALL_TYPES (инцидент P0)",
        )
        self.assertGreaterEqual(
            report.get("handler_count", 0), 8, "обработчики зарегистрированы"
        )
        self.assertTrue(report.get("has_conversation"), "диалог регистрации")
        self.assertGreaterEqual(report.get("error_handlers", 0), 1)
        self.assertGreaterEqual(report.get("callback_handlers", 0), 4)
        self.assertTrue(
            report.get("db_has_announcement_drafts"),
            "миграции создали announcement_drafts (конструктор анонсов)",
        )
        self.assertFalse(
            report.get("db_has_legacy_drafts_backup"),
            "на свежей базе архива легаси-черновиков нет",
        )
        self.assertTrue(
            report.get("db_has_user_states"), "миграции создали user_states"
        )
        self.assertTrue(
            str(report.get("database_url", "")).endswith("smoke.db"),
            "подключена временная БД, не пользовательская",
        )

    def test_smoke_from_poster_dir(self):
        with tempfile.TemporaryDirectory(prefix="smoke_db_") as db_dir:
            before = _listing(str(POSTER_DIR))
            result, report = _run_smoke(str(POSTER_DIR), db_dir)
            after = _listing(str(POSTER_DIR))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SMOKE_REPORT=", result.stdout, "отчёт не получен")
        self._assert_report(report, cwd="Poster")
        self.assertEqual(after, before, "запуск из Poster/ не создаёт новых файлов")

    def test_smoke_from_repo_root(self):
        with tempfile.TemporaryDirectory(prefix="smoke_db_") as db_dir:
            before = _listing(str(REPO_DIR))
            result, report = _run_smoke(str(REPO_DIR), db_dir)
            after = _listing(str(REPO_DIR))

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_report(report, cwd="корень репозитория")
        self.assertEqual(
            after,
            before,
            "запуск из корня не создаёт случайную базу и не засоряет каталог",
        )
        self.assertNotIn("post_bot.db", after, "случайная база в корне")

    def test_smoke_from_arbitrary_tmp_dir(self):
        with tempfile.TemporaryDirectory(prefix="smoke_cwd_") as cwd:
            with tempfile.TemporaryDirectory(prefix="smoke_db_") as db_dir:
                result, report = _run_smoke(cwd, db_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self._assert_report(report, cwd="временный каталог")
                self.assertEqual(
                    os.listdir(cwd), [], "в чужом каталоге не появилось файлов"
                )


try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MainInterfaceSmokeTests(unittest.TestCase):
    """Основной интерфейс через fake-bot и временную SQLite (§1)."""

    @scenario_test
    async def test_menu_and_dialog_smoke(self, h):
        # Меню на постоянном экране
        await h.text("/start")
        self.assertIn("Выберите действие", h.screen_text())

        # Переход в конструктор анонса: команда /new_wizard — тот же вход,
        # что и «📍 Создать для площадки», но без площадки
        await h.text("/new_wizard")
        self.assertTrue(
            h.screen_text().strip(),
            f"конструктор должен отрисовать экран, получен: {h.screen_text()[:80]!r}",
        )

        # Состояние и ref экрана закреплены во временной БД
        states = h.user_states()
        self.assertTrue(
            any(s.screen_message_id is not None for s in states),
            "ref экрана сохранён в user_states",
        )
        # Прямая сеть не использовалась: все вызовы — FakeBot;
        # состояние ведёт ЕДИНЫЙ конструктор анонса (легаси-мастера нет)
        state = h.dialog_state(name="announcement_wizard")
        self.assertIsNotNone(state, "диалог конструктора активен")


if __name__ == "__main__":
    unittest.main()
