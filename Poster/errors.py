# errors.py
"""
Глобальная обработка ошибок обработки обновлений (PTB error handler).

Вынесена из bot.py отдельным модулем, чтобы логика была тестируемой:
bot.py при импорте настраивает логирование и запускает бота, импорт
модуля в тестах недопустим. Само настройка логирования выполняется
только в bot.main().

Поведение:
- любая ошибка обработки — в лог с traceback;
- пользователю отправляется сообщение «внутренняя ошибка», но не чаще
  одного раза в ERROR_NOTICE_INTERVAL_SECONDS на чат (защита от спама);
- «message is not modified» — штатное следствие повторного нажатия кнопки:
  оно не ломает сценарий и не должно пугать пользователя.
"""

import logging
import time

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Минимальный интервал между уведомлениями об ошибке в одном чате
ERROR_NOTICE_INTERVAL_SECONDS = 5.0

ERROR_NOTICE_TEXT = (
    "⚠️ Произошла внутренняя ошибка. Попробуйте повторить действие чуть позже."
)

# chat_id -> время последнего уведомления (time.monotonic)
_last_error_notice: dict[int, float] = {}


async def _send_error_notice(message, chat_id: int) -> bool:
    """
    Уведомляет пользователя об ошибке с троттлингом на чат.

    Возвращает True, если сообщение отправлено (или произошла первая
    попытка), False — если интервал ещё не истёк либо отправка не удалась.
    """
    now = time.monotonic()
    if now - _last_error_notice.get(chat_id, 0.0) < ERROR_NOTICE_INTERVAL_SECONDS:
        return False
    _last_error_notice[chat_id] = now
    try:
        await message.reply_text(ERROR_NOTICE_TEXT)
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление об ошибке")
        return False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик исключений PTB: лог + ответ пользователю.

    Раньше ошибки попадали только в лог — при исключении бот «молчал»,
    и пользователь не понимал, что произошло.
    """
    error = context.error

    # Повторное нажатие кнопки на уже отредактированном сообщении —
    # штатная ситуация, а не сбой обработки (текст PTB — «Message is not…»,
    # сравниваем без учёта регистра)
    if (
        isinstance(error, BadRequest)
        and "message is not modified" in str(error).lower()
    ):
        logger.info("Кнопка нажата повторно (message is not modified) — пропуск")
        return

    logger.error("Exception while handling an update:", exc_info=error)

    if isinstance(update, Update):
        message = update.effective_message
        if message is not None:
            await _send_error_notice(message, message.chat_id)
