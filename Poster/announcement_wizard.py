# announcement_wizard.py
"""
Доменная логика адаптивного мастера анонсов Stage 27A.

Модуль не зависит от Telegram и не пишет в БД. Он отвечает только за:

- представление независимого снимка шаблона (`content` и, если есть,
  `extended_content` + сохранённые привязки `код → brick_id`);
- восстановление `brick_id` из снимка без повторного разрешения `[код]`;
- вычисление детерминированной последовательности недостающих вопросов;
- проверку значений через валидаторы Stage 22A и правила даты/времени
  Stage 17;
- контекст выбранного места (`название`, `адрес`, `ссылка`).

Хранение, права, транзакции и optimistic locking — в `announcement_store.py`.
Границы Stage 27A: Telegram-интерфейс нового мастера, публикация и
перенос фотографий в новую модель не реализуются.
"""

from __future__ import annotations

import html as html_module
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from brick_store import BrickDefinition
from brick_types import (
    BRICK_SCOPE_PERSONAL,
    BRICK_SCOPE_PLACE,
    BRICK_SCOPE_SYSTEM,
    SYSTEM_BRICKS_BY_CODE,
    BrickValueResult,
    BrickValueStatus,
    validate_brick_value,
)
from template_parser import (
    BrickNode,
    FormatNode,
    LinkNode,
    TemplateParseError,
    parse_template,
)
from template_types import (
    TEMPLATE_CONTENT,
    TEMPLATE_EXTENDED_CONTENT,
    TEMPLATE_TEXTS,
)

# Порядок текстов шаблона: вопросы строятся в порядке первого появления
# кирпичика, сначала в каноническом тексте, затем в расширенном.
TEXT_ORDER = TEMPLATE_TEXTS

# Постоянные ID системных кирпичиков каталога (миграции 6 и 15). Контекст
# места строится по ID, а не по повторному разрешению текстовых кодов. ID
# берутся из каталога, чтобы не дублировать магические числа.
PLACE_BRICK_ID_NAME = SYSTEM_BRICKS_BY_CODE["место"].id
PLACE_BRICK_ID_ADDRESS = SYSTEM_BRICKS_BY_CODE["адрес_места"].id
PLACE_BRICK_ID_URL = SYSTEM_BRICKS_BY_CODE["ссылка_места"].id
# Poster 2.0 S1: пользовательский код эталона `[адрес]`. Старый
# `адрес_места` остаётся для уже сохранённых снимков; автоподстановка
# выполняется в оба кирпичика.
ADDRESS_BRICK_ID = SYSTEM_BRICKS_BY_CODE["адрес"].id


class WizardStage(StrEnum):
    """Текущий этап незавершённого анонса."""

    PLACE = "place"
    TEMPLATE = "template"
    QUESTIONS = "questions"
    EXTRAS = "extras"
    PREVIEW = "preview"
    COMPLETED = "completed"


WIZARD_STAGES = tuple(stage.value for stage in WizardStage)


class AnnouncementError(ValueError):
    """Базовая доменная ошибка адаптивного мастера."""


class AnnouncementActorError(RuntimeError):
    """Telegram-пользователь ещё не зарегистрирован в `users`."""


class AnnouncementNotFoundError(AnnouncementError):
    """Черновик не найден или принадлежит другому автору."""


class AnnouncementConflictError(AnnouncementError):
    """Конфликт ревизии, идемпотентности или одновременной записи."""


class AnnouncementPublicationError(AnnouncementError):
    """Публикация уже начата/завершена или её состояние не позволяет шаг."""


class AnnouncementStageError(AnnouncementError):
    """Действие недопустимо на текущем этапе мастера."""


class AnnouncementBrickError(AnnouncementError):
    """Кирпичик недоступен, архивный или несовместим с этим анонсом."""


class AnnouncementValueError(AnnouncementError):
    """Значение не прошло проверку типа/правил."""


class AnnouncementSnapshotError(AnnouncementError):
    """Снимок шаблона повреждён или не содержит нужной привязки."""


