# Миграции схемы SQLite: Stage 19A, 19B, 20A, 20B, 21, 22A, 22B, 23, 24, 25, 26, 27A, 27C и 28

Статус: **реализовано в Stage 19A, 19B, 20A, 20B, 21, 22A, 22B, 23, 24, 25, 26, 27A, 27C и 28**.
Документ описывает фактический механизм, созданные таблицы, ограничения,
автоматическую резервную копию, совместимость со старой базой и порядок
безопасного применения миграций. Текущая последняя версия — **22**
(`builtin_templates_v4`, Poster 2.0 U1a; до неё — 21 `place_city`,
20 `brick_proposal_description`, 19 `brick_code_conflicts`,
18 `builtin_templates_v3`, ещё раньше — 17 `archive_legacy_drafts`
и 16 `announcement_draft_origin`).
Целевая архитектура
справочника мест, кирпичиков и шаблонов — в
[`stage18_architecture.md`](stage18_architecture.md).

## 1. Границы Stage 19A, 19B, 20A, 20B, 21, 22A, 22B, 23, 24, 25, 26, 27A, 27C и 28

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

Stage 23 — шаблоны как данные:

- версия 8 (`templates_versions_snapshots`): таблицы `templates`,
  `template_versions`, `template_version_bricks`, `template_snapshots`,
  `template_snapshot_bricks`; частичный уникальный индекс «один стандарт на
  место», CHECK-ограничения формы и непустой версии, триггеры
  неизменяемости версий/снимков/привязок и монотонной `revision`;
- стандартный шаблон создаётся вместе с местом; миграция бэкфиллит v1 для
  существующих мест, не перезаписывая уже имеющиеся стандарты;
- личные шаблоны изолированы по `owner_user_id`; версии и снимки
  неизменяемы; привязки — постоянные `brick_id`;
- `template_store.py` — создание личного шаблона, чтение, правка секций,
  снимки, права и оптимистичная блокировка.

Stage 24 — парсер и коды привязок:

- версия 9 (`template_binding_codes`): колонка `code` в
  `template_version_bricks` и `template_snapshot_bricks` с backfill по
  `bricks.code`; временно снимаются/восстанавливаются триггеры
  неизменяемости; уникальные индексы `(version, code)` и `(snapshot, code)`
  гарантируют однозначное восстановление `brick_id`;
- `template_parser.py` и `template_compiler.py`: разбор `[код]`,
  экранирование, Telegram-разметка/ссылки, привязка к `brick_id` при
  сохранении и рендер сохранённой версии/снимка;
- миграция 9 не меняет `drafts`, WIP, фото и более ранние таблицы.

Stage 25 — Telegram-редактор шаблонов:

- версия 10 (`template_sessions`): `telegram_user_id` PK, JSON-объект
  payload с CHECK `json_valid`/`json_type='object'`, `updated_at`;
- `template_state.py` — фазы редактора, загрузка/сохранение/очистка,
  признаки черновика и текстовой фазы;
- `handlers/templates.py` — «Мои шаблоны» и `/templates`, карточка, создание
  из стандарта/пустого, редактирование секций полным сообщением,
  предпросмотр, доступные кирпичики, разрешение неоднозначностей,
  подтверждённый сброс, стандартный шаблон из кабинета места;
- состояние изолировано от `user_states`, `place_sessions`,
  `admin_sessions` и `brick_sessions`; переход в другой раздел, `/start`,
  `/help`, `/cancel` очищают незавершённое редактирование;
- 144 новых теста Stage 23–25 (включая миграции 8–10, совместимость,
  снимки, сбросы и 23 интеграционных сценария редактора) — всего
  **1077** в полном наборе.

Stage 26 — предложения кирпичиков:

- версия 11 (`brick_proposals`): неизменяемый снимок личного кирпичика,
  область `place`/`system`, автор, `place_id`, дата, четыре статуса
  (`pending`, `approved`, `rejected`, `withdrawn`), аудит решения и ссылка
  на отдельный созданный кирпичик; TTL/`expires_at`/`expired` отсутствуют;
- `brick_proposal_store.py` — снимок при отправке, списки/карточки/счётчики,
  права действующего админа места/суперадмина, approve/reject/withdraw,
  конфликты кодов и транзакционное одобрение через `BEGIN IMMEDIATE` +
  условный `UPDATE ... WHERE status='pending'`;
- `handlers/bricks.py` — «📨» в строке кирпичика и «📤 Мои предложения»:
  мастер область → место из справочника → проверка снимка → подтверждение →
  отправка; общей кнопки «📨 Предложить кирпичик» нет (U3a №8), устаревший
  `bp_start` без id получает подсказку; отзыв с отдельным подтверждением;
  stale и повторные callback не меняют решение;
- `handlers/admin.py` — в существующем кабинете: кнопка и очередь
  «🧱 Предложения кирпичиков» только текущего места, сводная очередь мест
  и глобальная очередь для суперадмина, карточки и подтверждаемые решения;
- 69 новых тестов Stage 26: 10 schema, 19 service и 40 интеграционных
  через `Application.process_update` (FakeBot, временный SQLite) — всего
  **1146** в полном наборе.

Граница 26: личные шаблоны управляются только владельцем; роль админа
места или суперадмина не открывает чужие личные шаблоны. Стандартный
шаблон доступен действующему админу этого места или суперадмину; права
перепроверяются в сервисе при сохранении/сбросе. Сброс создаёт новую
версию и не меняет историю/снимки. Предложения кирпичиков встроены в
существующие «Мои кирпичики» и кабинет; второго кабинета нет. Одобрение
создаёт отдельный кирпичик и не перебинживает старые версии/снимки
шаблонов. На момент Stage 26 шаблоны ещё не вставлялись в мастер поста;
`draft_values` и адаптивное создание оставались будущим Stage 27.

Stage 27A — фундамент адаптивного мастера анонсов:

- версия 12 (`announcement_wizard`): отдельные таблицы `announcement_drafts`,
  `announcement_values` и `announcement_extras`; старые `drafts`/
  `user_states` не меняются и не конвертируются;
- `announcement_wizard.py` — независимый снимок версии шаблона, сохранённые
  привязки `код → brick_id`, детерминированные вопросы, значения места,
  все семь типов и переиспользование валидаторов Stage 22A/17;
- `announcement_store.py` — место и альтернативное название без создания
  нового места, стандартный/личный шаблон с проверкой прав, значения по
  `brick_id`, дополнительные кирпичики только этого анонса, этапы,
  рестарт, атомарные операции и optimistic locking по `revision`;
- 43 новых теста Stage 27A: 6 schema, 13 wizard и 24 store — всего
  **1189** в полном наборе.

Граница 27A: на этом этапе Telegram-интерфейс нового мастера, фотопоток в
новой модели и публикация ещё не были реализованы; они добавлены в Stage
27B/27C. Девятишаговый мастер, `POST_STEPS`, старые черновики, WIP и
публикация не изменялись. `draft_values` из ранних планов не понадобился:
значения хранятся в `announcement_values` по постоянному `brick_id`.

Stage 27C — фотографии и публикация адаптивного мастера:

- версия 13 (`announcement_photos_publication`): `photos`, publication-поля,
  индекс и триггеры формы/JSON/неизменяемости `announcement_drafts`;
- отдельная JSON-коллекция `file_id` (не кирпичики, без `[фото]`), лимит
  100 и дедупликация; UI-экран добавления/удаления/просмотра вложений;
- `begin_announcement_publication` замораживает готовый HTML и фото до
  первой сетевой операции, `resume` использует только замороженный
  payload; значения/extras/фото после начала не меняются сервисом;
- прогресс `fingerprint/step/ids/total` сохраняется после каждой
  подтверждённой операции; `failed`/`partial`/`sent` различают полный
  отказ, частичную доставку и подтверждённый Telegram успех;
- 18 новых тестов Stage 27C: 11 сервисных/schema и 7 интеграционных —
  всего **1226** в полном наборе.

Граница 27C: отдельного согласования/очереди публикации нет — автор
подтверждает отправку в свой личный чат; используется существующий sender
`send_rendered_post`. Exactly-once для потерянного ответа Telegram не
обещается: после `partial` продолжение отправляет только недостающие
операции, а возможный дубль последней группы честно показан в UI.

Stage 28 — автоматическая выдача и замена доставленной версии:

- версия 14 (`announcement_redelivery`): схема данных не меняется, только
  триггер `trg_announcement_drafts_published_immutable` разрешает ровно
  один переход `sent -> sending` с переносом старых `ids` в `previous_ids`;
- отдельной финальной кнопки нет: последний ответ/пропуск сам собирает и
  выдаёт анонс в личный чат отдельным сообщением, а кнопки управления
  живут в служебном экране;
- правка доставленного анонса не отправляет копию на каждом шаге:
  «🔄 Применить изменения» пересобирает payload и заменяет прошлую версию;
  прошлые сообщения удаляются только после полного подтверждения новой;
  неудача удаления (лимит Telegram 48 часов) не считается сбоем доставки;
- `ids` текущей отправки и `previous_ids` заменяемой версии разделены;
  незавершённую отправку нельзя сбросить, пока есть `previous_ids` —
  иначе единственный адрес прошлой копии был бы потерян;
- выдача и временный предпросмотр вложений разрешены только в личном чате
  автора; группа/канал получают отказ без отправки и без потери данных;
- 11 новых тестов Stage 28: 7 schema/backup и 4 e2e-сценария (групповой
  чат, порядок замены версии, отказ удаления старше 48 часов, правка
  extras) — всего **1237** в полном наборе.

Граница 28: это последняя доставленная версия, а не «неизменяемая
история» — следующая пересборка перезаписывает payload. Новых колонок,
таблиц, ролей, очереди согласования, клонирования черновиков и публикации
в каналы нет.

