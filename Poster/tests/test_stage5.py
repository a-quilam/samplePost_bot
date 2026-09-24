# tests/test_stage5.py
"""
Тесты Stage 5 (приёмка проекта в production):

- models.utcnow(): наивный UTC вместо устаревшего datetime.utcnow;
- PRAGMA SQLite (busy timeout, WAL, synchronous) — конкурентный доступ;
- фоновая чистка черновиков не трогает активные согласования;
- /remove_responsible не падает с DetachedInstanceError после удаления.

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


if __name__ == "__main__":
    unittest.main()
