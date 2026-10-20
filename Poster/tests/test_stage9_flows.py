# tests/test_stage9_flows.py
"""
Stage 9: интеграционные сценарии черновиков и фотографий.

Всё идёт через Application.process_update реального приложения:
- полный цикл черновика: сохранение → список → открытие → правка →
  обновление без копий → удаление → пагинация → изоляция пользователей;
- автосохранение незавершённого поста при всех выходах;
- TTL черновиков с учётом updated_at;
- фото: одиночное, замена, альбом, «Фото готово», замена альбома,
  запоздавшие фото, инвариант preview == final (включая фото и порядок).

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest
from datetime import timedelta

try:
    from sqlalchemy import update as sa_update

    import handlers.jobs as jobs_module
    from handlers.post_creation import EDIT_FIELD, POST_CREATION, POST_STEPS
    from models import Draft, utcnow
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        call_text,
        goto_preview,
        inline_buttons,
        scenario_test,
    )
    from utils.tg_html import escape_html

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DraftLifecycleTests(unittest.TestCase):
    """Черновики через реальный диалог: цикл, страницы, удаление, TTL."""

    @scenario_test
    async def test_save_reopen_edit_update_without_duplicates(self, h):
        # Сохранение на превью
        await goto_preview(h, title="Черновик в работе")
        await h.callback("save_draft")

        self.assertIn("Пост сохранён в черновики", h.reply_texts()[-1])
        self.assertIsNone(h.dialog_state())
        self.assertNotIn("title", h.user_data(), "данные чистятся после сохранения")
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "Черновик в работе")
        self.assertEqual(drafts[0].user_id, 7)

        # Список: карточка + кнопки действий
        await h.text("/drafts")
        listing = h.replies()[-1]
        self.assertIn("Ваши черновики", listing["text"])
        self.assertEqual(listing["parse_mode"], "HTML")
        self.assertEqual(
            inline_buttons(listing["reply_markup"]),
            [
                ("✏️ Редактировать черновик 1", "editdraft_1"),
                ("❌ Удалить черновик 1", "delete_1"),
                ("↩️ Главное меню", "main_menu"),
            ],
        )

        # Открытие черновика — диалог редактора с его данными
        await h.callback("editdraft_1")
        self.assertEqual(len(h.answers()), 2, "открытие отвечает на callback")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("title"), "Черновик в работе")
        self.assertEqual(h.user_data().get("editing_draft_id"), 1)
        self.assertIn("Черновик в работе", h.posts()[-1])

        # Правка + повторное сохранение — обновление, а не копия
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Отредактированный черновик")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        await h.callback("save_draft")

        self.assertIn("Черновик 1 обновлён", h.reply_texts()[-1])
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1, "дубликатов быть не должно")
        self.assertEqual(drafts[0].title, "Отредактированный черновик")
        self.assertIsNone(h.dialog_state())
        self.assertNotIn("title", h.user_data())

    @scenario_test
    async def test_drafts_pagination_walks_all_pages(self, h):
        session = h.session_factory()
        try:
            for i in range(17):
                session.add(Draft(user_id=7, title=f"Черновик {i + 1}"))
            session.commit()
        finally:
            session.close()

        await h.text("/drafts")
        first_page = h.replies()[-1]
        self.assertIn("Показано 15 из 17", first_page["text"])
        buttons = inline_buttons(first_page["reply_markup"])
        self.assertIn(("Вперёд ➡️", "draftpage_15"), buttons)
        self.assertNotIn(("⬅️ Назад", "draftpage_0"), buttons, "первая страница")
        self.assertEqual(len([b for b in buttons if b[1].startswith("editdraft_")]), 15)

        # Вперёд → вторая страница (редактируется то же сообщение)
        await h.callback("draftpage_15")
        self.assertEqual(len(h.answers()), 1)
        second_page = h.calls[-1]
        self.assertEqual(second_page["type"], "edit_message")
        self.assertIn("Показано 2 из 17", call_text(second_page) or "")
        buttons2 = inline_buttons(second_page["reply_markup"])
        self.assertIn(("⬅️ Назад", "draftpage_0"), buttons2)
        self.assertFalse(any(data == "draftpage_15" for _, data in buttons2))

        # Назад → первая страница
        await h.callback("draftpage_0")
        self.assertEqual(len(h.answers()), 2)
        back = h.calls[-1]
        self.assertIn("Показано 15 из 17", call_text(back) or "")

        # Листание не порождает новых сообщений — только правка старого
        self.assertEqual(len([c for c in h.calls if c["type"] == "message"]), 1)

    @scenario_test
    async def test_delete_draft_is_owner_scoped(self, h):
        await goto_preview(h, title="Удаляемый черновик")
        await h.callback("save_draft")
        answers_before = len(h.answers())

        # Чужой пользователь не может удалить (отказ + свой пустой список)
        await h.callback("delete_1", user_id=8)
        edits = [c for c in h.calls if c["type"] == "edit_message"]
        self.assertIn("Черновик не найден", call_text(edits[-1]) or "")
        self.assertEqual(len(h.drafts()), 1)
        self.assertIn("нет черновиков", h.reply_texts()[-1])

        # Владелец удаляет — список сразу перерисовывается
        await h.callback("delete_1")
        edits = [c for c in h.calls if c["type"] == "edit_message"]
        self.assertIn("Черновик 1 удалён", call_text(edits[-1]) or "")
        self.assertEqual(h.drafts(), [])
        self.assertIn("нет черновиков", h.reply_texts()[-1])
        self.assertEqual(len(h.answers()), answers_before + 2, "обе кнопки отвечены")

    @scenario_test
    async def test_foreign_user_cannot_see_or_open_draft(self, h):
        await goto_preview(h, title="Мой черновик")
        await h.callback("save_draft")

        # Открытие чужого черновика — отказ, диалог НЕ начинается
        await h.callback("editdraft_1", user_id=8)
        self.assertIn("Черновик не найден", h.reply_texts()[-1])
        self.assertIsNone(h.dialog_state(user_id=8))
        self.assertIsNone(h.dialog_state())

        # Список чужого пользователя пуст
        await h.text("/drafts", user_id=8)
        self.assertIn("нет черновиков", h.reply_texts()[-1])
        self.assertEqual(h.drafts(user_id=8), [])

    @scenario_test
    async def test_every_exit_saves_work_in_progress(self, h):
        exits = ("/cancel", "/start", "/drafts", "📝 Черновики")
        for number, exit_trigger in enumerate(exits, start=1):
            with self.subTest(exit=exit_trigger):
                before = len(h.drafts())
                await h.start_dialog()
                await h.text(f"ВИП перед выходом {number}")

                await h.text(exit_trigger)

                self.assertIsNone(h.dialog_state(), "диалог должен завершиться")
                drafts = h.drafts()
                self.assertEqual(len(drafts), before + 1, "ВИП должен сохраниться")
                self.assertEqual(drafts[-1].title, f"ВИП перед выходом {number}")
                self.assertNotIn("title", h.user_data())
                # Пользователь получил ответ, а не тишину
                self.assertTrue(h.reply_texts())

    @scenario_test
    async def test_cancel_updates_opened_draft_instead_of_copying(self, h):
        await goto_preview(h, title="Исходный черновик")
        await h.callback("save_draft")
        await h.callback("editdraft_1")
        self.assertEqual(h.dialog_state(), POST_CREATION)

        # Несохранённые правки + /cancel → обновление того же черновика
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Правленый ВИП")
        await h.text("/cancel")

        self.assertIn("Создание поста отменено", h.reply_texts()[-1])
        self.assertIsNone(h.dialog_state())
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1, "копия не должна создаваться")
        self.assertEqual(drafts[0].title, "Правленый ВИП")
        self.assertNotIn("title", h.user_data())

    @scenario_test
    async def test_ttl_counts_from_updated_at(self, h):
        # updated_at имеет default=utcnow — явные метки ставим UPDATE'ом
        # (в UPDATE onupdate не срабатывает для перечисленных колонок)
        session = h.session_factory()
        try:
            session.add_all(
                [
                    Draft(user_id=7, title="Протухший"),
                    Draft(user_id=7, title="Правленый недавно"),
                    Draft(user_id=7, title="Свежий"),
                ]
            )
            session.commit()
            stale = session.query(Draft).filter_by(title="Протухший").one()
            touched = session.query(Draft).filter_by(title="Правленый недавно").one()
            fresh = session.query(Draft).filter_by(title="Свежий").one()

            old = utcnow() - timedelta(days=41)
            session.execute(
                sa_update(Draft)
                .where(Draft.id == stale.id)
                .values(created_at=old, updated_at=None)
            )
            session.execute(
                sa_update(Draft)
                .where(Draft.id == touched.id)
                .values(created_at=old, updated_at=utcnow() - timedelta(days=1))
            )
            session.execute(
                sa_update(Draft)
                .where(Draft.id == fresh.id)
                .values(created_at=utcnow() - timedelta(days=1), updated_at=None)
            )
            session.commit()
            touched_id, fresh_id = touched.id, fresh.id
        finally:
            session.close()

        jobs_module.sync_remove_old_drafts()

        remaining = sorted(d.id for d in h.drafts())
        self.assertEqual(
            remaining,
            sorted([touched_id, fresh_id]),
            "удаляется только неиспользуемый старый черновик",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PhotoAlbumTests(unittest.TestCase):
    """Фото и альбомы: сбор, завершение, замена, запоздавшие снимки."""

    @scenario_test
    async def test_single_photo_and_replacement_via_edit(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("photo-1")
        self.assertIn("Фото добавлено.", h.reply_texts()[-1])
        first_preview = h.calls[-1]
        self.assertEqual(first_preview["type"], "photo")

        # Замена фото через «Редактировать» → «Изображение»
        await h.callback("edit_post")
        await h.callback("edit_image")
        record = h.calls[-1]
        self.assertEqual(record["type"], "edit_message")
        self.assertIn("Отправьте новое изображение", call_text(record) or "")
        self.assertEqual(h.dialog_state(), EDIT_FIELD)

        await h.photo("photo-2")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        second_preview = h.calls[-1]
        self.assertEqual(second_preview["type"], "photo")
        self.assertEqual(second_preview["photo"], "photo-2")

        # Готовый пост уходит с новым фото и текстом превью
        await h.callback("finish")
        finals = [c for c in h.calls if c["type"] == "photo"]
        self.assertEqual(len(finals), 3, "два превью + готовый пост")
        self.assertEqual(finals[-1]["photo"], "photo-2")
        self.assertEqual(finals[-1]["caption"], second_preview["caption"])
        self.assertIsNone(finals[-1].get("reply_markup"))
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_album_collects_until_done_then_preview_equals_final(self, h):
        await h.goto_step(step_index("image"))

        await h.photo("a1", media_group_id="mg-1")
        first_reply = h.reply_texts()[-1]
        self.assertIn("Получаю фотографии альбома", first_reply)
        self.assertEqual(
            inline_buttons(h.replies()[-1]["reply_markup"]),
            [("✅ Фото готово", "media_done")],
        )

        # Остальные фото альбома добавляются молча (без спама подтверждений)
        replies_before = len(h.reply_texts())
        await h.photo("a2", media_group_id="mg-1")
        await h.photo("a3", media_group_id="mg-1")
        self.assertEqual(len(h.reply_texts()), replies_before)

        await h.callback("media_done")
        self.assertIn("Фотографии добавлены", h.reply_texts()[-2])
        self.assertEqual(h.dialog_state(), POST_CREATION)

        # Превью альбома: сама медиа-группа + lead-сообщение с кнопками
        # (send_post отправляет альбом раньше lead-сообщения)
        lead = [c for c in h.calls if c["type"] == "message"][-1]
        self.assertEqual(lead["text"], "Выберите действие:")
        self.assertEqual(
            [data for _, data in inline_buttons(lead["reply_markup"])],
            ["edit_post", "save_draft", "finish"],
        )
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual([m.media for m in preview_album["media"]], ["a1", "a2", "a3"])
        preview_caption = preview_album["media"][0].caption
        self.assertIsNotNone(preview_caption)

        # Готовый пост: тот же состав, тот же порядок, та же подпись
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        albums = [c for c in h.calls if c["type"] == "media_group"]
        self.assertEqual(len(albums), 2, "превью и готовый пост")
        final_album = albums[-1]
        self.assertEqual([m.media for m in final_album["media"]], ["a1", "a2", "a3"])
        self.assertEqual(final_album["media"][0].caption, preview_caption)
        self.assertEqual(h.reply_texts()[-1], "Пост готов.")

    @scenario_test
    async def test_late_album_photo_after_done_does_not_change_post(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("a1", media_group_id="mg-1")
        await h.callback("media_done")
        preview = h.calls[-1]
        self.assertEqual(preview["type"], "photo")

        # Запоздавшие фото альбома: подсказка (одна на альбом) + дедупликация
        await h.photo("a2", media_group_id="mg-1")
        self.assertIn("Редактировать", h.reply_texts()[-1])
        replies_before = len(h.reply_texts())
        await h.photo("a3", media_group_id="mg-1")
        self.assertEqual(len(h.reply_texts()), replies_before, "дедуп по альбому")
        self.assertEqual(h.user_data()["photos"], ["a1"], "состав не изменился")

        # preview == final: запоздавшие фото не попали в пост
        await h.callback("finish")
        finals = [c for c in h.calls if c["type"] == "photo"]
        self.assertEqual(finals[-1]["photo"], "a1")
        self.assertEqual(finals[-1]["caption"], preview["caption"])

    @scenario_test
    async def test_replace_album_via_edit_field(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("a1", media_group_id="mg-1")
        await h.photo("a2", media_group_id="mg-1")
        await h.callback("media_done")
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual([m.media for m in preview_album["media"]], ["a1", "a2"])

        # Замена альбома в правке поля «Изображение»
        await h.callback("edit_post")
        await h.callback("edit_image")
        await h.photo("b1", media_group_id="mg-2")
        self.assertEqual(h.dialog_state(), EDIT_FIELD, "идёт сбор нового альбома")
        await h.photo("b2", media_group_id="mg-2")
        await h.callback("media_done")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        preview_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual([m.media for m in preview_album["media"]], ["b1", "b2"])
        preview_caption = preview_album["media"][0].caption

        await h.callback("finish")
        final_album = [c for c in h.calls if c["type"] == "media_group"][-1]
        self.assertEqual([m.media for m in final_album["media"]], ["b1", "b2"])
        self.assertEqual(final_album["media"][0].caption, preview_caption)
        self.assertEqual(h.user_data().get("photos"), None, "данные очищены")

    @scenario_test
    async def test_media_done_without_photos_keeps_image_step(self, h):
        await h.goto_step(step_index("image"))

        await h.callback("media_done")

        self.assertIn("Фотографии не получены", h.reply_texts()[-1])
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(
            h.user_data().get("current_step"),
            step_index("image"),
            "шаг не должен продвигаться без фото",
        )
        self.assertEqual(
            inline_buttons(h.replies()[-1]["reply_markup"]),
            [("Пропустить", "skip")],
        )

        # После отказа шаг корректно пропускается
        await h.text("Пропустить")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIsNotNone(h.posts()[-1])

    @scenario_test
    async def test_photo_outside_image_step_explains_and_keeps_data(self, h):
        # На шаге «Дата» — подсказка, шаг и данные не тронуты
        await h.start_dialog()
        await h.text("Концерт")
        step_before = h.user_data().get("current_step")
        await h.photo("stray-1")
        self.assertIn("Фото можно добавить на шаге", h.reply_texts()[-1])
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("current_step"), step_before)
        self.assertEqual(h.user_data().get("title"), "Концерт")
        self.assertNotIn("photos", h.user_data())

        # На превью — путь к замене через «Редактировать»
        await h.goto_step(step_index("image"))
        await h.text("Пропустить")
        posts_before = len(h.posts())
        await h.photo("stray-2")
        self.assertIn("Редактировать", h.reply_texts()[-1])
        self.assertEqual(len(h.posts()), posts_before)
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_photo_during_text_edit_keeps_field_value(self, h):
        await goto_preview(h, title="Концерт")
        await h.callback("edit_post")
        await h.callback("edit_title")

        await h.photo("not-here")

        self.assertEqual(h.dialog_state(), EDIT_FIELD, "правка не сорвана")
        self.assertEqual(h.user_data().get("title"), "Концерт", "значение цело")
        self.assertIn("Изображение", h.reply_texts()[-1])

    @scenario_test
    async def test_preview_final_invariant_with_mixed_fields(self, h):
        """Смешанный пост: текст, фото, спецсимволы — превью == финал."""
        await h.goto_step(step_index("title"))
        await h.text("Концерт «Хиты» (2026)!")
        await h.text("17.11.2026")
        for _ in range(len(POST_STEPS) - 3):
            await h.text("Пропустить")
        await h.photo("photo-mix")

        preview = h.calls[-1]
        await h.callback("finish")
        final = [c for c in h.calls if c["type"] == "photo"][-1]

        self.assertEqual(preview["caption"], final["caption"])
        self.assertEqual(preview["parse_mode"], final["parse_mode"])
        self.assertEqual(preview["photo"], final["photo"])
        self.assertIn(escape_html("Концерт «Хиты» (2026)!"), final["caption"])
