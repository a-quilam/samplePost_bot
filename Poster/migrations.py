# migrations.py
"""
Нумерованные миграции схемы SQLite (Stage 19A/19B).

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
  второй процесс ждёт, затем видит уже применённые версии;
- перед первым ожидающим DDL существующей непустой базы автоматически
  создаётся WAL-aware копия (SQLite Backup API + integrity_check). Сбой
  копии отменяет миграцию; готовые копии не удаляются.

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
import os
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# Столько же, сколько у основного движка (database.SQLITE_BUSY_TIMEOUT_SECONDS).
BUSY_TIMEOUT_MS = 30_000

# Копия делается в каталоге рядом с базой: `post_bot.db.backup-...`.
# Пользовательские файлы с другими именами приложение не трогает.
BACKUP_SUFFIX = ".backup-"

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


class BackupError(MigrationError):
    """Не удалось безопасно скопировать базу — миграцию начинать нельзя."""


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

# --- Версия 2: контакты места и состояние сценария «Места» -----------------

MIGRATION_2_PLACE_CONTACTS_UI = Migration(
    version=2,
    name="place_contacts_and_ui_state",
    statements=(
        # Необязательные контакты для карточки; Stage 19B их только читает.
        "ALTER TABLE places ADD COLUMN address TEXT",
        "ALTER TABLE places ADD COLUMN url TEXT",
        # Состояние Telegram-сценария выбора места хранится отдельно от
        # WIP-поста в user_states. Ключ — Telegram user ID (интерфейсное
        # состояние), payload ограничен фазой и выбранными id.
        """
        CREATE TABLE place_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """,
    ),
)

# --- Версия 3: администраторы мест и состояние админского сценария ----------

MIGRATION_3_PLACE_ADMINS = Migration(
    version=3,
    name="place_admins_and_admin_ui_state",
    statements=(
        # История назначений. Действующая роль = revoked_at IS NULL.
        # Частичный уникальный индекс ниже гарантирует не более одного
        # действующего администратора на место. Отозванные строки остаются
        # для аудита; при замене они помечаются в той же транзакции.
        """
        CREATE TABLE place_admins (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            assigned_by_user_id INTEGER,
            assigned_at DATETIME NOT NULL,
            revoked_by_user_id INTEGER,
            revoked_at DATETIME,
            CONSTRAINT fk_place_admins_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_admins_user_id
                FOREIGN KEY (user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_admins_assigned_by_user_id
                FOREIGN KEY (assigned_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_place_admins_revoked_by_user_id
                FOREIGN KEY (revoked_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX ix_place_admins_place_id ON place_admins (place_id)",
        "CREATE INDEX ix_place_admins_user_id ON place_admins (user_id)",
        """
        CREATE UNIQUE INDEX ux_place_admins_one_active
            ON place_admins (place_id) WHERE revoked_at IS NULL
        """,
        # Интерфейсное состояние кабинета: фаза, выбранное место и кандидат.
        # Права здесь не хранятся и не восстанавливаются из этого JSON.
        """
        CREATE TABLE admin_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL
        )
        """,
    ),
)

# --- Версия 4: аудит административных изменений данных места (Stage 20B) ------

MIGRATION_4_PLACE_EDIT_LOG = Migration(
    version=4,
    name="place_edit_log",
    statements=(
        # Аудит редактирования места (Stage 20B): одна строка на успешную
        # операцию. Секретов нет: здесь только поля места (названия, адрес,
        # ссылка) и внутренние ключи.
        """
        CREATE TABLE place_edit_log (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            actor_user_id INTEGER,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_place_edit_log_place_id
                FOREIGN KEY (place_id)
                REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_place_edit_log_actor_user_id
                FOREIGN KEY (actor_user_id)
                REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX ix_place_edit_log_place_id ON place_edit_log (place_id)",
    ),
)

# --- Версия 5: передачи администратора места (Stage 21) ---------------------

MIGRATION_5_ADMIN_TRANSFERS = Migration(
    version=5,
    name="admin_transfers",
    statements=(
        # Одно предложение передачи. Сырой токен ссылки-приглашения не
        # хранится: только SHA-256. from_assignment_id фиксирует назначение,
        # на основании которого начата передача; после замены администратора
        # предложение становится непригодным для подтверждения.
        """
        CREATE TABLE admin_transfers (
            id INTEGER NOT NULL PRIMARY KEY,
            place_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            from_assignment_id INTEGER,
            to_user_id INTEGER,
            opened_by_user_id INTEGER,
            to_username VARCHAR(255),
            token_hash VARCHAR(64),
            status TEXT NOT NULL,
            created_at DATETIME NOT NULL,
            expires_at DATETIME NOT NULL,
            opened_at DATETIME,
            confirmed_at DATETIME,
            closed_at DATETIME,
            notified_at DATETIME,
            CONSTRAINT ck_admin_transfers_status
                CHECK (status IN (
                    'pending', 'confirmed', 'rejected', 'cancelled',
                    'expired', 'superseded'
                )),
            CONSTRAINT fk_admin_transfers_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_admin_transfers_from_user_id
                FOREIGN KEY (from_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_admin_transfers_from_assignment_id
                FOREIGN KEY (from_assignment_id) REFERENCES place_admins (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_admin_transfers_to_user_id
                FOREIGN KEY (to_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_admin_transfers_opened_by_user_id
                FOREIGN KEY (opened_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        "CREATE INDEX ix_admin_transfers_place_id ON admin_transfers (place_id)",
        "CREATE INDEX ix_admin_transfers_from_user_id "
        "ON admin_transfers (from_user_id)",
        "CREATE INDEX ix_admin_transfers_to_user_id " "ON admin_transfers (to_user_id)",
        # Не более одного активного предложения на место. Просроченные и
        # закрытые строки сохраняются как аудит и новому предложению не мешают.
        """
        CREATE UNIQUE INDEX ux_admin_transfers_one_pending_place
            ON admin_transfers (place_id) WHERE status = 'pending'
        """,
        """
        CREATE INDEX ix_admin_transfers_pending_to_user
            ON admin_transfers (to_user_id) WHERE status = 'pending'
        """,
        # Токен приглашения уникален; NULL у передач известному получателю.
        """
        CREATE UNIQUE INDEX ux_admin_transfers_token_hash
            ON admin_transfers (token_hash) WHERE token_hash IS NOT NULL
        """,
    ),
)

# --- Версия 6: универсальный реестр кирпичиков (Stage 22A) ------------------


MIGRATION_6_BRICKS = Migration(
    version=6,
    name="bricks_registry",
    statements=(
        # Определение поля отделено от значения в анонсе. Системные коды
        # резервируются триггерами ниже; системные определения вставляются
        # этой же миграцией со стабильными ID 1..9.
        """
        CREATE TABLE bricks (
            id INTEGER NOT NULL PRIMARY KEY,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            scope TEXT NOT NULL,
            source TEXT NOT NULL,
            place_id INTEGER,
            owner_user_id INTEGER,
            rules_json TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME,
            CONSTRAINT ck_bricks_type
                CHECK (type IN (
                    'short_text', 'long_text', 'date', 'time', 'url',
                    'number', 'choice'
                )),
            CONSTRAINT ck_bricks_scope
                CHECK (scope IN ('system', 'place', 'personal')),
            CONSTRAINT ck_bricks_source
                CHECK (source IN ('ask', 'place', 'context')),
            CONSTRAINT ck_bricks_status
                CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_bricks_scope_owner
                CHECK (
                    (scope = 'system'
                        AND place_id IS NULL AND owner_user_id IS NULL)
                    OR (scope = 'place'
                        AND place_id IS NOT NULL AND owner_user_id IS NULL)
                    OR (scope = 'personal'
                        AND place_id IS NULL AND owner_user_id IS NOT NULL)
                ),
            CONSTRAINT ck_bricks_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_bricks_code_trimmed
                CHECK (code = trim(code)),
            CONSTRAINT ck_bricks_code_no_space
                CHECK (instr(code, ' ') = 0),
            CONSTRAINT ck_bricks_code_no_brackets
                CHECK (instr(code, '[') = 0 AND instr(code, ']') = 0),
            CONSTRAINT ck_bricks_no_photo_code
                CHECK (code <> 'фото'),
            CONSTRAINT ck_bricks_title_not_empty
                CHECK (length(trim(title)) > 0),
            CONSTRAINT ck_bricks_rules_json_object
                CHECK (
                    rules_json IS NULL
                    OR (json_valid(rules_json)
                        AND json_type(rules_json) = 'object')
                ),
            CONSTRAINT fk_bricks_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_bricks_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_bricks_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        # Один код внутри области. Системные коды глобально уникальны;
        # разные пользователи/места могут использовать одинаковый код.
        """
        CREATE UNIQUE INDEX ux_bricks_system_code
            ON bricks (code) WHERE scope = 'system'
        """,
        """
        CREATE UNIQUE INDEX ux_bricks_place_code
            ON bricks (place_id, code) WHERE scope = 'place'
        """,
        """
        CREATE UNIQUE INDEX ux_bricks_personal_code
            ON bricks (owner_user_id, code) WHERE scope = 'personal'
        """,
        # Индексы доступности/поиска. Частичные уникальные индексы выше
        # обслуживают точный поиск в своей области, эти — общие выборки.
        """
        CREATE INDEX ix_bricks_scope_status_code
            ON bricks (scope, status, code)
        """,
        """
        CREATE INDEX ix_bricks_place_status ON bricks (place_id, status)
        """,
        """
        CREATE INDEX ix_bricks_owner_status ON bricks (owner_user_id, status)
        """,
        """
        CREATE INDEX ix_bricks_type_status ON bricks (type, status)
        """,
        "CREATE INDEX ix_bricks_code ON bricks (code)",
        "CREATE INDEX ix_bricks_title ON bricks (title)",
        # Постоянный внутренний ID нельзя переписать UPDATE-ом.
        """
        CREATE TRIGGER trg_bricks_id_immutable
        BEFORE UPDATE OF id ON bricks
        FOR EACH ROW
        WHEN NEW.id <> OLD.id
        BEGIN
            SELECT RAISE(ABORT, 'bricks.id is immutable');
        END
        """,
        # Код и глобальная принадлежность системного кирпичика неизменяемы.
        """
        CREATE TRIGGER trg_bricks_system_code_immutable
        BEFORE UPDATE OF code ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system' AND NEW.code <> OLD.code
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_bricks_system_owner_immutable
        BEFORE UPDATE OF scope, place_id, owner_user_id ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system'
            AND (NEW.scope <> 'system'
                OR NEW.place_id IS NOT NULL
                OR NEW.owner_user_id IS NOT NULL)
        BEGIN
            SELECT RAISE(ABORT, 'system brick is global');
        END
        """,
        # Системный кирпичик можно архивировать, но не удалить: код остаётся
        # зарезервированным навсегда.
        """
        CREATE TRIGGER trg_bricks_system_no_delete
        BEFORE DELETE ON bricks
        FOR EACH ROW
        WHEN OLD.scope = 'system'
        BEGIN
            SELECT RAISE(ABORT, 'system brick cannot be deleted');
        END
        """,
        # Системные и несистемные коды не могут сосуществовать: [код] всегда
        # означает системный кирпичик и не может быть подменён личным/местным.
        """
        CREATE TRIGGER trg_bricks_reserve_codes_insert
        BEFORE INSERT ON bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.code = NEW.code
              AND (
                  (NEW.scope = 'system' AND b.scope <> 'system')
                  OR (NEW.scope <> 'system' AND b.scope = 'system')
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """,
        """
        CREATE TRIGGER trg_bricks_reserve_codes_update
        BEFORE UPDATE OF code, scope ON bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id <> OLD.id
              AND b.code = NEW.code
              AND (
                  (NEW.scope = 'system' AND b.scope <> 'system')
                  OR (NEW.scope <> 'system' AND b.scope = 'system')
              )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """,
        # Системный реестр Stage 22A. ID и коды стабильны; изменение
        # определения — только новой миграцией, а не повторным сидированием.
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (1, 'название', 'Название мероприятия', 'short_text',
             'system', 'ask', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (2, 'дата', 'Дата', 'date', 'system', 'ask',
             '{"horizon_months": 12}', 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (3, 'время_начала', 'Время начала', 'time',
             'system', 'ask', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (4, 'время_окончания', 'Время окончания', 'time',
             'system', 'ask', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (5, 'место', 'Название места', 'short_text',
             'system', 'place', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (6, 'адрес_места', 'Адрес места', 'short_text',
             'system', 'place', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (7, 'ссылка_места', 'Ссылка места', 'url',
             'system', 'place', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (8, 'описание', 'Описание', 'long_text',
             'system', 'ask', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at)
        VALUES
            (9, 'контакт', 'Контакт', 'short_text',
             'system', 'ask', NULL, 'active', NULL,
             CURRENT_TIMESTAMP, NULL)
        """,
    ),
)

# --- Версия 7: управление личными кирпичиками (Stage 22B) ------------------


MIGRATION_7_BRICK_MANAGEMENT = Migration(
    version=7,
    name="brick_management",
    statements=(
        # Ревизия — явная оптимистическая блокировка для редактирования.
        # Она не заменяет id и не участвует в ссылках шаблонов.
        "ALTER TABLE bricks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
        # Состояние Telegram-конструктора отделено от WIP-поста, «Мест» и
        # админки: переход в другой раздел не смешивает текстовые вводы.
        """
        CREATE TABLE brick_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_brick_sessions_payload_object CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            )
        )
        """,
        # Ревизия может только расти. Условный UPDATE в сервисе дополнительно
        # проверяет ожидаемую ревизию, поэтому потерянное обновление не
        # проходит даже при конкурентном редактировании.
        """
        CREATE TRIGGER trg_bricks_revision_increases
        BEFORE UPDATE OF revision ON bricks
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'brick revision must increase');
        END
        """,
    ),
)

DEFAULT_MIGRATIONS: tuple[Migration, ...] = (
    MIGRATION_1_USERS_PLACES,
    MIGRATION_2_PLACE_CONTACTS_UI,
    MIGRATION_3_PLACE_ADMINS,
    MIGRATION_4_PLACE_EDIT_LOG,
    MIGRATION_5_ADMIN_TRANSFERS,
    MIGRATION_6_BRICKS,
    MIGRATION_7_BRICK_MANAGEMENT,
)

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


def _open_source_connection(path: str) -> sqlite3.Connection:
    """
    Соединение с исходной базой для чтения/backup.

    Сначала пробуем строго read-only URI: backup не должен менять рабочую
    базу. Для WAL-базы после аварийного завершения может отсутствовать
    `-shm`; в этом случае SQLite иногда не открывает файл только для чтения.
    Тогда используется обычное соединение (оно не меняет пользовательские
    данные, но позволяет SQLite корректно прочитать WAL).
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except sqlite3.Error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    return sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)


def _has_pending_migrations(path: str, migrations: Sequence[Migration]) -> bool:
    """
    Есть ли что применять — без записи в базу.

    Новый/пустой файл ещё не содержит пользовательских данных: копия не
    нужна. Для существующей базы читаем только журнал; его отсутствие на
    старой базе означает, что ожидают все миграции.
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        connection = _open_source_connection(path)
        try:
            journal_exists = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            ).fetchone()
            if journal_exists is None:
                return bool(migrations)
            row = connection.execute(
                "SELECT count(*) FROM schema_migrations"
            ).fetchone()
            applied = int(row[0]) if row is not None else 0
            return applied < len(migrations)
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(
            "Не удалось прочитать состояние базы перед миграцией, поэтому "
            f"безопасная резервная копия невозможна: {exc}"
        ) from exc


def _backup_destination(path: str) -> Path:
    """Уникальное имя рядом с базой; существующие файлы не перезаписываются."""
    base = Path(path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = base.with_name(f"{base.name}{BACKUP_SUFFIX}{stamp}-p{os.getpid()}")
    counter = 1
    while candidate.exists():
        candidate = base.with_name(
            f"{base.name}{BACKUP_SUFFIX}{stamp}-p{os.getpid()}-{counter}"
        )
        counter += 1
    return candidate


def create_database_backup(path: str) -> Path:
    """
    Создаёт согласованную копию SQLite через Backup API.

    Прямое копирование только основного файла при WAL потеряло бы последние
    зафиксированные транзакции. `Connection.backup()` читает базу как SQLite,
    включая WAL, и создаёт цельный снимок. Копия сначала пишется во временный
    файл, проверяется `PRAGMA integrity_check` и только затем атомарно
    переименовывается. Готовые копии никогда не удаляются.
    """
    source_path = Path(path)
    temp_name: str | None = None
    source: sqlite3.Connection | None = None
    target: sqlite3.Connection | None = None
    try:
        if not source_path.exists() or source_path.stat().st_size == 0:
            raise BackupError(f"Нет базы для копирования: {path}")
        destination = _backup_destination(path)
        fd, temp_name = tempfile.mkstemp(
            prefix=f"{source_path.name}.backup-tmp-", dir=str(source_path.parent)
        )
        os.close(fd)
        # Не удаляем файл: mkstemp сразу создал его с правами 0600. SQLite
        # открывает пустой файл как пустую базу и пишет копию уже без окна,
        # в котором файл мог бы получить более широкие права.
        source = _open_source_connection(path)
        target = sqlite3.connect(
            temp_name,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        source.backup(target)
        row = target.execute("PRAGMA integrity_check").fetchone()
        if row is None or str(row[0]).lower() != "ok":
            raise BackupError("Резервная копия не прошла проверку целостности SQLite.")
        target.close()
        target = None
        source.close()
        source = None
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, destination)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(
            f"Не удалось создать резервную копию перед миграцией: {exc}"
        ) from exc
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        if temp_name is not None and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                logger.warning("Не удалось удалить временный файл копии %s", temp_name)
    logger.info("Создана резервная копия базы перед миграцией: %s", destination)
    return destination


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

    # Защита пользовательских данных: копия создаётся ДО первой блокировки
    # и любой DDL. Если копию сделать нельзя, миграция не начинается.
    if _has_pending_migrations(path, migrations):
        create_database_backup(path)

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
