# tests/test_poster2_menu.py
"""
Poster 2.0, S2: главное меню из пяти кнопок и два прямых входа в конструктор.

Проверяется через настоящее приложение `bot.build_application`, FakeBot и
временную SQLite (tests.integration_harness): реальный Telegram, `.env` и
`post_bot.db` не используются.

Покрытие:

- состав меню (ровно пять кнопок, старые входы отсутствуют);
- тап по площадке сразу создаёт подтверждённый черновик со стандартным
  шаблоном места — без промежуточных подтверждений;
- тап по личному шаблону сразу создаёт черновик без площадки;
- «➕ Новая площадка» в режиме выбора → создание → автозапуск анонса;
- CRUD личных шаблонов в разделе выбора;
- первый `/start` выдаёт ровно один личный шаблон, повторный — без дубля,
  после осознанного удаления шаблон не воскрешает;
- «Мои анонсы» сохраняют завершённые анонсы;
- «Управление»: администрирование только по правам, `/admin` работает;
- HTML-экранирование пользовательских названий;
- негативные сценарии: чужой шаблон (IDOR), подделанный callback,
  устаревшая кнопка.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import delete, select

try:
    from announcement_store import (
        ORIGIN_PERSONAL,
        ORIGIN_PLACE,
        begin_announcement_publication,
        get_announcement_draft,
        list_announcement_drafts,
        mark_announcement_publication_sent,
        set_announcement_stage,
    )
    from announcement_wizard import WizardStage
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        AnnouncementDraft,
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        TemplateVersion,
        TemplateVersionBrick,
        User,
    )
    from place_store import create_place
    from template_store import (
        INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY,
        create_personal_template,
        list_personal_templates,
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


OTHER_TG = 8_900_002
SUPERADMIN_TG = 8_900_003

MENU_BUTTONS = [
    "📍 Создать для площадки",
    "📝 Создать по личному шаблону",
    "📂 Мои анонсы",
    "⚙️ Управление",
    "❓ Помощь",
]
MENU_CALLBACKS = [
    "pick_place",
    "pick_template",
    "my_announcements",
    "manage",
    "help",
]


def _user(session, telegram_user_id: int = DEFAULT_USER_ID):
    user = session.scalar(select(User).where(User.telegram_user_id == telegram_user_id))
    if user is None:
        raise AssertionError(f"Пользователь {telegram_user_id} не создан")
    return user


def _latest_draft(h, telegram_user_id: int = DEFAULT_USER_ID):
    with h.session_factory() as session:
        user = _user(session, telegram_user_id)
        return session.scalar(
            select(AnnouncementDraft)
            .where(AnnouncementDraft.author_user_id == user.id)
            .order_by(AnnouncementDraft.id.desc())
        )


def _draft_details(h, draft_id: int, telegram_user_id: int = DEFAULT_USER_ID):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=telegram_user_id,
            draft_id=draft_id,
        )


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
):
    """Место со стандартным шаблоном (через доменный сервис).

    `admin=True` выдаёт действующую роль администратора этого места.
    """
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. Тестовая, 1",
            url="https://example.com/place",
        )
        if admin:
            from models import utcnow

            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return place.id


def _seed_personal_template(
    h,
    *,
    name: str = "Личный шаблон",
    actor_tg: int = DEFAULT_USER_ID,
    content: str = "[название]",
):
    with h.session_factory() as session:
        get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            content=content,
        )
        session.commit()
        return template.template_id


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


async def _open_preview(h) -> None:
    """
    Открывает обзор анонса с экрана завершения (U2b).

    На экране завершения ровно две кнопки, кнопки обзора там нет; обзор
    запрашивается тем же callback'ом, что и «К обзору» с экрана фото, —
    текст показывается, версия не пересобирается.
    """
    data = _callbacks(h, "aw_preview")
    if data:
        await h.callback(data[0])
        return
    ui = h.user_data().get("announcement_ui") or {}
    draft_id = ui.get("draft_id")
    revision = ui.get("revision")
    if draft_id is None or revision is None:
        raise AssertionError(f"нет callback aw_preview: {h.screen_buttons()}")
    await h.callback(f"aw_preview:{int(draft_id)}:{int(revision)}")


def _last_alert(h) -> str | None:
    answers = h.answers()
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


def _personal_count(h, telegram_user_id: int = DEFAULT_USER_ID) -> int:
    with h.session_factory() as session:
        return len(list_personal_templates(session, telegram_user_id))


def _delete_personal_templates(h, telegram_user_id: int = DEFAULT_USER_ID) -> None:
    """Осознанное удаление всех личных шаблонов (сервиса удаления нет)."""
    with h.session_factory() as session:
        user = _user(session, telegram_user_id)
        template_ids = list(
            session.scalars(
                select(Template.id).where(
                    Template.kind == "personal",
                    Template.owner_user_id == user.id,
                )
            )
        )
        # Порядок важен: триггер запрещает удалять версии, пока жива строка
        # `templates`; кирпичики версии чистятся после самих версий.
        for template_id in template_ids:
            session.execute(delete(Template).where(Template.id == template_id))
            version_ids = list(
                session.scalars(
                    select(TemplateVersion.id).where(
                        TemplateVersion.template_id == template_id
                    )
                )
            )
            for version_id in version_ids:
                # Сначала версия: триггеры защищают привязки, пока жива строка
                # версии, поэтому кирпичики удаляются после неё.
                session.execute(
                    delete(TemplateVersion).where(TemplateVersion.id == version_id)
                )
                session.execute(
                    delete(TemplateVersionBrick).where(
                        TemplateVersionBrick.template_version_id == version_id
                    )
                )
        session.commit()


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MainMenuTests(unittest.TestCase):
    """Главное меню Poster 2.0: ровно пять кнопок."""

    @scenario_test
    async def test_menu_has_five_buttons(self, h):
        await h.text("/start")
        buttons = h.screen_buttons()
        self.assertEqual([label for label, _ in buttons], MENU_BUTTONS)
        self.assertEqual([data for _, data in buttons], MENU_CALLBACKS)

    @scenario_test
    async def test_old_entries_are_gone(self, h):
        await h.text("/start")
        labels = [label for label, _ in h.screen_buttons()]
        data = [value for _, value in h.screen_buttons()]
        for legacy in ("📝 Новый мастер", "✏️ Создать пост", "📂 Черновики"):
            self.assertNotIn(legacy, labels)
        for legacy in ("aw_start", "create_post", "drafts", "places", "templates"):
            self.assertNotIn(legacy, data)

    @scenario_test
    async def test_help_screen_lists_new_entries(self, h):
        await h.text("/start")
        await h.callback("help")
        text = h.screen_text()
        self.assertIn("Создать для площадки", text)
        self.assertIn("Создать по личному шаблону", text)
        self.assertIn("Мои анонсы", text)
        self.assertIn("Управление", text)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlacePickTests(unittest.TestCase):
    """«📍 Создать для площадки»: тап по площадке сразу начинает анонс."""

    @scenario_test
    async def test_list_shows_places_and_tap_starts_immediately(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")

        # Список площадок — первый экран, без промежуточного «справочника».
        self.assertIn("Создать для площадки", h.screen_text())
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

        answers_before = len(h.answers())
        await h.callback(f"pl_pick:{place_id}")

        # Черновик создан сразу: место подтверждено, снимок стандартного
        # шаблона зафиксирован, пользователь уже на экране вопросов.
        draft = _latest_draft(h)
        self.assertIsNotNone(draft)
        details = _draft_details(h, draft.id)
        self.assertEqual(details.origin, ORIGIN_PLACE)
        self.assertEqual(details.place_id, place_id)
        self.assertIsNotNone(details.template_snapshot)
        self.assertEqual(details.template_snapshot.source_kind, "standard")
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_FOR_TEST)
        # Никаких дополнительных подтверждений места/шаблона между тапом и
        # вопросами: только ответ на сам callback.
        self.assertLessEqual(len(h.answers()) - answers_before, 1)

    @scenario_test
    async def test_new_place_from_pick_creates_and_auto_starts(self, h):
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_add")
        await h.text("Новая площадка S2")
        await h.callback("pl_create_confirm")
        # U1a: у новой площадки обязателен город; анонс стартует сразу после.
        await h.text("Москва")

        # Площадка создана и сразу открыт анонс по её стандартному шаблону.
        draft = _latest_draft(h)
        self.assertIsNotNone(draft)
        details = _draft_details(h, draft.id)
        self.assertEqual(details.origin, ORIGIN_PLACE)
        self.assertIsNotNone(details.place_id)
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_pick_search_filters_and_starts(self, h):
        first = _seed_place(h, name="Дом культуры")
        _seed_place(h, name="Театр Этаж")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick_search")
        await h.text("Дом")

        calls = _callbacks(h, "pl_pick:")
        self.assertIn(f"pl_pick:{first}", calls)
        await h.callback(f"pl_pick:{first}")
        details = _draft_details(h, _latest_draft(h).id)
        self.assertEqual(details.place_id, first)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplatePickTests(unittest.TestCase):
    """«📝 Создать по личному шаблону»: тап сразу начинает анонс."""

    @scenario_test
    async def test_tap_personal_template_starts_without_place(self, h):
        template_id = _seed_personal_template(h, name="Мой шаблон")
        await h.text("/start")
        await h.callback("pick_template")

        self.assertIn(f"tp_pick:{template_id}", _callbacks(h, "tp_pick:"))
        await h.callback(f"tp_pick:{template_id}")

        draft = _latest_draft(h)
        details = _draft_details(h, draft.id)
        self.assertEqual(details.origin, ORIGIN_PERSONAL)
        self.assertIsNone(details.place_id)
        self.assertEqual(details.template_snapshot.source_kind, "personal")
        self.assertIn("Вопрос 1", h.screen_text())

    @scenario_test
    async def test_create_template_in_pick_section_then_start(self, h):
        await h.text("/start")
        await h.callback("pick_template")
        await h.callback("tp_create")
        await h.text("Шаблон из раздела")
        await h.callback("tp_cstd")
        await h.callback("tp_csave")

        with h.session_factory() as session:
            created = session.scalar(
                select(Template).where(Template.name == "Шаблон из раздела")
            )
        self.assertIsNotNone(created)
        # Создание вернуло в карточку шаблона — правка доступна там же.
        self.assertEqual(h.screen_text().count("Шаблон из раздела"), 1)

    @scenario_test
    async def test_edit_personal_template_from_pick(self, h):
        template_id = _seed_personal_template(h, name="К правке")
        await h.text("/start")
        await h.callback("pick_template")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")
        await h.text("Новый текст")
        await h.callback("tp_save")

        with h.session_factory() as session:
            from models import TemplateVersion

            version = session.scalar(
                select(TemplateVersion)
                .where(TemplateVersion.template_id == template_id)
                .order_by(TemplateVersion.id.desc())
            )
        self.assertEqual(version.content, "Новый текст")

    @scenario_test
    async def test_personal_template_starts_regardless_of_place_absence(self, h):
        """Личный шаблон не требует площадки: в списке нет поиска мест."""
        _seed_personal_template(h, name="Без места")
        await h.text("/start")
        await h.callback("pick_template")
        self.assertNotIn("pl_pick:", " ".join(_callbacks(h, "pl_")))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InitialTemplateTests(unittest.TestCase):
    """Первый `/start` выдаёт стартовый шаблон ровно один раз."""

    @scenario_test
    async def test_first_start_creates_exactly_one_template(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            templates = list_personal_templates(session, DEFAULT_USER_ID)
        self.assertEqual(len(templates), 1)
        # Poster 2.1 R2: стартовая копия называется «Дефолтный шаблон».
        self.assertEqual(templates[0].display_name, "Дефолтный шаблон")

    @scenario_test
    async def test_repeated_start_does_not_duplicate(self, h):
        await h.text("/start")
        await h.text("/start")
        await h.callback("main_menu")
        await h.text("/start")
        with h.session_factory() as session:
            templates = list_personal_templates(session, DEFAULT_USER_ID)
        self.assertEqual(len(templates), 1)

    @scenario_test
    async def test_deleted_template_is_not_resurrected_by_start(self, h):
        await h.text("/start")
        self.assertEqual(_personal_count(h), 1)
        _delete_personal_templates(h)
        self.assertEqual(_personal_count(h), 0)

        # Повторный /start после осознанного удаления не воскрешает шаблон:
        # выдачу помнит неизменяемый снимок-метка, а не строка `templates`.
        await h.text("/start")
        await h.callback("main_menu")
        await h.text("/start")
        self.assertEqual(_personal_count(h), 0)

        # И явный повторный вызов сервиса тоже не создаёт второй.
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID)
            session.commit()
        self.assertEqual(_personal_count(h), 0)

    @scenario_test
    async def test_preexisting_user_gets_starter_template_once(self, h):
        """Существующая строка `users` — не повод остаться без шаблона.

        План S1 п.7 / S2 п.3: однократный бэкфилл имеющихся пользователей
        (например, после передачи прав или из прежней версии продукта).
        """
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="old-user")
            session.commit()

        await h.text("/start")
        self.assertEqual(_personal_count(h), 1, "бэкфилл на /start")

        await h.callback("main_menu")
        await h.text("/start")
        self.assertEqual(_personal_count(h), 1, "повторный /start без дубля")

    @scenario_test
    async def test_starter_marker_snapshot_survives_template_deletion(self, h):
        """Метка выдачи — снимок `INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY`."""
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="old-user")
            session.commit()

        await h.text("/start")
        with h.session_factory() as session:
            keys = list(
                session.scalars(
                    select(TemplateSnapshot.idempotency_key).where(
                        TemplateSnapshot.idempotency_key
                        == INITIAL_PERSONAL_TEMPLATE_SNAPSHOT_KEY
                    )
                )
            )
        self.assertEqual(len(keys), 1, "метка выдачи записана один раз")

        _delete_personal_templates(h)
        with h.session_factory() as session:
            snapshots = list(
                session.scalars(
                    select(TemplateSnapshot.id).where(
                        TemplateSnapshot.source_kind == "personal"
                    )
                )
            )
        self.assertEqual(len(snapshots), 1, "метка переживает удаление шаблона")

    @scenario_test
    async def test_personal_announcement_history_blocks_starter(self, h):
        """Есть личный анонс (и снимок) — стартовый шаблон не выдаётся."""
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="old-user")
            session.commit()
        template_id = _seed_personal_template(h, name="Свой рабочий")
        await h.text("/start")
        await h.callback("pick_template")
        await h.callback(f"tp_pick:{template_id}")
        self.assertIn("Вопрос 1", h.screen_text())

        # Пользователь удалил свой единственный шаблон, но история осталась.
        _delete_personal_templates(h)
        self.assertEqual(_personal_count(h), 0)

        await h.text("/start")
        self.assertEqual(
            _personal_count(h), 0, "история владения не даёт новый стартовый"
        )

    @scenario_test
    async def test_new_user_gets_own_template(self, h):
        await h.text("/start")
        await h.text("/start", user_id=OTHER_TG)
        with h.session_factory() as session:
            self.assertEqual(len(list_personal_templates(session, DEFAULT_USER_ID)), 1)
            self.assertEqual(len(list_personal_templates(session, OTHER_TG)), 1)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalPlaceLineTests(unittest.TestCase):
    """Личный анонс без площадки: пустой строки «Место: » нет ни на одном экране.

    `_place_lines` в `handlers/announcement_wizard.py` — единый helper для пяти
    экранов: выбор шаблона, обзор, отправка, «доставлено» и отказ в неличном
    чате. У `origin='personal'` площадки нет по схеме, поэтому строка не
    печатается вовсе (а не «Место: » с пустым значением).
    """

    @staticmethod
    def _assert_no_empty_place(test, name, text):
        empty = [line for line in text.splitlines() if line.strip() == "Место:"]
        test.assertEqual(empty, [], f"{name}: пустая строка «Место:»:\n{text}")

    @scenario_test
    async def test_all_personal_screens_have_no_empty_place_line(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Без места",
                content="\n".join(
                    p for p in ("", "Постоянный текст [название]", "") if p
                ),
            )
            session.commit()
            template_id = template.template_id

        await h.text("/start")
        await h.callback("pick_template")
        self._assert_no_empty_place(self, "список шаблонов", h.screen_text())

        await h.callback(f"tp_pick:{template_id}")
        self._assert_no_empty_place(self, "вопрос", h.screen_text())

        await h.text("Значение")
        # Poster 2.0 S3 п.6: последний кирпичик ведёт на шаг фотографий, а не
        # к доставке; там проверяем отсутствие пустого «Место:», затем
        # завершаем шаг фото и смотрим экран доставки.
        photo_step = h.screen_text()
        self.assertIn("Фотографии анонса", photo_step)
        self._assert_no_empty_place(self, "шаг фото", photo_step)

        await h.callback(_callback(h, "aw_photo_none"))
        delivered = h.screen_text()
        self.assertIn("✅ Анонс готов", delivered)
        self._assert_no_empty_place(self, "доставлено", delivered)

        await _open_preview(h)
        preview = h.screen_text()
        self.assertIn("Обзор анонса", preview)
        self._assert_no_empty_place(self, "обзор", preview)

        # Экран «выбор шаблона» в шаге 2 достижим через возврат назад.
        draft = _latest_draft(h)
        with h.session_factory() as session:
            updated = set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                stage=WizardStage.TEMPLATE,
                expected_revision=draft.revision,
            )
        await h.callback(f"aw_open:{updated.draft_id}:{updated.revision}")
        self._assert_no_empty_place(self, "шаг выбора шаблона", h.screen_text())

        # Экран «отправка» — реальный старт публикации и открытие черновика.
        with h.session_factory() as session:
            previewed = set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                stage=WizardStage.PREVIEW,
                expected_revision=updated.revision,
            )
            publishing = begin_announcement_publication(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=previewed.draft_id,
            )
        await h.callback(f"aw_open:{draft.id}:{publishing.draft.revision}")
        status = h.screen_text()
        self.assertIn("Отправка готового анонса", status)
        self._assert_no_empty_place(self, "отправка", status)

    @scenario_test
    async def test_private_chat_refusal_has_no_empty_place_line(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Без места",
                content="\n".join(
                    p for p in ("", "Постоянный текст [название]", "") if p
                ),
            )
            session.commit()
            template_id = template.template_id

        group_id = 900_002
        await h.callback(f"tp_pick:{template_id}", chat_id=group_id, chat_type="group")
        await h.text("Значение", chat_id=group_id, chat_type="group")
        # S3 п.6: отказ в неличном чате происходит на финале шага фото.
        self.assertIn("Фотографии анонса", h.screen_text())
        await h.callback(
            _callback(h, "aw_photo_none"), chat_id=group_id, chat_type="group"
        )
        refusal = h.screen_text()
        self.assertIn("только в личный чат", refusal)
        self._assert_no_empty_place(self, "отказ в неличном чате", refusal)

    @scenario_test
    async def test_place_announcement_still_shows_place_line(self, h):
        """Контроль: у анонса по площадке строка «Место: …» остаётся."""
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        for _ in range(25):
            skips = _callbacks(h, "aw_qskip")
            if not skips:
                break
            await h.callback(skips[0])
        # S3 п.6: сначала шаг фото, доставка — по «Без фотографий».
        self.assertIn("Фотографии анонса", h.screen_text())
        await h.callback(_callback(h, "aw_photo_none"))
        delivered = h.screen_text()
        self.assertIn("✅ Анонс готов", delivered)
        # U2b: экран завершения фиксирован и не повторяет пост. Строка
        # «Место: …» остаётся там, где живёт сам анонс — в посте и обзоре.
        # В самом посте место — часть шаблона площадки, без префикса «Место:».
        self.assertIn("Дом культуры", h.posts()[-1] or "")
        self.assertNotIn("Место:", delivered)
        await _open_preview(h)
        self.assertIn("Место: Дом культуры", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MyAnnouncementsTests(unittest.TestCase):
    """«📂 Мои анонсы»: история завершённых анонсов не теряется."""

    @scenario_test
    async def test_finished_announcement_stays_after_new_one(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        first = _latest_draft(h)

        # Завершаем первый анонс: обзор → отправка → «отправлен».
        with h.session_factory() as session:
            set_announcement_stage(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first.id,
                stage=WizardStage.PREVIEW,
                expected_revision=first.revision,
            )
            begin_announcement_publication(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first.id,
            )
            mark_announcement_publication_sent(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first.id,
                message_ids=(501,),
                fingerprint="fp-s2",
            )

        # Второй анонс по тому же месту.
        await h.callback("main_menu")
        await h.callback(f"pl_pick:{place_id}")
        second = _latest_draft(h)
        self.assertNotEqual(first.id, second.id)

        await h.callback("main_menu")
        await h.callback("my_announcements")
        with h.session_factory() as session:
            drafts = list_announcement_drafts(
                session, actor_telegram_user_id=DEFAULT_USER_ID
            )
        ids = {item.draft_id for item in drafts}
        self.assertIn(first.id, ids, "завершённый анонс остаётся в истории")
        self.assertIn(second.id, ids, "новый анонс не удаляет предыдущий")
        self.assertIn("Мои анонсы", h.screen_text())

    @scenario_test
    async def test_my_announcements_empty_state(self, h):
        await h.text("/start")
        await h.callback("my_announcements")
        self.assertIn("Мои анонсы", h.screen_text())
        self.assertIn("Анонсов пока нет", h.screen_text())

    @scenario_test
    async def test_continue_unfinished_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        draft = _latest_draft(h)
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        self.assertIn("Вопрос 1", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ManageSectionTests(unittest.TestCase):
    """«⚙️ Управление»: администрирование только по правам."""

    @scenario_test
    async def test_manage_hides_admin_without_rights(self, h):
        await h.text("/start")
        await h.callback("manage")
        buttons = h.screen_buttons()
        self.assertIn(("📍 Площадки", "manage_places"), buttons)
        self.assertIn(("📝 Шаблоны", "manage_templates"), buttons)
        self.assertIn(("🧱 Кирпичики", "manage_bricks"), buttons)
        self.assertNotIn(("🛡 Администрирование", "admin_panel"), buttons)

    @scenario_test
    async def test_manage_shows_admin_for_superadmin(self, h):
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback("manage", user_id=SUPERADMIN_TG)
            self.assertIn(("🛡 Администрирование", "admin_panel"), h.screen_buttons())

    @scenario_test
    async def test_admin_command_works_for_place_admin(self, h):
        _seed_place(h, name="Дом культуры", admin=True)
        await h.text("/admin")
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn("Мои места", h.screen_text())

    @scenario_test
    async def test_admin_command_denied_without_rights(self, h):
        await h.text("/admin")
        self.assertIn("Раздел доступен только администраторам Poster.", h.screen_text())

    @scenario_test
    async def test_manage_opens_places_and_templates(self, h):
        _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage")
        await h.callback("manage_places")
        # R2b, дефект №26: раздел «Площадки» — справочник, а не выбор места
        # для немедленного старта анонса.
        self.assertIn("📍 Площадки", h.screen_text())
        self.assertIn("Дом культуры", h.screen_text())
        self.assertNotIn("pl_pick:", [data for _label, data in h.screen_buttons()])
        await h.callback("main_menu")
        await h.callback("manage")
        await h.callback("manage_templates")
        self.assertIn("Мои шаблоны", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EscapingTests(unittest.TestCase):
    """Пользовательские названия экранируются в HTML-экране."""

    @scenario_test
    async def test_place_name_is_escaped_in_pick_list(self, h):
        place_id = _seed_place(h, name="Кофейня <b> & «Ёж»")
        await h.text("/start")
        await h.callback("pick_place")
        text = h.screen_text()
        self.assertIn("&lt;b&gt;", text)
        self.assertIn("&amp;", text)
        self.assertNotIn("<b>", text)
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h, "pl_pick:"))

    @scenario_test
    async def test_template_name_is_escaped_in_pick_list(self, h):
        _seed_personal_template(h, name="Шаблон <i> & «Кавычки»")
        await h.text("/start")
        await h.callback("pick_template")
        text = h.screen_text()
        self.assertIn("&lt;i&gt;", text)
        self.assertIn("&amp;", text)
        self.assertNotIn("<i>", text)

    @scenario_test
    async def test_announcement_title_is_escaped_in_history(self, h):
        place_id = _seed_place(h, name="Место <script> & «Тест»")
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        await h.callback("main_menu")
        await h.callback("my_announcements")
        text = h.screen_text()
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NegativeTests(unittest.TestCase):
    """Негативные сценарии: IDOR, подделанный callback, устаревшая кнопка."""

    @scenario_test
    async def test_foreign_personal_template_is_rejected(self, h):
        foreign_id = _seed_personal_template(h, name="Чужой шаблон", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("pick_template")
        # В списке чужих шаблонов нет.
        self.assertNotIn(f"tp_pick:{foreign_id}", _callbacks(h, "tp_pick:"))

        # Подделанный тап по чужому ID: анонс не создаётся.
        await h.callback(f"tp_pick:{foreign_id}")
        self.assertIsNone(_latest_draft(h))
        alert = _last_alert(h) or ""
        self.assertTrue(
            "не найден" in alert.lower() or "недоступ" in alert.lower(),
            alert,
        )

    @scenario_test
    async def test_foreign_place_pick_is_rejected(self, h):
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback("pl_pick:999999")
        with h.session_factory() as session:
            user = _user(session)
            drafts = session.scalar(
                select(AnnouncementDraft.id).where(
                    AnnouncementDraft.author_user_id == user.id
                )
            )
        self.assertIsNone(drafts, "несуществующая площадка не создаёт черновик")
        self.assertTrue(_last_alert(h))

    @scenario_test
    async def test_forged_pick_callback_is_answered(self, h):
        await h.text("/start")
        await h.callback("pl_pick:abc")
        # Кнопка не из экрана: ответ на callback есть, диалог не начат.
        self.assertTrue(h.answers(), "callback обязан быть отвечен")
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_stale_pick_page_does_not_break_dialog(self, h):
        await h.text("/start")
        # Страница списка без открытого раздела: «устарело», диалог не начат.
        await h.callback("pl_pickp:8")
        alert = _last_alert(h) or ""
        self.assertIn("устарела", alert.lower())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_stale_template_page_does_not_break_dialog(self, h):
        await h.text("/start")
        await h.callback("tp_pickpage:5")
        alert = _last_alert(h) or ""
        self.assertIn("устарела", alert.lower())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_my_announcements_does_not_leak_other_users(self, h):
        await h.text("/start", user_id=OTHER_TG)
        foreign_place = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.callback(f"pl_pick:{foreign_place}", user_id=OTHER_TG)
        foreign = _latest_draft(h, OTHER_TG)
        self.assertIsNotNone(foreign)

        await h.text("/start")
        await h.callback("my_announcements")
        self.assertNotIn(f"aw_open:{foreign.id}", _callbacks(h, "aw_open:"))
        await h.callback(f"aw_open:{foreign.id}")
        self.assertNotIn("Вопрос 1", h.screen_text())


# Константа состояния вопросов объявляется в тесте, чтобы не тянуть лишние
# импорты; значение совпадает с `handlers.announcement_wizard.AW_QUESTIONS`.
AW_QUESTIONS_FOR_TEST = 2
