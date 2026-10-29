# tests/test_poster2_r4_ux.py
"""
R4 (Poster 2.1): UX-чистка и чат как приложение — тесты разработчика.

Проверяется требование, а не только текущая реализация:

- №24: на экранах предложений есть явный раздел и крошка, «◀️ Назад»
  возвращает на логичный предыдущий экран, тупиков нет (с каждого экрана
  можно дойти до хаба кирпичиков);
- №27: описание кирпичика в предпросмотре моноширинно (`<code>`) и
  экранировано, символы `<` и `&` не ломают экран и не исполняются;
- №28: строка хаба — «название + компактная кнопка 📨», пагинация работает
  и не теряет кнопку предложения;
- №29: карточка предложения содержит семь полей, а предложение, отправленное
  до миграции 20 (описание `NULL`), показывает «—»;
- чат как приложение: устаревший экран удаляется при навигации, ввод
  пользователя удаляется после успешной обработки, отказ Telegram на
  удаление не теряет данные и не ломает флоу, живые экраны не копятся;
- юмор: регресс — шутка не попадает ни в живой блок, ни в публикацию, ни в
  предупреждения/ошибки.

Все сценарии идут через настоящее `Application.process_update`, FakeBot и
временную SQLite (`tests.integration_harness`). Реальная `post_bot.db`,
`.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy import text as sql_text

try:
    from telegram.error import BadRequest

    from announcement_store import (
        get_announcement_draft,
        list_announcement_drafts,
    )
    from brick_proposal_store import (
        PROPOSAL_STATUS_PENDING,
        create_brick_proposal,
    )
    from brick_store import (
        create_personal_brick,
    )
    from models import (
        Brick,
        BrickProposal,
        BrickSession,
        PlaceAdmin,
        User,
        utcnow,
    )
    from place_store import create_place
    from template_store import create_personal_template
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OWNER_TG = 2_140_001
ADMIN_TG = 2_140_002
SUPER_TG = 2_140_003
OTHER_TG = 2_140_004

# Крошка раздела предложений: человек всегда видит, где он находится (№24).
CRUMB = "🧱 Кирпичики → 📨 Предложения"


def _callbacks(h: Harness) -> list[str]:
    return [data or "" for _label, data in h.screen_buttons()]


def _callback(h: Harness, prefix: str) -> str:
    found = [data for data in _callbacks(h) if data.startswith(prefix)]
    if not found:
        raise AssertionError(f"нет кнопки {prefix!r}: {h.screen_buttons()!r}")
    return found[0]


def _button(h: Harness, data: str) -> str:
    labels = [label for label, value in h.screen_buttons() if value == data]
    if not labels:
        raise AssertionError(f"нет кнопки {data!r}: {h.screen_buttons()!r}")
    return labels[0]


def _has_button(h: Harness, data: str) -> bool:
    return any(value == data for _label, value in h.screen_buttons())


def _screen_sends(h: Harness) -> list[dict]:
    """Только отправки экрана (не правки): рост значит копящиеся экраны."""
    return [call for call in h.screens() if call["type"] == "message"]


def _screen_id(h: Harness, user_id: int) -> int | None:
    """message_id текущего экрана конкретного пользователя.

    `Harness.screen_message_id()` смотрит только пользователя по умолчанию,
    поэтому для сценариев с другим Telegram ID ref берётся из user_data.
    """
    ref = h.user_data(user_id=user_id).get("screen")
    return int(ref["message_id"]) if isinstance(ref, dict) else None


class R4HarnessCase(unittest.TestCase):
    """Общие фикстуры: пользователь, место, личный кирпичик, предложение."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def ensure_user(
        self, h: Harness, telegram_user_id: int, *, username: str | None = None
    ) -> int:
        with h.session_factory() as session:
            user = get_or_create_user(
                session,
                telegram_user_id,
                username=username or f"user{telegram_user_id}",
                full_name=f"Пользователь {telegram_user_id}",
            )
            return user.id

    def make_place(self, h: Harness, name: str, *, admin_tg: int | None = None) -> int:
        with h.session_factory() as session:
            creator = None
            if admin_tg is not None:
                creator = get_or_create_user(
                    session,
                    admin_tg,
                    username=f"admin{admin_tg}",
                    full_name=f"Админ {admin_tg}",
                )
            place = create_place(
                session,
                name,
                created_by_user_id=creator.id if creator is not None else None,
            )
            if creator is not None:
                session.add(
                    PlaceAdmin(
                        place_id=place.id,
                        user_id=creator.id,
                        assigned_by_user_id=None,
                        assigned_at=utcnow(),
                    )
                )
                session.commit()
            return place.id

    def make_personal(
        self,
        h: Harness,
        owner_tg: int,
        *,
        code: str,
        title: str = "Личный кирпичик",
        value_type: str = "short_text",
        rules: dict | None = None,
        description: str | None = None,
    ):
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            return create_personal_brick(
                session,
                actor_telegram_user_id=owner_tg,
                code=code,
                title=title,
                value_type=value_type,
                rules=rules,
                description=description,
            )

    def make_proposal(
        self,
        h: Harness,
        owner_tg: int,
        source_id: int,
        *,
        scope: str = "place",
        place_id: int | None = None,
    ) -> int:
        self.ensure_user(h, owner_tg)
        with h.session_factory() as session:
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=owner_tg,
                source_brick_id=source_id,
                scope=scope,
                place_id=place_id,
            )
            return proposal.id

    def proposal_row(self, h: Harness, proposal_id: int) -> BrickProposal:
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            self.assertIsNotNone(row)
            assert row is not None
            return row

    def personal_brick_count(self, h: Harness, owner_tg: int) -> int:
        """Личные кирпичики владельца (системные из миграции не считаются)."""
        with h.session_factory() as session:
            user = session.scalar(select(User).where(User.telegram_user_id == owner_tg))
            if user is None:
                return 0
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(Brick)
                    .where(Brick.owner_user_id == user.id)
                )
                or 0
            )

    # --- навигация --------------------------------------------------------

    async def open_hub(self, h: Harness, user_id: int = OWNER_TG) -> None:
        await h.text("/bricks", user_id=user_id)

    async def walk_to_preview(
        self, h: Harness, source_id: int, place_id: int, user_id: int = OWNER_TG
    ) -> None:
        await self.open_hub(h, user_id)
        await h.callback("bp_start", user_id=user_id)
        await h.callback(f"bp_source:{source_id}", user_id=user_id)
        await h.callback("bp_target:place", user_id=user_id)
        await h.callback(f"bp_place:{place_id}", user_id=user_id)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ProposalNavigationTests(R4HarnessCase):
    """№24: раздел, шаг, «Назад» и отсутствие тупиков."""

    @scenario_test
    async def test_every_wizard_screen_has_crumb_and_step(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал крошек")
        source = self.make_personal(h, OWNER_TG, code="крошка_24", title="Крошка")
        await self.open_hub(h)
        await h.callback("bp_start", user_id=OWNER_TG)

        self.assertIn(CRUMB, h.screen_text(), "шаг 1 без крошки раздела")
        self.assertIn("шаг 1 из 4", h.screen_text())
        self.assertIn("Выберите кирпичик", h.screen_text())

        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("шаг 2 из 4", h.screen_text())

        await h.callback("bp_target:place", user_id=OWNER_TG)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("шаг 3 из 4", h.screen_text())

        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("шаг 4 из 4", h.screen_text())

    @scenario_test
    async def test_list_and_card_have_crumb_without_step(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал истории")
        source = self.make_personal(h, OWNER_TG, code="история_24")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Мои предложения", h.screen_text())
        self.assertNotIn("шаг", h.screen_text(), "у списка нет шага мастера")

        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Предложение кирпичика", h.screen_text())
        self.assertNotIn("шаг", h.screen_text())

    @scenario_test
    async def test_back_walks_whole_wizard_and_returns_to_hub(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал возврата")
        source = self.make_personal(h, OWNER_TG, code="возврат_24")
        await self.walk_to_preview(h, source.id, place_id)
        self.assertIn("шаг 4 из 4", h.screen_text())

        # Шаг 4 → шаг 3.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("шаг 3 из 4", h.screen_text())
        self.assertTrue(_has_button(h, f"bp_place:{place_id}"))

        # Шаг 3 → шаг 2.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("шаг 2 из 4", h.screen_text())
        self.assertTrue(_has_button(h, "bp_target:place"))

        # Шаг 2 → шаг 1.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("шаг 1 из 4", h.screen_text())
        self.assertTrue(_has_button(h, f"bp_source:{source.id}"))

        # Шаг 1 → хаб: тупика нет.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertTrue(_has_button(h, "bk_create"))

    @scenario_test
    async def test_back_from_scope_after_row_entry_goes_to_hub(
        self, h: Harness
    ) -> None:
        """Вход из строки хаба пропускает шаг 1 — «назад» не должен возвращать
        на экран повторного выбора того же кирпичика."""
        source = self.make_personal(h, OWNER_TG, code="строкой_24", title="Строкой")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)
        self.assertIn("шаг 2 из 4", h.screen_text())
        back_label = _button(h, "bp_back")
        self.assertIn("кирпичикам", back_label.lower())

        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои кирпичики", h.screen_text())

    @scenario_test
    async def test_back_from_card_and_withdraw_confirm_reaches_hub(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал карточки")
        source = self.make_personal(h, OWNER_TG, code="карточка_24")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        await h.callback("bp_withdraw", user_id=OWNER_TG)

        self.assertIn("Отозвать предложение?", h.screen_text())
        self.assertIn(CRUMB, h.screen_text())
        # «Назад» из подтверждения — обратно в карточку, а не в список.
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Предложение кирпичика", h.screen_text())
        self.assertTrue(_has_button(h, "bp_withdraw"))

        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои предложения", h.screen_text())

        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertIn("Мои кирпичики", h.screen_text())

    @scenario_test
    async def test_every_proposal_screen_offers_a_back_button(self, h: Harness) -> None:
        """Ни один экран флоу не остаётся без выхода (проверка «нет тупиков»)."""
        place_id = self.make_place(h, "Зал кнопок")
        source = self.make_personal(h, OWNER_TG, code="кнопки_24")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)

        await self.open_hub(h)
        await h.callback("bp_start", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "шаг 1 без «назад»")
        await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "шаг 2 без «назад»")
        await h.callback("bp_target:place", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "шаг 3 без «назад»")
        await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "шаг 4 без «назад»")
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "карточка без «назад»")
        await h.callback("bp_back", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "список без «назад»")
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "карточка из списка без «назад»")
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, "bp_back"), "подтверждение без «назад»")

    @scenario_test
    async def test_short_labels_keep_actions(self, h: Harness) -> None:
        """Короткие подписи не отнимают функциональность (создание/просмотр/отзыв)."""
        place_id = self.make_place(h, "Зал подписей")
        source = self.make_personal(h, OWNER_TG, code="подписи_24", title="Подписи")
        await self.walk_to_preview(h, source.id, place_id)

        labels = " ".join(label for label, _data in h.screen_buttons())
        for label in labels.split():
            self.assertLessEqual(len(label), 30, f"длинная подпись: {label!r}")

        # Отправка работает и остаётся доступной короткой кнопкой.
        self.assertTrue(_has_button(h, "bp_send"))
        await h.callback("bp_send", user_id=OWNER_TG)
        self.assertIn("Предложение отправлено", h.screen_text())

        # Отзыв доступен с карточки (короткая подпись, то же действие).
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        self.assertIn("Отозвать предложение?", h.screen_text())
        await h.callback("bp_withdraw_yes", user_id=OWNER_TG)
        with h.session_factory() as session:
            proposal = session.scalar(select(BrickProposal))
            assert proposal is not None
            self.assertEqual(proposal.status, "withdrawn")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DescriptionFormattingTests(R4HarnessCase):
    """№27 и №29: `<code>`, экранирование и семь полей карточки."""

    @scenario_test
    async def test_brick_preview_wraps_description_in_code_and_escapes(
        self, h: Harness
    ) -> None:
        await self.open_hub(h)
        await h.callback("bk_create", user_id=OWNER_TG)
        await h.text("Ведущий", user_id=OWNER_TG)
        await h.callback("bk_accept_code", user_id=OWNER_TG)
        await h.text("<b>Имя</b> & 'без должности'", user_id=OWNER_TG)
        await h.callback("bk_type:short_text", user_id=OWNER_TG)
        # Экран предпросмотра вызывается одноимённой кнопкой сохранения.
        self.assertIn("Проверьте кирпичик", h.screen_text())
        text = h.screen_text()
        self.assertIn(
            "Описание: <code>&lt;b&gt;Имя&lt;/b&gt; &amp; 'без должности'</code>",
            text,
            "описание не моноширинно или не экранировано",
        )
        self.assertNotIn("<b>Имя</b>", text, "HTML из описания не должен исполняться")

    @scenario_test
    async def test_question_screen_wraps_description_in_code(self, h: Harness) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, OWNER_TG, username="r4-desc")
            create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code="ttl",
                title="Ведущий",
                value_type="short_text",
                rules={},
                description="A < B & C",
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Описание",
                header="",
                body="[ttl]",
                footer="",
            )
            template_id = template.template_id
        await h.text("/start", user_id=OWNER_TG)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER_TG)
        text = h.screen_text()
        self.assertIn("📝 <code>A &lt; B &amp; C</code>", text)
        self.assertNotIn("A < B", text)

    @scenario_test
    async def test_own_card_has_seven_fields(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал семи")
        source = self.make_personal(
            h,
            OWNER_TG,
            code="семь_29",
            title="Семь полей",
            description="Инструкция <автору> & всем",
        )
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        text = h.screen_text()

        self.assertIn("Название: <b>Семь полей</b>", text)
        self.assertIn("Обозначение: <code>[семь_29]</code>", text)
        self.assertIn("Тип: Короткий текст", text)
        self.assertIn(
            "Описание: <code>Инструкция &lt;автору&gt; &amp; всем</code>", text
        )
        self.assertIn("Автор:", text)
        self.assertIn("Цель: место «Зал семи»", text)
        self.assertIn("Статус: ⏳ Ожидает решения", text)
        self.assertNotIn("<автору>", text, "описание не экранировано")

    @scenario_test
    async def test_admin_card_has_seven_fields(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал админа", admin_tg=ADMIN_TG)
        source = self.make_personal(
            h,
            OWNER_TG,
            code="админ_29",
            title="Админ-карточка",
            description="Кто & как",
        )
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await h.text("/admin", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=ADMIN_TG)
        text = h.screen_text()

        self.assertIn("Название: <b>Админ-карточка</b>", text)
        self.assertIn("Обозначение: <code>[админ_29]</code>", text)
        self.assertIn("Тип: Короткий текст", text)
        self.assertIn("Описание: <code>Кто &amp; как</code>", text)
        self.assertIn("Автор:", text)
        self.assertIn("Цель: место «Зал админа»", text)
        self.assertIn("Статус:", text)

    @scenario_test
    async def test_old_proposal_without_description_shows_dash(
        self, h: Harness
    ) -> None:
        """Предложение до миграции 20: `description IS NULL` → «—», не пусто."""
        place_id = self.make_place(h, "Зал миграции", admin_tg=ADMIN_TG)
        source = self.make_personal(h, OWNER_TG, code="старое_29", title="Старое")
        self.ensure_user(h, OWNER_TG)
        with h.session_factory() as session:
            author = get_or_create_user(session, OWNER_TG, username="legacy")
            brick = session.get(Brick, source.id)
            assert brick is not None
            # Прямой INSERT: триггер неизменяемости снимка запрещает менять
            # description уже после отправки — поэтому legacy-строка создаётся
            # сразу без описания, как это было до миграции 20.
            session.execute(
                sql_text(
                    "INSERT INTO brick_proposals ("
                    "author_user_id, scope, place_id, source_brick_id, title, "
                    "code, type, source, rules_json, description, status, "
                    "created_at) VALUES ("
                    ":author, 'place', :place, :brick, :title, :code, :type, "
                    "'place', :rules, NULL, 'pending', :created)"
                ),
                {
                    "author": author.id,
                    "place": place_id,
                    "brick": source.id,
                    "title": brick.title,
                    "code": brick.code,
                    "type": brick.value_type,
                    "rules": brick.rules_json,
                    "created": "2026-10-01 10:00:00",
                },
            )
            session.commit()
            proposal_id = int(
                session.scalar(sql_text("SELECT last_insert_rowid()")) or 0
            )
        self.assertGreater(proposal_id, 0)

        # Автор видит «—».
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        self.assertIn("Описание: —", h.screen_text())
        self.assertNotIn("Описание: \n", h.screen_text())

        # Администратор видит то же самое.
        await h.text("/admin", user_id=ADMIN_TG)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=ADMIN_TG)
        self.assertIn("Описание: —", h.screen_text())

    @scenario_test
    async def test_pending_without_description_stays_readable(self, h: Harness) -> None:
        """Старые данные не теряются: остальные поля карточки на месте."""
        place_id = self.make_place(h, "Зал старых")
        source = self.make_personal(h, OWNER_TG, code="старое_2", title="Старое 2")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            self.assertIsNone(row.description)
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)
            self.assertEqual(row.title, "Старое 2")


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HubProposalButtonTests(R4HarnessCase):
    """№28: строка = название + компактная «📨», пагинация."""

    @scenario_test
    async def test_row_has_compact_offer_button(self, h: Harness) -> None:
        source = self.make_personal(h, OWNER_TG, code="строка_28", title="Строка")
        await self.open_hub(h)
        self.assertTrue(_has_button(h, f"bp_start:{source.id}"))
        self.assertTrue(_has_button(h, f"bk_open:{source.id}"))
        # Компактная подпись: только значок, без слов.
        compact = _button(h, f"bp_start:{source.id}")
        self.assertEqual(compact, "📨")
        # Строка названия остаётся отдельной кнопкой открытия карточки.
        self.assertIn("Строка", _button(h, f"bk_open:{source.id}"))

    @scenario_test
    async def test_row_button_skips_source_step(self, h: Harness) -> None:
        source = self.make_personal(h, OWNER_TG, code="пропуск_28", title="Пропуск")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER_TG)
        self.assertIn("Куда предложить кирпичик?", h.screen_text())
        self.assertNotIn(f"bp_source:{source.id}", _callbacks(h))

    @scenario_test
    async def test_pagination_keeps_offer_button_on_every_page(
        self, h: Harness
    ) -> None:
        created = []
        for index in range(9):
            created.append(
                self.make_personal(
                    h,
                    OWNER_TG,
                    code=f"стр_{index:02d}",
                    title=f"Кирпичик {index:02d}",
                )
            )
        await self.open_hub(h)
        first_page = [data for data in _callbacks(h) if data.startswith("bp_start:")]
        self.assertEqual(len(first_page), 8, "на странице 8 строк")
        self.assertTrue(_has_button(h, "bk_page:8"), "нет кнопки следующей страницы")

        await h.callback("bk_page:8", user_id=OWNER_TG)
        second_page = [data for data in _callbacks(h) if data.startswith("bp_start:")]
        self.assertEqual(len(second_page), 1)
        self.assertEqual(second_page[0], f"bp_start:{created[8].id}")

    @scenario_test
    async def test_archived_row_has_no_offer_button(self, h: Harness) -> None:
        source = self.make_personal(h, OWNER_TG, code="архив_28", title="Архивный")
        with h.session_factory() as session:
            from brick_store import archive_personal_brick

            result = archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                brick_id=source.id,
                expected_revision=source.revision,
            )
            self.assertEqual(result.status, "archived")
        await self.open_hub(h)
        await h.callback("bk_archlist", user_id=OWNER_TG)
        self.assertTrue(_has_button(h, f"bk_open:{source.id}"))
        self.assertFalse(
            _has_button(h, f"bp_start:{source.id}"),
            "архивный кирпичик предложить нельзя — кнопки быть не должно",
        )


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ChatAsApplicationTests(R4HarnessCase):
    """Чат как приложение: один экран, уборка ввода, устойчивость к отказам."""

    @scenario_test
    async def test_repeated_navigation_does_not_pile_up_screens(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал навигации")
        source = self.make_personal(h, OWNER_TG, code="навигация_4", title="Навигация")
        await self.open_hub(h)
        sends_after_hub = len(_screen_sends(h))
        for _ in range(5):
            await h.callback("bp_start", user_id=OWNER_TG)
            await h.callback(f"bp_source:{source.id}", user_id=OWNER_TG)
            await h.callback("bp_target:place", user_id=OWNER_TG)
            await h.callback(f"bp_place:{place_id}", user_id=OWNER_TG)
            await h.callback("bp_back", user_id=OWNER_TG)
            await h.callback("bp_back", user_id=OWNER_TG)
            await h.callback("bp_back", user_id=OWNER_TG)
            await h.callback("bp_back", user_id=OWNER_TG)
            await h.callback("bp_mine", user_id=OWNER_TG)
            await h.callback("bp_back", user_id=OWNER_TG)
        self.assertEqual(
            len(_screen_sends(h)),
            sends_after_hub,
            "служебный экран должен перерисовываться, а не добавляться",
        )
        self.assertGreater(len(h.edits()), 40)

    @scenario_test
    async def test_stale_screen_is_deleted_on_message_navigation(
        self, h: Harness
    ) -> None:
        await self.open_hub(h)
        old_screen = _screen_id(h, OWNER_TG)
        self.assertIsNotNone(old_screen)
        # Пользовательское сообщение ниже экрана: старое сообщение удаляется,
        # экран пересоздаётся рядом с вводом, а не копится.
        await h.text_from_below("/bricks", user_id=OWNER_TG)
        self.assertIn(old_screen, h.deleted_ids())
        new_screen = _screen_id(h, OWNER_TG)
        self.assertIsNotNone(new_screen)
        self.assertNotEqual(new_screen, old_screen)

    @scenario_test
    async def test_user_input_is_deleted_after_success(self, h: Harness) -> None:
        await self.open_hub(h)
        await h.callback("bk_create", user_id=OWNER_TG)
        update = await h.text("Кирпич навигации", user_id=OWNER_TG)
        assert update.message is not None
        user_message_id = update.message.message_id
        self.assertIn(user_message_id, h.deleted_ids())
        self.assertIn("Техническое обозначение", h.screen_text())

    @scenario_test
    async def test_stray_text_on_screen_phase_keeps_the_screen(
        self, h: Harness
    ) -> None:
        """Свободный текст на экранной фазе не должен оставлять раздел без кнопок.

        Раньше такое сообщение уходило глобальной подсказке главного меню:
        экран карточки затирался строкой «Воспользуйтесь кнопками меню», и
        человек оказывался в тупике — ни «Назад», ни «Изменить».
        """
        source = self.make_personal(h, OWNER_TG, code="мусор_4", title="Мусор")
        await self.open_hub(h)
        await h.callback(f"bk_open:{source.id}", user_id=OWNER_TG)
        update = await h.text("текст в экранной фазе", user_id=OWNER_TG)
        assert update.message is not None

        self.assertNotIn(
            "Воспользуйтесь кнопками меню",
            h.screen_text(),
            "экран раздела затёрт подсказкой главного меню",
        )
        # Кнопки карточки на месте: из экрана по-прежнему есть выход.
        self.assertTrue(_has_button(h, "bk_back"))
        self.assertTrue(_has_button(h, "bk_edit"))
        # Сообщение пользователя убрано best-effort.
        self.assertIn(update.message.message_id, h.deleted_ids())

    @scenario_test
    async def test_stray_photo_on_screen_phase_keeps_the_screen(
        self, h: Harness
    ) -> None:
        """Фото вне шага «Фотографии» — тот же тупик, что и текст."""
        source = self.make_personal(h, OWNER_TG, code="фото_4", title="Фото")
        await self.open_hub(h)
        await h.callback(f"bk_open:{source.id}", user_id=OWNER_TG)
        await h.photo("stray-photo", user_id=OWNER_TG)

        self.assertNotIn("Фото добавляются в анонсе", h.screen_text())
        self.assertTrue(_has_button(h, "bk_back"), "карточка потеряла выход")
        self.assertTrue(_has_button(h, "bk_edit"))

    @scenario_test
    async def test_stray_text_on_proposal_screen_keeps_back_button(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал мусора")
        source = self.make_personal(h, OWNER_TG, code="мусор_24", title="Мусор")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        await h.text("текст на карточке предложения", user_id=OWNER_TG)

        self.assertIn(CRUMB, h.screen_text(), "раздел подменён подсказкой меню")
        self.assertTrue(_has_button(h, "bp_back"))
        self.assertTrue(_has_button(h, "bp_withdraw"))
        # Данные не тронуты: предложение всё ещё ожидает решения.
        self.assertEqual(self.proposal_row(h, proposal_id).status, "pending")

    @scenario_test
    async def test_failed_delete_keeps_data_and_flow(self, h: Harness) -> None:
        """Отказ Telegram на удаление: данные целы, флоу продолжается."""
        place_id = self.make_place(h, "Зал отказа")
        source = self.make_personal(h, OWNER_TG, code="отказ_4", title="Отказ")
        proposal_id = self.make_proposal(h, OWNER_TG, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)

        # Telegram «не даёт» удалять: любой delete_message падает.
        h.bot.errors_by_type["delete_message"] = BadRequest(
            "Bad Request: message can't be deleted"
        )
        await h.text_from_below("/bricks", user_id=OWNER_TG)
        # Экран перерисован (это тот же раздел, но новое сообщение), а данные
        # не тронуты: карточка предложения открыта как была.
        self.assertIn("Предложение кирпичика", h.screen_text())
        self.assertEqual(self.proposal_row(h, proposal_id).status, "pending")

        # Флоу продолжается: карточка открывается и отзыв работает.
        await h.callback("bp_mine", user_id=OWNER_TG)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER_TG)
        self.assertIn("Отказ", h.screen_text())
        await h.callback("bp_withdraw", user_id=OWNER_TG)
        await h.callback("bp_withdraw_yes", user_id=OWNER_TG)
        self.assertEqual(self.proposal_row(h, proposal_id).status, "withdrawn")
        self.assertEqual(
            self.personal_brick_count(h, OWNER_TG), 1, "кирпичик не потерян"
        )

    @scenario_test
    async def test_failed_delete_does_not_block_input_processing(
        self, h: Harness
    ) -> None:
        """Ввод принят и сохранён даже если удалить сообщение не удалось."""
        h.bot.errors_by_type["delete_message"] = BadRequest("message can't be deleted")
        await self.open_hub(h)
        await h.callback("bk_create", user_id=OWNER_TG)
        await h.text("Упорный кирпич", user_id=OWNER_TG)
        self.assertIn("Техническое обозначение", h.screen_text())
        with h.session_factory() as session:
            row = session.get(BrickSession, OWNER_TG)
            self.assertIsNotNone(row, "черновик не сохранён при отказе удаления")
            assert row is not None
            self.assertIn("Упорный кирпич", row.payload)

    @scenario_test
    async def test_live_block_above_and_one_service_screen(self, h: Harness) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, OWNER_TG, username="r4-live")
            create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code="ttl",
                title="Ведущий",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Порядок",
                header="",
                body="[ttl]",
                footer="",
            )
            template_id = template.template_id
        await h.text("/start", user_id=OWNER_TG)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER_TG)
        screen_id = _screen_id(h, OWNER_TG)
        assert screen_id is not None, "экран меню не отправлен"
        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER_TG)
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=drafts[0].draft_id,
            )
        self.assertTrue(details.live_message_ids, "живой блок не отправлен")
        self.assertLess(
            int(details.live_message_ids[0]),
            int(screen_id),
            "живой блок анонса должен быть выше служебного экрана",
        )
        # Служебный экран один: он отправлен, а не добавлен поверх другого.
        self.assertEqual(len(_screen_sends(h)), 2, "ровно два экрана: меню и вопрос")
        self.assertIn("Вопрос 1 из 1", h.screen_text())

    @scenario_test
    async def test_service_screen_is_only_one_after_multi_step_answers(
        self, h: Harness
    ) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, OWNER_TG, username="r4-live2")
            for code, title, kind in (
                ("aa", "Первый", "short_text"),
                ("bb", "Второй", "short_text"),
            ):
                create_personal_brick(
                    session,
                    actor_telegram_user_id=OWNER_TG,
                    code=code,
                    title=title,
                    value_type=kind,
                    rules={},
                )
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Порядок 2",
                header="",
                body="[aa]\n[bb]",
                footer="",
            )
            template_id = template.template_id
        await h.text("/start", user_id=OWNER_TG)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER_TG)
        screens_before = len(_screen_sends(h))
        await h.text("Ответ", user_id=OWNER_TG)
        self.assertEqual(len(_screen_sends(h)), screens_before)
        self.assertIn("Вопрос 2 из 2", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class HumorRegressionTests(R4HarnessCase):
    """Юмор: регресс — не в публикации, не в предупреждениях."""

    @scenario_test
    async def test_no_joke_in_publication_or_warnings(self, h: Harness) -> None:
        with h.session_factory() as session:
            get_or_create_user(session, OWNER_TG, username="r4-humor")
            create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code="ttl",
                title="Ведущий",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER_TG,
                name="Юмор",
                header="",
                body="[ttl]",
                footer="",
            )
            template_id = template.template_id

        with patch("utils.ui_humor.pick_variant", return_value=ui_humor.HUMOR_JOKES[0]):
            await h.text("/start", user_id=OWNER_TG)
            await h.callback(f"tp_pick:{template_id}", user_id=OWNER_TG)
            await h.callback(_callback(h, "aw_qskip"), user_id=OWNER_TG)
            await h.callback(_callback(h, "aw_photo_none"), user_id=OWNER_TG)

        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER_TG)
            draft = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=drafts[0].draft_id,
            )
        publication = draft.publication_text or ""
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, publication, "шутка попала в публикацию")
            for post in h.posts():
                self.assertNotIn(joke, post or "", "шутка попала в отправленный текст")

    @scenario_test
    async def test_warning_screen_never_jokes(self, h: Harness) -> None:
        """Предупреждение (занятый код) не шутит даже при выпавшей шутке."""
        place_id = self.make_place(h, "Зал предупреждений", admin_tg=ADMIN_TG)
        with h.session_factory() as session:
            get_or_create_user(session, SUPER_TG, username="super")
            from brick_store import create_place_brick

            create_place_brick(
                session,
                actor_telegram_user_id=SUPER_TG,
                place_id=place_id,
                code="занят_4",
                title="Занят",
                value_type="short_text",
            )
        source = self.make_personal(h, OWNER_TG, code="занят_4", title="Дубль")
        with patch("utils.ui_humor.pick_variant", return_value=ui_humor.HUMOR_JOKES[0]):
            await self.walk_to_preview(h, source.id, place_id)
        text = h.screen_text()
        self.assertIn("Отправить сейчас нельзя", text)
        for joke in ui_humor.HUMOR_JOKES:
            self.assertNotIn(joke, text, "шутка не должна попадать в предупреждение")


if __name__ == "__main__":
    unittest.main()
