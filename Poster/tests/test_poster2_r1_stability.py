# tests/test_poster2_r1_stability.py
"""
R1 (Poster 2.1): стабильность навигации кабинета и критичный UX.

Дефекты владельца, закрываемые здесь:

- №22 — «◀️ Назад» из списка альтернативных названий: раньше кнопка
  `adm_cancel` в фазе `edit_aliases` не входила в allow-list
  `handle_cancel_flow` → «Произошла внутренняя ошибка», затем «Кнопка
  устарела». Теперь возврат ведёт к карточке места. Плюс таблица
  «фаза-источник → целевая фаза» для ВСЕХ кнопок `adm_cancel` кабинета;
- №23 — легаси reply-клавиатура старых версий: на входах в актуальный UI
  уходит `ReplyKeyboardRemove`, дальше интерфейс живёт на inline-кнопках;
- №21 — поиск получателя: у любого ввода в фазе `assign_search` есть явный
  исход (@username / числовой ID / мусор; найдено / не найдено).

Все сценарии идут через настоящий `Application.process_update`, FakeBot и
временную SQLite (tests.integration_harness): реальные Telegram, `.env` и
`post_bot.db` не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from models import Place, PlaceName

try:
    from admin_state import load_admin_session, save_admin_session
    from place_store import normalize_place_name
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)


SUPERADMIN_TG = 8_810_100
TARGET_TG = 8_810_200
UNKNOWN_TG = 8_810_300

DENIED_ALERT = "Доступ к административному разделу закрыт."
STALE_ALERT = "Кнопка устарела: откройте административный кабинет заново."


def patched_superadmin():
    """Супер­администратор только на время блока (живая конфигурация)."""
    return patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,))


def insert_place(
    h, name: str = "Кофейня «Локал»", *, with_address: bool = False
) -> int:
    """Место с каноническим названием напрямую в базе."""
    session = h.session_factory()
    try:
        place = Place(address="ул. Тестовая, 1" if with_address else None)
        session.add(place)
        session.flush()
        session.add(
            PlaceName(
                place_id=place.id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=1,
            )
        )
        session.commit()
        return int(place.id)
    finally:
        session.close()


def ensure_user(h, telegram_user_id: int, username: str | None = None) -> None:
    from user_store import get_or_create_user

    session = h.session_factory()
    try:
        get_or_create_user(session, telegram_user_id, username=username)
    finally:
        session.close()


def add_alias(h, place_id: int, name: str) -> None:
    session = h.session_factory()
    try:
        session.add(
            PlaceName(
                place_id=place_id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=0,
            )
        )
        session.commit()
    finally:
        session.close()


def last_alert(h) -> str | None:
    answers = h.answers()
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


def callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def callback(h, prefix: str) -> str:
    found = callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


def reply_keyboard_removals(h) -> list[dict]:
    """Вызовы send_message с ReplyKeyboardRemove в reply_markup."""
    out: list[dict] = []
    for call in h.calls:
        markup = call.get("reply_markup")
        if call["type"] != "message" or markup is None:
            continue
        if type(markup).__name__ == "ReplyKeyboardRemove":
            out.append(call)
    return out


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AliasesBackTests(unittest.TestCase):
    """Дефект №22: «◀️ Назад» из альтернативных названий ведёт к месту."""

    @scenario_test
    async def test_back_from_aliases_returns_to_place_card(self, h):
        place_id = insert_place(h)
        add_alias(h, place_id, "Локал")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(f"adm_place:{place_id}")
            await h.callback(f"adm_edit_place:{place_id}")
            self.assertIn("Редактирование места", h.screen_text())
            await h.callback(f"adm_aliases:{place_id}")
            self.assertIn("Альтернативные названия", h.screen_text())
            self.assertTrue(callbacks(h, "adm_alias_rename:"))
            await h.callback("adm_cancel")

        self.assertNotEqual(last_alert(h), STALE_ALERT)
        # Карточка места, а не список названий и не экран ошибки.
        self.assertIn("🛡 Место", h.screen_text())
        self.assertIn("Кофейня «Локал»", h.screen_text())
        # Список названий закрыт: кнопок управления алиасами больше нет.
        self.assertFalse(callbacks(h, "adm_alias_rename:"))
        self.assertFalse(callbacks(h, "adm_alias_add:"))
        self.assertIn(
            "👤 Назначить администратора",
            [label for label, _data in h.screen_buttons()],
        )
        state = load_admin_session(SUPERADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "place")

    @scenario_test
    async def test_back_from_aliases_without_place_resets_root(self, h):
        """Валидного place_id нет — корневой экран, никакой ошибки."""
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            save_admin_session(SUPERADMIN_TG, {"phase": "edit_aliases", "page": 0})
            await h.callback("adm_cancel")

        self.assertNotEqual(last_alert(h), STALE_ALERT)
        self.assertIn("🛡 Все места", h.screen_text())
        state = load_admin_session(SUPERADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "places")


# «Фаза-источник → целевая фаза»: каждая кнопка adm_cancel кабинета.
# Ветки соответствуют реальным кнопкам (см. handlers/admin.py): поиск
# получателя, экраны передачи, меню редактирования, ввод/подтверждения
# полей, список альтернативных названий.
CANCEL_TARGETS: tuple[tuple[str, str], ...] = (
    ("assign_search", "place"),
    ("assign_results", "place"),
    ("transfer_username_input", "place"),
    ("transfer_candidates", "place"),
    ("transfer_confirm_known", "place"),
    ("transfer_confirm_invite", "place"),
    ("transfer_outgoing", "place"),
    ("edit_menu", "place"),
    ("edit_aliases", "place"),
    ("edit_name_input", "edit_menu"),
    ("edit_name_confirm", "edit_menu"),
    ("edit_address_input", "edit_menu"),
    ("edit_address_confirm", "edit_menu"),
    ("edit_address_delete_confirm", "edit_menu"),
    ("edit_url_input", "edit_menu"),
    ("edit_url_confirm", "edit_menu"),
    ("edit_url_delete_confirm", "edit_menu"),
    ("edit_alias_add_input", "edit_aliases"),
    ("edit_alias_add_confirm", "edit_aliases"),
    ("edit_alias_rename_input", "edit_aliases"),
    ("edit_alias_rename_confirm", "edit_aliases"),
    ("edit_alias_delete_confirm", "edit_aliases"),
)

# Ожидаемый маркер текста целевой фазы (кнопка «Назад» рендерит её сразу).
TARGET_MARKERS = {
    "place": "🛡 Место",
    "edit_menu": "Редактирование места",
    "edit_aliases": "Альтернативные названия",
}


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class CancelFlowTableTests(unittest.TestCase):
    """Каждая кнопка adm_cancel из каждой фазы ведёт в допустимую фазу."""

    @scenario_test
    async def test_every_cancel_button_renders_target_phase(self, h):
        place_id = insert_place(h, with_address=True)
        add_alias(h, place_id, "Локал")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            for source, target in CANCEL_TARGETS:
                with self.subTest(source=source):
                    await h.text("/admin")
                    save_admin_session(
                        SUPERADMIN_TG,
                        {
                            "phase": source,
                            "page": 0,
                            "place_id": place_id,
                            "mode": "assign",
                            "query": "кто-то",
                        },
                    )
                    await h.callback("adm_cancel")

                    self.assertNotEqual(
                        last_alert(h),
                        STALE_ALERT,
                        f"{source}: кнопка признана устаревшей",
                    )
                    self.assertNotEqual(last_alert(h), DENIED_ALERT)
                    self.assertIn(
                        TARGET_MARKERS[target],
                        h.screen_text(),
                        f"{source}: ожидалась фаза {target}",
                    )
                    state = load_admin_session(SUPERADMIN_TG)
                    assert state is not None
                    self.assertEqual(state["phase"], target, source)

    @scenario_test
    async def test_cancel_target_phases_are_renderable_states(self, h):
        """Целевые фазы входят в PHASES и не являются stale-only."""
        from handlers.admin import PHASES

        for source, target in CANCEL_TARGETS:
            self.assertIn(source, PHASES, source)
            self.assertIn(target, PHASES, target)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ReplyKeyboardRemoveTests(unittest.TestCase):
    """Дефект №23: «прилипшая» reply-клавиатура снимается на входах в UI."""

    @scenario_test
    async def test_start_drops_legacy_reply_keyboard(self, h):
        await h.text("/start")

        removals = reply_keyboard_removals(h)
        self.assertTrue(removals, "на /start не отправлен ReplyKeyboardRemove")
        self.assertFalse(removals[0]["reply_markup"].selective)
        # Дальше UI живёт на inline-кнопках: главное меню на месте.
        self.assertIn("Выберите действие", h.screen_text())
        labels = [label for label, _data in h.screen_buttons()]
        self.assertIn("📍 Создать для площадки", labels)
        # Служебное сообщение не остаётся в чате.
        self.assertTrue(h.deleted_ids(), "служебное сообщение не удалено")

    @scenario_test
    async def test_repeat_start_does_not_spam_keyboard_removal(self, h):
        await h.text("/start")
        first = len(reply_keyboard_removals(h))
        self.assertEqual(first, 1)
        await h.text("/start")
        self.assertEqual(len(reply_keyboard_removals(h)), first)

    @scenario_test
    async def test_wizard_entry_drops_legacy_reply_keyboard(self, h):
        from tests.test_poster2_menu import _seed_personal_template

        template_id = _seed_personal_template(h)
        await h.callback(f"tp_pick:{template_id}")

        self.assertTrue(
            reply_keyboard_removals(h),
            "старт конструктора не снял легаси reply-клавиатуру",
        )
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertTrue(callbacks(h, "aw_"), "конструктор должен жить на inline")

    @scenario_test
    async def test_place_pick_entry_drops_legacy_reply_keyboard(self, h):
        from tests.test_poster2_menu import _seed_place

        place_id = _seed_place(h)
        await h.callback(f"pl_pick:{place_id}")

        self.assertTrue(reply_keyboard_removals(h))
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_admin_panel_drops_legacy_reply_keyboard(self, h):
        insert_place(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            self.assertTrue(
                reply_keyboard_removals(h),
                "вход в кабинет не снял леги reply-клавиатуру",
            )
            self.assertIn("🛡 Все места", h.screen_text())
            self.assertTrue(h.screen_buttons())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AssignSearchOutcomeTests(unittest.TestCase):
    """Дефект №21: любой ввод в assign_search даёт явный результат."""

    async def _open_search(self, h) -> int:
        place_id = insert_place(h)
        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        await h.callback(f"adm_assign:{place_id}")
        self.assertIn("Поиск получателя", h.screen_text())
        return place_id

    @scenario_test
    async def test_known_username_is_found(self, h):
        ensure_user(h, TARGET_TG, username="a_quilam")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text("@a_quilam")
        self.assertIn("Найдено: 1", h.screen_text())
        self.assertIn("@a_quilam", h.screen_text())
        self.assertTrue(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_username_without_at_is_found(self, h):
        ensure_user(h, TARGET_TG, username="a_quilam")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text("a_quilam")
        self.assertIn("@a_quilam", h.screen_text())
        self.assertTrue(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_unknown_username_explains_start(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text("@a_quilam")
        text = h.screen_text()
        self.assertIn("Пользователь не найден", text)
        self.assertIn("@a_quilam", text)
        self.assertIn("/start", text)
        self.assertFalse(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_known_numeric_id_is_found(self, h):
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text(str(TARGET_TG))
        self.assertIn(str(TARGET_TG), h.screen_text())
        self.assertTrue(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_unknown_numeric_id_explains_start(self, h):
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)
            await h.text(str(UNKNOWN_TG))
        text = h.screen_text()
        self.assertIn("Пользователь не найден", text)
        self.assertIn(str(UNKNOWN_TG), text)
        self.assertIn("/start", text)
        self.assertFalse(callbacks(h, "adm_pick:"))

    @scenario_test
    async def test_garbage_input_reports_format_error_and_allows_retry(self, h):
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await self._open_search(h)

            for garbage in ("!!!", "   ", "@"):
                with self.subTest(garbage=garbage):
                    await h.text(garbage)
                    self.assertIn("Неверный формат", h.screen_text())
                    # Фаза не потеряна: следующий корректный ввод ищет.
                    self.assertIn("Поиск получателя", h.screen_text())
                    state = load_admin_session(SUPERADMIN_TG)
                    assert state is not None
                    self.assertEqual(state["phase"], "assign_search")

            await h.text("@target")
            self.assertIn("Найдено: 1", h.screen_text())

    @scenario_test
    async def test_pick_user_branches_never_silent(self, h):
        place_id = insert_place(h)
        ensure_user(h, TARGET_TG, username="target")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(f"adm_place:{place_id}")
            await h.callback(f"adm_assign:{place_id}")
            await h.text("@target")
            await h.callback(callback(h, "adm_pick:"))
            self.assertIn("Подтвердите назначение", h.screen_text())
            await h.callback("adm_confirm")
        self.assertIn("Администратор назначен", h.screen_text())

        # Устаревший экран выбора: место уже занято — ЯВНЫЙ alert, не тишина.
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
        self.assertIn("уже", (last_alert(h) or "").lower())
        self.assertIn("🛡 Место", h.screen_text())

    @scenario_test
    async def test_pick_user_missing_place_is_explicit(self, h):
        """Место исчезло между поиском и тапом — alert, а не молчание."""
        ensure_user(h, TARGET_TG, username="target")
        save_admin_session(
            SUPERADMIN_TG,
            {
                "phase": "assign_results",
                "page": 0,
                "place_id": 999_999,
                "mode": "assign",
                "query": "@target",
                "query_kind": "username",
            },
        )
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.callback(f"adm_pick:{TARGET_TG}")
        self.assertIn("не существует", (last_alert(h) or "").lower())
