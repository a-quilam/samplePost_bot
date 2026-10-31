# tests/test_poster2_r5_matrix_tester.py
"""
R5 (Poster 2.1) — независимая проверка новой работы: добор к
`test_poster2_r5_matrix.py` и фиксу плашки доставки уведомлений о конфликте.

Что закрывается здесь (и чего нет в наборе разработчика):

1. Негатив к самоназначению: обычный пользователь и админ ДРУГОГО места не
   могут выдать себе роль администратора подделанными callback'ами
   `adm_assign/adm_pick/adm_confirm`; строк `place_admins` не появляется.
2. Честность плашки (фикс `_notify_conflict_admins`) проверяется по тексту,
   специфичному для КОНФЛИКТА, а не по общему слову «не доставлено»:
   - полный сбой: «Уведомления администраторам мест не доставлены: N;
     задача видна в карточке места»;
   - смешанная доставка: «уведомлено: 1, не доставлено: 1»;
   - полный успех: «Администраторов мест уведомлено: N» без «не доставлены».
   Существующий тест разработчика проходит и при молчаливой плашке (его
   `assertIn("не доставлено")` удовлетворяет строка про автора), поэтому
   здесь нужен отдельный, специфичный assert.
3. IDOR в НОВЫХ сценариях R5 (сбой доставки, архивация носителя): админ
   другого места не открывает чужую задачу и не решает её подделанными
   `adm_bconflict*`; задача и местный кирпичик не меняются.

Все базы — временные SQLite; реальная `post_bot.db`, `.env` и секреты не
открываются; сеть Telegram заменена FakeBot, сценарии идут через настоящее
`Application.process_update`.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select

try:
    from brick_proposal_store import (
        PROPOSAL_STATUS_APPROVED,
        approve_brick_proposal,
        create_brick_proposal,
    )
    from brick_store import (
        CONFLICT_STATUS_OPEN,
        create_personal_brick,
        create_place_brick,
        list_pending_conflict_notifications,
    )
    from models import (
        Brick,
        BrickConflict,
        BrickConflictNotification,
        BrickProposal,
        PlaceAdmin,
        utcnow,
    )
    from place_store import create_place
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(exc)


SUPER_TG = 8_800_001
AUTHOR_TG = 8_800_002
OWNER_TG = 8_800_003
OTHER_ADMIN_TG = 8_800_004
ORDINARY_TG = 8_800_005

STALE_ALERT = "Эта кнопка больше не работает. Откройте раздел администрирования заново."
DENIED_ALERT = "У вас нет доступа к разделу администрирования."


def last_alert(h: Harness) -> str | None:
    answers = [c for c in h.calls if c["type"] == "callback_answer"]
    if not answers:
        return None
    args = answers[-1].get("args") or ()
    if args:
        return str(args[0])
    return answers[-1].get("text")


def buttons(h: Harness) -> list[tuple[str, str | None]]:
    return h.screen_buttons()


def callback_by_prefix(h: Harness, prefix: str) -> str:
    matches = [data for _, data in buttons(h) if data and data.startswith(prefix)]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {buttons(h)!r}\n{h.screen_text()}"
        )
    return matches[0]


def admin_rows(h: Harness, place_id: int) -> list[PlaceAdmin]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            ).all()
        )


def conflict_counters(h: Harness) -> tuple[int, int]:
    with h.session_factory() as session:
        conflicts = int(
            session.scalar(select(func.count()).select_from(BrickConflict)) or 0
        )
        notifications = int(
            session.scalar(select(func.count()).select_from(BrickConflictNotification))
            or 0
        )
    return conflicts, notifications


def plaque(h: Harness) -> str:
    """
    Только плашка-уведомление на экране (первая секция до карточки).

    Экран после решения начинается с итоговой плашки, затем идёт карточка
    предложения. Проверять честность доставки нужно по плашке, а не по всему
    тексту: в карточке нет слов про уведомления, но общая проверка «не
    доставлено» ловила бы и строку про автора.
    """
    return h.screen_text().split("\n\n", 1)[0]


def ensure_user(
    h: Harness, telegram_user_id: int, *, username: str, full_name: str
) -> int:
    """Регистрирует пользователя и возвращает его `id` (не ORM-объект)."""
    with h.session_factory() as session:
        user = get_or_create_user(
            session, telegram_user_id, username=username, full_name=full_name
        )
        session.commit()
        return int(user.id)


def make_place_with_admin(
    h: Harness,
    name: str,
    *,
    admin_tg: int,
    admin_username: str,
    code: str | None = None,
) -> tuple[int, int | None]:
    """Место + действующий админ (+ местный кирпичик, если задан код)."""
    with h.session_factory() as session:
        admin = get_or_create_user(
            session, admin_tg, username=admin_username, full_name="Админ места"
        )
        place = create_place(
            session, name, city="Тестовый город", created_by_user_id=admin.id
        )
        session.add(
            PlaceAdmin(place_id=place.id, user_id=admin.id, assigned_at=utcnow())
        )
        session.commit()
        local_id: int | None = None
        if code is not None:
            local = create_place_brick(
                session,
                actor_telegram_user_id=admin_tg,
                place_id=place.id,
                code=code,
                title=f"Местный носитель {name}",
                value_type="short_text",
                rules={},
            )
            local_id = int(local.id)
        return int(place.id), local_id


def make_global_proposal(h: Harness, code: str, *, title: str) -> int:
    with h.session_factory() as session:
        get_or_create_user(
            session, OWNER_TG, username="idea_owner", full_name="Автор идеи"
        )
        brick = create_personal_brick(
            session,
            actor_telegram_user_id=OWNER_TG,
            code=code,
            title=title,
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            session,
            actor_telegram_user_id=OWNER_TG,
            source_brick_id=brick.id,
            scope="system",
        )
        session.commit()
        return int(proposal.id)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class SelfAssignNegativeTests(unittest.TestCase):
    """Обычный пользователь и админ другого места не назначают себя сами."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @scenario_test
    async def test_ordinary_user_self_assign_is_denied_and_writes_nothing(
        self, h: Harness
    ) -> None:
        place_id, _ = make_place_with_admin(
            h, "Пустое место", admin_tg=SUPER_TG, admin_username="boss"
        )
        # Освобождаем место, чтобы кнопка «назначить» была доступна флоу.
        with h.session_factory() as session:
            row = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            )
            if row is not None:
                row.revoked_at = utcnow()
                session.commit()
        ensure_user(h, SUPER_TG, username="boss", full_name="Супер Админ")
        ensure_user(h, ORDINARY_TG, username="selfish", full_name="Обычный")
        self.assertEqual(
            [r for r in admin_rows(h, place_id) if r.revoked_at is None], []
        )

        # Роль суперадмина НЕ включает этого пользователя: его подделки
        # не должны опираться на чужую конфигурацию.
        with patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)):
            await h.text("/admin", user_id=ORDINARY_TG)
            await h.callback(f"adm_assign:{place_id}", user_id=ORDINARY_TG)
            self.assertEqual(last_alert(h), DENIED_ALERT)
            await h.callback(f"adm_pick:{ORDINARY_TG}", user_id=ORDINARY_TG)
            self.assertEqual(last_alert(h), DENIED_ALERT)
            await h.callback("adm_confirm", user_id=ORDINARY_TG)
            self.assertEqual(last_alert(h), DENIED_ALERT)

        self.assertEqual(
            [r for r in admin_rows(h, place_id) if r.revoked_at is None],
            [],
            "самоназначение обычного пользователя не создаёт роль",
        )

    @scenario_test
    async def test_place_admin_of_other_place_cannot_self_assign(
        self, h: Harness
    ) -> None:
        own_place, _ = make_place_with_admin(
            h, "Своё место", admin_tg=OTHER_ADMIN_TG, admin_username="other"
        )
        foreign_place, _ = make_place_with_admin(
            h, "Чужое место", admin_tg=SUPER_TG, admin_username="boss2"
        )
        with h.session_factory() as session:
            row = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.place_id == foreign_place)
            )
            if row is not None:
                row.revoked_at = utcnow()
                session.commit()
        ensure_user(h, SUPER_TG, username="boss2", full_name="Супер Админ")

        await h.text("/admin", user_id=OTHER_ADMIN_TG)
        self.assertIn("🛡 Мои места", h.screen_text())
        await h.callback(f"adm_place:{foreign_place}", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), "Место недоступно.")
        await h.callback(f"adm_assign:{foreign_place}", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), DENIED_ALERT)
        await h.callback(f"adm_pick:{OTHER_ADMIN_TG}", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), DENIED_ALERT)
        await h.callback("adm_confirm", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), DENIED_ALERT)

        active = [r for r in admin_rows(h, foreign_place) if r.revoked_at is None]
        self.assertEqual(active, [], "роль на чужое место не выдана")
        own_active = [r for r in admin_rows(h, own_place) if r.revoked_at is None]
        self.assertEqual(len(own_active), 1, "своя роль не затронута")
        self.assertEqual(
            own_active[0].user_id,
            ensure_user(h, OTHER_ADMIN_TG, username="other", full_name="Админ места"),
            "своя роль принадлежит этому же пользователю",
        )


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConflictPlaqueHonestyTests(unittest.TestCase):
    """
    Плашка после одобрения глобального предложения честно описывает доставку.

    Проверяется именно фрагмент про АДМИНИСТРАТОРОВ МЕСТ, а не общее слово
    «не доставлено» (оно же есть в строке про автора предложения).
    """

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _open_approve_confirm(self, h: Harness, proposal_id: int) -> None:
        ensure_user(h, SUPER_TG, username="boss_r5", full_name="Супер Админ")
        await h.text("/admin", user_id=SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        self.assertIn("Одобрить предложение кирпичика", h.screen_text())
        self.assertIn(
            f"adm_bprop_approve_yes:{proposal_id}",
            [data for _, data in buttons(h)],
        )

    @scenario_test
    async def test_all_failed_plaque_is_specific_to_conflict_admins(
        self, h: Harness
    ) -> None:
        code = "плашка_сбой_р5"
        place_id, _ = make_place_with_admin(
            h, "Зал сбоя", admin_tg=AUTHOR_TG, admin_username="author_r5", code=code
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный")
        await self._open_approve_confirm(h, proposal_id)

        h.bot.fail_types.add("message")
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        notice = plaque(h)
        self.assertIn("Предложение одобрено", notice)
        self.assertIn(
            "Не доставлено уведомлений администраторам мест: 1",
            notice,
            "плашка обязана назвать число НЕдоставленных уведомлений о конфликте",
        )
        self.assertIn("Задача доступна в карточке места", notice)
        self.assertNotIn("уведомлено", notice.lower(), "нельзя писать «уведомлено»")
        self.assertNotIn("поставлены в очередь", notice)
        # Решение сохранено, задача и её адресат не потеряны.
        with h.session_factory() as session:
            conflicts = list(session.scalars(select(BrickConflict)).all())
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].place_id, place_id)
            self.assertEqual(conflicts[0].status, CONFLICT_STATUS_OPEN)

    @scenario_test
    async def test_partial_delivery_plaque_counts_both_sides(self, h: Harness) -> None:
        code = "плашка_микс_р5"
        make_place_with_admin(
            h, "Зал A", admin_tg=AUTHOR_TG, admin_username="author_a", code=code
        )
        make_place_with_admin(
            h, "Зал B", admin_tg=OTHER_ADMIN_TG, admin_username="author_b", code=code
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный микс")
        await self._open_approve_confirm(h, proposal_id)

        # Порядок отправки на подтверждении: автору предложения, затем
        # уведомления по задачам (A, B). Разрешаем 2 успешных message:
        # автор и админ A доставлены, админ B — сбой.
        h.bot.fail_after["message"] = 2
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        notice = plaque(h)
        self.assertIn("Предложение одобрено", notice)
        self.assertIn(
            "Уведомлено администраторов мест: 1. Не доставлено уведомлений: 1",
            notice,
            "при смешанной доставке плашка обязана назвать оба числа",
        )
        with h.session_factory() as session:
            statuses = {
                row.telegram_user_id: (row.status, row.attempts)
                for row in session.scalars(select(BrickConflictNotification)).all()
            }
        self.assertEqual(statuses[AUTHOR_TG], ("sent", 1))
        self.assertEqual(statuses[OTHER_ADMIN_TG], ("failed", 1))

    @scenario_test
    async def test_successful_delivery_plaque_says_notified_only(
        self, h: Harness
    ) -> None:
        code = "плашка_успех_р5"
        make_place_with_admin(
            h, "Зал успеха", admin_tg=AUTHOR_TG, admin_username="author_ok", code=code
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный успех")
        await self._open_approve_confirm(h, proposal_id)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        notice = plaque(h)
        self.assertIn("Уведомлено администраторов мест: 1", notice)
        self.assertNotIn("не доставлено", notice)
        self.assertNotIn("не доставлены", notice)
        with h.session_factory() as session:
            notification = session.scalar(select(BrickConflictNotification))
            assert notification is not None
            self.assertEqual(notification.status, "sent")
            self.assertEqual(notification.attempts, 1)

    @scenario_test
    async def test_place_without_admin_plaque_says_queued_and_keeps_task(
        self, h: Harness
    ) -> None:
        """Место без администратора: получателей нет — плашка про очередь."""
        code = "плашка_безадмина_р5"
        ensure_user(h, SUPER_TG, username="boss_noadmin", full_name="Супер Админ")
        with h.session_factory() as session:
            owner = get_or_create_user(
                session, OWNER_TG, username="owner_noadmin", full_name="Автор"
            )
            place = create_place(
                session,
                "Зал без админа",
                city="Тестовый город",
                created_by_user_id=owner.id,
            )
            session.commit()
            place_id = int(place.id)
        # Кирпичик места создаёт суперадмин: админа у места нет.
        with h.session_factory() as session:
            create_place_brick(
                session,
                actor_telegram_user_id=SUPER_TG,
                place_id=place_id,
                code=code,
                title="Местный без админа",
                value_type="short_text",
                rules={},
            )
        proposal_id = make_global_proposal(h, code, title="Глобальный без админа")
        await self._open_approve_confirm(h, proposal_id)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        notice = plaque(h)
        self.assertIn("ожидают отправки. Если", notice)
        self.assertNotIn("Администраторов мест уведомлено", notice)
        self.assertNotIn("не доставлены", notice)
        with h.session_factory() as session:
            # Очередь пуста (получателей нет), но задача создана и видна.
            self.assertEqual(list_pending_conflict_notifications(session), [])
            conflict = session.scalar(select(BrickConflict))
            assert conflict is not None
            self.assertEqual(conflict.place_id, place_id)
            self.assertEqual(conflict.status, CONFLICT_STATUS_OPEN)

    @scenario_test
    async def test_repeat_approval_reports_already_decided_without_new_plaque(
        self, h: Harness
    ) -> None:
        code = "плашка_повтор_р5"
        make_place_with_admin(
            h, "Зал повтора", admin_tg=AUTHOR_TG, admin_username="author_rep", code=code
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный повтор")
        await self._open_approve_confirm(h, proposal_id)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        before = conflict_counters(h)

        # Повторное подтверждение из уже отрисованной карточки: фаза стала
        # proposal_card, поэтому кнопка считается устаревшей и решения не
        # применяет; новых задач и попыток доставки нет.
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        self.assertEqual(last_alert(h), STALE_ALERT)
        self.assertEqual(
            conflict_counters(h), before, "повтор не плодит задачи/очередь"
        )
        with h.session_factory() as session:
            notification = session.scalar(select(BrickConflictNotification))
            assert notification is not None
            self.assertEqual(notification.attempts, 1, "повторной доставки нет")
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            self.assertEqual(proposal.status, PROPOSAL_STATUS_APPROVED)
            # Сервисный повтор из другого окна — идемпотентный отказ.
            result = approve_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=proposal_id,
            )
        self.assertEqual(result.status, "already_decided")
        self.assertIn("уже одобрено", (result.message or "").lower())
        self.assertEqual(conflict_counters(h), before)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class R5ScenarioIdorTests(unittest.TestCase):
    """Чужие подделанные `adm_bconflict*` не видят и не решают задачу R5."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def _outsider_cabinet(self, h: Harness) -> int:
        own_place, _ = make_place_with_admin(
            h, "Своё место IDOR", admin_tg=OTHER_ADMIN_TG, admin_username="intruder"
        )
        await h.text("/admin", user_id=OTHER_ADMIN_TG)
        await h.callback(f"adm_place:{own_place}", user_id=OTHER_ADMIN_TG)
        self.assertIn("🛡 Место", h.screen_text())
        return own_place

    @scenario_test
    async def test_outsider_cannot_resolve_task_created_by_failed_delivery(
        self, h: Harness
    ) -> None:
        code = "idor_сбой_р5"
        place_id, local_id = make_place_with_admin(
            h, "Зал IDOR", admin_tg=AUTHOR_TG, admin_username="author_idor", code=code
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный IDOR")
        ensure_user(h, SUPER_TG, username="boss_idor", full_name="Супер Админ")
        await h.text("/admin", user_id=SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        h.bot.fail_types.add("message")
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        h.bot.fail_types.discard("message")
        with h.session_factory() as session:
            conflict = session.scalar(select(BrickConflict))
            assert conflict is not None
            conflict_id = int(conflict.id)
            self.assertEqual(conflict.status, CONFLICT_STATUS_OPEN)

        await self._outsider_cabinet(h)
        # Чужая карточка места и чужие задачи не открываются.
        await h.callback(f"adm_bconflicts:{place_id}", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), STALE_ALERT)
        # После stale экран остаётся карточкой СВОЕГО места: входим в свой
        # список конфликтов и подделываем там чужой id задачи.
        own_place = int(
            next(
                data.split(":", 1)[1]
                for _label, data in buttons(h)
                if data and data.startswith("adm_bconflicts:")
            )
        )
        await h.callback(f"adm_bconflicts:{own_place}", user_id=OTHER_ADMIN_TG)
        self.assertIn("Конфликты кодов", h.screen_text())
        self.assertIn("Нерешённых задач нет.", h.screen_text())
        answers_before = len([c for c in h.calls if c["type"] == "callback_answer"])
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=OTHER_ADMIN_TG)
        self.assertGreater(
            len([c for c in h.calls if c["type"] == "callback_answer"]),
            answers_before,
            "подделка получает явный ответ",
        )
        self.assertEqual(last_alert(h), "Задача недоступна.")
        self.assertNotIn("Код занят двумя кирпичиками", h.screen_text())
        # Поддельные решения без нужной фазы — устаревшие кнопки.
        await h.callback(
            f"adm_bconflict_switch_yes:{conflict_id}", user_id=OTHER_ADMIN_TG
        )
        self.assertEqual(last_alert(h), STALE_ALERT)
        await h.callback(f"adm_bconflict_rename:{conflict_id}", user_id=OTHER_ADMIN_TG)
        self.assertEqual(last_alert(h), STALE_ALERT)

        with h.session_factory() as session:
            conflict = session.get(BrickConflict, conflict_id)
            assert conflict is not None
            self.assertEqual(conflict.status, CONFLICT_STATUS_OPEN)
            assert local_id is not None
            local = session.get(Brick, int(local_id))
            assert local is not None
            self.assertEqual(local.code, code)
            self.assertEqual(local.status, "active")
            # Ни одного уведомления постороннему не создано.
            recipients = {
                row.telegram_user_id
                for row in session.scalars(select(BrickConflictNotification)).all()
            }
        self.assertEqual(recipients, {AUTHOR_TG})

    @scenario_test
    async def test_task_stays_resolvable_by_its_own_admin_after_idor_attempt(
        self, h: Harness
    ) -> None:
        code = "idor_после_р5"
        place_id, local_id = make_place_with_admin(
            h,
            "Зал после IDOR",
            admin_tg=AUTHOR_TG,
            admin_username="author_ok2",
            code=code,
        )
        proposal_id = make_global_proposal(h, code, title="Глобальный после IDOR")
        ensure_user(h, SUPER_TG, username="boss_ok2", full_name="Супер Админ")
        await h.text("/admin", user_id=SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)
        with h.session_factory() as session:
            conflict = session.scalar(select(BrickConflict))
            assert conflict is not None
            conflict_id = int(conflict.id)
            self.assertEqual(conflict.status, CONFLICT_STATUS_OPEN)

        # Чужой пытается решить задачу — и терпит неудачу.
        await self._outsider_cabinet(h)
        await h.callback(
            f"adm_bconflict_switch_yes:{conflict_id}", user_id=OTHER_ADMIN_TG
        )
        self.assertEqual(last_alert(h), STALE_ALERT)

        # Настоящий админ места по-прежнему видит и решает задачу.
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Нерешённых конфликтов кодов: 1", h.screen_text())
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Код занят двумя кирпичиками", h.screen_text())
        await h.callback(f"adm_bconflict_switch:{conflict_id}", user_id=AUTHOR_TG)
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("задача закрыта", h.screen_text().lower())
        with h.session_factory() as session:
            conflict = session.get(BrickConflict, conflict_id)
            assert conflict is not None
            self.assertEqual(conflict.status, "switched")
            assert local_id is not None
            local = session.get(Brick, int(local_id))
            assert local is not None
            self.assertEqual(local.status, "active", "местный носитель не удалён")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
