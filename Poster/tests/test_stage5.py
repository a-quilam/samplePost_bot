# tests/test_stage5.py
"""
Тесты Stage 5 (приёмка проекта в production):

- models.utcnow(): наивный UTC вместо устаревшего datetime.utcnow;
- PRAGMA SQLite (busy timeout, WAL, synchronous) — конкурентный доступ;
- фоновая чистка старых черновиков (TTL);
- split_long_text: отправка текста длиннее лимита Telegram (4096);
- is_post_empty: пустой пост не отправляется;
- UX диалога: подсказка на превью, «Черновики» завершают диалог,
  дедупликация промптов альбома, единая клавиатура главного меню;
- errors.py: уведомление пользователя об ошибке + троттлинг,
  подавление «message is not modified»;
- порядок регистрации обработчиков (build_application);
- список черновиков: сортировка, лимит страницы, пагинация draftpage_.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import asyncio
import os
import tempfile
import unittest
import warnings
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  — регистрация таблиц в Base.metadata
from base import Base
from database import SQLITE_BUSY_TIMEOUT_SECONDS, apply_sqlite_pragmas
from models import Draft, utcnow

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    import bot as bot_module
    import handlers.post_creation as post_creation_module
    from handlers.drafts import (
        DRAFTS_PAGE_SIZE,
        build_drafts_message,
        fetch_user_drafts,
        handle_drafts_page,
    )
    from handlers.main_menu import help_command, main_menu_keyboard
    from handlers.post_creation import (
        POST_CREATION,
        POST_STEPS,
        handle_message,
        handle_photo,
        is_post_empty,
        process_edit,
        send_post,
        split_long_text,
        view_drafts_and_exit,
    )

    HANDLERS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HANDLERS_IMPORT_ERROR = str(e)


def make_session_factory():
    """Фабрика сессий над отдельной БД в памяти."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def make_old_draft(session, user_id=111, age_days=40, title="Старый пост"):
    draft = Draft(
        user_id=user_id, title=title, created_at=utcnow() - timedelta(days=age_days)
    )
    session.add(draft)
    session.commit()
    return draft


