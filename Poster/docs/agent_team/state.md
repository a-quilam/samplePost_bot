# Состояние команды Poster

## Этап U1a — Город, стандартный шаблон v4, автозаполнение места

**Актуальное состояние (сверка 2026-10-30, машина hp):** Poster 2.1 (R1–R5)
принят и развёрнут. По заданию владельца «Poster 2.2» начата программа
U1–U5 (исправление подтверждённых UX-дефектов). **U1a принят** (этап,
коммит в момент приёмки): миграция 21 (места: колонка city + системный
кирпичик «город» id=11), миграция 22 (BUILTIN v4: дата/время/место/адрес в
шапке, тело=описание, подвал=контакт), автозаполнение живого анонса
данными места (re-read снимка при фиксации шаблона), город в списках/
поиске/карточках/регистрации/редактировании. Тестов: 1473 OK.
**U1b принят** (908ade8 + 86fb9c5): сохранение Telegram-форматирования —
capture_message_text (entities во всех точках ввода), каноническая модель
SafeHtml с allow-list санитизацией при записи и чтении, рендер-шов
(значения не экранируются повторно, вложенность с разметкой шаблона),
LinkPreviewOptions(is_disabled=True) во всех точках отправки, усечение
длинных HTML-значений на экранах, strip_link_tags в подписях ссылок.
Тестов: 1522 OK.
**U2 принят** (c0a5964 + bb5ed6b): компактный календарь (8 быстрых дат
скользящим окном 2×4, два 7-дневных диапазона в строке, сетка ≤12 месяцев
со стрелками, дубли-кнопки удалены, legacy-redirect), фото-шаг как обычный
вопрос (ровно 2 кнопки, автозавершение по фото/альбому, подпись
игнорируется), компактный экран завершения (2 кнопки, инвариант порядка
message_id через _ensure_service_screen_below), компактный экран
редактирования готового анонса, пересборка живого блока при смене состава
фото (дефект потери фото закрыт). Тестов: 1633 OK.
**U3 принят** (09c33d4 + 063c4a6): исправления №8/№9/№13 — дубль кнопки
предложения кирпичиков удалён (только 📨 в строке), utils/keyboard_layouts
(переиспользуемые раскладки), общий доменный fallback в errors.py (доменные
ошибки → понятный alert + главное меню вместо «внутренней ошибки»), фазы
в allow-listах возвратов (await_new_name, конфликты), alias_warning fallback.
Тестов: 1727 OK (краулер тестировщика: 502+ back-кликов, 0 внутренних
ошибок). U4–U5 — впереди.
**U4 принят** (b75e673 + 604462b): исправление №10 — единый каталог
текстов config/ui_texts.yaml (140 ключей, defaults+валидация, tr с
экранированием, README docs/ui_texts.md); №12 — deep links площадок
(миграция 23 place_invite_links, случайный токен ~128 бит, resume
черновика, права/отзыв/перевыпуск, доменный fallback). Тестов: 1853 OK.
**U5 приёмка пройдена** (9de9684): все 10 контрольных сценариев владельца
подтверждены независимой интеграционной приёмкой (пробники FakeBot);
устранён визуальный дубль текстового анонса без фото (промоушен живого
блока: одна копия выше служебного экрана; повтор после сбоя чистит
stale-блок). Тестов: 1871 OK. CI success. U5b выполнен: тестовая БД
забэкапирована/архивирована (backups/, SHA-256 подтверждены), создана
чистая штатными миграциями (schema_migrations=23), бот развёрнут
(active, лог чист).
**Poster 2.2 завершён полностью.**

**Checkpoint Poster 2.3 (старт 2026-10-31, по заданию владельца).**Программа H1–H4, автономно, без промежуточных вопросов владельцу:
- **H1 — стабильность + форматирование:** авто-resume активного черновика
  при потере состояния ConversationHandler (рестарт службы теряет
  in-memory состояние → текст проваливается в «Воспользуйтесь кнопками
  меню…» и теряется ввод; восстановление по БД-черновику в личном чате,
  диагностический лог без секретов); поддержка `date_time` entity
  (`<tg-time>` + allow-list, безопасная деградация); регрессионные тесты
  цитат (blockquote/expandable/multiple/рядом с bold/spoiler, раунд-трип
  через БД, редактирование, с фото и без).
- **H2 — единый шаблон:** канонический `content` вместо header/body/footer,
  опциональный `extended_content` в `template_versions`; миграция 24
  (перенос секций через `"\n".join(непустые)`); снимки хранят оба текста;
  extras без section/position, рендер расширенного шаблона при выбранных
  extras; скрытие строк с brick-токенами, рендерящимися пустыми.
- **H3 — редактор/UX:** список доп. кирпичиков (brick-токены расширенного,
  выбор → значение → рендер → возврат), главный экран правки
  (`📝 Данные | 🖼 Фото`, `🧱 Доп. поля`, `🏠 В меню`), управление площадкой
  парами `✏️ Адрес | 🗑` / `✏️ Ссылка | 🗑` с подтверждением, единый
  компонент «Текущее значение» (разделители, без `<code>`, без обрезки),
  клавиатуры ≥2 кнопки в строке, snapshots вместо скриншотов.
- **H4 — тексты + приёмка + релиз:** полнота `config/ui_texts.yaml`,
  обновление docs/ui_texts.md, полный `make check`, независимая приёмка,
  commit/push, зелёный CI, WAL-бэкап в backups/ (integrity/FK/SHA-256),
  очистка БД штатным путём, миграции/bootstrap, перезапуск служб,
  polling без restart-loop.

Порядок на каждый этап: ожидаемое поведение → тест → воспроизведение
старого дефекта → фикс → целевые проверки → независимый контроль →
коммит. Финальная визуальная приёмка — владельцу (FakeBot ≠ реальный
Telegram).

**H1 принят (2026-10-31).** Авто-resume активного черновика при потере
in-memory состояния ConversationHandler (рестарт службы): снимок шага
(user_states.payload), AnnouncementResumeHandler — только private chat,
при отсутствии активного диалога; после рестарта текст/фото продолжают
активный шаг и сохраняются в черновик; группы, отсутствие черновика и
явные выходы (/cancel, /start, /help, «🏠 В меню») дают прежнюю
подсказку меню, значения не затираются (дефект найден независимым
тестировщиком: post-restart выходы не снимали снимок → затирание
значения; закрыт: _drop_resume в main_menu + drop_resume_snapshot).
Диагностический лог без секретов. entity date_time деградирует в
читаемый текст (format_date_time, APP_TZ; без unix_time — исходный
текст), <tg-time>+unix/format в allow-list (санитизация подтверждена
adversarial-пробами ревизора). Регрессионные тесты цитат: multiple/
expandable blockquote, соседство с bold/spoiler/code, БД-раунд-трип,
правка, доставка с фото и без. Тестов: 1907 OK (было 1871), ruff/black/
mypy чисто. Ревизор: approved, критических замечаний нет (квирк resume
group→private — осознанное решение, доставка и так в личный чат).
Файлы: announcement_wizard.py, main_menu.py, tg_html.py,
test_poster2_h1_stability_formatting.py (новый, 36), test_stage21_transfer_flow.py.

Единый документ состояния, который ведёт `poster-manager` после каждого
завершённого этапа. Владелец читает его, чтобы понять, где остановилась
команда. Новый большой этап начинается только по отдельному сообщению
владельца.

## Шаблон записи

```
## Этап <id> — <название>
Дата: <YYYY-MM-DD>
Статус: done | blocked | failed
Задание: <1–3 предложения>
Изменено: <файлы>
Проверки: <команда → итоговая строка>
Замечания ревизора: <кратко и как закрыты>
Открытые вопросы: <список>
Коммит: <hash или «нет»>
Push: <нет | выполнено по разрешению>
```

---

## Журнал