Stage 28 — автоматическая выдача и замена доставленной версии:

- версия 14 (`announcement_redelivery`): **схема данных не меняется** —
  ни новых колонок, ни таблиц, ни индексов. Меняется ровно один триггер:
  `trg_announcement_drafts_published_immutable` разрешает единственный
  проверяемый переход `sent -> sending` для того же анонса;
- адреса доставленных сообщений и отпечаток содержимого уже живут в
  `publication_progress` (`ids`/`fingerprint`), время — в `published_at`,
  поэтому миграция 14 не добавляет `delivered_*`-колонок;
- отдельной кнопки «опубликовать» больше нет: последний ответ мастера сам
  собирает и выдаёт анонс в личный чат; правка данных/фото/extras
  применяется явной кнопкой «🔄 Применить изменения» и заменяет прошлую
  версию;
- `previous_ids` отделены от `ids` текущей отправки: прошлую копию удаляют
  только после полного подтверждения новой, а неудача удаления (лимит
  Telegram 48 часов) не делает анонс недоступным.

Граница 28: это последняя доставленная версия, а не «неизменяемая
история» — следующая пересборка перезаписывает payload. Ролей, очереди
согласования, клонирования черновиков и публикации в каналы/группы нет:
выдача и предпросмотр вложений разрешены только в личном чате автора.
Старый девятишаговый мастер, его `Draft.photos`/`Draft.image`, права,
шаблоны, парсер и исторические snapshots не изменялись.

Историческая справка: на момент завершения 19A в коде не было Telegram-UI,
поиска и альтернативных названий. В 19B они реализованы; границы ниже
описывают уже текущее состояние.

Не реализовано и остаётся будущим этапам:

- слияние дублей мест;
- `draft_values` (не понадобился), полный интерфейс управления кирпичиками
  места и Mini App;
- замена девятишагового мастера или подключение нового мастера к
  существующим обработчикам постов;
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

### Версия 8 (Stage 23) — `templates_versions_snapshots`

Добавляет шаблоны, неизменяемые версии, снимки и привязки по `brick_id`.
Ключевые объекты:

```sql
CREATE TABLE templates (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('standard','personal')),
    place_id INTEGER REFERENCES places(id) ON DELETE CASCADE,
    owner_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CHECK ((kind='standard' AND place_id IS NOT NULL
            AND owner_user_id IS NULL AND name IS NULL)
        OR (kind='personal' AND place_id IS NULL
            AND owner_user_id IS NOT NULL AND length(trim(name)) > 0))
);
CREATE UNIQUE INDEX ux_templates_one_standard_place
    ON templates(place_id) WHERE kind = 'standard';

CREATE TABLE template_versions (
    id INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    header TEXT NOT NULL,
    body TEXT NOT NULL,
    footer TEXT NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at DATETIME NOT NULL,
    builtin_version INTEGER,
    UNIQUE (template_id, version),
    CHECK (header <> '' OR body <> '' OR footer <> '')
);

CREATE TABLE template_version_bricks (
    template_version_id INTEGER NOT NULL
        REFERENCES template_versions(id) ON DELETE CASCADE,
    brick_id INTEGER NOT NULL REFERENCES bricks(id) ON DELETE RESTRICT,
    PRIMARY KEY (template_version_id, brick_id)
);

CREATE TABLE template_snapshots (...);        -- самостоятельная копия версии
CREATE TABLE template_snapshot_bricks (...);  -- копия brick_id снимка
```

- стандарт создаётся/бэкфиллится для каждого места (v1) с встроенным
  текстом и привязками к системным кирпичикам 1–9;
- `revision` — номер текущей версии и ожидаемая ревизия условного UPDATE;
  триггер запрещает её неувеличение;
- `kind/place_id/owner_user_id` неизменяемы; стандарт нельзя удалить, пока
  существует место;
- версии, снимки и их привязки защищены триггерами от UPDATE/DELETE;
- `drafts`, WIP, фото и более ранние таблицы не переписываются.

### Версия 9 (Stage 24) — `template_binding_codes`

```sql
ALTER TABLE template_version_bricks  ADD COLUMN code TEXT NOT NULL DEFAULT '';
ALTER TABLE template_snapshot_bricks ADD COLUMN code TEXT NOT NULL DEFAULT '';
-- временно снять триггеры неизменяемости привязок,
-- заполнить code из bricks.code по brick_id,
-- восстановить триггеры и создать уникальные индексы:
CREATE UNIQUE INDEX ux_template_version_bricks_version_code
    ON template_version_bricks(template_version_id, code);
CREATE UNIQUE INDEX ux_template_snapshot_bricks_snapshot_code
    ON template_snapshot_bricks(template_snapshot_id, code);
```

- `code` хранит авторский код `[имя]` и служит мостом к конкретному
  `brick_id`; рендер не разрешает код заново;
- backfill идёт по фактическим `bricks.code`, а не по разбору текста;
- уникальность `(version, code)`/`(snapshot, code)` исключает
  неоднозначное восстановление;
- миграция 9 не меняет тексты версий/снимков и не затрагивает `drafts`/WIP.

### Версия 10 (Stage 25) — `template_sessions`

```sql
CREATE TABLE template_sessions (
    telegram_user_id INTEGER NOT NULL PRIMARY KEY,
    payload TEXT NOT NULL CHECK (
        json_valid(payload) AND json_type(payload) = 'object'
    ),
    updated_at DATETIME NOT NULL
);
```

- только интерфейсное состояние Telegram-редактора шаблонов; ключ —
  постоянный Telegram user ID, без FK на `users`;
- изолировано от `user_states`, `place_sessions`, `admin_sessions` и
  `brick_sessions`;
- права, ID шаблона и revision проверяются сервисом заново; payload не
  является источником прав;
- миграция 10 не меняет сохранённые шаблоны, версии, снимки, `drafts`,
  WIP и фото.

### Версия 11 (Stage 26) — `brick_proposals`

```sql
CREATE TABLE brick_proposals (
    id                 INTEGER NOT NULL PRIMARY KEY,
    author_user_id     INTEGER NOT NULL REFERENCES users(id)
                       ON DELETE RESTRICT,
    scope              TEXT NOT NULL CHECK (scope IN ('place','system')),
    place_id           INTEGER REFERENCES places(id) ON DELETE RESTRICT,
    source_brick_id    INTEGER NOT NULL REFERENCES bricks(id)
                       ON DELETE RESTRICT,
    title              TEXT NOT NULL,
    code               TEXT NOT NULL,
    type               TEXT NOT NULL CHECK (type IN (
        'short_text','long_text','date','time','url','number','choice')),
    source             TEXT NOT NULL CHECK (source IN ('ask','place','context')),
    rules_json         TEXT,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending','approved',
                                         'rejected','withdrawn')),
    created_at         DATETIME NOT NULL,
    decided_at         DATETIME,
    decided_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    approved_brick_id  INTEGER REFERENCES bricks(id) ON DELETE RESTRICT,
    decision_note      TEXT,
    CHECK ((scope='place' AND place_id IS NOT NULL)
        OR (scope='system' AND place_id IS NULL)),
    CHECK (length(code) BETWEEN 2 AND 64
           AND code = trim(code)
           AND instr(code, ' ') = 0
           AND instr(code, '[') = 0
           AND instr(code, ']') = 0
           AND code <> 'фото'),
    CHECK (length(trim(title)) > 0),
    CHECK (rules_json IS NULL OR (json_valid(rules_json)
           AND json_type(rules_json) = 'object')),
    CHECK (
        (status='pending' AND decided_at IS NULL
            AND decided_by_user_id IS NULL
            AND approved_brick_id IS NULL AND decision_note IS NULL)
        OR (status='approved' AND decided_at IS NOT NULL
            AND decided_by_user_id IS NOT NULL
            AND approved_brick_id IS NOT NULL AND decision_note IS NULL)
        OR (status='rejected' AND decided_at IS NOT NULL
            AND decided_by_user_id IS NOT NULL
            AND approved_brick_id IS NULL)
        OR (status='withdrawn' AND decided_at IS NOT NULL
            AND decided_by_user_id IS NOT NULL
            AND approved_brick_id IS NULL AND decision_note IS NULL)
    )
);
```

Индексы: автор/статус/дата, место/статус/дата, статус/область/дата и
`source_brick_id`; частичный уникальный индекс
`approved_brick_id WHERE approved_brick_id IS NOT NULL` не позволяет двум
предложениям сослаться на один созданный кирпичик.

Триггеры:

- `trg_brick_proposals_snapshot_immutable` — нельзя изменить автора, область,
  `place_id`, `source_brick_id`, название, код, тип, источник, правила или
  `created_at`; снимок не зависит от последующей правки/архива источника;
- `trg_brick_proposals_source_owner` — INSERT принимает только действующий
  **личный** кирпичик этого же автора;
- `trg_brick_proposals_single_transition` — статус меняется ровно один раз,
  только из `pending` в `approved`/`rejected`/`withdrawn`;
- `trg_brick_proposals_decision_immutable` — после решения аудит и ссылка на
  созданный кирпичик неизменяемы;
- `trg_brick_proposals_no_delete` — история не удаляется физически;
- `trg_brick_proposals_approval_link` — `approved_brick_id` обязан указывать
  на отдельный активный кирпичик той же области/места, с теми же
  названием/кодом/типом/источником/правилами; исходный личный кирпичик
  вместо копии не принимается.

Миграция 11 также пересоздаёт `trg_bricks_reserve_codes_insert` и
`trg_bricks_reserve_codes_update` с сохранением имён:

