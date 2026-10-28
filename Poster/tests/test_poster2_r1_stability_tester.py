# tests/test_poster2_r1_stability_tester.py
"""
R1 (Poster 2.1) — независимые сценарии тестировщика: №22, №23, №21.

Написано НЕ разработчиком: поведение проверяется по требованиям владельца,
без опоры на внутренние константы и хелперы продуктового кода. Все сценарии
идут через настоящий `Application.process_update`, FakeBot и временную
SQLite (`tests.integration_harness`): реальные `post_bot.db`, `.env` и
Telegram не используются.

Покрытие:
- №22: «◀️ Назад» из списка альтернативных названий ведёт на карточку
  места, включая обратный путь через добавление названия и отмену;
  пользователь не видит «Произошла внутренняя ошибка» и «Кнопка устарела».
- №23: `ReplyKeyboardRemove` уходит ровно один раз на вход в актуальный UI
  (/start, главное меню, оба старта конструктора, кабинет), служебное
  сообщение удаляется; сбой Telegram на отправке/удалении не ломает вход.
- №21: у любого ввода в фазе поиска получателя есть явный исход; негативные
  ветки (не суперадмин, устаревшая фаза, мусор, «не найден», уже есть админ,
  место исчезло) дают alert/текст, а не тишину.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from telegram import ReplyKeyboardRemove
from telegram.error import BadRequest

from models import PlaceAdmin, PlaceName, utcnow
from place_store import create_place, normalize_place_name
from template_store import create_personal_template
from user_store import get_or_create_user

try:
    from admin_state import load_admin_session, save_admin_session
    from place_admin_store import get_active_place_admin
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)


OTHER_SUPER_TG = 7_810_999
SUPERADMIN_TG = 7_810_100
PLACE_ADMIN_TG = 7_810_150
TARGET_TG = 7_810_200
UNKNOWN_TG = 7_810_300

DENIED_ALERT = "Доступ к административному разделу закрыт."
STALE_ALERT = "Кнопка устарела: откройте административный кабинет заново."
ERROR_NOTICE_FRAGMENT = "Произошла внутренняя ошибка"


# --- окружение -------------------------------------------------------------


@contextmanager
def patched_superadmin(telegram_user_id: int = SUPERADMIN_TG):
    """Суперадминистратор только на время блока (живая конфигурация)."""
    with patch("config.SUPERADMIN_USER_IDS", (telegram_user_id,)):
        yield


def ensure_user(
    h,
    telegram_user_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
) -> int:
    with h.session_factory() as session:
        return int(
            get_or_create_user(
                session,
                telegram_user_id,
                username=username,
                full_name=full_name or f"Пользователь {telegram_user_id}",
            ).id
        )


def make_place(h, name: str = "Дом культуры", *, with_address: bool = True) -> int:
    """Место через доменный сервис: каноническое название + стандартный шаблон."""
    with h.session_factory() as session:
        place = create_place(
            session,
            name,
            created_by_user_id=None,
            address="ул. Тестовая, 1" if with_address else None,
        )
        return int(place.id)


def make_place_admin(h, place_id: int, telegram_user_id: int) -> int:
    """Выдаёт действующую роль администратора места (подготовка фикстуры)."""
    ensure_user(h, telegram_user_id, username=f"admin{telegram_user_id}")
    with h.session_factory() as session:
        from models import User

        user = (
            session.query(User).filter(User.telegram_user_id == telegram_user_id).one()
        )
        session.add(
            PlaceAdmin(
                place_id=place_id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        session.commit()
        return int(user.id)


def make_personal_template(h, *, actor_tg: int = 7, name: str = "Личный") -> int:
    ensure_user(h, actor_tg, username=f"user{actor_tg}")
    with h.session_factory() as session:
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            header="",
            body="[название]",
            footer="",
        )
        return int(template.template_id)


def add_alias_raw(h, place_id: int, name: str) -> None:
    """Альтернативное название напрямую — только для подготовки данных."""
    with h.session_factory() as session:
        session.add(
            PlaceName(
                place_id=place_id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=0,
            )
        )
        session.commit()


def alias_names(h, place_id: int) -> list[str]:
    with h.session_factory() as session:
        rows = session.query(PlaceName).filter(PlaceName.place_id == place_id).all()
        return sorted(row.name for row in rows if not row.is_canonical)


def last_alert(h) -> str | None:
    answers = h.answers()
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


def all_visible_texts(h) -> str:
    """Всё, что пользователь мог увидеть: ответы, экраны и alert'ы."""
    parts = list(h.reply_texts()) + list(h.screen_texts())
    parts += [str(c.get("text") or "") for c in h.answers()]
    return "\n".join(parts)


