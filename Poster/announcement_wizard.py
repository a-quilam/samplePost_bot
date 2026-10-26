# announcement_wizard.py
"""
Доменная логика адаптивного мастера анонсов Stage 27A.

Модуль не зависит от Telegram и не пишет в БД. Он отвечает только за:

- представление независимого снимка шаблона (три секции + сохранённые
  привязки `код → brick_id`);
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
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
    TEMPLATE_SECTION_HEADER,
)

SECTION_ORDER = (
    TEMPLATE_SECTION_HEADER,
    TEMPLATE_SECTION_BODY,
    TEMPLATE_SECTION_FOOTER,
)

# Постоянные ID системных кирпичиков из миграции 6. Контекст места строится
# по ID, а не по повторному разрешению текстовых кодов.
PLACE_BRICK_ID_NAME = 5
PLACE_BRICK_ID_ADDRESS = 6
PLACE_BRICK_ID_URL = 7

EXTRA_POSITION_START = "start"
EXTRA_POSITION_END = "end"
EXTRA_POSITIONS = (EXTRA_POSITION_START, EXTRA_POSITION_END)


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
    sections: Mapping[str, str]
    bindings: tuple[SnapshotBinding, ...] = field(default_factory=tuple)
    source_name: str | None = None

    @property
    def brick_ids(self) -> tuple[int, ...]:
        """Уникальные brick_id в порядке первого появления в секциях."""
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
    section: str
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
    Разделяет допустимость пропуска поля и готовность собранного текста.

    - отдельный кирпичик пропускать можно;
    - текст обязан остаться непустым после удаления разметки;
    - если в снимке есть кирпичики, хотя бы один из них (включая известные
      значения места) должен иметь значение — полностью пустой анонс
      готовым не считается;
    - статический шаблон без кирпичиков может быть готов, если его текст
      непуст.
    """
    visible = visible_announcement_text(rendered_html)
    issues: list[str] = []
    warnings: list[str] = []
    if not visible:
        issues.append(
            "В анонсе нет текста: все поля пропущены, а шаблон не содержит "
            "постоянного текста."
        )
    if snapshot_brick_count > 0 and filled_value_count == 0:
        issues.append(
            "В шаблоне есть поля, но ни одно значение не заполнено. "
            "Заполните хотя бы одно поле или выберите другой шаблон."
        )
    if not issues and snapshot_brick_count > 0:
        warnings.append(
            "Часть полей может быть пропущена — это допустимо, но проверьте "
            "обзор перед следующим этапом."
        )
    return AnnouncementReadiness(
        ready=not issues,
        visible_text=visible,
        issues=tuple(issues),
        warnings=tuple(warnings),
    )


def place_context_values(
    *,
    display_name: str,
    address: str | None,
    url: str | None,
) -> dict[int, object]:
    """
    Известные значения места по постоянным brick_id.

    Отсутствующий адрес/ссылка не превращаются в пустую строку или заглушку:
    такого ключа просто нет, и мастер задаст вопрос только при необходимости.
    """
    values: dict[int, object] = {PLACE_BRICK_ID_NAME: display_name}
    if address is not None and address.strip():
        values[PLACE_BRICK_ID_ADDRESS] = address
    if url is not None and url.strip():
        values[PLACE_BRICK_ID_URL] = url
    return values


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


def iter_snapshot_bricks(
    snapshot: AnnouncementTemplateSnapshot,
) -> Iterable[BrickNode]:
    """
    Обходит секции в порядке шапка → тело → подвал.

    `brick_id` восстанавливается только из сохранённых привязок снимка.
    Текстовый код не разрешается заново и не может подменить определение.
    """
    bindings = _bindings_by_code(snapshot)
    for section in SECTION_ORDER:
        raw = snapshot.sections.get(section)
        if raw is None:
            continue
        try:
            tokens = parse_template(raw, section=section)
        except TemplateParseError as exc:
            raise AnnouncementSnapshotError(
                f"Секция {section} снимка не разбирается: {exc}"
            ) from exc
        for node in _iter_nodes(tokens):
            if node.code not in bindings:
                raise AnnouncementSnapshotError(
                    f"У токена [{node.code}] нет сохранённой привязки в снимке."
                )
            node.brick_id = bindings[node.code]
            yield node


def snapshot_tokens_by_section(
    snapshot: AnnouncementTemplateSnapshot,
) -> dict[str, list[Any]]:
    """
    Разбирает снимок и проставляет brick_id во всех токенах.

    Используется сервисом для передачи компилятору Stage 24. Повторного
    разрешения кодов по названию здесь нет.
    """
    bindings = _bindings_by_code(snapshot)
    result: dict[str, list[Any]] = {}
    for section in SECTION_ORDER:
        raw = snapshot.sections.get(section)
        if raw is None:
            continue
        try:
            tokens = parse_template(raw, section=section)
        except TemplateParseError as exc:
            raise AnnouncementSnapshotError(
                f"Секция {section} снимка не разбирается: {exc}"
            ) from exc
        for node in _iter_nodes(tokens):
            if node.code not in bindings:
                raise AnnouncementSnapshotError(
                    f"У токена [{node.code}] нет сохранённой привязки в снимке."
                )
            node.brick_id = bindings[node.code]
        result[section] = tokens
    return result


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

    - порядок — первое появление кирпичика в шапке → теле → подвале;
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

    for node in iter_snapshot_bricks(snapshot):
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
                section=snapshot_section(snapshot, node.code),
                required=False,
                allow_empty=True,
                context_error=context_error,
            )
        )
    return questions


def snapshot_section(
    snapshot: AnnouncementTemplateSnapshot,
    code: str,
) -> str:
    """Секция первого появления сохранённого кода в снимке."""
    for section in SECTION_ORDER:
        raw = snapshot.sections.get(section)
        if raw is None:
            continue
        try:
            tokens = parse_template(raw, section=section)
        except TemplateParseError:
            continue
        for node in _iter_nodes(tokens):
            if isinstance(node, BrickNode) and node.code == code:
                return section
    return TEMPLATE_SECTION_BODY


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
    place_id: int,
    owner_user_id: int,
) -> str | None:
    """
    Причина, по которой определение несовместимо с анонсом, либо None.

    Системный кирпичик виден всем; кирпичик места — только выбранному месту;
    личный — только своему владельцу.
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
