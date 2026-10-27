# delivery.py
"""
Poster 2.0 S4: единая Telegram-доставка готового анонса.

Один логический пост:

- без фото — одно сообщение с текстом;
- 1..10 фото — медиа-группы по 2–10 снимков (остаток из одного фото
  отправляется отдельным `sendPhoto`), текст ровно один раз — подписью
  ПОСЛЕДНЕГО фотосегмента, то есть под фото, а не над ними;
- текст не режется на части: лимиты (4096 без фото, 1024 с фото после
  разбора entities) проверяются ДО вызова доставки, а не «лечатся»
  автосокращением.

Модуль намеренно не импортирует легаси-мастер (`handlers/post_creation.py`)
и ничего из диалога: только Telegram + отпечаток payload. Это убирает
циклический импорт и оставляет один сборщик прогресса для живого и
готового поста.

Каждая подтверждённая операция сразу попадает в `progress`/`on_progress`
через `ids`; при сбое в прогресс ничего не добавляется — падение не
считается успехом. Продолжение (`progress["step"]`) досылает только
недостающее.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from telegram import InputMediaPhoto

logger = logging.getLogger(__name__)

# Лимиты Telegram для одного анонса (после разбора entities, т.е. по
# видимому тексту). Без фото — обычное сообщение, с фото — подпись.
ANNOUNCEMENT_TEXT_LIMIT = 4096
ANNOUNCEMENT_CAPTION_LIMIT = 1024

# Решение владельца от 2026-10-27: не более 10 фото на анонс (разделяет
# `announcement_store.MAX_ANNOUNCEMENT_PHOTOS`).
ANNOUNCEMENT_PHOTO_LIMIT = 10

# Telegram принимает не более 10 снимков в одном sendMediaGroup; группа из
# одного элемента недопустима — такой хвост уходит через sendPhoto.
MEDIA_GROUP_LIMIT = 10

# Невидимый маркер пустой подписи (§5.6: пустой текст + фото → только фото,
# без точки). Telegram не принимает пустой текст сообщения, а форма
# публикации требует непустой `publication_text`, поэтому в замороженном
# payload хранится этот маркер: подписью он НЕ становится, а отпечаток
# нормализуется к технической `.`, как и у пустого текста.
EMPTY_CAPTION_TEXT = "\u200b"

# Символы, которые не делают текст видимым: маркеры пустой подписи и BOM.
_INVISIBLE_CHARS = (EMPTY_CAPTION_TEXT, "\u2060", "\ufeff")


def visible_text(text: str | None) -> str:
    """Текст без невидимых маркеров: пустая подпись остаётся пустой."""
    value = text or ""
    for char in _INVISIBLE_CHARS:
        value = value.replace(char, "")
    return value.strip()


def delivery_fingerprint(text: str, photos: Sequence[str]) -> str:
    """
    Отпечаток отправленного: текст + упорядоченный список фото.

    Одинаковое содержимое даёт одинаковый отпечаток (можно продолжить
    частичную отправку и не слать дубль), а любая правка делает прежние
    message_id несопоставимыми с новым содержимым.

    Пустой видимый текст нормализуется к пустой строке: у анонса с фото
    подпись пустая, а в замороженном payload стоит маркер пустой подписи
    (`EMPTY_CAPTION_TEXT`), и без нормализации живой блок и финальная версия
    считались бы разными (это дало бы второй «готовый» пост). Литеральная
    `.` — обычный видимый текст, поэтому у неё свой отпечаток: правка
    `.` → пусто обязана доставить новую версию.
    """
    basis = visible_text(text)
    digest = hashlib.sha256()
    digest.update(basis.encode("utf-8"))
    for file_id in photos:
        digest.update(b"\x00")
        digest.update(str(file_id).encode("utf-8"))
    return digest.hexdigest()


def media_batches(photo_count: int) -> list[int]:
    """
    Разбиение фото на вызовы Telegram: 2–10 в группе, хвост 1 — отдельно.

    Возвращает размеры партий по порядку. Хвост из одного фото НЕЛЬЗЯ
    отправить `sendMediaGroup`, поэтому партия размера 1 уходит отдельным
    `sendPhoto` (см. `send_announcement_post`).
    """
    if photo_count <= 0:
        return []
    sizes: list[int] = []
    remaining = photo_count
    while remaining > 0:
        size = min(MEDIA_GROUP_LIMIT, remaining)
        sizes.append(size)
        remaining -= size
    return sizes


def _progress_ids(progress: dict[str, Any]) -> list[int]:
    ids = progress.get("ids")
    if not isinstance(ids, (list, tuple)):
        return []
    return [int(item) for item in ids if isinstance(item, int)]


def is_retry_after(exc: BaseException) -> bool:
    """Flood control: Telegram просит подождать `retry_after` секунд."""
    return exc.__class__.__name__ == "RetryAfter"


def is_forbidden(exc: BaseException) -> bool:
    """Бот заблокирован/удалён из чата: продолжать бессмысленно."""
    return exc.__class__.__name__ == "Forbidden"


def is_not_modified(exc: BaseException) -> bool:
    """Правка не изменила сообщение — это успех, а не сбой."""
    return "message is not modified" in str(exc).lower()


def is_not_found(exc: BaseException) -> bool:
    """Сообщение для правки/удаления исчезло: нужен новый блок."""
    text = str(exc).lower()
    return "not found" in text or "message to edit" in text


async def call_telegram(
    perform: Callable[[], Awaitable[Any]],
    *,
    retry_limit: int = 1,
    max_sleep: float = 30.0,
) -> Any:
    """
    Один вызов Telegram с единственным повтором на `RetryAfter` (§5.4).

    `RetryAfter` — не сбой доставки, а просьба подождать: анонс повторяется
    ровно один раз после указанной паузы (с разумным потолком ожидания).
    `Forbidden` и остальные ошибки пробрасываются сразу: падение не должно
    считаться успехом, а блокировку бота бессмысленно повторять.
    """
    attempt = 0
    while True:
        try:
            return await perform()
        except Exception as exc:  # noqa: BLE001 — решение по типу ниже
            if not is_retry_after(exc) or attempt >= retry_limit:
                raise
            attempt += 1
            retry_after = float(getattr(exc, "retry_after", 0) or 0)
            delay = max(0.0, min(retry_after, max_sleep))
            logger.warning(
                "Telegram просит подождать %.1f с (RetryAfter) — повтор %s.",
                delay,
                attempt,
            )
            if delay:
                await asyncio.sleep(delay)


async def send_announcement_post(
    bot,
    chat_id: int,
    text: str,
    photos: Sequence[str],
    *,
    progress: dict | None = None,
    on_progress: Callable[[dict], None] | None = None,
    live: bool = False,
) -> list[int]:
    """
    Отправляет анонс одним логическим постом и возвращает message_id.

    `progress` — контейнер продолжения: `fingerprint`, `step`, `ids`,
    `total`. Изменение текста/фото сбрасывает прогресс по отпечатку, иначе
    продолжение досылает только недостающие операции. `on_progress`
    вызывается после каждой подтверждённой Telegram операции, поэтому
    запись в БД не опережает реальную отправку.

    `live` помечает верхний блок конструктора (`disable_web_page_preview=
    False`): payload тот же, отличается только маркер «это ещё не готовый
    пост», по которому служебный экран (`True`) и доставка (без маркера)
    отличаются от живого предпросмотра.
    """
    file_ids = [str(item) for item in photos if item]
    batches = media_batches(len(file_ids))
    operations = len(batches) if batches else 1
    tracking = progress if progress is not None else ({} if on_progress else None)

    done = 0
    sent: list[int] = []
    if tracking is not None:
        fingerprint = delivery_fingerprint(text, file_ids)
        if tracking.get("fingerprint") == fingerprint:
            sent = _progress_ids(tracking)
            done = int(tracking.get("step") or 0)
            # Одна операция может вернуть НЕСКОЛЬКО message_id: настоящий
            # `sendMediaGroup` отвечает списком по сообщению на каждое фото.
            # Поэтому `ids` сверяются с числом операций только снизу (`len >=
            # done`): иначе альбом из N фото считался бы битым прогрессом и
            # отправлялся повторно.
            if done < 0 or done > operations or len(sent) < done:
                logger.warning(
                    "Прогресс отправки анонса не соответствует числу операций "
                    "— отправка начинается заново."
                )
                sent = []
                done = 0
        elif tracking.get("fingerprint"):
            logger.warning("Содержимое анонса изменилось — отправка заново.")
        tracking["fingerprint"] = fingerprint
        tracking["step"] = done
        tracking["ids"] = list(sent)
        tracking["total"] = operations

    def _notify() -> None:
        if tracking is None:
            return
        tracking["ids"] = list(sent)
        if on_progress is not None:
            on_progress(dict(tracking))

    def _remember(messages) -> None:
        if messages is None:
            return
        if isinstance(messages, (list, tuple)):
            for message in messages:
                message_id = getattr(message, "message_id", None)
                if message_id is not None:
                    sent.append(int(message_id))
        else:
            message_id = getattr(messages, "message_id", None)
            if message_id is not None:
                sent.append(int(message_id))

    operation = 0

    async def _run(perform: Callable[[], Awaitable[Any]]) -> None:
        nonlocal operation
        current = operation
        operation += 1
        if current < done:
            return
        # RetryAfter — пауза и один повтор (не сбой доставки); Forbidden и
        # прочее пробрасываются: подтверждённый id не записывается.
        result = await call_telegram(perform)
        _remember(result)
        if tracking is not None:
            # Шаг фиксируется ТОЛЬКО после подтверждённого ответа Telegram:
            # упавший вызов не увеличивает прогресс.
            tracking["step"] = current + 1
            tracking["ids"] = list(sent)
            if on_progress is not None:
                on_progress(dict(tracking))

    _notify()

    if not file_ids:
        await _run(
            lambda: bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                **({"disable_web_page_preview": False} if live else {}),
            )
        )
        return sent

    offset = 0
    batch_index = 0
    for size in batches:
        batch = file_ids[offset : offset + size]
        is_last_batch = batch_index == len(batches) - 1
        offset += size
        batch_index += 1
        if size == 1:

            async def photo_runner(batch=batch, is_last_batch=is_last_batch):
                return await bot.send_photo(
                    chat_id=chat_id,
                    photo=batch[0],
                    caption=text if is_last_batch and text else None,
                    parse_mode="HTML" if is_last_batch and text else None,
                )

            await _run(photo_runner)
            continue

        def group_runner(batch=batch, is_last_batch=is_last_batch):
            media = [InputMediaPhoto(media=file_id) for file_id in batch]
            if is_last_batch and text:
                media[-1] = InputMediaPhoto(
                    media=batch[-1], caption=text, parse_mode="HTML"
                )
            return bot.send_media_group(chat_id=chat_id, media=media)

        await _run(group_runner)

    return sent
