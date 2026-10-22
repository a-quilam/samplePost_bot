# tests/test_stage11_scenarios.py
"""
Stage 11, коммит G: интеграционные тесты 24 сценариев брифа (§12).

Каждый тест соответствует пункту списка сценариев брифа Stage 11
(«ТЕСТЫ»): реальный Application.process_update, FakeBot, временная
SQLite. Проверяются количество отправленных, отредактированных и
удалённых сообщений, сохранность данных и отсутствие дублей.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from telegram import MessageEntity
    from telegram.error import BadRequest

    from handlers.post_creation import POST_CREATION, POST_STEPS
    from models import Draft, get_draft_photos
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


GREETING = "Здравствуйте! 👋\n\nВыберите действие:"


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


def screen_sends(h) -> list[dict]:
    """Отрисовки экрана, создавшие НОВОЕ сообщение (не edit)."""
    return [s for s in h.screens() if s["type"] == "message"]


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class Stage11ScenarioTests(unittest.TestCase):
    """24 сценария брифа Stage 11, §12 — по одному тесту на пункт."""

    # —— 1–4: /start и первый экран ————————————————————————————————

    @scenario_test
    async def test_01_start_creates_first_screen(self, h):
        """1. /start создаёт первый экран (одно сообщение, команду убирает)."""
        update = await h.text("/start")

        self.assertEqual(h.screen_text(), GREETING)
        self.assertEqual(len(screen_sends(h)), 1, "ровно одно сообщение-экран")
        self.assertEqual(len(h.replies()), 0, "ответных сообщений нет")
        self.assertIn(
            update.message.message_id, h.deleted_ids(), "/start удалён best-effort"
        )
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_02_repeated_start_edits_existing_screen(self, h):
        """2. Повторный /start не плодит сообщений: экран тот же, меню на месте."""
        first = await h.text("/start")
        ref_before = h.user_data()["screen"]["message_id"]

        second = await h.text("/start")

        self.assertEqual(h.user_data()["screen"]["message_id"], ref_before)
        self.assertEqual(len(screen_sends(h)), 1, "новое сообщение не создано")
        # Настоящий Telegram отвечает «message is not modified» на идентичную
        # перерисовку (Stage 16, реализм FakeBot): меню уже на экране —
        # новая правка не нужна и не порождает событий
        self.assertEqual(h.screen_text(), GREETING)
        self.assertIn(second.message.message_id, h.deleted_ids())
        self.assertIn(first.message.message_id, h.deleted_ids())

        # Изменившееся меню редактирует ТОТ ЖЕ экран, а не создаёт новый
        await h.start_dialog()
        await h.text("/start")
        self.assertEqual(h.user_data()["screen"]["message_id"], ref_before)
        last = h.screens()[-1]
        self.assertEqual(last["type"], "edit_message")
        self.assertEqual(last["message_id"], ref_before, "правится тот же экран")
        self.assertEqual(h.screen_text(), GREETING)

    @scenario_test
    async def test_03_start_during_creation_saves_nonempty_wip_once(self, h):
        """3. /start во время создания сохраняет непустой WIP — один раз."""
        await h.text("/start")
        await h.start_dialog()
        await h.text("Незавершённый пост")
        self.assertEqual(h.dialog_state(), POST_CREATION)

        await h.text("/start")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 1, "черновик создан ровно один раз")
        self.assertEqual(drafts[0].title, "Незавершённый пост")
        self.assertIsNone(h.dialog_state(), "диалог завершён")
        self.assertIn("сохранён в черновики", h.reply_texts()[-1])
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertEqual(len(screen_sends(h)), 1, "меню — на том же экране")

        # Повторный /start: WIP уже сохранён и очищен — дубля нет
        await h.text("/start")
        self.assertEqual(len(h.drafts()), 1, "дубликата черновика нет")

    @scenario_test
    async def test_04_start_during_creation_keeps_empty_wip_unsaved(self, h):
        """4. /start не сохраняет пустой WIP (запись не создаётся)."""
        await h.text("/start")
        await h.start_dialog()  # шаг 1, данных ещё нет

        await h.text("/start")

        self.assertEqual(h.drafts(), [], "пустой черновик запрещён")
        self.assertIsNone(h.dialog_state())
        self.assertIn("Здравствуйте", h.screen_text())

    # —— 5–8: ввод и очистка временных сообщений ——————————————————————

    @scenario_test
    async def test_05_text_input_updates_screen(self, h):
        """5. После ввода текста экран обновляется (edit, без новых сообщений)."""
        await h.text("/start")
        await h.start_dialog()
        screens_before = len(h.screens())

        await h.text("Заголовок сцены")

        self.assertEqual(len(h.screens()), screens_before + 1)
        self.assertEqual(h.screens()[-1]["type"], "edit_message")
        self.assertTrue(
            h.screen_text().startswith("Шаг 2 из 9 — Дата."),
            f"ожидался промпт следующего шага, получено: {h.screen_text()!r}",
        )
        self.assertIn("📌 Заполнено: 1 из 9 — Заголовок", h.screen_text())
        self.assertEqual(len(screen_sends(h)), 1)

    @scenario_test
    async def test_06_accepted_input_deleted_rejected_kept(self, h):
        """6. Успешный ввод удаляется только после сохранения; ошибочный — остаётся."""
        await h.text("/start")
        await h.start_dialog()

        good = await h.text("Хороший заголовок")
        self.assertEqual(h.user_data().get("title"), "Хороший заголовок")
        self.assertIn(good.message.message_id, h.deleted_ids())

        # Шаг «Дата»: невалидное значение — ввод НЕ удаляется, ошибка на экране
        bad = await h.text("17.11.265")
        self.assertNotIn(bad.message.message_id, h.deleted_ids())
        self.assertTrue(h.screen_text().startswith("Шаг 2 из 9 — Дата."))
        self.assertIn("⚠️", h.screen_text(), "ошибка показана на экране")
        self.assertIsNone(h.user_data().get("date"))
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_07_db_failure_keeps_user_input(self, h):
        """7. Ошибка сохранения в SQLite не приводит к удалению пользовательского ввода."""
        await h.text("/start")
        await h.start_dialog()

        # Реальный сбой: таблица состояний недоступна
        with h.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE user_states")

        update = await h.text("Заголовок при сбое БД")

        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertEqual(h.user_data().get("title"), "Заголовок при сбое БД")
        self.assertTrue(
            h.screen_text().startswith("Шаг 2 из 9 — Дата."),
            "диалог продолжается на том же экране",
        )
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_08_delete_failure_does_not_break_scenario(self, h):
        """8. Ошибка удаления (Telegram запретил) не ломает сценарий."""
        h.bot.errors_by_type["delete_message"] = BadRequest(
            "Bad Request: message can't be deleted"
        )

        await h.text("/start")
        await h.start_dialog()
        update = await h.text("Переживший заголовок")

        self.assertEqual(h.user_data().get("title"), "Переживший заголовок")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertTrue(h.screen_text().startswith("Шаг 2 из 9 — Дата."))
        self.assertNotIn(update.message.message_id, h.deleted_ids())
        self.assertEqual(
            len(h.replies()), 0, "технической ошибки пользователю не показано"
        )

    # —— 9–11: навигация и превью ———————————————————————————————————

    @scenario_test
    async def test_09_back_returns_previous_screen_with_data(self, h):
        """9. «◀️ Назад» возвращает на предыдущий экран — данные сохранены."""
        await h.text("/start")
        await h.start_dialog()
        await h.text("Заголовок на месте")
        await h.text("Пропустить")  # дата пропущена → шаг 3
        self.assertTrue(h.screen_text().startswith("Шаг 3 из 9"))

        await h.callback("back")

        self.assertTrue(
            h.screen_text().startswith("Шаг 2 из 9 — Дата."),
            f"ожидался предыдущий шаг, получено: {h.screen_text()!r}",
        )
        self.assertEqual(h.user_data().get("title"), "Заголовок на месте")
        self.assertIsNone(h.user_data().get("date"))
        self.assertIn("📌 Заполнено: 1 из 9 — Заголовок", h.screen_text())
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(len(screen_sends(h)), 1, "навигация — только правки экрана")

    @scenario_test
    async def test_10_main_menu_creates_no_extra_messages(self, h):
        """10. Главное меню не создаёт лишних сообщений."""
        await h.text("/start")
        await h.start_dialog()
        replies_before = len(h.replies())

        await h.callback("main_menu")

        self.assertIsNone(h.dialog_state())
        self.assertEqual(h.drafts(), [], "пустой пост не сохраняется")
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertEqual(h.screens()[-1]["type"], "edit_message")
        self.assertEqual(len(screen_sends(h)), 1, "новых сообщений нет")
        self.assertEqual(len(h.replies()), replies_before, "без новых ответов")

    @scenario_test
    async def test_11_edit_does_not_stack_previews(self, h):
        """11. Редактирование поста не создаёт стек превью: старое удаляется."""
        await goto_preview(h, title="Исходный заголовок")
        first_preview = list(h.user_data().get("preview_messages") or [])
        self.assertEqual(len(first_preview), 1)

        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Обновлённый заголовок")

        second_preview = list(h.user_data().get("preview_messages") or [])
        self.assertEqual(len(second_preview), 1)
        self.assertNotEqual(first_preview, second_preview)
        for message_id in first_preview:
            self.assertIn(message_id, h.deleted_ids(), "старое превью удалено")
        self.assertNotIn(second_preview[0], h.deleted_ids(), "новое превью живо")
        self.assertEqual(
            len(h.post_records()), 2, "ровно два превью: старое заменено новым"
        )
        self.assertEqual(len(screen_sends(h)), 1, "экран один на весь сценарий")

    # —— 12–14: черновики ————————————————————————————————————————————

    @scenario_test
    async def test_12_draft_opens_on_existing_screen(self, h):
        """12. Открытие черновика использует существующий экран (без дублей)."""
        session = h.session_factory()
        try:
            session.add(Draft(user_id=h.user_id, title="Готовый черновик"))
            session.commit()
        finally:
            session.close()

        await h.text("/start")
        await h.callback("drafts")
        self.assertIn("Ваши черновики", h.screen_text())
        sends_before = len(screen_sends(h))

        await h.callback("editdraft_1")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("title"), "Готовый черновик")
        self.assertIn("Готовый черновик", h.posts()[-1], "превью показано")
        self.assertEqual(len(screen_sends(h)), sends_before, "экран существующий")
        self.assertEqual(h.screens()[-1]["type"], "edit_message")

    @scenario_test
    async def test_13_drafts_pagination_updates_screen(self, h):
        """13. Пагинация черновиков обновляет экран, а не создаёт сообщения."""
        session = h.session_factory()
        try:
            for i in range(17):
                session.add(Draft(user_id=h.user_id, title=f"Черновик {i + 1}"))
            session.commit()
        finally:
            session.close()

        await h.text("/start")
        await h.callback("drafts")
        self.assertIn("Показано 15 из 17", h.screen_text())
        sends_before = len(screen_sends(h))

        await h.callback("draftpage_15")

        self.assertIn("Показано 2 из 17", h.screen_text())
        self.assertEqual(h.screens()[-1]["type"], "edit_message")
        self.assertEqual(len(screen_sends(h)), sends_before)
        self.assertEqual(len(h.answers()), 2, "drafts + листание отвечены")
        self.assertEqual(len(h.replies()), 0)

    @scenario_test
    async def test_14_draft_deletion_requires_confirmation(self, h):
        """14. Удаление черновика требует подтверждения."""
        session = h.session_factory()
        try:
            session.add(Draft(user_id=h.user_id, title="Под удаление"))
            session.commit()
        finally:
            session.close()

        await h.text("/start")
        await h.callback("drafts")
        await h.callback("delete_1")

        self.assertEqual(len(h.drafts()), 1, "без подтверждения не удалён")
        self.assertIn("Удалить черновик 1", h.screen_text())

        await h.callback("confirmdelete_1")

        self.assertEqual(h.drafts(), [], "после подтверждения удалён")
        self.assertIn("Черновик 1 удалён.", h.screen_text())
        self.assertEqual(len(h.answers()), 3, "каждый callback отвечен")

    # —— 15–16: устаревшие кнопки и дубли —————————————————————————————

    @scenario_test
    async def test_15_stale_finish_does_not_change_result(self, h):
        """15. Старый callback не изменяет текущий результат (дубля поста нет)."""
        await goto_preview(h, title="Готовый пост")
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        posts_done = len(h.post_records())
        self.assertEqual(posts_done, 2, "превью + готовый пост")

        await h.callback("finish")  # устаревшее нажатие

        self.assertEqual(
            len(h.post_records()), posts_done, "повторный «Готово» не шлёт дубль"
        )
        self.assertEqual(len(h.answers()), 2, "устаревшая кнопка отвечена")
        self.assertIn("Пост готов.", h.screen_text(), "экран не сбит")
        self.assertEqual(h.drafts(), [], "пост не скопирован в черновики")
        self.assertNotIn("title", h.user_data())

    @scenario_test
    async def test_16_repeated_press_creates_no_duplicates(self, h):
        """16. Повторное нажатие не создаёт дублей черновиков и сообщений."""
        await goto_preview(h, title="Единственный")

        await h.callback("save_draft")
        await h.callback("save_draft")

        self.assertEqual(len(h.drafts()), 1, "ровно один черновик")
        self.assertEqual(len(h.answers()), 2, "оба нажатия отвечены")
        self.assertIn("Пост сохранён в черновики.", h.screen_text())
        self.assertNotIn("title", h.user_data())

    # —— 17–18: фотографии и альбомы ——————————————————————————————————

    @scenario_test
    async def test_17_single_photo_and_album_saved(self, h):
        """17. Одиночная фотография и альбом сохраняются (все file_id)."""
        # Одиночное фото → превью → черновик
        await h.goto_step(step_index("image"))
        photo = await h.photo("solo-photo-1")
        self.assertEqual(h.post_records()[-1]["type"], "photo")
        self.assertIn(photo.message.message_id, h.deleted_ids())
        await h.callback("save_draft")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(get_draft_photos(drafts[0]), ["solo-photo-1"])

        # Альбом → «Фото готово» → превью → второй черновик (без дубля первого)
        await h.goto_step(step_index("image"))
        first = await h.photo("album-1", media_group_id="mg-s17")
        second = await h.photo("album-2", media_group_id="mg-s17")
        await h.callback("media_done")
        self.assertEqual(h.post_records()[-1]["type"], "media_group")
        await h.callback("save_draft")

        drafts = h.drafts()
        self.assertEqual(len(drafts), 2, "второй пост — новая запись")
        self.assertEqual(get_draft_photos(drafts[1]), ["album-1", "album-2"])
        for source in (first, second):
            self.assertIn(source.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_18_late_photo_does_not_change_saved_result(self, h):
        """18. Запоздавшее фото не меняет уже собранное превью и результат."""
        await h.goto_step(step_index("image"))
        await h.photo("late-1", media_group_id="mg-late")
        await h.photo("late-2", media_group_id="mg-late")
        await h.callback("media_done")

        posts_before = len(h.post_records())
        photos_before = list(h.user_data().get("photos") or [])

        late = await h.photo("late-3", media_group_id="mg-late")  # гонка альбома

        self.assertEqual(len(h.post_records()), posts_before, "превью не пересоздано")
        self.assertEqual(list(h.user_data().get("photos") or []), photos_before)
        self.assertNotIn(
            late.message.message_id, h.deleted_ids(), "непринятое фото не тронуто"
        )

        await h.callback("finish")
        albums = [c for c in h.calls if c["type"] == "media_group"]
        self.assertEqual([m.media for m in albums[-1]["media"]], ["late-1", "late-2"])

    # —— 19–20: готовый пост и устойчивость ————————————————————————————

    @scenario_test
    async def test_19_final_post_survives_cleanup(self, h):
        """19. Очистка удаляет превью, готовый пост остаётся в чате навсегда."""
        await goto_preview(h, title="Навсегда в чате")
        preview_ids = list(h.user_data().get("preview_messages") or [])
        self.assertEqual(len(preview_ids), 1)
        deletes_before = set(h.deleted_ids())

        await h.callback("finish")

        final_id = h.bot._next_message_id  # id только что отправленного поста
        new_deletes = set(h.deleted_ids()) - deletes_before
        self.assertEqual(
            new_deletes, set(preview_ids), "очищено ровно превью — и ничего больше"
        )
        self.assertNotIn(final_id, h.deleted_ids(), "готовый пост не удаляется")
        self.assertEqual(len(h.post_records()), 2, "превью + готовый пост")
        self.assertIn("Пост готов.", h.screen_text())

    @scenario_test
    async def test_20_api_failure_keeps_data_and_allows_retry(self, h):
        """20. Сбой Telegram API не теряет данные и не врёт об успехе."""
        await goto_preview(h, title="Ценный текст")
        h.bot.fail_types.add("message")

        await h.callback("finish")

        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог жив")
        self.assertEqual(h.user_data().get("title"), "Ценный текст", "данные целы")
        self.assertIn("Не удалось отправить готовый пост", h.screen_text())
        self.assertEqual(len(h.post_records()), 1, "только превью — ложного успеха нет")

        # Сеть вернулась — повторное «Готово» доводит дело до конца
        h.bot.fail_types.discard("message")
        await h.callback("finish")

        self.assertEqual(len(h.post_records()), 2)
        self.assertIn("Пост готов.", h.screen_text())
        self.assertIsNone(h.dialog_state())
        self.assertEqual(h.drafts(), [])

    # —— 21–22: восстановление экрана и рестарт ——————————————————————————

    @scenario_test
    async def test_21_screen_recovered_after_deletion(self, h):
        """21. Удалённый экран восстанавливается: новый вместо ошибки."""
        await h.text("/start")
        ref_before = h.user_data()["screen"]["message_id"]

        # Telegram отвечает «сообщение не найдено» на правку экрана
        h.bot.errors_by_type["edit_message"] = BadRequest("Message to edit not found")
        await h.text("/start")
        h.bot.errors_by_type.pop("edit_message", None)

        ref_after = h.user_data()["screen"]["message_id"]
        self.assertNotEqual(ref_after, ref_before, "создан новый экран")
        self.assertEqual(h.screens()[-1]["type"], "message")
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertEqual(
            len(h.replies()), 0, "технической ошибки пользователю не показано"
        )

    @scenario_test
    async def test_22_restart_restores_screen_and_offers_wip(self, h):
        """22. После перезапуска: экран правится, WIP предлагается к продолжению."""
        await h.text("/start")
        await h.start_dialog()
        await h.text("Незавершённый пост")
        screen_id_before = h.user_data()["screen"]["message_id"]
        self.assertTrue(any(state.payload for state in h.user_states()))
        sends_before = len(screen_sends(h))

        await h.restart()  # новое Application: user_data и диалог потеряны

        self.assertEqual(h.user_data(), {}, "состояние процесса потеряно")

        await h.text("/start")

        self.assertEqual(
            h.user_data()["screen"]["message_id"],
            screen_id_before,
            "восстановлен ref старого экрана",
        )
        last = h.screens()[-1]
        self.assertEqual(last["type"], "edit_message")
        self.assertEqual(last["message_id"], screen_id_before)
        self.assertEqual(len(screen_sends(h)), sends_before, "дубля экрана нет")
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertIn(
            ("▶️ Продолжить пост", "restore_wip"),
            h.screen_buttons(),
            "меню предлагает продолжить WIP",
        )
        self.assertEqual(h.drafts(), [], "рестарт не создаёт черновиков")

        await h.callback("restore_wip")

        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог возобновлён")
        self.assertEqual(h.user_data().get("title"), "Незавершённый пост")
        self.assertTrue(
            h.screen_text().startswith("Шаг 2 из 9 — Дата."),
            f"возобновились с прерванного шага, получено: {h.screen_text()!r}",
        )
        self.assertEqual(len(screen_sends(h)), sends_before)

    # —— 23–24: форматирование и инвариант PREVIEW == FINAL ————————————————

    @scenario_test
    async def test_23_formatting_survives_screen_transitions(self, h):
        """23. Форматирование Stage 10 сохраняется при всех переходах экрана."""
        await h.text("/start")
        await h.start_dialog()
        await h.text(
            "Яркий заголовок",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=15)],
        )
        await h.text("Пропустить")  # дата → шаг 3
        await h.callback("back")  # переход экрана назад → шаг 2 (дата)
        self.assertTrue(h.screen_text().startswith("Шаг 2 из 9 — Дата."))
        self.assertEqual(h.user_data().get("title"), "Яркий заголовок")

        # вперёд до превью: шаги «Дата»…«Изображение»
        for _ in range(8):
            await h.text("Пропустить")

        preview = h.post_records()[-1]
        self.assertIn("<b>Яркий заголовок</b>", preview["text"] or "")
        self.assertEqual(preview["parse_mode"], "HTML")

        await h.callback("finish")
        final = h.post_records()[-1]
        self.assertEqual(final["text"], preview["text"])
        self.assertEqual(final["parse_mode"], "HTML")
        self.assertIn("<b>Яркий заголовок</b>", final["text"] or "")

    @scenario_test
    async def test_24_preview_equals_final_all_media_forms(self, h):
        """24. PREVIEW == FINAL для текста, HTML, фото и альбома."""
        # Текст
        await goto_preview(h, title="Просто текст")
        await h.callback("finish")
        text_preview, text_final = h.post_records()[-2:]
        self.assertEqual(text_final["text"], text_preview["text"])
        self.assertEqual(text_final["parse_mode"], text_preview["parse_mode"])

        # HTML (форматирование из entities)
        await h.start_dialog()
        await h.text(
            "Яркий заголовок",
            entities=[MessageEntity(type=MessageEntity.BOLD, offset=0, length=15)],
        )
        for _ in range(8):
            await h.text("Пропустить")
        html_preview = h.post_records()[-1]
        self.assertIn("<b>Яркий заголовок</b>", html_preview["text"] or "")
        await h.callback("finish")
        html_final = h.post_records()[-1]
        self.assertEqual(html_final["text"], html_preview["text"])
        self.assertEqual(html_final["parse_mode"], html_preview["parse_mode"])

        # Фото
        await h.goto_step(step_index("image"))
        await h.photo("s24-photo")
        photo_preview = h.post_records()[-1]
        self.assertEqual(photo_preview["type"], "photo")
        await h.callback("finish")
        photo_final = h.post_records()[-1]
        self.assertEqual(photo_final["type"], "photo")
        self.assertEqual(photo_final["photo"], photo_preview["photo"])
        self.assertEqual(photo_final["caption"], photo_preview["caption"])
        self.assertEqual(photo_final["parse_mode"], photo_preview["parse_mode"])

        # Альбом
        await h.goto_step(step_index("image"))
        await h.photo("s24-a1", media_group_id="mg-s24")
        await h.photo("s24-a2", media_group_id="mg-s24")
        await h.callback("media_done")
        album_preview = [c for c in h.calls if c["type"] == "media_group"][-1]
        await h.callback("finish")
        album_final = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual(
            [m.media for m in album_final["media"]],
            [m.media for m in album_preview["media"]],
        )
        self.assertEqual(
            getattr(album_final["media"][0], "caption", None),
            getattr(album_preview["media"][0], "caption", None),
        )
        self.assertIsNone(getattr(album_final["media"][1], "caption", None))