- прямой системный/несистемный дубль кода по-прежнему запрещён;
- INSERT системного кирпичика с кодом активного личного разрешён только в
  момент одобрения ожидающего глобального предложения, когда снимок точно
  совпадает с создаваемым кирпичиком и носитель кода ровно один;
- обычная правка продвинутого личного кирпичика разрешена только при
  неизменных `code` и `scope`; смена кода на зарезервированный системный
  или создание нового дубля блокируются.

Одобрение выполняется в одной транзакции: `BEGIN IMMEDIATE` → повторная
проверка актора/прав/статуса/области/типа/кода/правил/конфликтов → создание
отдельного `Brick` → условный `UPDATE ... WHERE status='pending'` → commit.
Если `UPDATE` не затронул строку, созданный кирпичик откатывается. Поля
`expires_at` нет; `pending` бессрочен. Миграция 11 не меняет кирпичики,
шаблоны, версии, снимки, привязки, `drafts`, WIP и фото.

### Версия 12 (Stage 27A) — `announcement_wizard`

```sql
CREATE TABLE announcement_drafts (
    id                   INTEGER NOT NULL PRIMARY KEY,
    author_user_id       INTEGER NOT NULL,
    place_id             INTEGER NOT NULL,
    place_name_id        INTEGER,
    place_display_name   TEXT NOT NULL,
    place_address        TEXT,
    place_url            TEXT,
    template_id          INTEGER,
    template_version     INTEGER,
    template_snapshot_id INTEGER,
    stage                TEXT NOT NULL DEFAULT 'place',
    revision             INTEGER NOT NULL DEFAULT 1,
    idempotency_key      TEXT,
    created_at           DATETIME NOT NULL,
    updated_at           DATETIME NOT NULL,
    CONSTRAINT ck_announcement_drafts_stage CHECK (stage IN (
        'place','template','questions','extras','preview','completed')),
    CONSTRAINT ck_announcement_drafts_revision CHECK (revision >= 1),
    CONSTRAINT ck_announcement_drafts_display_name
        CHECK (length(trim(place_display_name)) > 0),
    CONSTRAINT ck_announcement_drafts_address
        CHECK (place_address IS NULL OR length(trim(place_address)) > 0),
    CONSTRAINT ck_announcement_drafts_url
        CHECK (place_url IS NULL OR length(trim(place_url)) > 0),
    CONSTRAINT ck_announcement_drafts_idempotency
        CHECK (idempotency_key IS NULL OR length(trim(idempotency_key)) > 0),
    CONSTRAINT ck_announcement_drafts_template_shape CHECK (
        (template_id IS NULL AND template_version IS NULL
            AND template_snapshot_id IS NULL)
        OR (template_id IS NOT NULL AND template_version IS NOT NULL
            AND template_version >= 1 AND template_snapshot_id IS NOT NULL)
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
);

CREATE TABLE announcement_values (
    id         INTEGER NOT NULL PRIMARY KEY,
    draft_id   INTEGER NOT NULL,
    brick_id   INTEGER NOT NULL,
    state      TEXT NOT NULL,
    value_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT ck_announcement_values_state
        CHECK (state IN ('valid','empty_optional')),
    CONSTRAINT ck_announcement_values_payload CHECK (
        (state = 'valid' AND value_json IS NOT NULL
            AND json_valid(value_json) AND json_type(value_json) = 'object')
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
);

CREATE TABLE announcement_extras (
    id         INTEGER NOT NULL PRIMARY KEY,
    draft_id   INTEGER NOT NULL,
    brick_id   INTEGER NOT NULL,
    section    TEXT NOT NULL,
    position   TEXT NOT NULL DEFAULT 'end',
    occurrence INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT ck_announcement_extras_section
        CHECK (section IN ('header','body','footer')),
    CONSTRAINT ck_announcement_extras_position
        CHECK (position IN ('start','end')),
    CONSTRAINT ck_announcement_extras_occurrence CHECK (occurrence >= 1),
    CONSTRAINT fk_announcement_extras_draft
        FOREIGN KEY (draft_id) REFERENCES announcement_drafts (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_announcement_extras_brick
        FOREIGN KEY (brick_id) REFERENCES bricks (id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_announcement_extras_draft_brick_occurrence
        UNIQUE (draft_id, brick_id, occurrence)
);
```

Индексы:

- `ix_announcement_drafts_author_updated` — выборка анонсов автора по
  времени обновления;
- `ux_announcement_drafts_snapshot` — частичный уникальный индекс: один
  `template_snapshot_id` принадлежит ровно одному анонсу, повторное
  использование живого/чужого снимка запрещено;
- `ux_announcement_drafts_idempotency` — частичный уникальный индекс
  `(author_user_id, idempotency_key)`: повтор запроса не создаёт второй
  анонс;
- `ix_announcement_values_brick`, `ix_announcement_extras_draft` и
  `ix_announcement_extras_brick` — поиск значений/дополнений по анонсу и
  кирпичику.

Триггеры:

- `trg_announcement_drafts_revision_increases` — `revision` может только
  расти. Сервис делает `UPDATE ... WHERE revision = :expected`; прямой SQL,
  уменьшающий ревизию, отклоняется SQLite;
- `trg_announcement_drafts_identity_immutable` — автор, `place_id`, ключ
  идемпотентности и `created_at` не переписываются. Подмена ID не делает
  чужой анонс своим и не меняет выбранное место;
- `trg_announcement_drafts_template_immutable` — после привязки снимка
  нельзя изменить `template_id`, `template_version` или
  `template_snapshot_id`; правка живого шаблона и повторный выбор не
  переписывают начатый анонс;
- `trg_announcement_values_identity_immutable` — `draft_id`, `brick_id` и
  `created_at` значения неизменяемы (меняются только `state`, payload и
  `updated_at`);
- `trg_announcement_extras_identity_immutable` — `draft_id`, `brick_id`,
  `occurrence` и `created_at` дополнительного кирпичика неизменяемы
  (меняются секция, позиция и `updated_at`).

Семантика Stage 27A:

- **Место.** `place_id` — постоянная ссылка на справочник;
  `place_name_id` — выбранный вариант названия (канонический или
  альтернативный), `place_display_name` — его снимок. Выбор альтернативы
  не создаёт новое место. `place_address`/`place_url` — снимок известных
  контактов; `NULL` означает «значения нет», а не пустую строку/заглушку.
  При подтверждении сервис обновляет снимок из справочника.
- **Шаблон.** До выбора все три поля `NULL`, после — заполнены вместе.
  Снимок создаётся в `template_snapshots`/`template_snapshot_bricks`
  (Stage 23) в той же транзакции и не зависит от последующих версий.
- **Значения.** Отсутствие строки в `announcement_values` — вопрос ещё не
  отвечен; `state='valid'` с JSON-объектом payload — допустимое значение;
  `state='empty_optional'` — явно пропущенное допустимое пустое значение.
  Значения связаны с постоянным `brick_id`, а не с текстовым `[код]`.
- **Дополнения.** `announcement_extras` существует только в этом анонсе:
  секция (`header`/`body`/`footer`), позиция (`start`/`end`) и
  `occurrence` (Stage 27A использует `1`). Уникальность
  `(draft_id, brick_id, occurrence)` не даёт добавить один и тот же
  кирпичик дважды в одной роли.
- **Этапы и locking.** `stage` — `place`, `template`, `questions`,
  `extras`, `preview` или `completed`; переходы проверяет сервис.
  `revision` растёт при каждом изменении, конфликт ожидаемой ревизии
  возвращает `AnnouncementConflictError` и не меняет данные. Все операции
  автора перепроверяют владельца в сервисе; чужой анонс выглядит как
  «не найден».
- **Старые данные.** Миграция 12 не читает и не меняет `drafts`,
  `user_states`, WIP, фото/альбомы, `place_sessions`, `template_sessions`,
  `brick_sessions` и `brick_proposals`. Старый формат черновиков не
  конвертируется: новый мастер Stage 27B/27C остаётся отдельным потоком.

### Версия 13 (Stage 27C) — `announcement_photos_publication`

Добавочная миграция к `announcement_drafts`; новые таблицы не создаются.

```sql
ALTER TABLE announcement_drafts ADD COLUMN photos TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_status TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_text TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_photos TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_progress TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_error TEXT;
ALTER TABLE announcement_drafts ADD COLUMN publication_started_at DATETIME;
ALTER TABLE announcement_drafts ADD COLUMN published_at DATETIME;

CREATE INDEX ix_announcement_drafts_publication_status
    ON announcement_drafts (publication_status);
```

- `photos` — JSON-список Telegram `file_id` (отдельная коллекция, не
  кирпичики); `NULL`/пусто = фото нет. `publication_photos` — замороженная
  на момент подтверждения копия, отдельная от редактируемой коллекции.
- `publication_status` — `NULL` (не публиковался), `sending`, `partial`,
  `failed` или `sent`; `publication_text` — полный готовый HTML;
  `publication_progress` — JSON-объект `fingerprint/step/ids/total`;
  `publication_started_at` заполняется при старте, `published_at` — только
  после подтверждения Telegram.
- `publication_error` — короткая причина последнего сбоя (не секрет и не
  payload).
- CHECK в модели дополнительно требуют JSON-массив для `photos`/
  `publication_photos` и JSON-объект для `publication_progress`; на
  мигрированной базе те же типы проверяют триггеры.

Триггеры версии 13:

- `trg_announcement_drafts_media_json_insert` /
  `..._media_json_update` — валидный JSON, `array` для фото и `object` для
  прогресса;
- `trg_announcement_drafts_publication_status_insert` /
  `..._update` — только четыре известных статуса;
- `trg_announcement_drafts_publication_shape_insert` /
  `trg_announcement_drafts_publication_shape` — статус всегда вместе с
  непустым текстом, списком фото и временем старта; `sent` требует
  `published_at`, остальные статусы его запрещают; сброс в `NULL` требует
  полностью пустых publication-полей;
