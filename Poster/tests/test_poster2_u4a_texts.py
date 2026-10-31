# tests/test_poster2_u4a_texts.py
"""
U4a (Poster 2.2, исправление №10): единый редактируемый каталог текстов.

Требование владельца: «передать ОДИН YAML-файл редактору, получить
исправленную версию и обновить весь текст интерфейса без редактирования
Python-кода».

Что закрепляется:

(а) каталог `config/ui_texts.yaml` — полный: каждый вшитый дефолт имеет
    строку в файле с тем же набором переменных `{...}`, лишних ключей нет;
(б) загрузка и валидация: синтаксис YAML, строковые значения, обязательные
    ключи, совпадение переменных; невалидный/отсутствующий файл → работа на
    вшитых дефолтах, отчёт с причиной, бот живой (CRITICAL в лог);
(в) `tr`: подстановка переменных, HTML-экранирование и шаблона, и значений,
    отсутствующая обязательная переменная даёт безопасный текст (дефолт), а
    не падение; неизвестный ключ — пустая строка и запись в лог;
(г) экраны берут текст ИЗ КАТАЛОГА: правка YAML + `reload_texts()` меняет
    главное меню, справку и шаг фотографий без правки Python;
(д) `tr_safe` не экранирует уже проверенный HTML сохранённого значения.

Тесты идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`): реальные Telegram, `.env` и
`post_bot.db` не используются.

Файл чувствителен к правкам U4a (проверено мутацией):

- вернуть в `handlers/main_menu.py` литерал `"Здравствуйте! 👋"` вместо
  `tr("menu.main")` → падает `CatalogScreenTests` («правка YAML не изменила
  экран»);
- убрать экранирование в `utils/ui_texts.tr` → падает
  `TrRenderingTests.test_html_injection_is_escaped`;
- не подставлять дефолт при отсутствующей переменной → падает
  `TrRenderingTests.test_missing_variable_falls_back_to_default`.
"""

from __future__ import annotations

import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


CATALOG_PATH = Path(__file__).resolve().parent.parent / "config" / "ui_texts.yaml"


class CatalogFileTests(unittest.TestCase):
    """Сам файл: полнота, отсутствие лишних ключей, синтаксис."""

    def test_catalog_file_is_valid(self) -> None:
        report = ui_texts.reload_texts(CATALOG_PATH)
        self.assertTrue(report.ok, report.errors)
        self.assertFalse(report.used_defaults)
        self.assertEqual(report.errors, ())

    def test_catalog_covers_every_default_key(self) -> None:
        """Каждый дефолт имеет строку в файле — иначе правка «не доходит»."""
        report = ui_texts.reload_texts(CATALOG_PATH)
        self.assertTrue(report.ok, report.errors)
        # warnings пусты: ни одного дефолта без строки в файле и ни одного
        # лишнего ключа, который код не использует.
        self.assertEqual(report.warnings, ())
        self.assertEqual(report.keys, len(ui_texts.DEFAULTS))

    def test_catalog_values_equal_defaults(self) -> None:
        """U4a переносит тексты литерально: файл совпадает с дефолтами."""
        report = ui_texts.reload_texts(CATALOG_PATH)
        self.assertTrue(report.ok, report.errors)
        catalog = ui_texts.texts()
        for key, value in ui_texts.DEFAULTS.items():
            self.assertEqual(catalog[key], value, key)

    def test_default_values_are_used_by_code_not_duplicated(self) -> None:
        """
        U4a-fix D2/D3: ни один литерал экрана не дублирует строку каталога.

        Если такая строка стоит в Python-коде, правка YAML оставляет на
        экране старый текст — владелец правит файл и не видит эффекта.
        Проверяются экраны, переведённые на каталог: места, конструктор и
        обработчик ошибок.
        """
        import ast

        root = CATALOG_PATH.parent.parent
        sources = [
            root / "handlers" / "places.py",
            root / "handlers" / "announcement_wizard.py",
            root / "errors.py",
        ]
        default_values = {
            value
            for key, value in ui_texts.DEFAULTS.items()
            if len(value) > 6 and "{" not in value
        }
        duplicates: list[str] = []
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in default_values:
                        duplicates.append(
                            f"{path.relative_to(root)}:{node.lineno}: {node.value!r}"
                        )
        self.assertEqual(
            duplicates,
            [],
            "литералы дублируют каталог и не меняются через YAML: "
            + "; ".join(duplicates),
        )

    def test_required_keys_are_marked(self) -> None:
        for key in ("menu.main", "menu.manage", "help.text_html"):
            self.assertIn(key, ui_texts.REQUIRED_KEYS)
            self.assertIn(key, ui_texts.DEFAULTS)


