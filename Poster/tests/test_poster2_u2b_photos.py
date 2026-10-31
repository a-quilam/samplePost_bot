# tests/test_poster2_u2b_photos.py
"""
Poster 2.0 U2b, шаг фотографий (№5 конструктора).

Проверяется новый контракт шага:

- ровно ДВЕ кнопки в ОДНОЙ строке: «◀️ Назад» (`aw_photo_back`) и
  «⏭ Пропустить» (`aw_photo_none`);
- короткий текст без технических объяснений (медиагруппы, партии, статусы),
  счётчик добавляется только когда фотографии уже есть, шутка пустого
  анонса сохраняется;
- прежнее поведение приёма: одиночное фото и целый альбом (2–10) сразу
  завершают анонс, подпись пользователя игнорируется, 11-е фото
  отклоняется без потери принятых;
- лимиты текста 4096/1024 не изменились;
- рестарт посреди альбома ничего не теряет и не дублирует.

Тесты идут через настоящее приложение (`build_application`), FakeBot и
временную SQLite: реальная `post_bot.db` и сеть Telegram не используются.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

try:
    from announcement_store import MAX_ANNOUNCEMENT_PHOTOS, get_announcement_draft
    from handlers import wizard_photos
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from tests.integration_harness import (
        DEFAULT_CHAT_ID,
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _callbacks,
        _choose_personal,
        _latest_draft,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


PHOTO_STEP_TITLE = "📷 Фотографии анонса"
PHOTO_STEP_HINT = (
    "Пришлите фото или альбом. Всего можно добавить до "
    f"{MAX_ANNOUNCEMENT_PHOTOS} фото."
)
# Слова, которых на коротком шаге быть не должно: это прежние технические
# объяснения, которые владелец просил убрать.
TECHNICAL_WORDS = ("медиагрупп", "партия", "партии", "file_id", "статус")


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _keyboard_rows(h) -> list[list[tuple[str, str | None]]]:
    """Матрица inline-клавиатуры экрана: строки и кнопки (а не плоский список)."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


async def _to_photo_step(h, *, content: str | None = None, value: str = "Значение"):
    """Личный шаблон → ответ на единственный кирпичик → шаг фото."""
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(h, content=content)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text(value)
    if PHOTO_STEP_TITLE not in h.screen_text():
        raise AssertionError(f"Не шаг фото: {h.screen_text()}")
    return place_id, template_id, bricks


async def _finish_without_photos(h) -> None:
    await h.callback(_callback(h, "aw_photo_none"))
    if "✅ Анонс готов." not in h.screen_text():
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")


