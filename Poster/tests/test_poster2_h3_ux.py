# tests/test_poster2_h3_ux.py
"""
Poster 2.3 H3, редактор и UX (требования A, B, C).

Проверяется снаружи, как у владельца: снапшоты текста экрана и структуры
клавиатуры через FakeBot, а не поиск отдельных подстрок.

- **B.** Главный экран правки готового анонса: строка
  «📝 Данные | 🖼 Фото», «🧱 Дополнительные поля» только когда добавлять есть
  что, «🏠 В главное меню». Кнопок «Применить изменения», «Обзор»,
  «Показать вложения» и ложного «Назад» нет. Сохранённое значение сразу
  заменяет доставленную версию: отдельного подтверждения не требуется.
- **C.** Управление площадкой: пары «✏️ Адрес | 🗑» и «✏️ Ссылка | 🗑» с
  подтверждением и отменой; у названия и города корзины нет; удаление
  города через UI недоступно; альтернативные названия — один вход.
- **A (часть).** Список дополнительных полей не предлагает уже выбранные
  кирпичики и не показывает кнопку «➕ Добавить поле», когда добавлять
  нечего.
- **D.** Общий компонент «Текущее значение» на экранах шаблонов и места:
  подпись, разделители, сохранённая разметка, без `<code>`, без
  эстетической обрезки. Предел только физический (лимит Telegram), и о нём
  сказано явно: экран остаётся в 4096 символов даже с двумя огромными
  значениями (адрес + ссылка).
- **E.** Раскладки шаблонного редактора: связанные действия стоят рядом,
  необратимые — отдельной строкой.

Негативные сценарии: старый callback удаления города не подтверждает
удаление, отмена подтверждения сохраняет значение, устаревшая кнопка не
меняет данные, огромная ссылка не обрезается молча и не ломает кликабельность
реальных ссылок.
"""

from __future__ import annotations

import unittest

try:
    from announcement_store import get_announcement_draft, save_announcement_value
    from brick_store import create_personal_brick, get_personal_brick
    from handlers.announcement_wizard import DELIVERED_PENDING_TEXT
    from template_store import (
        create_personal_template,
        get_template_version,
    )
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
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


def _details(h, draft_id: int):
    """Детали черновика, включая адреса доставленных сообщений."""
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


def _rows(h) -> list[list[tuple[str, str | None]]]:
    """Структура клавиатуры экрана: строки и кнопки, как их видит автор."""
    record = h.screen()
    assert record is not None, "экрана ещё не было"
    markup = record.get("reply_markup")
    assert markup is not None, "у экрана нет клавиатуры"
    return [
        [(button.text, button.callback_data) for button in row]
        for row in markup.inline_keyboard
    ]


