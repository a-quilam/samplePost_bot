# template_types.py
"""
Чистые контракты шаблонов анонсов Stage 23.

Здесь нет SQLAlchemy и Telegram: только закрытый каталог видов шаблонов,
три текстовые секции, контролируемое встроенное содержимое Poster и
стабильные ID системных кирпичиков, на которые оно ссылается.

Границы Stage 23:

- шаблон хранит авторский текст без разбора и нормализации;
- `[код]` пока остаётся частью текста, а не исполняемым выражением;
- синтаксис ссылок и подстановка кирпичиков появятся в Stage 24;
- встроенный стандарт имеет собственную версию, чтобы будущий сброс не
  зависел от случайного изменения констант.
"""

from __future__ import annotations

from dataclasses import dataclass

from brick_types import SYSTEM_BRICKS_BY_CODE

TEMPLATE_KIND_STANDARD = "standard"
TEMPLATE_KIND_PERSONAL = "personal"
TEMPLATE_KINDS = (TEMPLATE_KIND_STANDARD, TEMPLATE_KIND_PERSONAL)

TEMPLATE_SECTION_HEADER = "header"
TEMPLATE_SECTION_BODY = "body"
TEMPLATE_SECTION_FOOTER = "footer"
TEMPLATE_SECTIONS = (
    TEMPLATE_SECTION_HEADER,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
)

STANDARD_TEMPLATE_DISPLAY_NAME = "Стандартный шаблон места"

# Имя первого личного шаблона пользователя (Poster 2.0). Отдельная константа,
# чтобы UI и сервис не расходились в тексте.
INITIAL_PERSONAL_TEMPLATE_NAME = "Мой шаблон"

# Контролируемая версия встроенного стандарта. Меняется только вместе с
# осознанным изменением содержимого и документацией; существующие шаблоны
# при обновлении приложения автоматически не перезаписываются.
#
# Версия 2 (Poster 2.0 S1) — точный эталон владельца (§6.1.1 спецификации):
# жирный заголовок, `[дата] // [время_начала]` в одну строку, место
# гиперссылкой на `[ссылка_места]`, отдельная нежирная строка `[адрес]`,
# тело `[описание]`, подвал `[контакт]`. Эмодзи, время окончания и
# `адрес_места` в стандарт не входят — это отдельные кирпичики.
BUILTIN_TEMPLATE_VERSION = 2

# Синтаксис `[место]([ссылка_места])` — поддерживаемая парсером условная
# гиперссылка: без URL остаётся только подпись, без пустых скобок.
BUILTIN_TEMPLATE_HEADER = "**[название]**"
BUILTIN_TEMPLATE_BODY = (
    "[дата] // [время_начала]\n"
    "[место]([ссылка_места])\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
BUILTIN_TEMPLATE_FOOTER = "[контакт]"


@dataclass(frozen=True)
class BuiltinBrickBinding:
    """Постоянная связь встроенного текста с системным brick_id."""

    brick_id: int
    code: str


# Явные пары «ID → код», а не результат поиска по имени во время выполнения.
# Это те же стабильные определения, которые вставлены миграциями 6 и 15.
BUILTIN_TEMPLATE_BINDINGS: tuple[BuiltinBrickBinding, ...] = (
    BuiltinBrickBinding(1, "название"),
    BuiltinBrickBinding(2, "дата"),
    BuiltinBrickBinding(3, "время_начала"),
    BuiltinBrickBinding(5, "место"),
    BuiltinBrickBinding(7, "ссылка_места"),
    BuiltinBrickBinding(10, "адрес"),
    BuiltinBrickBinding(8, "описание"),
    BuiltinBrickBinding(9, "контакт"),
)
BUILTIN_TEMPLATE_BRICK_IDS: tuple[int, ...] = tuple(
    binding.brick_id for binding in BUILTIN_TEMPLATE_BINDINGS
)


@dataclass(frozen=True)
class BuiltinTemplateDefinition:
    """Неизменяемый встроенный стандарт и его контролируемая версия."""

    version: int
    header: str
    body: str
    footer: str
    bindings: tuple[BuiltinBrickBinding, ...] = BUILTIN_TEMPLATE_BINDINGS

    @property
    def brick_ids(self) -> tuple[int, ...]:
        return tuple(binding.brick_id for binding in self.bindings)


BUILTIN_TEMPLATE = BuiltinTemplateDefinition(
    version=BUILTIN_TEMPLATE_VERSION,
    header=BUILTIN_TEMPLATE_HEADER,
    body=BUILTIN_TEMPLATE_BODY,
    footer=BUILTIN_TEMPLATE_FOOTER,
)


class TemplateValidationError(ValueError):
    """Некорректное название или текстовые секции шаблона."""


def _validate_catalog() -> None:
    """
    Проверяет, что пары встроенного стандарта совпадают с каталогом Stage 22A.

    Это не разбор пользовательского текста: только защита от случайного
    рассинхронизирования стабильных ID и кодов при правке кода.
    """
    for binding in BUILTIN_TEMPLATE_BINDINGS:
        definition = SYSTEM_BRICKS_BY_CODE.get(binding.code)
        if definition is None or definition.id != binding.brick_id:
            raise RuntimeError(
                "Встроенный шаблон ссылается на неизвестный системный "
                f"кирпичик: {binding.brick_id}:{binding.code!r}."
            )


_validate_catalog()


def validate_template_name(name: object) -> str:
    """
    Название личного шаблона.

    Значимые символы не переписываются и не обрезаются; проверяется только
    тип и наличие непустого человекочитаемого содержимого. Уникальность
    названий не требуется: разные пользователи и один владелец могут иметь
    несколько шаблонов с одинаковым названием.
    """
    if not isinstance(name, str):
        raise TemplateValidationError("Название шаблона должно быть строкой.")
    if not name.strip():
        raise TemplateValidationError("Название шаблона не должно быть пустым.")
    return name


def validate_template_section_name(section: object) -> str:
    """Имя одной из трёх независимо изменяемых секций."""
    if section not in TEMPLATE_SECTIONS:
        raise TemplateValidationError(
            "Секция должна быть одной из: header, body, footer."
        )
    return str(section)


def validate_template_sections(
    header: object,
    body: object,
    footer: object,
) -> tuple[str, str, str]:
    """
    Проверяет три секции, сохраняя их символы один в один.

    Пустая отдельная секция допустима; недопустим только шаблон, в котором
    все три секции состоят из одних пробельных символов. `.strip()` здесь
    используется ТОЛЬКО для проверки и никогда не меняет сохраняемое значение.
    """
    values = (header, body, footer)
    if any(not isinstance(value, str) for value in values):
        raise TemplateValidationError("Секции шаблона должны быть строками.")
    checked = (str(header), str(body), str(footer))
    if not any(value.strip() for value in checked):
        raise TemplateValidationError(
            "Шаблон не должен состоять только из пустых секций."
        )
    return checked