class _TempCatalog(unittest.TestCase):
    """База: временный YAML-файл и гарантированный откат каталога."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="poster_u4a_")
        self.path = Path(self._tmp.name) / "ui_texts.yaml"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        # Глобальный каталог — на боевой файл: иначе соседние тесты увидели бы
        # временные строки.
        ui_texts.reload_texts(CATALOG_PATH)

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def write_catalog(self, drop: tuple[str, ...] = (), /, **overrides: str) -> Path:
        """Полный валидный YAML: дефолты + переопределения (минус `drop`)."""
        values = dict(ui_texts.DEFAULTS)
        for key in drop:
            values.pop(key, None)
        values.update(overrides)
        lines = []
        for key, value in values.items():
            escaped = (
                str(value)
                .replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("\n", "\\n")
            )
            lines.append(f'"{key}": "{escaped}"\n')
        return self.write("".join(lines))


class LoadAndValidateTests(_TempCatalog):
    """Загрузка/валидация и безопасный откат на дефолты."""

    def test_valid_partial_catalog_merges_with_defaults(self) -> None:
        """Необязательный ключ можно убрать: строка берётся из дефолта."""
        self.write_catalog(("menu.outside.text",), **{"menu.main": "Новое меню"})
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(ui_texts.tr("menu.main"), "Новое меню")
        # Отсутствующий необязательный ключ взят из дефолта, а не потерян.
        self.assertEqual(
            ui_texts.tr("menu.outside.text"), ui_texts.DEFAULTS["menu.outside.text"]
        )
        self.assertTrue(any("menu.outside.text" in item for item in report.warnings))

    def test_broken_yaml_falls_back_to_defaults(self) -> None:
        self.write('"menu.main": "незакрытая строка\n  : : :\n')
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL) as logs:
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertTrue(any("синтаксис YAML" in item for item in report.errors))
        self.assertIn("CRITICAL", "".join(logs.output))
        # Каталог целиком дефолтный: ни одной строки из битого файла.
        self.assertEqual(ui_texts.texts(), ui_texts.DEFAULTS)

    def test_non_utf8_file_falls_back_to_defaults(self) -> None:
        """
        U4a-fix D1: файл в cp1251 (типичный «ANSI» редактора).

        Без перехвата `UnicodeDecodeError` импорт `utils.ui_texts` падал бы
        вместе с `errors.py` и всеми handlers — бот не стартовал бы вовсе.
        Ожидание: невалидный каталог, дефолты, CRITICAL и понятная причина.
        """
        self.path.write_bytes(
            CATALOG_PATH.read_text(encoding="utf-8").encode("cp1251", errors="replace")
        )
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL) as logs:
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertTrue(
            any("кодировка" in item and "UTF-8" in item for item in report.errors),
            report.errors,
        )
        self.assertIn("CRITICAL", "".join(logs.output))
        self.assertEqual(ui_texts.texts(), ui_texts.DEFAULTS)

    def test_truncated_utf8_file_falls_back_to_defaults(self) -> None:
        """U4a-fix D1: файл обрезан посередине символа — тот же безопасный откат."""
        raw = CATALOG_PATH.read_bytes()
        self.path.write_bytes(raw[:400] + b"\xd0")
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertTrue(any("UTF-8" in item for item in report.errors), report.errors)
        self.assertEqual(ui_texts.texts(), ui_texts.DEFAULTS)

    def test_cli_reports_non_utf8_as_invalid_without_traceback(self) -> None:
        """CLI обязан вернуть 1 и «invalid», а не падать traceback'ом."""
        import os
        import subprocess
        import sys

        self.path.write_bytes('"menu.main": "Меню"\n'.encode("cp1251") + b"\xff\xfe")
        completed = subprocess.run(
            [sys.executable, "-m", "utils.ui_texts"],
            cwd=str(CATALOG_PATH.parent.parent),
            env={**os.environ, ui_texts.PATH_ENV_VARIABLE: str(self.path)},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 1, completed.stderr[-1000:])
        self.assertIn("ui_texts: invalid", completed.stdout)
        self.assertNotIn("Traceback", completed.stderr)

    def test_non_string_value_is_invalid(self) -> None:
        self.write('"menu.main": 42\n')
        report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("menu.main" in item for item in report.errors))
        self.assertEqual(ui_texts.tr("menu.main"), ui_texts.DEFAULTS["menu.main"])

    def test_root_must_be_mapping(self) -> None:
        self.write("- строка\n- ещё строка\n")
        report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)

    def test_missing_required_key_is_invalid(self) -> None:
        values = {k: v for k, v in ui_texts.DEFAULTS.items() if k != "menu.manage"}
        lines = [
            '"{}": "{}"\n'.format(
                key,
                value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"),
            )
            for key, value in values.items()
        ]
        self.write("".join(lines))
        report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("menu.manage" in item for item in report.errors))

    def test_changed_placeholders_are_invalid(self) -> None:
        self.write('"wizard.question.header": "Вопрос {step} из {total}"\n')
        report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("wizard.question.header" in item for item in report.errors))
        self.assertEqual(
            ui_texts.tr("wizard.question.header", step=1, total=2, title="Дата"),
            "🧱 Вопрос 1 из 2: Дата",
        )

    def test_unknown_key_is_warning_not_error(self) -> None:
        self.write_catalog(**{"menu.такого_ключа_нет": "лишняя строка"})
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)
        self.assertTrue(any("неизвестный ключ" in item for item in report.warnings))

    def test_missing_file_falls_back_to_defaults(self) -> None:
        absent = Path(self._tmp.name) / "no-such-file.yaml"
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
            report = ui_texts.reload_texts(absent)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertEqual(ui_texts.texts(), ui_texts.DEFAULTS)

    def test_environment_path_is_honoured(self) -> None:
        self.write_catalog(**{"menu.main": "Из переменной окружения"})
        with mock.patch.dict(
            "os.environ", {ui_texts.PATH_ENV_VARIABLE: str(self.path)}
        ):
            report = ui_texts.reload_texts()
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(ui_texts.tr("menu.main"), "Из переменной окружения")


