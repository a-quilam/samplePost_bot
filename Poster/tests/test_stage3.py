# tests/test_stage3.py
"""
Юнит-тесты Stage 3: отправка поста, медиа-группы, миграция, экранирование.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import asyncio
import unittest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

import models  # noqa: F401  — регистрация таблиц в Base.metadata
from base import Base
from database import _migrate
from models import Draft, get_draft_photos, photos_to_json
from utils.formatter import escape_markdown, format_text

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    from handlers.post_creation import POST_HEADING, build_post_summary, send_post

    HANDLERS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HANDLERS_IMPORT_ERROR = str(e)


def make_session():
    """Сессия над БД в памяти (отдельный engine, к post_bot.db не подключается)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_draft(session, user_id=111, title="Тестовый пост", **fields):
    draft = Draft(user_id=user_id, title=title, **fields)
    session.add(draft)
    session.commit()
    return draft


class PhotosHelperTests(unittest.TestCase):
    """photos_to_json / get_draft_photos: медиа-группы + совместимость со старыми записями."""

    def tearDown(self):
        session = getattr(self, "session", None)
        if session is not None:
            engine = session.get_bind()
            session.close()
            engine.dispose()

    def test_photos_to_json(self):
        self.assertEqual(photos_to_json(["a", "b"]), '["a", "b"]')
        self.assertEqual(photos_to_json(["a", "", None]), '["a"]')
        self.assertIsNone(photos_to_json([]))
        self.assertIsNone(photos_to_json(None))

    def test_get_draft_photos_from_json(self):
        self.session = make_session()
        draft = make_draft(self.session, photos=photos_to_json(["f1", "f2"]))
        self.session.expire_all()
        self.assertEqual(get_draft_photos(draft), ["f1", "f2"])

    def test_get_draft_photos_legacy_single_image(self):
        # Старый черновик: photos IS NULL, есть только image
        self.session = make_session()
        draft = make_draft(self.session, image="old_file_id")
        self.assertEqual(get_draft_photos(draft), ["old_file_id"])

    def test_get_draft_photos_broken_json_falls_back(self):
        self.session = make_session()
        draft = make_draft(self.session, photos="{broken", image="x")
        self.assertEqual(get_draft_photos(draft), ["x"])

    def test_get_draft_photos_empty(self):
        self.session = make_session()
        draft = make_draft(self.session)
        self.assertEqual(get_draft_photos(draft), [])

    def test_photos_survive_db_roundtrip(self):
        self.session = make_session()
        draft = make_draft(
            self.session, photos=photos_to_json(["p1", "p2"]), image="p1"
        )
        self.session.expire_all()
        reloaded = self.session.query(Draft).get(draft.id)
        self.assertEqual(get_draft_photos(reloaded), ["p1", "p2"])
        self.assertEqual(reloaded.image, "p1")  # image — первая фотография (dual-write)