def buttons(h) -> list[tuple[str, str | None]]:
    return h.screen_buttons()


def callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in buttons(h)
        if data is not None and data.startswith(prefix)
    ]


def one_callback(h, prefix: str) -> str:
    found = callbacks(h, prefix)
    if not found:
        raise AssertionError(f"нет кнопки {prefix!r}; экран: {buttons(h)!r}")
    return found[0]


def is_keyboard_remove(markup: Any) -> bool:
    return isinstance(markup, ReplyKeyboardRemove)


def reset_calls(h) -> list[dict]:
    """Успешно ДОСТАВЛЕННЫЕ вызовы снятия reply-клавиатуры."""
    return [
        call
        for call in h.calls
        if call["type"] == "message" and is_keyboard_remove(call.get("reply_markup"))
    ]


def any_reply_keyboard_markup(h) -> bool:
    """Отправлялась ли клиенту reply-клавиатура (её быть не должно вовсе)."""
    for call in h.calls:
        markup = call.get("reply_markup")
        if markup is not None and type(markup).__name__ == "ReplyKeyboardMarkup":
            return True
    return False


@contextmanager
def spy_reset_messages(h):
    """
    Запоминает message_id служебных сообщений с ReplyKeyboardRemove.

    Нужен, чтобы проверить именно удаление того сообщения, которое ушло
    клиенту (а не «какое-то» удаление), не полагаясь на нумерацию FakeBot.
    """
    captured: list[int] = []
    original = h.bot.send_message

    async def wrapper(*args: Any, **kwargs: Any):
        message = await original(*args, **kwargs)
        if is_keyboard_remove(kwargs.get("reply_markup")):
            captured.append(int(message.message_id))
        return message

    h.bot.send_message = wrapper
    try:
        yield captured
    finally:
        h.bot.send_message = original


@contextmanager
def failing_reset_send(h, error: Exception | None = None):
    """Telegram отказывает ТОЛЬКО на служебном сообщении снятия клавиатуры."""
    boom = error or BadRequest("Bad Request: chat not found")
    original = h.bot.send_message

    async def wrapper(*args: Any, **kwargs: Any):
        if is_keyboard_remove(kwargs.get("reply_markup")):
            raise boom
        return await original(*args, **kwargs)

    h.bot.send_message = wrapper
    try:
        yield
    finally:
        h.bot.send_message = original