class UtcnowTests(unittest.TestCase):
    """models.utcnow(): наивный UTC, без DeprecationWarning."""

    def test_naive_utc_close_to_now(self):
        value = utcnow()
        self.assertIsNone(value.tzinfo)
        self.assertLess(abs((datetime_now_utc() - value).total_seconds()), 5)

    def test_no_deprecation_warning(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            models.utcnow()
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertEqual(deprecations, [])

    def test_draft_created_at_naive(self):
        session_factory, engine = make_session_factory()
        session = session_factory()
        try:
            draft = Draft(user_id=1, title="Пост")
            session.add(draft)
            session.commit()
            self.assertIsNone(draft.created_at.tzinfo)
        finally:
            session.close()
            engine.dispose()


def datetime_now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


class SqlitePragmaTests(unittest.TestCase):
    """PRAGMA: busy timeout + WAL — защита от «database is locked»."""

    def _engine_with_pragmas(self, url):
        engine = create_engine(url)
        from sqlalchemy import event

        event.listen(engine, "connect", apply_sqlite_pragmas)
        return engine

    def test_wal_and_timeouts_on_file_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine_with_pragmas(
                f"sqlite:///{os.path.join(tmp, 'check.db')}"
            )
            try:
                with engine.connect() as connection:
                    busy = connection.execute(text("PRAGMA busy_timeout")).scalar()
                    journal = connection.execute(text("PRAGMA journal_mode")).scalar()
                    sync = connection.execute(text("PRAGMA synchronous")).scalar()
                self.assertEqual(busy, SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
                self.assertEqual(journal, "wal")
                # 1 = NORMAL
                self.assertEqual(sync, 1)
            finally:
                engine.dispose()

    def test_in_memory_db_does_not_error(self):
        # :memory: не поддерживает WAL — PRAGMA не должна ронять соединение
        engine = self._engine_with_pragmas("sqlite:///:memory:")
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()


class RemoveOldDraftsTests(unittest.TestCase):
    """sync_remove_old_drafts: TTL 30 дней — старые удаляются, свежие остаются."""

    def setUp(self):
        import handlers.jobs as jobs_module

        self.jobs = jobs_module
        self._orig_session_local = jobs_module.SessionLocal
        self.session_factory, self.engine = make_session_factory()
        jobs_module.SessionLocal = self.session_factory

    def tearDown(self):
        self.jobs.SessionLocal = self._orig_session_local
        self.engine.dispose()

    def test_removes_old_keeps_fresh(self):
        session = self.session_factory()
        try:
            make_old_draft(session, title="Старый 1")
            make_old_draft(session, title="Старый 2")
            session.add(Draft(user_id=111, title="Свежий", created_at=utcnow()))
            session.commit()
        finally:
            session.close()

        self.jobs.sync_remove_old_drafts()

        session = self.session_factory()
        try:
            titles = {d.title for d in session.query(Draft).all()}
        finally:
            session.close()
        self.assertEqual(titles, {"Свежий"})


class SplitLongTextTests(unittest.TestCase):
    """split_long_text: текст длиннее лимита Telegram уходит частями."""

    def test_short_text_unchanged(self):
        self.assertEqual(split_long_text("короткий текст"), ["короткий текст"])

    def test_multiline_short_unchanged_exactly(self):
        text = "Строка 1\n\nСтрока 2"
        self.assertEqual(split_long_text(text), [text])

    def test_long_single_line_split_lossless(self):
        text = "Важное событие начнётся в 14:00 (вход свободный)! " * 300
        self.assertGreater(len(text), 8000)
        chunks = split_long_text(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)
        # Ни один символ не потерян (границы — только переводы строк)
        self.assertEqual("".join(chunks), text)

    def test_escape_pairs_not_torn(self):
        # Строка из экранированных пар '\\.'; limit=101 заставляет границу
        # попадать между '\\' и '.' — сплит обязан откатиться, не разрывая пару
        text = "\\." * 3000
        chunks = split_long_text(text, limit=101)
        self.assertGreater(len(chunks), 1)

        def trailing_backslashes(s: str) -> int:
            count = 0
            for char in reversed(s):
                if char != "\\":
                    break
                count += 1
            return count

        for chunk in chunks:
            self.assertEqual(
                trailing_backslashes(chunk) % 2,
                0,
                f"часть заканчивается незакрытым экранированием: ...{chunk[-10:]!r}",
            )
        self.assertEqual("".join(chunks), text)

    def test_internal_newlines_preserved(self):
        text = "а" * 10 + "\n" + "б" * 10 + "\n" + "В" * 300
        chunks = split_long_text(text, limit=100)
        self.assertTrue(all(len(c) <= 100 for c in chunks))
        # Внутренние переводы строк внутри частей сохраняются,
        # содержимое не теряет ни одного символа (кроме границ между сообщениями)
        joined = "".join(chunks)
        self.assertIn("\n", joined)
        self.assertEqual(joined.replace("\n", ""), text.replace("\n", ""))


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class SendPostLongTextTests(unittest.TestCase):
    """send_post: длинный текст не превышает лимит сообщения Telegram."""

    class StubBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            self.calls.append(
                {"type": "message", "text": text, "reply_markup": reply_markup}
            )

        async def send_photo(
            self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None
        ):
            self.calls.append({"type": "photo", "caption": caption})

        async def send_media_group(self, chat_id, media):
            self.calls.append({"type": "media_group", "media": media})

    def _send(self, post, photos=None, reply_markup=None):
        bot = self.StubBot()
        asyncio.run(
            send_post(
                bot,
                -100500,
                post,
                photos or [],
                heading="📢 *Новый пост:*",
                reply_markup=reply_markup,
            )
        )
        return bot.calls

    def test_text_messages_within_limit(self):
        long_post = {"title": "Анонс", "text": "Подробности события. " * 500}
        calls = self._send(long_post)
        messages = [c for c in calls if c["type"] == "message"]
        self.assertGreater(len(messages), 1)
        for call in messages:
            self.assertLessEqual(len(call["text"]), 4000)
        joined = "".join(c["text"] for c in messages)
        self.assertIn("Подробности события", joined)

    def test_markup_attached_to_first_chunk_only(self):
        long_post = {"text": "Слово. " * 800}
        calls = self._send(long_post, reply_markup="MARKUP")
        messages = [c for c in calls if c["type"] == "message"]
        self.assertEqual(messages[0]["reply_markup"], "MARKUP")
        for call in messages[1:]:
            self.assertIsNone(call["reply_markup"])

    def test_single_photo_overflow_still_split(self):
        # "!" экранируется в MarkdownV2 — текст растёт и превышает лимит
        long_post = {"title": "Очень! " * 600}
        calls = self._send(long_post, photos=["p1"])
        self.assertEqual(calls[0]["type"], "photo")
        self.assertIsNone(calls[0]["caption"])
        messages = [c for c in calls if c["type"] == "message"]
        self.assertGreater(len(messages), 1)
        for call in messages:
            self.assertLessEqual(len(call["text"]), 4000)


class IsPostEmptyTests(unittest.TestCase):
    """is_post_empty: пустой пост не отправляется по кнопке «Готово»."""

    def test_empty_dict(self):
        self.assertTrue(is_post_empty({}))

    def test_all_placeholders(self):
        self.assertTrue(
            is_post_empty(
                {
                    "title": "Не указано",
                    "date": "Не указано",
                    "text": "Не указано",
                    "place_url": None,
                    "image": None,
                    "photos": None,
                }
            )
        )

    def test_title_makes_not_empty(self):
        self.assertFalse(is_post_empty({"title": "Встреча"}))

    def test_text_makes_not_empty(self):
        self.assertFalse(is_post_empty({"text": "Привет!"}))

    def test_photo_only_not_empty(self):
        self.assertFalse(is_post_empty({"image": "file_id_1"}))

    def test_photos_json_only_not_empty(self):
        self.assertFalse(is_post_empty({"photos": '["file_id_1"]'}))


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class MainMenuKeyboardTests(unittest.TestCase):
    """Единая клавиатура главного меню + справка /help."""

    def test_keyboard_structure(self):
        from telegram import ReplyKeyboardMarkup

        keyboard = main_menu_keyboard()
        self.assertIsInstance(keyboard, ReplyKeyboardMarkup)
        texts = [button.text for row in keyboard.keyboard for button in row]
        self.assertEqual(texts, ["✏️ Создать пост", "📝 Черновики"])
        self.assertTrue(keyboard.resize_keyboard)

    def test_help_lists_commands(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=1),
        )
        asyncio.run(help_command(update, SimpleNamespace(user_data={})))
        text = reply.await_args.args[0]
        for command in ("/start", "/create_post", "/drafts", "/cancel", "/help"):
            self.assertIn(command, text)


class ErrorNoticeTests(unittest.TestCase):
    """errors.py: ответ пользователю при ошибках, троттлинг, подавление."""

    def setUp(self):
        import errors

        self.errors = errors
        errors._last_error_notice.clear()
        self._orig_notice = errors._send_error_notice
        self.notice = AsyncMock(return_value=True)
        errors._send_error_notice = self.notice

    def tearDown(self):
        self.errors._send_error_notice = self._orig_notice
        self.errors._last_error_notice.clear()

    @staticmethod
    def _update(chat_id=555):
        from datetime import datetime

        from telegram import Chat, Message, Update

        return Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime(2026, 1, 1),
                chat=Chat(id=chat_id, type="private"),
                text="тест",
            ),
        )

    def test_not_modified_is_not_notified(self):
        from telegram.error import BadRequest

        context = SimpleNamespace(
            error=BadRequest("Message is not modified: нажатие повторено")
        )
        with self.assertLogs(self.errors.logger, level="INFO"):
            asyncio.run(self.errors.error_handler(self._update(), context))
        self.notice.assert_not_awaited()

    def test_other_error_notifies_user_with_chat_id(self):
        context = SimpleNamespace(error=RuntimeError("boom"))
        with self.assertLogs(self.errors.logger, level="ERROR"):
            asyncio.run(self.errors.error_handler(self._update(), context))
        self.notice.assert_awaited_once()
        self.assertEqual(self.notice.await_args.args[1], 555)

    def test_non_update_object_does_not_crash(self):
        context = SimpleNamespace(error=RuntimeError("boom"))
        with self.assertLogs(self.errors.logger, level="ERROR"):
            asyncio.run(self.errors.error_handler(object(), context))
        self.notice.assert_not_awaited()

    def test_throttles_repeated_notices_per_chat(self):
        # Реальная реализация троттлинга (мок в setUp подменён обратно)
        self.errors._send_error_notice = self._orig_notice
        message = SimpleNamespace(reply_text=AsyncMock())

        first = asyncio.run(self.errors._send_error_notice(message, 42))
        second = asyncio.run(self.errors._send_error_notice(message, 42))

        self.assertTrue(first)
        self.assertFalse(second)
        message.reply_text.assert_awaited_once()


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class DialogUxTests(unittest.TestCase):
    """UX диалога: превью, альбомы, редактирование, кнопка «Черновики»."""

    def test_preview_text_keeps_dialog_open(self):
        # Раньше текст на превью молча завершал диалог (END) — теперь подсказка
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(text="просто текст", reply_text=reply),
            effective_user=SimpleNamespace(id=1),
        )
        context = SimpleNamespace(user_data={"current_step": len(POST_STEPS)})

        result = asyncio.run(handle_message(update, context))

        self.assertEqual(result, POST_CREATION)
        reply.assert_awaited()
        self.assertIn("кнопк", reply.await_args.args[0])

    def test_album_outside_image_step_prompts_once(self):
        # Альбом не на шаге изображения: один промпт на весь альбом, без спама
        context = SimpleNamespace(user_data={"current_step": 0})

        def make_message():
            return SimpleNamespace(
                photo=[SimpleNamespace(file_id="fid")],
                media_group_id="mg-1",
                reply_text=AsyncMock(),
            )

        first = make_message()
        asyncio.run(
            handle_photo(
                SimpleNamespace(
                    effective_message=first, effective_user=SimpleNamespace(id=1)
                ),
                context,
            )
        )
        second = make_message()
        asyncio.run(
            handle_photo(
                SimpleNamespace(
                    effective_message=second, effective_user=SimpleNamespace(id=1)
                ),
                context,
            )
        )

        first.reply_text.assert_awaited_once()
        second.reply_text.assert_not_awaited()

    def test_edit_without_field_returns_to_preview(self):
        # EDIT_FIELD без edit_field: не пишем мусор в user_data, а к превью
        class StubBot:
            def __init__(self):
                self.calls = []

            async def send_message(self, **kwargs):
                self.calls.append(kwargs)

            async def send_photo(self, **kwargs):
                self.calls.append(kwargs)

            async def send_media_group(self, **kwargs):
                self.calls.append(kwargs)

        bot = StubBot()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(
                text="какой-то текст", reply_text=AsyncMock()
            ),
            effective_user=SimpleNamespace(id=1),
            effective_chat=SimpleNamespace(id=77),
        )
        context = SimpleNamespace(user_data={}, bot=bot)

        result = asyncio.run(process_edit(update, context))

        self.assertEqual(result, POST_CREATION)
        self.assertTrue(bot.calls, "превью должно быть отправлено")
        self.assertNotIn("", context.user_data, "мусорный ключ не должен появиться")

    def test_drafts_button_inside_dialog_shows_list_and_ends(self):
        from telegram.ext import ConversationHandler

        import handlers.drafts as drafts_module

        session_factory, engine = make_session_factory()
        orig = drafts_module.SessionLocal
        drafts_module.SessionLocal = session_factory
        try:
            reply = AsyncMock()
            update = SimpleNamespace(
                effective_message=SimpleNamespace(reply_text=reply),
                effective_user=SimpleNamespace(id=1),
            )
            context = SimpleNamespace(user_data={})

            result = asyncio.run(view_drafts_and_exit(update, context))

            self.assertEqual(result, ConversationHandler.END)
            reply.assert_awaited()
            self.assertIn("нет черновиков", reply.await_args.args[0])
        finally:
            drafts_module.SessionLocal = orig
            engine.dispose()


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class HandlerOrderTests(unittest.TestCase):
    """
    Порядок регистрации: ConversationHandler ПЕРВЫМ — иначе reply-кнопки
    внутри диалога перехватываются глобальными хендлерами и состояние висит.
    """

    def test_conversation_registered_before_main_menu(self):
        from telegram.ext import CommandHandler, ConversationHandler

        application = bot_module.build_application()
        handlers = application.handlers[0]

        conv_index = next(
            i for i, h in enumerate(handlers) if isinstance(h, ConversationHandler)
        )
        start_index = next(
            i
            for i, h in enumerate(handlers)
            if isinstance(h, CommandHandler) and "start" in (h.commands or set())
        )
        self.assertLess(conv_index, start_index)

    def test_drafts_button_matched_first_in_conversation_states(self):
        from datetime import datetime

        from telegram import Chat, Message, Update
        from telegram.ext import ConversationHandler, MessageHandler

        application = bot_module.build_application()
        conversation = next(
            h for h in application.handlers[0] if isinstance(h, ConversationHandler)
        )

        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime(2026, 1, 1),
                chat=Chat(id=555, type="private"),
                text="📝 Черновики",
            ),
        )

        for state in (
            post_creation_module.POST_CREATION,
            post_creation_module.EDIT_FIELD,
        ):
            matched = [
                h
                for h in conversation.states[state]
                if isinstance(h, MessageHandler) and h.check_update(update)
            ]
            self.assertTrue(matched, f"кнопка «Черновики» не обрабатывается в {state}")
            # Первым должен идти УЗКИЙ обработчик кнопки (заканчивает диалог),
            # а не generic TEXT — иначе текст кнопки запишется в поле поста.
            # Проверяем поведением: первый сработавший обработчик не должен
            # принимать произвольный текст.
            probe = Update(
                update_id=2,
                message=Message(
                    message_id=2,
                    date=datetime(2026, 1, 1),
                    chat=Chat(id=555, type="private"),
                    text="просто текст",
                ),
            )
            self.assertFalse(
                matched[0].check_update(probe),
                f"в состоянии {state} generic TEXT-хендлер идёт перед кнопкой",
            )


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class DraftsPaginationTests(unittest.TestCase):
    """Список черновиков: сортировка, лимит страницы, навигация."""

    def _seed(self, count: int, user_id: int = 111):
        session_factory, engine = make_session_factory()
        session = session_factory()
        try:
            for i in range(count):
                session.add(Draft(user_id=user_id, title=f"Пост {i}"))
            session.commit()
        finally:
            session.close()
        return session_factory, engine

    @staticmethod
    def _make_draft(draft_id: int):
        return SimpleNamespace(
            id=draft_id,
            title=f"Заголовок {draft_id}",
            date="01.01.2026",
            time_start="10:00",
            time_end="12:00",
            place_name="Зал",
        )

    def test_first_page_newest_first_and_limited(self):
        session_factory, engine = self._seed(20)
        session = session_factory()
        try:
            drafts, total = fetch_user_drafts(session, 111, 0)
            self.assertEqual(total, 20)
            self.assertEqual(len(drafts), DRAFTS_PAGE_SIZE)
            # Новые сверху: id 20..6
            self.assertEqual([d.id for d in drafts], list(range(20, 5, -1)))
        finally:
            session.close()
            engine.dispose()

    def test_second_page_returns_rest(self):
        session_factory, engine = self._seed(20)
        session = session_factory()
        try:
            drafts, total = fetch_user_drafts(session, 111, DRAFTS_PAGE_SIZE)
            self.assertEqual(total, 20)
            self.assertEqual([d.id for d in drafts], [5, 4, 3, 2, 1])
        finally:
            session.close()
            engine.dispose()

    def test_out_of_range_page_is_empty(self):
        session_factory, engine = self._seed(3)
        session = session_factory()
        try:
            drafts, total = fetch_user_drafts(session, 111, 100)
            self.assertEqual(drafts, [])
            self.assertEqual(total, 3)
        finally:
            session.close()
            engine.dispose()

    def test_other_users_drafts_not_counted(self):
        session_factory, engine = make_session_factory()
        session = session_factory()
        try:
            session.add(Draft(user_id=111, title="Свой 1"))
            session.add(Draft(user_id=111, title="Свой 2"))
            session.add(Draft(user_id=999, title="Чужой"))
            session.commit()
            drafts, total = fetch_user_drafts(session, 111, 0)
            self.assertEqual(total, 2)
            self.assertEqual({d.user_id for d in drafts}, {111})
        finally:
            session.close()
            engine.dispose()

    def test_build_message_note_and_forward_button(self):
        drafts = [self._make_draft(i) for i in range(1, DRAFTS_PAGE_SIZE + 1)]
        text, markup = build_drafts_message(drafts, offset=0, total=20)
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("из 20", text)
        self.assertIn("draftpage_15", datas)  # вперёд на вторую страницу
        self.assertNotIn("draftpage_0", datas)  # «назад» с первой страницы не нужно
        self.assertIn("main_menu", datas)

    def test_build_message_back_button_on_later_page(self):
        drafts = [self._make_draft(i) for i in range(16, 21)]
        text, markup = build_drafts_message(drafts, offset=15, total=20)
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("draftpage_0", datas)  # назад — на первую страницу
        self.assertNotIn("draftpage_20", datas)  # вперёд за конец списка не нужно

    def test_no_navigation_when_all_drafts_shown(self):
        # Вся выборка помещается на одну страницу — листание не нужно
        drafts = [self._make_draft(1), self._make_draft(2)]
        text, markup = build_drafts_message(drafts, total=2)
        datas = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertFalse(any(d.startswith("draftpage_") for d in datas))
        self.assertNotIn("листайте", text)

    def test_page_handler_renders_requested_page(self):
        session_factory, engine = self._seed(20)
        import handlers.drafts as drafts_module

        orig = drafts_module.SessionLocal
        drafts_module.SessionLocal = session_factory
        try:
            query = SimpleNamespace(
                data="draftpage_15",
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=111),
                edit_message_text=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query)

            asyncio.run(handle_drafts_page(update, SimpleNamespace(user_data={})))

            query.answer.assert_awaited()
            query.edit_message_text.assert_awaited()
            text_sent = query.edit_message_text.await_args.args[0]
            self.assertIn("из 20", text_sent)
            markup = query.edit_message_text.await_args.kwargs["reply_markup"]
            datas = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertIn("draftpage_0", datas)
        finally:
            drafts_module.SessionLocal = orig
            engine.dispose()

    def test_page_handler_clamps_to_first_when_page_emptied(self):
        session_factory, engine = self._seed(20)
        import handlers.drafts as drafts_module

        orig = drafts_module.SessionLocal
        drafts_module.SessionLocal = session_factory
        try:
            query = SimpleNamespace(
                data="draftpage_100",
                answer=AsyncMock(),
                from_user=SimpleNamespace(id=111),
                edit_message_text=AsyncMock(),
            )
            update = SimpleNamespace(callback_query=query)

            asyncio.run(handle_drafts_page(update, SimpleNamespace(user_data={})))

            text_sent = query.edit_message_text.await_args.args[0]
            # Страница опустела → возврат к первой (новейшие черновики сверху)
            self.assertIn("Пост 19", text_sent)
        finally:
            drafts_module.SessionLocal = orig
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