@dataclass(frozen=True)
class SnapshotBinding:
    """Сохранённая в снимке связь авторского кода с постоянным brick_id."""

    code: str
    brick_id: int


@dataclass(frozen=True)
class AnnouncementTemplateSnapshot:
    """Независимый снимок версии шаблона для конкретного анонса."""

    snapshot_id: int
    source_template_id: int
    source_template_version: int
    source_kind: str
    source_place_id: int | None
    texts: Mapping[str, str]
    bindings: tuple[SnapshotBinding, ...] = field(default_factory=tuple)
    source_name: str | None = None

    @property
    def brick_ids(self) -> tuple[int, ...]:
        """Уникальные brick_id в порядке первого появления в текстах."""
        ids: list[int] = []
        for node in iter_snapshot_bricks(self):
            if node.brick_id not in ids:
                assert node.brick_id is not None
                ids.append(node.brick_id)
        return tuple(ids)


@dataclass(frozen=True)
class Question:
    """
    Один недостающий кирпичик шаблона.

    Все вопросы необязательны: мастер может пропустить значение, но не может
    записать некорректное. `context_error` объясняет, почему известное
    значение справочника не было принято (например, битая ссылка).
    """

    brick_id: int
    code: str
    title: str
    value_type: str
    rules: Mapping[str, object]
    source: str
    required: bool = False
    allow_empty: bool = True
    context_error: str | None = None


@dataclass(frozen=True)
class AnnouncementReadiness:
    """
    Итог проверки собранного текста для следующего этапа.

    Пропуск отдельного кирпичика разрешён, но пустой текст или шаблон,
    в котором ни одно поле так и не получило значения, готовым анонсом
    не считается. Публикация в Stage 27B не выполняется: решение лишь
    показывает, можно ли передать текст дальше.
    """

    ready: bool
    visible_text: str
    issues: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


_HTML_TAG_RE = re.compile(r"<[^>]*>")


def visible_announcement_text(rendered_html: str) -> str:
    """Текст без HTML-разметки для проверки готовности и служебной сводки."""
    without_tags = _HTML_TAG_RE.sub("", rendered_html or "")
    return html_module.unescape(without_tags).strip()


def evaluate_announcement_readiness(
    rendered_html: str,
    *,
    snapshot_brick_count: int,
    filled_value_count: int,
) -> AnnouncementReadiness:
    """
    Проверяет, можно ли передать собранный текст дальше.

    Poster 2.0 S3 / §5.6 (решение владельца): **полностью пустой анонс
    готов**. Пропустить можно каждый кирпичик, и это не блокирует переход
    к следующему шагу: техническая заглушка `.` подставляется на этапе
    сборки сообщения, а не здесь. Поэтому `issues` пуст и для пустого
    текста, а `visible_text` остаётся пустым — по нему вызывающий слой
    решает про `.` и про шутку в служебном экране.

    Оба параметра сохранены в подписи: ими пользуется сводка обзора
    (сколько полей заполнено) и внешние вызывающие; готовность от них
    больше не зависит.
    """
    visible = visible_announcement_text(rendered_html)
    return AnnouncementReadiness(
        ready=True,
        visible_text=visible,
        issues=(),
        warnings=(),
    )


def place_values_by_code(
    *,
    display_name: str | None,
    address: str | None,
    url: str | None,
    city: str | None = None,
) -> dict[str, object]:
    """
    Известные значения места по **кодам** системных кирпичиков.

    Коды, а не ID: в уже существующей базе реальный `bricks.id` кирпичика
    `[адрес]` зависит от того, какие пользовательские кирпичики были созданы
    раньше (миграция 15 берёт `MAX(id)+1`). Поэтому сопоставление
    «код → brick_id» всегда берётся из снимка шаблона, а не из каталога.

    Адрес подставляется в оба кода — новый `[адрес]` (Poster 2.0) и старый
    `[адрес_места]`, чтобы сохранённые снимки продолжали работать. Город
    (U1a) — отдельный код `[город]`, в стандартный шаблон не входит.
    """
    values: dict[str, object] = {}
    if display_name is not None and display_name.strip():
        values["место"] = display_name
    if address is not None and address.strip():
        values["адрес"] = address
        values["адрес_места"] = address
    if url is not None and url.strip():
        values["ссылка_места"] = url
    if city is not None and city.strip():
        values["город"] = city
    return values


