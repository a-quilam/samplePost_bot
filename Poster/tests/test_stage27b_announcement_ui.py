# tests/test_stage27b_announcement_ui.py
"""
Stage 27B: интеграционные сценарии Telegram-интерфейса адаптивного мастера.

Обновления проходят через настоящее приложение `build_application`, как в
Stage 9: проверяются меню, ConversationHandler, экраны, callback-кнопки и
сохранение в SQLite. Telegram и реальная `post_bot.db` не используются.
"""

from __future__ import annotations

import unittest
from datetime import date

from sqlalchemy import func, select

try:
    from announcement_store import (
        FIELD_STATUS_KNOWN,
        FIELD_STATUS_VALID,
        build_announcement_fields,
        choose_announcement_template,
        confirm_announcement_place,
        get_announcement_draft,
        start_announcement_wizard,
    )
    from announcement_wizard import WizardStage
    from brick_store import archive_personal_brick, create_personal_brick
    from handlers.announcement_wizard import (
        AW_EXTRAS,
        AW_PLACE,
        AW_PREVIEW,
        AW_QUESTIONS,
        CONVERSATION_NAME,
        PREVIEW_VISIBLE_LIMIT,
    )
    from models import (
        AnnouncementDraft,
        AnnouncementValue,
        Place,
        PlaceAdmin,
        TemplateSnapshot,
        User,
        UserState,
        utcnow,
    )
    from place_store import add_alternative_name, create_place
    from template_store import (
        create_personal_template,
        get_standard_template,
        update_template_section,
    )
    from template_types import TEMPLATE_SECTION_BODY
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OTHER_TG = 8_270_002
TemplateSpec = tuple[str, str, str, dict[str, object]]


def _callbacks(h, prefix: str, suffix: str | None = None) -> list[str]:
    found = []
    for _label, data in h.screen_buttons():
        if (
            data
            and data.startswith(prefix)
            and (suffix is None or data.endswith(suffix))
        ):
            found.append(data)
    return found


def _callback(h, prefix: str, suffix: str | None = None) -> str:
    found = _callbacks(h, prefix, suffix)
    if not found:
        raise AssertionError(
            f"Нет callback {prefix!r} ({suffix!r}); есть: {h.screen_buttons()}"
        )
    return found[0]


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


def _draft_by_id(h, draft_id: int):
    with h.session_factory() as session:
        return session.get(AnnouncementDraft, draft_id)


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    address: str | None = "ул. Тестовая, 1",
    url: str | None = "https://example.com/place",
    alias: str | None = None,
):
    with h.session_factory() as session:
        user = _user(session)
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address=address,
            url=url,
        )
        session.add(
            PlaceAdmin(
                place_id=place.id,
                user_id=user.id,
                assigned_by_user_id=None,
                assigned_at=utcnow(),
            )
        )
        alias_row = None
        if alias is not None:
            alias_row = add_alternative_name(
                session,
                place.id,
                alias,
                created_by_user_id=user.id,
            )
        session.commit()
        return (
            place.id,
            alias_row.name_id if alias_row is not None else None,
        )


def _seed_personal_template(
    h,
    *,
    name: str = "Личный шаблон",
    specs: list[TemplateSpec] | None = None,
    body: str | None = None,
    actor_tg: int = DEFAULT_USER_ID,
):
    specs = specs or [("main", "Основное поле", "short_text", {})]
    with h.session_factory() as session:
        get_or_create_user(session, actor_tg, username=f"user-{actor_tg}")
        bricks: dict[str, int] = {}
        for code, title, value_type, rules in specs:
            definition = create_personal_brick(
                session,
                actor_telegram_user_id=actor_tg,
                code=code,
                title=title,
                value_type=value_type,
                rules=rules,
            )
            bricks[code] = definition.id
        if body is None:
            body = " ".join(f"[{code}]" for code, _title, _type, _rules in specs)
        template = create_personal_template(
            session,
            actor_telegram_user_id=actor_tg,
            name=name,
            header="",
            body=body,
            footer="",
        )
        session.commit()
        return template.template_id, template.revision, bricks


async def _open_new(h) -> None:
    await h.callback("aw_start")
    await h.callback("aw_new")


async def _open_place(h, place_id: int, query: str = "Дом") -> None:
    await _open_new(h)
    await h.callback("aw_place_search")
    await h.text(query)
    await h.callback(f"aw_place_open:{place_id}")