class TrRenderingTests(_TempCatalog):
    """`tr`: подстановки, экранирование, безопасные дефолты."""

    def test_placeholders_are_substituted(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        self.assertEqual(
            ui_texts.tr("wizard.question.header", step=2, total=5, title="Дата"),
            "🧱 Вопрос 2 из 5: Дата",
        )
        self.assertEqual(
            ui_texts.tr("wizard.photos.counter", count=3, limit=10),
            "Добавлено фотографий: 3 из 10.",
        )

    def test_html_injection_is_escaped(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        text = ui_texts.tr(
            "wizard.question.header",
            step=1,
            total=1,
            title="<script>alert(1)</script> & ко",
        )
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;", text)

    def test_html_key_keeps_own_markup_but_escapes_values(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        text = ui_texts.tr(
            "wizard.question.description_html", description="<b>не тег</b> & x"
        )
        self.assertTrue(text.startswith("📝 <code>"))
        self.assertIn("&lt;b&gt;не тег&lt;/b&gt; &amp; x", text)
        # Разметка каталога осталась разметкой.
        self.assertIn("<code>", text)

    def test_non_html_key_escapes_its_own_template(self) -> None:
        self.write_catalog(**{"menu.main": "Меню <b>без</b> разметки"})
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(
            ui_texts.tr("menu.main"), "Меню &lt;b&gt;без&lt;/b&gt; разметки"
        )

    def test_missing_variable_falls_back_to_default(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        with self.assertLogs("utils.ui_texts", level=logging.ERROR) as logs:
            text = ui_texts.tr("wizard.question.header", step=1, total=3)
        self.assertEqual(text, "🧱 Вопрос 1 из 3: ")
        self.assertIn("title", "".join(logs.output))

    def test_missing_variable_never_raises(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        for key in (
            "wizard.photos.counter",
            "places.search.empty",
            "buttons.finished.photos",
        ):
            ui_texts.tr(key)  # не должно падать

    def test_unknown_key_returns_empty_string_and_logs(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        with self.assertLogs("utils.ui_texts", level=logging.ERROR):
            self.assertEqual(ui_texts.tr("menu.такого_ключа_нет"), "")

    def test_none_value_is_empty_not_literal_none(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        self.assertEqual(
            ui_texts.tr("wizard.question.header", step=1, total=1, title=None),
            "🧱 Вопрос 1 из 1: ",
        )

    def test_tr_safe_does_not_double_escape(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        self.assertEqual(
            ui_texts.tr_safe(
                "wizard.question.current_value", value="<b>жирный</b> & x"
            ),
            "Текущее значение: <b>жирный</b> & x",
        )

    def test_texts_returns_copy(self) -> None:
        ui_texts.reload_texts(CATALOG_PATH)
        snapshot = ui_texts.texts()
        snapshot["menu.main"] = "подмена"
        self.assertEqual(ui_texts.tr("menu.main"), ui_texts.DEFAULTS["menu.main"])


class CatalogScreenTests(_TempCatalog):
    """Экраны читают текст из каталога: правка YAML меняет интерфейс."""

    @scenario_test
    async def test_main_menu_text_comes_from_catalog(self, h: Harness) -> None:
        self.write_catalog(**{"menu.main": "Новое приветствие\n\nВыберите действие:"})
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)
        await h.text("/start")
        self.assertEqual(h.screen_text(), "Новое приветствие\n\nВыберите действие:")

    @scenario_test
    async def test_main_menu_buttons_come_from_catalog(self, h: Harness) -> None:
        self.write_catalog(**{"buttons.main.manage": "⚙️ Настройки"})
        ui_texts.reload_texts(self.path)
        await h.text("/start")
        self.assertIn(("⚙️ Настройки", "manage"), h.screen_buttons())

    @scenario_test
    async def test_help_text_comes_from_catalog(self, h: Harness) -> None:
        # Переменные шаблона сохраняются: набор {…} менять нельзя.
        self.write_catalog(
            **{
                "help.text_html": (
                    "<b>Справка</b>\n\nНовый текст справки: {max_photos} фото, "
                    "{text_limit} и {caption_limit} символов."
                )
            }
        )
        ui_texts.reload_texts(self.path)
        await h.text("/help")
        self.assertIn("<b>Справка</b>", h.screen_text())
        self.assertIn(
            "Новый текст справки: 10 фото, 4096 и 1024 символов.", h.screen_text()
        )

    @scenario_test
    async def test_invalid_catalog_keeps_menu_alive_with_defaults(
        self, h: Harness
    ) -> None:
        self.write('"menu.main": "битый\n yaml : : ["\n')
        with self.assertLogs("utils.ui_texts", level=logging.CRITICAL):
            ui_texts.reload_texts(self.path)
        await h.text("/start")
        self.assertEqual(h.screen_text(), ui_texts.DEFAULTS["menu.main"])

    @scenario_test
    async def test_photo_step_text_comes_from_catalog(self, h: Harness) -> None:
        from handlers import wizard_photos

        self.write_catalog(
            **{
                "wizard.photos.title": "📷 Снимки",
                "wizard.photos.hint": "Пришлите до {limit} снимков.",
            }
        )
        ui_texts.reload_texts(self.path)
        details = type("Details", (), {"photos": (), "draft_id": 1, "revision": 1})()
        text = wizard_photos.photo_step_text(details)
        self.assertIn("📷 Снимки", text)
        self.assertIn("Пришлите до 10 снимков.", text)

    @scenario_test
    async def test_finished_screen_text_comes_from_catalog(self, h: Harness) -> None:
        from handlers import announcement_wizard

        self.write_catalog(
            **{
                "wizard.finished.text": "✅ Готово.\n\nПерешлите сообщение выше",
                "buttons.finished.edit": "✏️ Правка",
            }
        )
        ui_texts.reload_texts(self.path)
        self.assertEqual(
            ui_texts.tr("wizard.finished.text"),
            "✅ Готово.\n\nПерешлите сообщение выше",
        )
        self.assertEqual(ui_texts.tr("buttons.finished.edit"), "✏️ Правка")
        # Константы модуля — те же строки каталога (дефолты при загрузке).
        self.assertEqual(
            announcement_wizard.DELIVERED_TEXT,
            ui_texts.DEFAULTS["wizard.finished.text"],
        )

    @scenario_test
    async def test_error_notice_text_comes_from_catalog(self, h: Harness) -> None:
        import errors as errors_module

        self.write_catalog(**{"errors.internal": "⚠️ Сбой. Повторите позже."})
        ui_texts.reload_texts(self.path)
        self.assertEqual(errors_module.error_notice_text(), "⚠️ Сбой. Повторите позже.")
        self.assertEqual(
            errors_module.domain_notice_text(),
            ui_texts.DEFAULTS["errors.domain"],
        )


class DeadKeyScreenTests(_TempCatalog):
    """
    U4a-fix D2/D3: ключи каталога — живые, а не декоративные.

    Владелец правит подпись навигации, строку карточки или кнопку
    применения — экран обязан показать новый текст, иначе строка в YAML
    ни на что не влияет.
    """

    def _seed_places(self, h: Harness, count: int) -> None:
        from place_store import create_place
        from tests.test_stage27b_announcement_ui import _user

        with h.session_factory() as session:
            user = _user(session)
            for index in range(count):
                create_place(
                    session,
                    f"Место {index:02d}",
                    city="Город",
                    created_by_user_id=user.id,
                )
            session.commit()

    @scenario_test
    async def test_pagination_and_retry_labels_come_from_catalog(
        self, h: Harness
    ) -> None:
        self.write_catalog(
            **{
                "buttons.nav.prev": "НАЗАД-КАТАЛОГ",
                "buttons.nav.next": "ВПЕРЁД-КАТАЛОГ",
                "buttons.nav.retry_search": "ИСКАТЬ-КАТАЛОГ",
            }
        )
        self.assertTrue(ui_texts.reload_texts(self.path).ok)

        await h.text("/start")
        self._seed_places(h, 12)
        await h.text("/places")
        await h.callback("pl_all")
        self.assertIn("ВПЕРЁД-КАТАЛОГ", [label for label, _ in h.screen_buttons()])

        page_callbacks = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("pl_page:")
        ]
        await h.callback(page_callbacks[-1])
        self.assertIn("НАЗАД-КАТАЛОГ", [label for label, _ in h.screen_buttons()])

        await h.callback("pl_search")
        await h.text("нет-такого-места")
        self.assertIn("ИСКАТЬ-КАТАЛОГ", [label for label, _ in h.screen_buttons()])

    @scenario_test
    async def test_canonical_line_and_menu_button_come_from_catalog(
        self, h: Harness
    ) -> None:
        from tests.test_stage27b_announcement_ui import (
            _callback,
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        # H3: кнопки «Применить изменения» больше нет, поэтому проверяем
        # тот же смысл на живой кнопке экрана правки — «🏠 В главное меню».
        self.write_catalog(
            **{
                "places.card.canonical": "КАНОН: {name}",
                "buttons.finished.main_menu": "МЕНЮ-КАТАЛОГ",
            }
        )
        self.assertTrue(ui_texts.reload_texts(self.path).ok)

        await h.text("/start")
        _place_id, alias_id = _seed_place(h, name="Дом культуры", alias="ДК")
        await h.text("/places")
        await h.callback("pl_all")
        await h.callback(
            [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("pl_open")
            ][0]
        )
        await h.callback("pl_names:0")
        await h.callback(f"pl_name:{alias_id}")
        self.assertIn("КАНОН: Дом культуры", h.screen_text())

        template_id, _rev, _bricks = _seed_personal_template(h)
        await _start_to_template(h, _place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        await h.callback(_callback(h, "aw_photo_none"))
        await h.callback(_callback(h, "aw_edit"))
        self.assertIn("МЕНЮ-КАТАЛОГ", [label for label, _ in h.screen_buttons()])

        # Экран правки фотографий — тот же ключ заголовка, что шаг фото, и
        # та же подпись выхода в меню (одна кнопка — одна подпись).
        photos = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_photos:")
        ]
        await h.callback(photos[0])
        self.assertTrue(
            h.screen_text().startswith(ui_texts.DEFAULTS["wizard.photos.title"])
        )
        self.assertIn("МЕНЮ-КАТАЛОГ", [label for label, _ in h.screen_buttons()])

    @scenario_test
    async def test_photos_edit_screen_title_follows_catalog(self, h: Harness) -> None:
        """U4a-fix D3: заголовок экрана правки фото — из каталога, не литерал."""
        from tests.test_stage27b_announcement_ui import (
            _callback,
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        self.write_catalog(**{"wizard.photos.title": "📷 СНИМКИ-ПРАВКА"})
        self.assertTrue(ui_texts.reload_texts(self.path).ok)

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        await h.callback(_callback(h, "aw_photo_none"))
        await h.callback(_callback(h, "aw_edit"))
        photos = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_photos:")
        ]
        await h.callback(photos[0])
        self.assertTrue(h.screen_text().startswith("📷 СНИМКИ-ПРАВКА"))


if __name__ == "__main__":
    unittest.main()
