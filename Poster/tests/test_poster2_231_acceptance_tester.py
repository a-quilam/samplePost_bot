# tests/test_poster2_231_acceptance_tester.py
"""
Poster 2.3.1 — независимые приёмочные сценарии Tester (тест-до-кода).

Проверяется требование владельца: доп. кирпичики анонса берутся только
из расширенного текста шаблона (E1), у шаблона есть путь создания
расширенной версии (E2), экран «Поля анонса» без лишних кнопок (E3),
статусы полей не путают «из справочника» с «пропущено» (E5).

Все действия идут через настоящий `Application.process_update` и реальные
callback-маршруты экранов (FakeBot, временный SQLite). Прямые доменные
вызовы используются только для подготовки данных.
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import FIELD_STATUS_KNOWN, build_announcement_fields
    from brick_store import create_personal_brick
    from models import AnnouncementExtra, AnnouncementValue
    from template_store import create_personal_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    IMPORT_ERROR = str(exc)


VALUE_PROMPT = "Отправьте короткий текст"
ALREADY_IN_SNAPSHOT = "уже входит в выбранный шаблон"


def _seed_brick(session, code: str, title: str):
    return create_personal_brick(
        session,
        actor_telegram_user_id=DEFAULT_USER_ID,
        code=code,
        title=title,
        value_type="short_text",
        rules={},
    )


def _seed_extended_template(
    h,
    *,
    name: str,
    content: str,
    extended: str | None,
    extra_code: str = "add1",
) -> dict[str, int]:
    """Личный шаблон с расширенным текстом. Возвращает {код: brick_id}."""
    with h.session_factory() as session:
        bricks = {"main": _seed_brick(session, "main", "Основа").id}
        if extra_code:
            bricks[extra_code] = _seed_brick(session, extra_code, "Доп 1").id
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            content=content,
            extended_content=extended,
        )
        session.commit()
        bricks["template_id"] = template.template_id
        return bricks


async def _boot(h) -> None:
    """Вход в меню: создаёт пользователя в БД (как реальный /start)."""
    await h.text("/start")


async def _skip_until_photos(h) -> None:
    """Пропускает все вопросы текущего шага (пока не шаг фото)."""
    for _attempt in range(12):
        if "Фотографии анонса" in h.screen_text():
            return
        skip = _callbacks(h, "aw_qskip")
        if not skip:
            return
        await h.callback(skip[0])
    raise AssertionError(f"Не дошли до шага фото: {h.screen_text()[:300]}")


async def _start_announcement(h, place_id: int, template_id: int) -> None:
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)


def _draft_extras(h, draft_id: int):
    with h.session_factory() as session:
        return (
            session.query(AnnouncementExtra)
            .filter(AnnouncementExtra.draft_id == draft_id)
            .all()
        )


def _upsert_value(h, *, draft_id: int, brick_id: int, text: str) -> None:
    """Готовит значение кирпичика (подготовка данных, не UI)."""
    with h.session_factory() as session:
        row = (
            session.query(AnnouncementValue)
            .filter_by(draft_id=draft_id, brick_id=brick_id)
            .one_or_none()
        )
        if row is None:
            session.add(
                AnnouncementValue(
                    draft_id=draft_id,
                    brick_id=brick_id,
                    state="valid",
                    value_json='{"text": "%s"}' % text,
                )
            )
        else:
            row.state = "valid"
            row.value_json = '{"text": "%s"}' % text
        session.commit()


def _add_extra_row(h, *, draft_id: int, brick_id: int) -> None:
    with h.session_factory() as session:
        exists = (
            session.query(AnnouncementExtra)
            .filter_by(draft_id=draft_id, brick_id=brick_id)
            .one_or_none()
        )
        if exists is None:
            session.add(AnnouncementExtra(draft_id=draft_id, brick_id=brick_id))
            session.commit()


def _all_button_texts(h) -> list[str]:
    return [label for label, _data in h.screen_buttons()]


def _is_extend_button(data: str | None, label: str) -> bool:
    """Кнопка создания расширенной версии шаблона (E2)."""
    if data and "ext" in data.lower():
        return True
    return "расширен" in label.lower()


async def _assert_can_create_extended(h) -> None:
    """
    На карточке шаблона есть путь создания расширенной версии.

    Кнопка должна реально открывать редактор расширенного текста, а не
    отвечать «в этом шаблоне нет расширенного текста» (E2: путь создания).
    """
    buttons = h.screen_buttons()
    extend = [
        (label, data) for label, data in buttons if _is_extend_button(data, label)
    ]
    if not extend:
        raise AssertionError(
            "в карточке шаблона должна быть кнопка создания расширенной "
            f"версии; есть: {buttons}"
        )
    _label, data = extend[0]
    await h.callback(data)
    alerts = " | ".join(h.answer_texts())
    if "нет расширенного текста" in alerts:
        raise AssertionError(
            "кнопка расширенной версии не создаёт её, а сообщает об "
            f"отсутствии: {alerts}"
        )
    if "Отправьте весь новый текст" not in h.screen_text():
        raise AssertionError(
            "кнопка должна открывать редактор расширенного текста, "
            f"экран: {h.screen_text()[:300]!r}"
        )


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class Poster231AcceptanceTests(unittest.TestCase):
    # —— E1: конструктор не спрашивает доп. поля ——————————————————
    @scenario_test
    async def test_s1_question_flow_ignores_extended(self, h) -> None:
        """Дефект 3: вопросы задаются только по content, не по extended."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        bricks = _seed_extended_template(
            h,
            name="Расширяемый",
            content="A [main] B C",
            extended="A [main] B [add1] C",
        )
        await _start_announcement(h, place_id, bricks["template_id"])

        self.assertIn("Основа", h.screen_text())
        await h.text("Основное значение")

        self.assertNotIn(
            "Доп 1",
            h.screen_text(),
            "доп. поле из extended не должно спрашиваться в конструкторе",
        )
        self.assertIn(
            "Фотографии анонса",
            h.screen_text(),
            "после единственного content-вопроса сразу шаг фото",
        )

    # —— E2: путь создания расширенной версии ————————————————————
    @scenario_test
    async def test_s2_standard_template_create_extended_button(self, h) -> None:
        """Дефект 1: в карточке стандартного шаблона нет создания extended."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        await h.callback("admin_panel")
        await h.callback(f"adm_place:{place_id}")
        self.assertIn(
            f"tp_std:{place_id}",
            [data for _label, data in h.screen_buttons()],
        )
        await h.callback(f"tp_std:{place_id}")
        await _assert_can_create_extended(h)

    @scenario_test
    async def test_s3_personal_template_create_extended_button(self, h) -> None:
        """Дефект 1: в карточке личного шаблона нет создания extended."""
        await _boot(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Личный шаблон"
        )
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await _assert_can_create_extended(h)

    # —— E3: «Поля анонса» без лишних кнопок ——————————————————————
    @scenario_test
    async def test_s4_question_manager_has_no_extra_buttons(self, h) -> None:
        """Дефект 2: на aw_qmanage не должно быть aw_extras/aw_preview."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h, name="Личный")
        await _start_announcement(h, place_id, template_id)
        await h.text("Значение")
        await _skip_until_photos(h)
        await _no_photos(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))

        self.assertIn("Поля анонса", h.screen_text())
        datas = [data for _label, data in h.screen_buttons()]
        self.assertEqual(
            [d for d in datas if d and d.startswith("aw_extras")],
            [],
            "«Добавить поле» не место на экране списка полей",
        )
        self.assertEqual(
            [d for d in datas if d and d.startswith("aw_preview")],
            [],
            "«Обзор анонса» не место на экране списка полей",
        )

    # —— E1: список доступных доп. полей и его применение ————————
    @scenario_test
    async def test_s5_available_extras_excludes_content_bricks(self, h) -> None:
        """Дефект 4: список = extended − content − выбранные, выбор работает."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        bricks = _seed_extended_template(
            h,
            name="Расширяемый",
            content="A [main] B",
            extended="A [main] [add1] B",
        )
        await _start_announcement(h, place_id, bricks["template_id"])
        await h.text("Значение")
        await _skip_until_photos(h)
        await _no_photos(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_extras"))
        add_data = _callback(h, "aw_exadd")
        _prefix, draft_id, revision = add_data.split(":")
        await h.callback(add_data)

        datas = [data for _label, data in h.screen_buttons()]
        self.assertNotIn(
            f"aw_exbrick:{draft_id}:{revision}:{bricks['main']}",
            datas,
            "кирпичик из content не должен предлагаться как доп. поле",
        )
        offered = [d for d in datas if d and d.endswith(f":{bricks['add1']}")]
        self.assertTrue(
            offered, f"кирпичик только из extended должен предлагаться: {datas}"
        )

        await h.callback(offered[0])
        self.assertNotIn(
            ALREADY_IN_SNAPSHOT,
            h.screen_text(),
            "предложенный кирпичик нельзя отклонять как «уже в шаблоне»",
        )
        self.assertIn(
            VALUE_PROMPT,
            h.screen_text(),
            "после выбора доп. поля должен быть запрос значения",
        )

    @scenario_test
    async def test_s6_extra_value_renders_at_extended_position(self, h) -> None:
        """Дефект 5: значение доп. поля рендерится на месте [add1], не в конце."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        bricks = _seed_extended_template(
            h,
            name="Расширяемый",
            content="A [main] B C",
            extended="A [main] B [add1] C",
        )
        await _start_announcement(h, place_id, bricks["template_id"])
        await h.text("M")
        await _skip_until_photos(h)

        draft = _latest_draft(h)
        _add_extra_row(h, draft_id=draft.id, brick_id=bricks["add1"])
        _upsert_value(h, draft_id=draft.id, brick_id=bricks["add1"], text="ADD")

        await _no_photos(h)
        text = (h.posts() or [""])[-1] or ""
        self.assertEqual(
            "A M B ADD C",
            text,
            "доп. поле рендерится ровно на месте [add1] расширенного текста, "
            "один раз (без копии в конце); "
            f"фактически: {text!r}",
        )

    @scenario_test
    async def test_s7_delete_last_extra_returns_to_content(self, h) -> None:
        """Удаление последнего доп. поля возвращает рендер к content."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        bricks = _seed_extended_template(
            h,
            name="Расширяемый",
            content="A [main] B",
            extended="A [main] [add1] B",
        )
        await _start_announcement(h, place_id, bricks["template_id"])
        await h.text("M")
        await _skip_until_photos(h)

        draft = _latest_draft(h)
        _add_extra_row(h, draft_id=draft.id, brick_id=bricks["add1"])
        _upsert_value(h, draft_id=draft.id, brick_id=bricks["add1"], text="ADD")
        await _no_photos(h)
        before = (h.posts() or [""])[-1] or ""
        self.assertIn("ADD", before)

        await _open_edit(h)
        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exdel"))
        self.assertEqual(
            _draft_extras(h, draft.id), [], "доп. поле должно быть удалено"
        )
        after = (h.posts() or [""])[-1] or ""
        self.assertNotIn("ADD", after, "после удаления значение не рендерится")
        self.assertIn("A M B", after)

    @scenario_test
    async def test_s8_no_extended_no_extras_button(self, h) -> None:
        """Без extended доп. поля добавлять некуда: кнопки и выбора нет."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Без расширения", content="A [main] B"
        )
        await _start_announcement(h, place_id, template_id)
        await h.text("M")
        await _skip_until_photos(h)
        await _no_photos(h)
        await _open_edit(h)

        self.assertEqual(
            [
                d
                for d in (data for _l, data in h.screen_buttons())
                if d and d.startswith("aw_extras")
            ],
            [],
            "у шаблона без расширенного текста нет кнопки доп. полей",
        )

        draft = _latest_draft(h)
        ui = h.user_data().get("announcement_ui") or {}
        revision = ui.get("revision") or draft.revision
        await h.callback(f"aw_extras:{draft.id}:{revision}")
        text = h.screen_text()
        self.assertNotIn("Произошла внутренняя ошибка", text)
        self.assertNotIn("Traceback", text)
        datas = [data for _label, data in h.screen_buttons()]
        self.assertEqual(
            [d for d in datas if d and d.startswith("aw_exadd")],
            [],
            "добавлять нечего: расширенного текста в снимке нет",
        )
        self.assertEqual(
            [d for d in datas if d and d.startswith("aw_exbrick")],
            [],
            "кирпичики вне расширенного текста не предлагаются",
        )

    # —— Совместимость и статусы полей ————————————————————————————
    @scenario_test
    async def test_s9_legacy_draft_without_extended_still_renders(self, h) -> None:
        """Старый черновик без extended: доп. поле не ломает рендер (append)."""
        await _boot(h)
        place_id, _alias = _seed_place(h)
        bricks = _seed_extended_template(
            h,
            name="Без расширения",
            content="A [main] B C",
            extended=None,
        )
        await _start_announcement(h, place_id, bricks["template_id"])
        await h.text("M")
        await _skip_until_photos(h)

        draft = _latest_draft(h)
        _add_extra_row(h, draft_id=draft.id, brick_id=bricks["add1"])
        _upsert_value(h, draft_id=draft.id, brick_id=bricks["add1"], text="ADD")
        await _no_photos(h)

        # Снимок без extended: legacy-доп. поле дописывается в конец
        # (совместимость чтения старых черновиков), рендер не падает.
        text = (h.posts() or [""])[-1] or ""
        self.assertIn("ADD", text, f"legacy-значение должно быть видно: {text!r}")
        self.assertIn("A M B C", text)
        self.assertNotIn("Произошла внутренняя ошибка", h.screen_text())

    @scenario_test
    async def test_s10_known_status_not_shown_as_skipped(self, h) -> None:
        """E5: поле из справочника не показывается как «пропущено»."""
        await _boot(h)
        place_id, _alias = _seed_place(h, address="ул. Тестовая, 1")
        with h.session_factory() as session:
            _seed_brick(session, "main", "Основа")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="С адресом",
                content="A [main] B [адрес]",
            )
            session.commit()
            template_id = template.template_id

        await _start_announcement(h, place_id, template_id)
        await h.text("M")
        await _skip_until_photos(h)
        await _no_photos(h)

        draft = _latest_draft(h)
        with h.session_factory() as session:
            fields = build_announcement_fields(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                today=__import__("datetime").date.today(),
            )
        address_fields = [f for f in fields if f.status == FIELD_STATUS_KNOWN]
        self.assertTrue(address_fields, "адрес места должен быть KNOWN")

        await _open_edit(h)
        ui = h.user_data().get("announcement_ui") or {}
        revision = ui.get("revision") or draft.revision
        await h.callback(f"aw_preview:{draft.id}:{revision}")
        text = h.screen_text()
        field_lines = [
            line
            for line in text.splitlines()
            if line.startswith("•") and "Адрес" in line
        ]
        self.assertTrue(
            field_lines,
            f"поле из справочника должно быть видно в списке полей: {text!r}",
        )
        for line in field_lines:
            self.assertIn(
                "из справочника",
                line,
                f"поле из справочника не должно выглядеть пропущенным: {line!r}",
            )
            self.assertNotIn("пропущено", line)

        # В списке «Поля анонса» KNOWN-поле не должно получать статус
        # «пропущено»: либо его там нет, либо статус честный.
        await h.callback(_callback(h, "aw_qmanage"))
        for label in _all_button_texts(h):
            if "Адрес" in label:
                self.assertNotIn("пропущено", label)


if __name__ == "__main__":
    unittest.main()
