# tests/test_poster2_u2b_finish.py
"""
Poster 2.0 U2b, экран завершения (№6) и компактная правка готового анонса.

Проверяется:

- точный текст экрана завершения и ровно две кнопки в одной строке:
  «✏️ Изменить» (`aw_edit`) и «🏠 В меню» (`main_menu`);
- строка о неприменённых изменениях появляется ТОЛЬКО когда они есть;
- «Изменить» открывает компактный экран категорий и не отправляет копию;
- «В меню» оставляет готовый пост и ровно один служебный экран;
- инвариант порядка: все `message_id` постов ниже `message_id` служебного
  экрана — по фактическим id из FakeBot (`call_message_ids`), в том числе
  после замены версии;
- идемпотентность «Применить изменения» и устаревание только устаревших
  кнопок.

Тесты идут через настоящее приложение, FakeBot и временную SQLite.
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import (
        DELIVERED_EDIT_TEXT,
        DELIVERED_PENDING_TEXT,
        DELIVERED_TEXT,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        is_screen_record,
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

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


DELIVERED_BUTTONS = [("✏️ Изменить", "aw_edit"), ("🏠 В меню", "main_menu")]


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _keyboard_rows(h) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры экрана: строки и кнопки."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


def _post_ids(h, record: dict) -> tuple[int, ...]:
    """Фактические `message_id` сообщений, созданных этим постом."""
    return tuple(h.bot.call_message_ids.get(id(record), ()))


def _present_post_ids(h) -> list[int]:
    """
    `message_id` постов, которые РЕАЛЬНО остались в чате.

    Заменённые версии Telegram удаляет, поэтому их id в проверку порядка
    входить не должны — иначе тест требовал бы невозможного порядка от
    удалённого сообщения.
    """
    deleted = set(h.deleted_ids())
    out: list[int] = []
    for record in h.post_records():
        ids = _post_ids(h, record)
        if ids and all(message_id in deleted for message_id in ids):
            continue
        out.extend(ids)
    return out


def _screen_sends(h) -> list[dict]:
    """Созданные (не отредактированные) сообщения-экраны."""
    return [c for c in h.screens() if c["type"] == "message"]


async def _delivered(
    h,
    *,
    value: str = "Значение",
    code: str = "main",
    specs: list[tuple[str, str, str, dict[str, object]]] | None = None,
):
    """Личный анонс, доставленный «Пропустить» на шаге фото."""
    place_id, _alias_id = _seed_place(h)
    if specs is None:
        specs = [(code, "Основное поле", "short_text", {})]
    template_id, _revision, bricks = _seed_personal_template(h, specs=specs)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    await _no_photos(h)
    if h.screen_text().split("\n\n")[0] != DELIVERED_TEXT.split("\n\n")[0]:
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    draft = _latest_draft(h)
    return draft, bricks


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredScreenTests(unittest.TestCase):
    """Текст и кнопки экрана завершения."""

    @scenario_test
    async def test_exact_text_and_two_buttons_in_one_row(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        self.assertEqual(h.screen_text(), DELIVERED_TEXT)
        self.assertNotIn(DELIVERED_PENDING_TEXT, h.screen_text())

        rows = _keyboard_rows(h)
        self.assertEqual(len(rows), 1, f"ожидалась одна строка: {rows}")
        labels = [label for label, _data in rows[0]]
        self.assertEqual(labels, ["✏️ Изменить", "🏠 В меню"])
        self.assertEqual(
            [data for _label, data in rows[0]],
            [f"aw_edit:{draft.id}:{draft.revision}", "main_menu"],
        )
        # Служебных сводок и кнопок прошлого экрана нет.
        for gone in ("aw_qmanage", "aw_extras", "aw_apply", "aw_preview", "aw_save"):
            self.assertFalse(_callbacks(h, gone), f"лишняя кнопка {gone}")
        self.assertNotIn("Место:", h.screen_text())

    @scenario_test
    async def test_pending_line_appears_only_with_unapplied_changes(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        # Без правок строки нет.
        await h.callback(_callback(h, "aw_edit"))
        self.assertNotIn(DELIVERED_PENDING_TEXT, h.screen_text())
        await h.callback(_callback(h, "aw_panel"))
        self.assertEqual(h.screen_text(), DELIVERED_TEXT)

        # После правки значения экран предупреждает об изменениях.
        await h.callback(_callback(h, "aw_edit"))
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Новое значение")
        self.assertIn(DELIVERED_PENDING_TEXT, h.screen_text())
        self.assertTrue(h.screen_text().startswith(DELIVERED_TEXT))
        # Новая версия не ушла: её отправляет только «Применить изменения».
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(_details(h, draft.id).publication_status, "sent")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredEditScreenTests(unittest.TestCase):
    """«✏️ Изменить» — компактный экран категорий без дублей."""

    @scenario_test
    async def test_edit_screen_lists_categories_without_sending_copies(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()

        await h.callback(_callback(h, "aw_edit"))
        self.assertEqual(h.screen_text(), DELIVERED_EDIT_TEXT)
        labels = [label for label, _data in h.screen_buttons()]
        self.assertEqual(len(labels), 5, labels)
        self.assertTrue(any(label.startswith("✏️ Данные") for label in labels))
        self.assertTrue(any(label.startswith("📷 Фотографии") for label in labels))
        self.assertTrue(any(label.startswith("➕ Кирпичики") for label in labels))
        self.assertTrue(
            any(label.startswith("🔄 Применить изменения") for label in labels)
        )
        self.assertIn("◀️ Назад", labels)
        # Экран правки ничего не отправляет и не меняет версию.
        self.assertEqual(h.posts(), posts_before)
        self.assertEqual(_details(h, draft.id).revision, draft.revision)

    @scenario_test
    async def test_back_returns_to_delivered_screen(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        await h.callback(_callback(h, "aw_edit"))

        await h.callback(_callback(h, "aw_panel"))
        self.assertEqual(h.screen_text(), DELIVERED_TEXT)
        rows = _keyboard_rows(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [label for label, _data in rows[0]], ["✏️ Изменить", "🏠 В меню"]
        )

    @scenario_test
    async def test_edit_categories_roundtrip_keeps_single_post(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        posts_before = len(h.posts())

        await h.callback(_callback(h, "aw_edit"))
        await h.callback(_callback(h, "aw_photos"))
        self.assertIn("📷 Фотографии анонса", h.screen_text())
        # Возврат из коллекции фото (обзор доставленного) не отправляет
        # копию и не теряет готовый пост.
        await h.callback(_callback(h, "aw_preview"))
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertEqual(len(h.posts()), posts_before)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredMenuTests(unittest.TestCase):
    """«🏠 В меню»: пост цел, служебный экран один."""

    @scenario_test
    async def test_menu_keeps_post_and_single_service_screen(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        post_ids = _present_post_ids(h)
        self.assertTrue(post_ids)
        screen_id = h.screen_message_id()
        self.assertIsNotNone(screen_id)
        sends_before = len(_screen_sends(h))

        await h.callback("main_menu")

        self.assertIn("Выберите действие", h.screen_text())
        self.assertFalse(_callbacks(h, "aw_edit"), "экран завершения закрыт")
        # Пост не удалён и не переотправлен.
        for message_id in post_ids:
            self.assertNotIn(message_id, h.deleted_ids())
        self.assertEqual(_present_post_ids(h), post_ids)
        self.assertEqual(len(h.posts()), 1)
        # Второго служебного экрана не появилось: меню встало в тот же.
        self.assertEqual(len(_screen_sends(h)), sends_before)
        self.assertEqual(h.screen_message_id(), screen_id)

        # Прошлый анонс не тронут и в БД.
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(len(stored.delivered_message_ids), 1)

    @scenario_test
    async def test_menu_does_not_touch_earlier_announcements(self, h) -> None:
        await h.text("/start")
        first, _bricks = await _delivered(h, value="Первый")
        first_ids = set(_details(h, first.id).delivered_message_ids)

        await h.callback("main_menu")
        await h.text("/start")
        second, _bricks2 = await _delivered(
            h,
            value="Второй",
            specs=[("second", "Второе поле", "short_text", {})],
        )
        second_ids = set(_details(h, second.id).delivered_message_ids)
        self.assertNotEqual(second.id, first.id)
        self.assertFalse(second_ids & first_ids)

        await h.callback("main_menu")
        self.assertFalse(set(first_ids) & set(h.deleted_ids()))
        self.assertEqual(set(_details(h, first.id).delivered_message_ids), first_ids)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class MessageOrderInvariantTests(unittest.TestCase):
    """Все реальные посты выше служебного экрана — по фактическим id."""

    def _assert_order(self, h) -> None:
        post_ids = _present_post_ids(h)
        screen_id = h.screen_message_id()
        self.assertTrue(post_ids, "нет ни одного поста")
        self.assertIsNotNone(screen_id, "нет служебного экрана")
        assert screen_id is not None
        self.assertLess(
            max(post_ids),
            screen_id,
            f"пост должен быть выше экрана: posts={post_ids}, screen={screen_id}",
        )
        # Экран, в который рисуется UI, — именно тот, что ниже поста.
        last_screen = h.screens()[-1]
        self.assertTrue(is_screen_record(last_screen))

    @scenario_test
    async def test_first_delivery_puts_screen_below_post(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        self._assert_order(h)

    @scenario_test
    async def test_replacement_version_keeps_screen_below_post(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        first_ids = _details(h, draft.id).delivered_message_ids

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Обновлено")
        await _open_edit(h)
        await h.callback(_callback(h, "aw_apply"))

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(tuple(stored.previous_message_ids), first_ids)
        self.assertIn("Обновлено", h.posts()[-1] or "")
        # Прошлая версия удалена только после подтверждённой новой.
        self.assertTrue(set(first_ids) <= set(h.deleted_ids()))
        self._assert_order(h)

        # Возврат в меню и повторное открытие не ломают порядок и не плодят
        # второе сообщение экрана.
        sends_before = len(_screen_sends(h))
        await h.callback("main_menu")
        self.assertEqual(len(_screen_sends(h)), sends_before)
        self._assert_order(h)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredApplyTests(unittest.TestCase):
    """Идемпотентность «Применить изменения» и устаревание кнопок."""

    @scenario_test
    async def test_apply_without_changes_does_not_create_a_post(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        posts_before = h.posts()
        version_ids = _details(h, draft.id).delivered_message_ids

        await _open_edit(h)
        await h.callback(_callback(h, "aw_apply"))
        await _open_edit(h)
        await h.callback(_callback(h, "aw_apply"))

        self.assertEqual(h.posts(), posts_before)
        stored = _details(h, draft.id)
        self.assertEqual(tuple(stored.delivered_message_ids), version_ids)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(h.screen_text(), DELIVERED_TEXT)
        # Никакой новой копии: пост остался единственным.
        self.assertEqual(len(_present_post_ids(h)), len(version_ids))

    @scenario_test
    async def test_success_shows_only_delivered_text_not_stale_notice(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        # Обычный успех: экран не смешивает «Анонс готов» и «Экран устарел».
        self.assertEqual(h.screen_text(), DELIVERED_TEXT)
        self.assertNotIn("Экран устарел", h.screen_text())
        self.assertNotIn("Экран устарел", h.answer_texts())

    @scenario_test
    async def test_stale_button_only_warns_about_old_action(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        stale_edit = _callback(h, "aw_edit")

        # Правка меняет ревизию: старая кнопка «Изменить» устарела.
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Другое значение")

        await h.callback(stale_edit)
        self.assertIn("Экран устарел", h.answer_texts()[-1])
        # Экран при этом остаётся экраном завершения с его двумя кнопками.
        self.assertTrue(h.screen_text().startswith(DELIVERED_TEXT))
        rows = _keyboard_rows(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            [label for label, _data in rows[0]], ["✏️ Изменить", "🏠 В меню"]
        )
        # Ничего не отправлено и не потеряно.
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(_details(h, draft.id).publication_status, "sent")

    @scenario_test
    async def test_repeated_stale_callback_changes_nothing(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        stale_edit = _callback(h, "aw_edit")
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Другое")

        await h.callback(stale_edit)
        first_text = h.screen_text()
        posts_after_first = h.posts()

        await h.callback(stale_edit)
        self.assertIn("Экран устарел", h.answer_texts()[-1])
        self.assertEqual(h.screen_text(), first_text)
        self.assertEqual(h.posts(), posts_after_first)
