# handlers/wizard_live.py
"""
Poster 2.0: живой верхний блок анонса (S3 — текст, S4 — медиаблок).

Логический верхний блок — это настоящий анонс, который стоит в чате ВЫШЕ
единственного служебного экрана. Он может состоять из нескольких сообщений
Telegram, когда у анонса есть фотографии:

- собрать текст тем же `template_compiler.render_announcement`, что и
  итоговая отправка (одна точка правды для live/final);
- отправить его ДО нижнего вопроса (S3 п.2), в том числе техническую `.`,
  если видимый текст пуст и фотографий нет;
- с фото — отправить фотографии партиями 2–10 и текст ровно один раз
  подписью ПОСЛЕДНЕГО сегмента (фото над текстом, S4 §5.3);
- после каждого ответа отредактировать существующий блок
  (`editMessageText` / `editMessageCaption`), а не создавать новый;
- если блок изменился по составу (появились/исчезли фото) — отправить
  новую версию, а прошлую удалять ТОЛЬКО после подтверждённого успеха;
- восстановить адреса из БД после рестарта: повторного снимка и дублей нет.

Медиа-группу нельзя «дополнить»: `editMessageCaption` меняет только
подпись, поэтому смена набора фото пересобирает блок. Номер и адреса
подтверждённых сообщений хранит `live_message_ids_json` — миграция для
этого уже есть (S1).

Клавиатуры верхний блок не содержит: кнопки живут только на служебном
экране, иначе готовый анонс нельзя было бы скопировать.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from telegram.error import TelegramError

from announcement_store import (
    AnnouncementError,
    build_announcement_render_payload,
    set_announcement_live_message_ids,
)
from database import SessionLocal
from delivery import (
    LIVE_LINK_PREVIEW,
    delivery_fingerprint,
    media_batches,
    send_announcement_post,
)
from template_compiler import render_announcement

if TYPE_CHECKING:  # pragma: no cover — только для типов, без циклического импорта
    from telegram import Bot

logger = logging.getLogger(__name__)

# Где в `user_data` лежит служебное состояние живого блока: отпечаток уже
# показанной версии и прогресс частичной отправки. В БД этого нет (схема
# не меняется): после рестарта блок считается актуальным, если количество
# его сообщений совпадает с ожидаемым числом сегментов.
LIVE_STATE_KEY = "announcement_live_state"
LIVE_PROGRESS_KEY = "announcement_live_progress"


@dataclass(frozen=True)
class LiveBlock:
    """Результат синхронизации: адреса верхнего блока и отправленный текст."""

    message_ids: tuple[int, ...]
    text: str
    # Сколько сообщений пришлось отправить заново (0 — отредактировали).
    replaced: int = 0
    # Сколько фото в показанном блоке (для финализации и порядка в чате).
    photo_count: int = 0
    # Отпечаток показанного содержимого: по нему финал узнаёт свою версию.
    fingerprint: str = ""

    @property
    def has_media(self) -> bool:
        return self.photo_count > 0


def render_live_text(
    *,
    telegram_user_id: int,
    draft_id: int,
    has_photos: bool = False,
    session_factory=None,
) -> str:
    """
    Собирает текст верхнего блока из текущего черновика.

    Использует тот же `render_announcement`, что и итоговая отправка: живой
    блок и готовый анонс не могут разойтись. Техническая `.` появляется
    только при полностью пустом видимом тексте и отсутствии фото; значения
    кирпичиков не меняются.
    """
    factory = session_factory or SessionLocal
    with factory() as session:
        payload = build_announcement_render_payload(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )
    return render_announcement(
        payload.tokens_by_section,
        payload.values_by_id,
        payload.definitions_by_id,
        has_photos=has_photos,
    )


def current_live_ids(details) -> tuple[int, ...]:
    """Адреса живого блока из состояния черновика (порядок сохранён)."""
    return tuple(getattr(details, "live_message_ids", ()) or ())


def expected_segments(photo_count: int) -> int:
    """Сколько сообщений займёт блок: 1 без фото, иначе партии 2–10."""
    return len(media_batches(photo_count)) or 1


def live_state(user_data: dict) -> dict:
    state = user_data.get(LIVE_STATE_KEY)
    return state if isinstance(state, dict) else {}


def remember_live_state(user_data: dict, *, draft_id: int, block: LiveBlock) -> None:
    """Запоминает, какая именно версия показана (до рестарта процесса)."""
    user_data[LIVE_STATE_KEY] = {
        "draft_id": int(draft_id),
        "fingerprint": block.fingerprint,
        "ids": list(block.message_ids),
        "photo_count": block.photo_count,
    }


def forget_live_state(user_data: dict) -> None:
    user_data.pop(LIVE_STATE_KEY, None)
    user_data.pop(LIVE_PROGRESS_KEY, None)


def live_matches(user_data: dict, *, draft_id: int, fingerprint: str) -> bool:
    """Совпадает ли показанная версия с ожидаемым содержимым (в сессии)."""
    state = live_state(user_data)
    return (
        int(state.get("draft_id") or 0) == int(draft_id)
        and bool(fingerprint)
        and state.get("fingerprint") == fingerprint
    )


def _persist_ids(
    *,
    telegram_user_id: int,
    draft_id: int,
    message_ids: tuple[int, ...],
    revision: int | None,
    session_factory=None,
) -> tuple[int, ...]:
    """
    Пишет подтверждённые адреса в БД.

    Сбой записи не отменяет уже показанный пользователю блок: он остаётся
    в чате, а следующий ответ попробует сохранить адреса снова. Возвращается
    актуальный набор (при сбое — переданный).
    """
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            updated = set_announcement_live_message_ids(
                session,
                actor_telegram_user_id=telegram_user_id,
                draft_id=draft_id,
                message_ids=message_ids,
                expected_revision=revision,
            )
    except AnnouncementError as exc:
        logger.info("Не удалось сохранить адреса живого блока #%s: %s", draft_id, exc)
        return message_ids
    return tuple(updated.live_message_ids)


def _is_not_modified(exc: BaseException) -> bool:
    return "message is not modified" in str(exc).lower()


async def _edit_text_block(
    bot: "Bot", chat_id: int, message_ids: tuple[int, ...], text: str
) -> bool:
    """Правит текстовый блок; True — содержимое уже актуально."""
    try:
        await bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_ids[0],
            parse_mode="HTML",
            reply_markup=None,
            link_preview_options=LIVE_LINK_PREVIEW,
        )
    except TelegramError as exc:
        if _is_not_modified(exc):
            logger.info("Живой блок уже актуален (message is not modified).")
            return True
        logger.info("Живой блок не отредактирован (%s) — отправляю новую версию.", exc)
        return False
    return True


async def _edit_caption(bot: "Bot", chat_id: int, message_id: int, text: str) -> bool:
    """Правит подпись последнего сегмента медиаблока; True — уже актуальна."""
    try:
        await bot.edit_message_caption(
            chat_id=chat_id,
            message_id=message_id,
            caption=text or None,
            parse_mode="HTML" if text else None,
        )
    except TelegramError as exc:
        if _is_not_modified(exc):
            logger.info("Подпись живого блока уже актуальна.")
            return True
        logger.info("Подпись живого блока не изменена (%s) — пересобираю.", exc)
        return False
    return True


async def _delete_old(bot: "Bot", chat_id: int, old_ids: tuple[int, ...]) -> None:
    """Удаляет прошлую версию блока best-effort (только после успеха новой)."""
    for message_id in old_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as exc:  # noqa: BLE001 — удаление не сбой доставки
            logger.info(
                "Прошлая версия живого блока (%s) не удалена: %s", message_id, exc
            )


async def sync_live_block(
    bot: "Bot",
    chat_id: int,
    *,
    telegram_user_id: int,
    draft_id: int,
    revision: int | None = None,
    known_ids: tuple[int, ...] = (),
    text: str | None = None,
    has_photos: bool = False,
    photos: tuple[str, ...] = (),
    force_recreate: bool = False,
    user_data: dict | None = None,
    session_factory=None,
) -> LiveBlock:
    """
    Показывает актуальный верхний блок: правит существующий или отправляет.

    Порядок сообщений в чате: сначала верхний блок, затем служебный экран —
    поэтому вызывающий код обновляет живой блок ДО отрисовки вопроса.

    Правила замены:

    - текст без фото: `editMessageText` (неизменённый текст — успех);
    - медиаблок: `editMessageCaption` последнего сегмента, если число
      сегментов совпадает с ожидаемым. Состав фото неизменен — новых
      сообщений нет;
    - состав изменился (фото добавились/удалились) или править нечего —
      отправляется новая версия, и только после её успеха прошлые адреса
      удаляются. До успеха старая версия остаётся в чате.
    """
    file_ids = tuple(str(item) for item in photos if item)
    has_photos = bool(has_photos or file_ids)
    if text is None:
        text = render_live_text(
            telegram_user_id=telegram_user_id,
            draft_id=draft_id,
            has_photos=has_photos,
            session_factory=session_factory,
        )
    fingerprint = delivery_fingerprint(text, file_ids)
    segments = expected_segments(len(file_ids))
    ids = tuple(known_ids)

    if ids and not force_recreate and len(ids) == segments:
        if not file_ids:
            if await _edit_text_block(bot, chat_id, ids, text):
                return LiveBlock(
                    message_ids=ids,
                    text=text,
                    fingerprint=fingerprint,
                    photo_count=0,
                )
        elif await _edit_caption(bot, chat_id, ids[-1], text):
            # Подпись правится и пустой: у сообщения с фото это «уже
            # актуально», а у ТЕКСТОВОГО блока Telegram отвечает «there is no
            # caption in the message». Так блок без фото заменяется
            # медиаблоком, а не признаётся готовым постом (дефект S4-1:
            # текстовая `.` оставалась в чате, хотя появилось фото).
            return LiveBlock(
                message_ids=ids,
                text=text,
                fingerprint=fingerprint,
                photo_count=len(file_ids),
            )

    progress: dict | None = None
    if user_data is not None:
        stored = user_data.get(LIVE_PROGRESS_KEY)
        if not isinstance(stored, dict) or force_recreate:
            # Пересборка с нуля: остатки прошлой попытки не должны «дослать»
            # старые адреса вместо нового блока.
            stored = {}
            user_data[LIVE_PROGRESS_KEY] = stored
        progress = stored

    def _on_progress(snapshot: dict) -> None:
        # Частичная отправка: фиксируем подтверждённые адреса, чтобы после
        # сбоя не отправлять их второй раз в этой сессии.
        confirmed = tuple(int(mid) for mid in snapshot.get("ids") or [])
        if confirmed:
            _persist_ids(
                telegram_user_id=telegram_user_id,
                draft_id=draft_id,
                message_ids=confirmed,
                revision=revision,
                session_factory=session_factory,
            )

    sent = await send_announcement_post(
        bot,
        chat_id,
        text,
        list(file_ids),
        progress=progress,
        on_progress=_on_progress if progress is not None else None,
        live=True,
    )
    new_ids = tuple(int(mid) for mid in sent)
    if not new_ids:
        # Telegram не вернул message_id: блок не подтверждён, старый цел.
        return LiveBlock(
            message_ids=ids,
            text=text,
            replaced=len(ids),
            photo_count=len(file_ids),
        )
    stored_ids = _persist_ids(
        telegram_user_id=telegram_user_id,
        draft_id=draft_id,
        message_ids=new_ids,
        revision=revision,
        session_factory=session_factory,
    )
    replaced = [old for old in ids if old not in stored_ids]
    if replaced:
        await _delete_old(bot, chat_id, tuple(replaced))
    if user_data is not None:
        user_data.pop(LIVE_PROGRESS_KEY, None)
    return LiveBlock(
        message_ids=stored_ids,
        text=text,
        replaced=len(replaced),
        photo_count=len(file_ids),
        fingerprint=fingerprint,
    )