- `trg_announcement_drafts_published_immutable` — после `sent` нельзя
  изменить статус, замороженный текст/фото, прогресс, время или причину.
  В версии 14 этот триггер заменяется (см. следующий раздел): править
  замороженные поля по-прежнему нельзя, но появляется ровно один
  разрешённый переход `sent -> sending`.

Семантика Stage 27C:

- **Фото.** Коллекция необязательна, не влияет на готовность текста, не
  входит в `announcement_values`/`extras` и не участвует в шаблонных
  вопросах. Добавление идемпотентно по `file_id`; удаление/очистка меняют
  `revision`. После `publication_status IS NOT NULL` сервисные изменения
  коллекции запрещены.
- **Публикация.** До первой сетевой операции сервис собирает preview и
  фиксирует `sending` + текст/фото. `resume` не пересобирает payload даже
  при изменении исходного шаблона. `mark_failed` ставит `partial` при
  `step > 0`, иначе `failed`; `mark_sent`, в свою очередь, ставит `sent`,
  `published_at` и `stage=completed` только после успешного ответа
  Telegram.
- **Сброс.** Разрешён только для `failed`, `step=0`, пустых `ids`; очищает
  все publication-поля, но не текст/значения/фото/extras. После `partial`
  доступно только продолжение.
- **Старые данные.** Миграция 13 не меняет `drafts`, WIP,
  `Draft.image`/`Draft.photos`, `announcement_values` и
  `announcement_extras`; у существующих анонсов новые поля остаются
  `NULL`, пока публикация не начата.

### Версия 14 (Stage 28) — `announcement_redelivery`

Миграция **не меняет схему данных**: ни новых колонок, ни таблиц, ни
индексов, ни CHECK-ов. Единственное изменение — замена одного триггера:

```sql
DROP TRIGGER trg_announcement_drafts_published_immutable;

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
END;
```

- **Зачем.** Адаптивный мастер Stage 28 выдаёт готовый анонс автоматически
  и позволяет править доставленную версию: новая сборка должна заменить
  прошлую. Без замены триггера переход `sent -> sending` был невозможен на
  уровне схемы, и правка после доставки упиралась бы в ошибку БД.
- **Что разрешено ровно.** Только `sent -> sending` для того же
  `announcement_drafts.id`, только с очисткой `published_at`, свежим
  `publication_started_at`, непустыми текстом/фото/прогрессом, пустой
  ошибкой и **переносом адресов прошлой версии** в `previous_ids`.
  Сравнение `IS` с JSON-значением `$.ids` старого прогресса не зависит от
  пробелов и порядка ключей. Payload при этом перезаписывается — это
  последняя доставленная версия, а не «неизменяемая история».
- **Что запрещено по-прежнему.** Правка замороженного текста/фото/прогресса
  у `sent` без перехода; `sent -> failed`/`partial`/`sent`; `previous_ids`,
  не совпадающие с прошлыми `ids` (иначе адреса прошлой версии потерялись
  бы или были бы выданы за адреса новой); сброс `published_at` в `NULL`
  без перехода; любой `UPDATE`, не проходящий shape-триггеры версии 13.
- **Почему отдельные колонки не нужны.** `ids` текущей доставки и
  `previous_ids` заменяемой версии живут в `publication_progress`, время
  доставки — в `published_at`, отпечаток содержимого — в
  `publication_progress.fingerprint`. Отдельные `delivered_*`/`history`
  колонки не добавлялись: они дублировали бы уже проверяемые инварианты.
- **Старые данные.** Миграция 14 не читает и не меняет `drafts`,
  `user_states`, WIP, фото, шаблоны и `announcement_*`; меняется только
  определение триггера. Атомарность обеспечивает общая транзакция
  `run_migrations`: при сбое любого шага `DROP`/`CREATE` откатываются
  вместе, и остаётся триггер версии 13.

### Версия 15 (Poster 2.0 S1) — `brick_description_and_address`

Добавочная миграция: одна новая колонка и один новый системный кирпичик.
Существующие таблицы, строки, индексы и триггеры не перестраиваются.

```sql
ALTER TABLE bricks ADD COLUMN description TEXT;

-- Триггер резервирования снимается только на время вставки и сразу
-- восстанавливается дословным определением миграции 11.
DROP TRIGGER IF EXISTS trg_bricks_reserve_codes_insert;

INSERT INTO bricks
    (id, code, title, type, scope, source, rules_json, status,
     created_by_user_id, created_at, updated_at, description)
SELECT
    COALESCE(MAX(id), 0) + 1, 'адрес', 'Адрес', 'short_text',
    'system', 'place', NULL, 'active', NULL, CURRENT_TIMESTAMP,
    NULL, NULL
FROM bricks;

CREATE TRIGGER trg_bricks_reserve_codes_insert
BEFORE INSERT ON bricks
FOR EACH ROW
WHEN EXISTS ( ... системный и несистемный носитель одного кода ... )
AND NOT ( ... исключение Stage 26 для одобренного предложения ... )
BEGIN
    SELECT RAISE(ABORT, 'system brick code is reserved');
END;
```

- **Зачем.** Poster 2.0 использует пользовательский код `[адрес]`
  (отдельная строка адреса в эталоне §6.1.1) и хочет показывать пояснение
  автора кирпичика в нижнем экране мастера. `description` — необязательный
  `TEXT`: `NULL` означает «пояснения нет», пустая строка не подставляется.
  Текст не является частью шаблона, не компилируется в анонс и выводится в
  интерфейс только после HTML-экранирования (`utils.tg_html.escape_html`).
- **Почему не `id=10`.** Миграция 6 создавала таблицу без резерва ID, поэтому
  в уже работающей базе пользовательские и местные кирпичики могли занять
  номер 10 и далее; жёсткий `id=10` дал бы `UNIQUE constraint failed`.
  Вставка берёт `MAX(id)+1`, сохраняя все прежние ID. Системность задаётся
  `scope='system'` и неизменяемостью кода (триггеры миграции 6), а не
  номером: каталог `SYSTEM_BRICKS` сверяется с БД по коду, а реальный
  `brick_id` для рендера берётся из снимка шаблона.
- **Старый код сохраняется.** `адрес_места` (id 6) не переименовывается и не
  удаляется: он остаётся в уже сохранённых версиях, снимках и шаблонах.
  Оба кирпичика получают значение из `places.address`, поэтому старые
  шаблоны продолжают рендериться без правок.
- **Совместимость.** Колонка добавочная (`ALTER TABLE ... ADD COLUMN`), у
  существующих строк остаётся `NULL`; триггеры `bricks` не затрагиваются.
  Повторный запуск миграции невозможен (журнал версий), а повторный
  `INSERT` кода `адрес` защищён уникальностью системного кода.
- **Коллизия кода `адрес` (дефект, найденный тестировщиком S1).** До Poster
  2.0 кода `адрес` не было в `SYSTEM_BRICK_CODES`, поэтому в базе версии 14
  мог уже существовать личный или местный кирпичик с этим кодом — активный
  или архивный. Триггер миграции 6 `trg_bricks_reserve_codes_insert`
  запрещает сосуществование системного и несистемного носителя одного кода,
  и обычный `INSERT` системного `адрес` падал с `system brick code is
  reserved`, блокируя обновление приложения. Решение: миграция снимает этот
  триггер на время единственной вставки и восстанавливает его дословно тем
  же определением, что действовало после миграции 11 (включая исключение
  Stage 26 для одобрения предложения). Так как DDL и DML идут одной
  транзакцией `run_migrations`, окна без триггера для других писателей нет.
- **Легаси-кирпичик не переписывается.** Его `id`, код, название, тип,
  область, статус и владелец остаются прежними; строка не «повышается» до
  системной и не архивируется. Сохранённые версии и снимки ссылаются на
  `brick_id`, а не на код, поэтому старые шаблоны рендерятся как раньше.
  Сосуществование двух носителей одного кода уже допущено проектом в Stage
  26 (повышенный личный кирпичик сохраняет свой код рядом с системным), и
  разбор кода сохраняет тот же приоритет: `resolve_brick_code_*` отдаёт
  system, поэтому новые `[адрес]` указывают на системный кирпичик, а
  `check_code_availability` считает код занятым для новых определений.
  Владелец легаси-кирпичика по-прежнему может менять название и настройки,
  не меняя код: исключение «собственный неизменный код» в
  `check_code_availability` действует и при `system_reserved` (например,
  `адрес`), но только для существующей строки того же владельца/места,
  переданной через `exclude_brick_id`. Новый кирпичик с кодом `адрес` или
  смена кода на `адрес` отклоняются.
- **Инвариант не ослаблен.** Восстановленный триггер идентичен
  действовавшему: новый системный код не может быть создан поверх
  несистемного ни одним будущим `INSERT`, а смена кода/области на системный
  по-прежнему запрещена `trg_bricks_reserve_codes_update`.
- **Предостережение.** Добавлять новые системные кирпичики можно только
  новой миграцией и только через `MAX(id)+1`; жёсткие ID в DDL и в
  прикладном коде запрещены. Временное снятие триггера допустимо только для
  добавления системного кода, который раньше не был системным, и только с
  немедленным восстановлением того же определения. Бэкап перед DDL делает
  `run_migrations`.

### Версия 16 (Poster 2.0 S1) — `announcement_draft_origin`

Перестройка `announcement_drafts`: личный шаблон может не содержать
кирпичика «Место», поэтому `place_id` и снимок названия становятся
необязательными. SQLite не умеет снимать `NOT NULL`/`CHECK` через
`ALTER TABLE`, поэтому таблица пересоздаётся.

