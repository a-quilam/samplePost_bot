# tests/test_poster2_u4a_verify_tester.py
"""
U4a, независимая приёмка каталога текстов (poster-tester).

Проверяется критерий владельца (сценарий 10) снаружи, как у владельца:
есть ОДИН файл `config/ui_texts.yaml`; правим в нём строки, применяем
(перезапуск службы / явный `reload_texts`) — соответствующие экраны
показывают новый текст, Python-код не менялся. Ошибка YAML не должна
повреждать систему: бот живёт на вшитых дефолтах, в логе CRITICAL.

Независимость от тестов разработчика: сценарии написаны заново, ключи и
последовательности другие; «перезапуск службы» проверяется отдельно —
новый процесс с `UI_TEXTS_PATH` и настоящим `Application.process_update`.

Тесты с пометкой `reproduces` фиксируют найденные дефекты: они падают на
текущем коде и должны стать зелёными после исправления `poster-developer`.
Ожидаемое поведение описано в docstring каждого такого теста.

Реальные Telegram, `.env` (только чтение существующего), `post_bot.db` и
секреты не затрагиваются: Harness подменяет Bot на FakeBot, а базу — на
временный SQLite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _full_catalog(**overrides: str) -> str:
    """
    Полный валидный файл: все дефолты + правки владельца.

    Так выглядит работа редактора: он получает полный файл и меняет в нём
    только нужные значения.
    """
    values = dict(ui_texts.DEFAULTS)
    values.update(overrides)
    return "".join(f"{_quote(key)}: {_quote(value)}\n" for key, value in values.items())


class _Case(unittest.TestCase):
    """Временный каталог; глобальный каталог всегда возвращается на боевой."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="poster_u4a_verify_")
        self.tmp = Path(self._tmp.name)
        self.path = self.tmp / "ui_texts.yaml"
        ui_texts.reload_texts(CATALOG)

    def tearDown(self) -> None:
        ui_texts.reload_texts(CATALOG)
        self._tmp.cleanup()

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class OwnerScenarioTests(_Case):
    """Сценарий 10 целиком: правка YAML → экраны показывают новый текст."""

    @scenario_test
    async def test_six_screens_follow_one_yaml_edit(self, h: Harness) -> None:
        """
        Один файл, шесть правок, шесть экранов.

        Ни одна строка Python при этом не меняется: правится только файл,
        затем каталог применяется (`reload_texts`, в бою — перезапуск).
        """
        self.write(
            _full_catalog(
                **{
                    "menu.main": "ПРИВЕТ-ПРИЁМКА\n\nВыберите действие:",
                    "help.text_html": (
                        "<b>СПРАВКА-ПРИЁМКА</b>\n\nФото: {max_photos}, "
                        "текст: {text_limit}, подпись: {caption_limit}."
                    ),
                    "wizard.photos.title": "📷 СНИМКИ-ПРИЁМКА",
                    "wizard.photos.hint": "Пришлите до {limit} картинок.",
                    "wizard.finished.text": "✅ ГОТОВО-ПРИЁМКА",
                    "buttons.finished.edit": "✏️ ПРАВКА-ПРИЁМКА",
                    "bricks.hub.title_html": "🧱 <b>КИРПИЧИ-ПРИЁМКА</b>",
                    "places.all.empty": "📚 ПУСТО-ПРИЁМКА",
                    "places.hub.title": "📍 МЕСТА-ПРИЁМКА",
                    "errors.stale.places": "СТАРО-ПРИЁМКА",
                    "buttons.nav.back": "◀️ НАЗАД-ПРИЁМКА",
                }
            )
        )
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)

        # 1. главное меню
        await h.text("/start")
        self.assertEqual(h.screen_text(), "ПРИВЕТ-ПРИЁМКА\n\nВыберите действие:")

        # 2. справка (доверенная разметка + переменная домена)
        await h.text("/help")
        self.assertIn("<b>СПРАВКА-ПРИЁМКА</b>", h.screen_text())
        self.assertIn("Фото: 10, текст: 4096, подпись: 1024.", h.screen_text())

        # 3. хаб кирпичиков
        await h.callback("manage")
        await h.callback("manage_bricks")
        self.assertTrue(h.screen_text().startswith("🧱 <b>КИРПИЧИ-ПРИЁМКА</b>"))

        # 4. пустой список мест + 5. устаревшая кнопка
        await h.text("/places")
        self.assertTrue(h.screen_text().startswith("📍 МЕСТА-ПРИЁМКА"))
        await h.callback("pl_all")
        self.assertEqual(h.screen_text(), "📚 ПУСТО-ПРИЁМКА")
        # состояние раздела сброшено вручную: кнопка экрана устарела
        import place_state

        place_state.clear_place_session(h.user_id)
        await h.callback("pl_all")
        self.assertIn("СТАРО-ПРИЁМКА", h.answer_texts())

        # 6. фото-шаг и экран завершения конструктора
        from tests.test_stage27b_announcement_ui import (
            _callback,
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        self.assertTrue(h.screen_text().startswith("📷 СНИМКИ-ПРИЁМКА"))
        self.assertIn("Пришлите до 10 картинок.", h.screen_text())
        self.assertIn(("◀️ НАЗАД-ПРИЁМКА", "aw_photo_back:1:5"), h.screen_buttons())

        await h.callback(_callback(h, "aw_photo_none"))
        self.assertEqual(h.screen_text(), "✅ ГОТОВО-ПРИЁМКА")
        # Ревизия черновика зависит от числа шагов: сверяем префикс callback.
        edit_callbacks = [
            data
            for label, data in h.screen_buttons()
            if label == "✏️ ПРАВКА-ПРИЁМКА" and data and data.startswith("aw_edit:")
        ]
        self.assertTrue(edit_callbacks, h.screen_buttons())

    def test_fresh_process_reads_catalog_without_python_change(self) -> None:
        """
        «Перезапуск службы»: новый процесс получает правленый файл по
        `UI_TEXTS_PATH` и рисует главное меню из него.
        """
        path = self.write(_full_catalog(**{"menu.main": "ПОСЛЕ-РЕСТАРТА"}))
        script = f"""
import asyncio, json, sys
sys.path.insert(0, {str(ROOT)!r})
from tests.integration_harness import Harness

async def main():
    h = Harness()
    await h.start()
    try:
        await h.text("/start")
        print("RESULT" + json.dumps({{"menu": h.screen_text()}}))
    finally:
        await h.close()

asyncio.run(main())
"""
        env = dict(os.environ)
        env[ui_texts.PATH_ENV_VARIABLE] = str(path)
        completed = subprocess.run(
            [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        line = next(
            (
                item
                for item in completed.stdout.splitlines()
                if item.startswith("RESULT")
            ),
            None,
        )
        self.assertIsNotNone(line, completed.stdout[-2000:])
        assert line is not None
        self.assertEqual(json.loads(line[len("RESULT") :])["menu"], "ПОСЛЕ-РЕСТАРТА")


class BrokenYamlTests(_Case):
    """Битый файл: бот жив, экраны на вшитых дефолтах, в логе CRITICAL."""

    def test_tab_indent_in_the_middle_is_rejected(self) -> None:
        source = CATALOG.read_text(encoding="utf-8").split("\n")
        index = next(
            number
            for number, line in enumerate(source)
            if line.startswith('"wizard.photos.title"')
        )
        source[index] = "\t" + source[index]
        self.write("\n".join(source))
        with self.assertLogs("utils.ui_texts", level="CRITICAL") as logs:
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertIn("синтаксис YAML", " ".join(report.errors))
        self.assertIn("CRITICAL", "".join(logs.output))
        # Ни одной строки из битого файла: каталог целиком дефолтный.
        self.assertEqual(ui_texts.texts(), ui_texts.DEFAULTS)

    def test_unclosed_quote_is_rejected(self) -> None:
        self.write('"menu.main": "Меню без закрывающей кавычки\n')
        with self.assertLogs("utils.ui_texts", level="CRITICAL"):
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("синтаксис YAML" in item for item in report.errors))

    @scenario_test
    async def test_screens_stay_alive_on_broken_yaml(self, h: Harness) -> None:
        self.write('"menu.main": "незакрытая\n\t- : [\n')
        with self.assertLogs("utils.ui_texts", level="CRITICAL"):
            ui_texts.reload_texts(self.path)
        await h.text("/start")
        self.assertEqual(h.screen_text(), ui_texts.DEFAULTS["menu.main"])
        self.assertIn(("📍 Создать для площадки", "pick_place"), h.screen_buttons())
        await h.text("/places")
        self.assertIn("📍 Места", h.screen_text())


class EncodingTests(_Case):
    """Файл, сохранённый не в UTF-8 или обрезанный, тоже не должен ломать бота."""

    def _run_import(self, path: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env[ui_texts.PATH_ENV_VARIABLE] = str(path)
        script = (
            "import utils.ui_texts as u\n"
            "print('MENU:' + u.tr('menu.main')[:12])\n"
            "print('REPORT:' + str(u.last_report().used_defaults))\n"
        )
        return subprocess.run(
            [sys.executable, "-W", "ignore::DeprecationWarning", "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_non_utf8_file_falls_back_to_defaults(self) -> None:
        """
        reproduces D1. Ожидание: файл в cp1251 (типичная ошибка «ANSI» в
        редакторе) — невалидный каталог: процесс стартует, работает на
        дефолтах, в лог CRITICAL. Факт: `Path.read_text(encoding="utf-8")`
        бросает `UnicodeDecodeError`, его никто не ловит → импорт
        `utils.ui_texts` (а с ним `errors`, `handlers.*`, бот) падает с
        traceback, CRITICAL нет, дефолтов нет.
        """
        raw = CATALOG.read_text(encoding="utf-8").encode("cp1251", errors="replace")
        path = self.tmp / "cp1251.yaml"
        path.write_bytes(raw)
        completed = self._run_import(path)
        self.assertNotIn(
            "UnicodeDecodeError", completed.stderr, completed.stderr[-1500:]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])
        self.assertIn("MENU:", completed.stdout)
        self.assertIn("REPORT:True", completed.stdout)

    def test_truncated_file_falls_back_to_defaults(self) -> None:
        """
        reproduces D1 (второе воспроизведение). Ожидание: обрезанный файл
        (сохранение прервалось на середине символа) → дефолты + CRITICAL,
        бот стартует. Факт: тот же `UnicodeDecodeError` на импорте.
        """
        raw = CATALOG.read_bytes()
        path = self.tmp / "truncated.yaml"
        path.write_bytes(raw[:400] + b"\xd0")
        completed = self._run_import(path)
        self.assertNotIn(
            "UnicodeDecodeError", completed.stderr, completed.stderr[-1500:]
        )
        self.assertEqual(completed.returncode, 0, completed.stderr[-1500:])


class PlaceholderTests(_Case):
    """Обязательные переменные: удаление не даёт падения и сырого `{name}`."""

    def test_removed_placeholder_invalidates_whole_file(self) -> None:
        self.write(
            _full_catalog(**{"places.create_check.input": "Вы введите без переменной"})
        )
        with self.assertLogs("utils.ui_texts", level="CRITICAL"):
            report = ui_texts.reload_texts(self.path)
        self.assertFalse(report.ok)
        self.assertTrue(report.used_defaults)
        self.assertTrue(
            any("places.create_check.input" in item for item in report.errors),
            report.errors,
        )

    @scenario_test
    async def test_screen_shows_default_text_after_variable_removal(
        self, h: Harness
    ) -> None:
        self.write(
            _full_catalog(**{"places.all.title": "📚 Все места (итог) — без total"})
        )
        with self.assertLogs("utils.ui_texts", level="CRITICAL"):
            ui_texts.reload_texts(self.path)
        await h.text("/places")
        await h.callback("pl_all")
        text = h.screen_text()
        # Каталог отброшен целиком: экран пустого справочника — вшитый дефолт.
        self.assertEqual(text, ui_texts.DEFAULTS["places.all.empty"])
        self.assertNotIn("без total", text)

    def test_runtime_missing_variable_is_safe(self) -> None:
        with self.assertLogs("utils.ui_texts", level="ERROR"):
            text = ui_texts.tr("wizard.question.header", step=2, total=4)
        self.assertIsInstance(text, str)
        self.assertNotIn("{title}", text)
        self.assertNotIn("KeyError", text)
        self.assertTrue(text.startswith("🧱 Вопрос 2 из 4"))

    def test_none_value_does_not_leak_none(self) -> None:
        self.assertEqual(ui_texts.tr("places.card.city", city=None), "Город: ")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class HtmlInjectionTests(_Case):
    """Пользовательские данные с разметкой остаются текстом на экране."""

    @scenario_test
    async def test_search_query_is_escaped_on_screen(self, h: Harness) -> None:
        await h.text("/places")
        await h.callback("pl_search")
        await h.text("<b>зло</b> & <script>alert(1)</script>")
        text = h.screen_text()
        self.assertNotIn("<b>", text)
        self.assertNotIn("<script>", text)
        self.assertIn("&lt;b&gt;зло&lt;/b&gt;", text)
        self.assertIn("&lt;script&gt;", text)

    @scenario_test
    async def test_place_fields_are_escaped_everywhere(self, h: Harness) -> None:
        from place_store import create_place
        from tests.test_stage27b_announcement_ui import _user

        await h.text("/start")
        with h.session_factory() as session:
            user = _user(session)
            create_place(
                session,
                "<b>Место</b><script>x</script>",
                city="<i>Город</i>",
                created_by_user_id=user.id,
                address="<u>адрес</u>",
                url="javascript:alert(1)",
            )
            session.commit()

        await h.text("/places")
        await h.callback("pl_all")
        listed = h.screen_text()
        self.assertNotIn("<b>Место", listed)
        self.assertIn("&lt;b&gt;Место&lt;/b&gt;", listed)
        card_callbacks = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("pl_open")
        ]
        await h.callback(card_callbacks[0])
        card = h.screen_text()
        self.assertNotIn("<u>адрес", card)
        self.assertNotIn("<i>Город", card)
        # Ссылка с опасной схемой не превращается в <a href>.
        self.assertNotIn("<a href", card)
        self.assertIn("javascript:alert(1)", card)

    @scenario_test
    async def test_answer_value_is_escaped_in_html_key(self, h: Harness) -> None:
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("<b>жирный</b> & <script>x</script>")
        # Экран вопроса: описание кирпичика идёт в <code> и экранируется.
        text = h.screen_text()
        self.assertNotIn("<script>", text)


class CompletenessTests(_Case):
    """Экран обязан брать строку из каталога, а не из литерала в Python."""

    @scenario_test
    async def test_pagination_labels_come_from_catalog(self, h: Harness) -> None:
        """
        reproduces D2. Ожидание: `buttons.nav.prev` / `buttons.nav.next` /
        `buttons.nav.retry_search` — живые ключи каталога: владелец правит
        их в YAML, и кнопки «⬅️ Назад / Вперёд ➡️ / 🔁 Искать снова» меняются.
        Факт: эти ключи нигде не читаются (в `handlers/places.py` стоят
        литералы), поэтому правка файла не меняет ни одну кнопку.
        """
        from place_store import create_place
        from tests.test_stage27b_announcement_ui import _user

        self.write(
            _full_catalog(
                **{
                    "buttons.nav.prev": "НАЗАД-КАТАЛОГ",
                    "buttons.nav.next": "ВПЕРЁД-КАТАЛОГ",
                    "buttons.nav.retry_search": "ИСКАТЬ-КАТАЛОГ",
                }
            )
        )
        report = ui_texts.reload_texts(self.path)
        self.assertTrue(report.ok, report.errors)

        await h.text("/start")
        with h.session_factory() as session:
            user = _user(session)
            for index in range(12):
                create_place(
                    session,
                    f"Место {index:02d}",
                    city="Город",
                    created_by_user_id=user.id,
                )
            session.commit()

        await h.text("/places")
        await h.callback("pl_all")
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("ВПЕРЁД-КАТАЛОГ", labels)

        next_callbacks = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("pl_page:")
        ]
        await h.callback(next_callbacks[-1])
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("НАЗАД-КАТАЛОГ", labels)

        await h.callback("pl_search")
        await h.text("нет-такого-места")
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("ИСКАТЬ-КАТАЛОГ", labels)

    @scenario_test
    async def test_place_card_canonical_label_comes_from_catalog(
        self, h: Harness
    ) -> None:
        """
        reproduces D2. Ожидание: `places.card.canonical` — живой ключ:
        у места с альтернативным названием строка «Каноническое название: …»
        берётся из каталога. Факт: ключ не используется, в
        `handlers/places.py` (строки 264 и 299) стоит литерал.
        """
        from tests.test_stage27b_announcement_ui import _seed_place

        self.write(_full_catalog(**{"places.card.canonical": "КАНОН-КАТАЛОГ: {name}"}))
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
        # Строка «Каноническое название» появляется, когда выбрано
        # альтернативное написание: карточка → названия → «ДК».
        await h.callback("pl_names:0")
        await h.callback(f"pl_name:{alias_id}")
        self.assertIn("КАНОН-КАТАЛОГ: Дом культуры", h.screen_text())

    @scenario_test
    async def test_menu_button_label_is_the_same_on_every_screen(
        self, h: Harness
    ) -> None:
        """
        reproduces D3. Ожидание: у одной и той же кнопки выхода в меню
        (`buttons.finished.main_menu`) подпись на всех экранах берётся из
        каталога. H3: прежняя кнопка «Применить изменения» удалена, поэтому
        проверяется живой ключ с тем же смыслом «одна кнопка — одна подпись».
        """
        from tests.test_stage27b_announcement_ui import (
            _callback,
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        self.write(_full_catalog(**{"buttons.finished.main_menu": "МЕНЮ-КАТАЛОГ"}))
        self.assertTrue(ui_texts.reload_texts(self.path).ok)

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        await h.callback(_callback(h, "aw_photo_none"))
        await h.callback(_callback(h, "aw_edit"))

        edit_labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("МЕНЮ-КАТАЛОГ", edit_labels)

        photos = [
            data
            for _label, data in h.screen_buttons()
            if data and data.startswith("aw_photos:")
        ]
        await h.callback(photos[0])
        collected = [label for label, _data in h.screen_buttons()]
        self.assertIn("МЕНЮ-КАТАЛОГ", collected)


class CatalogContractTests(unittest.TestCase):
    """CLI-проверка и отсутствие «мёртвых» ключей в каталоге."""

    def test_cli_exit_codes(self) -> None:
        env = dict(os.environ)
        env[ui_texts.PATH_ENV_VARIABLE] = str(CATALOG)
        good = subprocess.run(
            [
                sys.executable,
                "-W",
                "ignore::DeprecationWarning",
                "-m",
                "utils.ui_texts",
            ],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(good.returncode, 0, good.stdout + good.stderr)
        self.assertIn("ui_texts: ok", good.stdout)

        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "ui_texts.yaml"
            broken.write_text('"menu.main": "битый\n\t- : [\n', encoding="utf-8")
            env[ui_texts.PATH_ENV_VARIABLE] = str(broken)
            bad = subprocess.run(
                [
                    sys.executable,
                    "-W",
                    "ignore::DeprecationWarning",
                    "-m",
                    "utils.ui_texts",
                ],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("ui_texts: invalid", bad.stdout)
        self.assertIn("ERROR:", bad.stdout)
        self.assertNotIn("Traceback", bad.stderr)

    def test_no_dead_keys(self) -> None:
        """
        reproduces D2 (статическая половина). Ожидание: каждый ключ каталога
        где-то читается (`tr(...)` или переменная-ключ), иначе владелец
        правит строку, которая ни на что не влияет.
        """
        import re

        pattern = re.compile(r"""["']([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)["']""")
        referenced: set[str] = set()
        for path in ROOT.rglob("*.py"):
            if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
                continue
            if path.name == "ui_texts.py":
                continue
            referenced |= set(pattern.findall(path.read_text(encoding="utf-8")))
        dead = sorted(set(ui_texts.DEFAULTS) - referenced)
        self.assertEqual(
            dead, [], "ключи каталога, которые никто не читает: " + ", ".join(dead)
        )

    def test_docs_do_not_promise_more_than_delivered(self) -> None:
        """
        reproduces D3 (документальная половина). Ожидание: README честно
        говорит, какие экраны ещё не переведены на каталог. Факт: заявлено
        «Все пользовательские тексты интерфейса Poster собраны в одном
        файле», а экран фотографий в правке и подсказка кирпичика остались
        литералами.
        """
        docs = (ROOT / "docs" / "ui_texts.md").read_text(encoding="utf-8")
        sources = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "handlers").glob("*.py")
        )
        leftovers = [
            text
            for text in ("📷 Фотографии анонса", "Значение пока не задано.")
            if text in sources
        ]
        if leftovers:
            self.assertIn(
                "не переведены",
                docs,
                "в коде остались экранные литералы ("
                + ", ".join(leftovers)
                + "), а README обещает единый каталог всех текстов",
            )


if __name__ == "__main__":
    unittest.main()
