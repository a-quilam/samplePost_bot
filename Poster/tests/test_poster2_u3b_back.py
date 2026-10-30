# tests/test_poster2_u3b_back.py
"""
U3b (Poster 2.1, исправление №9): «Назад» без «внутренней ошибки».

Что закрепляется здесь:

(а) восстановленная фаза `alias_warning` без валидного `pending_alias`
    (старый снимок WIP, частичная запись, рестарт процесса) не бросает
    `PlaceNameError`: показывается карточка места с понятным сообщением;
    защита работает и для пустой строки, и для мусора, и для названия из
    одной пунктуации, при этом валидное название по-прежнему предупреждает
    о похожем месте (защита не «глушит» штатный сценарий);
(б) экран ввода названия нового шаблона (`await_new_name`) — живой экран
    создания: `tp_back` и `tp_hub` возвращают в список шаблонов (в том числе
    после рестарта процесса), а не отвечают «Кнопка устарела»;
(в) матрица возвратов по разделам (места, справочник, выбор площадки,
    кабинет, алиасы, очередь предложений, конфликты кодов, шаблоны,
    кирпичики, предложения, конструктор, рестарт, удаление объекта, смена
    прав, двойное нажатие): ни один шаг не даёт «Произошла внутренняя
    ошибка», не уходит в аварийный fallback и не оставляет callback без
    ответа. Для кнопок, которые экран предлагает сам, дополнительно
    требуется живой переход: «Кнопка устарела» здесь — дефект навигации,
    а не честный отказ (конфликты кодов: задача → список → место; очередь
    предложений и карточка предложения → список);
(г) общий обработчик ошибок: ожидаемая доменная ошибка при рендере
    (наследник `PlaceNameError`/`TemplateError`/`BrickError`/`ProposalError`/
    `AnnouncementError`) даёт понятное сообщение и fallback-экран вместо
    «внутренней ошибки», а неожиданный сбой кода по-прежнему уведомляет
    как сбой — классификация не «глотает» настоящие падения.

Файл чувствителен к правкам U3b (проверено мутациями):

- убрать `PHASE_AWAIT_NAME` из allow-list `handle_back`/`handle_hub`
  (`handlers/templates.py`) → падают тесты класса `AwaitNewNameBackTests`
  («Кнопка устарела» вместо списка шаблонов);
- вернуть исключение в `alias_warning` без `pending_alias`
  (`handlers/places.py`) → падают тесты класса
  `AliasWarningWithoutPendingAliasTests` (вместо карточки места открывается
  главное меню из общего fallback);
- сузить allow-list `handle_open_place` до `{"places"}` или
  `handle_conflicts_open` до `{"place"}`, `handle_conflict_open` до
  `{"conflicts_list"}` (`handlers/admin.py`) → падают тесты класса
  `AdminBackMatrixTests` на цепочке «задача → список → место» и на возврате
  из ввода переименования.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (tests.integration_harness): реальные Telegram, `.env` и
`post_bot.db` не используются.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

try:
    import errors as errors_module
    from errors import domain_notice_text
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        call_text,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_ERROR = str(exc)


SUPERADMIN_TG = 8_810_100
ADMIN_TG = 8_810_200
OUTSIDER_TG = 8_810_300

INTERNAL_FRAGMENT = "Произошла внутренняя ошибка"
STALE_FRAGMENT = "Кнопка устарела"
ALIAS_LOST_FRAGMENT = "Название для добавления не сохранилось"
CREATE_LOST_FRAGMENT = "Создание шаблона не подтверждено"
MENU_FRAGMENT = "Выберите действие"

_MISSING = object()


def patched_superadmin():
    """Супер­администратор только на время блока (живая конфигурация)."""
    return patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,))


# --- наблюдение за шагом навигации -------------------------------------------


def user_texts(h: Harness) -> list[str]:
    """Все тексты, которые увидел пользователь: экраны, ответы, подписи."""
    seen: list[str] = []
    for call in h.calls:
        for value in (call_text(call), call.get("caption")):
            if isinstance(value, str):
                seen.append(value)
    return seen


def internal_errors(h: Harness) -> list[str]:
    return [text for text in user_texts(h) if INTERNAL_FRAGMENT in text]


def domain_fallbacks(h: Harness) -> list[str]:
    return [text for text in user_texts(h) if domain_notice_text() in text]


def mark(h: Harness) -> tuple[int, int, int]:
    """
    Отметка «до шага»: ошибки, fallback'и и число ответов на callback.

    Уведомление об ошибке троттлится 5 секунд на чат, поэтому перед каждым
    шагом троттлинг сбрасывается: иначе вторая ошибка матрицы была бы не
    видна и тест прошёл бы «по привычке».
    """
    errors_module._last_error_notice.clear()
    return len(internal_errors(h)), len(domain_fallbacks(h)), len(h.answers())


class BackChecks(unittest.TestCase):
    """
    Миксин пошаговых проверок навигации.

    Наследуется от `unittest.TestCase`, чтобы обращения к `assert*` были
    типизированы; собственных тестов у класса нет.
    """

    def assert_no_failure(
        self, h: Harness, before: tuple[int, int, int], label: str
    ) -> None:
        """На шаге не появилось ни «внутренней ошибки», ни аварийного fallback."""
        new_internal = internal_errors(h)[before[0] :]
        new_fallback = domain_fallbacks(h)[before[1] :]
        self.assertEqual(
            new_internal, [], f"{label}: «внутренняя ошибка» вместо навигации"
        )
        self.assertEqual(
            new_fallback, [], f"{label}: аварийный fallback вместо нормального экрана"
        )

    async def click(self, h: Harness, data: str) -> None:
        """Нажимает callback и требует ответа на него (спиннер не висит)."""
        before = mark(h)
        await h.callback(data)
        self.assert_no_failure(h, before, data)
        self.assertGreaterEqual(
            len(h.answers()) - before[2], 1, f"{data}: callback остался без ответа"
        )

    async def click_live_back(self, h: Harness, data: str, *, target: str) -> None:
        """
        Живой возврат: без сбоя, без ложного «Кнопка устарела», на целевом экране.

        Отдельно от `click`, потому что «устарела» — валидный ответ только для
        действительно устаревшей кнопки; на экране, который сам её предлагает,
        это дефект навигации.
        """
        before = mark(h)
        n_texts = len(h.answer_texts())
        await h.callback(data)
        self.assert_no_failure(h, before, data)
        alerts = h.answer_texts()[n_texts:]
        stale = [text for text in alerts if STALE_FRAGMENT in text]
        self.assertEqual(stale, [], f"{data}: ложное «Кнопка устарела»")
        self.assertIn(target, h.screen_text(), f"{data}: не тот целевой экран")


# --- подготовка данных -------------------------------------------------------


def seed_place(
    h: Harness,
    *,
    name: str = "Дом культуры",
    admin: bool = True,
) -> int:
    """Место со стандартным шаблоном; по умолчанию с ролью админа у ADMIN_TG."""
    from tests.test_poster2_menu import _seed_place

    return int(_seed_place(h, name=name, actor_tg=ADMIN_TG, admin=admin))


def seed_template(h: Harness, *, name: str = "Личный шаблон") -> int:
    """Личный шаблон владельца ADMIN_TG."""
    from tests.test_poster2_menu import _seed_personal_template

    return int(_seed_personal_template(h, name=name, actor_tg=ADMIN_TG))


def seed_personal_brick(h: Harness, *, code: str = "u3b_brick") -> int:
    """Личный кирпичик ADMIN_TG (источник предложения и владелец карточки)."""
    from brick_store import create_personal_brick
    from user_store import get_or_create_user

    with h.session_factory() as session:
        get_or_create_user(session, ADMIN_TG, username="author")
        brick = create_personal_brick(
            session,
            actor_telegram_user_id=ADMIN_TG,
            code=code,
            title="Кирпичик U3b",
            value_type="short_text",
        )
        session.commit()
        return int(brick.id)


def seed_pending_proposal(
    h: Harness, *, scope: str = "system", place_id: int | None = None
) -> int:
    """Ожидающее предложение кирпичика ADMIN_TG для системы или места."""
    from brick_proposal_store import create_brick_proposal
    from brick_store import create_personal_brick
    from user_store import get_or_create_user

    code = "u3b_offer" if scope == "system" else "u3b_place_offer"
    with h.session_factory() as session:
        get_or_create_user(session, ADMIN_TG, username="author")
        source = create_personal_brick(
            session,
            actor_telegram_user_id=ADMIN_TG,
            code=code,
            title="Предложенный U3b",
            value_type="short_text",
        )
        session.commit()
        proposal = create_brick_proposal(
            session,
            actor_telegram_user_id=ADMIN_TG,
            source_brick_id=int(source.id),
            scope=scope,
            place_id=place_id,
        )
        session.commit()
        return int(proposal.id)


def seed_conflict(h: Harness) -> tuple[int, int]:
    """
    Место + активная задача конфликта кодов.

    У места есть местный носитель кода, глобальное предложение того же кода
    одобрено — как в реальном сценарии R3.
    """
    from brick_proposal_store import (
        approve_brick_proposal,
        create_brick_proposal,
    )
    from brick_store import create_personal_brick, create_place_brick
    from user_store import get_or_create_user

    place_id = seed_place(h, name="Место автора")
    with h.session_factory() as session:
        get_or_create_user(session, SUPERADMIN_TG, username="boss")
        create_place_brick(
            session,
            actor_telegram_user_id=ADMIN_TG,
            place_id=place_id,
            code="u3b_code",
            title="Местный носитель",
            value_type="short_text",
        )
        source = create_personal_brick(
            session,
            actor_telegram_user_id=ADMIN_TG,
            code="u3b_code",
            title="Глобальный носитель",
            value_type="short_text",
        )
        session.commit()
        proposal = create_brick_proposal(
            session,
            actor_telegram_user_id=ADMIN_TG,
            source_brick_id=int(source.id),
            scope="system",
        )
        session.commit()
        proposal_id = int(proposal.id)

    with patched_superadmin(), h.session_factory() as session:
        result = approve_brick_proposal(
            session,
            actor_telegram_user_id=SUPERADMIN_TG,
            proposal_id=proposal_id,
        )
        session.commit()
        assert result.conflicts, "ожидалась активная задача конфликта кодов"
        return place_id, int(result.conflicts[0].id)


def _state(h: Harness, module: str, loader: str, user_id: int) -> dict | None:
    """Текущее сохранённое состояние сценария (None — сессии нет)."""
    import importlib

    return getattr(importlib.import_module(module), loader)(user_id)


# --- (а) alias_warning без валидного pending_alias ---------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AliasWarningWithoutPendingAliasTests(BackChecks):
    """(а) восстановленный `alias_warning` без названия — карточка, не сбой."""

    def _restore(self, place_id: int, pending: Any = _MISSING) -> None:
        from place_state import save_place_session

        payload: dict[str, Any] = {
            "phase": "alias_warning",
            "place_id": place_id,
            "alias_origin": "names",
        }
        if pending is not _MISSING:
            payload["pending_alias"] = pending
        save_place_session(ADMIN_TG, payload)

    def _assert_card_instead_of_failure(self, h: Harness, label: str) -> None:
        from place_state import load_place_session

        self.assertEqual(internal_errors(h), [], f"{label}: внутренняя ошибка")
        screen = h.screen_text()
        self.assertIn(ALIAS_LOST_FRAGMENT, screen, label)
        self.assertIn("📍 Дом культуры", screen, label)
        self.assertNotIn("Похожее название у другого места", screen, label)
        labels = [text for text, _data in h.screen_buttons()]
        self.assertIn("🔤 Выбрать альтернативное название", labels, label)
        self.assertIn("◀️ Вернуться к списку", labels, label)
        state = load_place_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "confirm", label)
        self.assertNotIn("pending_alias", state, label)

    @scenario_test
    async def test_restored_phase_without_name_opens_place_card(self, h):
        """В снимке нет `pending_alias`: `find_name_conflicts` не вызывается."""
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            self._restore(place_id)
            before = mark(h)
            await h.text("/places")
            self.assert_no_failure(h, before, "alias_warning без pending_alias")
            self._assert_card_instead_of_failure(h, "alias_warning без pending_alias")

    @scenario_test
    async def test_survives_process_restart(self, h):
        """Фаза восстановлена из БД после рестарта — поведение то же."""
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            self._restore(place_id)
            await h.restart()
            before = mark(h)
            await h.text("/places")
            self.assert_no_failure(h, before, "alias_warning после рестарта")
            self._assert_card_instead_of_failure(h, "alias_warning после рестарта")

    @scenario_test
    async def test_invalid_name_values_open_place_card(self, h):
        """Пустое, «мусорное» и нестроковое название — тот же безопасный исход."""
        place_id = seed_place(h)
        cases: tuple[tuple[str, Any], ...] = (
            ("пустая строка", ""),
            ("только пробелы", "   "),
            ("только пунктуация", "«»"),
            ("не строка", 42),
            ("None", None),
        )
        with patched_superadmin():
            h.user_id = ADMIN_TG
            for label, pending in cases:
                with self.subTest(pending=label):
                    self._restore(place_id, pending)
                    before = mark(h)
                    await h.text("/places")
                    self.assert_no_failure(h, before, label)
                    self._assert_card_instead_of_failure(h, label)

    @scenario_test
    async def test_valid_name_still_warns_about_conflict(self, h):
        """Положительный контроль: защита не отменяет штатное предупреждение."""
        from place_state import load_place_session

        place_id = seed_place(h, name="Дом культуры")
        seed_place(h, name="Дом культуры имени Ивана", admin=False)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            self._restore(place_id, "Дом культуры им. Ивана")
            before = mark(h)
            await h.text("/places")
            self.assert_no_failure(h, before, "alias_warning с валидным названием")
            screen = h.screen_text()
            self.assertIn("Похожее название у другого места", screen)
            self.assertIn("Дом культуры имени Ивана", screen)
            self.assertNotIn(ALIAS_LOST_FRAGMENT, screen)
            labels = [text for text, _data in h.screen_buttons()]
            self.assertIn("✅ Да, добавить", labels)
            self.assertIn("❌ Нет, отмена", labels)
            state = load_place_session(ADMIN_TG)
            assert state is not None
            self.assertEqual(state["phase"], "alias_warning")


# --- (б) await_new_name: tp_back и tp_hub ------------------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AwaitNewNameBackTests(BackChecks):
    """(б) экран ввода названия шаблона — живой экран создания."""

    async def _open_create(self, h: Harness) -> None:
        await h.text("/templates")
        await self.click(h, "tp_create")
        state = _state(h, "template_state", "load_template_session", ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "await_new_name")

    def _assert_hub(self, h: Harness, label: str) -> None:
        from template_state import load_template_session

        self.assertIn("📄 Мои шаблоны", h.screen_text(), label)
        self.assertNotIn(STALE_FRAGMENT, " ".join(h.answer_texts()), label)
        state = load_template_session(ADMIN_TG)
        assert state is not None
        self.assertEqual(state["phase"], "hub", label)

    @scenario_test
    async def test_back_from_name_input_returns_to_templates(self, h):
        seed_template(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await self._open_create(h)
            before = mark(h)
            await h.callback("tp_back")
            self.assert_no_failure(h, before, "await_new_name → tp_back")
            self.assertGreaterEqual(len(h.answers()) - before[2], 1)
            self.assertIn(CREATE_LOST_FRAGMENT, h.screen_text())
            self._assert_hub(h, "await_new_name → tp_back")
            # Создание не подтверждено: шаблонов по-прежнему один.
            self.assertIn("Всего: 1", h.screen_text())

    @scenario_test
    async def test_hub_button_from_name_input_returns_to_templates(self, h):
        seed_template(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await self._open_create(h)
            before = mark(h)
            await h.callback("tp_hub")
            self.assert_no_failure(h, before, "await_new_name → tp_hub")
            self.assertGreaterEqual(len(h.answers()) - before[2], 1)
            self._assert_hub(h, "await_new_name → tp_hub")

    @scenario_test
    async def test_back_after_restart_returns_to_templates(self, h):
        """Фаза `await_new_name` переживает рестарт — кнопки остаются рабочими."""
        seed_template(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await self._open_create(h)
            await h.restart()
            before = mark(h)
            await h.callback("tp_back")
            self.assert_no_failure(h, before, "await_new_name → tp_back после рестарта")
            self.assertGreaterEqual(len(h.answers()) - before[2], 1)
            self.assertIn(CREATE_LOST_FRAGMENT, h.screen_text())
            self._assert_hub(h, "await_new_name → tp_back после рестарта")


# --- (в) матрица возвратов ---------------------------------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class PlacesBackMatrixTests(BackChecks):
    """(в) раздел «Места», справочник площадок и выбор для анонса."""

    @scenario_test
    async def test_section_and_manage_backs(self, h):
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/places")
            for data in (
                "pl_all",
                f"pl_open:{place_id}",
                "pl_names:0",
                "pl_back",
                "pl_back",
                "pl_hub",
            ):
                await self.click(h, data)

            await h.text("/start")
            await self.click(h, "manage_places")
            for data in (
                f"plm_open:{place_id}",
                "plm_back",
                "plm_add",
                "plm_back",
                "plm_back",
            ):
                await self.click(h, data)

    @scenario_test
    async def test_pick_backs(self, h):
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/start")
            for data in (
                "pick_place",
                "pl_pick_search",
                "pl_pick_back",
                "pl_pick_add",
                "pl_back",
                f"pl_pick:{place_id}",
            ):
                await self.click(h, data)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AdminBackMatrixTests(BackChecks):
    """
    (в) кабинет: карточка места, редактирование, алиасы, очередь, конфликты.

    Возвраты внутрь кабинета проверяются как живые (`click_live_back`): экран
    сам предлагает эту кнопку, поэтому «Кнопка устарела» на ней — дефект
    навигации административных возвратов (исправление №9).
    """

    @scenario_test
    async def test_place_card_edit_and_alias_backs(self, h):
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            for data in (
                f"adm_place:{place_id}",
                f"adm_edit_place:{place_id}",
                "adm_cancel",
                f"adm_aliases:{place_id}",
                "adm_cancel",
                f"adm_edit_name:{place_id}",
                "adm_cancel",
                "adm_cancel",
                "adm_places",
            ):
                await self.click(h, data)

    @scenario_test
    async def test_place_proposals_queue_backs(self, h):
        place_id = seed_place(h)
        seed_pending_proposal(h, scope="place", place_id=place_id)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            await self.click(h, f"adm_place:{place_id}")
            await self.click_live_back(
                h, f"adm_bprop_place:{place_id}", target="🧱 Предложения кирпичиков"
            )
            await self.click_live_back(h, "adm_bprop_back", target="🛡 Место")
            # ADMIN_TG — администратор места, но не суперадмин: список мест
            # называется «Мои места».
            await self.click_live_back(h, "adm_places", target="🛡 Мои места")

    @scenario_test
    async def test_conflict_card_list_place_chain(self, h):
        """
        U3b+: «К конфликтам» → «К месту» — живые возвраты, не «устарело».

        Раньше `adm_bconflicts:<id>` принимался только из фазы `place`, а
        `adm_place:<id>` — только из `places`: обе кнопки собственного экрана
        отвечали «Кнопка устарела», хотя состояние кабинета было актуальным.
        """
        place_id, conflict_id = seed_conflict(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            await self.click(h, f"adm_place:{place_id}")
            await self.click_live_back(
                h, f"adm_bconflicts:{place_id}", target="⚔️ Конфликты кодов"
            )
            await self.click_live_back(
                h, f"adm_bconflict:{conflict_id}", target="Задача разрешения конфликта"
            )
            await self.click_live_back(
                h, f"adm_bconflicts:{place_id}", target="⚔️ Конфликты кодов"
            )
            await self.click_live_back(h, f"adm_place:{place_id}", target="🛡 Место")
            # Повторный вход: «К конфликтам» работает и со второго раза.
            await self.click_live_back(
                h, f"adm_bconflicts:{place_id}", target="⚔️ Конфликты кодов"
            )
            await self.click_live_back(h, f"adm_place:{place_id}", target="🛡 Место")
            state = _state(h, "admin_state", "load_admin_session", ADMIN_TG)
            assert state is not None
            self.assertEqual(state["phase"], "place")
            await self.click_live_back(h, "main_menu", target=MENU_FRAGMENT)

    @scenario_test
    async def test_conflict_rename_and_switch_backs(self, h):
        """«Назад» из ввода переименования и из подтверждения переключения."""
        place_id, conflict_id = seed_conflict(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            await self.click(h, f"adm_place:{place_id}")
            await self.click(h, f"adm_bconflicts:{place_id}")
            await self.click(h, f"adm_bconflict:{conflict_id}")
            await self.click_live_back(
                h,
                f"adm_bconflict_rename:{conflict_id}",
                target="Переименование местного кирпичика",
            )
            await self.click_live_back(
                h, f"adm_bconflict:{conflict_id}", target="Задача разрешения конфликта"
            )
            await self.click_live_back(
                h,
                f"adm_bconflict_switch:{conflict_id}",
                target="Переключиться на глобальный кирпичик?",
            )
            await self.click_live_back(
                h, f"adm_bconflict:{conflict_id}", target="Задача разрешения конфликта"
            )

    @scenario_test
    async def test_conflicts_backs_are_denied_for_outsider(self, h):
        """Возвраты не расширяют доступ: чужой получает отказ, а не экран."""
        place_id, conflict_id = seed_conflict(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            await self.click(h, f"adm_place:{place_id}")
            await self.click(h, f"adm_bconflicts:{place_id}")
            await self.click(h, f"adm_bconflict:{conflict_id}")

            # Чужой участник: кабинет закрыт, состояние администратора пустое.
            h.user_id = OUTSIDER_TG
            await h.text("/admin")
            self.assertIn("Раздел доступен только администраторам", h.screen_text())
            for data in ("adm_places", f"adm_bconflicts:{place_id}"):
                with self.subTest(data=data):
                    await self.click(h, data)
                    self.assertNotIn("Задача разрешения конфликта", h.screen_text())
                    self.assertNotIn("⚔️ Конфликты кодов", h.screen_text())
                    self.assertIn(
                        "Раздел доступен только администраторам", h.screen_text()
                    )
                    self.assertIsNone(
                        _state(h, "admin_state", "load_admin_session", OUTSIDER_TG)
                    )

    @scenario_test
    async def test_global_moderation_queue_backs(self, h):
        proposal_id = seed_pending_proposal(h)
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await self.click(h, "adm_bprop_global")
            await self.click_live_back(
                h, f"adm_bprop_open:{proposal_id}", target="Предложение кирпичика"
            )
            await self.click_live_back(
                h, "adm_bprop_back", target="🌍 Глобальные предложения"
            )
            await self.click(h, f"adm_bprop_open:{proposal_id}")
            await self.click(h, f"adm_bprop_approve:{proposal_id}")
            await self.click_live_back(
                h, "adm_bprop_back", target="Предложение кирпичика"
            )
            await self.click(h, f"adm_bprop_approve:{proposal_id}")
            await self.click(h, f"adm_bprop_approve_yes:{proposal_id}")
            await self.click_live_back(
                h, "adm_bprop_back", target="🌍 Глобальные предложения"
            )
            await self.click_live_back(h, "adm_bprop_back", target="🛡 Все места")

    @scenario_test
    async def test_places_queue_from_admin_list(self, h):
        seed_place(h)
        seed_pending_proposal(h, scope="system")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await self.click(h, "adm_bprop_places")
            await self.click_live_back(h, "adm_bprop_back", target="🛡 Все места")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class TemplatesBricksBackMatrixTests(BackChecks):
    """(в) шаблоны, кирпичики и предложения кирпичиков."""

    @scenario_test
    async def test_template_backs(self, h):
        template_id = seed_template(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/templates")
            for data in (
                f"tp_open:{template_id}",
                f"tp_edit_b:{template_id}",
                f"tp_bricks:{template_id}",
                "tp_back",
                f"tp_help:b:{template_id}",
                "tp_back",
                f"tp_open:{template_id}",
                "tp_hub",
                "tp_create",
                "tp_back",
                "tp_create",
                "tp_hub",
            ):
                await self.click(h, data)

    @scenario_test
    async def test_brick_and_proposal_backs(self, h):
        brick_id = seed_personal_brick(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/bricks")
            for data in (
                f"bk_open:{brick_id}",
                "bk_edit",
                "bk_back",
                "bk_back",
                "bk_create",
                "bk_back",
                f"bp_start:{brick_id}",
                "bp_back",
            ):
                await self.click(h, data)

            await self.click(h, f"bp_start:{brick_id}")
            await self.click(h, "bp_target:system")
            await self.click(h, "bp_back")
            await self.click(h, "bp_back")

            await self.click(h, f"bp_start:{brick_id}")
            await self.click(h, "bp_target:system")
            await self.click(h, "bp_send")
            await self.click(h, "bp_back")

            opens = [
                data
                for _label, data in h.screen_buttons()
                if data is not None and data.startswith("bp_open:")
            ]
            self.assertTrue(opens, "в «Мои предложения» нет карточки предложения")
            await self.click(h, opens[0])
            await self.click(h, "bp_back")
            await self.click(h, "bp_back")


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class WizardBackMatrixTests(BackChecks):
    """(в) конструктор анонса: возвраты вопросов и выход в черновики."""

    @scenario_test
    async def test_question_backs(self, h):
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/start")
            await self.click(h, "pick_place")
            await self.click(h, f"pl_pick:{place_id}")
            backs = [
                data
                for _label, data in h.screen_buttons()
                if data is not None and data.startswith("aw_qback:")
            ]
            self.assertTrue(backs, "на экране вопроса нет «Назад»")
            await self.click(h, backs[0])
            await self.click(h, "aw_home")

    @scenario_test
    async def test_stale_question_button_after_restart_is_explained(self, h):
        """Старая кнопка после рестарта: понятный алерт и живой маршрут."""
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/start")
            await self.click(h, "pick_place")
            await self.click(h, f"pl_pick:{place_id}")
            backs = [
                data
                for _label, data in h.screen_buttons()
                if data is not None and data.startswith("aw_qback:")
            ]
            self.assertTrue(backs)
            await h.restart()

            before = mark(h)
            await h.callback(backs[0])
            self.assert_no_failure(h, before, "aw_qback после рестарта")
            self.assertGreaterEqual(len(h.answers()) - before[2], 1)
            alert = h.answer_texts()[-1]
            self.assertIn("после перезапуска", alert)

            await self.click(h, "main_menu")
            self.assertIn(MENU_FRAGMENT, h.screen_text())
            await self.click(h, "my_announcements")
            opens = [
                data
                for _label, data in h.screen_buttons()
                if data is not None and data.startswith("aw_open:")
            ]
            self.assertTrue(opens, "после рестарта черновик не открывается из списка")
            await self.click(h, opens[0])
            self.assertIn("Вопрос 1", h.screen_text())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class StaleScreenMatrixTests(BackChecks):
    """(в) удаление объекта, смена прав, двойное нажатие."""

    @scenario_test
    async def test_back_after_place_deleted(self, h):
        from models import Place

        seed_place(h, name="Дом культуры")
        other_id = seed_place(h, name="Дом культуры имени Ивана", admin=False)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/places")
            await self.click(h, "pl_all")
            await self.click(h, f"pl_open:{other_id}")
            await self.click(h, "pl_names:0")
            with h.session_factory() as session:
                session.delete(session.get(Place, other_id))
                session.commit()
            before = mark(h)
            await h.callback("pl_back")
            self.assert_no_failure(h, before, "pl_back после удаления места")
            self.assertIn("Место не существует или было удалено", h.screen_text())
            await self.click(h, "pl_hub")
            self.assertIn("📍 Места", h.screen_text())

    @scenario_test
    async def test_back_after_rights_revoked(self, h):
        from models import PlaceAdmin, utcnow

        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/admin")
            await self.click(h, f"adm_place:{place_id}")
            await self.click(h, f"adm_edit_place:{place_id}")
            with h.session_factory() as session:
                row = (
                    session.query(PlaceAdmin)
                    .filter(PlaceAdmin.place_id == place_id)
                    .one()
                )
                row.revoked_at = utcnow()
                session.commit()
            before = mark(h)
            await h.callback("adm_cancel")
            self.assert_no_failure(h, before, "adm_cancel после отзыва прав")
            self.assertIn("Раздел доступен только администраторам", h.screen_text())
            await self.click(h, "main_menu")
            self.assertIn(MENU_FRAGMENT, h.screen_text())

    @scenario_test
    async def test_double_callback_answers_twice_without_failure(self, h):
        place_id = seed_place(h)
        with patched_superadmin():
            h.user_id = ADMIN_TG
            await h.text("/places")
            before = mark(h)
            await h.callback("pl_all")
            await h.callback("pl_all")
            self.assert_no_failure(h, before, "двойной pl_all")
            self.assertGreaterEqual(len(h.answers()) - before[2], 2)

            await h.text("/templates")
            before = mark(h)
            await h.callback("tp_hub")
            await h.callback("tp_hub")
            self.assert_no_failure(h, before, "двойной tp_hub")
            self.assertGreaterEqual(len(h.answers()) - before[2], 2)

            await h.text("/admin")
            before = mark(h)
            await h.callback(f"adm_place:{place_id}")
            await h.callback(f"adm_place:{place_id}")
            self.assert_no_failure(h, before, "двойной adm_place")
            self.assertGreaterEqual(len(h.answers()) - before[2], 2)


# --- (г) общий fallback доменных ошибок --------------------------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class DomainErrorFallbackTests(BackChecks):
    """(г) доменная ошибка — понятный fallback; сбой кода — прежний алерт."""

    @scenario_test
    async def test_expected_domain_error_opens_menu_with_notice(self, h):
        import handlers.places as places_module
        from place_store import PlaceNameError

        seed_place(h)

        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise PlaceNameError("Название места не может быть пустым")

        with (
            patched_superadmin(),
            patch.object(places_module, "_all_places_content", broken),
            patch.object(errors_module.logger, "warning") as warning,
        ):
            h.user_id = ADMIN_TG
            await h.text("/places")
            await h.callback("pl_all")

        self.assertEqual(internal_errors(h), [], "доменная ошибка показана как сбой")
        self.assertIn(domain_notice_text(), h.answer_texts())
        self.assertIn(MENU_FRAGMENT, h.screen_text())
        warning.assert_called()

        # Меню сбрасывает сломанную сессию: дальше раздел снова открывается.
        with patched_superadmin():
            before = mark(h)
            await h.text("/places")
            await h.callback("pl_all")
            self.assert_no_failure(h, before, "повторный вход в «Места» после fallback")
        self.assertIn("📚 Все места", h.screen_text())

    @scenario_test
    async def test_unexpected_error_is_still_reported_as_failure(self, h):
        import handlers.places as places_module

        seed_place(h)

        def broken(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("сбой кода")

        with (
            patched_superadmin(),
            patch.object(places_module, "_all_places_content", broken),
            patch.object(errors_module.logger, "error") as error,
        ):
            h.user_id = ADMIN_TG
            await h.text("/places")
            await h.callback("pl_all")

        self.assertIn(INTERNAL_FRAGMENT, "\n".join(user_texts(h)))
        self.assertNotIn(domain_notice_text(), user_texts(h))
        error.assert_called()
