# tests/test_stage4.py
"""
Тесты Stage 4 (качество и продакшен):

- логирование с ротацией (log_config.py);
- согласованность .env.example и чтения переменных (config/database/log_config);
- разбор конфигурации в изолированном процессе (subprocess);
- узкие (narrowing) хелперы типов PTB (utils/tg_context.py);
- клавиатура списка черновиков (build_drafts_message);
- roundtrip полей черновика (_post_fields/_apply_fields);
- статические проверки Docker-артефактов (.env не попадает в образ!).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any, cast

import log_config
from handlers.drafts import build_drafts_message
from handlers.post_creation import _apply_fields, _clear_post_data, _post_fields
from models import photos_to_json
from utils import tg_context as ctx
from utils.tg_context import data as ctx_data

POSTER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(POSTER_DIR)


def _run_python(code: str, env_overrides: dict) -> subprocess.CompletedProcess:
    """Запуск python -c в каталоге Poster с заданными переменными окружения."""
    env = os.environ.copy()
    env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=POSTER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


class LoggingSetupTests(unittest.TestCase):
    """log_config.setup_logging: ротация, идемпотентность, уровень."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = logging.getLogger()
        self.old_level = self.root.level
        self.old_httpx_level = logging.getLogger("httpx").level
        # Забираем «наши» хендлеры до теста, чтобы не задеть чужие
        self._added = [
            h for h in self.root.handlers if getattr(h, log_config._MARKER_ATTR, False)
        ]

    def tearDown(self):
        # Убираем хендлеры, добавленные тестом, и закрываем их
        for h in list(self.root.handlers):
            if getattr(h, log_config._MARKER_ATTR, False):
                self.root.removeHandler(h)
                h.close()
        self.root.setLevel(self.old_level)
        logging.getLogger("httpx").setLevel(self.old_httpx_level)
        self.tmp.cleanup()

    def test_creates_rotating_file_handler(self):
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        file_handlers = [
            h
            for h in self.root.handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, log_config._MARKER_ATTR, False)
        ]
        self.assertEqual(len(file_handlers), 1)
        handler = file_handlers[0]
        self.assertEqual(handler.maxBytes, log_config.LOG_MAX_BYTES)
        self.assertEqual(handler.backupCount, log_config.LOG_BACKUP_COUNT)
        self.assertGreater(log_config.LOG_MAX_BYTES, 0)

    def test_log_file_written_with_structured_format(self):
        log_config.setup_logging(level=logging.DEBUG, log_dir=self.tmp.name)
        logging.getLogger("test.stage4").info("проверка записи")
        log_path = os.path.join(self.tmp.name, log_config.LOG_FILE_NAME)
        self.assertTrue(os.path.exists(log_path))
        with open(log_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("проверка записи", content)
        self.assertIn("test.stage4", content)
        self.assertIn("INFO", content)

    def test_setup_is_idempotent(self):
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        marked = [
            h for h in self.root.handlers if getattr(h, log_config._MARKER_ATTR, False)
        ]
        consoles = [
            h for h in marked if not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        files = [
            h for h in marked if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertEqual(len(consoles), 1, "консольный хендлер должен быть один")
        self.assertEqual(len(files), 1, "файловый хендлер должен быть один")

    def test_format_is_structured(self):
        # допускается ширина поля: %(levelname)-8s
        for part in ("%(asctime)s", "%(levelname)", "%(name)s", "%(message)s"):
            self.assertIn(part, log_config.LOG_FORMAT)

    def test_resolve_level(self):
        self.assertEqual(log_config.resolve_level("DEBUG"), logging.DEBUG)
        self.assertEqual(log_config.resolve_level("warning"), logging.WARNING)
        self.assertEqual(log_config.resolve_level(""), logging.INFO)
        self.assertEqual(log_config.resolve_level(None), logging.INFO)
        self.assertEqual(log_config.resolve_level("нет_такого"), logging.INFO)

    def test_httpx_level_hides_token_urls(self):
        # httpx пишет в URL токен бота — его INFO не должен попадать в лог
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        self.assertEqual(
            logging.getLogger("httpx").level,
            logging.WARNING,
            "INFO-запросы httpx содержат токен и должны быть приглушены",
        )


class EnvExampleConsistencyTests(unittest.TestCase):
    """Каждая читаемая переменная из кода описана в .env.example."""

    @staticmethod
    def _example_keys() -> set:
        keys = set()
        with open(os.path.join(POSTER_DIR, ".env.example"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r"([A-Za-z0-9_]+)=", line.strip())
                if m:
                    keys.add(m.group(1))
        return keys

    @staticmethod
    def _getenv_keys(filename: str) -> set:
        with open(os.path.join(POSTER_DIR, filename), encoding="utf-8") as f:
            return set(re.findall(r'os\.getenv\(\s*"([A-Za-z0-9_]+)"', f.read()))

    def test_all_getenv_keys_documented(self):
        documented = self._example_keys()
        used = set()
        for filename in ("config.py", "database.py", "log_config.py"):
            used |= self._getenv_keys(filename)
        missing = used - documented
        self.assertFalse(
            missing,
            f"Переменные читаются в коде, но не описаны в .env.example: {missing}",
        )

    def test_required_keys_present(self):
        documented = self._example_keys()
        self.assertIn("TELEGRAM_BOT_TOKEN", documented)
        self.assertIn("ADMIN_IDS", documented)

    def test_example_contains_no_secrets(self):
        with open(os.path.join(POSTER_DIR, ".env.example"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(("TELEGRAM_BOT_TOKEN=", "ADMIN_IDS=")):
                    self.assertEqual(
                        line, line.split("=")[0] + "=", f"Найдено значение: {line}"
                    )


class ConfigParseTests(unittest.TestCase):
    """config.py в изолированном процессе (env приоритетнее .env)."""

    def test_token_parsed_from_env(self):
        result = _run_python(
            "import config; print(config.TELEGRAM_BOT_TOKEN)",
            {"TELEGRAM_BOT_TOKEN": "12345:TEST-TOKEN"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("12345:TEST-TOKEN", result.stdout)

    def test_missing_token_raises(self):
        # Пустая строка в окружении блокирует загрузку значения из .env —
        # конфигурация обязана упасть с понятной ошибкой
        result = _run_python(
            "import config",
            {"TELEGRAM_BOT_TOKEN": ""},
        )
        self.assertNotEqual(result.returncode, 0, "config обязан упасть без токена")
        self.assertIn("TELEGRAM_BOT_TOKEN", result.stderr)


class DatabaseUrlTests(unittest.TestCase):
    """database.py: DATABASE_URL переопределяет путь, пустое значение = default."""

    CODE = "import database; print(database.SQLALCHEMY_DATABASE_URL)"

    def test_default_path(self):
        result = _run_python(self.CODE, {"DATABASE_URL": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sqlite:///./post_bot.db", result.stdout)

    def test_override_path(self):
        result = _run_python(
            self.CODE, {"DATABASE_URL": "sqlite:////app/data/post_bot.db"}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sqlite:////app/data/post_bot.db", result.stdout)


class TgContextHelperTests(unittest.TestCase):
    """utils/tg_context: понятная ошибка вместо падения на None."""

    def test_query_without_callback_raises(self):
        from telegram import Update

        update = Update(update_id=1)
        with self.assertRaises(RuntimeError):
            ctx.query(update)

    def test_message_without_message_raises(self):
        from telegram import Update

        update = Update(update_id=1)
        with self.assertRaises(RuntimeError):
            ctx.message(update)

    def test_user_without_user_raises(self):
        from telegram import Update

        update = Update(update_id=1)
        with self.assertRaises(RuntimeError):
            ctx.user(update)

    def test_chat_without_chat_raises(self):
        from telegram import Update

        update = Update(update_id=1)
        with self.assertRaises(RuntimeError):
            ctx.chat(update)

    def test_data_returns_existing_dict(self):
        class StubContext:
            user_data = {"current_step": 2}

        stub = cast(Any, StubContext())
        self.assertEqual(ctx_data(stub)["current_step"], 2)

    def test_data_missing_raises(self):
        class StubContext:
            user_data = None

        with self.assertRaises(RuntimeError):
            ctx_data(cast(Any, StubContext()))


class DraftsKeyboardTests(unittest.TestCase):
    """build_drafts_message: кнопки редактирования/удаления и возврата в меню."""

    @staticmethod
    def _draft(draft_id: int) -> Any:
        return SimpleNamespace(
            id=draft_id,
            title=f"Заголовок {draft_id}",
            date="01.01.2026",
            time_start="10:00",
            time_end="12:00",
            place_name="Зал",
        )

    def test_empty_list(self):
        text, markup = build_drafts_message([], total=0)
        self.assertIn("нет черновиков", text)
        self.assertIsNone(markup)

    def test_buttons_for_each_draft(self):
        text, markup = build_drafts_message([self._draft(1), self._draft(2)], total=2)
        datas = [
            button.callback_data for row in markup.inline_keyboard for button in row
        ]
        for draft_id in (1, 2):
            self.assertIn(f"editdraft_{draft_id}", datas)
            self.assertIn(f"delete_{draft_id}", datas)
        self.assertIn("main_menu", datas)
        # callback_data внутри одного сообщения не дублируются
        self.assertEqual(len(datas), len(set(datas)))
        self.assertIn("Заголовок 1", text)


class DraftFieldRoundtripTests(unittest.TestCase):
    """Редактирование черновика: применение и чтение полей без потерь."""

    def test_apply_and_read_fields(self):
        from models import Draft

        draft = Draft(user_id=1)
        user_data = {
            "title": "Новый заголовок",
            "date": "15.09.2026",
            "time_start": "18:00",
            "time_end": "20:00",
            "place_name": "Актовый зал",
            "place_url": "https://example.com",
            "text": "Описание",
            "contact": "@contact",
            "image": "file-id-1",
        }
        _apply_fields(draft, user_data)
        fields = _post_fields(user_data)
        # ни одно поле из user_data не потерялось при сборке колонок
        for key, value in user_data.items():
            self.assertEqual(fields[key], value, f"поле {key} потерялось")
        # и всё доехало до черновика (включая двойную запись image/photos)
        for key, value in fields.items():
            self.assertEqual(getattr(draft, key), value, f"черновик: поле {key}")
        self.assertEqual(draft.image, "file-id-1")
        self.assertEqual(draft.photos, photos_to_json(["file-id-1"]))

    def test_clear_post_data_removes_dialog_state(self):
        user_data = {
            "title": "T",
            "editing_draft_id": 5,
            "edit_field": "title",
            "photos": ["f1"],
            "pending_media": {"x": 1},
        }
        _clear_post_data(user_data)
        self.assertEqual(user_data, {})

    def test_photos_json_helper_still_consistent(self):
        self.assertEqual(photos_to_json(["a", "b"]), '["a", "b"]')
        self.assertIsNone(photos_to_json([]))
        self.assertIsNone(photos_to_json(None))


class DockerArtifactsTests(unittest.TestCase):
    """Статические проверки Docker-конфигурации (без запуска Docker)."""

    def _read(self, filename: str) -> str:
        with open(os.path.join(POSTER_DIR, filename), encoding="utf-8") as f:
            return f.read()

    def test_dockerfile_installs_requirements_and_runs_bot(self):
        content = self._read("Dockerfile")
        self.assertIn("requirements.txt", content)
        self.assertIn("CMD", content)
        self.assertIn("bot.py", content)
        self.assertIn("USER", content)  # непривилегированный пользователь

    def test_compose_persists_data_and_reads_env_file(self):
        content = self._read("docker-compose.yml")
        self.assertIn("env_file", content)
        self.assertIn(".env", content)
        self.assertIn("sqlite:////app/data/post_bot.db", content)
        self.assertIn("bot_data", content)
        self.assertIn("restart", content)

    def test_secrets_not_baked_into_image(self):
        dockerignore = self._read(".dockerignore")
        # .env и базы обязаны исключаться из контекста сборки
        self.assertRegex(dockerignore, r"(?m)^\.env$")
        self.assertRegex(dockerignore, r"(?m)^\*\.db$")
        # В самом Dockerfile нет хардкода токена
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", self._read("Dockerfile"))


if __name__ == "__main__":
    unittest.main()
