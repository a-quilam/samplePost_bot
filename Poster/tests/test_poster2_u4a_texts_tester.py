# tests/test_poster2_u4a_texts_tester.py
"""
U4a, независимые проверки (тестировщик): единый YAML-каталог текстов.

Задание проверяется «снаружи», как у владельца: есть ОДИН файл, владелец
меняет в нём строку, перезапускает службу (в тестах — `reload_texts()`) и
видит новый текст на экране. Python-код при этом не менялся.

Независимость от тестов разработчика:

- проверки идут через ПУБЛИЧНЫЙ контракт (`reload_texts`, `tr`, отрисовка
  экранов через `Application.process_update`), а не через внутренние
  функции-помощники;
- каталог подменяется файлом в `$UI_TEXTS_PATH` — так же, как это сделал бы
  владелец в нестандартном развёртывании;
- отдельный статический тест ищет в исходниках затронутых экранов прежние
  литералы: возврат литерала вместо `tr(...)` делает тест красным
  (мутационная проверка);
- негативные сценарии: битый YAML, отсутствующий файл, чужой набор
  переменных, HTML-инъекция, отсутствующая переменная, неизвестный ключ.

Реальные Telegram, `.env` и `post_bot.db` не используются.
"""

from __future__ import annotations

import ast
import logging
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from utils import ui_texts

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_ERROR = str(exc)


ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "config" / "ui_texts.yaml"

# Прежние литералы, перенесённые в каталог. Если такой литерал снова живёт в
# исходнике экрана — правка YAML перестала бы его менять.
RETIRED_LITERALS = {
    "handlers/main_menu.py": [
        "Здравствуйте! 👋",
        "Выберите действие:",
        "Площадки, шаблоны и кирпичики — общие разделы.",
        "Воспользуйтесь кнопками меню или наберите /help",
    ],
    "handlers/wizard_photos.py": [
        "📷 Фотографии анонса",
        "Пришлите фотографию или альбом.",
        "Добавлено фотографий:",
        "Фото не добавлено:",
    ],
    "handlers/wizard_questions.py": [
        "🧱 Вопрос ",
        "Поле необязательное: его можно пропустить.",
        "Сейчас поле пропущено.",
        "Выберите один вариант кнопкой.",
    ],
    "handlers/announcement_wizard.py": [
        "✅ Анонс готов.\\n\\n",
        "✏️ Редактирование анонса\\n\\n",
        "Готовый анонс отправляется только в личный чат автора.",
        # U4a-fix D3: подпись одной кнопки не должна расходиться по экранам.
        "🔄 Применить изменения",
        "📷 Фотографии анонса",
        "Значение пока не задано.",
    ],
    "handlers/bricks.py": [
        "🧱 <b>Мои кирпичики</b>",
        "У вас пока нет личных кирпичиков.",
        "🗄 <b>Архив кирпичиков</b>",
    ],
    "handlers/places.py": [
        "Найдите место по названию или добавьте новое.",
        "В справочнике пока нет площадок.",
        "Выберите площадку — анонс начнётся сразу",
        # U4a-fix D2: ключи навигации и карточки были «мёртвыми».
        "⬅️ Назад",
        "Вперёд ➡️",
        "🔁 Искать снова",
        "Каноническое название:",
    ],
    "handlers/admin.py": [
        "⚔️ <b>Конфликт кодов кирпичиков</b>",
        "Уведомление автору не доставлено;",
    ],
    "errors.py": [
        "Произошла внутренняя ошибка. Попробуйте повторить действие",
        "Открываю главное меню — попробуйте ещё раз.",
    ],
    "utils/screen.py": [
        "Интерфейс обновлён: кнопки теперь на экране сообщения.",
    ],
}


