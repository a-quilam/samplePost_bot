# tests/test_stage21_transfer_flow.py
"""
Stage 21: интеграционные сценарии передачи администратора места.

Все сценарии идут через настоящий Application.process_update, FakeBot и
временную SQLite. Проверяются кнопка «Передать админку», известный и
неизвестный получатель, одноразовая ссылка, утверждение кандидата
инициатором, подтверждение в аккаунте получателя, отмена/отказ, сбой
доставки, устаревшие кнопки, рестарт, сохранение незавершённого поста,
немедленная смена прав Stage 20B и длинный адрес без молчаливого обрезания.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from announcement_store import list_announcement_drafts
from models import (
    AdminSession,
    AdminTransfer,
    Place,
    PlaceAdmin,
    PlaceName,
    User,
)
from permissions import is_place_admin
from place_store import normalize_place_name
from user_store import get_or_create_user

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)


SUPERADMIN_TG = 730100
INITIATOR_TG = 730200
TARGET_TG = 730300
OTHER_TG = 730400
THIRD_TG = 730500

DENIED_ALERT = "Доступ к административному разделу закрыт."


def insert_place(
    h,
    name: str,
    *,
    address: str | None = None,
    url: str | None = None,
) -> int:
    """Вставляет место с каноническим названием напрямую в базу."""
    session = h.session_factory()
    try:
        place = Place(address=address, url=url)
        session.add(place)
        session.flush()
        session.add(
            PlaceName(
                place_id=place.id,
                name=name,
                name_search=normalize_place_name(name),
                is_canonical=1,
            )
        )
        session.commit()
        return int(place.id)
    finally:
        session.close()


def ensure_user(
    h,
    telegram_user_id: int,
    *,
    username: str | None = None,
    full_name: str | None = None,
) -> None:
    """Создаёт известного пользователя напрямую (без Telegram-обновления)."""
    from user_store import get_or_create_user

    session = h.session_factory()
    try:
        get_or_create_user(
            session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )
    finally:
        session.close()


def assign_place_admin(h, place_id: int, telegram_user_id: int) -> None:
    """Назначает действующего администратора напрямую для подготовки сценария."""
    ensure_user(h, telegram_user_id, username=f"admin{telegram_user_id}")
    session = h.session_factory()
    try:
        user = session.scalar(
            select(User).where(User.telegram_user_id == telegram_user_id)
        )
        assert user is not None
        session.add(
            PlaceAdmin(
                place_id=place_id,
                user_id=user.id,
                assigned_at=dt.now(),
            )
        )
        session.commit()
    finally:
        session.close()


def active_admin_tg(h, place_id: int) -> int | None:
    session = h.session_factory()
    try:
        row = session.execute(
            select(User.telegram_user_id)
            .join(PlaceAdmin, PlaceAdmin.user_id == User.id)
            .where(
                PlaceAdmin.place_id == place_id,
                PlaceAdmin.revoked_at.is_(None),
            )
        ).first()
        return int(row[0]) if row is not None else None
    finally:
        session.close()


def transfer_row(h, transfer_id: int) -> AdminTransfer:
    session = h.session_factory()
    try:
        row = session.get(AdminTransfer, transfer_id)
        assert row is not None
        session.expunge(row)
        return row
    finally:
        session.close()


def pending_transfer(h, place_id: int) -> AdminTransfer | None:
    session = h.session_factory()
    try:
        row = session.scalar(
            select(AdminTransfer).where(
                AdminTransfer.place_id == place_id,
                AdminTransfer.status == "pending",
            )
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def make_personal_template(h, telegram_user_id: int) -> int:
    """Личный шаблон с двумя кирпичиками: минимальный конструктор для теста."""
    from brick_store import create_personal_brick
    from template_store import create_personal_template

    session = h.session_factory()
    try:
        get_or_create_user(session, telegram_user_id, username="wip")
        create_personal_brick(
            session,
            actor_telegram_user_id=telegram_user_id,
            code="ttl",
            title="Заголовок",
            value_type="short_text",
            rules={},
        )
        create_personal_brick(
            session,
            actor_telegram_user_id=telegram_user_id,
            code="dsc",
            title="Описание",
            value_type="long_text",
            rules={},
        )
        template = create_personal_template(
            session,
            actor_telegram_user_id=telegram_user_id,
            name="Шаблон передачи",
            header="",
            body="[ttl]\n[dsc]",
            footer="",
        )
        session.commit()
        return template.template_id
    finally:
        session.close()


def single_draft_id(h, telegram_user_id: int) -> int:
    from announcement_store import list_announcement_drafts

    with h.session_factory() as session:
        drafts = list_announcement_drafts(
            session, actor_telegram_user_id=telegram_user_id
        )
    assert len(drafts) == 1, drafts
    return drafts[0].draft_id


def user_state_snapshot(h, telegram_user_id: int) -> list[tuple]:
    """Экранное состояние пользователя: ID сообщения и payload."""
    from models import UserState

    with h.session_factory() as session:
        rows = list(
            session.scalars(
                select(UserState).where(UserState.user_id == telegram_user_id)
            ).all()
        )
    return [(row.screen_message_id, row.payload) for row in rows]


def screen_callback(h, prefix: str) -> str:
    matches = [
        data for _, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {h.screen_buttons()!r}\n{h.screen_text()}"
        )
    return matches[0]


def notification_callback(h, chat_id: int, prefix: str) -> str:
    """Callback-данные из последнего обычного сообщения бота в личный чат."""
    for call in reversed(h.calls):
        if call["type"] != "message":
            continue
        if int(call.get("chat_id") or 0) != chat_id:
            continue
        markup = call.get("reply_markup")
        rows = getattr(markup, "inline_keyboard", None) or []
        for row in rows:
            for button in row:
                data = getattr(button, "callback_data", None)
                if data and data.startswith(prefix):
                    return str(data)
    raise AssertionError(f"нет уведомления с кнопкой '{prefix}' в чате {chat_id}")


def message_texts_to(h, chat_id: int) -> list[str]:
    return [
        str(call.get("text") or "")
        for call in h.calls
        if call["type"] == "message" and int(call.get("chat_id") or 0) == chat_id
    ]


def notification_texts_to(h, chat_id: int) -> list[str]:
    """
    Тексты личных уведомлений пользователю: send и правка сообщения.

    Уведомление — не экран управления: `h.screen_text()` описывает только
    постоянный экран инициатора и не должен подменяться чужим сообщением
    (U1b: у видов записей разные `link_preview_options`).
    """
    return [
        str(call.get("text") or "")
        for call in h.calls
        if call["type"] in ("message", "edit_message")
        and int(call.get("chat_id") or 0) == chat_id
    ]


def callback_answers(h) -> list[dict]:
    return [c for c in h.calls if c["type"] == "callback_answer"]


def last_alert(h) -> str | None:
    answers = callback_answers(h)
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


async def open_transfer_input(h, place_id: int) -> None:
    """Из главного меню открывает карточку места и экран передачи."""
    await h.text("/admin")
    await h.callback(screen_callback(h, "adm_place:"))
    labels = [label for label, _ in h.screen_buttons()]
    if "🔁 Передать админку" not in labels:
        raise AssertionError(f"нет кнопки передачи: {labels!r}\n{h.screen_text()}")
    await h.callback(f"adm_transfer:{place_id}")
    if "Введите @username" not in h.screen_text():
        raise AssertionError(f"нет экрана ввода получателя:\n{h.screen_text()}")


async def create_known_offer(h, place_id: int, username: str = "newadmin") -> int:
    await open_transfer_input(h, place_id)
    await h.text(f"@{username}")
    if "Подтвердите создание предложения" not in h.screen_text():
        raise AssertionError(f"нет подтверждения получателя:\n{h.screen_text()}")
    await h.callback("adm_transfer_confirm")
    row = pending_transfer(h, place_id)
    if row is None:
        raise AssertionError(f"предложение не создано:\n{h.screen_text()}")
    return int(row.id)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class TransferScreenTests(unittest.TestCase):
    """Кнопка передачи и отказ посторонним."""

    @scenario_test
    async def test_active_admin_sees_transfer_button_and_input(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        h.user_id = INITIATOR_TG

        await h.text("/admin")
        await h.callback(screen_callback(h, "adm_place:"))
        labels = [label for label, _ in h.screen_buttons()]
        self.assertIn("🔁 Передать админку", labels)

        await h.callback(f"adm_transfer:{place_id}")
        text = h.screen_text()
        self.assertIn("Передача администрации места", text)
        self.assertIn("Введите @username", text)
        self.assertIn("До этого вы остаётесь администратором", text)

    @scenario_test
    async def test_superadmin_without_place_role_has_no_transfer_button(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        h.user_id = SUPERADMIN_TG

        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            labels = [label for label, _ in h.screen_buttons()]
            self.assertNotIn("🔁 Передать админку", labels)
            self.assertIn("🔁 Заменить администратора", labels)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class KnownRecipientFlowTests(unittest.TestCase):
    """Известный получатель: уведомление, подтверждение и смена прав."""

    @scenario_test
    async def test_full_known_flow_grants_and_revokes_access_immediately(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, place_id)
        self.assertIn("Получатель получил уведомление", h.screen_text())
        row = transfer_row(h, transfer_id)
        self.assertEqual(row.status, "pending")

        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(last_alert(h), "Передача подтверждена")
        self.assertEqual(active_admin_tg(h, place_id), TARGET_TG)

        # Обе стороны получили итог; уведомление инициатору — отдельное событие.
        texts = message_texts_to(h, INITIATOR_TG)
        self.assertTrue(
            any("Передача администрации завершена" in text for text in texts),
            texts,
        )
        self.assertEqual(
            transfer_row(h, transfer_id).status,
            "confirmed",
        )

        # Новый админ сразу получает кабинет и Stage 20B (Poster 2.0 S2:
        # «🛡 Администрирование» живёт внутри «⚙️ Управление»).
        await h.text("/start", user_id=TARGET_TG, chat_id=TARGET_TG)
        await h.callback("manage", user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertIn(
            ("🛡 Администрирование", "admin_panel"),
            h.screen_buttons(),
        )
        await h.callback("admin_panel", user_id=TARGET_TG, chat_id=TARGET_TG)
        await h.callback(f"adm_place:{place_id}", user_id=TARGET_TG, chat_id=TARGET_TG)
        await h.callback(
            f"adm_edit_place:{place_id}", user_id=TARGET_TG, chat_id=TARGET_TG
        )
        self.assertIn("Редактирование места", h.screen_text())

        # Старый админ немедленно теряет права: кнопка на его открытом экране
        # передачи упирается в свежую проверку роли, а /admin закрыт.
        await h.callback(
            f"adm_transfer_refresh:{place_id}",
            user_id=INITIATOR_TG,
            chat_id=INITIATOR_TG,
        )
        self.assertEqual(last_alert(h), DENIED_ALERT)
        await h.text("/admin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        self.assertIn("Раздел доступен только администраторам Poster.", h.screen_text())

    @scenario_test
    async def test_other_account_cannot_accept_someone_elses_offer(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        ensure_user(h, OTHER_TG, username="guestuser", full_name="Другой Гость")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, place_id)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=OTHER_TG, chat_id=OTHER_TG)
        self.assertEqual(last_alert(h), "Предложение адресовано другому пользователю.")
        self.assertEqual(transfer_row(h, transfer_id).status, "pending")
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)

        # Настоящий получатель после этого может подтвердить.
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(transfer_row(h, transfer_id).status, "confirmed")
        self.assertEqual(active_admin_tg(h, place_id), TARGET_TG)

    @scenario_test
    async def test_delivery_failure_is_explicit_and_offer_stays_valid(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        await open_transfer_input(h, place_id)
        h.bot.fail_types.add("message")
        await h.text("@newadmin")
        await h.callback("adm_transfer_confirm")
        row = pending_transfer(h, place_id)
        assert row is not None
        transfer_id = int(row.id)
        self.assertIn("уведомление не доставлено", h.screen_text().lower())
        row = transfer_row(h, transfer_id)
        self.assertEqual(row.status, "pending")
        self.assertIsNone(row.notified_at)
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)

        h.bot.fail_types.discard("message")
        notify = screen_callback(h, "adm_transfer_notify:")
        await h.callback(notify)
        self.assertIn("Уведомление доставлено", h.screen_text())
        self.assertIsNotNone(transfer_row(h, transfer_id).notified_at)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(active_admin_tg(h, place_id), TARGET_TG)

    @scenario_test
    async def test_recipient_decline_keeps_rights_and_allows_new_offer(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, place_id)
        decline = notification_callback(h, TARGET_TG, "adm_transfer_decline:")
        await h.callback(decline, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(last_alert(h), "Предложение отклонено")
        self.assertEqual(transfer_row(h, transfer_id).status, "rejected")
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)
        self.assertTrue(
            any(
                "Получатель отклонил передачу" in text
                for text in message_texts_to(h, INITIATOR_TG)
            )
        )

        # Новое предложение возможно, но требует нового уведомления.
        # После отказа активного предложения нет: экран сразу просит username.
        await h.text("/admin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        self.assertIn("Введите @username", h.screen_text())
        await h.text("@newadmin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback(
            "adm_transfer_confirm", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        new_row = pending_transfer(h, place_id)
        self.assertIsNotNone(new_row)
        assert new_row is not None
        self.assertNotEqual(new_row.id, transfer_id)

    @scenario_test
    async def test_initiator_cancel_before_confirm_and_stale_accept(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, place_id)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        cancel = screen_callback(h, "adm_transfer_cancel:")
        await h.callback(cancel)
        self.assertEqual(transfer_row(h, transfer_id).status, "cancelled")
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)
        self.assertIn("Права остались у вас", h.screen_text())
        self.assertTrue(
            any(
                "Передача администрации отменена" in text
                for text in message_texts_to(h, TARGET_TG)
            )
        )

        # Старая кнопка в уведомлении не должна ничего подтвердить.
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(transfer_row(h, transfer_id).status, "cancelled")
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)
        # Получатель видит объяснение в своём уведомлении, а не молчание.
        self.assertTrue(
            any(
                "Передача отменена инициатором" in text
                for text in notification_texts_to(h, TARGET_TG)
            ),
            notification_texts_to(h, TARGET_TG),
        )
        self.assertIn("Права остались у вас", h.screen_text())

    @scenario_test
    async def test_restart_restores_outgoing_offer_and_verification(self, h):
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, place_id)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")

        await h.restart()
        await h.text("/admin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        text = h.screen_text()
        self.assertIn("Передача администрации", text)
        self.assertIn("Новый Админ (@newadmin)", text)
        self.assertIn("Получатель известен", text)

        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(transfer_row(h, transfer_id).status, "confirmed")
        self.assertEqual(active_admin_tg(h, place_id), TARGET_TG)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class InviteFlowTests(unittest.TestCase):
    """Неизвестный получатель: ссылка, кандидат, утверждение инициатором."""

    @scenario_test
    async def test_invite_link_shown_once_and_never_persisted_raw(self, h):
        place_id = insert_place(h, "Театр «Ссылка»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        h.user_id = INITIATOR_TG

        await open_transfer_input(h, place_id)
        await h.text("@stranger")
        self.assertIn("Получатель пока не известен боту", h.screen_text())
        await h.callback("adm_transfer_confirm")
        text = h.screen_text()
        self.assertIn("Одноразовая ссылка-приглашение", text)
        match = re.search(r"start=(tf_\d+_[A-Za-z0-9_-]+)", text)
        self.assertIsNotNone(match, text)
        assert match is not None
        payload = match.group(1)
        # Токен urlsafe может сам содержать «_», поэтому отделяем его от
        # префикса `tf_<id>_` только по первым двум разделителям.
        token = payload.split("_", 2)[2]

        row = pending_transfer(h, place_id)
        assert row is not None
        self.assertIsNone(row.to_user_id)
        self.assertIsNotNone(row.token_hash)
        self.assertNotEqual(row.token_hash, token)
        self.assertNotIn(token, row.token_hash or "")

        # Сырой токен не попадает в JSON состояния кабинета в SQLite.
        session = h.session_factory()
        try:
            states = list(session.scalars(select(AdminSession)).all())
            for state in states:
                self.assertNotIn(token, state.payload)
        finally:
            session.close()

        # После обновления ссылка из БД не восстанавливается и не показывается.
        await h.callback(screen_callback(h, "adm_transfer_refresh:"))
        self.assertNotIn(payload, h.screen_text())
        self.assertIn("показана один раз", h.screen_text())

    @scenario_test
    async def test_full_unknown_recipient_flow_requires_two_sided_consent(self, h):
        place_id = insert_place(h, "Театр «Ссылка»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="invitee", full_name="Приглашённый")
        ensure_user(h, OTHER_TG, username="stealer", full_name="Третий")
        h.user_id = INITIATOR_TG

        await open_transfer_input(h, place_id)
        await h.text("@stranger")
        await h.callback("adm_transfer_confirm")
        match = re.search(r"start=(tf_\d+_[A-Za-z0-9_-]+)", h.screen_text())
        assert match is not None
        payload = match.group(1)
        row = pending_transfer(h, place_id)
        assert row is not None
        transfer_id = int(row.id)

        # Первый открывший — только кандидат; прав и кнопки подтверждения нет.
        await h.text(f"/start {payload}", user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertIn("Приглашение открыто", h.screen_text())
        self.assertIn("Это ещё не передача прав", h.screen_text())
        self.assertEqual(active_admin_tg(h, place_id), INITIATOR_TG)
        row = transfer_row(h, transfer_id)
        self.assertIsNotNone(row.opened_by_user_id)
        self.assertIsNone(row.to_user_id)

        # Другая учётная запись не может перепривязать уже открытую ссылку.
        await h.text(f"/start {payload}", user_id=OTHER_TG, chat_id=OTHER_TG)
        self.assertIn("уже открыта другим аккаунтом", h.screen_text())
        self.assertEqual(
            transfer_row(h, transfer_id).opened_by_user_id, row.opened_by_user_id
        )

        # Инициатор видит кандидата и утверждает его отдельным действием.
        await h.callback(
            f"adm_transfer_refresh:{place_id}",
            user_id=INITIATOR_TG,
            chat_id=INITIATOR_TG,
        )
        self.assertIn("Ссылку открыл", h.screen_text())
        self.assertIn(str(TARGET_TG), h.screen_text())
        approve = screen_callback(h, "adm_transfer_approve:")
        await h.callback(approve, user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        self.assertIn("Получатель утверждён", h.screen_text())
        self.assertEqual(
            transfer_row(h, transfer_id).to_user_id,
            transfer_row(h, transfer_id).opened_by_user_id,
        )

        # После утверждения другой аккаунт всё ещё не может подтвердить.
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=OTHER_TG, chat_id=OTHER_TG)
        self.assertEqual(last_alert(h), "Предложение адресовано другому пользователю.")

        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(active_admin_tg(h, place_id), TARGET_TG)
        self.assertEqual(transfer_row(h, transfer_id).status, "confirmed")

    @scenario_test
    async def test_invalid_link_and_self_link_do_not_grant_rights(self, h):
        place_id = insert_place(h, "Театр «Ссылка»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        h.user_id = INITIATOR_TG

        await open_transfer_input(h, place_id)
        await h.text("@stranger")
        await h.callback("adm_transfer_confirm")
        match = re.search(r"start=(tf_\d+_[A-Za-z0-9_-]+)", h.screen_text())
        assert match is not None
        payload = match.group(1)
        transfer_id = payload.split("_", 2)[1]

        # Ссылка не должна открываться без правильного токена.
        await h.text(
            f"/start tf_{transfer_id}_wrong-token-value-123456",
            user_id=TARGET_TG,
            chat_id=TARGET_TG,
        )
        self.assertIn("Ссылка-приглашение недействительна", h.screen_text())
        row = transfer_row(h, int(transfer_id))
        self.assertIsNone(row.opened_by_user_id)

        # Собственная ссылка не делает инициатора кандидатом.
        await h.text(f"/start {payload}", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        self.assertIn("Это ваша собственная ссылка", h.screen_text())
        self.assertIsNone(transfer_row(h, int(transfer_id)).opened_by_user_id)

    @scenario_test
    async def test_admin_replacement_supersedes_open_callback(self, h):
        place_id = insert_place(h, "Театр «Ссылка»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="invitee", full_name="Приглашённый")
        ensure_user(h, THIRD_TG, username="thirdadmin", full_name="Третий")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        h.user_id = INITIATOR_TG

        await open_transfer_input(h, place_id)
        await h.text("@stranger")
        await h.callback("adm_transfer_confirm")
        match = re.search(r"start=(tf_\d+_[A-Za-z0-9_-]+)", h.screen_text())
        assert match is not None
        payload = match.group(1)
        await h.text(f"/start {payload}", user_id=TARGET_TG, chat_id=TARGET_TG)

        await h.callback(
            f"adm_transfer_refresh:{place_id}",
            user_id=INITIATOR_TG,
            chat_id=INITIATOR_TG,
        )
        approve = screen_callback(h, "adm_transfer_approve:")
        await h.callback(approve, user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")

        # Stage 20A: суперадмин заменяет администратора напрямую.
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            from place_admin_store import replace_place_admin

            session = h.session_factory()
            try:
                result = replace_place_admin(
                    session,
                    actor_telegram_user_id=SUPERADMIN_TG,
                    place_id=place_id,
                    target_telegram_user_id=THIRD_TG,
                    expected_admin_telegram_user_id=INITIATOR_TG,
                )
            finally:
                session.close()
        self.assertEqual(result.status, "replaced")
        row = pending_transfer(h, place_id)
        self.assertIsNone(row)
        self.assertEqual(active_admin_tg(h, place_id), THIRD_TG)

        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)
        self.assertEqual(active_admin_tg(h, place_id), THIRD_TG)
        self.assertTrue(
            any(
                "Предложение недействительно" in text
                for text in notification_texts_to(h, TARGET_TG)
            ),
            notification_texts_to(h, TARGET_TG),
        )


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class SafetyAndAuditFlowTests(unittest.TestCase):
    """WIP, Stage 20B и длинный адрес."""

    @scenario_test
    async def test_unfinished_post_survives_transfer_and_rights_change(self, h):
        """Начатый анонс переживает передачу админки и смену прав (S5).

        Мастер поста удалён, поэтому незавершённая работа — это черновик
        конструктора (`announcement_drafts`) со значениями кирпичиков.
        После передачи админки бывший админ по-прежнему открывает его в
        «📂 Мои анонсы» и может продолжить, а `user_states` (экран) цела.
        """
        place_id = insert_place(h, "Кофейня «Передача»")
        assign_place_admin(h, place_id, INITIATOR_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        template_id = make_personal_template(h, INITIATOR_TG)
        h.user_id = INITIATOR_TG

        await h.text("/start", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback("pick_template", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback(
            f"tp_pick:{template_id}", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        await h.text(
            "Незавершённый пост про передачу",
            user_id=INITIATOR_TG,
            chat_id=INITIATOR_TG,
        )
        await h.text(
            "Описание про передачу", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        draft_id = single_draft_id(h, INITIATOR_TG)
        before = user_state_snapshot(h, INITIATOR_TG)

        # Передача админки второму пользователю. Команда закрывает
        # конструктор, черновик при этом остаётся в базе.
        await h.text("/admin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback(
            screen_callback(h, "adm_place:"),
            user_id=INITIATOR_TG,
            chat_id=INITIATOR_TG,
        )
        await h.callback(
            f"adm_transfer:{place_id}", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        await h.text("@newadmin", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback(
            "adm_transfer_confirm", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)

        # Права сменились: бывший админ больше не админ места.
        session = h.session_factory()
        try:
            self.assertFalse(is_place_admin(session, INITIATOR_TG, place_id))
        finally:
            session.close()

        # Незавершённый анонс не потерян и открывается в «📂 Мои анонсы».
        await h.text("/start", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        await h.callback("my_announcements", user_id=INITIATOR_TG, chat_id=INITIATOR_TG)
        self.assertIn("Мои анонсы", h.screen_text())
        self.assertIn(
            f"aw_open:{draft_id}",
            [data for _, data in h.screen_buttons() if data],
            "начатый анонс виден в списке",
        )
        await h.callback(
            f"aw_open:{draft_id}", user_id=INITIATOR_TG, chat_id=INITIATOR_TG
        )
        self.assertIn("Обзор анонса", h.screen_text())
        self.assertIn("Незавершённый пост про передачу", h.screen_text())

        # Содержимое черновика и экранное состояние целы.
        with h.session_factory() as session:
            drafts = list_announcement_drafts(
                session, actor_telegram_user_id=INITIATOR_TG
            )
            self.assertEqual([draft.draft_id for draft in drafts], [draft_id])
            payloads = [
                value.payload for value in drafts[0].values if value.payload is not None
            ]
        self.assertIn({"text": "Незавершённый пост про передачу"}, payloads)
        self.assertIn({"text": "Описание про передачу"}, payloads)
        self.assertEqual(user_state_snapshot(h, INITIATOR_TG), before)

    @scenario_test
    async def test_long_address_saved_and_displayed_as_explicit_preview(self, h):
        long_address = "улица " + ("Очень длинный адрес " * 200)
        place_id = insert_place(h, "Длинный адрес", address=long_address)
        assign_place_admin(h, place_id, INITIATOR_TG)
        h.user_id = INITIATOR_TG

        await h.text("/admin")
        await h.callback(screen_callback(h, "adm_place:"))
        text = h.screen_text()
        self.assertIn("Показаны первые 1500 символов", text)
        self.assertIn("Полное значение сохранено", text)

        await h.callback(screen_callback(h, "adm_edit_place:"))
        text = h.screen_text()
        self.assertIn("Показаны первые 1500 символов", text)

        session = h.session_factory()
        try:
            place = session.get(Place, place_id)
            assert place is not None
            self.assertEqual(place.address, long_address)
            self.assertGreater(len(place.address or ""), 1500)
            self.assertFalse((place.address or "").endswith("…"))
        finally:
            session.close()

    @scenario_test
    async def test_transfer_does_not_touch_other_place_or_unrelated_screens(self, h):
        own_place = insert_place(h, "Своё место")
        other_place = insert_place(h, "Чужое место")
        assign_place_admin(h, own_place, INITIATOR_TG)
        assign_place_admin(h, other_place, OTHER_TG)
        ensure_user(h, TARGET_TG, username="newadmin", full_name="Новый Админ")
        h.user_id = INITIATOR_TG

        transfer_id = await create_known_offer(h, own_place)
        accept = notification_callback(h, TARGET_TG, "adm_transfer_accept:")
        await h.callback(accept, user_id=TARGET_TG, chat_id=TARGET_TG)

        self.assertEqual(active_admin_tg(h, own_place), TARGET_TG)
        self.assertEqual(active_admin_tg(h, other_place), OTHER_TG)
        session = h.session_factory()
        try:
            self.assertFalse(is_place_admin(session, TARGET_TG, other_place))
        finally:
            session.close()
        self.assertEqual(transfer_row(h, transfer_id).status, "confirmed")

    @scenario_test
    async def test_second_place_admin_cannot_start_transfer_for_foreign_place(self, h):
        own_place = insert_place(h, "Своё место")
        foreign_place = insert_place(h, "Чужое место")
        assign_place_admin(h, own_place, INITIATOR_TG)
        assign_place_admin(h, foreign_place, OTHER_TG)
        h.user_id = INITIATOR_TG

        await h.text("/admin")
        await h.callback(screen_callback(h, "adm_place:"))
        await h.callback(f"adm_transfer:{own_place}")
        await h.text("@newadmin")
        await h.callback("adm_transfer_confirm")
        self.assertIsNotNone(pending_transfer(h, own_place))
        self.assertIsNone(pending_transfer(h, foreign_place))
        self.assertEqual(active_admin_tg(h, foreign_place), OTHER_TG)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
