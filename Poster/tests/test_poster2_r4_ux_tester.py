# tests/test_poster2_r4_ux_tester.py
"""
R4 (Poster 2.1): независимые проверки тестировщика — по требованиям владельца,
без опоры на реализацию и тесты разработчика.

Проверяется:

1. №24: полный флоу предложения — из строки хаба и общим входом; на каждом
   экране раздел/крошка, рабочая «назад», нет тупиков; отзыв работает.
2. №27: описание с `<`, `>`, `&` — внутри `<code>`, экранировано.
3. №28: у каждой строки хаба компактная кнопка предложения; пагинация.
4. №29: карточка предложения — 7 полей; старая запись с `description = NULL`
   показывает «—»; одобрение переносит описание в созданный кирпичик.
5. Чат как приложение: экраны не копятся, ввод удаляется, отказ Telegram на
   удаление не теряет данные, живой блок выше экрана, рестарт без дублей,
   альбом не дробит порядок.
6. Юмор: ≈1 %, детерминированный, отключаемый, не попадает в публикацию и
   предупреждения.
7. Негатив: forged callbacks (`bp_start` с чужим/несуществующим id,
   устаревшие кнопки предложений), IDOR на карточке предложения.

Только FakeBot + временная SQLite (`tests/integration_harness`).
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import select
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
        withdraw_brick_proposal,
    )
    from brick_store import (
        archive_personal_brick,
        create_personal_brick,
    )
    from models import Brick, BrickProposal, BrickSession, PlaceAdmin, utcnow
    from place_store import create_place
    from template_store import create_personal_template
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user
    from utils import ui_humor

    IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина уходит в skip-сообщение
    IMPORT_ERROR = str(exc)


OWNER = 2_150_001
ADMIN = 2_150_002
SUPER = 2_150_003
STRANGER = 2_150_004

CRUMB = "🧱 Кирпичики → 📨 Предложения"
PROPOSAL_FIELDS = (
    "Название:",
    "Обозначение:",
    "Тип:",
    "Описание:",
    "Автор:",
    "Цель:",
    "Статус:",
)


def callbacks(h: Harness) -> list[str]:
    return [data or "" for _label, data in h.screen_buttons()]


def has(h: Harness, data: str) -> bool:
    return data in callbacks(h)


def find(h: Harness, prefix: str) -> str:
    for data in callbacks(h):
        if data.startswith(prefix):
            return data
    raise AssertionError(f"нет кнопки {prefix!r}: {h.screen_buttons()!r}")


def label_of(h: Harness, data: str) -> str:
    for label, value in h.screen_buttons():
        if value == data:
            return label
    raise AssertionError(f"нет кнопки {data!r}: {h.screen_buttons()!r}")


def screen_sends(h: Harness) -> list[dict]:
    return [call for call in h.screens() if call["type"] == "message"]


def record_text(record: dict) -> str:
    """Текст записи FakeBot: kwargs["text"] либо первый позиционный аргумент."""
    text = record.get("text")
    if text is not None:
        return str(text)
    args = record.get("args") or ()
    return str(args[0]) if args else ""


def live_block_texts(h: Harness, live_ids: Any) -> list[str]:
    """
    Текущий текст живого блока по сохранённым в черновике message_id.

    Записи send_message/send_photo в FakeBot НЕ содержат message_id в
    kwargs: id выдаётся в `_make_message` и живёт только в возвращённом
    Message (и в `_message_content`). Поэтому фильтр «record.message_id in
    live_ids» ловит лишь edit'ы, а живой блок без правки (например, пустой
    анонс «.») выглядел бы отсутствующим.
    """
    wanted = {int(mid) for mid in live_ids}
    store = getattr(h.bot, "_message_content", {}) or {}
    texts = [
        str(text)
        for (_chat_id, message_id), content in store.items()
        if int(message_id) in wanted and content[1] is not None
        for text in (content[1],)
    ]
    if texts:
        return texts
    # Запасной путь: id сообщений бота растут от 100 в порядке send-вызовов.
    sends = [
        call for call in h.calls if call["type"] in ("message", "photo", "media_group")
    ]
    return [
        str(call.get("text") or call.get("caption") or "")
        for index, call in enumerate(sends, start=1)
        if 100 + index in wanted
    ]


def screen_id(h: Harness, user_id: int = OWNER) -> int | None:
    ref = h.user_data(user_id=user_id).get("screen")
    return int(ref["message_id"]) if isinstance(ref, dict) else None


def alert_texts(h: Harness) -> list[str]:
    """Тексты ответов callback'ам (show_alert и обычные)."""
    out: list[str] = []
    for call in h.answers():
        args = call.get("args") or ()
        if args:
            out.append(str(args[0]))
        elif call.get("text"):
            out.append(str(call["text"]))
    return out


