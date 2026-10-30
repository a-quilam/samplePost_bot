# utils/ui_texts.py
"""
Единый редактируемый каталог текстов интерфейса (Poster 2.2 U4a, дефект №10).

Владелец передаёт редактору ОДИН файл `config/ui_texts.yaml`, получает
исправленную версию и обновляет тексты интерфейса без правки Python-кода.

Устройство:

- `DEFAULTS` — вшитые дефолты. Это страховка: при отсутствующем, битом или
  неполном YAML бот работает на них, а не падает и не остаётся с половиной
  каталога.
- Файл читается один раз при импорте модуля (`reload_texts()` перечитывает
  явно, после правки YAML и перезапуска службы).
- `tr(key, **vars)` — единственная точка подстановки. Значения переменных
  ЭКРАНИРУЮТСЯ (`html.escape`), поэтому пользовательские данные не могут
  внедрить разметку. Шаблон экранируется целиком, кроме ключей с суффиксом
  `_html`: только они содержат доверенную Telegram-разметку (`<b>`, `<code>`).
- Ошибка подстановки (нет обязательной переменной, шаблон испорчен) не
  ломает отправку: в лог пишется ERROR, наружу уходит безопасный текст —
  вшитый дефолт этой же строки.
- `tr_safe` — исключение для значений, уже прошедших allow-list
  (`utils.tg_html.SafeHtml`): повторное экранирование испортило бы
  сохранённое форматирование. Используется точечно, с комментарием.

Валидация файла: синтаксис YAML, строковые значения, обязательные ключи и
совпадение набора переменных `{name}` с дефолтом. При любой ошибке файл
считается невалидным целиком: лог CRITICAL и работа на `DEFAULTS`
(бот остаётся живым, владелец видит причину в логах).

Проверка файла вручную: `.venv/bin/python -m utils.ui_texts`.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from utils.tg_html import escape_html

logger = logging.getLogger(__name__)

# Путь по умолчанию: Poster/config/ui_texts.yaml (рядом с config.py).
DEFAULT_TEXTS_PATH = Path(__file__).resolve().parent.parent / "config" / "ui_texts.yaml"

# Переменная окружения для тестов и нестандартных развёртываний.
PATH_ENV_VARIABLE = "UI_TEXTS_PATH"

# Подстановка: {place}, {step}, {total}, {value}, {limit}, {photos}, ...
PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")

# Ключи, без которых каталог считается неполным: это входные экраны бота.
# Если редактор случайно удалил один из них, файл признаётся невалидным —
# иначе бот запустился бы «наполовину» с чужим текстом.
REQUIRED_KEYS = frozenset({"menu.main", "menu.manage", "help.text_html"})

# Вшитые дефолты. Значения совпадают с прежними литералами Python-кода
# буквально: U4a переносит тексты, а не переписывает формулировки.
DEFAULTS: dict[str, str] = {
    # --- главное меню -------------------------------------------------------
    "menu.main": "Здравствуйте! 👋\n\nВыберите действие:",
    "menu.manage": (
        "⚙️ Управление\n\n"
        "Площадки, шаблоны и кирпичики — общие разделы. Администрирование "
        "видно только тем, кому владелец Poster действительно выдал права."
    ),
    "menu.outside.photo": (
        "Фото добавляются в анонсе на шаге «Фотографии». Начните анонс "
        "кнопкой «📍 Создать для площадки» или «📝 Создать по личному "
        "шаблону»."
    ),
    "menu.outside.text": (
        "Воспользуйтесь кнопками меню или наберите /help — список команд."
    ),
    "menu.cancel.place": "Сценарий выбора места отменён.",
    "menu.cancel.admin": "Административный сценарий отменён.",
    "menu.cancel.brick": "Создание или изменение кирпичика отменено. Данные не изменены.",
    "menu.cancel.template": "Сценарий шаблонов отменён. Данные шаблонов не изменены.",
    "menu.cancel.none": "Сейчас нет активного создания поста.",
    # --- справка ------------------------------------------------------------
    "help.text_html": (
        "<b>Помощь</b>\n\n"
        "Я собираю анонсы по шаблону: вопросы, живой текст сверху экрана, "
        "фотографии и готовый пост в этот чат.\n\n"
        "<b>Кнопки меню</b>\n"
        "— <b>«📍 Создать для площадки»</b> — список зарегистрированных "
        "площадок; тап по названию сразу начинает анонс по её стандартному "
        "шаблону. Новую площадку можно добавить там же — анонс откроется "
        "сразу после создания;\n"
        "— <b>«📝 Создать по личному шаблону»</b> — ваши личные шаблоны. Тап "
        "по названию сразу начинает анонс; создание и правка шаблонов — в "
        "том же разделе;\n"
        "— <b>«📂 Мои анонсы»</b> — незавершённые анонсы можно продолжить, "
        "завершённые остаются в истории; новые анонсы их не удаляют;\n"
        "— <b>«⚙️ Управление»</b> — площадки, шаблоны и кирпичики. Пункт "
        "«🛡 Администрирование» виден только при выданных правах;\n"
        "— <b>«❓ Помощь»</b> — эта справка.\n\n"
        "<b>Два входа в конструктор</b>\n"
        "«Создать для площадки» подставляет площадку и её стандартный "
        "шаблон, готовый пост приходит с адресом места. «Создать по личному "
        "шаблону» работает без площадки — адрес и контакты берутся из "
        "вашего шаблона. Оба входа ведут в один и тот же конструктор.\n\n"
        "<b>Фотографии</b>\n"
        "Можно отправить одно фото, несколько сразу или альбомом — бот "
        "соберёт партию и добавит её к анонсу. Лимит — "
        "{max_photos} фото на анонс; 11-е не примется, и экран покажет, что "
        "сообщение осталось.\n\n"
        "<b>Текст и лимиты</b>\n"
        "Без фотографий готовый анонс может занимать до "
        "{text_limit} символов, с фотографиями подпись — до "
        "{caption_limit}. Слишком длинный анонс не режется молча и не "
        "отправляется частями: бот покажет, что именно сократить, и "
        "предложит правку.\n\n"
        "<b>Ввод и очистка</b>\n"
        "Ваши сообщения с ответами и фото удаляются после того, как данные "
        "надёжно сохранены. Ошибочный ввод остаётся на экране, пока вы не "
        "исправите поле. Навигация по кнопкам не плодит сообщения: экран "
        "перерисовывается.\n\n"
        "<b>Готовый анонс</b>\n"
        "После отправки анонс можно открыть в «📂 Мои анонсы», изменить поля, "
        "фото или дополнительные кирпичики и применить изменения — придёт "
        "новая версия. Частичная отправка не дублирует уже доставленные "
        "сообщения.\n\n"
        "<b>Команды</b>\n"
        "/start — главное меню\n"
        "/new_wizard — новый анонс\n"
        "/places — площадки\n"
        "/templates — личные шаблоны\n"
        "/bricks — мои кирпичики\n"
        "/admin — кабинет, если у вас есть права администратора\n"
        "/drafts — мои анонсы (то же, что кнопка «📂 Мои анонсы»)\n"
        "/cancel — закрыть текущий диалог без удаления черновика\n"
        "/help — эта справка"
    ),
    # --- конструктор: вопрос, календарь, время ------------------------------
    "wizard.question.header": "🧱 Вопрос {step} из {total}: {title}",
    "wizard.question.description_html": "📝 <code>{description}</code>",
    "wizard.question.optional": "Поле необязательное: его можно пропустить.",
    "wizard.question.current_value": "Текущее значение: {value}",
    "wizard.question.skipped": (
        "Сейчас поле пропущено. Можно заполнить его или перейти дальше."
    ),
    "wizard.question.missing": "Значение пока не задано.",
    "wizard.question.error": "⚠️ {message}",
    "wizard.status.valid": "заполнено",
    "wizard.status.skipped": "пропущено",
    "wizard.status.missing": "не заполнено",
    "wizard.status.from_reference": "из справочника",
    "wizard.hint.short_text": "Ожидается короткий текст одним сообщением.",
    "wizard.hint.long_text": "Можно отправить несколько строк одним сообщением.",
    "wizard.hint.date": (
        "Выберите дату календарём или отправьте её текстом, например 31.12.2026."
    ),
    "wizard.hint.date_limits": "{base} Ограничения: {rules}",
    "wizard.hint.time": (
        "Отправьте время, например 18:30 или 9:00. Быстрый выбор: {presets}."
    ),
    "wizard.hint.url": (
        "Отправьте ссылку: https://…, t.me/… или домен. Опасные схемы "
        "не принимаются."
    ),
    "wizard.hint.number": "Отправьте число цифрами, например 12.",
    "wizard.hint.number_limits": "{base} Ограничения: {rules}.",
    "wizard.hint.choice_single": "Выберите один вариант кнопкой.",
    "wizard.hint.choice_multiple": (
        "Отметьте один или несколько вариантов кнопками, затем «Готово»."
    ),
    "wizard.hint.fallback": "Введите значение типа «{title}».",
    "wizard.number_rule.integer": "целое число",
    "wizard.number_rule.range": "от {min} до {max}",
    "wizard.number_rule.min": "не меньше {min}",
    "wizard.number_rule.max": "не больше {max}",
    "wizard.number_rule.suffix": "суффикс «{suffix}»",
    "wizard.date_rule.min": "не раньше {min_date}",
    "wizard.date_rule.max": "не позже {max_date}",
    "wizard.date_rule.horizon": "горизонт {months} мес.",
    # --- конструктор: шаг фотографий ---------------------------------------
    "wizard.photos.title": "📷 Фотографии анонса",
    "wizard.photos.hint": (
        "Пришлите фотографию или альбом. Можно добавить до {limit} фотографий."
    ),
    "wizard.photos.counter": "Добавлено фотографий: {count} из {limit}.",
    "wizard.photos.limit": (
        "⚠️ В анонсе уже {limit} фотографий — это максимум. "
        "Новые снимки не добавлены ({rejected}), уже принятые "
        "сохранены. Лишнее можно удалить на экране «📷 Фотографии»."
    ),
    "wizard.photos.accepted_one": "✅ Принято: {count} фотография.",
    "wizard.photos.accepted_many": "✅ Принято: {count} фотографии.",
    "wizard.photos.duplicate": "Это фото уже есть в анонсе — дубль не создан.",
    "wizard.photos.rejected": "Фото не добавлено: {reason}",
    # --- конструктор: экран завершения и правки -----------------------------
    "wizard.finished.text": (
        "✅ Анонс готов.\n\nСообщение выше можно скопировать или переслать"
    ),
    "wizard.finished.pending": (
        "✏️ Есть изменения, ещё не отправленные в чат. "
        "Нажмите «Применить изменения»."
    ),
    "wizard.finished.edit_text": (
        "✏️ Редактирование анонса\n\n"
        "Выберите, что изменить. Новая копия уходит в чат только по "
        "«Применить изменения»."
    ),
    "wizard.ready.collecting": (
        "✅ Анонс готов: он собирается и приходит в этот чат автоматически."
    ),
    "wizard.ready.sent": "✅ Анонс готов: он уже отправлен в этот чат.",
    # --- кирпичики: хаб, архив, пустые состояния ----------------------------
    "bricks.hub.title_html": "🧱 <b>Мои кирпичики</b>",
    "bricks.hub.intro": (
        "Кирпичик — это определение типизированного поля для будущих "
        "шаблонов анонса. Сейчас он не вставляется в посты и шаблоны."
    ),
    "bricks.hub.active_count": "Активных кирпичиков: {count}.",
    "bricks.hub.propose_hint": (
        "Чтобы предложить кирпичик месту или всей системе, нажмите "
        "«📨» в его строке."
    ),
    "bricks.hub.empty_archive": (
        "В активном списке пока нет кирпичиков. В архиве осталось: "
        "{archived}. Откройте «🗄 Архив», чтобы восстановить "
        "нужный или создать новый кирпичик."
    ),
    "bricks.hub.empty": (
        "У вас пока нет личных кирпичиков. Создайте первый — например, "
        "«Ведущий мероприятия» или «Формат участия»."
    ),
    "bricks.archive.title_html": "🗄 <b>Архив кирпичиков</b>",
    "bricks.archive.intro": (
        "Архивные кирпичики не показываются в активном списке, но остаются "
        "вашими. Их можно восстановить; код всё это время остаётся занятым."
    ),
    "bricks.page_note": (
        "Показано {from}–{to} из {total}. Страница {page} из {pages}."
    ),
    # --- места: справочник, поиск, регистрация ------------------------------
    "places.hub.title": "📍 Места",
    "places.hub.intro": (
        "Найдите место по названию или добавьте новое. Поиск учитывает "
        "канонические и альтернативные названия."
    ),
    "places.all.title": "📚 Все места — {total}",
    "places.all.empty": "📚 Все места\n\nВ справочнике пока нет мест.",
    "places.search.title": "🔎 Результаты поиска: «{query}»",
    "places.search.empty": (
        "🔎 Поиск: «{query}»\n\n"
        "Ничего не найдено. Место не создаётся автоматически — его можно "
        "добавить отдельно, после проверки дублей."
    ),
    "places.create_check.title": "🔍 Проверьте, нет ли уже этого места в базе",
    "places.create_check.input": "Вы введите: «{name}»",
    "places.create_check.possible": "Возможные совпадения:",
    "places.create_check.none": "Похожих мест не найдено.",
    "places.create_check.confirm": (
        "Если это другое заведение с похожим названием, создание нового "
        "места допустимо — но только по явной кнопке ниже."
    ),
    "places.card.canonical": "Каноническое название: {name}",
    "places.card.addresses": "Адрес: {address}",
    "places.card.link": "Ссылка: {url}",
    "places.card.city": "Город: {city}",
    "places.card.hint": (
        "Нажмите «✅ Подтвердить», чтобы выбрать это место, или "
        "«🔤 Выбрать альтернативное название» для другого написания."
    ),
    "places.page_note": "Показано {from}–{to} из {total}.\nСтраница {page}.",
    "places.manage.title": "📍 Площадки — {total}",
    "places.manage.empty": (
        "📍 Площадки\n\n"
        "В справочнике пока нет площадок. Добавьте первую — у неё сразу "
        "появится стандартный шаблон анонса."
    ),
    "places.manage.hint": (
        "Тап по площадке открывает карточку справочника: название, "
        "альтернативные названия, адрес и ссылка."
    ),
    "places.search_input": (
        "🔎 Поиск места\n\n"
        "Введите часть названия места. Регистр, «е/ё», пробелы и "
        "пунктуация не важны."
    ),
    "places.manage_search_input": (
        "🔎 Поиск места\n\n"
        "Введите часть названия. Регистр, «е/ё», пробелы и пунктуация "
        "не важны."
    ),
    "places.pick_search_input": (
        "🔎 Поиск площадки\n\n"
        "Введите часть названия. Регистр, «е/ё», пробелы и пунктуация "
        "не важны."
    ),
    "places.pick_search.empty": (
        "🔎 Поиск площадки: «{query}»\n\n"
        "Ничего не найдено. Площадка не создаётся автоматически — её можно "
        "добавить отдельно."
    ),
    "places.create_input": (
        "➕ Новое место\n\n"
        "Введите название нового места. Сначала проверим, нет ли уже "
        "похожего места в справочнике."
    ),
    "places.create_input.current": "Текущее название: «{name}»",
    "places.create_input.manage_hint": (
        "У нового места сразу появится стандартный шаблон "
        "анонса. Анонс не запускается."
    ),
    "places.create_city_input": (
        "🏙 В каком городе площадка?\n\n"
        "Название: «{name}»\n\n"
        "Введите город обычным текстом (например, Москва или "
        "Санкт-Петербург). Город виден в списке площадок и помогает "
        "найти место поиском."
    ),
    "places.pick.title": "📍 Создать для площадки",
    "places.pick.empty": (
        "В справочнике пока нет площадок. Добавьте первую — анонс "
        "начнётся сразу по её стандартному шаблону."
    ),
    "places.pick.intro": (
        "Выберите площадку — анонс начнётся сразу по её стандартному "
        "шаблону, без лишних подтверждений."
    ),
    # --- Poster 2.2 U4b: ссылка на создание анонса площадки -----------------
    "places.invite.title": (
        "🔗 Ссылка для создания анонса\n\n"
        "📍 {place}\n\n"
        "Отправьте ссылку тому, кто будет делать анонс. Бот откроет "
        "конструктор сразу с этой площадкой и её стандартным шаблоном: "
        "выбирать площадку и шаблон не нужно.\n\n"
        "Ссылка не даёт прав администратора: открывший не сможет "
        "редактировать площадку или шаблон."
    ),
    "places.invite.link": "{link}",
    "places.invite.regenerated": (
        "🔁 Прежняя ссылка отозвана — по ней конструктор больше не "
        "откроется. Новая ссылка ниже."
    ),
    "places.invite.invalid": (
        "Ссылка недействительна: возможно, её отозвали или площадка "
        "удалена. Открываю главное меню — можно начать анонс обычным способом."
    ),
    "places.invite.no_rights": "Ссылку выдаёт администратор площадки.",
    "places.invite.no_link": (
        "⚠️ Не удалось получить имя бота для ссылки. Попробуйте открыть "
        "раздел заново чуть позже."
    ),
    # --- тексты ошибок и устаревших кнопок ----------------------------------
    "errors.internal": (
        "⚠️ Произошла внутренняя ошибка. Попробуйте повторить действие чуть позже."
    ),
    "errors.domain": (
        "Экран устарел: данные изменились или не сохранились. "
        "Открываю главное меню — попробуйте ещё раз."
    ),
    "errors.stale.wizard": (
        "Экран устарел: данные уже изменились. Показано актуальное состояние."
    ),
    "errors.expired.wizard": (
        "Кнопка устарела после перезапуска. Откройте «📂 Мои анонсы» и "
        "продолжите сохранённый черновик."
    ),
    "errors.private_chat_only": (
        "Готовый анонс отправляется только в личный чат автора. "
        "Откройте мастер в личном чате с ботом — черновик сохранён."
    ),
    "errors.stale.places": "Кнопка устарела: откройте раздел «Места» заново.",
    "errors.stale.pick": "Кнопка устарела: откройте «Создать для площадки» заново.",
    "errors.stale.manage": "Кнопка устарела: откройте «Управление → Площадки» заново.",
    "errors.stale.bricks": "Кнопка устарела: откройте раздел заново.",
    "errors.stale.admin": "Кнопка устарела: откройте административный кабинет заново.",
    "errors.stale.templates": "Кнопка устарела: откройте шаблон заново.",
    # --- администрирование: уведомление и плашки о конфликте ----------------
    "admin.conflict.notification_html": (
        "⚔️ <b>Конфликт кодов кирпичиков</b>\n\n"
        "📍 {place}\n"
        "Код: <code>[{code}]</code>\n"
        "Местный кирпичик: {brick}\n\n"
        "Глобальный кирпичик с этим кодом одобрен и уже действует для новых "
        "привязок. Местный кирпичик не изменён: выберите, переименовать его "
        "или переключить шаблон места на глобальный.\n\n"
        "Откройте: /admin → «Мои места» → место → «⚔️ Конфликты кодов»."
    ),
    "admin.conflict.admins_both": (
        " Администраторов мест уведомлено: {notified}, " "не доставлено: {undelivered}."
    ),
    "admin.conflict.admins_notified": " Администраторов мест уведомлено: {notified}.",
    "admin.conflict.admins_undelivered": (
        " Уведомления администраторам мест не доставлены: {undelivered}; "
        "задача видна в карточке места."
    ),
    "admin.conflict.admins_queued": (
        " Уведомления администраторам мест поставлены в очередь; "
        "если администратора нет, задача видна в карточке места."
    ),
    "admin.conflict.author_delivered": " Автору отправлено уведомление.",
    "admin.conflict.author_undelivered": (
        " Уведомление автору не доставлено; решение сохранено, статус "
        "доступен в «Мои предложения»."
    ),
    # --- общие кнопки -------------------------------------------------------
    "buttons.main.create_place": "📍 Создать для площадки",
    "buttons.main.create_template": "📝 Создать по личному шаблону",
    "buttons.main.announcements": "📂 Мои анонсы",
    "buttons.main.manage": "⚙️ Управление",
    "buttons.main.help": "❓ Помощь",
    "buttons.manage.places": "📍 Площадки",
    "buttons.manage.templates": "📝 Шаблоны",
    "buttons.manage.bricks": "🧱 Кирпичики",
    "buttons.manage.admin": "🛡 Администрирование",
    "buttons.nav.main_menu": "◀️ Главное меню",
    "buttons.nav.back": "◀️ Назад",
    "buttons.nav.prev": "⬅️ Назад",
    "buttons.nav.next": "Вперёд ➡️",
    "buttons.nav.retry_search": "🔁 Искать снова",
    "buttons.wizard.skip": "⏭ Пропустить",
    "buttons.wizard.choose_month": "📅 Выбрать месяц",
    "buttons.finished.edit": "✏️ Изменить",
    "buttons.finished.menu": "🏠 В меню",
    "buttons.finished.data": "✏️ Данные",
    "buttons.finished.photos": "📷 Фотографии ({count})",
    "buttons.finished.extras": "➕ Кирпичики ({count})",
    "buttons.finished.apply": "🔄 Применить изменения",
    "buttons.bricks.create": "➕ Создать кирпичик",
    "buttons.bricks.proposals": "📤 Мои предложения",
    "buttons.bricks.archive": "🗄 Архив ({count})",
    "buttons.places.search": "🔎 Найти место",
    "buttons.places.all": "📚 Все места",
    "buttons.places.add": "➕ Добавить место",
    "buttons.places.invite": "🔗 Ссылка для создания анонса",
    "buttons.places.invite_regenerate": "🔁 Перевыпустить",
    # --- служебные сообщения экрана -----------------------------------------
    "screen.legacy_keyboard_reset": (
        "Интерфейс обновлён: кнопки теперь на экране сообщения."
    ),
}


@dataclass(frozen=True)
class TextsReport:
    """Итог чтения и проверки YAML-каталога (для логов и CLI-проверки)."""

    path: Path
    ok: bool
    used_defaults: bool
    keys: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def summary(self) -> str:
        state = "ok" if self.ok else "invalid"
        source = "defaults" if self.used_defaults else "yaml"
        return (
            f"ui_texts: {state}, source={source}, keys={self.keys}, "
            f"errors={len(self.errors)}, warnings={len(self.warnings)}, "
            f"path={self.path}"
        )


def texts_path() -> Path:
    """Путь к каталогу: `UI_TEXTS_PATH` или `config/ui_texts.yaml`."""
    raw = os.getenv(PATH_ENV_VARIABLE)
    if raw and raw.strip():
        return Path(raw.strip()).expanduser()
    return DEFAULT_TEXTS_PATH


def _flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """
    Плоский вид YAML: вложенные разделы → ключи через точку.

    Строковые ключи с точкой принимаются и в самом YAML: редактору так
    проще искать строку. Не-словари на верхнем уровне отбрасываются — это
    ошибка формата, её ловит вызывающая проверка.
    """
    flat: dict[str, Any] = {}
    if not isinstance(data, dict):
        return flat
    for key, value in data.items():
        name = str(key) if prefix == "" else f"{prefix}.{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, name))
        else:
            flat[name] = value
    return flat


def _placeholders(template: str) -> set[str]:
    """Имена переменных, которые шаблон обязан получить при подстановке."""
    return set(PLACEHOLDER_RE.findall(template))


def _render(template: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """Подставляет значения и возвращает (текст, список отсутствующих имён)."""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        missing.append(name)
        return ""

    return PLACEHOLDER_RE.sub(replace, template), missing


def validate_texts(
    data: Any, *, path: Path | None = None
) -> tuple[dict[str, str], TextsReport]:
    """
    Проверяет разобранный YAML и возвращает (каталог, отчёт).

    Ошибки формата/обязательных ключей/переменных → `ok=False` и пустой
    каталог: вызывающий слой берёт `DEFAULTS` целиком, а не смесь файла и
    дефолтов (иначе интерфейс стал бы непредсказуемым).
    """
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        errors.append("корень файла должен быть словарём разделов")
        return {}, TextsReport(
            path=path or texts_path(),
            ok=False,
            used_defaults=True,
            keys=0,
            errors=tuple(errors),
        )
    flat = _flatten(data)
    values: dict[str, str] = {}
    for key, value in flat.items():
        if not isinstance(value, str):
            errors.append(f"{key}: значение должно быть строкой")
            continue
        if key not in DEFAULTS:
            warnings.append(f"{key}: неизвестный ключ — строка не используется")
            continue
        expected = _placeholders(DEFAULTS[key])
        actual = _placeholders(value)
        if actual != expected:
            errors.append(
                f"{key}: переменные {sorted(actual)} не совпадают с дефолтом "
                f"{sorted(expected)}"
            )
            continue
        values[key] = value
    for key in sorted(REQUIRED_KEYS):
        if key not in values:
            missing_in_file = key not in flat
            if missing_in_file:
                errors.append(f"{key}: обязательный ключ отсутствует")
    for key in sorted(DEFAULTS):
        if key not in values and key not in flat:
            warnings.append(f"{key}: ключа нет в файле — используется дефолт")
    report = TextsReport(
        path=path or texts_path(),
        ok=not errors,
        used_defaults=bool(errors),
        keys=len(values) if not errors else len(DEFAULTS),
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
    return ({} if errors else values), report


def _load(path: Path) -> tuple[dict[str, str], TextsReport]:
    """Читает файл и проверяет его; любая проблема → дефолты и отчёт."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report = TextsReport(
            path=path,
            ok=False,
            used_defaults=True,
            keys=len(DEFAULTS),
            errors=("файл каталога не найден",),
        )
        return dict(DEFAULTS), report
    except UnicodeDecodeError as exc:
        # Файл сохранён не в UTF-8 (типичный «ANSI» редактора) или обрезан
        # посередине символа. Без этой ветки импорт модуля падал бы
        # UnicodeDecodeError вместе с errors.py и всеми handlers — бот не
        # стартовал бы вовсе. Считаем файл невалидным: дефолты + CRITICAL.
        report = TextsReport(
            path=path,
            ok=False,
            used_defaults=True,
            keys=len(DEFAULTS),
            errors=(
                "не удалось прочитать файл каталога: кодировка должна быть "
                f"UTF-8 (байт {exc.start}: {exc.reason})",
            ),
        )
        return dict(DEFAULTS), report
    except OSError as exc:
        report = TextsReport(
            path=path,
            ok=False,
            used_defaults=True,
            keys=len(DEFAULTS),
            errors=(f"файл каталога не читается: {exc}",),
        )
        return dict(DEFAULTS), report
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        report = TextsReport(
            path=path,
            ok=False,
            used_defaults=True,
            keys=len(DEFAULTS),
            errors=(f"синтаксис YAML: {exc}",),
        )
        return dict(DEFAULTS), report
    values, report = validate_texts(data, path=path)
    if not report.ok:
        return dict(DEFAULTS), report
    catalog = dict(DEFAULTS)
    catalog.update(values)
    return catalog, report


