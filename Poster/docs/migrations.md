# Миграции схемы SQLite: Stage 19A, 19B, 20A, 20B, 21, 22A, 22B и 23

Статус: **реализовано в Stage 19A, 19B, 20A, 20B, 21, 22A, 22B и 23**. Документ
описывает фактический механизм, созданные таблицы, ограничения,
автоматическую резервную копию, совместимость со старой базой и порядок
безопасного применения миграций. Текущая последняя версия — **8**
(`templates_versions_snapshots`, Stage 23). Целевая архитектура справочника мест,
кирпичиков и шаблонов — в
[`stage18_architecture.md`](stage18_architecture.md).

## 1. Границы Stage 19A, 19B, 20A, 20B, 21, 22A, 22B и 23

Stage 19A — фундамент:

- собственный нумерованный механизм миграций SQLite (`migrations.py`);
- журнал `schema_migrations` с версией, именем и контрольной суммой DDL;
- версия 1: таблицы `users`, `places`, `place_names` и их индексы;
- нормализация названий и атомарное создание места с каноническим названием
  (`place_store.py`);
- атомарный UPSERT пользователя по Telegram user ID (`user_store.py`);
- включение `PRAGMA foreign_keys=ON` на соединениях приложения;
- автоматические тесты на временных файлах SQLite.

Stage 19B — справочник в Telegram и защита перед миграцией:

- версия 2: необязательные `places.address/url` и таблица
  `place_sessions` для состояния сценария;
- поиск по каноническим/альтернативным названиям, ранжирование и похожесть;
- Telegram-поток «Места»: списки, карточки, проверка дублей, явное создание,
  альтернативные названия, подтверждение выбора;
- автоматическая WAL-aware копия перед первым ожидающим применением
  миграции к существующей непустой базе;
- 65 автоматических тестов Stage 19B (всего 687 на момент 19B).

Stage 20A — роли, кабинеты и аудит назначений:

- версия 3: таблица `place_admins` (история назначений с частичным
  уникальным индексом «не более одного действующего админа на место») и
  таблица `admin_sessions` (интерфейсное состояние кабинета);
- конфигурация ролей: `SUPERADMIN_USER_IDS` (строгий парсинг, источник
  истины — только env) и необязательная `SUPERADMIN_CONTACT_URL`;
- централизованные проверки прав (`permissions.py`), атомарные
  назначение/замена/отзыв (`place_admin_store.py`);
- кабинет суперадмина и администратора места (`handlers/admin.py`), экран
  «Я админ места» в карточке места;
- регистрация/обновление пользователей при каждом Update
  (`handlers/user_tracking.py`);
- 61 новый тест Stage 20A (всего 748).

Stage 20B — редактирование данных места с журналом аудита:

- версия 4: таблица `place_edit_log` — одна строка аудита на успешное
  изменение (`place_id`, `actor_user_id`, `action`, `old_value`,
  `new_value`, `created_at`);
- редактирование в кабинете (`place_edit_store.py` + `handlers/admin.py`):
  каноническое название (старое остаётся альтернативой для поиска),
  альтернативные названия (добавление/переименование/удаление с пагинацией),
  адрес и ссылка (установка/удаление; NULL = отсутствие; у адреса нет
  искусственного предела длины, ссылка ограничена 500 символами интерфейса
  и нормализуется в http(s));
- атомарность каждого изменения: проверка текущего значения → изменение →
  строка аудита → commit; оптимистичная блокировка по ожидаемому значению
  (статус `changed`, если поле изменил другой администратор);
- права перепроверяются при каждом действии: только суперадмин или
  действующий администратор именно этого места;
- 39 новых тестов Stage 20B: 24 юнит-логики (`test_stage20b_edit_logic.py`)
  и 15 интеграционных потоков (`test_stage20b_edit_flow.py`) — всего 787.

Stage 21 — передача администратора места:

- версия 5: таблица `admin_transfers` — предложение на 24 часа, известный
  получатель (`to_user_id`), кандидат из одноразовой ссылки
  (`opened_by_user_id`), только SHA-256 токена, статусы и история;
  частичный уникальный индекс разрешает **одно** `pending`-предложение на
  место, второй уникальный индекс — уникальность хеша токена;
- передать может только действующий администратор именно этого места;
  суперадминистратор без роли места передачу не создаёт (свои полномочия
  Stage 20A у него сохраняются);
- известный получатель привязан к постоянному Telegram user ID; username
  остаётся подсказкой и может смениться;
- неизвестный получатель открывает ссылку с одноразовым токеном, но
  становится только кандидатом: `to_user_id` появляется после отдельного
  утверждения инициатором;
- подтверждение получателя атомарно закрывает предложение, отзывает старое
  назначение и создаёт новое; при ошибке откатывается всё;
- срок проверяется лениво (`now < expires_at`), без планировщика;
- замена/отзыв администратора Stage 20A в той же транзакции переводит
  ожидающее предложение в `superseded`;
- 56 новых тестов Stage 21: 40 юнит-логики
  (`test_stage21_transfer_logic.py`) и 16 интеграционных потоков
  (`test_stage21_transfer_flow.py`) — всего 843.

Stage 22A — фундамент реестра кирпичиков:

- версия 6 (`bricks_registry`): таблица `bricks`, девять системных строк с
  постоянными ID 1–9, частичные уникальные индексы, CHECK/FK-ограничения,
  индексы доступности/поиска и шесть триггеров защиты;
- `brick_types.py` — каталог типов (`short_text`, `long_text`, `date`,
  `time`, `url`, `number`, `choice`), девять системных определений,
  нормализация технического кода/заголовка, строгая проверка `rules_json`,
  проверка значений и пять состояний;
- `brick_store.py` — Telegram-независимый сервис: чтение, поиск, проверка
  уникальности, авторизованное создание, видимость, списки и разрешение
  `[код]` по приоритету система → выбранное место → личный кирпичик зрителя;
- существующие `drafts`, WIP, `user_states`, фото/альбомы, календарь,
  публикация и права администраторов **не меняются**; `draft_values`,
  шаблоны, парсер, заявки и Telegram-интерфейс кирпичиков не создаются;
- 52 новых теста Stage 22A: 19 типов/значений, 14 схемы/ограничений,
  16 сервиса/прав/приватности/гонок и 3 совместимости со Stage 21 — всего
  895.

Stage 22B — Telegram-управление личными кирпичиками:

- версия 7 (`brick_management`): `bricks.revision` с `DEFAULT 1`,
  таблица `brick_sessions` для UI-состояния и триггер монотонной ревизии;
- `brick_state.py` — загрузка/сохранение/очистка `brick_sessions`;
- `brick_store.py` — оптимистичные `update/archive/restore` по
  `owner_user_id + revision`, пагинация владельца, проверка доступности кода
  с учётом архива и `resolve_brick_code_candidates`;