def open_admin_place(h) -> int:
    """Кабинет → карточка места (суперадмин уже выставлен в h.user_id)."""
    place_id = make_place(h)
    return place_id


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AliasesBackNavigationTests(unittest.TestCase):
    """Дефект №22: «Назад» из списка названий — карточка места, без ошибок."""

    async def _open_place_card(self, h, place_id: int) -> None:
        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        self.assertIn("🛡 Место", h.screen_text())

    @scenario_test
    async def test_full_path_card_aliases_add_cancel_alias_back_to_card(self, h):
        """Карточка → алиасы → добавить → отмена → список → «Назад»."""
        place_id = make_place(h)
        add_alias_raw(h, place_id, "Локал")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_place_card(h, place_id)

            await h.callback(f"adm_edit_place:{place_id}")
            self.assertIn("Редактирование места", h.screen_text())
            await h.callback(f"adm_aliases:{place_id}")
            self.assertIn("Альтернативные названия", h.screen_text())
            self.assertIn("Локал", h.screen_text())

            # Добавление названия: ввод → подтверждение → отмена.
            await h.callback(one_callback(h, "adm_alias_add:"))
            self.assertIn("Добавление альтернативного названия", h.screen_text())
            await h.text("Локал Сити")
            self.assertIn("Подтвердите добавление", h.screen_text())

            await h.callback("adm_cancel")
            # Отмена добавления возвращает именно список названий.
            self.assertIn("Альтернативные названия", h.screen_text())
            self.assertTrue(
                callbacks(h, "adm_alias_add:"),
                "в списке названий нет кнопки добавления",
            )
            # Данные не изменены отменой.
            self.assertEqual(alias_names(h, place_id), ["Локал"])

            # Теперь «◀️ Назад» из списка названий.
            await h.callback("adm_cancel")
            self.assertIn("🛡 Место", h.screen_text())
            self.assertIn("Дом культуры", h.screen_text())

        # Ни «внутренней ошибки», ни «кнопка устарела» на всём пути.
        visible = all_visible_texts(h)
        self.assertNotIn(ERROR_NOTICE_FRAGMENT, visible)
        self.assertNotIn(STALE_ALERT, visible)
        state = load_admin_session(SUPERADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")

    @scenario_test
    async def test_back_from_empty_aliases_list_returns_to_card(self, h):
        """Список без названий: кнопка «Назад» тоже ведёт на карточку."""
        place_id = make_place(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_place_card(h, place_id)
            await h.callback(f"adm_edit_place:{place_id}")
            await h.callback(f"adm_aliases:{place_id}")
            self.assertIn("Пока нет альтернативных названий", h.screen_text())

            await h.callback("adm_cancel")

        self.assertIn("🛡 Место", h.screen_text())
        self.assertNotIn(ERROR_NOTICE_FRAGMENT, all_visible_texts(h))
        self.assertNotIn(STALE_ALERT, all_visible_texts(h))

    @scenario_test
    async def test_back_from_aliases_after_rename_confirm(self, h):
        """Отмена переименования: подтверждение → список → карточка."""
        place_id = make_place(h)
        add_alias_raw(h, place_id, "Локал")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_place_card(h, place_id)
            await h.callback(f"adm_edit_place:{place_id}")
            await h.callback(f"adm_aliases:{place_id}")
            await h.callback(one_callback(h, "adm_alias_rename:"))
            self.assertIn("Переименование", h.screen_text())
            await h.text("Локал Новый")
            self.assertIn("Подтвердите переименование", h.screen_text())

            await h.callback("adm_cancel")
            self.assertIn("Альтернативные названия", h.screen_text())
            await h.callback("adm_cancel")

        self.assertIn("🛡 Место", h.screen_text())
        self.assertNotIn(ERROR_NOTICE_FRAGMENT, all_visible_texts(h))
        self.assertNotIn(STALE_ALERT, all_visible_texts(h))
        self.assertEqual(alias_names(h, place_id), ["Локал"])

    @scenario_test
    async def test_repeated_back_does_not_break_navigation(self, h):
        """Повторное нажатие «Назад» (устаревшая кнопка) — явное сообщение."""
        place_id = make_place(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_place_card(h, place_id)
            await h.callback(f"adm_edit_place:{place_id}")
            await h.callback(f"adm_aliases:{place_id}")
            await h.callback("adm_cancel")
            self.assertIn("🛡 Место", h.screen_text())
            # Кнопка со старого экрана: фаза уже place, повторный adm_cancel
            # должен дать защитное «Кнопка устарела», а не падение.
            await h.callback("adm_cancel")

        self.assertNotIn(ERROR_NOTICE_FRAGMENT, all_visible_texts(h))
        self.assertEqual(last_alert(h), STALE_ALERT)
        self.assertIn("🛡 Место", h.screen_text())


# --- №23: снятие легаси reply-клавиатуры -----------------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class LegacyReplyKeyboardTests(unittest.TestCase):
    """Дефект №23: на входе в UI уходит ровно один ReplyKeyboardRemove."""

    async def _assert_entry_dropped_keyboard(self, h, marker: str) -> None:
        """(общий помощник не нужен — снятие видно по spy_reset_messages)"""

    @scenario_test
    async def test_start_drops_keyboard_once_and_deletes_service_message(self, h):
        with spy_reset_messages(h) as reset_ids:
            await h.text("/start")

        self.assertEqual(len(reset_ids), 1, "на /start нужно ровно одно снятие")
        self.assertIn(reset_ids[0], h.deleted_ids(), "служебное сообщение осталось")
        self.assertIn("Выберите действие", h.screen_text())
        self.assertFalse(any_reply_keyboard_markup(h))

    @scenario_test
    async def test_main_menu_callback_drops_keyboard(self, h):
        """Свежая сессия: вход в меню по callback тоже снимает клавиатуру."""
        with spy_reset_messages(h) as reset_ids:
            await h.callback("main_menu")

        self.assertEqual(len(reset_ids), 1, "callback меню не снял клавиатуру")
        self.assertIn(reset_ids[0], h.deleted_ids())
        self.assertIn("Выберите действие", h.screen_text())

    @scenario_test
    async def test_navigation_between_screens_does_not_repeat_removal(self, h):
        """Повторных снятий нет: признак в user_data подавляет их на экранах."""
        with spy_reset_messages(h) as reset_ids:
            await h.text("/start")
            self.assertEqual(len(reset_ids), 1)
            await h.callback("main_menu")
            await h.callback("manage")
            await h.callback("main_menu")
            await h.callback("help")

        self.assertEqual(len(reset_ids), 1, "клавиатура снимается повторно")

    @scenario_test
    async def test_place_pick_entry_drops_keyboard(self, h):
        place_id = make_place(h)
        with spy_reset_messages(h) as reset_ids:
            await h.callback(f"pl_pick:{place_id}")

        self.assertEqual(len(reset_ids), 1, "старт конструктора не снял клавиатуру")
        self.assertIn(reset_ids[0], h.deleted_ids())
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertTrue(callbacks(h, "aw_"), "конструктор должен жить на inline")

    @scenario_test
    async def test_personal_template_entry_drops_keyboard(self, h):
        template_id = make_personal_template(h)
        with spy_reset_messages(h) as reset_ids:
            await h.callback(f"tp_pick:{template_id}")

        self.assertEqual(len(reset_ids), 1)
        self.assertIn(reset_ids[0], h.deleted_ids())
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_admin_entry_drops_keyboard(self, h):
        make_place(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            with spy_reset_messages(h) as reset_ids:
                await h.text("/admin")

        self.assertEqual(len(reset_ids), 1, "вход в кабинет не снял клавиатуру")
        self.assertIn(reset_ids[0], h.deleted_ids())
        self.assertIn("🛡 Все места", h.screen_text())

    @scenario_test
    async def test_send_failure_does_not_break_entry(self, h):
        """Telegram отказал на служебном сообщении — меню всё равно открылось."""
        with failing_reset_send(h):
            await h.text("/start")

        self.assertFalse(reset_calls(h), "сбойный вызов не должен считаться доставкой")
        self.assertIn("Выберите действие", h.screen_text())
        self.assertTrue(buttons(h), "главное меню должно остаться рабочим")
        self.assertNotIn(ERROR_NOTICE_FRAGMENT, all_visible_texts(h))

    @scenario_test
    async def test_delete_failure_does_not_break_entry(self, h):
        """Удалить служебное сообщение не удалось — вход всё равно работает."""
        h.bot.errors_by_type = {
            "delete_message": BadRequest("Bad Request: message can't be deleted")
        }
        await h.text("/start")

        self.assertEqual(len(reset_calls(h)), 1)
        self.assertIn("Выберите действие", h.screen_text())
        self.assertNotIn(ERROR_NOTICE_FRAGMENT, all_visible_texts(h))

    @scenario_test
    async def test_restart_repeats_removal_observation(self, h):
        """
        Наблюдение (не блокер): признак снятия живёт в user_data и теряется
        при рестарте процесса, поэтому после рестарта снятие повторится.
        Требование владельца — не спамить на КАЖДОМ экране внутри сессии.
        """
        with spy_reset_messages(h) as reset_ids:
            await h.text("/start")
            self.assertEqual(len(reset_ids), 1)
            # Навигация по экранам внутри сессии не добавляет снятий.
            await h.callback("manage")
            await h.callback("main_menu")
            self.assertEqual(len(reset_ids), 1)

            await h.restart()
            await h.text("/start")
            self.assertEqual(
                len(reset_ids),
                2,
                "после рестарта процесса снятие повторяется (ожидаемое наблюдение)",
            )


# --- №21: назначение администратора места ----------------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AssignAdminSearchTests(unittest.TestCase):
    """Дефект №21: у поиска получателя и подтверждения всегда явный исход."""

    async def _open_search(self, h) -> int:
        place_id = make_place(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(f"adm_place:{place_id}")
            await h.callback(f"adm_assign:{place_id}")
        self.assertIn("Поиск получателя", h.screen_text())
        return place_id

    # --- позитивные ветки --------------------------------------------------

    @scenario_test
    async def test_known_username_lists_result(self, h):
        ensure_user(h, TARGET_TG, username="a_quilam")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text("@a_quilam")
        self.assertIn("@a_quilam", h.screen_text())
        self.assertTrue(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_known_numeric_id_lists_result(self, h):
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text(str(TARGET_TG))
        self.assertIn(str(TARGET_TG), h.screen_text())
        self.assertTrue(callbacks(h, "adm_pick:"))

    # --- негативные ветки ввода -------------------------------------------

    @scenario_test
    async def test_empty_and_garbage_input_reports_format(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            for garbage in ("   ", "@", "!!!", "…—", "🙂🙂"):
                with self.subTest(garbage=garbage):
                    await h.text(garbage)
                    text = h.screen_text()
                    self.assertIn("Неверный формат", text, f"ввод {garbage!r}")
                    # Фаза сохранена: следующий ввод снова ищет.
                    self.assertIn("Поиск получателя", text)
                    state = load_admin_session(SUPERADMIN_TG)
                    assert state is not None
                    self.assertEqual(state["phase"], "assign_search")

    @scenario_test
    async def test_unknown_username_explains_start(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text("@nobody_here")
        text = h.screen_text()
        self.assertIn("не найден", text.lower())
        self.assertIn("@nobody_here", text)
        self.assertIn("/start", text)
        self.assertFalse(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_unknown_numeric_id_explains_start(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text(str(UNKNOWN_TG))
        text = h.screen_text()
        self.assertIn("не найден", text.lower())
        self.assertIn(str(UNKNOWN_TG), text)
        self.assertIn("/start", text)
        self.assertFalse(callbacks(h, "adm_pick:"))

    # --- подтверждение выбора ---------------------------------------------

    @scenario_test
    async def test_success_branch_reports_assignment(self, h):
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = await self._open_search(h)
            await h.text("@target")
            await h.callback(one_callback(h, "adm_pick:"))
            self.assertIn("Подтвердите назначение", h.screen_text())
            await h.callback("adm_confirm")

        self.assertIn("Администратор назначен", h.screen_text())
        with h.session_factory() as session:
            active = get_active_place_admin(session, place_id)
        assert active is not None
        self.assertEqual(active.user.telegram_user_id, TARGET_TG)

    @scenario_test
    async def test_already_admin_branch_is_explicit(self, h):
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            place_id = await self._open_search(h)
            await h.text("@target")
            await h.callback(one_callback(h, "adm_pick:"))
            await h.callback("adm_confirm")
        self.assertIn("Администратор назначен", h.screen_text())

        # Тап по устаревшему экрану результатов: место уже занято.
        save_admin_session(
            SUPERADMIN_TG,
            {
                "phase": "assign_results",
                "page": 0,
                "place_id": place_id,
                "mode": "assign",
                "query": "@target",
                "query_kind": "username",
            },
        )
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.callback(f"adm_pick:{TARGET_TG}")

        alert = last_alert(h) or ""
        self.assertTrue(alert, "нажатие не дало никакого результата")
        self.assertIn("уже", alert.lower())
        self.assertIn("🛡 Место", h.screen_text())

    @scenario_test
    async def test_missing_place_branch_is_explicit(self, h):
        ensure_user(h, TARGET_TG, username="target")
        save_admin_session(
            SUPERADMIN_TG,
            {
                "phase": "assign_results",
                "page": 0,
                "place_id": 987_654,
                "mode": "assign",
                "query": "@target",
                "query_kind": "username",
            },
        )
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.callback(f"adm_pick:{TARGET_TG}")

        alert = (last_alert(h) or "").lower()
        self.assertIn("не существует", alert)

    @scenario_test
    async def test_confirm_when_rights_lost_is_explicit(self, h):
        """Права отозваны между выбором и подтверждением — отказ, не тишина."""
        ensure_user(h, TARGET_TG, username="target")
        place_id = make_place(h)
        save_admin_session(
            SUPERADMIN_TG,
            {
                "phase": "confirm_assign",
                "page": 0,
                "place_id": place_id,
                "mode": "assign",
                "target_telegram_user_id": TARGET_TG,
            },
        )
        with patch("config.SUPERADMIN_USER_IDS", (OTHER_SUPER_TG,)):
            h.user_id = SUPERADMIN_TG
            await h.callback("adm_confirm")

        self.assertEqual(last_alert(h), DENIED_ALERT)
        self.assertNotIn("Администратор назначен", h.screen_text())

    # --- негативные проверки доступа --------------------------------------

    @scenario_test
    async def test_non_superadmin_cannot_open_assign_flow(self, h):
        place_id = make_place(h)
        make_place_admin(h, place_id, PLACE_ADMIN_TG)

        h.user_id = PLACE_ADMIN_TG
        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        self.assertIn("🛡 Место", h.screen_text())

        # Не-суперадмин не может запустить назначение.
        await h.callback(f"adm_assign:{place_id}")
        self.assertEqual(last_alert(h), DENIED_ALERT)
        # И не может «подтвердить» чужой выбор.
        await h.callback(f"adm_pick:{TARGET_TG}")
        self.assertEqual(last_alert(h), DENIED_ALERT)

    @scenario_test
    async def test_non_superadmin_text_in_search_phase_is_denied(self, h):
        place_id = make_place(h)
        ensure_user(h, PLACE_ADMIN_TG, username="place_admin")
        save_admin_session(
            PLACE_ADMIN_TG,
            {"phase": "assign_search", "page": 0, "place_id": place_id},
        )
        h.user_id = PLACE_ADMIN_TG
        await h.text("@target")

        self.assertIn("доступен только администраторам", h.screen_text().lower())
        self.assertNotIn("Найдено", h.screen_text())

    @scenario_test
    async def test_stale_phase_buttons_still_report_stale(self, h):
        """Старое защитное поведение сохранено: чужая фаза → «Кнопка устарела»."""
        make_place(h)  # место в кабинете, но ни одна фаза не активна
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            for callback_data in (
                "adm_cancel",
                "adm_confirm",
                "adm_search_again",
            ):
                with self.subTest(callback=callback_data):
                    await h.callback(callback_data)
                    self.assertEqual(last_alert(h), STALE_ALERT)

            # Кнопка выбора получателя вне фазы результатов.
            await h.callback(f"adm_pick:{TARGET_TG}")
            self.assertEqual(last_alert(h), STALE_ALERT)
