# handlers/wizard_photos.py
"""
Poster 2.0 S4: последний шаг конструктора — фотографии анонса.

Шаг живёт после последнего кирпичика и не имеет кнопки «Готово»: анонс
готовится автоматически, как только принята ЦЕЛАЯ Telegram-партия — одно
фото или завершённый альбом (2–10 снимков). «Без фотографий» завершает
сразу, без дополнительных вопросов.

Модуль держит три вещи:

- вид шага: текст и кнопки (переехали из `announcement_wizard._render_photo_step`);
- приём партии: идемпотентное сохранение `file_id` с CAS и жёстким лимитом
  10 фото на анонс (решение владельца 2026-10-27). 11-е фото отклоняется,
  уже принятые не теряются и не перезаписываются;
- границу альбома: Telegram присылает каждое фото отдельным Update с общим
  `media_group_id`, поэтому партия собирается в буфере `user_data` и
  закрывается короткой задержкой (`asyncio`) либо следующим действием
  пользователя. До закрытия партии готовность НЕ наступает.

Каждый снимок сразу сохраняется в SQLite (идемпотентно и с CAS), поэтому
рестарт посреди альбома ничего не теряет и не дублирует: незавершённая
партия просто не запускает финализацию.

Порядок в чате («медиаблок сверху, служебный экран снизу») обеспечивает
`handlers/wizard_live.py`; шаг только просит синхронизацию до отрисовки.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from telegram import InlineKeyboardMarkup

from announcement_store import (
    MAX_ANNOUNCEMENT_PHOTOS,
    AnnouncementError,
    add_announcement_photo,
    get_announcement_draft,
)
from database import SessionLocal
from utils import keyboard_layouts
from utils.tg_html import escape_html
from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Пауза закрытия альбома: Telegram доставляет все снимки одной группы почти
# одновременно, а новый Update той же группы приходит в пределах десятых
# секунды. Задержка нужна, чтобы не начать сборку анонса на первом снимке
# трёхсоставного альбома.
ALBUM_DEBOUNCE_SECONDS = 0.2

# Буфер незавершённой партии в `user_data`: media_group_id → [file_id, ...].
PENDING_ALBUM_KEY = "wizard_photos_pending"
PENDING_TASK_KEY = "wizard_photos_task"
# Итог приёма по партии: сколько принято/отклонено. По нему партия решает,
# завершать анонс автоматически или показать объяснение.
PENDING_RESULT_KEY = "wizard_photos_results"


def h(value: object) -> str:
    """HTML-экранирование значения для текста экрана."""
    return escape_html(str(value if value is not None else ""))


@dataclass(frozen=True)
class PhotoBatchResult:
    """Итог приёма партии: актуальный черновик и что именно произошло."""

    details: Any
    accepted: tuple[str, ...] = ()
    duplicate: tuple[str, ...] = ()
    rejected: tuple[str, ...] = ()
    photos_before: int = 0

    @property
    def limit_reached(self) -> bool:
        return bool(self.rejected)

    @property
    def changed(self) -> bool:
        """Партия реально что-то добавила (дубли и отказы — не изменение)."""
        return bool(self.accepted)


def photo_limit(details: Any) -> int:
    """Сколько ещё снимков примет анонс (лимит 10 на весь анонс)."""
    return max(0, MAX_ANNOUNCEMENT_PHOTOS - len(getattr(details, "photos", ())))


def add_photo_batch(
    *,
    telegram_user_id: int,
    draft_id: int,
    revision: int | None,
    file_ids: list[str] | tuple[str, ...],
    session_factory=None,
) -> PhotoBatchResult:
    """
    Сохраняет последовательность `file_id` с CAS и лимитом анонса.

    Каждое подтверждённое добавление поднимает revision, поэтому ожидаемая
    ревизия обновляется между вызовами. Дубль `file_id` идемпотентен и не
    считается новым снимком. При исчерпании лимита остаток возвращается в
    `rejected`: данные уже принятых фото сохранены и не перезаписаны.
    Ошибка домена пробрасывается — вызывающий слой покажет её честно.
    """
    factory = session_factory or SessionLocal
    with factory() as session:
        details = get_announcement_draft(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )
    photos_before = len(details.photos)
    known = list(details.photos)
    accepted: list[str] = []
    duplicate: list[str] = []
    rejected: list[str] = []
    current_revision = revision
    for index, file_id in enumerate(file_ids):
        value = str(file_id)
        if value in known:
            # Уже сохранён (в БД или этой же партией): повтор Update'а.
            duplicate.append(value)
            continue
        if len(known) >= MAX_ANNOUNCEMENT_PHOTOS:
            # 11-е фото и далее: не принимаем, но и не теряем молча.
            rejected.extend(str(item) for item in file_ids[index:])
            break
        with factory() as session:
            updated = add_announcement_photo(
                session,
                actor_telegram_user_id=telegram_user_id,
                draft_id=draft_id,
                file_id=value,
                expected_revision=current_revision,
            )
        details = updated
        known = list(updated.photos)
        current_revision = updated.revision
        accepted.append(value)
    return PhotoBatchResult(
        details=details,
        accepted=tuple(accepted),
        duplicate=tuple(duplicate),
        rejected=tuple(rejected),
        photos_before=photos_before,
    )


def limit_notice(result: PhotoBatchResult) -> str:
    """Объяснение отказа без потери уже принятых фото (S4 §5.3)."""
    if not result.rejected:
        return ""
    return tr(
        "wizard.photos.limit",
        limit=MAX_ANNOUNCEMENT_PHOTOS,
        rejected=len(result.rejected),
    )


def batch_notice(result: PhotoBatchResult) -> str:
    """Короткое подтверждение приёма партии для нижнего экрана."""
    if result.rejected:
        return limit_notice(result)
    if result.accepted:
        count = len(result.accepted)
        key = (
            "wizard.photos.accepted_one"
            if count == 1
            else "wizard.photos.accepted_many"
        )
        return tr(key, count=count)
    if result.duplicate:
        return tr("wizard.photos.duplicate")
    return ""


def photo_step_text(
    details: Any,
    *,
    notice: str | None = None,
    announcement_is_empty: bool = False,
) -> str:
    """
    Текст нижнего экрана шага фото: короткая инструкция и счётчик.

    Технических объяснений (медиагруппы, статус публикации, «партия») здесь
    нет: владелец просил одно понятное действие. Шутка — только в этом
    служебном экране и только пока текста анонса нет (S3 п.11).
    """
    total = len(getattr(details, "photos", ()) or ())
    lines = [
        tr("wizard.photos.title"),
        "",
        tr("wizard.photos.hint", limit=MAX_ANNOUNCEMENT_PHOTOS),
    ]
    if total:
        lines.append(
            tr("wizard.photos.counter", count=total, limit=MAX_ANNOUNCEMENT_PHOTOS)
        )
    if notice:
        lines.insert(1, notice)
    if announcement_is_empty:
        from utils import ui_humor

        humor = ui_humor.get_humor_line(announcement_is_empty=True)
        if humor:
            lines.extend(["", humor])
    return "\n".join(lines)


def photo_step_keyboard(details: Any) -> InlineKeyboardMarkup:
    """
    Кнопки шага фото: ровно две в одной строке — «Назад» и «Пропустить».

    «Пропустить» (callback `aw_photo_none`) сохраняет прежнюю семантику:
    анонс завершается сразу, без вложений. Добавление фото отдельной
    кнопкой не нужно — снимки принимаются обычным сообщением, о чём и
    говорит текст экрана. «Назад» возвращает к предыдущему шагу (последний
    кирпичик), не меняя уже сохранённые данные.

    U3a №13: пара собирается общим шаблоном `keyboard_layouts.pair`, а не
    вручную — так же, как пара «Назад | Пропустить» на вопросе даты.
    """
    draft_id = details.draft_id
    revision = details.revision
    return InlineKeyboardMarkup(
        keyboard_layouts.pair(
            keyboard_layouts.button(
                tr("buttons.nav.back"), f"aw_photo_back:{draft_id}:{revision}"
            ),
            keyboard_layouts.button(
                tr("buttons.wizard.skip"), f"aw_photo_none:{draft_id}:{revision}"
            ),
        )
    )


async def render_photo_step(
    update,
    context,
    details,
    *,
    notice: str | None = None,
    announcement_is_empty: bool = False,
) -> int:
    """Отрисовывает шаг фото: сначала живой верхний блок, затем экран."""
    from handlers.announcement_wizard import (
        AW_PREVIEW,
        _render,
        _set_details,
        _sync_live,
        _ui,
    )

    ui = _ui(context)
    ui["photo_step"] = True
    _set_details(ui, details)
    await _sync_live(update, context, details)
    await _render(
        update,
        context,
        photo_step_text(
            details, notice=notice, announcement_is_empty=announcement_is_empty
        ),
        photo_step_keyboard(details),
    )
    return AW_PREVIEW


# --- буфер альбома ---------------------------------------------------------


def _pending(user_data: dict[str, Any]) -> dict[str, list[str]]:
    buffer = user_data.get(PENDING_ALBUM_KEY)
    if not isinstance(buffer, dict):
        buffer = {}
        user_data[PENDING_ALBUM_KEY] = buffer
    return buffer


def buffer_photo(
    user_data: dict[str, Any], media_group_id: str | None, file_id: str
) -> str:
    """
    Кладёт `file_id` в партию и возвращает ключ партии.

    Одиночное фото (без `media_group_id`) — партия из одного снимка с
    уникальным ключом: она закрывается сразу, без ожидания. Одинаковый
    `file_id` дважды в партию не попадает: повтор Update'а не дублирует
    ни сохранение, ни финализацию.
    """
    key = media_group_id or f"single-{file_id}"
    batch = _pending(user_data).setdefault(key, [])
    if file_id not in batch:
        batch.append(file_id)
    return key


def store_result(user_data: dict[str, Any], key: str, result: PhotoBatchResult) -> None:
    """Запоминает итог приёма партии до решения о финализации."""
    results = user_data.get(PENDING_RESULT_KEY)
    if not isinstance(results, dict):
        results = {}
        user_data[PENDING_RESULT_KEY] = results
    results[key] = result


def absorb_result(
    user_data: dict[str, Any], key: str, result: PhotoBatchResult
) -> PhotoBatchResult:
    """
    Складывает итоги отдельных Update'ов одной партии в общий.

    Telegram присылает альбом несколькими Update'ами, поэтому «принято/
    отклонено» копится по партии: финализация наступает только после её
    закрытия, а лимит объясняется один раз.
    """
    previous = user_data.get(PENDING_RESULT_KEY)
    previous = previous.get(key) if isinstance(previous, dict) else None
    merged = PhotoBatchResult(
        details=result.details,
        accepted=(
            (previous.accepted if isinstance(previous, PhotoBatchResult) else ())
            + result.accepted
        ),
        duplicate=(
            (previous.duplicate if isinstance(previous, PhotoBatchResult) else ())
            + result.duplicate
        ),
        rejected=(
            (previous.rejected if isinstance(previous, PhotoBatchResult) else ())
            + result.rejected
        ),
        photos_before=(
            previous.photos_before
            if isinstance(previous, PhotoBatchResult)
            else result.photos_before
        ),
    )
    store_result(user_data, key, merged)
    return merged


def take_result(user_data: dict[str, Any], key: str) -> PhotoBatchResult | None:
    """Забирает итог партии (None — партия ещё не принималась)."""
    results = user_data.get(PENDING_RESULT_KEY)
    if not isinstance(results, dict):
        return None
    value = results.pop(key, None)
    if not results:
        user_data.pop(PENDING_RESULT_KEY, None)
    return value if isinstance(value, PhotoBatchResult) else None


def take_batch(user_data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Забирает партию из буфера; повторный вызов возвращает пустой кортеж."""
    buffer = _pending(user_data)
    items = buffer.pop(key, [])
    if not buffer:
        user_data.pop(PENDING_ALBUM_KEY, None)
    return tuple(items)


