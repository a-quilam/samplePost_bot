# brick_types.py
"""
Каталог типов, технические коды и проверка правил кирпичиков (Stage 22A).

Слой не зависит от Telegram и SQLAlchemy: здесь только чистые функции
нормализации названий/кодов, разбора `rules_json` и проверки значений.
Хранение определений и проверки прав находятся в `brick_store.py`.

Принципы Stage 22A:

- тип значения — закрытый каталог; неопределённого `any` нет;
- `rules_json` — только декларативные данные, никакого eval/exec/SQL;
- пользовательские тексты не обрезаются и не ограничиваются произвольно;
- технический код ограничен 64 символами не как содержимое поста, а как
  токен синтаксиса `[код]`, который должен помещаться в сообщение об ошибке
  и будущие callback-и; это ограничение отдельно задокументировано;
- дата и время проверяются существующими парсерами Poster;
- URL проверяется существующим `normalize_url` (опасные схемы запрещены);
- фотографии не являются типом кирпичика: `photo`/`media` отсутствуют.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from utils.calendar_view import add_months
from utils.parsers import app_today, parse_date_flexible
from utils.validators import normalize_time, normalize_url


class BrickValidationError(ValueError):
    """Некорректное имя, тип, источник или правило кирпичика."""


class BrickCodeError(BrickValidationError):
    """Некорректный технический код кирпичика."""


class BrickRulesError(BrickValidationError):
    """Некорректные декларативные правила типа."""


# --- Закрытый каталог типов и источников ------------------------------------

BRICK_TYPE_SHORT_TEXT = "short_text"
BRICK_TYPE_LONG_TEXT = "long_text"
BRICK_TYPE_DATE = "date"
BRICK_TYPE_TIME = "time"
BRICK_TYPE_URL = "url"
BRICK_TYPE_NUMBER = "number"
BRICK_TYPE_CHOICE = "choice"

BRICK_TYPES = (
    BRICK_TYPE_SHORT_TEXT,
    BRICK_TYPE_LONG_TEXT,
    BRICK_TYPE_DATE,
    BRICK_TYPE_TIME,
    BRICK_TYPE_URL,
    BRICK_TYPE_NUMBER,
    BRICK_TYPE_CHOICE,
)

BRICK_TYPE_TITLES = {
    BRICK_TYPE_SHORT_TEXT: "Короткий текст",
    BRICK_TYPE_LONG_TEXT: "Длинный текст",
    BRICK_TYPE_DATE: "Дата",
    BRICK_TYPE_TIME: "Время",
    BRICK_TYPE_URL: "Ссылка",
    BRICK_TYPE_NUMBER: "Число",
    BRICK_TYPE_CHOICE: "Выбор",
}

BRICK_SCOPE_SYSTEM = "system"
BRICK_SCOPE_PLACE = "place"
BRICK_SCOPE_PERSONAL = "personal"
BRICK_SCOPES = (BRICK_SCOPE_SYSTEM, BRICK_SCOPE_PLACE, BRICK_SCOPE_PERSONAL)

BRICK_SOURCE_ASK = "ask"
BRICK_SOURCE_PLACE = "place"
BRICK_SOURCE_CONTEXT = "context"
BRICK_SOURCES = (BRICK_SOURCE_ASK, BRICK_SOURCE_PLACE, BRICK_SOURCE_CONTEXT)

BRICK_STATUS_ACTIVE = "active"
BRICK_STATUS_ARCHIVED = "archived"
BRICK_STATUSES = (BRICK_STATUS_ACTIVE, BRICK_STATUS_ARCHIVED)

MIN_BRICK_CODE_LENGTH = 2
MAX_BRICK_CODE_LENGTH = 64

# Запрещённый код: фотографии остаются отдельными вложениями анонса и не
# получают токен шаблона. Это правило продукта, а не тип значения.
FORBIDDEN_BRICK_CODES = frozenset({"фото"})


@dataclass(frozen=True)
class SystemBrickDefinition:
    """Неизменяемое описание системного кирпичика из миграции."""

    id: int
    code: str
    title: str
    value_type: str
    source: str
    rules: Mapping[str, object]


# Стабильные ID и коды. Миграция 6 вставляет ровно эти определения.
# Менять существующие ID/коды нельзя; новые системные кирпичики добавляются
# новой миграцией и новым ID.
SYSTEM_BRICKS: tuple[SystemBrickDefinition, ...] = (
    SystemBrickDefinition(
        1,
        "название",
        "Название мероприятия",
        BRICK_TYPE_SHORT_TEXT,
        BRICK_SOURCE_ASK,
        {},
    ),
    SystemBrickDefinition(
        2,
        "дата",
        "Дата",
        BRICK_TYPE_DATE,
        BRICK_SOURCE_ASK,
        {"horizon_months": 12},
    ),
    SystemBrickDefinition(
        3,
        "время_начала",
        "Время начала",
        BRICK_TYPE_TIME,
        BRICK_SOURCE_ASK,
        {},
    ),
    SystemBrickDefinition(
        4,
        "время_окончания",
        "Время окончания",
        BRICK_TYPE_TIME,
        BRICK_SOURCE_ASK,
        {},
    ),
    SystemBrickDefinition(
        5,
        "место",
        "Название места",
        BRICK_TYPE_SHORT_TEXT,
        BRICK_SOURCE_PLACE,
        {},
    ),
    SystemBrickDefinition(
        6,
        "адрес_места",
        "Адрес места",
        BRICK_TYPE_SHORT_TEXT,
        BRICK_SOURCE_PLACE,
        {},
    ),
    SystemBrickDefinition(
        7,
        "ссылка_места",
        "Ссылка места",
        BRICK_TYPE_URL,
        BRICK_SOURCE_PLACE,
        {},
    ),
    SystemBrickDefinition(
        8,
        "описание",
        "Описание",
        BRICK_TYPE_LONG_TEXT,
        BRICK_SOURCE_ASK,
        {},
    ),
    SystemBrickDefinition(
        9,
        "контакт",
        "Контакт",
        BRICK_TYPE_SHORT_TEXT,
        BRICK_SOURCE_ASK,
        {},
    ),
)

SYSTEM_BRICK_CODES = tuple(item.code for item in SYSTEM_BRICKS)
SYSTEM_BRICK_IDS = tuple(item.id for item in SYSTEM_BRICKS)
SYSTEM_BRICKS_BY_CODE = {item.code: item for item in SYSTEM_BRICKS}


# --- Технические коды и человекочитаемые названия --------------------------


def _require_text(value: object, *, what: str) -> str:
    if not isinstance(value, str):
        raise BrickValidationError(f"{what} должен быть строкой.")
    return value


def normalize_brick_code(raw: object) -> str:
    """
    Канонический технический код.

    NFKC, trim и casefold делают код регистронезависимым; разрешены буквы,
    цифры, `_` и `-`, минимум 2 и максимум 64 символа. Пробелы, скобки и
    управляющие символы запрещены. Ограничение длины — свойство токена
    `[код]`, а не пользовательского текста; содержимое кирпичика не
    ограничивается.
    """
    text = _require_text(raw, what="Технический код").strip()
    code = unicodedata.normalize("NFKC", text).casefold()
    if not (MIN_BRICK_CODE_LENGTH <= len(code) <= MAX_BRICK_CODE_LENGTH):
        raise BrickCodeError(
            "Технический код должен содержать от "
            f"{MIN_BRICK_CODE_LENGTH} до {MAX_BRICK_CODE_LENGTH} символов."
        )
    for char in code:
        if char in "_-" or char.isalnum():
            continue
        raise BrickCodeError(
            "Технический код может содержать только буквы, цифры, «_» и «-»."
        )
    if not any(char.isalnum() for char in code):
        raise BrickCodeError(
            "Технический код должен содержать хотя бы одну букву или цифру."
        )
    return code


def validate_brick_code(raw: object) -> str:
    """Синтаксис кода плюс продуктовый запрет кода `фото`."""
    code = normalize_brick_code(raw)
    if code in FORBIDDEN_BRICK_CODES:
        raise BrickCodeError(
            "Код «фото» зарезервирован: фотографии — вложения анонса, " "а не кирпичик."
        )
    return code


def clean_brick_title(raw: object) -> str:
    """
    Человекочитаемое название кирпичика.

    NFKC, trim и сжатие пробелов, но без молчаливого обрезания: длина не
    ограничивается. Одинаковые названия разрешены — таблица не имеет
    UNIQUE по `title`; однозначность будущих шаблонов обеспечивает `code`.
    """
    text = _require_text(raw, what="Название кирпичика")
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    if not normalized:
        raise BrickValidationError("Название кирпичика не может быть пустым.")
    return normalized


def ensure_brick_type(value_type: object) -> str:
    """Проверяет, что тип входит в закрытый каталог Stage 22A."""
    if value_type not in BRICK_TYPES:
        raise BrickValidationError(
            f"Неизвестный тип кирпичика: {value_type!r}. "
            f"Допустимо: {', '.join(BRICK_TYPES)}."
        )
    return str(value_type)


def ensure_brick_source(source: object) -> str:
    """Источник значения — отдельное от scope понятие."""
    if source not in BRICK_SOURCES:
        raise BrickValidationError(
            f"Неизвестный источник значения: {source!r}. "
            f"Допустимо: {', '.join(BRICK_SOURCES)}."
        )
    return str(source)


# --- Правила типов (rules_json) --------------------------------------------


def _reject_unknown_keys(
    rules: Mapping[str, object], allowed: set[str], *, value_type: str
) -> None:
    unknown = sorted(set(rules) - allowed)
    if unknown:
        raise BrickRulesError(
            f"Для типа {value_type} неизвестные ключи правил: " f"{', '.join(unknown)}."
        )


def _int_rule(value: object, *, key: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BrickRulesError(f"Правило {key} должно быть целым числом.")
    if minimum is not None and value < minimum:
        raise BrickRulesError(f"Правило {key} должно быть не меньше {minimum}.")
    return value


def _number_rule(value: object, *, key: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrickRulesError(f"Правило {key} должно быть числом.")
    if isinstance(value, float) and not math.isfinite(value):
        raise BrickRulesError(f"Правило {key} должно быть конечным числом.")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _absolute_date_rule(value: object, *, key: str) -> str:
    if not isinstance(value, str):
        raise BrickRulesError(f"Правило {key} должно быть датой ДД.ММ.ГГГГ.")
    cleaned = value.strip()
    try:
        parsed = datetime.strptime(cleaned, "%d.%m.%Y").date()
    except ValueError as exc:
        raise BrickRulesError(
            f"Правило {key} должно быть существующей датой ДД.ММ.ГГГГ."
        ) from exc
    return parsed.strftime("%d.%m.%Y")


def validate_rules(
    value_type: str, rules: Mapping[str, object] | None
) -> dict[str, object]:
    """
    Строгая проверка декларативных правил и их канонизация.

    Неизвестные ключи отклоняются: опечатка в правиле не должна молча
    отключать ограничение. Пользовательские правила не исполняются.
    """
    ensure_brick_type(value_type)
    source = {} if rules is None else rules
    if not isinstance(source, Mapping):
        raise BrickRulesError("Правила кирпичика должны быть JSON-объектом.")
    for key in source:
        if not isinstance(key, str):
            raise BrickRulesError("Ключи правил должны быть строками.")

    if value_type in (
        BRICK_TYPE_SHORT_TEXT,
        BRICK_TYPE_LONG_TEXT,
        BRICK_TYPE_TIME,
        BRICK_TYPE_URL,
    ):
        _reject_unknown_keys(source, set(), value_type=value_type)
        return {}

    if value_type == BRICK_TYPE_DATE:
        _reject_unknown_keys(
            source, {"horizon_months", "min_date", "max_date"}, value_type=value_type
        )
        result: dict[str, object] = {}
        if "horizon_months" in source:
            result["horizon_months"] = _int_rule(
                source["horizon_months"], key="horizon_months", minimum=0
            )
        for key in ("min_date", "max_date"):
            if key in source:
                result[key] = _absolute_date_rule(source[key], key=key)
        if "min_date" in result and "max_date" in result:
            earliest = datetime.strptime(str(result["min_date"]), "%d.%m.%Y").date()
            latest = datetime.strptime(str(result["max_date"]), "%d.%m.%Y").date()
            if earliest > latest:
                raise BrickRulesError("min_date не может быть позже max_date.")
        return result

    if value_type == BRICK_TYPE_NUMBER:
        _reject_unknown_keys(
            source, {"min", "max", "integer", "suffix"}, value_type=value_type
        )
        result = {}
        for key in ("min", "max"):
            if key in source:
                result[key] = _number_rule(source[key], key=key)
        if "min" in result and "max" in result:
            if Decimal(str(result["min"])) > Decimal(str(result["max"])):
                raise BrickRulesError("min не может быть больше max.")
        if "integer" in source:
            if not isinstance(source["integer"], bool):
                raise BrickRulesError("Правило integer должно быть true/false.")
            result["integer"] = source["integer"]
        if "suffix" in source:
            if not isinstance(source["suffix"], str):
                raise BrickRulesError("Правило suffix должно быть строкой.")
            result["suffix"] = source["suffix"]
        return result

    # choice
    _reject_unknown_keys(source, {"choices", "multiple"}, value_type=value_type)
    if "choices" not in source:
        raise BrickRulesError("Для типа choice обязателен список choices.")
    raw_choices = source["choices"]
    if (
        not isinstance(raw_choices, Sequence)
        or isinstance(raw_choices, (str, bytes, bytearray))
        or not raw_choices
    ):
        raise BrickRulesError("choices должен быть непустым списком строк.")
    choices: list[str] = []
    for choice in raw_choices:
        if not isinstance(choice, str):
            raise BrickRulesError("Каждый вариант choices должен быть строкой.")
        cleaned = choice.strip()
        if not cleaned:
            raise BrickRulesError("Вариант choices не может быть пустым.")
        if cleaned in choices:
            raise BrickRulesError("Варианты choices не должны повторяться.")
        choices.append(cleaned)
    result = {"choices": choices}
    if "multiple" in source:
        if not isinstance(source["multiple"], bool):
            raise BrickRulesError("Правило multiple должно быть true/false.")
        result["multiple"] = source["multiple"]
    return result


def parse_rules(
    value_type: str, rules: Mapping[str, object] | str | None
) -> dict[str, object]:
    """Разбирает JSON-строку/словарь и валидирует правила без исполнения кода."""
    if rules is None:
        return validate_rules(value_type, None)
    if isinstance(rules, str):
        try:
            decoded = json.loads(rules)
        except (TypeError, ValueError) as exc:
            raise BrickRulesError(
                "Правила кирпичика содержат некорректный JSON."
            ) from exc
        if not isinstance(decoded, dict):
            raise BrickRulesError("Правила кирпичика должны быть JSON-объектом.")
        return validate_rules(value_type, decoded)
    if not isinstance(rules, Mapping):
        raise BrickRulesError("Правила кирпичика должны быть JSON-объектом.")
    return validate_rules(value_type, rules)


def rules_to_json(
    value_type: str, rules: Mapping[str, object] | str | None
) -> str | None:
    """Канонический JSON или NULL для пустых правил."""
    validated = parse_rules(value_type, rules)
    if not validated:
        return None
    return json.dumps(validated, ensure_ascii=False, sort_keys=True, allow_nan=False)


def rules_from_json(value_type: str, raw: str | None) -> dict[str, object]:
    """Читает правила из БД; повреждённое значение — явная ошибка."""
    if raw is None:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise BrickRulesError(
            "Правила кирпичика в базе содержат некорректный JSON."
        ) from exc
    if not isinstance(decoded, dict):
        raise BrickRulesError("Правила кирпичика в базе должны быть объектом.")
    return validate_rules(value_type, decoded)


# --- Результат проверки значения --------------------------------------------


class BrickValueStatus(StrEnum):
    """Пять различимых состояний значения, нужных будущему мастеру."""

    MISSING = "missing"
    EMPTY_OPTIONAL = "empty_optional"
    VALID = "valid"
    INVALID = "invalid"
    NEEDS_INPUT = "needs_input"


@dataclass(frozen=True)
class BrickValueResult:
    """Нормализованное значение либо диагностируемая причина отказа."""

    status: BrickValueStatus
    payload: dict[str, object] | None = None
    error: str | None = None

    @property
    def is_valid(self) -> bool:
        return self.status is BrickValueStatus.VALID

    @property
    def is_empty_optional(self) -> bool:
        return self.status is BrickValueStatus.EMPTY_OPTIONAL


def _invalid(message: str) -> BrickValueResult:
    return BrickValueResult(BrickValueStatus.INVALID, error=message)


def _is_empty_value(raw: object) -> bool:
    if raw is None:
        return True
    if isinstance(raw, str):
        return not raw.strip()
    if isinstance(raw, (list, tuple)):
        return len(raw) == 0
    return False


def _decimal_from_raw(raw: object) -> Decimal | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        if not math.isfinite(raw):
            return None
        return Decimal(str(raw))
    if isinstance(raw, Decimal):
        return raw if raw.is_finite() else None
    if isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() else None
    return None


def _decimal_to_number(value: Decimal) -> int | float | None:
    if value == value.to_integral_value():
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _date_bounds(
    rules: Mapping[str, object], today: date
) -> tuple[date | None, date | None]:
    earliest: date | None = None
    latest: date | None = None
    if "horizon_months" in rules:
        months = rules["horizon_months"]
        if isinstance(months, bool) or not isinstance(months, int):
            raise BrickRulesError("Правило horizon_months должно быть целым числом.")
        earliest = today
        latest = add_months(today, months)
    if "min_date" in rules:
        candidate = datetime.strptime(str(rules["min_date"]), "%d.%m.%Y").date()
        earliest = candidate if earliest is None else max(earliest, candidate)
    if "max_date" in rules:
        candidate = datetime.strptime(str(rules["max_date"]), "%d.%m.%Y").date()
        latest = candidate if latest is None else min(latest, candidate)
    return earliest, latest


def validate_brick_value(
    value_type: str,
    raw: object,
    *,
    rules: Mapping[str, object] | str | None = None,
    allow_empty: bool = False,
    today: date | None = None,
) -> BrickValueResult:
    """
    Проверяет и нормализует значение без Telegram API.

    `allow_empty=True` разрешает только явно пустое необязательное значение;
    непустые значения сохраняются без обрезания. Пустая строка/None при
    `allow_empty=False` — диагностируемая ошибка, а не молчаливое превращение
    в NULL.
    """
    ensure_brick_type(value_type)
    validated_rules = parse_rules(value_type, rules)

    if _is_empty_value(raw):
        if allow_empty:
            return BrickValueResult(BrickValueStatus.EMPTY_OPTIONAL)
        return _invalid("Значение не может быть пустым.")

    if value_type in (BRICK_TYPE_SHORT_TEXT, BRICK_TYPE_LONG_TEXT):
        if not isinstance(raw, str):
            return _invalid("Ожидается текст.")
        return BrickValueResult(BrickValueStatus.VALID, payload={"text": raw})

    if value_type == BRICK_TYPE_DATE:
        if not isinstance(raw, str):
            return _invalid("Ожидается дата текстом.")
        current = today if today is not None else app_today()
        normalized = parse_date_flexible(raw, today=current)
        if normalized is None:
            return _invalid("Не удалось распознать дату.")
        parsed = datetime.strptime(normalized, "%d.%m.%Y").date()
        earliest, latest = _date_bounds(validated_rules, current)
        if earliest is not None and parsed < earliest:
            return _invalid(
                f"Дата должна быть не раньше {earliest.strftime('%d.%m.%Y')}."
            )
        if latest is not None and parsed > latest:
            return _invalid(f"Дата должна быть не позже {latest.strftime('%d.%m.%Y')}.")
        return BrickValueResult(BrickValueStatus.VALID, payload={"date": normalized})

    if value_type == BRICK_TYPE_TIME:
        if not isinstance(raw, str):
            return _invalid("Ожидается время текстом.")
        normalized = normalize_time(raw)
        if normalized is None:
            return _invalid("Не удалось распознать время.")
        return BrickValueResult(BrickValueStatus.VALID, payload={"time": normalized})

    if value_type == BRICK_TYPE_URL:
        if not isinstance(raw, str):
            return _invalid("Ожидается ссылка текстом.")
        normalized = normalize_url(raw)
        if normalized is None:
            return _invalid(
                "Это не корректная http(s)-ссылка. Опасные схемы и "
                "произвольный текст запрещены."
            )
        return BrickValueResult(BrickValueStatus.VALID, payload={"url": normalized})

    if value_type == BRICK_TYPE_NUMBER:
        number = _decimal_from_raw(raw)
        if number is None:
            return _invalid("Ожидается число.")
        if validated_rules.get("integer") and number != number.to_integral_value():
            return _invalid("Ожидается целое число.")
        if "min" in validated_rules:
            minimum = Decimal(str(validated_rules["min"]))
            if number < minimum:
                return _invalid(f"Число должно быть не меньше {minimum}.")
        if "max" in validated_rules:
            maximum = Decimal(str(validated_rules["max"]))
            if number > maximum:
                return _invalid(f"Число должно быть не больше {maximum}.")
        normalized_number = _decimal_to_number(number)
        if normalized_number is None:
            return _invalid("Число слишком велико для хранения.")
        return BrickValueResult(
            BrickValueStatus.VALID, payload={"number": normalized_number}
        )

    # choice — варианты заданы в rules_json; отдельной системы опросов нет.
    raw_choices = validated_rules["choices"]
    if not isinstance(raw_choices, list):
        raise BrickRulesError("Правило choices должно быть списком.")
    choices = [str(item) for item in raw_choices]
    multiple = bool(validated_rules.get("multiple", False))
    if multiple:
        if isinstance(raw, str):
            selected = [raw]
        elif isinstance(raw, (list, tuple)):
            selected = list(raw)
        else:
            return _invalid("Ожидается один или несколько вариантов выбора.")
        if not all(isinstance(item, str) for item in selected):
            return _invalid("Каждый выбранный вариант должен быть строкой.")
        if len(set(selected)) != len(selected):
            return _invalid("Один и тот же вариант выбран несколько раз.")
        if any(item not in choices for item in selected):
            return _invalid("Выбран вариант, которого нет в списке допустимых.")
        canonical = [item for item in choices if item in selected]
        return BrickValueResult(BrickValueStatus.VALID, payload={"choices": canonical})

    if not isinstance(raw, str):
        return _invalid("Ожидается один вариант выбора.")
    if raw not in choices:
        return _invalid("Выбран вариант, которого нет в списке допустимых.")
    return BrickValueResult(BrickValueStatus.VALID, payload={"choice": raw})


def value_payload_from_text(
    value_type: str,
    text: str | None,
    *,
    rules: Mapping[str, object] | str | None = None,
    allow_empty: bool = False,
    today: date | None = None,
) -> BrickValueResult:
    """Удобная обёртка для будущего адаптивного ввода (без Telegram)."""
    return validate_brick_value(
        value_type,
        text,
        rules=rules,
        allow_empty=allow_empty,
        today=today,
    )