## Этап U5 — Интеграционная приёмка и финальные фиксы
Дата: 2026-10-30
Статус: done
Задание: финальная приёмка по 10 контрольным сценариям владельца;
устранение найденных дефектов соответствия спеке.
Результаты приёмки:
- Сценарии 1–10 все pass (пробники /tmp/u5/*.py, FakeBot, временные
  SQLite; 177 целевых тестов этапов + собственные пробы).
- Кандидаты: plm_back из города — не дефект (пробник шёл не тем путём);
  aw_manage_back — существует (announcement_wizard.py:1940/:5480),
  IndexError был дефектом пробника; живое превью 103 рядом с постом 105 —
  ДЕФЕКТ соответствия poster2_spec.md §4.1/§4.2 (визуальный дубль).
Изменено (U5-fix + U5-D1):
- `Poster/handlers/announcement_wizard.py` — гейт промоушена расширен на
  текстовые анонсы (publication_status is None и нет previous_message_ids
  → живой блок доводится editMessageText до финального HTML и признаётся
  доставленным; вторая копия не отправляется); text=preview.html в
  _sync_live/_promote_live_post (publication_text == текст в чате);
  fallback stale_live_ids: при невозможности промоушена — штатная
  доставка + удаление живого блока после подтверждения + очистка
  live_message_ids; aw_pub_resume снимает stale_live_ids из БД до
  отправки и передаёт в _run_announcement_publication (повтор после
  сбоя не оставляет дубля).
- `Poster/tests/integration_harness.py` — delivered_posts/content_text/
  _delivered_history/current_post_ids; posts()/post_records() учитывают
  живой блок как доставленный пост.
- Тесты: test_poster2_delivery (+TextAnnouncementSingleCopyTests 7,
  +3 регрессии повтора/отказа delete/чужой блок),
  test_poster2_u5_duplicates_tester (новый, 9); актуализированы
  stage27c, stage29, u1b (без ослабления — инвариант «ровно одна копия
  текста в чате, она выше служебного экрана»).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1871 tests ... OK`,
  ruff/black/mypy чисто (170 файлов).
- Мутационная проверка: возврат гейта `preview.draft.photos and` →
  3 красных; откат передачи stale_live_ids в aw_pub_resume → 3 красных.
- poster-tester (независимо): полная матрица дублирования — одна копия
  во всех штатных путях, замена после правки, повтор без отправки,
  фото/альбом без регрессии, рестарт.
Замечания ревизора: approve. Неблокирующие: дублирующий параметр
html/text в _promote_live_post; stale_live_ids вычисляется при успешном
промоушене зря; отказ Telegram в delete → копия остаётся (известное
ограничение §4.2).
Открытые вопросы: нет блокирующих.
Коммиты: 9de9684 (U5). Push: выполнен. CI: success.
Дальше: очистка тестовой БД (WAL-бэкап → архив → чистая БД штатными
миграциями → bootstrap-проверка) и развёртывание.

## U5b — Очистка тестовой БД и развёртывание
Дата: 2026-10-30 22:07–22:42
Статус: done
Порядок (выполнил poster-developer, менеджер верифицировал):
1. Останов бота: `systemctl --user stop poster-bot.service` → inactive.
2. WAL-бэкап: sqlite3 backup API →
   `backups/poster_preclean_20261030_220737.db` (434 176 B,
   SHA-256 816b7df2…142b29, integrity ok, FK-check 0 строк).
3. Архив тестовой БД (mv, не rm):
   `backups/archive/post_bot_20261030_220737.db`
   (SHA-256 1cd3bef9…4ccd3e) + легаси-копия
   `post_bot.db.backup-20261030T185949513960Z-p468027` (+wal/shm).
4. Чистая БД штатным путём `database.init_db()` → `run_migrations()`:
   25 таблиц, schema_migrations max=23, integrity ok, FK 0.
   user_version=0 — проект ведёт версию в журнале schema_migrations
   (PRAGMA не используется, механику не меняли).
5. Bootstrap: системные кирпичики 11 (включая «город»), шаблон v4
   проверен (builtin_version=4, 8 привязок); users/places/drafts/
   proposals/invite_links = 0; суперадмин/токен/контакт — вне БД
   (EnvironmentFile юнита), подхватываются при старте.
6. Запуск: active (running), NRestarts=0, лог «Запуск бота» →
   «Application started», 0 traceback/ERROR, 0 токенов в логах.
Прокси tg-connect-proxy.service — active, не трогался.
Примечание о пути бэкапа: /home/mush смонтирован read-only (rw только
poster-dev, opencode-каталоги, /var/tmp), требуемый
/home/mush/backups/poster/ недоступен; по решению владельца бэкапы
размещены в /home/mush/workspaces/poster-dev/backups/ (700, вне git —
backups/ в .gitignore:176). SHA-256 подтверждены после переноса.
Открытые пункты (не блокирующие): PRAGMA user_version не выставляется
механизмом миграций (задокументировано); периодические
telegram.error.TimedOut через CONNECT-прокси — известная сетевая
особенность, не критично.


## Этап U4 — Каталог текстов и deep links (U4a + U4b)
Дата: 2026-10-30
Статус: done
Задание: исправления №10 (единый каталог текстов интерфейса), №12
(ссылка быстрого создания анонса площадки).
Изменено:
- `Poster/config/ui_texts.yaml` (новый, 140 ключей, 9 секций,
  комментарии) + `Poster/utils/ui_texts.py` (новый): DEFAULTS,
  загрузка с валидацией (битый YAML/cp1251/обрезанный UTF-8 → дефолты +
  CRITICAL, импорт не падает), tr()/tr_safe() (экранирование значений,
  обязательные переменные, однопроходная подстановка), reload_texts(),
  CLI-валидация (exit 0/1 без traceback).
- Перенос литералов в handlers/{main_menu,wizard_questions,
  wizard_photos,announcement_wizard,bricks,places,admin,templates}.py,
  errors.py, utils/screen.py (~147 мест); мёртвые ключи подключены
  (nav.prev/next/retry_search, places.card.canonical); дубли литералов
  сняты (buttons.finished.apply и др.).
- `Poster/docs/ui_texts.md` (новый) — инструкция владельцу.
- №12: `Poster/migrations.py` (миграция 23 place_invite_links), models.py
  (PlaceInviteLink), place_invite_store.py (новый; secrets.token_urlsafe,
  отзыв/перевыпуск, гонки через частичный UNIQUE-индекс), utils/deep_links.py,
  handlers/place_invite.py (новый; InviteStartHandler, open_invite_payload,
  resume незавершённого черновика той же площадки), карточка места
  (кнопка/показ/перевыпуск), errors.py (PlaceInviteError в доменные).
- Тесты: новые u4a_texts (30+), u4a_texts_tester (24), u4a_verify_tester
  (~20), u4b_deeplink, u4b_deeplink_tester, u4b_defects_tester,
  u4b_acceptance_tester; актуализированы stage22a/b, stage26, r3.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1853 tests ... OK`,
  ruff/black/mypy чисто.
- Мутационная проверка: tr→литерал в меню (2 красных), UnicodeDecodeError-
  ветка (5 красных), возврат литералов D2/D3 (красные), revoked_at
  (красный), существование площадки (красный), resume (2 красных).
- poster-tester (независимо): сценарий 10 владельца (правка YAML →
  экраны, битый YAML → дефолты, инъекции экранированы, CLI, README);
  сценарий 9 (ссылка → другой пользователь → конструктор сразу; новый/
  существующий/WIP/отказы/права/идемпотентность). Найдены и закрыты:
  D1 не-UTF-8 ронял импорт; D2 мёртвые ключи; D3 дубли литералов;
  дубль черновика при повторном /start; гонка прав → внутренняя ошибка.
Замечания ревизора: approve (U4a: экранирование/устойчивость/README;
U4b: токен/права/resume/миграция/регресс /start). Неблокирующие:
импорт-тексты фиксированы на старте (рестарт — задокументировано);
permission-wording «экран устарел» вместо «нет доступа»; часть навигации
admin/bricks/templates ещё литералы (зафиксировано в docs).
Открытые вопросы: нет блокирующих.
Коммиты: b75e673 (U4a), 604462b (U4b). Push: выполнен. CI: success (оба).

## Этап U3 — Кирпичики, возвраты, раскладки (U3a + U3b)
Дата: 2026-10-30
Статус: done
Задание: исправления №8 (дубль кнопки предложения), №9 (все кнопки
возврата), №13 (компоновка inline-кнопок).
Изменено:
- `Poster/handlers/bricks.py` — общая «📨 Предложить кирпичик» удалена;
  предложение только из строки кирпичика (📨 справа); bp_start без id →
  безопасный alert (PROPOSAL_CREATE_HINT на пустом хабе); мастер 3 шага;
  подсказки пустых состояний не ссылаются на несуществующие кнопки.
- `Poster/utils/keyboard_layouts.py` (новый) — button/rows_of/pair/
  list_rows/nav_row/danger_row; применены в bricks, wizard_photos,
  wizard_questions (дни календаря), announcement_wizard (пара даты,
  месяц-сетка, nav, _place_buttons).
- `Poster/errors.py` — общий механизм: is_expected_domain_error →
  DOMAIN_NOTICE_TEXT + fallback в главное меню вместо «⚠️ Произошла
  внутренняя ошибка» (программные исключения падают громко как раньше,
  права-отказы не превращаются в fallback).
- `Poster/handlers/places.py` — _pending_alias_text (валидация до
  find_name_conflicts), alias_warning без названия → карточка + notice.
- `Poster/handlers/templates.py` — await_new_name в allow-list
  handle_back/handle_hub.
- `Poster/handlers/admin.py` — allow-list: handle_open_place
  (+conflicts_list), handle_conflicts_open (+conflict_card),
  handle_conflict_open (+rename/switch).
- Тесты: новые u3a_bricks (10+2), u3a_bricks_tester (7), u3a_layouts (22),
  u3a_layouts_tester (12), u3a_acceptance_tester (16), u3b_back (25);
  актуализированы stage26, r4_ux.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1727 tests ... OK`,
  ruff/black/mypy чисто.
- Мутационная проверка: общая кнопка вернута → 5 красных; хелперы
  сломаны → до 11 красных; await_new_name убран из allow-list → 3
  красных; alias_warning исключение → 7 красных; admin allow-list →
  красные цепочки конфликтов.
- poster-tester (независимо, U3a): хаб/предложения/раскладки/негативы;
  найдены и закрыты 2 текстовых дефекта-подсказки (пустой хаб, пустые
  предложения).
- poster-tester (независимо, U3b): живой BFS 573 экрана / 502 back-клика
  + рестарт 154 клика + фаззинг 132 кейса — 0 внутренних ошибок.
Замечания ревизора: approve. Неблокирующие: permission-ошибки дают
wording «экран устарел» вместо «нет доступа» (доступ не открывают);
handle_hub из AWAIT_NAME без notice (асимметрия, потери нет);
_pending_alias_text не проверяет длину (уходит в общий fallback);
тест (г) fallback классов — только PlaceNameError прямой.
Открытые вопросы: нет блокирующих.
Коммиты: 09c33d4 (U3a), 063c4a6 (U3b). Push: выполнен. CI: success
(оба).

## Этап U2 — Календарь, фото-шаг, завершение (U2a + U2b)
Дата: 2026-10-30
Статус: done
Задание: исправления №3 (календарь), №5 (фотографии), №6 (завершение).
U2a: новый компактный календарь. U2b: фото-шаг как обычный вопрос, экран
завершения, порядок сообщений.
Изменено:
- `Poster/utils/calendar_view.py` — переписан: quick_days (ровно 8,
  скользящее окно), followup_ranges (2×7 дней без пересечений/пропусков),
  month_grid (≤12 кнопок) + month_sequence (стрелки до горизонта),
  format_range (год при переходе), is_valid_month_key, horizon-механика.
- `Poster/handlers/wizard_questions.py` — calendar_shortcut_rows: 2 строки
  × 4 даты + 2 диапазона в одной строке + «📅 Выбрать месяц»; дубли-кнопки
  («Текущая неделя», «Следующая неделя», «Другая дата», «Календарь:
  месяцы и диапазоны») удалены.
- `Poster/handlers/announcement_wizard.py` — новые календарные экраны
  (раскрытие диапазона на том же экране, возврат к быстрым датам, месяцы
  со стрелками, валидация года/месяца до date(), _legacy_calendar_redirect
  для старых callback); _render_delivered переписан (точный текст «✅ Анонс
  готов.», ровно 2 кнопки ✏️ Изменить | 🏠 В меню); _render_delivered_edit
  (компактный экран категорий); _ensure_service_screen_below (инвариант
  порядка message_id); _back_from_photo_step; aw_edit/aw_photo_back;
  notice → answer.
- `Poster/handlers/wizard_photos.py` — короткая инструкция, ровно 2 кнопки
  (◀️ Назад | ⏭ Пропустить), автозавершение по фото/альбому сохранено.
- `Poster/handlers/wizard_live.py` — LiveBlock.file_ids + shown_file_ids:
  правка подписи только при совпадении состава, иначе пересборка (дефект
  потери фото при том же числе сегментов).
- Тесты: новые u2a_calendar (18) + u2a_calendar_tester (19) +
  u2a_calendar_verify_tester (приёмка) + u2b_photos (19+) +
  u2b_finish (13) + u2b_*_tester (15, негативы); актуализированы stage17_*,
  stage27b/c, live, menu, delivery, limits, u1b, stage19b, stage29.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1633 tests ... OK`,
  ruff/black/mypy чисто.
- Мутационная проверка: календарь (старое окно недели, MONTH_GRID_MAX=13,
  guard-ы валидации), фото/завершение (старая клавиатура, длинный текст/
  8 кнопок, сломанный порядок, подпись в текст, потеря состава фото) —
  все ловятся.
- poster-tester (независимо, U2a): все дни недели, концы месяца/года,
  високос, матрица клавиатуры, сплошной перебор 1000 дней (полнота
  без пересечений); найдены и закрыты D1 (13 месяцев) и D2 (ValueError
  на подделанном годе).
- poster-tester (независимо, U2b): полный прогон 28 проб; найден дефект
  потери фото при совпадении числа сегментов (унаследованный) — закрыт
  с регрессионным тестом.
Замечания ревизора: approve (U2a: off-by-one не найден, legacy redirect,
IDOR/CAS; U2b: порядок по фактическим id, пересборка без удаления чужих
сообщений, aw_apply идемпотентен). Неблокирующие: на фото-шаге нельзя
удалить принятое фото до финала (правится после доставки через Изменить→
Фотографии — продуктовое следствие «ровно 2 кнопки»).
Открытые вопросы: нет блокирующих.
Коммиты: c0a5964 (U2a), bb5ed6b (U2b). Push: выполнен. CI: success
(runs 41006219765, 41024368328).

## Этап U1b — Сохранение Telegram-форматирования
Дата: 2026-10-30
Статус: done
Задание: исправление №4 (критический дефект целостности данных): entities
пользователя (bold/italic/underline/strike/spoiler/code/pre/blockquote/
expandable_blockquote/text_link) терялись при вводе значений кирпичиков.
Изменено:
- `Poster/utils/tg_html.py` — FormattedText, capture_text/
  capture_message_text (захват entities, UTF-16 offsets), SafeHtml,
  sanitize_html (allow-list: теги/атрибуты Telegram, href только
  http(s)/tg, идемпотентность), _TAG_TAIL_RE (текст `a < b & c` не
  «съедается»), strip_link_tags, `#` в language-class.
- `Poster/announcement_store.py` — payload значений `{"text","html"}`;
  html пишется только через sanitize при видимой разнице; allow-list при
  чтении (defense-in-depth); пустой по видимому тексту html → plain;
  _render_value — шов рендера (SafeHtml для текстовых типов).
- `Poster/template_compiler.py` — SafeHtml не экранируется повторно
  (вложенность с разметкой шаблона); strip_link_tags в подписи LinkNode
  (без nested links).
- `Poster/delivery.py`, `handlers/wizard_live.py`, `utils/screen.py`,
  `handlers/admin.py` — LinkPreviewOptions(is_disabled=True) во всех
  точках; disable_web_page_preview удалён.
- `Poster/handlers/announcement_wizard.py`, `handlers/wizard_questions.py`
  — capture_message_text во всех точках ввода значений; экраны показывают
  сохранённое форматирование (stored_value_html) с усечением
  truncate_html (закрытие тегов, visible ≤ лимита).
- `tests/integration_harness.py` — link_preview_options в
  POST_MESSAGE_KEYS, is_live_record/is_notification_record,
  классификация записей по identity-инстансам.
- Тесты: новые test_poster2_u1b_formatting.py (15) и
  test_poster2_u1b_tester.py (16 + регрессии A/B/C/D + strip_link_tags);
  актуализированы live/s5/humor/calendar/u1a_verify/audit/stage11/stage21.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1522 tests ... OK`,
  ruff/black/mypy чисто (145 файлов).
- Мутационная проверка: 6 мутаций ловятся (плоский захват теряет entities,
  SafeHtml-ветка, link preview, _render_value plain, stored_value_html,
  sanitize при чтении, strip_link_tags).
- poster-tester (независимо): полный цикл 8 состояний (ввод → вопрос →
  возврат → рестарт → завершение → медиа/альбом → редактирование),
  UTF-16 эмодзи+кириллица, безопасность (~40 векторов инъекций), двойное
  экранирование, негативы. Найдены и закрыты дефекты: A/B (длинный
  HTML-значение ломал экран вопроса — нет усечения), C (пустой html в БД
  обнулял анонс), D (language-c#).
Замечания ревизора: approve по 7 компонентам. Неблокирующие:
sanitize_html не балансирует разметку (путь только через правку БД,
capture всегда сбалансирован); text/html расхождение в подделанной строке
(html выигрывает, но allow-list'нут); перекрывающиеся entities и разрезанная
суррогатная пара в entities_to_html — пред-существующее ограничение PTB.
Открытые вопросы: нет блокирующих.
Коммиты: 908ade8, 86fb9c5 (+chore ac34527 — уборка случайного probe.txt).
Push: выполнен. CI: success (run 40987581243).

## Этап U1a — Город, стандартный шаблон v4, автозаполнение места
Дата: 2026-10-30
Статус: done
Задание: по заданию владельца Poster 2.2 (исправления №1 автозаполнение,
№2 город, №7 структура шаблона): миграция 21 (places.city +
announcement_drafts.place_city + системный кирпичик «город» id=11),
миграция 22 (BUILTIN v4), re-read place-снимка при фиксации шаблона,
город в списках/поиске/карточках/регистрации/редактировании, фикс
LinkNode без URL.
Изменено:
- `Poster/migrations.py` — миграции 21, 22; LATEST_VERSION=22.
- `Poster/brick_types.py` — SYSTEM_BRICKS + «город» (id=11).
- `Poster/models.py` — Place.city, AnnouncementDraft.place_city.
- `Poster/place_store.py`, `Poster/place_edit_store.py` — обязательный
  city, validate_place_city, city_set/city_delete + аудит, поиск по городу.
- `Poster/template_types.py` — BUILTIN v4 (шапка: название/дата/время/
  место/адрес; тело: описание; подвал: контакт).
- `Poster/template_compiler.py` — LinkNode без URL → подпись без <a>.
- `Poster/announcement_store.py` — _refresh_place_snapshot при
  choose_announcement_template (re-read места).
- `Poster/handlers/places.py`, `handlers/admin.py`,
  `handlers/announcement_wizard.py` — фаза create_city_input, город в
  карточках/списках/поиске/редактировании (adm_edit_city_*), возвраты,
  «Название · Город» через utils/place_labels.
- `Poster/utils/place_labels.py` (новый) — единая place_title.
- `Poster/docs/migrations.md` — версии 21, 22.
- Тесты: новые test_poster2_u1a_{migrations,place_city,template_v4,
  autofill}.py (57) и независимый test_poster2_u1a_verify_tester.py (22);
  актуализированы ~30 легаси-модулей (версия 22, 11 кирпичиков, v4).
Проверки:
- `make check PYTHON=.venv/bin/python` → ruff/black/mypy чисто,
  `Ran 1473 tests ... OK`.
- Мутационная проверка (разработчик): 7 мутаций ловятся (re-read снимка,
  LinkNode без URL, обязательный city, отбор миграции 22, занятый id=11,
  place_city в снимке, [город] в шаблоне → 9 fail).
- poster-tester (независимо): 22 теста, включая UI-сценарии регистрации
  места с городом, stale/IDOR-негативы, переписанный сценарий выбора
  площадки через официальный вход.
Замечания ревизора: approve по 6 компонентам, блокирующих нет.
Неблокирующие: M22-эвристика может задеть личную копию, байт-в-байт
равную v3 (прецедент M18, история цела); ранг категории «город» в поиске
выше «similar» (покрыто тестом); вход в справочник с экрана вопроса
«место» личного шаблона отсутствует (вне требований этапа, отдельное
решение владельца).
Открытые вопросы: нет блокирующих.
Коммит: при приёмке. Push: выполнен в origin/main.

## Этап R5 — Тест-матрица A–G, очистка данных, развёртывание
Дата: 2026-10-29
Статус: done
Задание: по плану `poster2_fix_plan.md` (раздел R5): добить тест-матрицу
A–G автотестами, независимые проверки и ревизия, полный make check,
коммит/push/CI, остановка бота, WAL-бэкап реальной БД, чистая БД штатной
инициализацией, деплой systemd, проверка polling/автозапуска.
Изменено:
- `Poster/tests/test_poster2_r5_matrix.py` (новый, 4 теста, разработчик):
  самоназначение суперадмина (username + ID, идемпотентный повтор); конфликт
  кода с разными value_type (number vs short_text — одобрение разрешено,
  глобальный приоритет, локальный цел); сбой доставки уведомления о
  конфликте (failed, одобрение не откатывается, задача видна в кабинете);
  архивация локального носителя → superseded при первом решении, callback
  безопасны, снимки не переписаны.
- `Poster/tests/test_poster2_r5_matrix_tester.py` (новый, 9 тестов,
  независимый тестировщик): негативы самоназначения (обычный юзер, чужой
  админ — DENIED_ALERT, строк place_admins нет); честность плашки доставки
  (все сбой / смешанно / успех / место без админа / повторное одобрение);
  IDOR adm_bconflict_* от чужого админа; легитимный админ решает задачу
  после чужой попытки.
- `Poster/handlers/admin.py` — фикс `_notify_conflict_admins` (дефект,
  найденный при написании тестов): счётчик доставки по факту отправки
  (tuple delivered/undelivered), честные плашки «уведомлено: X, не
  доставлено: Y» / «не доставлены: Y; задача видна в карточке места».
- `Poster/docs/agent_team/state.md` — эта запись.
Матрица A–G (gap-анализ architect): A 9/9, B 11/11, C 14/14, D 8/9→9/9,
E 7/7, F 14/19→19/19 (частично покрыто ранее), G 6/7 + операционная
проверка чистой БД выполнена (не автотест).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1394 tests ... OK`; ruff/
  black/mypy чисто (137 файлов).
- Мутационная проверка (независимо): 8/8 мутаций ловятся (старый счётчик,
  молчаливая ветка плашки, отсутствие конфликтов, приоритет, stale,
  самоназначение, откат одобрения).
- CI: run 40895835071 (c2bdc34) — success (Python 3.13 и 3.14).
- Бэкап реальной БД (schema 17): /tmp/opencode/poster-backup/
  post_bot_backup_r5_20261029.db, 385024 байт,
  SHA-256 1468e180e997d0139022df52d29f3473e5be5e76dcdb92dec723e0a15beeb63e,
  integrity ok, fk 0; архив оригинала: post_bot_pre_cleanup_r5_20261029.db
  (та же SHA-256).
- Чистая БД: init_db → schema 20, integrity ok, fk 0, 10 системных
  кирпичиков (scope=system), пользователей/площадок/шаблонов/предложений/
  анонсов/конфликтов — 0. Стандартный шаблон создаётся при /start первого
  пользователя («Дефолтный шаблон», BUILTIN v3 — покрыто тестами).
  SUPERADMIN_USER_IDS=249601025, SUPERADMIN_CONTACT_URL=https://t.me/a_quilam
  — проверены в env (значения не выводились).
- Деплой: poster-bot.service и tg-connect-proxy.service — active + enabled;
  polling подтверждён (ESTAB через прокси 127.0.0.1:3128, getUpdates
  отвечает, конфликтов 409 нет); 25+ мин стабильной работы, NRestarts=0,
  0 ошибок в журнале. Первый старт после очистки упал из-за нестабильности
  внешней сети через CONNECT-прокси (telegram.error.TimedOut ×2), после
  стабилизации сети старт успешен, рестарт-петли нет.
Замечания ревизора: approve по всем 3 компонентам (фикс минимальный, права
и модель данных не затронуты, экранирование корректно, тесты без реальной
БД и секретов). Неблокирующие: модульный docstring matrix.py упоминет
«продукт не меняется» — устарел (класс-docstring корректен); fallback
архивации в тесте пишет ORM напрямую (задокументировано); в редкой гонке
всех mark=False плашка «поставлены в очередь» чуть неточна (pre-existing).
Открытые вопросы: нет блокирующих.
Коммит: c2bdc34 (opencode.jsonc и .opencode/agents/*.md владельца — вне
коммита). Push: выполнен в origin/main. CI: success.
Развёртывание: проверено (см. выше), Poster работает на чистой БД v20.

---

## Этап R4 — UX-чистка и чат как приложение
Дата: 2026-10-29
Статус: done
Задание: по плану `poster2_fix_plan.md` (раздел R4, дефекты №24, №27, №28,
№29 + чат-как-приложение): описание кирпичика в карточках предложения с
миграцией 20, крошки и «шаг N из 4» во флоу предложений, хаб кирпичиков со
строками-входами, описание в `<code>` с экранированием, свободный текст на
экранных фазах не затирает экран.
Изменено:
- `Poster/migrations.py` — миграция 20 «brick_proposal_description»:
  ALTER TABLE brick_proposals ADD COLUMN description TEXT + пересоздание
  trg_brick_proposals_snapshot_immutable (описание тоже иммутабельно);
  LATEST_VERSION=20.
- `Poster/models.py` — description у BrickProposal/BrickProposalSnapshot.
- `Poster/brick_proposal_store.py` — перенос описания в снимок и в кирпичик
  при одобрении.
- `Poster/handlers/bricks.py` — карточки предложения с 7 полями (title/code/
  type/description/author/target/status), legacy NULL → «—»; крошка
  «🧱 Кирпичики → 📨 Предложения» + «шаг N из 4»; хаб со строками
  «название + 📨 bp_start:<id>» (вход с пропуском шага 1, назад → хаб через
  proposal_entry); SCREEN_PHASES + BrickScreenTextHandler (свободный текст/
  фото на экранных фазах не затирают экран, ввод удаляется best-effort);
  _description_html.
- `Poster/handlers/admin.py` — _proposal_description_html (админ-карточка).
- `Poster/handlers/announcement_wizard.py`, `handlers/wizard_questions.py` —
  описание в `<code>` в вопросе анонса и предпросмотре.
- `Poster/docs/migrations.md` — раздел «Версия 20».
- Тесты: новые test_poster2_r4_ux.py (26, разработчик) и
  test_poster2_r4_ux_tester.py (27+8 хелперов, независимый); актуализированы
  ~17 регрессионных модулей (версия 20).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1381 tests ... OK`; ruff/
  black/mypy чисто (135 файлов).
- R4-набор: `tests.test_poster2_r4_ux tests.test_poster2_r4_ux_tester` →
  56 тестов OK.
- poster-tester (независимо): все 6 пунктов требований + негативы
  (IDOR, forged bp_start, stale) + юмор-регресс с мутационной проверкой.
  1 дефект в собственном тесте (фильтр живого блока: send-записи FakeBot не
  содержат message_id) — исправлен, продуктового дефекта нет.
Замечания ревизора: approve (6 независимых попыток; каждая доходила до
лимита шагов на чтении diff, но вердикт дали все, блокирующих нет).
Неблокирующие (в бэклог R5+): мёртвый код (_conflict_short_label,
count_open_conflicts, list_open_conflicts, _description_text в bricks.py),
дубли description-html-хелперов, устаревшее число тестов в
docs/migrations.md, pending-уведомления без ретрая (перенос с R3).
Открытые вопросы: нет блокирующих.
Коммит: 0bc09dc (opencode.jsonc и .opencode/agents/*.md владельца — вне
коммита). CI: completed success (run 40861108666).
Push: выполнен в origin/main.

### Контрольная точка для R5 (компактная)

Завершено: S1–S5 (Poster 2.0), R1 (578818e), R2 (2fc237f), R3 (3df2c3f),
R4 (этап, коммит в момент приёмки). CI зелёный по факту push каждого этапа.
Схема: LATEST_VERSION=20 (мigrations 18: BUILTIN v3/«Дефолтный шаблон»;
19: brick_conflicts + brick_conflict_notifications; 20:
brick_proposals.description). Тесты: 1381 OK, ruff/black/mypy чисто
(135 файлов).

Осталось R5 (по `poster2_fix_plan.md` §R5):
1. Автоматизировать оставшиеся части тест-матрицы A–G (п. плана).
2. Очистка тестовой БД: остановить poster-bot.service (`systemctl --user`);
   WAL-бэкап с `PRAGMA integrity_check`/`foreign_key_check`; записать
   путь/размер/SHA-256/версию схемы; собрать чистую БД штатным механизмом
   (миграции), сохранив: код, миграции, системные кирпичики, стандартный
   шаблон, конфиг, токен, systemd-юниты, прокси, ID владельца (249601025).
   Реальную `Poster/post_bot.db` не читать/не выводить.
3. Развёртывание: `systemctl --user` poster-bot.service +
   tg-connect-proxy.service; проверить active/enabled, авторизацию в
   Telegram, polling, логи, отсутствие рестарт-петли; сеть/SSH/проектор
   не трогать.
4. Финальный отчёт владельцу (8 разделов) + рекомендации keep/raise/lower
   по лимитам шагов подагентов.

Ограничения: бот запускается `systemctl --user` (не `make run`); токен/env в
`/home/mush/.config/opencode/poster-bot.env` (не выводить); тестовая БД
содержит только тестовые данные — заменить после проверенного бэкапа.

---
## Этап R3 — Глобальные кирпичики и конфликты кодов
Дата: 2026-10-29
Статус: done
Задание: по плану `poster2_fix_plan.md` (раздел R3): полная модель
конфликтов кодов — одобрение глобального кирпичика при локальном носителе
того же кода разрешено, глобальный выигрывает для новых привязок; задачи
разрешения конфликта по местам с идемпотентными уведомлениями; два исхода
(переименовать локальный / переключиться на глобальный); миграция 19.
Изменено:
- `Poster/migrations.py` — миграция 19: таблицы brick_conflicts и
  brick_conflict_notifications (FK RESTRICT/CASCADE, CHECK-формы,
  уникальные индексы), триггеры терминальности/иммутабельности/no-delete,
  пересоздание trg_bricks_reserve_codes_insert без требования «ровно один
  несистемный носитель» (замена R1-ограничения, прямой INSERT системного
  кода остаётся заблокирован без pending-предложения); LATEST_VERSION=19.
- `Poster/models.py` — BrickConflict, BrickConflictNotification.
- `Poster/brick_store.py` — домен конфликтов: create_code_conflicts,
  list/count/get, resolve_conflict_by_rename / _by_switch (CAS по
  templates.revision, старые версии/снимки нетронуты), _rebind_place_
  template_to_system, очередь уведомлений; фикс гонок: _begin_immediate_
  write проверяет DBAPI-уровень (raw.in_transaction), expire_all после
  захвата блокировки, IntegrityError → already_resolved только при
  фактическом закрытии задачи.
- `Poster/brick_proposal_store.py` — создание задач в транзакции одобрения.
- `Poster/handlers/admin.py` — фазы conflicts_list/conflict_card/
  conflict_rename_input/conflict_switch_confirm, кнопка «⚔️ Конфликты
  кодов (N)» и индикация в карточке места, идемпотентная рассылка
  _notify_conflict_admins, права перепроверяются по БД.
- `Poster/docs/migrations.md` — раздел «Версия 19».
- Тесты: новые test_poster2_r3_conflicts (25, разработчик) и
  test_poster2_r3_conflicts_tester (25, независимый); актуализированы ~15
  регрессионных модулей (версия 19); test_poster2_r1_brick_codes_tester
  переписан осознанно под модель R3 (одобрение + задача вместо error).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1324 tests ... OK`; ruff/
  black/mypy чисто (133 файла).
- Гонки (локальные прогоны разработчика): rename+switch 160 циклов,
  switch+switch 80×4 потока, карточка-открыта-заранее 60 — проигравший
  всегда already_resolved, 0 дефектов.
- poster-tester (независимо, 4 вызова из-за лимитов): конфликтный сценарий
  целиком, идемпотентность задач/уведомлений, гонки двух сеансов, рестарт
  engine/Harness, оба исхода, индикация, IDOR/forged/stale, личный носитель
  без задачи, миграция 19 на свежей/заполненной БД, регресс «без носителей
  → без задач». Найдены и исправлены: ложный superseded при гонке
  (write-lock не брался — DBAPI-проверка + expire_all), error вместо
  already_resolved при двойном switch (UNIQUE template_versions —
  сериализация номера версии), dead-end навигации конфликтов.
Замечания ревизора: approve (2 полных отчёта). Неблокирующие (перенос в
R4/бэклог): (1) мёртвый код — count_open_conflicts/list_open_conflicts/
_conflict_short_label/local_brick_revision; (2) docs/migrations.md —
устаревшее число тестов; (3) pending-уведомления без ретрая при сбое
доставки (задача остаётся видимой в карточке места — компенсация);
(4) get_place_name_for_display дублирует place_store; (5) восстановленный
вручную архивный носитель после одобрения не получает задачу (соответствует
модели «только active», держать в виду).
Открытые вопросы: нет блокирующих.
Коммит: см. git log (этап R3; opencode.jsonc и .opencode/agents/*.md
владельца — вне коммита).
Push: выполняется в origin/main.

---

## Этап R2 — Шаблоны (BUILTIN v3, миграция 18) и разделы «Управления»
Дата: 2026-10-29
Статус: done
Задание: по плану `poster2_fix_plan.md` (раздел R2, дефекты №24-тексты
шаблона / №25 / №26): BUILTIN v3 (эталон Части III), миграция 18 с
переименованием нетронутых копий в «Дефолтный шаблон», «♻️ Восстановить
стандарт», «Управление → Шаблоны» без запуска анонса, «Управление →
Площадки» — настоящий справочник с карточкой и правами. Выполнен двумя
частями R2a (шаблоны+миграция) и R2b (интерфейс управления).
Изменено:
- `Poster/template_types.py` — BUILTIN v3, BUILTIN_TEMPLATE_VERSION=3,
  INITIAL_PERSONAL_TEMPLATE_NAME=«Дефолтный шаблон».
- `Poster/migrations.py` — миграция 18: обновление ТОЛЬКО нетронутых копий
  (builtin_version=2 И содержимое побайтово v2 И имя «Мой шаблон»; плюс
  осознанное расширение: builtin_version IS NULL с теми же признаками —
  так выдавала прежняя версия), новая версия + переименование в одной
  транзакции, brick_id по коду, эталоны захардкожены локально,
  идемпотентность; LATEST_VERSION=18.
- `Poster/handlers/templates.py` — open_templates никогда не возобновляет
  pick и не запускает анонс (№25); фазы delete_confirm/deleting, кнопка
  «🗑 Удалить шаблон» только у личных, отказ при снимках/анонсах (FK
  RESTRICT → понятное сообщение, данные целы); «♻️ Восстановить стандарт»
  (R2a) в карточке.
- `Poster/handlers/places.py` — справочник «Управление → Площадки» (№26):
  фазы manage_* с префиксом plm_* (список/поиск/карточка/заявка админа),
  права (can_manage_place → редактирование, is_superadmin →
  администраторы, иначе просмотр + «🛡 Я админ места»), plm_add создаёт
  место с manage_mode без автозапуска анонса; фикс dead-end:
  handle_manage_back принимает manage_admin_request (→ manage_card) и
  create_input при manage_mode (→ manage_list); сброс manage_mode при
  выходе из справочника (_drop_manage_mode).
- `Poster/handlers/admin.py` — точки входа open_place_admin_editor /
  open_place_admin_card с повторной проверкой прав по БД; убран двойной
  query.answer() при переходах.
- `Poster/handlers/main_menu.py` — manage_templates/manage_places переведены
  на новые входы.
- `Poster/docs/migrations.md` — раздел «Версия 18».
- Тесты: новые test_poster2_r2_templates (20), test_poster2_r2_management
  (20), независимые test_poster2_r2_management_tester (21),
  test_poster2_r2_render_tester (9), test_poster2_r2_migration_tester (15),
  test_poster2_r2_owner_tester (16); актуализирован
  test_poster2_menu (регресс manage-экранов) и легаси-тесты схем.
- `Poster/docs/agent_team/agent_efficiency.md` — журнал эксперимента
  эффективности подагентов (задание владельца от 2026-10-28).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1264 tests ... OK`;
  ruff/black/mypy чисто (127 файлов).
- poster-tester (независимо, 5 вызовов из-за лимитов/дофина): рендер v3
  побайтово по эталону владельца в реально доставленном сообщении, `//`
  при пропусках, пустой → «.»; миграция 17→18 на заполненной БД со всеми
  негативами (нетронутая/отредактированная/переименованная копии, повторный
  прогон no-op, integrity/FK); новый пользователь → «Дефолтный шаблон» v3;
  №25 (ни один путь из раздела не запускает анонс; удаление с историей —
  отказ с сохранением данных); №26 (карточка, права трёх ролей, IDOR,
  stale, создание места → стандарт v3 без анонса, регресс pl_pick →
  анонс). Найдены и исправлены 2 продуктовых dead-end plm_back.
Замечания ревизора: approve. Неблокирующие: (1) эвристика миграции 18
захватит вручную пересозданную копию с именем «Мой шаблон» и текстом v2 —
компромисс задокументирован в комментарии и migrations.md; (2) при сбое БД
в open_place_admin_editor алерт «место не существует» вместо «не удалось
проверить доступ» — отказ по умолчанию безопасен; (3) created_by_user_id=NULL
у новой builtin-версии — косметика. Действий не требуется.
Открытые вопросы: нет блокирующих. Перенесено ранее: удаление шаблона
стирает историю версий (сообщение об этом отсутствует) — известный
компромисс, не блокер.
Коммит: см. git log (этап R2; opencode.jsonc и .opencode/agents/*.md
владельца — вне коммита).
Push: выполняется в origin/main.

---

## Этап R1 — Стабильность и критичный UX
Дата: 2026-10-28
Статус: done
Задание: по плану `poster2_fix_plan.md` (раздел R1): дефекты №22 («Назад»
из альтернативных названий), №23 (легаси reply-клавиатура), №21 (молчание
при назначении админа места), №30-стабилизация (глобальный код кирпичика
блокировался локальным носителем). Без изменения схемы БД.
Изменено:
- `Poster/handlers/admin.py` — №22: `edit_aliases` в allow-list
  `handle_cancel_flow` + ветка возврата к карточке места (зеркально
  `edit_menu`, сброс `alias_page`); №21: `_normalize_assign_query`
  (username/id/name), явные тексты «не найден»/«неверный формат»,
  `query_kind` в состоянии с очисткой во всех ветках; №23: вызов
  drop_legacy_reply_keyboard в open_admin.
- `Poster/utils/screen.py` — №23: `drop_legacy_reply_keyboard`
  (ReplyKeyboardRemove, служебное сообщение сразу удаляется, флаг
  в user_data, ошибки Telegram не ломают вход).
- `Poster/handlers/main_menu.py`, `Poster/handlers/announcement_wizard.py` —
  вызовы хелпера на входах (/start, главное меню, оба старта конструктора).
- `Poster/brick_store.py` — №30: scope="system" больше не блокируется
  place/personal-носителями кода; внутрисистемный дубль и резервы сохранены;
  комментарий о границе R1/R3 (2+ носителей → триггер → явный error).
- `Poster/tests/integration_harness.py` — replies() игнорирует удалённые
  служебные сообщения (учёт message_id фото/медиагрупп).
- Тесты: новые `test_poster2_r1_stability.py` (28),
  `test_poster2_r1_brick_codes.py`, независимые
  `test_poster2_r1_stability_tester.py` (25) и
  `test_poster2_r1_brick_codes_tester.py` (15); актуализирован
  `test_stage22a_brick_service.py` (новое поведение №30 с негативами).
- `Poster/docs/agent_team/poster2_fix_plan.md` — план R1–R5 (новый).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1163 tests ... OK`; ruff/
  black/mypy чисто (125 файлов).
- poster-tester (независимо): 40 тестов OK; мутационная проверка — все
  4 мутации пойманы (тесты падают на до-фиксном поведении); двухносительный
  кейс даёт явный status="error" (предложение остаётся pending).
Замечания ревизора: approve. Неблокирующие: (1) `replies()` в harness
скрывает любые удалённые ответы — семантику держать в поле зрения;
(2) флаг снятия клавиатуры в user_data теряется при рестарте процесса
(одно повторное снятие — допустимо); (3) «+7…» трактуется как поиск по
имени (согласовано со store); (4) UI предложения при 2+ носителях говорит
«свободно», затем явная ошибка одобрения — известное ограничение R1,
закрывается в R3. opencode.jsonc — вне коммита этапа.
Открытые вопросы: нет блокирующих.
Коммит: см. git log (этап R1).
Push: выполнен в origin/main.

---

## Этап S5 — Удаление легаси, описания кирпичиков, тексты
Дата: 2026-10-28
Статус: done
Задание: по плану (раздел S5): убрать старый девятишаговый конструктор,
его WIP и мёртвый код без бессрочного слоя совместимости; закончить UX
кирпичиков; читаемые справочные тексты; редкие безопасные шутки интерфейса.
Изменено:
- УДАЛЕНО: `Poster/handlers/post_creation.py`, `Poster/handlers/drafts.py`,
  `Poster/handlers/callbacks.py` (маршрут main_menu перенесён в
  main_menu.py); модель `Draft` и легаси-хелперы из models.py.
- `Poster/migrations.py` — миграция 17: drafts → legacy_drafts_backup
  (переименование-архив вместо DROP, requires_tables, идемпотентность);
  `database.py` — init_db = run_migrations + create_all; docs/migrations.md.
- `Poster/bot.py` — сняты легаси-обработчики; main_menu.py — help переписан
  под Poster 2.0, /create_post и restore_wip устарели (ведут в меню,
  обработчик stale-кнопки в announcement_wizard оставлен намеренно);
  view_drafts → open_my_announcements.
- `_show_media_preview` переведён с легаси send_rendered_post на
  delivery.send_announcement_post (закрыто замечание ревизора S4).
- Описание кирпичика: brick_store (description в CRUD), handlers/bricks.py
  (фаза description_input, «⏭ Без описания», bk_edit_description,
  объяснения трёх полей), AnnouncementFieldState.description →
  wizard_questions.question_text с h()-экранированием.
- Юмор: utils/ui_humor.py (HUMOR_JOKES, pick_variant на hashlib ≈1%,
  humor_allowed — белый список question/button, запрет
  error/rights/limits/data_loss), config.py UI_HUMOR_ENABLED (default on),
  .env.example; подключение в _render_question — только текст, после
  сборки клавиатуры, ключ user_id:draft_id:revision:brick_id стабилен.
- Чистка мёртвого кода (долг S4 + ревизия S5): удалены
  _flush_photo_batches, flush_pending/pending_keys, мёртвый параметр
  humor_line.
- README.md / docs/stage18_architecture.md — легаси помечен историческим,
  структура кода актуализирована.
- Тесты: удалены ~20 легаси-модулей (stage6, stage9_*, stage10_*, stage11_*,
  stage12_reliability, stage14_drafts/input_limits, stage16_*, 17_coverage —
  живое покрытие перенесено); переписаны stage13/17/19a/19b/21/22/25/26/27/
  28 и LegacyDraftCompatibilityTests; новые test_poster2_legacy_removal (12),
  test_poster2_humor (17), test_poster2_s5_tester (12, независимые).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1095 tests ... OK
  (skipped=1)`; ruff/black/mypy чисто (121 файл). Число тестов снизилось
  с 1405: удалены только легаси-проверки удалённого поведения (перечень в
  отчёте разработчика и комментариях тестов), живое покрытие сохранено.
- poster-tester: 29+156+12 тестов OK; сквозные сценарии (полный путь,
  описание с экранированием, устаревшие входы, флаг юмора off, миграция 17
  на старой схеме, шутка не в live/publication_text); дефектов нет.
Замечания ревизора: approve. Неблокирующие закрыты (мёртвый код, параметр
humor_line); остались мелкие дубли _description_text/EMPTY_ANNOUNCEMENT_JOKES
- не блокер.
Открытые вопросы: нет.
Коммит: см. git log (этап S5).
Push: выполнен в origin/main.

---

## Этап S4 — Фото, ограничения и финализация уже живого анонса
Дата: 2026-10-28
Статус: done
Задание: по плану (раздел S4): фото/альбом или «Без фотографий» → анонс
готов автоматически; медиаблок сверху; без повторных копий поста; при
превышении длины данные и ввод сохраняются.
Изменено:
- `Poster/delivery.py` (новый) — единый доставщик: delivery_fingerprint,
  media_batches (2–10 на sendMediaGroup, хвост 1 → sendPhoto), подпись
  только у последнего сегмента, RetryAfter-повтор, прогресс пишется только
  после подтверждённого ответа Telegram (падение ≠ успех), досыл недостающего;
  EMPTY_CAPTION_TEXT (\u200b) — внутренний маркер пустой подписи, наружу не
  выводится (visible_text).
- `Poster/handlers/wizard_photos.py` (новый) — фото-шаг: приём фото по
  одному и альбомов (буфер), лимит 10 (решение владельца), отклонение 11-го
  с сохранением принятых, автофинал после полной партии, CAS, удаление ввода
  только после подтверждённой доставки.
- `Poster/handlers/wizard_live.py` — медиаблок live: editMessageCaption,
  пересборка при смене состава, удаление старого только после успеха.
- `Poster/template_compiler.py` — visible_text_length (UTF-16 после
  entities), announcement_text_limit (4096 без фото / 1024 с фото),
  check_announcement_length.
- `Poster/announcement_store.py` — MAX_ANNOUNCEMENT_PHOTOS 100→10; заморозка
  payload: пустой текст с фото → маркер пустой подписи, без фото → «.»;
  проверка длины перед заморозкой.
- `Poster/handlers/announcement_wizard.py` — подключение wizard_photos
  (буфер альбома, автофинал), финализация по fingerprint (_promote_live_post:
  доставленный медиаблок становится sent без повторной отправки; сверка
  photo_count), проблемный экран _render_length_problem (длина/лимит,
  топ кирпичиков, «Изменить другой кирпичик», «Без фотографий» → 4096 +
  авто-применение фото после сокращения), aw_photo_drop, _run_announcement_publication
  → delivery.send_announcement_post.
- `Poster/admin_transfer_store.py` — внеплановый, но обоснованный фикс
  флейка CI-класса: проигравший CAS-гонку подтверждения передачи получал
  устаревший «superseded» из identity map; теперь строка перечитывается и
  возвращается честный «already_confirmed» (_result_after_lost_race). Прав
  и данных не касается.
- Тесты: `tests/test_poster2_limits.py` (новый, 15), `tests/test_poster2_delivery.py`
  (новый, 22), `tests/test_poster2_delivery_tester.py` (новый, 6 независимых),
  актуализация легаси (stage16/27c/21, integration_harness) — покрытие
  доставки сохранено.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1405 tests ... OK
  (skipped=1)`; ruff/black/mypy чисто (140 файлов).
- poster-tester: 85 тестов OK; 30/30 прогонов transfer-набора (флейк
  устранён); 19/19 независимых проб S4 (альбом, лимит 10/11, RetryAfter-
  досыл, fingerprint-финал, «.»→пусто, порядок сообщений); регресс-проверка
  флейк-фикса на старом коде подтверждает суть дефекта.
Замечания ревизора: approve. Неблокирующие (перенесены в S5): мёртвый код
wizard_photos.flush_pending / _flush_photo_batches; легаси-предпросмотр
send_rendered_post в _show_attachments_preview (дублирование доставки
delivery vs post_creation); opencode.jsonc — вне коммитов этапа.
Открытые вопросы: нет.
Коммит: см. git log (этап S4).
Push: выполнен в origin/main.

---

## Этап S3 — Один экран конструктора: живой анонс, календарь, время
Дата: 2026-10-27
Статус: done
Задание: по плану (раздел S3): живой анонс с первого вопроса, единый
служебный экран, календарь сразу, пресеты времени, шаг фото вместо
автодоставки, готовность пустого анонса, буквальная статика, placeholder «.»,
хук шутки.
Изменено:
- `Poster/template_compiler.py` — render_announcement (единый рендер
  live/final, буквальная статика, PLACEHOLDER_TEXT «.»).
- `Poster/handlers/wizard_live.py` (новый) — sync живого блока: отправка до
  служебного экрана, editMessageText после ответа, live_message_ids_json,
  восстановление правкой без дублей.
- `Poster/handlers/wizard_questions.py` (новый) — вынесенные вопрос/календарь/
  время; календарь сразу на вопросе даты; TIME_PRESETS 18:00/18:30/19:00.
- `Poster/handlers/announcement_wizard.py` — _sync_live,
  _keep_live_above_screen (свой устаревший экран удаляется, новый ниже
  анонса), шаг фото (_render_photo_step, aw_photo_none), aw_timeset (CAS),
  готовность пустого анонса, шутка в нижнем экране; декомпозиция 5514-строк
  обработчика.
- `Poster/announcement_store.py` — build_announcement_preview →
  render_announcement (publication_text «.» для пустого);
  set_announcement_live_message_ids (без роста revision — оправдано CAS
  кнопок, обоснование ревизора).
- `Poster/utils/ui_humor.py` (новый) — фиксированный набор шуток + хук
  (заготовка S5 по п.11 плана S3).
- `Poster/tests/test_poster2_live.py` (новый, 24 теста),
  `Poster/tests/test_poster2_live_tester.py` (новый, 16 независимых),
  актуализация 16 легаси-проверок (шаг фото; покрытие доставки сохранено).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1360 tests ... OK
  (skipped=1)`; ruff/black/mypy чисто.
- poster-tester, 2 круга: найден и закрыт критичный дефект порядка
  (анонс оказывался НИЖЕ служебного экрана при первом входе) —
  _keep_live_above_screen; независимо подтверждены: порядок по message_id,
  рестарт без дубля, потеря live восстанавливается, IDOR, «.» не в values,
  шутка не в публикации.
Замечания ревизора: approve. Неблокирующее (мёртвый алиас _format_value)
закрыто удалением. Ограничение задокументировано: при отказе Telegram на
удаление экрана порядок не восстановим (fallback, данные не теряются).
Открытые вопросы: нет.
Коммит: см. git log (этап S3).
Push: выполнен в origin/main.

---

## Этап S2 — Меню, два входа, «Мои анонсы», «Управление»
Дата: 2026-10-27
Статус: done
Задание: по плану (раздел S2): меню ровно из 5 кнопок; оба входа сразу
запускают один конструктор; «Мои анонсы» с историей; «Управление» по
правам; диагностика невидимой админки §7.5 по фактам.
Изменено:
- `Poster/handlers/main_menu.py` — 5 кнопок (pick_place, pick_template,
  my_announcements, manage, help); подменю «Управление»; фикс §7.5:
  logger.error → logger.exception в _show_admin_button.
- `Poster/handlers/places.py` — режим pick: список площадок первым экраном,
  пагинация, поиск отдельным действием, «➕ Новая площадка» → создание →
  автозапуск анонса.
- `Poster/handlers/templates.py` — PHASE_PICK: список личных шаблонов,
  тап → немедленный старт, CRUD в том же разделе.
- `Poster/handlers/announcement_wizard.py` — entry-точки pl_pick/tp_pick/
  my_announcements; start_place_announcement_pick (0 подтверждений);
  _place_lines: пустая «Место: » убрана с 5 экранов личных анонсов.
- `Poster/handlers/user_tracking.py` — выдача стартового личного шаблона
  на /start (в т.ч. бэкфилл существующих пользователей).
- `Poster/template_store.py` — has_personal_template_history; маркер выдачи
  INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY (идемпотентность, анти-воскрешение).
- `Poster/tests/test_poster2_menu.py` (новый, 37 тестов),
  `Poster/tests/test_poster2_menu_tester.py` (независимые, 20 тестов),
  актуализация 12 legacy-тестов (только подписи/входы, без ослабления).
- `Poster/docs/agent_team/admin_access_report.md` (новый) — §7.5: гипотеза 3
  подтверждена (немая диагностика), остальные честно не подтверждены
  статически; права не расширялись, /admin цел.
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1320 tests ... OK
  (skipped=1)`; ruff/black/mypy чисто (130 файлов).
- poster-tester: 2 круга; найдены и закрыты 2 дефекта: (1) пустая строка
  «Место: » у личного анонса; (2) бэкфилл стартового шаблона существующим
  пользователям. Граничные случаи бэкфилла, IDOR, экранирование, /admin —
  подтверждены независимо.
Замечания ревизора: approve. Неблокирующие: (1) remember_conversation_state
использует приватный API PTB (_get_key/_update_state) — пересмотреть в S3+;
(2) дублирование литерала "personal" в template_store — снять в S3+;
(3) физический DELETE шаблона упирается в FK RESTRICT при production-PRAGMA
— продуктового сервиса удаления нет, вопрос S3+ (архивация vs ON DELETE).
Открытые вопросы: судьба физического удаления личного шаблона (S3+).
Коммит: см. git log (этап S2).
Push: выполнен в origin/main.

---

## Этап S1 — Домен: единый вход и площадка, не обязательная для личного шаблона
Дата: 2026-10-27
Статус: done
Задание: реализовать домен Poster 2.0 по плану (раздел S1): миграции 15/16,
origin personal/place, place_id: int | None, кирпичик [адрес], BUILTIN v2,
сервис выдачи первого личного шаблона; доменные тесты и описание миграций.
Изменено:
- `Poster/migrations.py` — миграция 15 (bricks.description; системный код
  `адрес` без хардкода ID; при коллизии с несистемным `адрес` триггер
  `trg_bricks_reserve_codes_insert` снимается и дословно восстанавливается
  в одной транзакции) и миграция 16 (перестройка `announcement_drafts`:
  place_id NULL, origin, live_message_ids_json + CHECK, индексы, триггеры
  12–14 + immutable origin, перенос прежних строк с origin='place').
- `Poster/models.py`, `Poster/announcement_store.py`,
  `Poster/announcement_wizard.py` — place_id: int | None, origin,
  personal-анонс без чтения Place, автоподстановка только по снимку
  шаблона, системные кирпичики по коду вместо хардкодов.
- `Poster/brick_types.py` — SYSTEM_BRICKS: `адрес` (10), BUILTIN v2.
- `Poster/template_types.py`, `Poster/template_store.py` — эталон §6.1.1,
  ensure_initial_personal_template (идемпотентный, без воскрешения
  удалённого; подключение к /start — S2).
- `Poster/brick_store.py` — **исключение из границ, обосновано ревизией**:
  check_code_availability допускает существующей несистемной строке её
  собственный код (легаси-кирпичик `адрес` остаётся редактируемым);
  новые кирпичики с системным кодом по-прежнему запрещены.
- `Poster/docs/migrations.md` — разделы «Версия 15 (Poster 2.0 S1)» и
  «Версия 16 (Poster 2.0 S1)».
- `Poster/tests/test_poster2_domain.py` (новый, 21 тест) — миграции 15/16
  на заполненной временной БД, целостность/идемпотентность/откат, коллизия
  кода `адрес`, personal-анонс, разовое имя вне справочника,
  ensure_initial_personal_template, [адрес]+экранирование, IDOR, CAS.
- Актуализированы тесты stage19a/b, 22a/b, 23, 25, 26, 27a/b, 28
  (10 кирпичиков, 16 миграций, BUILTIN v2); test_stage28 — сверка 14–16.
- `Poster/docs/agent_team/hp_migration.md` — отчёт о переносе на hp
  (документ передачи, добавлен в репозиторий).
Проверки:
- `make check PYTHON=.venv/bin/python` → `Ran 1263 tests ... OK
  (skipped=1)`; Ruff/Black чистые; mypy `Success: no issues found in 128
  source files`. Skip — проверка реальной БД, которая на hp отсутствует.
- Независимая проверка `poster-tester`: собственные сценарии на временных
  SQLite — миграции 14→16, атомарность отката 15 и 16, IDOR, CAS-гонка
  двух сессий, границы исключения check_code_availability, легаси-снимки.
Замечания ревизора: approve. (1) brick_store.py вне списка границ плана —
отклонение обосновано дефектом коллизии кода, задокументировано, принять
как утверждённое исключение. (2) Неблокирующее: мёртвая константа
ADDRESS_BRICK_ID в announcement_wizard.py — убрать или задействовать в S3.
(3) opencode.jsonc изменён посторонне — в коммит S1 не включён.
(4) BUILTIN_TEMPLATE_BINDINGS хранит brick_id=10 как валидационную
константу — фактический ID резолвится по коду; учитывать в S2/S3.
Как закрыты: (1) зафиксировано в этой записи; (2) перенесено в S3;
(3) исключено из коммита; (4) перенесено в S2/S3.
Открытые вопросы: нет блокирующих; см. неблокирующие (2) и (4).
Коммит: 612ab66 «S1: домен Poster 2.0 — миграции 15/16, личный шаблон
без площадки, кирпичик [адрес]» (opencode.jsonc в коммит не включён).
Push: НЕ выполнен — на hp нет GitHub-доступа (credential helper, SSH-ключи
и gh не переносились; push по HTTPS запросил логин и завершился ошибкой).
Локальный коммит сохранён; push требуется после настройки доступа
владельцем (ssh-ключ/токен/gh auth login).

---

## Контрольная точка переноса (hp) — S1 в работе
Дата: 2026-10-27
Статус: in_progress (запись не является завершённым этапом)
Причина: владелец запросил паузу для переноса работы на машину hp.
Активных операций нет: подагенты не запущены, фоновых тестов и git-операций
нет. Коммит и push для переноса НЕ выполнялись по прямому указанию.

Состояние S1 на момент паузы:
- HEAD: faa299f (bdf92ac — утверждение архитектуры, родитель). Рабочее
  дерево НЕ закоммичено: 28 файлов, +1065/−147 (реализация S1 + актуализация
  тестов). `opencode.jsonc` изменён посторонне и в коммит S1 не входит.
- Реализация в дереве: миграции 15 (description + кирпичик [адрес]) и 16
  (announcement_drafts: place_id NULL, origin, live_message_ids), origin
  personal/place, place_id: int | None, BUILTIN v2 по §6.1.1,
  ensure_initial_personal_template, [адрес] в SYSTEM_BRICKS (id=10).
- Сессии разработчика S1: ses_e8368183dffevsNTWw9N5xJWq7 (завершена по
  лимиту шагов, контекст сохранён, можно продолжать по sessionID).

Проверки на момент паузы:
- mypy → `Success: no issues found in 127 source files` (0 ошибок).
- unittest discover (Poster) → `Ran 1242 tests ... OK`.
- `make check PYTHON=.venv/bin/python` → НЕ запускался (финальный прогон
  после создания test_poster2_domain.py).

Незавершённые работы S1 (передать poster-developer, можно продолжить
сессию ses_e8368183dffevsNTWw9N5xJWq7):
1. Создать `Poster/tests/test_poster2_domain.py`: миграции 15/16 на
   заполненной временной БД, foreign_key_check/integrity_check, origin=place
   у прежних строк, сохранность publication_progress/live_message_ids после
   рестарта, origin=personal без площадки, разовое место не пишется в
   places/place_names, идемпотентность ensure_initial_personal_template без
   воскрешения удалённого, [адрес] + экранирование description, IDOR чужого
   личного шаблона, CAS-конфликт.
2. Дописать `Poster/docs/migrations.md`: разделы «Версия 15 (Poster 2.0 S1)»
   и «Версия 16».
3. Финальный `make check PYTHON=.venv/bin/python` до 0 failures/errors.
4. Далее по процедуре: poster-tester (независимая проверка на временных
   SQLite) → poster-reviewer (миграции, права, снимки, регрессии) →
   приёмка, state.md, commit, push.

Сделано в S1 к этой точке:
- Реализованы миграции 15/16; origin и place_id: int | None; поддержка
  анонсов по личному шаблону без площадки; кирпичик [адрес]; BUILTIN v2;
  сервис выдачи первого личного шаблона; автоподстановка известных сведений
  в пределах снимка шаблона.
- Исправлены обе ошибки mypy в confirm_announcement_place.
- Актуализированы ~26 тестовых файлов (stage19a/b, 22a/b, 23, 25, 26, 27a/b,
  28) под 10 кирпичиков, 16 миграций, BUILTIN v2; test_stage28 переименован
  в ..._one_to_sixteen с сохранением CHECKSUMS_1_13 и сверкой 14–16.
- Не ослаблены security-проверки (IDOR, superadmin, CAS, immutable-триггеры).

Запрещено после переноса: не начинать S2–S5 до приёмки S1; не мигрировать
реальную БД; не перезапускать работающего бота; opencode.jsonc не трогать.

---

## Настройка команды — разделение ролей и модели GLM
Дата: 2026-10-27
Статус: done
Задание: Привести мультиагентную команду к системе делегирования: менеджер
не пишет код, роли разделены реальными правами OpenCode V2, модели
перераспределены после проверки GLM. Poster 2.0 не разрабатывался.
Изменено:
- `.opencode/agents/poster-manager.md` — убран `steps`; `edit` разрешён
  только в `Poster/docs/agent_team/*`, исходники/тесты/миграции запрещены
  и через shell (редиректы, `tee`, `sed -i`, `cp`/`mv`/`rm`, heredoc,
  инлайн-скрипты, `make format`, `make migrate`); модель
  `routerai/z-ai/glm-5.3-flash`; инструкции переписаны под процедуру
  «этап → разработчик → тесты → ревизия → приёмка».
- `.opencode/agents/poster-developer.md` — роль исполнителя задания, `steps:
  80` сохранён, модель DeepSeek V4.1 Flash; явно: этап не выбирает, не
  делегирует, чинит дефекты по воспроизведению.
- `.opencode/agents/poster-tester.md` — независимая проверка, правит только
  `Poster/tests/*`, продукт через shell закрыт, дефект не исправляет.
- `.opencode/agents/poster-architect.md`, `.opencode/agents/poster-reviewer.md`
  — модель GLM, требование короткого фактического отчёта, закрыт heredoc
  и конвейер в интерпретатор.
- `Poster/docs/agent_team/README.md` — состав, модели, матрица прав,
  процедура менеджера.
- `Poster/docs/agent_team/state.md` (эта запись).
Проверки:
- `opencode debug agents` → модели и права совпадают, у менеджера `steps`
  отсутствует.
- RouterAI `z-ai/glm-5.3-flash` → ответ и вызов инструмента (`shell`)
  подтверждены в OpenCode V2.
- Smoke прав: менеджер не может `write`/`edit` исходник (`Permission denied:
  edit`) и не может писать код через shell, но делегирует создание файла
  разработчику; тестировщик пишет/удаляет `Poster/tests/*`; ревизор и
  архитектор не пишут никуда; чтение и запуск тестов работают.
- `make check PYTHON=.venv/bin/python` → **падает**: mypy, 2 ошибки в
  `Poster/announcement_store.py` (строки 1176, 1181) — незавершённая
  работа S1, не относится к этому этапу и не менялась.
Открытые вопросы: ошибки mypy S1 — задача для `poster-developer` в рамках
S1; этот этап их не исправлял.
Коммит: см. `git log` (тематический коммит настроек команды).
Push: выполнен в `origin/main`.

---

## Этап P0 — архитектурная спецификация и план Poster 2.0
Дата: 2026-10-26
Статус: done (ожидает утверждения владельцем; реализация не начата)
Задание: Зафиксировать архитектуру Poster 2.0 и разбить реализацию на
этапы без изменения кода, БД, миграций и работающего бота. Исследование
выполняли `poster-architect` и `poster-reviewer` параллельно (только
чтение), менеджер свёл выводы и оформил документы.
Изменено:
- `Poster/docs/agent_team/poster2_spec.md` (новый) — спецификация:
  инвентаризация двух конструкторов, компоненты, состояния, два сообщения,
  лимиты/фото/ошибки/рестарты, матрица «требование → реализация»,
  предлагаемые миграции, план удаления легаси, 13 пунктов решений владельца.
- `Poster/docs/agent_team/poster2_plan.md` (новый) — 5 этапов S1–S5 с
  границами, зависимостями, критериями приёмки и точными командами тестов.
- `Poster/docs/agent_team/state.md` (эта запись).
Проверки:
- `make check` → не запускался (код не менялся, по заданию).
- Миграции на реальной БД → не запускались.
- Работающий бот → не трогался; `make run`/`bot.py`/`botctl.sh` не вызывались.
- Bot API (sendMediaGroup/editMessageMedia/editMessageCaption/deleteMessage)
  → сверено по официальной документации.
Замечания архитектора: два полноценных конструктора (легаси `Draft` +
`POST_STEPS` и `announcement_*`), «живого» анонса во время заполнения нет,
стандартный шаблон создаётся только для площадки, личный шаблон новому
пользователю не создаётся, пропуск кирпичика оставляет висячие эмодзи и
пустые строки, ID 5/6/7 захардкожены.
Замечания ревизора: блокирующие расхождения по меню, двум входам,
отсутствию входа «по личному шаблону», кнопке «Готово — получить пост»,
обработке лимита 1024/4096 (сейчас текст режется на части вместо остановки);
поле описания кирпичика отсутствует; 5 проверяемых гипотез, почему
владелец не видит админку. Часть ревизии не завершена (лимит шагов):
не дочитаны `handlers/places.py`, `handlers/drafts.py`, `handlers/templates.py`,
не проверены все ветки edit/alias/transfer в `handlers/admin.py`.
Как закрыты: расхождения сведены в §7 и §10 спецификации; пропущенный
ревизором контракт `place_id` (`AnnouncementDraft.place_id NOT NULL`)
найден менеджером и вынесен в §8.2/§8.4 как основной риск S1.
Открытые вопросы (решение владельца, §10 спецификации):
1. Код кирпичика адреса: `адрес_места` или новый `адрес`.
2. ~~Анонс, где пропущены все кирпичики~~ — **решено 2026-10-27**: завершать
   всегда, пустой результат → `.`, шутка только в служебном сообщении.
3. Ссылка площадки в стандартном шаблоне: `[место]([ссылка_места])` или
   отдельная строка `🔗 [ссылка_места]`.
4. Максимум фотографий и поведение при >10.
5. Как считать длину 1024/4096 (видимый текст vs сырой HTML).
6. Редактирование готового анонса: сразу или по кнопке.
7. Судьба фото при правке готового анонса.
8. Нужен ли диагностический экран «почему нет доступа».
9. Права админа места на чужие шаблоны/кирпичики.
10. Создание площадки из конструктора.
11. **Миграция 15 (описание кирпичика) — НЕ СОГЛАСОВАНА.**
12. **Миграция 16 (перестройка `announcement_drafts`) — НЕ СОГЛАСОВАНА.**
13. Судьба таблиц легаси `drafts`/`user_states` (оставить или `DROP`).
14. Разделитель `//`: обновлять ли `BUILTIN` в `template_types.py` под
    требование 4 (`[дата] // [время_начала]`).
15. Режим юмора при пустом анонсе, если юмор отключён, и кто пишет тексты
    шуток (рекомендация: флаг главнее; тексты — при реализации S5).
Коммит: нет
Push: нет

Следующий шаг: владелец утверждает спецификацию и отвечает по п.11–13.
Только после этого начинается S1. Этапы S2–S5 не начаты.

## Этап P0.1 — уточнения владельца от 2026-10-27
Дата: 2026-10-27
Статус: done (корректировка документов; реализация по-прежнему не начата)
Задание: Внести в спецификацию и план пять уточнений владельца: завершение
при всех пропущенных кирпичиках, буквальное сохранение статического текста
шаблона, заглушка `.` для пустого анонса без фото, шутка только в нижнем
служебном сообщении, отключаемый модуль редких юмористических вариаций в S5.
Изменено:
- `Poster/docs/agent_team/poster2_spec.md`:
  - §1.6 — блокировка готовности снята (решение п.1);
  - §3 — завершение разрешено всегда;
  - §5.6 (новый) — пустой анонс и заглушка `.`, шутка в служебном экране;
  - §5.7 (новый) — модуль редких вариаций: 1%, без LLM, стабильный ключ,
    белый список, отключение, тесты обычных и шуточных вариантов;
  - §5.7 — добавлено исключение для пустого анонса (шутка обязательна,
    но подчиняется флагу) и уточнено, что именно заменяется;
  - §6.1 п.3 — **отменено** прежнее предложение «чистить сирот»; статика
    шаблона сохраняется буквально; п.6 — про `//`;
  - §7 — строка «все кирпичики пропускаемые» переведена в «решено»;
  - §7.6 (новый) — таблица пяти уточнений и их привязка к этапам;
  - §2.1 — `template_compiler.py` получил `render_announcement()`;
  - §10 п.2 — помечен как решённый;
  - §10 п.14–15 (новые) — обновление `BUILTIN` под `//` и режим юмора при
    пустом анонсе;
  - §11 — риски по буквальной статике, «протёкшей» шутке и заглушке;
  - §12 — критерии готовности обновлены (15 пунктов §10, уточнения учтены).
- `Poster/docs/agent_team/poster2_plan.md`:
  - S3: новые работы 8–11 (готовность, буквальный рендер, заглушка `.`,
    шутка в служебном экране) и 5 новых критериев приёмки;
  - S3: границы расширены на `template_compiler.py`
    (`render_announcement`), зависимость от S4 переформулирована;
  - S4: заглушка `.` как последний шаг сборки в `delivery.py`, критерии
    про `publication_text`;
  - S5: модуль `Poster/utils/ui_humor.py`, работы 7, критерии и тесты,
    новый `test_poster2_humor.py` (обычные + шуточные варианты);
  - сводная таблица и раздел «Что НЕ входит»;
  - новый раздел «Уточнения владельца от 2026-10-27 и их место в плане»;
  - раздел «Проверки корректировки P0.1».
Проверки:
- `make check` → не запускался (код не менялся).
- Миграции, реальная БД, работающий бот → не трогались.
Найденное расхождение: прежняя §6.1 п.3 требовала удалять «сирот» и
схлопывать пустые строки, что прямо противоречит уточнению п.2 (буквальная
статика). Противоречие устранено: постобработка секций отменена, менять
`_section_html` запрещено. Второе расхождение — §5.7 против §10 п.15
(обязательность шутки vs отключаемость юмора): принят вариант «флаг
главнее», оба документа выровнены.
Замечания ревизора: не запрашивались — этап документальный, код не менялся.
Открытые вопросы: п.1, 3–15 §10 спецификации. П.2 закрыт.
Коммит: нет
Push: нет

Следующий шаг: владелец утверждает архитектуру вместе с уточнениями и
отвечает по п.11–13. Реализация S1–S5 не начата.

---

## Этап P0.2 — независимая сверка архитектуры с договорённостями
Дата: 2026-10-27
Статус: done (документальная редакция готова; ожидает утверждения перед S1)
Задание: перечитать предоставленные `poster2_spec.md`, `poster2_plan.md`,
`state.md` и `README.md`; сопоставить с прямыми решениями владельца
Poster 2.0, исправить противоречия и согласовать перечень работ.
Изменено (в пакете исправленных копий; **не** в локальном репозитории):
- `poster2_spec.md`: пересмотрен жизненный цикл «верхний анонс — нижний экран»;
  учтены несколько верхних Telegram-сообщений и отдельные `live_message_ids`,
  финализация без повторной отправки уже живого поста, правильная замена версии;
  уточнены ограничения Telegram и обработка ошибок.
- `poster2_spec.md`: закреплён точный формат BUILTIN: жирная шапка
  `[название]`, `[дата] // [время_начала]`, кликабельное `[место]`,
  отдельная строка `[адрес]`, тело `[описание]`, подвал `[контакт]`.
  Личный начальный шаблон выдаётся при регистрации/первом `/start`, не
  создаётся вновь после намеренного удаления.
- `poster2_spec.md`: уточнены два разных входа в **один** конструктор;
  свободное разовое место разрешено в кирпичике личного шаблона и не
  записывается в справочник; для создания по площадке место только
  зарегистрированное или вновь созданное. Адрес/ссылка автозаполняются,
  когда такие кирпичики есть в снимке.
- `poster2_spec.md`: отклонён предложенный командой общий максимум 10 фото.
  Сохранён действующий проектный предел до 10 фото, при этом один вызов
  `sendMediaGroup` имеет 2–10 фото; хвост из одного фото идёт через
  `sendPhoto`. Подпись должна визуально идти после последних фото,
  проверяется на Telegram-клиентах. 10 — **временный существующий
  проектный максимум**, а не новое прямое решение владельца.
- `poster2_spec.md`: исправлена автоматическая готовность: сначала все
  кирпичики, затем фотографии/«Без фотографий», потом готовый пост без
  подтверждения; при превышении лимита пользовательский ввод и фото
  сохранены, доступна правка **любого** кирпичика или отказ от фото.
- `poster2_spec.md`: админка должна реально работать для обладателей прав,
  а не завершаться диагностикой. Сведения о легаси не превращаются в
  требование сохранять старый девятишаговый конструктор/таблицы вечно.
- `poster2_plan.md`: этапы S1–S5 согласованы с новыми правилами;
  добавлены проверки многосообщенческих фото, порядка сообщений,
  fingerprint-дедупликации при финале, стартового личного шаблона,
  глобальной админки и чистоты чата.
- `state.md`: новый актуальный статус и эта запись P0.2; прежние P0/P0.1
  сохранены как история, а не требования к реализации.
- `README.md`: **не изменялся**; описывает действующие права команды,
  в том числе запрет push, который сам по себе не является решением по UX.
Проверки:
- Сверка по тексту предоставленных файлов и договорённостям — выполнена.
- Документальные проверки структуры и противоречий — выполнены.
- `make check`, интеграционные тесты Poster и real Telegram API — не
  запускались, потому что не было доступа к рабочему репозиторию/боту.
- Код, миграции, `post_bot.db`, ключи, работающий бот, реальные настройки
  OpenCode — **не изменялись**.
Замечания ревизора:
- Критические расхождения исправлены в документах, но исправления
  **пока не проверены против актуального исходного кода**.
- Отдельно предстоят тесты порядка медиа/служебного экрана и устранение
  дублей при переходе live → sent, особенно после рестарта и на 11 фото.
Открытые вопросы:
- Перед кодовой реализацией подтвердить редакцию P0.2 целиком.
- Миграции 15/16 в документах — технические **предложения**, не применены;
  точное SQL и выбор хранения массива живых ID уточняется по коду.
- Деталь приёма нескольких **раздельных** фото без лишней кнопки должна
  быть решена и протестирована в S4 в соответствии с автоматической
  готовностью после одной полной Telegram-партии.
- Нормы Bot API 4096/1024 после entities подтверждены; реальное отображение
  подписи при нескольких медиагруппах — предмет ручного Telegram-теста.
- Точный общий максимум фото отдельно владельцем не определялся;
  10 оставлен как существующая ёмкость, не объявлять его новой договорённостью.
Коммит: нет (исправленные файлы переданы отдельно)
Push: нет

Следующий шаг: заменить три файла в `Poster/docs/agent_team/` на новые
версии из пакета и дать владельцу проверить продуктовые решения. После
утверждения — передать менеджеру именно новую спецификацию и план,
выполнять S1–S5 с автотестами и независимой ревизией этапов.

Решение владельца от 2026-10-27: ограничить количество фотографий одним анонсом до 10. Решение окончательное, перенести в S4.