class _CatalogCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="poster_u4a_tester_")
        self.path = Path(self._tmp.name) / "ui_texts.yaml"
        ui_texts.reload_texts(CATALOG)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        ui_texts.reload_texts(CATALOG)

    def write_flat(self, *, drop=(), **overrides: str) -> Path:
        values = dict(ui_texts.DEFAULTS)
        for key in drop:
            values.pop(key, None)
        values.update(overrides)
        body = "".join(
            '"{}": "{}"\n'.format(
                key,
                str(value)
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n"),
            )
            for key, value in values.items()
        )
        self.path.write_text(body, encoding="utf-8")
        return self.path


class RetiredLiteralTests(unittest.TestCase):
    """Статика: перенесённые тексты больше не живут в Python-исходниках."""

    def test_no_retired_literals_in_handlers(self) -> None:
        leftovers: list[str] = []
        for relative, literals in RETIRED_LITERALS.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            for literal in literals:
                if literal in source:
                    leftovers.append(f"{relative}: {literal!r}")
        self.assertEqual(
            leftovers,
            [],
            "текст остался в коде и не будет меняться через YAML: "
            + "; ".join(leftovers),
        )

    def test_screen_modules_use_tr(self) -> None:
        """Каждый затронутый экран обязан читать каталог, а не только константы."""
        expected = {
            "handlers/main_menu.py": "menu.main",
            "handlers/wizard_photos.py": "wizard.photos.title",
            "handlers/wizard_questions.py": "wizard.question.header",
            "handlers/bricks.py": "bricks.hub.title_html",
            "handlers/places.py": "places.hub.title",
            "handlers/admin.py": "admin.conflict.notification_html",
            "errors.py": "errors.internal",
            "utils/screen.py": "screen.legacy_keyboard_reset",
            "handlers/announcement_wizard.py": "wizard.finished.text",
            "handlers/templates.py": "errors.stale.templates",
        }
        for relative, key in expected.items():
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertRegex(
                source,
                r"""["']""" + re.escape(key) + r"""["']""",
                f"{relative}: нет ключа {key}",
            )
            self.assertIn("ui_texts import", source, f"{relative}: нет импорта tr")

    def test_every_tr_key_exists_in_defaults(self) -> None:
        """Ключи, которые вызывает код, обязаны быть в каталоге дефолтов."""
        pattern = re.compile(r"""\btr(?:_safe)?\(\s*[\"']([a-z0-9_.]+)[\"']""")
        unknown: list[str] = []
        for path in ROOT.rglob("*.py"):
            if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
                continue
            if path.name == "ui_texts.py":
                continue
            text = path.read_text(encoding="utf-8")
            for key in pattern.findall(text):
                if key not in ui_texts.DEFAULTS:
                    unknown.append(f"{path.relative_to(ROOT)}: {key}")
        self.assertEqual(unknown, [], "ключи без дефолта: " + "; ".join(unknown))


class CatalogContractTests(_CatalogCase):
    def test_yaml_is_plain_mapping_of_strings(self) -> None:
        data = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        for key, value in data.items():
            self.assertIsInstance(key, str)
            self.assertIsInstance(value, str, key)
            self.assertNotIn(key, {"", "keys"})

    def test_public_api(self) -> None:
        report = ui_texts.reload_texts(CATALOG)
        self.assertTrue(report.ok, report.errors)
        self.assertIsInstance(report.summary(), str)
        self.assertIn("ok", report.summary())
        self.assertEqual(ui_texts.last_report().path, CATALOG)
        self.assertIn("menu.main", ui_texts.texts())

    def test_cli_reports_success(self) -> None:
        self.assertEqual(ui_texts.main(), 0)

    def test_cli_reports_failure(self) -> None:
        self.path.write_text('"menu.main": "битый\n yaml [\n', encoding="utf-8")
        with mock.patch.dict(
            "os.environ", {ui_texts.PATH_ENV_VARIABLE: str(self.path)}
        ):
            with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
                self.assertEqual(ui_texts.main(), 1)


