# tests/test_stage9_stage8.py
"""
Stage 9: значения Stage 8 сквозь Telegram-диалог до превью.

Проверяется не сам парсер (это test_stage8_parsers), а прохождение
введённых значений через ConversationHandler: валидация, нормализация,
хранение в user_data, поведение при ошибке (поле не стирается, шаг не
двигается, подсказка показана). Часы замораживаются frozen_today —
относительные даты детерминированы.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import POST_CREATION, POST_STEPS
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        frozen_today,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


# Формы дат при замороженном «сегодня» = 20.10.2026 (вторник)
DATE_CASES = [
    ("1711", "17.11.2026"),
    ("17.11.26", "17.11.2026"),
    ("17.11.2026", "17.11.2026"),
    ("171126", "17.11.2026"),
    ("сегодня", "20.10.2026"),
    ("завтра", "21.10.2026"),
    ("в пятницу", "23.10.2026"),
    ("17 ноября", "17.11.2026"),
    ("17 ноя", "17.11.2026"),
    # лото-пасхалка подчиняется тем же правилам разбора даты
    ("гуси-лебеди октября", "22.10.2026"),
]

# Формы времени: разделитель не влияет, часы 1–11 вечерние по умолчанию
TIME_CASES = [
    ("7:30", "19:30"),
    ("7.30", "19:30"),
    ("730", "19:30"),
    ("7 30", "19:30"),
    ("930", "21:30"),
    ("9:00", "21:00"),
    ("0900", "21:00"),
    ("12", "00:00"),
    ("12:30", "12:30"),
    ("полседьмого", "18:30"),
    ("пол12", "23:30"),
    ("топор:30", "19:30"),  # лото: топор → 7:30 → вечернее правило
    ("7:30 утра", "07:30"),  # явный период важнее вечернего правила
]

URL_CASES = [
    ("t.me/random_culture", "https://t.me/random_culture"),
    ("https://t.me/random_culture", "https://t.me/random_culture"),
    ("@random_culture", "https://t.me/random_culture"),
    ("example.com", "https://example.com"),
]

# Неоднозначный/некорректный ввод — ошибка с подсказкой, поле не стирается
INVALID_CASES = [
    ("date", ["17 ноября 26", "17.11.265"]),
    ("time_start", ["18:30 вечера", "18:30 утра", "13:00 ночи"]),
    ("place_url", ["javascript:alert(1)", "data:text/html,x"]),
]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class Stage8ValuesThroughDialogTests(unittest.TestCase):
    """Даты, время и ссылки: путь «ввод → валидация → хранение»."""

    async def _check_at_step(self, h, key, raw, expected):
        await h.goto_step(step_index(key))
        await h.text(raw)
        self.assertEqual(
            h.user_data().get(key), expected, f"ввод {raw!r} для поля {key}"
        )
        self.assertEqual(h.dialog_state(), POST_CREATION)
        # Выход без мусора в следующих кейсах (ВИП сохраняется в черновик)
        await h.text("/cancel")
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_date_formats_normalize_through_dialog(self, h):
        with frozen_today(2026, 10, 20):
            for raw, expected in DATE_CASES:
                with self.subTest(raw=raw):
                    await self._check_at_step(h, "date", raw, expected)

    @scenario_test
    async def test_relative_dates_when_today_is_friday(self, h):
        with frozen_today(2026, 10, 23):  # пятница
            for raw, expected in (
                ("сегодня", "23.10.2026"),
                ("в пятницу", "30.10.2026"),
                ("в следующую пятницу", "30.10.2026"),
            ):
                with self.subTest(raw=raw):
                    await self._check_at_step(h, "date", raw, expected)

    @scenario_test
    async def test_time_forms_normalize_through_dialog(self, h):
        for raw, expected in TIME_CASES:
            with self.subTest(raw=raw):
                await self._check_at_step(h, "time_start", raw, expected)

    @scenario_test
    async def test_url_forms_normalize_through_dialog(self, h):
        for raw, expected in URL_CASES:
            with self.subTest(raw=raw):
                await self._check_at_step(h, "place_url", raw, expected)

    @scenario_test
    async def test_invalid_values_report_hint_and_keep_step(self, h):
        valid_sample = {
            "date": "17.11.2026",
            "time_start": "19:00",
            # ссылка проверяется в нормализованном виде (ожидание ввода)
            "place_url": "https://t.me/valid_channel",
        }
        for key, bad_values in INVALID_CASES:
            with self.subTest(field=key):
                await h.goto_step(step_index(key))
                step_before = h.user_data().get("current_step")

                for raw in bad_values:
                    await h.text(raw)
                    reply = h.reply_texts()[-1]
                    self.assertTrue(
                        reply.startswith("Некорректный формат."),
                        f"ожидалась подсказка для {raw!r}, получено: {reply!r}",
                    )
                    # Шаг не сдвинулся, поле не появилось, диалог жив
                    self.assertEqual(
                        h.user_data().get("current_step"), step_before, f"{raw!r}"
                    )
                    self.assertIsNone(h.user_data().get(key), f"{raw!r}")
                    self.assertEqual(h.dialog_state(), POST_CREATION)
                    # Кнопка пропуска доступна и на ошибке
                    self.assertIn("Пропустить", reply)

                # После ошибок корректное значение принимается как обычно
                await h.text(valid_sample[key])
                self.assertEqual(h.user_data().get(key), valid_sample[key])
                self.assertEqual(h.user_data().get("current_step"), step_before + 1)
                await h.text("/cancel")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EasterEggSilenceTests(unittest.TestCase):
    """Пасхалка лото-числительных не рекламируется в подсказках и /help."""

    # Слова, характерные только для словаря пасхалки
    SECRET_WORDS = (
        "топор",
        "сено",
        "косим",
        "перевертыш",
        "туда-сюда",
        "гуси",
        "лебеди",
        "лото",
    )

    def assert_no_secret(self, texts, source):
        joined = "\n".join(t.lower() for t in texts)
        for word in self.SECRET_WORDS:
            self.assertNotIn(word, joined, f"пасхалка {word!r} засветилась в {source}")

    @scenario_test
    async def test_secret_absent_from_start_help_prompts_and_edit(self, h):
        # /start и /help
        await h.text("/start")
        await h.text("/help")
        self.assert_no_secret(h.reply_texts(), "приветствии и справке")

        # Все промпты девяти шагов (диалог пройден на пропусках)
        await h.start_dialog()
        wizard_replies = [h.reply_texts()[-1]]
        for _ in range(len(POST_STEPS) - 1):
            await h.text("Пропустить")
            wizard_replies.append(h.reply_texts()[-1])
        self.assert_no_secret(wizard_replies, "промптах шагов")

        # Превью (пустое) → меню правки и промпты каждого поля
        await h.callback("edit_post")
        edit_texts = [h.reply_texts()[-1]]
        for step in POST_STEPS:
            await h.callback(f"edit_{step['key']}")
            edit_texts.append(str(h.calls[-1].get("text") or ""))
            await h.text("Пропустить")
        self.assert_no_secret(edit_texts, "промптах редактирования")
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_secret_still_works_as_input(self, h):
        """Пасхалка молчит в подсказках, но продолжает работать на вводе."""
        with frozen_today(2026, 10, 20):
            await h.goto_step(step_index("date"))
            await h.text("гуси-лебеди октября")
            self.assertEqual(h.user_data().get("date"), "22.10.2026")
            await h.text("/cancel")
