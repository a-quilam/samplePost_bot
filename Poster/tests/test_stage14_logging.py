# tests/test_stage14_logging.py
"""
Stage 14, §3–§4: постоянное расположение логов и ротация.

- путь к logs/ привязан к каталогу модуля, а не к текущей рабочей
  директории: запуск из любого каталога пишет в Poster/logs/,
  случайные logs/ в каталогах запуска не создаются;
- ротация срабатывает при достижении размера, число архивов ограничено
  backupCount, Unicode переживает границу ротации (файлы остаются
  корректным UTF-8 из целых записей).

Повторная настройка без дублей handlers и константы политики (5 МБ × 5)
проверены Stage 4 (tests/test_stage4.py).

Все проверки ротации идут во временных каталогах — действующие журналы
Poster/logs/ в тестах не ротируются и не создаются.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import log_config


class LogPathTests(unittest.TestCase):
    """§3: логи в Poster/logs/ независимо от рабочей директории."""

    def test_log_dir_is_absolute_and_anchored_to_module(self):
        module_dir = os.path.dirname(os.path.abspath(log_config.__file__))
        self.assertEqual(
            log_config.LOG_DIR,
            os.path.join(module_dir, "logs"),
            "путь логов привязан к каталогу модуля (как BASE_DIR в database.py)",
        )
        self.assertTrue(os.path.isabs(log_config.LOG_DIR))

    def test_default_log_dir_independent_of_cwd(self):
        """Запуск из произвольного каталога не создаёт logs/ в cwd."""
        module_dir = Path(log_config.__file__).resolve().parent
        code = (
            "import json, os, log_config;"
            "print(json.dumps({'dir': log_config.LOG_DIR,"
            " 'cwd_files': sorted(os.listdir('.'))}))"
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=tmp,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(module_dir)},
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(
                data["dir"],
                str(module_dir / "logs"),
                "каталог логов не зависит от CWD",
            )
            self.assertEqual(
                data["cwd_files"],
                [],
                "в каталоге запуска не появляются случайные файлы/logs",
            )


class RotationTests(unittest.TestCase):
    """§4: ротация по размеру, лимит архивов, Unicode на границе."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = logging.getLogger()
        self.old_level = self.root.level
        # Гигиена: убираем «чужие» помеченные хендлеры предыдущих тестов
        for h in list(self.root.handlers):
            if getattr(h, log_config._MARKER_ATTR, False):
                self.root.removeHandler(h)
                h.close()
        # Стабильная работа в тестовом процессе: без консольного шума
        self._old_max = log_config.LOG_MAX_BYTES
        self._old_backup = log_config.LOG_BACKUP_COUNT

    def tearDown(self):
        for h in list(self.root.handlers):
            if getattr(h, log_config._MARKER_ATTR, False):
                self.root.removeHandler(h)
                h.close()
        self.root.setLevel(self.old_level)
        log_config.LOG_MAX_BYTES = self._old_max
        log_config.LOG_BACKUP_COUNT = self._old_backup
        self.tmp.cleanup()

    def test_rotation_triggers_and_archives_capped(self):
        # Реальные константы политики (5 МБ × 5) проверены атрибутами
        # хендлера в Stage 4; здесь — сам механизм на малом размере.
        log_config.LOG_MAX_BYTES = 4096
        log_config.LOG_BACKUP_COUNT = 2
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        # Консоль приглушаем: 200 записей не должны засорять вывод тестов
        for h in self.root.handlers:
            if getattr(h, log_config._MARKER_ATTR, False) and not isinstance(
                h, logging.handlers.RotatingFileHandler
            ):
                h.setLevel(logging.CRITICAL + 1)

        logger = logging.getLogger("test.stage14.rotation")
        unit = "Юникод-запись 🎉 " + "х" * 200
        for i in range(200):  # ≈ 200 × 230 байт ≫ 3 × 4 КБ — ротации неизбежны
            logger.info("%03d %s", i, unit)

        names = sorted(os.listdir(self.tmp.name))
        self.assertIn("bot.log", names)
        self.assertIn("bot.log.1", names, "ротация создала первый архив")
        self.assertIn("bot.log.2", names, "ротация создала второй архив")
        self.assertNotIn("bot.log.3", names, "архивы ограничены LOG_BACKUP_COUNT")

        # Каждый файл — корректный UTF-8 из целых записей (Unicode
        # пережил границу ротации) и содержит маркер записи
        for name in names:
            with open(os.path.join(self.tmp.name, name), encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Юникод-запись 🎉", content, f"{name}: маркер записи")

    def test_setup_twice_does_not_duplicate_handlers(self):
        # §4.2: повторная инициализация (рестарт/тест) без дублей
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
        self.assertEqual(len(consoles), 1, "консольный хендлер один")
        self.assertEqual(len(files), 1, "файловый хендлер один")

    def test_console_output_continues_with_file(self):
        # §4.3: логи продолжают идти в консоль при файловом хендлере
        log_config.setup_logging(level=logging.INFO, log_dir=self.tmp.name)
        consoles = [
            h
            for h in self.root.handlers
            if not isinstance(h, logging.handlers.RotatingFileHandler)
            and getattr(h, log_config._MARKER_ATTR, False)
        ]
        self.assertEqual(len(consoles), 1)
        self.assertIsInstance(consoles[0], logging.StreamHandler)


if __name__ == "__main__":
    unittest.main()
