# tests/test_poster2_humor.py
"""
Poster 2.0 S5 п.7 (§5.7): редкие юмористические вариации интерфейса.

Две группы проверок, как требует план S5:

* **Обычные варианты** — юмор выключен, не выпал по частоте или экран не
  входит в белый список: интерфейс ведёт себя как без юмора.
* **Шуточные варианты** — вариация выпала: меняется только текст, а
  `callback_data`, порядок кнопок и состояние черновика совпадают.

Модуль `utils/ui_humor` чистый: без Telegram-типов, без сети, без `random()`.
Интеграционная часть идёт через настоящее приложение, FakeBot и временную
SQLite (не реальная `post_bot.db`, не сеть Telegram).
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

from models import Brick

try:
    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_store import create_personal_brick
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_live_record,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


JOKE_PROBE = "ШУТКА-ПРОБА-7f3a: сегодня без пафоса."


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data for _label, data in h.screen_buttons() if data and data.startswith(prefix)
    ]


def _without_notice(text: str) -> str:
    """Убирает одноразовую строку «сохранено» — она не часть экрана."""
    blocks = text.split("\n\n")
    return "\n\n".join(block for block in blocks if not block.startswith("Значение «"))


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет callback {prefix!r}: {h.screen_buttons()}")
    return found[0]


def _seed_template(h, *, bricks: int = 2) -> int:
    """Личный шаблон из `bricks` кирпичиков с описаниями для автора."""
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="humor")
        codes = ["ttl", "dsc", "ext"][:bricks]
        idiom = {
            "ttl": (
                "Заголовок",
                "short_text",
                "Имя и фамилия ведущего, без должности.",
            ),
            "dsc": ("Описание", "long_text", "Что будет & чего не будет."),
            "ext": ("Дополнительно", "short_text", "Любая короткая ремарка."),
        }
        for code in codes:
            title, value_type, description = idiom[code]
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code=code,
                title=title,
                value_type=value_type,
                rules={},
                description=description,
            )
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name="Юмор",
            content="\n".join(
                p for p in ("", "\n".join(f"[{code}]" for code in codes), "") if p
            ),
        )
        session.commit()
        return template.template_id


def _latest_draft(h):
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


def _live_records(h) -> list[dict]:
    """Сообщения/правки живого блока: HTML-предпросмотр без клавиатуры (U1b)."""
    return [
        call
        for call in h.calls
        if call.get("reply_markup") is None and is_live_record(call)
    ]


def _live_text(h) -> str:
    texts = [
        str(call.get("text") or "")
        for call in _live_records(h)
        if call.get("text") is not None
    ]
    if not texts:
        raise AssertionError("живой блок ещё не отправлен")
    return texts[-1]


class HumorCoreTests(unittest.TestCase):
    """Обычные варианты: детерминизм, частота, белый список, флаг."""

    def test_jokes_are_a_fixed_constant_without_llm(self) -> None:
        self.assertGreaterEqual(len(ui_humor.HUMOR_JOKES), 3)
        self.assertTrue(
            all(isinstance(item, str) and item for item in ui_humor.HUMOR_JOKES)
        )
        # Шутка пустого анонса берётся из того же набора (S5 п.7).
        self.assertEqual(
            set(ui_humor.EMPTY_ANNOUNCEMENT_JOKES), set(ui_humor.HUMOR_JOKES)
        )

    def test_pick_variant_is_deterministic_for_one_key(self) -> None:
        keys = ["7:1:1:5", "7:1:1:6", "7:2:3:11", "8:1:1:1"]
        first = {key: ui_humor.pick_variant(key) for key in keys}
        for _ in range(50):
            self.assertEqual({key: ui_humor.pick_variant(key) for key in keys}, first)
        # Результат всегда либо None, либо фраза из набора.
        for value in first.values():
            self.assertTrue(value is None or value in ui_humor.HUMOR_JOKES)

    def test_pick_variant_uses_hash_not_process_hash(self) -> None:
        """Ключ не зависит от PYTHONHASHSEED: sha256 стабилен между процессами."""
        key = "7:1:1:5"
        expected = ui_humor.pick_variant(key)
        with patch.dict("os.environ", {"PYTHONHASHSEED": "12345"}):
            self.assertEqual(ui_humor.pick_variant(key), expected)

    def test_variant_rate_is_about_one_percent(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(20_000)]
        hits = [key for key in keys if ui_humor.pick_variant(key) is not None]
        share = len(hits) / len(keys)
        self.assertGreaterEqual(share, 0.004, f"юмор выпадает слишком редко: {share}")
        self.assertLessEqual(share, 0.016, f"юмор выпадает слишком часто: {share}")

    def test_hits_use_the_whole_joke_set(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(20_000)]
        found = {ui_humor.pick_variant(key) for key in keys}
        found.discard(None)
        self.assertEqual(found, set(ui_humor.HUMOR_JOKES))

    def test_whitelist_allows_only_question_and_button(self) -> None:
        self.assertTrue(ui_humor.humor_allowed(ui_humor.SCREEN_QUESTION))
        self.assertTrue(ui_humor.humor_allowed(ui_humor.SCREEN_BUTTON))
        for kind in (
            ui_humor.SCREEN_ERROR,
            ui_humor.SCREEN_RIGHTS,
            ui_humor.SCREEN_LIMITS,
            ui_humor.SCREEN_DATA_LOSS,
            "unknown",
            "",
        ):
            self.assertFalse(ui_humor.humor_allowed(kind), kind)

    def test_forbidden_screens_never_joke_even_forced(self) -> None:
        keys = [f"{7 + index}:1:1:{index}" for index in range(5_000)]
        for kind in (
            ui_humor.SCREEN_ERROR,
            ui_humor.SCREEN_RIGHTS,
            ui_humor.SCREEN_LIMITS,
            ui_humor.SCREEN_DATA_LOSS,
        ):
            for key in keys:
                self.assertIsNone(
                    ui_humor.pick_variant(key, screen_kind=kind, forced=True),
                    f"{kind} не должен шутить ({key})",
                )

    def test_forced_show_ignores_rate_but_keeps_the_set(self) -> None:
        jokes = {
            ui_humor.pick_variant(
                f"empty:{index}",
                screen_kind=ui_humor.SCREEN_QUESTION,
                forced=True,
            )
            for index in range(500)
        }
        self.assertNotIn(None, jokes, "обязательный показ не должен пропадать")
        self.assertTrue(jokes <= set(ui_humor.HUMOR_JOKES))

    def test_flag_off_disables_everything_including_empty_announce(self) -> None:
        with patch("config.UI_HUMOR_ENABLED", False):
            self.assertFalse(ui_humor.humor_enabled())
            for index in range(2_000):
                self.assertIsNone(ui_humor.pick_variant(f"{7 + index}:1:1:{index}"))
            self.assertIsNone(
                ui_humor.pick_variant("empty:1", forced=True),
                "флаг главнее обязательного показа",
            )
            self.assertIsNone(ui_humor.empty_announcement_joke())
            self.assertIsNone(
                ui_humor.get_humor_line(announcement_is_empty=True),
                "пустой анонс без шутки при выключенном юморе",
            )

    def test_flag_on_shows_empty_announce_joke_and_hides_it_when_filled(self) -> None:
        with patch("config.UI_HUMOR_ENABLED", True):
            self.assertTrue(ui_humor.humor_enabled())
            joke = ui_humor.get_humor_line(announcement_is_empty=True)
            self.assertIn(joke, ui_humor.HUMOR_JOKES)
            self.assertEqual(ui_humor.get_humor_line(announcement_is_empty=True), joke)
            self.assertIsNone(ui_humor.get_humor_line(announcement_is_empty=False))

    def test_config_parses_env_flag_with_default_on(self) -> None:
        import config

        self.assertTrue(config.parse_humor_enabled(None))
        self.assertTrue(config.parse_humor_enabled("1"))
        self.assertTrue(config.parse_humor_enabled("да"))
        for raw in ("0", "false", "NO", " off ", "нет", "выкл"):
            self.assertFalse(config.parse_humor_enabled(raw), raw)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorQuestionScreenTests(unittest.TestCase):
    """Шуточные варианты: меняется только текст экрана вопроса."""

    @scenario_test
    async def test_joke_changes_text_only_not_keyboard_or_draft(self, h):
        template_id = _seed_template(h)
        # Ключ шутки содержит brick_id второго вопроса. Он зависит от числа
        # системных кирпичиков (U1a добавила `[город]`), поэтому берём его из
        # базы, а не хардкодом.
        with h.session_factory() as session:
            second_id = session.scalar(
                select(Brick.id).where(Brick.code == "dsc", Brick.scope == "personal")
            )
        assert second_id is not None, "личный кирпичик второго вопроса не создан"

        await h.text("/start")

        def fake_pick(key, **kwargs):
            # Шутим только на втором вопросе, чтобы сравнить один и тот же
            # экран в одном и том же состоянии.
            return JOKE_PROBE if f":{second_id}" in key else None

        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            self.assertNotIn(JOKE_PROBE, h.screen_text())
            await h.text("Заголовок анонса")
            self.assertIn("Вопрос 2 из 2", h.screen_text())
            joked_text = h.screen_text()
            joked_buttons = h.screen_buttons()
            self.assertIn(JOKE_PROBE, joked_text)

            # Тот же второй вопрос без шутки: назад и снова вперёд.
            with patch("utils.ui_humor.pick_variant", return_value=None):
                await h.callback(_callback(h, "aw_qback"))
                await h.callback(_callback(h, "aw_qnext"))
                plain_text = h.screen_text()
                plain_buttons = h.screen_buttons()
        after_plain = _details(h, _latest_draft(h).draft_id)
        self.assertNotIn(JOKE_PROBE, plain_text)
        self.assertIn("Вопрос 2 из 2", plain_text)
        # Описание кирпичика видно на экране вопроса (S5 п.5).
        self.assertIn("Что будет &amp; чего не будет", plain_text)

        self.assertEqual(
            [data for _label, data in joked_buttons],
            [data for _label, data in plain_buttons],
            "шутка не меняет callback_data и порядок кнопок",
        )
        self.assertEqual(
            [label for label, _data in joked_buttons],
            [label for label, _data in plain_buttons],
            "шутка не меняет подписи кнопок",
        )
        # Текст отличается только добавленной шуткой: служебная строка
        # «сохранено» живёт один показ и сравнивается отдельно.
        self.assertEqual(
            _without_notice(joked_text.replace(f"\n\n{JOKE_PROBE}", "")),
            _without_notice(plain_text),
        )

        # Состояние черновика шуткой не затронуто: до и после показа с
        # шуткой ревизия и значения совпадают.
        draft = _latest_draft(h)
        details = _details(h, draft.draft_id)
        self.assertEqual(
            [value.payload for value in details.values],
            [{"text": "Заголовок анонса"}],
        )
        self.assertEqual(details.revision, after_plain.revision)
        self.assertEqual(
            [(value.brick_id, value.status) for value in details.values],
            [(value.brick_id, value.status) for value in after_plain.values],
        )

    @scenario_test
    async def test_same_screen_keeps_the_same_text_after_redraw(self, h):
        template_id = _seed_template(h)
        keys: list[str] = []

        def fake_pick(key, **kwargs):
            keys.append(key)
            return JOKE_PROBE

        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            await h.text("Заголовок анонса")
            second_first = h.screen_text()
            # «◀️ Назад» и «➡️ Далее» перерисовывают тот же вопрос без
            # изменения данных: ключ и текст обязаны совпасть.
            await h.callback(_callback(h, "aw_qback"))
            self.assertIn("Вопрос 1 из 2", h.screen_text())
            await h.callback(_callback(h, "aw_qnext"))
            second_again = h.screen_text()

        self.assertGreaterEqual(len(keys), 4)
        self.assertEqual(keys[1], keys[3], "ключ одного экрана стабилен")
        self.assertEqual(
            _without_notice(second_again),
            _without_notice(second_first),
            "текст не «прыгает» при перерисовке",
        )

    @scenario_test
    async def test_error_screen_has_no_joke_ever(self, h):
        """Белый список: экран ошибки не шутит, даже когда юмор «выпал»."""
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="humor")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="dt",
                title="Дата",
                value_type="date",
                rules={},
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="ttl",
                title="Заголовок",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Ошибка",
                content="\n".join(p for p in ("", "[ttl]\n[dt]", "") if p),
            )
            session.commit()
            template_id = template.template_id

        def fake_pick(key, **kwargs):
            if kwargs.get("screen_kind") == ui_humor.SCREEN_ERROR:
                return None
            return JOKE_PROBE

        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", side_effect=fake_pick):
            await h.callback(f"tp_pick:{template_id}")
            self.assertIn(JOKE_PROBE, h.screen_text())
            await h.text("Заголовок анонса")
            self.assertIn("Вопрос 2 из 2", h.screen_text())
            # Невалидная дата: экран диагностики, юмор запрещён.
            await h.text("не дата вовсе")
            error_text = h.screen_text()
        self.assertIn("⚠️", error_text)
        self.assertNotIn(JOKE_PROBE, error_text)

    @scenario_test
    async def test_joke_never_reaches_live_block_or_publication(self, h):
        template_id = _seed_template(h, bricks=1)
        await h.text("/start")
        with patch("utils.ui_humor.pick_variant", return_value=JOKE_PROBE):
            await h.callback(f"tp_pick:{template_id}")
            self.assertIn(JOKE_PROBE, h.screen_text())
            await h.callback(_callback(h, "aw_qskip"))
            await h.callback(_callback(h, "aw_photo_none"))

        live = _live_text(h)
        self.assertNotIn(JOKE_PROBE, live)
        draft = _details(h, _latest_draft(h).draft_id)
        self.assertNotIn(JOKE_PROBE, draft.publication_text or "")
        for text in h.posts():
            self.assertNotIn(JOKE_PROBE, text or "")

    @scenario_test
    async def test_flag_off_removes_joke_and_empty_announce_line(self, h):
        template_id = _seed_template(h, bricks=1)
        await h.text("/start")
        with patch("config.UI_HUMOR_ENABLED", False):
            await h.callback(f"tp_pick:{template_id}")
            question_screen = h.screen_text()
            await h.callback(_callback(h, "aw_qskip"))
            empty_screen = h.screen_text()
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, question_screen, "флаг выключает и вопросы")
            self.assertNotIn(joke, empty_screen, "флаг главнее пустого анонса")
        # Экран вопроса остаётся рабочим: юмор не влияет на смысл.
        self.assertIn("Вопрос 1 из 1", question_screen)
        self.assertIn("Имя и фамилия ведущего", question_screen)
        self.assertIn("Фотографии анонса", empty_screen)
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, empty_screen, "при выключенном флаге шуток нет")

    @scenario_test
    async def test_description_is_escaped_on_the_question_screen(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="humor")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="ttl",
                title="Ведущий",
                value_type="short_text",
                rules={},
                description='<b>Имя</b> & "без должности"',
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Экранирование",
                content="\n".join(p for p in ("", "[ttl]", "") if p),
            )
            session.commit()
            template_id = template.template_id
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        text = h.screen_text()
        self.assertIn("&lt;b&gt;Имя&lt;/b&gt; &amp;", text)
        self.assertNotIn("<b>Имя</b>", text)


if __name__ == "__main__":
    unittest.main()
