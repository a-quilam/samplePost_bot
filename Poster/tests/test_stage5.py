# tests/test_stage5.py
"""
Тесты Stage 5 (приёмка проекта в production):

- models.utcnow(): наивный UTC вместо устаревшего datetime.utcnow;
- PRAGMA SQLite (busy timeout, WAL, synchronous) — конкурентный доступ;
- фоновая чистка черновиков не трогает активные согласования;
- /remove_responsible отвечает после удаления (имя фиксируется до commit);
- split_long_text: отправка текста длиннее лимита Telegram (4096);
- is_post_empty: пустой пост не уходит на согласование;
- send_for_approval: черновик сохранён даже при сбое отправки в чат;
- notify_responsible: недоставленное уведомление ≠ сбой назначения.

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
from approval import (
    STATUS_APPROVED,
    STATUS_ASSIGNED,
    STATUS_DECLINED,
    STATUS_PUBLISHED,
)
from base import Base
from database import SQLITE_BUSY_TIMEOUT_SECONDS, apply_sqlite_pragmas
from models import Draft, PostApproval, utcnow

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    import handlers.post_creation as post_creation_module
    from handlers.approval import notify_responsible
    from handlers.post_creation import (
        is_post_empty,
        send_for_approval,
        send_post,
        split_long_text,
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
    """sync_remove_old_drafts: TTL 30 дней, активные согласования не трогаются."""

    def setUp(self):
        import handlers.jobs as jobs_module

        self.jobs = jobs_module
        self._orig_session_local = jobs_module.SessionLocal
        self.session_factory, self.engine = make_session_factory()
        jobs_module.SessionLocal = self.session_factory

    def tearDown(self):
        self.jobs.SessionLocal = self._orig_session_local
        self.engine.dispose()

    def _seed(self):
        session = self.session_factory()
        try:
            old_free = make_old_draft(session, title="Без согласования")
            old_declined = make_old_draft(session, title="Отклонённый")
            old_published = make_old_draft(session, title="Опубликованный")
            old_assigned = make_old_draft(session, title="На согласовании")
            old_approved = make_old_draft(session, title="Согласован")
            fresh = Draft(user_id=111, title="Свежий", created_at=utcnow())
            session.add(fresh)
            session.commit()
            for draft, status in (
                (old_declined, STATUS_DECLINED),
                (old_published, STATUS_PUBLISHED),
                (old_assigned, STATUS_ASSIGNED),
                (old_approved, STATUS_APPROVED),
            ):
                session.add(
                    PostApproval(
                        draft_id=draft.id,
                        responsible_telegram_id=42,
                        status=status,
                    )
                )
            session.commit()
            return {
                d.title: d.id
                for d in (
                    old_free,
                    old_declined,
                    old_published,
                    old_assigned,
                    old_approved,
                    fresh,
                )
            }
        finally:
            session.close()

    def _titles_left(self):
        session = self.session_factory()
        try:
            return {d.title for d in session.query(Draft).all()}
        finally:
            session.close()

    def _approvals_left(self):
        session = self.session_factory()
        try:
            return session.query(PostApproval).count()
        finally:
            session.close()

    def test_removes_inactive_keeps_active(self):
        self._seed()
        self.jobs.sync_remove_old_drafts()

        self.assertEqual(
            self._titles_left(),
            {"На согласовании", "Согласован", "Свежий"},
        )

    def test_orphan_approvals_removed_with_drafts(self):
        self._seed()
        self.jobs.sync_remove_old_drafts()
        # Остаются ровно 2 записи согласования (assigned + approved)
        self.assertEqual(self._approvals_left(), 2)


class RemoveResponsibleTests(unittest.TestCase):
    """remove_responsible: имя фиксируется до удаления (без DetachedInstanceError)."""

    def setUp(self):
        import handlers.admin as admin_module

        self.admin = admin_module
        self._orig_session_local = admin_module.SessionLocal
        self._orig_admin_ids = admin_module.ADMIN_IDS
        self.session_factory, self.engine = make_session_factory()
        admin_module.SessionLocal = self.session_factory
        admin_module.ADMIN_IDS = [777]

        session = self.session_factory()
        try:
            from models import ResponsiblePerson

            session.add(ResponsiblePerson(name="Иван Тестов", telegram_id=42))
            session.commit()
        finally:
            session.close()

    def tearDown(self):
        self.admin.SessionLocal = self._orig_session_local
        self.admin.ADMIN_IDS = self._orig_admin_ids
        self.engine.dispose()

    def test_remove_replies_success_and_deletes_row(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=777),
        )
        context = SimpleNamespace(args=["42"])

        asyncio.run(self.admin.remove_responsible(update, context))

        reply.assert_awaited()
        text_sent = reply.await_args.args[0]
        self.assertIn("удалён успешно", text_sent)
        self.assertIn("Иван Тестов", text_sent)

        session = self.session_factory()
        try:
            from models import ResponsiblePerson

            self.assertIsNone(
                session.query(ResponsiblePerson).filter_by(telegram_id=42).first()
            )
        finally:
            session.close()

    def test_remove_missing_person_replies_not_found(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=777),
        )
        context = SimpleNamespace(args=["999"])

        asyncio.run(self.admin.remove_responsible(update, context))

        reply.assert_awaited()
        self.assertIn("не найден", reply.await_args.args[0])


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
    """is_post_empty: пустой пост не отправляется на согласование."""

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
class SendForApprovalGuardTests(unittest.TestCase):
    """send_for_approval: пустой пост отклоняется до отправки."""

    def test_empty_post_replies_and_sends_nothing(self):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=5),
        )
        context = SimpleNamespace(
            user_data={
                "title": "Не указано",
                "date": "Не указано",
                "text": "Не указано",
            },
            bot=object(),  # бот не должен использоваться — проверка раньше отправки
        )

        asyncio.run(send_for_approval(update, context))

        reply.assert_awaited()
        self.assertIn("Пост пуст", reply.await_args.args[0])


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class SendForApprovalFailureTests(unittest.TestCase):
    """Сбой отправки в чат согласования: черновик сохранён и НЕ потерян."""

    class FailingBot:
        async def send_message(self, **kwargs):
            raise RuntimeError("chat not found")

        async def send_photo(self, **kwargs):
            raise RuntimeError("chat not found")

        async def send_media_group(self, **kwargs):
            raise RuntimeError("chat not found")

    def setUp(self):
        self._orig_session_local = post_creation_module.SessionLocal
        self._orig_review = post_creation_module.REVIEW_CHAT_ID
        self.session_factory, self.engine = make_session_factory()
        post_creation_module.SessionLocal = self.session_factory
        post_creation_module.REVIEW_CHAT_ID = "-100777"

    def tearDown(self):
        post_creation_module.SessionLocal = self._orig_session_local
        post_creation_module.REVIEW_CHAT_ID = self._orig_review
        self.engine.dispose()

    def _run(self, user_data, bot):
        reply = AsyncMock()
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=5),
        )
        context = SimpleNamespace(user_data=user_data, bot=bot)
        asyncio.run(send_for_approval(update, context))
        return reply

    def test_draft_saved_even_if_review_send_fails(self):
        reply = self._run({"title": "Концерт", "text": "В 19:00"}, self.FailingBot())

        # Пользователь должен знать, что пост сохранён, а не потерян
        last_text = reply.await_args.args[0]
        self.assertIn("Черновик сохранён", last_text)
        self.assertIn("не удалось", last_text)

        session = self.session_factory()
        try:
            drafts = session.query(Draft).all()
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0].title, "Концерт")
        finally:
            session.close()

    def test_success_path_reports_sent(self):
        class OkBot:
            async def send_message(self, **kwargs):
                return None

            async def send_photo(self, **kwargs):
                return None

            async def send_media_group(self, **kwargs):
                return None

        reply = self._run({"title": "Концерт"}, OkBot())
        self.assertIn("Пост отправлен на согласование", reply.await_args.args[0])


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class NotifyResponsibleTests(unittest.TestCase):
    """notify_responsible: 403/сбой доставки ≠ сбой назначения."""

    def test_success_returns_true(self):
        calls = []

        class Bot:
            async def send_message(self, **kwargs):
                calls.append(kwargs)

            async def send_photo(self, **kwargs):
                calls.append(kwargs)

            async def send_media_group(self, **kwargs):
                calls.append(kwargs)

        draft = Draft(user_id=1, title="Пост")
        notified = asyncio.run(notify_responsible(Bot(), 42, draft, 7))
        self.assertTrue(notified)
        self.assertTrue(calls)

    def test_failure_returns_false(self):
        class Bot:
            async def send_message(self, **kwargs):
                raise RuntimeError("Forbidden: bot was blocked by the user")

            async def send_photo(self, **kwargs):
                raise RuntimeError("Forbidden")

            async def send_media_group(self, **kwargs):
                raise RuntimeError("Forbidden")

        draft = Draft(user_id=1, title="Пост")
        notified = asyncio.run(notify_responsible(Bot(), 42, draft, 7))
        self.assertFalse(notified)


if __name__ == "__main__":
    unittest.main()
