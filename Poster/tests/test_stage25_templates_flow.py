# tests/test_stage25_templates_flow.py
"""
Stage 25: Telegram-сценарии «Мои шаблоны» через Application.process_update.

Настоящее приложение, FakeBot и временная SQLite. Проверяются фактический
текст экрана, кнопки, callback и сохранённые строки; живой Telegram и
настоящая база не нужны.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import select

from announcement_store import list_announcement_drafts
from brick_store import create_personal_brick, create_place_brick
from models import AnnouncementDraft, PlaceAdmin, Template, TemplateSnapshot, utcnow
from place_store import create_place
from template_state import (
    load_template_session,
    save_template_session,
)
from template_store import (
    create_personal_template,
    create_template_snapshot,
    get_builtin_template,
    get_template_version,
    update_template_content,
)
from template_types import BUILTIN_TEMPLATE
from tests.integration_harness import (
    HARNESS_IMPORT_ERROR,
    scenario_test,
)
from tests.stage23_helpers import OTHER_TG, OWNER_TG, SUPERADMIN_TG
from user_store import get_or_create_user

try:
    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001
    INTEGRATION_ERROR = str(e)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class TemplatesUiTests(unittest.TestCase):
    """Telegram-интерфейс раздела «Мои шаблоны»."""

    def personal_templates(self, h) -> list:
        with h.session_factory() as session:
            from models import Template

            return list(
                session.scalars(
                    select(Template)
                    .where(Template.kind == "personal")
                    .order_by(Template.id)
                ).all()
            )

    def make_user(self, h, telegram_user_id: int):
        with h.session_factory() as session:
            return get_or_create_user(
                session,
                telegram_user_id,
                username=f"user{telegram_user_id}",
                full_name=f"User {telegram_user_id}",
            )

    def make_personal_template(self, h) -> int:
        """Личный шаблон из одного кирпичика: минимальный конструктор."""
        with h.session_factory() as session:
            get_or_create_user(session, h.user_id, username="templates")
            create_personal_brick(
                session,
                actor_telegram_user_id=h.user_id,
                code="ttl",
                title="Заголовок",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=h.user_id,
                name="Личный для теста",
                content="\n".join(p for p in ("", "[ttl]", "") if p),
            )
            session.commit()
            return template.template_id

    def make_place_with_admin(self, h, admin_tg: int) -> tuple[int, int]:
        """Место со стандартным шаблоном и действующим админом."""
        with h.session_factory() as session:
            admin = get_or_create_user(
                session,
                admin_tg,
                username=f"admin{admin_tg}",
                full_name=f"Admin {admin_tg}",
            )
            place = create_place(
                session,
                f"Место {admin_tg}",
                city="Тестовый город",
                created_by_user_id=admin.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=admin.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            template = session.scalar(
                select(Template).where(
                    Template.place_id == place.id,
                    Template.kind == "standard",
                )
            )
            assert template is not None
            return place.id, template.id

    def template_versions(self, h, template_id: int) -> list:
        with h.session_factory() as session:
            from models import TemplateVersion

            return list(
                session.scalars(
                    select(TemplateVersion)
                    .where(TemplateVersion.template_id == template_id)
                    .order_by(TemplateVersion.version)
                ).all()
            )

    def template_row(self, h, template_id: int):
        with h.session_factory() as session:
            return session.get(Template, template_id)

    def screen_callbacks(self, h) -> set[str | None]:
        return {callback for _text, callback in h.screen_buttons()}

    @scenario_test
    async def test_open_empty_templates_shows_explanation(self, h) -> None:
        """Открытие «Мои шаблоны» при отсутствии шаблонов."""
        self.make_user(h, OWNER_TG)
        await h.text("/templates", user_id=OWNER_TG)
        self.assertIn("Личных шаблонов пока нет", h.screen_text())
        self.assertIn(("➕ Создать шаблон", "tp_create"), h.screen_buttons())
        self.assertIn(("◀️ Назад", "main_menu"), h.screen_buttons())

    @scenario_test
    async def test_open_templates_with_list_and_pagination(self, h) -> None:
        """Открытие «Мои шаблоны» с существующими шаблонами."""
        self.make_user(h, OWNER_TG)
        with h.session_factory() as session:
            create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Первый",
                content="\n".join(
                    p for p in ("Шапка", "Тело [название]", "Подвал") if p
                ),
            )
        await h.text("/templates", user_id=OWNER_TG)
        self.assertIn("Первый", h.screen_text())
        self.assertIn(("➕ Создать шаблон", "tp_create"), h.screen_buttons())

    @scenario_test
    async def test_create_template_via_standard(self, h) -> None:
        """Создание шаблона на основе встроенного стандарта."""
        self.make_user(h, OWNER_TG)
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback("tp_create", user_id=OWNER_TG)
        self.assertIn("Введите название", h.screen_text())
        await h.text("Моя афиша", user_id=OWNER_TG)
        self.assertIn("С чего начать?", h.screen_text())
        await h.callback("tp_cstd", user_id=OWNER_TG)
        self.assertIn("Создать шаблон?", h.screen_text())
        await h.callback("tp_csave", user_id=OWNER_TG)
        self.assertIn("Моя афиша", h.screen_text())
        rows = self.personal_templates(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Моя афиша")

    @scenario_test
    async def test_create_template_via_empty(self, h) -> None:
        """Создание пустого шаблона."""
        self.make_user(h, OWNER_TG)
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback("tp_create", user_id=OWNER_TG)
        await h.text("Пустой", user_id=OWNER_TG)
        await h.callback("tp_cempty", user_id=OWNER_TG)
        self.assertIn("Создать шаблон?", h.screen_text())
        await h.callback("tp_csave", user_id=OWNER_TG)
        self.assertIn("Пустой", h.screen_text())
        rows = self.personal_templates(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "Пустой")

    @scenario_test
    async def test_edit_body_saves_new_version(self, h) -> None:
        """Редактирование тела шаблона через текстовый ввод."""
        self.make_user(h, OWNER_TG)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Редактируемый",
                content="\n".join(
                    p for p in ("Шапка", "Старое тело [название]", "Подвал") if p
                ),
            )
        tid = created.template_id
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback(f"tp_open:{tid}", user_id=OWNER_TG)
        self.assertIn("Редактируемый", h.screen_text())
        await h.callback(f"tp_edit:{tid}", user_id=OWNER_TG)
        self.assertIn("Правка текста", h.screen_text())
        await h.text("Новое тело [дата]", user_id=OWNER_TG)
        self.assertIn("Новое тело", h.screen_text())
        await h.callback("tp_save", user_id=OWNER_TG)
        self.assertIn("Редактируемый", h.screen_text())
        rows = self.personal_templates(h)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].revision, 2)

    @scenario_test
    async def test_reset_to_standard(self, h) -> None:
        """Сброс личного шаблона к встроенному стандарту."""
        self.make_user(h, OWNER_TG)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Сбрасываемый",
                content="\n".join(
                    p
                    for p in ("Кастомная шапка", "Кастомное тело", "Кастомный подвал")
                    if p
                ),
            )
        tid = created.template_id
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback(f"tp_open:{tid}", user_id=OWNER_TG)
        await h.callback(f"tp_reset:{tid}", user_id=OWNER_TG)
        self.assertIn("Восстановить стандарт?", h.screen_text())
        await h.callback(f"tp_reset_yes:{tid}", user_id=OWNER_TG)
        self.assertIn("Сбрасываемый", h.screen_text())
        rows = self.personal_templates(h)
        self.assertEqual(rows[0].revision, 2)
        # Контент сброшен к встроенному.
        v2 = get_template_version_direct(h, rows[0].id, 2)
        self.assertEqual(v2.content, BUILTIN_TEMPLATE.content)
        self.assertEqual(v2.extended_content, BUILTIN_TEMPLATE.extended_content)

    @scenario_test
    async def test_other_user_cannot_access_foreign_template(self, h) -> None:
        """Посторонний пользователь не видит чужой шаблон."""
        self.make_user(h, OWNER_TG)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Секретный",
                content="\n".join(p for p in ("Шапка", "Тело", "Подвал") if p),
            )
        other_tg = OWNER_TG + 100
        self.make_user(h, other_tg)
        await h.text("/templates", user_id=other_tg)
        self.assertIn("Личных шаблонов пока нет", h.screen_text())
        # Прямой callback тоже не открывает.
        await h.callback(f"tp_open:{created.template_id}", user_id=other_tg)
        self.assertIn(
            "Шаблон не найден",
            h.answers()[-1].get("text", "") if h.answers() else "",
        )

    @scenario_test
    async def test_unknown_brick_rejected(self, h) -> None:
        """Неизвестный кирпичик отклоняется при попытке сохранить."""
        self.make_user(h, OWNER_TG)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Валидация",
                content="\n".join(
                    p for p in ("Шапка", "Тело [название]", "Подвал") if p
                ),
            )
        tid = created.template_id
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback(f"tp_open:{tid}", user_id=OWNER_TG)
        await h.callback(f"tp_edit:{tid}", user_id=OWNER_TG)
        await h.text("Текст [несуществующий]", user_id=OWNER_TG)
        self.assertIn("несуществующий", h.screen_text())
        await h.callback("tp_save", user_id=OWNER_TG)
        # Версия не изменилась.
        rows = self.personal_templates(h)
        self.assertEqual(rows[0].revision, 1)

    @scenario_test
    async def test_cancel_clears_state(self, h) -> None:
        """Отмена очищает интерфейсное состояние."""
        self.make_user(h, OWNER_TG)
        await h.text("/templates", user_id=OWNER_TG)
        await h.callback("tp_create", user_id=OWNER_TG)
        await h.callback("tp_cancel", user_id=OWNER_TG)
        self.assertIn("Личных шаблонов пока нет", h.screen_text())
        self.assertIsNone(load_template_session(h.user_id))

    @scenario_test
    async def test_templates_command_in_post_wizard_keeps_announcement(self, h) -> None:
        """/templates во время конструктора не теряет начатый анонс (S5)."""
        self.make_user(h, h.user_id)
        template_id = self.make_personal_template(h)
        await h.text("/start")
        await h.callback(f"tp_pick:{template_id}")
        await h.text("Незавершённый заголовок", user_id=h.user_id)
        await h.text("/templates", user_id=h.user_id)
        self.assertIsNone(h.dialog_state())
        self.assertIn("Мои шаблоны", h.screen_text())

        with h.session_factory() as session:
            drafts = list(session.scalars(select(AnnouncementDraft)).all())
        self.assertEqual(len(drafts), 1)
        # Значение сохранено в анонсе, а не в удалённом легаси-черновике.
        with h.session_factory() as session:
            details = list_announcement_drafts(
                session, actor_telegram_user_id=h.user_id
            )
        self.assertEqual(len(details), 1)
        self.assertEqual(
            [value.payload for value in details[0].values],
            [{"text": "Незавершённый заголовок"}],
        )

    @scenario_test
    async def test_standard_template_admin_edit_reset_and_back(self, h) -> None:
        """Стандартный шаблон из админки: правка, снимок, сброс, возврат."""
        admin_tg = OWNER_TG
        place_id, template_id = self.make_place_with_admin(h, admin_tg)
        await h.callback(f"tp_std:{place_id}", user_id=admin_tg)
        self.assertIn("Стандартный шаблон места", h.screen_text())
        self.assertIn("Версия: 1", h.screen_text())
        buttons = self.screen_callbacks(h)
        self.assertIn(f"tp_edit:{template_id}", buttons)
        self.assertIn(f"tp_back_place:{place_id}", buttons)

        await h.callback(f"tp_edit:{template_id}", user_id=admin_tg)
        self.assertIn("Правка текста", h.screen_text())
        await h.text("Своё тело [название]", user_id=admin_tg)
        self.assertIn("Своё тело", h.screen_text())
        await h.callback("tp_save", user_id=admin_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)

        with h.session_factory() as session:
            snapshot = create_template_snapshot(
                session,
                actor_telegram_user_id=admin_tg,
                template_id=template_id,
                expected_revision=2,
            )
        await h.callback(f"tp_reset:{template_id}", user_id=admin_tg)
        self.assertIn("Восстановить стандарт?", h.screen_text())
        await h.callback(f"tp_reset_yes:{template_id}", user_id=admin_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 3)
        versions = self.template_versions(h, template_id)
        self.assertEqual([item.version for item in versions], [1, 2, 3])
        self.assertEqual(versions[1].content, "Своё тело [название]")
        builtin = get_builtin_template()
        self.assertEqual(versions[2].content, builtin.content)
        self.assertEqual(versions[2].builtin_version, builtin.version)
        with h.session_factory() as session:
            snapshot_row = session.get(TemplateSnapshot, snapshot.snapshot_id)
        assert snapshot_row is not None
        self.assertEqual(snapshot_row.source_template_version, 2)
        self.assertEqual(snapshot_row.content, "Своё тело [название]")

        await h.callback(f"tp_back_place:{place_id}", user_id=admin_tg)
        self.assertIn(f"Место {admin_tg}", h.screen_text())
        self.assertIsNone(load_template_session(admin_tg))
        self.assertIn(
            f"tp_std:{place_id}",
            self.screen_callbacks(h),
        )

    @scenario_test
    async def test_standard_template_access_admin_and_superadmin(self, h) -> None:
        """Стандарт закрыт для постороннего и открыт суперадмину."""
        admin_tg = OTHER_TG
        stranger_tg = OWNER_TG
        place_id, _template_id = self.make_place_with_admin(h, admin_tg)
        self.make_user(h, stranger_tg)

        await h.callback(f"tp_std:{place_id}", user_id=stranger_tg)
        self.assertIn(
            "только администратору места",
            h.answers()[-1].get("text", ""),
        )
        self.assertIsNone(load_template_session(stranger_tg))

        with patch("config.SUPERADMIN_USER_IDS", (stranger_tg,)):
            await h.callback(f"tp_std:{place_id}", user_id=stranger_tg)
            self.assertIn("Стандартный шаблон места", h.screen_text())
            await h.callback(f"tp_back_place:{place_id}", user_id=stranger_tg)
            self.assertIn(f"Место {admin_tg}", h.screen_text())
            self.assertIn(f"tp_std:{place_id}", self.screen_callbacks(h))
        self.assertIsNone(load_template_session(stranger_tg))

    @scenario_test
    async def test_standard_template_rights_rechecked_on_save(self, h) -> None:
        """После отзыва админки сохранение отклоняется, текст остаётся."""
        admin_tg = OWNER_TG
        place_id, template_id = self.make_place_with_admin(h, admin_tg)
        await h.callback(f"tp_std:{place_id}", user_id=admin_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=admin_tg)
        await h.text("Текст до отзыва прав [название]", user_id=admin_tg)
        self.assertIn("Текст до отзыва прав", h.screen_text())

        with h.session_factory() as session:
            admin_row = session.scalar(
                select(PlaceAdmin).where(
                    PlaceAdmin.place_id == place_id,
                    PlaceAdmin.revoked_at.is_(None),
                )
            )
            assert admin_row is not None
            admin_row.revoked_at = utcnow()
            session.commit()

        await h.callback("tp_save", user_id=admin_tg)
        self.assertIn("Нет прав на изменение шаблона", h.screen_text())
        self.assertIn("Текст до отзыва прав", h.screen_text())
        state = load_template_session(admin_tg)
        assert state is not None
        self.assertEqual(state["phase"], "section_error")
        self.assertEqual(state["error_kind"], "forbidden")
        self.assertEqual(state["draft_text"], "Текст до отзыва прав [название]")
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 1)
        self.assertEqual(len(self.template_versions(h, template_id)), 1)

        # Экран запрета остаётся валидным: после восстановления прав можно
        # вернуться к тому же тексту и сохранить его, не теряя черновик.
        with h.session_factory() as session:
            admin_row = session.scalar(
                select(PlaceAdmin).where(
                    PlaceAdmin.place_id == place_id,
                    PlaceAdmin.revoked_at.is_not(None),
                )
            )
            assert admin_row is not None
            admin_row.revoked_at = None
            session.commit()

        await h.callback("tp_retry", user_id=admin_tg)
        self.assertIn("Текст до отзыва прав", h.screen_text())
        await h.text("Текст после восстановления прав [название]", user_id=admin_tg)
        await h.callback("tp_save", user_id=admin_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)

    @scenario_test
    async def test_stale_revision_preserves_text_and_refresh_saves(self, h) -> None:
        """Конфликт revision не перезаписывает чужие изменения."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="Конфликт",
                content="\n".join(
                    p for p in ("Шапка", "Старое тело [название]", "Подвал") if p
                ),
            )
        template_id = created.template_id
        await h.text("/templates", user_id=owner_tg)
        await h.callback(f"tp_open:{template_id}", user_id=owner_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=owner_tg)

        with h.session_factory() as session:
            updated = update_template_content(
                session,
                actor_telegram_user_id=owner_tg,
                template_id=template_id,
                expected_revision=1,
                content="Чужая правка",
            )
        self.assertEqual(updated.status, "updated")

        await h.text("Мой новый текст [дата]", user_id=owner_tg)
        self.assertIn("изменился в другом окне", h.screen_text())
        self.assertIn("Мой новый текст", h.screen_text())
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)
        with h.session_factory() as session:
            current = get_template_version(session, owner_tg, template_id, 2)
        assert current is not None
        # H2: текст один, поэтому версия 2 — ровно чужая правка целиком;
        # наш черновик в неё не попал.
        self.assertEqual(current.content, "Чужая правка")

        await h.callback("tp_refresh", user_id=owner_tg)
        self.assertIn("Версия обновлена до 2", h.screen_text())
        self.assertIn("Мой новый текст", h.screen_text())
        self.assertEqual(
            h.screen_text().count(
                "Ваш текст сохранён. Проверьте его и подтвердите сохранение."
            ),
            1,
        )
        await h.callback("tp_save", user_id=owner_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 3)
        with h.session_factory() as session:
            saved = get_template_version(session, owner_tg, template_id, 3)
        assert saved is not None
        self.assertEqual(saved.content, "Мой новый текст [дата]")

        # Повторное/устаревшее «Сохранить» после успеха не создаёт версию.
        await h.callback("tp_save", user_id=owner_tg)
        self.assertIn("больше не работает", h.answers()[-1].get("text", ""))
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 3)
        self.assertEqual(len(self.template_versions(h, template_id)), 3)

    @scenario_test
    async def test_restart_resumes_confirm_and_saving(self, h) -> None:
        """После рестарта подтверждение и прерванное сохранение продолжаются."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="После рестарта",
                content="\n".join(
                    p for p in ("Шапка", "Тело [название]", "Подвал") if p
                ),
            )
        template_id = created.template_id
        await h.text("/templates", user_id=owner_tg)
        await h.callback(f"tp_open:{template_id}", user_id=owner_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=owner_tg)
        await h.text("Текст после рестарта [дата]", user_id=owner_tg)
        state = load_template_session(owner_tg)
        assert state is not None
        self.assertEqual(state["phase"], "confirm_save")
        state["phase"] = "saving"
        save_template_session(owner_tg, state)

        await h.restart()
        await h.text("/templates", user_id=owner_tg)
        self.assertIn("Сохранение не подтверждено", h.screen_text())
        self.assertIn("Текст после рестарта", h.screen_text())
        await h.callback("tp_save", user_id=owner_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)
        with h.session_factory() as session:
            saved = get_template_version(session, owner_tg, template_id, 2)
        assert saved is not None
        self.assertEqual(saved.content, "Текст после рестарта [дата]")

    @scenario_test
    async def test_ambiguity_pick_saves_concrete_brick_id(self, h) -> None:
        """Неоднозначный код разрешается кнопкой и сохраняет brick_id."""
        admin_tg = OWNER_TG
        place_id, template_id = self.make_place_with_admin(h, admin_tg)
        with h.session_factory() as session:
            place_brick = create_place_brick(
                session,
                actor_telegram_user_id=admin_tg,
                place_id=place_id,
                code="тестовый",
                title="Местный кирпичик",
                value_type="short_text",
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=admin_tg,
                code="тестовый",
                title="Личный кирпичик",
                value_type="short_text",
            )
        await h.callback(f"tp_std:{place_id}", user_id=admin_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=admin_tg)
        await h.text("[тестовый]", user_id=admin_tg)
        self.assertIn("Какой кирпичик использовать?", h.screen_text())
        state = load_template_session(admin_tg)
        assert state is not None
        groups = state["pending_ambiguities"]
        self.assertEqual(len(groups), 1)
        candidates = groups[0]["candidates"]
        place_index = next(
            index
            for index, candidate in enumerate(candidates)
            if candidate["scope"] == "place"
        )
        self.assertEqual(candidates[place_index]["brick_id"], place_brick.id)
        self.assertIn(f"tp_pick:0:{place_index}", self.screen_callbacks(h))
        await h.callback(f"tp_pick:0:{place_index}", user_id=admin_tg)
        self.assertIn("Выбран кирпичик", h.screen_text())
        self.assertIn("[тестовый]", h.screen_text())
        await h.callback("tp_save", user_id=admin_tg)
        with h.session_factory() as session:
            saved = get_template_version(session, admin_tg, template_id, 2)
        assert saved is not None
        self.assertEqual(saved.content, "[тестовый]")
        self.assertIn(place_brick.id, saved.brick_ids)

    @scenario_test
    async def test_ambiguity_brick_list_returns_to_same_choice(self, h) -> None:
        """Из неоднозначности можно открыть кирпичики и вернуться."""
        admin_tg = OWNER_TG
        place_id, template_id = self.make_place_with_admin(h, admin_tg)
        with h.session_factory() as session:
            create_place_brick(
                session,
                actor_telegram_user_id=admin_tg,
                place_id=place_id,
                code="тестовый",
                title="Местный кирпичик",
                value_type="short_text",
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=admin_tg,
                code="тестовый",
                title="Личный кирпичик",
                value_type="short_text",
            )
        await h.callback(f"tp_std:{place_id}", user_id=admin_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=admin_tg)
        await h.text("[тестовый]", user_id=admin_tg)
        self.assertIn("Какой кирпичик использовать?", h.screen_text())
        self.assertIn(f"tp_bricks:{template_id}", self.screen_callbacks(h))

        await h.callback(f"tp_bricks:{template_id}", user_id=admin_tg)
        self.assertIn("Доступные кирпичики", h.screen_text())
        self.assertNotIn("устарела", h.answers()[-1].get("text") or "")
        await h.callback("tp_back", user_id=admin_tg)
        self.assertIn("Какой кирпичик использовать?", h.screen_text())
        self.assertNotIn("устарела", h.answers()[-1].get("text") or "")
        state = load_template_session(admin_tg)
        assert state is not None
        self.assertEqual(state["phase"], "resolve_ambiguity")
        self.assertEqual(state["draft_text"], "[тестовый]")

    @scenario_test
    async def test_photos_do_not_change_section_draft(self, h) -> None:
        """Фото и альбом в текстовой фазе не становятся текстом шаблона."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="Фото",
                content="\n".join(
                    p for p in ("Шапка", "Исходное тело [название]", "Подвал") if p
                ),
            )
        template_id = created.template_id
        await h.text("/templates", user_id=owner_tg)
        await h.callback(f"tp_open:{template_id}", user_id=owner_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=owner_tg)
        before = load_template_session(owner_tg)
        assert before is not None

        await h.photo("photo-1", user_id=owner_tg)
        await h.photo("photo-2", media_group_id="album-1", user_id=owner_tg)
        await h.photo("photo-3", media_group_id="album-1", user_id=owner_tg)
        after = load_template_session(owner_tg)
        assert after is not None
        self.assertEqual(after["phase"], "section_edit")
        self.assertEqual(after["draft_text"], before["draft_text"])
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 1)

        await h.text("Текст после фото [название]", user_id=owner_tg)
        await h.callback("tp_save", user_id=owner_tg)
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)

    @scenario_test
    async def test_full_unicode_text_and_clear_section(self, h) -> None:
        """Полный текст с Unicode/переводами строк сохраняется без обрезки."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        with h.session_factory() as session:
            created = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="Unicode",
                content="\n".join(
                    p for p in ("Шапка", "Старое [название]", "Старый подвал") if p
                ),
            )
        template_id = created.template_id
        full_text = (
            "  Первая строка  \n"
            "Вторая строка\n"
            "Юникод: 🎭 — «ёжик», 𐍈\n"
            "[название] и [дата]  "
        )
        await h.text("/templates", user_id=owner_tg)
        await h.callback(f"tp_open:{template_id}", user_id=owner_tg)
        await h.callback(f"tp_edit:{template_id}", user_id=owner_tg)
        await h.text(full_text, user_id=owner_tg)
        self.assertIn("Юникод: 🎭", h.screen_text())
        await h.callback("tp_save", user_id=owner_tg)
        with h.session_factory() as session:
            saved = get_template_version(session, owner_tg, template_id, 2)
        assert saved is not None
        self.assertEqual(saved.content, full_text)

        # H2: текст шаблона один, поэтому «Очистить» оставляет пустой
        # черновик — сохранить его нельзя, прежняя версия остаётся.
        await h.callback(f"tp_edit:{template_id}", user_id=owner_tg)
        await h.callback("tp_clear", user_id=owner_tg)
        self.assertIn("Текст не сохранён", h.screen_text())
        self.assertIn("пустых строк", h.screen_text())
        row = self.template_row(h, template_id)
        assert row is not None
        self.assertEqual(row.revision, 2)
        with h.session_factory() as session:
            unchanged = get_template_version(session, owner_tg, template_id, 2)
        assert unchanged is not None
        self.assertEqual(unchanged.content, full_text)

    @scenario_test
    async def test_brick_list_scoping_and_pagination(self, h) -> None:
        """Личный шаблон не показывает чужие/местовые кирпичики."""
        owner_tg = OWNER_TG
        other_tg = OTHER_TG
        admin_tg = SUPERADMIN_TG
        self.make_user(h, owner_tg)
        self.make_user(h, other_tg)
        place_id, _template_id = self.make_place_with_admin(h, admin_tg)
        with h.session_factory() as session:
            create_personal_brick(
                session,
                actor_telegram_user_id=owner_tg,
                code="мой_код",
                title="Мой кирпичик",
                value_type="short_text",
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=other_tg,
                code="чужой_код",
                title="Чужой кирпичик",
                value_type="short_text",
            )
            create_place_brick(
                session,
                actor_telegram_user_id=admin_tg,
                place_id=place_id,
                code="местный_код",
                title="Местный кирпичик",
                value_type="short_text",
            )
            created = create_personal_template(
                session,
                actor_telegram_user_id=owner_tg,
                name="Кирпичики",
                content="\n".join(p for p in ("Шапка", "[мой_код]", "Подвал") if p),
            )
        template_id = created.template_id
        await h.text("/templates", user_id=owner_tg)
        await h.callback(f"tp_open:{template_id}", user_id=owner_tg)
        await h.callback(f"tp_bricks:{template_id}", user_id=owner_tg)
        first_page = h.screen_text()
        self.assertIn("Для личного шаблона", first_page)
        self.assertNotIn("чужой_код", first_page)
        self.assertNotIn("местный_код", first_page)
        self.assertIn("tp_bpage:8", self.screen_callbacks(h))
        await h.callback("tp_bpage:8", user_id=owner_tg)
        second_page = h.screen_text()
        self.assertIn("[мой_код]", second_page)
        self.assertNotIn("чужой_код", second_page)
        self.assertNotIn("местный_код", second_page)
        self.assertIn("tp_back", self.screen_callbacks(h))

    @scenario_test
    async def test_standard_brick_list_has_place_but_not_personal(self, h) -> None:
        """Стандарт: системные + кирпичики места, без личных."""
        admin_tg = OWNER_TG
        place_id, template_id = self.make_place_with_admin(h, admin_tg)
        with h.session_factory() as session:
            create_place_brick(
                session,
                actor_telegram_user_id=admin_tg,
                place_id=place_id,
                code="местовой_код",
                title="Кирпичик места",
                value_type="short_text",
            )
            create_personal_brick(
                session,
                actor_telegram_user_id=admin_tg,
                code="личный_админа",
                title="Личный админа",
                value_type="short_text",
            )
        await h.callback(f"tp_std:{place_id}", user_id=admin_tg)
        await h.callback(f"tp_bricks:{template_id}", user_id=admin_tg)
        first_page = h.screen_text()
        self.assertIn("Для стандартного шаблона", first_page)
        self.assertNotIn("личный_админа", first_page)
        self.assertNotIn("ID", first_page)
        # Системных кирпичиков больше страницы: доступна вторая страница.
        self.assertIn("tp_bpage:8", self.screen_callbacks(h))
        await h.callback("tp_bpage:8", user_id=admin_tg)
        second_page = h.screen_text()
        self.assertIn("[местовой_код]", second_page)
        self.assertNotIn("личный_админа", second_page)
        self.assertIn("tp_bpage:0", self.screen_callbacks(h))

    @scenario_test
    async def test_template_list_pagination(self, h) -> None:
        """Список личных шаблонов листается без потери состояния."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        with h.session_factory() as session:
            for index in range(10):
                create_personal_template(
                    session,
                    actor_telegram_user_id=owner_tg,
                    name=f"Шаблон {index + 1:02d}",
                    content="\n".join(
                        p for p in ("Шапка", "[название]", "Подвал") if p
                    ),
                )
        await h.text("/templates", user_id=owner_tg)
        self.assertIn("Шаблон 01", h.screen_text())
        self.assertNotIn("Шаблон 10", h.screen_text())
        self.assertIn("tp_page:1", self.screen_callbacks(h))
        await h.callback("tp_page:1", user_id=owner_tg)
        self.assertIn("Шаблон 09", h.screen_text())
        self.assertIn("Шаблон 10", h.screen_text())
        self.assertNotIn("Шаблон 01", h.screen_text())
        self.assertIn("tp_page:0", self.screen_callbacks(h))

    @scenario_test
    async def test_create_name_retry_and_navigation_clears_session(self, h) -> None:
        """Неверное имя не теряет сценарий, /places очищает сессию."""
        owner_tg = OWNER_TG
        self.make_user(h, owner_tg)
        await h.text("/templates", user_id=owner_tg)
        await h.callback("tp_create", user_id=owner_tg)
        await h.text("   ", user_id=owner_tg)
        self.assertIn("не должно быть пустым", h.screen_text())
        state = load_template_session(owner_tg)
        assert state is not None
        self.assertEqual(state["phase"], "await_new_name")
        await h.text("Нормальное имя", user_id=owner_tg)
        state = load_template_session(owner_tg)
        assert state is not None
        self.assertEqual(state["phase"], "create_choice")
        self.assertEqual(state["name"], "Нормальное имя")

        await h.text("/places", user_id=owner_tg)
        self.assertIsNone(load_template_session(owner_tg))
        self.assertIn("Места", h.screen_text())


def get_template_version_direct(h, template_id: int, version: int):
    """Прямое чтение версии из БД для проверки содержимого."""
    with h.session_factory() as session:
        from models import TemplateVersion

        return session.scalars(
            select(TemplateVersion).where(
                TemplateVersion.template_id == template_id,
                TemplateVersion.version == version,
            )
        ).one()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