Новая схема (существенные изменения против версии 12/13):

```sql
place_id             INTEGER NULL;      -- было NOT NULL
place_display_name   TEXT NULL;         -- было NOT NULL
origin               TEXT NOT NULL DEFAULT 'place';
live_message_ids_json TEXT NULL;        -- упорядоченные ID живого блока

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
CONSTRAINT ck_announcement_drafts_live_ids
    CHECK (
        live_message_ids_json IS NULL
        OR (json_valid(live_message_ids_json)
            AND json_type(live_message_ids_json) = 'array')
    );
```

- **Зачем.** `origin` описывает источник **шаблона**, а не место, выбранное
  позже в кирпичике «Место»: анонс по личному шаблону не привязан к
  площадке (`place_id IS NULL`), но кирпичик `[место]` в нём допустим и
  может содержать разовое имя. `live_message_ids_json` хранит адреса
  **всех** сообщений верхнего живого блока: альбом — это несколько
  сообщений, одного `live_message_id` недостаточно. Поле не является
  историей версий: как и `publication_progress`, оно описывает текущий блок.
- **Порядок шагов (одна транзакция).** Копии `announcement_drafts`,
  `announcement_values`, `announcement_extras` → `DROP TABLE
  announcement_drafts` (каскад удаляет детей, их строки уже в копиях) →
  новая схема → индексы и триггеры (до вставки, чтобы вставка проверила
  данные) → перенос черновиков с `origin='place'` → перенос значений и
  дополнительных кирпичиков → удаление копий. Сбой любого шага откатывает
  всю миграцию: `run_migrations` применяет ожидающие версии одной
  транзакцией.
- **Триггеры.** Пересоздаются определения версий 12–14 плюс
  `trg_announcement_drafts_identity_immutable` теперь защищает и `origin`:
  `personal`-черновик нельзя выдать за анонс площадки подменой одной
  колонки. Триггеры детей (`trg_announcement_values_identity_immutable`,
  `trg_announcement_extras_identity_immutable`) принадлежат своим таблицам,
  поэтому `DROP` родителя их не снимает; миграция снимает их явно и
  создаёт заново, чтобы имена не конфликтовали.
- **Снимки и связи.** `template_id`/`template_version`/`template_snapshot_id`,
  уникальный `ux_announcement_drafts_snapshot`, `announcement_values`,
  `announcement_extras`, FK на `users`/`places`/`place_names`/`templates`/
  `template_snapshots` и `publication_progress` переносятся без изменений.
  Прежние строки получают `origin='place'` (они всегда создавались с
  местом), `live_message_ids_json` — `NULL`. Снимки шаблонов неизменяемы
  (`trg_template_snapshots_no_update`), миграция их не трогает.
- **Совместимость и предостережения.** `PRAGMA foreign_keys=OFF` внутри
  транзакции — no-op, поэтому перестройка идёт через копии: удалять копии
  до успешной вставки нельзя, иначе данные потеряются при откате. Пустой
  `live_message_ids_json`/`NULL` **не** означает «доставить заново»:
  повторная отправка управляется `publication_status`/`publication_progress`,
  а `sent` без `ids` по-прежнему запрещает правку и пересборку. Проверки
  целостности после миграции: `PRAGMA foreign_key_check`,
  `PRAGMA integrity_check` и совпадение количества строк/связей.
  Миграция 16 проверена на временных SQLite с заполненными связанными
  таблицами и идемпотентна при повторном запуске.



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
- **Граница Stage 22B (историческая):** личный Telegram-конструктор,
  карточка, правка, мягкий архив/восстановление и пагинация добавлены.
  Тогда `draft_values`, шаблоны, парсер `[код]`, `brick_proposals`,
  интерфейс кирпичиков места и адаптивное создание ещё не были добавлены;
  сейчас шаблоны/парсер реализованы в Stage 23–25, предложения кирпичиков —
  в Stage 26, новый мастер — в Stage 27A (`announcement_values`; `draft_values`
  не создавался).
  Существующие `drafts`/WIP/`user_states` и фото/альбомы не мигрируются.
- **Архив и код.** Физического удаления нет; архивный код остаётся
  занятым, в том числе для владельца. Восстановление возвращает тот же
  `brick_id` и код. Тип после создания неизменяем. Все операции проверяют
  владельца в сервисе; суперадмин и админ места не получают доступа к
  чужим личным кирпичикам.

### 4.8. `announcement_drafts`, `announcement_values`, `announcement_extras`

Созданы миграцией 12 (Stage 27A), фото/публикационные колонки добавлены
миграцией 13 (Stage 27C), триггер замены доставленной версии обновлён
миграцией 14 (Stage 28); полная схема, индексы и триггеры — в разделе
«Версия 12»–«Версия 14» выше. Это отдельная модель незавершённого анонса,
не связанная с легаси `drafts`/`user_states`.

- `announcement_drafts` хранит автора (`users.id`), постоянный `place_id`,
  выбранный вариант названия и снимок известных данных места, выбранный
  шаблон/версию/снимок, этап, `revision`, ключ идемпотентности, отдельную
  JSON-коллекцию `photos` и замороженный publication payload.
- `announcement_values` хранит нормализованный payload конкретного
  `brick_id`: отсутствие строки — «не задано», `empty_optional` — явно
  допустимое пустое значение. Определение кирпичика остаётся в `bricks`.
- `announcement_extras` хранит дополнительные кирпичики только этого
  анонса с секцией, позицией и `occurrence`. Это не глобальная привязка
  шаблона и не влияет на другие черновики.
- Фотографии являются отдельной коллекцией вложений, не кирпичиками и не
  частью шаблонного текста. При публикации они замораживаются в
  `publication_photos`; `publication_progress` хранит подтверждённые
  message_id/step/total для продолжения.
- Прямой доступ к чужому анонсу по ID невозможен через сервис; SQL-триггеры
  дополнительно защищают идентичность, снимок шаблона, форму публикации и
  неизменяемость уже отправленного payload. Единственное исключение
  версии 14 — явная пересборка `sent -> sending` с переносом адресов
  прошлой копии в `previous_ids`.

## 5. Порядок инициализации

`database.init_db()` вызывается при старте бота и выполняет:

1. `run_migrations(engine)` — при необходимости копия базы, затем журнал и
   недостающие версии 1–14;
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

База Stage 22B (журнал 1–7) при обновлении до Stage 23:

- получает миграцию 8: таблицы шаблонов, версий, снимков и привязок;
- для каждого существующего места создаётся ровно один стандартный шаблон
  v1 с встроенным текстом и привязками к системным кирпичикам 1–9;
- сохраняет кирпичики, `drafts`, WIP, фото/альбомы, места, роли, правки и
  передачи без изменений; `draft_values` не появляются;
- перед DDL автоматически копируется состояние 7.

База Stage 23 (журнал 1–8) при обновлении до Stage 24:

- получает миграцию 9: колонки `code` в двух таблицах привязок, backfill по
  фактическим `bricks.code`, восстановление триггеров и уникальные индексы;
- тексты версий/снимков и все прежние данные не меняются;
- повторный запуск видит журнал 1–9 и ничего не применяет.

База Stage 24 (журнал 1–9) при обновлении до Stage 25:

- получает только миграцию 10: пустую `template_sessions`;
- сохранённые личные и стандартные шаблоны, версии, снимки, привязки,
  `drafts`, WIP, фото, места, роли и передачи не меняются;
- перед DDL автоматически копируется состояние 9; незавершённого
  редактора шаблонов в старой базе нет, поэтому переносить нечего.

База Stage 25 (журнал 1–10) при обновлении до Stage 26:

- получает только миграцию 11: пустую `brick_proposals`, четыре индекса,
  частичный уникальный индекс созданного кирпичика, шесть триггеров
  предложений и пересозданные триггеры резервации кодов;
- сохраняет личные/местные/системные кирпичики, личные и стандартные
  шаблоны, версии, снимки, привязки, `drafts`, WIP, фото, места, роли,
  правки и передачи без изменений; у существующих кирпичиков код, область
  и ревизия не меняются;
- данных предложений в старой базе нет, поэтому переносить нечего;
- перед DDL автоматически копируется состояние 10; повторный запуск видит
  журнал 1–11 и ничего не применяет.

База Stage 26 (журнал 1–11) при обновлении до Stage 27A:

- получает только миграцию 12: три пустые таблицы
  `announcement_drafts`/`announcement_values`/`announcement_extras`, их
  индексы и пять триггеров;
- сохраняет кирпичики, шаблоны, версии, снимки, привязки, предложения,
  `drafts`, WIP, фото, места, роли, правки и передачи без изменений;
- старые черновики и WIP не конвертируются и не получают строк в новых
  таблицах: Stage 27A не меняет формат легаси-черновика;
- перед DDL автоматически копируется состояние 11; повторный запуск видит
  журнал 1–12 и ничего не применяет.

База Stage 27A/27B (журнал 1–12) при обновлении до Stage 27C:

- получает только миграцию 13: восемь добавляемых колонок
  (`photos`, `publication_*`, `published_at`), индекс по статусу и семь
  триггеров формы/JSON/неизменяемости; новые таблицы не создаются;
- сохраняет все анонсы, значения, extras, snapshots, кирпичики, шаблоны,
  легаси `drafts`, WIP, фото и роли без изменений;
- у существующих анонсов новые поля остаются `NULL`; старые фото
  легаси-мастера не переносятся;
- перед DDL автоматически копируется состояние 12; повторный запуск видит
  журнал 1–13 и ничего не применяет.

База Stage 27C (журнал 1–13) при обновлении до Stage 28:

- получает только миграцию 14: `DROP TRIGGER` + `CREATE TRIGGER` для
  `trg_announcement_drafts_published_immutable`; новых колонок, таблиц и
  индексов нет;
