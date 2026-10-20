# tests/test_stage9_routing.py
"""
Stage 9: матрица маршрутизации и устойчивость бота как единой системы.

- CallbackMatrixTests: каждый callback в каждом состоянии диалога
  (нет диалога / POST_CREATION / EDIT_FIELD) получает answer_callback_query —
  спиннер Telegram не зависает ни в одном состоянии;
- CommandRoutingTests: /start, /help, /drafts, /cancel, /create_post
  внутри и вне диалога — ровно один ответ, ВИП сохраняется, состояние
  не «висит» (следующее сообщение не пишется в поле поста);
- RegistrationOrderTests: порядок регистрации обработчиков и контракт
  callback_data реальных клавиатур;
- ResilienceTests: сбои отправки не оставляют диалог в неожиданном
  состоянии, глобальный error_handler уведомляет пользователя;
- OutsideDialogTests: текст/фото/неизвестная команда вне диалога.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v
"""

import unittest

try:
    from telegram.ext import ConversationHandler

    import bot as bot_module
    from handlers.post_creation import (
        EDIT_FIELD,
        POST_CREATION,
        POST_STEPS,
        get_media_done_keyboard,
        get_post_actions_keyboard,
        get_skip_keyboard,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        goto_preview,
        inline_buttons,
        scenario_test,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_IMPORT_ERROR = str(e)


def step_index(key: str) -> int:
    return next(i for i, s in enumerate(POST_STEPS) if s["key"] == key)


# Все inline-кнопки диалога и глобальных экранов
CALLBACKS = [
    "edit_post",
    "edit_title",
    "save_draft",
    "finish",
    "skip",
    "media_done",
    "cancel_edit",
    "main_menu",
    "delete_1",
    "draftpage_0",
    "editdraft_1",
]

STATES = (None, POST_CREATION, EDIT_FIELD)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CallbackMatrixTests(unittest.TestCase):
    """Каждый callback × каждое состояние → корректное завершение."""

    async def _enter_state(self, h, state):
        if state == POST_CREATION:
            await h.start_dialog()
            await h.text("Заголовок в диалоге")
        elif state == EDIT_FIELD:
            await goto_preview(h)
            await h.callback("edit_post")
            await h.callback("edit_title")
            self.assertEqual(h.dialog_state(), EDIT_FIELD)

    @scenario_test
    async def test_every_callback_answered_in_every_state(self, h):
        for state in STATES:
            for data in CALLBACKS:
                # Выход из возможного предыдущего диалога — чистая точка входа
                if h.dialog_state() is not None:
                    await h.text("/cancel")
                with self.subTest(state=state, data=data):
                    await self._enter_state(h, state)
                    answers_before = len(h.answers())

                    await h.callback(data)

                    self.assertGreater(
                        len(h.answers()),
                        answers_before,
                        f"callback {data!r} в состоянии {state!r} должен получить "
                        "answer_callback_query (иначе спиннер зависает)",
                    )
                    self.assertIn(
                        h.dialog_state(),
                        (None, POST_CREATION, EDIT_FIELD),
                        f"состояние после {data!r} должно быть определённым",
                    )

    @scenario_test
    async def test_edit_menu_lists_all_nine_fields_and_cancel(self, h):
        await goto_preview(h)
        await h.callback("edit_post")

        menu = h.replies()[-1]
        self.assertIn("Выберите поле", menu["text"])
        self.assertEqual(
            [data for _, data in inline_buttons(menu["reply_markup"])],
            [f"edit_{step['key']}" for step in POST_STEPS] + ["cancel_edit"],
        )

    @scenario_test
    async def test_dialog_buttons_carry_expected_callback_data(self, h):
        # Кнопка пропуска на каждом шаге
        await h.start_dialog()
        self.assertEqual(
            inline_buttons(h.replies()[-1]["reply_markup"]),
            [("Пропустить", "skip")],
        )
        # Кнопки завершения сбора альбома
        self.assertEqual(
            [
                (b.text, b.callback_data)
                for row in get_media_done_keyboard().inline_keyboard
                for b in row
            ],
            [("✅ Фото готово", "media_done")],
        )
        # Кнопки превью и кнопка пропуска как функции-конструкторы
        self.assertEqual(
            [data for _, data in inline_buttons(get_post_actions_keyboard())],
            ["edit_post", "save_draft", "finish"],
        )
        self.assertEqual(
            [data for _, data in inline_buttons(get_skip_keyboard())],
            ["skip"],
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CommandRoutingTests(unittest.TestCase):
    """Команды и reply-кнопки внутри/вне диалога: один ответ, без зависаний."""

    OUTSIDE_EXPECTED = {
        "/start": "Здравствуйте",
        "/help": "Команды:",
        "/drafts": "черновик",
        "/cancel": "нет активного создания поста",
    }

    @scenario_test
    async def test_commands_outside_dialog_single_reply(self, h):
        for cmd, fragment in self.OUTSIDE_EXPECTED.items():
            with self.subTest(cmd=cmd):
                self.assertIsNone(h.dialog_state())
                before = len(h.reply_texts())
                await h.text(cmd)
                self.assertEqual(
                    len(h.reply_texts()) - before, 1, f"{cmd}: ровно один ответ"
                )
                self.assertIn(fragment, h.reply_texts()[-1])
                self.assertIsNone(h.dialog_state())

        # /create_post вне диалога — обычная точка входа
        await h.text("/create_post")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        await h.text("/cancel")

    @scenario_test
    async def test_commands_inside_dialog_end_it_and_save_wip_once(self, h):
        for cmd in self.OUTSIDE_EXPECTED:
            with self.subTest(cmd=cmd):
                await h.start_dialog()
                await h.text(f"ВИП перед {cmd}")
                drafts_before = len(h.drafts())
                replies_before = len(h.reply_texts())

                await h.text(cmd)

                self.assertIsNone(h.dialog_state(), "диалог должен завершиться")
                self.assertEqual(
                    len(h.drafts()),
                    drafts_before + 1,
                    "ВИП должен сохраниться ровно один раз",
                )
                new_replies = h.reply_texts()[replies_before:]
                self.assertEqual(
                    len(new_replies),
                    2,
                    "уведомление о сохранении ВИП + ровно один ответ команды",
                )
                self.assertIn("сохранён в черновики", new_replies[0])
                self.assertNotIn("title", h.user_data())

                # Состояние не «висит»: следующее сообщение уходит в подсказку,
                # а не в поле поста
                await h.text("потом обычный текст")
                self.assertIn("Воспользуйтесь кнопками меню", h.reply_texts()[-1])

    @scenario_test
    async def test_commands_during_edit_field_end_dialog(self, h):
        for cmd in self.OUTSIDE_EXPECTED:
            with self.subTest(cmd=cmd):
                await goto_preview(h)
                await h.callback("edit_post")
                await h.callback("edit_title")
                self.assertEqual(h.dialog_state(), EDIT_FIELD)
                drafts_before = len(h.drafts())

                await h.text(cmd)

                self.assertIsNone(h.dialog_state())
                self.assertEqual(len(h.drafts()), drafts_before + 1)
                self.assertNotIn("edit_field", h.user_data(), "состояние не висит")
                self.assertNotIn("title", h.user_data())

    @scenario_test
    async def test_create_post_inside_edit_field_restarts_dialog(self, h):
        # Команда и reply-кнопка — обе entry-точки: перезапуск с сохранением ВИП
        for trigger in ("/create_post", "✏️ Создать пост"):
            with self.subTest(trigger=trigger):
                # Чистая настройка каждого кейса: превью с ВИПом → правка поля
                if h.dialog_state() is not None:
                    await h.text("/cancel")
                await goto_preview(h, title="Прежний ВИП")
                await h.callback("edit_post")
                await h.callback("edit_title")
                self.assertEqual(h.dialog_state(), EDIT_FIELD)
                drafts_before = len(h.drafts())

                await h.text(trigger)

                self.assertEqual(h.dialog_state(), POST_CREATION)
                self.assertEqual(
                    h.reply_texts()[-1],
                    f"Шаг 1 из {len(POST_STEPS)} — {POST_STEPS[0]['label']}.\n"
                    f"{POST_STEPS[0]['prompt']}",
                )
                self.assertEqual(
                    len(h.drafts()),
                    drafts_before + 1,
                    "прежний ВИП сохранён один раз",
                )
                self.assertNotIn("title", h.user_data())

                # Пустой ВИП — повторный перезапуск второй черновик не создаёт
                drafts_before = len(h.drafts())
                await h.text(trigger)
                self.assertEqual(h.dialog_state(), POST_CREATION)
                self.assertEqual(len(h.drafts()), drafts_before)

    @scenario_test
    async def test_outside_dialog_text_photo_and_unknown_command_answered(self, h):
        cases = [
            ("привет", "Воспользуйтесь кнопками меню"),
            ("/bogus", "Неизвестная команда"),
        ]
        for raw, fragment in cases:
            with self.subTest(raw=raw):
                before = len(h.reply_texts())
                await h.text(raw)
                self.assertEqual(len(h.reply_texts()) - before, 1)
                self.assertIn(fragment, h.reply_texts()[-1])

        before = len(h.reply_texts())
        await h.photo("stray-photo")
        self.assertEqual(len(h.reply_texts()) - before, 1)
        self.assertIn("Фото понадобится на шаге", h.reply_texts()[-1])
        self.assertIsNone(h.dialog_state())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class RegistrationOrderTests(unittest.TestCase):
    """Порядок регистрации: диалог первым, устаревшие кнопки следом."""

    @staticmethod
    def _application():
        return bot_module.build_application()

    def test_conversation_first_stale_second_unknown_last(self):
        group = self._application().handlers[0]
        self.assertIsInstance(group[0], ConversationHandler)
        self.assertEqual(
            getattr(group[1].callback, "__name__", ""),
            "stale_dialog_action",
            "устаревшие кнопки обязаны идти сразу после диалога",
        )
        self.assertEqual(
            getattr(group[-1].callback, "__name__", ""),
            "unknown_command",
            "неизвестные команды — последними",
        )

    def test_conversation_configuration(self):
        application = self._application()
        conv = application.handlers[0][0]
        self.assertTrue(conv.allow_reentry, "перезапуск диалога должен работать")
        self.assertFalse(conv.persistent, "persistence в Stage 9 не внедряется")
        # Три entry-точки: команда, reply-кнопка, открытие черновика
        self.assertEqual(len(conv.entry_points), 3)
        entry_names = [getattr(h.callback, "__name__", "") for h in conv.entry_points]
        self.assertIn("start_post_creation", entry_names)
        self.assertIn("start_edit_draft", entry_names)
        # Обработчик ошибок зарегистрирован
        self.assertTrue(application.error_handlers, "error_handler обязателен")

    def test_states_hold_text_and_callbacks_in_both_states(self):
        conv = self._application().handlers[0][0]
        for state in (POST_CREATION, EDIT_FIELD):
            names = [getattr(h.callback, "__name__", "") for h in conv.states[state]]
            with self.subTest(state=state):
                self.assertIn("view_drafts_and_exit", names, "reply-кнопка «Черновики»")
        # В POST_CREATION текст обрабатывает handle_message
        names_creation = [
            getattr(h.callback, "__name__", "") for h in conv.states[POST_CREATION]
        ]
        self.assertIn("handle_message", names_creation)
        # В EDIT_FIELD текст обрабатывает process_edit
        names_edit = [
            getattr(h.callback, "__name__", "") for h in conv.states[EDIT_FIELD]
        ]
        self.assertIn("process_edit", names_edit)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ResilienceTests(unittest.TestCase):
    """Сбои отправки: диалог не ломается, ошибки не теряют данные."""

    @scenario_test
    async def test_send_failure_at_entry_allows_clean_retry(self, h):
        h.bot.fail_types.add("message")
        await h.text("✏️ Создать пост")  # падение не должно выйти наружу

        self.assertIsNone(h.dialog_state(), "диалог не начался — состояние чистое")
        h.bot.fail_types.clear()

        await h.text("✏️ Создать пост")
        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(
            h.reply_texts()[-1],
            f"Шаг 1 из {len(POST_STEPS)} — {POST_STEPS[0]['label']}.\n"
            f"{POST_STEPS[0]['prompt']}",
        )

    @scenario_test
    async def test_send_failure_mid_dialog_keeps_step_and_data(self, h):
        await h.start_dialog()
        h.bot.fail_types.add("message")

        await h.text("Концерт")  # поле сохраняется, промпт не уходит
        h.bot.fail_types.clear()

        self.assertEqual(h.dialog_state(), POST_CREATION)
        self.assertEqual(h.user_data().get("title"), "Концерт")
        self.assertEqual(h.user_data().get("current_step"), 1)

        # Диалог продолжается ровно с того же места
        await h.text("17.11.2026")
        self.assertEqual(h.user_data().get("date"), "17.11.2026")
        self.assertEqual(h.user_data().get("current_step"), 2)

    @scenario_test
    async def test_finish_send_failure_keeps_post_and_reports(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("photo-r1")
        preview = h.calls[-1]

        h.bot.fail_types.add("photo")
        await h.callback("finish")
        h.bot.fail_types.clear()

        self.assertEqual(len(h.answers()), 1, "кнопка отвечена даже при сбое")
        self.assertEqual(h.dialog_state(), POST_CREATION, "остаёмся на превью")
        self.assertIn("Не удалось отправить", h.reply_texts()[-1])
        self.assertEqual(
            h.user_data().get("photos"), ["photo-r1"], "данные не потеряны"
        )

        # Повторная попытка проходит, превью == готовый пост
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        finals = [c for c in h.calls if c["type"] == "photo"]
        self.assertEqual(finals[-1]["caption"], preview["caption"])
        self.assertEqual(finals[-1]["photo"], "photo-r1")

    @scenario_test
    async def test_unexpected_failure_goes_to_error_handler(self, h):
        await h.goto_step(step_index("image"))
        await h.photo("alb-1", media_group_id="mg-e")

        # Одиночное фото уходит через send_photo — ломаем именно его,
        # ошибка выходит из обработчика в error_handler
        h.bot.fail_types.add("photo")
        await h.callback("media_done")  # превью не отправилось → error_handler
        h.bot.fail_types.clear()

        # Пользователь получил внятное уведомление (не тишину),
        # а кнопка уже отвечена — спиннер не висит
        self.assertTrue(
            any("внутренняя ошибка" in t for t in h.reply_texts()),
            "error_handler должен уведомить пользователя",
        )
        self.assertEqual(h.dialog_state(), POST_CREATION, "состояние не потеряно")
        self.assertEqual(h.user_data().get("photos"), ["alb-1"], "данные целы")

        # Повтор: пост уходит с теми же данными
        await h.callback("finish")
        self.assertIsNone(h.dialog_state())
        finals = [c for c in h.calls if c["type"] == "photo"]
        self.assertEqual(finals[-1]["photo"], "alb-1")
