# tests/test_stage20_admin_flow.py
"""
Stage 20A: интеграционные сценарии административного кабинета.

Все сценарии идут через настоящий Application.process_update, FakeBot и
временную SQLite. Проверяются:
- видимость кнопки кабинета в главном меню и отказ посторонним;
- кабинет суперадминистратора: список мест, карточка, назначение,
  замена и отзыв администратора;
- кабинет администратора места: только свои места, без полномочий;
- отказ по подделанным callback-данным и устаревшим кнопкам;
- экран «Я админ места»: объяснение без заявок и без выдачи прав;
- восстановление кабинета после рестарта процесса;
- трекинг известных пользователей при каждом Update.
"""

from __future__ import annotations

import unittest
from datetime import datetime as dt
from unittest.mock import patch

from sqlalchemy import select

from models import Place, PlaceAdmin, PlaceName, User
from place_admin_store import get_active_place_admin

try:
    from place_store import normalize_place_name
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(e)


SUPERADMIN_TG = 700100
TARGET_TG = 700200
TARGET2_TG = 700300
ORDINARY_TG = 700400

DENIED_ALERT = "У вас нет доступа к разделу администрирования."
SUPER_ONLY_ALERT = "Действие доступно только суперадминистратору."
STALE_ALERT = "Эта кнопка больше не работает. Откройте раздел администрирования заново."


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
):
    """Создаёт известного пользователя напрямую (без Telegram-обновления)."""
    from user_store import get_or_create_user

    session = h.session_factory()
    try:
        user = get_or_create_user(
            session,
            telegram_user_id,
            username=username,
            full_name=full_name,
        )
        return user.telegram_user_id
    finally:
        session.close()


def user_count(h) -> int:
    session = h.session_factory()
    try:
        return len(list(session.scalars(select(User)).all()))
    finally:
        session.close()


def place_admin_count(h, place_id: int) -> int:
    session = h.session_factory()
    try:
        return len(
            list(
                session.scalars(
                    select(PlaceAdmin).where(
                        PlaceAdmin.place_id == place_id,
                        PlaceAdmin.revoked_at.is_(None),
                    )
                ).all()
            )
        )
    finally:
        session.close()


def admin_snapshot(h, place_id: int) -> tuple[int, int] | None:
    """(telegram_user_id админа, назначивший) действующего админа места."""
    session = h.session_factory()
    try:
        active = get_active_place_admin(session, place_id)
        if active is None:
            return None
        return active.user.telegram_user_id, active.assignment_id
    finally:
        session.close()


def screen_callback(h, prefix: str) -> str:
    matches = [
        data for _, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {h.screen_buttons()!r}\n{h.screen_text()}"
        )
    return matches[0]


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
    # kwargs разворачиваются харнессом в саму запись вызова
    return record.get("text")


