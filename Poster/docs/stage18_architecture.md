# Stage 18 — архитектура мест, кирпичиков, шаблонов и администрирования

**Статус:** целевая архитектура; Stage 19A–27A реализованы. Stage 19A —
безопасный фундамент (журнал миграций, `users`, `places`, `place_names`),
Stage 19B — Telegram-справочник «Места» (поиск, карточки, создание,
альтернативные названия, подтверждение выбора), миграцию 2
(`places.address/url`, `place_sessions`) и WAL-aware автоматическую копию
перед миграцией. Stage 20A — роли, кабинеты и аудит назначений, Stage 20B —
редактирование данных места, Stage 21 — передача админки с двусторонним
подтверждением и сроком 24 часа. **Stage 22A** — фундамент кирпичиков:
реестр `bricks` (миграция 6), девять системных определений, типы/правила/
значения и Telegram-независимый сервис. **Stage 22B** — Telegram-раздел
«Мои кирпичики» (миграция 7): создание всех семи типов, карточка, правка,
мягкий архив/восстановление, ревизии и `brick_sessions`.

**Stage 23** добавил шаблоны как данные (миграция 8): `templates`,
`template_versions`, неизменяемые версии, снимки и привязки по `brick_id`,
стандарты мест с авто-созданием/бэкфиллом и личные шаблоны. **Stage 24** —
парсер/компилятор `[код]` и миграция 9 (`code` в привязках, чтобы рендер
восстанавливал конкретный `brick_id` без повторного разрешения кода).
**Stage 25** — полноценный редактор «Мои шаблоны», стандартный шаблон в
кабинете места и миграция 10 (`template_sessions`). **Stage 26** —
согласование кирпичиков: миграция 11 (`brick_proposals`), бессрочные
предложения личных кирпичиков месту/системе, подтверждаемые решения,
транзакционное создание отдельного кирпичика и очереди в существующих
кабинетах. **Stage 27A** — сервисный фундамент адаптивного мастера
(миграция 12): `announcement_drafts`, `announcement_values` и
`announcement_extras`, независимый снимок шаблона, детерминированные
вопросы по `brick_id`, значения места, этапы, рестарт и optimistic locking.
**Stage 27B** добавил Telegram-интерфейс мастера: меню «📝 Новый мастер»,
выбор места и шаблона, вопросы всех семи типов, дополнительные кирпичики,
предпросмотр и сохранение черновика. **Stage 27C** подключил отдельные
фотографии и публикацию с замороженным payload и продолжением после
частичного сбоя. **Stage 28** убрал финальную кнопку: последний ответ сам
выдаёт готовый анонс в личный чат отдельным сообщением, а правка данных,
фото или extras пересобирает и заменяет прошлую версию по кнопке
«🔄 Применить изменения».

> **S5.** Старый девятишаговый мастер, его WIP, `Draft` и обработчики
> удалены; разделы ниже, описывающие его как действующий, — исторические.
> Таблица `drafts` переименована миграцией 17 в `legacy_drafts_backup`
> (без потери данных, см. `docs/migrations.md`). Единственный мастер
> анонсов — этот.

Базовая ревизия Stage 18: `b7b9b9512f9d14bd51010240924666c8940c4bdb`
(проект архитектуры; 591 тест). После Stage 19A — 622 теста, 19B — 687,
20A — 748, 20B — 787, 21 — 843, 22A — 895, 22B — 932, 23–25 — 1077,
26 — 1146, 27A — 1189, 27B — 1208, 27C — 1226, 28 — 1237; CI Python 3.13 и 3.14. Реализованная схема, backup и
правила миграций описаны в [`docs/migrations.md`](migrations.md); фактические
пользовательские потоки — в [`README.md`](../README.md).

## Содержание

