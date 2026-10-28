# tests/test_stage13_migrations.py
"""
Stage 13, §2 (актуализировано в Poster 2.0 S5): совместимость миграций
со схемой прошлых версий.

Проверяется ТОЛЬКО на временных файловых базах (настоящая
Poster/post_bot.db не затрагивается):

- init-последовательность (run_migrations + create_all) на старой схеме
  `drafts` без photos/updated_at/formats;
- миграция 17 переименовывает `drafts` в `legacy_drafts_backup` без DROP:
  строки прежних черновиков остаются читаемыми, колонки сохраняются;
- `user_states` (живая инфраструктура экрана) создаётся на старой базе и
  переживает повторные запуски; `create_all` не воскрешает `drafts`;
- повторное применение миграций идемпотентно.
"""

import unittest

try:
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.orm import sessionmaker

    from base import Base
    from migrations import run_migrations
    from models import UserState

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
    """Старая схема → init: данные сохранены в архиве, живые таблицы созданы."""

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
        run_migrations(self.engine)
        Base.metadata.create_all(bind=self.engine)

    def test_init_on_legacy_db_archives_drafts(self):
        self._init_like_production()

        inspector = inspect(self.engine)
        tables = set(inspector.get_table_names())
        self.assertNotIn("drafts", tables, "легаси-таблица переименована")
        self.assertIn("legacy_drafts_backup", tables, "архив на месте")
        self.assertIn("user_states", tables, "user_states создана на старой БД")

        # RENAME сохраняет колонки и строки: данные читаются как есть
        columns = {c["name"] for c in inspector.get_columns("legacy_drafts_backup")}
        self.assertTrue(
            {"title", "text", "image", "created_at"} <= columns,
            f"колонки прежнего черновика сохранены: {sorted(columns)}",
        )
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT title, text, image, created_at "
                    "FROM legacy_drafts_backup WHERE id = 1"
                )
            ).fetchone()
        self.assertEqual(row.title, "Старый пост")
        self.assertEqual(row.text, "Текст старого поста")
        self.assertEqual(row.image, "old_file")
        self.assertTrue(row.created_at, "created_at не тронут")

    def test_repeated_migration_keeps_user_states_and_archive(self):
        self._init_like_production()
        session_factory = sessionmaker(bind=self.engine)

        session = session_factory()
        try:
            session.add(
                UserState(
                    user_id=7,
                    chat_id=777,
                    screen_message_id=42,
                    payload='{"screen": "main"}',
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
            self.assertIn("main", state.payload)
        finally:
            session.close()

        tables = set(inspect(self.engine).get_table_names())
        self.assertIn("legacy_drafts_backup", tables, "архив не переименован дважды")
        self.assertNotIn("drafts", tables, "create_all не воскрешает легаси-таблицу")

        with self.engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM legacy_drafts_backup")
            ).scalar()
        self.assertEqual(count, 1, "повторный запуск не дублирует строки архива")


if __name__ == "__main__":
    unittest.main()
