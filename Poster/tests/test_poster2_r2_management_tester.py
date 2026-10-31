# tests/test_poster2_r2_management_tester.py
"""
R2 (Poster 2.1) — независимая проверка №25 и №26 (poster-tester).

Сценарии написаны по требованиям владельца, а не по реализации:
«Управление → Шаблоны» — только управление шаблонами, «Управление →
Площадки» — настоящий справочник. Всё идёт через настоящее приложение
(`bot.build_application`), FakeBot и временную SQLite
(`tests.integration_harness`): реальные `post_bot.db`, `.env` и Telegram не
используются.

Покрытие №25:
- из раздела шаблонов нельзя запустить анонс ни одним путём: ни входом,
  ни карточкой, ни всеми её кнопками (черновики и диалог конструктора
  остаются нетронутыми);
- сохранённая фаза `pick` не возобновляется ни кнопкой «Управление →
  Шаблоны», ни `/templates`;
- удаление шаблона с историей (снимок/анонс) — понятный отказ, данные целы;
- подделки: `tp_delete` на стандарт, `tp_delete_yes` без подтверждения,
  `tp_delete_yes` после чужой правки (race) отклоняются без потери данных.

Покрытие №26:
- карточка справочника и действия трёх ролей (админ места, суперадмин,
  не-админ), экранирование данных;
- IDOR: подделанные `plm_edit`/`plm_admins` по чужому id отклоняются;
- stale-кнопки новых фаз получают явный ответ и не меняют данные;
- создание места из справочника: стандартный шаблон есть и рендерится ровно
  в эталон v3, анонс не запускается;
- регресс: тап по площадке в «Создать для площадки» по-прежнему сразу
  запускает анонс.

Ранее найденные дефекты №26 (навигация справочника) исправлены и закреплены
регресс-тестами: экран «🛡 Я админ места» и ввод названия нового места
показывали кнопку «◀️ Назад» с callback `plm_back`, который в этих фазах
отвечал «Кнопка устарела» и оставлял пользователя на том же экране. Сейчас
`plm_back` работает в этих фазах, тесты
(`HandbookNavigationRegressionTests`) зелёные и держат поведение от регресса —
директив `expectedFailure` в файле нет.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import func, select

try:
    from admin_state import load_admin_session
    from announcement_store import (
        start_announcement_wizard,
    )
    from brick_store import get_system_brick
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        AnnouncementDraft,
        Place,
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        utcnow,
    )
    from place_state import load_place_session
    from place_store import add_alternative_name, create_place
    from template_state import load_template_session
    from template_store import (
        create_personal_template,
        get_personal_template,
        get_standard_template,
        list_personal_templates,
        render_template_version,
        update_template_content,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(exc)


OTHER_TG = 8_920_002
SUPERADMIN_TG = 8_920_003

# Фаза вопросов конструктора: анонс действительно начат.
AW_QUESTIONS_PHASE = 2

# Эталон владельца (Часть III) — побайтово, как он уходит в Telegram.
OWNER_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "@contact"
)

STALE_PLACES = "устарела"
STALE_TEMPLATES = "устарела"


# --- Помощники ---------------------------------------------------------------


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
    address: str | None = "ул. Тестовая, 1",
    url: str | None = "https://example.com/place",
) -> int:
    """Место со стандартным шаблоном; `admin=True` выдаёт роль админа места."""
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address=address,
            url=url,
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
    content: str = "**[название]**\n[описание]",
) -> int:
    """Личный шаблон без снимков и анонсов — его можно удалить."""
    with h.session_factory() as session:
        get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            content=content,
        )
        session.commit()
        return int(template.template_id)


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


def _draft_count(h) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0)


def _personal_row(h, template_id: int):
    with h.session_factory() as session:
        return get_personal_template(session, DEFAULT_USER_ID, template_id)


def _personal_templates(h, telegram_user_id: int = DEFAULT_USER_ID) -> list:
    with h.session_factory() as session:
        return list_personal_templates(session, telegram_user_id)


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


def _snapshot_count(h, template_id: int) -> int:
    with h.session_factory() as session:
        return int(
            session.scalar(
                select(func.count(TemplateSnapshot.id)).where(
                    TemplateSnapshot.source_template_id == template_id
                )
            )
            or 0
        )


def _values_for(session) -> dict[int, str]:
    raw = {
        "название": "Летний концерт",
        "дата": "12.07.2027",
        "время_начала": "19:00",
        "место": "Дом культуры",
        "ссылка_места": "https://example.com/place",
        "адрес": "ул. Ленина, 1",
        "описание": "Большой зал, вход свободный.",
        "контакт": "@contact",
    }
    values: dict[int, str] = {}
    for code, value in raw.items():
        brick = get_system_brick(session, code)
        assert brick is not None, f"нет системного кирпича {code}"
        values[int(brick.id)] = value
    return values


# --- №25: «Управление → Шаблоны» ---------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateSectionOnlyManagesTests(unittest.TestCase):
    """Дефект №25: раздел шаблонов никогда не запускает создание анонса."""

    @scenario_test
    async def test_every_card_button_keeps_no_announcement(self, h):
        """Ни одна кнопка карточки не создаёт черновик и не открывает диалог."""
        template_id = _seed_personal_template(h, name="Карточка")
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        self.assertEqual(_draft_count(h), 0)

        # Обходим все кнопки карточки и возвращаемся в неё после каждой:
        # «назад» у подтверждений ведёт в ту же карточку.
        explored: set[str] = set()
        for _round in range(8):
            buttons = [
                data
                for _label, data in h.screen_buttons()
                if data and data.startswith("tp_")
            ]
            pending = [
                data
                for data in buttons
                if data not in explored and not data.startswith("tp_delete_yes")
            ]
            if not pending:
                break
            data = pending[0]
            explored.add(data)
            await h.callback(data)
            self.assertIsNone(
                h.dialog_state(name=CONVERSATION_NAME),
                f"кнопка {data} открыла диалог конструктора",
            )
            self.assertEqual(_draft_count(h), 0, f"кнопка {data} создала анонс")
            if (load_template_session(DEFAULT_USER_ID) or {}).get(
                "phase"
            ) != "card" and not data.startswith("tp_delete"):
                await h.callback(f"tp_open:{template_id}")

        # Ни в одном состоянии раздела нет старта анонса.
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertEqual(_callbacks(h, "pl_pick:"), [])
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h, "tp_delete:"))

    @scenario_test
    async def test_manage_entry_and_command_do_not_resume_saved_pick(self, h):
        """Сохранённая фаза `pick` не возобновляется ни кнопкой, ни /templates."""
        template_id = _seed_personal_template(h, name="Из выбора")
        await h.text("/start")
        await h.callback("pick_template")
        self.assertIn(f"tp_pick:{template_id}", _callbacks(h, "tp_pick:"))

        await h.callback("manage_templates")
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub")
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0)

        await h.callback("pick_template")
        await h.text("/templates")
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "hub")
        self.assertEqual(_callbacks(h, "tp_pick:"), [])
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0)

    @scenario_test
    async def test_delete_with_announcement_history_is_refused_and_keeps_data(self, h):
        """Анонс по шаблону блокирует удаление: отказ понятен, данные целы."""
        template_id = _seed_personal_template(h, name="С анонсом")
        with h.session_factory() as session:
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                origin="personal",
                template_id=template_id,
            )
            draft_id = int(draft.draft_id)
            session.commit()

        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        await h.callback(f"tp_delete_yes:{template_id}")

        self.assertIn("нельзя удалить", h.screen_text())
        self.assertIsNotNone(_personal_row(h, template_id), "шаблон сохранён")
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")
        with h.session_factory() as session:
            self.assertIsNotNone(session.get(AnnouncementDraft, draft_id))
            self.assertEqual(
                int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0), 1
            )

    @scenario_test
    async def test_forged_delete_of_standard_is_rejected(self, h):
        """`tp_delete` на стандарт места: отказ, шаблон и место на месте."""
        place_id = _seed_place(h, name="Стандарт", admin=True)
        template_id = _standard_template_id(h, place_id)
        await h.text("/start")
        await h.callback(f"tp_std:{place_id}")

        self.assertEqual(_callbacks(h, "tp_delete:"), [], "у стандарта нет удаления")
        await h.callback(f"tp_delete:{template_id}")
        self.assertIn("Стандартный шаблон удалить нельзя", _last_alert(h))
        self.assertEqual(_standard_template_id(h, place_id), template_id)
        with h.session_factory() as session:
            self.assertIsNotNone(session.get(Place, place_id))

    @scenario_test
    async def test_delete_yes_without_confirmation_is_stale(self, h):
        """`tp_delete_yes` из карточки (без подтверждения) ничего не удаляет."""
        template_id = _seed_personal_template(h, name="Без подтверждения")
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        await h.callback(f"tp_delete_yes:{template_id}")
        self.assertIn(STALE_TEMPLATES, _last_alert(h).lower())
        self.assertIsNotNone(_personal_row(h, template_id))
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")

    @scenario_test
    async def test_delete_after_concurrent_edit_is_stale_and_keeps_new_version(self, h):
        """Гонка: правка из другого окна отменяет удаление по старому экрану."""
        template_id = _seed_personal_template(h, name="Гонка")
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")

        with h.session_factory() as session:
            result = update_template_content(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=template_id,
                expected_revision=1,
                content="правка из другого окна",
            )
            self.assertEqual(result.status, "updated")
            session.commit()

        await h.callback(f"tp_delete_yes:{template_id}")
        self.assertIn("Удаление не выполнено", h.screen_text())
        row = _personal_row(h, template_id)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.revision, 2)
        self.assertEqual(row.current_version.content, "правка из другого окна")
        state = load_template_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "card")
        self.assertEqual(state["revision"], 2)

    @scenario_test
    async def test_foreign_template_delete_is_denied(self, h):
        """IDOR: чужой шаблон не открывается и не удаляется."""
        with h.session_factory() as session:
            foreign_user = get_or_create_user(
                session, OTHER_TG, username="other-template"
            )
            foreign = create_personal_template(
                session,
                actor_telegram_user_id=OTHER_TG,
                name="Чужой",
                content="\n".join(p for p in ("**[название]**", "[описание]", "") if p),
            )
            foreign_id = int(foreign.template_id)
            session.commit()
            self.assertIsNotNone(foreign_user)

        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{foreign_id}")
        self.assertIn("не найден", _last_alert(h).lower())
        await h.callback(f"tp_delete:{foreign_id}")
        self.assertIn(STALE_TEMPLATES, _last_alert(h).lower())
        await h.callback(f"tp_delete_yes:{foreign_id}")
        self.assertIn(STALE_TEMPLATES, _last_alert(h).lower())

        with h.session_factory() as session:
            self.assertIsNotNone(session.get(Template, foreign_id))


# --- №26: «Управление → Площадки» -------------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceHandbookTests(unittest.TestCase):
    """Дефект №26: справочник площадок с карточкой и правами трёх ролей."""

    @scenario_test
    async def test_manage_places_opens_handbook_and_drops_saved_pick(self, h):
        """Справочник открывается списком, сохранённый выбор места не оживает."""
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        # Сначала вход «Создать для площадки» (сохранённая фаза pick).
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

        await h.callback("manage_places")
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")
        # Это справочник, а не выбор места для анонса.
        self.assertEqual(_callbacks(h, "pl_pick:"), [])
        self.assertIn(f"plm_open:{place_id}", _callbacks(h, "plm_open:"))
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0)

        # Тап открывает карточку, а не конструктор.
        await h.callback(f"plm_open:{place_id}")
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn("Альтернативные названия", h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0)

    @scenario_test
    async def test_card_shows_name_aliases_address_and_link_escaped(self, h):
        place_id = _seed_place(
            h,
            name="Дом <b>& Ко",
            admin=True,
            address="ул. <i>Ленина</i>, 1",
            url="https://example.com/a?x=1&y=2",
        )
        _add_alias(h, place_id, "ДК")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        text = h.screen_text()
        self.assertIn("Дом &lt;b&gt;&amp; Ко", text)
        self.assertIn("Альтернативные названия: 1", text)
        self.assertIn("ул. &lt;i&gt;Ленина&lt;/i&gt;, 1", text)
        self.assertIn("https://example.com/a?x=1&amp;y=2", text)
        self.assertNotIn("<b>& Ко", text, "пользовательские данные не экранированы")

    @scenario_test
    async def test_place_admin_sees_edit_only(self, h):
        place_id = _seed_place(h, name="Своё место", admin=True)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        self.assertIn(f"plm_edit:{place_id}", _callbacks(h, "plm_edit:"))
        self.assertEqual(_callbacks(h, "plm_admins:"), [], "не суперадмин")
        self.assertEqual(
            _callbacks(h, "plm_admin_request:"), [], "админу заявка не нужна"
        )

        await h.callback(f"plm_edit:{place_id}")
        admin_state = load_admin_session(DEFAULT_USER_ID)
        self.assertIsNotNone(admin_state)
        assert admin_state is not None
        self.assertEqual(admin_state["phase"], "edit_menu")
        self.assertEqual(admin_state["place_id"], place_id)
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0)

    @scenario_test
    async def test_non_admin_gets_view_and_request_without_actions(self, h):
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        self.assertIn("просмотр справочника", h.screen_text())
        self.assertEqual(_callbacks(h, "plm_edit:"), [])
        self.assertEqual(_callbacks(h, "plm_admins:"), [])
        self.assertIn(
            f"plm_admin_request:{place_id}", _callbacks(h, "plm_admin_request:")
        )
        # Заявка только объясняет: роль не меняется, админ-кабинет не открыт.
        await h.callback(f"plm_admin_request:{place_id}")
        self.assertIn("Я админ места", h.screen_text())
        self.assertIn("не выдаёт полномочия", h.screen_text())
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))
        with h.session_factory() as session:
            admins = session.scalars(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            ).all()
        self.assertEqual(len(admins), 0, "состав админов места не изменился")

    @scenario_test
    async def test_superadmin_sees_admins_and_edit(self, h):
        place_id = _seed_place(h, name="Любое место", actor_tg=OTHER_TG)
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback("manage_places", user_id=SUPERADMIN_TG)
            await h.callback(f"plm_open:{place_id}", user_id=SUPERADMIN_TG)

            self.assertIn(f"plm_edit:{place_id}", _callbacks(h, "plm_edit:"))
            self.assertIn(f"plm_admins:{place_id}", _callbacks(h, "plm_admins:"))
            self.assertEqual(
                _callbacks(h, "plm_admin_request:"), [], "суперадмин не просит роль"
            )

            await h.callback(f"plm_admins:{place_id}", user_id=SUPERADMIN_TG)
            admin_state = load_admin_session(SUPERADMIN_TG)
            self.assertIsNotNone(admin_state)
            assert admin_state is not None
            self.assertEqual(admin_state["phase"], "place")
            self.assertEqual(admin_state["place_id"], place_id)

    @scenario_test
    async def test_foreign_place_edit_and_admins_are_denied(self, h):
        """IDOR: подделанные `plm_edit`/`plm_admins` чужого места отклоняются."""
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")

        await h.callback(f"plm_edit:{place_id}")
        self.assertIn("Редактирование доступно администратору места", _last_alert(h))
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

        await h.callback(f"plm_admins:{place_id}")
        self.assertIn("только суперадминистратору", _last_alert(h))
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(Template.id)).where(
                            Template.place_id == place_id
                        )
                    )
                    or 0
                ),
                1,
                "стандартный шаблон места не тронут",
            )
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")

    @scenario_test
    async def test_forged_edit_for_another_place_than_card_is_stale(self, h):
        """Кнопка с id, отличным от открытой карточки, не открывает редактор."""
        own = _seed_place(h, name="Своё", admin=True)
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{own}")
        await h.callback(f"plm_edit:{foreign}")

        self.assertIn(STALE_PLACES, _last_alert(h).lower())
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

    @scenario_test
    async def test_stale_callbacks_without_state_are_answered(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        for callback in (
            f"plm_open:{place_id}",
            f"plm_edit:{place_id}",
            f"plm_admins:{place_id}",
            f"plm_admin_request:{place_id}",
            "plm_page:0",
            "plm_spage:0",
            "plm_search",
            "plm_add",
            "plm_back",
        ):
            with self.subTest(callback=callback):
                await h.callback(callback)
                self.assertIn(STALE_PLACES, _last_alert(h).lower())
        self.assertIsNone(load_place_session(DEFAULT_USER_ID))
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))
        self.assertEqual(_draft_count(h), 0)

    @scenario_test
    async def test_stale_button_does_not_redraw_from_another_phase(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"plm_open:{place_id}")

        self.assertIn(STALE_PLACES, _last_alert(h).lower())
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")

    @scenario_test
    async def test_search_result_not_matching_query_cannot_be_opened(self, h):
        """Устаревшая кнопка выдачи поиска не открывает место из другого запроса."""
        _seed_place(h, name="Дом культуры")
        other = _seed_place(h, name="Библиотека", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_search")
        await h.text("Дом")
        await h.callback(f"plm_open:{other}")

        self.assertIn(STALE_PLACES, _last_alert(h).lower())
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_search_results")


# --- №26: создание места из справочника и регресс pl_pick --------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceCreationFromHandbookTests(unittest.TestCase):
    """Создание места из справочника: стандарт v3 есть, анонс не стартует."""

    @scenario_test
    async def test_created_place_has_v3_standard_and_no_announcement(self, h):
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        self.assertIn("Анонс не запускается", h.screen_text())

        await h.text("Новая площадка R2")
        self.assertIn("Проверьте, нет ли уже этого места", h.screen_text())
        await h.callback("pl_create_confirm")
        await h.text("Москва")

        text = h.screen_text()
        self.assertIn("Новая площадка R2", text)
        self.assertIn("✅ Место добавлено", text)
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_draft_count(h), 0, "создание не начинает анонс")
        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))

        with h.session_factory() as session:
            place = session.scalar(select(Place).order_by(Place.id.desc()))
            self.assertIsNotNone(place)
            assert place is not None
            place_id = int(place.id)
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            self.assertIsNotNone(standard, "у нового места есть стандартный шаблон")
            assert standard is not None
            rendered = render_template_version(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                values_by_id=_values_for(session),
            )
        self.assertEqual(rendered, OWNER_REFERENCE, "стандарт нового места — v4")

        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")
        self.assertEqual(state["place_id"], place_id)
        # Создатель места не становится админом места (`place_store.create_place`),
        # поэтому действий редактирования нет — только просмотр и «Я админ места».
        self.assertEqual(_callbacks(h, "plm_edit:"), [], "создатель не админ места")
        self.assertEqual(_callbacks(h, "plm_admins:"), [], "не суперадмин")
        self.assertIn(
            f"plm_admin_request:{place_id}", _callbacks(h, "plm_admin_request:")
        )
        self.assertIn("просмотр справочника", h.screen_text())

    @scenario_test
    async def test_pick_place_tap_still_starts_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

        await h.callback(f"pl_pick:{place_id}")
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_PHASE)
        self.assertIn("Вопрос 1", h.screen_text())
        with h.session_factory() as session:
            self.assertEqual(
                int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0), 1
            )


# --- Регресс №26: навигация справочника --------------------------------------


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HandbookNavigationRegressionTests(unittest.TestCase):
    """
    Регрессионные тесты навигации справочника (№26).

    Кнопка «◀️ Назад» на двух экранах отправляет `plm_back`, и он работает в
    обеих фазах:
    - «🛡 Я админ места» (`manage_admin_request`) → карточка (`manage_card`);
    - ввод названия нового места (`create_input`) → список (`manage_list`).

    Ожидание владельца: у каждого экрана раздела есть рабочая навигация —
    «назад» возвращает на предыдущий экран справочника, а не отвечает
    «Кнопка устарела» и не оставляет пользователя на месте.

    Ранее это был дефект; сейчас поведение исправлено, тесты зелёные и
    охраняют его от регресса (`expectedFailure` здесь нет и не требуется).
    """

    @scenario_test
    async def test_back_from_admin_request_returns_to_card(self, h):
        """«🛡 Я админ места» → «◀️ К карточке» должно вернуть в карточку."""
        place_id = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        await h.callback(f"plm_admin_request:{place_id}")
        self.assertEqual(
            (load_place_session(DEFAULT_USER_ID) or {}).get("phase"),
            "manage_admin_request",
        )

        await h.callback("plm_back")
        self.assertNotIn(
            STALE_PLACES,
            _last_alert(h).lower(),
            "кнопка «◀️ К карточке» отвечает «Кнопка устарела»",
        )
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_card")
        self.assertIn("Альтернативные названия", h.screen_text())

    @scenario_test
    async def test_back_from_new_place_input_returns_to_list(self, h):
        """Создание места из справочника → «◀️ Назад» должно вернуть в список."""
        _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        self.assertIn("plm_back", _callbacks(h, "plm_back"))
        self.assertEqual(
            (load_place_session(DEFAULT_USER_ID) or {}).get("phase"), "create_input"
        )

        await h.callback("plm_back")
        self.assertNotIn(
            STALE_PLACES,
            _last_alert(h).lower(),
            "кнопка «◀️ Назад» на вводе названия отвечает «Кнопка устарела»",
        )
        state = load_place_session(DEFAULT_USER_ID)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state["phase"], "manage_list")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