def place_context_values(
    *,
    display_name: str | None,
    address: str | None,
    url: str | None,
    city: str | None = None,
    brick_ids_by_code: Mapping[str, int] | None = None,
) -> dict[int, object]:
    """
    Известные значения места по brick_id.

    Отсутствующий адрес/ссылка/город не превращаются в пустую строку или
    заглушку: такого ключа просто нет, и мастер задаст вопрос только при
    необходимости.

    `brick_ids_by_code` — реальные ID из снимка/определений. Без него берутся
    канонические ID каталога; это годится только для чистой базы, где
    миграции создали кирпичики подряд, поэтому рабочий слой всегда передаёт
    явное сопоставление.
    """
    known = place_values_by_code(
        display_name=display_name, address=address, url=url, city=city
    )
    mapping: Mapping[str, int]
    if brick_ids_by_code is None:
        mapping = {
            code: SYSTEM_BRICKS_BY_CODE[code].id
            for code in known
            if code in SYSTEM_BRICKS_BY_CODE
        }
    else:
        mapping = brick_ids_by_code
    values: dict[int, object] = {}
    for code, raw in known.items():
        brick_id = mapping.get(code)
        if brick_id is not None:
            values[brick_id] = raw
    return values


def snapshot_brick_ids_by_code(
    snapshot: AnnouncementTemplateSnapshot,
) -> dict[str, int]:
    """
    Реальные brick_id снимка по коду кирпичика.

    Нужен автоподстановке: в существующей базе `bricks.id` системного
    `[адрес]` зависит от порядка миграций и пользовательских кирпичиков,
    поэтому каталог даёт только код, а конкретный ID — снимок.
    """
    return _bindings_by_code(snapshot)


def _bindings_by_code(
    snapshot: AnnouncementTemplateSnapshot,
) -> dict[str, int]:
    bindings: dict[str, int] = {}
    for binding in snapshot.bindings:
        if binding.code in bindings and bindings[binding.code] != binding.brick_id:
            raise AnnouncementSnapshotError(
                f"Код [{binding.code}] имеет несколько разных привязок в снимке."
            )
        bindings[binding.code] = binding.brick_id
    return bindings


def _iter_nodes(nodes: Iterable[Any]) -> Iterable[Any]:
    for node in nodes:
        if isinstance(node, BrickNode):
            yield node
        elif isinstance(node, LinkNode):
            yield from _iter_nodes(node.label)
            if isinstance(node.url, BrickNode):
                yield node.url
        elif isinstance(node, FormatNode):
            yield from _iter_nodes(node.children)
        elif hasattr(node, "children"):
            yield from _iter_nodes(getattr(node, "children"))


def _parse_snapshot_text(
    snapshot: AnnouncementTemplateSnapshot,
    text_name: str,
    bindings: Mapping[str, int],
) -> list[Any] | None:
    """
    Разбирает один текст снимка и проставляет brick_id во всех токенах.

    Привязки берутся только из снимка: код не разрешается заново.
    """
    raw = snapshot.texts.get(text_name)
    if raw is None:
        return None
    try:
        tokens = parse_template(raw)
    except TemplateParseError as exc:
        raise AnnouncementSnapshotError(
            f"Текст «{text_name}» снимка не разбирается: {exc}"
        ) from exc
    for node in _iter_nodes(tokens):
        if node.code not in bindings:
            raise AnnouncementSnapshotError(
                f"У токена [{node.code}] нет сохранённой привязки в снимке."
            )
        node.brick_id = bindings[node.code]
    return tokens


