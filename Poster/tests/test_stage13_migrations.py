# tests/test_stage13_migrations.py
"""
Stage 13, §2: совместимость миграций со схемами прошлых версий.

Проверяется ТОЛЬКО на временных файловых базах (настоящая
Poster/post_bot.db не затрагивается):

- init-последовательность (create_all + _migrate) на старой схеме drafts
  без photos/updated_at/formats и без таблицы user_states;
- существующие черновики переживают миграцию без изменений;
- повторное применение миграций идемпотентно, user_states с WIP-снимком
  сохраняется;
- Draft.updated_at: у легаси-записей NULL (TTL — по created_at), у новых
  записей заполняется моделью; форматирование (Draft.formats) пишется
  и читается после миграции.
"""

import unittest
from datetime import timedelta

try:
    from sqlalchemy import create_engine, func, inspect, text
    from sqlalchemy.orm import sessionmaker

    from base import Base
    from database import _migrate
    from handlers.jobs import DRAFT_TTL_DAYS
    from models import Draft, UserState, get_draft_formats, utcnow

    INTEGRATION_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)

# Схема drafts до Stage 7: без photos/updated_at/formats; user_states нет
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

LEGACY_INSERT = """
    INSERT INTO drafts (id, user_id, title, text, image, created_at)
    VALUES (1, 111, 'Старый пост', 'Текст старого поста', 'old_file',
            '2026-01-01 00:00:00')
"""


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"миграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LegacySchemaMigrationsTests(unittest.TestCase):
    """Старая схема → init (create_all + _migrate): данные сохранены."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="poster_migrations_")
        self.engine = create_engine(f"sqlite:///{self._tmp.name}/legacy.db")
        with self.engine.begin() as connection:
            connection.execute(text(OLD_DRAFTS_DDL))
            connection.execute(text(LEGACY_INSERT))

    def tearDown(self):
        self.engine.dispose()
        self._tmp.cleanup()

    def _init_like_production(self) -> None:
        """Ровно то, что делает init_db() при старте бота."""
        Base.metadata.create_all(bind=self.engine)
        _migrate(self.engine)

    def test_init_on_legacy_db_preserves_drafts(self):
        self._init_like_production()

        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        self.assertIn("drafts", tables, "старая таблица сохранена")
        self.assertIn("user_states", tables, "user_states создана на старой БД")

        columns = {c["name"] for c in inspector.get_columns("drafts")}
        for column in ("photos", "updated_at", "formats"):
            self.assertIn(column, columns, f"колонка {column} добавлена")

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT title, text, image, created_at, photos, "
                    "formats, updated_at FROM drafts WHERE id = 1"
                )
            ).fetchone()
        self.assertEqual(row.title, "Старый пост")
        self.assertEqual(row.text, "Текст старого поста")
        self.assertEqual(row.image, "old_file")
        self.assertTrue(row.created_at, "created_at не тронут")
        self.assertIsNone(row.photos, "photos — NULL, данные не записываются")
        self.assertIsNone(row.formats, "formats — NULL, текст не переписан")
        self.assertIsNone(row.updated_at, "updated_at — NULL у легаси-записи")

        # Легаси-черновик читается общими хелперами без ошибок
        legacy = Draft(title=row.title, image=row.image, formats=row.formats)
        self.assertEqual(get_draft_formats(legacy), {})

    def test_repeated_migration_keeps_user_states_and_drafts(self):
        self._init_like_production()
        session_factory = sessionmaker(bind=self.engine)

        session = session_factory()
        try:
            session.add(
                UserState(
                    user_id=7,
                    chat_id=777,
                    screen_message_id=42,
                    payload='{"title": "В работе", "current_step": 3}',
                )
            )
            session.commit()
        finally:
            session.close()

        # Повторные запуски (рестарты) — идемпотентно, ничего не теряется
        self._init_like_production()
        self._init_like_production()

        session = session_factory()
        try:
            state = session.get(UserState, 7)
            self.assertIsNotNone(state, "user_states пережила миграции")
            self.assertEqual(state.screen_message_id, 42)
            self.assertIn("В работе", state.payload)
            draft = session.get(Draft, 1)
            self.assertIsNotNone(draft)
            self.assertEqual(draft.title, "Старый пост")
            self.assertEqual(draft.photos, None)
        finally:
            session.close()

    def test_new_draft_after_migration_has_updated_at_and_formats(self):
        self._init_like_production()
        session_factory = sessionmaker(bind=self.engine)

        session = session_factory()
        try:
            session.add(
                Draft(
                    user_id=111,
                    title="Новый пост",
                    formats='{"title": "<b>Новый</b>"}',
                )
            )
            session.commit()
            session.expire_all()
            draft = session.query(Draft).filter(Draft.title == "Новый пост").one()
            self.assertIsNotNone(
                draft.updated_at, "модель заполняет updated_at для новых записей"
            )
            self.assertIsNotNone(draft.created_at)
            self.assertEqual(get_draft_formats(draft), {"title": "<b>Новый</b>"})
        finally:
            session.close()

    def test_ttl_filter_matches_jobs_semantics_after_migration(self):
        """Фильтр sync_remove_old_drafts работает на мигрированной схеме."""
        self._init_like_production()
        session_factory = sessionmaker(bind=self.engine)

        with self.engine.begin() as connection:
            # Недавно правленный СТАРЫЙ черновик — удаляться не должен
            connection.execute(
                text(
                    "INSERT INTO drafts (id, user_id, title, created_at, "
                    "updated_at) VALUES (2, 111, 'Правленый', "
                    "'2026-01-01 00:00:00', :now)"
                ),
                {"now": utcnow().isoformat(sep=" ")},
            )

        session = session_factory()
        try:
            cutoff = utcnow() - timedelta(days=DRAFT_TTL_DAYS)
            # Тот же фильтр, что в handlers.jobs.sync_remove_old_drafts
            expired = [
                d.id
                for d in session.query(Draft)
                .filter(func.coalesce(Draft.updated_at, Draft.created_at) < cutoff)
                .all()
            ]
            self.assertEqual(expired, [1], "легаси-запись устарела по created_at")
            self.assertNotIn(2, expired, "недавно правленный черновик TTL не удаляет")
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
