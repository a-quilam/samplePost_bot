# handlers/approval.py
"""
Согласование постов (вне ConversationHandler создания поста):

- назначение ответственного в review-чате — id поста приходит в callback_data,
  данные поста читаются из БД, а НЕ из context.user_data нажавшего админа;
- действия ответственного с НАЗНАЧЕННЫМ ему постом: просмотр,
  согласование, отказ.
"""

import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from sqlalchemy.orm import Session

from database import SessionLocal
from models import ResponsiblePerson
from utils.formatter import escape_markdown
from handlers.post_creation import build_post_summary
from approval import (
    STATUS_APPROVED,
    STATUS_DECLINED,
    assign_responsible,
    decide,
    draft_to_post_data,
    get_approval,
    get_draft,
    parse_post_action,
    parse_responsible_callback,
)

logger = logging.getLogger(__name__)


def _responsible_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """Кнопки работы ответственного с конкретным постом."""
    keyboard = [
        [InlineKeyboardButton("✅ Согласовать", callback_data=f"approvepost_{draft_id}")],
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"declinepost_{draft_id}")],
        [InlineKeyboardButton("📄 Показать пост", callback_data=f"viewpost_{draft_id}")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _view_only_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    """Кнопки после принятия решения: остаётся только просмотр."""
    keyboard = [[InlineKeyboardButton("📄 Показать пост", callback_data=f"viewpost_{draft_id}")]]
    return InlineKeyboardMarkup(keyboard)


def _build_post_message(draft, heading: str) -> str:
    """
    Итоговый текст поста из БД + автор и id поста.
    Заголовок/поля экранируются в build_post_summary, id и user_id — цифры.
    """
    text = build_post_summary(draft_to_post_data(draft), heading=heading)
    text += f"*ID поста:* {draft.id}\n*Автор поста:* {draft.user_id}"
    return text


async def _send_post_message(bot, chat_id: int, draft, heading: str, reply_markup=None) -> None:
    """Отправляет пост ответственному: фото с подписью либо текстом."""
    text = _build_post_message(draft, heading)
    if draft.image:
        # Лимит подписи к фото — 1024 символа: при переполнении отправляем раздельно
        if len(text) <= 1000:
            await bot.send_photo(
                chat_id=chat_id,
                photo=draft.image,
                caption=text,
                parse_mode='MarkdownV2',
                reply_markup=reply_markup,
            )
            return
        await bot.send_photo(chat_id=chat_id, photo=draft.image)
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode='MarkdownV2',
        reply_markup=reply_markup,
    )


def _alert_text(text: str) -> str:
    """Callback answer ограничен 200 символами."""
    return text if len(text) <= 190 else text[:187] + '...'


async def _answer_safely(query, text: str = None, show_alert: bool = False) -> None:
    """Отвечает на callback; повторный ответ (после ошибки) не роняет обработку."""
    try:
        if text is None:
            await query.answer()
        else:
            await query.answer(_alert_text(text), show_alert=show_alert)
    except Exception:
        logger.exception("Не удалось ответить на CallbackQuery")