def iter_snapshot_bricks(
    snapshot: AnnouncementTemplateSnapshot,
    *,
    text_name: str | None = None,
) -> Iterable[BrickNode]:
    """
    Обходит тексты снимка в порядке `content` → `extended_content`.

    `brick_id` восстанавливается только из сохранённых привязок снимка.
    Текстовый код не разрешается заново и не может подменить определение.

    `text_name` ограничивает обход одним текстом: конструктор вопросов
    спрашивает только `content` (Poster 2.3.1, E1).
    """
    bindings = _bindings_by_code(snapshot)
    text_names = TEXT_ORDER if text_name is None else (text_name,)
    for name in text_names:
        tokens = _parse_snapshot_text(snapshot, name, bindings)
        if tokens is None:
            continue
        yield from _iter_nodes(tokens)


def snapshot_tokens_by_text(
    snapshot: AnnouncementTemplateSnapshot,
) -> dict[str, list[Any]]:
    """
    Разбирает снимок и проставляет brick_id во всех токенах.

    Используется сервисом для передачи компилятору Stage 24. Повторного
    разрешения кодов по названию здесь нет.
    """
    bindings = _bindings_by_code(snapshot)
    result: dict[str, list[Any]] = {}
    for text_name in TEXT_ORDER:
        tokens = _parse_snapshot_text(snapshot, text_name, bindings)
        if tokens is not None:
            result[text_name] = tokens
    return result


def snapshot_brick_ids_in_text(
    snapshot: AnnouncementTemplateSnapshot,
    text_name: str,
) -> tuple[int, ...]:
    """
    Уникальные brick_id одного текста снимка в порядке первого появления.

    Используется правилом доп. кирпичиков (Poster 2.3.1, E1): список
    кандидатов считается по расширенному тексту за вычетом канонического.
    """
    ids: list[int] = []
    for node in iter_snapshot_bricks(snapshot, text_name=text_name):
        assert node.brick_id is not None
        if node.brick_id not in ids:
            ids.append(node.brick_id)
    return tuple(ids)


def snapshot_has_extended_content(snapshot: AnnouncementTemplateSnapshot) -> bool:
    """Есть ли у снимка расширенный текст (иначе доп. поля недоступны)."""
    raw = snapshot.texts.get(TEMPLATE_EXTENDED_CONTENT)
    return isinstance(raw, str) and bool(raw.strip())


def extra_candidate_brick_ids(
    snapshot: AnnouncementTemplateSnapshot,
) -> tuple[int, ...]:
    """
    Кирпичики, которые можно добавить как доп. поле этого анонса.

    Правило Poster 2.3.1 (E1): brick_id расширенного текста снимка минус
    brick_id канонического текста, в порядке первого появления в
    расширенном. Выбранные extras и доступность определений проверяет
    вызывающий слой. Без `extended_content` кандидатов нет: доп. поле
    некуда поставить в тексте.
    """
    if not snapshot_has_extended_content(snapshot):
        return ()
    content_ids = set(snapshot_brick_ids_in_text(snapshot, TEMPLATE_CONTENT))
    return tuple(
        brick_id
        for brick_id in snapshot_brick_ids_in_text(snapshot, TEMPLATE_EXTENDED_CONTENT)
        if brick_id not in content_ids
    )


def _validate_context_value(
    definition: BrickDefinition,
    raw: object,
    *,
    today: date | None,
) -> BrickValueResult:
    return validate_brick_value(
        definition.value_type,
        raw,
        rules=definition.rules,
        allow_empty=True,
        today=today,
    )