def patched_superadmin(*extra: int):
    """Роль суперадмина на время сценария (и для основного пользователя)."""
    return patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG, *extra))


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AdminMenuAccessTests(unittest.TestCase):
    """Вход в кабинет и видимость кнопки в главном меню."""

    @scenario_test
    async def test_main_menu_hides_admin_button_without_rights(self, h):
        await h.text("/start")
        labels = [label for label, _ in h.screen_buttons()]
        self.assertNotIn("🛡 Админка", labels)
        # Poster 2.0 S2: «Управление» доступно всем, но администрирования в нём
        # нет без выданных прав.
        await h.callback("manage")
        self.assertNotIn(("🛡 Администрирование", "admin_panel"), h.screen_buttons())

        # команда /admin без прав: экран отказа только с главным меню
        await h.text("/admin")
        self.assertIn("Раздел доступен только администраторам Poster.", h.screen_text())
        self.assertEqual(h.screen_buttons(), [("🏠 В меню", "main_menu")])

    @scenario_test
    async def test_admin_panel_callback_denied_does_not_change_screen(self, h):
        await h.text("/start")
        screens_before = len(h.screens())
        await h.callback("admin_panel")

        self.assertEqual(last_alert(h), DENIED_ALERT)
        # экран главного меню не заменяется отказом: данные экрана не менялись
        self.assertEqual(len(h.screens()), screens_before)

    @scenario_test
    async def test_admin_button_visible_for_superadmin_and_cabinet_opens(self, h):
        insert_place(h, "Кофейня «Локал»")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/start")
            # Poster 2.0 S2: административный вход — внутри «⚙️ Управление».
            await h.callback("manage")
            self.assertIn(("🛡 Администрирование", "admin_panel"), h.screen_buttons())

            await h.callback("admin_panel")
            self.assertIn("🛡 Все места", h.screen_text())
            self.assertIn("не назначен", h.screen_text())

    @scenario_test
    async def test_admin_button_visible_for_place_admin(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")

        session = h.session_factory()
        try:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            session.add(
                PlaceAdmin(place_id=place_id, user_id=user.id, assigned_at=dt.now()),
            )
            session.commit()
        finally:
            session.close()

        await h.text("/start")
        await h.callback("manage")
        self.assertIn(("🛡 Администрирование", "admin_panel"), h.screen_buttons())

    @scenario_test
    async def test_place_admin_opens_cabinet_with_own_place_only(self, h):
        own_place = insert_place(h, "Кофейня «Локал»")
        insert_place(h, "Театр «Этаж»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        session = h.session_factory()
        try:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            session.add(
                PlaceAdmin(
                    place_id=own_place,
                    user_id=user.id,
                    assigned_at=dt.now(),
                )
            )
            session.commit()
        finally:
            session.close()

        await h.text("/admin")
        text = h.screen_text()
        self.assertIn("🛡 Мои места", text)
        self.assertIn("Кофейня «Локал»", text)
        self.assertNotIn("Театр «Этаж»", text)
        self.assertEqual(screen_callback(h, "adm_place:"), f"adm_place:{own_place}")

        # карточка своего места: без кнопок назначения
        await h.callback(f"adm_place:{own_place}")
        labels = [label for label, _ in h.screen_buttons()]
        self.assertNotIn("👤 Назначить администратора", labels)
        self.assertNotIn("🔁 Заменить администратора", labels)
        self.assertNotIn("🚫 Снять администратора", labels)

    @scenario_test
    async def test_place_admin_cannot_open_other_place(self, h):
        own_place = insert_place(h, "Кофейня «Локал»")
        other_place = insert_place(h, "Театр «Этаж»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        session = h.session_factory()
        try:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            session.add(
                PlaceAdmin(
                    place_id=own_place,
                    user_id=user.id,
                    assigned_at=dt.now(),
                )
            )
            session.commit()
        finally:
            session.close()

        await h.text("/admin")
        answers_before = len(callback_answers(h))
        # подделанный callback на чужое место: отказ, экран не меняется
        await h.callback(f"adm_place:{other_place}")
        self.assertEqual(last_alert(h), "Место недоступно.")
        self.assertGreater(len(callback_answers(h)), answers_before)
        self.assertIn("🛡 Мои места", h.screen_text())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class SuperadminAssignFlowTests(unittest.TestCase):
    """Назначение администратора: поиск, подтверждение, аудит."""

    @scenario_test
    async def test_full_assign_flow_creates_active_row(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, TARGET_TG, username="target", full_name="Новый Человек")
        self.assertEqual(place_admin_count(h, place_id), 0)

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            self.assertIn(
                "👤 Назначить администратора",
                [label for label, _ in h.screen_buttons()],
            )

            await h.callback(f"adm_assign:{place_id}")
            self.assertIn("Поиск получателя", h.screen_text())
            self.assertIn("уже пользовался Poster.", h.screen_text())

            await h.text("@target")
            self.assertIn("Найдено: 1", h.screen_text())
            self.assertIn("Новый Человек (@target)", h.screen_text())

            await h.callback(screen_callback(h, "adm_pick:"))
            self.assertIn("Подтвердите назначение", h.screen_text())
            self.assertIn("Новый Человек (@target)", h.screen_text())

            await h.callback("adm_confirm")
            self.assertIn("Администратор назначен", h.screen_text())

        self.assertEqual(place_admin_count(h, place_id), 1)
        session = h.session_factory()
        try:
            row = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            )
            self.assertIsNotNone(row)
            self.assertIsNone(row.revoked_at)
            target = session.scalar(
                select(User).where(User.telegram_user_id == TARGET_TG)
            )
            self.assertEqual(row.user_id, target.id)
            self.assertIsNotNone(row.assigned_at)
            superadmin = session.scalar(
                select(User).where(User.telegram_user_id == SUPERADMIN_TG)
            )
            self.assertEqual(row.assigned_by_user_id, superadmin.id)
        finally:
            session.close()

    @scenario_test
    async def test_search_unknown_user_offers_no_assignment(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, TARGET_TG, username="target", full_name="Новый Человек")

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            await h.text("@ghost")
            self.assertIn("Poster пока не знает", h.screen_text())
            labels = [label for label, _ in h.screen_buttons()]
            self.assertNotIn("✅ Подтвердить", labels)
            self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_empty_search_shows_error_without_state_change(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            await h.text("   ")
            self.assertIn("Введите @username", h.screen_text())
            self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_assign_target_without_registration_is_refused(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            await h.text(str(TARGET2_TG))
            self.assertIn("Poster пока не знает", h.screen_text())
            self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_cancel_returns_to_place_card_without_changes(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            await h.callback("adm_cancel")
            self.assertIn("Кофейня «Локал»", h.screen_text())
            self.assertEqual(place_admin_count(h, place_id), 0)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class SuperadminReplaceRevokeTests(unittest.TestCase):
    """Замена и отзыв администратора: подтверждения и аудит."""

    def _assign_direct(self, h, place_id: int, telegram_user_id: int) -> None:
        session = h.session_factory()
        try:
            from place_admin_store import assign_place_admin

            with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
                result = assign_place_admin(
                    session,
                    actor_telegram_user_id=SUPERADMIN_TG,
                    place_id=place_id,
                    target_telegram_user_id=telegram_user_id,
                )
            self.assertEqual(result.status, "assigned")
        finally:
            session.close()

    def _replace_direct(self, h, place_id: int, expected_tg: int, new_tg: int) -> None:
        """Симулирует параллельную замену администратора между confirm'ами."""
        session = h.session_factory()
        try:
            from place_admin_store import replace_place_admin

            with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
                result = replace_place_admin(
                    session,
                    actor_telegram_user_id=SUPERADMIN_TG,
                    place_id=place_id,
                    target_telegram_user_id=new_tg,
                    expected_admin_telegram_user_id=expected_tg,
                )
            self.assertEqual(result.status, "replaced")
        finally:
            session.close()

    @scenario_test
    async def test_replace_flow_swaps_active_row(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, TARGET_TG, username="target", full_name="Первый Админ")
        ensure_user(h, TARGET2_TG, username="second", full_name="Второй Админ")
        self._assign_direct(h, place_id, TARGET_TG)

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            self.assertIn(
                "🔁 Заменить администратора", [label for label, _ in h.screen_buttons()]
            )
            self.assertIn("Первый Админ (@target)", h.screen_text())

            await h.callback(f"adm_replace:{place_id}")
            await h.text("@second")
            await h.callback(screen_callback(h, "adm_pick:"))
            self.assertIn("Подтвердите замену", h.screen_text())

            await h.callback("adm_confirm")
            self.assertIn("Администратор заменён", h.screen_text())

        session = h.session_factory()
        try:
            rows = list(
                session.scalars(
                    select(PlaceAdmin)
                    .where(PlaceAdmin.place_id == place_id)
                    .order_by(PlaceAdmin.id)
                ).all()
            )
            self.assertEqual(len(rows), 2)
            revoked = [row for row in rows if row.revoked_at is not None]
            active = [row for row in rows if row.revoked_at is None]
            self.assertEqual(len(revoked), 1)
            self.assertEqual(len(active), 1)
            target = session.scalar(
                select(User).where(User.telegram_user_id == TARGET_TG)
            )
            target2 = session.scalar(
                select(User).where(User.telegram_user_id == TARGET2_TG)
            )
            self.assertEqual(revoked[0].user_id, target.id)
            self.assertEqual(active[0].user_id, target2.id)
            self.assertIsNotNone(revoked[0].revoked_by_user_id)
        finally:
            session.close()

    @scenario_test
    async def test_revoke_flow_clears_admin(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, TARGET_TG, username="target", full_name="Первый Админ")
        self._assign_direct(h, place_id, TARGET_TG)

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            self.assertIn(
                "🚫 Снять администратора", [label for label, _ in h.screen_buttons()]
            )

            await h.callback(f"adm_revoke:{place_id}")
            self.assertIn("Подтвердите снятие", h.screen_text())

            await h.callback("adm_confirm")
            self.assertIn("Администратор снят", h.screen_text())
            self.assertIn("не назначен", h.screen_text())

        self.assertEqual(place_admin_count(h, place_id), 0)
        session = h.session_factory()
        try:
            row = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            )
            self.assertIsNotNone(row)
            self.assertIsNotNone(row.revoked_at)
        finally:
            session.close()

    @scenario_test
    async def test_confirmed_revoke_after_role_change_reports_changed(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, TARGET_TG, username="target", full_name="Первый Админ")
        ensure_user(h, TARGET2_TG, username="second", full_name="Второй Админ")
        self._assign_direct(h, place_id, TARGET_TG)

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_revoke:{place_id}")
            self.assertIn("Подтвердите снятие", h.screen_text())

            # параллельное изменение: роль перешла другому администратору
            self._replace_direct(h, place_id, TARGET_TG, TARGET2_TG)

            await h.callback("adm_confirm")
            self.assertIn("Данные изменились", h.screen_text())
            self.assertEqual(place_admin_count(h, place_id), 1)

        # аудит не содержит второй записи об отзыве несуществующей роли:
        # одна отозванная строка от замены, одна активная у нового админа
        session = h.session_factory()
        try:
            rows = list(
                session.scalars(
                    select(PlaceAdmin)
                    .where(PlaceAdmin.place_id == place_id)
                    .order_by(PlaceAdmin.id)
                ).all()
            )
            self.assertEqual(len(rows), 2)
            revoked = [row for row in rows if row.revoked_at is not None]
            active = [row for row in rows if row.revoked_at is None]
            self.assertEqual(len(revoked), 1)
            self.assertEqual(len(active), 1)
            target2 = session.scalar(
                select(User).where(User.telegram_user_id == TARGET2_TG)
            )
            self.assertEqual(active[0].user_id, target2.id)
        finally:
            session.close()


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ForgedCallbackTests(unittest.TestCase):
    """Подделанные callback-данные и устаревшие кнопки."""

    @scenario_test
    async def test_ordinary_user_forged_place_callback_gets_stale_alert(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        # cabinet ещё не открыт: подделка отвечает «устарела», прав не меняет
        await h.callback(f"adm_place:{place_id}")
        self.assertEqual(last_alert(h), STALE_ALERT)
        self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_ordinary_user_forged_assign_callback_gets_alert(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        # никакого кабинета: super-only фаза недоступна посторонним
        await h.callback(f"adm_assign:{place_id}")
        self.assertEqual(last_alert(h), DENIED_ALERT)
        self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_place_admin_assign_callback_denied(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        other_place = insert_place(h, "Театр «Этаж»")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        session = h.session_factory()
        try:
            from place_admin_store import assign_place_admin

            with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
                result = assign_place_admin(
                    session,
                    actor_telegram_user_id=SUPERADMIN_TG,
                    place_id=place_id,
                    target_telegram_user_id=h.user_id,
                )
            self.assertEqual(result.status, "assigned")
        finally:
            session.close()

        with patch("config.SUPERADMIN_USER_IDS", ()):
            await h.text("/admin")
            self.assertIn("🛡 Мои места", h.screen_text())
            # подделка назначения на своё место: только суперадмин
            await h.callback(f"adm_assign:{place_id}")
            self.assertEqual(last_alert(h), DENIED_ALERT)
            self.assertEqual(place_admin_count(h, place_id), 1)
            self.assertEqual(place_admin_count(h, other_place), 0)

    @scenario_test
    async def test_place_admin_pick_callback_denied(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        ensure_user(h, TARGET_TG, username="target", full_name="Новый Человек")
        ensure_user(h, SUPERADMIN_TG, username="boss", full_name="Супер Админ")
        session = h.session_factory()
        try:
            from place_admin_store import assign_place_admin

            with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
                result = assign_place_admin(
                    session,
                    actor_telegram_user_id=SUPERADMIN_TG,
                    place_id=place_id,
                    target_telegram_user_id=h.user_id,
                )
            self.assertEqual(result.status, "assigned")
        finally:
            session.close()

        with patch("config.SUPERADMIN_USER_IDS", ()):
            await h.text("/admin")
            await h.callback(f"adm_place:{place_id}")
            # фаза place, а не assign_search: adm_pick попадает в stale-обработчик
            await h.callback(f"adm_pick:{TARGET_TG}")
            self.assertEqual(last_alert(h), DENIED_ALERT)
            self.assertEqual(place_admin_count(h, place_id), 1)

    @scenario_test
    async def test_stale_confirm_callback_does_not_mutate(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        await h.callback("adm_confirm")
        self.assertEqual(last_alert(h), DENIED_ALERT)
        self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_main_menu_clears_admin_session_and_stale_callbacks_fail(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")

            await h.callback("main_menu")
            self.assertIn("Что хотите сделать?", h.screen_text())

            # после выхода из кабинета его кнопки становятся устаревшими
            await h.callback(f"adm_place:{place_id}")
            self.assertEqual(last_alert(h), STALE_ALERT)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class AdminRequestScreenTests(unittest.TestCase):
    """Экран «Я админ места»: объяснение, без заявок и выдачи прав."""

    @scenario_test
    async def test_admin_request_screen_for_ordinary_user(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")

        await h.text("/start")
        await h.callback("places")
        await h.callback(screen_callback(h, "pl_all"))
        await h.callback(screen_callback(h, "pl_open:"))
        # карточка подтверждения: кнопка «Я админ места» для постороннего
        await h.callback(screen_callback(h, "pl_admin_request:"))

        self.assertIn("🛡 Я администратор места", h.screen_text())
        self.assertIn("не отправляет заявку", h.screen_text())
        self.assertIn("Контакт администратора пока не указан", h.screen_text())
        # права не выданы и заявки нет
        self.assertEqual(place_admin_count(h, place_id), 0)

    @scenario_test
    async def test_admin_request_screen_shows_contact_link(self, h):
        insert_place(h, "Кофейня «Локал»")

        with patch("config.SUPERADMIN_CONTACT_URL", "https://t.me/poster_admin"):
            await h.text("/start")
            await h.callback("places")
            await h.callback(screen_callback(h, "pl_all"))
            await h.callback(screen_callback(h, "pl_open:"))
            await h.callback(screen_callback(h, "pl_admin_request:"))

            self.assertIn("Контакт суперадминистратора", h.screen_text())

    @scenario_test
    async def test_place_admin_sees_no_admin_request_button(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, h.user_id, username="placeadmin", full_name="Местный Админ")
        session = h.session_factory()
        try:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
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

        await h.text("/start")
        await h.callback("places")
        await h.callback(screen_callback(h, "pl_all"))
        await h.callback(screen_callback(h, "pl_open:"))
        labels = [label for label, _ in h.screen_buttons()]
        self.assertNotIn("🛡 Я администратор места", labels)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class UserTrackingTests(unittest.TestCase):
    """Регистрация известных пользователей при каждом Update."""

    @scenario_test
    async def test_start_registers_user(self, h):
        self.assertEqual(user_count(h), 0)
        await h.text("/start")
        self.assertGreaterEqual(user_count(h), 1)
        session = h.session_factory()
        try:
            user = session.scalar(
                select(User).where(User.telegram_user_id == h.user_id)
            )
            self.assertIsNotNone(user)
        finally:
            session.close()

    @scenario_test
    async def test_profile_updates_do_not_duplicate_users(self, h):
        await h.text("/start")
        before = user_count(h)
        # то же имя — дубликат не создаётся
        await h.text("/help")
        self.assertEqual(user_count(h), before)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class RestartRecoveryTests(unittest.TestCase):
    """Кабинет переживает рестарт процесса."""

    @scenario_test
    async def test_search_phase_survives_restart(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, TARGET_TG, username="target", full_name="Новый Человек")

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            self.assertIn("Поиск получателя", h.screen_text())

            await h.restart()
            # экран восстанавливается из БД, состояние фазы — тоже
            self.assertIn("Поиск получателя", h.screen_text())

            await h.text("@target")
            self.assertIn("Найдено: 1", h.screen_text())

            await h.callback(screen_callback(h, "adm_pick:"))
            self.assertIn("Подтвердите назначение", h.screen_text())
            await h.callback("adm_confirm")
            self.assertIn("Администратор назначен", h.screen_text())

        self.assertEqual(place_admin_count(h, place_id), 1)

    @scenario_test
    async def test_confirm_phase_survives_restart(self, h):
        place_id = insert_place(h, "Кофейня «Локал»")
        ensure_user(h, TARGET_TG, username="target", full_name="Новый Человек")

        with patched_superadmin():
            h.user_id = SUPERADMIN_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            await h.callback(f"adm_assign:{place_id}")
            await h.text("@target")
            await h.callback(screen_callback(h, "adm_pick:"))
            self.assertIn("Подтвердите назначение", h.screen_text())

            await h.restart()
            self.assertIn("Подтвердите назначение", h.screen_text())

            await h.callback("adm_confirm")
            self.assertIn("Администратор назначен", h.screen_text())

        self.assertEqual(place_admin_count(h, place_id), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
