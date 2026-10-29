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
    """
    Одна неизменяемая миграция: версия, имя и набор DDL/DML-шагов.

    `requires_tables` — таблицы, без которых шаги неприменимы (например,
    архив легаси-таблицы на давно очищенной базе). Если хотя бы одной нет,
    шаги пропускаются, а версия всё равно фиксируется в журнале: миграция
    идемпотентна и на пустой/новой базе, где таблица никогда не создавалась.
    """

    version: int
    name: str
    statements: tuple[str, ...]
    requires_tables: tuple[str, ...] = ()

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

# --- Версия 8: шаблоны, версии, привязки и снимки (Stage 23) ----------------


MIGRATION_8_TEMPLATES = Migration(
    version=8,
    name="templates_versions_snapshots",
    statements=(
        # Шаблон хранит только стабильную идентичность. Текст живёт в
        # неизменяемых версиях, а revision одновременно является номером
        # текущей версии и ожидаемой ревизией оптимистичной блокировки.
        """
        CREATE TABLE templates (
            id INTEGER NOT NULL PRIMARY KEY,
            kind TEXT NOT NULL,
            place_id INTEGER,
            owner_user_id INTEGER,
            name TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_templates_kind
                CHECK (kind IN ('standard', 'personal')),
            CONSTRAINT ck_templates_shape
                CHECK (
                    (kind = 'standard'
                        AND place_id IS NOT NULL
                        AND owner_user_id IS NULL
                        AND name IS NULL)
                    OR (kind = 'personal'
                        AND place_id IS NULL
                        AND owner_user_id IS NOT NULL
                        AND length(trim(name)) > 0)
                ),
            CONSTRAINT ck_templates_revision CHECK (revision >= 1),
            CONSTRAINT fk_templates_place_id
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_templates_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_templates_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        # Ровно один стандартный шаблон на место. Личные шаблоны не входят
        # в этот частичный индекс и могут иметь одинаковые названия.
        """
        CREATE UNIQUE INDEX ux_templates_one_standard_place
            ON templates (place_id) WHERE kind = 'standard'
        """,
        "CREATE INDEX ix_templates_owner_kind ON templates (owner_user_id, kind)",
        "CREATE INDEX ix_templates_place_kind ON templates (place_id, kind)",
        # Вид и владелец шаблона неизменяемы: стандарт нельзя превратить в
        # личный или перенести подменой place_id/owner_user_id.
        """
        CREATE TRIGGER trg_templates_identity_immutable
        BEFORE UPDATE OF kind, place_id, owner_user_id ON templates
        FOR EACH ROW
        WHEN NEW.kind <> OLD.kind
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.owner_user_id IS NOT OLD.owner_user_id
        BEGIN
            SELECT RAISE(ABORT, 'template identity is immutable');
        END
        """,
        # Ревизия может только расти; сервис дополнительно сравнивает её
        # условным UPDATE, поэтому более новая версия не перезаписывается.
        """
        CREATE TRIGGER trg_templates_revision_increases
        BEFORE UPDATE OF revision ON templates
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'template revision must increase');
        END
        """,
        # Стандарт нельзя физически удалить и оставить место без шаблона.
        """
        CREATE TRIGGER trg_templates_standard_no_delete
        BEFORE DELETE ON templates
        FOR EACH ROW
        WHEN OLD.kind = 'standard'
            AND EXISTS (SELECT 1 FROM places WHERE id = OLD.place_id)
        BEGIN
            SELECT RAISE(ABORT, 'standard template cannot be deleted');
        END
        """,
        # Версии содержат все три секции одной строкой: header/body/footer
        # одной версии не могут быть собраны из разных сохранений.
        """
        CREATE TABLE template_versions (
            id INTEGER NOT NULL PRIMARY KEY,
            template_id INTEGER NOT NULL,
            version INTEGER NOT NULL,
            header TEXT NOT NULL,
            body TEXT NOT NULL,
            footer TEXT NOT NULL,
            created_by_user_id INTEGER,
            created_at DATETIME NOT NULL,
            builtin_version INTEGER,
            CONSTRAINT uq_template_versions_template_version
                UNIQUE (template_id, version),
            CONSTRAINT ck_template_versions_version CHECK (version >= 1),
            CONSTRAINT ck_template_versions_builtin_version
                CHECK (builtin_version IS NULL OR builtin_version >= 1),
            CONSTRAINT ck_template_versions_not_all_empty
                CHECK (header <> '' OR body <> '' OR footer <> ''),
            CONSTRAINT fk_template_versions_template_id
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_template_versions_created_by_user_id
                FOREIGN KEY (created_by_user_id) REFERENCES users (id)
                ON DELETE SET NULL
        )
        """,
        # История неизменяема: правка только добавляет следующую версию.
        """
        CREATE TRIGGER trg_template_versions_no_update
        BEFORE UPDATE ON template_versions
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template versions are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_versions_no_delete
        BEFORE DELETE ON template_versions
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM templates WHERE id = OLD.template_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'template versions cannot be deleted');
        END
        """,
        # Исполнимая связь — постоянный brick_id, а не повторный поиск по
        # коду. Один кирпичик может использоваться версией один раз.
        """
        CREATE TABLE template_version_bricks (
            template_version_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            PRIMARY KEY (template_version_id, brick_id),
            CONSTRAINT fk_template_version_bricks_version
                FOREIGN KEY (template_version_id)
                REFERENCES template_versions (id) ON DELETE CASCADE,
            CONSTRAINT fk_template_version_bricks_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX ix_template_version_bricks_brick_id
            ON template_version_bricks (brick_id)
        """,
        """
        CREATE TRIGGER trg_template_version_bricks_no_update
        BEFORE UPDATE ON template_version_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_version_bricks_no_delete
        BEFORE DELETE ON template_version_bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM template_versions
            WHERE id = OLD.template_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'template bindings cannot be deleted');
        END
        """,
        # Снимок — самостоятельная копия. Составной FK фиксирует конкретную
        # версию источника; тексты и связи копируются в собственные таблицы.
        """
        CREATE TABLE template_snapshots (
            id INTEGER NOT NULL PRIMARY KEY,
            owner_user_id INTEGER NOT NULL,
            source_template_id INTEGER NOT NULL,
            source_template_version INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            source_name TEXT,
            source_place_id INTEGER,
            source_owner_user_id INTEGER,
            header TEXT NOT NULL,
            body TEXT NOT NULL,
            footer TEXT NOT NULL,
            builtin_version INTEGER,
            idempotency_key TEXT,
            created_at DATETIME NOT NULL,
            CONSTRAINT fk_template_snapshots_owner_user_id
                FOREIGN KEY (owner_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_version
                FOREIGN KEY (source_template_id, source_template_version)
                REFERENCES template_versions (template_id, version)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_place_id
                FOREIGN KEY (source_place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_template_snapshots_source_owner_user_id
                FOREIGN KEY (source_owner_user_id) REFERENCES users (id)
                ON DELETE SET NULL,
            CONSTRAINT ck_template_snapshots_source_kind
                CHECK (source_kind IN ('standard', 'personal')),
            CONSTRAINT ck_template_snapshots_source_version
                CHECK (source_template_version >= 1),
            CONSTRAINT ck_template_snapshots_builtin_version
                CHECK (builtin_version IS NULL OR builtin_version >= 1),
            CONSTRAINT ck_template_snapshots_not_all_empty
                CHECK (header <> '' OR body <> '' OR footer <> ''),
            CONSTRAINT ck_template_snapshots_idempotency_key
                CHECK (
                    idempotency_key IS NULL
                    OR length(trim(idempotency_key)) > 0
                )
        )
        """,
        """
        CREATE UNIQUE INDEX ux_template_snapshots_idempotency
            ON template_snapshots (owner_user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE INDEX ix_template_snapshots_owner_created
            ON template_snapshots (owner_user_id, created_at)
        """,
        """
        CREATE INDEX ix_template_snapshots_source
            ON template_snapshots (source_template_id, source_template_version)
        """,
        # Снимок нельзя изменить или удалить: он остаётся ровно тем
        # состоянием, которое было выбрано для черновика.
        """
        CREATE TRIGGER trg_template_snapshots_no_update
        BEFORE UPDATE ON template_snapshots
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_snapshots_no_delete
        BEFORE DELETE ON template_snapshots
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template snapshots cannot be deleted');
        END
        """,
        """
        CREATE TABLE template_snapshot_bricks (
            template_snapshot_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            PRIMARY KEY (template_snapshot_id, brick_id),
            CONSTRAINT fk_template_snapshot_bricks_snapshot
                FOREIGN KEY (template_snapshot_id)
                REFERENCES template_snapshots (id) ON DELETE CASCADE,
            CONSTRAINT fk_template_snapshot_bricks_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX ix_template_snapshot_bricks_brick_id
            ON template_snapshot_bricks (brick_id)
        """,
        """
        CREATE TRIGGER trg_template_snapshot_bricks_no_update
        BEFORE UPDATE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_snapshot_bricks_no_delete
        BEFORE DELETE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings cannot be deleted');
        END
        """,
        # Backfill существующих мест. Строки, уже имеющие стандарт, не
        # перезаписываются; DML идемпотентен по условиям NOT EXISTS.
        """
        INSERT INTO templates
            (kind, place_id, owner_user_id, name, revision,
             created_by_user_id, created_at, updated_at)
        SELECT
            'standard', p.id, NULL, NULL, 1, NULL,
            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM places AS p
        WHERE NOT EXISTS (
            SELECT 1 FROM templates AS t
            WHERE t.kind = 'standard' AND t.place_id = p.id
        )
        """,
        """
        INSERT INTO template_versions
            (template_id, version, header, body, footer,
             created_by_user_id, created_at, builtin_version)
        SELECT
            t.id, 1, '[название]',
            '🗓 [дата]
🕒 [время_начала] – [время_окончания]
📍 [место]
[адрес_места]

[описание]',
            '🔗 [ссылка_места]
📞 [контакт]',
            NULL, CURRENT_TIMESTAMP, 1
        FROM templates AS t
        WHERE t.kind = 'standard'
          AND NOT EXISTS (
              SELECT 1 FROM template_versions AS v
              WHERE v.template_id = t.id AND v.version = 1
          )
        """,
        """
        INSERT INTO template_version_bricks (template_version_id, brick_id)
        SELECT v.id, b.id
        FROM template_versions AS v
        JOIN templates AS t ON t.id = v.template_id
        JOIN bricks AS b
          ON b.id IN (1, 2, 3, 4, 5, 6, 7, 8, 9)
         AND b.scope = 'system'
        WHERE t.kind = 'standard'
          AND v.version = 1
          AND v.builtin_version = 1
        """,
    ),
)

# --- Версия 9: коды в привязках шаблонов (Stage 24) -------------------------

MIGRATION_9_BINDING_CODES = Migration(
    version=9,
    name="template_binding_codes",
    statements=(
        # Код в привязках — мост между сохранённой версией и исполнением.
        # Рендер восстанавливает brick_id из этих строк, не разрешая
        # текстовый код заново: переименование кирпичика после сохранения
        # не должно незаметно подменять значение.
        "ALTER TABLE template_version_bricks ADD COLUMN code TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE template_snapshot_bricks ADD COLUMN code TEXT NOT NULL DEFAULT ''",
        # Триггеры неизменяемости привязок временно снимаются: backfill
        # заполняет код для уже существующих строк; без этого UPDATE
        # заблокирован триггером миграции 8.
        "DROP TRIGGER IF EXISTS trg_template_version_bricks_no_update",
        "DROP TRIGGER IF EXISTS trg_template_version_bricks_no_delete",
        "DROP TRIGGER IF EXISTS trg_template_snapshot_bricks_no_update",
        "DROP TRIGGER IF EXISTS trg_template_snapshot_bricks_no_delete",
        # Backfill: код берём из bricks по brick_id, не предполагая структуру
        # текста. Каждая связь версии/снимка получает код своего кирпичика.
        """
        UPDATE template_version_bricks
        SET code = (
            SELECT bricks.code FROM bricks
            WHERE bricks.id = template_version_bricks.brick_id
        )
        WHERE code = ''
        """,
        """
        UPDATE template_snapshot_bricks
        SET code = (
            SELECT bricks.code FROM bricks
            WHERE bricks.id = template_snapshot_bricks.brick_id
        )
        WHERE code = ''
        """,
        # Триггеры неизменяемости восстанавливаются после backfill.
        """
        CREATE TRIGGER trg_template_version_bricks_no_update
        BEFORE UPDATE ON template_version_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'template bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_version_bricks_no_delete
        BEFORE DELETE ON template_version_bricks
        FOR EACH ROW
        WHEN EXISTS (
            SELECT 1 FROM template_versions
            WHERE id = OLD.template_version_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'template bindings cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER trg_template_snapshot_bricks_no_update
        BEFORE UPDATE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings are immutable');
        END
        """,
        """
        CREATE TRIGGER trg_template_snapshot_bricks_no_delete
        BEFORE DELETE ON template_snapshot_bricks
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'snapshot bindings cannot be deleted');
        END
        """,
        # Один код может быть привязан к одной версии ровно один раз, чтобы
        # рендер однозначно восстанавливал brick_id по коду из сохранённой
        # версии (без повторного разрешения кода при исполнении).
        """
        CREATE UNIQUE INDEX ux_template_version_bricks_version_code
            ON template_version_bricks (template_version_id, code)
        """,
        """
        CREATE UNIQUE INDEX ux_template_snapshot_bricks_snapshot_code
            ON template_snapshot_bricks (template_snapshot_id, code)
        """,
    ),
)

# --- Версия 10: интерфейсное состояние редактора шаблонов (Stage 25) -------

MIGRATION_10_TEMPLATE_SESSIONS = Migration(
    version=10,
    name="template_sessions",
    statements=(
        """
        CREATE TABLE template_sessions (
            telegram_user_id INTEGER NOT NULL PRIMARY KEY,
            payload TEXT NOT NULL CHECK (
                json_valid(payload) AND json_type(payload) = 'object'
            ),
            updated_at DATETIME NOT NULL
        )
        """,
    ),
)

# --- Версия 11: предложения новых кирпичиков (Stage 26) ---------------------


MIGRATION_11_BRICK_PROPOSALS = Migration(
    version=11,
    name="brick_proposals",
    statements=(
        # Снимок определения на момент отправки. `source_brick_id` — именно
        # исходный личный кирпичик, а не будущая созданная копия. Срок
        # действия отсутствует: pending живёт, пока автор или администратор
        # явно не закроет предложение.
        """
        CREATE TABLE brick_proposals (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            scope TEXT NOT NULL,
            place_id INTEGER,
            source_brick_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            code TEXT NOT NULL,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            rules_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at DATETIME NOT NULL,
            decided_at DATETIME,
            decided_by_user_id INTEGER,
            approved_brick_id INTEGER,
            decision_note TEXT,
            CONSTRAINT ck_brick_proposals_scope
                CHECK (scope IN ('place', 'system')),
            CONSTRAINT ck_brick_proposals_target
                CHECK (
                    (scope = 'place' AND place_id IS NOT NULL)
                    OR (scope = 'system' AND place_id IS NULL)
                ),
            CONSTRAINT ck_brick_proposals_status
                CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn')),
            CONSTRAINT ck_brick_proposals_type
                CHECK (type IN (
                    'short_text', 'long_text', 'date', 'time', 'url',
                    'number', 'choice'
                )),
            CONSTRAINT ck_brick_proposals_source
                CHECK (source IN ('ask', 'place', 'context')),
            CONSTRAINT ck_brick_proposals_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_brick_proposals_code_trimmed
                CHECK (code = trim(code)),
            CONSTRAINT ck_brick_proposals_code_no_space
                CHECK (instr(code, ' ') = 0),
            CONSTRAINT ck_brick_proposals_code_no_brackets
                CHECK (instr(code, '[') = 0 AND instr(code, ']') = 0),
            CONSTRAINT ck_brick_proposals_no_photo_code
                CHECK (code <> 'фото'),
            CONSTRAINT ck_brick_proposals_title_not_empty
                CHECK (length(trim(title)) > 0),
            CONSTRAINT ck_brick_proposals_rules_json_object
                CHECK (
                    rules_json IS NULL
                    OR (json_valid(rules_json)
                        AND json_type(rules_json) = 'object')
                ),
            CONSTRAINT ck_brick_proposals_decision_shape
                CHECK (
                    (status = 'pending'
                        AND decided_at IS NULL
                        AND decided_by_user_id IS NULL
                        AND approved_brick_id IS NULL
                        AND decision_note IS NULL)
                    OR (status = 'approved'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NOT NULL
                        AND decision_note IS NULL)
                    OR (status = 'rejected'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NULL)
                    OR (status = 'withdrawn'
                        AND decided_at IS NOT NULL
                        AND decided_by_user_id IS NOT NULL
                        AND approved_brick_id IS NULL
                        AND decision_note IS NULL)
                ),
            CONSTRAINT fk_brick_proposals_author
                FOREIGN KEY (author_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_source_brick
                FOREIGN KEY (source_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_decided_by
                FOREIGN KEY (decided_by_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_proposals_approved_brick
                FOREIGN KEY (approved_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX ix_brick_proposals_author_status_created
            ON brick_proposals (author_user_id, status, created_at)
        """,
        """
        CREATE INDEX ix_brick_proposals_place_status_created
            ON brick_proposals (place_id, status, created_at)
        """,
        """
        CREATE INDEX ix_brick_proposals_status_scope_created
            ON brick_proposals (status, scope, created_at)
        """,
        """
        CREATE INDEX ix_brick_proposals_source_brick
            ON brick_proposals (source_brick_id)
        """,
        # Один одобренный кирпичик не может быть результатом двух предложений.
        """
        CREATE UNIQUE INDEX ux_brick_proposals_approved_brick
            ON brick_proposals (approved_brick_id)
            WHERE approved_brick_id IS NOT NULL
        """,
        # Снимок — не указатель: исходное определение можно редактировать или
        # архивировать, но уже отправленное предложение не переписывается.
        """
        CREATE TRIGGER trg_brick_proposals_snapshot_immutable
        BEFORE UPDATE OF
            author_user_id, scope, place_id, source_brick_id, title, code,
            type, source, rules_json, created_at
        ON brick_proposals
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.scope IS NOT OLD.scope
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.source_brick_id IS NOT OLD.source_brick_id
            OR NEW.title IS NOT OLD.title
            OR NEW.code IS NOT OLD.code
            OR NEW.type IS NOT OLD.type
            OR NEW.source IS NOT OLD.source
            OR NEW.rules_json IS NOT OLD.rules_json
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal snapshot is immutable');
        END
        """,
        # Решение принимается ровно один раз: pending → approved/rejected/
        # withdrawn. Повторный callback не может изменить терминальный статус.
        """
        CREATE TRIGGER trg_brick_proposals_single_transition
        BEFORE UPDATE OF status ON brick_proposals
        FOR EACH ROW
        WHEN OLD.status <> 'pending'
            OR NEW.status NOT IN ('approved', 'rejected', 'withdrawn')
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal decision is final');
        END
        """,
        # После решения поля аудита и ссылка на созданный кирпичик неизменяемы.
        """
        CREATE TRIGGER trg_brick_proposals_decision_immutable
        BEFORE UPDATE ON brick_proposals
        FOR EACH ROW
        WHEN OLD.status <> 'pending'
            AND (
                NEW.status IS NOT OLD.status
                OR NEW.decided_at IS NOT OLD.decided_at
                OR NEW.decided_by_user_id IS NOT OLD.decided_by_user_id
                OR NEW.approved_brick_id IS NOT OLD.approved_brick_id
                OR NEW.decision_note IS NOT OLD.decision_note
            )
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal decision is immutable');
        END
        """,
        # История предложений не удаляется физически; терминальные строки
        # остаются для аудита и отображения автору.
        """
        CREATE TRIGGER trg_brick_proposals_no_delete
        BEFORE DELETE ON brick_proposals
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal history cannot be deleted');
        END
        """,
        # Источник — действующий личный кирпичик именно этого автора. После
        # отправки его можно архивировать/редактировать: триггер проверяет
        # только INSERT, а снимок уже зафиксирован.
        """
        CREATE TRIGGER trg_brick_proposals_source_owner
        BEFORE INSERT ON brick_proposals
        FOR EACH ROW
        WHEN NOT EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id = NEW.source_brick_id
              AND b.scope = 'personal'
              AND b.owner_user_id = NEW.author_user_id
              AND b.status = 'active'
        )
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal source must be an active personal brick');
        END
        """,
        # Одобрение обязано ссылаться на реально созданный отдельный кирпичик
        # той же области/места и того же снимка. Исходный личный кирпичик
        # одобрить вместо копии нельзя.
        """
        CREATE TRIGGER trg_brick_proposals_approval_link
        BEFORE UPDATE OF status, approved_brick_id ON brick_proposals
        FOR EACH ROW
        WHEN NEW.status = 'approved' AND NOT EXISTS (
            SELECT 1 FROM bricks b
            WHERE b.id = NEW.approved_brick_id
              AND b.id <> OLD.source_brick_id
              AND b.status = 'active'
              AND b.scope = NEW.scope
              AND b.place_id IS NEW.place_id
              AND b.owner_user_id IS NULL
              AND b.code = NEW.code
              AND b.title = NEW.title
              AND b.type = NEW.type
              AND b.source = NEW.source
              AND b.rules_json IS NEW.rules_json
        )
        BEGIN
            SELECT RAISE(ABORT, 'approved brick does not match proposal');
        END
        """,
        # Узкое исключение для продвижения личного кода в системный.
        #
        # Общее правило Stage 22A («системный и несистемный код не
        # сосуществуют») остаётся: прямой INSERT системного кирпичика с кодом
        # личного по-прежнему запрещён. Исключение действует только в момент
        # одобрения глобального предложения: существует ровно один несистемный
        # носитель кода — исходный личный кирпичик, и pending-снимок полностью
        # совпадает с вставляемым системным кирпичиком. Так требование «не
        # менять исходный личный кирпичик» не нарушается, а код становится
        # системным для всех будущих определений.
        "DROP TRIGGER trg_bricks_reserve_codes_insert",
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
        AND NOT (
            NEW.scope = 'system'
            AND EXISTS (
                SELECT 1
                FROM brick_proposals p
                JOIN bricks src ON src.id = p.source_brick_id
                WHERE p.scope = 'system'
                  AND p.status = 'pending'
                  AND p.code = NEW.code
                  AND p.title = NEW.title
                  AND p.type = NEW.type
                  AND p.source = NEW.source
                  AND p.rules_json IS NEW.rules_json
                  AND src.code = NEW.code
                  AND (
                      SELECT count(*) FROM bricks b2
                      WHERE b2.code = NEW.code AND b2.scope <> 'system'
                  ) = 1
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """,
        # Продвинутый личный кирпичик сохраняет свой код, поэтому обычное
        # редактирование названия/настроек не должно упираться в системный
        # дубль. Разрешаем UPDATE только при неизменных коде и области
        # несистемного кирпичика; смена кода на системный по-прежнему
        # запрещена.
        "DROP TRIGGER trg_bricks_reserve_codes_update",
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
        AND NOT (
            NEW.scope = OLD.scope
            AND NEW.scope <> 'system'
            AND NEW.code = OLD.code
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """,
    ),
)


# --- Версия 12: состояние адаптивного мастера анонсов (Stage 27A) ----------


MIGRATION_12_ANNOUNCEMENT_WIZARD = Migration(
    version=12,
    name="announcement_wizard",
    statements=(
        # Незавершённый анонс нового мастера. Хранится отдельно от легаси
        # `drafts`/`user_states`: старый WIP не конвертируется и не меняется.
        # place_* — снимок выбранного места на момент подтверждения; NULL
        # означает отсутствующее значение, а не пустую строку/заглушку.
        """
        CREATE TABLE announcement_drafts (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            place_id INTEGER NOT NULL,
            place_name_id INTEGER,
            place_display_name TEXT NOT NULL,
            place_address TEXT,
            place_url TEXT,
            template_id INTEGER,
            template_version INTEGER,
            template_snapshot_id INTEGER,
            stage TEXT NOT NULL DEFAULT 'place',
            revision INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_drafts_stage
                CHECK (stage IN (
                    'place', 'template', 'questions', 'extras',
                    'preview', 'completed'
                )),
            CONSTRAINT ck_announcement_drafts_revision
                CHECK (revision >= 1),
            CONSTRAINT ck_announcement_drafts_display_name
                CHECK (length(trim(place_display_name)) > 0),
            CONSTRAINT ck_announcement_drafts_address
                CHECK (place_address IS NULL OR length(trim(place_address)) > 0),
            CONSTRAINT ck_announcement_drafts_url
                CHECK (place_url IS NULL OR length(trim(place_url)) > 0),
            CONSTRAINT ck_announcement_drafts_idempotency
                CHECK (
                    idempotency_key IS NULL
                    OR length(trim(idempotency_key)) > 0
                ),
            CONSTRAINT ck_announcement_drafts_template_shape
                CHECK (
                    (template_id IS NULL
                        AND template_version IS NULL
                        AND template_snapshot_id IS NULL)
                    OR (template_id IS NOT NULL
                        AND template_version IS NOT NULL
                        AND template_version >= 1
                        AND template_snapshot_id IS NOT NULL)
                ),
            CONSTRAINT fk_announcement_drafts_author
                FOREIGN KEY (author_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place_name
                FOREIGN KEY (place_name_id) REFERENCES place_names (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_announcement_drafts_template
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_snapshot
                FOREIGN KEY (template_snapshot_id) REFERENCES template_snapshots (id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX ix_announcement_drafts_author_updated
            ON announcement_drafts (author_user_id, updated_at)
        """,
        # Один снимок принадлежит ровно одному анонсу: живая версия шаблона
        # и чужой черновик не могут использовать его повторно.
        """
        CREATE UNIQUE INDEX ux_announcement_drafts_snapshot
            ON announcement_drafts (template_snapshot_id)
            WHERE template_snapshot_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX ux_announcement_drafts_idempotency
            ON announcement_drafts (author_user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """,
        # Значение конкретного анонса. Определение кирпичика остаётся в
        # `bricks`; здесь только постоянный brick_id и нормализованный payload.
        # Отсутствие строки — «значение не задано»; state='empty_optional' —
        # явно допустимое пустое значение (разные состояния мастера).
        """
        CREATE TABLE announcement_values (
            id INTEGER NOT NULL PRIMARY KEY,
            draft_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            state TEXT NOT NULL,
            value_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_values_state
                CHECK (state IN ('valid', 'empty_optional')),
            CONSTRAINT ck_announcement_values_payload
                CHECK (
                    (state = 'valid'
                        AND value_json IS NOT NULL
                        AND json_valid(value_json)
                        AND json_type(value_json) = 'object')
                    OR (state = 'empty_optional' AND value_json IS NULL)
                ),
            CONSTRAINT fk_announcement_values_draft
                FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_announcement_values_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_announcement_values_draft_brick
                UNIQUE (draft_id, brick_id)
        )
        """,
        """
        CREATE INDEX ix_announcement_values_brick
            ON announcement_values (brick_id)
        """,
        # Дополнительный кирпичик существует только в этом анонсе. Секция и
        # позиция задают вставку, occurrence оставлен для будущих повторяемых
        # экземпляров; Stage 27A создаёт occurrence=1.
        """
        CREATE TABLE announcement_extras (
            id INTEGER NOT NULL PRIMARY KEY,
            draft_id INTEGER NOT NULL,
            brick_id INTEGER NOT NULL,
            section TEXT NOT NULL,
            position TEXT NOT NULL DEFAULT 'end',
            occurrence INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_extras_section
                CHECK (section IN ('header', 'body', 'footer')),
            CONSTRAINT ck_announcement_extras_position
                CHECK (position IN ('start', 'end')),
            CONSTRAINT ck_announcement_extras_occurrence
                CHECK (occurrence >= 1),
            CONSTRAINT fk_announcement_extras_draft
                FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_announcement_extras_brick
                FOREIGN KEY (brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT uq_announcement_extras_draft_brick_occurrence
                UNIQUE (draft_id, brick_id, occurrence)
        )
        """,
        """
        CREATE INDEX ix_announcement_extras_draft
            ON announcement_extras (draft_id)
        """,
        """
        CREATE INDEX ix_announcement_extras_brick
            ON announcement_extras (brick_id)
        """,
        # Ревизия растёт только сервисом: условный UPDATE ... WHERE revision
        # остаётся настоящей optimistic locking даже при прямом SQL.
        """
        CREATE TRIGGER trg_announcement_drafts_revision_increases
        BEFORE UPDATE OF revision ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft revision must increase');
        END
        """,
        # Автор, место и ключ идемпотентности не переписываются: подмена ID
        # не превращает чужой анонс в свой и не меняет выбранное место.
        """
        CREATE TRIGGER trg_announcement_drafts_identity_immutable
        BEFORE UPDATE OF author_user_id, place_id, idempotency_key, created_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.idempotency_key IS NOT OLD.idempotency_key
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft identity is immutable');
        END
        """,
        # Однажды выбранный snapshot шаблона нельзя подменить: правка живого
        # шаблона или повторный выбор не переписывают начатый анонс.
        """
        CREATE TRIGGER trg_announcement_drafts_template_immutable
        BEFORE UPDATE OF template_id, template_version, template_snapshot_id
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.template_snapshot_id IS NOT NULL
            AND (
                NEW.template_id IS NOT OLD.template_id
                OR NEW.template_version IS NOT OLD.template_version
                OR NEW.template_snapshot_id IS NOT OLD.template_snapshot_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement template snapshot is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_values_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, created_at ON announcement_values
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement value identity is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_extras_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, occurrence, created_at
        ON announcement_extras
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.occurrence IS NOT OLD.occurrence
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement extra identity is immutable');
        END
        """,
    ),
)


# --- Версия 13: фото и публикация нового мастера (Stage 27C) ----------------


MIGRATION_13_ANNOUNCEMENT_PUBLICATION = Migration(
    version=13,
    name="announcement_photos_publication",
    statements=(
        # Фотографии — отдельная коллекция вложений анонса, а не кирпичики
        # и не часть HTML-текста. NULL/[] означают «фотографий нет».
        "ALTER TABLE announcement_drafts ADD COLUMN photos TEXT",
        # Замороженный payload публикации. Пока статус не начат, все поля
        # NULL. После начала отправки текст/фото больше не пересобираются.
        "ALTER TABLE announcement_drafts ADD COLUMN publication_status TEXT",
        "ALTER TABLE announcement_drafts ADD COLUMN publication_text TEXT",
        "ALTER TABLE announcement_drafts ADD COLUMN publication_photos TEXT",
        "ALTER TABLE announcement_drafts ADD COLUMN publication_progress TEXT",
        "ALTER TABLE announcement_drafts ADD COLUMN publication_error TEXT",
        "ALTER TABLE announcement_drafts ADD COLUMN publication_started_at DATETIME",
        "ALTER TABLE announcement_drafts ADD COLUMN published_at DATETIME",
        """
        CREATE INDEX ix_announcement_drafts_publication_status
            ON announcement_drafts (publication_status)
        """,
        # JSON-проверки для фото/прогресса: ALTER TABLE в SQLite не умеет
        # добавлять CHECK, поэтому те же ограничения держат триггеры.
        """
        CREATE TRIGGER trg_announcement_drafts_media_json_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_media_json_update
        BEFORE UPDATE OF photos, publication_photos, publication_progress
        ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_status_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_status_update
        BEFORE UPDATE OF publication_status ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
        """,
        # Форма публикационных полей: начатая публикация всегда имеет
        # замороженный текст и список фото; успешная — время подтверждения.
        # Сброс к редактированию разрешён только в полностью пустое состояние.
        """
        CREATE TRIGGER trg_announcement_drafts_publication_shape_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_shape
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
        """,
        # Подтверждённо отправленный анонс нельзя вернуть в отправку или
        # подменить его payload: повторная публикация не создаёт дубль.
        """
        CREATE TRIGGER trg_announcement_drafts_published_immutable
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.publication_status = 'sent'
            AND (
                NEW.publication_status IS NOT OLD.publication_status
                OR NEW.publication_text IS NOT OLD.publication_text
                OR NEW.publication_photos IS NOT OLD.publication_photos
                OR NEW.publication_progress IS NOT OLD.publication_progress
                OR NEW.publication_error IS NOT OLD.publication_error
                OR NEW.publication_started_at IS NOT OLD.publication_started_at
                OR NEW.published_at IS NOT OLD.published_at
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement is already published');
        END
        """,
    ),
)


# --- Версия 14: правка доставленного анонса (Stage 28) ----------------------
#
# Готовый анонс теперь автоматически выдаётся в личный чат автора, а после
# правки данных заменяется новой версией. Миграция НЕ меняет схему данных:
# адреса уже доставленных сообщений и отпечаток содержимого живут в
# `publication_progress` (ключи `ids`/`fingerprint`), время — в `published_at`.
# Единственное изменение — разрешить ровно один проверяемый переход
# `sent -> sending` (новая заморозка того же анонса). Миграции 1–13 не
# меняются; DDL выполняется в общей транзакции `run_migrations`.

MIGRATION_14_ANNOUNCEMENT_REDELIVERY = Migration(
    version=14,
    name="announcement_redelivery",
    statements=(
        "DROP TRIGGER trg_announcement_drafts_published_immutable",
        # Переход из `sent` возможен только в `sending` и только с явным
        # переносом прошлых message_id в `previous_ids`: адреса прежней
        # версии нельзя потерять (иначе её не заменить и не удалить) и нельзя
        # выдать за адреса новой отправки. Всё остальное (правка payload
        # идущей отправки, `sent -> failed`, повторный `sent`) по-прежнему
        # запрещено. Прежний payload при этом перезаписывается — это не
        # неизменяемая история, а последняя доставленная версия.
        """
        CREATE TRIGGER trg_announcement_drafts_published_immutable
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.publication_status = 'sent'
            AND NOT (
                NEW.publication_status = 'sending'
                AND NEW.published_at IS NULL
                AND NEW.publication_started_at IS NOT NULL
                AND NEW.publication_text IS NOT NULL
                AND length(trim(NEW.publication_text)) > 0
                AND NEW.publication_photos IS NOT NULL
                AND NEW.publication_error IS NULL
                AND NEW.publication_progress IS NOT NULL
                AND (NEW.publication_progress -> '$.previous_ids')
                    IS (OLD.publication_progress -> '$.ids')
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement already delivered');
        END
        """,
    ),
)


# --- Версия 15: описание кирпичика и системный код `адрес` (Poster 2.0 S1) --
#
# Новый системный кирпичик `[адрес]` — пользовательский код эталона Poster
# 2.0. Старый `адрес_места` (id=6) НЕ переименовывается и не удаляется: он
# остаётся в уже сохранённых снимках и шаблонах. Оба кирпичика берут значение
# из `places.address`.
#
# `description` — необязательное пояснение автора кирпичика (NULL = нет
# описания). Колонка добавляется добавочным ALTER TABLE: существующие строки
# и триггеры миграции 6 не затрагиваются.
#
# Коллизия кода `адрес` (дефект найден независимым тестировщиком S1). До
# Poster 2.0 кода `адрес` в `SYSTEM_BRICK_CODES` не было, поэтому в работающей
# базе версии 14 уже мог существовать личный или местный кирпичик с этим
# кодом — активный или архивный. Триггер `trg_bricks_reserve_codes_insert`
# запрещает сосуществование системного и несистемного кода, поэтому обычный
# INSERT системного `адрес` падал с `system brick code is reserved` и блокировал
# обновление приложения целиком.
#
# Решение: на время одной вставки триггер резервирования снимается и сразу
# восстанавливается дословно тем же определением, что действует после
# миграции 11 (включая исключение Stage 26 для одобрения предложения). DDL и
# DML выполняются одной транзакцией `run_migrations`, поэтому окна без
# триггера для других писателей нет.
#
# Легаси-кирпичик при этом НЕ переписывается: его `id`, код, название, тип,
# область, статус и владелец остаются прежними. Сохранённые версии и снимки
# ссылаются на `brick_id`, а не на код, поэтому старые шаблоны продолжают
# рендериться. Сосуществование системного и несистемного носителя одного кода
# уже допущено проектом в Stage 26 («повышенный» личный кирпичик сохраняет
# свой код рядом с системным), и разбор кода сохраняет тот же приоритет:
# `resolve_brick_code_*` отдаёт system, поэтому новые `[адрес]` указывают на
# системный кирпичик, а `check_code_availability` считает код занятым для
# новых определений. Для уже существующей строки владелец по-прежнему может
# менять название/настройки, не меняя код (см. `check_code_availability`:
# исключение «собственный неизменный код» действует и при `system_reserved`).
#
# Инвариант для будущих записей не ослаблен: восстановленный триггер идентичен
# действовавшему, и никакой новый системный код не может быть создан поверх
# несистемного.

# Дословное определение `trg_bricks_reserve_codes_insert` из миграции 11.
# Хранится отдельной константой, чтобы миграция 15 восстанавливала ровно то
# состояние, которое сменила, и не зависела от форматирования.
_RESERVE_CODES_INSERT_TRIGGER = """
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
        AND NOT (
            NEW.scope = 'system'
            AND EXISTS (
                SELECT 1
                FROM brick_proposals p
                JOIN bricks src ON src.id = p.source_brick_id
                WHERE p.scope = 'system'
                  AND p.status = 'pending'
                  AND p.code = NEW.code
                  AND p.title = NEW.title
                  AND p.type = NEW.type
                  AND p.source = NEW.source
                  AND p.rules_json IS NEW.rules_json
                  AND src.code = NEW.code
                  AND (
                      SELECT count(*) FROM bricks b2
                      WHERE b2.code = NEW.code AND b2.scope <> 'system'
                  ) = 1
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """

MIGRATION_15_BRICK_DESCRIPTION_AND_ADDRESS = Migration(
    version=15,
    name="brick_description_and_address",
    statements=(
        "ALTER TABLE bricks ADD COLUMN description TEXT",
        # Временное снятие триггера резервирования: без него вставка системного
        # `адрес` невозможна в базе, где код уже занят несистемным кирпичиком.
        "DROP TRIGGER IF EXISTS trg_bricks_reserve_codes_insert",
        # Системный код `[адрес]` (Poster 2.0). ID не хардкодится: миграция 6
        # создала таблицу без резерва ID, поэтому пользовательские/местные
        # кирпичики в существующей базе могли занять 10 и далее, и жёсткий
        # `id=10` даёт UNIQUE constraint failed. Системность задаётся
        # `scope='system'` и неизменяемостью кода (триггеры миграции 6), а
        # каталог `SYSTEM_BRICKS[].id` сверяется с БД по коду при чтении, не
        # по номеру. `MAX(id)+1` сохраняет все прежние ID без изменений.
        """
        INSERT INTO bricks
            (id, code, title, type, scope, source, rules_json, status,
             created_by_user_id, created_at, updated_at, description)
        SELECT
            COALESCE(MAX(id), 0) + 1, 'адрес', 'Адрес', 'short_text',
            'system', 'place', NULL, 'active', NULL, CURRENT_TIMESTAMP,
            NULL, NULL
        FROM bricks
        """,
        _RESERVE_CODES_INSERT_TRIGGER,
    ),
)


# --- Версия 16: черновик без площадки (Poster 2.0 S1) -----------------------
#
# Личный шаблон может не содержать кирпичика «Место», поэтому
# `announcement_drafts.place_id` и снимок названия места становятся
# необязательными. `origin` фиксирует источник шаблона (place/personal), а не
# место, выбранное позже в кирпичике. `live_message_ids_json` хранит адреса
# всех сообщений живого медиаблока (мультимедиа — несколько сообщений).
#
# SQLite не умеет снимать NOT NULL/CHECK через ALTER TABLE, поэтому таблица
# перестраивается. `PRAGMA foreign_keys=OFF` внутри транзакции
# `run_migrations` — no-op, поэтому используется безопасная схема с копиями:
# DROP TABLE при включённых FK делает неявный DELETE и каскадно удаляет
# детей, но их строки уже сохранены в *_backup. Порядок шагов:
#   1-3) копии детей и черновиков;
#   4)   DROP старой таблицы (каскад удаляет детей);
#   5)   новая схема;
#   6)   индексы и триггеры (до вставки, чтобы вставка проверила данные);
#   7)   черновики с origin='place';
#   8)   значения и дополнительные кирпичики;
#   9)   удаление копий.
# Всё выполняется одной транзакцией: сбой любого шага откатывает миграцию.

MIGRATION_16_ANNOUNCEMENT_DRAFT_ORIGIN = Migration(
    version=16,
    name="announcement_draft_origin",
    statements=(
        "CREATE TABLE announcement_values_backup AS SELECT * FROM announcement_values",
        "CREATE TABLE announcement_extras_backup AS SELECT * FROM announcement_extras",
        "CREATE TABLE announcement_drafts_backup AS SELECT * FROM announcement_drafts",
        # Каскадное удаление детей — ожидаемо: их строки уже в *_backup.
        "DROP TABLE announcement_drafts",
        # Триггеры детей принадлежат их собственным таблицам: DROP родителя
        # их не снимает, а имена заняты. Сами таблицы и данные (в *_backup)
        # не меняются — снимаем только триггеры, чтобы пересоздать их ниже.
        "DROP TRIGGER IF EXISTS trg_announcement_values_identity_immutable",
        "DROP TRIGGER IF EXISTS trg_announcement_extras_identity_immutable",
        """
        CREATE TABLE announcement_drafts (
            id INTEGER NOT NULL PRIMARY KEY,
            author_user_id INTEGER NOT NULL,
            place_id INTEGER,
            place_name_id INTEGER,
            place_display_name TEXT,
            place_address TEXT,
            place_url TEXT,
            origin TEXT NOT NULL DEFAULT 'place',
            template_id INTEGER,
            template_version INTEGER,
            template_snapshot_id INTEGER,
            stage TEXT NOT NULL DEFAULT 'place',
            revision INTEGER NOT NULL DEFAULT 1,
            idempotency_key TEXT,
            photos TEXT,
            publication_status TEXT,
            publication_text TEXT,
            publication_photos TEXT,
            publication_progress TEXT,
            publication_error TEXT,
            publication_started_at DATETIME,
            published_at DATETIME,
            live_message_ids_json TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT ck_announcement_drafts_stage
                CHECK (stage IN (
                    'place', 'template', 'questions', 'extras',
                    'preview', 'completed'
                )),
            CONSTRAINT ck_announcement_drafts_revision
                CHECK (revision >= 1),
            CONSTRAINT ck_announcement_drafts_origin
                CHECK (origin IN ('place', 'personal')),
            CONSTRAINT ck_announcement_drafts_origin_place
                CHECK (
                    (origin = 'place'
                        AND place_id IS NOT NULL
                        AND place_display_name IS NOT NULL
                        AND length(trim(place_display_name)) > 0)
                    OR (origin = 'personal'
                        AND place_id IS NULL
                        AND place_display_name IS NULL)
                ),
            CONSTRAINT ck_announcement_drafts_address
                CHECK (place_address IS NULL OR length(trim(place_address)) > 0),
            CONSTRAINT ck_announcement_drafts_url
                CHECK (place_url IS NULL OR length(trim(place_url)) > 0),
            CONSTRAINT ck_announcement_drafts_idempotency
                CHECK (
                    idempotency_key IS NULL
                    OR length(trim(idempotency_key)) > 0
                ),
            CONSTRAINT ck_announcement_drafts_template_shape
                CHECK (
                    (template_id IS NULL
                        AND template_version IS NULL
                        AND template_snapshot_id IS NULL)
                    OR (template_id IS NOT NULL
                        AND template_version IS NOT NULL
                        AND template_version >= 1
                        AND template_snapshot_id IS NOT NULL)
                ),
            CONSTRAINT ck_announcement_drafts_live_ids
                CHECK (
                    live_message_ids_json IS NULL
                    OR (json_valid(live_message_ids_json)
                        AND json_type(live_message_ids_json) = 'array')
                ),
            CONSTRAINT fk_announcement_drafts_author
                FOREIGN KEY (author_user_id) REFERENCES users (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_place_name
                FOREIGN KEY (place_name_id) REFERENCES place_names (id)
                ON DELETE SET NULL,
            CONSTRAINT fk_announcement_drafts_template
                FOREIGN KEY (template_id) REFERENCES templates (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_announcement_drafts_snapshot
                FOREIGN KEY (template_snapshot_id) REFERENCES template_snapshots (id)
                ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX ix_announcement_drafts_author_updated
            ON announcement_drafts (author_user_id, updated_at)
        """,
        """
        CREATE UNIQUE INDEX ux_announcement_drafts_snapshot
            ON announcement_drafts (template_snapshot_id)
            WHERE template_snapshot_id IS NOT NULL
        """,
        """
        CREATE UNIQUE INDEX ux_announcement_drafts_idempotency
            ON announcement_drafts (author_user_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL
        """,
        """
        CREATE INDEX ix_announcement_drafts_publication_status
            ON announcement_drafts (publication_status)
        """,
        # --- Триггеры: копия миграций 12-14 плюс неизменяемость origin ------
        """
        CREATE TRIGGER trg_announcement_drafts_revision_increases
        BEFORE UPDATE OF revision ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.revision <= OLD.revision
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft revision must increase');
        END
        """,
        # Источник шаблона тоже неизменяем: personal-черновик нельзя выдать за
        # анонс площадки (и наоборот) подменой одной колонки.
        """
        CREATE TRIGGER trg_announcement_drafts_identity_immutable
        BEFORE UPDATE OF
            author_user_id, place_id, idempotency_key, created_at, origin
        ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.idempotency_key IS NOT OLD.idempotency_key
            OR NEW.created_at IS NOT OLD.created_at
            OR NEW.origin IS NOT OLD.origin
        BEGIN
            SELECT RAISE(ABORT, 'announcement draft identity is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_template_immutable
        BEFORE UPDATE OF template_id, template_version, template_snapshot_id
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.template_snapshot_id IS NOT NULL
            AND (
                NEW.template_id IS NOT OLD.template_id
                OR NEW.template_version IS NOT OLD.template_version
                OR NEW.template_snapshot_id IS NOT OLD.template_snapshot_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement template snapshot is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_values_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, created_at ON announcement_values
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement value identity is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_extras_identity_immutable
        BEFORE UPDATE OF draft_id, brick_id, occurrence, created_at
        ON announcement_extras
        FOR EACH ROW
        WHEN NEW.draft_id IS NOT OLD.draft_id
            OR NEW.brick_id IS NOT OLD.brick_id
            OR NEW.occurrence IS NOT OLD.occurrence
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'announcement extra identity is immutable');
        END
        """,
        # JSON-ограничения фото/прогресса (миграция 13): CHECK на ALTER TABLE
        # в SQLite не добавить, поэтому те же правила держат триггеры.
        """
        CREATE TRIGGER trg_announcement_drafts_media_json_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_media_json_update
        BEFORE UPDATE OF photos, publication_photos, publication_progress
        ON announcement_drafts
        FOR EACH ROW
        WHEN (NEW.photos IS NOT NULL AND (
                json_valid(NEW.photos) = 0
                OR CASE WHEN json_valid(NEW.photos) = 1
                        THEN json_type(NEW.photos) END <> 'array'
            ))
            OR (NEW.publication_photos IS NOT NULL AND (
                json_valid(NEW.publication_photos) = 0
                OR CASE WHEN json_valid(NEW.publication_photos) = 1
                        THEN json_type(NEW.publication_photos) END <> 'array'
            ))
            OR (NEW.publication_progress IS NOT NULL AND (
                json_valid(NEW.publication_progress) = 0
                OR CASE WHEN json_valid(NEW.publication_progress) = 1
                        THEN json_type(NEW.publication_progress) END <> 'object'
            ))
        BEGIN
            SELECT RAISE(ABORT, 'announcement media must be valid JSON');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_status_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_status_update
        BEFORE UPDATE OF publication_status ON announcement_drafts
        FOR EACH ROW
        WHEN NEW.publication_status IS NOT NULL
            AND NEW.publication_status NOT IN
                ('sending', 'partial', 'failed', 'sent')
        BEGIN
            SELECT RAISE(ABORT, 'unknown announcement publication status');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_shape_insert
        BEFORE INSERT ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
        """,
        """
        CREATE TRIGGER trg_announcement_drafts_publication_shape
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN (
            NEW.publication_status IS NULL
            AND (
                NEW.publication_text IS NOT NULL
                OR NEW.publication_photos IS NOT NULL
                OR NEW.publication_progress IS NOT NULL
                OR NEW.publication_error IS NOT NULL
                OR NEW.publication_started_at IS NOT NULL
                OR NEW.published_at IS NOT NULL
            )
        ) OR (
            NEW.publication_status IS NOT NULL
            AND (
                NEW.publication_text IS NULL
                OR length(trim(NEW.publication_text)) = 0
                OR NEW.publication_photos IS NULL
                OR NEW.publication_started_at IS NULL
                OR (
                    NEW.publication_status IN ('sending', 'partial', 'failed')
                    AND NEW.published_at IS NOT NULL
                )
                OR (
                    NEW.publication_status = 'sent'
                    AND NEW.published_at IS NULL
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'invalid announcement publication shape');
        END
        """,
        # Точная редакция миграции 14: разрешён ровно проверяемый переход
        # sent -> sending с переносом адресов прошлой версии в previous_ids.
        """
        CREATE TRIGGER trg_announcement_drafts_published_immutable
        BEFORE UPDATE OF
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error,
            publication_started_at, published_at
        ON announcement_drafts
        FOR EACH ROW
        WHEN OLD.publication_status = 'sent'
            AND NOT (
                NEW.publication_status = 'sending'
                AND NEW.published_at IS NULL
                AND NEW.publication_started_at IS NOT NULL
                AND NEW.publication_text IS NOT NULL
                AND length(trim(NEW.publication_text)) > 0
                AND NEW.publication_photos IS NOT NULL
                AND NEW.publication_error IS NULL
                AND NEW.publication_progress IS NOT NULL
                AND (NEW.publication_progress -> '$.previous_ids')
                    IS (OLD.publication_progress -> '$.ids')
            )
        BEGIN
            SELECT RAISE(ABORT, 'announcement already delivered');
        END
        """,
        # Прежние черновики получали площадку до появления origin.
        """
        INSERT INTO announcement_drafts (
            id, author_user_id, place_id, place_name_id, place_display_name,
            place_address, place_url, origin, template_id, template_version,
            template_snapshot_id, stage, revision, idempotency_key, photos,
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error, publication_started_at,
            published_at, live_message_ids_json, created_at, updated_at
        )
        SELECT
            id, author_user_id, place_id, place_name_id, place_display_name,
            place_address, place_url, 'place', template_id, template_version,
            template_snapshot_id, stage, revision, idempotency_key, photos,
            publication_status, publication_text, publication_photos,
            publication_progress, publication_error, publication_started_at,
            published_at, NULL, created_at, updated_at
        FROM announcement_drafts_backup
        """,
        "INSERT INTO announcement_values SELECT * FROM announcement_values_backup",
        "INSERT INTO announcement_extras SELECT * FROM announcement_extras_backup",
        "DROP TABLE announcement_values_backup",
        "DROP TABLE announcement_extras_backup",
        "DROP TABLE announcement_drafts_backup",
    ),
)


# --- Версия 17: архив легаси-таблицы черновиков (Poster 2.0 S5) ------------
#
# Девятишаговый мастер и его WIP удалены из кода: `Draft` больше нет ни в
# моделях, ни в живых сценариях. Таблица `drafts` не удаляется безвозвратно:
# она переименовывается в `legacy_drafts_backup`, поэтому данные прежних
# черновиков остаются в файле базы и доступны для ручного разбора.
#
# Идемпотентность: `requires_tables=('drafts',)` — на новой базе таблицы
# никогда не было (create_all больше её не создаёт), на уже мигрированной
# она называется иначе. В обоих случаях шаги пропускаются, а версия
# фиксируется в журнале. `user_states` не трогается: это живая
# инфраструктура экрана и снимков состояния, а не легаси.

MIGRATION_17_ARCHIVE_LEGACY_DRAFTS = Migration(
    version=17,
    name="archive_legacy_drafts",
    statements=(
        # Переименование вместо DROP: данные не теряются, схема не копится.
        "ALTER TABLE drafts RENAME TO legacy_drafts_backup",
    ),
    requires_tables=("drafts",),
)


# --- Версия 18: нетронутые копии встроенного шаблона → версия 3 (R2) --------
#
# Poster 2.1 R2 уточнил эталон стандартного анонса (Часть III): после
# заголовка и строки «дата // время» появились пустые строки, сама строка
# даты/времени и площадка стали жирными. Уже выданные копии прежнего эталона
# без этого обновления остались бы «старыми», поэтому миграция добавляет
# каждому НЕТРОНУТОМУ шаблону новую версию с текстом BUILTIN v3.
#
# Правило «обновляем только доказуемо нетронутые»:
#
# - кандидат — ТЕКУЩАЯ версия шаблона (`template_versions.version =
#   templates.revision`), содержимое которой побайтово совпадает с эталоном
#   v2, а маркер версии — `builtin_version = 2`;
# - исключение по маркеру: стартовая личная копия до R2 создавалась сервисом
#   как обычный личный шаблон (`builtin_version IS NULL`). Для личного
#   шаблона с нетронутым именем «Мой шаблон» и побайтовым текстом v2 такого
#   совпадения достаточно: имя и все три секции совпадают с эталоном, то есть
#   копия доказуемо нетронута (после R2 такие копии сразу получают маркер);
# - правки человека (любая изменённая секция, новая версия без
#   `builtin_version`) в кандидаты не попадают: их текущая версия уже не v2;
# - личная копия дополнительно обязана сохранить имя «Мой шаблон»: если
#   человек переименовал копию, содержимое тоже НЕ обновляется — иначе одно
#   и то же действие (переименование) молча меняло бы текст;
# - стандартные шаблоны мест не имеют имени: у них проверяется только
#   содержимое и маркер v2, имя (NULL) остаётся прежним.
#
# Иммутабельность истории не нарушается: старая версия v2 остаётся в
# `template_versions` как есть, миграция только ДОБАВЛЯЕТ следующую версию,
# поднимает `templates.revision` (он же указатель текущей версии) и
# переименовывает нетронутую стартовую копию в «Дефолтный шаблон» в той же
# транзакции. Снимки (`template_snapshots`) не трогаются: это замороженные
# копии прошлых черновиков.
#
# Привязки резолвятся ПО КОДУ из `bricks`, а не хардкодом ID (S1-замечание):
# миграция 15 создала системный `[адрес]` с `MAX(id)+1`, поэтому жёсткие
# номера разошлись бы с реальной базой. Если хотя бы один из восьми
# системных кодов отсутствует/архивирован, шаблон не обновляется вовсе:
# версия с неполными привязками сломала бы рендер.
#
# Идемпотентность: журнал не даёт применить миграцию повторно, а сама
# выборка после применения пуста (текущие версии стали `builtin_version=3`).
# Временная таблица-очередь удаляется последним шагом, поэтому повторный
# запуск на уже мигрированной базе ничего не оставляет.

# Эталоны миграции заданы локально, а НЕ импортом из `template_types`:
# миграция обязана остаться самодостаточной и не зависеть от будущих правок
# текущих констант.
_M18_V2_HEADER = "**[название]**"
_M18_V2_BODY = (
    "[дата] // [время_начала]\n"
    "[место]([ссылка_места])\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
_M18_V2_FOOTER = "[контакт]"
_M18_V3_HEADER = "**[название]**"
_M18_V3_BODY = (
    "\n"
    "**[дата] // [время_начала]**\n"
    "\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
_M18_V3_FOOTER = "\n[контакт]"
_M18_OLD_INITIAL_NAME = "Мой шаблон"
_M18_NEW_INITIAL_NAME = "Дефолтный шаблон"
_M18_CODES = (
    "название",
    "дата",
    "время_начала",
    "место",
    "ссылка_места",
    "адрес",
    "описание",
    "контакт",
)


def _migration_18_literal(value: str) -> str:
    """SQL-литерал строки: шаги миграции — готовые строки без параметров."""
    return "'" + value.replace("'", "''") + "'"


_M18_CODES_SQL = ", ".join(_migration_18_literal(code) for code in _M18_CODES)
_M18_CODES_VALUES_SQL = " UNION ALL ".join(
    f"SELECT {_migration_18_literal(code)} AS code" for code in _M18_CODES
)

MIGRATION_18_BUILTIN_TEMPLATES_V3 = Migration(
    version=18,
    name="builtin_templates_v3",
    statements=(
        # Очередь кандидатов: одна строка на шаблон, чтобы все следующие шаги
        # работали с одним и тем же зафиксированным списком.
        """
        CREATE TABLE migration_18_targets (
            template_id INTEGER NOT NULL PRIMARY KEY,
            new_version INTEGER NOT NULL,
            rename_to_default INTEGER NOT NULL
        )
        """,
        f"""
        INSERT INTO migration_18_targets (template_id, new_version, rename_to_default)
        SELECT
            t.id,
            t.revision + 1,
            CASE
                WHEN t.kind = 'personal' AND t.name = {_migration_18_literal(_M18_OLD_INITIAL_NAME)}
                    THEN 1
                ELSE 0
            END
        FROM templates AS t
        JOIN template_versions AS v
          ON v.template_id = t.id AND v.version = t.revision
        WHERE v.header = {_migration_18_literal(_M18_V2_HEADER)}
          AND v.body = {_migration_18_literal(_M18_V2_BODY)}
          AND v.footer = {_migration_18_literal(_M18_V2_FOOTER)}
          AND (
              v.builtin_version = 2
              OR (
                  t.kind = 'personal'
                  AND t.name = {_migration_18_literal(_M18_OLD_INITIAL_NAME)}
              )
          )
          AND (
              t.kind = 'standard'
              OR (t.kind = 'personal' AND t.name = {_migration_18_literal(_M18_OLD_INITIAL_NAME)})
          )
          AND (
              SELECT count(*) FROM bricks AS b
              WHERE b.scope = 'system'
                AND b.status = 'active'
                AND b.code IN ({_M18_CODES_SQL})
          ) = {len(_M18_CODES)}
        """,
        # Новая версия: только INSERT, старая v2 остаётся в истории.
        f"""
        INSERT INTO template_versions
            (template_id, version, header, body, footer,
             created_by_user_id, created_at, builtin_version)
        SELECT
            g.template_id, g.new_version,
            {_migration_18_literal(_M18_V3_HEADER)},
            {_migration_18_literal(_M18_V3_BODY)},
            {_migration_18_literal(_M18_V3_FOOTER)},
            NULL, CURRENT_TIMESTAMP, 3
        FROM migration_18_targets AS g
        """,
        # Те же восемь системных кодов, но brick_id — из `bricks` по коду.
        f"""
        INSERT INTO template_version_bricks (template_version_id, brick_id, code)
        SELECT ver.id, b.id, c.code
        FROM migration_18_targets AS g
        JOIN template_versions AS ver
          ON ver.template_id = g.template_id AND ver.version = g.new_version
        CROSS JOIN ({_M18_CODES_VALUES_SQL}) AS c
        JOIN bricks AS b
          ON b.scope = 'system' AND b.status = 'active' AND b.code = c.code
        """,
        # Указатель текущей версии и имя стартовой копии — одной транзакцией
        # с созданием версии: переименования без обновления не бывает.
        f"""
        UPDATE templates
        SET revision = (
                SELECT g.new_version FROM migration_18_targets AS g
                WHERE g.template_id = templates.id
            ),
            name = CASE
                WHEN (
                    SELECT g.rename_to_default FROM migration_18_targets AS g
                    WHERE g.template_id = templates.id
                ) = 1
                THEN {_migration_18_literal(_M18_NEW_INITIAL_NAME)}
                ELSE name
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id IN (SELECT template_id FROM migration_18_targets)
        """,
        "DROP TABLE migration_18_targets",
    ),
    requires_tables=(
        "templates",
        "template_versions",
        "template_version_bricks",
        "bricks",
    ),
)


# --- Версия 19: задачи разрешения конфликтов кодов (Poster 2.1 R3) ----------
#
# R1 позволил одобрять глобальный кирпичик при локальном носителе того же
# кода, но INSERT системного кирпичика разрешался триггером ровно при ОДНОМ
# несистемном носителе. R3 доводит модель до требований владельца:
#
# - глобальный кирпичик создаётся при любом числе локальных носителей и
#   выигрывает для НОВЫХ привязок (разрешение кода system → place → personal
#   уже отдаёт приоритет системе);
# - существующие версии/снимки шаблонов и анонсы не переписываются
#   (`template_version_bricks`/`template_snapshot_bricks` неизменяемы);
# - по каждому месту с местным носителем создаётся задача разрешения
#   конфликта (`brick_conflicts`) и идемпотентное уведомление его админу
#   (`brick_conflict_notifications`);
# - решение задачи терминально: `open` → `renamed`/`switched`; `superseded`
#   закрывает задачу, если местный носитель исчез сам (архив/переименование
#   вне сценария).
#
# Схема только добавочная: существующие таблицы `bricks`,
# `brick_proposals`, `templates`, `template_versions` не перестраиваются.
# Пересоздаётся единственный триггер `trg_bricks_reserve_codes_insert`:
# из исключения «одобрение глобального предложения» убрано требование ровно
# одного несистемного носителя. Общее правило «системный и несистемный код
# не сосуществуют» для прямого INSERT (сервис `create_system_brick`)
# сохранено: исключение по-прежнему требует pending-предложение с полностью
# совпадающим снимком.


MIGRATION_19_BRICK_CONFLICTS = Migration(
    version=19,
    name="brick_code_conflicts",
    statements=(
        # Одна задача = «глобальный кирпичик + местный носитель того же кода».
        # Обе ссылки на bricks неизменяемы (RESTRICT): история решений не
        # должна рассыпаться при архивации/удалении кирпичика.
        """
        CREATE TABLE brick_conflicts (
            id INTEGER NOT NULL PRIMARY KEY,
            code TEXT NOT NULL,
            place_id INTEGER NOT NULL,
            system_brick_id INTEGER NOT NULL,
            local_brick_id INTEGER NOT NULL,
            proposal_id INTEGER,
            status TEXT NOT NULL DEFAULT 'open',
            created_at DATETIME NOT NULL,
            resolved_at DATETIME,
            resolved_by_user_id INTEGER,
            resolution_note TEXT,
            CONSTRAINT ck_brick_conflicts_status
                CHECK (status IN ('open', 'renamed', 'switched', 'superseded')),
            CONSTRAINT ck_brick_conflicts_code_length
                CHECK (length(code) BETWEEN 2 AND 64),
            CONSTRAINT ck_brick_conflicts_shape
                CHECK (
                    (status = 'open'
                        AND resolved_at IS NULL
                        AND resolved_by_user_id IS NULL)
                    OR (status IN ('renamed', 'switched')
                        AND resolved_at IS NOT NULL
                        AND resolved_by_user_id IS NOT NULL)
                    OR (status = 'superseded'
                        AND resolved_at IS NOT NULL
                        AND resolved_by_user_id IS NULL)
                ),
            CONSTRAINT fk_brick_conflicts_place
                FOREIGN KEY (place_id) REFERENCES places (id)
                ON DELETE CASCADE,
            CONSTRAINT fk_brick_conflicts_system_brick
                FOREIGN KEY (system_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_local_brick
                FOREIGN KEY (local_brick_id) REFERENCES bricks (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_proposal
                FOREIGN KEY (proposal_id) REFERENCES brick_proposals (id)
                ON DELETE RESTRICT,
            CONSTRAINT fk_brick_conflicts_resolved_by
                FOREIGN KEY (resolved_by_user_id) REFERENCES users (id)
                ON DELETE RESTRICT
        )
        """,
        # Идемпотентность: одна пара носителей — одна задача. Повторное
        # одобрение/повторный прогон упирается в этот индекс.
        """
        CREATE UNIQUE INDEX ux_brick_conflicts_system_local
            ON brick_conflicts (system_brick_id, local_brick_id)
        """,
        "CREATE INDEX ix_brick_conflicts_place_status ON brick_conflicts (place_id, status)",
        "CREATE INDEX ix_brick_conflicts_local_brick ON brick_conflicts (local_brick_id)",
        # Очередь уведомлений админам мест. Получатель — постоянный Telegram
        # user ID (в `users` он может отсутствовать: суперадмин из конфига).
        """
        CREATE TABLE brick_conflict_notifications (
            id INTEGER NOT NULL PRIMARY KEY,
            conflict_id INTEGER NOT NULL,
            telegram_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            sent_at DATETIME,
            CONSTRAINT ck_brick_conflict_notifications_status
                CHECK (status IN ('pending', 'sent', 'failed')),
            CONSTRAINT ck_brick_conflict_notifications_attempts
                CHECK (attempts >= 0),
            CONSTRAINT ck_brick_conflict_notifications_shape
                CHECK (
                    (status = 'pending' AND sent_at IS NULL)
                    OR (status IN ('sent', 'failed') AND sent_at IS NOT NULL)
                ),
            CONSTRAINT fk_brick_conflict_notifications_conflict
                FOREIGN KEY (conflict_id) REFERENCES brick_conflicts (id)
                ON DELETE CASCADE
        )
        """,
        """
        CREATE UNIQUE INDEX ux_brick_conflict_notifications_target
            ON brick_conflict_notifications (conflict_id, telegram_user_id)
        """,
        """
        CREATE INDEX ix_brick_conflict_notifications_status
            ON brick_conflict_notifications (status, created_at)
        """,
        # Статус задачи терминален: повторный callback не переигрывает решение
        # и не переписывает аудит.
        """
        CREATE TRIGGER trg_brick_conflicts_single_transition
        BEFORE UPDATE OF status ON brick_conflicts
        FOR EACH ROW
        WHEN OLD.status <> 'open'
            OR NEW.status NOT IN ('renamed', 'switched', 'superseded')
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict decision is final');
        END
        """,
        """
        CREATE TRIGGER trg_brick_conflicts_decision_immutable
        BEFORE UPDATE ON brick_conflicts
        FOR EACH ROW
        WHEN OLD.status <> 'open'
            AND (
                NEW.status IS NOT OLD.status
                OR NEW.resolved_at IS NOT OLD.resolved_at
                OR NEW.resolved_by_user_id IS NOT OLD.resolved_by_user_id
                OR NEW.resolution_note IS NOT OLD.resolution_note
            )
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict decision is immutable');
        END
        """,
        # Снимок задачи (код, место, носители) неизменяем: иначе история
        # решений перестала бы объяснять, что именно разрешали.
        """
        CREATE TRIGGER trg_brick_conflicts_snapshot_immutable
        BEFORE UPDATE OF
            code, place_id, system_brick_id, local_brick_id, proposal_id,
            created_at
        ON brick_conflicts
        FOR EACH ROW
        WHEN NEW.code IS NOT OLD.code
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.system_brick_id IS NOT OLD.system_brick_id
            OR NEW.local_brick_id IS NOT OLD.local_brick_id
            OR NEW.proposal_id IS NOT OLD.proposal_id
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict snapshot is immutable');
        END
        """,
        """
        CREATE TRIGGER trg_brick_conflicts_no_delete
        BEFORE DELETE ON brick_conflicts
        FOR EACH ROW
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict history cannot be deleted');
        END
        """,
        # Уведомление отправляется ровно один раз: pending → sent/failed,
        # обратного перехода нет.
        """
        CREATE TRIGGER trg_brick_conflict_notifications_single_transition
        BEFORE UPDATE OF status ON brick_conflict_notifications
        FOR EACH ROW
        WHEN OLD.status <> 'pending' OR NEW.status = 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'brick conflict notification is final');
        END
        """,
        # R3: снимаем ограничение «ровно один несистемный носитель» из
        # исключения для одобрения глобального предложения. Прямой INSERT
        # системного кирпичика поверх местного по-прежнему запрещён.
        "DROP TRIGGER trg_bricks_reserve_codes_insert",
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
        AND NOT (
            NEW.scope = 'system'
            AND EXISTS (
                SELECT 1
                FROM brick_proposals p
                JOIN bricks src ON src.id = p.source_brick_id
                WHERE p.scope = 'system'
                  AND p.status = 'pending'
                  AND p.code = NEW.code
                  AND p.title = NEW.title
                  AND p.type = NEW.type
                  AND p.source = NEW.source
                  AND p.rules_json IS NEW.rules_json
                  AND src.code = NEW.code
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'system brick code is reserved');
        END
        """,
    ),
    requires_tables=(
        "bricks",
        "places",
        "users",
        "brick_proposals",
    ),
)


# --- Версия 20: описание в снимке предложения (Poster 2.1 R4) ---------------
#
# Карточка предложения должна показывать описание кирпичика (№29), а
# снимок — не указатель: описание фиксируется при отправке и не меняется
# при последующих правках личного кирпичика. Колонка добавляется
# добавочным `ALTER TABLE ADD COLUMN`; таблица не перестраивается, триггеры
# и индексы остаются прежними. Существующие pending-предложения получают
# `NULL` — карточка показывает «—», данные не переписываются.
#
# Триггер неизменяемости снимка (`trg_brick_proposals_snapshot_immutable`)
# пересоздаётся в той же транзакции: он перечисляет колонки снимка явно,
# и без `description` новое поле можно было бы переписать после отправки.
# Прежнее поведение для остальных колонок сохранено дословно.

MIGRATION_20_PROPOSAL_DESCRIPTION = Migration(
    version=20,
    name="brick_proposal_description",
    statements=(
        "ALTER TABLE brick_proposals ADD COLUMN description TEXT",
        "DROP TRIGGER IF EXISTS trg_brick_proposals_snapshot_immutable",
        """
        CREATE TRIGGER trg_brick_proposals_snapshot_immutable
        BEFORE UPDATE OF
            author_user_id, scope, place_id, source_brick_id, title, code,
            type, source, rules_json, description, created_at
        ON brick_proposals
        FOR EACH ROW
        WHEN NEW.author_user_id IS NOT OLD.author_user_id
            OR NEW.scope IS NOT OLD.scope
            OR NEW.place_id IS NOT OLD.place_id
            OR NEW.source_brick_id IS NOT OLD.source_brick_id
            OR NEW.title IS NOT OLD.title
            OR NEW.code IS NOT OLD.code
            OR NEW.type IS NOT OLD.type
            OR NEW.source IS NOT OLD.source
            OR NEW.rules_json IS NOT OLD.rules_json
            OR NEW.description IS NOT OLD.description
            OR NEW.created_at IS NOT OLD.created_at
        BEGIN
            SELECT RAISE(ABORT, 'brick proposal snapshot is immutable');
        END
        """,
    ),
    requires_tables=(
        "bricks",
        "brick_proposals",
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
    MIGRATION_8_TEMPLATES,
    MIGRATION_9_BINDING_CODES,
    MIGRATION_10_TEMPLATE_SESSIONS,
    MIGRATION_11_BRICK_PROPOSALS,
    MIGRATION_12_ANNOUNCEMENT_WIZARD,
    MIGRATION_13_ANNOUNCEMENT_PUBLICATION,
    MIGRATION_14_ANNOUNCEMENT_REDELIVERY,
    MIGRATION_15_BRICK_DESCRIPTION_AND_ADDRESS,
    MIGRATION_16_ANNOUNCEMENT_DRAFT_ORIGIN,
    MIGRATION_17_ARCHIVE_LEGACY_DRAFTS,
    MIGRATION_18_BUILTIN_TEMPLATES_V3,
    MIGRATION_19_BRICK_CONFLICTS,
    MIGRATION_20_PROPOSAL_DESCRIPTION,
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


def _missing_tables(connection: sqlite3.Connection, names: Sequence[str]) -> list[str]:
    """Каких из ожидаемых таблиц нет в базе (пустой список — все на месте)."""
    missing: list[str] = []
    for name in names:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        if row is None:
            missing.append(name)
    return missing


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
                missing = _missing_tables(connection, migration.requires_tables)
                if missing:
                    # Таблицы нет — шаги неприменимы. Версия фиксируется:
                    # повторный запуск не должен пытаться снова.
                    logger.info(
                        "Миграция %s (%s) пропущена: нет таблиц %s.",
                        migration.version,
                        migration.name,
                        ", ".join(missing),
                    )
                else:
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
