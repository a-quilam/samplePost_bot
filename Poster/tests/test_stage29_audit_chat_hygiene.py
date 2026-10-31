# tests/test_stage29_audit_chat_hygiene.py
"""
Аудит качества (пост-Stage 28): чистота чата при нескольких анонсах.

Требование владельца: чат состоит преимущественно из готовых анонсов и
одного актуального экрана; второй анонс не удаляет и не меняет результат
первого; служебное сообщение одно; временные сообщения убираются.

Все проверки идут через настоящее Application.process_update и FakeBot;
assert'ы фиксируют, КАКИЕ message_id должны остаться в чате.
"""

from __future__ import annotations

import unittest

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        is_screen_record,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
    )
    from tests.test_stage27c_announcement_ui import (
        _no_photos,
        _personal_delivered,
        _stored,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _live_ids(h) -> set[int]:
    """
    message_id сообщений бота, «существующих» в чате на текущий момент:
    отправленные и не удалённые. message_id восстанавливается детерминированно
    (FakeBot раздаёт их по порядку с 101): i-я отправка имеет id 100+i.
    """
    sent: set[int] = set()
    index = 0
    for call in h.calls:
        if call["type"] == "message":
            index += 1
            sent.add(100 + index)
        elif call["type"] == "photo":
            index += 1
            sent.add(100 + index)
        elif call["type"] == "media_group":
            # FakeBot возвращает ОДНО сообщение на группу
            index += 1
            sent.add(100 + index)
    for mid in h.deleted_ids():
        sent.discard(int(mid))
    return sent


def _details_ids(h, draft_id: int) -> tuple[int, ...]:
    """Сохранённые адреса живого блока конкретного черновика."""
    return tuple(_stored(h, draft_id).live_message_ids)


def _screen_send_ids(h) -> set[int]:
    """message_id сообщений, отправленных как служебный экран (не анонс/пост)."""
    out: set[int] = set()
    index = 0
    for call in h.calls:
        if call["type"] not in {"message", "photo", "media_group"}:
            continue
        index += 1
        if is_screen_record(call):
            out.add(100 + index)
    return out


def _edited_message_ids(h) -> set[int]:
    """message_id всех edit_message_text — готовые анонсы не должны там быть."""
    out: set[int] = set()
    for call in h.calls:
        if call["type"] != "edit_message":
            continue
        mid = call.get("message_id")
        if mid is None:
            pos_args = tuple(call.get("args") or ())
            mid = pos_args[1] if len(pos_args) > 1 else None
        if mid is not None:
            out.add(int(mid))
    return out


def _sends_outside_chat(h) -> list[dict]:
    """Вызовы отправки в чат, отличный от личного чата автора."""
    outside: list[dict] = []
    for call in h.calls:
        if call["type"] not in {"message", "photo", "media_group"}:
            continue
        chat_id = call.get("chat_id")
        if chat_id is None:
            pos_args = tuple(call.get("args") or ())
            chat_id = pos_args[0] if pos_args else None
        if chat_id is None or int(chat_id) != h.chat_id:
            outside.append(call)
    return outside


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ChatHygieneSecondAnnouncementTests(unittest.TestCase):
    @scenario_test
    async def test_second_announcement_keeps_first_result_and_single_screen(self, h):
        """Второй анонс: первый результат не тронут, служебное сообщение одно."""
        await h.text("/start")
        # — первый анонс —
        draft1, _bricks = await _personal_delivered(h)
        stored1 = _stored(h, draft1.id)
        first_result_ids = tuple(stored1.delivered_message_ids)
        self.assertTrue(first_result_ids)
        screen_after_first = h.screen_message_id()
        self.assertIsNotNone(screen_after_first)
        # U5: текстовый анонс без фото доставляется промоушеном живого блока —
        # правки до этого момента относятся к самой доставке, а не к чужому
        # анонсу. Дальше готовый результат редактироваться не должен.
        edits_after_first = _edited_message_ids(h)

        # — выход в меню и второй анонс —
        await h.callback("main_menu")
        await h.callback("aw_start")
        self.assertIn("Мои анонсы", h.screen_text())
        # Начали второй: экран ПЕРЕРИСОВЫВАЕТСЯ, а не создаёт новое сообщение.
        self.assertEqual(h.screen_message_id(), screen_after_first)

        place_id, _alias = _seed_place(h, name="Второе место")
        template_id, _rev, _bricks2 = _seed_personal_template(
            h, name="Второй шаблон", specs=[("main2", "Поле 2", "short_text", {})]
        )
        await h.callback("aw_new")
        await h.callback("aw_place_search")
        await h.text("Второе")
        await h.callback(f"aw_place_open:{place_id}")
        await h.callback(_callback(h, "aw_place_confirm"))
        await _choose_personal(h, template_id)
        await h.text("Второе значение")
        # S3 п.6: последний кирпичик ведёт на шаг фото; доставка — по нему.
        await _no_photos(h)

        # — результат второго анонса —
        from tests.test_stage27b_announcement_ui import _latest_draft

        draft2 = _latest_draft(h)
        self.assertNotEqual(draft2.id, draft1.id)
        stored2 = _stored(h, draft2.id)
        self.assertEqual(stored2.publication_status, "sent")
        second_result_ids = tuple(stored2.delivered_message_ids)

        # 1. Первый результат остаётся живым и не редактировался.
        for mid in first_result_ids:
            self.assertIn(mid, _live_ids(h))
        self.assertFalse(
            set(first_result_ids) & (_edited_message_ids(h) - edits_after_first),
            "готовый анонс отредактирован",
        )

        # 2. Второй результат жив.
        for mid in second_result_ids:
            self.assertIn(mid, _live_ids(h))

        # 3. Служебное сообщение по-прежнему ОДНО. Poster 2.0 S3 §4 требует,
        # чтобы анонс был выше экрана; Telegram не переставляет сообщения,
        # поэтому при первом показе анонса экран пересоздаётся ниже — старый
        # удаляется, второго живого экрана не появляется.
        alive = _live_ids(h)
        alive_screens = _screen_send_ids(h) & alive
        self.assertEqual(len(alive_screens), 1, f"живых экранов: {alive_screens}")
        self.assertEqual(alive_screens, {h.screen_message_id()})
        self.assertNotIn(screen_after_first, alive, "старый экран остался в чате")
        self.assertLess(
            max(tuple(_details_ids(h, draft2.id)) or (0,)),
            h.screen_message_id(),
            "анонс второго черновика оказался ниже экрана",
        )

        # 5. Никаких чужих сообщений третьим лицам: все отправки — в этот чат.
        self.assertEqual(_sends_outside_chat(h), [])

    @scenario_test
    async def test_reopen_delivered_announcement_does_not_resend(self, h):
        """Повторное открытие доставленного анонса не шлёт новую копию."""
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        count = len(h.post_records())
        stored = _stored(h, draft.id)
        result_ids = tuple(stored.delivered_message_ids)

        # Выход и повторное открытие из списка черновиков.
        await h.callback("main_menu")
        await h.callback("aw_start")
        data = _callbacks(h, "aw_open")
        self.assertTrue(data)
        await h.callback(data[-1])  # последний = доставленный
        self.assertEqual(len(h.post_records()), count, "повторная отправка копии")
        again = _stored(h, draft.id)
        self.assertEqual(tuple(again.delivered_message_ids), result_ids)
        self.assertEqual(again.revision, stored.revision, "лишний revision")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ScreenHtmlEscapingTests(unittest.TestCase):
    """
    Аудит: заголовки кирпичиков задаёт человек, а экраны шлются с
    parse_mode=HTML. Экранирование обязательно, иначе тег в заголовке
    ломает разметку экрана или даёт Telegram «Can't parse entities»
    (экран перестаёт перерисовываться).
    """

    HTML_TITLE = "Заголовок <b>с тегом</b>"

    async def _add_html_titled_extra(self, h) -> int:
        """Доставляет базовый анонс и возвращает extra с HTML-заголовком.

        Poster 2.3.1 (E1): кандидат в доп. поля — только кирпичик
        расширенного текста шаблона, поэтому кирпичик создаётся вместе
        с шаблоном.
        """
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, bricks = _seed_personal_template(
            h,
            specs=[
                ("main", "Основное", "short_text", {}),
                ("html_brick", self.HTML_TITLE, "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[html_brick]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Значение")
        # S3 п.6: доставка — после шага фотографий.
        await _no_photos(h)
        return int(bricks["html_brick"])

    async def _open_extra_prompt(self, h, brick_id: int) -> None:
        """Открывает экран ввода значения extra-кирпичика (с пагинацией)."""
        from tests.test_stage27b_announcement_ui import _callback

        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exadd"))
        data = [d for d in _callbacks(h, "aw_exbrick") if d.endswith(f":{brick_id}")]
        if not data:
            nav = _callbacks(h, "aw_exbpage", ":1")
            if nav:
                await h.callback(nav[0])
            data = [
                d for d in _callbacks(h, "aw_exbrick") if d.endswith(f":{brick_id}")
            ]
        await h.callback(data[0])
        sec = _callbacks(h, "aw_exsec")[0]
        await h.callback(sec)
        pos = _callbacks(h, "aw_expos")[0]
        await h.callback(pos)

    @scenario_test
    async def test_extra_brick_title_is_escaped_in_section_and_prompt(self, h):
        """Экраны выбора секции и ввода значения показывают тег как текст."""
        brick_id = await self._add_html_titled_extra(h)
        from tests.test_stage27b_announcement_ui import _callback, _open_edit

        # U2b: после доставки доп. кирпичики открываются с экрана «Изменить».
        await _open_edit(h)
        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exadd"))
        data = [d for d in _callbacks(h, "aw_exbrick") if d.endswith(f":{brick_id}")]
        if not data:
            nav = _callbacks(h, "aw_exbpage", ":1")
            if nav:
                await h.callback(nav[0])
            data = [
                d for d in _callbacks(h, "aw_exbrick") if d.endswith(f":{brick_id}")
            ]
        await h.callback(data[0])

        raw_section = h.screen()["text"]
        self.assertIn("Заголовок &lt;b&gt;с тегом&lt;/b&gt;", raw_section)
        self.assertNotIn("<b>с тегом", raw_section)

        # H2: выбора секции/позиции больше нет — кирпичик сразу ждёт значение.
        raw_prompt = h.screen()["text"]
        self.assertIn("Заголовок &lt;b&gt;с тегом&lt;/b&gt;", raw_prompt)
        self.assertNotIn("<b>с тегом", raw_prompt)

    @scenario_test
    async def test_calendar_title_escapes_field_title(self, h):
        """Экран календаря экранирует заголовок поля-даты."""
        from tests.test_stage27b_announcement_ui import _callbacks

        await h.text("/start")
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(
            h, specs=[("when", "Дата <i>событ</i>ия", "date", {})], content="[when]"
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        raw = h.screen()["text"]
        self.assertIn("Дата &lt;i&gt;событ&lt;/i&gt;ия", raw)
        self.assertNotIn("<i>событ", raw)
        # Экран календаря доступен: переключиться на календарь
        cal = _callbacks(h, "aw_cal")
        self.assertTrue(cal, "кнопки календаря не найдены")

    @scenario_test
    async def test_question_screen_value_is_escaped(self, h):
        """Вопрос: значение и заголовок экранированы (регрессия аудита)."""
        from tests.test_stage27b_announcement_ui import (
            _choose_personal,
            _seed_personal_template,
            _seed_place,
            _start_to_template,
        )

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _rev, _bricks = _seed_personal_template(
            h,
            specs=[("main", "Поле <u>с тегом</u>", "short_text", {})],
            content="[main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        raw = h.screen()["text"]
        self.assertIn("Поле &lt;u&gt;с тегом&lt;/u&gt;", raw)
        self.assertNotIn("<u>с тегом", raw)
        await h.text("<b>ответ</b>")
        # S3 п.6: сначала шаг фото, доставка — по «Без фотографий».
        await _no_photos(h)
        # Значение ушло в готовый анонс экранированным (теги — как текст).
        stored = _stored(h, _latest_draft(h).id)
        self.assertIn("&lt;b&gt;ответ&lt;/b&gt;", stored.publication_text)
        self.assertNotIn("<b>ответ</b>", stored.publication_text)
        # И в списке полей: заголовок в кнопке — простой текст кнопки
        # (без HTML-парсинга), вопрос показывает значение как текст.
        from tests.test_stage27b_announcement_ui import _callback, _open_edit

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        button_labels = [label for label, _data in h.screen_buttons()]
        self.assertTrue(
            any("Поле <u>с тегом</u>" in label for label in button_labels),
            "заголовок поля не найден в кнопках",
        )
        edit_data = _callbacks(h, "aw_qgoto")
        self.assertTrue(edit_data)
        await h.callback(edit_data[0])
        raw3 = h.screen()["text"]
        self.assertIn("Текущее значение: &lt;b&gt;ответ&lt;/b&gt;", raw3)
        self.assertNotIn("<b>ответ</b>", raw3)
