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
  оно не ломает сценарий и не должно пугать пользователя;
- ОЖИДАЕМЫЕ доменные ошибки (валидация названия, недоступный шаблон,
  кирпичик, черновик анонса) — не сбой бота, а рассинхронизация экрана с
  данными (устаревшая кнопка, восстановленная после рестарта фаза с
  потерянным ключом). Для них вместо «внутренней ошибки» показывается
  понятное сообщение и открывается ближайший живой родительский экран
  (главное меню) — общий fallback на все разделы.
"""

import logging
import time

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from utils.ui_texts import tr

logger = logging.getLogger(__name__)

# Минимальный интервал между уведомлениями об ошибке в одном чате
ERROR_NOTICE_INTERVAL_SECONDS = 5.0

# Тексты живут в каталоге config/ui_texts.yaml (U4a, дефект №10): владелец
# правит формулировки без Python-кода. Здесь читаются в момент отправки.
ERROR_NOTICE_KEY = "errors.internal"

# Понятный текст для ожидаемых доменных ошибок: экран рассинхронизирован,
# данные не потеряны, пользователь возвращается в меню и пробует снова.
DOMAIN_NOTICE_KEY = "errors.domain"

# chat_id -> время последнего уведомления (time.monotonic)
_last_error_notice: dict[int, float] = {}

# Кэш классов ожидаемых доменных ошибок (собирается лениво: импорт доменных
# модулей не должен удорожать импорт errors.py и не создаёт циклов).
_expected_error_types: tuple[type[BaseException], ...] | None = None


def expected_domain_errors() -> tuple[type[BaseException], ...]:
    """
    Классы доменных ошибок, которые не являются сбоем бота.

    Это ошибки валидации и прав из хранилищ предметной области: они
    возникают, когда экран опирается на устаревшие/неполные данные
    (например, восстановленная фаза без нужного ключа). Показывать на них
    «внутренняя ошибка» нельзя — пользователю нужен понятный fallback.

    Список собирается один раз; недоступность любого модуля (урезанная
    сборка, тестовый стенд) просто сужает классификацию, а не ломает
    обработчик ошибок.
    """
    global _expected_error_types
    if _expected_error_types is not None:
        return _expected_error_types

    types: list[type[BaseException]] = []

    def add(*candidates: object) -> None:
        for candidate in candidates:
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                types.append(candidate)

    try:  # места: названия
        from place_store import PlaceNameError

        add(PlaceNameError)
    except Exception:  # noqa: BLE001 — классификация не должна падать
        logger.debug("Классификация ошибок мест недоступна", exc_info=True)
    try:  # шаблоны
        from template_compiler import TemplateCompileError
        from template_store import (
            TemplateActorError,
            TemplateError,
            TemplatePermissionError,
        )
        from template_types import TemplateValidationError

        add(
            TemplateError,
            TemplatePermissionError,
            TemplateActorError,
            TemplateValidationError,
            TemplateCompileError,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Классификация ошибок шаблонов недоступна", exc_info=True)
    try:  # кирпичики и предложения
        from brick_proposal_store import (
            ProposalActorError,
            ProposalError,
            ProposalPermissionError,
        )
        from brick_store import BrickActorError, BrickError, BrickPermissionError

        add(
            BrickError,
            BrickPermissionError,
            BrickActorError,
            ProposalError,
            ProposalPermissionError,
            ProposalActorError,
        )
    except Exception:  # noqa: BLE001
        logger.debug("Классификация ошибок кирпичиков недоступна", exc_info=True)
    try:  # черновики анонсов
        from handlers.announcement_wizard import (
            AnnouncementActorError,
            AnnouncementError,
        )

        add(AnnouncementError, AnnouncementActorError)
    except Exception:  # noqa: BLE001
        logger.debug("Классификация ошибок анонсов недоступна", exc_info=True)
    try:  # ссылки «создать анонс площадки» (U4b)
        from place_invite_store import (
            PlaceInviteError,
            PlaceInvitePermissionError,
        )

        # Оба исключения слоя ссылок — доменные: гонка прав между проверкой
        # в обработчике и повторной проверкой при отрисовке, удалённая
        # площадка, редкая одновременная запись. Это не сбой кода, поэтому
        # пользователю нужен понятный fallback, а не «внутренняя ошибка».
        add(PlaceInviteError, PlaceInvitePermissionError)
    except Exception:  # noqa: BLE001
        logger.debug("Классификация ошибок ссылок площадки недоступна", exc_info=True)

    _expected_error_types = tuple(types)
    return _expected_error_types


def error_notice_text() -> str:
    """Текст уведомления о внутренней ошибке (из каталога)."""
    return tr(ERROR_NOTICE_KEY)


def domain_notice_text() -> str:
    """Текст fallback'а для ожидаемых доменных ошибок (из каталога)."""
    return tr(DOMAIN_NOTICE_KEY)


def is_expected_domain_error(error: BaseException | None) -> bool:
    """True — ошибка ожидаемая (данные/права), а не сбой кода."""
    if error is None:
        return False
    types = expected_domain_errors()
    return bool(types) and isinstance(error, types)


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
        await message.reply_text(error_notice_text())
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление об ошибке")
        return False


async def _answer_query(update: Update, text: str) -> bool:
    """Отвечает на callback (снимает «спиннер»), ошибки Telegram не критичны."""
    query = update.callback_query
    if query is None:
        return False
    try:
        await query.answer(text, show_alert=True)
        return True
    except Exception:  # noqa: BLE001 — повторный answer не должен ломать fallback
        logger.info("Не удалось ответить на callback в fallback: %s", text)
        return False


async def _render_fallback_parent(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """
    Открывает ближайший живой родительский экран — главное меню.

    Меню перерисовывает существующий экран и сбрасывает незавершённые
    «сломанные» сессии разделов, поэтому следующая навигация снова рабочая.
    Любой сбой самого fallback'а не должен порождать вторую ошибку.
    """
    try:
        from handlers.main_menu import render_main_menu

        await render_main_menu(update, context)
        return True
    except Exception:  # noqa: BLE001 — fallback best-effort
        logger.exception("Не удалось открыть fallback-экран после доменной ошибки")
        return False


async def _handle_expected_error(
    update: object, context: ContextTypes.DEFAULT_TYPE, error: BaseException
) -> bool:
    """
    Понятный fallback вместо «внутренней ошибки» для доменных ошибок.

    Возвращает True, если ошибка обработана здесь (штатное уведомление
    пользователю уже показано и не нужно).
    """
    if not is_expected_domain_error(error):
        return False
    logger.warning("Ожидаемая доменная ошибка при обработке экрана: %s", error)
    if not isinstance(update, Update):
        return True
    await _answer_query(update, domain_notice_text())
    await _render_fallback_parent(update, context)
    return True


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

    if error is not None and await _handle_expected_error(update, context, error):
        return

    logger.error("Exception while handling an update:", exc_info=error)

    if isinstance(update, Update):
        message = update.effective_message
        if message is not None:
            await _send_error_notice(message, message.chat_id)
