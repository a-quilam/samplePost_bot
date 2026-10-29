# tests/test_poster2_menu_tester.py
"""
Poster 2.0, S2 — независимая проверка (poster-tester).

Сценарии написаны заново, независимо от tests/test_poster2_menu.py:
сквозные пути через настоящее приложение, FakeBot и временную SQLite.
Реальная `post_bot.db`, `.env` и сеть не используются.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import delete, event, func, select
from sqlalchemy import text as sql_text
from sqlalchemy.exc import IntegrityError

try:
    from announcement_store import (
        ORIGIN_PERSONAL,
        ORIGIN_PLACE,
        get_announcement_draft,
        list_announcement_drafts,
    )
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        TemplateVersion,
        TemplateVersionBrick,
        User,
        utcnow,
    )
    from place_store import create_place
    from template_store import (
        create_personal_template,
        ensure_initial_personal_template,
        list_personal_templates,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001
    INTEGRATION_IMPORT_ERROR = str(exc)

OTHER_TG = 8_950_002
SUPER_TG = 8_950_003

MENU = [
    ("📍 Создать для площадки", "pick_place"),
    ("📝 Создать по личному шаблону", "pick_template"),
    ("📂 Мои анонсы", "my_announcements"),
    ("⚙️ Управление", "manage"),
    ("❓ Помощь", "help"),
]

DANGEROUS = "Место <A> & B"


def _user(session, tg: int = DEFAULT_USER_ID):
    user = session.scalar(select(User).where(User.telegram_user_id == tg))
    if user is None:
        raise AssertionError(f"нет пользователя {tg}")
    return user


def _seed_place(h, *, name=DANGEROUS, actor_tg=DEFAULT_USER_ID, admin=False):
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"u{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address="ул. <b>, 1",
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
        return place.id


def _seed_personal(h, *, name="Личный шаблон", actor_tg=DEFAULT_USER_ID):
    with h.session_factory() as session:
        get_or_create_user(session, actor_tg, username=f"u{actor_tg}")
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            header="",
            body="[название]",
            footer="",
        )
        session.commit()
        return template.template_id


def _drafts(h, tg=DEFAULT_USER_ID):
    with h.session_factory() as session:
        return list_announcement_drafts(session, actor_telegram_user_id=tg)


def _details(h, draft_id, tg=DEFAULT_USER_ID):
    with h.session_factory() as session:
        return get_announcement_draft(
            session, actor_telegram_user_id=tg, draft_id=draft_id
        )


def _personal(h, tg=DEFAULT_USER_ID):
    with h.session_factory() as session:
        return list_personal_templates(session, tg)


def _delete_all_personal(h, tg=DEFAULT_USER_ID):
    """Осознанное удаление всех личных шаблонов (в продукте UI удаления нет)."""
    with h.session_factory() as session:
        user = _user(session, tg)
        ids = list(
            session.scalars(
                select(Template.id).where(
                    Template.kind == "personal", Template.owner_user_id == user.id
                )
            )
        )
        for template_id in ids:
            session.execute(delete(Template).where(Template.id == template_id))
            version_ids = list(
                session.scalars(
                    select(TemplateVersion.id).where(
                        TemplateVersion.template_id == template_id
                    )
                )
            )
            for version_id in version_ids:
                session.execute(
                    delete(TemplateVersion).where(TemplateVersion.id == version_id)
                )
                session.execute(
                    delete(TemplateVersionBrick).where(
                        TemplateVersionBrick.template_version_id == version_id
                    )
                )
        session.commit()


def _btns(h):
    return h.screen_buttons()


def _cb(h, prefix):
    found = [d for _l, d in _btns(h) if d and d.startswith(prefix)]
    if not found:
        raise AssertionError(f"нет callback {prefix}: {_btns(h)}")
    return found[0]


def _alert(h):
    answers = h.answers()
    if not answers:
        return ""
    args = answers[-1].get("args") or ()
    return str(args[0]) if args else ""


async def _skip_all_questions(h, limit=25):
    """
    Проходит вопросы пропусками и завершает шаг фотографий.

    Poster 2.0 S3 п.6: последний кирпичик ведёт на шаг фото, а не к доставке;
    анонс завершает «Без фотографий» (кнопки «Готово» нет).
    """
    for _ in range(limit):
        skips = [d for _l, d in _btns(h) if d and d.startswith("aw_qskip")]
        if not skips:
            break
        await h.callback(skips[0])
    else:
        raise AssertionError(f"вопросы не закончились: {h.screen_text()[:300]}")
    no_photos = [d for _l, d in _btns(h) if d and d.startswith("aw_photo_none")]
    if not no_photos:
        raise AssertionError(f"нет шага фото: {h.screen_text()[:300]}")
    await h.callback(no_photos[0])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FullPathTests(unittest.TestCase):
    @scenario_test
    async def test_start_then_place_tap_jumps_straight_to_questions(self, h):
        # Порядок как у живого пользователя: сначала первый контакт, потом
        # (админ/другой пользователь) появляется площадка.
        await h.text("/start")
        self.assertEqual(_btns(h), MENU, "меню — ровно пять кнопок")
        self.assertEqual(len(_personal(h)), 1, "первый контакт дал один шаблон")
        place_id = _seed_place(h)

        await h.callback("pick_place")
        list_screen = h.screen_text()
        self.assertIn("Создать для площадки", list_screen)
        self.assertIn(f"pl_pick:{place_id}", _cb(h, "pl_pick:"))

        before_tap = len(h.screens())
        await h.callback(f"pl_pick:{place_id}")
        new_screens = h.screen_texts()[before_tap:]
        self.assertEqual(
            len(new_screens), 1, f"между тапом и вопросами лишние экраны: {new_screens}"
        )
        for text in new_screens:
            self.assertNotIn("Шаг 1", text)
            self.assertNotIn("Шаг 2", text)
            self.assertNotIn("Выберите шаблон", text)
            self.assertNotIn("подтверд", text.lower())
        self.assertIn("🧱 Вопрос 1", h.screen_text())

        drafts = _drafts(h)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].origin, ORIGIN_PLACE)
        self.assertEqual(drafts[0].place_id, place_id)
        self.assertEqual(drafts[0].template_snapshot.source_kind, "standard")

    @scenario_test
    async def test_personal_template_tap_starts_with_origin_personal(self, h):
        template_id = _seed_personal(h)
        await h.text("/start")
        await h.callback("pick_template")
        self.assertIn(f"tp_pick:{template_id}", _cb(h, "tp_pick:"))

        before = len(h.screens())
        await h.callback(f"tp_pick:{template_id}")
        self.assertEqual(len(h.screens()) - before, 1)
        self.assertIn("🧱 Вопрос 1", h.screen_text())
        drafts = _drafts(h)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].origin, ORIGIN_PERSONAL)
        self.assertIsNone(drafts[0].place_id)
        self.assertEqual(drafts[0].template_snapshot.source_kind, "personal")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class InitialTemplateTests(unittest.TestCase):
    @scenario_test
    async def test_deleted_template_not_restored_but_second_user_gets_one(self, h):
        await h.text("/start")
        self.assertEqual(len(_personal(h)), 1)
        _delete_all_personal(h)
        self.assertEqual(len(_personal(h)), 0)

        # новый контакт (обычный текст) и повторный /start не воскрешают
        await h.text("привет")
        await h.text("/start")
        await h.callback("main_menu")
        await h.text("/start")
        self.assertEqual(len(_personal(h)), 0, "удалённый шаблон не восстановлен")

        # второй пользователь — свой шаблон
        await h.text("/start", user_id=OTHER_TG)
        self.assertEqual(len(_personal(h, OTHER_TG)), 1)
        self.assertEqual(len(_personal(h)), 0)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MyAnnouncementsTests(unittest.TestCase):
    @scenario_test
    async def test_finished_a_survives_b_and_unfinished_b_continues(self, h):
        place_id = _seed_place(h)
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        first = _drafts(h)[0]
        await _skip_all_questions(h)
        first = _details(h, first.draft_id)
        self.assertEqual(first.publication_status, "sent")

        await h.callback("main_menu")
        await h.callback(f"pl_pick:{place_id}")
        drafts = _drafts(h)
        self.assertEqual(len(drafts), 2)
        second = next(d for d in drafts if d.draft_id != first.draft_id)
        self.assertNotEqual(first.draft_id, second.draft_id)
        self.assertIn("🧱 Вопрос 1", h.screen_text())

        await h.callback("main_menu")
        await h.callback("my_announcements")
        text = h.screen_text()
        self.assertIn("Мои анонсы", text)
        self.assertIn("готовый анонс отправлен", text)
        opens = [d for _l, d in _btns(h) if d and d.startswith("aw_open:")]
        self.assertIn(f"aw_open:{first.draft_id}", opens, "A остаётся в истории")
        self.assertIn(f"aw_open:{second.draft_id}", opens, "B тоже в списке")

        # незавершённый B продолжается с того же шага
        await h.callback(f"aw_open:{second.draft_id}")
        self.assertIn("🧱 Вопрос 1", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), 2)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ManageTests(unittest.TestCase):
    @scenario_test
    async def test_admin_entry_visibility_and_admin_command(self, h):
        await h.text("/start")
        await h.callback("manage")
        labels = [label for label, _ in _btns(h)]
        self.assertNotIn("🛡 Администрирование", labels)
        for expected in ("📍 Площадки", "📝 Шаблоны", "🧱 Кирпичики"):
            self.assertIn(expected, labels)

        await h.text("/admin")
        self.assertIn("только администраторам", h.screen_text())

        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/start", user_id=SUPER_TG)
            await h.callback("manage", user_id=SUPER_TG)
            self.assertIn(
                ("🛡 Администрирование", "admin_panel"),
                _btns(h),
                "суперадмин видит вход",
            )
            await h.callback("admin_panel", user_id=SUPER_TG)
            self.assertIn("места", h.screen_text().lower())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class IdorTests(unittest.TestCase):
    @scenario_test
    async def test_foreign_template_and_foreign_draft_are_rejected(self, h):
        foreign_template = _seed_personal(h, name="Чужой", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("pick_template")
        self.assertNotIn(f"tp_pick:{foreign_template}", _cb(h, "tp_pick:"))
        await h.callback(f"tp_pick:{foreign_template}")
        self.assertEqual(_drafts(h), [], "чужой шаблон не создаёт анонс")
        self.assertNotIn("Чужой", h.screen_text())

        # чужой черновик
        foreign_place = _seed_place(h, name="Чужое место", actor_tg=OTHER_TG)
        await h.callback("main_menu")
        await h.callback(f"pl_pick:{foreign_place}", user_id=OTHER_TG)
        foreign_draft = _drafts(h, OTHER_TG)[0]
        self.assertIn("🧱 Вопрос 1", h.screen_text())

        await h.callback("main_menu")
        await h.callback("my_announcements")
        self.assertNotIn("Чужое место", h.screen_text())
        self.assertNotIn(
            f"aw_open:{foreign_draft.draft_id}",
            [d for _l, d in _btns(h) if d],
        )
        await h.callback(f"aw_open:{foreign_draft.draft_id}")
        self.assertNotIn("🧱 Вопрос 1", h.screen_text())
        self.assertNotIn("Чужое место", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class EscapingTests(unittest.TestCase):
    @scenario_test
    async def test_dangerous_place_name_never_breaks_html_on_the_path(self, h):
        place_id = _seed_place(h, name=DANGEROUS)
        dangerous_tpl = "Шаблон <b> & «Кавычки»"
        template_id = _seed_personal(h, name=dangerous_tpl)

        await h.text("/start")
        seen = [h.screen_text()]

        await h.callback("pick_place")
        seen.append(h.screen_text())
        await h.callback(f"pl_pick:{place_id}")
        seen.append(h.screen_text())
        await _skip_all_questions(h)
        seen.append(h.screen_text())

        await h.callback("main_menu")
        await h.callback("my_announcements")
        seen.append(h.screen_text())

        await h.callback("main_menu")
        await h.callback("pick_template")
        seen.append(h.screen_text())
        await h.callback(f"tp_pick:{template_id}")
        seen.append(h.screen_text())

        for text in seen:
            self.assertNotIn("<A>", text, text)
            self.assertNotIn("<b>", text, text)
            self.assertNotIn("<script>", text, text)
        joined = "\n".join(seen)
        self.assertIn("&lt;A&gt;", joined)
        self.assertIn("&amp;", joined)
        self.assertIn("&lt;b&gt;", joined)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LongNameTests(unittest.TestCase):
    @scenario_test
    async def test_long_names_do_not_break_buttons(self, h):
        long_place = ("Очень длинное название площадки " * 3)[:70]
        long_tpl = ("Очень длинное имя шаблона " * 3)[:70]
        place_id = _seed_place(h, name=long_place)
        template_id = _seed_personal(h, name=long_tpl)

        await h.text("/start")
        await h.callback("pick_place")
        for label, data in _btns(h):
            self.assertLessEqual(
                len(data.encode()), 64, f"callback_data > 64 байт: {data!r}"
            )
            self.assertLessEqual(len(label), 64, f"подпись > 64: {label!r}")
        await h.callback(f"pl_pick:{place_id}")
        self.assertIn("🧱 Вопрос 1", h.screen_text())

        await h.callback("main_menu")
        await h.callback("pick_template")
        for label, data in _btns(h):
            self.assertLessEqual(len(data.encode()), 64)
            self.assertLessEqual(len(label), 64, f"подпись > 64: {label!r}")
        await h.callback(f"tp_pick:{template_id}")
        self.assertIn("🧱 Вопрос 1", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExistingUserBackfillTests(unittest.TestCase):
    """
    План S1 п.7 / S2 п.3: у существующего пользователя (строка `users` уже
    есть, например после передачи прав или из прежней версии продукта)
    стартовый личный шаблон должен появиться при /start; у нового — уже есть.
    """

    @scenario_test
    async def test_preexisting_user_gets_starter_template_on_start(self, h):
        preexisting = 8_950_010
        with h.session_factory() as session:
            get_or_create_user(session, preexisting, username="old-user")
            session.commit()

        await h.text("/start", user_id=preexisting)
        await h.callback("pick_template", user_id=preexisting)

        self.assertEqual(
            len(_personal(h, preexisting)),
            1,
            "существующий пользователь остался без стартового шаблона: "
            f"экран = {h.screen_text()!r}",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalPlaceholderTests(unittest.TestCase):
    """
    Дефект S2: у анонса по личному шаблону площадки нет, но экраны печатают
    «Место: » с пустым значением (обзор и экран после отправки).

    Ожидание: строка про место либо отсутствует, либо содержит осмысленное
    значение. Воспроизведение — см. тест ниже (падает).
    """

    @scenario_test
    async def test_personal_screens_have_no_empty_place_line(self, h):
        with h.session_factory() as session:
            from brick_store import create_personal_brick

            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="title",
                title="Название",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Без места",
                header="",
                body="Постоянный текст [title]",
                footer="",
            )
            session.commit()
            template_id = template.template_id

        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Значение")
        # S3 п.6: сначала шаг фото, доставка — по «Без фотографий».
        await h.callback(_cb(h, "aw_photo_none"))
        delivered = h.screen_text()
        self.assertIn("Готовый анонс отправлен", delivered)

        await h.callback(_cb(h, "aw_preview"))
        preview = h.screen_text()
        self.assertIn("Обзор анонса", preview)

        for name, text in (("обзор", preview), ("после отправки", delivered)):
            empty = [line for line in text.splitlines() if line.strip() == "Место:"]
            self.assertEqual(
                empty,
                [],
                f"{name}: пустая строка «Место:» у анонса без площадки:\n{text}",
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PersonalNoPlaceAllScreensTests(unittest.TestCase):
    """
    Фикс «пустое Место:» проверяется на ВСЕХ пяти экранах личного анонса
    (выбор шаблона недоступен личному анонсу — там место есть по схеме):
    обзор, статус отправки, доставлено, отказ в неличном чате.
    """

    def _seed(self, h):
        from brick_store import create_personal_brick

        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="u")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="title",
                title="Название",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Без места",
                header="",
                body="Постоянный текст [title]",
                footer="",
            )
            session.commit()
            return template.template_id

    @staticmethod
    def _no_empty_place(case, name, text):
        empty = [line for line in text.splitlines() if line.strip() == "Место:"]
        case.assertEqual(empty, [], f"{name}: пустая «Место:»:\n{text}")
        case.assertNotIn("Место: \n", text, name)

    @scenario_test
    async def test_publish_status_screen_has_no_empty_place(self, h):
        from datetime import date

        from announcement_store import (
            begin_announcement_publication,
            get_announcement_draft,
        )

        template_id = self._seed(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Значение")
        draft = _drafts(h)[0]
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
            )
            begin_announcement_publication(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
                expected_revision=details.revision,
                today=date.today(),
            )

        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.draft_id}")
        status = h.screen_text()
        self.assertIn("Отправка готового анонса", status)
        self._no_empty_place(self, "статус отправки", status)

    @scenario_test
    async def test_private_chat_refusal_has_no_empty_place(self, h):
        group = {"chat_id": -1003, "chat_type": "group"}
        template_id = self._seed(h)
        await h.text("/start", **group)
        await h.callback("pick_template", **group)
        await h.callback(f"tp_pick:{template_id}", **group)
        await h.text("Значение", **group)
        # S3 п.6: отказ в неличном чате — на финале шага фото.
        self.assertIn("Фотографии анонса", h.screen_text())
        await h.callback(_cb(h, "aw_photo_none"), **group)

        refusal = h.screen_text()
        self.assertIn("Готовый анонс не отправлен", refusal)
        self._no_empty_place(self, "отказ в неличном чате", refusal)

    @scenario_test
    async def test_place_flow_still_prints_place(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback(f"pl_pick:{place_id}")
        await _skip_all_questions(h)
        delivered = h.screen_text()
        self.assertIn("Место: Дом культуры", delivered)

        await h.callback(_cb(h, "aw_preview"))
        self.assertIn("Место: Дом культуры", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class StarterTemplateBackfillTests(unittest.TestCase):
    """Граничные случаи выдачи стартового шаблона (фикс 2)."""

    PRE = 8_950_020
    ANON = 8_950_021
    NOBODY = 8_950_022

    @scenario_test
    async def test_preexisting_user_gets_exactly_one_on_start(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, self.PRE, username="old")
            session.commit()

        await h.text("/start", user_id=self.PRE)
        self.assertEqual(len(_personal(h, self.PRE)), 1)
        with h.session_factory() as session:
            snapshots = session.scalar(
                select(func.count())
                .select_from(TemplateSnapshot)
                .where(TemplateSnapshot.source_kind == "personal")
            )
        self.assertEqual(snapshots, 1, "метка-снимок выдаётся один раз")

    @scenario_test
    async def test_repeated_start_does_not_duplicate_starter(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, self.PRE, username="old")
            session.commit()

        for _ in range(3):
            await h.text("/start", user_id=self.PRE)
        self.assertEqual(len(_personal(h, self.PRE)), 1)
        with h.session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(TemplateSnapshot)
                    .where(TemplateSnapshot.source_kind == "personal")
                ),
                1,
            )

    @scenario_test
    async def test_start_with_payload_also_grants_starter(self, h):
        payload_user = 8_950_023
        with h.session_factory() as session:
            get_or_create_user(session, payload_user, username="payload")
            session.commit()

        await h.text("/start tf_somepayload", user_id=payload_user)
        self.assertEqual(len(_personal(h, payload_user)), 1)

    @scenario_test
    async def test_non_start_entry_does_not_grant_starter(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, self.NOBODY, username="quiet")
            session.commit()

        await h.text("/templates", user_id=self.NOBODY)
        await h.callback("pick_template", user_id=self.NOBODY)
        await h.text("случайный текст", user_id=self.NOBODY)
        self.assertEqual(
            len(_personal(h, self.NOBODY)), 0, "вне /start стартовый шаблон не выдаётся"
        )

    @scenario_test
    async def test_personal_announcement_without_templates_blocks_new_starter(self, h):
        template_id = _seed_personal(h, actor_tg=self.ANON)
        await h.text("/start", user_id=self.ANON)
        await h.callback(f"tp_pick:{template_id}", user_id=self.ANON)
        await h.text("Значение", user_id=self.ANON)
        self.assertTrue(_drafts(h, self.ANON))

        # шаблон удалён пользователем; снимки/анонс остаются как след истории
        _delete_all_personal(h, self.ANON)
        self.assertEqual(len(_personal(h, self.ANON)), 0)

        await h.text("/start", user_id=self.ANON)
        self.assertEqual(
            len(_personal(h, self.ANON)),
            0,
            "история личного анонса не даёт выдать новый стартовый шаблон",
        )

    @scenario_test
    async def test_service_is_idempotent_on_repeat(self, h):
        with h.session_factory() as session:
            get_or_create_user(session, self.PRE, username="old")
            session.commit()
        await h.text("/start", user_id=self.PRE)

        with h.session_factory() as session:
            first = ensure_initial_personal_template(
                session, actor_telegram_user_id=self.PRE
            )
            second = ensure_initial_personal_template(
                session, actor_telegram_user_id=self.PRE
            )
        self.assertEqual(first.template_id, second.template_id)
        self.assertEqual(len(_personal(h, self.PRE)), 1)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MarkerSnapshotProductionPragmaTests(unittest.TestCase):
    """
    Разведка риска: метка-снимок ссылается на версию шаблона через
    RESTRICT-внешний ключ. При включённых production-PRAGMA (foreign_keys=ON)
    удаление шаблона, у которого есть снимок, физически невозможно.

    Тест фиксирует факт, а не требование: продукт сейчас не имеет сервиса
    удаления, но критерий S2 «намеренное удаление» им затрагивается.
    """

    @scenario_test
    async def test_production_fk_blocks_template_delete(self, h):
        from database import apply_sqlite_pragmas

        h.engine.dispose()
        event.listen(h.engine, "connect", apply_sqlite_pragmas)
        await h.text("/start")

        with h.session_factory() as session:
            self.assertEqual(session.scalar(sql_text("PRAGMA foreign_keys")), 1)
            user = _user(session)
            template_id = session.scalar(
                select(Template.id).where(
                    Template.kind == "personal", Template.owner_user_id == user.id
                )
            )
            with self.assertRaises(IntegrityError):
                session.execute(delete(Template).where(Template.id == template_id))
                session.commit()
