# utils/screen.py
"""
Единый интерфейсный экран приложения (Stage 11).

Пользователь видит одно постоянное сообщение-экран, которое перерисовывается
через editMessageText, а не создаётся заново. Временные сообщения (ввод
пользователя, подсказки, медиа-превью) отслеживаются и удаляются
best-effort — только после надёжного сохранения данных.

Состояние UI хранится в двух местах:
- user_data (оперативно, PTB) — chat/message_id экрана, списки временных
  сообщений и превью;
- таблица user_states (SQLite) — message_id экрана и JSON-снимок WIP:
  выживает при рестарте процесса (см. models.UserState).

Экраны шлются с disable_web_page_preview=True: сводка поста может
содержать ссылки, превью не должно раздувать интерфейсное сообщение.
"""

import json
import logging
from typing import Any

from telegram import Message
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import SessionLocal
from models import UserState, utcnow
from utils import tg_context as ctx

logger = logging.getLogger(__name__)


# —— экран (постоянное сообщение управления) ——————————————————————————


def remember_screen(user_data: dict, chat_id: int, message_id: int) -> None:
    """Запоминает текущий экран в user_data."""
    user_data["screen"] = {"chat_id": chat_id, "message_id": message_id}


def screen_ref(user_data: dict) -> dict | None:
    """Идентификаторы текущего экрана (None — экрана ещё не было)."""
    ref = user_data.get("screen")
    return ref if isinstance(ref, dict) else None


async def render_screen(
    context: ContextTypes.DEFAULT_TYPE,
    user_data: dict,
    chat_id: int,
    text: str,
    reply_markup=None,
) -> None:
    """
    Рисует экран: правит существующее сообщение, при неудаче создаёт новое.

    Неудаль редактирования (сообщение удалено, потеряно после рестарта,
    недоступно) — не ошибка для пользователя: создаётся новый экран.
    «Message is not modified» — экран уже актуален (повторное нажатие
    кнопки), проглатывается.
    """
    bot = context.bot
    ref = screen_ref(user_data)
    if ref:
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=ref["chat_id"],
                message_id=ref["message_id"],
                parse_mode="HTML",
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except TelegramError as e:
            if "message is not modified" in str(e).lower():
                logger.info("Экран уже актуален (message is not modified).")
                return
            logger.info(f"Экран не отредактирован ({e}) — создаю новый.")
    message = await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    if message is not None:
        remember_screen(user_data, chat_id, message.message_id)


async def render_screen_from_update(
    update: Any, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
) -> None:
    """Рендер экрана по update (chat берётся из effective_chat)."""
    await render_screen(
        context, ctx.data(context), ctx.chat(update).id, text, reply_markup
    )


# —— временные сообщения (best-effort очистка) —————————————————————————


def track_temp(user_data: dict, message: Message | None) -> None:
    """
    Запоминает временное сообщение (ввод пользователя, подсказка) для
    последующего удаления. Ничего не делает для None.
    """
    if message is None or not hasattr(message, "message_id"):
        return
    temps: list[int] = user_data.setdefault("temp_messages", [])
    if message.message_id not in temps:
        temps.append(message.message_id)


def track_preview(user_data: dict, message_ids: list[int]) -> None:
    """
    Запоминает сообщения текущего превью (текст/фото/альбом). Превью —
    временный результат: при следующем превью или переходе удаляется,
    стек старых версий не накапливается. Готовый пост сюда НЕ попадает.
    """
    user_data["preview_messages"] = [int(mid) for mid in message_ids]


async def cleanup_messages(bot, chat_id: int, message_ids: list[int]) -> None:
    """
    Удаляет сообщения best-effort: ошибки Telegram (уже удалено, слишком
    старое, запрет в чате) логируются и не прерывают сценарий — данные
    уже сохранены, диалог не должен ломаться из-за очистки.
    """
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramError as e:
            logger.info(f"Не удалось удалить сообщение {message_id}: {e}")