- сохраняет все анонсы, значения, extras, фотографии, publication payload,
  снимки, кирпичики, шаблоны, легаси `drafts`, WIP и роли без изменений;
- у уже доставленных анонсов (`sent`) прошлые `ids` и `published_at`
  остаются на месте; переход `sent -> sending` станет доступен только
  после явной пересборки;
- перед DDL автоматически копируется состояние 13 (копия видит
  зафиксированные данные из WAL); при сбое любого шага `DROP`/`CREATE`
  откатываются вместе, и остаётся триггер версии 13; повторный запуск
  видит журнал 1–14 и ничего не применяет.

`init_db` затем:

- `create_all` не трогает существующие таблицы;
- легаси-таблица `drafts` переименована миграцией 17 в
  `legacy_drafts_backup` (см. ниже); `user_states` и WIP-снимок экрана не
  меняются — это живая инфраструктура интерфейса.

Проверено автоматически на временной базе, воспроизводящей старую схему,
с фиктивными черновиком и WIP: значения полей и WIP-снимок остаются
прежними, а строки прежнего черновика читаются из `legacy_drafts_backup`.

### Версия 17 (Poster 2.0 S5) — `archive_legacy_drafts`

Девятишаговый мастер удалён из кода вместе с моделью `Draft`: таблица
`drafts` больше не используется ни одним живым сценарием. Безвозвратного
`DROP` нет — данные прежних черновиков могут понадобиться владельцу:

```sql
ALTER TABLE drafts RENAME TO legacy_drafts_backup;
```

- **Почему переименование, а не DROP.** Данные не теряются и остаются в
  том же файле базы; схема при этом не копится «вечным» легаси-слоем:
  приложение больше не читает и не пишет эту таблицу, `create_all` её не
  создаёт (модели `Draft` нет), поэтому после миграции в рабочей схеме
  остаётся только архив.
- **Идемпотентность.** У миграции `requires_tables=('drafts',)`: если
  таблицы нет (новая база или уже мигрированная), шаги пропускаются, а
  версия фиксируется в журнале. Повторный `run_migrations` ничего не
  делает, `init_db` не падает.
- **Что не трогается.** `announcement_drafts`, `announcement_values`,
  `announcement_extras`, `users`, `place_admins`, `places`, `place_names`,
  `templates*`, `bricks*` и `user_states` остаются как есть. `user_states`
  — живая инфраструктура постоянного экрана и снимков состояния
  (`utils/screen.py`), а не легаси-WIP.
- **Проверка перед удалением архива.** Удалять `legacy_drafts_backup`
  вручную можно только после резервной копии базы; миграции этот шаг не
  делают.
- **Совместимость.** На базе без `drafts` (свежая установка) миграция —
  no-op с записью версии. На старой базе строки прежних черновиков
  остаются читаемыми (SQL и колонки сохраняются при `RENAME`).

### Версия 18 (Poster 2.1 R2) — `builtin_templates_v3`

Встроенный стандартный шаблон приведён к эталону владельца (Часть III):
заголовок жирным, пустая строка, жирная строка «дата // время», пустая
строка, жирная кликабельная площадка, нежирный адрес строкой ниже, пустая
строка, описание, пустая строка, контакт. Схема не меняется: миграция
создаёт **новую версию** шаблона, а старую оставляет в истории.

Что обновляется (кандидаты выбираются в один зафиксированный список
`migration_18_targets`):

- стандартный шаблон места, если его текущая версия побайтово равна
  локальному эталону v2 и имеет `builtin_version = 2`;
- личная стартовая копия «Мой шаблон», если её текущая версия побайтово
  равна тому же эталону v2 (маркер `builtin_version` у неё может быть
  `NULL` — так выглядели копии, выданные до появления версии).

Что **не** обновляется:

- копия, изменённая человеком: любое расхождение с эталоном v2 (в шапке,
  теле или подвале) исключает её из списка;
- копия, переименованная пользователем: личный шаблон с другим именем не
  считается стартовой копией, даже если содержимое совпадает;
- копия, уже имеющая `builtin_version = 3`, и любая копия, отличная от
  текущей версии (`revision`).

Шаги одной транзакции:

1. `CREATE TABLE migration_18_targets` — очередь кандидатов
   (`template_id`, `new_version = revision + 1`, флаг переименования);
2. `INSERT INTO template_versions` — текст v3 и `builtin_version = 3`;
   прежняя версия не `UPDATE`-ится и остаётся читаемой;
3. `INSERT INTO template_version_bricks` — те же восемь системных кодов
   (`название`, `дата`, `время_начала`, `место`, `ссылка_места`, `адрес`,
   `описание`, `контакт`), причём `brick_id` резолвится **по коду** из
   `bricks`, а не хардкодом номера: миграция 15 создала `[адрес]` с
   `MAX(id)+1`, поэтому жёсткие ID разошлись бы с реальной базой. Если
   хотя бы один из восьми активных системных кодов отсутствует, шаблон не
   обновляется вовсе — версия с неполными привязками сломала бы рендер;
4. `UPDATE templates` — `revision` указывает на новую версию; в этой же
   транзакции стартовая личная копия переименовывается из «Мой шаблон» в
   «Дефолтный шаблон». Переименования без обновления содержимого не
   бывает: оба действия связаны одной транзакцией;
5. `DROP TABLE migration_18_targets`.

- **Идемпотентность.** `requires_tables=('templates', 'template_versions',
  'template_version_bricks', 'bricks')`: на базе без этих таблиц шаги
  пропускаются, версия фиксируется в журнале. Повторный запуск невозможен
  (журнал версий), а после применения выборка кандидатов пуста — текущие
  версии уже `builtin_version = 3`. Временная таблица-очередь удаляется
  последним шагом, поэтому незавершённая миграция откатывается целиком.
- **Что не трогается.** `users`, `places`, `place_names`, `bricks`,
  `template_snapshots`, `template_bindings`, `announcement_*`, легаси
  `drafts`/`legacy_drafts_backup` и `user_states` остаются как есть.
  История версий и снимки не переписываются: старые версии остаются
  доступными для чтения.
- **Тексты миграции локальны.** Эталоны v2/v3 заданы константами внутри
  `migrations.py`, а не импортом из `template_types`: применённая миграция
  не должна зависеть от будущих правок текущих констант.
- **Совместимость.** На уже мигрированной базе (версия 18) миграция —
  no-op с записью версии. На базе версии 17 нетронутые копии получают
  новую версию, тронутые и переименованные остаются как есть. Новый
  пользователь получает стартовый личный шаблон «Дефолтный шаблон»
  сразу с текстом v3; «Восстановить стандарт» создаёт версию с
  `builtin_version = 3`.

### Версия 19 (Poster 2.1 R3) — `brick_code_conflicts`

Глобальный (системный) кирпичик выигрывает для НОВЫХ привязок, а местные
носители того же кода не переписываются молча: по каждому месту-носителю
создаётся задача разрешения конфликта и идемпотентное уведомление его
администратору. Миграция **только добавляет** схему: существующие таблицы
(`bricks`, `brick_proposals`, `templates*`, `places`, `users`) не
перестраиваются и не переписываются.

Что создаётся:

- `brick_conflicts` — задача «системный кирпичик + местный носитель»
  (`code`, `place_id`, `system_brick_id`, `local_brick_id`, `proposal_id`,
  `status`, `created_at`, `resolved_at`, `resolved_by_user_id`,
  `resolution_note`). CHECK-форма связывает статус с полями решения:
  `open` без решения; `renamed`/`switched` с `resolved_at` и
  `resolved_by_user_id`; `superseded` только с `resolved_at`
  (автоматическое закрытие, актора нет). Обе ссылки на `bricks` —
  `ON DELETE RESTRICT`: история решений не должна рассыпаться при
  архивации кирпичика; `place_id` — `ON DELETE CASCADE` вслед за местом.
- Уникальный индекс `ux_brick_conflicts_system_local`
  (`system_brick_id`, `local_brick_id`) — идемпотентность: повторное
  одобрение/повторный прогон не создаёт вторую задачу. Рабочие индексы
  `ix_brick_conflicts_place_status` и `ix_brick_conflicts_local_brick`.
- `brick_conflict_notifications` — очередь уведомлений админам мест
  (`conflict_id`, `telegram_user_id`, `status`, `attempts`, `created_at`,
  `sent_at`) с уникальным индексом
  `ux_brick_conflict_notifications_target` (`conflict_id`,
  `telegram_user_id`) и индексом `ix_brick_conflict_notifications_status`.
  Получатель хранится как Telegram user ID и может отсутствовать в
  `users` (суперадмин из конфига).
- Триггеры истории: `trg_brick_conflicts_single_transition` (решение
  терминально), `trg_brick_conflicts_decision_immutable` (поля решения
  после закрытия не переписываются), `trg_brick_conflicts_snapshot_immutable`
  (код, место, носители, предложение и дата создания неизменяемы),
  `trg_brick_conflicts_no_delete` (удаление истории запрещено),
  `trg_brick_conflict_notifications_single_transition` (`pending` →
  `sent`/`failed` один раз, обратного перехода нет).
- Триггер `trg_bricks_reserve_codes_insert` пересоздаётся в той же
  транзакции: из исключения для одобрения глобального предложения
  убирается требование «ровно один несистемный носитель». Прямой INSERT
  системного кирпичика поверх местного по-прежнему блокируется —
  ослаблена только легальная ветка одобрения через `brick_proposals`.

- **Идемпотентность.** `requires_tables=('bricks', 'places', 'users',
  'brick_proposals')`. Версия фиксируется в журнале; повторный запуск
  невозможен, а после применения повторный `create_code_conflicts`
  упирается в уникальные индексы и возвращает пустой список.