async def _delivered(
    h,
    *,
    name: str = "Личный шаблон",
    specs: list[tuple[str, str, str, dict[str, object]]] | None = None,
    content: str | None = None,
    extended_content: str | None = None,
):
    """Личный анонс, доставленный «Без фотографий»."""
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name=name,
        specs=specs,
        content=content,
        extended_content=extended_content,
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Значение")
    await _no_photos(h)
    return _latest_draft(h), bricks


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredEditLayoutTests(unittest.TestCase):
    """Требование B: компактный экран правки и немедленное применение."""

    @scenario_test
    async def test_layout_is_two_columns_and_main_menu(self, h) -> None:
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        await _open_edit(h)

        rows = _rows(h)
        # Первая строка: данные и фото рядом — два связанных действия.
        self.assertEqual(
            [label for label, _data in rows[0]],
            ["📝 Данные", "🖼 Фото (0)"],
            f"первая строка: {rows[0]}",
        )
        self.assertEqual(
            [data for _label, data in rows[0]],
            [
                f"aw_qmanage:{draft.id}:{draft.revision}",
                f"aw_photos:{draft.id}:{draft.revision}:0",
            ],
        )
        # Последняя строка: выход в меню.
        self.assertEqual([label for label, _data in rows[-1]], ["🏠 В меню"], rows)
        # Экран правки ничего не отправляет и не меняет версию.
        self.assertEqual(len(h.posts()), 1)
        self.assertEqual(_details(h, draft.id).revision, draft.revision)

    @scenario_test
    async def test_no_apply_preview_or_fake_back_button(self, h) -> None:
        await h.text("/start")
        await _delivered(h)
        await _open_edit(h)

        labels = [label for label, _data in h.screen_buttons()]
        callbacks = [data for _label, data in h.screen_buttons()]
        for gone in ("Применить изменения", "Обзор", "Показать вложения", "Назад"):
            self.assertFalse(
                any(gone in label for label in labels),
                f"лишняя кнопка «{gone}»: {labels}",
            )
        for gone in ("aw_apply", "aw_preview", "aw_photo_show", "aw_panel"):
            self.assertFalse(
                any(data and data.startswith(gone) for data in callbacks),
                f"лишний callback {gone}: {callbacks}",
            )

    @scenario_test
    async def test_saved_value_replaces_version_without_extra_button(self, h) -> None:
        """Ввёл значение — пост в чате уже новый, кнопки «Применить» нет."""
        await h.text("/start")
        draft, _bricks = await _delivered(h)
        first_ids = tuple(_details(h, draft.id).delivered_message_ids)
        self.assertTrue(first_ids)

        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Новое значение")

        stored = _details(h, draft.id)
        self.assertEqual(stored.publication_status, "sent")
        self.assertEqual(len(h.posts()), 2, "новая версия не отправлена сразу")
        self.assertEqual(h.posts()[-1], "Новое значение")
        self.assertNotEqual(tuple(stored.delivered_message_ids), first_ids)
        # Прошлая версия заменена, а не оставлена второй копией.
        self.assertIn(first_ids[0], h.deleted_ids())
        self.assertNotIn(
            "Применить изменения",
            [label for label, _data in h.screen_buttons()],
        )

    @scenario_test
    async def test_extras_screen_has_no_apply_and_returns_to_list(self, h) -> None:
        await h.text("/start")
        # Poster 2.3.1 (E1): extras доступны только при расширенном тексте
        # шаблона, поэтому без него экран не открыть.
        await _delivered(
            h,
            specs=[
                ("main", "Основное", "short_text", {}),
                ("extra", "Дополнительное", "short_text", {}),
            ],
            content="[main]",
            extended_content="[main]\n[extra]",
        )
        await _open_edit(h)
        data = _callbacks(h, "aw_extras")
        if not data:
            self.skipTest("в шаблоне нет доступных дополнительных полей")
        await h.callback(data[0])
        labels = [label for label, _data in h.screen_buttons()]
        self.assertNotIn("🔄 Применить изменения", labels)
        self.assertIn("🏠 В меню", labels)

    @scenario_test
    async def test_save_of_sent_announcement_never_promises_removed_button(
        self, h
    ) -> None:
        """
        H3 (дефект ревизора): «сохранить» у доставленного анонса.

        Кнопки «Применить изменения» больше нет, поэтому сообщение о
        неприменённых изменениях не должно на неё ссылаться: статус уже
        `sent`, значит применение сорвалось, и честный текст отправляет
        автора к повторной правке или к экрану повтора отправки.
        """
        await h.text("/start")
        draft, _bricks = await _delivered(h)

        with h.session_factory() as session:
            current = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            # Значение меняется в обход мастера — так «неприменённое»
            # изменение реально остаётся: статус `sent`, а отпечаток другой.
            save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                brick_id=current.values[0].brick_id,
                value="Изменение в обход применения",
                expected_revision=current.revision,
            )
        posts_before = h.posts()

        # Переоткрытие черновика подтягивает новую ревизию: экран завершения
        # честно показывает строку о неотправленных изменениях.
        await h.text("/start")
        await h.callback("my_announcements")
        await h.callback(f"aw_open:{draft.id}")
        self.assertIn(DELIVERED_PENDING_TEXT, h.screen_text())

        ui = h.user_data()["announcement_ui"]
        await h.callback(f"aw_save:{ui['draft_id']}:{ui['revision']}")

        notice = h.answer_texts()[-1]
        self.assertNotIn("Применить изменения", notice, notice)
        self.assertIn("Повторить отправку", notice, notice)
        # «Сохранить» у доставленного анонса ничего не отправляет.
        self.assertEqual(h.posts(), posts_before)
        self.assertFalse(_callbacks(h, "aw_apply"))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceEditorLayoutTests(unittest.TestCase):
    """Требование C: пары «✏️ | 🗑», подтверждение и отмена."""

    async def _open_menu(self, h, place_id: int) -> None:
        """Кабинет → карточка места → меню редактирования (как у владельца)."""
        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        await h.callback(f"adm_edit_place:{place_id}")
        assert "✏️ Изменить место" in h.screen_text()

    @scenario_test
    async def test_address_and_url_are_paired_with_trash_only(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await self._open_menu(h, place_id)

        rows = _rows(h)
        pairs = {tuple(label for label, _data in row) for row in rows}
        self.assertIn(("✏️ Адрес", "🗑"), pairs, f"нет пары адреса: {rows}")
        self.assertIn(("✏️ Ссылка", "🗑"), pairs, f"нет пары ссылки: {rows}")
        labels = [label for label, _data in h.screen_buttons()]
        self.assertEqual(labels.count("🗑"), 2, labels)
        # У названия и города корзины нет: город только меняется.
        self.assertNotIn("🗑 Удалить город", labels)
        self.assertNotIn("Удалить название", " ".join(labels))
        self.assertIn("🏙 Изменить город", labels)

    @scenario_test
    async def test_address_delete_needs_confirmation_and_cancel_keeps_value(
        self, h
    ) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await self._open_menu(h, place_id)

        await h.callback(f"adm_edit_address_delete:{place_id}")
        self.assertIn("Удалить адрес?", h.screen_text())
        rows = _rows(h)
        self.assertEqual(
            [label for label, _data in rows[0]],
            ["✅ Подтвердить"],
            "подтверждение не должно сливаться с отменой",
        )
        self.assertEqual([label for label, _data in rows[1]], ["❌ Отмена"])

        # Отмена: адрес остаётся прежним, меню возвращается.
        await h.callback("adm_cancel")
        self.assertIn("ул. Тестовая, 1", h.screen_text())
        self.assertIn("✏️ Адрес", [label for label, _data in h.screen_buttons()])

        # Подтверждение: значение удалено.
        await h.callback(f"adm_edit_address_delete:{place_id}")
        await h.callback("adm_edit_confirm")
        self.assertIn("✅ Адрес места удалён.", h.screen_text())
        pairs = {tuple(label for label, _data in row) for row in _rows(h)}
        self.assertIn(("✏️ Адрес",), pairs, "у адреса корзина должна исчезнуть")
        self.assertIn(("✏️ Ссылка", "🗑"), pairs, "ссылка не должна пострадать")

    @scenario_test
    async def test_stale_city_delete_does_not_delete_city(self, h) -> None:
        """Негатив: старый callback удаления города больше не работает."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await self._open_menu(h, place_id)

        await h.callback(f"adm_edit_city_delete:{place_id}")
        self.assertNotIn("Подтвердите удаление города", h.screen_text())
        self.assertNotIn("✅ Город места удалён.", h.screen_text())

    @scenario_test
    async def test_aliases_have_single_entry_point(self, h) -> None:
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        await self._open_menu(h, place_id)

        alias_buttons = [
            (label, data)
            for label, data in h.screen_buttons()
            if data and data.startswith("adm_aliases:")
        ]
        self.assertEqual(
            [label for label, _data in alias_buttons],
            ["📝 Альтернативные названия"],
            f"альтернативные названия должны иметь один вход: {alias_buttons}",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class CurrentValueDisplayTests(unittest.TestCase):
    """Требование D: одно «Текущее значение» на шаблонах и в месте."""

    # Лимит сообщения Telegram; экран длиннее просто не отправится.
    TELEGRAM_LIMIT = 4096

    @scenario_test
    async def test_template_card_shows_value_as_block_without_code(self, h) -> None:
        from utils.current_value import SEPARATOR, TRUNCATED_NOTE

        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Короткий", content="Тело [main]"
        )
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        text = h.screen_text()
        # Подпись, рамка, значение, рамка — без <code> вокруг значения.
        self.assertIn(f"📝 Текст:\n{SEPARATOR}\nТело [main]\n{SEPARATOR}", text)
        self.assertNotIn("<code>", text)
        self.assertNotIn(TRUNCATED_NOTE, text, "короткое значение не помечается")

    @scenario_test
    async def test_long_template_value_is_clipped_only_by_telegram_limit(
        self, h
    ) -> None:
        from utils.current_value import SEPARATOR, TRUNCATED_NOTE

        body = "A" * 5000 + " [main]"
        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Длинный", content=body
        )
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        text = h.screen_text()
        self.assertLessEqual(
            len(text), self.TELEGRAM_LIMIT, "экран не влезет в Telegram"
        )
        # Старая эстетическая обрезка (1200/900) ушла: видно заметно больше.
        self.assertGreater(text.count("A"), 2000)

        # Явная пометка вместо молчаливого «…»: полное значение в базе.
        self.assertIn(TRUNCATED_NOTE, text)
        self.assertNotIn("на экране сокращено", text, "косметической обрезки нет")
        self.assertNotIn("<code>", text)

        # Обе половины рамки на месте: значение осталось блоком.
        self.assertEqual(text.count(SEPARATOR), 2)
        self.assertIn("📝 Текст:", text)

    @scenario_test
    async def test_two_huge_place_values_fit_one_screen(self, h) -> None:
        """Дефект H3: адрес и ссылка вместе выносили экран за 4096."""
        from utils.current_value import TRUNCATED_NOTE

        long_address = "ул. Очень длинная улица, " * 200
        # Такая ссылка могла попасть в базу до появления ограничения длины:
        # карточка обязана показать её без отказа Telegram.
        legacy_url = "https://example.com/" + "x" * 4000
        await h.text("/start")
        place_id, _alias = _seed_place(h, address=long_address, url=legacy_url)

        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        card = h.screen_text()
        self.assertLessEqual(
            len(card), self.TELEGRAM_LIMIT, "экран места не влезет в Telegram"
        )
        # Оба значения помечены как фрагмент: адрес и ссылка.
        self.assertEqual(card.count(TRUNCATED_NOTE), 2)
        self.assertIn("Адрес:", card)
        self.assertIn("Ссылка:", card)

        # Меню редактирования несёт те же два значения — тоже в пределах лимита.
        await h.callback(f"adm_edit_place:{place_id}")
        menu = h.screen_text()
        self.assertLessEqual(len(menu), self.TELEGRAM_LIMIT)
        self.assertEqual(menu.count(TRUNCATED_NOTE), 2)

    @scenario_test
    async def test_long_valid_url_keeps_screen_fit_and_single_note(self, h) -> None:
        """Максимальная допустимая ссылка (500) рядом с длинным адресом."""
        from utils.current_value import TRUNCATED_NOTE

        long_address = "ул. Очень длинная улица, " * 200
        url = "https://example.com/" + "x" * 470
        await h.text("/start")
        place_id, _alias = _seed_place(h, address=long_address, url=url)

        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        card = h.screen_text()
        self.assertLessEqual(len(card), self.TELEGRAM_LIMIT)
        # Короткая по меркам Telegram ссылка не помечается: помечен только адрес.
        self.assertEqual(card.count(TRUNCATED_NOTE), 1)
        self.assertIn(f'<a href="{url}">', card, "ссылка остаётся кликабельной")

        # Подтверждение нового адреса: «текущий» и «новый» делят бюджет экрана.
        await h.callback(f"adm_edit_place:{place_id}")
        await h.callback(f"adm_edit_address:{place_id}")
        await h.text("Новый адрес " * 400)
        confirm = h.screen_text()
        self.assertLessEqual(
            len(confirm), self.TELEGRAM_LIMIT, "подтверждение не влезет в Telegram"
        )
        self.assertIn("Текущий адрес:", confirm)
        self.assertIn("Новый адрес:", confirm)
        self.assertEqual(confirm.count(TRUNCATED_NOTE), 2)

    @scenario_test
    async def test_short_url_stays_clickable_without_note(self, h) -> None:
        from utils.current_value import TRUNCATED_NOTE

        await h.text("/start")
        place_id, _alias = _seed_place(h, url="https://example.com/place")

        await h.text("/admin")
        await h.callback(f"adm_place:{place_id}")
        card = h.screen_text()
        self.assertIn(
            '<a href="https://example.com/place">https://example.com/place</a>',
            card,
            "обычная ссылка должна остаться кликабельной",
        )
        self.assertNotIn(TRUNCATED_NOTE, card)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ScreenLimitRegressionTests(unittest.TestCase):
    """
    Дефекты H3: экран с несколькими длинными значениями длиннее 4096.

    Раньше каждое значение укладывалось в лимит по отдельности, а вместе они
    выносили экран за предел Telegram, и Telegram отклонял сообщение целиком
    (пользователь видел ошибку вместо экрана). Проверяется фактический текст
    экрана, который ушёл бы в API, и сохранность полного значения в БД.
    """

    TELEGRAM_LIMIT = 4096

    @scenario_test
    async def test_template_card_with_two_huge_values_fits_and_marked(self, h):
        """Дефект 1: карточка шаблона с каноническим и расширенным текстом."""
        from utils.current_value import TRUNCATED_NOTE

        body = "A" * 5000
        extended = "B" * 5000
        await h.text("/start")
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="h3-user")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="main",
                title="Основное",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Два огромных",
                content=body + " [main]",
                extended_content=extended,
            )
            session.commit()
            template_id = template.template_id

        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")

        card = h.screen_text()
        self.assertLessEqual(
            len(card), self.TELEGRAM_LIMIT, "экран не влезет в Telegram"
        )
        # Оба значения помечены: ни одно не потеряно молча.
        self.assertEqual(card.count(TRUNCATED_NOTE), 2, card[-200:])
        self.assertGreater(card.count("A"), 500)
        self.assertGreater(card.count("B"), 500)
        self.assertNotIn("<code>", card)

        # Полные значения остались в БД, показан только фрагмент.
        with h.session_factory() as session:
            version = get_template_version(session, DEFAULT_USER_ID, template_id, 1)
        assert version is not None
        self.assertEqual(version.content, body + " [main]")
        self.assertEqual(version.extended_content, extended)

    @scenario_test
    async def test_text_editor_with_huge_draft_fits_and_marked(self, h):
        """Дефект 2: экран правки текста с черновиком в 5000 символов."""
        from utils.current_value import TRUNCATED_NOTE

        body = "A" * 5000
        await h.text("/start")
        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="h3-user")
            create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="main",
                title="Основное",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Длинный черновик",
                content=body + " [main]",
            )
            session.commit()
            template_id = template.template_id

        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_edit:{template_id}")

        screen = h.screen_text()
        self.assertLessEqual(len(screen), self.TELEGRAM_LIMIT)
        self.assertIn(TRUNCATED_NOTE, screen)
        self.assertGreater(screen.count("A"), 1000)
        # Подсказка про формат осталась целиком: сокращено только значение.
        self.assertIn("Отправьте весь новый текст одним", screen)
        self.assertIn("📋 Кирпичики", [label for label, _ in h.screen_buttons()])

    @scenario_test
    async def test_brick_description_of_5000_fits_every_screen(self, h):
        """Дефект 3: описание кирпичика раздувало карточку, меню и превью."""
        from utils.current_value import TRUNCATED_NOTE

        await h.text("/start")
        with h.session_factory() as session:
            description = "О" * 5000
            brick = create_personal_brick(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                code="bigdesc",
                title="Кирпичик",
                value_type="short_text",
                rules={},
                description=description,
            )
            session.commit()
            brick_id = brick.id

        await h.text("/bricks")
        await h.callback(f"bk_open:{brick_id}")

        card = h.screen_text()
        self.assertLessEqual(len(card), self.TELEGRAM_LIMIT)
        self.assertIn(TRUNCATED_NOTE, card)
        self.assertIn("Описание: <code>", card, "описание остаётся моноширинным")

        await h.callback("bk_edit")
        menu = h.screen_text()
        self.assertLessEqual(len(menu), self.TELEGRAM_LIMIT)
        self.assertIn(TRUNCATED_NOTE, menu)

        await h.callback("bk_preview")
        preview = h.screen_text()
        self.assertLessEqual(len(preview), self.TELEGRAM_LIMIT)
        self.assertIn(TRUNCATED_NOTE, preview)
        await h.callback("bk_cancel")

        # Описание в БД не пострадало: сокращён только экран.
        with h.session_factory() as session:
            stored = get_personal_brick(session, DEFAULT_USER_ID, brick_id)
        assert stored is not None
        self.assertEqual(stored.description, description)

    @scenario_test
    async def test_extra_value_of_5600_marked_and_stored_fully(self, h):
        """Дефект 4: огромное сохранённое значение доп. кирпичика."""
        from utils.current_value import TRUNCATED_NOTE

        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(
            h,
            name="С доп. полем",
            specs=[
                ("main", "Основное", "short_text", {}),
                ("extra", "Дополнительное", "short_text", {}),
            ],
            content="[main]",
            # Poster 2.3.1 (E1): кандидат в доп. поля — кирпичик
            # расширенного текста шаблона.
            extended_content="[main]\n[extra]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Главное")
        await _no_photos(h)
        draft = _latest_draft(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_extras"))
        await h.callback(_callback(h, "aw_exadd"))
        extra_id = bricks["extra"]
        found = None
        for _attempt in range(6):
            for _label, data in h.screen_buttons():
                if (
                    data
                    and data.startswith("aw_exbrick")
                    and data.endswith(f":{extra_id}")
                ):
                    found = data
            if found:
                break
            pages = [
                data for label, data in h.screen_buttons() if label == "▶️" and data
            ]
            if not pages:
                break
            await h.callback(pages[0])
        self.assertIsNotNone(found, f"кирпичик не найден: {h.screen_buttons()}")
        await h.callback(found)

        # Значение попадает в базу не только через экран (API, старая запись,
        # повторная доставка): показ обязан выдержать любой сохранённый текст.
        long_value = "Очень длинное " * 400  # ≈5600 символов
        with h.session_factory() as session:
            current = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
            save_announcement_value(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
                brick_id=extra_id,
                value=long_value,
                expected_revision=current.revision,
            )

        # Полное значение сохранено в БД — сокращён только экран.
        with h.session_factory() as session:
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.id,
            )
        value = details.value_for(extra_id)
        assert value is not None and value.payload is not None
        self.assertEqual(value.payload["text"], long_value)

        # Список дополнительных полей укладывается в лимит и честно помечает
        # сокращение (раньше огромное значение обрывалось молчаливым «…»).
        # Возврат с экрана значения: ревизия в UI устарела, поэтому экран
        # сначала восстанавливается, а затем показывает актуальный список.
        await h.callback(_callback(h, "aw_exbackval"))
        if not _callbacks(h, "aw_exedit"):
            await h.callback(_callback(h, "aw_extras"))
        listing = h.screen_text()
        self.assertLessEqual(len(listing), self.TELEGRAM_LIMIT)
        self.assertIn(TRUNCATED_NOTE, listing)

        # Экран вопроса с сохранённым значением: в пределах лимита и с
        # честной пометкой, `<code>` вокруг значения нет.
        await h.callback(_callback(h, "aw_exedit"))
        prompt = h.screen_text()
        self.assertLessEqual(len(prompt), self.TELEGRAM_LIMIT, prompt[-200:])
        self.assertIn(TRUNCATED_NOTE, prompt)
        self.assertIn("Текущее значение:", prompt)
        self.assertNotIn("&lt;", prompt)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplateEditorLayoutTests(unittest.TestCase):
    """Требование E: раскладки шаблонного редактора."""

    async def _open_card(self, h) -> int:
        await h.text("/start")
        template_id, _revision, _bricks = _seed_personal_template(
            h, name="Раскладка", content="Тело [main]"
        )
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        return template_id

    @scenario_test
    async def test_card_puts_view_actions_in_one_row(self, h) -> None:
        await self._open_card(h)

        rows = _rows(h)
        pairs = {tuple(label for label, _data in row) for row in rows}
        self.assertIn(
            ("👁 Предпросмотр", "📋 Кирпичики"),
            pairs,
            f"действия «посмотреть» должны стоять рядом: {rows}",
        )
        # Необратимое восстановление — отдельной строкой, не рядом с просмотром.
        reset_rows = [
            row
            for row in rows
            if any("Восстановить стандарт" in label for label, _ in row)
        ]
        self.assertEqual(len(reset_rows), 1)
        self.assertEqual(
            len(reset_rows[0]), 1, f"опасная кнопка не одна в строке: {rows}"
        )

    @scenario_test
    async def test_text_editor_puts_help_actions_in_one_row(self, h) -> None:
        template_id = await self._open_card(h)
        await h.callback(f"tp_edit:{template_id}")

        rows = _rows(h)
        pairs = {tuple(label for label, _data in row) for row in rows}
        self.assertIn(
            ("📋 Кирпичики", "❓ Подсказка"),
            pairs,
            f"справочные действия должны стоять рядом: {rows}",
        )
        # «Очистить» необратима для черновика — отдельной строкой.
        clear_rows = [
            row for row in rows if any("Очистить" in label for label, _ in row)
        ]
        self.assertEqual(len(clear_rows), 1)
        self.assertEqual(len(clear_rows[0]), 1, f"очистка не одна в строке: {rows}")

    @scenario_test
    async def test_create_choice_puts_two_bases_in_one_row(self, h) -> None:
        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback("tp_create")
        await h.text("Новый шаблон")

        rows = _rows(h)
        pairs = {tuple(label for label, _data in row) for row in rows}
        self.assertIn(
            ("📄 Стандартный", "📄 Пустой"),
            pairs,
            f"два варианта основы — один выбор: {rows}",
        )