class PhotoStepViewTests(unittest.TestCase):
    """Вид шага: чистые функции текста и клавиатуры (без Telegram)."""

    def _details(self, *, photos: tuple[str, ...] = ()) -> SimpleNamespace:
        return SimpleNamespace(draft_id=7, revision=3, photos=photos)

    def test_text_is_short_and_has_no_technical_explanations(self) -> None:
        text = wizard_photos.photo_step_text(self._details())
        self.assertEqual(text, f"{PHOTO_STEP_TITLE}\n\n{PHOTO_STEP_HINT}")
        for word in TECHNICAL_WORDS:
            self.assertNotIn(word, text)
        # Счётчика нет, пока фотографий нет.
        self.assertNotIn("Добавлено фото", text)

    def test_counter_appears_only_when_photos_exist(self) -> None:
        text = wizard_photos.photo_step_text(
            self._details(photos=("a", "b")),
        )
        self.assertIn("Добавлено фото: 2 из 10.", text)
        self.assertIn(PHOTO_STEP_HINT, text)

    def test_empty_announcement_keeps_its_joke(self) -> None:
        text = wizard_photos.photo_step_text(
            self._details(), announcement_is_empty=True
        )
        self.assertGreater(len(text.splitlines()), 3, "шутка не добавилась")
        self.assertIn(PHOTO_STEP_HINT, text)

    def test_keyboard_has_exactly_two_buttons_in_one_row(self) -> None:
        markup = wizard_photos.photo_step_keyboard(self._details())
        rows = [
            [(button.text, button.callback_data) for button in row]
            for row in markup.inline_keyboard
        ]
        self.assertEqual(len(rows), 1, "кнопки должны быть в одной строке")
        self.assertEqual(
            rows[0],
            [
                ("◀️ Назад", "aw_photo_back:7:3"),
                ("⏭ Пропустить", "aw_photo_none:7:3"),
            ],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoStepWiringTests(unittest.TestCase):
    """Шаг в настоящем мастере: кнопки, возврат, «Пропустить»."""

    @scenario_test
    async def test_step_has_exactly_two_buttons_in_one_row(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)

        rows = _keyboard_rows(h)
        self.assertEqual(len(rows), 1, f"ожидалась одна строка: {rows}")
        labels = [label for label, _data in rows[0]]
        self.assertEqual(labels, ["◀️ Назад", "⏭ Пропустить"])

        draft = _latest_draft(h)
        self.assertEqual(
            [data for _label, data in rows[0]],
            [
                f"aw_photo_back:{draft.id}:{draft.revision}",
                f"aw_photo_none:{draft.id}:{draft.revision}",
            ],
        )
        # Прежних кнопок шага больше нет, финальной кнопки тоже.
        for gone in ("aw_photo_add", "aw_photo_clear", "aw_send", "aw_apply"):
            self.assertFalse(_callbacks(h, gone), f"лишняя кнопка {gone}")
        self.assertEqual(h.posts(), [], "анонс ещё не должен быть отправлен")

    @scenario_test
    async def test_text_has_no_technical_details_and_shows_counter_later(
        self, h
    ) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        text = h.screen_text()
        for word in TECHNICAL_WORDS:
            self.assertNotIn(word, text)
        self.assertIn(PHOTO_STEP_HINT, text)
        self.assertNotIn("Добавлено фото", text)

        # После отклонённого 11-го снимка шаг остаётся с фотографиями и
        # показывает счётчик (снимки приняты, анонс ещё не готов).
        for index in range(MAX_ANNOUNCEMENT_PHOTOS + 1):
            await h.photo(f"cnt-{index}", media_group_id="mg-counter")
        await asyncio.sleep(0.4)
        self.assertIn("Добавлено фото: 10 из 10.", h.screen_text())
        self.assertIn(PHOTO_STEP_HINT, h.screen_text())

    @scenario_test
    async def test_back_returns_to_last_brick_without_losing_data(self, h) -> None:
        await h.text("/start")
        _place, _template, bricks = await _to_photo_step(h, value="Черновик")
        draft = _latest_draft(h)

        await h.callback(_callback(h, "aw_photo_back"))
        self.assertIn("Вопрос 1 из 1", h.screen_text())
        self.assertIn("Черновик", h.screen_text())
        stored = _details(h, draft.id)
        self.assertEqual(stored.photos, ())
        self.assertIsNone(stored.publication_status)
        self.assertEqual(h.posts(), [])

        # «Далее» снова ведёт на шаг фото, значение не потеряно.
        await h.callback(_callback(h, "aw_qnext"))
        self.assertIn(PHOTO_STEP_TITLE, h.screen_text())
        self.assertEqual(_details(h, draft.id).revision, stored.revision)
        self.assertEqual(bricks["main"], bricks["main"])

    @scenario_test
    async def test_stale_back_button_does_not_change_anything(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)
        stale_back = _callback(h, "aw_photo_back")

        # Ревизия меняется после добавления фото (экран шага переживает это),
        # поэтому старая кнопка «Назад» обязана быть отвергнута.
        await h.photo("rev-1", media_group_id="mg-rev")
        await asyncio.sleep(0.4)
        self.assertIsNotNone(_details(h, draft.id).publication_status)

        posts_before = h.posts()
        await h.callback(stale_back)
        self.assertIn("Данные изменились.", h.answer_texts()[-1])
        self.assertEqual(h.posts(), posts_before)
        self.assertIn("✅ Анонс готов.", h.screen_text())

    @scenario_test
    async def test_skip_finishes_without_photos(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)

        await h.callback(_callback(h, "aw_photo_none"))
        self.assertIn("✅ Анонс готов.", h.screen_text())
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ())
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoBatchFinalizeTests(unittest.TestCase):
    """Одиночное фото и альбом 2–10 завершают анонс сами."""

    def _media_groups(self, h) -> list[dict]:
        return [call for call in h.calls if call["type"] == "media_group"]

    @scenario_test
    async def test_single_photo_finalizes_without_confirmation(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)
        live_ids = set(_details(h, draft.id).live_message_ids)

        await h.photo("single-1")

        self.assertIn("✅ Анонс готов.", h.screen_text())
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("single-1",))
        # Подтверждающих кнопок нет: ни «Отправить», ни «Применить».
        self.assertFalse(_callbacks(h, "aw_send"))
        self.assertFalse(_callbacks(h, "aw_apply"))
        # Прошлый живой блок заменён, а не оставлен рядом.
        self.assertTrue(live_ids <= set(h.deleted_ids()))

    @scenario_test
    async def test_album_of_two_finalizes_once(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, value="Значение")
        draft = _latest_draft(h)

        await h.photo("alb-0", media_group_id="mg-two")
        await h.photo("alb-1", media_group_id="mg-two")
        self.assertIsNone(
            _details(h, draft.id).publication_status,
            "до конца партии анонс не должен быть готов",
        )
        await asyncio.sleep(0.4)

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("alb-0", "alb-1"))
        groups = self._media_groups(h)
        self.assertEqual(len(groups), 1, "ровно одна медиагруппа на альбом")
        captions = [getattr(item, "caption", None) for item in groups[0]["media"]]
        self.assertEqual(captions, [None, "Значение"])
        self.assertIn("✅ Анонс готов.", h.screen_text())

    @scenario_test
    async def test_album_of_ten_finalizes_once(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)

        uploads = []
        for index in range(MAX_ANNOUNCEMENT_PHOTOS):
            update = await h.photo(f"ten-{index}", media_group_id="mg-ten")
            uploads.append(update.message.message_id)
        await asyncio.sleep(0.4)

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(
            stored.publication_photos,
            tuple(f"ten-{index}" for index in range(MAX_ANNOUNCEMENT_PHOTOS)),
        )
        groups = self._media_groups(h)
        self.assertEqual(len(groups), 1)
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            [f"ten-{index}" for index in range(MAX_ANNOUNCEMENT_PHOTOS)],
        )
        for message_id in uploads:
            self.assertIn(message_id, h.deleted_ids(), "ввод удаляется после успеха")

    @scenario_test
    async def test_user_caption_is_ignored(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, value="Текст анонса")
        draft = _latest_draft(h)

        await h.photo("cap-1", caption="ПОДПИСЬ ПОЛЬЗОВАТЕЛЯ")

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_text, "Текст анонса")
        self.assertNotIn("ПОДПИСЬ", stored.publication_text or "")
        self.assertNotIn("ПОДПИСЬ", str(h.posts()))
        self.assertNotIn("ПОДПИСЬ", h.screen_text())

    @scenario_test
    async def test_caption_of_album_is_ignored(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, value="Текст анонса")
        draft = _latest_draft(h)

        await h.photo("cap-a", media_group_id="mg-cap", caption="ЧУЖАЯ ПОДПИСЬ")
        await h.photo("cap-b", media_group_id="mg-cap", caption="ЕЩЁ ПОДПИСЬ")
        await asyncio.sleep(0.4)

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_photos, ("cap-a", "cap-b"))
        self.assertEqual(stored.publication_text, "Текст анонса")
        groups = self._media_groups(h)
        captions = [getattr(item, "caption", None) for item in groups[0]["media"]]
        self.assertEqual(captions, [None, "Текст анонса"])


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoLimitOnStepTests(unittest.TestCase):
    """Лимит 10 на шаге фото: 11-е отклонено, принятые не потеряны."""

    @scenario_test
    async def test_eleventh_photo_is_rejected_and_ten_stay(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)

        for index in range(MAX_ANNOUNCEMENT_PHOTOS):
            await h.photo(f"ok-{index}", media_group_id="mg-limit")
        rejected = await h.photo("over-limit", media_group_id="mg-limit")
        await asyncio.sleep(0.4)

        stored = _details(h, draft.id)
        self.assertEqual(
            stored.photos,
            tuple(f"ok-{index}" for index in range(MAX_ANNOUNCEMENT_PHOTOS)),
        )
        self.assertIsNone(stored.publication_status, "партия с отказом не финал")
        self.assertIn("больше добавить нельзя", h.screen_text())
        self.assertIn("Добавлено фото: 10 из 10.", h.screen_text())
        # Сообщение с отклонённым снимком остаётся: автор видит причину.
        self.assertNotIn(rejected.message.message_id, h.deleted_ids())
        # Шаг по-прежнему даёт ровно две кнопки.
        self.assertEqual(len(_keyboard_rows(h)), 1)

        await _finish_without_photos(h)
        sent = _details(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(len(sent.publication_photos), MAX_ANNOUNCEMENT_PHOTOS)
        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["media"]), MAX_ANNOUNCEMENT_PHOTOS)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TextLimitTests(unittest.TestCase):
    """Лимиты 4096 без фото и 1024 с фото не изменились."""

    LONG_VALUE = "Длинный текст анонса " * 150  # ~3150 символов

    @scenario_test
    async def test_long_text_without_photos_is_delivered(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, content="[main]", value=self.LONG_VALUE)
        draft = _latest_draft(h)

        await _finish_without_photos(h)
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ())
        self.assertGreater(len(stored.publication_text or ""), 1024)
        self.assertLessEqual(len(stored.publication_text or ""), 4096)

    @scenario_test
    async def test_same_text_with_photo_hits_caption_limit(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, content="[main]", value=self.LONG_VALUE)
        draft = _latest_draft(h)

        photo_update = await h.photo("limit-photo")
        screen = h.screen_text()
        self.assertIn("не помещается", screen)
        self.assertIn("при лимите 1024", screen)

        stored = _details(h, draft.id)
        self.assertIsNone(stored.publication_status, "превышение не отправляется")
        self.assertEqual(stored.photos, ("limit-photo",))
        self.assertNotIn(photo_update.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_short_text_with_photo_is_delivered_as_caption(self, h) -> None:
        await h.text("/start")
        await _to_photo_step(h, content="[main]", value="Коротко")
        draft = _latest_draft(h)

        await h.photo("short-photo")
        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_text, "Коротко")
        self.assertIn("✅ Анонс готов.", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoAlbumRestartTests(unittest.TestCase):
    """Рестарт посреди альбома: фото сохранены, дублей нет."""

    @scenario_test
    async def test_restart_mid_album_keeps_photos_and_next_photo_finalizes(
        self, h
    ) -> None:
        await h.text("/start")
        await _to_photo_step(h)
        draft = _latest_draft(h)

        await h.photo("mid-0", media_group_id="mg-mid")
        await h.photo("mid-1", media_group_id="mg-mid")
        pending = h.user_data().get(wizard_photos.PENDING_TASK_KEY)
        if pending is not None:
            pending.cancel()
        before = _details(h, draft.id)
        self.assertEqual(before.photos, ("mid-0", "mid-1"))
        self.assertIsNone(before.publication_status)

        await h.restart()
        await asyncio.sleep(0.3)
        self.assertEqual(
            _details(h, draft.id).photos,
            ("mid-0", "mid-1"),
            "фото пережили рестарт",
        )
        self.assertEqual(
            [call for call in h.calls if call["type"] == "media_group"],
            [],
            "рестарт не отправил дубль",
        )

        # Продолжение: тот же черновик открывается из «Мои анонсы»; новый
        # снимок завершает анонс, прежние фото не переотправляются.
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        photo_button = _callbacks(h, "aw_photos")
        if photo_button:
            await h.callback(photo_button[0])
        await h.photo("mid-2")
        await asyncio.sleep(0.4)
        sent = _details(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("mid-0", "mid-1", "mid-2"))
        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "прежние фото не отправлены повторно")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            ["mid-0", "mid-1", "mid-2"],
        )
        self.assertIn("✅ Анонс готов.", h.screen_text())


