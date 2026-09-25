# tests/test_stage6.py
"""
Тесты Stage 6 (однопользовательский конструктор поста):

- полный цикл: создание → все поля → превью → «Готово» →
  пользователь получает готовый пост в ЛИЧНОМ чате;
- повторное нажатие «Готово» и пустой пост не дублируют отправку;
- сбой отправки не теряет введённые данные;
- в проекте больше нет активных путей отправки поста
  в review/publication chat (поиск источников + маршрутизация кнопок);
- callback'и диалога и глобальных кнопок не перехватывают друг друга;
- пропуск полей: пустые поля не видны в сводке, заглушек нет;
- редактирование: русские названия полей в меню и промпте;
- черновики: незавершённый пост сохраняется при выходах, открытие
  другого черновика не теряет правки, обновление без копий;
- фото: подсказка вне шага изображения, защита поля от фото;
- неактуальные кнопки превью в режиме редактирования: короткая
  подсказка вместо тишины/ошибки, состояние правки сохраняется;
- команды /start, /help, /drafts во время создания сохраняют
  незавершённый пост и завершают диалог.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import asyncio
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

POSTER_DIR = Path(__file__).resolve().parent.parent

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    from telegram import (
        Bot,
        CallbackQuery,
        Chat,
        Message,
        MessageEntity,
        Update,
        User,
    )
    from telegram.ext import CallbackQueryHandler, ConversationHandler

    import bot as bot_module
    import config
    from base import Base
    from handlers.post_creation import (
        EDIT_FIELD,
        POST_CREATION,
        POST_STEPS,
        cancel_creation,
        finish_post,
        get_post_actions_keyboard,
        handle_callback_query,
        handle_edit,
        handle_message,
        handle_photo,
        handle_stale_preview_action,
        help_during_creation,
        process_edit,
        start_during_creation,
        start_edit_draft,
        start_post_creation,
        view_drafts_and_exit,
    )
    from models import Draft
    from utils.formatter import escape_markdown

    # Bot нужен только для check_update CommandHandler (сравнивает имя бота);
    # сеть не используется — username подставляется вместо getMe
    ROUTING_TEST_BOT = Bot("123456:TEST-TOKEN")
    ROUTING_TEST_BOT._bot_user = User(
        id=1, is_bot=True, first_name="Тест", username="routing_test"
    )

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


def make_message_update(text=None, *, photo=None, media_group_id=None):
    """Update, имитирующий сообщение пользователя (текст или фото)."""
    message = SimpleNamespace(
        text=text,
        photo=photo,
        media_group_id=media_group_id,
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
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        from_user=SimpleNamespace(id=7),
    )
    message = SimpleNamespace(text=None, photo=None, reply_text=AsyncMock())
    return SimpleNamespace(
        callback_query=query,
        effective_message=message,
        effective_user=SimpleNamespace(id=7),
        effective_chat=SimpleNamespace(id=777),
    )


def make_text_update(text):
    """
    Настоящий Update с сообщением (у команд — bot_command-entity).

    Нужен для проверки фильтров ConversationHandler: filters.COMMAND решает
    по entities, поэтому заглушке из SimpleNamespace их не подставить.
    """
    entities = []
    if text.startswith("/"):
        entities = [
            MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))
        ]
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=Chat(id=777, type="private"),
        from_user=User(id=7, first_name="Тест", is_bot=False),
        text=text,
        entities=entities,
    )
    message.set_bot(ROUTING_TEST_BOT)
    return Update(update_id=1, message=message)


def make_callback_route_update(data):
    """Настоящий Update с CallbackQuery — для проверки паттернов диалога."""
    user = User(id=7, first_name="Тест", is_bot=False)
    message = Message(
        message_id=2,
        date=datetime(2026, 1, 1),
        chat=Chat(id=777, type="private"),
        from_user=user,
    )
    message.set_bot(ROUTING_TEST_BOT)
    return Update(
        update_id=2,
        callback_query=CallbackQuery(
            id="1",
            chat_instance="c",
            from_user=user,
            message=message,
            data=data,
        ),
    )


def resolve_dialog_handler(conv, state, update, chat_id=777, user_id=7):
    """
    Хендлер ConversationHandler, обслуживающий update в заданном состоянии.

    Состояние задаётся напрямую во внутреннем хранилище PTB (ключ
    (chat, user)): публичного способа «поставить активный диалог в
    состояние» без реального бота нет.
    """
    key = (chat_id, user_id)
    conv._conversations[key] = state
    try:
        result = conv.check_update(update)
    finally:
        conv._conversations.pop(key, None)
    if not result:
        return None
    if isinstance(result, tuple):
        # PTB 22.x возвращает (state, key, handler, check_result) — берём хендлер
        return next((item for item in result if hasattr(item, "callback")), None)
    return result


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


def make_session_factory():
    """Сессия над БД в памяти (отдельный engine, к post_bot.db не подключается)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


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
        # сводка с русскими названиями полей
        self.assertIn("*Заголовок*:", preview["caption"])

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
class PartialPostTests(unittest.TestCase):
    """Пропуск полей: пустые поля не показываются в превью, заглушек нет."""

    def test_skip_fields_preview_shows_only_filled(self):
        bot = RecordingBot()
        user_data = {"current_step": 0}
        context = SimpleNamespace(user_data=user_data, bot=bot)

        # Заголовок — ввод, дата — кнопка «Пропустить»
        asyncio.run(handle_message(make_message_update("Концерт"), context))
        self.assertEqual(user_data["title"], "Концерт")
        asyncio.run(handle_callback_query(make_callback_update("skip"), context))
        self.assertIsNone(user_data["date"])  # пропуск = пустое поле, не строка

        # Оставшиеся 6 текстовых полей — пропуск словом
        for _ in range(6):
            asyncio.run(handle_message(make_message_update("Пропустить"), context))
        self.assertIsNone(user_data["place_url"])
        # 9-й шаг (изображение) — тоже пропуск, дальше превью
        asyncio.run(handle_message(make_message_update("Пропустить"), context))

        self.assertEqual(len(bot.calls), 1)
        summary = bot.calls[0].get("caption") or bot.calls[0]["text"]
        self.assertIn("• *Заголовок*: Концерт", summary)
        self.assertNotIn("• *Дата*:", summary)  # пропущенное поле не показано
        self.assertNotIn("Не указано", summary)  # нигде — ни как значение, ни как текст

    def test_partial_post_can_be_finished(self):
        # Пустой пост блокируется, частично заполненный — отправляется
        bot = RecordingBot()
        user_data = {"current_step": 0}
        context = SimpleNamespace(user_data=user_data, bot=bot)

        asyncio.run(handle_message(make_message_update("Только заголовок"), context))
        for _ in range(8):
            asyncio.run(handle_callback_query(make_callback_update("skip"), context))

        self.assertTrue(asyncio.run(finish_post(make_message_update(""), context)))
        # последняя отправка — готовый пост (превью было первым)
        self.assertEqual(len(bot.calls), 2)
        summary = bot.calls[-1].get("text") or bot.calls[-1].get("caption")
        self.assertIn("Только заголовок", summary)
        self.assertNotIn("Не указано", summary)


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class EditFlowTests(unittest.TestCase):
    """Редактирование: меню и промпт с человекочитаемыми названиями полей."""

    @staticmethod
    def _to_preview():
        return {"current_step": len(POST_STEPS)}, RecordingBot()

    def test_edit_menu_lists_all_fields_in_russian(self):
        user_data, bot = self._to_preview()
        context = SimpleNamespace(user_data=user_data, bot=bot)
        update = make_callback_update("edit_post")

        asyncio.run(handle_callback_query(update, context))

        reply = update.effective_message.reply_text.await_args
        self.assertIn("Выберите поле", reply.args[0])
        labels = [
            button.text
            for row in reply.kwargs["reply_markup"].inline_keyboard
            for button in row
        ]
        expected = [step["label"] for step in POST_STEPS]
        for label in expected:
            self.assertIn(label, labels)

    def test_edit_prompt_uses_russian_field_name(self):
        user_data, bot = self._to_preview()
        context = SimpleNamespace(user_data=user_data, bot=bot)
        update = make_callback_update("edit_time_start")

        result = asyncio.run(handle_edit(update, context))

        prompt = update.callback_query.edit_message_text.await_args.args[0]
        self.assertIn("«Время начала»", prompt)
        self.assertNotIn("time_start", prompt)  # внутренний ключ не показывается
        self.assertEqual(result, EDIT_FIELD)  # остаёмся в режиме редактирования


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class DraftLifecycleTests(unittest.TestCase):
    """Черновики: сохранение при выходах, открытие, обновление без копий."""

    def setUp(self):
        import handlers.drafts as drafts_module
        import handlers.post_creation as post_creation_module

        self.drafts_module = drafts_module
        self.post_creation_module = post_creation_module
        self._orig_session_local = (
            post_creation_module.SessionLocal,
            drafts_module.SessionLocal,
        )
        self.session_factory, self.engine = make_session_factory()
        post_creation_module.SessionLocal = self.session_factory
        drafts_module.SessionLocal = self.session_factory

    def tearDown(self):
        self.post_creation_module.SessionLocal = self._orig_session_local[0]
        self.drafts_module.SessionLocal = self._orig_session_local[1]
        self.engine.dispose()

    def _all_drafts(self):
        session = self.session_factory()
        try:
            return session.query(Draft).order_by(Draft.id).all()
        finally:
            session.close()

    @staticmethod
    def _replies(update):
        return [
            call.args[0] for call in update.effective_message.reply_text.await_args_list
        ]

    def test_drafts_button_saves_work_in_progress(self):
        user_data = {
            "current_step": 3,
            "title": "Недописанный пост",
            "date": "25.12.2026",
        }
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update("")

        result = asyncio.run(view_drafts_and_exit(update, context))

        self.assertEqual(result, ConversationHandler.END)
        drafts = self._all_drafts()
        self.assertEqual(len(drafts), 1, "незавершённый пост должен быть сохранён")
        self.assertEqual(drafts[0].user_id, 7)
        self.assertEqual(drafts[0].title, "Недописанный пост")
        self.assertEqual(drafts[0].date, "25.12.2026")
        # данные очищены, пользователь предупреждён
        self.assertNotIn("title", user_data)
        self.assertTrue(
            any("сохранён в черновики" in text for text in self._replies(update))
        )

    def test_cancel_saves_work_in_progress(self):
        user_data = {"current_step": 1, "title": "Отменённый пост"}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())

        result = asyncio.run(cancel_creation(make_message_update(""), context))

        self.assertEqual(result, ConversationHandler.END)
        drafts = self._all_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "Отменённый пост")
        self.assertNotIn("title", user_data)

    def test_restart_create_saves_previous_wip(self):
        user_data = {"current_step": 1, "title": "Прежний ВИП"}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())

        asyncio.run(start_post_creation(make_message_update(""), context))

        drafts = self._all_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "Прежний ВИП")
        # новый пост начат с чистого листа
        self.assertEqual(user_data.get("current_step"), 0)
        self.assertNotIn("title", user_data)

    def test_empty_wip_is_not_saved(self):
        user_data = {"current_step": 0}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update("")

        asyncio.run(view_drafts_and_exit(update, context))

        self.assertEqual(self._all_drafts(), [])
        self.assertFalse(
            any("сохранён" in text for text in self._replies(update)),
            "о пустом посте нечего сообщать",
        )

    def test_opening_another_draft_saves_previous_wip(self):
        session = self.session_factory()
        try:
            draft_a = Draft(user_id=7, title="Черновик A")
            draft_b = Draft(user_id=7, title="Черновик B")
            session.add_all([draft_a, draft_b])
            session.commit()
            a_id, b_id = draft_a.id, draft_b.id
        finally:
            session.close()

        # открыт черновик A, в нём есть несохранённые правки
        user_data = {
            "current_step": 9,
            "editing_draft_id": a_id,
            "title": "Правки в A",
            "date": None,
        }
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())

        result = asyncio.run(
            start_edit_draft(make_callback_update(f"editdraft_{b_id}"), context)
        )

        self.assertEqual(result, POST_CREATION)
        drafts = {d.id: d for d in self._all_drafts()}
        self.assertEqual(len(drafts), 2, "копий быть не должно")
        self.assertEqual(drafts[a_id].title, "Правки в A", "правки A потеряны")
        # user_data показывает черновик B
        self.assertEqual(user_data.get("title"), "Черновик B")
        self.assertEqual(user_data.get("editing_draft_id"), b_id)

    def test_legacy_placeholder_opens_as_empty_field(self):
        session = self.session_factory()
        try:
            draft = Draft(user_id=7, title="Не указано", date="15.09.2026")
            session.add(draft)
            session.commit()
            draft_id = draft.id
        finally:
            session.close()

        user_data = {}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())

        asyncio.run(
            start_edit_draft(make_callback_update(f"editdraft_{draft_id}"), context)
        )

        self.assertIsNone(user_data.get("title"), "«Не указано» = пустое поле")
        self.assertEqual(user_data.get("date"), "15.09.2026")

    def test_save_draft_clears_data_and_no_duplicate_on_exit(self):
        user_data = {"current_step": 9, "title": "Готовый черновик"}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())

        asyncio.run(handle_callback_query(make_callback_update("save_draft"), context))

        self.assertEqual(len(self._all_drafts()), 1)
        self.assertNotIn("title", user_data, "после сохранения данные чистятся")

        # повторный выход из диалога не создаёт второй (пустой) черновик
        asyncio.run(view_drafts_and_exit(make_message_update(""), context))
        self.assertEqual(len(self._all_drafts()), 1)


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class PhotoGuardTests(unittest.TestCase):
    """Фото вне шага — подсказка; фото при правке другого поля — не стирает."""

    def test_photo_outside_image_step_gives_hint(self):
        user_data = {"current_step": 0, "title": "Концерт"}  # шаг «Заголовок»
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update(photo=[SimpleNamespace(file_id="f1")])

        result = asyncio.run(handle_photo(update, context))

        self.assertEqual(result, POST_CREATION)
        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Изображение", reply)
        # поле не тронуто, шаг не продвинут, фото не потеряно молча
        self.assertEqual(user_data["title"], "Концерт")
        self.assertEqual(user_data["current_step"], 0)
        self.assertNotIn("photos", user_data)

    def test_photo_at_preview_points_to_edit_button(self):
        user_data = {"current_step": len(POST_STEPS)}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update(photo=[SimpleNamespace(file_id="f1")])

        asyncio.run(handle_photo(update, context))

        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Редактировать", reply)

    def test_photo_during_other_field_edit_keeps_value(self):
        user_data = {"edit_field": "title", "title": "Концерт"}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update(photo=[SimpleNamespace(file_id="f1")])

        result = asyncio.run(process_edit(update, context))

        self.assertEqual(result, EDIT_FIELD)
        self.assertEqual(user_data["title"], "Концерт", "поле не должно стираться")
        reply = update.effective_message.reply_text.await_args.args[0]
        self.assertIn("Изображение", reply)


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class AlbumRaceTests(unittest.TestCase):
    """
    A1 — гонка альбома: фотографии принимаются только пока активен шаг
    изображения; после «✅ Фото готово» запоздавшие фото не меняют данные
    поста, поэтому превью показывает ровно те фото, что уйдут в готовый пост.
    """

    @staticmethod
    def _image_step() -> int:
        return next(
            index for index, step in enumerate(POST_STEPS) if step["key"] == "image"
        )

    @staticmethod
    def _photo(file_id, media_group_id):
        return make_message_update(
            photo=[SimpleNamespace(file_id=file_id)], media_group_id=media_group_id
        )

    @staticmethod
    def _calls_of(bot, kind):
        return [call for call in bot.calls if call["type"] == kind]

    def test_album_collects_all_photos_until_done(self):
        # Нормальный альбом: все фото ДО «Фото готово» — в посте, шаг завершён
        bot = RecordingBot()
        user_data = {"current_step": self._image_step()}
        context = SimpleNamespace(user_data=user_data, bot=bot)

        first = self._photo("a-1", "mg-1")
        second = self._photo("a-2", "mg-1")
        third = self._photo("a-3", "mg-1")
        asyncio.run(handle_photo(first, context))
        asyncio.run(handle_photo(second, context))
        asyncio.run(handle_photo(third, context))

        self.assertEqual(user_data["photos"], ["a-1", "a-2", "a-3"])
        # подтверждение сбора — одно на альбом, без спама на каждое фото
        first_reply = first.effective_message.reply_text
        first_reply.assert_awaited_once()
        second.effective_message.reply_text.assert_not_awaited()
        third.effective_message.reply_text.assert_not_awaited()

        asyncio.run(handle_callback_query(make_callback_update("media_done"), context))

        self.assertEqual(user_data["current_step"], self._image_step() + 1)
        previews = self._calls_of(bot, "media_group")
        self.assertEqual(len(previews), 1, "превью должно быть отправлено")
        self.assertEqual(
            [media.media for media in previews[0]["media"]],
            ["a-1", "a-2", "a-3"],
        )

    def test_late_album_photo_after_done_does_not_change_post(self):
        # Гонка: пользователь нажал «Фото готово», превью уже показано,
        # а фото альбома ещё доходят — запоздавшие НЕ меняют данные поста.
        bot = RecordingBot()
        user_data = {"current_step": self._image_step(), "title": "Концерт"}
        context = SimpleNamespace(user_data=user_data, bot=bot)

        asyncio.run(handle_photo(self._photo("a-1", "mg-1"), context))
        asyncio.run(handle_callback_query(make_callback_update("media_done"), context))
        self.assertEqual(len(self._calls_of(bot, "photo")), 1, "превью с одним фото")

        late = self._photo("a-2", "mg-1")
        asyncio.run(handle_photo(late, context))
        late_again = self._photo("a-3", "mg-1")
        asyncio.run(handle_photo(late_again, context))

        self.assertEqual(
            user_data["photos"], ["a-1"], "запоздавшие фото не попадают в пост"
        )
        # пользователь не получает тишины: подсказка + дедуп по альбому
        late_reply = late.effective_message.reply_text
        late_reply.assert_awaited_once()
        self.assertIn("Редактировать", late_reply.await_args.args[0])
        late_again.effective_message.reply_text.assert_not_awaited()

        asyncio.run(handle_callback_query(make_callback_update("finish"), context))

        photo_calls = self._calls_of(bot, "photo")
        self.assertEqual(len(photo_calls), 2, "превью и готовый пост")
        preview, final = photo_calls
        self.assertEqual(preview["photo"], final["photo"], "preview == final: фото")
        self.assertEqual(
            preview["caption"], final["caption"], "preview == final: текст"
        )
        self.assertEqual(preview["parse_mode"], final["parse_mode"])


