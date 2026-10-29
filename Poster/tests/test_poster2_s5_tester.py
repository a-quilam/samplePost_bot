# tests/test_poster2_s5_tester.py
"""
Poster 2.0, S5 — независимая проверка (poster-tester).

Сценарии написаны заново, независимо от `test_poster2_legacy_removal.py` и
`test_poster2_humor.py`. Используется настоящее приложение
(`build_application`), FakeBot и временная SQLite; реальная `post_bot.db`,
`.env` и сеть Telegram не затрагиваются.

Покрытие:

- миграция 17 на старой схеме: архив читаем, живые таблицы целы, повтор
  идемпотентен, свежая база архива не содержит;
- описание кирпичика: экранированный показ на экране вопроса, отсутствие
  блока без описания, «⏭ Без описания» → NULL, правка описания;
- юмор: распределение `pick_variant` ≈1% на большой выборке ключей,
  выключенный флаг убирает шутки в сквозном сценарии (включая пустой
  анонс), шутка не попадает в live-блок и `publication_text`.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

from base import Base
from migrations import LATEST_VERSION, run_migrations
from tests.stage19a_helpers import (
    OLD_DRAFT,
    connect,
    create_old_schema,
    make_engine,
    table_names,
    temp_db_path,
)

try:
    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from models import Brick
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


JOKE_PROBE = "ШУТКА-ПРОВЕРКА: сцена выдержит."


# —— помощники сценариев ----------------------------------------------------


def _buttons(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _button(h, prefix: str) -> str:
    found = _buttons(h, prefix)
    if not found:
        raise AssertionError(f"нет кнопки {prefix!r}: {h.screen_buttons()}")
    return found[0]


def _seed_template(h, *, bricks):
    """Личный шаблон из описаний `bricks` = [(code, title, description|None)]."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="s5-tester")
        codes = []
        for code, title, description in bricks:
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=title,
                value_type="short_text",
                rules={},
                description=description,
            )
            codes.append(code)
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="S5",
            header="",
            body="\n".join(f"[{code}]" for code in codes),
            footer="",
        )
        session.commit()
        return template.template_id


def _draft(h):
    with h.session_factory() as session:
        drafts = list_announcement_drafts(
            session, actor_telegram_user_id=DEFAULT_USER_ID
        )
    if not drafts:
        raise AssertionError("черновик не создан")
    return drafts[0]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=DEFAULT_USER_ID, draft_id=draft_id
        )


def _live_texts(h) -> list[str]:
    """HTML-предпросмотр живого блока: без клавиатуры и без превью ссылок."""
    return [
        str(call.get("text") or "")
        for call in h.calls
        if call.get("reply_markup") is None
        and call.get("disable_web_page_preview") is False
        and call.get("text") is not None
        and call["type"] in {"message", "edit_message"}
    ]


# —— миграция 17 ------------------------------------------------------------