async def _confirm_place(h) -> None:
    await h.callback(_callback(h, "aw_place_confirm"))


async def _start_to_template(h, place_id: int, query: str = "Дом") -> None:
    await _open_place(h, place_id, query)
    await _confirm_place(h)
    if "Шаг 2. Выберите шаблон" not in h.screen_text():
        raise AssertionError(f"Не экран шаблона: {h.screen_text()}")


async def _choose_personal(h, template_id: int) -> None:
    await h.callback(_callback(h, "aw_template", f":{template_id}"))


async def _no_photos(h) -> None:
    """
    Завершает шаг фотографий (Poster 2.0 S3 п.6).

    Последний кирпичик ведёт на шаг фото, а не к доставке; «Без фотографий»
    — единственный финал этого шага (кнопки «Готово» нет). Доставка как
    функция не менялась: её покрытие остаётся в тестах ниже.
    """
    if "Фотографии анонса" not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()}")
    await h.callback(_callback(h, "aw_photo_none"))


async def _preview(h) -> None:
    """
    Открывает обзор, если он есть.

    Stage 28: последний ответ сразу доставляет анонс, поэтому обзора может
    уже не быть — тогда экран управления доставкой и есть актуальный экран.
    """
    data = _callbacks(h, "aw_preview")
    if data:
        await h.callback(data[0])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardMenuTests(unittest.TestCase):
    @scenario_test
    async def test_new_menu_entry_and_removed_legacy_wizard(self, h) -> None:
        await h.text("/start")
        buttons = h.screen_buttons()
        # Poster 2.0 S2: в меню ровно пять кнопок, старых входов нет.
        self.assertEqual(
            [label for label, _ in buttons],
            [
                "📍 Создать для площадки",
                "📝 Создать по личному шаблону",
                "📂 Мои анонсы",
                "⚙️ Управление",
                "❓ Помощь",
            ],
        )
        self.assertNotIn("aw_start", [data for _, data in buttons])
        self.assertNotIn("create_post", [data for _, data in buttons])

        # Новый мастер по-прежнему доступен командой /new_wizard.
        await h.text("/new_wizard")
        self.assertIn("📂 Мои анонсы", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PLACE)

        # Poster 2.0 S5: легаси-мастер удалён. /create_post больше не
        # запускает диалог и не пишет WIP в user_states — команда неизвестна.
        await h.callback("main_menu")
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        with h.session_factory() as session:
            state = session.get(UserState, DEFAULT_USER_ID)
            payload_before = state.payload if state is not None else None
        await h.text("/create_post")
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertIsNone(h.dialog_state(), "легаси-диалог не запустился")
        with h.session_factory() as session:
            state = session.get(UserState, DEFAULT_USER_ID)
            payload_after = state.payload if state is not None else None
        self.assertEqual(payload_after, payload_before, "WIP не создаётся")

        # Единственный мастер по-прежнему открывается командой /new_wizard.
        await h.text("/new_wizard")
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PLACE)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardPlaceTests(unittest.TestCase):
    @scenario_test
    async def test_place_search_pagination_alias_back_and_storage(self, h) -> None:
        await h.text("/start")
        target_id, alias_id = _seed_place(h, alias="ДК «Тестовый»")
        with h.session_factory() as session:
            user = _user(session)
            for number in range(1, 10):
                create_place(
                    session,
                    f"Тестовое место {number}",
                    city="Тестовый город",
                    created_by_user_id=user.id,
                )
            session.commit()
            places_before = session.scalar(select(func.count(Place.id)))

        await _open_new(h)
        await h.callback("aw_place_all:0")
        self.assertIn("Всего: 10", h.screen_text())
        self.assertIn("Страница 1", h.screen_text())
        await h.callback(_callback(h, "aw_place_all", ":1"))
        self.assertIn("Страница 2", h.screen_text())
        await h.callback("aw_place_back")
        self.assertIn("Шаг 1. Выберите место", h.screen_text())

        await h.callback("aw_place_search")
        await h.text("Тестовое")
        self.assertIn("Найдено мест: 9", h.screen_text())
        await h.callback(_callback(h, "aw_place_results", ":1"))
        self.assertEqual(len(_callbacks(h, "aw_place_open")), 1)
        await h.callback("aw_place_back")

        await h.callback("aw_place_search")
        await h.text("ДК")
        self.assertIn("Дом культуры", h.screen_text())
        self.assertIn("совпадение: ДК «Тестовый»", h.screen_text())
        await h.callback(f"aw_place_open:{target_id}")
        self.assertIn("Проверьте место", h.screen_text())
        self.assertIn("Известные адрес и ссылка", h.screen_text())

        await h.callback("aw_place_back")
        self.assertIn("Результаты поиска", h.screen_text())
        await h.callback(f"aw_place_open:{target_id}")
        await h.callback(_callback(h, "aw_place_names"))
        self.assertTrue(
            any("ДК «Тестовый»" in label for label, _data in h.screen_buttons())
        )
        self.assertIn("Это не создаёт новое место", h.screen_text())
        await h.callback(_callback(h, "aw_place_name_pick", f":{alias_id}:0"))
        self.assertIn("ДК «Тестовый» (альтернативное)", h.screen_text())
        await _confirm_place(h)
        self.assertIn("Шаг 2. Выберите шаблон", h.screen_text())

        draft = _latest_draft(h)
        self.assertEqual(draft.place_id, target_id)
        self.assertEqual(draft.place_name_id, alias_id)
        self.assertEqual(draft.place_display_name, "ДК «Тестовый»")
        with h.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count(Place.id))), places_before
            )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardTemplateTests(unittest.TestCase):
    @scenario_test
    async def test_templates_are_isolated_and_snapshot_is_stable(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        owner_tid, owner_revision, owner_bricks = _seed_personal_template(
            h,
            name="Мой шаблон",
            body="СТАРЫЙ ТЕКСТ [main]",
        )
        foreign_tid, _foreign_revision, _foreign_bricks = _seed_personal_template(
            h,
            name="Чужой шаблон",
            body="ЧУЖОЙ [main]",
            actor_tg=OTHER_TG,
        )
        main_brick_id = owner_bricks["main"]

        await _start_to_template(h, place_id)
        text = h.screen_text()
        self.assertIn("Мой шаблон", text)
        self.assertNotIn("Чужой шаблон", text)
        self.assertFalse(
            any(data.endswith(f":{foreign_tid}") for _l, data in h.screen_buttons())
        )

        owner_callback = _callback(h, "aw_template", f":{owner_tid}")
        parts = owner_callback.split(":")
        draft_id, revision = int(parts[1]), int(parts[2])
        await h.callback(f"aw_template:{draft_id}:{revision}:{foreign_tid}")
        self.assertIn("недоступен", h.screen_text())
        draft = _draft_by_id(h, draft_id)
        self.assertIsNone(draft.template_snapshot_id)

        await h.callback(owner_callback)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS)
        draft = _draft_by_id(h, draft_id)
        snapshot_id = draft.template_snapshot_id
        self.assertIsNotNone(snapshot_id)

        # Текущая версия исходного шаблона меняется, снимок — нет.
        with h.session_factory() as session:
            update_template_section(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=owner_tid,
                expected_revision=owner_revision,
                section=TEMPLATE_SECTION_BODY,
                content="НОВЫЙ ТЕКСТ [main]",
            )

        await h.text("Значение")
        # Poster 2.0 S3 п.6: последний кирпичик ведёт на шаг фотографий.
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_photo_none"))
        self.assertFalse(_callbacks(h, "aw_send"))
        await _no_photos(h)
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        delivered = h.posts()[-1] or ""
        self.assertIn("СТАРЫЙ ТЕКСТ", delivered)
        self.assertNotIn("НОВЫЙ ТЕКСТ", delivered)
        draft = _draft_by_id(h, draft_id)
        self.assertEqual(draft.template_snapshot_id, snapshot_id)
        self.assertEqual(draft.template_id, owner_tid)
        self.assertEqual(draft.stage, WizardStage.COMPLETED.value)

        # Значение привязано к brick_id снимка.
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft_id,
                today=date.today(),
            )
        self.assertEqual(
            [field.raw_value for field in fields if field.brick_id == main_brick_id],
            ["Значение"],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardQuestionTests(unittest.TestCase):
    @scenario_test
    async def test_all_seven_types_calendar_validation_and_preview(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        specs: list[TemplateSpec] = [
            ("short", "Короткое поле", "short_text", {}),
            ("long", "Длинное поле", "long_text", {}),
            ("when", "Дата события", "date", {}),
            ("at", "Время начала", "time", {}),
            ("site", "Ссылка", "url", {}),
            ("num", "Цена", "number", {"min": 0}),
            ("cat", "Категория", "choice", {"choices": ["Музыка", "Театр"]}),
            (
                "tags",
                "Теги",
                "choice",
                {"choices": ["X", "Y", "Z"], "multiple": True},
            ),
        ]
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="Все типы",
            specs=specs,
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        self.assertIn("Вопрос 1 из 8", h.screen_text())
        self.assertIn("Ожидается короткий текст", h.screen_text())
        self.assertIn("Поле необязательное", h.screen_text())
        self.assertIn("Значение пока не задано", h.screen_text())
        await h.text("Коротко")

        self.assertIn("Вопрос 2 из 8", h.screen_text())
        await h.text("Длинный текст")

        self.assertIn("Вопрос 3 из 8", h.screen_text())
        self.assertIn("Выберите дату календарём", h.screen_text())
        # U2a: компактный календарь уже на вопросе — быстрые даты и месяцы.
        self.assertTrue(_callback(h, "aw_calday"))
        self.assertTrue(_callback(h, "aw_calmonths"))
        day_callback = _callback(h, "aw_calday")
        chosen_day = date.fromisoformat(day_callback.rsplit(":", 1)[-1])
        await h.callback(day_callback)

        self.assertIn("Вопрос 4 из 8", h.screen_text())
        await h.text("18:30")

        self.assertIn("Вопрос 5 из 8", h.screen_text())
        invalid_url = await h.text("not-a-url")
        self.assertIn("⚠️", h.screen_text())
        self.assertIn("Вопрос 5 из 8", h.screen_text())
        self.assertNotIn(invalid_url.effective_message.message_id, h.deleted_ids())
        await h.text("example.com/event")

        self.assertIn("Вопрос 6 из 8", h.screen_text())
        invalid_number = await h.text("много")
        self.assertIn("⚠️", h.screen_text())
        self.assertIn("Вопрос 6 из 8", h.screen_text())
        self.assertNotIn(invalid_number.effective_message.message_id, h.deleted_ids())
        await h.text("42")

        self.assertIn("Вопрос 7 из 8", h.screen_text())
        await h.callback(_callback(h, "aw_choice", ":0"))
        self.assertIn("Вопрос 8 из 8", h.screen_text())
        await h.callback(_callback(h, "aw_choice", ":0"))
        await h.callback(_callback(h, "aw_choice", ":1"))
        await h.callback(_callback(h, "aw_choice_done"))
        # Poster 2.0 S3 п.6: после последнего кирпичика — шаг фото.
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_photo_none"))
        self.assertFalse(_callbacks(h, "aw_send"))
        self.assertEqual(h.posts(), [])
        await _no_photos(h)
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        posts_after_delivery = h.posts()
        self.assertEqual(len(posts_after_delivery), 1)
        text = posts_after_delivery[0] or ""
        self.assertIn("Коротко", text)
        self.assertIn("Длинный текст", text)
        self.assertIn(chosen_day.strftime("%d.%m.%Y"), text)
        self.assertIn("18:30", text)
        self.assertIn("example.com/event", text)
        self.assertIn("42", text)
        self.assertIn("Музыка", text)
        self.assertIn("X, Y", text)
        self.assertTrue(_callbacks(h, "aw_qmanage"))
        # Финальной кнопки «отправить/сформировать» в доставке нет.
        self.assertFalse(_callbacks(h, "aw_send"))
        self.assertFalse(_callbacks(h, "aw_apply"))

        await h.callback(_callback(h, "aw_preview"))
        self.assertIn("Анонс готов", h.screen_text())
        await h.callback(_callback(h, "aw_save"))
        self.assertIn("Черновик сохранён", h.screen_text())
        self.assertEqual(h.posts(), posts_after_delivery)

        draft = _latest_draft(h)
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        values = {field.code: field.raw_value for field in fields}
        self.assertEqual(values["short"], "Коротко")
        self.assertEqual(values["long"], "Длинный текст")
        self.assertEqual(values["when"], chosen_day.strftime("%d.%m.%Y"))
        self.assertEqual(values["at"], "18:30")
        self.assertEqual(values["site"], "https://example.com/event")
        self.assertEqual(values["num"], 42)
        self.assertEqual(values["cat"], "Музыка")
        tags = values["tags"]
        self.assertIsInstance(tags, list)
        assert isinstance(tags, list)
        self.assertEqual(set(tags), {"X", "Y"})
        self.assertTrue(all(field.status == FIELD_STATUS_VALID for field in fields))
        # Stage 28: последний ответ сразу доставил анонс.
        self.assertEqual(draft.stage, WizardStage.COMPLETED.value)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardExtrasTests(unittest.TestCase):
    @scenario_test
    async def test_extra_pagination_sections_value_preview_edit_delete(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        specs: list[TemplateSpec] = [("main", "Основное", "short_text", {})]
        specs.extend(
            (f"extra{number}", f"Доп {number}", "short_text", {}) for number in range(8)
        )
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="С дополнительными",
            specs=specs,
            body="[main]",
        )
        extra_id = bricks["extra0"]

        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное")
        # Poster 2.0 S3 п.6: последний ответ ведёт на шаг фото; extras —
        # уже после доставки, с экрана управления.
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertEqual(h.posts(), [])
        await _no_photos(h)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        self.assertEqual(len(h.posts()), 1)
        await h.callback(_callback(h, "aw_extras"))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_EXTRAS)
        self.assertIn("Дополнительных кирпичиков пока нет", h.screen_text())

        await h.callback(_callback(h, "aw_exadd"))
        found = False
        for _attempt in range(4):
            if _callbacks(h, "aw_exbrick", f":{extra_id}"):
                found = True
                break
            labels = h.screen_buttons()
            next_page = [data for label, data in labels if label == "▶️" and data]
            if not next_page:
                break
            await h.callback(next_page[0])
        self.assertTrue(found, f"Личный кирпичик не найден: {h.screen_buttons()}")
        await h.callback(_callback(h, "aw_exbrick", f":{extra_id}"))
        self.assertIn("Выберите секцию", h.screen_text())
        await h.callback(_callback(h, "aw_exsec", ":body"))
        self.assertIn("Выберите положение", h.screen_text())
        await h.callback(_callback(h, "aw_expos", ":start"))
        self.assertIn("Значение пока не задано", h.screen_text())
        await h.text("Дополнительно")
        self.assertIn("Доп 0", h.screen_text())
        self.assertIn("Дополнительно", h.screen_text())

        first_draft_id = _latest_draft(h).id
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first_draft_id,
            )
        snapshot = details.template_snapshot
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.sections[TEMPLATE_SECTION_BODY], "[main]")
        self.assertEqual(len(details.extras), 1)
        self.assertEqual(details.extras[0].brick_id, extra_id)

        # Второй анонс по тому же шаблону не получает чужое доп. поле.
        with h.session_factory() as session:
            second = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                place_id=place_id,
            )
            second = confirm_announcement_place(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=second.draft_id,
                expected_revision=second.revision,
            )
            second = choose_announcement_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=second.draft_id,
                template_id=template_id,
                expected_revision=second.revision,
            )
            second = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=second.draft_id,
            )
            self.assertEqual(second.extras, ())

        # Stage 28: правки доп. поля применяются явной кнопкой и заменяют
        # доставленную версию.
        await h.callback(_callback(h, "aw_apply"))
        self.assertIn("Дополнительно", h.posts()[-1] or "")
        await _preview(h)
        self.assertIn("Дополнительно", h.screen_text())
        await h.callback(_callback(h, "aw_panel"))
        await h.callback(_callback(h, "aw_extras"))
        self.assertIn("Доп 0", h.screen_text())
        await h.callback(_callback(h, "aw_exedit"))
        self.assertIn("Дополнительно", h.screen_text())
        await h.callback(_callback(h, "aw_exskip"))
        self.assertIn("пропущено", h.screen_text())
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first_draft_id,
            )
            value = details.value_for(details.extras[0].brick_id)
            self.assertIsNotNone(value)
            assert value is not None
            self.assertTrue(value.is_empty_optional)
        await h.callback(_callback(h, "aw_exdel"))
        self.assertIn("Дополнительных кирпичиков пока нет", h.screen_text())
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=first_draft_id,
            )
            self.assertEqual(details.extras, ())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardPersistenceTests(unittest.TestCase):
    @scenario_test
    async def test_edit_save_restart_resume_and_no_snapshot_recreation(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Простой",
            body="Постоянный текст [main]",
        )

        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Исходное")
        # S3 п.6: последний кирпичик ведёт на шаг фото; доставка — по
        # «Без фотографий».
        await _no_photos(h)
        self.assertIn("Исходное", h.posts()[-1] or "")

        await h.callback(_callback(h, "aw_qmanage"))
        self.assertIn("Поля анонса", h.screen_text())
        self.assertTrue(
            any("заполнено" in label for label, _data in h.screen_buttons())
        )
        await h.callback(_callback(h, "aw_qgoto"))
        self.assertIn("Исходное", h.screen_text())
        await h.callback(_callback(h, "aw_qedit"))
        self.assertIn("Текущее значение: Исходное", h.screen_text())
        await h.text("Новое")
        # Правка доставленного анонса применяется явной кнопкой.
        self.assertIn("Применить изменения", str(h.screen_buttons()))
        await h.callback(_callback(h, "aw_apply"))
        self.assertIn("Новое", h.posts()[-1] or "")

        # Обзор открывается для проверки и не отправляет ещё одну копию.
        posts_after_apply = h.posts()
        await h.callback(_callback(h, "aw_preview"))
        self.assertIn("Новое", h.screen_text())
        self.assertEqual(h.posts(), posts_after_apply)

        await h.callback(_callback(h, "aw_save"))
        self.assertIn("Черновик сохранён", h.screen_text())
        posts_after_save = h.posts()
        self.assertEqual(posts_after_save, posts_after_apply)
        draft = _latest_draft(h)
        draft_id = draft.id
        snapshot_id = draft.template_snapshot_id
        self.assertEqual(draft.stage, WizardStage.COMPLETED.value)
        with h.session_factory() as session:
            snapshots_before = session.scalar(select(func.count(TemplateSnapshot.id)))

        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        self.assertIn("Незавершённые анонсы", h.screen_text())
        self.assertIn("Дом культуры", h.screen_text())
        await h.callback(_callback(h, "aw_open", f":{draft_id}"))
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        # Повторное открытие не отправляет ещё одну копию.
        self.assertEqual(h.posts(), posts_after_save)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

        resumed = _draft_by_id(h, draft_id)
        self.assertEqual(resumed.template_snapshot_id, snapshot_id)
        self.assertEqual(resumed.stage, WizardStage.COMPLETED.value)
        with h.session_factory() as session:
            snapshots_after = session.scalar(select(func.count(TemplateSnapshot.id)))
        self.assertEqual(snapshots_after, snapshots_before)

        # Выход в меню не удаляет сохранённый черновик нового мастера.
        await h.callback("main_menu")
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertIsNotNone(_draft_by_id(h, draft_id))
        # Выход в меню не отправляет и не удаляет доставленный анонс.
        self.assertEqual(h.posts(), posts_after_save)

    @scenario_test
    async def test_long_preview_is_truncated_but_sqlite_keeps_full_value(
        self, h
    ) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Длинный",
            specs=[("main", "Описание", "long_text", {})],
        )
        long_value = "А" * (PREVIEW_VISIBLE_LIMIT + 350)

        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text(long_value)
        # S3 п.6: сначала шаг фото, доставка — по «Без фотографий».
        await _no_photos(h)
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        await _preview(h)
        self.assertIn("Предпросмотр сокращён", h.screen_text())
        self.assertIn("Полные данные сохранены", h.screen_text())
        self.assertNotIn(long_value, h.screen_text())

        draft = _latest_draft(h)
        with h.session_factory() as session:
            row = session.scalar(
                select(AnnouncementValue).where(AnnouncementValue.draft_id == draft.id)
            )
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.value_json)
        self.assertGreater(len(row.value_json), PREVIEW_VISIBLE_LIMIT)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardSafetyTests(unittest.TestCase):
    @scenario_test
    async def test_stale_and_repeated_callbacks_do_not_overwrite(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        stale_skip = _callback(h, "aw_qskip")
        await h.text("Значение")
        # S3 п.6: сначала шаг фотографий, затем доставка.
        await _no_photos(h)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)
        self.assertEqual(len(h.posts()), 1)
        draft = _latest_draft(h)
        revision_after_save = draft.revision

        await h.callback(stale_skip)
        self.assertIn("Экран устарел", h.screen_text())
        self.assertIn(
            "➕ Дополнительные кирпичики",
            [label for label, _data in h.screen_buttons()],
        )
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        self.assertEqual(fields[0].status, FIELD_STATUS_VALID)
        self.assertEqual(fields[0].raw_value, "Значение")
        self.assertEqual(_draft_by_id(h, draft.id).revision, revision_after_save)

        # Повторное нажатие того же устаревшего callback ничего не меняет.
        await h.callback(stale_skip)
        self.assertIn("Экран устарел", h.screen_text())
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        self.assertEqual(fields[0].raw_value, "Значение")
        self.assertEqual(_draft_by_id(h, draft.id).revision, revision_after_save)

    @scenario_test
    async def test_foreign_draft_id_is_not_accessible(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        with h.session_factory() as session:
            other = get_or_create_user(session, OTHER_TG, username="foreign-owner")
            foreign = start_announcement_wizard(
                session,
                actor_telegram_user_id=OTHER_TG,
                place_id=place_id,
            )
            foreign_id = foreign.draft_id
            self.assertEqual(other.telegram_user_id, OTHER_TG)

        await h.callback("aw_start")
        self.assertNotIn("чужой анонс", h.screen_text().lower())
        await h.callback(f"aw_open:{foreign_id}")
        self.assertIn("не найден или недоступен", h.screen_text())
        self.assertIn("📂 Мои анонсы", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PLACE)
        self.assertIsNone(_latest_draft(h))

    @scenario_test
    async def test_preview_error_keeps_draft_and_offers_current_retry(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        await _no_photos(h)

        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                brick_id=bricks["main"],
                expected_revision=1,
            )

        await _preview(h)
        self.assertIn("Не удалось собрать предварительный текст", h.screen_text())
        self.assertIn("Черновик сохранён, данные не потеряны", h.screen_text())
        self.assertIn("Обновить", h.screen_text() + str(h.screen_buttons()))
        draft = _latest_draft(h)
        # Анонс уже доставлен: сбой пересборки не откатывает готовый статус.
        self.assertEqual(draft.stage, WizardStage.COMPLETED.value)
        self.assertIsNotNone(draft.template_snapshot_id)

        retry = _callback(h, "aw_apply")
        self.assertIn(f":{draft.id}:{draft.revision}", retry)
        await h.callback(retry)
        self.assertIn("Не удалось собрать", h.screen_text())
        self.assertNotIn("Экран устарел", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementWizardStandardTemplateTests(unittest.TestCase):
    @scenario_test
    async def test_standard_uses_known_place_values_and_skips_questions(
        self, h
    ) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        with h.session_factory() as session:
            standard_id = get_standard_template(
                session, DEFAULT_USER_ID, place_id
            ).template_id

        await _start_to_template(h, place_id)
        await h.callback(_callback(h, "aw_template", f":{standard_id}"))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS)
        draft = _latest_draft(h)
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=date.today(),
            )
        known = {
            field.code: field.raw_value for field in fields if field.status == "known"
        }
        self.assertEqual(known["место"], "Дом культуры")
        self.assertEqual(known["адрес"], "ул. Тестовая, 1")
        self.assertEqual(known["ссылка_места"], "https://example.com/place")
        askable = [field for field in fields if field.status != FIELD_STATUS_KNOWN]

        # Назад из первого вопроса показывает выбранный снимок, а не выбор.
        await h.callback(_callback(h, "aw_qback"))
        self.assertIn("Выбранный шаблон", h.screen_text())
        self.assertFalse(_callbacks(h, "aw_template"))
        await h.callback(_callback(h, "aw_cont"))
        for _field in askable:
            self.assertNotIn("Название места", h.screen_text())
            self.assertIn("Вопрос", h.screen_text())
            self.assertIn("Пропустить", str(h.screen_buttons()))
            await h.callback(_callback(h, "aw_qskip"))

        # Poster 2.0 S3 п.6: последний пропуск ведёт на шаг фото, и уже
        # «Без фотографий» завершает анонс.
        self.assertIn("Фотографии анонса", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_photo_none"))
        self.assertFalse(_callbacks(h, "aw_send"))
        await _no_photos(h)
        self.assertIn("Готовый анонс отправлен в этот чат", h.screen_text())
        posts = h.posts()
        self.assertEqual(len(posts), 1)
        text = posts[0] or ""
        self.assertIn("Дом культуры", text)
        self.assertIn("ул. Тестовая, 1", text)
        self.assertIn("https://example.com/place", text)
        with h.session_factory() as session:
            stored = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=_latest_draft(h).id,
            )
        self.assertIn("Дом культуры", stored.publication_text or "")
        self.assertNotIn("[", stored.publication_text or "")
        self.assertEqual(stored.publication_status, "sent")
        # «Из справочника» — пометка обзора, а не текст анонса.
        await _preview(h)
        self.assertIn("из справочника", h.screen_text())