def cancel_pending_task(user_data: dict[str, Any]) -> None:
    """Отменяет запланированное закрытие партии (диалог ушёл вперёд)."""
    task = user_data.pop(PENDING_TASK_KEY, None)
    if task is not None and not getattr(task, "done", lambda: True)():
        task.cancel()


def schedule_album_close(application, user_data: dict[str, Any], closer) -> None:
    """
    Закрывает партию после короткой паузы, если новых снимков не пришло.

    JobQueue у бота отключён (`job_queue(None)`), поэтому используется
    `Application.create_task`: задержка не блокирует обработку следующих
    фото того же альбома, а запасной путь — явное закрытие следующим
    действием пользователя через `cancel_pending_task` в мастере анонса.
    """
    if application is None:
        return
    cancel_pending_task(user_data)

    async def _wait() -> None:
        try:
            await asyncio.sleep(ALBUM_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:  # pragma: no cover — отмена штатная
            return
        try:
            await closer()
        except Exception as exc:  # noqa: BLE001 — шаг не должен ломать бота
            logger.exception("Не удалось закрыть партию фото: %s", exc)

    # PTB не создаёт задачи, пока приложение не запущено (тесты гоняют
    # обработчики напрямую) — тогда планируем обычной задачей текущей петли.
    if getattr(application, "running", False):
        task = application.create_task(_wait())
    else:
        task = asyncio.ensure_future(_wait())
    user_data[PENDING_TASK_KEY] = task


def rejected_photo_notice(error: BaseException) -> str:
    """Единый текст отказа по лимиту/домену — без потери принятых фото."""
    return tr("wizard.photos.rejected", reason=str(error)[:300])


__all__ = [
    "ALBUM_DEBOUNCE_SECONDS",
    "AnnouncementError",
    "PhotoBatchResult",
    "add_photo_batch",
    "batch_notice",
    "buffer_photo",
    "cancel_pending_task",
    "h",
    "limit_notice",
    "photo_limit",
    "photo_step_keyboard",
    "photo_step_text",
    "rejected_photo_notice",
    "render_photo_step",
    "schedule_album_close",
    "store_result",
    "take_batch",
    "take_result",
]
