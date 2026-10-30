# handlers/place_invite.py
"""
Открытие ссылки «создать анонс площадки» через `/start <token>`
(Poster 2.2 U4b, миграция 23).

Администратор площадки показывает ссылку `https://t.me/<бот>?start=<token>`.
Другой человек открывает её — бот стартует и СРАЗУ открывает конструктор этой
площадки: без выбора площадки, без выбора шаблона, с её стандартным шаблоном.

Правила:

- ссылка НЕ даёт прав: `place_admins` по ней не создаётся, площадку и
  шаблон открывший не редактирует;
- повторное открытие ТОЙ ЖЕ ссылки продолжает уже начатый черновик этой
  площадки (введённые данные и вопрос, на котором остановились), а не
  создаёт второй: Telegram повторяет update, и человек нажимает ссылку
  снова;
- прежний незавершённый анонс по ДРУГОЙ площадке НЕ удаляется: он
  остаётся в «📂 Мои анонсы», а по ссылке открывается черновик её площадки;
- отозванный/подделанный токен и удалённая площадка дают один понятный
  ответ и главное меню — это не ошибка и не подсказка о существовании
  токена;
- `/start` БЕЗ payload не перехватывается: обычный вход в меню не меняется.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, ConversationHandler

from admin_state import clear_admin_session
from brick_state import clear_brick_session
from database import SessionLocal
from place_invite_store import is_valid_token_shape, open_invite
from place_state import clear_place_session
from template_state import clear_template_session
from utils import screen
from utils import tg_context as ctx
from utils.ui_texts import tr

logger = logging.getLogger(__name__)


class InviteStartHandler(CommandHandler):
    """
    Entry-point диалога: `/start <token>` ссылки на создание анонса.

    Ссылка должна открывать конструктор сразу, поэтому она обязана быть
    точкой входа `ConversationHandler` — иначе текст следующего шага не
    попадёт в диалог. Перехватываются только payload'ы правильной формы:
    `/start` без payload и `/start tf_...` (передача прав) остаются прежним
    обработчикам, а мусорный payload разбирает обычный вход в меню, который
    показывает тот же безопасный отказ.
    """

    def check_update(self, update: object):
        matched = super().check_update(update)
        if matched is None or matched is False:
            return None
        payload = _raw_payload(update)
        if payload is None or payload.startswith("tf_"):
            return None
        if not is_valid_token_shape(payload):
            return None
        return matched


def _raw_payload(update: object) -> str | None:
    """Ровно один аргумент `/start` из текста сообщения."""
    message = getattr(update, "effective_message", None)
    text = getattr(message, "text", None)
    if not isinstance(text, str):
        return None
    parts = text.split()
    if len(parts) != 2:
        return None
    return parts[1]


async def open_invite_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Точка входа диалога: сразу открывает конструктор площадки из ссылки."""
    state = await open_invite_payload(update, context)
    return state if state is not None else ConversationHandler.END


def invite_start_handler() -> InviteStartHandler:
    """Готовый entry-point `ConversationHandler` для ссылки (U4b)."""
    return InviteStartHandler("start", open_invite_start)


def invite_payload(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """
    Единственный аргумент `/start` (None — команда без payload).

    Payload передачи прав (`tf_...`) — чужое пространство имён: его
    обрабатывает `handlers.admin.handle_transfer_start`, и здесь он не
    перехватывается.
    """
    args = getattr(context, "args", None) or []
    if len(args) != 1:
        return None
    payload = args[0]
    if not isinstance(payload, str) or not payload:
        return None
    if payload.startswith("tf_"):
        return None
    return payload


async def open_invite_payload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """
    Разбирает `/start <payload>` и открывает конструктор площадки.

    Возвращает состояние диалога анонсов (его нужно вернуть из обработчика
    `/start`), `ConversationHandler.END` для недействительной ссылки или
    None, если payload нет — тогда вызывающий работает как обычно.

    Недействительная ссылка — не ошибка: понятное сообщение и главное меню.
    """
    payload = invite_payload(context)
    if payload is None:
        return None

    user_id = ctx.user(update).id
    invite = None
    session = SessionLocal()
    try:
        result = open_invite(session, payload)
        if result.status == "ok":
            invite = result.invite
    except Exception as e:  # noqa: BLE001 — ссылка не должна ронять бота
        logger.error("Не удалось открыть ссылку создания анонса: %s", e)
    finally:
        session.close()

    if invite is None:
        await _render_invalid(update, context, user_id)
        return ConversationHandler.END

    from handlers.announcement_wizard import (
        resume_place_announcement,
        start_place_announcement_pick,
    )

    # U4b, дефект приёмки: ссылка на ТУ ЖЕ площадку при повторном открытии
    # продолжает уже начатый черновик (с введёнными данными), а не создаёт
    # второй. Незавершённый анонс по ДРУГОЙ площадке остаётся нетронутым:
    # для него открывается новый черновик этой площадки.
    state = await resume_place_announcement(update, context, invite.place_id)
    if state is None:
        state = await start_place_announcement_pick(update, context, invite.place_id)
    await screen.drop_user_input(update, context)
    return state


async def _render_invalid(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    """Понятное сообщение и главное меню: ссылка не сработала."""
    from handlers.main_menu import main_menu_keyboard

    await screen.reanchor_screen(update, context)
    clear_place_session(user_id)
    clear_admin_session(user_id)
    clear_brick_session(user_id)
    clear_template_session(user_id)
    user_data = ctx.data(context)
    if screen.screen_ref(user_data) is None:
        screen.hydrate_screen_ref(user_data, user_id)
    chat_id = ctx.chat(update).id
    await screen.render_screen(
        context,
        user_data,
        chat_id,
        tr("places.invite.invalid"),
        main_menu_keyboard(),
    )
    screen.save_screen_ref(user_id, user_data, chat_id)
    await screen.drop_user_input(update, context)
