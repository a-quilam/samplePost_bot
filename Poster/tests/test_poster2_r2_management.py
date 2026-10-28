# tests/test_poster2_r2_management.py
"""
Poster 2.1 R2b: «Управление → Шаблоны» (дефект №25) и «Управление → Площадки»
(дефект №26).

Проверяется через настоящее приложение `bot.build_application`, FakeBot и
временную SQLite (tests.integration_harness): реальный Telegram, `.env` и
`post_bot.db` не используются.

Покрытие №25:
- вход «Управление → Шаблоны» открывает список управления и не возобновляет
  сохранённую фазу выбора шаблона для анонса;
- тап по шаблону в управлении открывает карточку, а не конструктор; кнопок
  старта анонса нет ни в списке, ни в карточке;
- регресс: вход «📝 Создать по личному шаблону» по-прежнему сразу начинает
  анонс;
- «♻️ Восстановить стандарт» работает из карточки управления;
- удаление личного шаблона без истории проходит; при наличии снимка —
  понятный отказ, данные не теряются; у стандарта места кнопки удаления нет.

Покрытие №26:
- «Управление → Площадки» открывает справочник, а не выбор места для анонса;
- карточка места показывает название, число альтернатив, адрес и ссылку;
- действия зависят от актуальных прав (админ места, суперадмин, не-админ);
- IDOR: чужое место не открывает редактор даже подделанной кнопкой;
- устаревшая/чужая кнопка справочника получает stale-ответ и ничего не меняет;
- создание места из справочника выдаёт стандартный шаблон и не запускает
  анонс;
- навигация справочника: «◀️ К карточке» с заявки на админа и «◀️ Назад» с
  ввода нового места возвращают на предыдущий экран без «Кнопка устарела»;
- режим справочника не протекает в обычный раздел «Места»: там «◀️ Назад»
  отправляет `pl_back`, а не `plm_back`;
- регресс: тап по площадке в режиме выбора сразу начинает анонс.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

try:
    from admin_state import load_admin_session
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        AnnouncementDraft,
        Place,
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        User,
        utcnow,
    )
    from place_state import load_place_session
    from place_store import add_alternative_name, create_place
    from template_state import load_template_session
    from template_store import (
        create_personal_template,
        get_personal_template,
        list_personal_templates,
    )
    from template_types import BUILTIN_TEMPLATE
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)

OTHER_TG = 8_910_002
SUPERADMIN_TG = 8_910_003

# Фаза вопросов конструктора: анонс действительно начат (см. test_poster2_menu).
AW_QUESTIONS_PHASE = 2


# --- Помощники ---------------------------------------------------------------


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
) -> int:
    """Место со стандартным шаблоном; `admin=True` выдаёт роль админа места."""
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            name,
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        if admin:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return int(place.id)


def _add_alias(h, place_id: int, name: str, actor_tg: int = DEFAULT_USER_ID) -> None:
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        add_alternative_name(session, place_id, name, created_by_user_id=user.id)
        session.commit()


def _seed_personal_template(
    h,
    *,
    name: str = "Личный шаблон",
    actor_tg: int = DEFAULT_USER_ID,
    body: str = "[название]",
) -> int:
    """Личный шаблон без снимков и анонсов — его можно удалить."""
    with h.session_factory() as session:
        get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            header="",
            body=body,
            footer="",
        )
        session.commit()
        return int(template.template_id)


def _standard_template_id(h, place_id: int) -> int:
    with h.session_factory() as session:
        template = session.scalar(
            select(Template).where(
                Template.place_id == place_id,
                Template.kind == "standard",
            )
        )
        if template is None:
            raise AssertionError(f"у места {place_id} нет стандартного шаблона")
        return int(template.id)


def _latest_draft(h, telegram_user_id: int = DEFAULT_USER_ID):
    with h.session_factory() as session:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        if user is None:
            return None
        return session.scalar(
            select(AnnouncementDraft)
            .where(AnnouncementDraft.author_user_id == user.id)
            .order_by(AnnouncementDraft.id.desc())
        )


def _draft_count(h) -> int:
    with h.session_factory() as session:
        return len(list(session.scalars(select(AnnouncementDraft)).all()))


def _callbacks(h, prefix: str) -> list[str]:
    return [
        data
        for _label, data in h.screen_buttons()
        if data is not None and data.startswith(prefix)
    ]


def _callback(h, prefix: str) -> str:
    found = _callbacks(h, prefix)
    if not found:
        raise AssertionError(f"Нет callback {prefix!r}; есть: {h.screen_buttons()}")
    return found[0]


def _last_alert(h) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = record.get("args") or ()
    return str(args[0]) if args else str(record.get("text") or "")


def _personal_templates(h, telegram_user_id: int = DEFAULT_USER_ID) -> list:
    with h.session_factory() as session:
        return list_personal_templates(session, telegram_user_id)


def _personal_row(h, template_id: int):
    """Личный шаблон владельца по id (None — удалён или недоступен)."""
    with h.session_factory() as session:
        return get_personal_template(session, DEFAULT_USER_ID, template_id)


def _snapshot_count(h, template_id: int) -> int:
    with h.session_factory() as session:
        return len(
            session.scalars(
                select(TemplateSnapshot.id).where(
                    TemplateSnapshot.source_template_id == template_id
                )
            ).all()
        )


# --- №25: «Управление → Шаблоны» ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateManagementTests(unittest.TestCase):
    """Дефект №25: управление шаблонами не запускает создание анонса."""

    @scenario_test
    async def test_manage_templates_opens_management_not_pick(self, h):
        _seed_personal_template(h, name="Мой шаблон")
        # Сначала открыт вход «создать по личному шаблону» (фаза pick).
        await h.text("/start")
        await h.callback("pick_template")
        self.assertIn("Создать по личному шаблону", h.screen_text())

        # «Управление → Шаблоны» не возобновляет pick: это список управления.
        await h.callback("manage_templates")
        self.assertIn("Мои шаблоны", h.screen_text())
        self.assertNotIn("Создать по личному шаблону", h.screen_text())
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub")

    @scenario_test
    async def test_tap_template_in_management_opens_card_without_start(self, h):
        template_id = _seed_personal_template(h, name="Карточка")
        await h.text("/start")
        await h.callback("manage_templates")

        await h.callback(f"tp_open:{template_id}")
        text = h.screen_text()
        self.assertIn("Карточка", text)
        self.assertIn("Личный шаблон", text)
        # Ни карточка, ни список не предлагают старт анонса.
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertEqual(_callbacks(h, "pl_pick:"), [])
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h, "tp_delete:"))
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0, "просмотр шаблона не создаёт анонс")

    @scenario_test
    async def test_pick_template_still_starts_announcement(self, h):
        template_id = _seed_personal_template(h, name="Для анонса")
        await h.text("/start")
        await h.callback("pick_template")
        self.assertIn(f"tp_pick:{template_id}", _callbacks(h, "tp_pick:"))

        await h.callback(f"tp_pick:{template_id}")
        self.assertIsNotNone(_latest_draft(h))
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_PHASE)

    @scenario_test
    async def test_restore_standard_from_manage_card(self, h):
        template_id = _seed_personal_template(
            h, name="Свой шаблон", body="Своё тело без кирпичиков"
        )
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        await h.callback(f"tp_reset:{template_id}")
        self.assertIn("Восстановить стандарт?", h.screen_text())
        await h.callback(f"tp_reset_yes:{template_id}")

        rows = _personal_templates(h)
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0].revision, 2)
        current = rows[0].current_version
        self.assertEqual(current.header, BUILTIN_TEMPLATE.header)
        self.assertEqual(current.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(current.footer, BUILTIN_TEMPLATE.footer)
        # Пользователь остался в карточке того же шаблона.
        self.assertIn("Свой шаблон", h.screen_text())

    @scenario_test
    async def test_delete_personal_template_without_history(self, h):
        template_id = _seed_personal_template(h, name="Удаляемый")
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Удалить шаблон?", h.screen_text())
        await h.callback(f"tp_delete_yes:{template_id}")

        self.assertIn("Шаблон удалён", h.screen_text())
        self.assertIn("Мои шаблоны", h.screen_text())
        self.assertIsNone(_personal_row(h, template_id))
        self.assertEqual(_personal_templates(h), [])

    @scenario_test
    async def test_delete_blocked_by_snapshot_keeps_data(self, h):
        # Стартовый шаблон `/start` сразу имеет снимок-метку выдачи.
        await h.text("/start")
        rows = _personal_templates(h)
        self.assertEqual(len(rows), 1)
        template_id = rows[0].template_id
        self.assertGreaterEqual(_snapshot_count(h, template_id), 1)

        await h.callback("manage")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Удалить шаблон?", h.screen_text())
        await h.callback(f"tp_delete_yes:{template_id}")

        self.assertIn("нельзя удалить", h.screen_text())
        self.assertIsNotNone(_personal_row(h, template_id), "шаблон сохранён")
        self.assertEqual(_snapshot_count(h, template_id), 1)
        # Пользователь остался в карточке и может продолжить работу.
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h, "tp_delete:"))

    @scenario_test
    async def test_standard_template_has_no_delete_button(self, h):
        place_id = _seed_place(h, name="Стандарт", admin=True)
        template_id = _standard_template_id(h, place_id)
        await h.text("/start")
        await h.callback(f"tp_std:{place_id}")

        self.assertIn("Стандартный шаблон места", h.screen_text())
        self.assertIn("Стандарт", h.screen_text())
        self.assertEqual(_callbacks(h, "tp_delete:"), [])
        self.assertIn(f"tp_reset:{template_id}", _callbacks(h, "tp_reset:"))
        # Карточка стандарта тоже не предлагает начать анонс.
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertEqual(_callbacks(h, "pl_pick:"), [])
        # Возврат ведёт в кабинет места, а не в список личных шаблонов.
        self.assertIn(f"tp_back_place:{place_id}", _callbacks(h, "tp_back_place:"))
        # Подделанное удаление стандарта отклоняется и ничего не ломает.
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Стандартный шаблон удалить нельзя", _last_alert(h))
        self.assertIsNotNone(_standard_template_id(h, place_id))


# --- №26: «Управление → Площадки» -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceManagementTests(unittest.TestCase):
    """Дефект №26: справочник площадок с карточкой и действиями по правам."""

    @scenario_test
    async def test_manage_places_opens_handbook_not_pick(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_places")

        text = h.screen_text()
        self.assertIn("📍 Площадки", text)
        self.assertIn("Дом культуры", text)
        self.assertIn(f"plm_open:{place_id}", _callbacks(h, "plm_open:"))
        # Это справочник: ни старта анонса, ни выбора места для поста.
        self.assertEqual(_callbacks(h, "pl_pick:"), [])
        self.assertNotIn("Вопрос 1", text)
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")

    @scenario_test
    async def test_card_shows_details_and_admin_actions(self, h):
        place_id = _seed_place(h, name="Дом культуры", admin=True)
        _add_alias(h, place_id, "ДК")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        text = h.screen_text()
        self.assertIn("Дом культуры", text)
        self.assertIn("Альтернативные названия: 1", text)
        self.assertIn("Адрес: ул. Тестовая, 1", text)
        self.assertIn("https://example.com/place", text)
        self.assertIn(f"plm_edit:{place_id}", _callbacks(h, "plm_edit:"))
        # Админу места заявка не нужна, а назначение чужих админов — не его.
        self.assertEqual(_callbacks(h, "plm_admin_request:"), [])
        self.assertEqual(_callbacks(h, "plm_admins:"), [])

    @scenario_test
    async def test_non_admin_sees_view_and_request_only(self, h):
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        text = h.screen_text()
        self.assertIn("Чужое место", text)
        self.assertIn("просмотр справочника", text)
        self.assertEqual(_callbacks(h, "plm_edit:"), [])
        self.assertIn(
            f"plm_admin_request:{place_id}", _callbacks(h, "plm_admin_request:")
        )
        # Кнопка заявки только объясняет: роль не меняется.
        await h.callback(f"plm_admin_request:{place_id}")
        self.assertIn("Я админ места", h.screen_text())
        self.assertIn("не выдаёт полномочия", h.screen_text())

    @scenario_test
    async def test_superadmin_sees_admins_button(self, h):
        place_id = _seed_place(h, name="Дом культуры", actor_tg=OTHER_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback("manage_places", user_id=SUPERADMIN_TG)
            await h.callback(f"plm_open:{place_id}", user_id=SUPERADMIN_TG)

            self.assertIn("Дом культуры", h.screen_text())
            self.assertIn(f"plm_admins:{place_id}", _callbacks(h, "plm_admins:"))
            self.assertIn(f"plm_edit:{place_id}", _callbacks(h, "plm_edit:"))

            # «👥 Администраторы» открывает существующий кабинет места.
            await h.callback(f"plm_admins:{place_id}", user_id=SUPERADMIN_TG)
            admin_state = load_admin_session(SUPERADMIN_TG)
            self.assertIsNotNone(admin_state)
            assert admin_state is not None
            self.assertEqual(admin_state["phase"], "place")
            self.assertEqual(admin_state["place_id"], place_id)
            self.assertIn("🛡 Место", h.screen_text())

    @scenario_test
    async def test_non_admin_cannot_edit_foreign_place(self, h):
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        # Подделанная кнопка: id совпадает с карточкой, но прав нет.
        await h.callback(f"plm_edit:{place_id}")
        self.assertIn("Редактирование доступно администратору места", _last_alert(h))
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))
        # Место и его стандартный шаблон не тронуты, справочник не сброшен.
        with h.session_factory() as session:
            templates = session.scalars(
                select(Template).where(Template.place_id == place_id)
            ).all()
        self.assertEqual(len(templates), 1)
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")

    @scenario_test
    async def test_stale_manage_callbacks_are_answered(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        # Без открытого справочника (сохранённого состояния нет) кнопки
        # устарели: данные не меняются и спиннер не висит.
        await h.callback(f"plm_open:{place_id}")
        self.assertIn("устарела", _last_alert(h).lower())
        await h.callback(f"plm_edit:{place_id}")
        self.assertIn("устарела", _last_alert(h).lower())
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

        # Кнопка списка в фазе карточки тоже устарела: повторный `plm_open`
        # не должен перерисовывать карточку из чужой фазы.
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"plm_open:{place_id}")
        self.assertIn("устарела", _last_alert(h).lower())
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")

    @scenario_test
    async def test_open_deleted_place_returns_to_list(self, h):
        place_id = _seed_place(h, name="Удалённое место")
        await h.text("/start")
        await h.callback("manage_places")
        with h.session_factory() as session:
            place = session.get(Place, place_id)
            assert place is not None
            session.delete(place)
            session.commit()
        # Карточка несуществующего места не открывается, сессия — список.
        await h.callback(f"plm_open:{place_id}")
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

    @scenario_test
    async def test_add_place_from_handbook_creates_standard_template(self, h):
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        self.assertIn("Анонс не запускается", h.screen_text())

        await h.text("Новая площадка R2b")
        await h.callback("pl_create_confirm")

        text = h.screen_text()
        self.assertIn("Новая площадка R2b", text)
        self.assertIn("✅ Место добавлено", text)
        with h.session_factory() as session:
            new_place = session.scalar(select(Place).order_by(Place.id.desc()))
            self.assertIsNotNone(new_place)
            assert new_place is not None
            standard = session.scalar(
                select(Template).where(
                    Template.place_id == new_place.id,
                    Template.kind == "standard",
                )
            )
        self.assertIsNotNone(standard, "у нового места есть стандартный шаблон")
        self.assertEqual(
            _draft_count(h), 0, "создание из справочника не начинает анонс"
        )
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")

    @scenario_test
    async def test_back_from_admin_request_returns_to_card(self, h):
        """«🛡 Я админ места» → «◀️ К карточке»: возврат в карточку без alert."""
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"plm_admin_request:{place_id}")
        self.assertIn("plm_back", _callbacks(h, "plm_back"))
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_admin_request")

        await h.callback("plm_back")
        self.assertNotIn("устарела", _last_alert(h).lower())
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")
        self.assertEqual(state["place_id"], place_id)
        self.assertIn("Альтернативные названия", h.screen_text())

    @scenario_test
    async def test_back_from_new_place_input_returns_to_list(self, h):
        """Создание из справочника → «◀️ Назад»: возврат в список справочника."""
        _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        self.assertIn("plm_back", _callbacks(h, "plm_back"))
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.get("manage_mode"))

        await h.callback("plm_back")
        self.assertNotIn("устарела", _last_alert(h).lower())
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        self.assertIn("Дом культуры", h.screen_text())

    @scenario_test
    async def test_ordinary_back_sends_pl_back_not_plm_back(self, h):
        """Обычный сценарий: «Назад» на вводе места не уходит в `plm_back`."""
        await h.text("/start")
        await h.callback("places")
        await h.callback("pl_add")
        self.assertIn("pl_back", _callbacks(h, "pl_back"))
        self.assertEqual(_callbacks(h, "plm_back"), [])

        await h.callback("pl_back")
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub")
        self.assertNotIn("📍 Площадки", h.screen_text())

    @scenario_test
    async def test_manage_mode_does_not_leak_into_places_section(self, h):
        """Выход из справочника сбрасывает `manage_mode` в обычном разделе."""
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.get("manage_mode"))

        # Пользователь уходит в обычный раздел «Места» кнопкой меню.
        await h.callback("places")
        self.assertEqual(_callbacks(h, "plm_back"), [])
        self.assertIn("pl_back", _callbacks(h, "pl_back"))
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertNotIn("manage_mode", state)

        await h.callback("pl_back")
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub")

    @scenario_test
    async def test_pick_place_tap_still_starts_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

        await h.callback(f"pl_pick:{place_id}")
        self.assertIsNotNone(_latest_draft(h))
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_PHASE)