class _InMemoryDbMixin:
    """Патчит SessionLocal на БД в памяти: post_bot.db тестами не трогается."""

    def setUp(self):
        import handlers.drafts as drafts_module
        import handlers.post_creation as post_creation_module

        self.post_creation_module = post_creation_module
        self.drafts_module = drafts_module
        self._orig_session_local = (
            post_creation_module.SessionLocal,
            drafts_module.SessionLocal,
        )
        self.session_factory, self.engine = make_session_factory()
        post_creation_module.SessionLocal = self.session_factory
        drafts_module.SessionLocal = self.session_factory

    def tearDown(self):
        self.post_creation_module.SessionLocal = self._orig_session_local[0]
        self.drafts_module.SessionLocal = self._orig_session_local[1]
        self.engine.dispose()

    def _all_drafts(self):
        session = self.session_factory()
        try:
            return session.query(Draft).order_by(Draft.id).all()
        finally:
            session.close()


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class StalePreviewActionTests(_InMemoryDbMixin, unittest.TestCase):
    """Кнопки превью во время EDIT_FIELD: подсказка, состояние сохраняется."""

    @staticmethod
    def _in_edit_field():
        user_data = {
            "current_step": len(POST_STEPS),
            "edit_field": "title",
            "title": "Концерт",
        }
        return user_data, SimpleNamespace(user_data=user_data, bot=RecordingBot())

    def test_repeat_edit_press_keeps_dialog_and_state(self):
        user_data, context = self._in_edit_field()
        update = make_callback_update("edit_post")

        result = asyncio.run(handle_stale_preview_action(update, context))

        # диалог НЕ завершается, текущее редактирование не сбрасывается
        self.assertEqual(result, EDIT_FIELD)
        self.assertEqual(user_data["edit_field"], "title")
        self.assertEqual(user_data["title"], "Концерт")
        query = update.callback_query
        query.answer.assert_awaited()
        self.assertIn(
            "Сначала завершите редактирование поля",
            query.answer.await_args.args[0],
        )
        # «Неизвестное поле для редактирования.» больше не отправляется
        query.edit_message_text.assert_not_awaited()

    def test_finish_and_save_draft_get_hint_instead_of_silence(self):
        for data in ("finish", "save_draft"):
            with self.subTest(data=data):
                user_data, context = self._in_edit_field()
                update = make_callback_update(data)

                result = asyncio.run(handle_stale_preview_action(update, context))

                self.assertEqual(result, EDIT_FIELD)
                self.assertIn(
                    "Сначала завершите редактирование поля",
                    update.callback_query.answer.await_args.args[0],
                )
                # ни отправки поста, ни сохранения — и данные целы
                self.assertEqual(context.bot.calls, [])
                self.assertEqual(user_data["title"], "Концерт")
        self.assertEqual(self._all_drafts(), [], "черновик не должен создаваться")

    def test_stale_buttons_routed_to_hint_handler(self):
        application = bot_module.build_application()
        conv = next(
            h for h in application.handlers[0] if isinstance(h, ConversationHandler)
        )
        for data in ("edit_post", "finish", "save_draft"):
            with self.subTest(data=data):
                handler = resolve_dialog_handler(
                    conv, EDIT_FIELD, make_callback_route_update(data)
                )
                self.assertIsNotNone(handler, f"'{data}' должен обслуживаться")
                self.assertEqual(
                    handler.callback.__name__,
                    "handle_stale_preview_action",
                    f"'{data}' не должен уходить в handle_edit или молчать",
                )

    def test_finish_button_label_explains_result(self):
        buttons = [
            button
            for row in get_post_actions_keyboard().inline_keyboard
            for button in row
        ]
        finish = next(button for button in buttons if button.callback_data == "finish")
        self.assertIn("Готово", finish.text)
        self.assertIn("получить пост", finish.text)


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class CommandDuringCreationTests(_InMemoryDbMixin, unittest.TestCase):
    """/start, /help, /drafts во время диалога: сохранение WIP + завершение."""

    @staticmethod
    def _replies(update):
        return [
            call.args[0] for call in update.effective_message.reply_text.await_args_list
        ]

    @staticmethod
    def _conversation():
        application = bot_module.build_application()
        return next(
            h for h in application.handlers[0] if isinstance(h, ConversationHandler)
        )

    def test_start_during_creation_saves_and_exits(self):
        user_data = {"current_step": 2, "title": "Начатый пост"}
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update("/start")

        result = asyncio.run(start_during_creation(update, context))

        self.assertEqual(result, ConversationHandler.END)
        drafts = self._all_drafts()
        self.assertEqual(len(drafts), 1, "незавершённый пост должен сохраниться")
        self.assertEqual(drafts[0].title, "Начатый пост")
        self.assertNotIn("title", user_data)
        self.assertTrue(
            any("Здравствуйте" in text for text in self._replies(update)),
            "пользователь должен увидеть главное меню",
        )

    def test_help_during_edit_saves_and_exits(self):
        user_data = {
            "current_step": len(POST_STEPS),
            "edit_field": "date",
            "date": "25.12.2026",
        }
        context = SimpleNamespace(user_data=user_data, bot=RecordingBot())
        update = make_message_update("/help")

        result = asyncio.run(help_during_creation(update, context))

        self.assertEqual(result, ConversationHandler.END)
        drafts = self._all_drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].date, "25.12.2026")
        # состояние правки не «висит» после выхода
        self.assertNotIn("edit_field", user_data)
        self.assertNotIn("date", user_data)
        self.assertTrue(
            any("Команды:" in text for text in self._replies(update)),
            "должна показаться справка",
        )

    def test_commands_route_to_exit_fallbacks(self):
        conv = self._conversation()
        expected = {
            "/start": "start_during_creation",
            "/help": "help_during_creation",
            "/drafts": "view_drafts_and_exit",
            "/cancel": "cancel_creation",
            # повторный /create_post перезапускает диалог (entry-точка)
            "/create_post": "start_post_creation",
        }
        for state in (POST_CREATION, EDIT_FIELD):
            for command, handler_name in expected.items():
                with self.subTest(state=state, command=command):
                    handler = resolve_dialog_handler(
                        conv, state, make_text_update(command)
                    )
                    self.assertIsNotNone(handler, "команда должна обслуживаться")
                    # и НЕ уходит в handle_message/process_edit — текстом поля
                    self.assertEqual(handler.callback.__name__, handler_name)

    def test_commands_without_dialog_go_to_global_handlers(self):
        conv = self._conversation()
        for command in ("/start", "/help", "/drafts", "/cancel"):
            with self.subTest(command=command):
                # fallback'и работают только при активном диалоге — иначе
                # /start отвечал бы дважды (глобальный хендлер тоже зарегистрирован)
                self.assertFalse(conv.check_update(make_text_update(command)))
        # /create_post вне диалога — обычная entry-точка
        self.assertTrue(conv.check_update(make_text_update("/create_post")))


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