def _delivered_photo_ids(h, details) -> tuple[str, ...]:
    """
    Состав фото РЕАЛЬНО доставленного поста (по его `message_id`).

    Записи `calls` не содержат id, поэтому адреса берутся из
    `FakeBot.call_message_ids`: проверяется не «что записано в БД», а что
    именно лежит в чате.
    """
    delivered = set(details.delivered_message_ids)
    if not delivered:
        raise AssertionError("пост считается доставленным, но адресов нет")
    for record in h.post_records():
        ids = set(h.bot.call_message_ids.get(id(record), ()))
        if not ids & delivered:
            continue
        if record["type"] == "photo":
            return (str(record.get("photo")),)
        if record["type"] == "media_group":
            return tuple(str(getattr(item, "media", None)) for item in record["media"])
        return ()
    raise AssertionError(f"доставленный пост {sorted(delivered)} не найден в чате")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class LiveBlockPhotoCompositionTests(unittest.TestCase):
    """
    Дефект U2b: число сегментов то же, состав фото другой.

    «1 фото» и «1 фото + ещё одно» дают одинаковое число сообщений блока,
    поэтому правка подписи выглядела успешной, а в чате оставался прежний
    снимок. Готовый пост обязан содержать ВСЕ фото черновика.
    """

    LONG_VALUE = "Длинный текст анонса " * 150  # ~3150 символов

    @scenario_test
    async def test_second_photo_with_same_segments_reaches_delivered_post(
        self, h
    ) -> None:
        await h.text("/start")
        await _to_photo_step(h, content="[main]", value=self.LONG_VALUE)
        draft = _latest_draft(h)
        await h.photo("F-1")
        self.assertIn("не помещается", h.screen_text(), "ожидался проблемный экран")
        self.assertIsNone(_details(h, draft.id).publication_status)

        # Мои анонсы → открыть → Фотографии → Добавить фото.
        await h.callback("main_menu")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        await h.callback(_callback(h, "aw_photos"))
        await h.callback(_callback(h, "aw_photo_add"))
        await h.photo("F-2")
        self.assertEqual(
            _details(h, draft.id).photos,
            ("F-1", "F-2"),
            "второе фото не сохранено",
        )
        self.assertIsNone(_details(h, draft.id).publication_status)

        # Сокращение текста собирает анонс заново (автофинал).
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.text("Коротко")
        self.assertIn("✅ Анонс готов.", h.screen_text())

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.publication_photos, ("F-1", "F-2"))
        # Главное: в чате лежит пост ровно с этим составом, а не только F-1.
        self.assertEqual(
            _delivered_photo_ids(h, stored),
            stored.publication_photos,
            "доставленный пост не содержит все фото анонса",
        )

    @scenario_test
    async def test_same_segment_count_with_new_photo_rebuilds_live_block(
        self, h
    ) -> None:
        """Прямая проверка `sync_live_block`: блок пересобирается, а не правится."""
        from announcement_store import add_announcement_photo
        from handlers import wizard_live

        await h.text("/start")
        await _to_photo_step(h, content="[main]", value=self.LONG_VALUE)
        draft = _latest_draft(h)
        await h.photo("F-1")
        self.assertIn("не помещается", h.screen_text(), "ожидался проблемный экран")
        details = _details(h, draft.id)
        self.assertEqual(details.photos, ("F-1",))

        # Первый медиаблок: один сегмент, в нём ровно F-1.
        first = await wizard_live.sync_live_block(
            h.bot,
            DEFAULT_CHAT_ID,
            telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft.id,
            known_ids=wizard_live.current_live_ids(details),
            text="Текст",
            photos=("F-1",),
            user_data=h.user_data(),
        )
        self.assertEqual(first.file_ids, ("F-1",))
        self.assertEqual(len(first.message_ids), 1, "ожидался один сегмент")
        # `_sync_live` в диалоге запоминает показанный состав; здесь вызов
        # прямой, поэтому состояние сессии фиксируется явно.
        wizard_live.remember_live_state(h.user_data(), draft_id=draft.id, block=first)

        # Второе фото: число сегментов то же (1), состав другой.
        with h.session_factory() as session:
            add_announcement_photo(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                file_id="F-2",
                expected_revision=_details(h, draft.id).revision,
            )
        second = await wizard_live.sync_live_block(
            h.bot,
            DEFAULT_CHAT_ID,
            telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft.id,
            known_ids=first.message_ids,
            text="Текст",
            photos=("F-1", "F-2"),
            user_data=h.user_data(),
        )
        self.assertNotEqual(
            second.message_ids, first.message_ids, "живой блок не пересобран"
        )
        self.assertEqual(second.file_ids, ("F-1", "F-2"))
        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[-1]["media"]],
            ["F-1", "F-2"],
            "в чат ушёл блок без нового фото",
        )
        self.assertIn(first.message_ids[0], h.deleted_ids(), "старый блок не убран")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ShownCompositionTests(unittest.TestCase):
    """`shown_file_ids`: сверка фактического состава показанного блока."""

    def test_known_composition_is_compared_by_addresses_and_draft(self) -> None:
        from handlers import wizard_live

        state = {
            "draft_id": 5,
            "fingerprint": "fp",
            "ids": [10],
            "photo_count": 1,
            "file_ids": ["F-1"],
        }
        user_data = {wizard_live.LIVE_STATE_KEY: state}
        self.assertEqual(
            wizard_live.shown_file_ids(user_data, draft_id=5, message_ids=(10,)),
            ("F-1",),
        )
        self.assertIsNone(
            wizard_live.shown_file_ids(user_data, draft_id=5, message_ids=(11,)),
            "чужие адреса не описывают показанный блок",
        )
        self.assertIsNone(
            wizard_live.shown_file_ids(user_data, draft_id=6, message_ids=(10,)),
            "состояние другого черновика не описывает этот блок",
        )
        # После рестарта состояния нет: состав неизвестен (остаётся счётчик).
        self.assertIsNone(wizard_live.shown_file_ids({}, draft_id=5, message_ids=(10,)))