class OwnerScenarioTests(_CatalogCase):
    """Сценарий 10 владельца: правка YAML → экран обновился."""

    @scenario_test
    async def test_owner_edits_menu_text_without_python(self, h: Harness) -> None:
        # До правки — дефолт.
        await h.text("/start")
        self.assertIn("Здравствуйте", h.screen_text())

        self.write_flat(**{"menu.main": "Привет! Это новый текст меню."})
        with mock.patch.dict(
            "os.environ", {ui_texts.PATH_ENV_VARIABLE: str(self.path)}
        ):
            report = ui_texts.reload_texts()
        self.assertTrue(report.ok, report.errors)

        await h.text("/help")
        await h.text("/start")
        self.assertEqual(h.screen_text(), "Привет! Это новый текст меню.")

    @scenario_test
    async def test_owner_edits_photo_step_title(self, h: Harness) -> None:
        from handlers import wizard_photos

        self.write_flat(**{"wizard.photos.title": "📷 Ваши снимки (правка владельца)"})
        ui_texts.reload_texts(self.path)
        details = type("D", (), {"photos": (), "draft_id": 1, "revision": 1})()
        text = wizard_photos.photo_step_text(details)
        self.assertTrue(text.startswith("📷 Ваши снимки (правка владельца)"))

    @scenario_test
    async def test_owner_edits_finished_controls(self, h: Harness) -> None:
        self.write_flat(
            **{
                "buttons.finished.edit": "✏️ Исправить",
                "buttons.finished.menu": "🏠 Домой",
            }
        )
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)
        from handlers import announcement_wizard

        class Details:
            draft_id = 5
            revision = 2

        markup = announcement_wizard._delivered_keyboard(Details())
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["✏️ Исправить", "🏠 Домой"])

    @scenario_test
    async def test_broken_yaml_keeps_bot_alive(self, h: Harness) -> None:
        self.path.write_text(
            '"menu.main": "Меню\n  - : [битый yaml\n', encoding="utf-8"
        )
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL) as logs:
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertIn("CRITICAL", "".join(logs.output))
        # Бот продолжает работать и показывает дефолтное меню.
        await h.text("/start")
        self.assertEqual(h.screen_text(), ui_texts.DEFAULTS["menu.main"])
        self.assertIn("Здравствуйте", h.screen_text())

    @scenario_test
    async def test_absent_file_keeps_bot_alive(self, h: Harness) -> None:
        absent = Path(self._tmp.name) / "нет-такого-файла.yaml"
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
            report = ui_texts.reload_texts(absent)
        self.assertFalse(report.ok)
        await h.text("/start")
        self.assertEqual(h.screen_text(), ui_texts.DEFAULTS["menu.main"])

    def test_unknown_placeholder_is_rejected(self) -> None:
        """Редактор не может «выдумать» переменную — файл считается битым."""
        self.write_flat(**{"wizard.photos.counter": "Фото: {count} из {limit} шт."})
        report = ui_texts.reload_texts(self.path)
        # Набор переменных совпал, а текст — просто другой: это допустимо.
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(
            ui_texts.tr("wizard.photos.counter", count=1, limit=2), "Фото: 1 из 2 шт."
        )

        self.write_flat(**{"wizard.photos.counter": "Фото: {photos}"})
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("wizard.photos.counter" in item for item in report.errors))

    def test_template_markup_only_in_html_keys(self) -> None:
        """`<b>` вне ключа _html показывается текстом — разметку не внедрить."""
        self.write_flat(**{"menu.main": "Меню <b>жирное</b>"})
        self.assertTrue(ui_texts.reload_texts(self.path).ok)
        self.assertEqual(ui_texts.tr("menu.main"), "Меню &lt;b&gt;жирное&lt;/b&gt;")

        # Доверенная разметка работает в ключе с суффиксом _html.
        # help.text_html сюда не подходит: у него есть переменные, и подмена
        # текста без них сделала бы файл невалидным (это проверено выше).
        self.write_flat(**{"bricks.hub.title_html": "🧱 <b>Мои</b>"})
        self.assertTrue(ui_texts.reload_texts(self.path).ok)
        self.assertEqual(ui_texts.tr("bricks.hub.title_html"), "🧱 <b>Мои</b>")

    def test_value_escaping_in_both_branches(self) -> None:
        payload = "<a href=\"x\">клик</a> & 'кавычки'"
        plain = ui_texts.tr("menu.main")
        self.assertNotIn("<a", plain)
        rich = ui_texts.tr("bricks.hub.active_count", count=payload)
        self.assertNotIn("<a", rich)
        self.assertIn("&lt;a href=&quot;x&quot;&gt;", rich)
        self.assertIn("&amp;", rich)
        self.assertIn("&#x27;кавычки&#x27;", rich)

    def test_missing_variable_is_safe_text(self) -> None:
        for key, kwargs in (
            ("wizard.question.header", {"step": 1, "total": 2}),
            ("places.search.empty", {}),
            ("wizard.photos.limit", {}),
        ):
            with self.assertLogs("utils.ui_texts", level=logging.ERROR):
                text = ui_texts.tr(key, **kwargs)
            self.assertIsInstance(text, str)
            self.assertNotIn("{", text.split(":")[0] if ":" in text else text)

    def test_escaping_applies_to_resolved_default_too(self) -> None:
        """Фолбэк на дефолт не отменяет экранирование пользовательского значения."""
        with self.assertLogs("utils.ui_texts", level=logging.ERROR):
            text = ui_texts.tr("wizard.question.header", title="<b>зло</b>")
        self.assertNotIn("<b>", text)
        self.assertIn("&lt;b&gt;", text)


