# tests/test_stage6.py
"""
Тесты Stage 6 (однопользовательский конструктор поста):

- полный цикл: создание → все поля → превью → «Готово» →
  пользователь получает готовый пост в ЛИЧНОМ чате;
- повторное нажатие «Готово» и пустой пост не дублируют отправку;
- сбой отправки не теряет введённые данные;
- в проекте больше нет активных путей отправки поста
  в review/publication chat (поиск источников + маршрутизация кнопок);
- callback'и диалога и глобальных кнопок не перехватывают друг друга.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

POSTER_DIR = Path(__file__).resolve().parent.parent

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    from telegram.ext import CallbackQueryHandler, ConversationHandler

    import bot as bot_module
    import config
    from handlers.post_creation import (
        finish_post,
        handle_callback_query,
        handle_message,
        handle_photo,
    )
    from utils.formatter import escape_markdown

    HANDLERS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HANDLERS_IMPORT_ERROR = str(e)


class RecordingBot:
    """Заглушка бота: записывает все вызовы отправки (или падает)."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def _record(self, kind, kwargs):
        if self.fail:
            raise RuntimeError("telegram api down")
        self.calls.append({"type": kind, **kwargs})

    async def send_message(self, **kwargs):
        self._record("message", kwargs)

    async def send_photo(self, **kwargs):
        self._record("photo", kwargs)

    async def send_media_group(self, **kwargs):
        self._record("media_group", kwargs)


def make_message_update(text=None, *, photo=None):
    """Update, имитирующий сообщение пользователя (текст или фото)."""
    message = SimpleNamespace(
        text=text,
        photo=photo,
        media_group_id=None,
        reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_message=message,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=777),
    )


def make_callback_update(data):
    """Update, имитирующий нажатие inline-кнопки."""
    query = SimpleNamespace(
        data=data, answer=AsyncMock(), from_user=SimpleNamespace(id=7)
    )
    message = SimpleNamespace(text=None, photo=None, reply_text=AsyncMock())
    return SimpleNamespace(
        callback_query=query,
        effective_message=message,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=777),
    )


# Все 8 текстовых полей по порядку шагов (9-й шаг — изображение)
FULL_TEXT_VALUES = [
    "Концерт рок-группы",
    "25.12.2026",
    "19:00",
    "21:30",
    "Большой зал",
    "Ждём всех!",
    "@rock_contact",
    "https://example.com/hall",
]