async def handle_responsible_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Назначение ответственного за пост в review-чате.
    callback_data: responsible_<draft_id>_<telegram_id>.
    Автор и ответственный берутся из БД — независимо от того, кто нажал кнопку.
    Повторное нажатие не создаёт дублей: назначение ровно одно на пост.
    """
    query = update.callback_query

    parsed = parse_responsible_callback(query.data)
    if parsed is None:
        await _answer_safely(query, "Неверный формат данных кнопки.", show_alert=True)
        return
    draft_id, telegram_id = parsed

    session: Session = SessionLocal()
    try:
        person = session.query(ResponsiblePerson).filter_by(telegram_id=telegram_id).first()
        if person is None:
            await _answer_safely(query, "Ответственный не найден.", show_alert=True)
            return

        approval, outcome = assign_responsible(session, draft_id, telegram_id)

        if outcome == 'no_draft':
            await _answer_safely(query, "Пост не найден (возможно, удалён).", show_alert=True)
            return

        if outcome == 'same':
            # Повторное нажатие той же кнопки — консистентно, без дублей
            await _answer_safely(query, "Этот ответственный уже назначен на пост.", show_alert=True)
            return

        if outcome == 'other':
            current = session.query(ResponsiblePerson).filter_by(
                telegram_id=approval.responsible_telegram_id
            ).first()
            current_name = current.name if current else str(approval.responsible_telegram_id)
            await _answer_safely(query, f"Уже назначен ответственный: {current_name}", show_alert=True)
            return

        # outcome == 'created': уведомляем ответственного данными ИЗ БД
        draft = get_draft(session, draft_id)
        safe_name = escape_markdown(person.name)

        await _answer_safely(query, "Ответственный назначен ✓")

        await _send_post_message(
            context.bot,
            chat_id=telegram_id,
            draft=draft,
            heading="📌 *Вам назначен пост:*",
            reply_markup=_responsible_keyboard(draft_id),
        )

        # Снимаем клавиатуру выбора (пустая inline-клавиатура удаляет кнопки)
        await query.edit_message_text(
            f"Ответственный назначен: {safe_name}",
            parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([]),
        )
        logger.info(f"Пост #{draft_id}: назначен ответственный {telegram_id}.")
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка при назначении ответственного (пост #{draft_id}): {e}")
        await _answer_safely(query, f"Ошибка при назначении: {e}", show_alert=True)
    finally:
        session.close()


async def handle_view_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Просмотр поста ответственным. callback_data: viewpost_<draft_id>.
    Доступно только назначенному ответственному.
    """
    query = update.callback_query

    parsed = parse_post_action(query.data)
    if parsed is None:
        await _answer_safely(query, "Неверный формат данных кнопки.", show_alert=True)
        return
    _, draft_id = parsed

    session: Session = SessionLocal()
    try:
        approval = get_approval(session, draft_id)
        if approval is None:
            await _answer_safely(query, "Пост не назначался ответственному.", show_alert=True)
            return
        if approval.responsible_telegram_id != query.from_user.id:
            await _answer_safely(
                query, "Просмотр доступен только назначенному ответственному.", show_alert=True
            )
            return

        draft = get_draft(session, draft_id)
        if draft is None:
            await _answer_safely(query, "Пост не найден (возможно, удалён).", show_alert=True)
            return

        await _answer_safely(query)
        # Отдельным сообщением, чтобы не ломать кнопочное сообщение
        await _send_post_message(
            context.bot,
            chat_id=query.effective_chat.id,
            draft=draft,
            heading="📋 *Пост для согласования:*",
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка при просмотре поста #{draft_id}: {e}")
        await _answer_safely(query, f"Ошибка: {e}", show_alert=True)
    finally:
        session.close()


async def _handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE, new_status: str) -> None:
    """Фиксирует решение ответственного и обновляет кнопки в его сообщении."""
    query = update.callback_query

    parsed = parse_post_action(query.data)
    if parsed is None:
        await _answer_safely(query, "Неверный формат данных кнопки.", show_alert=True)
        return
    _, draft_id = parsed

    session: Session = SessionLocal()
    try:
        approval, outcome = decide(session, draft_id, query.from_user.id, new_status)

        if outcome == 'bad_status':
            await _answer_safely(query, "Недопустимое действие.", show_alert=True)
            return
        if outcome == 'no_approval':
            await _answer_safely(query, "Пост не назначался ответственному.", show_alert=True)
            return
        if outcome == 'forbidden':
            await _answer_safely(
                query, "Действие доступно только назначенному ответственному.", show_alert=True
            )
            return
        if outcome == 'already':
            word = 'согласован' if approval.status == STATUS_APPROVED else 'отклонён'
            await _answer_safely(
                query, f"Решение уже принято: пост {word}.", show_alert=True
            )
            return

        # outcome == 'ok'
        label = "✅ Пост согласован." if new_status == STATUS_APPROVED else "❌ Пост отклонён."
        await _answer_safely(query, "Решение принято ✓")

        markup = _view_only_keyboard(draft_id)
        if query.message is not None and query.message.photo:
            # Уведомление было отправлено как фото — правим подпись
            await query.edit_message_caption(caption=label, reply_markup=markup)
        else:
            await query.edit_message_text(text=label, reply_markup=markup)
        logger.info(
            f"Пост #{draft_id}: статус -> {new_status} "
            f"(ответственный {query.from_user.id})."
        )
    except Exception as e:
        session.rollback()
        logger.error(f"Ошибка при решении поста #{draft_id}: {e}")
        await _answer_safely(query, f"Ошибка: {e}", show_alert=True)
    finally:
        session.close()


async def handle_approve_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Согласование поста ответственным. callback_data: approvepost_<draft_id>."""
    await _handle_decision(update, context, STATUS_APPROVED)


async def handle_decline_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отклонение поста ответственным. callback_data: declinepost_<draft_id>."""
    await _handle_decision(update, context, STATUS_DECLINED)


def approval_handlers() -> list:
    """
    Возвращает обработчики согласования (регистрируются ДО ConversationHandler
    создания поста; паттерны не пересекаются с его callback_data).
    """
    return [
        CallbackQueryHandler(handle_approve_post, pattern=r'^approvepost_\d+$'),
        CallbackQueryHandler(handle_decline_post, pattern=r'^declinepost_\d+$'),
        CallbackQueryHandler(handle_view_post, pattern=r'^viewpost_\d+$'),
        CallbackQueryHandler(handle_responsible_selection, pattern=r'^responsible_\d+_\d+$'),
    ]