1. [Анализ существующей архитектуры Poster](#1-анализ-существующей-архитектуры-poster)
2. [Перечень сущностей и их взаимосвязей](#2-перечень-сущностей-и-их-взаимосвязей)
3. [Предлагаемая схема SQLAlchemy/SQLite](#3-предлагаемая-схема-sqlalchemysqlite)
4. [Ролевая модель и матрица прав](#4-ролевая-модель-и-матрица-прав)
5. [Жизненные циклы](#5-жизненные-циклы)
6. [Формат данных шаблонов и секций](#6-формат-данных-шаблонов-и-секций)
7. [Грамматика текстовых шаблонов и порядок обработки](#7-грамматика-текстовых-шаблонов-и-порядок-обработки)
8. [Схема адаптивного создания поста](#8-схема-адаптивного-создания-поста)
9. [Совместимость и миграция черновиков и WIP](#9-совместимость-и-миграция-черновиков-и-wip)
10. [Безопасность, конкурентные операции, транзакции](#10-безопасность-конкурентные-операции-транзакции)
11. [План автоматизированного тестирования](#11-план-автоматизированного-тестирования)
12. [Проверка дорожной карты Stage 19–28](#12-проверка-дорожной-карты-stage-1928)
13. [Обнаруженные неоднозначности и рекомендуемые решения](#13-обнаруженные-неоднозначности-и-рекомендуемые-решения)
14. [Технические риски и способы их уменьшения](#14-технические-риски-и-способы-их-уменьшения)
15. [Примеры типовых сценариев](#15-примеры-типовых-сценариев)
16. [Приложение А. Соответствие системных кирпичиков и легаси-полей](#приложение-а-соответствие-системных-кирпичиков-и-легаси-полей)
17. [Приложение Б. Словарь терминов](#приложение-б-словарь-терминов)

---

## 1. Анализ существующей архитектуры Poster

### 1.1. Состав проекта (фактический)

| Модуль | Строк | Ответственность |
|---|---:|---|
| `models.py` | 1412 | SQLAlchemy-модели `Draft`, `UserState`, `User`, `Place`, `PlaceName`, `PlaceSession`, `PlaceAdmin`, `AdminSession`, `PlaceEditLog`, `AdminTransfer`, `Brick`, `BrickSession`, `BrickProposal`, `Template`, `TemplateVersion`, `TemplateVersionBrick`, `TemplateSnapshot`, `TemplateSnapshotBrick`, `TemplateSession`, `AnnouncementDraft`, `AnnouncementValue`, `AnnouncementExtra`; сериализация `photos`/`formats` |
| `announcement_wizard.py` | 446 | Снимок нового мастера, сохранённые `код → brick_id`, детерминированные вопросы, контекст места, все семь типов и диагностика (без Telegram/БД) |
| `announcement_store.py` | 1285 | Место/альтернативное название, стандартный/личный шаблон, значения и extras, этапы, рестарт, идемпотентность и optimistic locking Stage 27A |
| `brick_proposal_store.py` | 1092 | Предложения кирпичиков Stage 26: снимок, права места/суперадмина, approve/reject/withdraw, конфликты кодов и транзакционное создание отдельного `Brick` |
| `database.py` | 118 | Движок SQLite (WAL, busy_timeout, `foreign_keys=ON`), `create_all` легаси-таблиц, легаси-бэкфилл `_migrate()` |
| `migrations.py` | 1930 | Нумерованные миграции 1–12: журнал, контрольные суммы, транзакции, защита от конкурентного запуска, WAL-aware копия перед DDL |
| `brick_types.py` | 695 | Каталог типов, девять системных определений, нормализация кодов/заголовков, строгие `rules_json`, проверка значений и пять состояний |
| `brick_store.py` | 1364 | Telegram-независимый реестр: чтение/поиск, видимость, разрешение `[код]` и кандидаты, проверка доступности, авторизованные создание/правка/архив/восстановление с ревизиями |
| `brick_state.py` | 139 | Ограниченное состояние Telegram-конструктора личных кирпичиков в `brick_sessions`; переживает рестарт |
| `template_types.py` | 176 | Секции, типы шаблонов, валидация имени/секций, встроенный стандарт v1 |
| `template_parser.py` | 490 | Лексер/парсер `[код]`, экранирование, Telegram-разметка, гиперссылки и диагностика |
| `template_compiler.py` | 451 | Привязка разобранного текста к постоянным `brick_id`, сборка версии и рендер сохранённой версии/снимка |
| `template_store.py` | 1848 | Шаблоны, версии, снимки, права владельца/места, ревизии, сброс к стандарту и создание личных шаблонов |
| `template_state.py` | 158 | Ограниченное состояние Telegram-редактора шаблонов в `template_sessions`; переживает рестарт |
| `user_store.py` | 80 | UPSERT пользователя по постоянному Telegram user ID |
| `permissions.py` | 98 | Роли суперадмина/админа места и проверки по постоянному Telegram ID |
| `place_store.py` | 552 | Нормализация, поиск/ранжирование/похожесть, карточки, атомарное создание места и альтернативных названий |
| `place_state.py` | 107 | Ограниченное JSON-состояние сценария «Места» (`place_sessions`) |
| `place_admin_store.py` | 479 | Атомарные назначение/замена/отзыв админа; `superseded` для ожидающей передачи |
| `place_edit_store.py` | 674 | Правка названий/адреса/ссылки, аудит `place_edit_log` |
| `admin_transfer_store.py` | 896 | Передача админки: 24 часа, SHA-256 токена, кандидат, утверждение, атомарное подтверждение |
| `admin_state.py` | 104 | Восстановление фазы кабинета после рестарта без хранения прав |
| `handlers/post_creation.py` | 2805 | Визард из 9 шагов, превью, редактирование, фото/медиагруппы, календарь Stage 17, черновики-точки входа; `/templates` сохраняет WIP и завершает диалог |
| `handlers/places.py` | 1569 | Раздел «Места»: поиск, списки, карточка, создание, альтернативы, подтверждение, stale-callback |
| `handlers/admin.py` | 4799 | Кабинет Stage 20A/20B, передача Stage 21 и предложения Stage 26: очереди места/суперадмина, подтверждения решений, проверки прав, уведомления, deep-link; вход в стандартный шаблон |
| `handlers/bricks.py` | 3020 | Разделы «Мои кирпичики» и «Мои предложения»: мастер создания/правки, предложение месту/системе, проверка снимка, отзыв, пагинация, stale-callback |
| `handlers/templates.py` | 2754 | Раздел «Мои шаблоны» и стандартный шаблон места: создание, карточка, секции, предпросмотр, доступные кирпичики, сброс, конфликты и stale-callback |
| `handlers/main_menu.py` | 329 | Главный экран, /start (включая `tf_` payload), /help, очистка состояния, подсказка вне диалога, вход в кирпичики и шаблоны |
| `handlers/drafts.py` | 306 | Список/листание/удаление черновиков на экране управления |
| `handlers/callbacks.py` | 41 | Кнопка «Главное меню» вне диалога |
| `utils/screen.py` | 408 | Единый экран: `render_screen`, re-anchor, best-effort очистка, WIP-снимок в `user_states` |
| `utils/parsers.py` | 391 | Гибкий разбор даты/времени; `app_today()` (зона `Europe/Moscow`); лото-числительные |
| `utils/calendar_view.py` | 281 | Чистая логика кнопочного календаря (Stage 17) |
| `utils/tg_html.py` | 348 | `_entities → безопасный HTML`, разбиение длинного HTML (`split_html`), UTF-16-смещения |
| `utils/formatter.py` | 74 | Типографика («ёлочки», тире) без экранирования разметки |
| `utils/validators.py` | 132 | `normalize_date/time/url`; опасные схемы URL отклоняются |
| `utils/lotto.py` | 413 | «Полседьмого», «без четверти семь» и т. п. |
| `bot.py` | 131 | Сборка `Application`; порядок хендлеров; `allowed_updates=ALL_TYPES` |
| `tests/*` | 71 тестовый файл | 1237 тестов; каркас `integration_harness.py` (FakeBot, `frozen_today`, временная SQLite); Stage 19A–28 — миграции, схема, места, роли, правка, передача, реестр и личные кирпичики, шаблоны/парсер/снимки/редактор, предложения кирпичиков, новый мастер, его Telegram-UI, фото, автоматическая выдача и замена версии, backup/restore |

### 1.2. Основные факты, от которых отталкивается проект

- **Легаси-таблицы.** `Draft` — 9 полей анкеты + `photos` (JSON file_id,
  независимая коллекция вложений), `formats` (JSON HTML-представлений),
  `created_at/updated_at`; `UserState` — `user_id` PK, `chat_id`,
  `screen_message_id`, `payload` (JSON WIP), `updated_at`. Stage 19A добавил
  `users`, `places`, `place_names` и журнал `schema_migrations`; Stage 19B —
  `places.address/url`, `place_sessions` и Telegram-справочник «Места»;
  Stage 20A — `place_admins`/`admin_sessions`, 20B — `place_edit_log`,
  Stage 21 — `admin_transfers`, Stage 22A — `bricks` (реестр кирпичиков),
  Stage 22B — `bricks.revision` и `brick_sessions` (конструктор личных
  кирпичиков), Stage 23 — `templates`, `template_versions`,
  `template_version_bricks`, `template_snapshots`,
  `template_snapshot_bricks`; Stage 24 — `code` в привязках; Stage 25 —
  `template_sessions` (редактор шаблонов). WIP-пост, состояние
  мест/кабинета/кирпичиков/шаблонов, передача и черновые значения
  кирпичиков изолированы.
- **Историческая проблема (снята в S5).** Регистр полей был жёстко прошит
  в `POST_STEPS` (список словарей `key/label/prompt/validator/normalizer`):
  на нём строились визард, редактор, сводка `build_post_summary`,
  `DRAFT_FIELD_KEYS`, клавиатуры и WIP-снимок. `POST_STEPS`,
  `build_post_summary`, `DRAFT_FIELD_KEYS`, состояния
  `POST_CREATION`/`EDIT_FIELD` и входы `/create_post`/`restore_wip`
  **удалены**. Состав анонса теперь задаёт шаблон и реестр кирпичиков.
- **Состояния диалога:** единственный мастер — именованный
  `ConversationHandler` `announcement_wizard` (`AW_*`). Fallback'и завершают
  диалог, сохраняя черновик в `announcement_drafts`. Устаревшие
  callback обслуживает `stale_dialog_action` по `STALE_DIALOG_CALLBACK_PATTERN`.
- **Единый экран:** `screen.render_screen` правит сообщение (`edit_message_text`,
  HTML, `disable_web_page_preview`), при недоступности создаёт новое и
  перезапоминает ref; «message is not modified» глотается. Re-anchor
  удаляет старый экран и рисует новый у ввода пользователя. Очистка
  временных сообщений выполняется **только после** подтверждённого сохранения
  (`sync_wip == True` / `save_state == True`).
- **WIP-снимок** (`_wip_payload`) зеркалит поля черновика + `formats`, `photos`,
  `current_step`, `editing_draft_id`, `edit_field`, медиа-состояние, ref экрана
  и прогресс частичной отправки. Пустой пост не сохраняется (`payload=NULL`).
- **Превью = готовый пост:** один и тот же `send_post`, `build_post_summary`,
  разбиение на части по 4000/1000 знаков UTF-16, медиагруппы по 10, прогресс
  частичной доставки (Stage 12) — новая попытка досылает недостающее.
- **Форматирование:** пользовательский ввод с entities сохраняется
  дословно + HTML в `user_data["formats"][key]`; плоский текст проходит
  типографику (`format_text`) и экранируется один раз при рендере.
- **Миграции:** с Stage 19A — нумерованный механизм `migrations.py` с журналом
  `schema_migrations`, контрольными суммами DDL и транзакционным применением;
  с Stage 19B перед первым ожидающим DDL создаётся WAL-aware автоматическая
  копия, а при её сбое миграция не начинается (подробно —
  [`docs/migrations.md`](migrations.md)). Старый `_migrate()`
  остался только для очень старых `drafts` (добавление колонок
  `photos/formats/updated_at`); новые изменения схемы идут только через
  нумерованные миграции.
- **Тесты:** изолированы от настоящего токена и БД (`tests/__init__.py`
  подставляет фиктивный токен и `DATABASE_URL` во временный каталог);
  интеграция через `Application.process_update`, время замораживается
  (`frozen_today` патчит `utils.parsers.datetime`, календарь Stage 17
  разделяет эту точку «сегодня» через `app_today()`).

### 1.3. Хрупкие места, важные для будущих этапов

> **S5.** Строки ниже описывают легаси-мастер, удалённый в S5: `POST_STEPS`,
> `build_post_summary`, шаги «Место»/«Изображение» и черновики поста больше
> не существуют. Таблица оставлена как исторический след анализа; актуальные
> места риска — адаптивный мастер (`handlers/announcement_wizard.py`),
> снимок шаблона и optimistic locking `announcement_drafts`.

| Место | Почему хрупко | Влияние на Stage 19–28 |
|---|---|---|
| `POST_STEPS` в `post_creation.py` | Метки/промпты/валидаторы завязаны на один список | Реестр кирпичиков и личный конструктор Stage 22A/22B уже есть; новый мастер Stage 27B использует отдельные `announcement_*`-данные, не меняя формат легаси-WIP |
| `build_post_summary` | Сводка перечисляет поля фиксированным списком | Рендер шаблона уже есть (Stage 23–25); новый предпросмотр Stage 27B собирает его по снимку и `brick_id` |
| Поле `place_url` — 8-й шаг, в конце анкеты | Если место выбрано из справочника, вопрос лишний | Мастер Stage 27B отсекает известные значения через `build_questions` Stage 27A |
| Фото-логика привязана к индексу шага «Изображение» | `handle_photo` знает шаг по `current_step` | При адаптивном создании фотографии остаются отдельным, необязательным потоком вложений; шаблон и кирпичики на него не влияют |
| Список черновиков печатает 4 поля | Не знает про произвольные поля | Карточки по кирпичикам, ограничение числа строк (Stage 28) |
| `handlers/drafts.db` (28 КБ) и пустой `responsible_persons.db` рядом с кодом | Остаточные локальные файлы сентября 2024; в git не попадали | Не использовать и не трогать в Stage 18; убрать отдельным техкоммитом в будущих этапах |

### 1.4. Что сознательно сохраняется

- Один интерфейсный экран, re-anchor, best-effort очистка, проверка
  «очистка только после подтверждённого сохранения».
- WIP/рестарт, черновики, редактирование, медиагруппы, календарь Stage 17.
- Фотографии — независимые вложения анонса, а не кирпичик и не узел
  текстового шаблона; приём фото/альбомов Stage 12–17 не меняется.
- Все текущие тесты как регрессионный базис.

---

## 2. Перечень сущностей и их взаимосвязей

```
users (id, telegram_user_id)
  │  1
  ├──< user_states             (WIP-снимок, ref экрана; user_id = Telegram ID)
  │
  ├──N:M> places ──1:N── place_names        (каноническое + альтернативы)
  │ place_admins ─(place_id 0..1 админ)── places
  │
  ├──< bricks                  (типизированный реестр полей)
  │      scope = system | place(place_id) | personal(owner_user_id)
  │
  ├──< templates ──1:N── template_versions
  │      kind = place(place_id) | personal(owner_user_id)
  │
  ├──< brick_proposals         (согласование кирпичиков; без срока)
  │      target = place(place_id-админ рассматривает) | global(суперадмин)
  │
  ├──< admin_transfers         (передача админки места, срок 24 ч)
  │
  └──< announcement_drafts     (новый мастер Stage 27A; отдельно от drafts)
         │ author n:1 users, place n:1 places,
         │ template n:1 templates, 1:1 template_snapshots
         ├──< announcement_values   (brick_id n:1 bricks; нет строки = не задано)
         └──< announcement_extras   (brick_id n:1 bricks; только этот анонс)

drafts.image / drafts.photos   (вложения анонса: фото/альбомы — НЕ кирпичик
                                 и не узел шаблона; живут отдельно от секций)
```

Отдельно от этой схемы: `place_sessions`, `admin_sessions`,
`brick_sessions` и `template_sessions` — состояние UI-сценариев «Места»,
кабинета, личных кирпичиков и редактора шаблонов (ключ — Telegram user ID,
без FK на `users`). Это не WIP-пост и не черновик: выбранное место, фаза
кабинета или черновик кирпичика не попадают ни в `user_states`, ни в
`drafts`, ни в `announcement_*`.

**Граница Stage 27A:** в схеме реально существуют `templates`,
`template_versions`, `template_version_bricks`, `template_snapshots`,
`template_snapshot_bricks`, `template_sessions` (миграции 8–10),
`brick_proposals` (миграция 11) и `announcement_drafts`/
`announcement_values`/`announcement_extras` (миграция 12), а также `bricks`,
`revision`, `brick_sessions` и `brick_proposals`. Старые `drafts` и WIP
по-прежнему хранят девять легаси-полей, а фотографии — отдельно в
`drafts.photos`; в `announcement_*` они не переносятся. `draft_values` из
ранних планов не создавался: значения нового мастера живут в
`announcement_values` и связаны постоянным `brick_id`.

Ключевые свойства связей:

- **Пользователь.** `users.id` — внутренний ключ для будущих связей;
  `users.telegram_user_id` — постоянный внешний идентификатор (UNIQUE).
  `Draft.user_id` и `UserState.user_id` — легаси-значения Telegram ID,
  они не переделываются в FK.
- **Место ↔ названия.** Каноническое название — строка `place_names` с
  флагом `is_canonical` (не более одной на место); альтернативы — соседние
  строки. С Stage 27A `announcement_drafts` хранит выбранный `place_name_id`
  и снимок отображаемого имени: альтернатива используется в анонсе, но не
  создаёт новое место и не переименовывает существующее. `place_sessions`
  остаётся UI-состоянием справочника.
- **Состояние мест.** `place_sessions` — ограниченный JSON (фаза, id,
  введённое название, номера страниц). Оно переживает рестарт, очищается при
  выходе из сценария и не меняет WIP `user_states`.
- **Место ↔ админ.** `place_admins` — история назначений; активная строка
  (`revoked_at IS NULL`) не более одной на место, администратора может не
  быть вовсе. Замена — атомарный UPDATE старой строки + INSERT новой;
  передача Stage 21 делает то же после двустороннего подтверждения.
- **Кирпичик.** Полиморфная принадлежность: `system` (без владельца),
  `place` (место), `personal` (пользователь). Stage 22A реализовал реестр
  `bricks` и сервис чтения/создания; Stage 22B — управление личными
  кирпичиками, ревизии и `brick_sessions`. Значения нового анонса живут в
  `announcement_values` по постоянному `brick_id`; привязки шаблонов также
  хранят `brick_id` и авторский код только для рендера. Фотографии
  кирпичиком не являются.
- **Шаблон ↔ версии.** `template_versions` хранит историю секций;
  `announcement_drafts` ссылается на независимый `template_snapshot_id`,
  поэтому правка живого шаблона не меняет начатый анонс (§9). Снимок
  принадлежит ровно одному анонсу (частичный уникальный индекс).
- **Вложения анонса.** Фотографии — самостоятельная коллекция конкретного
  черновика (`Draft.image`/`Draft.photos` у легаси, `announcement_drafts.photos`
  у нового мастера): не входят в `template_snapshot`,
  `announcement_values` или `announcement_extras`, не задаются составом
  кирпичиков и не блокируют создание анонса при отсутствии. Фото не являются
  кирпичиком и не получают токен `[фото]`.
- **Новый анонс (`announcement_*`).** `announcement_drafts` — автор, место,
  выбранное название и снимок контактов, шаблон/версия/снимок, этап,
  ревизия и ключ идемпотентности. `announcement_values` различает
  отсутствующее значение и явно допустимое пустое; `announcement_extras`
  добавляет кирпичик только в этот анонс. Записи изолированы от `drafts`,
  WIP и других авторов; триггеры защищают идентичность и снимок.
- **Заявки (`brick_proposals`) и передачи (`admin_transfers`)** —
  отдельные сущности-процессы; они не смешиваются с самими кирпичиками,
  шаблонами и анонсами (согласование касается только кирпичиков).

---

## 3. Предлагаемая схема SQLAlchemy/SQLite

Все изменения только добавочные. С Stage 19A новые таблицы и колонки идут
через нумерованные миграции [`docs/migrations.md`](migrations.md), а не через
`create_all`: он остаётся только легаси-базлайном `drafts`/`user_states`.
Ниже — целевая схема (SQLite; в моделях — SQLAlchemy 2.0 `Mapped[]`).
`users`/`places`/`place_names` реализованы в Stage 19A, `bricks` — в
Stage 22A, `revision`/`brick_sessions` — в Stage 22B; `templates`,
`template_versions`, снимки и `template_sessions` — в Stage 23–25;
`brick_proposals` — в Stage 26; `announcement_drafts`/`announcement_values`/
`announcement_extras` — в Stage 27A. `draft_values` и новые легаси-колонки
черновиков не создавались: новый мастер хранит данные отдельно, а
девятишаговый формат остаётся без изменений.

### 3.1. `users`

**Stage 19A — реализовано** (роли в Stage 20A определяет только конфигурация;
колонка `is_superadmin` не добавлялась и не планируется):

```sql
CREATE TABLE users (
    id               INTEGER PRIMARY KEY,       -- внутренний ID для связей
    telegram_user_id INTEGER NOT NULL UNIQUE,   -- постоянный Telegram user id
    username         TEXT,                      -- последнее увиденное (без @)
    full_name        TEXT,                      -- first/last name для карточек
    created_at       DATETIME NOT NULL,
    updated_at       DATETIME,
    last_seen_at     DATETIME
);
```

- `telegram_user_id` уникален на уровне БД; username — справочное поле,
  которое может меняться и не является идентификатором.
- В Stage 19A есть слой хранения (`user_store.get_or_create_user`,
  атомарный UPSERT). В Stage 19B справочник создаёт/находит пользователя
  при создании места и добавлении альтернативы. **Stage 20A**: фоновый
  обработчик (`handlers/user_tracking.py`, группа -1) регистрирует и
  обновляет профиль пользователя при каждом Update, поэтому кабинеты видят
  актуальные username/имя у всех, кто писал боту.
- Роль суперадмина: список постоянных Telegram user id в конфигурации
  (`.env: SUPERADMIN_USER_IDS`, разделитель «,», только числа). Источник
  истины — **только env** (`permissions.is_superadmin` читает
  `config.SUPERADMIN_USER_IDS` при каждом вызове); колонки `is_superadmin`
  нет, DB/кнопками суперадмина назначить нельзя. Пустая конфигурация
  закрывает административные функции, обычные сценарии работают. username —
  не идентификатор (меняется пользователем). Суперадмин назначает админа
  места по постоянному Telegram user ID, который приложение переводит во
  внутренний `users.id`.

### 3.2. `places` и `place_names`

**Stage 19A/19B — реализовано** (фундамент, альтернативные названия и
контакты; интерфейс администратора ещё нет):

```sql
CREATE TABLE places (
    id                 INTEGER PRIMARY KEY,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at         DATETIME NOT NULL,
    address            TEXT,   -- Stage 19B, миграция 2
    url                TEXT    -- Stage 19B, миграция 2
);
CREATE INDEX ix_places_created_by_user_id ON places(created_by_user_id);

CREATE TABLE place_names (
    id                 INTEGER PRIMARY KEY,
    place_id           INTEGER NOT NULL REFERENCES places(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,       -- как показываем (орфография пользователя)
    name_search        TEXT NOT NULL,       -- нормализовано для поиска (§5.6)
    is_canonical       INTEGER NOT NULL,    -- 0|1; не более одной на место
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at         DATETIME NOT NULL,
    CHECK (length(trim(name)) > 0),
    CHECK (length(trim(name_search)) > 0),
    CHECK (is_canonical IN (0, 1)),
    UNIQUE (place_id, name_search)
);
CREATE INDEX ix_place_names_place_id ON place_names(place_id);
CREATE INDEX ix_place_names_name_search ON place_names(name_search);
CREATE UNIQUE INDEX ux_place_names_one_canonical
    ON place_names(place_id) WHERE is_canonical = 1;
```

- `UNIQUE (place_id, name_search)` запрещает дубли названия внутри одного
  места, но не между местами: похожие названия **не** сливаются неявно.
- Частичный уникальный индекс допускает не более одной канонической строки;
  наличие хотя бы одной обеспечивает прикладной слой
  (`place_store.create_place` создаёт место и каноническое название в одной
  транзакции). В базе место без названий технически возможно, но публичного
  API для этого нет.
- `created_by_user_id` — внутренний `users.id`, не Telegram ID. Создатель
  места **не** получает административных прав.
- `address`/`url` добавлены миграцией 2 Stage 19B. С Stage 20B их правит
  админ места (или суперадмин) через кабинет: ввод и подтверждение, атомарная
  запись и строка аудита `place_edit_log`; пустые значения запрещены,
  отсутствие = `NULL`. У адреса **нет искусственного предела** длины:
  значение сохраняется полностью, а длинный показ ограничен явным
  фрагментом с пометкой «Полное значение сохранено». Ссылка ограничена
  500 символами интерфейса. `comment`, `is_active`, `updated_at` остаются
  будущими колонками.
- Каноническое название переименовывается админом места (или суперадмином,
  Stage 20B): это UPDATE строки с флагом, а не создание нового места;
  старое название остаётся альтернативным для поиска.
- Поиск, подсказка при создании (`«Такое место уже есть?»`), альтернативные
  названия и явное подтверждение реализованы в Stage 19B
  (`handlers/places.py` + `place_store.py`). Похожесть не сливает места:
  решение всегда за пользователем.
- `places.created_by_user_id` — только авторство. Создатель не получает
  администратора, шаблон или иные скрытые права.

### 3.3. `place_admins` — администраторы мест

**Stage 20A — реализовано** (миграция 3):

```sql
CREATE TABLE place_admins (
    id                 INTEGER NOT NULL PRIMARY KEY,
    place_id           INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    assigned_by_user_id INTEGER,
    assigned_at        DATETIME NOT NULL,
    revoked_by_user_id INTEGER,
    revoked_at         DATETIME,
    CONSTRAINT fk_place_admins_place_id
        FOREIGN KEY (place_id) REFERENCES places (id) ON DELETE CASCADE,
    CONSTRAINT fk_place_admins_user_id
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_place_admins_assigned_by_user_id
        FOREIGN KEY (assigned_by_user_id) REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT fk_place_admins_revoked_by_user_id
        FOREIGN KEY (revoked_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE INDEX ix_place_admins_place_id ON place_admins (place_id);
CREATE INDEX ix_place_admins_user_id ON place_admins (user_id);
CREATE UNIQUE INDEX ux_place_admins_one_active
    ON place_admins (place_id) WHERE revoked_at IS NULL;
```

- Таблица — **история назначений**: действующая роль = строка с
  `revoked_at IS NULL`; отозванные строки остаются для аудита (кто, кого,
  когда назначил и снял).
- Частичный уникальный индекс `ux_place_admins_one_active` гарантирует на
  уровне БД «не более одного действующего администратора на место»; у места
  может быть **ноль или один** действующий админ.
- При создании места строка **не создаётся** (админа нет); суперадмин
  назначает его позже по постоянному Telegram user ID. Создатель места
  автоматически админом не становится.
- Замена администратора — атомарная транзакция: UPDATE старой строки
  (условный по `revoked_at IS NULL`, проверяется `rowcount == 1`) + INSERT
  новой; конкурентные операции защищены индексом и проверкой ожидаемого
  состояния (`expected_admin_telegram_user_id`), без молчаливой подмены.
- `user_id`/`assigned_by_user_id`/`revoked_by_user_id` — внутренние
  `users.id`; связь с Telegram идёт через `users.telegram_user_id`, поэтому
  смена username не меняет права.

### 3.3а. `admin_sessions` — состояние кабинета

**Stage 20A — реализовано** (миграция 3):

```sql
CREATE TABLE admin_sessions (
    telegram_user_id INTEGER NOT NULL PRIMARY KEY,
    payload TEXT NOT NULL,
    updated_at DATETIME NOT NULL
);
```

- Интерфейсное состояние кабинета: фаза, выбранное место, кандидат,
  ожидаемый текущий админ — переживает рестарт (см. `admin_state.py`).
- Роли здесь **не хранятся**: права перепроверяются по конфигурации
  суперадминов и таблице `place_admins` при каждом действии; JSON — только
  навигация, а не источник полномочий.

### 3.3б. `place_edit_log` — аудит редактирования места

**Stage 20B — реализовано** (миграция 4):

```sql
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
);
CREATE INDEX ix_place_edit_log_place_id ON place_edit_log (place_id);
```

- Одна строка аудита на **успешную** операцию редактирования места
  (`place_edit_store.py`): `action` — `rename_canonical`, `alias_add`,
  `alias_rename`, `alias_delete`, `address_set`, `address_delete`,
  `url_set`, `url_delete`.
- `old_value`/`new_value` — значения поля до и после; для переименования
  канонического названия в `new_value` попадает новое название, старое
  остаётся альтернативным. Попытки, отклонённые правами/дубликатом/гонкой,
  в журнале не фиксируются.
- `actor_user_id` — внутренний `users.id` редактора; `ON DELETE SET NULL`
  сохраняет строку аудита даже при удалении пользователя.
- `place_id` — `ON DELETE CASCADE`: журнал места уходит вместе с местом.
- Мутация и строка аудита пишутся в одной транзакции; оптимистичная
  блокировка по ожидаемому старому значению защищает от потери изменений
  при параллельном редактировании.

### 3.4. `bricks` — реестр кирпичиков (Stage 22A, управление — Stage 22B)

```sql
CREATE TABLE bricks (
    id            INTEGER NOT NULL PRIMARY KEY,
    code          TEXT NOT NULL,     -- как пишут в шаблоне: [ссылка_места]
    title         TEXT NOT NULL,     -- человекочитаемое: «Ссылка места»
    type          TEXT NOT NULL,     -- short_text|long_text|date|time|url|number|choice
    rules_json    TEXT,              -- строгий JSON-объект правил типа (§3.5)
    source        TEXT NOT NULL,     -- ask|place|context: откуда берётся значение
    scope         TEXT NOT NULL,     -- system|place|personal
    place_id      INTEGER,
    owner_user_id INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',  -- active|archived
    revision      INTEGER NOT NULL DEFAULT 1,      -- Stage 22B, оптимистичная блокировка
    created_by_user_id INTEGER,
    created_at    DATETIME NOT NULL,
    updated_at    DATETIME,
    CONSTRAINT ck_bricks_scope_owner CHECK (
        (scope = 'system'   AND place_id IS NULL AND owner_user_id IS NULL)
        OR (scope = 'place'    AND place_id IS NOT NULL AND owner_user_id IS NULL)
        OR (scope = 'personal' AND owner_user_id IS NOT NULL AND place_id IS NULL)
    ),
    CONSTRAINT ck_bricks_type CHECK (type IN (
        'short_text', 'long_text', 'date', 'time', 'url', 'number', 'choice'
    )),
    CONSTRAINT ck_bricks_source CHECK (source IN ('ask', 'place', 'context')),
    CONSTRAINT ck_bricks_status CHECK (status IN ('active', 'archived')),
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
    FOREIGN KEY (place_id) REFERENCES places (id) ON DELETE CASCADE,
    FOREIGN KEY (owner_user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX ux_bricks_system_code   ON bricks(code) WHERE scope = 'system';
CREATE UNIQUE INDEX ux_bricks_place_code    ON bricks(place_id, code) WHERE scope = 'place';
CREATE UNIQUE INDEX ux_bricks_personal_code ON bricks(owner_user_id, code) WHERE scope = 'personal';
```

- **Реализованный объём.** Миграция 6 создаёт таблицу, девять системных
  строк с ID 1–9, индексы доступности/поиска
  (`scope,status,code`, `place_id,status`, `owner_user_id,status`,
  `type,status`, `code`, `title`) и триггеры защиты. Сервис
  `brick_store.py` работает без Telegram; `brick_types.py` содержит каталог
  типов и чистые проверки. Stage 22B добавил `revision` и
  `brick_sessions`; шаблоны/парсер — Stage 23–25, предложения — Stage 26,
  новый мастер — Stage 27A. `draft_values` не создавался.
- **Устойчивость идентификаторов.** `bricks.id` постоянен: триггер
  `trg_bricks_id_immutable` запрещает `UPDATE id`. Шаблоны и
  `announcement_values` ссылаются на `id`, а `code` нужен только в тексте
  шаблона и сообщениях об ошибках. Код системного кирпичика и его глобальная
  принадлежность также неизменяемы; системную строку можно архивировать,
  но нельзя удалить.
- **Системные коды непереприсваиваемы.** Девять кодов фиксируются
  миграцией (Приложение А): `название`, `дата`, `время_начала`,
  `время_окончания`, `место`, `адрес_места`, `ссылка_места`, `описание`,
  `контакт`. Триггеры `trg_bricks_reserve_codes_*` запрещают сосуществование
  системного и несистемного кода в любом направлении, даже для архивной
  системной строки. Создание системного кирпичика сервисом — только
  суперадмин.
- **Технический код.** NFKC, trim, casefold; Unicode-буквы/цифры/`_`/`-`;
  2–64 символа; хотя бы одна буква или цифра; `фото` запрещён. Это
  ограничение **только токена**, не заголовка и не значения. Заголовки
  могут совпадать у разных кирпичиков.
- **Разрешение `[код]`.** Системный кирпичик → кирпичик выбранного места →
  личный кирпичик зрителя. Приоритет не зависит от порядка строк SQL.
  Частичные уникальные индексы разрешают одинаковые коды у разных
  пользователей/мест; системный код остаётся глобальным. Архивный
  системный код не «проваливается» в личный или местный.
- **Права и приватность.** Личный кирпичик создаётся только на актора;
  кирпичик места — админом этого места или суперадмином; системный —
  суперадмином. Чужие личные кирпичики не видны, не ищутся и не
  разрешаются. Проверки выполняются в сервисе, а ограничения владения и
  уникальности — ещё и реальными CHECK/UNIQUE/FK/триггерами SQLite.
- **Расширяемость.** Новый тип данных = изменение каталога `brick_types.py`
  и, при необходимости, новая миграция для ограничений. `rules_json` —
  данные, не исполняемый код и не SQL. Схема правил описана в §3.5.
- **Фотографии — не тип кирпичика.** В `bricks` нет и не будет типа
  `photo`/`media`, кода `фото` или токена `[фото]`: изображения —
  независимая коллекция вложений анонса (§7.5), они не вставляются в
  шапку/тело/подвал и не участвуют в текстовом конструкторе.

#### 3.4.1. `revision` и `brick_sessions` (Stage 22B)

```sql
ALTER TABLE bricks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE brick_sessions (
    telegram_user_id INTEGER PRIMARY KEY,
    payload          TEXT NOT NULL CHECK (
        json_valid(payload) AND json_type(payload) = 'object'
    ),
    updated_at       DATETIME NOT NULL
);
```

- **Ревизия.** `revision` — монотонный счётчик успешных правок, архива и
  восстановления. `brick_id` не меняется. Правка/архив/восстановление
  выполняются одним условным `UPDATE ... WHERE owner_user_id=? AND status=?
  AND revision=?`; если строка уже изменилась, сервис возвращает `stale`
  и не перезаписывает более новую версию. Триггер
  `trg_bricks_revision_increases` запрещает прямой SQL, уменьшающий или
  сохраняющий ревизию.
- **Состояние конструктора.** `brick_sessions` хранит только UI-фазу,
  страницы, черновик и `brick_id`/`revision`. Это не кирпичик и не значение
  анонса. Сессия переживает рестарт, но не является источником истины о
  сохранённых кирпичиках. `/start`, `/help`, главное меню, другие разделы,
  `/cancel` и вход в мастер поста очищают незавершённый черновик; сохранённые
  строки `bricks` не затрагиваются.
- **Границы.** `brick_sessions` не участвует в публикации, не заменяет WIP
  поста и не содержит `draft_values`. Заявки, шаблоны и места в Stage 22B
  не получают Telegram-управления кирпичиками.

#### 3.4.2. Контракт кодов и будущих шаблонов

- В тексте будущего шаблона остаётся `[код]`, но **связывание** шаблона с
  кирпичиком должно идти по стабильному `brick_id`, а не по заголовку или
  текущему отображаемому коду. Заголовки могут совпадать; код можно
  изменить, пока кирпичик активен.
- `resolve_brick_code_candidates(code, viewer, place_id)` возвращает все
  видимые кандидаты (системный → место → личный), выбранного по
  детерминированному runtime-приоритету и признак `ambiguous`. Будущий
  редактор шаблона обязан показать неоднозначность и сохранить `brick_id`;
  runtime не «угадывает» по заголовку.
- Архивный системный код не разрешается как личный/местный и помечается
  `blocked_by_archived_system`. Архивный несистемный код не попадает в
  активные кандидаты, но остаётся занятым для своего владельца/места:
  восстановление возвращает тот же `brick_id` и код, а новый кирпичик с
  этим кодом создать нельзя.
- Одинаковые коды разных владельцев/мест не конфликтуют; при выбранном
  месте приоритет у кирпичика места, затем у личного. Это совместимо с
  будущей привязкой по `brick_id`: шаблон хранит конкретную сущность, а не
  разрешает код заново при каждом рендере.

### 3.5. `rules_json` (типизированные правила)

| Тип | Ключи `rules_json` | Интерфейс ввода (будущий) |
|---|---|---|
| `short_text` | (по умолчанию — нет; см. примечание) | текстовое сообщение |
| `long_text` | (по умолчанию — нет; см. примечание) | текст, форматирование Telegram допустимо |
| `date` | `horizon_months` (сегодня + 12 мес), `min_date`, `max_date` | текст или календарь Stage 17 |
| `time` | — | текст (`1830`, «полседьмого», …) |
| `url` | — | текст; нормализация `normalize_url` |
| `number` | `min`, `max`, `integer`, `suffix` («₽») | текст |
| `choice` | `choices` (непустой список допустимых) + `multiple` (bool) | inline-клавиатура |

- **Строгий JSON-объект.** `rules_json` может быть `NULL` или JSON-объектом;
  неизвестные ключи, массивы, скаляры, невалидный JSON и значения неверного
  типа отклоняются. Это данные для проверки/интерфейса, а не код, SQL или
  исполняемое выражение.
- **Фотографии в этой таблице отсутствуют принципиально:** это не тип
  значения кирпичика. Приём изображений, альбомов и медиагрупп — отдельный
  существующий поток вложений анонса (§7.5), не настраиваемый шаблоном.
- Правила — данные, не код: добавление кирпичика не требует миграций.
- **Искусственных лимитов длины значений нет и не появится** (правило
  Stage 14: без молчаливых обрезаний; реальные ограничения — только
  платформенные, §10.1). Ограничен только технический `code` (2–64).
  Если текст не помещается в платформенный лимит, публикация разбивает
  сообщения или явно сообщает об ошибке — без потери введённых данных.
- Источник `place` означает: значение берётся из выбранного места
  (`место` → название, `адрес_места` → адрес, `ссылка_места` → URL), и бот
  не спрашивает его повторно, если оно известно. Канонический `place_id`
  сохраняется вызывающим слоем; выбранное альтернативное название не
  создаёт новое место. Источник `ask` требует ввода, `context` зарезервирован
  для значений системного контекста; в сидированном наборе таких кирпичиков
  пока нет.

### 3.6. `templates`, версии, снимки и `template_sessions`

**Stage 23–25 — реализовано** (миграции 8, 9, 10). Фактическая схема
отличается от первоначального наброска: секции хранятся отдельными
колонками, а не `sections_json`; `revision` — и номер текущей версии, и
ожидаемая ревизия оптимистичной блокировки.

```sql
CREATE TABLE templates (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('standard','personal')),
    place_id          INTEGER REFERENCES places(id) ON DELETE CASCADE,
    owner_user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    name              TEXT,              -- только у личного шаблона
    revision          INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at        DATETIME NOT NULL,
    updated_at        DATETIME NOT NULL,
    CHECK ((kind = 'standard' AND place_id IS NOT NULL
            AND owner_user_id IS NULL AND name IS NULL)
        OR (kind = 'personal' AND place_id IS NULL
            AND owner_user_id IS NOT NULL AND length(trim(name)) > 0))
);
CREATE UNIQUE INDEX ux_templates_one_standard_place
    ON templates(place_id) WHERE kind = 'standard';
-- Триггеры: неизменяемость kind/place_id/owner_user_id, рост revision,
-- запрет удаления стандарта, пока существует место.

CREATE TABLE template_versions (
    id                INTEGER PRIMARY KEY,
    template_id       INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    version           INTEGER NOT NULL CHECK (version >= 1),
    header            TEXT NOT NULL,
    body              TEXT NOT NULL,
    footer            TEXT NOT NULL,
    created_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at        DATETIME NOT NULL,
    builtin_version   INTEGER,           -- заполнен у сброса/первичной v1
    UNIQUE (template_id, version),
    CHECK (header <> '' OR body <> '' OR footer <> '')  -- не все пусты
);
-- Триггеры запрещают UPDATE/DELETE версии: правка добавляет новую.

CREATE TABLE template_version_bricks (
    template_version_id INTEGER NOT NULL REFERENCES template_versions(id)
        ON DELETE CASCADE,
    brick_id            INTEGER NOT NULL REFERENCES bricks(id)
        ON DELETE RESTRICT,
    code                TEXT NOT NULL DEFAULT '',  -- миграция 9
    PRIMARY KEY (template_version_id, brick_id),
    UNIQUE (template_version_id, code)
);

CREATE TABLE template_snapshots (        -- самостоятельная копия версии
    id, owner_user_id, source_template_id, source_template_version,
    source_kind, source_name, source_place_id, source_owner_user_id,
    header, body, footer, builtin_version, idempotency_key, created_at
);
CREATE TABLE template_snapshot_bricks (
    template_snapshot_id INTEGER NOT NULL,
    brick_id             INTEGER NOT NULL,
    code                 TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (template_snapshot_id, brick_id),
    UNIQUE (template_snapshot_id, code)
);
-- Обе таблицы снимков неизменяемы (триггеры UPDATE/DELETE).

CREATE TABLE template_sessions (         -- миграция 10, Stage 25
    telegram_user_id INTEGER PRIMARY KEY,
    payload          TEXT NOT NULL CHECK (
        json_valid(payload) AND json_type(payload) = 'object'
    ),
    updated_at       DATETIME NOT NULL
);
```

- **Стандартный шаблон** принадлежит конкретному месту: ровно одна строка
  `kind='standard'` на место. v1 создаётся вместе с местом и бэкфиллится
  миграцией 8 для существующих мест; встроенный стандарт содержит
  `[название]`, дату/время, место/адрес, описание и ссылку/контакт.
- **Личный шаблон** принадлежит исключительно создателю
  (`owner_user_id`); другие пользователи, включая админов места и
  суперадмина, не открывают его через личный раздел. Публикация и
  совместное использование не входят в roadmap 19–28.
- **Привязки** — постоянные `brick_id`, а не повторный поиск по названию.
  Миграция 9 добавила `code` в привязки версии и снимка: рендер
  восстанавливает конкретный `brick_id` по сохранённому коду, поэтому
  переименование кирпичика после сохранения не подменяет значение.
  Внутри одной версии/снимка код уникален.
- **История неизменяема.** Правка секции создаёт следующую версию и
  увеличивает `templates.revision`; «Сбросить к стандартному» тоже
  добавляет версию с `builtin_version=1`, не изменяя старые. Снимки
  копируют все три секции и привязки и не связаны с легаси-`drafts`.
- **Секции содержат только текст**; вложения анонса в шаблон и версии не
  входят. Полностью пустая версия запрещена CHECK; поэтому «Пустой
  шаблон» в UI получает минимальное тело `[название]`.
- **`template_sessions`** — только интерфейсная навигация Stage 25:
  фаза, ID шаблона, секция, исходная revision, черновой текст, имя при
  создании и выбранные `brick_id`. Права в ней не хранятся и
  перепроверяются сервисом.

### 3.7. `brick_proposals` — согласование кирпичиков

**Stage 26 — реализовано** (миграция 11):

```sql
CREATE TABLE brick_proposals (
    id                 INTEGER PRIMARY KEY,
    author_user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    scope              TEXT NOT NULL CHECK (scope IN ('place','system')),
    place_id           INTEGER REFERENCES places(id) ON DELETE RESTRICT,
    source_brick_id    INTEGER NOT NULL REFERENCES bricks(id) ON DELETE RESTRICT,
    title              TEXT NOT NULL,
    code               TEXT NOT NULL,
    type               TEXT NOT NULL,
    source             TEXT NOT NULL,
    rules_json         TEXT,
    status             TEXT NOT NULL DEFAULT 'pending',
                       -- pending|approved|rejected|withdrawn
    created_at         DATETIME NOT NULL,
    decided_at         DATETIME,
    decided_by_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    approved_brick_id  INTEGER REFERENCES bricks(id) ON DELETE RESTRICT,
    decision_note      TEXT,
    CHECK ((scope='place' AND place_id IS NOT NULL)
        OR (scope='system' AND place_id IS NULL))
);
```

- **Не заявка на анонс.** Согласуется новый кирпичик; исходный
  `source_brick_id` — действующий личный кирпичик автора.
- **Снимок неизменяем.** `title`, `code`, `type`, `source`, `rules_json`,
  `source_brick_id`, автор, область, `place_id` и `created_at` фиксируются
  при отправке. Правка или архивация личного кирпичика после этого снимок
  не меняет.
- **Срок не ограничен.** `expires_at`/TTL нет; `pending` существует до
  решения или отзыва. Автоматического `expired` нет.
- **Ровно четыре статуса:** `pending`, `approved`, `rejected`, `withdrawn`.
  Повторное предложение после отказа/отзыва — **новая строка**, без
  `revises_id`; прежняя остаётся в истории. `superseded` у предложений
  кирпичиков не используется (этот статус относится к передачам админки).
- **История не удаляется.** Триггеры запрещают DELETE и изменение снимка;
  переход разрешён только из `pending`; терминальные поля аудита
  неизменяемы.
- **Одобрение.** Отдельный `Brick` создаётся в целевой области с новым
  `brick_id`; ссылка `approved_brick_id` проверяется триггером на совпадение
  области, места, кода, названия, типа, источника и правил. Исходный личный
  кирпичик и привязки шаблонов не перебинживаются.
- **Транзакция.** Решение выполняется через `BEGIN IMMEDIATE`, повторную
  проверку актора/прав/области/кода и условный
  `UPDATE ... WHERE status='pending'`. Проигравшая гонка не оставляет
  второго кирпичика и не меняет терминальный статус.

### 3.8. `admin_transfers` — передача админки

**Stage 21 — реализовано** (миграция 5):

```sql
CREATE TABLE admin_transfers (
    id                 INTEGER PRIMARY KEY,
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

- Механика — §5.5. Роль переходит только после **двусторонней** привязки:
  инициатор утверждает конкретного получателя (`to_user_id`), получатель
  подтверждает в своём чате; до этого прежний админ сохраняет полномочия.
  Наличие токена/открытие ссылки само по себе прав не даёт.
- `from_assignment_id` фиксирует назначение, с которого начата передача:
  после замены/отзыва администратора предложение становится `superseded`,
  даже если прежнего админа назначили снова.
- `to_user_id` — внутренний `users.id`; Telegram user ID используется только
  при поиске/назначении. `opened_by_user_id` фиксирует, кто открыл
  приглашение, и служит лишь кандидатом для утверждения инициатором.
- Сырой токен (`secrets.token_urlsafe(32)`) хранится только в ссылке;
  в БД — SHA-256. Частичный уникальный индекс допускает одно `pending`
  предложение на место; закрытые строки остаются историей.
- `notified_at` заполняется только после успешного ответа Telegram API;
  NULL — доставка не подтверждена и показывается инициатору явно.
- `closed_at`/`confirmed_at` фиксируют завершение; `rejected`/`cancelled`/
  `expired`/`superseded` права не меняют.

### 3.9. Черновики: легаси и `announcement_*` (Stage 27A/27C/28)

**Фактическая схема** — миграции 12 (Stage 27A) и 13 (Stage 27C); миграция
14 (Stage 28) меняет только триггер неизменяемости. Полный SQL, индексы и
триггеры — в [`docs/migrations.md`](migrations.md), разделы «Версия
12»–«Версия 14». Старые `drafts`/`user_states` не получают колонок
и не конвертируются; `draft_values` из ранних планов не создавался.

```sql
CREATE TABLE announcement_drafts (
    id                   INTEGER PRIMARY KEY,
    author_user_id       INTEGER NOT NULL REFERENCES users(id),
    place_id             INTEGER NOT NULL REFERENCES places(id),
    place_name_id        INTEGER REFERENCES place_names(id),
    place_display_name   TEXT NOT NULL,   -- снимок выбранного названия
    place_address        TEXT,            -- NULL = адрес неизвестен
    place_url            TEXT,            -- NULL = ссылка неизвестна
    template_id          INTEGER REFERENCES templates(id),
    template_version     INTEGER,
    template_snapshot_id INTEGER REFERENCES template_snapshots(id),
    stage                TEXT NOT NULL DEFAULT 'place',
    revision             INTEGER NOT NULL DEFAULT 1,
    idempotency_key      TEXT,
    created_at           DATETIME NOT NULL,
    updated_at           DATETIME NOT NULL
);

CREATE TABLE announcement_values (
    id         INTEGER PRIMARY KEY,
    draft_id   INTEGER NOT NULL REFERENCES announcement_drafts(id)
               ON DELETE CASCADE,
    brick_id   INTEGER NOT NULL REFERENCES bricks(id),
    state      TEXT NOT NULL,   -- 'valid' | 'empty_optional'
    value_json TEXT,            -- JSON-объект только для 'valid'
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (draft_id, brick_id)
);

CREATE TABLE announcement_extras (
    id         INTEGER PRIMARY KEY,
    draft_id   INTEGER NOT NULL REFERENCES announcement_drafts(id)
               ON DELETE CASCADE,
    brick_id   INTEGER NOT NULL REFERENCES bricks(id),
    section    TEXT NOT NULL,   -- 'header' | 'body' | 'footer'
    position   TEXT NOT NULL DEFAULT 'end',  -- 'start' | 'end'
    occurrence INTEGER NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (draft_id, brick_id, occurrence)
);
```

- **Место.** `place_id` — постоянная ссылка, `place_name_id` — выбранный
  канонический/альтернативный вариант, `place_display_name` — его снимок.
  Альтернатива не создаёт место и не меняет каноническое название.
  `place_address`/`place_url` — снимок известных данных справочника; `NULL`
  означает отсутствие, а не пустую строку/заглушку.
- **Шаблон.** Тройка `template_id`/`template_version`/
  `template_snapshot_id` заполняется вместе либо не заполняется вовсе
  (CHECK). Снимок из `template_snapshots` создаётся в той же транзакции,
  уникален для анонса и не меняется после привязки.
- **Значения.** Нет строки — значение не задано; `valid` — нормализованный
  payload; `empty_optional` — явно допустимый пропуск. Определение,
  валидаторы и правила читаются по `brick_id` из реестра Stage 22A.
- **Дополнения.** `announcement_extras` — вставка в шапку/тело/подвал
  только этого анонса; не меняет шаблон, снимок или другие черновики.
- **Этапы и locking.** `stage`: `place`, `template`, `questions`, `extras`,
  `preview`, `completed`. `revision` монотонно растёт; сервис обновляет
  строку условным `UPDATE ... WHERE revision = :expected` и при конфликте
  не меняет данные. Триггеры дополнительно запрещают менять автора, место,
  ключ идемпотентности, снимок шаблона и идентичность значений/дополнений.
- **Изоляция.** Операции ищут анонс по `author_user_id`; чужой ID даёт
  `AnnouncementNotFoundError`, не раскрывая существование. `ON DELETE
  CASCADE` очищает значения/дополнения только вместе с самим анонсом.
- **Фотографии.** Не входят в `announcement_values`/`extras`: у легаси
  черновика остаются `Draft.image`/`Draft.photos`, у нового мастера —
  отдельный `announcement_drafts.photos`; публикация замораживает копию
  в `publication_photos`.
- **Публикация.** `publication_status` (`sending`/`partial`/`failed`/
  `sent`), `publication_text`, `publication_photos`,
  `publication_progress` (`fingerprint/step/ids/total`),
  `publication_error`, `publication_started_at`, `published_at`.
  CHECK/триггеры проверяют JSON-типы, форму статуса и неизменяемость
  подтверждённого payload; сервис дополнительно запрещает правку
  содержимого после начала отправки и сброс после частичного успеха.
  Адреса текущей доставки (`ids`) и заменяемой версии (`previous_ids`)
  живут в `publication_progress` рядом с отпечатком содержимого —
  отдельных `delivered_*`-колонок и «истории версий» нет.

**Будущий `draft_values`.** Ранний проект предполагал добавить
`draft_values` и `payload_level` прямо в `drafts`. Stage 27A от него
отказался: старый формат остаётся нетронутым, а новый мастер использует
`announcement_*`. Если будущий этап решит конвертировать легаси-черновик,
это будет явная отдельная операция, а не автоматическая миграция.

### 3.10. `schema_migrations`

**Реализовано в Stage 19A–22B** (`migrations.py`; подробно —
[`docs/migrations.md`](migrations.md)):

```sql
CREATE TABLE schema_migrations (
    version    INTEGER PRIMARY KEY,   -- 1, 2, 3, ... (непрерывно)
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,         -- sha256 нормализованного DDL
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

- Версия 1 — `users_places_place_names`: три таблицы и их индексы.
- Версия 2 — `place_contacts_and_ui_state`: `places.address/url` и
  `place_sessions` (Stage 19B).
- Версия 3 — `place_admins_and_admin_ui_state`: `place_admins` и
  `admin_sessions` (Stage 20A).
- Версия 4 — `place_edit_log`: аудит правок места (Stage 20B).
- Версия 5 — `admin_transfers`: предложения передачи админки, хеш токена,
  одно `pending` на место (Stage 21).
- Версия 6 — `bricks_registry`: таблица `bricks`, девять системных строк,
  индексы и триггеры защиты (Stage 22A).
- Версия 7 — `brick_management`: `bricks.revision`, `brick_sessions`
  и триггер монотонной ревизии (Stage 22B).
- Повторный запуск видит прежние версии и ничего не применяет; миграции 6–7
  при этом не перезаписывают контролируемые изменения системных заголовков,
  правил и пользовательские кирпичики.
- Неизвестная/более новая версия, разрыв журнала или изменённая
  контрольная сумма = `IncompatibleSchemaError`; база не меняется.
- Все ожидающие миграции применяются одной транзакцией `BEGIN IMMEDIATE`;
  сбой откатывает весь запуск. Конкурентный старт ждёт блокировку и видит
  уже применённые версии.
- Перед первым ожидающим DDL для существующей непустой базы автоматически
  создаётся WAL-aware копия (SQLite Backup API, `integrity_check`, права
  `0600`, атомарное имя). При сбое копии миграция не начинается.
- Механизм требует файловую SQLite (не `:memory:`). Применение к настоящей
  `post_bot.db` — только осознанно, после эксплуатационной резервной копии
  (runbook в `docs/migrations.md`); тесты используют временные файлы.
- Alembic сознательно не вводится: одна SQLite-база, только добавочный DDL,
  нет autogenerate-потребности; свой механизм — ~630 строк с журналом,
  суммами и транзакциями. Решение и ограничения описаны в docs/migrations.md.

---

## 4. Ролевая модель и матрица прав

### 4.1. Роли

| Роль | Определение | Где хранится |
|---|---|---|
| Суперадмин | Постоянный Telegram **user id** из конфигурации `.env` (`SUPERADMIN_USER_IDS`), не username | только env, `permissions.is_superadmin` (Stage 20A; DB/кнопками не назначается) |
| Администратор места | **Ноль или один** действующий админ на место, активная строка в `place_admins` (`revoked_at IS NULL`); назначается суперадмином по Telegram user ID | БД (Stage 20A) |
| Обычный пользователь | Любой, кто взаимодействует с ботом; права определяются отсутствием двух ролей выше | — |

### 4.2. Матрица прав (бизнес-операции)

| Операция | Суперадмин | Админ места | Пользователь |
|---|:---:|:---:|:---:|
| Создавать анонсы, черновики, личные шаблоны, личные кирпичики | ✓ | ✓ | ✓ |
| Добавлять новое место (после самопроверки дублей) | ✓ | ✓ | ✓ |
| Добавлять альтернативное название месту | ✓ | ✓ | ✓ |
| Редактировать/удалять альтернативные названия | любое | только своего | — |
| Переименовать каноническое название места | ✓ | своего | — |
| Редактировать адрес/ссылку места | ✓ | своего | — |
| Редактировать стандартный шаблон места | ✓ | своего | — |
| Удалить место целиком | ✓ (скрыть) | — | — |
| Назначить/заменить администратора места | ✓ | — | — |
| Передать админку другому пользователю | ✓ | своего (без суперадмина) | — |
| Рассматривать предложения кирпичиков к месту (Stage 26) | ✓ | своего | — |
| Рассматривать глобальные предложения кирпичиков (Stage 26) | ✓ | — | — |
| Предлагать свой личный кирпичик месту/системе | ✓ | ✓ | ✓ |
| Создавать/редактировать/архивировать/восстанавливать личные кирпичики | только свои | только свои | только свои |
| Просматривать чужие личные кирпичики | — | — | — |
| Искать/просматривать справочник мест | ✓ | ✓ | ✓ |
| Кнопка «Я админ места» | — | — | ✓ (показ контакта суперадмина; прав не даёт) |

Правила реализации (Stage 20A):

1. Проверка прав — **функцией бизнес-логики** (`permissions.py`,
   `place_admin_store.py`), вызываемой в каждом административном действии и
   перепроверяемой внутри транзакций store-слоя, плюс ограничениями самой БД
   (`ON DELETE`, частичный уникальный индекс). Видимость Telegram-кнопки —
   только удобство, не граница безопасности.
2. Актор — только `update.effective_user.id`; callback передаёт лишь
   `place_id`/`telegram_user_id` цели, никогда роль или ID актора. Все
   обращения к чужим сущностям перепроверяются; подделанный callback получает
   отказ, не меняя данных.
3. Кнопка «Я админ места» показывает контакт суперадмина (ссылка из
   `.env: SUPERADMIN_CONTACT_URL`, валидируется при старте; если не задана —
   честно сообщает «контакт не указан»). Нажатие не выдаёт полномочий, не
   создаёт заявку и не меняет ролей — обращение к суперадмину пользователь
   делает сам.
4. Суперадмин назначает администратора места **по постоянному Telegram user
   ID** (не по username); получатель должен быть уже известен боту. Создатель
   места администратором автоматически не становится. У места может не быть
   администратора вовсе.
5. Права проверяются на уровне бизнес-логики и ограничений БД, а не только
   видимостью кнопок: callback с чужим `place_id` должен получить отказ
   независимо от того, как выглядел экран.
6. Конкуренция: назначение опирается на частичный уникальный индекс, замена
   и отзыв — на условный UPDATE с проверкой ожидаемого действующего админа;
   параллельные операции дают честный отказ (`changed`/`has_admin`/
   `conflict`), а не порчу аудита.

---

## 5. Жизненные циклы

Ниже — жизненные циклы. В Stage 19A реализованы схема
`users`/`places`/`place_names` и слой создания; в Stage 19B — Telegram-флоу
поиска, создания и выбора места, альтернативные названия и подтверждение;
в Stage 20A/20B — роли, кабинет и редактирование данных; в Stage 21 —
двусторонняя передача админки; в Stage 22A — реестр `bricks` и сервис
создания/чтения, в Stage 22B — Telegram-управление личными кирпичиками;
в Stage 23–25 — шаблоны, парсер/рендер и Telegram-редактор шаблонов.
Stage 27A добавил сервис нового адаптивного мастера, Stage 27B — его
Telegram-UI, Stage 27C — отдельные фото и явную публикацию. Полный интерфейс
кирпичиков места и Mini App — будущие этапы; предложения кирпичиков
реализованы в Stage 26.

### 5.1. Место

```
[пользователь ввёл название]
        │ поиск похожих (§5.6)
        ├── нашлись похожие ──► карточки кандидатов
        │        │ «Подтвердить»            → выбран существующий place_id
        │        │ «Выбрать альтернативное» → alt-имя существующего места
        │        │ «Вернуться к списку»     → повторный выбор
        │        └ «Создать новое место»    ▼
        └── пусто/не то ──────────► [подтверждение пользователем]
                                       «Создать новое место "…"»
                                            │
                                            ▼
        ACTIVE ─────────────────────────────────────────────────────┐
          • alt-имена: добавляют все, правят/удаляют админ/суперадмин│
          • стандартный шаблон создаётся вместе с местом (Stage 23)  │
          • админ назначается суперадмином (Stage 20)                │
          • переименование/правка адреса — админ или суперадмин      │
        ARCHIVED (только суперадмин, «скрыть»): старые черновики      │
        и посты остаются читаемыми ◄─────────────────────────────────┘
```

- Пользователь, добавивший место, **не** становится его админом (прямое
  требование ТЗ); `places.created_by_user_id` — только служебная информация.
- В Stage 19B шаги подтверждения/альтернативы и создание после проверки
  дублей уже работают; переименование и контакты — Stage 20B (кабинет,
  аудит `place_edit_log`), слияние дублей — будущий этап.
- Неявного слияния мест нет; слияние — отдельная осознанная операция
  администратора (рекомендация §13.2).
- Фотографии не участвуют в жизненном цикле места: это вложения анонса,
  а не свойство места или шаблона.

### 5.2. Шаблон

```
PLACE (стандартный):  создаётся вместе с местом (v1) и бэкфиллится
                       миграцией 8 для существующих мест
   ACTIVE ──правка секции админом/суперадмином──► новая версия vN
        │ «Сбросить к стандартному» → подтверждение → весь seeded-текст vN+1
PERSONAL (личный):     создаётся владельцем (копия стандарта или «пустой»)
   ACTIVE ──правка владельцем──► новая версия
        │ применяется к выбранному месту в Stage 27B
        │ виден и доступен ТОЛЬКО владельцу; шаринг/публикация не входят
        │ в roadmap 19–28
        │ «Сбросить к стандартному» — весь личный шаблон возвращается
        │ к встроенному стандарту (не к первой версии пользователя)
ARCHIVED: старые версии доступны черновикам через снимок (§9)
```

- Стандартный шаблон принадлежит конкретному месту; личный — конкретному
  пользователю. Никаких «общих» или публикуемых личных шаблонов нет.
- Сброс реализован в Stage 25 для всего шаблона, а не для отдельной секции:
  сервис создаёт новую версию с встроенным текстом, историю и снимки не
  трогает. Требуется явное подтверждение; права перепроверяются.
- Фотографии не являются частью шаблона и не версионируются вместе с ним.

### 5.3. Кирпичик

```
SYSTEM: сидируется миграцией 6 с фиксированными ID 1–9 и кодами
        (Приложение А); код и владение неизменяемы, системную строку
        нельзя удалить, только архивировать
PERSONAL: create_personal_brick → ACTIVE (owner_user_id = актор)
PLACE:  create_place_brick админом места/суперадмином → scope='place'
GLOBAL: create_system_brick только суперадмином → новый scope='system',
        автор фиксируется в created_by_user_id; код резервируется навсегда
ARCHIVED: не попадает в выдачу/поиск по умолчанию; системный код остаётся
        зарезервированным, начатые анонсы (Stage 27A) рендерятся по снимку
```

- В Stage 22A реализованы прямые сервисные операции создания; Stage 22B
  добавил личный Telegram-конструктор: все семь типов, правку названия/кода/
  правил, мягкий архив и восстановление. Тип после создания неизменяем;
  `brick_id` стабилен, архивный код остаётся занятым, физического удаления
  из личного UI нет.
- Все операции личного раздела проверяют владельца в сервисе: роль админа
  места или суперадмина не открывает чужие личные кирпичики. Архив и
  восстановление идемпотентны; повторные/устаревшие callback возвращают
  понятный статус и не меняют данные.
- Предложение личного кирпичика месту/глобально (`brick_proposals`) —
  Stage 26; сервис и UI не меняют исходный кирпичик.
- Значения кирпичиков нового мастера живут в `announcement_values` и связаны
  с `announcement_drafts` (Stage 27A); шаблоны, версии, снимки и парсер
  `[код]` реализованы в Stage 23–25. Текущие `drafts`/WIP не мигрируются,
  `draft_values` не создавался.

### 5.4. Предложение кирпичика

```
PENDING ──одобрено──► APPROVED (отдельный Brick создан той же транзакцией)
   │  ──отклонено───► REJECTED (причина хранится в proposal; UI её не требует)
   │  ──отозвано─────► WITHDRAWN (автором, пока PENDING)
   └  после REJECTED/WITHDRAWN → новая строка предложения, без revises_id
```

- **Срока давности у предложений нет.** `PENDING` существует до решения
  администратора/суперадмина или отзыва автором; автоматического истечения
  через 24/30 дней, поля `expires_at`, фоновых проверок и статуса `EXPIRED`
  нет. Если у места нет администратора, предложение ждёт назначения.
- **Ровно четыре статуса:** `PENDING`, `APPROVED`, `REJECTED`, `WITHDRAWN`.
  `superseded` относится только к передачам админки. История предложений
  не удаляется; повторное предложение — новая запись.
- **Снимок.** Название, код, тип, правила, `source_brick_id`, автор, область,
  `place_id` и `created_at` неизменяемы. Правка/архив исходного личного
  кирпичика снимок не меняют.
- **Права.** Предложение места решает действующий админ этого места или
  суперадмин; глобальное — только суперадмин из `SUPERADMIN_USER_IDS`.
  Передача админки сохраняет предложения и меняет только актора,
  у которого есть доступ; открытая старая карточка не одобряет.
- **Конкуренция.** Перед решением берётся `BEGIN IMMEDIATE`, права/статус/
  область/код перепроверяются, затем выполняется условный
  `UPDATE ... WHERE status='pending'`. Проигравший не создаёт кирпичик;
  повторный/stale callback не меняет терминальный статус.
- **Одобрение.** В одной транзакции создаётся отдельный `Brick` в целевой
  области с новым `brick_id`; исходный личный кирпичик, его владелец, код и
  привязки шаблонов не меняются. Конфликт кода — понятная ошибка без
  переименования; при сбое `approved` не сохраняется.
- **Уведомления.** Очередь в существующем кабинете — первичный канал.
  После уже сохранённого решения автору best-effort отправляется личное
  сообщение; ошибка Telegram API не откатывает решение и отмечается как
  недоставленная.

### 5.5. Передача администрирования места (24 часа)

Общие правила:

- передаёт только действующий администратор **этого** места. Глобальная
  роль суперадминистратора сама по себе права передачи не даёт, хотя
  полномочия Stage 20A (назначить/заменить/снять) сохраняются;
- **до подтверждения права остаются у прежнего администратора**;
- получатель должен быть надёжно установлен. Одноразовый токен/ссылка
  доказывают лишь доступ к ссылке, но **не** личность и **не** совпадение с
  введённым `@username`;
- окончательное подтверждение делается в личном чате получателя; только
  после него одна транзакция атомарно меняет администратора;
- предложение действует ровно **24 часа** (`now < expires_at`); проверка
  ленивая при чтении/нажатии, планировщика/JobQueue нет;
- если надёжно установить адресата нельзя, автоматическая передача прав
  не выполняется. Бот не делает предположений о Telegram API, которых у него
  нет: username он знает только по ранее увиденным пользователям.

#### A. Получатель уже известен боту

```
[админ] «Передать админку» → вводит @username
   → поиск по users.username (последнее увиденное значение; это подсказка,
     а не идентификатор)
       • найден один/несколько кандидатов → админ явно выбирает карточку
         конкретного user_id (не просто подтверждает текст username)
       • никого → сценарий B
   → заявка PENDING с to_user_id = выбранный users.id
   → получателю в ЛИЧНЫЙ ЧАТ (он уже писал боту — Telegram это позволяет):
     «Администратор места … передаёт вам управление»
     [Подтверждаю] [Отклоняю]
```

Даже при известном username решает именно выбранный `user_id`: username
мог смениться или достаться другому человеку.

#### B. Получатель неизвестен боту

```
[админ] создаёт одноразовую ссылку-приглашение
   https://t.me/<bot>?start=tf_<transfer_id>_<token>
   (токен 32 байта; в БД только sha256; админ передаёт ссылку сам —
    бот не может писать незнакомому пользователю первым)
[получатель] открывает ссылку
   → бот фиксирует opened_by_user_id + opened_at
   → экран: «Приглашение открыто. Это ещё не передача: инициатор должен
     подтвердить получателя»
   → НИКАКИХ прав и НИКАКОГО to_user_id на этом шаге
[админ] видит: «Ссылку открыл пользователь X (id N).
                 [Утвердить получателем] [Отменить]»
   → только после явного утверждения to_user_id = opened_by_user_id
[получатель] в личном чате: [Подтверждаю] [Отклоняю]
```

Если ссылку открыл не тот человек, админ отменяет передачу и создаёт новую;
открытие ссылки само по себе никогда не назначает администратора.

#### Подтверждение получателя (реализовано в Stage 21)

```
[получатель] «Подтверждаю»:
   сессия SQLite, все проверки и записи до одного commit:
     1) actor telegram ID == users.id из to_user_id и == нажавший;
     2) transfer: status='pending' AND expires_at > now
                   AND to_user_id = нажавший;
     3) place_admins: строка from_assignment_id всё ещё активна
                      (revoked_at IS NULL), принадлежит from_user_id и месту;
     4) UPDATE admin_transfers SET status='confirmed',
        confirmed_at=now, closed_at=now
        WHERE id=? AND status='pending' AND to_user_id=?
        (rowcount == 1 — обязательный критерий);
     5) UPDATE place_admins SET revoked_at=now, revoked_by_user_id=to_user
        WHERE id=from_assignment_id AND revoked_at IS NULL
        (rowcount == 1 — обязательный критерий);
     6) INSERT нового place_admins (to_user_id, assigned_by=from_user_id);
     7) commit; при любой ошибке — rollback всех шагов.
   → прежний админ полностью теряет права места (активная строка одна)
[иначе] EXPIRED (лениво при чтении/нажатии, ровно 24 часа),
        CANCELLED (инициатором), REJECTED (получателем),
        SUPERSEDED (админ места заменён/снят; повторное назначение
        прежнего админа не оживляет предложение)
```

Реализация: `admin_transfer_store.py`; успешное подтверждение отправляет
обеим сторонам best-effort уведомление, но смена прав не зависит от
доставки этих сообщений. После рестарта весь прогресс восстанавливается из
`admin_transfers`, `admin_sessions` и `place_admins`.

Защиты (каждая — отдельный тест §11):

| Угроза | Механизм |
|---|---|
| Подтверждение посторонним | Кнопка только в чате `to_user_id`; проверка `to_user_id = нажавший` в транзакции |
| Токен-ссылка перехвачена | Открытие даёт только кандидата `opened_by_user_id`; права появляются лишь после утверждения инициатором и подтверждения получателем |
| Введён неверный/чужой username | Username — подсказка; инициатор выбирает конкретный `users.id`, получатель подтверждает |
| Смена username получателя | После фиксации `to_user_id` проверяется именно user_id, не username |
| Повторное подтверждение | `WHERE status='pending'` — второе нажатие видит `confirmed` и отвечает «уже подтверждено» |
| Конкурирующие передачи | Частичный уникальный индекс допускает одно `pending` на место; замена/снятие админа Stage 20A помечает предложение `superseded` в той же транзакции |
| Истечение суток | Проверка `expires_at` в самой транзакции подтверждения + ленивое истечение при чтении; ровно 24:00:00 уже недействительно |
| Устаревшие кнопки | Статус/срок перечитываются из БД, кнопки без актуального статуса дают честный ответ |
| Отзыв полномочий инициатора в ожидании | Шаг (3): передатчик и его assignment обязаны оставаться текущим активным админом |
| Подмена ID места | `place_id` берётся из строки предложения, а не из callback подтверждения |
| Неожиданный перезапуск | Всё состояние — в БД (`expires_at`, `opened_by_user_id`, статус); никакого состояния в памяти процесса |
| Роль переходит дважды | Активная строка `place_admins` одна; условные UPDATE с `rowcount==1`, откат всей транзакции при конфликте |

### 5.6. Нормализация и сравнение названий мест

- Нормализация (`name_search`): NFKC, casefold/нижний регистр, `ё→е`,
  пунктуация и пробельные символы заменяются пробелом, пробелы сжимаются,
  результат обязан быть непустым и содержать хотя бы один буквенно-цифровой
  символ. Хранится рядом с оригиналом, меняется вместе с ним. Примеры:
  `Кофейня «Дом»` → `кофейня дом`, `Ёлка-Кафе` → `елка кафе`; название
  целиком из пунктуации/эмодзи отклоняется.
- Нормализация реализована в Stage 19A
  (`place_store.normalize_place_name`) и используется ограничением
  `UNIQUE(place_id, name_search)`, поиском и проверкой конфликтов.
- Поиск Stage 19B: точное каноническое → точная альтернатива → частичное
  совпадение → похожее. Похожесть — максимум Jaccard по 3-граммам и
  `difflib.SequenceMatcher` при пороге 0.8; это ловит опечатки в коротких
  названиях и перестановки слов. Ранжирование и порог фиксируются тестами.
- Проверка альтернативы возвращает конфликты по другим местам; UI требует
  явного «Да, добавить» и никогда не сливает места молча.
- **Неявного слияния нет:** совпадение даёт только *подсказку* пользователю.
  Два разных места с похожими названиями существуют одновременно;
  слияние/присвоение альтернативного имени — только явное действие
  пользователя или администратора.

### 5.7. Повторные предложения кирпичиков

- После `REJECTED` или `WITHDRAWN` автор может отправить предложение снова.
  Это новая строка без `revises_id`; прежняя остаётся в истории и не
  переиспользуется. `superseded` для предложений не применяется.
- Если в целевой области уже есть кирпичик с таким кодом, новое
  предложение отклоняется на этапе проверки и не отправляется в очередь.
  Исключение — глобальное продвижение активного личного кирпичика: создаётся
  системный кирпичик с тем же кодом, исходный остаётся активным, а новые
  дубликаты кода блокируются.
- Системные коды зарезервированы; молчаливое переименование или создание
  дубля не выполняются. Старые версии/снимки шаблонов не перебинживаются.

---

## 6. Формат данных шаблонов и секций

### 6.1. Секции

Шаблон состоит из трёх независимо редактируемых секций: `header` (шапка),
`body` (тело), `footer` (подвал). Хранение — исходный текст в синтаксисе §7:

```json
{
  "schema": 1,
  "sections": {
    "header": "🎬 [название]\n[дата], [время_начала]",
    "body":   "[описание]",
    "footer": "📍 [место]([ссылка_места])\n[контакт]"
  }
}
```

- Порядок секций при рендере фиксирован: шапка → тело → подвал; внутри
  секции — как написано.
- Пустая секция — допустима (в анонсе не даёт строки).
- В секциях нет и не может быть узла/токена фотографий: изображения —
  отдельная коллекция вложений анонса (§7.5).

### 6.2. Что шаблон определяет

1. **Состав кирпичиков** анонса (все вхождения `[код]`).
2. **Откуда значение:** кирпичики с `source='place'` (`место`,
   `ссылка_места`, `адрес`) берутся из выбранного места; известные значения
   не спрашиваются повторно (§8.3).
3. **Что спросить у пользователя:** остальные кирпичики без значения.
4. **Фотографии шаблон не определяет.** Это независимая коллекция вложений
   конкретного анонса: она может быть пустой или содержать любое число
   фотографий/альбомов независимо от выбранного шаблона. Отсутствие
   изображений никогда не блокирует создание анонса. Текстовый рендер и
   сборка публикации с вложениями разделены (§7.5).

### 6.3. Значения кирпичиков (`payload_json`)

| Тип | Формат значения |
|---|---|
| `short_text`/`long_text` | `{"text": "…"}` |
| `date` | `{"date": "ДД.ММ.ГГГГ"}` |
| `time` | `{"time": "ЧЧ:ММ"}` |
| `url` | `{"url": "https://…"}` |
| `number` | `{"number": 350}` |
| `choice` | `{"choice": "Кино"}` / `{"choices": […]}` |

В Stage 27A `payload_json` хранится в `announcement_values.value_json`
(JSON-объект). Отдельного `formats_json` нет: значение хранится в
нормализованном виде и рендерится компилятором Stage 24; легаси
`Draft.formats` остаётся только у старого мастера.

Фотографии **не имеют** записи в `announcement_values`/`draft_values`: их
file_id остаются в существующей коллекции вложений анонса
(`Draft.image`/`Draft.photos` и WIP-снимок) и не смешиваются со значениями
текстовых кирпичиков.

### 6.4. «Стандартное содержимое» для кнопки «Сбросить к стандартному»

- Встроенный стандарт (`template_types.py`, `BUILTIN_VERSION=1`) — единый
  для стандартного и личного шаблонов: шапка `[название]`, тело с датой,
  временем, местом, адресом и описанием, подвал со ссылкой и контактом.
- **Стандартный шаблон места** при сбросе получает этот встроенный текст;
  новые места создаются сразу с ним, существующие бэкфиллятся миграцией 8.
  Сброс создаёт новую версию и не меняет историю.
- **Личный шаблон** при создании «На основе стандартного» копирует
  встроенный текст; «Пустой шаблон» получает минимальное тело
  `[название]`, потому что схема запрещает полностью пустую версию.
  «Сбросить к стандартному» возвращает весь личный шаблон к встроенному
  стандарту, а не к первой пользовательской версии.
- Сброс выполняется только после явного подтверждения, создаёт новую
  версию и не затрагивает снимки, историю и легаси-черновики. Фотографии
  в стандарт не входят.

---

## 7. Грамматика текстовых шаблонов и порядок обработки

### 7.1. Токены синтаксиса

| Запись | Значение |
|---|---|
| `обычный текст` | литеральный текст |
| `[код]` | ссылка на кирпичик (`код`: буквы/цифры/`_`/`-`, ≥2 знаков) |
| `текст(url)` или `[кирпичик](url)` | гиперссылка: label + цель |
| `\(` `\)` `\[` `\]` `\\` | литеральные скобки и обратный слэш |
| `*жирный*`, `_курсив_`, `` `моно` `` | минимальный Markdown (только если сообщение пришло БЕЗ entities) |

- Сообщение пользователя с Telegram-разметкой (entities из клиента) —
  первичный источник стилей: **entities приоритетнее маркеров Markdown**,
  маркеры в таком сообщении считаются текстом. Это устраняет двойную
  интерпретацию одного фрагмента.
- Скобки внутри `[…]`: код кирпичика не может содержать `]`, поэтому
  `[…]` не требует вложенного разбора.

### 7.2. Грамматика (EBNF-подобная запись)

```ebnf
section      = line_item* ;
line_item    = node | newline ;
node         = text | link | brick ;
link         = label , "(" , target , ")" ;
label        = text_with_escapes | brick ;
target       = url_literal | brick ;        (* [место]([ссылка_места]) *)
brick        = "[" , code , "]" ;
text_with_escapes = ( literal | escape )* ;
escape       = "\" , any_char ;
```

Ограничения (проверяются на этапе разбора):

1. `link.label` — не пустой; `link.target` — либо кирпичик типа `url`,
   либо литеральный URL, прошедший `normalize_url`.
2. Вложенная гиперссылка запрещена: `link.target` не может сам быть
   `LinkNode`; `[a]([b](c))` — ошибка «ссылка не может быть вложена в ссылку».
3. Ссылка на несуществующий/архивный кирпичик — ошибка предпросмотра
   с подсказкой ближайших кодов (§7.6).
4. Фотографии не являются узлами грамматики: токена `[фото]` нет.
   Единственные узлы — текст, кирпичик, ссылка, стиль и перевод строки.

### 7.3. Конвейер обработки (промежуточное представление)

Сознательно отказываемся от цепочки regex-замен — она ломается на
вложенности и экранировании. Этапы:

```
исходный текст секции (с entities или без)
   → 1. Лексер: текст / escape / "[" код "]" / "(", ")" / URL
   → 2. Парсер: IR-узлы
        TextNode(html)                — уже экранированный текст
        BrickNode(brick_id, attrs)    — ссылка на кирпичик
        LinkNode(label: IR, target: BrickNode | UrlLiteral)
        StyleNode(style, child)       — b/i/u/s/code/spoiler (из entities/Markdown)
        NewlineNode
   → 3. Резолвер: BrickNode → значение из announcement_values / места / вопроса
        (неизвестное значение → узел-заглушка «—» и запись в отчёт предпросмотра)
   → 4. Рендер IR → Telegram-HTML: каждый узел строит разметку сам
        (escape_html один раз на текстовый узел; вложенные Style/Link —
        корректная вложенность тегов по построению)
   → 5. План ТЕКСТОВЫХ сообщений: текстовые части (split_html из utils.tg_html)
```

Конвейер заканчивается текстом. Вложения не входят в IR и не участвуют в
шагах 1–5: сборка публикации (§7.5) получает уже отрендеренный HTML и
отдельный список фотографий анонса.

```python
# псевдокод рендера узлов
def render(node) -> str:
    match node:
        case TextNode(html):        return html          # экранирован при создании
        case BrickNode(b):          return render_value(b) or "—"
        case LinkNode(label, tgt):
            href = resolve_url(tgt)  # normalize_url; None → без <a>, +диагностика
            return f'<a href="{escape_html(href)}">{render(label)}</a>' if href else render(label)
        case StyleNode(st, child):  return wrap(st, render(child))
        case NewlineNode():         return "\n"
```

### 7.4. Примеры разбора (из ТЗ)

| Источник | Результат |
|---|---|
| `[место]([ссылка_места])` | `<a href="URL">Название места</a>` |
| `[место](ссылка_места)` | ошибка предпросмотра: «внутри скобок — текст `ссылка_места`, а не URL; заключите его в [квадратные скобки]» |
| `Билеты(https://example.com)` | `<a href="https://example.com">Билеты</a>` |
| `Билеты(t.me/example)` | `<a href="https://t.me/example">Билеты</a>` |
| `Наше-уютное-место([ссылка_места])` | `<a href="URL">Наше уютное место</a>` (дефисы → пробелы) |
| `\[не кирпичик\]` | текст `[не кирпичик]` |

### 7.5. Текстовый рендер и сборка публикации с вложениями

**Фотографии — вложения анонса, а не кирпичики и не узлы шаблона.**

- В тексте шаблона нет `[фото]`; попытка использовать такой код — обычная
  ошибка «неизвестный кирпичик», а не особый случай.
- Коллекция изображений принадлежит конкретному анонсу и не зависит от
  выбранного шаблона: 0, 1, несколько фото или несколько альбомов.
- Существующий приём (одиночные фото, медиагруппы, накопление нескольких
  альбомов, хранение `file_id`, отправка чанками по лимиту Telegram)
  сохраняется без изменений. В Stage 19A этот код не меняется.
- Отсутствие фотографий **никогда не блокирует** создание/сохранение анонса.

**Два независимых слоя:**

1. **Текстовый рендер** (IR §7.3) собирает HTML только из секций шаблона и
   значений кирпичиков. Он не знает о фотографиях.
2. **Сборка публикации** получает готовый HTML + отдельный список вложений
   анонса и формирует сообщения Telegram: текстовые части через `split_html`
   (4000/4096 UTF-16), подпись к медиа (1000/1024), альбомы по 10,
   прогресс частичной доставки Stage 12. Порядок и правила — существующего
   `send_post`; шаблон не задаёт позицию альбома.

- Снимок шаблона (`template_snapshot`) содержит только секции; правка
  шаблона не затрагивает фотографии, а добавление/удаление фото не меняет
  шаблон.
- Для фотографий не создаётся специальный rich-редактор: редактирование
  вложений остаётся существующим фото-потоком.

### 7.6. Диагностика ошибок (обычному пользователю)

Каждая ошибка предпросмотра: что не так → где (строка/секция) → как
исправить → кнопка «Исправить» (снова прислать секцию). Пример:

```
⚠️ Не понял кирпичик [главный_спикер] (строка 2 секции «Тело»).
Похожие: [ведущий]. Исправьте текст и пришлите снова.
```

Повторный ввод секции правит тот же экран; подтверждение «✅ Сохранить
секцию» появляется только при нулевых ошибках.

### 7.7. Нормализация URL (цели гиперссылок)

Реализация — существующий `normalize_url` из `utils/validators.py`
(Stage 8), без новой параллельной функции:

| Ввод пользователя | Хранится как |
|---|---|
| `t.me/random_culture` | `https://t.me/random_culture` |
| `https://t.me/random_culture` | без изменений |
| `example.com`, `www.example.com` | `https://example.com` (www НЕ добавляется автоматически) |
| `example.com/page` | `https://example.com/page` (путь/параметры/фрагмент сохраняются) |
| `http://example.com` | `http://example.com` (явно указанная схема сохраняется) |

- Схема отсутствует → добавляется `https://`; опасные схемы
  (`javascript:`, `data:`, `file:`, …), пробелы, userinfo, некорректные
  адреса отклоняются — значение не становится гиперссылкой.
- Невалидная цель литеральной ссылки или кирпичика типа `url` — ошибка
  предпросмотра в редакторе; пустое значение `ссылка_места` → текст
  без гиперссылки (§7.3).
- Если ссылка выбранного места известна и корректна, кирпичик
  `ссылка_места` не спрашивается повторно (§8.3).

---

## 8. Схема адаптивного создания поста

### 8.1. Последовательность (backend — Stage 27A, UI — Stage 27B, фото и выдача — Stage 27C/28)

**Stage 27A** реализовал сервисный слой: `announcement_store` создаёт
анонс по `place_id`, сохраняет выбранное название/контакты, привязывает
стандартный или личный шаблон с независимым снимком, принимает значения
по `brick_id`, добавляет extras, переключает этапы, восстанавливает
состояние после рестарта и защищает изменения `revision`.
**Stage 27B** добавил отдельный Telegram-интерфейс мастера.
**Stage 27C** подключил отдельную коллекцию фотографий и публикацию:
готовый HTML и фото замораживаются в SQLite, отправляются существующим
`sender`, а прогресс каждой подтверждённой операции сохраняется для
продолжения без повтора уже доставленного. **Stage 28** сделал выдачу
автоматической (без финальной кнопки) и добавил безопасную замену прошлой
версии после правки.
Прежний мастер «✏️ Создать пост» удалён в S5 (историческая схема ниже).

```
«📝 Новый мастер» / /new_wizard
 → ГЛАВНЫЙ ЭКРАН МАСТЕРА (продолжить черновик или новый анонс)
 → ВЫБОР МЕСТА   (поиск, пагинация, карточка, каноническое/альтернативное
                  название, подтверждение; place_id и place_name_id раздельно)
 → ВЫБОР ШАБЛОНА (стандартный места — всегда; только личные шаблоны автора;
                  выбранная версия фиксируется независимым снимком)
 → ВОПРОСЫ       (по одному на brick_id; известные значения места не
                  спрашиваются; Back/Skip/«Изменить ответ»; date —
                  календарь Stage 17, url — normalize_url, остальные —
                  валидаторы Stage 22A)
 → ДОП. КИРПИЧИКИ (необязательно: определение → секция → позиция →
                  типизированное значение → подтверждение; только этот анонс)
 → ПРЕДПРОСМОТР  (место, шаблон, заполненные/пропущенные поля, extras,
                  правка значений, компилированный текст по снимку)
 → ФОТОГРАФИИ    (необязательно: одиночные/альбомы, список, добавление,
                  просмотр вложений, удаление; отдельно от текста/extras)
 → ВЫДАЧА        (последний ответ или осознанный пропуск СРАЗУ собирает и
                  отправляет готовый анонс в личный чат; отдельной кнопки
                  «опубликовать» и экрана подтверждения нет)
 → ГОТОВЫЙ АНОНС (отдельное сообщение без клавиатуры — его можно скопировать)
 → ЭКРАН ДЕЙСТВИЙ (управление в отдельном сообщении: изменить данные/фото/
                  extras, «🔄 Применить изменения», «💾 Сохранить и выйти»)
```

Правка доставленного анонса не отправляет копию на каждом шаге: изменения
копятся в черновике, а «🔄 Применить изменения» пересобирает payload и
заменяет прошлую версию. Если анонс ещё не доставлялся (остановлен на
обзоре, сохранён, восстановлен после полного сбоя), экран предлагает
явное «📨 Отправить в этот чат». «💾 Сохранить и выйти» — отдельное
действие сохранения/выхода, оно ничего не публикует. Девятишагового
мастера с S5 нет.

### 8.2. Состояния ConversationHandler

Новый мастер — отдельный **именованный** `ConversationHandler`
`announcement_wizard`; он регистрируется раньше прежнего диалога и не
переиспользует `POST_CREATION`/`EDIT_FIELD` старого мастера. Состояния:
`AW_PLACE`, `AW_TEMPLATE`, `AW_QUESTIONS`, `AW_EXTRAS`, `AW_PREVIEW`.
Экраны фотографий, готового анонса и статуса отправки остаются
в `AW_PREVIEW` и используют тот же основной экран/`reanchor_screen`.
Точки входа — callback `aw_start` и `/new_wizard`; внутри мастера доступны
`/start`, `/cancel`, `/drafts`, `/places`, `/bricks`, `/templates`,
`/admin`, `/help`. Явный вход завершает посторонние диалоги и очищает
временные сессии других разделов, но не удаляет WIP и черновики.

Навигационный контекст (`place`, `page`, активный `brick_id`, `source`)
хранится в `user_data["announcement_ui"]`; доменное состояние — только в
`announcement_*`. Callback-кнопки после выбора шаблона содержат
`draft_id` и `revision`; устаревшие, повторные и чужие действия
отклоняются без записи. Вне активного диалога `aw_*`-кнопка получает
короткий ответ «кнопка устарела».

### 8.3. Вычисление вопросов

`announcement_store.build_announcement_fields` вызывает тот же
`announcement_wizard.build_questions`, что и сервис Stage 27A: порядок
задаёт первое появление `brick_id` в шапке → теле → подвале,
повторяющийся кирпичик спрашивается один раз, отсутствие строки и
допустимое пустое значение различаются, архивный/несовместимый кирпичик
даёт диагностическую ошибку. Telegram-слой не ищет кирпичики по
текстовому коду и не дублирует выбор полей: он рисует вопросы по
`brick_id` и сохраняет значения через `save_announcement_value`.

- Все вопросы Stage 27A необязательны (`required=False`,
  `allow_empty=True`); явный пропуск сохраняется как `empty_optional`,
  отсутствие значения — как `missing`.
- Известные имя, адрес и ссылка места подставляются по постоянным
  `brick_id`; отсутствующие адрес/ссылка не выдумываются и не блокируют
  анонс.
- Все семь типов используют общие правила Stage 22A; дата дополнительно
  открывает календарь Stage 17, время разбирается тем же парсером,
  URL нормализуется до сохранения. При ошибке валидации ввод остаётся в
  чате, а текст ошибки показывается на экране вопроса.

### 8.4. Пропуск и «Назад»

- «⏭ Пропустить» сохраняет `empty_optional`; поле остаётся в снимке и
  учитывается в предпросмотре как пропущенное, но не обязано иметь
  значение.
- «◀️ Назад» переходит к предыдущему вопросу; с первого вопроса — к
  экрану уже выбранного снимка (повторный выбор шаблона недоступен).
- У заполненного вопроса видны текущее значение и «✏️ Изменить ответ».
  Ошибка валидации не стирает ни текущее сохранённое значение, ни
  неудачный ввод пользователя.

### 8.5. «Добавить дополнительный кирпичик»

Меню дополнительных кирпичиков показывает определения, доступные
пользователю (системные, личные и кирпичики места), которых ещё нет в
снимке этого анонса. Выбор идёт по `brick_id`:

1. определение → секция (шапка/тело/подвал) → позиция (в начало/в конец);
2. значение вводится по типу кирпичика и сохраняется в
   `announcement_values` этого анонса;
3. запись `announcement_extras` существует только у текущего черновика;
   шаблон, его снимок и другие анонсы не меняются;
4. добавленные поля видны в списке, предпросмотре и меню правки; значение
   можно изменить, пропустить или удалить вместе с extras-записью;
5. drag-and-drop, изменение позиции после добавления и редактор шаблона
   в Stage 27B не входят. Фотографии extras не являются.

### 8.6. Предпросмотр, правка и сохранение

- Предпросмотр показывает место и выбранное название, шаблон и версию
  снимка, заполненные/пропущенные/известные поля, дополнительные
  кирпичики и диагностику, если текст собрать нельзя.
- Компилированный текст строится `template_compiler.render_template` по
  снимку, `brick_id` и значениям; поиска кирпичиков по текстовому коду
  нет. Ошибка архивного/несовместимого кирпичика выводится как причина,
  черновик и значения сохраняются.
- Длинный видимый текст сокращается с пометкой «полные данные сохранены
  в черновике»; в SQLite остаётся полное значение.
- «✏️ Изменить поля» открывает менеджер полей, «Изменить ответ» — нужный
  вопрос; extras редактируются отдельным экраном.
- «✅ Сохранить черновик» оставляет `stage=preview` и не запускает
  реальную или фейковую публикацию. Выход в меню не удаляет сохранённый
  анонс. У доставленного анонса эта же кнопка называется «💾 Сохранить и
  выйти» и тоже ничего не публикует; недоставленный анонс, остановленный
  на обзоре, завершается явным «📨 Отправить в этот чат» (Stage 28).
- После рестарта главный экран мастера перечисляет незавершённые анонсы
  автора; открытие восстанавливает место, шаблон, снимок, ответы, extras
  и фотографии, не создавая новый снимок. Чужой ID выглядит как «не найден».

### 8.7. Фотографии, автоматическая выдача и замена версии (Stage 27C/28)

Фотографии — отдельная коллекция вложений, а не кирпичики, не значения и
не часть шаблонного текста. Токен `[фото]` и типы `photo`/`media` не
добавлены. Коллекция хранится в `announcement_drafts.photos` как
JSON-список Telegram `file_id`, дедуплицируется по `file_id`, ограничена
100 снимками и не участвует в `announcement_values`/`announcement_extras`.
Одиночные снимки и альбомы нескольких `Update` подряд добавляются в одну
коллекцию; после успешного сохранения временный пользовательский ввод
удаляется. Добавление/удаление/очистка меняют `revision`, поэтому
повторный или устаревший callback не перезаписывает коллекцию.

Выдача запускается автоматически последним ответом (или осознанным
пропуском) без отдельной кнопки и без экрана подтверждения. Готовый текст
и фото приходят отдельным сообщением без клавиатуры, а кнопки управления
живут в служебном сообщении экрана: так анонс можно скопировать или
переслать, не задев служебные кнопки. До первой сетевой операции
`begin_announcement_publication` собирает
готовый HTML тем же компилятором Stage 24, проверяет форматирование,
ссылки, типы и длину, затем атомарно фиксирует:

- `publication_status='sending'`;
- `publication_text` — полный замороженный HTML;
- `publication_photos` — замороженный список фото;
- `publication_started_at`, пустой `publication_progress`, без `published_at`.

После этой фиксации значения, extras, фотографии и шаблон нельзя менять
через сервис, пока идёт отправка; исходный шаблон может меняться, но
snapshot и payload текущей отправки уже не пересобираются. Отправка идёт
существующим `send_rendered_post` (общая низкоуровневая часть `send_post`),
с обычными медиагруппами по 10 снимков и разбиением текста по лимитам
Telegram. Второго sender/формата разметки или очереди согласования нет;
получатель — только личный чат автора: мастер можно открыть и в группе, но
выдача и временный предпросмотр вложений в неличный чат отклоняются до
любого сетевого вызова, а данные остаются в черновике.

`send_rendered_post` после каждой подтверждённой Telegram-операции
синхронно вызывает `on_progress`; новый мастер сохраняет
`fingerprint/step/ids/total` в `publication_progress`. Поэтому продолжение
пропускает уже подтверждённые операции. Модель восстановления честная:

| Состояние | Что разрешено |
|---|---|
| `sending`, прогресс есть | только продолжить замороженный payload |
| `partial` | только дослать оставшееся; возврат к правке запрещён |
| `failed`, step=0, ids пуст | повторить или вернуться к правке |
| `failed` после возможной отправки | повтор/правка предупреждают о возможном дубле |
| `sent` | просмотр и правка; явное «🔄 Применить изменения» заменяет версию |

Успех показывается только после успешного ответа Telegram и сохранения
`sent`/`published_at`/`stage=completed`. Сбой API, рестарт, повторный или
stale callback и уже выполненная публикация не приводят к автоматическому
повтору всей последовательности. Если ответ Telegram потерян после
медиагруппы, exactly-once не обещается: UI прямо предупреждает о
возможном дубле. Сброс к редактированию очищает только публикационный
payload и разрешён лишь при полностью неопределённом (`failed`, step=0,
ids пуст) результате.

Замена доставленной версии (Stage 28, миграция 14):

- правка данных/фото/extras у `sent` не отправляет новую копию сама по
  себе; «🔄 Применить изменения» пересобирает payload и переводит анонс в
  `sending`, перенося адреса прошлой копии в `previous_ids`;
- `publication_progress.ids` — адреса текущей отправки, `previous_ids` —
  только заменяемой версии. Они не смешиваются: удалять можно лишь прошлую
  копию и только после того, как новая подтверждена целиком;
- прошлая копия остаётся в чате единственной полной версией, пока новая не
  доставлена. Неудача удаления (Telegram не удаляет сообщения старше 48
  часов) логируется и не считается сбоем доставки — анонс уже доступен;
- сброс к редактированию при наличии `previous_ids` запрещён: иначе
  потерялся бы единственный адрес прошлой версии;
- это последняя доставленная версия, а не «неизменяемая история»: при
  следующей пересборке payload перезаписывается.

---

## 9. Совместимость и миграция черновиков и WIP

### 9.1. Принципы

1. **Только добавочные миграции.** С Stage 19A это реализовано:
   `migrations.py` + журнал `schema_migrations` с контрольными суммами,
   транзакционным применением и проверкой версий (docs/migrations.md).
   Каждая новая миграция добавляется в конец списка; уже применённые
   миграции не редактируются.
2. Никаких разрушительных операций (DROP/переименование колонок,
   перезапись данных). Если однажды понадобится — отдельно оформленный план
   восстановления: остановка бота, резервная копия `post_bot.db` (+ WAL),
   ручной запуск, откат = возврат копии. Реальная база никогда не мигрирует
   из тестов; миграции на проде — осознанный запуск владельца.
3. С Stage 19B перед первым ожидающим DDL существующей непустой базы
   автоматически создаётся WAL-aware копия; сбой копии отменяет миграцию.
   Копии не удаляются и не перезаписываются.
4. Тесты миграций — только на временных БД (Stage 19A/19B:
   `tests/test_stage19a_migrations.py`, `tests/test_stage19b_backup.py`,
   включая старую схему с фиктивными черновиками/WIP, WAL, restore и сбой
   копии).

### 9.2. WIP (`user_states.payload`) — прежний формат, не изменён

Stage 27B **не меняет** `user_states` и формат WIP девятишагового мастера.
Новый мастер не хранит доменное состояние в памяти или в payload v1:
автор, место, название, шаблон/версия, снимок, значения, extras, этап и
`revision` лежат в `announcement_*`. В `user_data["announcement_ui"]`
остаётся только навигация текущего экрана (страница, фаза выбора места,
активный `brick_id`); после рестарта она восстанавливается из БД.

- `schema=2`/`payload_level=2` для `drafts` не появились: старый WIP
  читается и сохраняется прежним кодом.
- Вход в новый мастер завершает старый диалог, но не стирает сохранённый
  WIP; старый черновик можно продолжить отдельно.
- Начатый анонс Stage 27A/27B открывается через список нового мастера и
  не конвертируется в легаси-`drafts` автоматически.
- Фотографии Stage 27C хранятся в `announcement_drafts.photos` как отдельная
  коллекция вложений, а не в `announcement_values`/`announcement_extras`;
  они не являются кирпичиком и не попадают в шаблонный текст.
- Публикация Stage 27C не меняет формат WIP: замороженный HTML/фото и
  прогресс лежат в `announcement_drafts`, а `user_states.payload` остаётся
  прежним.

### 9.3. Чтение старых черновиков (`payload_level=1`)

Старые черновики открываются в прежнем редакторе (9 шагов + превью
`build_post_summary`) и **никак не перезаписываются**. Отображение на
кирпичики — служебный адаптер чтения (Приложение А):

| Колонка `drafts` | Кирпичик | Тип |
|---|---|---|
| `title` | `название` | short_text |
| `date` | `дата` | date (ДД.ММ.ГГГГ) |
| `time_start` | `время_начала` | time (ЧЧ:ММ) |
| `time_end` | `время_окончания` | time |
| `place_name` | `место` | short_text |
| `place_url` | `ссылка_места` | url |
| `text` | `описание` | long_text |
| `contact` | `контакт` | short_text |

`адрес_места` не имеет отдельной легаси-колонки: адрес существует только
в справочнике `places.address` и в старый черновик не копировался.

`image`/`photos` **не** отображаются на кирпичик: это независимая коллекция
вложений анонса. При явной конвертации она переносится в новую модель как
отдельные данные и не превращается в `draft_values`.

Если в будущем владелец откроет старый черновик и явно применит шаблон,
это будет отдельная осознанная конвертация в `announcement_*`; автоматической
конвертации в Stage 27A нет. Легаси-колонки остаются нетронутыми, а
`draft_values`/`payload_level=2` из раннего плана не создавались.

### 9.4. Неизменность старых постов при правке шаблона

- Новый анонс хранит **собственный снимок** секций
  (`announcement_drafts.template_snapshot_id` →
  `template_snapshots`) и значения кирпичиков (`announcement_values`).
  Рендер начатого анонса работает только со снимком: правка
  `template_versions` исполнения не меняет.
- Для легаси-черновиков источником истины остаются девять колонок `drafts`;
  адаптер чтения Приложения А не переписывает их и не создаёт `draft_values`.
- Колонки `template_id`/`template_version` в новом анонсе — справочные (из
  какой версии вырос снимок, для диффа и «обновить по стандартному?»).
- Готовые посты уже отправлены в чат Telegram и физически неизменяемы;
  дизайн не вводит «пересборки» старых постов.

### 9.5. Контрольная точка совместимости

- После Stage 28 полный прогон: **1237 тестов** (1226 после Stage 27C +
  11 Stage 28: 7 schema/backup и 4 новых e2e-сценария) зелёные на Python
  3.13/3.14. Тесты проверяют, что старый черновик/WIP не получают строк в
  `announcement_*`, а новая схема не меняет `drafts`.
- Миграция 13 добавляет только фото/публикационные колонки, индекс и
  триггеры к `announcement_drafts`; она не переписывает старые анонсы,
  `drafts`, WIP и фотоколлекции легаси-мастера. Миграция 14 меняет только
  один триггер (проверяемый `sent -> sending`) и не трогает данные.
- Сценарий «старый черновик → открытие → правка → сохранение» по-прежнему
  не меняет `payload_level`/легаси-строку; вход в новый мастер сохраняет
  WIP и не удаляет старый черновик.
- Отдельный снимок шаблона не пересоздаётся при повторном открытии
  анонса; правка текущего шаблона на него не влияет.
- Отправленный payload (`sent`) не редактируется на месте: правка доступна
  только через новую сборку, которая заменяет прошлую версию; сброс к
  правке возможен только до первой подтверждённой операции и только пока
  нет `previous_ids`.

---

## 10. Безопасность, конкурентные операции, транзакции

### 10.1. Целостность ввода

- Все текстовые значения экранируются один раз (`tg_html.escape_html`);
  конвейер §7.3 не пропускает пользовательский HTML/Markdown в обход IR.
- URL-цели гиперссылок — только после `normalize_url` (http/https,
  без `javascript:`/`data:`/пробелов/userinfo); невалидные цели не
  становятся ссылками (правила §7.7; уже реализовано в
  `utils/validators.py`).
- Callback-данные: только индексы/ISO-даты/ID ≤ 64 байт без текста
  пользователя (правило Stage 17 сохраняется для всех новых сценариев).
- Дисциплина Telegram-лимитов сохраняется: 4096 знаков на текстовое
  сообщение, 1024 на подпись к альбому (лимиты Stage 10/12 проверяются в
  UTF-16), callback ≤ 64 байт. Новые кирпичики ограничиваются только
  платформенными лимитами; произвольных молчаливых обрезаний нет.
- Если текст не помещается, публикация разбивает сообщения (`split_html`)
  или явно сообщает об ошибке; введённые данные при этом не теряются.
- Предпросмотр нового мастера сокращает длинный видимый текст
  (`PREVIEW_VISIBLE_LIMIT`) и явно пишет, что полные данные сохранены в
  SQLite; молчаливого обрезания сохранённого значения нет.
- Фотографии подчиняются своим платформенным правилам (10 в альбоме,
  1024 в подписи) и обрабатываются существующей отправкой чанками; они не
  становятся кирпичиками и не влияют на лимиты текстовых полей.

### 10.2. Кнопки и устаревшие состояния

- Паттерн `STALE_DIALOG_CALLBACK_PATTERN` расширяется префиксами новых
  сценариев (`place_`, `tpl_`, `edsec_`, `brick_`, `xfer_`); кнопки
  нового мастера используют `aw_` и отдельный именованный диалог.
- Каждое действие перечитывает состояние сущности из БД и сравнивает с
  ожидаемым (id + `revision`/статус); несоответствие — честный ответ
  «Кнопка устарела…» без побочных эффектов. Для анонса дополнительно
  проверяются `draft_id`, `revision` и автор; чужие ID не раскрываются.
- Защита от двойного нажатия: идемпотентность по статусу (upsert
  `announcement_values` по UNIQUE(draft_id, brick_id); решения заявок по
  условному UPDATE).

### 10.3. Транзакции конкурентных решений

Единый приём для заявок, передач админки и смены админа:

```python
with session.begin():                       # BEGIN IMMEDIATE (SQLAlchemy)
    row = session.get(BrickProposal, pid, with_for_update=True)
    if row.status != "pending":             # у заявок нет срока
        return HonestAnswer("Заявка уже рассмотрена")
    row.status = "approved"
    create_brick(session, row.payload_json)  # та же транзакция
# commit: атомарно; сбой → rollback, кирпичик не создан наполовину
```

- Для передач админки дополнительно проверяется `expires_at` внутри той же
  транзакции; у заявок на кирпичики срока нет — только решение или отзыв.
- SQLite — один писатель (WAL + busy_timeout 30 с): длинных блокировок нет,
  откаты честны; `rowcount`-проверки UPDATE обязательны.
- Мультишаговые сценарии хранят прогресс в БД (заявка/передача/чертёж) —
  рестарт процесса не теряет и не дублирует шаг.

### 10.4. Приватность и журналы

- Правила Stage 14 (токен и пользовательский текст не попадают в логи)
  сохраняются; новые подсистемы логируют только id и итог.
- `.env` не изменяется в тестах; фантомный токен как в `tests/__init__.py`.

---

## 11. План автоматизированного тестирования

Базис — существующая инфраструктура: FakeBot, `frozen_today`,
`Application.process_update`, временная SQLite (§1.4). Новые каркасы
тестов не нужны.

### 11.1. Слои тестов

| Слой | Что проверяется | Примеры |
|---|---|---|
| Чистые функции | Нормализация имён, похожесть, грамматика, IR-рендер, URL | `tests/test_stage21_transfer_logic.py`, `tests/test_stage20b_edit_logic.py` |
| Модели/миграции | Схемы на временных БД, идемпотентность, легаси-строки | `tests/test_stage19a_migrations.py`, `tests/test_stage19a_schema.py` |
| Кирпичики | Типы/правила/значения, права, приватность, разрешение `[код]`, гонки, реальные ограничения SQLite, жизненный цикл Stage 22B | `tests/test_stage22a_brick_types.py`, `test_stage22a_brick_service.py`, `test_stage22b_brick_service.py`, `test_stage22b_brick_schema.py` |
| Интеграция | Все сценарии чата через `process_update`: места, роли, правка, передача, личные кирпичики, шаблоны, предложения | `tests/test_stage21_transfer_flow.py`, `tests/test_stage22b_bricks_flow.py`, `tests/test_stage25_templates_flow.py`, `tests/test_stage26_brick_proposals_flow.py` |
| Предложения Stage 26 | Снимок, четыре статуса, права/передача админки, конфликты кодов, транзакции и гонки, уведомления, бессрочность | `tests/test_stage26_brick_proposals_schema.py`, `test_stage26_brick_proposals_service.py` |
| Новый мастер Stage 27A | Схема/триггеры, независимый снимок, вопросы по `brick_id`, значения места, все семь типов, extras, этапы, рестарт, идемпотентность и optimistic locking | `tests/test_stage27a_announcement_schema.py`, `test_stage27a_announcement_wizard.py`, `test_stage27a_announcement_store.py` |
| UI нового мастера Stage 27B | Меню и старый WIP, место/альтернатива/пагинация/назад, изоляция шаблонов и стабильный снимок, все семь типов, календарь/валидация, extras, рестарт/сохранение/правка, устаревшие и чужие callback, диагностика, стандартные известные значения | `tests/test_stage27b_announcement_ui.py`, `test_stage27b_announcement_fields.py` |
| Фото, выдача и замена Stage 27C/28 | Отдельная коллекция 0/1/много фото и медиагрупп, дедупликация, рестарт, предпросмотр вложений, заморозка HTML/фото, оба вида шаблонов, семь типов и extras в итоговом HTML, успех только после Telegram, сбой API, частичная отправка, продолжение без дублей, stale/двойной callback, права, автоматическая выдача, отказ групповому чату, порядок замены и неудача удаления старше 48 часов | `tests/test_stage27c_announcement_publication.py`, `test_stage27c_announcement_ui.py` |
| Миграция 14 Stage 28 | Журнал/суммы 1–13 без изменений, сохранность легаси-данных, отсутствие новых колонок, атомарный откат, разрешённый `sent -> sending` только с `previous_ids`, WAL-копия и блокировка при сбое копии | `tests/test_stage28_announcement_redelivery.py` |
| UI-покрытие | Перебор всех уровней меню, один экран, один answer, без пустых экранов | `tests/test_stage17_calendar_flow.py` (S5: легаси-coverage удалён) |
| Регрессии Stage 11–28 | 1237 тестов остаются зелёными на каждом следующем этапе | существующие файлы, `make check` |

### 11.2. Критические сценарии (минимально обязательные)

1. **Места:** создание с проверкой дублей; подтверждение кандидата;
   альтернативное название (добавление любым, правка только админом);
   похожие названия не сливаются; нормализация `name_search` (`ё`,
   регистр, пунктуация).
2. **Роли:** каждый запрет матрицы §4.2 проверяется запросом «чужого»
   объекта (place_id другого админа) — через callback и через прямой вызов.
3. **Передача админки:** полный цикл (§5.5) — подтверждение получателем;
   истечение 24 ч (замороженное время); запрет повторного подтверждения;
   одно `pending`-предложение на место; замена/снятие админа и повторное
   назначение прежнего переводят ожидающее предложение в `superseded`;
   увольнение инициатора в ожидании; подмена ID места; рестарт процесса
   посреди ожидания; передача незнакомому пользователю по ссылке: открытие
   даёт только кандидата, без утверждения инициатора и подтверждения
   получателя права не переходят. Первый открывший фиксируется; другой
   аккаунт не получает доступ, а повторное открытие тем же кандидатом
   показывает ожидание без дополнительных прав.
4. **Кирпичики (Stage 22A/22B — реализовано):** девять системных
   определений; создание личного/места/системного с проверкой прав; конфликт
   кода с системным запрещён; приватность и разрешение `[код]`; пять
   состояний значений; реальные UNIQUE/CHECK/FK/триггеры; конкурентная
   гонка. Stage 22B: все семь типов, подсказки/конфликты кодов, настройки
   числа/выбора, предпросмотр без записи, правка с неизменным ID/типом,
   мягкий архив/восстановление, пожизненная резервация кодов, пагинация,
   рестарт, stale-callback и сбои БД. Заявки (одобрение админом, повторное
   решение, отзыв, отсутствие истечения, публикация глобально) — Stage 26:
   40 интеграционных сценариев через `Application.process_update`, включая
   передачу админки, stale/повторные callback, конфликт кода и сбой
   Telegram-уведомления при уже сохранённом решении.
5. **Шаблоны/редактор (Stage 23–25 — реализовано):** создание личного
   шаблона из стандарта и «пустого»; правка каждой секции полным текстом;
   ошибки синтаксиса (неизвестный кирпичик, неоднозначный код, вложенные
   ссылки, отсутствие `[фото]` как узла); неоднозначность разрешается
   кнопкой и сохраняет конкретный `brick_id`; предпросмотр трёх секций;
   доступные кирпичики без чужих личных и внутренних ID; «Сбросить к
   встроенному стандарту» только после подтверждения, с новой версией и
   неизменными историей/снимками; личный шаблон изолирован по владельцу;
   стандарт открывается админу места/суперадмину из кабинета, права
   перепроверяются при сохранении; конфликты revision, повторные callback,
   двойное сохранение и рестарт сохраняют введённый текст и не
   перезаписывают чужие изменения. Снимки шаблона и значения нового мастера
   реализованы в Stage 27A (сервис), Telegram-UI — в Stage 27B.
6. **Грамматика/рендер:** все примеры §7.4; вложенные стили Telegram
   (bold внутри ссылки и наоборот); экранированные скобки; `split_html`
   длинного рендера не рвёт теги; URL-цели без схемы нормализуются.
7. **Адаптивное создание (backend Stage 27A, UI Stage 27B — реализовано):**
   вопросы только для отсутствующих кирпичиков; кирпичик `source=place`
   не спрашивается при известном значении; порядок шапка→тело→подвал и
   один вопрос на повторяющийся `brick_id`; все семь типов и правила
   даты/времени/URL; отсутствие значения отличается от допустимого
   пустого; extras только этого анонса; независимый снимок не меняется
   при правке шаблона; чужой анонс не открывается по ID; ревизия/
   идемпотентность защищают повторные действия. Telegram-UI проверен
   через `Application.process_update`: меню, место/альтернатива,
   шаблоны, вопросы, календарь, extras, предпросмотр, сохранение и
   рестарт. Stage 27C проверяет фото 0/1/11 и медиагруппы, заморозку
   HTML/фото, успех только после Telegram, сбой API, частичную отправку,
   продолжение и stale/двойной callback. Фотографии остаются отдельным
   необязательным потоком и не входят в вопросы или шаблон.
8. **Предложения Stage 26:** очередь места изолирована по `place_id`, сводная и глобальная очереди суперадмина разделены; approve/reject/withdraw требуют подтверждения; stale/повторный callback и гонка не меняют решение; снимок переживает правку/архив источника; после передачи админки старый админ теряет доступ, новый решает; сбой уведомления не откатывает решение; `pending` не истекает.
9. **Миграции и WIP:** старый черновик/WIP открывается и не меняется;
   Stage 27A не создаёт для него строк в `announcement_*`; автоматической
   конвертации и `payload_level=2` нет. Stage 19A дополнительно проверяет
   журнал версий, откат сбойной миграции, конкурентный запуск и сохранность
   старых `drafts`/WIP; Stage 19B — миграцию 2, WAL-aware копию, сбой копии
   до DDL, restore и неизменность рабочей базы; Stage 27A — миграцию 12 и
   сохранность всех данных Stage 26.
10. **Полнота/безопасность:** пустые экраны отсутствуют после каждого
   действия; очистка временных сообщений только после подтверждённого
   сохранения; callback ≤ 64 байт.
11. **Фото/публикация Stage 27C (выполнено):** оба вида шаблонов; 0/1/11
   фото и медиагруппы; одиночные/альбомные Update и дедупликация;
   рестарт до и во время отправки; просмотр, удаление и замена фото;
   заморозка HTML/фото после подтверждения; успех только после ответа
   Telegram; сбой API до/после первой операции; `partial` с продолжением
   только недостающего; stale/двойной callback; чужой draft; старый
   мастер/WIP и исторические snapshots не меняются.

### 11.3. Принципы приёмки

- Каждый будущий этап: свой `make check` + полный регресс.
- Интеграционные тесты не обращаются к сети; время заморожено; БД —
  временная; токен — фантомный.

---

## 12. Проверка дорожной карты Stage 19–28

Исходная карта: 19 места → 20 права → 21 передача → 22 кирпичики →
23 шаблоны → 24 парсер → 25 редактор → 26 согласование → 27 адаптивное
создание → 28 сквозные тесты.

### 12.1. Найденные зависимости

| Факт | Влияние |
|---|---|
| Нумерованные миграции и журнал **реализованы в Stage 19A**, backup и миграция 2 — в 19B, миграции 3–7 — в 20A/20B/21/22A/22B, 8–10 — в 23/24/25, 11 — в 26, 12 — в 27A, 13 — в 27C | Stage 28+ добавляют только новые версии; `_migrate()` больше не расширяется |
| Stage 19B содержит Telegram-UI, поиск, создание и альтернативные названия | Stage 20 может опираться на `place_sessions`/`place_names`, не меняя WIP и мастер |
| Стандартный шаблон создаётся вместе с местом и бэкфиллится миграцией 8 | Работающий код не зависит от шаблонов; v1 существует для старых и новых мест |
| Предложения кирпичиков (26) зависят от ролей (20), кирпичиков (22) и шаблонов (23–25) | Порядок корректен; Stage 26 реализован |
| Редактор (25) требует парсер (24) | Порядок корректен |
| Адаптивное создание (27) требует всего вышеуказанного + рендера превью (24) | Порядок корректен |
| Читатели черновиков (список, редактор) дорисовывают «карточек по кирпичикам» только к 27/28 | Список черновиков (Stage 11) на 19–26 продолжает печатать легаси-поля — совместимо |
| Передача (21) возможна лишь когда есть админы и кабинеты (20) | Порядок корректен |

### 12.2. Предлагаемые изменения (без механической перенумерации)

1. **Stage 19 = Stage 19A + 19B.**
   - **19A (выполнено):** `schema_migrations`, таблицы `users`, `places`,
     `place_names`, нормализация названий, атомарное создание места,
     тесты ограничений и миграций. Telegram-интерфейса, поиска и
     альтернативных названий нет.
   - **19B (выполнено):** Telegram-поток поиска/создания/выбора места,
     подсказки о дублях, альтернативные названия (любым пользователем),
     подтверждённое отображаемое имя в ограниченном состоянии сценария,
     миграция 2 и WAL-aware backup. Мастер поста не изменён; стандартный
     шаблон v1 по-прежнему относится к Stage 23. Интеграция места в новый
     мастер реализована в Stage 27B.
2. **Stage 20 = пользователи/роли + кабинеты.** Реализовано: роли и
   кабинеты с назначением/заменой/отзывом админа суперадмином (Stage 20A),
   кнопка «Я админ места» (только контакт суперадмина), редактирование
   данных места с журналом аудита (Stage 20B). Остаётся: «входящие»
   кабинета (уведомления в экране) и объединение дублей места
   (осознанная операция админа; требование «не допускать неявных
   слияний»). Альтернативы добавлены в 19B.
3. **Stage 21 (выполнено):** передача админки по §5.5: известный
   получатель по постоянному Telegram ID, неизвестный — через одноразовую
   ссылку с SHA-256 в БД, первый открывший лишь кандидат, утверждение
   инициатором, подтверждение в своём аккаунте, ровно 24 часа, отмена/отказ,
   `superseded` после Stage 20A и атомарная смена `place_admins`.
4. **Stage 22 = кирпичики.**
   - **22A (выполнено):** реестр `bricks`, миграция 6, девять системных
     кирпичиков с постоянными ID/кодами (Приложение А), каталог типов,
     строгие правила, пять состояний значений и Telegram-независимый
     сервис с правами/приватностью. `drafts`/WIP/фото не меняются,
     `draft_values` ещё нет. Фотографии в реестр не входят.
   - **22B (выполнено):** Telegram-раздел «Мои кирпичики», миграция 7
     (`revision`, `brick_sessions`), все семь типов, правка, мягкий архив/
     восстановление и пожизненная резервация кодов. Предложения кирпичиков
     реализованы в Stage 26; значения нового мастера — в
     `announcement_values` (Stage 27A); шаблоны и парсер — в Stage 23–25.
5. **Stage 23 = шаблоны (выполнено).** Миграция 8: `templates`,
   `template_versions`, `template_version_bricks`, `template_snapshots`,
   `template_snapshot_bricks`; стандарты мест с авто-созданием и backfill,
   личные шаблоны с владельцем, неизменяемая история, снимки,
   оптимистичная блокировка по `revision` и защита прав. Девятишаговый
   мастер поста не изменён; новый мастер хранит значения отдельно
   (Stage 27A), `draft_values` не создавался.
6. **Stage 24 = парсер/рендер (выполнено).** Миграция 9 добавляет `code` в
   привязки версии и снимка. Модули `template_parser.py` (лексер,
   грамматика, экранирование) и `template_compiler.py` (привязка к
   постоянным `brick_id`, сборка версии и рендер сохранённой версии).
   Грамматика: `[код]` — кирпичик; `[[ ]]` — литеральные скобки; `\*`,
   `\_`, `\~`, `\(`, `\)` — экранирование спецсимволов; `**жирный**`,
   `*курсив*`, `__подчёркивание__`, `~~зачёркнутый~~` — форматирование;
   `Текст(https://url)`, `[место]([ссылка_места])` — гиперссылки.
   Разбор/привязка выполняется при сохранении, подстановка значений — при
   рендере снимка. Согласование кирпичиков реализовано в Stage 26;
   сервис нового мастера — в Stage 27A, Telegram-UI — в Stage 27B;
   фото и публикация нового мастера реализованы в Stage 27C; с S5
   девятишагового мастера нет.
7. **Stage 25 = редактор шаблонов (выполнено).** Миграция 10
   (`template_sessions`). Раздел «📄 Мои шаблоны» и `/templates` для всех;
   личные шаблоны изолированы по владельцу. Создание из стандарта или
   «пустого» шаблона, карточка, редактирование шапки/тела/подвала полным
   сообщением, проверка синтаксиса Stage 24, разрешение неоднозначных
   кодов кнопками с сохранением конкретного `brick_id`, предпросмотр,
   доступные кирпичики, подтверждённый сброс к встроенному стандарту.
   Стандартный шаблон открывается из кабинета админа места и доступен
   действующему админу/суперадмину; права перепроверяются сервисом.
   Конфликты revision, повторные/устаревшие callback, двойное сохранение
   и рестарт не перезаписывают чужие изменения; введённый текст
   сохраняется для повторной попытки. WIP-пост не затрагивается,
   `/templates` в мастере сохраняет WIP. Фотографии остаются отдельными
   вложениями.
8. **Stage 26 = согласование кирпичиков (выполнено).** Миграция 11
   (`brick_proposals`), мастер предложения и «Мои предложения», очереди
   места/суперадмина, approve/reject/withdraw с подтверждениями, снимок,
   транзакционное создание отдельного кирпичика, best-effort уведомления.
   Предложения бессрочны до решения или отзыва; `revises_id`, `expired` и
   `superseded` у предложений нет.
9. **Stage 27A = фундамент адаптивного мастера, Stage 27B = UI,
   Stage 27C = фото/публикация (выполнено).** Миграция 12
   (`announcement_drafts`/`announcement_values`/`announcement_extras`),
   `announcement_wizard.py` (снимок, вопросы по `brick_id`, значения места,
   все семь типов) и `announcement_store.py` (место/альтернатива, шаблон,
   значения, extras, этапы, рестарт, optimistic locking). Stage 27B
   добавил меню «📝 Новый мастер», отдельный именованный
   `ConversationHandler`, выбор места/шаблона, вопросы, extras,
   предпросмотр и сохранение черновика. Stage 27C добавил миграцию 13
   (`photos` и замороженный publication payload), отдельную коллекцию
   вложений, явное подтверждение и отправку существующим sender с
   прогрессом по операциям, продолжением `partial` и защитой от повторной
   публикации. Новый анонс изолирован от легаси-черновиков/WIP; старый
   девятишаговый мастер, парсер, права и исторические snapshots не
   изменены.
10. **Stage 28 = сквозная интеграция:** регресс всех этапов, тесты
    совместимости черновиков, полное UI-покрытие и чек-лист
    миграций (временные БД).

### 12.3. Почему не объединять

Каждый этап заканчивается зелёным регрессом (после Stage 25 — 1077,
после Stage 26 — 1146, после Stage 27A — 1189, после Stage 27B — 1208,
после Stage 27C — 1226, после Stage 28 — 1237 тестов) и не пересекается с
соседями по файлам: конфликтов параллельной правки одной поверхности
минимум. Объединение «кирпичики+шаблоны» или «парсер+редактор» сделало бы
один этап вдвое больше самого крупного из существующих (Stage 17) и
нарушило бы правило независимых приёмок.

---

## 13. Обнаруженные неоднозначности и рекомендуемые решения

| # | Неоднозначность ТЗ | Рекомендация |
|---|---|---|
| 1 | «Публикация кирпичика глобально суперадмином» — каков итоговый статус? | Создаётся отдельный `Brick` `scope='system'` с тем же `code`; автор предложения хранится в `brick_proposals.author_user_id`, а `created_by_user_id` нового кирпичика — суперадмин, принявший решение. Исходный личный кирпичик не меняется. Одобренный код закрепляется за кирпичиком; новые дубликаты блокируются |
| 2 | Кто может сливать дубли мест | Явное слияние — операция администратора места/суперадмина (рекомендуется в Stage 20); пользователю — только подсказка «похожее место уже есть» |
| 3 | «Выбрать альтернативное название» — что именно сохраняется? | `announcement_drafts` фиксирует `place_id`, `place_name_id` и снимок `place_display_name`; альтернатива не создаёт место и не меняет каноническое название. Telegram-показ реализован в Stage 27B. Если вариант должен стать постоянным альтернативным названием, это отдельное явное действие «Добавить название» |
| 4 | Формат ссылок `[место]([ссылка_места])` с подставлением URL | Гиперссылка рендерится только с допустимой целью; неизвестная/невалидная цель → текст без ссылки + диагностика предпросмотра |
| 5 | Несколько экземпляров одного кирпичика | Для extras разрешены: `occurrence` в `announcement_extras` и снимке; в списках «доп. информации» экземпляры различимы. В Stage 27A сервис создаёт `occurrence=1`, повтор кирпичика в тексте рендерится из одного значения |
| 6 | Время жизни заявки на кирпичик | **Бессрочно:** `pending` до решения администратора/суперадмина или отзыва автором. Автоматического истечения через 30 дней и статуса `expired` нет |
| 7 | Что видит пользователь по кнопке «Я админ места» | Только контакт суперадмина из конфига; заявка не создаётся, права не выдаются |
| 8 | Как передать админку незнакомому получателю | Одноразовая ссылка: открытие фиксирует только кандидата; инициатор явно утверждает конкретный user_id, получатель подтверждает в чате. Токен сам по себе прав не даёт и не доказывает личность |
| 9 | Фотографии в шаблоне | Фотографии — **вложения анонса, а не кирпичик**: токена `[фото]`, узла Photo и типа `photos` нет. Коллекция 0..N фото/альбомов не зависит от шаблона; отсутствие фото не блокирует создание |
| 10 | Видимость личных шаблонов | Только владелец; публикация/шаринг не входят в roadmap 19–28 |
| 11 | Идут ли старые черновики через новую модель при открытии | Нет: читаются легаси-адаптером; автоматической конвертации нет. Новый мастер Stage 27A/27B хранит данные в `announcement_*`; явная конвертация — только будущая отдельная операция |
| 12 | Где живут правила ввода кирпичиков (лимиты, валидация, интерфейс) | В `rules_json` как данные; тип и его интерфейс — в каталоге типов; миграции схемы не требуются. Произвольных молчаливых обрезаний нет: платформенные лимиты — разбиение или явная ошибка без потери данных |
| 13 | Что входит в Stage 19B после сокращения 19A | 19A — инфраструктура (выполнено). 19B — создание/поиск/выбор места в чате и альтернативы (выполнено). Шаблон v1 остаётся в Stage 23, интеграция места в новый мастер реализована в Stage 27B |

---

## 14. Технические риски и способы их уменьшения

| Риск | Последствие | Смягчение |
|---|---|---|
| Разрастание `post_creation.py` (2805 строк) до нечитаемости | Регрессии, конфликты правок | Новые флоу — отдельные модули `handlers/places.py`, `handlers/templates.py`, `handlers/bricks.py`; `post_creation.py` получает только точки входа |
| Грамматика шаблонов недооценена (вложенные стили/ссылки) | Битые анонсы, инъекции | IR-конвейер §7.3, переносимые тесты §11.2.6, отклонение полного Markdown |
| Ленивые статусы передачи забываются | «Вечные» приглашения | Проверка `expires_at` в самой транзакции + тесты на замороженном времени; заявки кирпичиков при этом бессрочны осознанно |
| Двойная запись значений (легаси-колонки и новый мастер) | Расхождение данных | Легаси — только для 9 полей старых черновиков; новый мастер — только `announcement_values`; адаптер чтения — единственный путь к легаси-колонкам |
| Telegram-ограничения (64 Б callback, 100 кнопок, 4096/1024 текста) | Падения экранов | Та же дисциплина Stage 11/17: пагинация, короткие callback, `split_html`; без молчаливых обрезаний |
| Бот не может писать незнакомым пользователям | Передача админки «в никуда» | Ссылка-приглашение с одноразовым токеном; открытие даёт лишь кандидата, нужны утверждение инициатора и подтверждение получателя (§5.5) |
| Перехваченная ссылка-приглашение | Посторонний получает место | Токен не доказывает личность и не даёт прав: `opened_by_user_id` — только кандидат, `to_user_id` утверждает действующий админ, финальное подтверждение — у получателя |
| Миграции повреждают реальную базу | Потеря данных | Нумерованный журнал, контрольные суммы, одна транзакция на запуск, WAL-aware автоматическая копия перед DDL и отмена миграции при сбое копии; тесты на временных БД; реальная база — только после эксплуатационного backup и осознанного запуска |
| Уход от «одного экрана» | Чат переполняется сообщениями | Все новые флоу обязаны рендерить через `screen.render_screen`; приёмочные тесты проверяют отсутствие лишних сообщений (второго экрана) |
| Суперадмин конфиг с ошибкой | Роль потеряна | Валидация `SUPERADMIN_USER_IDS` при старте (фоллбек-список тоже), тест на парсинг конфига |

---

## 15. Примеры типовых сценариев

Формат: выдержки из чата (пользователь ↔ бот). Все экраны — на едином
экране управления; сообщения пользователя удаляются после подтверждённого
сохранения (артефакты чата для читаемости). Сценарии ниже показывают
поток Stage 27B с шаблонами и Stage 27C с фото/публикацией; выбор места
использует существующий поток «📍 Места» → поиск/список → карточка →
подтверждение.

### 15.1. Создание нового места

```
Пользователь: Создать пост
Бот (экран):  Шаг «Место». Введите место или выберите из справочника.
              [Ввести название…] — отправьте текст
Пользователь: Кофейня «Дом»
Бот (экран):  Похожее место уже есть в справочнике:
              • Кофейня «Дом» (центр)      [Подтвердить]
              ↳ это совпадение или ваше — другое место?
              [Вернуться к списку] [Создать новое место «Кофейня „Дом“»]
Пользователь: (кнопка) Создать новое место
Бот (экран):  Место создано и доступно всем.
              Продолжим: [Выбрать шаблон] [◀️ Назад]
```

### 15.2. Альтернативное название для существующего места

```
Пользователь: Кофейня Дом
Бот (экран):  Похожее место уже есть:
              • Кофейня «Дом» (центр)
              [Подтвердить]
              [Выбрать альтернативное название]
              [Вернуться к списку]
Пользователь: (кнопка) Выбрать альтернативное название
Бот (экран):  Название «Кофейня Дом» сохранено как альтернативное у места
              «Кофейня «Дом»». Альтернативу может добавить любой,
              править/удалять — администратор места или суперадмин.
              Продолжим с выбора шаблона: [Дальше]
```

### 15.3. Передача администрирования (получатель уже известен боту)

```
Админ (кабинет места): Передать админку → вводит @anna_pk
Бот (экран админа):    Найден пользователь Anna (id 555, писала боту ранее).
                       Заявка действует 24 часа. Место пока остаётся вашим.
Получатель (личный чат с ботом):
                       Администратор места «Кофейня «Дом»» передаёт вам
                       управление этим местом.
                       [Подтверждаю] [Отклоняю]
Получатель: (кнопка) Подтверждаю
Бот:   Роль передана атомарно: Anna — администратор места; прежний админ
       полностью потерял права управления этим местом.
       (Повторное нажатие кнопки при уже подтверждённой передаче отвечает
       честно: «Передача уже подтверждена» — без новых действий.)
```

### 15.4. Личный кирпичик и предложение его месту

```
Пользователь: (в «🧱 Мои кирпичики» уже есть личный [age_limit])
Пользователь: (кнопка 📨 в строке кирпичика «Возрастное ограничение»)
Бот (экран):  Куда предложить? [📍 Конкретное место] [🌍 Вся система]
Пользователь: (кнопка) 📍 Конкретное место
Бот (экран):  Выберите место: [Кофейня «Дом»] [◀️ Назад]
Пользователь: (кнопка) Кофейня «Дом»
Бот (экран):  Проверьте предложение: название, [age_limit], тип, настройки,
              место. [📨 Отправить] [◀️ Назад]
Пользователь: (кнопка) 📨 Отправить
Бот (экран):  Предложение отправлено. Срока давности нет: оно ждёт
              решения или отзыва автором.
Админ места (кабинет места → «🧱 Предложения кирпичиков»):
              Пользователь предлагает кирпичик «Возрастное ограничение»
              (код age_limit, тип «текст») для места.
              [Одобрить] [Отклонить]
→ Одобрено: отдельный кирпичик создаётся как scope='place' в той же
  транзакции; повторное решение отвечает «Предложение уже рассмотрено».
```

### 15.5. Редактирование шапки стандартного шаблона

```
Админ (кабинет места → Стандартный шаблон):
              Секции: [Шапка] [Тело] [Подвал] [◀️ Назад]
→ Шапка:      Текущее содержимое:
              ─────────────
              🎬 [название]
              [дата], [время_начала]
              ─────────────
              Пришлите новый текст шапки. [Сбросить к стандартному]
Админ:        Лекция [название] у [автора]
Бот (экран):  ⚠️ Кирпичик [автора] не найден. Похожие: [ведущий].
              Секция пока не сохранена — пришлите исправленный текст.
Админ:        Лекция [название] ([дата], [время_начала])
              Билеты(https://lampushka.ru)
Бот (экран):  Предпросмотр шапки со значениями этого анонса:
              Лекция о городе (22.10.2026, 19:00)
              Билеты → https://lampushka.ru (гиперссылка)
              [✅ Сохранить секцию] [◀️ Исправить]
→ Сброс по кнопке (только после подтверждения, затрагивает только шапку):
              возвращается seeded-текст шапки; создаётся версия vN+1.
```

### 15.6. Применение личного шаблона к выбранному месту

```
Пользователь: Создать пост → место «Театр Лампушка»
Бот (экран):  Выберите шаблон:
              • Стандартный шаблон места      [Выбрать]
              • Мои шаблоны: «Выставка»        [Выбрать]
Пользователь: (кнопка) «Выставка»
Бот:          Шаблон применён к месту. Снимок шаблона зафиксирован в анонсе.
              Вопросы (по кирпичикам без значений):
              − [дата]          → календарь Stage 17
              − [время_начала]  → текст
              кирпичики [место] и [ссылка_места] НЕ спрашиваются:
              значения берутся из справочника мест.
              Фотографии в Stage 27C добавляются на экране «📷 Фотографии»
              (одиночные/альбомы) и не задаются шаблоном. С Stage 28
              последний ответ сам отправляет готовый анонс в личный чат
              отдельным сообщением; отдельной кнопки публикации нет.
```

### 15.7. Добавление отсутствующего кирпичика в конкретный анонс

```
Превью: … 📍 Театр Лампушка …
Пользователь: (кнопка) ➕ Доп. информация
Бот (экран):  Доступные кирпичики (не в шаблоне):
              [Время окончания] [Гардероб] [Цена] [Контакты…]
Пользователь: Время окончания
Бот:          Значение: (ввод) 21:30
Бот:          Куда вставить? [Шапка] [Тело] [Подвал]
              → секция «Тело»: [В начало] [В конец] → «В конец»
Бот (экран):  Кирпичик добавлен ТОЛЬКО в этот анонс (шаблон места не менялся).
              [✏️ Изменить значение] [🗑 Удалить]
              (изменение позиции после добавления и drag-and-drop —
              за границами Stage 27B)
```

### 15.8. Передача админки незнакомому получателю (двусторонняя привязка)

```
Админ (кабинет): Передать админку → @new_person
Бот:  Пользователь @new_person боту ещё не писал. Передайте ему ссылку:
      https://t.me/<bot>?start=tf_42_<token>
      Ссылка действует 24 часа. Открытие ссылки само по себе прав не даёт.
Получатель (открыл ссылку): Приглашение открыто. Это ещё не передача:
      инициатор должен утвердить вас, после чего вы подтвердите согласие.
      [Главное меню]
Админ (экран): Ссылку открыл пользователь New Person (id 777).
      [Утвердить получателя] [Отменить передачу]
Админ: (кнопка) Утвердить получателя
Получатель (личный чат): Вам передают управление местом «Кофейня «Дом»».
      [Подтверждаю] [Отклоняю]
Получатель: Подтверждаю
Бот:  Роль передана атомарно. Если ссылку открыл не тот человек, админ
      отменяет передачу до подтверждения — права остаются у прежнего админа.
```

---

## Приложение А. Соответствие системных кирпичиков и легаси-полей

Stage 22A сидирует миграцией 6 **девять** системных кирпичиков с
фиксированными (неизменяемыми) ID и кодами. Восемь текстовых полей анкеты
и адрес места из справочника дают девять кирпичиков; у адреса нет отдельной
легаси-колонки. Девятый шаг анкеты — изображение — кирпичиком не является:
фотографии остаются отдельной коллекцией вложений анонса
(`Draft.image`/`Draft.photos`).

| ID | `code` | Тип | `source` | Легаси-колонка `drafts` |
|---:|---|---|---|---|
| 1 | `название` | short_text | ask | `title` |
| 2 | `дата` | date (горизонт: сегодня + 12 календарных месяцев) | ask | `date` |
| 3 | `время_начала` | time | ask | `time_start` |
| 4 | `время_окончания` | time | ask | `time_end` |
| 5 | `место` | short_text | place | `place_name` |
| 6 | `адрес_места` | short_text | place | — (`places.address`) |
| 7 | `ссылка_места` | url | place | `place_url` |
| 8 | `описание` | long_text | ask | `text` |
| 9 | `контакт` | short_text | ask | `contact` |

`image`/`photos` в этот список не входят и не получают `code`: шаблон не
управляет изображениями, а публикация добавляет их как вложения.

Дальнейшие пользовательские/стандартные кирпичики (например, `цена` —
number с суффиксом ₽, `ведущий` — short_text, `возраст` — choice) появятся
через реестр и предложения Stage 26. Их не нужно добавлять миграцией
схемы: правила хранятся в `rules_json`, но **системные** определения
меняются только контролируемой миграцией, а не повторным сидированием при
старте.

Правила отображения определяются типом: текст — как есть, URL — как ссылка
после `normalize_url`, число — с суффиксом из `rules_json`. Отдельной
колонки `display` в Stage 22A нет.

## Приложение Б. Словарь терминов

| Термин | Значение |
|---|---|
| Кирпичик | Типизированное **текстовое** поле; реестр в `bricks`; в шаблоне пишется `[код]`. Фотографии кирпичиком не являются |
| Область кирпичика (scope) | `system` — общий для всех; `place` — утверждён админом места; `personal` — личный |
| Шаблон | Три секции текста с `[кирпичиками]`; версии в `template_versions` |
| Снимок шаблона | Закреплённая в анонсе копия секций — защита от правок шаблона; вложений не содержит |
| Вложение анонса | Фотографии/медиагруппы конкретного анонса (`Draft.image`/`photos`); не кирпичик, не узел шаблона, необязательны |
| IR | Промежуточное представление секции (узлы §7.3) |
| Заявка | Предложение кирпичика админу места / суперадмину (`brick_proposals`); без срока давности |
| Передача | Назначение нового админа места через двустороннее подтверждение (`admin_transfers`) |
| Доп. информация | Вставка лишнего кирпичика в конкретный анонс (`announcement_extras` Stage 27A; UI — Stage 27B, реализовано) |
| name_search | Нормализованное имя места для поиска/сравнения |
