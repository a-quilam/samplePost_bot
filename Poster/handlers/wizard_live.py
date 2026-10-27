# handlers/wizard_live.py
"""
Poster 2.0 S3: живой верхний блок анонса.

Логический верхний блок — это настоящий анонс (Telegram HTML), который
стоит в чате ВЫШЕ единственного служебного экрана. Модуль отвечает только
за него:

- собрать текст тем же `template_compiler.render_announcement`, что и
  итоговая отправка (одна точка правды для live/final);
- отправить его ДО нижнего вопроса (S3 п.2), в том числе техническую `.`,
  если видимый текст пуст и фотографий нет;
- после каждого ответа отредактировать существующее сообщение
  (`editMessageText`), а не создавать новое;
- если редактирование невозможно (сообщение удалено, старше 48 часов,
  «message is not modified»), отправить новую версию и только после
  успешной отправки обновить сохранённые адреса;
- восстановить адреса из БД после рестарта: повторного снимка и дублей нет.

Медиа-сценарии (альбомы, лимиты, замена нескольких сообщений, финальная
доставка) — S4. В S3 верхний блок текстовый; `live_message_ids_json`
хранит упорядоченный набор уже на будущее.

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
from template_compiler import render_announcement

if TYPE_CHECKING:  # pragma: no cover — только для типов, без циклического импорта
    from telegram import Bot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveBlock:
    """Результат синхронизации: адреса верхнего блока и отправленный текст."""

    message_ids: tuple[int, ...]
    text: str
    # Сколько сообщений пришлось отправить заново (0 — отредактировали).
    replaced: int = 0


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
    session_factory=None,
) -> LiveBlock:
    """
    Показывает актуальный верхний блок: правит существующий или отправляет.

    Порядок сообщений в чате: сначала верхний блок, затем служебный экран —
    поэтому вызывающий код обновляет живой блок ДО отрисовки вопроса.

    Правила замены:

    - есть сохранённый ID → `editMessageText`; при «message is not
      modified» это успех (блок уже актуален);
    - редактирование невозможно → новая версия отправляется ниже, и только
      после её успеха прошлый адрес забывается. До успеха старая версия
      остаётся в чате: пользователь не теряет анонс.
    """
    if text is None:
        text = render_live_text(
            telegram_user_id=telegram_user_id,
            draft_id=draft_id,
            has_photos=has_photos,
            session_factory=session_factory,
        )
    ids = tuple(known_ids)
    if ids:
        target = ids[0]
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=target,
                parse_mode="HTML",
                reply_markup=None,
                disable_web_page_preview=False,
            )
        except TelegramError as exc:
            if "message is not modified" in str(exc).lower():
                logger.info("Живой блок уже актуален (message is not modified).")
                return LiveBlock(message_ids=ids, text=text)
            logger.info(
                "Живой блок не отредактирован (%s) — отправляю новую версию.", exc
            )
        else:
            # Остальные адреса (на будущее — альбомы S4) не трогаем: их
            # замена требует отдельного подтверждения на каждое сообщение.
            return LiveBlock(message_ids=ids, text=text)

    # Живой блок — предпросмотр, а не доставленный пост: явный
    # `disable_web_page_preview=False` отличает его от `send_rendered_post`
    # (тот не передаёт этот параметр) и от служебного экрана (`True`).
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=None,
        disable_web_page_preview=False,
    )
    message_id = getattr(message, "message_id", None)
    if message_id is None:
        return LiveBlock(message_ids=ids, text=text, replaced=1)
    replaced_ids: tuple[int, ...] = (int(message_id),)
    stored = _persist_ids(
        telegram_user_id=telegram_user_id,
        draft_id=draft_id,
        message_ids=replaced_ids,
        revision=revision,
        session_factory=session_factory,
    )
    return LiveBlock(message_ids=stored, text=text, replaced=len(ids))