- `handlers/bricks.py` — раздел «Мои кирпичики»: все семь типов, настройки
  числа/выбора, предпросмотр без записи, карточка, правка с неизменным
  типом/ID, архив/восстановление, пагинация, stale-callback и ошибки БД;
- 37 новых тестов Stage 22B: 14 сервиса, 5 схемы, 2 совместимости 6→7 и
  16 интеграционных UI-сценариев — всего **932**.

Граница 22B: личные кирпичики управляются только владельцем; роль админа
места или суперадмина не открывает чужие личные строки. Архив мягкий:
физического удаления нет, ID и код не меняются, архивный код остаётся
занятым. Шаблоны, `template_versions`, `draft_values`, парсер, заявки,
интерфейс кирпичиков места и Mini App **не реализованы**.

Историческая справка: на момент завершения 19A в коде не было Telegram-UI,
поиска и альтернативных названий. В 19B они реализованы; границы ниже
описывают уже текущее состояние.

Не реализовано и остаётся будущим этапам:

- слияние дублей мест;
- шаблоны, версии шаблонов, `draft_values`, парсер шаблонов, заявки,
  интерфейс кирпичиков места и адаптивное создание (Stage 23+ и 26+);
- интеграция выбранного места в девятишаговый мастер (Stage 27);
- изменения текущего визарда, WIP, календаря, фото/альбомов и отправки постов;
- миграции настоящей `Poster/post_bot.db` (она не изменялась).

## 2. Почему свой механизм, а не Alembic

Выбран собственный механизм примерно на 900 строк.

Причины:

1. **Одна файловая SQLite-база.** Нет парка БД, веток схемы и параллельных
   окружений, ради которых нужен Alembic.
2. **Только добавочные изменения.** Политика проекта запрещает DROP,
   переименование и перезапись данных. Autogenerate Alembic для SQLite
   требует ручной проверки (batch mode, ограничения ALTER), то есть не
   экономит работу.
3. **Меньше зависимостей и файлов.** Нет `alembic.ini`, `env.py`, каталога
   `versions/` и отдельного CLI; миграции применяются при штатном старте
   `init_db()`.
4. **Нужные гарантии уже есть:** журнал, контрольные суммы, транзакционное
   применение, проверка несовместимых версий, защита от конкурентного старта.
5. **Простота аудита.** Список миграций — явные SQL-шаги в одном модуле;
   тесты читают настоящую SQLite.

Ограничения решения: нет downgrade/autogenerate, нет истории ветвлений.
Если в будущем схема станет сложной или появятся несколько СУБД, переход на
Alembic возможен: текущий журнал можно перенести в baseline-ревизию.
Разрушительные изменения в любом случае требуют отдельного плана
восстановления, а не автоматического downgrade.

## 3. Журнал `schema_migrations`

