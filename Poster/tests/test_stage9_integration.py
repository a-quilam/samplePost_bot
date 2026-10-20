# tests/test_stage9_integration.py
"""
Stage 9: интеграционные тесты пользовательских сценариев.

Каждый тест прогоняет реальные последовательности обновлений через
Application.process_update НАСТОЯЩЕГО приложения из bot.build_application():
фильтры, ConversationHandler, глобальные команды, обработчик ошибок.
Подменяются только внешние границы (Telegram API, БД, часы) — см.
integration_harness.

Покрытие:
- полный сценарий: /start → «Создать пост» → 9 полей → превью → «Готово»;
- пропуск каждого из девяти полей, минимальный и пустой пост;
- редактирование каждого поля, повторная правка, отмена;
- устаревшие кнопки превью, текст на экране превью;
- выход в главное меню и перезапуск посреди диалога.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from handlers.post_creation import EDIT_FIELD, POST_CREATION, POST_STEPS
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


# Значения восьми текстовых полей по порядку шагов (9-й шаг — изображение)
WIZARD_VALUES = {
    "title": "Концерт рок-группы",
    "date": "17.11.2026",
    "time_start": "19:00",
    "time_end": "21:30",
    "place_name": "Большой зал",
    "text": "Ждём всех!",
    "contact": "@rock_contact",
    "place_url": "https://example.com/hall",
}

# Свежие значения для редактирования каждого поля
NEW_VALUES = {
    "title": "Новый заголовок",
    "date": "20.12.2026",
    "time_start": "20:00",
    "time_end": "22:00",
    "place_name": "Малый зал",
    "text": "Новый текст анонса",
    "contact": "@new_contact",
    "place_url": "https://example.org/hall",
    "image": "photo-edited",
}


def expected_prompts() -> list[str]:
    """Ожидаемые промпты всех девяти шагов из POST_STEPS."""
    return [
        f"Шаг {index + 1} из {len(POST_STEPS)} — {step['label']}.\n{step['prompt']}"
        for index, step in enumerate(POST_STEPS)
    ]


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class FullCreationFlowTests(unittest.TestCase):
    """Полный маршрут: /start → создание → 9 полей → превью → «Готово»."""

    @scenario_test
    async def test_nine_steps_photo_finish_preview_equals_final(self, h):
        # /start — приветствие на экране управления с inline-кнопками меню
        await h.text("/start")
        self.assertIsNone(h.dialog_state())
        self.assertEqual(h.screen_text(), "Здравствуйте! 👋\n\nВыберите действие:")
        self.assertEqual(
            [(text, data) for text, data in h.screen_buttons()],
            [
                ("✏️ Создать пост", "create_post"),
                ("📂 Черновики", "drafts"),
                ("❔ Помощь", "help"),
            ],
        )

        # «Создать пост» — диалог открыт, шаг 1
        await h.start_dialog()
        self.assertEqual(h.dialog_state(), POST_CREATION)

        # Девять полей по порядку: каждый шаг даёт следующий промпт
        for key, value in WIZARD_VALUES.items():
            await h.text(value)
            index = step_index(key) + 1
            self.assertEqual(
                h.reply_texts()[-1],
                expected_prompts()[index],
                f"после поля '{key}' должен идти промпт шага {index + 1}",
            )
            self.assertEqual(h.dialog_state(), POST_CREATION)

        # Фото — закрывает шаг «Изображение» и показывает превью
        await h.photo("photo-final-1")
        self.assertEqual(h.reply_texts()[-1], "Фото добавлено.")
        preview = h.calls[-1]
        self.assertEqual(preview["type"], "photo")
        self.assertEqual(preview["photo"], "photo-final-1")
        self.assertEqual(preview["parse_mode"], "HTML")
        self.assertEqual(
            inline_buttons(preview["reply_markup"]),
            [
                ("✏️ Редактировать", "edit_post"),
                ("📄 Сохранить в черновики", "save_draft"),
                ("✅ Готово — получить пост", "finish"),
            ],
        )

        # Превью содержит все введённые значения (HTML-экранированные) и метки
        caption = preview["caption"]
        for key, value in WIZARD_VALUES.items():
            self.assertIn(f"• <b>{POST_STEPS[step_index(key)]['label']}</b>:", caption)
            self.assertIn(escape_html(value), caption)
        self.assertIn("Добавлено", caption)  # изображение

        # Ровно девять промптов шагов — без лишних сообщений
        prompts = [t for t in h.reply_texts() if t.startswith("Шаг ")]
        self.assertEqual(prompts, expected_prompts()[: len(POST_STEPS)])

        # «✅ Готово — получить пост» → финальный пост, диалог завершён
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        self.assertEqual(len(h.answers()), 1, "callback обязан быть отвечен")
        self.assertEqual(h.reply_texts()[-1], "Пост готов.")
        menu_buttons = h.replies()[-1]["reply_markup"].inline_keyboard
        self.assertIn(
            "✏️ Создать пост",
            [b.text for row in menu_buttons for b in row],
        )

        # ПРЕВЬЮ == ГОТОВЫЙ ПОСТ: дословно одинаковый текст и фото
        posts = h.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        final = [c for c in h.calls if c["type"] == "photo"][-1]
        self.assertIsNone(final.get("reply_markup"), "у готового поста нет кнопок")
        self.assertEqual(final["photo"], "photo-final-1")

        # Данные поста очищены, все отправки сообщений — в личный чат
        for key in list(WIZARD_VALUES) + ["photos", "image"]:
            self.assertNotIn(key, h.user_data())
        self.assertEqual({c["chat_id"] for c in h.calls if "chat_id" in c}, {777})

    @scenario_test
    async def test_text_only_post_preview_equals_final(self, h):
        await h.start_dialog()
        await h.text("Только заголовок")
        for _ in range(len(POST_STEPS) - 1):
            await h.text("Пропустить")

        # Превью — текстовое сообщение с кнопками действий
        preview = h.calls[-1]
        self.assertEqual(preview["type"], "message")
        self.assertEqual(preview["parse_mode"], "HTML")
        self.assertEqual(
            [data for _, data in inline_buttons(preview["reply_markup"])],
            ["edit_post", "save_draft", "finish"],
        )
        # Пропущенные поля не видны, заглушек «Не указано» нет
        self.assertIn("• <b>Заголовок</b>: Только заголовок", preview["text"])
        self.assertNotIn("• <b>Дата</b>:", preview["text"])
        self.assertNotIn("Не указано", preview["text"])

        await h.callback("finish")
        posts = h.posts()
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1], "превью == готовый пост")
        self.assertIsNone(h.dialog_state())
        self.assertEqual(len(h.answers()), 1)

    @scenario_test
    async def test_all_nine_prompts_and_skip_keyboard(self, h):
        """Каждый шаг просит поле и несёт кнопку «Пропустить» (callback skip)."""
        await h.start_dialog()
        for index, step in enumerate(POST_STEPS):
            record = h.replies()[-1]
            self.assertEqual(
                record["text"], expected_prompts()[index], f"шаг {step['key']}"
            )
            self.assertEqual(
                inline_buttons(record["reply_markup"]),
                [("Пропустить", "skip")],
                f"на шаге {step['key']} должна быть кнопка пропуска",
            )
            if index == step_index("image"):
                await h.photo("photo-prompt")
            else:
                await h.text(WIZARD_VALUES.get(step["key"], "x"))
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(len([c for c in h.calls if c["type"] == "photo"]), 1)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class SkipAndEditTests(unittest.TestCase):
    """Пропуски, минимальный/пустой пост, редактирование полей."""

    @scenario_test
    async def test_skip_each_of_nine_fields(self, h):
        for skip_index, step in enumerate(POST_STEPS):
            with self.subTest(field=step["key"]):
                await h.start_dialog()
                for index, candidate in enumerate(POST_STEPS):
                    if index == skip_index:
                        await h.text("Пропустить")
                    elif candidate["key"] == "image":
                        await h.photo("photo-all")
                    else:
                        await h.text(WIZARD_VALUES[candidate["key"]])

                # Превью достигнуто, пропущенное поле не показано и не хранится
                summary = h.posts()[-1]
                self.assertEqual(h.dialog_state(), POST_CREATION)
                if step["key"] == "image":
                    self.assertEqual(
                        h.calls[-1]["type"], "message", "фото не должно отправляться"
                    )
                    self.assertNotIn("• <b>Изображение</b>:", summary)
                    self.assertIsNone(h.user_data().get("image"))
                else:
                    self.assertNotIn(f"• <b>{step['label']}</b>:", summary)
                    self.assertIsNone(h.user_data().get(step["key"]))
                self.assertNotIn("Не указано", summary)

                # Завершение частичного поста работает, состояние чистое
                await h.callback("finish")
                self.assertIsNone(h.dialog_state())
                for key in WIZARD_VALUES:
                    self.assertNotIn(key, h.user_data())

    @scenario_test
    async def test_minimal_post_title_only(self, h):
        await h.start_dialog()
        await h.text("Только заголовок")
        for _ in range(len(POST_STEPS) - 1):
            await h.text("Пропустить")

        await h.callback("finish")
        posts = h.posts()
        self.assertEqual(len(posts), 2)
        self.assertIn("Только заголовок", posts[1])
        self.assertNotIn("Не указано", posts[1])
        self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_empty_post_save_draft_creates_nothing_and_explains(self, h):
        """Пустой пост не создаёт черновик: понятный отказ, диалог живёт.

        Ни записи в БД, ни очистки user_data, ни завершения диалога —
        пользователь видит объяснение, заполняет поле и сохраняет снова.
        """
        await h.start_dialog()
        for _ in range(len(POST_STEPS)):
            await h.text("Пропустить")

        await h.callback("save_draft")

        self.assertEqual(len(h.drafts()), 0, "пустой черновик не создаётся")
        self.assertIn("Пост пуст", h.reply_texts()[-1])
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог остаётся")
        self.assertEqual(
            h.user_data().get("current_step"),
            len(POST_STEPS),
            "отказ не чистит данные диалога",
        )
        self.assertEqual(len(h.answers()), 1, "кнопка отвечена")

        # Заполнение снимает защиту: обычное сохранение работает как раньше
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Спасение пустого поста")
        await h.callback("save_draft")

        self.assertEqual(len(h.drafts()), 1, "заполненный пост сохраняется")
        self.assertIsNone(h.dialog_state())
        self.assertNotIn("title", h.user_data(), "после сохранения данные чистятся")

    @scenario_test
    async def test_emptying_opened_draft_refuses_save_and_keeps_record(self, h):
        """Очистка всех полей открытого черновика не портит и не дублирует запись."""
        await goto_preview(h, title="Исходный контент")
        await h.callback("save_draft")
        await h.callback("editdraft_1")

        # Очищаем единственное заполненное поле и пробуем сохранить
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.callback("skip")
        await h.callback("save_draft")

        self.assertEqual(len(h.drafts()), 1, "вторая запись не появилась")
        self.assertEqual(h.drafts()[0].title, "Исходный контент", "запись не порчена")
        self.assertIn("Пост пуст", h.reply_texts()[-1])
        self.assertEqual(h.dialog_state(), POST_CREATION, "диалог остаётся")

    @scenario_test
    async def test_empty_post_finish_is_rejected_and_dialog_survives(self, h):
        await h.start_dialog()
        for _ in range(len(POST_STEPS)):
            await h.text("Пропустить")

        preview = h.posts()[-1]
        self.assertIn("Пока ни одно поле не заполнено", preview)
        posts_before = len(h.posts())
        answers_before = len(h.answers())

        await h.callback("finish")

        self.assertEqual(len(h.posts()), posts_before, "пустой пост не отправляется")
        self.assertEqual(len(h.answers()), answers_before + 1, "кнопка отвечена")
        self.assertIn("Пост пуст", h.reply_texts()[-1])
        self.assertEqual(
            h.dialog_state(), POST_CREATION, "диалог должен остаться на превью"
        )

        # Защита не ломает заполнение: заголовок принят, отправка проходит
        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.text("Спасение пустого поста")
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        self.assertEqual(len(h.posts()), posts_before + 2)
        self.assertIn("Спасение пустого поста", h.posts()[-1])

    @scenario_test
    async def test_edit_each_field_updates_preview(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("photo-original")
        old_photo = h.calls[-1]["photo"]

        for key, new_value in NEW_VALUES.items():
            with self.subTest(field=key):
                await h.callback("edit_post")
                self.assertEqual(h.dialog_state(), EDIT_FIELD)

                await h.callback(f"edit_{key}")
                # Промпт правки редактирует INLINE-сообщение меню
                record = h.calls[-1]
                self.assertEqual(record["type"], "edit_message")
                label = POST_STEPS[step_index(key)]["label"]
                prompt_text = call_text(record) or ""
                if key == "image":
                    self.assertIn("Отправьте новое изображение", prompt_text)
                else:
                    self.assertIn(f"«{label}»", prompt_text)
                self.assertEqual(h.dialog_state(), EDIT_FIELD)

                if key == "image":
                    await h.photo(new_value)
                else:
                    await h.text(new_value)
                self.assertEqual(h.dialog_state(), POST_CREATION)

                preview = h.calls[-1]
                if key == "image":
                    self.assertEqual(preview["type"], "photo")
                    self.assertNotEqual(preview["photo"], old_photo)
                    old_photo = preview["photo"]
                else:
                    preview_text = h.posts()[-1]
                    self.assertIn(escape_html(new_value), preview_text)
                    if key in WIZARD_VALUES:
                        self.assertNotIn(escape_html(WIZARD_VALUES[key]), preview_text)

        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        final = h.posts()[-1]
        self.assertIn(escape_html(NEW_VALUES["title"]), final)

    @scenario_test
    async def test_repeat_edit_same_field_keeps_last_value(self, h):
        await h.goto_step(step_index("title"))
        await h.text("Первый вариант")
        for _ in range(len(POST_STEPS) - 2):
            await h.text("Пропустить")
        await h.photo("photo-rep")

        for value in ("Второй вариант", "Третий вариант"):
            await h.callback("edit_post")
            await h.callback("edit_title")
            await h.text(value)
            self.assertEqual(h.dialog_state(), POST_CREATION)
            self.assertIn(escape_html(value), h.posts()[-1])

        self.assertNotIn("Первый вариант", h.posts()[-1])
        await h.callback("finish")
        self.assertIn(escape_html("Третий вариант"), h.posts()[-1])

    @scenario_test
    async def test_cancel_edit_returns_to_preview_without_changes(self, h):
        await goto_preview(h)
        preview_text = h.posts()[-1]

        await h.callback("edit_post")
        self.assertEqual(h.dialog_state(), EDIT_FIELD)
        await h.callback("cancel_edit")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        # Отмена редактирует сообщение меню правки (edit_message_text)
        last = h.calls[-1]
        self.assertEqual(last["type"], "edit_message")
        self.assertIn("Редактирование отменено", call_text(last) or "")
        self.assertEqual(
            [data for _, data in inline_buttons(last["reply_markup"])],
            ["edit_post", "save_draft", "finish"],
        )
        self.assertEqual(h.posts()[-1], preview_text, "пост не изменился")

        await h.callback("finish")
        self.assertEqual(h.posts()[-1], preview_text)

    @scenario_test
    async def test_stale_preview_buttons_during_edit_hint_and_keep_state(self, h):
        for data in ("finish", "save_draft", "edit_post"):
            with self.subTest(data=data):
                await goto_preview(h)
                await h.callback("edit_post")
                await h.callback("edit_title")

                posts_before = len(h.posts())
                drafts_before = len(h.drafts())
                answers_before = len(h.answers())

                await h.callback(data)

                self.assertEqual(len(h.answers()), answers_before + 1)
                self.assertIn(
                    "Сначала завершите редактирование поля",
                    h.answers()[-1].get("text"),
                )
                self.assertEqual(len(h.posts()), posts_before, "пост не отправлен")
                self.assertEqual(len(h.drafts()), drafts_before, "черновик не создан")
                self.assertEqual(h.dialog_state(), EDIT_FIELD)

                # Выход из правки (новое значение) и чистое завершение диалога
                await h.text("Правка после подсказки")
                await h.callback("finish")
                self.assertIsNone(h.dialog_state())

    @scenario_test
    async def test_text_at_preview_gives_hint_and_keeps_dialog(self, h):
        await goto_preview(h)
        posts_before = len(h.posts())

        await h.text("просто сообщение")

        self.assertIn("Пост готов к действию", h.reply_texts()[-1])
        self.assertEqual(len(h.posts()), posts_before)
        self.assertEqual(h.dialog_state(), POST_CREATION)

    @scenario_test
    async def test_skip_button_during_edit_clears_field(self, h):
        await goto_preview(h)
        self.assertIn("• <b>Заголовок</b>:", h.posts()[-1])

        await h.callback("edit_post")
        await h.callback("edit_title")
        await h.callback("skip")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertIsNone(h.user_data().get("title"))
        self.assertNotIn("• <b>Заголовок</b>:", h.posts()[-1])

    @scenario_test
    async def test_main_menu_button_ends_dialog_and_saves_wip(self, h):
        await h.start_dialog()
        await h.text("Недописанный пост")

        await h.callback("main_menu")

        self.assertIsNone(h.dialog_state())
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].title, "Недописанный пост")
        self.assertNotIn("title", h.user_data())
        # уведомление о сохранении ВИП — reply; меню — на экране
        self.assertIn("сохранён в черновики", h.reply_texts()[-1])
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertIn("Выберите действие", h.screen_text())
        self.assertGreaterEqual(len(h.answers()), 1)

    @scenario_test
    async def test_create_post_button_mid_dialog_saves_wip_and_restarts(self, h):
        await h.start_dialog()
        await h.text("Прежний ВИП")

        await h.text("✏️ Создать пост")

        self.assertEqual(h.dialog_state(), POST_CREATION)
        drafts = h.drafts()
        self.assertEqual(len(drafts), 1, "прежний пост должен сохраниться")
        self.assertEqual(drafts[0].title, "Прежний ВИП")
        self.assertNotIn("title", h.user_data())
        self.assertEqual(h.reply_texts()[-1], expected_prompts()[0])

        # Повторный /create_post — та же точка входа (entry-точка диалога)
        await h.text("/create_post")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.reply_texts()[-1], expected_prompts()[0])
        self.assertEqual(len(h.drafts()), 1, "пустой ВИП не создаёт второй черновик")