- **Что не трогается.** Существующие `bricks` (в том числе местные
  носители конфликтных кодов), версии и снимки шаблонов, предложения и
  `place_admins` остаются как есть: миграция ничего не переименовывает и
  не переносит привязки. Исходы (переименование местного кирпичика или
  новая версия стандартного шаблона места с привязкой на системный)
  применяются только по решению администратора места и в отдельных
  транзакциях.
- **Совместимость.** На базе версии 18 миграция добавляет только две
  новые таблицы, индексы и триггеры; `PRAGMA integrity_check` и
  `foreign_key_check` остаются `ok`/пустыми, данные не меняются.

### Версия 20 (Poster 2.1 R4) — `brick_proposal_description`

Карточка предложения кирпичика показывает все семь полей (№29): название,
технический код, тип, описание, автор, цель и статус. Описания в снимке
предложения не было — оно фиксировалось только в `bricks.description` и
терялось при отправке.

- `ALTER TABLE brick_proposals ADD COLUMN description TEXT` — одна
  добавочная колонка, таблица **не перестраивается**. Значение `NULL`
  означает «описания нет»; у pending-предложений, отправленных до
  миграции, колонка остаётся `NULL` и карточка показывает «—». Данные
  не переписываются, backfill не нужен.
- Триггер `trg_brick_proposals_snapshot_immutable` пересоздаётся в той же
  транзакции: он перечисляет колонки снимка явно, и без `description`
  новое поле можно было бы переписать после отправки. Прежние условия
  для остальных колонок сохранены дословно.
- `requires_tables=('bricks', 'brick_proposals')`: на базе без предложений
  версия всё равно фиксируется в журнале.
- **Совместимость.** На базе версии 19 миграция добавляет ровно одну
  колонку; индексы, ограничения и остальные триггеры не меняются,
  `PRAGMA integrity_check`/`foreign_key_check` остаются `ok`/пустыми.
- **Что не трогается.** `bricks`, существующие предложения (кроме нового
  `NULL`-поля), решения, шаблоны и `brick_conflicts` остаются как есть.
  Одобрение нового предложения переносит описание в созданный кирпичик.

### Версия 21 (Poster 2.0 U1a) — `place_city`

В карточке места появился город: он заполняется при создании места и
показывается в карточке, списке мест и результатах поиска.

- `ALTER TABLE places ADD COLUMN city TEXT` — одна добавочная колонка,
  таблица **не перестраивается**. `NULL` означает «город не указан»;
  прежние места остаются без города, проверить/заполнить его можно
  редактированием места.
- `ALTER TABLE announcement_drafts ADD COLUMN place_city TEXT CONSTRAINT
  ck_announcement_drafts_city CHECK (place_city IS NULL OR
  length(trim(place_city)) > 0)` — снимок города на момент выбора шаблона.
  Ограничение не даёт сохранить пустую строку вместо города; `NULL`
  разрешён, чтобы старые черновики не требовали backfill.
- Системный кирпичик `[город]` (`code='город'`, тип `short_text`,
  `scope='system'`, `source='place'`) добавляется в `bricks` той же
  транзакцией. `id` — `MAX(id)+1`, как и в прежних миграциях системных
  кирпичиков. Триггер `trg_bricks_reserve_codes_insert` пересоздаётся:
  встроенные коды остаются зарезервированными для пользовательских
  кирпичиков.
- `requires_tables=('places', 'announcement_drafts', 'bricks')`: без этих
  таблиц версия не применяется и в журнал не попадает.
- **Совместимость.** На базе версии 20 миграция добавляет по одной колонке
  в `places` и `announcement_drafts` и одну строку в `bricks`; индексы,
  ограничения и остальные триггеры не меняются, `PRAGMA integrity_check`
  и `foreign_key_check` остаются `ok` и пустыми. Заполнение города не
  переписывает прежние строки.
- **Что не трогается.** Названия, адреса, ссылки, администраторы,
  шаблоны и черновики остаются как есть (кроме нового `NULL`-поля
  `place_city`).

### Версия 22 (Poster 2.0 U1a) — `builtin_templates_v4`

Живой анонс показал, что эталон v3 разрывает дату/время и площадку пустой
строкой, а тело содержит лишние блоки. Версия 4:

- шапка — жирный `[название]`, пустая строка, жирное
  `[дата] // [время_начала]`, сразу под ним (без пустой строки) жирная
  площадка-ссылка `[место]([ссылка_места])` и нежирный `[адрес]`;
- тело — только `[описание]`;
- подвал — только `[контакт]`.

Миграция обновляет **только доказуемо нетронутые** копии встроенного
стандарта:

- `CREATE TABLE migration_22_targets` — зафиксированный на всю миграцию
  список кандидатов «`template_id` → `revision + 1`». Он же защищает от
  гонки: решения принимаются один раз, а не пересчитываются на каждом
  шаге.
- Кандидат — шаблон, текущая версия которого (`version = revision`)
  дословно совпадает с текстом v3, помечена `builtin_version IN (1, 2, 3)`
  либо является личной копией с именем `Дефолтный шаблон`, и все восемь
  системных кодов шаблона активны.
- `INSERT INTO template_versions (... builtin_version=4)` — новая версия
  добавляется, v3 остаётся в истории. Версия-предок не удаляется и не
  переписывается.
- `INSERT INTO template_version_bricks` — те же восемь системных кодов;
  `brick_id` берётся из `bricks` по коду, а не хардкодится.
- `UPDATE templates SET revision = new_version` — указатель текущей версии
  меняется в той же транзакции; `DROP TABLE migration_22_targets`.
- `requires_tables=('templates', 'template_versions',
  'template_version_bricks', 'bricks')`.
- **Совместимость.** На базе версии 21 миграция либо ничего не меняет
  (нет нетронутых копий), либо добавляет по одной версии в затронутые
  шаблоны. Изменённые человеком шаблоны не трогаются; снимки анонсов
  (`template_snapshots`) не переписываются.

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

## 11. Тесты Stage 19A–28

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

Файлы Stage 23:

- `tests/test_stage23_template_schema.py` — 11 тестов миграции 8: реальные
  CHECK/FK/UNIQUE, частичный индекс стандарта, триггеры неизменяемости
  версий/снимков/привязок и роста `revision`, бэкфилл v1, идемпотентный
  рестарт;
- `tests/test_stage23_templates_service.py` — 20 тестов сервиса: стандарты
  мест, личные шаблоны, права владельца/админа/суперадмина, версии,
  оптимистичная блокировка и запрет чужих правок;
- `tests/test_stage23_templates_snapshots.py` — 8 тестов снимков:
  самостоятельная копия секций/привязок, неизменяемость, идемпотентность,
  права и сохранность после правки исходного шаблона;
- `tests/test_stage23_templates_compatibility.py` — 10 тестов совместимости
  с базой Stage 22B: миграция 8, бэкфилл существующих мест, сохранность
  `drafts`/WIP/фото/кирпичиков/ролей и отсутствие `draft_values`.

Файлы Stage 24:

- `tests/test_template_parser.py` — 30 тестов лексера/парсера: `[код]`,
  экранирование, Unicode/переводы строк, Telegram-разметка, гиперссылки и
  диагностика ошибок;
- `tests/test_template_compiler.py` — 27 тестов компилятора: привязка к
  `brick_id`, сохранение кодов, рендер версии/снимка, безопасная подстановка
  и запрет повторного разрешения кода;
- `tests/test_stage24_integration.py` — 10 интеграционных сценариев
  сохранения версий, неоднозначных кодов, снимков и рендера.

Файлы Stage 25:

- `tests/test_stage25_templates_flow.py` — 23 интеграционных сценария через
  `Application.process_update`: меню/`/templates`, создание из стандарта и
  пустого, карточка, секции, предпросмотр, кирпичики, сброс, стандарт из
  админки, отказ постороннему и суперадмин, отзыв прав при сохранении,
  конфликт revision, stale/повторные callback, двойное сохранение, рестарт,
  неоднозначность с выбором `brick_id`, Unicode, очистка секции, фото,
  пагинация, WIP и очистка при переходе;
- `tests/test_stage25_template_resets.py` — 6 сервисных тестов сбросов
  личного/стандартного шаблонов: подтверждение, владелец/админ/суперадмин,
  stale, not_found, новая версия, неизменность истории и снимков.

Файлы Stage 26:

- `tests/test_stage26_brick_proposals_schema.py` — 10 тестов миграции 11:
  схема/индексы/ограничения, четыре статуса, неизменяемый снимок,
  единственный переход, запрет DELETE, FK и триггеры резервации кодов;
- `tests/test_stage26_brick_proposals_service.py` — 19 сервисных тестов:
  создание снимка, права автора/места/суперадмина, списки и счётчики,
  approve/reject/withdraw, конфликты кодов и системные коды, повторные
  решения, транзакционная гонка, отсутствие TTL и аудит;
- `tests/test_stage26_brick_proposals_flow.py` — ровно 40 интеграционных
  сценариев через `Application.process_update`: мастер и «Мои предложения»,
  пагинация, отзыв, stale/повторные callback, приватность, очереди места и
  суперадмина, подтверждения, конфликт при одобрении, передача админки,
  уведомления при сбое Telegram, бессрочность, WIP и доступность
  одобренных кирпичиков редактору Stage 25.

Файлы Stage 27A:

- `tests/test_stage27a_announcement_schema.py` — 6 тестов миграции 12:
  таблицы/индексы/CHECK/FK, различие отсутствующего и пустого значения,
  уникальность снимка и идемпотентного ключа, неизменяемость автора/места/
  снимка/значений/дополнений, рост ревизии и сохранность легаси-черновика;
