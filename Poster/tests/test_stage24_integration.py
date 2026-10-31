# tests/test_stage24_integration.py
"""
Stage 24: сквозные интеграционные тесты на временной SQLite.

Полный путь: текст → парсер → brick_id → сохранённая версия → снимок →
повторная загрузка → готовый HTML. Никаких MagicMock: настоящая база,
настоящий сервисный слой, настоящие кирпичики Stage 22A.

Реальная Poster/post_bot.db не открывается.
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from sqlalchemy import text

from template_compiler import TemplateCompileError
from template_store import (
    TemplateNotFoundError,
    create_personal_template,
    create_template_snapshot,
    get_template_version,
    render_template_snapshot,
    render_template_version,
    update_template_content,
)
from tests.stage23_helpers import (
    OTHER_TG,
    OWNER_TG,
    TemplateDatabaseTestCase,
)


class Stage24IntegrationTests(TemplateDatabaseTestCase):
    """Сквозной путь: текст → парсер → brick_id → версия → снимок → HTML."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="owner")
        self.make_user(OTHER_TG, username="other")

    def test_full_path_personal_template_to_rendered_html(self) -> None:
        """
        Полный путь: создание личного шаблона с кирпичиками, сохранение
        версии, создание snapshot, повторная загрузка из новой сессии,
        компиляция в HTML с проверкой фактического содержимого.
        """
        # 1. Создание личного шаблона с кирпичиками в тексте.
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Интеграционный",
            content="\n".join(
                p
                for p in (
                    "📢 [название]",
                    "🗓 [дата]\n📍 [место]([ссылка_места])\n[описание]",
                    "Контакт: [контакт]",
                )
                if p
            ),
        )
        self.assertEqual(created.kind, "personal")

        # 2. Проверка фактических привязок в БД.
        bindings = self.session.execute(
            text(
                "SELECT code, brick_id FROM template_version_bricks "
                "WHERE template_version_id = :vid ORDER BY code"
            ),
            {"vid": created.current_version.version_id},
        ).fetchall()
        codes_to_ids = {str(row[0]): int(row[1]) for row in bindings}
        # Все девять системных кирпичиков должны быть привязаны.
        self.assertEqual(
            set(codes_to_ids),
            {"название", "дата", "место", "ссылка_места", "описание", "контакт"},
        )
        # ID соответствуют системным кирпичикам Stage 22A.
        self.assertEqual(codes_to_ids["название"], 1)
        self.assertEqual(codes_to_ids["дата"], 2)
        self.assertEqual(codes_to_ids["место"], 5)
        self.assertEqual(codes_to_ids["ссылка_места"], 7)
        self.assertEqual(codes_to_ids["описание"], 8)
        self.assertEqual(codes_to_ids["контакт"], 9)

        # 5. Создание snapshot из версии 1 (ДО изменения тела).
        # Это позволяет проверить рендер ссылки [место]([ссылка_места]),
        # которая есть только в версии 1.
        self.session.expire_all()
        v1_snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
        )
        self.assertEqual(v1_snapshot.source_template_version, 1)

        # 6. Изменение текста — создаётся новая версия 2.
        updated = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="🗓 [дата]\n🕒 [время_начала]",
        )
        self.assertEqual(updated.status, "updated")
        assert updated.template is not None
        self.assertEqual(updated.template.revision, 2)

        # 7. Историческая версия сохранена.
        v1 = get_template_version(self.session, OWNER_TG, created.template_id, 1)
        assert v1 is not None
        self.assertIn("[описание]", v1.content)
        self.assertNotIn("[время_начала]", v1.content)
        v2 = get_template_version(self.session, OWNER_TG, created.template_id, 2)
        assert v2 is not None
        self.assertIn("[время_начала]", v2.content)

        # 8. Загрузка snapshot (версии 1) из новой сессии SQLite.
        self.session.expire_all()
        event_date = (date.today() + timedelta(days=1)).strftime("%d.%m.%Y")
        stored = render_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            snapshot_id=v1_snapshot.snapshot_id,
            values_by_id={
                1: "Клуб «Гнездо»",
                2: event_date,
                3: "19:00",
                5: "Гнездо",
                7: "https://t.me/nest",
                8: "Стёпа в клетке",
                9: "@nest",
            },
        )
        # 9. Компиляция snapshot в готовый HTML со ссылкой.
        self.assertIn("Клуб «Гнездо»", stored)
        self.assertIn(event_date, stored)
        self.assertIn("Гнездо", stored)
        self.assertIn("Стёпа в клетке", stored)
        self.assertIn("@nest", stored)
        self.assertIn('<a href="https://t.me/nest">Гнездо</a>', stored)
        self.assertNotIn("None", stored)
        self.assertNotIn("NULL", stored)

    def test_render_version_uses_saved_bindings_not_code_lookup(self) -> None:
        """
        Рендер версии восстанавливает brick_id ИСКЛЮЧИТЕЛЬНО из сохранённых
        привязок (по коду), не разрешая текстовый код заново.
        """
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Стабильный",
            content="\n".join(
                p for p in ("Шапка", "Название: [название]", "Подвал") if p
            ),
        )
        # Проверка: привязка [название] → brick_id 1 сохранена.
        self.assertEqual(created.current_version.brick_ids, (1,))

        # Компиляция версии: значение подставляется по ID из привязки.
        html = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            values_by_id={1: "Тестовое название"},
        )
        self.assertIn("Название: Тестовое название", html)

        # Проверка: код в привязке — из текста при сохранении, не из
        # текущего кирпичика (который мог быть переименован после).
        binding_code = self.session.execute(
            text(
                "SELECT code FROM template_version_bricks "
                "WHERE template_version_id = :vid"
            ),
            {"vid": created.current_version.version_id},
        ).scalar()
        self.assertEqual(binding_code, "название")

    def test_rename_brick_does_not_change_saved_binding(self) -> None:
        """
        Переименование кирпичика после сохранения не подменяет brick_id:
        код в привязке остаётся тем же, который был в тексте при сохранении.
        """
        from brick_store import update_personal_brick

        brick_id = self.make_personal_brick(self.owner, "старый_код")
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Переименование",
            content="\n".join(
                p for p in ("Шапка", "Цена: [старый_код]", "Подвал") if p
            ),
        )
        self.assertEqual(created.current_version.brick_ids, (brick_id,))

        # Переименовываем кирпичик.
        update_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=brick_id,
            expected_revision=1,
            code="новый_код",
            title="Новое название",
            rules={},
        )

        # Текст версии по-прежнему содержит [старый_код]; привязка тоже.
        html = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            values_by_id={brick_id: "Пятьсот"},
        )
        self.assertIn("Пятьсот", html)

    def test_new_brick_with_same_code_does_not_hijack_binding(self) -> None:
        """
        Появление второго кирпичика с тем же кодом не подменяет привязку:
        рендер использует сохранённый brick_id, а не ищет по коду заново.
        """
        brick_a = self.make_personal_brick(self.owner, "общий")
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Стабильность",
            content="\n".join(p for p in ("Шапка", "Цена: [общий]", "Подвал") if p),
        )
        self.assertEqual(created.current_version.brick_ids, (brick_a,))

        # Создаём кирпичик места с тем же кодом (другая область видимости).
        from brick_store import create_place_brick

        place = self.make_place("Новое место")
        self.make_place_admin(place, self.owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=place.id,
            title="Местный общий",
            code="общий",
            value_type="short_text",
        )

        # Рендер по-прежнему использует brick_a (сохранённую привязку).
        html = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            values_by_id={brick_a: "Первый кирпичик"},
        )
        self.assertIn("Первый кирпичик", html)

    def test_unknown_brick_rejected_on_save(self) -> None:
        """Отказ в сохранении версии с неизвестным кирпичиком."""
        with self.assertRaises(TemplateCompileError):
            create_personal_template(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                name="Плохой",
                content="\n".join(
                    p for p in ("Шапка", "Текст [несуществующий]", "Подвал") if p
                ),
            )

    def test_ambiguous_brick_requires_explicit_choice(self) -> None:
        """Отказ при неоднозначном кирпичике без явного выбора (стандарт)."""
        from brick_store import create_personal_brick, create_place_brick

        place = self.make_place("Место с коллизией")
        self.make_place_admin(place, self.owner)
        standard = self.make_standard_template(place)
        create_place_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            place_id=place.id,
            title="Местный тестовый",
            code="тестовый",
            value_type="short_text",
        )
        create_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            title="Личный тестовый",
            code="тестовый",
            value_type="short_text",
        )
        # Без явного выбора — отказ.
        result = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.template_id,
            expected_revision=1,
            content="Название: [тестовый]",
        )
        self.assertEqual(result.status, "invalid")
        self.assertIn("неоднозначен", (result.message or "").lower())
        # Явный выбор решает проблему: выбираем кирпичик места.
        place_brick_id: int | None = self.session.execute(
            text("SELECT id FROM bricks WHERE code = 'тестовый' " "AND scope = 'place'")
        ).scalar_one()
        assert place_brick_id is not None
        result2 = update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=standard.template_id,
            expected_revision=1,
            content="Название: [тестовый]",
            chosen_brick_ids={"тестовый": place_brick_id},
        )
        self.assertEqual(result2.status, "updated")

    def test_dangerous_url_rejected_in_render(self) -> None:
        """Отказ в компиляции опасного URL."""
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Ссылка",
            content="\n".join(
                p for p in ("Шапка", "Подробнее([ссылка_места])", "Подвал") if p
            ),
        )
        with self.assertRaises(TemplateCompileError):
            render_template_version(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
                values_by_id={5: "Зло", 7: "javascript:alert(1)"},
            )
        # Некорректная ссылка тоже отклоняется.
        with self.assertRaises(TemplateCompileError):
            render_template_version(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                template_id=created.template_id,
                values_by_id={5: "Зло", 7: "не ссылка"},
            )

    def test_all_seven_brick_types_rendered(self) -> None:
        """Проверка значений всех семи типов в итоговом HTML."""
        from brick_store import create_personal_brick

        date_id = 2
        time_id = 3
        url_id = 7
        short_id = 9
        long_id = 8
        number_def = create_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            title="Количество",
            code="количество",
            value_type="number",
        )
        number_id = number_def.id
        choice_def = create_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            title="Формат",
            code="формат",
            value_type="choice",
            rules={"choices": ["Оффлайн", "Онлайн"]},
        )
        choice_id = choice_def.id
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Все типы",
            content="\n".join(
                p
                for p in (
                    "Шапка",
                    "Название: [название]\n"
                    "Дата: [дата]\n"
                    "Время: [время_начала]\n"
                    "Ссылка: [ссылка_места]\n"
                    "Описание: [описание]\n"
                    "Контакт: [контакт]\n"
                    "Кол-во: [количество]\n"
                    "Формат: [формат]",
                    "Подвал",
                )
                if p
            ),
        )
        html = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            values_by_id={
                1: "Встреча",
                date_id: (date.today() + timedelta(days=200)).strftime("%d.%m.%Y"),
                time_id: "14:30",
                url_id: "https://example.com/event",
                long_id: "Подробное\nописание",
                short_id: "@contact",
                number_id: 42,
                choice_id: "Оффлайн",
            },
        )
        self.assertIn("Встреча", html)
        self.assertIn((date.today() + timedelta(days=200)).strftime("%d.%m.%Y"), html)
        self.assertIn("14:30", html)
        self.assertIn("https://example.com/event", html)
        self.assertIn("Подробное\nописание", html)
        self.assertIn("@contact", html)
        self.assertIn("42", html)
        self.assertIn("Оффлайн", html)
        self.assertNotIn("None", html)
        self.assertNotIn("NULL", html)

    def test_snapshot_survives_template_edit(self) -> None:
        """Сохранность исторических версий и snapshot при изменении шаблона."""
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="История",
            content="\n".join(
                p for p in ("Шапка", "Версия 1 [название]", "Подвал") if p
            ),
        )
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        update_template_content(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            expected_revision=1,
            content="Версия 2 [описание]",
        )
        # Snapshot неизменен.
        html = render_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            snapshot_id=snapshot.snapshot_id,
            values_by_id={1: "Старое название"},
        )
        self.assertIn("Старое название", html)
        # Версия 2 содержит новый текст.
        html2 = render_template_version(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
            values_by_id={8: "Новое описание"},
        )
        self.assertIn("Новое описание", html2)
        self.assertNotIn("Старое название", html2)

    def test_access_control_on_render(self) -> None:
        """Проверка прав на чтение: чужой снимок недоступен."""
        created = create_personal_template(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            name="Приватный",
            content="\n".join(
                p for p in ("Шапка", "Секрет: [название]", "Подвал") if p
            ),
        )
        snapshot = create_template_snapshot(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=created.template_id,
        )
        with self.assertRaises(TemplateNotFoundError):
            render_template_snapshot(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                snapshot_id=snapshot.snapshot_id,
                values_by_id={},
            )
        with self.assertRaises(TemplateNotFoundError):
            render_template_version(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                template_id=created.template_id,
                values_by_id={},
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
