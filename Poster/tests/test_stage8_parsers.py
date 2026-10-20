# tests/test_stage8_parsers.py
"""
Тесты Stage 8 (гибкие даты, время, лото-пасхалка, ссылки):

- даты: все согласованные форматы, относительные, дни недели,
  високосные годы, границы суток/года, ошибки и неоднозначности;
- время: цифровые формы буквально, разговорные с вечерним правилом,
  особые случаи 0 и 12, явные утро/день/вечер/ночь, ошибки;
- лото-пасхалка: полный перенос значениями оригинала anounce,
  исправление substring-бага, границы токенов, негативы;
- ссылки: нормализация, опасные схемы, произвольный текст;
- конфигурация POST_STEPS: нормализаторы на месте, подсказки обновлены,
  пасхалка не рекламируется.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest
from datetime import date, datetime

try:
    from handlers.post_creation import POST_STEPS
    from utils.lotto import LOTTO_NUMBERS, substitute_lotto
    from utils.parsers import APP_TZ, parse_date_flexible, parse_time_flexible
    from utils.validators import (
        normalize_date,
        normalize_time,
        normalize_url,
        validate_date,
        validate_time,
        validate_url,
    )

    IMPORT_ERROR = None
except Exception as e:  # причина попадает в сообщение пропуска
    IMPORT_ERROR = str(e)

# Фиксированная «сегодня» для воспроизводимости: вторник, 20.10.2026
TODAY = date(2026, 10, 20)
# Пятница календарной недели TODAY-а
FRIDAY = date(2026, 10, 23)


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class LottoDictionaryTest(unittest.TestCase):
    """Словарь перенесён полностью и в оригинальных значениях."""

    def test_total_entry_count_matches_original(self):
        """Оригинал anounce: 239 уникальных ключей; в переносе 238."""
        # Опущены: цифровой ключ «38» (дублирует значение, бесполезен) и
        # проигравшие дубли («сено косим» 28, «туда-сюда» 59,
        # «перевертыши» 59) — runtime-поведение идентично оригиналу
        # (сверено ast-разбором оригинала: значения и ключи совпадают).
        self.assertEqual(len(LOTTO_NUMBERS), 238)

    def test_flagship_values(self):
        self.assertEqual(LOTTO_NUMBERS["гуси-лебеди"], 22)
        self.assertEqual(LOTTO_NUMBERS["топор"], 7)
        self.assertEqual(LOTTO_NUMBERS["ноль"], 0)
        self.assertEqual(LOTTO_NUMBERS["сто"], 100)
        self.assertEqual(LOTTO_NUMBERS["петровка"], 38)
        self.assertEqual(LOTTO_NUMBERS["перестройка"], 85)
        self.assertEqual(LOTTO_NUMBERS["чернобыль"], 86)

    def test_duplicate_keys_keep_original_runtime_behavior(self):
        """Дубли оригинала: позднее значение перетирает раннее (как в anounce)."""
        self.assertEqual(LOTTO_NUMBERS["сено косим"], 48)  # не 28
        self.assertEqual(LOTTO_NUMBERS["туда-сюда"], 69)  # не 59
        self.assertEqual(LOTTO_NUMBERS["перевертыши"], 69)  # не 59

    def test_substitution_basic(self):
        self.assertEqual(substitute_lotto("гуси-лебеди"), "22")
        self.assertEqual(substitute_lotto("топор"), "7")
        self.assertEqual(substitute_lotto("дюжина"), "12")
        self.assertEqual(substitute_lotto("сто"), "100")

    def test_substitution_normalizes_case_dashes_and_yo(self):
        self.assertEqual(substitute_lotto("ГУСИ-ЛЕБЕДИ"), "22")
        self.assertEqual(substitute_lotto("гуси лебеди"), "22")
        self.assertEqual(substitute_lotto("Гуси—Лебеди"), "22")
        self.assertEqual(substitute_lotto("ВОСЕМЬ"), "8")
        self.assertEqual(substitute_lotto("ЧЁрнобыль"), "86")  # ё→е, регистр

    def test_substitution_phrases_with_special_chars(self):
        self.assertEqual(substitute_lotto("день да ночь — сутки прочь"), "24")
        self.assertEqual(substitute_lotto("два сапога — пара"), "2")
        # Пунктуация вне фразы сохраняется (фраза внутри контекста)
        self.assertEqual(substitute_lotto("с новым годом!"), "31!")
        self.assertEqual(substitute_lotto("с новым годом"), "31")
        self.assertEqual(substitute_lotto("палата №6"), "6")
        self.assertEqual(substitute_lotto("холодное лето 53-го"), "53")
        self.assertEqual(substitute_lotto("али-баба"), "40")

    def test_substring_bug_fixed(self):
        """Оригинал путал подстроки: «два притопа» → 2. Теперь 23."""
        self.assertEqual(substitute_lotto("два притопа"), "23")
        self.assertEqual(substitute_lotto("три прихлопа"), "23")
        self.assertEqual(substitute_lotto("опять двадцать пять"), "25")
        self.assertEqual(substitute_lotto("лебедь с топором"), "27")
        self.assertEqual(substitute_lotto("лебединое озеро"), "20")
        self.assertEqual(substitute_lotto("бабушка надвое сказала"), "82")

    def test_token_boundaries_no_partial_words(self):
        """Фраза не совпадает внутри более длинного слова."""
        self.assertEqual(substitute_lotto("палатка"), "палатка")
        self.assertEqual(substitute_lotto("золотой"), "золотой")
        self.assertEqual(substitute_lotto("утятник"), "утятник")
        self.assertEqual(substitute_lotto("обычный текст"), "обычный текст")

    def test_native_date_phrases_not_substituted(self):
        """«8 марта» — дата, а не число 8: нативный разбор не ломаем."""
        self.assertEqual(substitute_lotto("8 марта"), "8 марта")
        self.assertEqual(substitute_lotto("17 ноября"), "17 ноября")
        self.assertEqual(substitute_lotto("сегодня"), "сегодня")
        self.assertEqual(substitute_lotto("в пятницу"), "в пятницу")

    def test_plain_numbers_and_digit_dash_preserved(self):
        self.assertEqual(substitute_lotto("17-11"), "17-11")
        self.assertEqual(substitute_lotto("17.11.2026"), "17.11.2026")
        self.assertEqual(substitute_lotto("18:30"), "18:30")

    def test_lotto_in_contexts(self):
        self.assertEqual(substitute_lotto("гуси-лебеди октября"), "22 октября")
        self.assertEqual(substitute_lotto("гуси-лебеди:15"), "22:15")
        self.assertEqual(substitute_lotto("топор марта"), "7 марта")


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class DateParserTest(unittest.TestCase):
    """parse_date_flexible: форматы, относительные, неделя, ошибки."""

    def assert_date(self, text: str, expected: str):
        self.assertEqual(
            parse_date_flexible(text, today=TODAY),
            expected,
            f"дата '{text}' должна стать '{expected}'",
        )

    def assert_invalid(self, text: str):
        self.assertIsNone(
            parse_date_flexible(text, today=TODAY),
            f"дата '{text}' должна быть отклонена",
        )

    def test_numeric_formats(self):
        self.assert_date("17.11.2026", "17.11.2026")
        self.assert_date("17.11.26", "17.11.2026")  # ГГ → 20ГГ
        self.assert_date("17.11", "17.11.2026")  # год текущий
        self.assert_date("17/11/2026", "17.11.2026")
        self.assert_date("17-11-2026", "17.11.2026")
        self.assert_date("17-11", "17.11.2026")

    def test_compact_formats(self):
        self.assert_date("1711", "17.11.2026")  # ДДММ
        self.assert_date("171126", "17.11.2026")  # ДДММГГ
        self.assert_date("0101", "01.01.2026")

    def test_month_word_formats(self):
        self.assert_date("17 ноября", "17.11.2026")
        self.assert_date("17 ноя", "17.11.2026")
        self.assert_date("17 ноя.", "17.11.2026")
        self.assert_date("17 ноября 2025", "17.11.2025")
        self.assert_date("1 января", "01.01.2026")
        self.assert_date("31 декабря", "31.12.2026")
        self.assert_date("29 февраля 2028", "29.02.2028")
        # «8 марта» НЕ подменяется пасхалкой — нативный разбор даты главнее
        self.assert_date("8 марта", "08.03.2026")

    def test_relative_dates(self):
        self.assert_date("сегодня", "20.10.2026")
        self.assert_date("завтра", "21.10.2026")
        self.assert_date("послезавтра", "22.10.2026")
        self.assert_date("через неделю", "27.10.2026")
        self.assert_date("через 1 день", "21.10.2026")
        self.assert_date("через 3 дня", "23.10.2026")
        self.assert_date("через 10 дней", "30.10.2026")
        self.assert_date("через десять дней", "30.10.2026")  # лото-подстановка

    def test_weekdays_regular(self):
        """«В X» — ближайший X строго после сегодняшнего дня."""
        self.assert_date("в пятницу", "23.10.2026")
        self.assert_date("в понедельник", "26.10.2026")
        self.assert_date("в воскресенье", "25.10.2026")
        self.assert_date("во вторник", "27.10.2026")  # сегодня вторник → +7

    def test_weekdays_next_week(self):
        """«В следующую X» — день следующей календарной недели."""
        self.assert_date("в следующую пятницу", "30.10.2026")
        self.assert_date("в следующий понедельник", "26.10.2026")

    def test_weekdays_when_today_is_friday(self):
        """Если сегодня пятница — оба выражения дают пятницу следующей недели."""
        for text in ("в пятницу", "в следующую пятницу"):
            self.assertEqual(
                parse_date_flexible(text, today=FRIDAY),
                "30.10.2026",
                f"'{text}' в пятницу → 30.10.2026",
            )

    def test_lotto_dates(self):
        self.assert_date("гуси-лебеди октября", "22.10.2026")
        self.assert_date("ГУСИ-ЛЕБЕДИ ОКТЯБРЯ", "22.10.2026")
        self.assert_date("гуси лебеди октября", "22.10.2026")
        self.assert_date("топор марта", "07.03.2026")
        self.assert_date("дюжина марта", "12.03.2026")
        self.assert_date("топор", None)  # «7» без месяца — не дата

    def test_invalid_dates(self):
        self.assert_invalid("31.02.2026")  # февраля 31 нет
        self.assert_invalid("29.02.2025")  # невисокосный
        self.assert_invalid("00.01.2026")  # нет нулевого дня
        self.assert_invalid("32.01.2026")
        self.assert_invalid("15.13.2026")  # нет 13-го месяца
        self.assert_invalid("2215")  # в дате 4 цифры = ДДММ: месяц 15!
        self.assert_invalid("1713")  # месяц 13
        self.assert_invalid("0000")
        self.assert_invalid("17.11.265")  # трёхзначный год — не угадываем
        self.assert_invalid("17 ноября 26")  # двухзначный год при месяце — нет
        self.assert_invalid("через 0 дней")
        self.assert_invalid("через 4000 дней")
        self.assert_invalid("в пятницу extra")
        self.assert_invalid("просто текст")

    def test_leap_years(self):
        self.assert_date("29.02.2028", "29.02.2028")  # високосный
        self.assert_date("29.02.2000", "29.02.2000")  # високосный (÷400)
        self.assert_invalid("29.02.1900")  # не високосный (÷100)

    def test_year_boundaries(self):
        self.assert_date("31.12", "31.12.2026")
        self.assert_date("01.01.2027", "01.01.2027")
        self.assert_date("31 декабря", "31.12.2026")

    def test_default_today_uses_app_timezone(self):
        """Без today используется согласованная зона приложения."""
        expected = datetime.now(APP_TZ).date().strftime("%d.%m.%Y")
        self.assertEqual(normalize_date("сегодня"), expected)

    def test_normalize_date_wrapper(self):
        # Год без года — текущий в зоне приложения (тест не стареет)
        year = datetime.now(APP_TZ).year
        self.assertEqual(normalize_date("17.11"), f"17.11.{year}")
        self.assertIsNone(normalize_date("31.02.2026"))


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class TimeParserTest(unittest.TestCase):
    """parse_time_flexible: цифровые буквально, разговорные — вечером."""

    def assert_time(self, text: str, expected: str):
        self.assertEqual(
            parse_time_flexible(text),
            expected,
            f"время '{text}' должно стать '{expected}'",
        )

    def assert_invalid(self, text: str):
        self.assertIsNone(
            parse_time_flexible(text), f"время '{text}' должно быть отклонено"
        )

    def test_digital_literal_with_minutes(self):
        """ЧЧ:ММ и ЧЧ.ММ — буквально, без вечернего сдвига."""
        self.assert_time("18:30", "18:30")
        self.assert_time("18.30", "18:30")
        self.assert_time("7:30", "07:30")  # цифровая запись — буквально
        self.assert_time("12:30", "12:30")  # особое правило 12 не действует
        self.assert_time("09:00", "09:00")
        self.assert_time("00:00", "00:00")
        self.assert_time("23:59", "23:59")

    def test_compact_4_digits_literal(self):
        self.assert_time("1840", "18:40")
        self.assert_time("2215", "22:15")
        self.assert_time("1830", "18:30")
        self.assert_time("0900", "09:00")
        self.assert_time("0000", "00:00")
        self.assert_time("1210", "12:10")
        self.assert_time("1230", "12:30")

    def test_three_digits_is_conversational(self):
        """3 цифры = разговорный час с минутами → вечернее правило."""
        self.assert_time("930", "21:30")
        self.assert_time("930 утра", "09:30")
        self.assert_time("130", "13:30")
        self.assert_time("030", "00:30")
        self.assert_time("123", "13:23")

    def test_space_form_is_conversational(self):
        self.assert_time("18 30", "18:30")
        self.assert_time("9 30", "21:30")
        self.assert_time("12 30", "00:30")  # разговорные 12 → полночь

    def test_bare_hour_evening_default(self):
        self.assert_time("7", "19:00")
        self.assert_time("9", "21:00")
        self.assert_time("18", "18:00")
        self.assert_time("17", "17:00")
        self.assert_time("23", "23:00")
        self.assert_time("0", "00:00")

    def test_special_zero_and_twelve(self):
        self.assert_time("12", "00:00")  # особое правило: 12 → полночь
        self.assert_time("00", "00:00")

    def test_explicit_periods(self):
        self.assert_time("12 утра", "12:00")
        self.assert_time("12 дня", "12:00")
        self.assert_time("7 утра", "07:00")
        self.assert_time("7 вечера", "19:00")
        self.assert_time("9:30 ночи", "21:30")
        self.assert_time("12:30 ночи", "00:30")
        self.assert_time("12 ночи", "00:00")
        self.assert_time("12 вечера", "00:00")
        self.assert_time("1230 ночи", "00:30")

    def test_conversational_expressions(self):
        self.assert_time("полседьмого", "18:30")
        self.assert_time("пол седьмого", "18:30")
        self.assert_time("половина седьмого", "18:30")
        self.assert_time("полседьмого утра", "06:30")
        self.assert_time("пол12", "23:30")
        self.assert_time("пол 12", "23:30")
        self.assert_time("пол двенадцатого", "23:30")
        self.assert_time("пол первого", "00:30")
        self.assert_time("четверть восьмого", "19:15")
        self.assert_time("четверть восьмого утра", "07:15")
        self.assert_time("без четверти семь", "18:45")
        self.assert_time("без четверти семь утра", "06:45")
        self.assert_time("без 15 пять", "16:45")
        self.assert_time("без пятнадцати пять", "16:45")  # лото-подстановка

    def test_lotto_times(self):
        self.assert_time("гуси-лебеди:15", "22:15")
        self.assert_time("топор:30", "07:30")  # цифровое с минутами — буквально
        self.assert_time("топор", "19:00")  # «7» разговорно → вечер
        self.assert_time("дюжина", "00:00")  # «12» разговорно → полночь

    def test_invalid_times(self):
        self.assert_invalid("25:00")  # часа 25 не бывает
        self.assert_invalid("18:70")  # минут 70
        self.assert_invalid("18:5")  # минуты должны быть двумя цифрами
        self.assert_invalid("2999")  # час 29
        self.assert_invalid("2400")
        self.assert_invalid("37:00")
        self.assert_invalid("18:30 вечера")  # 18 с «вечера» — не угадываем
        self.assert_invalid("18:30 утра")  # 18 с «утра» — не угадываем
        self.assert_invalid("13:00 ночи")
        self.assert_invalid("абв")
        self.assert_invalid("")
        self.assert_invalid("просто текст")
        self.assert_invalid("пол тринадцатого")  # часа 13 в «пол» нет

    def test_normalize_time_wrapper(self):
        self.assertEqual(normalize_time("7"), "19:00")
        self.assertEqual(normalize_time("1830"), "18:30")
        self.assertIsNone(normalize_time("25:00"))

    def test_day_boundaries(self):
        self.assert_time("23:59", "23:59")
        self.assert_time("00:00", "00:00")
        self.assert_time("0000", "00:00")


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class UrlNormalizationTest(unittest.TestCase):
    """normalize_url: нормализация, безопасность, негативы."""

    def assert_url(self, text: str, expected: str):
        self.assertEqual(normalize_url(text), expected, f"'{text}' → '{expected}'")

    def assert_invalid(self, text: str):
        self.assertIsNone(normalize_url(text), f"'{text}' должно быть отклонено")

    def test_telegram_forms(self):
        self.assert_url("t.me/random_culture", "https://t.me/random_culture")
        self.assert_url("https://t.me/random_culture", "https://t.me/random_culture")
        self.assert_url("telegram.me/random_culture", "https://t.me/random_culture")
        self.assert_url("www.t.me/x", "https://t.me/x")
        self.assert_url("@random_culture", "https://t.me/random_culture")
        self.assert_url("t.me/+AbCdEf", "https://t.me/+AbCdEf")

    def test_ordinary_sites(self):
        self.assert_url("example.com", "https://example.com")
        self.assert_url("https://example.com", "https://example.com")
        self.assert_url("http://example.com", "http://example.com")
        self.assert_url("www.example.com", "https://www.example.com")

    def test_paths_and_params_preserved_verbatim(self):
        self.assert_url(
            "example.com/page?x=1&y=2#block",
            "https://example.com/page?x=1&y=2#block",
        )
        self.assert_url("t.me/channel/123", "https://t.me/channel/123")
        self.assert_url("example.com/Файл", "https://example.com/Файл")

    def test_dangerous_schemes_rejected(self):
        self.assert_invalid("javascript:alert(1)")
        self.assert_invalid("data:text/html;base64,PHNjcmlwdD4=")
        self.assert_invalid("file:///etc/passwd")
        self.assert_invalid("tg://resolve?domain=x")
        self.assert_invalid("ftp://example.com")
        self.assert_invalid("vbscript:msgbox(1)")

    def test_malformed_rejected(self):
        self.assert_invalid("")
        self.assert_invalid("   ")
        self.assert_invalid("просто текст")
        self.assert_invalid("hello world")
        self.assert_invalid("example")  # без точки — не домен
        self.assert_invalid("http://localhost")  # без доменной зоны
        self.assert_invalid("https://")  # нет хоста
        self.assert_invalid("http:example.com")  # без слэша
        self.assert_invalid("//example.com")  # протокол-относительный
        self.assert_invalid("user@example.com")  # учётная запись/почта
        self.assert_invalid("https://user:pass@example.com")
        self.assert_invalid("exam ple.com")  # пробел
        self.assert_invalid("https://exam ple.com")
        self.assert_invalid("example.com:99999")  # порт вне диапазона

    def test_validate_wrappers(self):
        self.assertTrue(validate_url("example.com"))
        self.assertFalse(validate_url("javascript:alert(1)"))
        self.assertTrue(validate_date("17.11"))
        self.assertFalse(validate_date("31.02.2026"))
        self.assertTrue(validate_time("18:30"))
        self.assertFalse(validate_time("25:00"))


@unittest.skipIf(IMPORT_ERROR is not None, f"импорт недоступен: {IMPORT_ERROR}")
class PostStepsConfigTest(unittest.TestCase):
    """POST_STEPS: нормализаторы подключены, подсказки обновлены."""

    def _step(self, key: str) -> dict:
        return next(s for s in POST_STEPS if s["key"] == key)

    def test_normalizers_present(self):
        for key in ("date", "time_start", "time_end", "place_url"):
            self.assertIsNotNone(
                self._step(key).get("normalizer"), f"нет normalizer у '{key}'"
            )

    def test_date_step_normalizes_storage(self):
        step = self._step("date")
        year = datetime.now(APP_TZ).year
        self.assertEqual(step["normalizer"]("17.11"), f"17.11.{year}")
        self.assertEqual(step["normalizer"]("сегодня"), normalize_date("сегодня"))
        self.assertIsNone(step["normalizer"]("31.02.2026"))

    def test_time_steps_normalize_storage(self):
        for key in ("time_start", "time_end"):
            step = self._step(key)
            self.assertEqual(step["normalizer"]("1830"), "18:30")
            self.assertEqual(step["normalizer"]("7"), "19:00")
            self.assertIsNone(step["normalizer"]("25:00"))

    def test_url_step_normalizes_storage(self):
        step = self._step("place_url")
        self.assertEqual(
            step["normalizer"]("@random_culture"), "https://t.me/random_culture"
        )
        self.assertIsNone(step["normalizer"]("javascript:alert(1)"))

    def test_prompts_mention_new_formats(self):
        """Подсказки отражают новые возможности (и сохраняют старые маркеры)."""
        self.assertIn("ДД.ММ.ГГГГ", self._step("date")["prompt"])
        self.assertIn("сегодня", self._step("date")["prompt"])
        self.assertIn("ЧЧ:ММ", self._step("time_start")["prompt"])
        self.assertIn("930", self._step("time_start")["prompt"])
        self.assertIn("t.me", self._step("place_url")["prompt"])

    def test_easter_egg_not_advertised(self):
        """Пасхалка не упоминается ни в подсказках, ни в /help."""
        for step in POST_STEPS:
            for secret in ("гуси", "топор", "лото", "пасхалк", "лебеди"):
                self.assertNotIn(
                    secret,
                    step["prompt"].lower(),
                    f"пасхалка '{secret}' утекла в подсказку '{step['key']}'",
                )

    def test_old_draft_values_still_valid(self):
        """Старые значения черновиков проходят валидацию как прежде."""
        self.assertTrue(validate_date("25.12.2026"))
        self.assertTrue(validate_time("10:00"))
        self.assertTrue(validate_url("https://example.com"))


if __name__ == "__main__":
    unittest.main()