- `tests/test_stage27a_announcement_wizard.py` — 13 тестов доменной логики:
  независимый снимок и сохранённые `код → brick_id`, порядок
  шапка→тело→подвал, один вопрос на повторяющийся `brick_id`, пропуск
  известных/допустимо пустых значений, все семь типов, даты/время/URL,
  контекст места без заглушек и диагностика архива/несовместимости;
- `tests/test_stage27a_announcement_store.py` — 24 сервисных теста:
  место только по `place_id`, альтернативное название без создания места,
  стандартный/личный шаблон и запрет чужих личных, независимый снимок,
  значения по `brick_id`, допустимое пустое значение, дополнения только
  текущего анонса, этапы, рестарт, идемпотентность, optimistic locking,
  атомарный откат и изоляция автора.

Файлы Stage 27B:

- `tests/test_stage27b_announcement_ui.py` — 11 интеграционных сценариев
  через `Application.process_update`: меню и старый WIP, место/альтернатива,
  изоляция шаблонов и стабильный снимок, все семь типов, extras, рестарт,
  сохранение, stale/чужие callback и диагностика;
- `tests/test_stage27b_announcement_fields.py` — 8 сервисных тестов полей,
  предпросмотра, готовности и стандартных известных значений.

Файлы Stage 27C:

- `tests/test_stage27c_announcement_publication.py` — 11 сервисных/schema
  тестов: фото и дедупликация, freeze/resume, partial/reset/sent, CAS,
  JSON/status/shape/immutability-триггеры и итоговый HTML с семью типами и
  extras во всех секциях;
- `tests/test_stage27c_announcement_ui.py` — 10 интеграционных сценариев
  Stage 28: автоматическая выдача после последнего ответа/пропуска, оба
  вида шаблонов, 0/1/альбом/11 фото и медиагруппы, замена/перезапуск с
  личными фото, частичная отправка и продолжение, сбой первой отправки и
  повтор, рестарт во время отправки, чужой callback, групповой чат,
  порядок «сначала новая копия, потом удаление старой», неудача удаления
  старше 48 часов и правка extras с пересборкой.

Файлы Stage 28:

- `tests/test_stage28_announcement_redelivery.py` — 7 тестов миграции 14:
  журнал и контрольные суммы 1–13 без изменений, сохранность легаси
  `drafts`/WIP/фото после 1→14, отсутствие изменений колонок/индексов,
  атомарный откат `DROP`+`CREATE` при сбое и повторное применение,
  разрешённый `sent -> sending` только с `previous_ids`, запрет прочих
  переходов и правок из `sent`, копия перед миграцией видит
  зафиксированные данные из WAL, сбой копии блокирует версию 14.

Файлы Poster 2.1 R3 (миграция 19):

- `tests/test_poster2_r3_conflicts.py` — 21 тест модели конфликтов кодов:
  одобрение глобального при местном носителе и выигрыш для новых привязок,
  неизменность снимков/версий, идемпотентность задач и уведомлений, оба
  исхода (переименование и переключение шаблона), обязательность и
  индикация активной задачи в карточке места, негатив (IDOR, forged
  callback, stale, 2+ местных носителей), гонка решений, рестарт и миграция
  19 на заполненной базе;
- `tests/test_poster2_r1_brick_codes_tester.py` — модель R2→R3: два
  несистемных носителя больше не дают `status="error"`, местные кирпичики
  не переписываются, задачи создаются по местам.

Всего в полном наборе — **1285 тестов** (1237 после Stage 28 + тесты
Poster 2.1 R1–R3).

Все тесты используют только временные файлы SQLite. Настоящая
`Poster/post_bot.db` не открывается на запись; тест сверяет её sha256 и
mtime до и после полной инициализации временной базы.

## 12. Ограничения Stage 19A–28

- В базе технически возможно место без названий (нет триггера); публичного
  API для этого нет. `get_place_details`/списки такие строки не показывают.
- Альтернативные названия может добавить любой пользователь; редактировать
  и удалять их может суперадмин или админ места (Stage 20B, кабинет).
- Адрес/ссылка места редактируются админом места или суперадмином в
  кабинете (Stage 20B); заполнение посторонним недоступно. Адрес хранится
  без искусственного предела длины; ссылка — до 500 символов интерфейса.
- Слияния дублей мест нет: только предупреждение и явный выбор пользователя.
- Подтверждённое место хранится в `place_sessions` (сценарий); новый мастер
  использует собственные `place_id`/`place_name_id`, а девятишаговый мастер
  по-прежнему работает со своими полями.
- Стандартный шаблон создаётся атомарно вместе с местом и бэкфиллится
  миграцией 8 для существующих мест; ровно один стандарт на место.
- Stage 26 добавляет бессрочные предложения кирпичиков в существующие
  «Мои кирпичики» и кабинет. Stage 27A–27C добавляют новый адаптивный мастер
  с шаблонами, кирпичиками, extras, фото и публикацией; полный интерфейс
  управления кирпичиками места и Mini App не реализованы. Легаси мастер
  поста продолжает использовать девять полей; шаблоны и кирпичики в него не
  вставляются.
- Stage 27A–27C не заменяют девятишаговый мастер, `POST_STEPS`, старые
  черновики, WIP и публикацию: новый анонс живёт в отдельных
  `announcement_*`-таблицах. Старый формат черновика не конвертируется в
  `announcement_values`; `draft_values` из ранних планов не создавался.
- Публикация Stage 27C/28 не имеет отдельного согласования/очереди: готовый
  анонс автоматически уходит в личный чат автора. После `partial` доступно
  только продолжение замороженного payload; exactly-once при потерянном
  ответе Telegram не обещается, и UI честно предупреждает о возможном дубле
  последней группы. Возврат к правке разрешён при `failed`, `step=0` и
  пустых `ids`; при наличии `previous_ids` сброс запрещён, чтобы не потерять
  адрес прошлой доставленной копии.
- Stage 28 не хранит «историю версий»: после пересборки payload описывает
  последнюю доставленную версию. Telegram не удаляет сообщения старше 48
  часов, поэтому прошлая копия может остаться в чате — доставка при этом
  считается успешной. Выдача и временный предпросмотр вложений в
  группу/канал запрещены; отказ не меняет черновик.
- Все вопросы нового мастера необязательны: допустимый пропуск хранится как
  `empty_optional`, а не как отсутствие строки. Некорректное непустое
  значение отклоняется; известные значения места подставляются по
  постоянным ID 5–7, отсутствующие адрес/ссылка не заменяются заглушками.
- У предложения нет TTL/`expires_at` и фоновых проверок: `pending` ждёт
  решения или отзыва бессрочно. `approved`/`rejected`/`withdrawn` —
  терминальные статусы; `superseded` и `expired` для предложений не
  используются. Строки не удаляются физически, а снимок неизменяем.
- Одобрение создаёт отдельный кирпичик в целевой области с новым ID;
  исходный личный кирпичик, владелец, код и привязки шаблонов не меняются.
  Конфликт кода — ошибка без переименования/дубля; системные коды
  зарезервированы. Сбой в транзакции не оставляет `approved`.
- Очередь предложений места изолирована по `place_id`; сводная очередь мест
  и глобальная очередь доступны только суперадмину. Уведомление автору —
  best-effort после commit: сбой Telegram не откатывает решение.
- Стандартный шаблон открывается из кабинета действующим админом этого
  места или суперадмином; права перепроверяются сервисом при сохранении и
  сбросе. Личный шаблон доступен только владельцу, включая запрет доступа
  суперадмину.
- Версии и снимки шаблонов неизменяемы; правка/сброс создают новую версию.
  Полностью пустая версия запрещена CHECK, поэтому «Пустой шаблон» получает
  минимальное тело `[название]`.
- `template_sessions` — только UI-состояние: права, ID шаблона и revision
  проверяются заново; текст черновика может пережить рестарт, но не
  является сохранённой версией.
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

## 13. Готовность к Stage 28

После 19A–28 работают:

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
- `templates`/`template_versions`/снимки/привязки — стандарты мест,
  личные шаблоны, неизменяемая история, снимки, `revision` и права
  (Stage 23); `code` в привязках и парсер/рендер (Stage 24);
  `template_sessions` и Telegram-редактор (Stage 25);
- `brick_proposals` — неизменяемые бессрочные предложения, четыре
  статуса, права места/суперадмина, approve/reject/withdraw,
  транзакционное создание отдельного кирпичика, конфликты/резервация
  кодов, очереди в кабинетах и best-effort уведомления (Stage 26);
- `announcement_drafts`/`announcement_values`/`announcement_extras` —
  сервисный фундамент адаптивного мастера: место по `place_id` и
  альтернативному названию, стандартный/личный шаблон, независимый снимок,
  вопросы по сохранённым `brick_id`, явное пустое значение, дополнения
  только анонса, этапы, рестарт, идемпотентность и optimistic locking
  (Stage 27A), отдельная коллекция фото и публикация с замороженным
  HTML/фото и прогрессом подтверждённых операций (Stage 27C);
- автоматическая выдача доставленного анонса в личный чат и замена
  прошлой версии по явной кнопке (Stage 28, миграция 14 — только триггер
  проверяемого перехода `sent -> sending`);
- атомарные создание места, альтернативы, назначение/замена/отзыв
  администратора, подтверждение передачи, конкурентное создание кирпичика
  и условные обновления шаблонов (ограничения SQLite + одна транзакция,
  откат при сбое);
- внешние ключи/каскады на соединениях приложения.

Stage 23–28 не меняли мастер поста, WIP, календарь, фото/альбомы
легаси-мастера и его отправку. Полный интерфейс кирпичиков места не входит
в 27A–28. Реальная `Poster/post_bot.db` в тестах не изменялась.
