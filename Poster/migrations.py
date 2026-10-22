# migrations.py
"""
Нумерованные миграции схемы SQLite (Stage 19A).

Механизм свой, а не Alembic: проект — однофайловая SQLite-база с полностью
ручными, добавочными изменениями схемы. Alembic добавил бы зависимость,
каталог версий и autogenerate, который для SQLite всё равно требует ручной
проверки. Здесь достаточно журнала `schema_migrations` и списка DDL-шагов:

- каждая миграция применяется ровно один раз (журнал версий);
- повторный запуск ничего не делает (идемпотентность);
- журнал хранит контрольную сумму DDL: изменённую задним числом миграцию
  механизм отказывается применять к базе, где она уже записана;
- неизвестная/более новая версия или разрыв журнала = несовместимая схема;
- все ожидающие миграции применяются в ОДНОЙ транзакции SQLite
  (`BEGIN IMMEDIATE`): DDL в SQLite транзакционен, поэтому сбой посреди
  миграции не оставляет полуприменённое состояние;
- `BEGIN IMMEDIATE` + `busy_timeout` защищают от конкурентной инициализации:
  второй процесс ждёт, затем видит уже применённые версии.

Ограничение: миграции требуют файловую SQLite (не `:memory:`) — приложение
всегда работает с файлом; для временных баз тесты используют временные файлы.

Правила для будущих этапов:
- уже применённую миграцию нельзя редактировать — только новая версия;
- миграции только добавочные (CREATE TABLE/INDEX, ALTER TABLE ADD COLUMN);
- разрушительные операции требуют отдельного плана восстановления (docs/migrations.md).
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Столько же, сколько у основного движка (database.SQLITE_BUSY_TIMEOUT_SECONDS).
BUSY_TIMEOUT_MS = 30_000

_JOURNAL_DDL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " version INTEGER PRIMARY KEY,"
    " name TEXT NOT NULL,"
    " checksum TEXT NOT NULL,"
    " applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ")"
)


@dataclass(frozen=True)
class Migration:
    """Одна неизменяемая миграция: версия, имя и набор DDL/DML-шагов."""

    version: int
    name: str
    statements: tuple[str, ...]

    @property
    def checksum(self) -> str:
        """
        Контрольная сумма DDL.

        Пробельные различия (переносы строк, отступы) не считаются
        изменением; любое смысловое изменение SQL — считается.
        """
        digest = hashlib.sha256()
        for statement in self.statements:
            normalized = " ".join(statement.split())
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()


class MigrationError(RuntimeError):
    """Миграция не применена; база осталась в состоянии до запуска."""


class IncompatibleSchemaError(MigrationError):
    """Схема базы несовместима с кодом (новее, изменена или с разрывом)."""


# --- Версия 1: минимальный фундамент справочника мест -----------------------

MIGRATION_1_USERS_PLACES = Migration(
    version=1,
    name="users_places_place_names",
    statements=(
        """
        CREATE TABLE users (
            id INTEGER NOT NULL PRIMARY KEY,
            telegram_user_id INTEGER NOT NULL,
            username VARCHAR(255),
            full_name VARCHAR(255),
            created_at DATETIME NOT NULL,
            updated_at DATETIME,
            last_seen_at DATETIME,
            CONSTRAINT uq_users_telegram_user_id UNIQUE (telegram_user_id)
        )
        """,
        """
        CREATE TABLE places (
            id INTEGER NOT NULL PRIMARY KEY,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_places_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX ix_places_created_by_user_id ON places (created_by_user_id)",
        """
        CREATE TABLE place_names (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            name_search TEXT NOT NULL,
            is_canonical INTEGER NOT NULL,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_place_names_name_not_empty
                CHECK (length(trim(name)) > 0),
            CONSTRAINT ck_place_names_search_not_empty
                CHECK (length(trim(name_search)) > 0),
            CONSTRAINT ck_place_names_is_canonical
                CHECK (is_canonical IN (0, 1)),
            CONSTRAINT uq_place_names_place_search
                UNIQUE (place_id, name_search),
            CONSTRAINT fk_place_names_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_names_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX ix_place_names_place_id ON place_names (place_id)",
        "CREATE INDEX ix_place_names_name_search ON place_names (name_search)",
        # Ровно один канонический вариант: частичный уникальный индекс.
        # Отсутствие строк вообще (место без названия) не допускается
        # прикладным слоем place_store, а не триггером.
        """
        CREATE UNIQUE INDEX ux_place_names_one_canonical
            ON place_names (place_id) WHERE is_canonical = 1
        """,
    ),
)

DEFAULT_MIGRATIONS: tuple[Migration, ...] = (MIGRATION_1_USERS_PLACES,)

LATEST_VERSION = DEFAULT_MIGRATIONS[-1].version if DEFAULT_MIGRATIONS else 0


def _sqlite_file_path(engine: Engine) -> str:
    """Путь к файлу SQLite; миграции не поддерживают анонимные базы."""
    if engine.url.get_backend_name() != "sqlite":
        raise MigrationError(
            "Миграции Poster поддерживают только SQLite, "
            f"а не {engine.url.get_backend_name()!r}."
        )
    database = engine.url.database
    if not database or database == ":memory:":
        raise MigrationError(
            "Миграции требуют файловую SQLite-базу: путь к файлу не задан "
            "(DATABASE_URL указывает на :memory:)."
        )
    return str(database)


def _validate_migrations(migrations: Sequence[Migration]) -> None:
    """Версии обязаны быть непрерывными, начиная с 1 (без пропусков)."""
    versions = [migration.version for migration in migrations]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        raise ValueError(
            "Список миграций должен содержать непрерывные версии 1..N; "
            f"получено {versions!r}."
        )
    for migration in migrations:
        if not migration.name.strip():
            raise ValueError(f"У миграции {migration.version} пустое имя.")
        if not migration.statements:
            raise ValueError(f"У миграции {migration.version} нет шагов.")


def _validate_journal(
    applied: Sequence[sqlite3.Row], migrations: Sequence[Migration]
) -> None:
    """
    Журнал должен быть префиксом списка миграций кода с теми же суммами.

    Проверяются: версии (без чужих/пропущенных) и контрольные суммы DDL.
    """
    applied_versions = [int(row["version"]) for row in applied]
    expected_prefix = [migration.version for migration in migrations[: len(applied)]]
    if applied_versions != expected_prefix:
        known = {migration.version for migration in migrations}
        unknown = [version for version in applied_versions if version not in known]
        if unknown:
            raise IncompatibleSchemaError(
                "База содержит миграции, неизвестные этому коду: "
                f"{unknown!r}. Вероятно, база создана более новой версией "
                "Poster; обновите приложение или восстановите резервную копию."
            )
        raise IncompatibleSchemaError(
            "Журнал миграций не является непрерывным: "
            f"применено {applied_versions!r}, ожидался префикс "
            f"{expected_prefix!r}."
        )

    for row, migration in zip(applied, migrations):
        if str(row["checksum"]) != migration.checksum:
            raise IncompatibleSchemaError(
                f"Миграция {migration.version} ({migration.name}) была "
                "изменена после применения: контрольная сумма DDL не "
                "совпадает. Применяйте только добавление новых миграций."
            )


def run_migrations(
    engine: Engine,
    migrations: Sequence[Migration] = DEFAULT_MIGRATIONS,
) -> int:
    """
    Применяет недостающие миграции и возвращает последнюю версию схемы.

    `BEGIN IMMEDIATE` берётся до чтения журнала: конкурентный запуск
    (например, два старта бота) ждёт освобождения блокировки и затем видит
    уже применённые версии. Все ожидающие миграции идут одной транзакцией:
    сбой любой из них откатывает весь запуск, журнал не пополняется.

    Исключения:
    - IncompatibleSchemaError — журнал/схема не совместимы с кодом;
    - MigrationError — не удалось применить DDL (база не изменена).
    """
    _validate_migrations(migrations)
    path = _sqlite_file_path(engine)

    connection = sqlite3.connect(
        path,
        timeout=BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,  # транзакциями управляем явно
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise MigrationError(
                "Не удалось получить блокировку базы для миграций за "
                f"{BUSY_TIMEOUT_MS // 1000} секунд: {exc}"
            ) from exc

        try:
            connection.execute(_JOURNAL_DDL)
            applied = connection.execute(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            ).fetchall()
            _validate_journal(applied, migrations)

            for migration in migrations[len(applied) :]:
                logger.info(
                    "Применяется миграция %s (%s).",
                    migration.version,
                    migration.name,
                )
                try:
                    for statement in migration.statements:
                        connection.execute(statement)
                except Exception as exc:
                    # Любая ошибка шага = миграция не применена;
                    # внешний обработчик откатит ВСЮ транзакцию.
                    raise MigrationError(
                        f"Миграция {migration.version} ({migration.name}) "
                        f"не применена: {exc}"
                    ) from exc
                connection.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) "
                    "VALUES (?, ?, ?)",
                    (migration.version, migration.name, migration.checksum),
                )

            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
    finally:
        connection.close()

    return migrations[-1].version if migrations else 0