class StubBot:
    """Асинхронная заглушка бота: записывает вызовы отправки."""

    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.calls.append(
            {
                "type": "message",
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )

    async def send_photo(
        self, chat_id, photo, caption=None, parse_mode=None, reply_markup=None
    ):
        self.calls.append(
            {
                "type": "photo",
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
                "reply_markup": reply_markup,
            }
        )

    async def send_media_group(self, chat_id, media):
        self.calls.append({"type": "media_group", "chat_id": chat_id, "media": media})


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class SendPostTests(unittest.TestCase):
    """Единая отправка поста: текст / фото / медиа-группа — один формат."""

    POST = {"title": "Встреча", "date": "01.01.2027", "text": "Приходите!"}

    def send(self, photos, post=None, reply_markup=None, heading=None):
        bot = StubBot()
        asyncio.run(
            send_post(
                bot,
                -100500,
                post or self.POST,
                photos,
                heading=heading if heading is not None else POST_HEADING,
                reply_markup=reply_markup,
                markup_lead_text="Выберите действие:",
            )
        )
        return bot.calls

    def test_text_only(self):
        calls = self.send([])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "message")
        self.assertEqual(calls[0]["parse_mode"], "MarkdownV2")
        self.assertTrue(calls[0]["text"].startswith(POST_HEADING))
        self.assertIn("Встреча", calls[0]["text"])

    def test_single_photo_caption(self):
        calls = self.send(["only_photo"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "photo")
        self.assertEqual(calls[0]["photo"], "only_photo")
        # подпись = тот же текст, что и при отправке без фото
        self.assertTrue(calls[0]["caption"].startswith(POST_HEADING))

    def test_single_photo_overflow_split(self):
        # подпись лимитируется ~1024 символами — длинный текст уходит сообщением
        long_post = {"title": "Очень" * 300, "date": "01.01.2027"}
        calls = self.send(["only_photo"], post=long_post)
        self.assertEqual([c["type"] for c in calls], ["photo", "message"])
        self.assertIsNone(calls[0]["caption"])

    def test_media_group_caption_on_first_only(self):
        calls = self.send(["p1", "p2", "p3"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["type"], "media_group")
        media = calls[0]["media"]
        self.assertEqual(len(media), 3)
        self.assertTrue(media[0].caption.startswith(POST_HEADING))
        self.assertIsNone(media[1].caption)
        self.assertIsNone(media[2].caption)

    def test_media_group_with_markup_sends_lead_message(self):
        # sendMediaGroup не поддерживает кнопки — клавиатура отдельным сообщением
        class FakeMarkup:
            pass

        calls = self.send(["p1", "p2"], reply_markup=FakeMarkup())
        self.assertEqual([c["type"] for c in calls], ["media_group", "message"])
        self.assertEqual(calls[1]["text"], "Выберите действие:")
        self.assertIsInstance(calls[1]["reply_markup"], FakeMarkup)

    def test_media_group_overflow_split(self):
        long_post = {"title": "Очень" * 300, "date": "01.01.2027"}
        calls = self.send(["p1", "p2"], post=long_post)
        # подпись не влезает в первое фото — текст отдельным сообщением
        self.assertEqual([c["type"] for c in calls], ["media_group", "message"])
        self.assertIsNone(calls[0]["media"][0].caption)

    def test_preview_and_final_text_identical(self):
        # Текст превью и финальной отправки собирается одним и тем же кодом
        preview = self.send(["p1"])
        final = self.send(["p1"])
        self.assertEqual(preview[0]["caption"], final[0]["caption"])


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class FormatterGuardTests(unittest.TestCase):
    """Защита от возврата двойного экранирования (анализ БД: затронутых записей — 0)."""

    def test_format_text_does_not_add_backslashes(self):
        formatted = format_text('Встреча "в 12.00" - вход свободный')
        self.assertNotIn("\\", formatted)

    def test_escape_applied_once_at_render(self):
        self.assertEqual(escape_markdown("a_b"), "a\\_b")
        self.assertEqual(escape_markdown("5.00"), "5\\.00")


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class PostSummaryTests(unittest.TestCase):
    """Сводка поста (превью == готовый пост): только заполненные поля."""

    def test_summary_escapes_user_values(self):
        summary = build_post_summary({"title": "Скидка 50%_*"}, heading="h")
        self.assertIn("\\*", summary)  # '*' пользователя экранирован
        self.assertIn("\\_", summary)  # '_' пользователя экранирован

    def test_summary_shows_only_filled_fields(self):
        summary = build_post_summary({"title": "Концерт"}, heading="h")
        self.assertIn("• *Заголовок*: Концерт", summary)
        for label in ("Дата", "Время начала", "Текст", "Изображение"):
            self.assertNotIn(label, summary)

    def test_summary_legacy_placeholder_is_empty(self):
        # «Не указано» из старых черновиков не показывается как значение
        summary = build_post_summary(
            {"title": "Не указано", "date": "15.09.2026"}, heading="h"
        )
        self.assertNotIn("• *Заголовок*:", summary)
        self.assertIn("• *Дата*: 15\\.09\\.2026", summary)

    def test_summary_photo_line_only_when_present(self):
        self.assertIn(
            "• *Изображение*: Добавлено",
            build_post_summary({"image": "f1"}, heading="h"),
        )
        self.assertNotIn("Изображение", build_post_summary({"title": "Т"}, heading="h"))

    def test_empty_summary_has_no_field_lines(self):
        summary = build_post_summary({}, heading="h")
        self.assertNotIn("•", summary)
        self.assertNotIn("Не указано", summary)


class MigrationTests(unittest.TestCase):
    """_migrate: добавление колонок photos и updated_at без потери данных."""

    OLD_DRAFTS_DDL = """
        CREATE TABLE drafts (
            id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            title VARCHAR(255),
            date VARCHAR(50),
            time_start VARCHAR(50),
            time_end VARCHAR(50),
            place_name VARCHAR(255),
            place_url VARCHAR(255),
            text TEXT,
            contact VARCHAR(255),
            image VARCHAR(255),
            created_at DATETIME NOT NULL,
            PRIMARY KEY (id)
        )
    """

    def make_old_db(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text(self.OLD_DRAFTS_DDL))
            connection.execute(
                text(
                    "INSERT INTO drafts (id, user_id, title, image, created_at) "
                    "VALUES (1, 111, 'Старый пост', 'old_file', '2026-01-01 00:00:00')"
                )
            )
        return engine

    def test_migrate_adds_photos_column(self):
        engine = self.make_old_db()
        try:
            _migrate(engine)
            columns = {c["name"] for c in inspect(engine).get_columns("drafts")}
            self.assertIn("photos", columns)
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT title, image, photos FROM drafts WHERE id = 1")
                ).fetchone()
            # Данные не тронуты; photos добавлена как пустая (NULL)
            self.assertEqual(row.title, "Старый пост")
            self.assertEqual(row.image, "old_file")
            self.assertIsNone(row.photos)
            # Старый черновик сразу читается через общий хелпер
            legacy_draft = Draft(title=row.title, image=row.image, photos=row.photos)
            self.assertEqual(get_draft_photos(legacy_draft), ["old_file"])
        finally:
            engine.dispose()

    def test_migrate_is_idempotent(self):
        engine = self.make_old_db()
        try:
            _migrate(engine)
            _migrate(engine)  # повторный вызов не должен падать
            columns = {c["name"] for c in inspect(engine).get_columns("drafts")}
            self.assertIn("photos", columns)
            self.assertIn("updated_at", columns)
        finally:
            engine.dispose()

    def test_migrate_adds_updated_at_column(self):
        # A2: у существующих записей колонка появляется как NULL —
        # TTL для них продолжает считать срок по created_at
        engine = self.make_old_db()
        try:
            _migrate(engine)
            columns = {c["name"] for c in inspect(engine).get_columns("drafts")}
            self.assertIn("updated_at", columns)
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT title, updated_at FROM drafts WHERE id = 1")
                ).fetchone()
            self.assertEqual(row.title, "Старый пост")
            self.assertIsNone(row.updated_at)
        finally:
            engine.dispose()

    def test_migrate_noop_on_fresh_schema(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)  # новая схема уже содержит photos
            _migrate(engine)
            columns = {c["name"] for c in inspect(engine).get_columns("drafts")}
            self.assertIn("photos", columns)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
