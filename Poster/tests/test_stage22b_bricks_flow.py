# tests/test_stage22b_bricks_flow.py
"""
Stage 22B: Telegram-сценарий «Мои кирпичики» через Application.process_update.

Настоящее приложение, FakeBot и временная SQLite. Проверяются вход, все семь
типов, подсказка/конфликты кода, настройки, предпросмотр, редактирование,
архив/восстановление, пагинация, рестарт, изоляция сценариев и ошибки БД.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from brick_state import load_brick_session
from brick_store import (
    archive_personal_brick,
    create_personal_brick,
    update_personal_brick,
)
from brick_types import BRICK_TYPES
from models import Brick, User
from place_store import create_place
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    scenario_test,
)
from user_store import get_or_create_user


@unittest.skipIf(
    HARNESS_IMPORT_ERROR is not None,
    f"интеграционный каркас недоступен: {HARNESS_IMPORT_ERROR}",
)
class BricksUiTests(unittest.TestCase):
    def personal_bricks(self, h) -> list[Brick]:
        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            if user is None:
                return []
            return list(
                session.scalars(
                    select(Brick)
                    .where(Brick.owner_user_id == user.id)
                    .order_by(Brick.id)
                ).all()
            )

    def brick_rows(self, h) -> list[Brick]:
        # Системные кирпичики приходят миграцией 6 и не являются
        # пользовательскими строками этого сценария.
        with h.session_factory() as session:
            return list(
                session.scalars(
                    select(Brick).where(Brick.scope != "system").order_by(Brick.id)
                ).all()
            )

    def answer_text(self, h) -> str:
        parts: list[str] = []
        for call in h.answers():
            args = call.get("args") or ()
            kwargs = call.get("kwargs") or {}
            parts.extend(str(item) for item in args)
            parts.extend(f"{key}={value}" for key, value in kwargs.items())
            if call.get("text"):
                parts.append(str(call["text"]))
        return "\n".join(parts)

    async def start_create(
        self,
        h,
        title: str,
        *,
        code: str | None = None,
        description: str | None = None,
        **kwargs,
    ) -> None:
        await h.callback("bk_create", **kwargs)
        await h.text(title, **kwargs)
        if code is None:
            await h.callback("bk_accept_code", **kwargs)
        else:
            await h.text(code, **kwargs)
        # S5: между обозначением и типом спрашивается описание для автора.
        if description is None:
            await h.callback("bk_skip_description", **kwargs)
        else:
            await h.text(description, **kwargs)

    async def create_simple(
        self,
        h,
        title: str,
        code: str,
        *,
        value_type: str = BRICK_TYPES[0],
        **kwargs,
    ) -> None:
        await self.start_create(h, title, code=code, **kwargs)
        await h.callback(f"bk_type:{value_type}", **kwargs)
        if value_type == "number":
            await h.callback("bk_num:done", **kwargs)
        elif value_type == "choice":
            await h.callback("bk_choice_add", **kwargs)
            await h.text("Один", **kwargs)
            await h.callback("bk_choice_done", **kwargs)
        await h.callback("bk_save", **kwargs)

    async def seed_bricks(self, h, *, active: int, archived: int) -> None:
        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id)
            created = []
            for index in range(active + archived):
                created.append(
                    create_personal_brick(
                        session,
                        actor_telegram_user_id=user.telegram_user_id,
                        code=f"код_{index:02d}",
                        title=f"Кирпичик {index:02d}",
                        value_type="short_text",
                    )
                )
            for brick in created[active:]:
                archive_personal_brick(
                    session,
                    actor_telegram_user_id=user.telegram_user_id,
                    brick_id=brick.id,
                    expected_revision=brick.revision,
                )

    @scenario_test
    async def test_main_menu_entry_empty_state_and_back(self, h) -> None:
        await h.text("/start")
        # Poster 2.0 S2: кирпичики открываются из «⚙️ Управление».
        self.assertIn(("⚙️ Управление", "manage"), h.screen_buttons())
        await h.callback("manage")
        self.assertIn(("🧱 Кирпичики", "manage_bricks"), h.screen_buttons())
        await h.callback("manage_bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertIn("У вас пока нет личных кирпичиков", h.screen_text())
        self.assertIn(("➕ Создать кирпичик", "bk_create"), h.screen_buttons())
        await h.callback("main_menu")
        self.assertEqual(h.screen_text(), "Здравствуйте! 👋\n\nВыберите действие:")

        await h.text("/bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertEqual(self.personal_bricks(h), [])
        self.assertIsNotNone(load_brick_session(h.user_id))

    @scenario_test
    async def test_all_seven_types_require_explicit_save(self, h) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        title_update = await h.text("Ведущий & мероприятия")
        title_message_id = title_update.message.message_id
        self.assertIn("[ведущий_мероприятия]", h.screen_text())
        await h.callback("bk_accept_code")
        self.assertIn("Описание для автора анонса", h.screen_text())
        await h.callback("bk_skip_description")
        type_callbacks = [
            data
            for _, data in h.screen_buttons()
            if data and data.startswith("bk_type:")
        ]
        self.assertEqual(
            type_callbacks,
            [f"bk_type:{value_type}" for value_type in BRICK_TYPES],
        )
        self.assertNotIn("bk_type:photo", type_callbacks)
        self.assertNotIn("bk_type:any", type_callbacks)
        await h.callback("bk_type:short_text")
        self.assertIn("Проверьте кирпичик", h.screen_text())
        self.assertEqual(self.personal_bricks(h), [])
        await h.callback("bk_save")
        self.assertIn("создан", h.screen_text())
        self.assertIn("Ведущий &amp; мероприятия", h.screen_text())
        self.assertNotIn("Ведущий & мероприятия", h.screen_text())
        self.assertIn(title_message_id, h.deleted_ids())
        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].value_type, "short_text")

        values = [
            ("Большое описание", "большое_описание", "long_text"),
            ("Дата события", "дата_события", "date"),
            ("Время начала", "время_начала_личное", "time"),
            ("Ссылка на сайт", "ссылка_личная", "url"),
        ]
        for title, code, value_type in values:
            await self.start_create(h, title, code=code)
            await h.callback(f"bk_type:{value_type}")
            await h.callback("bk_save")
            self.assertIn("создан", h.screen_text())

        await self.start_create(h, "Цена билета", code="цена_личная")
        await h.callback("bk_type:number")
        await h.callback("bk_num:integer")
        await h.callback("bk_num:set_min")
        await h.text("0")
        await h.callback("bk_num:set_max")
        await h.text("10000")
        await h.callback("bk_num:set_suffix")
        await h.text("₽")
        await h.callback("bk_num:done")
        await h.callback("bk_save")

        await self.start_create(h, "Формат участия", code="формат_личный")
        await h.callback("bk_type:choice")
        await h.callback("bk_choice_add")
        await h.text("Очно")
        await h.callback("bk_choice_add")
        await h.text("Онлайн")
        await h.callback("bk_choice_multiple")
        await h.callback("bk_choice_done")
        await h.callback("bk_save")

        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 7)
        self.assertEqual({row.value_type for row in rows}, set(BRICK_TYPES))
        number = next(row for row in rows if row.value_type == "number")
        self.assertEqual(
            number.rules_json,
            '{"integer": true, "max": 10000, "min": 0, "suffix": "₽"}',
        )
        choice = next(row for row in rows if row.value_type == "choice")
        self.assertEqual(
            choice.rules_json,
            '{"choices": ["Очно", "Онлайн"], "multiple": true}',
        )

    @scenario_test
    async def test_suggestions_conflicts_and_duplicate_human_titles(self, h) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Цена", "цена")
        self.assertEqual(self.personal_bricks(h)[0].code, "цена")

        await h.callback("bk_create")
        await h.text("Цена")
        self.assertIn("[цена_2]", h.screen_text())
        self.assertTrue(
            any(
                data == "bk_accept_code" and "цена_2" in str(text)
                for text, data in h.screen_buttons()
            )
        )
        await h.text("цена")
        self.assertIn("уже используется", h.screen_text())
        self.assertEqual(len(self.personal_bricks(h)), 1)
        await h.text("дата")
        self.assertIn("зарезервирован системным", h.screen_text())
        await h.text("a")
        self.assertIn("от 2 до 64", h.screen_text())
        await h.text("новая_цена")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:short_text")
        await h.callback("bk_save")
        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 2)
        self.assertEqual([row.title for row in rows], ["Цена", "Цена"])
        self.assertEqual([row.code for row in rows], ["цена", "новая_цена"])

        await h.callback("bk_create")
        await h.text("Цена")
        self.assertIn("[цена_2]", h.screen_text())
        await h.callback("bk_accept_code")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:short_text")
        await h.callback("bk_save")
        self.assertEqual(len(self.personal_bricks(h)), 3)

    @scenario_test
    async def test_number_settings_validation_and_preview_change_walk(self, h) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Стоимость")
        await h.text("стоимость")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:number")
        await h.callback("bk_num:integer")
        await h.callback("bk_num:set_min")
        await h.text("не число")
        self.assertIn("Это не число", h.screen_text())
        self.assertEqual(self.personal_bricks(h), [])
        await h.text("10")
        await h.callback("bk_num:set_max")
        await h.text("5")
        self.assertIn("min не может быть больше max", h.screen_text())
        self.assertIn("Введите максимальное число", h.screen_text())
        await h.callback("bk_back")
        await h.callback("bk_num:set_suffix")
        await h.text("₽")
        await h.callback("bk_num:done")
        self.assertIn("Проверьте кирпичик", h.screen_text())
        await h.callback("bk_change")
        self.assertIn("Как назвать кирпичик", h.screen_text())
        await h.text("Стоимость билета")
        self.assertIn("Техническое обозначение", h.screen_text())
        await h.callback("bk_accept_code")
        await h.callback("bk_skip_description")
        self.assertIn("Выберите тип", h.screen_text())
        self.assertIn("Сейчас выбран: <b>Число</b>", h.screen_text())
        await h.callback("bk_type:number")
        self.assertIn("Настройки числа", h.screen_text())
        await h.callback("bk_num:done")
        await h.callback("bk_save")
        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].title, "Стоимость билета")
        self.assertEqual(
            rows[0].rules_json,
            '{"integer": true, "min": 10, "suffix": "₽"}',
        )

    @scenario_test
    async def test_choice_settings_add_remove_duplicate_and_empty_guard(
        self, h
    ) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Формат")
        await h.text("формат")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:choice")
        await h.callback("bk_choice_done")
        self.assertIn("Добавьте хотя бы один вариант", h.screen_text())
        await h.callback("bk_choice_add")
        await h.text("Очно")
        await h.callback("bk_choice_add")
        await h.text("Очно")
        self.assertIn("Такой вариант уже есть", h.screen_text())
        await h.text("Онлайн")
        await h.callback("bk_choice_remove:0")
        self.assertIn("Онлайн", h.screen_text())
        self.assertNotIn("Очно", h.screen_text())
        await h.callback("bk_choice_multiple")
        await h.callback("bk_choice_done")
        await h.callback("bk_save")
        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            rows[0].rules_json, '{"choices": ["Онлайн"], "multiple": true}'
        )

    @scenario_test
    async def test_edit_fields_keep_id_and_type_and_cancel_is_safe(self, h) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Цена", "цена", value_type="number")
        brick = self.personal_bricks(h)[0]
        brick_id = brick.id
        revision = brick.revision
        await h.callback(f"bk_open:{brick_id}")
        self.assertIn("Это только определение поля", h.screen_text())
        self.assertIn(("📦 В архив", "bk_archive"), h.screen_buttons())
        await h.callback("bk_edit")
        self.assertIn("Тип после создания не меняется", h.screen_text())
        self.assertNotIn("bk_type:short_text", [data for _, data in h.screen_buttons()])
        await h.callback("bk_edit_title")
        await h.text("Стоимость билета")
        self.assertIn("Изменить кирпичик", h.screen_text())
        await h.callback("bk_preview")
        await h.callback("bk_cancel")
        self.assertIn("Изменения отменены", h.screen_text())
        unchanged = self.personal_bricks(h)[0]
        self.assertEqual(unchanged.title, "Цена")
        self.assertEqual(unchanged.revision, revision)

        await h.callback("bk_edit")
        await h.callback("bk_edit_title")
        await h.text("Стоимость билета")
        await h.callback("bk_edit_settings")
        await h.callback("bk_num:set_min")
        await h.text("100")
        await h.callback("bk_num:set_suffix")
        await h.text("₽")
        await h.callback("bk_num:done")
        await h.callback("bk_preview")
        await h.callback("bk_save")
        self.assertIn("Изменения сохранены", h.screen_text())
        updated = self.personal_bricks(h)[0]
        self.assertEqual(updated.id, brick_id)
        self.assertEqual(updated.value_type, "number")
        self.assertEqual(updated.title, "Стоимость билета")
        self.assertEqual(updated.rules_json, '{"min": 100, "suffix": "₽"}')
        self.assertEqual(updated.revision, revision + 1)

    @scenario_test
    async def test_edit_code_conflict_and_stale_revision_are_not_overwritten(
        self, h
    ) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Первый", "первый")
        await self.create_simple(h, "Второй", "второй")
        first, second = self.personal_bricks(h)
        await h.callback(f"bk_open:{first.id}")
        await h.callback("bk_edit")
        await h.callback("bk_edit_code")
        await h.text("второй")
        self.assertIn("уже используется", h.screen_text())
        await h.text("третий")
        await h.callback("bk_preview")
        await h.callback("bk_save")
        updated = self.personal_bricks(h)[0]
        self.assertEqual(updated.id, first.id)
        self.assertEqual(updated.code, "третий")

        await h.callback("bk_edit")
        await h.callback("bk_edit_title")
        await h.text("Потерянное имя")
        await h.callback("bk_preview")
        with h.session_factory() as session:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            assert user is not None
            external = update_personal_brick(
                session,
                actor_telegram_user_id=user.telegram_user_id,
                brick_id=first.id,
                expected_revision=updated.revision,
                code="третий",
                title="Внешнее изменение",
                rules={},
            )
            self.assertEqual(external.status, "updated")
        await h.callback("bk_save")
        self.assertIn("уже изменился", h.screen_text())
        self.assertIn("Внешнее изменение", h.screen_text())
        self.assertNotIn("Потерянное имя", h.screen_text())
        final = next(row for row in self.personal_bricks(h) if row.id == first.id)
        self.assertEqual(final.title, "Внешнее изменение")
        self.assertEqual(final.id, first.id)
        self.assertEqual(final.revision, updated.revision + 1)
        self.assertNotEqual(final.id, second.id)

    @scenario_test
    async def test_archive_code_reservation_repeat_callbacks_and_restore(
        self, h
    ) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Архивный", "архивный")
        brick = self.personal_bricks(h)[0]
        original_id = brick.id
        await h.callback(f"bk_open:{original_id}")
        await h.callback("bk_archive")
        self.assertIn("Переместить кирпичик в архив", h.screen_text())
        await h.callback("bk_back")
        self.assertIn("Статус: 🟢 Активный", h.screen_text())
        await h.callback("bk_archive")
        await h.callback("bk_archive_confirm")
        self.assertIn("перемещён в архив", h.screen_text())
        self.assertIn("Статус: 🗄 В архиве", h.screen_text())
        archived = self.personal_bricks(h)[0]
        self.assertEqual(archived.id, original_id)
        self.assertEqual(archived.status, "archived")
        archived_revision = archived.revision
        await h.callback("bk_archive_confirm")
        self.assertIn("устарела", self.answer_text(h))
        self.assertEqual(self.personal_bricks(h)[0].revision, archived_revision)

        await h.callback("bk_back")
        self.assertIn("В активном списке пока нет кирпичиков", h.screen_text())
        self.assertIn(("🗄 Архив (1)", "bk_archlist"), h.screen_buttons())
        await h.callback("bk_create")
        await h.text("Новый с тем же кодом")
        await h.text("архивный")
        self.assertIn("уже используется", h.screen_text())
        self.assertEqual(len(self.personal_bricks(h)), 1)
        await h.callback("bk_back")
        await h.callback("bk_back")
        await h.callback("bk_archlist")
        self.assertIn("Архив кирпичиков", h.screen_text())
        await h.callback(f"bk_open:{original_id}")
        self.assertIn(("♻️ Восстановить", "bk_restore"), h.screen_buttons())
        self.assertNotIn("bk_edit", [data for _, data in h.screen_buttons()])
        await h.callback("bk_restore")
        self.assertIn("восстановлен", h.screen_text())
        restored = self.personal_bricks(h)[0]
        self.assertEqual(restored.id, original_id)
        self.assertEqual(restored.code, "архивный")
        self.assertEqual(restored.status, "active")

    @scenario_test
    async def test_cancel_creation_writes_nothing_and_cleans_temp_input(
        self, h
    ) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        title_update = await h.text("Временное название")
        code_update = await h.text("временный_код")
        description_update = await h.text("Временное описание")
        await h.callback("bk_type:short_text")
        self.assertIn("Проверьте кирпичик", h.screen_text())
        await h.callback("bk_cancel")
        self.assertIn("Создание отменено", h.screen_text())
        self.assertIn("В базе ничего не сохранено", h.screen_text())
        self.assertEqual(self.brick_rows(h), [])
        self.assertIn(title_update.message.message_id, h.deleted_ids())
        self.assertIn(code_update.message.message_id, h.deleted_ids())
        self.assertIn(description_update.message.message_id, h.deleted_ids())
        state = load_brick_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "hub")

    @scenario_test
    async def test_restart_recovers_wizard_and_screen(self, h) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("После рестарта")
        self.assertIn("[после_рестарта]", h.screen_text())
        await h.restart()
        await h.text("/bricks")
        self.assertIn("Техническое обозначение", h.screen_text())
        self.assertIn("[после_рестарта]", h.screen_text())
        await h.callback("bk_accept_code")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:short_text")
        await h.callback("bk_save")
        rows = self.personal_bricks(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].code, "после_рестарта")

    @scenario_test
    async def test_description_is_asked_stored_and_editable(self, h) -> None:
        """S5 п.5: описание — инструкция автору, видимая на вопросе."""
        await h.text("/bricks")
        await h.callback("bk_create")
        self.assertIn("Название видно в списках", h.screen_text())
        await h.text("Ведущий")
        self.assertIn("вставка <code>[код]</code>", h.screen_text())
        await h.text("ведущий")
        self.assertIn("Описание для автора анонса", h.screen_text())
        self.assertIn(("⏭ Без описания", "bk_skip_description"), h.screen_buttons())
        await h.text("Имя ведущего & <без должности>")
        self.assertIn("Выберите тип", h.screen_text())
        await h.callback("bk_type:short_text")
        self.assertIn("Проверьте кирпичик", h.screen_text())
        self.assertIn("&amp; &lt;без должности&gt;", h.screen_text())
        await h.callback("bk_save")
        brick = self.brick_rows(h)[0]
        # Описание хранится дословно, но экран экранирует его целиком.
        self.assertEqual(brick.description, "Имя ведущего & <без должности>")
        self.assertNotIn("& <без должности>", h.screen_text())

        await h.callback(f"bk_open:{brick.id}")
        # №27: предпросмотр описания — моноширинный блок `<code>`.
        self.assertIn(
            "Описание: <code>Имя ведущего &amp; &lt;без должности&gt;</code>",
            h.screen_text(),
        )
        await h.callback("bk_edit")
        self.assertIn(("📝 Описание: есть", "bk_edit_description"), h.screen_buttons())
        await h.callback("bk_edit_description")
        await h.text("Только имя")
        self.assertIn("Изменить кирпичик", h.screen_text())
        await h.callback("bk_preview")
        await h.callback("bk_save")
        updated = self.brick_rows(h)[0]
        self.assertEqual(updated.description, "Только имя")
        self.assertEqual(updated.id, brick.id)

        # «Без описания» очищает поле и не ломает шаг.
        await h.callback("bk_edit")
        await h.callback("bk_edit_description")
        await h.callback("bk_skip_description")
        await h.callback("bk_preview")
        self.assertIn("Описание: нет", h.screen_text())
        await h.callback("bk_save")
        self.assertIsNone(self.brick_rows(h)[0].description)

    @scenario_test
    async def test_navigation_and_cancel_discard_unfinished_draft(self, h) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Черновик навигации")
        await h.callback("main_menu")
        self.assertIsNone(load_brick_session(h.user_id))
        await h.text("случайный текст")
        self.assertIn("Воспользуйтесь кнопками меню", h.screen_text())
        self.assertEqual(self.brick_rows(h), [])

        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Черновик отмены")
        await h.text("/cancel")
        self.assertIn("кирпичика отменено", h.screen_text())
        self.assertIsNone(load_brick_session(h.user_id))
        await h.text("ещё текст")
        self.assertIn("Воспользуйтесь кнопками меню", h.screen_text())
        self.assertEqual(self.brick_rows(h), [])

    @scenario_test
    async def test_post_and_brick_flows_do_not_capture_each_other(self, h) -> None:
        # Конструктор стартует с выбора площадки: без места диалог не
        # начинается вовсе, поэтому площадка создаётся заранее.
        with h.session_factory() as session:
            user = get_or_create_user(session, h.user_id, username="bricks")
            place = create_place(
                session,
                "Театр кирпичиков",
                city="Тестовый город",
                created_by_user_id=user.id,
            )
            session.commit()
            place_id = place.id
        await h.start_dialog()
        await h.callback(f"pl_pick:{place_id}")
        self.assertIsNotNone(h.dialog_state())
        await h.text("/bricks")
        self.assertIsNone(h.dialog_state())
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertEqual(len(h.user_states()), 1)

        await h.callback("bk_create")
        await h.text("Кирпичик перед постом")
        self.assertIn("Техническое обозначение", h.screen_text())
        # S5: кнопка `create_post` устарела — легаси-мастер удалён. Внутри
        # сценария кирпичика она не запускает мастер и не сбрасывает
        # незавершённое создание кирпичика.
        await h.callback("create_post")
        self.assertNotIn("Шаг 1", h.screen_text())
        self.assertIn("Техническое обозначение", h.screen_text())
        self.assertIsNone(h.dialog_state())
        state = load_brick_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "code_input")

        # Из меню устаревшая кнопка просто перерисовывает меню.
        await h.callback("main_menu")
        await h.callback("create_post")
        self.assertIn("Здравствуйте", h.screen_text())
        self.assertIsNone(h.dialog_state())

        await h.text("/bricks")
        self.assertIn("Мои кирпичики", h.screen_text())
        await h.callback("bk_create")
        await h.text("Между сценариями")
        await h.callback("main_menu")
        # Конструктор анонса — единственный вход, он открывается из меню.
        await h.start_dialog()
        await h.callback(f"pl_pick:{place_id}")
        self.assertIsNotNone(h.dialog_state())
        self.assertIn("Вопрос 1 из", h.screen_text())

    @scenario_test
    async def test_pagination_and_archive_pages(self, h) -> None:
        await self.seed_bricks(h, active=10, archived=9)
        await h.text("/bricks")
        self.assertIn("Показано 1–8 из 10", h.screen_text())
        self.assertIn(("▶️", "bk_page:8"), h.screen_buttons())
        await h.callback("bk_page:8")
        self.assertIn("Показано 9–10 из 10", h.screen_text())
        self.assertNotIn("bk_page:8", [data for _, data in h.screen_buttons()])
        self.assertIn(("🗄 Архив (9)", "bk_archlist"), h.screen_buttons())
        await h.callback("bk_archlist")
        self.assertIn("Показано 1–8 из 9", h.screen_text())
        await h.callback("bk_apage:8")
        self.assertIn("Показано 9–9 из 9", h.screen_text())
        await h.callback("bk_hub")
        self.assertIn("Активных кирпичиков: 10", h.screen_text())

    @scenario_test
    async def test_stale_callbacks_and_repeated_save_are_idempotent(self, h) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Один", "один")
        self.assertEqual(len(self.personal_bricks(h)), 1)
        previous_text = h.screen_text()
        await h.callback("bk_save")
        self.assertEqual(h.screen_text(), previous_text)
        self.assertIn("устарела", self.answer_text(h))
        self.assertEqual(len(self.personal_bricks(h)), 1)
        await h.callback("bk_open:9999")
        self.assertIn("Кирпичик не найден", h.screen_text())
        await h.callback("bk_unknown")
        self.assertIn("устарела", self.answer_text(h))
        self.assertEqual(len(self.personal_bricks(h)), 1)

    @scenario_test
    async def test_database_error_keeps_preview_and_then_retry_succeeds(
        self, h
    ) -> None:
        await h.text("/bricks")
        await h.callback("bk_create")
        await h.text("Сбойный")
        await h.text("сбойный")
        await h.callback("bk_skip_description")
        await h.callback("bk_type:short_text")
        error = OperationalError("INSERT bricks", {}, Exception("disk I/O error"))
        with (
            patch("handlers.bricks.create_personal_brick", side_effect=error),
            patch("handlers.bricks._code_available", side_effect=error),
            self.assertLogs("handlers.bricks", level="ERROR"),
        ):
            await h.callback("bk_save")
        self.assertIn("Не удалось сохранить кирпичик", h.screen_text())
        self.assertIn("Проверьте кирпичик", h.screen_text())
        self.assertEqual(self.brick_rows(h), [])
        state = load_brick_session(h.user_id)
        assert state is not None
        self.assertEqual(state["phase"], "preview")
        await h.callback("bk_save")
        self.assertIn("создан", h.screen_text())
        self.assertEqual(len(self.personal_bricks(h)), 1)

    @scenario_test
    async def test_other_user_cannot_open_or_edit_personal_brick(self, h) -> None:
        await h.text("/bricks")
        await self.create_simple(h, "Личный", "личный")
        brick = self.personal_bricks(h)[0]
        other_user_id = h.user_id + 500
        await h.text("/bricks", user_id=other_user_id)
        self.assertIn("У вас пока нет личных кирпичиков", h.screen_text())
        await h.callback(f"bk_open:{brick.id}", user_id=other_user_id)
        self.assertIn("Кирпичик не найден", h.screen_text())
        with h.session_factory() as session:
            stored = session.get(Brick, brick.id)
            assert stored is not None
            self.assertEqual(stored.title, "Личный")
            self.assertIsNone(
                session.scalar(
                    select(User).where(User.telegram_user_id == other_user_id)
                ).username
            )


if __name__ == "__main__":
    unittest.main()