def drive_wizard(user_data, bot):
    """Проходит все текстовые шаги и добавляет фото — до состояния превью."""
    user_data.setdefault("current_step", 0)  # как делает start_post_creation
    context = SimpleNamespace(user_data=user_data, bot=bot)
    for value in FULL_TEXT_VALUES:
        asyncio.run(handle_message(make_message_update(value), context))
    photo_update = make_message_update(photo=[SimpleNamespace(file_id="photo-1")])
    asyncio.run(handle_photo(photo_update, context))
    return context


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class FullCycleTests(unittest.TestCase):
    """Создание → превью → «Готово» → готовый пост у пользователя."""

    def test_all_fields_then_finish_sends_final_post(self):
        bot = RecordingBot()
        user_data = {}
        context = drive_wizard(user_data, bot)

        # На превью отправлен ровно один пост (фото с подписью)
        self.assertEqual(len(bot.calls), 1)
        preview = bot.calls[0]
        self.assertEqual(preview["chat_id"], 777)
        for value in FULL_TEXT_VALUES:
            self.assertIn(escape_markdown(value), preview["caption"])

        # «Готово» → финальная отправка + подтверждение, диалог закрыт
        finish_update = make_callback_update("finish")
        result = asyncio.run(handle_callback_query(finish_update, context))

        from telegram.ext import ConversationHandler

        self.assertEqual(result, ConversationHandler.END)
        self.assertEqual(len(bot.calls), 2)
        final = bot.calls[1]
        # ПРЕВЬЮ == ГОТОВЫЙ ПОСТ: дословно одинаковый текст
        self.assertEqual(preview["caption"], final["caption"])
        # у готового поста нет кнопок действий
        self.assertIsNone(final.get("reply_markup"))

        # Все отправки — только в личный чат пользователя
        self.assertEqual({c["chat_id"] for c in bot.calls}, {777})

        # подтверждение «Пост готов.» отправлено пользователю
        confirmation = finish_update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Пост готов", confirmation)

        # введённые данные очищены — цикл завершён
        for key in ("title", "date", "time_start", "text", "photos", "image"):
            self.assertNotIn(key, user_data)

    def test_double_finish_press_sends_only_once(self):
        bot = RecordingBot()
        user_data = {}
        context = drive_wizard(user_data, bot)

        self.assertTrue(asyncio.run(finish_post(make_message_update(""), context)))
        sends_after_first = len(bot.calls)

        # Повторное нажатие: данные уже очищены — отправки нет
        self.assertFalse(asyncio.run(finish_post(make_message_update(""), context)))
        self.assertEqual(len(bot.calls), sends_after_first)

    def test_empty_post_finish_rejected(self):
        bot = RecordingBot()
        context = SimpleNamespace(user_data={}, bot=bot)
        update = make_message_update("")

        self.assertFalse(asyncio.run(finish_post(update, context)))
        self.assertEqual(bot.calls, [])
        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Пост пуст", reply)

    def test_send_failure_keeps_data_and_reports(self):
        bot = RecordingBot(fail=True)
        user_data = {"title": "Концерт", "text": "Ждём всех!"}
        context = SimpleNamespace(user_data=user_data, bot=bot)
        update = make_message_update("")

        self.assertFalse(asyncio.run(finish_post(update, context)))
        # Данные не потеряны, пользователь получил понятный ответ
        self.assertEqual(user_data.get("title"), "Концерт")
        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Не удалось отправить", reply)


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class NoWorkflowTests(unittest.TestCase):
    """В проекте не должно оставаться путей отправки в review/publication chat."""

    FORBIDDEN_TOKENS = [
        "REVIEW_CHAT_ID",
        "PUBLICATION_CHAT_ID",
        "ADMIN_IDS",
        "ResponsiblePerson",
        "PostApproval",
        "approvepost_",
        "declinepost_",
        "publishpost_",
        "viewpost_",
        "responsible_",
        "send_for_approval",
        "add_responsible",
        "remove_responsible",
        "handlers.approval",
        "handlers.admin",
        "notify_responsible",
        "edit_block_reason",
        "reset_declined",
        "assign_responsible",
    ]

    def test_no_workflow_symbols_in_source(self):
        offenders = []
        for path in sorted(POSTER_DIR.rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            content = path.read_text(encoding="utf-8")
            for token in self.FORBIDDEN_TOKENS:
                if token in content:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [], f"Найдены следы workflow: {offenders}")

    def test_config_has_no_workflow_vars(self):
        for name in ("REVIEW_CHAT_ID", "PUBLICATION_CHAT_ID", "ADMIN_IDS"):
            self.assertFalse(hasattr(config, name), f"config.{name} должен быть удалён")

    def test_workflow_callbacks_not_routed_to_any_handler(self):
        from telegram import CallbackQuery, Update, User

        application = bot_module.build_application()
        group = application.handlers[0]
        for data in (
            "approvepost_1",
            "declinepost_1",
            "publishpost_1",
            "responsible_1_2",
        ):
            update = Update(
                update_id=1,
                callback_query=CallbackQuery(
                    id="1",
                    from_user=User(id=1, first_name="Тест", is_bot=False),
                    chat_instance="c",
                    data=data,
                ),
            )
            matched = []
            for handler in group:
                if isinstance(handler, ConversationHandler):
                    # без активного состояния диалог берёт только entry-кнопки:
                    # их паттерны не должны отвечать на workflow-callback'и
                    for entry in handler.entry_points:
                        if isinstance(entry, CallbackQueryHandler) and entry.pattern:
                            if entry.pattern.search(data):
                                matched.append(handler)
                    for handlers in handler.states.values():
                        for h in handlers:
                            if isinstance(h, CallbackQueryHandler) and h.pattern:
                                if h.pattern.search(data):
                                    matched.append(handler)
                elif handler.check_update(update):
                    matched.append(handler)
            self.assertEqual(
                matched, [], f"callback '{data}' никому не должен отвечать"
            )


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class HandlerRoutingTests(unittest.TestCase):
    """Роутинг callback'ов: каждый обслуживается ровно одним местом."""

    DIALOG_CALLBACKS = [
        "skip",
        "save_draft",
        "finish",
        "edit_post",
        "media_done",
        "edit_title",
        "cancel_edit",
        "editdraft_1",
    ]
    GLOBAL_CALLBACKS = ["main_menu", "delete_1", "draftpage_15"]

    @staticmethod
    def _parts(application):
        group = application.handlers[0]
        conv = next(h for h in group if isinstance(h, ConversationHandler))
        others = [
            h for h in group if isinstance(h, CallbackQueryHandler) and h is not conv
        ]
        conv_patterns = []
        for handlers in list(conv.states.values()) + [list(conv.entry_points)]:
            for h in handlers:
                if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
                    conv_patterns.append(h.pattern)
        return conv_patterns, others

    def test_dialog_callbacks_not_claimed_outside_dialog(self):
        application = bot_module.build_application()
        _, others = self._parts(application)
        for data in self.DIALOG_CALLBACKS:
            hits = [h for h in others if h.pattern and h.pattern.search(data)]
            self.assertEqual(
                hits, [], f"callback '{data}' перехватывается вне диалога: {hits}"
            )

    def test_global_callbacks_not_claimed_by_dialog_and_served_once(self):
        application = bot_module.build_application()
        conv_patterns, others = self._parts(application)
        for data in self.GLOBAL_CALLBACKS:
            for pat in conv_patterns:
                self.assertIsNone(
                    pat.search(data),
                    f"callback '{data}' перехватывается диалогом: {pat.pattern}",
                )
            hits = [h for h in others if h.pattern and h.pattern.search(data)]
            self.assertEqual(
                len(hits),
                1,
                f"callback '{data}' должен обслуживаться одним обработчиком",
            )


if __name__ == "__main__":
    unittest.main()