async def cleanup_temps(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_data: dict
) -> None:
    """Удаляет все запомненные временные сообщения (ввод, подсказки)."""
    temps = user_data.pop("temp_messages", None) or []
    if temps:
        await cleanup_messages(context.bot, chat_id, temps)


async def cleanup_preview(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_data: dict
) -> None:
    """Удаляет сообщения текущего превью (медиа-предпросмотр)."""
    previews = user_data.pop("preview_messages", None) or []
    if previews:
        await cleanup_messages(context.bot, chat_id, previews)


# —— долговременное состояние (таблица user_states) ————————————————————


def _dumps(payload: dict | None) -> str | None:
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)


def _loads(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def save_state(
    user_id: int,
    user_data: dict,
    chat_id: int,
    payload: dict | None,
) -> None:
    """
    Сохраняет снимок состояния: ref экрана + WIP-payload.

    Вызывается после каждого надёжно принятого изменения: в БД попадает
    только подтверждённо сохранённый WIP — поэтому очистка временных
    сообщений пользователя безопасна даже при аварийном рестарте.
    Ошибка БД не прерывает сценарий (логируется) — данные остаются в
    user_data и попробуются сохранить при следующем изменении.
    """
    session = SessionLocal()
    try:
        row = session.get(UserState, user_id)
        if row is None:
            row = UserState(user_id=user_id)
            session.add(row)
        row.chat_id = chat_id
        ref = screen_ref(user_data)
        row.screen_message_id = ref["message_id"] if ref else None
        row.payload = _dumps(payload)
        row.updated_at = utcnow()
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Не удалось сохранить состояние пользователя {user_id}: {e}")
    finally:
        session.close()


def load_state(user_id: int) -> tuple[dict | None, dict | None]:
    """
    Читает (screen_ref, payload) из user_states.

    Используется при восстановлении после рестарта: экран известен — /start
    отредактирует его, а не создаст новый; WIP непуст — можно продолжить.
    """
    session = SessionLocal()
    try:
        row = session.get(UserState, user_id)
        if row is None:
            return None, None
        ref = (
            {"chat_id": row.chat_id, "message_id": row.screen_message_id}
            if row.chat_id is not None and row.screen_message_id is not None
            else None
        )
        return ref, _loads(row.payload)
    except Exception as e:
        logger.error(f"Не удалось прочитать состояние пользователя {user_id}: {e}")
        return None, None
    finally:
        session.close()


def clear_payload(user_id: int) -> None:
    """Очищает WIP-снимок (пост завершён/сохранён/отменён). ref экрана остаётся."""
    session = SessionLocal()
    try:
        row = session.get(UserState, user_id)
        if row is not None:
            row.payload = None
            row.updated_at = utcnow()
            session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Не удалось очистить WIP пользователя {user_id}: {e}")
    finally:
        session.close()


def hydrate_screen_ref(user_data: dict, user_id: int) -> dict | None:
    """
    Восстанавливает ref экрана из БД в user_data (после рестарта процесса).

    Возвращает ref, если он известен; user_data заполняется молча — вызывающий
    решает, как его использовать (обычно — рендер экрана через существующее
    сообщение).
    """
    ref, _payload = load_state(user_id)
    if ref:
        user_data["screen"] = ref
    return ref


def load_payload(user_id: int) -> dict | None:
    """Читает только WIP-payload (без ref экрана)."""
    _ref, payload = load_state(user_id)
    return payload


def save_screen_ref(user_id: int, user_data: dict, chat_id: int) -> None:
    """
    Сохраняет ref экрана в БД, НЕ трогая WIP-payload.

    Используется при отрисовке экранов вне диалога (главное меню, справка):
    экран должен пережить рестарт, а незавершённый пост — остаться в БД
    до явного завершения.
    """
    _ref, payload = load_state(user_id)
    save_state(user_id, user_data, chat_id, payload)