class Migration17Tests(unittest.TestCase):
    """Архив легаси-таблицы `drafts` на временных файлах SQLite."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster-s5-tester-")
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)

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

    def test_old_database_archived_and_readable(self) -> None:
        create_old_schema(self.path)
        run_migrations(self.engine)

        tables = table_names(self.path)
        self.assertNotIn("drafts", tables, "легаси-таблица переименована")
        self.assertIn("legacy_drafts_backup", tables)

        connection = connect(self.path)
        try:
            row = connection.execute(
                "SELECT title, text, image, photos, formats, created_at "
                "FROM legacy_drafts_backup WHERE id = ?",
                (OLD_DRAFT["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row, "строка старого черновика не потеряна")
        self.assertEqual(row["title"], OLD_DRAFT["title"])
        self.assertEqual(row["text"], OLD_DRAFT["text"])
        self.assertEqual(row["image"], OLD_DRAFT["image"])

    def test_repeat_is_idempotent(self) -> None:
        create_old_schema(self.path)
        run_migrations(self.engine)
        versions = self._journal()
        self.assertIn((17, "archive_legacy_drafts"), versions)
        self.assertEqual(versions[-1], (LATEST_VERSION, "brick_code_conflicts"))

        for _ in range(2):
            run_migrations(self.engine)
        self.assertEqual(self._journal(), versions, "журнал не растёт повторно")

        connection = connect(self.path)
        try:
            count = connection.execute(
                "SELECT count(*) FROM legacy_drafts_backup"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 1, "повтор не теряет и не дублирует архив")

    def test_live_tables_are_intact(self) -> None:
        create_old_schema(self.path)
        run_migrations(self.engine)
        tables = table_names(self.path)
        for name in (
            "users",
            "places",
            "place_names",
            "place_admins",
            "bricks",
            "templates",
            "announcement_drafts",
            "user_states",
        ):
            with self.subTest(table=name):
                self.assertIn(name, tables)
        # Живой user_states не переименован и не очищен архивом.
        connection = connect(self.path)
        try:
            states = connection.execute("SELECT payload FROM user_states").fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(states, "user_states — живая таблица, не архив")

    def test_fresh_database_has_no_archive(self) -> None:
        run_migrations(self.engine)
        Base.metadata.create_all(bind=self.engine)
        tables = table_names(self.path)
        self.assertNotIn("drafts", tables)
        self.assertNotIn("legacy_drafts_backup", tables)
        self.assertIn((17, "archive_legacy_drafts"), self._journal())
        self.assertEqual(self._journal()[-1], (LATEST_VERSION, "brick_code_conflicts"))


# —— описание кирпичика ------------------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BrickDescriptionTests(unittest.TestCase):
    """Описание — инструкция автору: видно на вопросе, хранится/правится."""

    @scenario_test
    async def test_description_escaped_on_question_and_hidden_when_absent(self, h):
        template_id = _seed_template(
            h,
            bricks=[
                ("t1", "Ведущий", "<b>Имя</b> & должность »не нужна«"),
                ("t2", "Без описания", None),
            ],
        )
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")

        first = h.screen_text()
        self.assertIn("📝", first)
        self.assertIn("&lt;b&gt;Имя&lt;/b&gt; &amp; должность »не нужна«", first)
        self.assertNotIn("<b>Имя</b>", first, "описание экранируется")

        await h.text("Иван")
        second = h.screen_text()
        self.assertIn("Вопрос 2 из 2", second)
        self.assertNotIn("📝", second, "без описания блок не показывается")

    @scenario_test
    async def test_skip_description_stores_null(self, h):
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Кирпичик без описания")
        await h.callback("bk_accept_code")
        self.assertIn("Описание для автора анонса", h.screen_text())
        self.assertIn("⏭ Без описания", [label for label, _ in h.screen_buttons()])
        await h.callback("bk_skip_description")
        await h.callback("bk_type:short_text")
        await h.callback("bk_save")

        with h.session_factory() as session:
            brick = session.scalar(
                select(Brick).where(Brick.title == "Кирпичик без описания")
            )
        self.assertIsNotNone(brick)
        self.assertIsNone(brick.description, "«Без описания» → NULL, не пустая строка")

    @scenario_test
    async def test_description_created_and_edited(self, h):
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Ведущий вечера")
        await h.callback("bk_accept_code")
        await h.text("Имя без должности & <без регалий>")
        await h.callback("bk_type:short_text")
        await h.callback("bk_save")

        with h.session_factory() as session:
            brick = session.scalar(select(Brick).where(Brick.title == "Ведущий вечера"))
            brick_id = brick.id
        self.assertEqual(brick.description, "Имя без должности & <без регалий>")

        await h.callback(f"bk_open:{brick_id}")
        card = h.screen_text()
        self.assertIn("&amp; &lt;без регалий&gt;", card)
        self.assertNotIn("& <без регалий>", card)

        await h.callback("bk_edit")
        await h.callback("bk_edit_description")
        await h.text("Только имя и фамилия")
        await h.callback("bk_preview")
        await h.callback("bk_save")

        with h.session_factory() as session:
            updated = session.get(Brick, brick_id)
        self.assertEqual(updated.description, "Только имя и фамилия")
        self.assertEqual(updated.id, brick_id)


# —— юмор -------------------------------------------------------------------


class HumorRateTests(unittest.TestCase):
    """Распределение детерминированного выбора на большой выборке ключей."""

    def test_rate_is_about_one_percent(self) -> None:
        sample = 20_000
        hits = 0
        for index in range(sample):
            key = f"7:1:2:{index}"
            if ui_humor.pick_variant(key) is not None:
                hits += 1
            self.assertEqual(
                ui_humor.pick_variant(key),
                ui_humor.pick_variant(key),
                "один ключ — один результат",
            )
        ratio = hits / sample
        self.assertGreaterEqual(ratio, 0.006, f"слишком редко: {ratio:.4f}")
        self.assertLessEqual(ratio, 0.014, f"слишком часто: {ratio:.4f}")

    def test_hit_is_from_the_fixed_set(self) -> None:
        for index in range(2_000):
            value = ui_humor.pick_variant(f"user:9:draft:{index}")
            if value is not None:
                self.assertIn(value, ui_humor.HUMOR_JOKES)
                break
        else:  # pragma: no cover — 1% на 2000 ключах практически невозможно
            self.fail("ни одного шуточного варианта на 2000 ключах")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorScenarioTests(unittest.TestCase):
    """Сквозные сценарии: границы шуток и флаг отключения."""

    @scenario_test
    async def test_flag_off_has_no_jokes_anywhere(self, h):
        template_id = _seed_template(h, bricks=[("ttl", "Заголовок", None)])
        with patch("config.UI_HUMOR_ENABLED", False):
            self.assertIsNone(ui_humor.pick_variant("7:1:2:1", forced=True))
            self.assertIsNone(ui_humor.get_humor_line(announcement_is_empty=True))

            await h.text("/start")
            screens = [h.screen_text()]
            await h.callback(f"tp_pick:{template_id}")
            screens.append(h.screen_text())
            # Пустой анонс: пропуск вопроса → шаг фото без текста анонса.
            await h.callback(_button(h, "aw_qskip"))
            screens.append(h.screen_text())
            await h.callback(_button(h, "aw_photo_none"))
            screens.append(h.screen_text())

        self.assertTrue(all(screens))
        for text in screens:
            for joke in ui_humor.HUMOR_JOKES:
                self.assertNotIn(joke, text, "флаг выключает шутки везде")
        for text in h.posts():
            for joke in ui_humor.HUMOR_JOKES:
                self.assertNotIn(joke, text or "")

    @scenario_test
    async def test_empty_announcement_joke_only_in_service_screen(self, h):
        template_id = _seed_template(h, bricks=[("ttl", "Заголовок", None)])
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.callback(_button(h, "aw_qskip"))
        photo_screen = h.screen_text()
        joke = ui_humor.get_humor_line(announcement_is_empty=True)
        self.assertIsNotNone(joke)
        self.assertIn(joke, photo_screen)

        await h.callback(_button(h, "aw_photo_none"))
        for live in _live_texts(h):
            self.assertNotIn(joke, live, "в live-блоке шутки нет")

        draft = _details(h, _draft(h).draft_id)
        self.assertNotIn(joke, draft.publication_text or "")
        for text in h.posts():
            self.assertNotIn(joke, text or "")

    @scenario_test
    async def test_joke_never_reaches_live_or_publication(self, h):
        template_id = _seed_template(h, bricks=[("ttl", "Заголовок", None)])

        def fake_pick(key, **kwargs):
            return JOKE_PROBE

        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            self.assertIn(JOKE_PROBE, h.screen_text())
            await h.text("Настоящий анонс")
            await h.callback(_button(h, "aw_photo_none"))

        self.assertIn("Настоящий анонс", " ".join(_live_texts(h)))
        for live in _live_texts(h):
            self.assertNotIn(JOKE_PROBE, live)
        draft = _details(h, _draft(h).draft_id)
        self.assertNotIn(JOKE_PROBE, draft.publication_text or "")
        for text in h.posts():
            self.assertNotIn(JOKE_PROBE, text or "")


if __name__ == "__main__":
    unittest.main()