# Текущий каталог. Перезагружается только через reload_texts(): горячего
# релоада по изменению файла нет — владелец перезапускает службу.
_catalog: dict[str, str] = {}
_last_report: TextsReport | None = None


def _initial_load() -> None:
    """Загрузка при импорте: бот не должен запускаться без каталога."""
    global _catalog, _last_report
    _catalog, _last_report = _load(texts_path())
    if _last_report.ok:
        logger.info("Каталог текстов интерфейса загружен: %s", _last_report.summary())
        for warning in _last_report.warnings:
            logger.warning("ui_texts: %s", warning)
        return
    # CRITICAL — владелец должен увидеть причину в логах: бот работает на
    # вшитых дефолтах, правки YAML не применены.
    logger.critical(
        "Каталог текстов интерфейса невалиден (%s) — используются вшитые "
        "дефолты. %s",
        _last_report.path,
        "; ".join(_last_report.errors),
    )


def reload_texts(path: Path | str | None = None) -> TextsReport:
    """
    Перечитывает каталог (явная перезагрузка, без слежения за файлом).

    `path` — для тестов и нестандартного развёртывания. Возвращает отчёт
    проверки; при ошибке каталог целиком заменяется дефолтами.
    """
    global _catalog, _last_report
    target = Path(path) if path is not None else texts_path()
    _catalog, _last_report = _load(target)
    if _last_report.ok:
        logger.info("Каталог текстов перечитан: %s", _last_report.summary())
        for warning in _last_report.warnings:
            logger.warning("ui_texts: %s", warning)
    else:
        logger.critical(
            "Каталог текстов интерфейса невалиден (%s) — используются вшитые "
            "дефолты. %s",
            _last_report.path,
            "; ".join(_last_report.errors),
        )
    return _last_report