```sql
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- `version` — непрерывная нумерация с 1. Пропуски запрещены.
- `name` — человекочитаемое имя миграции.
- `checksum` — sha256 нормализованного SQL (пробельные различия не влияют,
  смысловые — влияют).
- `applied_at` — время применения (UTC, `CURRENT_TIMESTAMP`).

Журнал создаётся самим механизмом до чтения списка миграций. Отдельная
миграция для журнала не нужна.

### Версия 1 (Stage 19A)

Имя: `users_places_place_names`.

Создаёт:

- `users` — пользователь с внутренним `id` и уникальным
  `telegram_user_id`;
- `places` — место с необязательным создателем;
- `place_names` — названия места, канонические и альтернативные;
- индексы, включая частичный уникальный индекс «не более одного
  канонического названия на место».

### Версия 2 (Stage 19B)

Имя: `place_contacts_and_ui_state`.

Добавляет:

- `places.address TEXT` и `places.url TEXT` — необязательные контакты для
  карточки; Stage 19B их только читает и показывает, интерфейса заполнения
  ещё нет;
- `place_sessions` — ограниченный JSON-снимок Telegram-сценария «Места»
  (фаза, выбранные id, введённое название). Ключ — Telegram user ID, а не
  `users.id`: это интерфейсное состояние, как `user_states.user_id`.
  Хранится отдельно, чтобы навигация по справочнику не меняла WIP-пост.

Обе миграции добавляют только новые объекты/колонки и не переписывают
существующие строки. Таблица `schema_migrations` в SQLAlchemy-моделях не
описана: её жизненным циклом владеет только `migrations.py`.
`Base.metadata.create_all()` её не создаёт и не изменяет.

### Версия 3 (Stage 20A)

Имя: `place_admins_and_admin_ui_state`.

Создаёт:

- `place_admins` — история назначений администраторов мест: `place_id`,
  `user_id`, `assigned_by_user_id`, `assigned_at`, `revoked_by_user_id`,
  `revoked_at`. Действующая роль — строка с `revoked_at IS NULL`; частичный
  уникальный индекс `ux_place_admins_one_active`
  (`ON place_admins (place_id) WHERE revoked_at IS NULL`) запрещает на
  уровне БД двух действующих администраторов одного места. Отмены —
  `ON DELETE CASCADE` для места/пользователя и `ON DELETE SET NULL` для
  акторов, чтобы аудит переживал удаление записей.
- `admin_sessions` — JSON-состояние административного кабинета по
  Telegram user ID (фаза, выбранное место, кандидат в администраторы).
  Роли в нём не хранятся: права перепроверяются по env-конфигурации и
  `place_admins` при каждом действии.

Миграция добавляет только новые таблицы и индексы; существующие строки
drafts/user_states/place_names/places не меняются.

### Версия 4 (Stage 20B)

Имя: `place_edit_log`.

Создаёт:

- `place_edit_log` — журнал аудита успешных изменений данных места:
  `place_id` (FK CASCADE), `actor_user_id` (FK SET NULL — запись сохраняется
  даже после удаления пользователя), `action`, `old_value`, `new_value`,
  `created_at`; индекс `ix_place_edit_log_place_id`. Одна строка на
  успешное изменение (переименование канонического названия, алиасы,
  адрес, ссылка); отменённые и неудачные попытки в журнал не пишутся.

Миграция добавляет только новую таблицу; существующие строки не меняются.

### Версия 5 (Stage 21)

Имя: `admin_transfers`.

Создаёт:

```sql
CREATE TABLE admin_transfers (
    id                 INTEGER NOT NULL PRIMARY KEY,
    place_id           INTEGER NOT NULL,
    from_user_id       INTEGER NOT NULL,
    from_assignment_id INTEGER,
    to_user_id         INTEGER,
    opened_by_user_id  INTEGER,
    to_username        VARCHAR(255),
    token_hash         VARCHAR(64),
    status             TEXT NOT NULL,
    created_at         DATETIME NOT NULL,
    expires_at         DATETIME NOT NULL,
    opened_at          DATETIME,
    confirmed_at       DATETIME,
    closed_at          DATETIME,
    notified_at        DATETIME,
    CHECK (status IN (
        'pending', 'confirmed', 'rejected', 'cancelled',
        'expired', 'superseded'
    )),
    FOREIGN KEY (place_id) REFERENCES places (id) ON DELETE CASCADE,
    FOREIGN KEY (from_user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (from_assignment_id) REFERENCES place_admins (id)
        ON DELETE SET NULL,
    FOREIGN KEY (to_user_id) REFERENCES users (id) ON DELETE SET NULL,
    FOREIGN KEY (opened_by_user_id) REFERENCES users (id)
        ON DELETE SET NULL
);
```

- `from_user_id`/`to_user_id` — внутренние `users.id`, а не username;
  `to_user_id` заполняется сразу для известного получателя либо после
  явного утверждения первого открывшего ссылку;
- `opened_by_user_id` — только кандидат, а не получатель прав;
- `token_hash` — SHA-256 случайного токена; сырой токен в таблице и
  `admin_sessions` не хранится;
- `from_assignment_id` фиксирует назначение, с которого начата передача:
  после замены/отзыва администратора предложение становится
  `superseded`;
- `notified_at` отмечает только подтверждённую Telegram доставку
  уведомления; NULL показывается инициатору как явный сбой/неизвестность.

Индексы:

```sql
CREATE UNIQUE INDEX ux_admin_transfers_one_pending_place
    ON admin_transfers (place_id) WHERE status = 'pending';
CREATE INDEX ix_admin_transfers_pending_to_user
    ON admin_transfers (to_user_id) WHERE status = 'pending';
CREATE UNIQUE INDEX ux_admin_transfers_token_hash
    ON admin_transfers (token_hash) WHERE token_hash IS NOT NULL;
CREATE INDEX ix_admin_transfers_place_id ON admin_transfers (place_id);
CREATE INDEX ix_admin_transfers_from_user_id ON admin_transfers (from_user_id);
CREATE INDEX ix_admin_transfers_to_user_id ON admin_transfers (to_user_id);
```

Частичный уникальный индекс разрешает **одно** активное предложение на
место; закрытые строки остаются историей. Миграция добавляет только новую
таблицу и индексы, существующие данные не переписываются.

### Версия 6 (Stage 22A)

Имя: `bricks_registry`.

Создаёт таблицу `bricks` и сидирует девять системных определений. Это
**определения полей**, а не значения конкретного анонса: `draft_values`,
`templates` и `template_versions` в версии 6 отсутствуют.

```sql
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
    CONSTRAINT ck_bricks_type CHECK (type IN (
        'short_text', 'long_text', 'date', 'time', 'url', 'number', 'choice'
    )),
    CONSTRAINT ck_bricks_scope CHECK (scope IN ('system', 'place', 'personal')),
    CONSTRAINT ck_bricks_source CHECK (source IN ('ask', 'place', 'context')),
    CONSTRAINT ck_bricks_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_bricks_scope_owner CHECK (
        (scope = 'system' AND place_id IS NULL AND owner_user_id IS NULL)
        OR (scope = 'place' AND place_id IS NOT NULL AND owner_user_id IS NULL)
        OR (scope = 'personal'
            AND place_id IS NULL AND owner_user_id IS NOT NULL)
    ),
    CONSTRAINT ck_bricks_code_length CHECK (length(code) BETWEEN 2 AND 64),
    CONSTRAINT ck_bricks_code_trimmed CHECK (code = trim(code)),
    CONSTRAINT ck_bricks_code_no_space CHECK (instr(code, ' ') = 0),
    CONSTRAINT ck_bricks_code_no_brackets
        CHECK (instr(code, '[') = 0 AND instr(code, ']') = 0),
    CONSTRAINT ck_bricks_no_photo_code CHECK (code <> 'фото'),
    CONSTRAINT ck_bricks_title_not_empty CHECK (length(trim(title)) > 0),
    CONSTRAINT ck_bricks_rules_json_object CHECK (
        rules_json IS NULL
        OR (json_valid(rules_json) AND json_type(rules_json) = 'object')
    ),
    CONSTRAINT fk_bricks_place_id
        FOREIGN KEY (place_id) REFERENCES places (id) ON DELETE CASCADE,
    CONSTRAINT fk_bricks_owner_user_id
        FOREIGN KEY (owner_user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_bricks_created_by_user_id
        FOREIGN KEY (created_by_user_id) REFERENCES users (id)
        ON DELETE SET NULL
);
```

Ключевые правила:

- `scope` и владельцы согласованы на уровне БД: у `system` оба владельца
  NULL, у `place` обязателен `place_id`, у `personal` — `owner_user_id`.
- Системные коды глобально уникальны; одинаковый код разрешён разным местам
  и разным пользователям. Это обеспечивают частичные уникальные индексы
  `ux_bricks_system_code`, `ux_bricks_place_code`,
  `ux_bricks_personal_code`.
- `code` — только технический токен: 2–64 символа, без пробелов, скобок и
  `фото`; нормализация NFKC/trim/casefold выполняется сервисом, а БД
  дополнительно защищает инварианты. Ограничений длины заголовка/значения
  нет, заголовки могут совпадать.
- `rules_json` — `NULL` или JSON-объект. Строгая проверка набора/типов
  ключей выполняется `brick_types.py`; значения не исполняются как код/SQL.
- `type` не содержит `any`, `photo`, `media`; `source` — только `ask`,
  `place`, `context`.
- Индексы `ix_bricks_scope_status_code`, `ix_bricks_place_status`,
  `ix_bricks_owner_status`, `ix_bricks_type_status`, `ix_bricks_code`,
  `ix_bricks_title` обслуживают списки, поиск и проверки доступности.

Триггеры (шесть):

| Триггер | Защита |
|---|---|
| `trg_bricks_id_immutable` | запрещает менять `id` |
| `trg_bricks_system_code_immutable` | запрещает менять код системного кирпичика |
| `trg_bricks_system_owner_immutable` | не даёт сделать системный кирпичик местным/личным |
| `trg_bricks_system_no_delete` | системную строку можно архивировать, но не удалить |
| `trg_bricks_reserve_codes_insert` | не даёт создать несистемный код, совпадающий с системным |
| `trg_bricks_reserve_codes_update` | то же при UPDATE кода/области |

Сид (ID и коды стабильны):

| ID | `code` | Тип | `source` | `rules_json` |
|---:|---|---|---|---|
| 1 | `название` | short_text | ask | NULL |
| 2 | `дата` | date | ask | `{"horizon_months": 12}` |
| 3 | `время_начала` | time | ask | NULL |
| 4 | `время_окончания` | time | ask | NULL |
| 5 | `место` | short_text | place | NULL |
| 6 | `адрес_места` | short_text | place | NULL |
| 7 | `ссылка_места` | url | place | NULL |
| 8 | `описание` | long_text | ask | NULL |
| 9 | `контакт` | short_text | ask | NULL |

Сид выполняется только внутри миграции 6, без повторного UPSERT при старте:
контролируемые изменения заголовков/правил не перезаписываются. Повторный
запуск идемпотентен, ID не меняются, архивный системный код остаётся
зарезервированным. Конкурентное создание кирпичика разрешается реальными
уникальными индексами/триггерами: проигравший получает контролируемую
ошибку уникальности, а не дубль.

### Версия 7 (Stage 22B)

Имя: `brick_management`.

Добавляет оптимистичную блокировку и отдельное состояние Telegram-
конструктора личных кирпичиков. Существующие строки и определения **не
переписываются**.

```sql
ALTER TABLE bricks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE brick_sessions (
    telegram_user_id INTEGER NOT NULL PRIMARY KEY,
    payload          TEXT NOT NULL,
    updated_at       DATETIME NOT NULL,
    CONSTRAINT ck_brick_sessions_payload_object CHECK (
        json_valid(payload) AND json_type(payload) = 'object'
    )
);

CREATE TRIGGER trg_bricks_revision_increases
BEFORE UPDATE OF revision ON bricks
FOR EACH ROW
WHEN NEW.revision <= OLD.revision
BEGIN
    SELECT RAISE(ABORT, 'brick revision must increase');
END;
```

- у всех существующих строк, включая девять системных, `revision = 1`;
- `brick_sessions` не имеет FK на `users`, как и `place_sessions`/
  `admin_sessions`: это UI-состояние по Telegram user ID, не доменные данные;
- `payload` — только JSON-объект; проверка выполняется CHECK-ограничением;
- ревизия может только расти. Сервис дополнительно делает условный
  `UPDATE ... WHERE revision = expected`, поэтому конкурентный редактор
  получает `stale`, а не затирает более новую версию;
- миграция 7 не добавляет `draft_values`, `templates`,
  `template_versions` или `brick_proposals` и не меняет `drafts`/WIP/фото;
- перед применением к базе версии 6 автоматически создаётся WAL-aware копия
  состояния 6 (общий механизм §6.1).

## 4. Созданные таблицы и ограничения

### 4.1. `users`

```sql
CREATE TABLE users (
    id               INTEGER NOT NULL PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    username         VARCHAR(255),
    full_name        VARCHAR(255),
    created_at       DATETIME NOT NULL,
    updated_at       DATETIME,
    last_seen_at     DATETIME,
    CONSTRAINT uq_users_telegram_user_id UNIQUE (telegram_user_id)
);
```

- `telegram_user_id` — постоянный идентификатор Telegram; **UNIQUE**
  проверяется самой SQLite.
- `username` — справочное поле, не идентификатор; хранится без ведущего `@`.
- `user_store.get_or_create_user` делает атомарный SQLite UPSERT
  (`ON CONFLICT(telegram_user_id) DO UPDATE`), поэтому конкурентные вызовы
  не создают дубль.
- `created_at/updated_at/last_seen_at` заполняются приложением (наивный UTC,
  как в существующих моделях).

### 4.2. `places`

```sql
CREATE TABLE places (
    id                 INTEGER NOT NULL PRIMARY KEY,
    created_by_user_id INTEGER,
    created_at         DATETIME NOT NULL,
    address            TEXT,                 -- добавлено миграцией 2
    url                TEXT,                 -- добавлено миграцией 2
    CONSTRAINT fk_places_created_by_user_id
        FOREIGN KEY (created_by_user_id) REFERENCES users (id)
        ON DELETE SET NULL
);
```

- `created_by_user_id` — внутренний `users.id`, не Telegram ID.
- Создатель **не** становится администратором автоматически; роли
  назначаются отдельно в Stage 20A. Создание места не выдаёт скрытых прав.
- `address`/`url` необязательны; пустые значения хранятся как NULL.
  С Stage 20B их редактирует администратор места или суперадмин в кабинете.
  У адреса нет искусственного предела длины (полное значение хранится в
  TEXT), ссылка ограничена 500 символами интерфейса.
- Удаление пользователя обнуляет создателя, само место сохраняется.

### 4.3. `place_names`

```sql
CREATE TABLE place_names (
    id                 INTEGER NOT NULL PRIMARY KEY,
    place_id           INTEGER NOT NULL,
    name               TEXT NOT NULL,
    name_search        TEXT NOT NULL,
    is_canonical       INTEGER NOT NULL,
    created_by_user_id INTEGER,
    created_at         DATETIME NOT NULL,
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
);
CREATE INDEX ix_place_names_place_id ON place_names (place_id);
CREATE INDEX ix_place_names_name_search ON place_names (name_search);
CREATE UNIQUE INDEX ux_place_names_one_canonical
    ON place_names (place_id) WHERE is_canonical = 1;
```

Действующие ограничения:

| Ограничение | Что запрещает |
|---|---|
| `CHECK (length(trim(name)) > 0)` | пустое/пробельное отображаемое название |
| `CHECK (length(trim(name_search)) > 0)` | пустая нормализованная строка (приложение дополнительно требует букву/цифру) |
| `CHECK (is_canonical IN (0,1))` | произвольные значения флага |
| `UNIQUE (place_id, name_search)` | два одинаковых нормализованных названия внутри одного места |
| `UNIQUE INDEX ... WHERE is_canonical = 1` | больше одного канонического названия у места |
| FK `place_id ... ON DELETE CASCADE` | название без места; удаление места удаляет названия |
| FK `created_by_user_id ... ON DELETE SET NULL` | ссылку на несуществующего пользователя |

Чего схема **не** делает:

- не требует наличия хотя бы одного названия у места (это обеспечивает
  прикладной `place_store.create_place`, который вставляет место и
  каноническое название одной транзакцией);
- не объединяет места с похожими названиями;
- не делает название уникальным между разными местами. Два места
  «Кофейня «Дом»» и «кофейня дом» — разные строки; решение о слиянии
  принимает администратор явно (Stage 20+), автоматического слияния нет.

### 4.4. `place_sessions`

```sql
CREATE TABLE place_sessions (
    telegram_user_id INTEGER NOT NULL PRIMARY KEY,
    payload          TEXT NOT NULL,   -- ограниченный JSON сценария
    updated_at       DATETIME NOT NULL
);
```

- Строка создаётся при входе в «Места», обновляется после каждого шага и
  очищается при возврате в меню, `/start`, `/help`, `/drafts`, `/cancel`,
  входе в мастер поста или переходе в другой раздел.
- В `payload` нет копии справочника и черновика: только `phase`, `place_id`,
  `display_name_id`, `query`, `candidate`, номера страниц и служебные id
  неудачных вводов. Этого достаточно для восстановления экрана после
  рестарта, но недостаточно, чтобы «оживить» доменные данные.
- Таблица не связана внешним ключом с `users`: ключ — Telegram ID, как у
  `user_states.user_id`. Это состояние интерфейса, а не роль/владение.

### 4.5. Нормализация названия

`place_store.normalize_place_name`:

1. NFKC (`unicodedata.normalize`);
2. `casefold` (регистр, в том числе кириллица);
3. `ё → е`;
4. пунктуация и пробельные символы → пробел;
5. сжатие пробелов;
6. пустой результат → `PlaceNameError`.

Примеры:

| Отображаемое | `name_search` |
|---|---|
| `Кофейня «Дом»` | `кофейня дом` |
| `Ёлка-Кафе` | `елка кафе` |
| `Тёплый   Дом` | `теплый дом` |

Отображаемое название сохраняет регистр, пунктуацию и эмодзи; нормализация
применяется только к `name_search`. Название целиком из пунктуации или
эмодзи приложение отклоняет: нужен хотя бы один буквенно-цифровой символ.

### 4.6. `create_place`

`place_store.create_place(session, canonical_name, created_by_user_id=None,
address=None, url=None)`:

- проверяет и очищает название;
- вычисляет `name_search`;
- вставляет `places` и `place_names` (`is_canonical=1`) в одной транзакции;
- при любой ошибке делает rollback, поэтому полузаписей не остаётся;
- возвращает сохранённое место.

Одинаковое название в двух местах разрешено: это осознанная политика «без
неявных слияний».

### 4.7. `bricks` и `brick_sessions`

`bricks` создана в версии 6, `revision` и `brick_sessions` — в версии 7;
полная SQL-схема, индексы, триггеры и сид — в разделах «Версия 6» и
«Версия 7» выше. Таблица `bricks` хранит только определения полей, не
значения анонса; `brick_sessions` — только черновик Telegram-конструктора.

- **Типы:** `short_text`, `long_text`, `date`, `time`, `url`, `number`,
  `choice`. Парсеры значений переиспользуют существующие функции даты,
  времени (`Europe/Moscow`, горизонт 12 месяцев) и безопасной
  нормализации URL; `any`/`photo`/`media` отсутствуют.
- **Области:** `system`, `place`, `personal`; **источники:** `ask`,
  `place`, `context`.
- **Разрешение `[код]`:** системный кирпичик → кирпичик выбранного места →
  личный кирпичик зрителя. Архивный системный код ничего не разрешает и не
  передаётся местному/личному. Одинаковые несистемные коды у разных
  владельцев не конфликтуют.
- **Состояния значения:** `missing`, `empty_optional`, `valid`, `invalid`,
  `needs_input`.
- **Права:** личный кирпичик создаёт только сам пользователь; кирпичик
  места — администратор этого места или суперадмин; системный — только
  зарегистрированный суперадмин. Чужие личные кирпичики не видны, не
  ищутся и не разрешаются.
- **Граница Stage 22B:** личный Telegram-конструктор, карточка, правка,
  мягкий архив/восстановление и пагинация добавлены. `draft_values`,
  шаблоны, парсер `[код]`, `brick_proposals`, интерфейс кирпичиков места и
  адаптивное создание не добавлены. Существующие `drafts`/WIP/
  `user_states` и фото/альбомы не мигрируются.
- **Архив и код.** Физического удаления нет; архивный код остаётся
  занятым, в том числе для владельца. Восстановление возвращает тот же
  `brick_id` и код. Тип после создания неизменяем. Все операции проверяют
  владельца в сервисе; суперадмин и админ места не получают доступа к
  чужим личным кирпичикам.

## 5. Порядок инициализации

`database.init_db()` вызывается при старте бота и выполняет:

1. `run_migrations(engine)` — при необходимости копия базы, затем журнал и
   недостающие версии 1–7;
2. `Base.metadata.create_all(engine)` — **легаси-базлайн** `drafts` и
   `user_states` (новые таблицы уже существуют и пропускаются);
3. `_migrate(engine)` — добавление колонок `photos/formats/updated_at`
   в очень старых `drafts`.

Почему такой порядок:

- миграции должны успеть создать новые таблицы до `create_all`;
- `create_all` не умеет добавлять колонки в существующие таблицы, поэтому
  исторический бэкфилл `_migrate` сохранён;
- на пустой базе `create_all` создаёт легаси-таблицы; на старой — не трогает
  существующие;
- на рабочей базе единственная точка инициализации — `init_db()`. Нельзя
  вызывать `Base.metadata.create_all()` на настоящей базе в обход журнала.

## 6. Транзакции, сбой и конкурентность

`run_migrations`:

1. проверяет, есть ли у существующей непустой базы ожидающие версии; если
   да — создаёт автоматическую копию (раздел 6.1). При сбое копии работа
   прекращается до любого DDL;
2. открывает отдельное соединение `sqlite3` (`isolation_level=None`);
3. ставит `busy_timeout=30000` и `PRAGMA foreign_keys=ON`;
4. выполняет `BEGIN IMMEDIATE`;
5. создаёт журнал (если его нет) и читает применённые версии;
6. проверяет совместимость и контрольные суммы;
7. выполняет все ожидающие миграции;
8. вставляет строки журнала в той же транзакции;
9. делает `COMMIT`; при любой ошибке — `ROLLBACK`.

Следствия:

- **Сбой посреди миграции** откатывает весь запуск: DDL SQLite транзакционен.
  Журнал не содержит неприменённой версии; база остаётся в состоянии до
  запуска.
- **Повторный запуск** после устранения причины применяет недостающие
  версии заново; уже зафиксированные версии не переприменяются.
- **Конкурентный старт** (два процесса): второй ждёт `BEGIN IMMEDIATE` до
  `busy_timeout`, затем видит журнал первого и ничего не делает.
- Ожидающие миграции применяются одной транзакцией. Если их несколько и
  одна падает, откатываются все ещё не зафиксированные — это осознанно:
  схема не остаётся наполовину обновлённой.
- Долгие миграции блокируют запись; для маленькой SQLite-базы шаги должны
  оставаться короткими.

### 6.1. Автоматическая копия перед миграцией (Stage 19B)

Копия делается **только** если файл базы уже существует, не пуст, и журнал
показывает, что есть непримененные версии. Новая/пустая база пользовательских
данных не содержит — для неё копия не создаётся.

Порядок:

1. `_has_pending_migrations` читает только `sqlite_master` и
   `schema_migrations`; отсутствие журнала у старой базы означает «ожидают
   все версии»;
2. `create_database_backup` открывает источник; сначала строго read-only,
   а при проблеме с WAL после аварийного завершения — обычным соединением,
   чтобы SQLite корректно прочитал WAL;
3. `sqlite3.Connection.backup()` создаёт снимок через SQLite Backup API, а
   не копированием основного файла: зафиксированные данные из WAL попадают
   в копию;
4. целевой файл создаётся `mkstemp` с правами `0600`, затем проверяется
   `PRAGMA integrity_check`; только после `ok` он атомарно переименовывается
   в `post_bot.db.backup-<UTC>-p<pid>[-N]` через `os.replace`;
5. готовые копии не удаляются и не перезаписываются. Имена содержат
   микросекунды и PID; суффикс `-N` защищает от коллизии. Пользовательские
   файлы с другими именами приложение вообще не ищет;
6. любая ошибка чтения/backup/integrity/rename → `BackupError`, миграция не
   начинается, база остаётся в состоянии до запуска.

Автоматической ротации нет: копии накапливаются, пока владелец не удалит их
вручную после успешного запуска. Два одновременных старта могут создать две
копии до того, как один из процессов получит блокировку; обе безопасны.
Копия — страховка именно механизма миграций; эксплуатационный backup перед
выкаткой всё равно рекомендуется (раздел 10).

## 7. Несовместимые версии и контрольные суммы

Механизм отказывается работать (`IncompatibleSchemaError`, база не меняется),
если:

- в журнале есть версия, которой нет в коде (база новее приложения);
- журнал не является непрерывным префиксом версий кода (пропуск/удаление
  строки);
- контрольная сумма уже применённой миграции не совпадает с кодом
  (миграцию изменили задним числом).

Правило: **применённую миграцию нельзя редактировать**. Любое изменение
схемы — новая версия в конце списка. Исправление опечатки в тексте
миграции, меняющее SQL, тоже требует новой версии; уже применённую строку
журнала вручную не правят.

`MigrationError` (не `IncompatibleSchemaError`) означает, что не удалось
применить DDL/получить блокировку; база откачена к состоянию до запуска.
Текст ошибки содержит номер и имя миграции.

## 8. Совместимость со старой базой

Старая база Poster (до Stage 19A) содержит `drafts`, `user_states` и данные
пользователей. Миграция 1:

- не меняет `drafts` и `user_states`;
- не переписывает строки;
- только добавляет новые таблицы и журнал.

База, уже прошедшая Stage 19A (журнал версии 1), при обновлении до 19B:

- получает `places.address/url` и `place_sessions` миграцией 2;
- сохраняет все места, названия, черновики и WIP без изменений;
- получает автоматическую копию именно состояния версии 1 перед миграцией.

База Stage 21 (журнал версий 1–5) при обновлении до Stage 22A:

- получает только миграцию 6: `bricks`, девять системных строк, индексы и
  триггеры;
- сохраняет `drafts`, `user_states`, `place_sessions`, фото/альбомы,
  `place_admins`, `place_edit_log` и `admin_transfers` без изменений;
- не получает `templates`, `template_versions`, `draft_values` или
  `brick_proposals`;
- перед DDL автоматически копирует состояние версии 5; повторный запуск
  видит журнал 1–6 и ничего не применяет.

База Stage 22A (журнал версий 1–6) при обновлении до Stage 22B:

- получает только миграцию 7: `bricks.revision = 1` у существующих строк,
  `brick_sessions` и триггер `trg_bricks_revision_increases`;
- сохраняет все кирпичики, `drafts`, WIP, фото/альбомы, места, роли и
  передачи без изменений;
- не получает `templates`, `template_versions`, `draft_values` или
  `brick_proposals`;
- перед DDL автоматически копирует состояние версии 6; копия не содержит
  `revision` и `brick_sessions`, проходит `integrity_check`, а повторный
  запуск видит журнал 1–7 и ничего не применяет.

`init_db` затем:

- `create_all` не трогает существующие таблицы;
- `_migrate` добавляет отсутствующие колонки `photos`, `formats`,
  `updated_at` в старые `drafts` (NULL у существующих строк — допустимо).

Проверено автоматически на временной базе, воспроизводящей старую схему,
с фиктивными черновиком и WIP: значения полей, JSON-форматы и WIP-снимок
остаются прежними (в древнем варианте добавляются только новые NULL-колонки).

## 9. Как добавлять миграции в будущих этапах

1. **Никогда** не редактировать применённую миграцию и не удалять строки
   журнала.
2. Добавить в `migrations.py` новый `Migration(version=N, name=...,
   statements=(...))` в конец `DEFAULT_MIGRATIONS`.
3. Версия — ровно `предыдущая + 1`, без пропусков.
4. Только добавочные операции: `CREATE TABLE/INDEX`, `ALTER TABLE ADD
   COLUMN`. Разрушительные изменения — отдельный план восстановления.
5. Все шаги одной миграции должны быть идемпотентны по смыслу и выполняться
   в одной транзакции; не использовать `executescript` (он ломает явную
   транзакционность).
6. Добавить тесты:
   - новая база: версия применяется, таблицы/ограничения есть;
   - старая база: данные сохраняются;
   - первая ожидающая версия: копия создаётся до DDL;
   - повторный запуск: миграция не применяется дважды;
   - сбой: журнал не содержит версию, схема откатана;
   - сбой копии: миграция не начинается.
7. Прогнать `make check` на Python 3.13 и 3.14.
8. Обновить этот документ и `stage18_architecture.md`, если меняется схема.

## 10. Безопасное применение к настоящей базе

Миграции применяются автоматически при старте бота (`init_db`). Приложение
само создаёт WAL-aware копию перед первым ожидающим применением (раздел 6.1),
но перед выкаткой всё равно нужно подготовить эксплуатационный backup:

1. **Остановить бота** и убедиться, что процесс завершён.
2. Сделать дополнительную резервную копию всех файлов базы:
   `post_bot.db`, `post_bot.db-wal`, `post_bot.db-shm` (если есть).
   Предпочтительно — `sqlite3 post_bot.db ".backup 'post_bot.db.backup-ГГГГММДД'"`;
   затем проверить `PRAGMA integrity_check`.
3. Развернуть новую версию кода.
4. Запустить бота (миграции применятся) **или** заранее, при остановленном
   боте, выполнить из каталога `Poster`:
   `DATABASE_URL="sqlite:////абсолютный/путь/post_bot.db" python -c "from database import init_db; init_db()"`.
5. Проверить лог миграций, наличие автоматической копии и
   `SELECT version, name FROM schema_migrations;`.
6. При ошибке: остановить бота, вернуть файлы копии на место, не править
   журнал вручную; сообщить об ошибке. Если миграция не начиналась из-за
   `BackupError`, база не изменена — устранить причину (место/права/диск) и
   повторить запуск; ручная правка журнала не нужна.

Запрещено:

- применять миграции к настоящей базе из тестов;
- запускать тесты с `DATABASE_URL`, указывающим на рабочую базу;
- чистить/удалять «мусор» в реальной базе;
- запускать polling ради проверки миграции.

## 11. Тесты Stage 19A–22B

Файлы Stage 19A:

- `tests/test_stage19a_migrations.py` — новая база, повторный запуск,
  рестарт, старая схема, древняя схема с `_migrate`, сбой посреди миграции,
  сохранность уже применённых версий, неизвестная версия, разрыв журнала,
  изменение применённой миграции, `:memory:` отказ, конкурентный запуск,
  неизменность реальной `post_bot.db`;
- `tests/test_stage19a_schema.py` — уникальность Telegram user ID, UPSERT,
  кириллица/Unicode, пустые названия (CHECK), конфликты имён внутри места,
  одинаковые названия в разных местах, частичный уникальный индекс,
  внешние ключи и каскады, конкурентное создание пользователей/мест,
  наличие `bricks` и отсутствие будущих шаблонных таблиц, фото остаются
  данными `Draft`.

Файлы Stage 19B:

- `tests/test_stage19b_store.py` — ранжирование поиска, `е/ё`, пунктуация,
  Unicode и частичные совпадения, похожесть, отсутствие слияний, атомарное
  создание, конкурентные альтернативы, лимит длины и контакты карточки;
- `tests/test_stage19b_places_flow.py` — 39 интеграционных сценариев через
  `Application.process_update`: меню, 500 мест, поиск, дубли, создание,
  альтернативы, подтверждение, рестарт, очистка, удалённые/подменённые места,
  устаревшие callback, долгий/неверный ввод и предел длины экрана,
  отсутствие записи в `drafts`;
- `tests/test_stage19b_backup.py` — новая/старая база, копия между версиями
  1 и 2, WAL, запрет миграции при сбое копии и `mkstemp`, права и целостность,
  сохранность пользовательских копий, restore и повторная миграция.

Файлы Stage 20A:

- `tests/test_stage20_admin_logic.py` — 34 юнит-теста слоя `place_admins`:
  строгий парсинг `SUPERADMIN_USER_IDS`/`SUPERADMIN_CONTACT_URL`, роли
  (`permissions`), поиск известных пользователей, атомарные
  назначение/замена/отзыв, аудит-строки, отказ посторонним, идемпотентность
  повторов, конкурентные вставки/отзывы на настоящей SQLite (частичный
  уникальный индекс) и честные статусы `changed`/`has_admin`/`conflict`;
- `tests/test_stage20_admin_flow.py` — 27 интеграционных сценариев через
  `Application.process_update`: видимость кнопки кабинета, отказ посторонним,
  кабинет суперадмина (список/карточка/назначение/замена/отзыв), кабинет
  администратора места (только свои места), экран «Я админ места», подделанные
  callback и устаревшие кнопки, рестарт с восстановлением фазы поиска и
  подтверждения, трекинг пользователей.

Файлы Stage 20B:

- `tests/test_stage20b_edit_logic.py` — 24 юнит-теста редактирования места:
  валидация/нормализация адреса и ссылки, права (суперадмин, админ места,
  посторонний), атомарность операций и статусы (ok/changed/duplicate/
  unchanged/canonical/not_found), аудит `place_edit_log`;
- `tests/test_stage20b_edit_flow.py` — 15 интеграционных сценариев через
  `Application.process_update`: меню редактирования, каноническое название
  (старое остаётся альтернативой), алиасы (добавление/переименование/
  удаление/дубликат), адрес и ссылка (установка/удаление), отказы посторонним,
  устаревшие callback и гонка подтверждения.

Файлы Stage 21:

- `tests/test_stage21_transfer_logic.py` — 40 юнит-тестов доменного слоя:
  проверка username, поиск известных получателей, права только действующего
  админа места, запрет self/чужого/отозванного назначения, привязка по
  постоянному Telegram ID, хранение только SHA-256, одно активное
  предложение на место, границы 24 часов (`23:59:59`/ровно `24:00:00`),
  ленивое истечение без планировщика, утверждение кандидата, отмена/отказ,
  конкурентное подтверждение с одним победителем, откат при сбое commit,
  `superseded` после Stage 20A и сохранность длинного адреса;
- `tests/test_stage21_transfer_flow.py` — 16 интеграционных сценариев через
  `Application.process_update`: кнопка только у действующего админа,
  известный и неизвестный получатель, одноразовая ссылка, первый кандидат,
  утверждение инициатором, подтверждение только в своём аккаунте, смена
  доступа Stage 20B сразу после передачи, отмена/отказ, сбой доставки,
  устаревшие callback, рестарт, сохранение WIP и явный предпросмотр
  длинного адреса.

Файлы Stage 22A:

- `tests/test_stage22a_brick_types.py` — 19 тестов каталога типов, девяти
  системных определений, нормализации кодов/заголовков, строгих правил,
  парсинга даты/времени/URL и пяти состояний значений;
- `tests/test_stage22a_brick_schema.py` — 14 тестов миграции 6: реальные
  UNIQUE/CHECK/FK, NULL-правила, индексы, шесть триггеров, сид, стабильные
  ID, идемпотентный рестарт, сохранение контролируемых изменений, запрет
  удаления системной строки и резервирование архивного кода;
- `tests/test_stage22a_brick_service.py` — 16 тестов сервиса: чтение,
  поиск, проверки уникальности, авторизованное создание, права
  админа/суперадмина, приватность личных кирпичиков, приоритет разрешения
  `[код]`, отсутствие подмены системного кода, конкурентная гонка;
- `tests/test_stage22a_compatibility.py` — 3 теста совместимости со Stage 21:
  миграция существующей базы, неизменность `drafts`/WIP/фото и отсутствие
  шаблонных таблиц, сохранность прав администраторов и передачи админки.

Файлы Stage 22B:

- `tests/test_stage22b_brick_schema.py` — 5 тестов миграции 7: `revision`,
  `brick_sessions`, CHECK JSON-объекта, триггер монотонной ревизии,
  резервирование архивного системного кода и идемпотентный рестарт;
- `tests/test_stage22b_brick_service.py` — 14 тестов сервиса: создание/
  правка с сохранением ID/типа, ревизии, мягкий архив/восстановление,
  идемпотентные статусы, пожизненная резервация кода, пагинация, права
  владельца, роль админа места и суперадмина, кандидаты `[код]`, откат
  ошибок и конкурентная гонка;
- `tests/test_stage22b_compatibility.py` — 2 теста миграции 6→7 на временной
  копии Stage 22A: WAL-aware копия состояния 6, сохранность черновика/WIP/
  места/кирпичика, `revision=1`, идемпотентность и сохранность
  `brick_sessions` после рестарта;
- `tests/test_stage22b_bricks_flow.py` — 16 интеграционных сценариев через
  `Application.process_update`: меню/`/bricks`, все семь типов, настройки
  числа/выбора, подсказки/конфликты кодов, предпросмотр без записи, правка
  с неизменным ID/типом, stale-ревизия, архив/восстановление, резервация
  кода, пагинация, рестарт, `/cancel`, изоляция от поста/мест/кабинета,
  устаревшие callback, сбой БД и приватность.

Всего в полном наборе — **932 теста** (895 после Stage 22A + 37 новых).

Все тесты используют только временные файлы SQLite. Настоящая
`Poster/post_bot.db` не открывается на запись; тест сверяет её sha256 и
mtime до и после полной инициализации временной базы.

## 12. Ограничения Stage 19A–22B

- В базе технически возможно место без названий (нет триггера); публичного
  API для этого нет. `get_place_details`/списки такие строки не показывают.
- Альтернативные названия может добавить любой пользователь; редактировать
  и удалять их может суперадмин или админ места (Stage 20B, кабинет).
- Адрес/ссылка места редактируются админом места или суперадмином в
  кабинете (Stage 20B); заполнение посторонним недоступно. Адрес хранится
  без искусственного предела длины; ссылка — до 500 символов интерфейса.
- Слияния дублей мест нет: только предупреждение и явный выбор пользователя.
- Подтверждённое место хранится в `place_sessions` (сценарий), но
  девятишаговый мастер его пока не использует — интеграция в Stage 27.
- Стандартный шаблон не создаётся: Stage 23 должен добавить бэкфилл
  существующих мест и атомарное создание v1 для каждого нового.
- Stage 22B добавляет только личный Telegram-конструктор кирпичиков.
  `draft_values`, `templates`, `template_versions`, `brick_proposals`,
  парсер `[код]`, интерфейс кирпичиков места, Mini App и адаптивное
  создание не реализованы. Мастер поста продолжает использовать девять
  легаси-полей, а сохранённые кирпичики пока не вставляются в посты.
- Личные кирпичики не удаляются физически и не показываются другим
  пользователям, включая суперадмина и админов мест. Архивный код остаётся
  занятым навсегда; тип после создания неизменяем.
- У кирпичика ограничен только технический код (2–64 символа); заголовок,
  `title` и значения произвольных искусственных лимитов не имеют. Заголовки
  могут совпадать. Фото не являются кирпичиком и не получают токен `[фото]`.
- Системные определения сидируются только миграцией 6. Повторный старт не
  перезаписывает контролируемые заголовки/правила; системную строку нельзя
  удалить, только архивировать. Архивный системный код остаётся
  зарезервированным и не разрешается как личный/местный.
- Чужие личные кирпичики не видны и не разрешаются; кирпичик места создаёт
  только администратор этого места или суперадмин, системный — только
  зарегистрированный суперадмин.
- Stage 20A управляет ролями, Stage 20B — данными места, Stage 21 — передачей
  админки через двустороннее подтверждение. Передачу создаёт только
  действующий администратор конкретного места; суперадмин без этой роли
  пользуется только своими полномочиями Stage 20A.
- У места не больше одного `pending`-предложения. Ленивое истечение
  выполняется при чтении/действии; фоновых задач нет, поэтому статус в БД
  может оставаться `pending`, пока к предложению не обратятся, но
  подтвердить его после `expires_at` нельзя.
- Сырой токен приглашения показывается только при создании и не
  восстанавливается. Если инициатор потерял ссылку, нужна отмена и новое
  предложение.
- Суперадмины задаются только переменной окружения; DB-колонки роли и
  назначения через бота нет. Пустой `SUPERADMIN_USER_IDS` закрывает кабинет,
  обычные функции продолжают работать.
- Миграции требуют файловую SQLite; `:memory:` даёт явную ошибку
  (тесты используют временные файлы).
- Автоматический backup страхует миграцию, но downgrade нет: откат — только
  восстановление копии. Автоматической ротации копий нет.
- `create_all` остаётся для легаси-таблиц; новые таблицы должны создаваться
  только миграциями. Прямой `create_all` на рабочей базе запрещён.

## 13. Готовность к Stage 23

После 19A–22B работают:

- журнал миграций, копия перед первым ожидающим применением и правило
  добавлять только новые версии;
- `users` с уникальным Telegram ID и UPSERT; регистрация профиля при каждом
  Update (`handlers/user_tracking.py`);
- `places` + `place_names` с каноническим и альтернативными названиями,
  нормализацией, ограничениями и поиском;
- `place_sessions` для восстановления сценария «Места», изолированный от WIP;
- `place_admins` — аудит назначений с ограничением «не более одного
  действующего админа на место» на уровне БД;
- `admin_sessions` — восстановление кабинета после рестарта без хранения
  прав;
- `place_edit_log` и редактирование данных места админом (название,
  альтернативные названия, адрес без 500-символьного предела, ссылка);
- `admin_transfers` — предложение на 24 часа, известный получатель,
  одноразовая ссылка (в БД только SHA-256), кандидат, явное утверждение
  инициатором, подтверждение получателем, отмена/отказ и `superseded` после
  изменения администратора;
- `bricks` — девять системных определений, типы/правила/источники/области,
  реальные ограничения и триггеры, сервис чтения/поиска/создания,
  приватность и разрешение `[код]`; личный Telegram-конструктор,
  ревизии, мягкий архив/восстановление и `brick_sessions` Stage 22B;
  значения анонса ещё не хранятся;
- атомарные создание места, альтернативы, назначение/замена/отзыв
  администратора, подтверждение передачи и конкурентное создание кирпичика
  (ограничения SQLite + одна транзакция, откат при сбое);
- внешние ключи/каскады на соединениях приложения.

Stage 22B не менял мастер поста, WIP, календарь, фото/альбомы и отправку.
Остаётся будущим: Stage 23 — шаблоны (включая стандартный шаблон для
существующих/новых мест), `template_versions` и `draft_values`; Stage 24 —
парсер; Stage 25 — редактор; Stage 26 — заявки на кирпичики и интерфейс
кирпичиков места; Stage 27 — интеграция подтверждённого места и значений
кирпичиков в мастер поста. Реальная `Poster/post_bot.db` в тестах не
изменялась.
