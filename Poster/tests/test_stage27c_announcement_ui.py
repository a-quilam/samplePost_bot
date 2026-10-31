# tests/test_stage27c_announcement_ui.py
"""
Stage 28: сквозные сценарии автоматической выдачи анонса и его замены.

Все Update проходят через настоящее приложение/FakeBot, БД временная.
Проверяются оба вида шаблонов, 0/1/много фото, рестарт, двойной callback,
сбой API, частичная отправка, продолжение, замена прошлой версии и защита
от дублей. Публикация в каналы не используется: готовый анонс приходит
только в личный чат автора.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from sqlalchemy import select

try:
    from announcement_store import (
        begin_announcement_publication,
        get_announcement_draft,
    )
    from announcement_wizard import WizardStage
    from handlers.announcement_wizard import AW_EXTRAS, AW_PREVIEW, CONVERSATION_NAME
    from models import User
    from template_store import get_standard_template
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        OTHER_TG,
        _callback,
        _callbacks,
        _choose_personal,
        _draft_by_id,
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


async def _personal_delivered(
    h,
    *,
    content: str | None = None,
    extended_content: str | None = None,
    specs: list[tuple[str, str, str, dict[str, object]]] | None = None,
):
    """Личный шаблон: последний ответ сразу доставляет анонс в этот чат."""
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        content=content,
        extended_content=extended_content,
        specs=specs,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Значение")
    # Poster 2.0 S3 п.6: последний кирпичик ведёт на шаг фото; доставка —
    # по «Без фотографий» (доставка как функция не менялась).
    await _no_photos(h)
    if "✅ Анонс готов." not in h.screen_text():
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    draft = _latest_draft(h)
    return draft, bricks


async def _standard_delivered(h):
    """Стандартный шаблон места: пропуск последнего вопроса доставляет."""
    place_id, _alias_id = _seed_place(h)
    with h.session_factory() as session:
        standard_id = get_standard_template(
            session, DEFAULT_USER_ID, place_id
        ).template_id
    await _start_to_template(h, place_id)
    await h.callback(_callback(h, "aw_template", f":{standard_id}"))
    for _ in range(20):
        skips = _callbacks(h, "aw_qskip")
        if not skips:
            break
        await h.callback(skips[0])
    await _no_photos(h)
    if "✅ Анонс готов." not in h.screen_text():
        raise AssertionError(f"Анонс не доставлен: {h.screen_text()}")
    return _latest_draft(h)


async def _open_photos(h) -> None:
    """
    Открывает коллекцию фото доставленного анонса.

    U2b: с экрана завершения сначала открывается компактный экран правки,
    и уже на нём есть категория «Фотографии».
    """
    if not _callbacks(h, "aw_photos"):
        await _open_edit(h)
    await h.callback(_callback(h, "aw_photos"))
    if "Фотографии анонса" not in h.screen_text():
        raise AssertionError(f"Не экран фотографий: {h.screen_text()}")


def _post_types(h, types: set[str]) -> list[dict]:
    return [call for call in h.calls if call["type"] in types]


def _stored(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementAutoDeliveryStandardTests(unittest.TestCase):
    @scenario_test
    async def test_standard_zero_photos_delivered_by_last_skip(self, h) -> None:
        await h.text("/start")
        draft = await _standard_delivered(h)

        # Финальной кнопки нет: доставка произошла последним ответом.
        self.assertFalse(_callbacks(h, "aw_pub_start"))
        self.assertFalse(_callbacks(h, "aw_pub_confirm"))
        self.assertFalse(_callbacks(h, "aw_send"))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

        posts = h.posts()
        self.assertEqual(len(posts), 1)
        self.assertIn("Дом культуры", posts[0] or "")
        self.assertIn("ул. Тестовая, 1", posts[0] or "")

        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(stored.stage, WizardStage.COMPLETED)
        self.assertIsNotNone(stored.published_at)
        self.assertEqual(stored.publication_photos, ())
        self.assertIn("Дом культуры", stored.publication_text or "")
        self.assertNotIn("[", stored.publication_text or "")
        self.assertTrue(stored.delivered_message_ids)
        self.assertTrue(stored.delivery_fingerprint)

        # Кнопки управления — в отдельном сообщении, а не в тексте анонса.
        self.assertIn("✅ Анонс готов.", h.screen_text())
        from tests.integration_harness import is_screen_record

        # U5: анонс без фото доставляется промоушеном живого блока — сообщение
        # создано живым блоком и доведено правкой до финального текста. Оно
        # остаётся постом без клавиатуры управления; второй копии нет.
        post_records = h.post_records()
        screen_records = [call for call in h.calls if is_screen_record(call)]
        self.assertEqual(len(post_records), 1)
        self.assertIsNone(post_records[0].get("reply_markup"))
        self.assertTrue(screen_records)
        self.assertEqual(h.posts()[0], stored.publication_text)

        # Старый callback подтверждения больше не существует и не отправляет.
        posts_after = len(h.post_records())
        await h.callback(f"aw_pub_confirm:{draft.id}:{draft.revision}")
        self.assertEqual(len(h.post_records()), posts_after)
        self.assertEqual(_stored(h, draft.id).published_at, stored.published_at)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementAutoDeliveryPhotoTests(unittest.TestCase):
    @scenario_test
    async def test_photos_view_replace_restart_and_deliver(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        self.assertEqual(len(h.posts()), 1)
        first_delivery_ids = set(_stored(h, draft.id).delivered_message_ids)
        self.assertTrue(first_delivery_ids)

        # H3: фото к доставленному анонсу применяются сразу — каждая
        # подтверждённая партия заменяет прошлую версию без «Применить».
        await _open_photos(h)
        first_update = await h.photo("photo-1", media_group_id="album-1")
        await h.photo("photo-2", media_group_id="album-1")
        stored = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(stored.photos)), ("photo-1", "photo-2"))
        revision = stored.revision
        deleted_inputs = [
            call["args"][1]
            for call in h.calls
            if call["type"] == "delete_message" and len(call["args"]) > 1
        ]
        self.assertIn(first_update.message.message_id, deleted_inputs)
        # Кнопки применения нет: версия заменилась сама.
        self.assertFalse(_callbacks(h, "aw_apply"))
        after_album = _stored(h, draft.id)
        self.assertEqual(after_album.publication_photos, ("photo-1", "photo-2"))
        self.assertEqual(
            [
                getattr(item, "media", None)
                for item in _post_types(h, {"media_group"})[-1]["media"]
            ],
            ["photo-1", "photo-2"],
        )

        # Повторный Update того же file_id не создаёт второе вложение и
        # не отправляет ещё одну версию.
        posts_before_duplicate = len(h.posts())
        await h.photo("photo-1")
        duplicate = _draft_by_id(h, draft.id)
        self.assertEqual(duplicate.revision, revision)
        self.assertEqual(tuple(json.loads(duplicate.photos)), ("photo-1", "photo-2"))
        self.assertEqual(len(h.posts()), posts_before_duplicate)

        # Рестарт теряет user_data, но фото и доставка остаются в SQLite.
        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("✅ Анонс готов.", h.screen_text())
        # H3: применённое изменение не оставляет «неприменённых» — экран
        # завершения показывает ровно свой фиксированный текст.
        self.assertNotIn("Есть изменения", h.screen_text())
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

        # Удаление одного фото и добавление нового — замена без фото-кирпичиков.
        await _open_photos(h)
        before_delete = _stored(h, draft.id)
        await h.callback(_callback(h, "aw_photo_del"))
        after_delete = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(after_delete.photos)), ("photo-2",))
        # H3: удаление тоже применено сразу, прошлая версия заменена.
        deleted_version = _stored(h, draft.id)
        self.assertEqual(deleted_version.publication_photos, ("photo-2",))
        self.assertEqual(
            set(deleted_version.previous_message_ids),
            set(before_delete.delivered_message_ids),
        )

        await h.photo("photo-3")
        after_add = _draft_by_id(h, draft.id)
        self.assertEqual(tuple(json.loads(after_add.photos)), ("photo-2", "photo-3"))

        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_photos, ("photo-2", "photo-3"))
        self.assertEqual(sent.photos, ("photo-2", "photo-3"))
        self.assertIn("Значение", sent.publication_text or "")
        self.assertNotIn("[фото]", sent.publication_text or "")
        self.assertEqual(
            [
                getattr(item, "media", None)
                for item in _post_types(h, {"media_group"})[-1]["media"]
            ],
            ["photo-2", "photo-3"],
        )
        new_ids = set(sent.delivered_message_ids)
        self.assertTrue(new_ids)
        # id прошлой версии не смешиваются с id текущей доставки.
        self.assertFalse(new_ids & first_delivery_ids)
        self.assertTrue(set(sent.previous_message_ids))
        self.assertFalse(set(sent.previous_message_ids) & new_ids)
        # Первая версия удалена после первой успешной замены.
        self.assertTrue(first_delivery_ids <= set(h.deleted_ids()))

        # Возврат в меню с экрана коллекции не отправляет копию и не
        # откатывает применённую версию.
        posts_before = len(h.posts())
        await h.callback("main_menu")
        self.assertIn("Что хотите сделать?", h.screen_text())
        self.assertEqual(len(h.posts()), posts_before)
        self.assertEqual(
            _stored(h, draft.id).publication_photos, ("photo-2", "photo-3")
        )

    @scenario_test
    async def test_single_photo_caption_failure_is_failed_and_resumes(self, h) -> None:
        """Сбой единственной операции (фото с подписью) — честный `failed`.

        S4 §5.1: текст с фото не режется на части и не уходит отдельным
        сообщением, поэтому отправка — ровно одна операция. Пока Telegram не
        подтвердил её, статус `failed` (а не `partial`): успех фиксируется
        только когда доставлено ВСЁ. Прошлая версия остаётся в чате, а
        продолжение досылает ровно один снимок — без дубля.
        """
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        await _open_photos(h)
        posts_before = len(h.posts())
        # Сбой Telegram на первой же замене версии: фото уже сохранено, но
        # доставка (H3 — сразу после приёма снимка) не подтверждена.
        h.bot.fail_after["photo"] = 0
        rejected = await h.photo("caption-photo")

        failed = _stored(h, draft.id)
        self.assertEqual(failed.publication_status, "failed")
        self.assertEqual(failed.publication_step, 0)
        self.assertEqual(failed.publication_total, 1)
        self.assertEqual(failed.publication_progress["ids"], [])
        self.assertFalse(failed.publication_can_reset)
        self.assertIn("не подтвердил ни одного шага отправки", h.screen_text())
        self.assertIn(
            "Повторить отправку", " ".join(label for label, _data in h.screen_buttons())
        )
        self.assertFalse(_callbacks(h, "aw_pub_reset"))
        self.assertEqual(len(h.posts()), posts_before, "новая версия не ушла")
        # Прошлая доставленная версия остаётся в чате до успеха новой:
        # её адреса сохранены, но не выдаются за id текущей отправки.
        self.assertTrue(failed.previous_message_ids)
        self.assertEqual(failed.delivered_message_ids, ())
        self.assertIn("Предыдущая версия анонса остаётся в чате", h.screen_text())
        # Ввод-фото не удалён: подтверждённой доставки не было.
        self.assertNotIn(rejected.message.message_id, h.deleted_ids())

        h.bot.fail_after.clear()
        photos_before = len(_post_types(h, {"photo"}))
        old_ids = set(failed.previous_message_ids)
        await h.callback(_callback(h, "aw_pub_resume"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        # Фото отправлено ровно один раз: подтверждённых операций не было.
        self.assertEqual(len(_post_types(h, {"photo"})), photos_before + 1)
        self.assertIn("Значение", h.posts()[-1] or "")
        # Прошлая версия удалена только после успеха новой.
        self.assertTrue(old_ids <= set(h.deleted_ids()))

    @scenario_test
    async def test_eleven_photos_are_capped_and_delivered_as_one_group(self, h) -> None:
        """Лимит 10: 11-е фото отклонено, принятые 10 уходят одной группой."""
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        await _open_photos(h)
        for index in range(11):
            update = await h.photo(f"group-photo-{index}")
        stored = _draft_by_id(h, draft.id)
        self.assertEqual(
            tuple(json.loads(stored.photos)),
            tuple(f"group-photo-{index}" for index in range(10)),
        )
        # Отказ объяснён на экране, а принятые фото не потеряны.
        self.assertIn("больше добавить нельзя", h.screen_text())
        self.assertIn("1", h.screen_text())
        # Сообщение, которым прислали 11-е фото, не удалено: автор видит,
        # что именно не принято.
        self.assertNotIn(update.message.message_id, h.deleted_ids())

        # H3: каждый принятый снимок применяется сразу, поэтому важно не
        # число отправок, а итог: последняя версия несёт все 10 фото одной
        # группой, а каждая следующая версия заменяет прошлую.
        self.assertFalse(_callbacks(h, "aw_apply"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(sent.publication_total, 1)
        self.assertEqual(
            sent.publication_photos,
            tuple(f"group-photo-{index}" for index in range(10)),
        )
        groups = _post_types(h, {"media_group"})
        self.assertTrue(groups, "фото так и не ушли")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[-1]["media"]],
            [f"group-photo-{index}" for index in range(10)],
        )
        self.assertEqual(
            [getattr(item, "caption", None) for item in groups[-1]["media"]][:-1],
            [None] * 9,
            "подпись разрешена только у последнего фотосегмента",
        )
        # В чате ровно одна актуальная версия: прошлые удалены.
        self.assertTrue(set(sent.previous_message_ids))
        self.assertFalse(
            set(sent.previous_message_ids) & set(sent.delivered_message_ids)
        )
        self.assertTrue(
            set(sent.previous_message_ids) <= set(h.deleted_ids()),
            "прошлые версии не удалены",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementAutoDeliveryFailureTests(unittest.TestCase):
    @scenario_test
    async def test_failed_first_delivery_keeps_data_and_allows_retry(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)

        await h.text("Значение")
        # S3 п.6: сначала шаг фотографий; доставку запускает «Без фотографий».
        self.assertIn("Фотографии анонса", h.screen_text())

        # U5: без фото готовым постом становится живой блок, и первая
        # доставка не зовёт send_message вообще. Чтобы проверить именно сбой
        # отправки, убираем доказанное состояние блока (адреса в БД и в
        # сессии): промоушен станет невозможен, а пересборка блока и
        # резервная отправка упрутся в отказ Telegram.
        from announcement_store import set_announcement_live_message_ids
        from handlers import wizard_live

        with h.session_factory() as session:
            set_announcement_live_message_ids(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=_latest_draft(h).id,
                message_ids=[],
            )
        wizard_live.forget_live_state(h.user_data())

        # Сбой Telegram на самой первой отправке: данные и черновик целы.
        h.bot.fail_after["message"] = 0
        await h.callback(_callback(h, "aw_photo_none"))

        draft = _latest_draft(h)
        failed = _stored(h, draft.id)
        self.assertEqual(failed.publication_status, "failed")
        self.assertEqual(failed.publication_step, 0)
        self.assertTrue(failed.publication_can_reset)
        self.assertIn("не подтвердил ни одного шага отправки", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_reset"))
        self.assertEqual(h.posts(), [])

        await h.callback(_callback(h, "aw_pub_reset"))
        reset = _stored(h, draft.id)
        self.assertIsNone(reset.publication_status)
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertIn("Значение", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_send"))
        # Значение не потеряно: данные остались в черновике.
        self.assertIsNone(reset.publication_text)
        self.assertIn("Основное поле", h.screen_text())

        # Повтор — только явным действием автора, без автоматического ретрая.
        h.bot.fail_after.clear()
        await h.callback(_callback(h, "aw_send"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertIn("✅ Анонс готов.", h.screen_text())
        self.assertEqual(len(h.post_records()), 1)

    @scenario_test
    async def test_restart_during_sending_resumes_frozen_payload(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            begin_announcement_publication(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                expected_revision=details.revision,
                today=date.today(),
            )

        await h.restart()
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        self.assertIn("Отправка не завершена", h.screen_text())
        self.assertTrue(_callbacks(h, "aw_pub_resume"))
        self.assertFalse(_callbacks(h, "aw_pub_start"))

        await h.callback(_callback(h, "aw_pub_resume"))
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertIn("✅ Анонс готов.", h.screen_text())

    @scenario_test
    async def test_foreign_callback_cannot_deliver(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Чужое изменение")
        # H3: «Применить изменения» больше нет, версия меняется сохранением.
        # Чужой callback пытается открыть правку по актуальной ревизии.
        self.assertFalse(_callbacks(h, "aw_apply"))
        stored = _stored(h, draft.id)
        send_data = f"aw_edit:{draft.id}:{stored.revision}"

        with h.session_factory() as session:
            other = session.scalar(
                select(User).where(User.telegram_user_id == OTHER_TG)
            )
            if other is None:
                other = User(
                    telegram_user_id=OTHER_TG,
                    username="foreign-stage28",
                    full_name="Чужой",
                )
                session.add(other)
                session.commit()

        posts_before = len(h.post_records())
        version_ids = tuple(stored.delivered_message_ids)
        await h.callback(send_data, user_id=OTHER_TG)
        self.assertEqual(len(h.post_records()), posts_before)
        stored = _stored(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(tuple(stored.delivered_message_ids), version_ids)
        # Чужая правка не открылась и не изменила сохранённое значение:
        # в тексте остаётся ровно то, что применил сам автор.
        self.assertEqual(stored.publication_text, "Чужое изменение")

    @scenario_test
    async def test_group_chat_never_receives_auto_delivery(self, h) -> None:
        """Мастер можно открыть в группе, но анонс туда не уходит."""
        group_id = 900_001
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(h)

        # Весь сбор идёт в групповом чате.
        await h.callback("aw_start", chat_id=group_id, chat_type="group")
        await h.callback("aw_place_search", chat_id=group_id, chat_type="group")
        await h.text("Дом", chat_id=group_id, chat_type="group")
        await h.callback(
            f"aw_place_open:{place_id}", chat_id=group_id, chat_type="group"
        )
        await h.callback(
            _callback(h, "aw_place_confirm"), chat_id=group_id, chat_type="group"
        )
        await h.callback(
            _callback(h, "aw_template", f":{template_id}"),
            chat_id=group_id,
            chat_type="group",
        )
        await h.text("Секретное значение", chat_id=group_id, chat_type="group")
        # S3 п.6: последний кирпичик ведёт на шаг фото; попытка завершить
        # анонс в группе отклоняется до вызова Telegram.
        self.assertIn("Фотографии анонса", h.screen_text())
        await h.callback(
            _callback(h, "aw_photo_none"), chat_id=group_id, chat_type="group"
        )

        # Ни одного поста: отказ до вызова Telegram.
        self.assertEqual(h.posts(), [])
        self.assertIn("только в личный чат", h.screen_text())
        draft = _latest_draft(h)
        stored = _stored(h, draft.id)
        self.assertIsNone(stored.publication_status)
        self.assertFalse(stored.delivered_message_ids)

        # Тот же черновик завершается в личном чате — там доставка проходит.
        await h.text("/start")
        await h.callback("aw_start")
        await h.callback(_callback(h, "aw_open", f":{draft.id}"))
        await h.callback(_callback(h, "aw_send"))
        self.assertEqual(len(h.posts()), 1)
        self.assertIn("Секретное значение", h.posts()[0] or "")
        self.assertTrue(_stored(h, draft.id).delivered_message_ids)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class AnnouncementRedeliveryTests(unittest.TestCase):
    @scenario_test
    async def test_edit_rebuild_keeps_old_version_until_new_success(self, h) -> None:
        """Успешная правка: новое сообщение выше, прошлое удалено после."""
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        first = _stored(h, draft.id)
        first_ids = tuple(first.delivered_message_ids)
        first_fingerprint = first.delivery_fingerprint
        self.assertEqual(len(h.posts()), 1)

        # Меняем значение через управление полями.
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Обновлённое")

        # H3: ответ сразу применил правку — отдельной кнопки нет, а
        # «неприменённых» изменений на экране завершения не бывает.
        self.assertFalse(_callbacks(h, "aw_apply"))
        self.assertNotIn("Есть изменения", h.screen_text())
        posts = h.posts()
        self.assertEqual(len(posts), 2)
        self.assertIn("Обновлённое", posts[-1] or "")

        second = _stored(h, draft.id)
        second_ids = tuple(second.delivered_message_ids)
        self.assertTrue(second_ids)
        self.assertFalse(set(second_ids) & set(first_ids))
        # Прошлые адреса сохранены отдельно и не выдаются за текущие.
        self.assertEqual(tuple(second.previous_message_ids), first_ids)
        self.assertNotEqual(second.delivery_fingerprint, first_fingerprint)
        # Удаление прошлой версии произошло только после успеха новой.
        self.assertTrue(set(first_ids) <= set(h.deleted_ids()))

        # Ещё одна правка заменяет уже вторую версию, а не первую.
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Третье")
        third = _stored(h, draft.id)
        self.assertEqual(tuple(third.previous_message_ids), second_ids)
        self.assertIn("Третье", h.posts()[-1] or "")
        self.assertEqual(len(h.posts()), 3)

    @scenario_test
    async def test_deletion_failure_of_old_version_keeps_new_one_available(
        self, h
    ) -> None:
        """Telegram не удаляет сообщения старше 48 часов — это не сбой."""
        await h.text("/start")
        draft, _bricks = await _personal_delivered(h)
        first_ids = tuple(_stored(h, draft.id).delivered_message_ids)

        from telegram.error import BadRequest

        h.bot.errors_by_type["delete_message"] = BadRequest(
            "Message can't be deleted for everyone"
        )
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Новая версия")

        # Неудача удаления не откатывает успешную доставку: новая версия есть,
        # статус остаётся sent, прошлая копия просто осталась в чате.
        self.assertIn("✅ Анонс готов.", h.screen_text())
        self.assertIn("Новая версия", h.posts()[-1] or "")
        sent = _stored(h, draft.id)
        self.assertEqual(sent.publication_status, "sent")
        self.assertEqual(tuple(sent.previous_message_ids), first_ids)
        self.assertTrue(sent.delivered_message_ids)
        self.assertFalse(set(sent.delivered_message_ids) & set(first_ids))
        self.assertFalse(set(first_ids) & set(h.deleted_ids()))

    @scenario_test
    async def test_extras_edit_after_delivery_rebuilds_post(self, h) -> None:
        """Доп. кирпичик: значение применяется пересборкой и заменой."""
        await h.text("/start")
        specs: list[tuple[str, str, str, dict[str, object]]] = [
            ("main", "Основное", "short_text", {}),
            ("extra0", "Доп 0", "short_text", {}),
        ]
        draft, bricks = await _personal_delivered(
            h,
            specs=specs,
            content="[main]",
            # Poster 2.3.1 (E1): кандидат в доп. поля — только кирпичик
            # расширенного текста шаблона.
            extended_content="[main]\n[extra0]",
        )
        first_ids = tuple(_stored(h, draft.id).delivered_message_ids)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_extras"))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_EXTRAS)

        await h.callback(_callback(h, "aw_exadd"))
        for _attempt in range(4):
            if _callbacks(h, "aw_exbrick", f":{bricks['extra0']}"):
                break
            next_page = [
                data for label, data in h.screen_buttons() if label == "▶️" and data
            ]
            if not next_page:
                break
            await h.callback(next_page[0])
        await h.callback(_callback(h, "aw_exbrick", f":{bricks['extra0']}"))
        # H2: секций и позиций больше нет — кирпичик сразу ждёт значение.
        await h.text("Дополнительно")
        self.assertIn("Дополнительно", h.screen_text())

        # H3: значение применилось сразу, без кнопки «Применить изменения».
        self.assertFalse(_callbacks(h, "aw_apply"))
        self.assertIn("Дополнительно", h.posts()[-1] or "")
        sent = _stored(h, draft.id)
        self.assertEqual(tuple(sent.previous_message_ids), first_ids)
        self.assertTrue(set(first_ids) <= set(h.deleted_ids()))

        # Удаление доп. кирпичика тоже приводит к новой версии. H3: после
        # сохранения значения остаёшься в списке дополнительных полей.
        self.assertIn("Доп 0", h.screen_text())
        await h.callback(_callback(h, "aw_exdel"))
        self.assertIn("Дополнительных полей пока нет", h.screen_text())
        # Удаление тоже применилось сразу: канонический текст без extras.
        self.assertNotIn("Дополнительно", h.posts()[-1] or "")


if __name__ == "__main__":
    unittest.main()