def last_report() -> TextsReport:
    """Отчёт последней загрузки (для диагностики и CLI)."""
    if _last_report is None:
        return TextsReport(
            path=texts_path(),
            ok=False,
            used_defaults=True,
            keys=len(DEFAULTS),
            errors=("каталог ещё не загружен",),
        )
    return _last_report


def texts() -> dict[str, str]:
    """Копия действующего каталога (изменение копии ни на что не влияет)."""
    return dict(_catalog)


def _lookup(key: str) -> tuple[str, bool]:
    """
    Шаблон и признак доверенной разметки для ключа.

    Неизвестный ключ — ошибка разработчика: в лог ERROR, наружу пустая
    строка. Каталог и дефолты синхронны, поэтому в рабочем коде такого быть
    не должно (это ловит тест каталога).
    """
    template = _catalog.get(key)
    if template is None:
        template = DEFAULTS.get(key)
        if template is None:
            logger.error("ui_texts: неизвестный ключ текста %r", key)
            return "", False
    return template, key.endswith("_html")


def tr(key: str, **values: object) -> str:
    """
    Текст интерфейса по ключу с безопасной подстановкой.

    - значения экранируются (`html.escape`), поэтому `<b>` из данных
      пользователя остаётся текстом;
    - сам шаблон используется как разметка ТОЛЬКО для ключей `*_html`;
    - нет обязательной переменной или шаблон испорчен → ERROR в лог и
      безопасный текст: вшитый дефолт этой же строки.
    """
    template, is_markup = _lookup(key)
    if not template:
        return ""
    if not is_markup:
        template = escape_html(template)
    prepared = {
        name: escape_html("" if value is None else str(value))
        for name, value in values.items()
    }
    text, missing = _render(template, prepared)
    if not missing:
        return text
    logger.error(
        "ui_texts: для ключа %r не переданы переменные %s — показан дефолт",
        key,
        ", ".join(sorted(set(missing))),
    )
    fallback = DEFAULTS.get(key, template)
    if not is_markup:
        fallback = escape_html(fallback)
    return _render(fallback, prepared)[0]


def tr_safe(key: str, **values: object) -> str:
    """
    Подстановка значений, УЖЕ прошедших allow-list (SafeHtml), без экранирования.

    Единственный штатный случай — сохранённое форматирование поля анонса
    (`stored_value_html`): повторное экранирование показало бы `&lt;b&gt;`
    вместо жирного текста. Для нового кода использовать `tr`: здесь
    безопасность значения — ответственность вызывающего слоя.
    """
    template, _is_markup = _lookup(key)
    if not template:
        return ""
    prepared = {
        name: "" if value is None else str(value) for name, value in values.items()
    }
    text, missing = _render(template, prepared)
    if not missing:
        return text
    logger.error(
        "ui_texts: для ключа %r не переданы переменные %s",
        key,
        ", ".join(sorted(set(missing))),
    )
    return _render(DEFAULTS.get(key, template), prepared)[0]


_initial_load()


def main() -> int:
    """
    CLI-проверка каталога: `.venv/bin/python -m utils.ui_texts`.

    Код возврата 0 — файл валиден, 1 — есть ошибки (подробности в stdout).
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = reload_texts()
    print(report.summary())
    for error in report.errors:
        print(f"ERROR: {error}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover — ручная проверка файла
    raise SystemExit(main())