def build_questions(
    snapshot: AnnouncementTemplateSnapshot,
    definitions: Mapping[int, BrickDefinition],
    *,
    place_values: Mapping[int, object] | None = None,
    context_values: Mapping[int, object] | None = None,
    answers: Mapping[int, BrickValueResult] | None = None,
    today: date | None = None,
) -> list[Question]:
    """
    Строит детерминированный список недостающих вопросов.

    - вопросы задаются ТОЛЬКО по каноническому `content` снимка
      (Poster 2.3.1, E1): доп. поля из `extended_content` спрашиваются
      не в конструкторе, а позже — после выбора кирпичика;
    - порядок — первое появление кирпичика в `content`;
    - повторяющийся `brick_id` встречается в списке один раз;
    - кирпичики вне снимка игнорируются, даже если переданы в context/answers;
    - известное и корректное значение места/контекста не спрашивается;
    - отсутствие строки и допустимое пустое значение различимы через
      `BrickValueResult`/`BrickValueStatus`;
    - архивное/отсутствующее определение — диагностируемая ошибка.
    """
    known: dict[int, object] = {}
    if context_values:
        known.update(context_values)
    if place_values:
        known.update(place_values)

    answered = answers or {}
    seen: set[int] = set()
    questions: list[Question] = []

    for node in iter_snapshot_bricks(snapshot, text_name=TEMPLATE_CONTENT):
        assert node.brick_id is not None
        brick_id = node.brick_id
        if brick_id in seen:
            continue
        seen.add(brick_id)

        definition = definitions.get(brick_id)
        if definition is None:
            raise AnnouncementBrickError(
                f"Определение кирпичика #{brick_id} из снимка недоступно."
            )
        if not definition.is_active:
            raise AnnouncementBrickError(
                f"Кирпичик #{brick_id} «{definition.code}» находится в архиве."
            )
        if definition.scope == BRICK_SCOPE_PLACE and definition.place_id is None:
            raise AnnouncementBrickError(
                f"Кирпичик #{brick_id} имеет некорректную область места."
            )

        answer = answered.get(brick_id)
        if answer is not None:
            if answer.status is BrickValueStatus.VALID:
                continue
            if answer.status is BrickValueStatus.EMPTY_OPTIONAL:
                continue
            if answer.status is BrickValueStatus.INVALID:
                raise AnnouncementValueError(
                    f"Сохранённое значение кирпичика #{brick_id} некорректно: "
                    f"{answer.error or 'неизвестная ошибка'}"
                )

        context_error: str | None = None
        if brick_id in known:
            context_result = _validate_context_value(
                definition, known[brick_id], today=today
            )
            if context_result.is_valid or context_result.is_empty_optional:
                continue
            context_error = context_result.error

        questions.append(
            Question(
                brick_id=brick_id,
                code=node.code,
                title=definition.title,
                value_type=definition.value_type,
                rules=dict(definition.rules),
                source=definition.source,
                required=False,
                allow_empty=True,
                context_error=context_error,
            )
        )
    return questions


def answer_payload_to_raw(answer: BrickValueResult) -> object | None:
    """
    Преобразует нормализованный payload значения обратно в вид, который
    принимает компилятор Stage 24. Явно пустое необязательное значение
    возвращает None; отсутствие значения сюда не попадает.
    """
    if answer.status is BrickValueStatus.EMPTY_OPTIONAL:
        return None
    if answer.status is not BrickValueStatus.VALID:
        return None
    payload = answer.payload or {}
    for key in ("text", "date", "time", "url", "number", "choice"):
        if key in payload:
            return payload[key]
    if "choices" in payload:
        choices = payload["choices"]
        if isinstance(choices, (list, tuple)):
            return list(choices)
    return None


def visible_brick_issue(
    definition: BrickDefinition,
    *,
    place_id: int | None,
    owner_user_id: int,
) -> str | None:
    """
    Причина, по которой определение несовместимо с анонсом, либо None.

    Системный кирпичик виден всем; кирпичик места — только выбранному месту;
    личный — только своему владельцу. У личного шаблона места нет
    (`place_id=None`), поэтому кирпичики мест в нём недоступны — это
    осознанное поведение, а не ошибка.
    """
    if not definition.is_active:
        return f"Кирпичик #{definition.id} «{definition.code}» находится в архиве."
    if definition.scope == BRICK_SCOPE_SYSTEM:
        return None
    if definition.scope == BRICK_SCOPE_PLACE:
        if definition.place_id != place_id:
            return (
                f"Кирпичик #{definition.id} относится к другому месту "
                "и несовместим с выбранным."
            )
        return None
    if definition.scope == BRICK_SCOPE_PERSONAL:
        if definition.owner_user_id != owner_user_id:
            return (
                f"Личный кирпичик #{definition.id} принадлежит другому " "пользователю."
            )
        return None
    return f"Неизвестная область кирпичика #{definition.id}."
