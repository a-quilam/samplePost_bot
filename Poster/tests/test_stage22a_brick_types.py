# tests/test_stage22a_brick_types.py
"""
Stage 22A: закрытый каталог типов, коды и чистые проверки значений.

Тесты не открывают базу и Telegram: проверяются контракты brick_types.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from brick_types import (
    BRICK_SOURCE_ASK,
    BRICK_SOURCE_CONTEXT,
    BRICK_SOURCE_PLACE,
    BRICK_TYPE_CHOICE,
    BRICK_TYPE_DATE,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_TIME,
    BRICK_TYPE_URL,
    BRICK_TYPES,
    FORBIDDEN_BRICK_CODES,
    MAX_BRICK_CODE_LENGTH,
    SYSTEM_BRICKS,
    BrickCodeError,
    BrickRulesError,
    BrickValidationError,
    BrickValueStatus,
    clean_brick_title,
    ensure_brick_source,
    ensure_brick_type,
    normalize_brick_code,
    parse_rules,
    rules_from_json,
    rules_to_json,
    validate_brick_code,
    validate_brick_value,
    validate_rules,
)


class BrickTypeCatalogTests(unittest.TestCase):
    def test_catalog_has_only_the_seven_supported_types(self) -> None:
        self.assertEqual(
            set(BRICK_TYPES),
            {
                "short_text",
                "long_text",
                "date",
                "time",
                "url",
                "number",
                "choice",
            },
        )
        for forbidden in ("any", "photo", "media", "photos"):
            with self.assertRaises(BrickValidationError):
                ensure_brick_type(forbidden)

    def test_system_registry_has_eleven_stable_ids_and_codes(self) -> None:
        # 9 кирпичиков миграции 6 + `[адрес]` миграции 15 (Poster 2.0 S1)
        # + `[город]` миграции 21 (Poster 2.0 U1a).
        self.assertEqual(len(SYSTEM_BRICKS), 11)
        self.assertEqual([item.id for item in SYSTEM_BRICKS], list(range(1, 12)))
        self.assertEqual(
            [item.code for item in SYSTEM_BRICKS],
            [
                "название",
                "дата",
                "время_начала",
                "время_окончания",
                "место",
                "адрес_места",
                "ссылка_места",
                "описание",
                "контакт",
                "адрес",
                "город",
            ],
        )
        by_code = {item.code: item for item in SYSTEM_BRICKS}
        self.assertEqual(by_code["название"].value_type, BRICK_TYPE_SHORT_TEXT)
        self.assertEqual(by_code["дата"].value_type, BRICK_TYPE_DATE)
        self.assertEqual(by_code["дата"].rules, {"horizon_months": 12})
        self.assertEqual(by_code["время_начала"].value_type, BRICK_TYPE_TIME)
        self.assertEqual(by_code["время_окончания"].value_type, BRICK_TYPE_TIME)
        self.assertEqual(by_code["место"].source, BRICK_SOURCE_PLACE)
        self.assertEqual(by_code["адрес_места"].source, BRICK_SOURCE_PLACE)
        # Poster 2.0 S1: пользовательский код эталона берёт адрес площадки.
        self.assertEqual(by_code["адрес"].source, BRICK_SOURCE_PLACE)
        self.assertEqual(by_code["адрес"].value_type, BRICK_TYPE_SHORT_TEXT)
        self.assertEqual(by_code["ссылка_места"].value_type, BRICK_TYPE_URL)
        self.assertEqual(by_code["ссылка_места"].source, BRICK_SOURCE_PLACE)
        self.assertEqual(by_code["описание"].value_type, BRICK_TYPE_LONG_TEXT)
        self.assertEqual(by_code["контакт"].value_type, BRICK_TYPE_SHORT_TEXT)
        self.assertTrue(all(item.title.strip() for item in SYSTEM_BRICKS))

    def test_code_normalizes_unicode_case_and_whitespace(self) -> None:
        self.assertEqual(normalize_brick_code("  ЦЕНА  "), "цена")
        self.assertEqual(normalize_brick_code("Ёлка"), "ёлка")
        self.assertEqual(normalize_brick_code("ＡＢ"), "ab")
        self.assertEqual(normalize_brick_code("a_b-c9"), "a_b-c9")

    def test_code_rejects_invalid_syntax_and_limits_only_the_token(self) -> None:
        self.assertEqual(normalize_brick_code("a" * MAX_BRICK_CODE_LENGTH), "a" * 64)
        for raw in (
            "",
            "a",
            "a" * 65,
            "a b",
            "a[b",
            "a]b",
            "a\nb",
            "__",
            "a.b",
        ):
            with self.subTest(raw=raw), self.assertRaises(BrickCodeError):
                normalize_brick_code(raw)

    def test_photo_code_is_forbidden_and_is_not_a_value_type(self) -> None:
        self.assertEqual(FORBIDDEN_BRICK_CODES, {"фото"})
        with self.assertRaises(BrickCodeError):
            validate_brick_code("ФОТО")
        for value_type in ("photo", "media", "any"):
            with (
                self.subTest(value_type=value_type),
                self.assertRaises(BrickValidationError),
            ):
                ensure_brick_type(value_type)

    def test_title_allows_duplicates_and_has_no_arbitrary_length_cap(self) -> None:
        self.assertEqual(clean_brick_title("  Кофейня\n«Дом»  "), "Кофейня «Дом»")
        long_title = "Название " + "я" * 10_000
        self.assertEqual(clean_brick_title(long_title), long_title)
        self.assertEqual(
            clean_brick_title("Одинаковое"), clean_brick_title("Одинаковое")
        )
        for raw in ("", "   ", "\n\t"):
            with self.subTest(raw=raw), self.assertRaises(BrickValidationError):
                clean_brick_title(raw)

    def test_sources_are_separate_from_scopes(self) -> None:
        for source in (BRICK_SOURCE_ASK, BRICK_SOURCE_PLACE, BRICK_SOURCE_CONTEXT):
            self.assertEqual(ensure_brick_source(source), source)
        with self.assertRaises(BrickValidationError):
            ensure_brick_source("user")


class BrickValueContractTests(unittest.TestCase):
    TODAY = date(2026, 10, 23)

    def test_text_values_are_not_truncated(self) -> None:
        long_text = "Начало\n" + ("строка " * 5_000) + "\nКонец"
        result = validate_brick_value(BRICK_TYPE_LONG_TEXT, long_text)
        self.assertEqual(result.status, BrickValueStatus.VALID)
        self.assertEqual(result.payload, {"text": long_text})
        short = "  короткий текст  "
        result = validate_brick_value(BRICK_TYPE_SHORT_TEXT, short)
        self.assertEqual(result.payload, {"text": short})

    def test_empty_optional_and_invalid_are_distinct(self) -> None:
        for value_type in (
            BRICK_TYPE_SHORT_TEXT,
            BRICK_TYPE_LONG_TEXT,
            BRICK_TYPE_DATE,
            BRICK_TYPE_TIME,
            BRICK_TYPE_URL,
            BRICK_TYPE_NUMBER,
        ):
            with self.subTest(value_type=value_type, value=None):
                self.assertEqual(
                    validate_brick_value(value_type, None).status,
                    BrickValueStatus.INVALID,
                )
                self.assertEqual(
                    validate_brick_value(value_type, None, allow_empty=True).status,
                    BrickValueStatus.EMPTY_OPTIONAL,
                )
            with self.subTest(value_type=value_type, value="   "):
                self.assertEqual(
                    validate_brick_value(value_type, "   ", allow_empty=True).status,
                    BrickValueStatus.EMPTY_OPTIONAL,
                )
        result = validate_brick_value(
            BRICK_TYPE_CHOICE,
            [],
            rules={"choices": ["один", "два"]},
            allow_empty=True,
        )
        self.assertEqual(result.status, BrickValueStatus.EMPTY_OPTIONAL)
        zero = validate_brick_value(BRICK_TYPE_NUMBER, 0)
        self.assertEqual(zero.status, BrickValueStatus.VALID)
        self.assertEqual(zero.payload, {"number": 0})

    def test_each_supported_type_returns_a_typed_payload(self) -> None:
        cases = [
            (BRICK_TYPE_SHORT_TEXT, "Текст", None, {"text": "Текст"}),
            (BRICK_TYPE_LONG_TEXT, "Две\nстроки", None, {"text": "Две\nстроки"}),
            (BRICK_TYPE_DATE, "25.12.2026", None, {"date": "25.12.2026"}),
            (BRICK_TYPE_TIME, "1830", None, {"time": "18:30"}),
            (BRICK_TYPE_URL, "example.com", None, {"url": "https://example.com"}),
            (BRICK_TYPE_NUMBER, "350", {"integer": True}, {"number": 350}),
            (
                BRICK_TYPE_CHOICE,
                "два",
                {"choices": ["один", "два"]},
                {"choice": "два"},
            ),
        ]
        for value_type, raw, rules, payload in cases:
            with self.subTest(value_type=value_type):
                result = validate_brick_value(
                    value_type, raw, rules=rules, today=self.TODAY
                )
                self.assertEqual(result.status, BrickValueStatus.VALID)
                self.assertEqual(result.payload, payload)

    def test_date_reuses_parser_and_twelve_month_horizon(self) -> None:
        rules = {"horizon_months": 12}
        for raw, expected in (
            ("23.10.2026", "23.10.2026"),
            ("25.12", "25.12.2026"),
            ("завтра", "24.10.2026"),
            ("23.10.2027", "23.10.2027"),
        ):
            with self.subTest(raw=raw):
                result = validate_brick_value(
                    BRICK_TYPE_DATE, raw, rules=rules, today=self.TODAY
                )
                self.assertEqual(result.status, BrickValueStatus.VALID)
                self.assertEqual(result.payload, {"date": expected})
        for raw in ("22.10.2026", "24.10.2027", "31.02.2026"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_brick_value(
                        BRICK_TYPE_DATE, raw, rules=rules, today=self.TODAY
                    ).status,
                    BrickValueStatus.INVALID,
                )

    def test_date_bounds_rules_and_bad_rules_are_strict(self) -> None:
        rules = {"min_date": "01.02.2027", "max_date": "01.03.2027"}
        self.assertEqual(
            validate_brick_value(
                BRICK_TYPE_DATE, "15.02.2027", rules=rules, today=self.TODAY
            ).payload,
            {"date": "15.02.2027"},
        )
        self.assertEqual(
            validate_brick_value(
                BRICK_TYPE_DATE, "31.01.2027", rules=rules, today=self.TODAY
            ).status,
            BrickValueStatus.INVALID,
        )
        bad_rules: list[dict[str, object]] = [
            {"horizon_months": -1},
            {"min_date": "31.02.2027"},
            {"max_date": "завтра"},
            {"min_date": "02.02.2027", "max_date": "01.02.2027"},
            {"unknown": 1},
        ]
        for candidate in bad_rules:
            with self.subTest(rules=candidate), self.assertRaises(BrickRulesError):
                validate_rules(BRICK_TYPE_DATE, candidate)

    def test_time_reuses_evening_and_compact_rules(self) -> None:
        for raw, expected in (
            ("7", "19:00"),
            ("1830", "18:30"),
            ("7 утра", "07:00"),
            ("12 ночи", "00:00"),
        ):
            with self.subTest(raw=raw):
                result = validate_brick_value(BRICK_TYPE_TIME, raw)
                self.assertEqual(result.status, BrickValueStatus.VALID)
                self.assertEqual(result.payload, {"time": expected})
        self.assertEqual(
            validate_brick_value(BRICK_TYPE_TIME, "25:00").status,
            BrickValueStatus.INVALID,
        )

    def test_url_normalizes_telegram_and_domains_but_rejects_danger(self) -> None:
        for raw, expected in (
            ("@event_bot", "https://t.me/event_bot"),
            ("t.me/event", "https://t.me/event"),
            ("telegram.me/event", "https://t.me/event"),
            ("example.com/path?q=1#x", "https://example.com/path?q=1#x"),
            ("https://example.com/a", "https://example.com/a"),
        ):
            with self.subTest(raw=raw):
                result = validate_brick_value(BRICK_TYPE_URL, raw)
                self.assertEqual(result.status, BrickValueStatus.VALID)
                self.assertEqual(result.payload, {"url": expected})
        for raw in (
            "javascript:alert(1)",
            "data:text/html,hi",
            "file:///etc/passwd",
            "tg://resolve?domain=x",
            "просто текст",
            "user@example.com",
            "https://example.com/a b",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_brick_value(BRICK_TYPE_URL, raw).status,
                    BrickValueStatus.INVALID,
                )

    def test_number_rules_and_values(self) -> None:
        rules = {"min": 0, "max": 1000, "integer": True, "suffix": " ₽"}
        self.assertEqual(
            validate_brick_value(BRICK_TYPE_NUMBER, "350", rules=rules).payload,
            {"number": 350},
        )
        self.assertEqual(
            validate_brick_value(BRICK_TYPE_NUMBER, "3.5", rules={"min": 0}).payload,
            {"number": 3.5},
        )
        for raw in ("-1", "350.5", "не число", True, None):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_brick_value(BRICK_TYPE_NUMBER, raw, rules=rules).status,
                    BrickValueStatus.INVALID,
                )
        for candidate in (
            {"min": "0"},
            {"max": True},
            {"integer": 1},
            {"unknown": 1},
            {"min": 10, "max": 1},
        ):
            with self.subTest(rules=candidate), self.assertRaises(BrickRulesError):
                validate_rules(BRICK_TYPE_NUMBER, candidate)

    def test_choice_single_and_multiple_are_declarative(self) -> None:
        single = {"choices": ["один", "два"]}
        self.assertEqual(
            validate_brick_value(BRICK_TYPE_CHOICE, "два", rules=single).payload,
            {"choice": "два"},
        )
        self.assertEqual(
            validate_brick_value(BRICK_TYPE_CHOICE, ["два"], rules=single).status,
            BrickValueStatus.INVALID,
        )
        multiple = {"choices": ["один", "два", "три"], "multiple": True}
        self.assertEqual(
            validate_brick_value(
                BRICK_TYPE_CHOICE, ["три", "один"], rules=multiple
            ).payload,
            {"choices": ["один", "три"]},
        )
        for raw in ("четыре", ["один", "один"], ["один", "нет"]):
            with self.subTest(raw=raw):
                self.assertEqual(
                    validate_brick_value(BRICK_TYPE_CHOICE, raw, rules=multiple).status,
                    BrickValueStatus.INVALID,
                )
        bad_choice_rules: list[dict[str, object]] = [
            {},
            {"choices": []},
            {"choices": ["один", "один"]},
            {"choices": "один"},
            {"choices": ["один"], "unknown": True},
        ]
        for candidate in bad_choice_rules:
            with self.subTest(rules=candidate), self.assertRaises(BrickRulesError):
                validate_rules(BRICK_TYPE_CHOICE, candidate)

    def test_rules_are_json_data_and_unknown_keys_cannot_execute_sql(self) -> None:
        parsed = parse_rules(
            BRICK_TYPE_NUMBER, '{"min": 0, "integer": true, "suffix": "шт."}'
        )
        self.assertEqual(parsed, {"min": 0, "integer": True, "suffix": "шт."})
        self.assertEqual(
            rules_to_json(BRICK_TYPE_NUMBER, {"min": 0, "integer": True}),
            '{"integer": true, "min": 0}',
        )
        self.assertEqual(
            rules_from_json(BRICK_TYPE_NUMBER, '{"integer": true, "min": 0}'),
            {"integer": True, "min": 0},
        )
        for value in ("[]", "null", "1", '"x"'):
            with self.subTest(value=value), self.assertRaises(BrickRulesError):
                parse_rules(BRICK_TYPE_NUMBER, value)
        with self.assertRaises(BrickRulesError):
            parse_rules(BRICK_TYPE_NUMBER, "{bad json")
        with self.assertRaises(BrickRulesError):
            parse_rules(
                BRICK_TYPE_NUMBER,
                json.dumps({"x": "1; DROP TABLE bricks; --"}),
            )
        with self.assertRaises(BrickRulesError):
            parse_rules(BRICK_TYPE_SHORT_TEXT, {"min_length": 1})

    def test_allow_empty_never_hides_malformed_nonempty_values(self) -> None:
        cases = [
            (BRICK_TYPE_DATE, "31.02.2026", None),
            (BRICK_TYPE_TIME, "25:00", None),
            (BRICK_TYPE_URL, "javascript:alert(1)", None),
            (BRICK_TYPE_NUMBER, "abc", None),
            (BRICK_TYPE_CHOICE, "нет", {"choices": ["да"]}),
        ]
        for value_type, raw, rules in cases:
            with self.subTest(value_type=value_type):
                self.assertEqual(
                    validate_brick_value(
                        value_type, raw, rules=rules, allow_empty=True
                    ).status,
                    BrickValueStatus.INVALID,
                )

    def test_system_seed_rules_are_valid_for_their_types(self) -> None:
        for item in SYSTEM_BRICKS:
            with self.subTest(code=item.code):
                validated = validate_rules(item.value_type, item.rules)
                self.assertEqual(validated, dict(item.rules))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