@unittest.skipIf(
    IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {IMPORT_ERROR}",
)
class R4TesterCase(unittest.TestCase):
    """Фикстуры: пользователи, место, кирпичики, предложения."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    # --- данные -----------------------------------------------------------

    def ensure_user(self, h: Harness, tg: int) -> int:
        with h.session_factory() as session:
            return get_or_create_user(
                session, tg, username=f"u{tg}", full_name=f"Пользователь {tg}"
            ).id

    def make_place(self, h: Harness, name: str, *, admin_tg: int | None = None) -> int:
        with h.session_factory() as session:
            creator = None
            if admin_tg is not None:
                creator = get_or_create_user(
                    session, admin_tg, username=f"adm{admin_tg}", full_name="Админ"
                )
            place = create_place(
                session,
                name,
                city="Тестовый город",
                created_by_user_id=creator.id if creator else None,
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
            return int(place.id)

    def make_personal(
        self,
        h: Harness,
        owner_tg: int,
        *,
        code: str,
        title: str,
        value_type: str = "short_text",
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
                rules={},
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
            return int(
                create_brick_proposal(
                    session,
                    actor_telegram_user_id=owner_tg,
                    source_brick_id=source_id,
                    scope=scope,
                    place_id=place_id,
                ).id
            )

    def seed_announcement(self, h: Harness, *, body: str, code: str = "ttl") -> int:
        with h.session_factory() as session:
            get_or_create_user(session, OWNER, username="owner", full_name="Владелец")
            create_personal_brick(
                session,
                actor_telegram_user_id=OWNER,
                code=code,
                title="Ведущий",
                value_type="short_text",
                rules={},
            )
            template = create_personal_template(
                session,
                actor_telegram_user_id=OWNER,
                name="Шаблон",
                content="\n".join(p for p in ("", body, "") if p),
            )
            return int(template.template_id)

    # --- навигация --------------------------------------------------------

    async def open_hub(self, h: Harness, user_id: int = OWNER) -> None:
        await h.text("/bricks", user_id=user_id)

    async def walk(self, h: Harness, source_id: int, place_id: int) -> None:
        # U3a №8: предложение стартует только из строки кирпичика.
        await self.open_hub(h)
        await h.callback(f"bp_start:{source_id}", user_id=OWNER)
        await h.callback("bp_target:place", user_id=OWNER)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER)


class ProposalFlowTests(R4TesterCase):
    """№24: раздел, крошка, рабочая «назад», отсутствие тупиков."""

    @scenario_test
    async def test_full_flow_from_hub_general_entry(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал потока")
        source = self.make_personal(h, OWNER, code="поток_1", title="Поток")
        await self.open_hub(h)

        await h.callback(f"bp_start:{source.id}", user_id=OWNER)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Куда предложить", h.screen_text())
        self.assertIn("Поток", h.screen_text())
        self.assertTrue(has(h, "bp_back"))

        await h.callback("bp_target:place", user_id=OWNER)
        self.assertIn(CRUMB, h.screen_text())
        self.assertTrue(has(h, "bp_back"))

        await h.callback(f"bp_place:{place_id}", user_id=OWNER)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Проверьте предложение", h.screen_text())
        self.assertTrue(has(h, "bp_send"))
        self.assertTrue(has(h, "bp_back"))

        await h.callback("bp_send", user_id=OWNER)
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Предложение кирпичика", h.screen_text())
        self.assertTrue(has(h, "bp_withdraw"))
        self.assertTrue(has(h, "bp_back"))

        # Выход из любого экрана: карточка → список → хаб.
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои предложения", h.screen_text())
        self.assertIn(CRUMB, h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои кирпичики", h.screen_text())
        self.assertIn("bk_create", callbacks(h))

    @scenario_test
    async def test_flow_from_row_button_and_back_returns_to_hub_without_loop(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал строки")
        source = self.make_personal(h, OWNER, code="строкой_1", title="Строкой")
        await self.open_hub(h)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER)
        # Шаг выбора кирпичика пропущен: сразу область, крошка на месте.
        self.assertNotIn("Выберите кирпичик", h.screen_text())
        self.assertIn(CRUMB, h.screen_text())
        self.assertIn("Куда предложить", h.screen_text())

        # «Назад» из области → хаб, а не повторный выбор того же кирпичика.
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои кирпичики", h.screen_text())

        # Полный проход без цикла: строка → место → предпросмотр → отправка.
        await h.callback(f"bp_start:{source.id}", user_id=OWNER)
        await h.callback("bp_target:place", user_id=OWNER)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER)
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Выберите место", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Куда предложить", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои кирпичики", h.screen_text())

    @scenario_test
    async def test_withdraw_reaches_withdrawn_and_keeps_source_brick(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал отзыва")
        source = self.make_personal(h, OWNER, code="отзыв_1", title="Отзыв")
        await self.walk(h, source.id, place_id)
        await h.callback("bp_send", user_id=OWNER)
        await h.callback("bp_withdraw", user_id=OWNER)
        self.assertIn("Отозвать предложение?", h.screen_text())
        self.assertTrue(has(h, "bp_withdraw_yes"))
        await h.callback("bp_withdraw_yes", user_id=OWNER)
        self.assertIn("Отозвано", h.screen_text())
        with h.session_factory() as session:
            row = session.scalar(select(BrickProposal))
            assert row is not None
            self.assertEqual(row.status, "withdrawn")
            brick = session.get(Brick, source.id)
            assert brick is not None
            self.assertEqual(brick.status, "active", "личный кирпичик не тронут")

    @scenario_test
    async def test_proposal_list_pages_and_back(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал списка")
        source = self.make_personal(h, OWNER, code="список_1", title="Список")
        for _ in range(9):
            self.make_proposal(h, OWNER, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER)
        self.assertIn(CRUMB, h.screen_text())
        first = [data for data in callbacks(h) if data.startswith("bp_open:")]
        self.assertEqual(len(first), 8)
        self.assertTrue(has(h, "bp_mpage:8"), "нет перехода на 2-ю страницу")

        await h.callback("bp_mpage:8", user_id=OWNER)
        second = [data for data in callbacks(h) if data.startswith("bp_open:")]
        self.assertEqual(len(second), 1)
        self.assertTrue(has(h, "bp_mpage:0"), "нет возврата на 1-ю страницу")
        self.assertTrue(has(h, "bp_back"))
        # Из второй страницы карточка открывается и «назад» возвращает.
        await h.callback(second[0], user_id=OWNER)
        self.assertIn("Предложение кирпичика", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои предложения", h.screen_text())
        await h.callback("bp_back", user_id=OWNER)
        self.assertIn("Мои кирпичики", h.screen_text())


class DescriptionEscapingTests(R4TesterCase):
    """№27: моноширинное экранированное описание."""

    @scenario_test
    async def test_brick_card_and_preview_escape_description(self, h: Harness) -> None:
        raw = "<script>alert(1)</script> & 'x' <b>"
        place_id = self.make_place(h, "Зал экранирования")
        source = self.make_personal(
            h,
            OWNER,
            code="escape_1",
            title="Экранирование",
            description=raw,
        )
        await self.open_hub(h)
        await h.callback(f"bk_open:{source.id}", user_id=OWNER)
        card = h.screen_text()
        self.assertIn("<code>", card, "описание в карточке не в <code>")
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; &amp; 'x'", card)
        self.assertNotIn("<script>", card, "сырой HTML из описания")
        self.assertNotIn("<b>", card.split("Описание:")[1].split("\n")[0])

        await h.callback("bk_back", user_id=OWNER)
        await h.callback(f"bp_start:{source.id}", user_id=OWNER)
        await h.callback("bp_target:place", user_id=OWNER)
        await h.callback(f"bp_place:{place_id}", user_id=OWNER)
        preview = h.screen_text()
        self.assertIn("Описание: <code>", preview, "предпросмотр без <code>")
        self.assertIn("&lt;script&gt;", preview)
        self.assertNotIn("<script>alert", preview)
        self.assertNotIn("&amp;lt;", preview, "двойное экранирование")


class HubRowsTests(R4TesterCase):
    """№28: строка = название + компактная кнопка, пагинация."""

    @scenario_test
    async def test_every_row_has_offer_button_and_pagination(self, h: Harness) -> None:
        created = [
            self.make_personal(h, OWNER, code=f"row_{i:02d}", title=f"Кирпичик {i:02d}")
            for i in range(9)
        ]
        await self.open_hub(h)
        rows = [data for data in callbacks(h) if data.startswith("bk_open:")]
        self.assertEqual(len(rows), 8, "на странице 8 строк")
        for data in rows:
            brick_id = data.split(":", 1)[1]
            self.assertTrue(
                has(h, f"bp_start:{brick_id}"),
                f"у строки {data} нет кнопки предложения",
            )
            self.assertEqual(label_of(h, f"bp_start:{brick_id}"), "📨")
        self.assertTrue(has(h, "bk_page:8"))

        await h.callback("bk_page:8", user_id=OWNER)
        last = [data for data in callbacks(h) if data.startswith("bk_open:")]
        self.assertEqual(last, [f"bk_open:{created[8].id}"])
        self.assertTrue(has(h, f"bp_start:{created[8].id}"))
        # Кнопка строки реально ведёт в предложение этого кирпичика.
        await h.callback(f"bp_start:{created[8].id}", user_id=OWNER)
        self.assertIn("Куда предложить", h.screen_text())

    @scenario_test
    async def test_archived_rows_have_no_offer_button(self, h: Harness) -> None:
        source = self.make_personal(h, OWNER, code="arch_1", title="Архив")
        with h.session_factory() as session:
            archive_personal_brick(
                session,
                actor_telegram_user_id=OWNER,
                brick_id=source.id,
                expected_revision=source.revision,
            )
        await self.open_hub(h)
        await h.callback("bk_archlist", user_id=OWNER)
        self.assertTrue(has(h, f"bk_open:{source.id}"))
        self.assertFalse(
            has(h, f"bp_start:{source.id}"),
            "архивный кирпичик нельзя предложить — кнопки быть не должно",
        )


class ProposalCardTests(R4TesterCase):
    """№29: 7 полей, legacy NULL → «—», перенос описания при одобрении."""

    @scenario_test
    async def test_card_shows_seven_fields(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал карточки")
        source = self.make_personal(
            h,
            OWNER,
            code="card_1",
            title="Карточка",
            description="Подсказка <автору> & всем",
        )
        proposal_id = self.make_proposal(h, OWNER, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER)
        text = h.screen_text()
        for field in PROPOSAL_FIELDS:
            self.assertIn(field, text, f"в карточке нет поля {field!r}")
            self.assertEqual(
                text.count(field), 1, f"поле {field!r} показано не один раз"
            )
        self.assertIn("<code>[card_1]</code>", text)
        self.assertIn(
            "Описание: <code>Подсказка &lt;автору&gt; &amp; всем</code>", text
        )
        author_line = next(
            line for line in text.splitlines() if line.startswith("Автор:")
        )
        self.assertNotEqual(author_line.strip(), "Автор:", "автор не показан")
        self.assertIn("@u2150001", author_line)
        self.assertIn("Цель: место «Зал карточки»", text)
        self.assertIn("Статус: ⏳ Ожидает решения", text)

    @scenario_test
    async def test_legacy_null_description_shows_dash_without_crash(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал legacy", admin_tg=ADMIN)
        source = self.make_personal(h, OWNER, code="legacy_1", title="Старое")
        self.ensure_user(h, OWNER)
        with h.session_factory() as session:
            author = get_or_create_user(session, OWNER, username="legacy")
            brick = session.get(Brick, source.id)
            assert brick is not None
            session.execute(
                sql_text(
                    "INSERT INTO brick_proposals (author_user_id, scope, place_id,"
                    " source_brick_id, title, code, type, source, rules_json,"
                    " description, status, created_at) VALUES (:a, 'place', :p,"
                    " :b, :t, :c, :ty, 'place', :r, NULL, 'pending', :d)"
                ),
                {
                    "a": author.id,
                    "p": place_id,
                    "b": source.id,
                    "t": brick.title,
                    "c": brick.code,
                    "ty": brick.value_type,
                    "r": brick.rules_json,
                    "d": "2026-10-01 10:00:00",
                },
            )
            session.commit()
            legacy_id = int(session.scalar(sql_text("SELECT last_insert_rowid()")) or 0)
        self.assertGreater(legacy_id, 0)

        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER)
        self.assertTrue(
            any("Старое" in label for label, _data in h.screen_buttons()),
            "legacy-предложение пропало из списка",
        )
        await h.callback("bp_open:" + str(legacy_id), user_id=OWNER)
        card = h.screen_text()
        self.assertIn("Описание: —", card)
        self.assertIn("Название: <b>Старое</b>", card)
        self.assertIn("Статус: ⏳ Ожидает решения", card)
        self.assertNotIn("Описание: <code></code>", card)

        # У администратора та же карточка, без падения.
        await h.text("/admin", user_id=ADMIN)
        await h.callback(f"adm_place:{place_id}", user_id=ADMIN)
        await h.callback(f"adm_bprop_place:{place_id}", user_id=ADMIN)
        await h.callback(f"adm_bprop_open:{legacy_id}", user_id=ADMIN)
        self.assertIn("Описание: —", h.screen_text())

    @scenario_test
    async def test_approval_copies_description_into_created_brick(
        self, h: Harness
    ) -> None:
        source = self.make_personal(
            h,
            OWNER,
            code="перенос_1",
            title="Перенос",
            description="Инструкция <автору> & всем",
        )
        proposal_id = self.make_proposal(h, OWNER, source.id, scope="system")
        await h.text("/admin", user_id=SUPER)
        await h.callback("adm_bprop_global", user_id=SUPER)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER)
        self.assertIn(
            "Описание: <code>Инструкция &lt;автору&gt; &amp; всем</code>",
            h.screen_text(),
        )
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER)
        with h.session_factory() as session:
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            self.assertEqual(proposal.status, "approved")
            created = session.scalar(
                select(Brick).where(Brick.code == "перенос_1", Brick.scope == "system")
            )
            self.assertIsNotNone(created, "кирпичик не создан")
            assert created is not None
            self.assertEqual(created.description, "Инструкция <автору> & всем")


class ChatApplicationTests(R4TesterCase):
    """Чат как приложение: экраны, ввод, отказ удаления, порядок, рестарт."""

    @scenario_test
    async def test_navigation_does_not_pile_up_screens(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал Навигации")
        source = self.make_personal(h, OWNER, code="nav_1", title="Навигация")
        await self.open_hub(h)
        sends = len(screen_sends(h))
        # U3a №8: экранов в мастере на один меньше, кругов — на один больше:
        # проверка «экран перерисовывается, а не копится» не ослаблена.
        for _ in range(6):
            await h.callback(f"bp_start:{source.id}", user_id=OWNER)
            await h.callback("bp_target:place", user_id=OWNER)
            await h.callback(f"bp_place:{place_id}", user_id=OWNER)
            for _ in range(3):
                await h.callback("bp_back", user_id=OWNER)
            self.assertIn("Мои кирпичики", h.screen_text())
        self.assertEqual(len(screen_sends(h)), sends, "служебные экраны копятся в чате")
        self.assertGreater(len(h.edits()), 30)

    @scenario_test
    async def test_user_input_deleted_after_success(self, h: Harness) -> None:
        await self.open_hub(h)
        await h.callback("bk_create", user_id=OWNER)
        update = await h.text("Кирпич ввода", user_id=OWNER)
        assert update.message is not None
        self.assertIn(update.message.message_id, h.deleted_ids())
        self.assertIn("Техническое обозначение", h.screen_text())
        with h.session_factory() as session:
            row = session.get(BrickSession, OWNER)
            self.assertIsNotNone(row, "ввод не сохранён")
            assert row is not None
            self.assertIn("Кирпич ввода", row.payload)

    @scenario_test
    async def test_delete_refused_keeps_data_and_flow(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал отказа")
        source = self.make_personal(h, OWNER, code="fail_1", title="Отказ")
        proposal_id = self.make_proposal(h, OWNER, source.id, place_id=place_id)
        await self.open_hub(h)
        await h.callback("bp_mine", user_id=OWNER)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER)

        h.bot.errors_by_type["delete_message"] = BadRequest(
            "Bad Request: message can't be deleted"
        )
        await h.text_from_below("/bricks", user_id=OWNER)
        self.assertIn("Предложение кирпичика", h.screen_text())
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)

        # Флоу продолжается и отзыв работает при отказе удаления.
        await h.callback("bp_mine", user_id=OWNER)
        await h.callback(f"bp_open:{proposal_id}", user_id=OWNER)
        await h.callback("bp_withdraw", user_id=OWNER)
        await h.callback("bp_withdraw_yes", user_id=OWNER)
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            self.assertEqual(row.status, "withdrawn")
            self.assertIsNotNone(session.get(Brick, source.id))

    @scenario_test
    async def test_delete_refused_does_not_lose_draft_input(self, h: Harness) -> None:
        h.bot.errors_by_type["delete_message"] = BadRequest("can't be deleted")
        await self.open_hub(h)
        await h.callback("bk_create", user_id=OWNER)
        await h.text("Черновик при отказе", user_id=OWNER)
        self.assertIn("Техническое обозначение", h.screen_text())
        with h.session_factory() as session:
            row = session.get(BrickSession, OWNER)
            assert row is not None
            self.assertIn("Черновик при отказе", row.payload)

    @scenario_test
    async def test_live_block_above_service_screen_with_album(self, h: Harness) -> None:
        template_id = self.seed_announcement(h, body="Анонс: [ttl]")
        await h.text("/start", user_id=OWNER)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER)
        await h.callback(find(h, "aw_qskip"), user_id=OWNER)
        self.assertIn("Фотографии анонса", h.screen_text())
        await h.photo("alb-0", media_group_id="r4-album", user_id=OWNER)
        await h.photo("alb-1", media_group_id="r4-album", user_id=OWNER)
        await asyncio.sleep(0.5)

        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER)
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER,
                draft_id=drafts[0].draft_id,
            )
        self.assertTrue(details.live_message_ids, "живой блок не отправлен")
        self.assertEqual(details.publication_photos, ("alb-0", "alb-1"))
        screen = screen_id(h)
        self.assertIsNotNone(screen, "служебный экран не отрисован")
        assert screen is not None
        self.assertLess(
            max(details.live_message_ids),
            screen,
            "служебный экран должен быть ниже живого блока",
        )
        # Альбом не создал второй служебный экран и не раздробил отправку.
        groups = [call for call in h.calls if call["type"] == "media_group"]
        self.assertEqual(len(groups), 1, "альбом раздроблен на несколько отправок")
        self.assertEqual(
            [getattr(item, "media", None) for item in groups[0]["media"]],
            ["alb-0", "alb-1"],
        )
        renders = [
            call for call in h.screens() if "Фотографии анонса" in record_text(call)
        ]
        self.assertEqual(len(renders), 1, "альбом создал лишний служебный экран")

    @scenario_test
    async def test_restart_creates_no_duplicate_live_block(self, h: Harness) -> None:
        template_id = self.seed_announcement(h, body="Анонс: [ttl]")
        await h.text("/start", user_id=OWNER)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER)
        await h.text("До рестарта", user_id=OWNER)
        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER)
            draft_id = drafts[0].draft_id
            live_before = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER,
                draft_id=draft_id,
            ).live_message_ids
        self.assertTrue(live_before)

        await h.restart()
        await h.text("/start", user_id=OWNER)
        await h.callback("my_announcements", user_id=OWNER)
        await h.callback(f"aw_open:{draft_id}", user_id=OWNER)

        with h.session_factory() as session:
            restored = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER,
                draft_id=draft_id,
            )
        self.assertEqual(restored.live_message_ids, live_before)
        self.assertEqual(
            len(screen_sends(h)),
            2,
            "служебных экранов должно быть два: меню и вопрос",
        )
        live_edits = [
            call
            for call in h.edits()
            if int(call.get("message_id") or 0) == int(live_before[0])
        ]
        self.assertTrue(live_edits, "живой блок не обновлён после рестарта")
        self.assertIn("До рестарта", record_text(live_edits[-1]))


class HumorTests(R4TesterCase):
    """Юмор: ~1 %, детерминированный, отключаемый, не в публикации."""

    def test_rate_around_one_percent_and_deterministic(self) -> None:
        keys = [f"key-{i}" for i in range(20_000)]
        hits = [key for key in keys if ui_humor.pick_variant(key)]
        self.assertLess(len(hits), len(keys) // 50, "частота юмора заметно выше 1 %")
        self.assertGreater(len(hits), 0, "юмор не срабатывает вовсе")
        self.assertLess(abs(len(hits) / len(keys) - 0.01), 0.005)
        for key in hits[:20]:
            self.assertEqual(
                ui_humor.pick_variant(key),
                ui_humor.pick_variant(key),
                "шутка недетерминирована",
            )

    def test_disabled_flag_turns_humor_off(self) -> None:
        key = next(
            f"off-{i}" for i in range(100_000) if ui_humor.pick_variant(f"off-{i}")
        )
        self.assertIsNotNone(ui_humor.empty_announcement_joke())
        with patch("config.UI_HUMOR_ENABLED", False):
            self.assertIsNone(ui_humor.pick_variant(key))
            self.assertIsNone(ui_humor.empty_announcement_joke())
            self.assertIsNone(ui_humor.get_humor_line(announcement_is_empty=True))

    def test_forbidden_screens_never_joke(self) -> None:
        for kind in ui_humor.FORBIDDEN_SCREEN_KINDS:
            for index in range(500):
                self.assertIsNone(ui_humor.pick_variant(f"k{index}", screen_kind=kind))

    @scenario_test
    async def test_joke_never_in_publication_or_live_block(self, h: Harness) -> None:
        template_id = self.seed_announcement(h, body="Текст: [ttl]")
        joke = ui_humor.HUMOR_JOKES[0]
        with patch("utils.ui_humor.pick_variant", return_value=joke):
            await h.text("/start", user_id=OWNER)
            await h.callback(f"tp_pick:{template_id}", user_id=OWNER)
            await h.text("Значение", user_id=OWNER)
            await h.callback(find(h, "aw_photo_none"), user_id=OWNER)

        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER)
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER,
                draft_id=drafts[0].draft_id,
            )
        publication = details.publication_text or ""
        self.assertIn("Значение", publication)
        live_ids = {int(mid) for mid in details.live_message_ids}
        live_texts = [
            record_text(call)
            for call in h.calls
            if int(call.get("message_id") or 0) in live_ids
            or (
                call["type"] in ("photo", "media_group")
                and int(call.get("message_id") or 0) in live_ids
            )
        ]
        self.assertTrue(live_texts, "живой блок не обновлялся")
        for variant in ui_humor.HUMOR_JOKES:
            self.assertNotIn(variant, publication, "шутка в публикации")
            for post in h.posts():
                self.assertNotIn(variant, post or "", "шутка в отправленном тексте")
            for reply in h.reply_texts():
                self.assertNotIn(variant, reply, "шутка в служебном ответе")
            for live_text in live_texts:
                self.assertNotIn(variant, live_text, "шутка попала в живой блок")

    @scenario_test
    async def test_empty_announcement_joke_only_on_service_screen(
        self, h: Harness
    ) -> None:
        template_id = self.seed_announcement(h, body="[ttl]")
        joke = ui_humor.empty_announcement_joke()
        self.assertIsNotNone(joke)
        await h.text("/start", user_id=OWNER)
        await h.callback(f"tp_pick:{template_id}", user_id=OWNER)
        await h.callback(find(h, "aw_qskip"), user_id=OWNER)
        self.assertIn(
            joke, h.screen_text(), "шутка пустого анонса не на служебном экране"
        )
        with h.session_factory() as session:
            drafts = list_announcement_drafts(session, actor_telegram_user_id=OWNER)
            details = get_announcement_draft(
                session,
                actor_telegram_user_id=OWNER,
                draft_id=drafts[0].draft_id,
            )
        self.assertNotIn(joke, details.publication_text or "")
        live_ids = {int(mid) for mid in details.live_message_ids}
        self.assertTrue(live_ids, "живой блок не отправлен")
        self.assertFalse(
            live_ids & {int(mid) for mid in h.deleted_ids()},
            "живой блок удалён из чата",
        )
        screen_ref = h.user_data(user_id=OWNER).get("screen")
        self.assertIsInstance(screen_ref, dict, "служебный экран не отрисован")
        assert isinstance(screen_ref, dict)
        self.assertLess(
            min(live_ids),
            int(screen_ref["message_id"]),
            "живой блок оказался ниже служебного экрана",
        )
        live_texts = live_block_texts(h, live_ids)
        self.assertTrue(live_texts, "живой блок не отправлялся и не правился")
        for live_text in live_texts:
            self.assertNotIn(joke, live_text, "шутка попала в живой блок")
        for post in h.posts():
            self.assertNotIn(joke, post or "", "шутка попала в отправленный пост")

    @scenario_test
    async def test_warning_screen_never_jokes(self, h: Harness) -> None:
        """Занятый код на предпросмотре — предупреждение, а не шутка."""
        place_id = self.make_place(h, "Зал предупреждения", admin_tg=ADMIN)
        from brick_store import create_place_brick

        with h.session_factory() as session:
            superadmin = get_or_create_user(session, SUPER, username="super")
            create_place_brick(
                session,
                actor_telegram_user_id=SUPER,
                place_id=place_id,
                code="занят_9",
                title="Занят",
                value_type="short_text",
            )
            self.assertIsNotNone(superadmin)
        source = self.make_personal(h, OWNER, code="занят_9", title="Дубль")
        with patch("utils.ui_humor.pick_variant", return_value=ui_humor.HUMOR_JOKES[0]):
            await self.walk(h, source.id, place_id)
        text = h.screen_text()
        self.assertIn("Отправить сейчас нельзя", text)
        self.assertFalse(has(h, "bp_send"), "отправка при занятом коде недоступна")
        for variant in ui_humor.HUMOR_JOKES:
            self.assertNotIn(variant, text, "шутка в предупреждении")

    @scenario_test
    async def test_disabled_flag_hides_joke_in_empty_announcement(
        self, h: Harness
    ) -> None:
        template_id = self.seed_announcement(h, body="[ttl]")
        with patch("config.UI_HUMOR_ENABLED", False):
            await h.text("/start", user_id=OWNER)
            await h.callback(f"tp_pick:{template_id}", user_id=OWNER)
            await h.callback(find(h, "aw_qskip"), user_id=OWNER)
        text = h.screen_text()
        for variant in ui_humor.HUMOR_JOKES:
            self.assertNotIn(variant, text, "юмор не отключился флагом")
        self.assertIn("Фотографии анонса", text, "экран шага фото сломан")


class ForgeryAndIdorTests(R4TesterCase):
    """Негатив: forged callbacks и IDOR."""

    @scenario_test
    async def test_forged_bp_start_with_foreign_or_missing_id(self, h: Harness) -> None:
        foreign = self.make_personal(h, STRANGER, code="чужой_1", title="Чужой")
        self.make_personal(h, OWNER, code="свой_1", title="Свой")
        await self.open_hub(h)
        self.assertIn("Мои кирпичики", h.screen_text())

        await h.callback(f"bp_start:{foreign.id}", user_id=OWNER)
        text = h.screen_text()
        self.assertIn("Мои кирпичики", text, "чужой id открыл мастер предложения")
        self.assertIn("не найден", text)
        self.assertNotIn("Чужой", text, "данные чужого кирпичика раскрыты")
        self.assertTrue(has(h, "bk_create"), "хаб остался без действий")

        await h.callback("bp_start:999999", user_id=OWNER)
        self.assertIn("не найден", h.screen_text())
        # Строковые кнопки 📨 остаются на месте — хаб не теряет действий.
        self.assertTrue(any(data.startswith("bp_start:") for data in callbacks(h)))

    @scenario_test
    async def test_malformed_bp_start_is_stale_and_safe(self, h: Harness) -> None:
        self.make_personal(h, OWNER, code="свой_2", title="Свой")
        await self.open_hub(h)
        before = h.screen_text()
        for data in ("bp_start:0", "bp_start:", "bp_start:abc", "bp_page:-1"):
            await h.callback(data, user_id=OWNER)
        self.assertEqual(h.screen_text(), before, "битая кнопка изменила экран")
        self.assertTrue(
            any("устарела" in text.lower() for text in alert_texts(h)),
            f"нет ответа на битую кнопку: {alert_texts(h)!r}",
        )

    @scenario_test
    async def test_stale_proposal_callbacks_do_not_change_data(
        self, h: Harness
    ) -> None:
        place_id = self.make_place(h, "Зал устаревших")
        source = self.make_personal(h, OWNER, code="устар_1", title="Устаревшая")
        proposal_id = self.make_proposal(h, OWNER, source.id, place_id=place_id)
        await self.open_hub(h)
        before = h.screen_text()
        # Кнопки, допустимые только на других фазах.
        for data in (
            f"bp_source:{source.id}",
            "bp_target:place",
            f"bp_place:{place_id}",
            "bp_send",
            f"bp_open:{proposal_id}",
            "bp_withdraw_yes",
        ):
            await h.callback(data, user_id=OWNER)
        self.assertEqual(h.screen_text(), before, "устаревшая кнопка сменила экран")
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)
            self.assertIsNone(row.decided_at)

    @scenario_test
    async def test_foreign_proposal_card_is_not_visible(self, h: Harness) -> None:
        place_id = self.make_place(h, "Зал IDOR")
        source = self.make_personal(h, OWNER, code="секрет_1", title="Секрет")
        proposal_id = self.make_proposal(h, OWNER, source.id, place_id=place_id)
        self.ensure_user(h, STRANGER)
        await self.open_hub(h, user_id=STRANGER)
        await h.callback("bp_mine", user_id=STRANGER)
        self.assertIn("Вы ещё не отправляли предложений", h.screen_text())

        # Прямой id чужого предложения на экране списка.
        await h.callback(f"bp_open:{proposal_id}", user_id=STRANGER)
        text = h.screen_text()
        self.assertNotIn("Секрет", text, "чужое предложение раскрыто")
        self.assertNotIn("секрет_1", text)
        self.assertIn("не найдено", text)
        # Чужой кирпичик тоже не виден в хабе.
        self.assertFalse(has(h, f"bk_open:{source.id}"))
        # И отзыв чужого предложения на уровне сервиса запрещён.
        with h.session_factory() as session:
            result = withdraw_brick_proposal(
                session,
                actor_telegram_user_id=STRANGER,
                proposal_id=proposal_id,
            )
        self.assertEqual(result.status, "not_found")
        with h.session_factory() as session:
            row = session.get(BrickProposal, proposal_id)
            assert row is not None
            self.assertEqual(row.status, PROPOSAL_STATUS_PENDING)


if __name__ == "__main__":
    unittest.main()