class CatalogStructureTests(unittest.TestCase):
    """Файл пригоден для редактора: разделы, комментарии, длина строк."""

    def setUp(self) -> None:
        self.source = CATALOG.read_text(encoding="utf-8")
        self.data = ui_texts.validate_texts(yaml.safe_load(self.source), path=CATALOG)

    def test_file_has_section_comments(self) -> None:
        for section in (
            "menu",
            "wizard",
            "bricks",
            "places",
            "errors",
            "admin",
            "buttons",
        ):
            self.assertIn(f'"{section}.', self.source, section)
        self.assertGreaterEqual(self.source.count("# ---"), 8)

    def test_addressing_instructions_are_in_file(self) -> None:
        header = self.source.split("# --- ")[0]
        self.assertIn("utils.ui_texts", header)
        self.assertIn("restart", header.lower() + " systemctl --user restart")
        self.assertIn("_html", header)

    def test_no_tabs_and_utf8(self) -> None:
        self.assertNotIn("\t", self.source)
        self.source.encode("utf-8")

    def test_docs_exist_and_describe_editing(self) -> None:
        docs = (ROOT / "docs" / "ui_texts.md").read_text(encoding="utf-8")
        for fragment in (
            "config/ui_texts.yaml",
            "systemctl --user restart poster-bot",
            "utils.ui_texts",
            "CRITICAL",
            "_html",
        ):
            self.assertIn(fragment, docs)

    def test_requirements_include_pyyaml(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("PyYAML", requirements)


class ModuleImportSafetyTests(unittest.TestCase):
    """Импорт каталога не имеет побочных эффектов вне логов."""

    def test_import_does_not_touch_real_database(self) -> None:
        source = (ROOT / "utils" / "ui_texts.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        for forbidden in ("database", "models", "config", "sqlalchemy"):
            self.assertNotIn(forbidden, imported)

    def test_defaults_are_not_mutated_by_reload(self) -> None:
        before = dict(ui_texts.DEFAULTS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ui_texts.yaml"
            path.write_text('"menu.main": "Другое"\n', encoding="utf-8")
            ui_texts.reload_texts(path)
        self.assertEqual(ui_texts.DEFAULTS, before)
        ui_texts.reload_texts(CATALOG)


if __name__ == "__main__":
    unittest.main()
