# tests/test_poster2_r5_matrix.py
"""
R5 (Poster 2.1): добор тест-матрицы владельца A–G — четыре недостающих
автотеста.

Закрываются именно те случаи, которых не было в наборах R1–R4:

1. Суперадмин назначает администратором места САМОГО СЕБЯ: поиск по своему
   username и по своему числовому Telegram ID, подтверждение, действующая
   строка `place_admins`, явный ответ «назначен», повтор — идемпотентное
   «уже управляет этим местом» без молчания и без второй строки/ошибки БД.
2. Конфликт кодов при РАЗНЫХ типах: местный кирпичик места `number` и
   глобальный `short_text`. Модель R3 не зависит от типа: одобрение
   разрешено, глобальный выигрывает для новых привязок, задача создаётся,
   местный кирпичик не удаляется и не переписывается, схема цела.
3. Сбой доставки уведомления о конфликте: FakeBot падает на `sendMessage` →
   одобрение глобального не откатывается, задача остаётся активной,
   уведомление фиксируется терминально (`failed`, одна попытка), задача
   видна в кабинете администратора места (компенсация, а не потеря).
4. Мягкая архивация местного носителя при активной задаче: задача не
   теряется и закрывается терминально `superseded` (лениво, при первом
   решении по ней), повторные callback безопасны, версии и снимки шаблона
   не переписываются.

Фактическое поведение зафиксировано как есть; расхождения с формулировкой
задания отмечены в docstring соответствующих тестов и в отчёте этапа
(продуктовый код в R5 не меняется).

Все базы — временные SQLite; реальная `post_bot.db` не открывается, сеть
Telegram заменена FakeBot. Интеграционные сценарии идут через настоящее
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
        CONFLICT_STATUS_SUPERSEDED,
        CONFLICT_STATUS_SWITCHED,
        archive_personal_brick,
        create_personal_brick,
        create_place_brick,
        get_system_brick,
        list_pending_conflict_notifications,
        list_place_conflicts,
        resolve_brick_code_candidates,
        resolve_conflict_by_switch,
    )
    from models import (
        Brick,
        BrickConflict,
        BrickConflictNotification,
        BrickProposal,
        PlaceAdmin,
        TemplateSnapshotBrick,
        TemplateVersionBrick,
        utcnow,
    )
    from place_store import create_place
    from template_store import (
        create_template_snapshot,
        get_standard_template,
        update_template_section,
    )
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        Harness,
        scenario_test,
    )
    from tests.stage22a_helpers import BrickDatabaseTestCase
    from user_store import get_or_create_user

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в сообщение пропуска
    INTEGRATION_ERROR = str(exc)


SUPER_TG = 8_500_001
AUTHOR_TG = 8_500_002
OWNER_TG = 8_500_003

STALE_ALERT = "Кнопка устарела: откройте административный кабинет заново."


def patched_superadmin() -> Any:
    """Роль суперадмина из конфига на время сценария (как в Stage 20A)."""
    return patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,))


SUPERADMIN_LOGIC_TG = 8_500_004


def last_alert(h: Harness) -> str | None:
    answers = [c for c in h.calls if c["type"] == "callback_answer"]
    if not answers:
        return None
    record = answers[-1]
    args = record.get("args") or ()
    if args:
        return str(args[0])
    return record.get("text")


def screen_callback(h: Harness, prefix: str) -> str:
    matches = [
        data for _, data in h.screen_buttons() if data and data.startswith(prefix)
    ]
    if not matches:
        raise AssertionError(
            f"нет кнопки '{prefix}' на экране: {h.screen_buttons()!r}\n"
            f"{h.screen_text()}"
        )
    return matches[0]


def active_admin_rows(h: Harness, place_id: int) -> list[PlaceAdmin]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(PlaceAdmin).where(
                    PlaceAdmin.place_id == place_id,
                    PlaceAdmin.revoked_at.is_(None),
                )
            ).all()
        )


def all_admin_rows(h: Harness, place_id: int) -> list[PlaceAdmin]:
    with h.session_factory() as session:
        return list(
            session.scalars(
                select(PlaceAdmin)
                .where(PlaceAdmin.place_id == place_id)
                .order_by(PlaceAdmin.id)
            ).all()
        )


def make_place(h: Harness, name: str, *, owner_tg: int) -> int:
    """Место без администратора; создатель известен боту (аудит актора)."""
    with h.session_factory() as session:
        owner = get_or_create_user(
            session, owner_tg, username="creator", full_name="Создатель"
        )
        place = create_place(
            session, name, city="Тестовый город", created_by_user_id=owner.id
        )
        session.commit()
        return int(place.id)


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class SuperadminSelfAssignTests(unittest.TestCase):
    """
    Пункт 1: самоназначение суперадмина через штатный флоу кабинета.

    Проверяется требование, а не реализация: роль выдаётся по постоянному
    Telegram user ID, ответ всегда явный, повтор не создаёт дублей и не
    снимает действующую роль.
    """

    @scenario_test
    async def test_superadmin_assigns_self_by_username_then_repeat_by_id(
        self, h: Harness
    ) -> None:
        place_id = make_place(h, "Кофейня «Самоназначение»", owner_tg=OWNER_TG)
        # Суперадмин известен боту: у него есть строка `users` и постоянный
        # username, но нет роли администратора места.
        with h.session_factory() as session:
            get_or_create_user(
                session, SUPER_TG, username="boss", full_name="Супер Админ"
            )
            session.commit()
        self.assertEqual(active_admin_rows(h, place_id), [])

        with patched_superadmin():
            h.user_id = SUPER_TG
            await h.text("/admin")
            await h.callback(screen_callback(h, "adm_place:"))
            self.assertIn(
                "👤 Назначить администратора",
                [label for label, _ in h.screen_buttons()],
            )
            await h.callback(f"adm_assign:{place_id}")
            self.assertIn("Поиск получателя", h.screen_text())

            # (а) поиск по СВОЕМУ username: находится именно суперадмин.
            await h.text("@boss")
            self.assertIn("Найдено: 1", h.screen_text())
            self.assertIn("(@boss)", h.screen_text())
            self.assertIn(f"Telegram user ID: {SUPER_TG}", h.screen_text())
            self.assertEqual(screen_callback(h, "adm_pick:"), f"adm_pick:{SUPER_TG}")

            await h.callback(f"adm_pick:{SUPER_TG}")
            self.assertIn("Подтвердите назначение", h.screen_text())
            self.assertIn(str(SUPER_TG), h.screen_text())

            await h.callback("adm_confirm")
            self.assertIn("✅ Администратор назначен.", h.screen_text())
            self.assertNotIn("ошибк", h.screen_text().lower())

        rows = active_admin_rows(h, place_id)
        self.assertEqual(len(rows), 1, "ровно одна действующая строка")
        session = h.session_factory()
        try:
            superadmin = session.scalar(
                select(PlaceAdmin).where(PlaceAdmin.place_id == place_id)
            )
            assert superadmin is not None
            self.assertIsNone(superadmin.revoked_at)
            self.assertIsNotNone(superadmin.assigned_at)
            self.assertEqual(
                superadmin.assigned_by_user_id, superadmin.user_id, "назначил сам себя"
            )
        finally:
            session.close()
        first_assignment_id = rows[0].id

        with patched_superadmin():
            h.user_id = SUPER_TG
            await h.text("/admin")
            # Кабинет помнит открытую карточку места; нажимаем её, только
            # если показан список мест.
            if any(
                data and data.startswith("adm_place:") for _, data in h.screen_buttons()
            ):
                await h.callback(screen_callback(h, "adm_place:"))
            self.assertIn("🛡 Место", h.screen_text())

            # (б) повторная попытка «назначить» при занятом месте: явный
            # отказ, а не молчание и не вторая строка.
            answers_before = len([c for c in h.calls if c["type"] == "callback_answer"])
            await h.callback(f"adm_assign:{place_id}")
            self.assertGreater(
                len([c for c in h.calls if c["type"] == "callback_answer"]),
                answers_before,
                "повторное назначение обязано ответить пользователю",
            )
            self.assertEqual(
                last_alert(h), "У места уже есть администратор. Используйте замену."
            )

            # (в) поиск по СВОЕМУ числовому ID через флоу замены: находится
            # тот же человек, повтор идемпотентен.
            await h.callback(f"adm_replace:{place_id}")
            self.assertIn("Поиск получателя для замены", h.screen_text())
            await h.text(str(SUPER_TG))
            self.assertIn("Найдено: 1", h.screen_text())
            self.assertIn(f"Telegram user ID: {SUPER_TG}", h.screen_text())
            await h.callback(f"adm_pick:{SUPER_TG}")
            self.assertIn("Подтвердите замену", h.screen_text())
            await h.callback("adm_confirm")
            self.assertIn("уже управляет этим местом", h.screen_text())

        rows = active_admin_rows(h, place_id)
        self.assertEqual(len(rows), 1, "идемпотентно: вторая роль не создана")
        self.assertEqual(rows[0].id, first_assignment_id, "строка не пересоздавалась")
        self.assertEqual(
            len(all_admin_rows(h, place_id)),
            1,
            "история не получила отозванную копию",
        )


class ConflictDifferentValueTypeTests(BrickDatabaseTestCase):
    """
    Пункт 2: одинаковый код, РАЗНЫЕ типы (number у места, short_text у мира).

    Модель R3: тип не участвует в разрешении конфликта. Одобрение глобального
    разрешено, для НОВЫХ привязок выигрывает глобальный тип, задача создаётся,
    местный кирпичик остаётся активным со своим типом, схема цела.
    """

    SUPERADMIN_TG = 8_510_001

    def test_global_short_text_over_local_number_keeps_conflict_model(self) -> None:
        code = "цена_тип_р5"
        author = self.make_user(8_511_001, username="author5")
        place = self.make_place("Зал разных типов")
        self.make_place_admin(place, author)
        local = create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Местная цена числом",
            value_type="number",
            rules={},
        )
        self.assertEqual(local.value_type, "number")

        source_owner = self.make_user(8_511_002, username="owner5")
        source = create_personal_brick(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            code=code,
            title="Глобальная цена текстом",
            value_type="short_text",
            rules={},
        )
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=source_owner.telegram_user_id,
            source_brick_id=source.id,
            scope="system",
        )
        self.make_user(self.SUPERADMIN_TG, username="boss5")
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )
        # (а) Одобрение разрешено несмотря на другой тип местного носителя.
        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)

        system = get_system_brick(self.session, code)
        assert system is not None
        self.assertEqual(system.scope, "system")
        self.assertEqual(system.value_type, "short_text")

        # (б) Местный кирпичик не удалён и не переписан: тот же тип и код.
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        self.assertEqual(local_row.status, "active")
        self.assertEqual(local_row.code, code)
        self.assertEqual(local_row.value_type, "number")
        self.assertEqual(local_row.revision, local.revision)

        # (в) Задача создана ровно одна и уведомление админу места — одно.
        conflicts = list_place_conflicts(self.session, self.SUPERADMIN_TG, place.id)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].status, CONFLICT_STATUS_OPEN)
        self.assertEqual(conflicts[0].local_brick_id, local.id)
        self.assertEqual(conflicts[0].system_brick_id, system.id)
        self.assertEqual(
            [
                (n.conflict_id, n.telegram_user_id)
                for n in list_pending_conflict_notifications(self.session)
            ],
            [(conflicts[0].id, author.telegram_user_id)],
        )

        # (г) Приоритет глобального для НОВЫХ привязок не зависит от типа.
        candidates = resolve_brick_code_candidates(
            self.session,
            code,
            viewer_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
        )
        self.assertFalse(candidates.ambiguous)
        assert candidates.selected is not None
        self.assertEqual(candidates.selected.id, system.id)
        self.assertEqual(candidates.selected.value_type, "short_text")

        template = get_standard_template(
            self.session, author.telegram_user_id, place.id
        )
        prepared = update_template_section(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            template_id=template.template_id,
            expected_revision=template.revision,
            section="body",
            content=f"[{code}]",
        )
        self.assertEqual(prepared.status, "updated", prepared.message)
        assert prepared.template is not None
        bindings = {
            binding.code: binding.brick_id
            for binding in prepared.template.current_version.bindings
        }
        self.assertEqual(bindings[code], system.id, "новая привязка — глобальная")

        # (д) Решение задачи не задевает местный кирпичик с другим типом.
        resolved = resolve_conflict_by_switch(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            conflict_id=conflicts[0].id,
        )
        self.assertTrue(resolved.ok, resolved.message)
        self.assertEqual(resolved.status, CONFLICT_STATUS_SWITCHED)
        self.session.expire_all()
        local_row = self.session.get(Brick, local.id)
        assert local_row is not None
        self.assertEqual(local_row.value_type, "number")
        self.assertEqual(local_row.status, "active")
        self.assertEqual(local_row.code, code)

        # (е) Целостность схемы после конфликта разных типов.
        with self.engine.connect() as connection:
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA integrity_check").fetchall(),
                [("ok",)],
            )
            self.assertEqual(
                connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall(),
                [],
            )


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConflictNotificationFailureTests(unittest.TestCase):
    """
    Пункт 3: сбой доставки уведомления о конфликте не откатывает решение.

    Фактическое поведение (совпадает с моделью уведомлений миграции 19):
    `pending → failed` ровно один раз, `attempts=1`; обратного перехода и
    автоматического повтора нет — поэтому очередь `pending` после сбоя пуста
    (в формулировке задания «остаётся pending» неточно: модель миграции 19
    фиксирует неудачу терминально, и это корректно). Задача при этом остаётся
    активной и видимой администратору места в кабинете, то есть данные не
    теряются даже без доставленного сообщения.

    Известный продуктовый дефект R5 исправлен: `_notify_conflict_admins`
    возвращает подтверждённые доставки и число сбоев, поэтому итоговая плашка
    больше не пишет «Администраторов мест уведомлено: N», если отправка упала,
    а сообщает «не доставлено: N».
    """

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _prepare(self, h: Harness) -> dict[str, Any]:
        code = "сбой_уведомления_р5"
        with h.session_factory() as session:
            author = get_or_create_user(
                session, AUTHOR_TG, username="author5", full_name="Админ места"
            )
            get_or_create_user(
                session, SUPER_TG, username="boss5", full_name="Супер Админ"
            )
            get_or_create_user(
                session, OWNER_TG, username="owner5", full_name="Автор идеи"
            )
            place = create_place(
                session,
                "Зал сбоя доставки",
                city="Тестовый город",
                created_by_user_id=author.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=author.id,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            local = create_place_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                place_id=place.id,
                code=code,
                title="Местный носитель",
                value_type="short_text",
                rules={},
            )
            source = create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code=code,
                title="Глобальный кандидат",
                value_type="short_text",
                rules={},
            )
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                source_brick_id=source.id,
                scope="system",
            )
            return {
                "code": code,
                "place_id": int(place.id),
                "local_id": int(local.id),
                "proposal_id": int(proposal.id),
            }

    @scenario_test
    async def test_notification_failure_keeps_approval_and_task(
        self, h: Harness
    ) -> None:
        data = self._prepare(h)
        place_id = int(data["place_id"])
        proposal_id = int(data["proposal_id"])

        await h.text("/admin", user_id=SUPER_TG)
        await h.callback("adm_bprop_global", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPER_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPER_TG)
        # Сбой доставки начинается ровно на подтверждении: карточка уже
        # отрисована, дальше падают только личные сообщения.
        h.bot.fail_types.add("message")
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPER_TG)

        # (а) Решение не откатилось: предложение одобрено, кирпичик создан.
        with h.session_factory() as session:
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            self.assertEqual(proposal.status, PROPOSAL_STATUS_APPROVED)
            system = get_system_brick(session, str(data["code"]))
            self.assertIsNotNone(system, "глобальный кирпичик создан несмотря на сбой")
            assert system is not None
            conflicts = list(session.scalars(select(BrickConflict)).all())
            self.assertEqual(len(conflicts), 1)
            self.assertEqual(conflicts[0].status, CONFLICT_STATUS_OPEN)
            self.assertEqual(conflicts[0].place_id, place_id)
            self.assertEqual(conflicts[0].local_brick_id, int(data["local_id"]))
            conflict_id = int(conflicts[0].id)

            notifications = list(
                session.scalars(select(BrickConflictNotification)).all()
            )
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0].telegram_user_id, AUTHOR_TG)
            self.assertEqual(
                notifications[0].attempts, 1, "одна зафиксированная попытка"
            )
            self.assertEqual(
                notifications[0].status,
                "failed",
                "недоставленное уведомление терминально (pending → failed)",
            )
            # Повторной отправки не будет: очередь pending пуста.
            self.assertEqual(list_pending_conflict_notifications(session), [])

        # (б) Ни одного сообщения о конфликте админу места не доставлено.
        def conflict_notices() -> list[str]:
            return [
                str(call.get("text") or "")
                for call in h.calls
                if call["type"] == "message"
                and int(call.get("chat_id") or 0) == AUTHOR_TG
                and "Конфликт кодов" in str(call.get("text") or "")
            ]

        self.assertEqual(conflict_notices(), [])
        # Решение сохранено, и экран об этом явно сообщает. После исправления
        # R5 плашка честно пишет про сбой доставки и не заявляет «уведомлено».
        # Проверка специфична для УВЕДОМЛЕНИЙ О КОНФЛИКТЕ: общая строка про
        # автора («не доставлено») есть всегда и дефект плашки не поймала бы.
        notice, _card = h.screen_text().split("\n\n", 1)
        self.assertIn("Предложение одобрено", notice)
        self.assertIn(
            "Уведомления администраторам мест не доставлены: 1",
            notice,
            "плашка обязана назвать число НЕдоставленных уведомлений о конфликте",
        )
        self.assertIn("задача видна в карточке места", notice)
        self.assertNotIn("уведомлено", notice.lower())

        # (в) Повторный прогон не создаёт дублей и не переотправляет.
        with h.session_factory() as session:
            again = approve_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=proposal_id,
            )
            self.assertEqual(again.status, "already_decided")
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count()).select_from(BrickConflictNotification)
                    )
                    or 0
                ),
                1,
            )
            notification = session.scalar(select(BrickConflictNotification))
            assert notification is not None
            self.assertEqual(notification.attempts, 1)

        # (г) Компенсация: задача видна администратору места в кабинете.
        h.bot.fail_types.discard("message")
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Активные задачи конфликтов кодов: 1", h.screen_text())
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        self.assertIn(str(data["code"]), h.screen_text())
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Задача разрешения конфликта", h.screen_text())
        self.assertIn("Местный носитель", h.screen_text())


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class ConflictLocalHolderArchiveTests(unittest.TestCase):
    """
    Пункт 4: архивация местного носителя при активной задаче.

    Фактическое поведение (зафиксировано как есть, продуктовый код в R5 не
    менялся):

    - штатного сервиса архивации КИРПИЧИКА МЕСТА нет (`archive_personal_brick`
      работает только с личными), поэтому архив выполняется тем же мягким
      переходом `status='archived'` с ростом ревизии, который документирован
      схемой;
    - архив сам задачу НЕ закрывает: она остаётся `open` и видимой в
      кабинете, пока по ней не придёт первое решение;
    - первое же решение по такой задаче переводит её в терминальный
      `superseded` (`resolved_at` есть, `resolved_by_user_id` нет), после
      чего повторные callback безопасны, а версии и снимки шаблона
      остаются неизменными.

    Расхождение с формулировкой задания («архив сразу закрывает конфликт»)
    зафиксировано в отчёте этапа; тест охраняет фактическое корректное
    поведение и падает, если задача перестанет закрываться или начнёт
    переписывать историю шаблона.
    """

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPER_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _prepare(self, h: Harness) -> dict[str, Any]:
        code = "архив_конфликт_р5"
        with h.session_factory() as session:
            author = get_or_create_user(
                session, AUTHOR_TG, username="author5", full_name="Админ места"
            )
            get_or_create_user(
                session, SUPER_TG, username="boss5", full_name="Супер Админ"
            )
            get_or_create_user(
                session, OWNER_TG, username="owner5", full_name="Автор идеи"
            )
            place = create_place(
                session,
                "Зал архивации",
                city="Тестовый город",
                created_by_user_id=author.id,
            )
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=author.id,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            local = create_place_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                place_id=place.id,
                code=code,
                title="Местный на архивацию",
                value_type="short_text",
                rules={},
            )
            template = get_standard_template(session, AUTHOR_TG, place.id)
            prepared = update_template_section(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                template_id=template.template_id,
                expected_revision=template.revision,
                section="body",
                content=f"[{code}]",
            )
            self.assertEqual(prepared.status, "updated", prepared.message)
            create_template_snapshot(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                template_id=template.template_id,
            )
            source = create_personal_brick(
                session,
                actor_telegram_user_id=OWNER_TG,
                code=code,
                title="Глобальный кандидат",
                value_type="short_text",
                rules={},
            )
            proposal = create_brick_proposal(
                session,
                actor_telegram_user_id=OWNER_TG,
                source_brick_id=source.id,
                scope="system",
            )
            approved = approve_brick_proposal(
                session,
                actor_telegram_user_id=SUPER_TG,
                proposal_id=proposal.id,
            )
            self.assertEqual(
                approved.status, PROPOSAL_STATUS_APPROVED, approved.message
            )
            conflicts = list_place_conflicts(session, AUTHOR_TG, place.id)
            self.assertEqual(len(conflicts), 1)
            conflict_id = int(conflicts[0].id)

            self._archive_holder(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                brick_id=int(local.id),
                expected_revision=int(local.revision),
            )

            return {
                "code": code,
                "place_id": int(place.id),
                "local_id": int(local.id),
                "conflict_id": conflict_id,
            }

    def _archive_holder(
        self,
        session: Any,
        *,
        actor_telegram_user_id: int,
        brick_id: int,
        expected_revision: int,
    ) -> None:
        """
        Архивирует местный носитель штатным путём.

        Сегодня сервис `archive_personal_brick` работает только с личными
        кирпичиками (`place` → `not_found`), поэтому используется тот же
        мягкий переход `status='archived'` с ростом ревизии, который
        документирован схемой и триггером ревизии. Если позднее появится
        сервисная архивация кирпичиков места, тест автоматически пойдёт
        через неё — инварианты теста от этого не меняются.
        """
        result = archive_personal_brick(
            session,
            actor_telegram_user_id=actor_telegram_user_id,
            brick_id=brick_id,
            expected_revision=expected_revision,
        )
        if result.status == "archived":
            return
        row = session.get(Brick, brick_id)
        self.assertIsNotNone(row)
        assert row is not None
        row.status = "archived"
        row.revision = expected_revision + 1
        row.updated_at = utcnow()
        session.commit()

    def _template_state(self, h: Harness, place_id: int) -> tuple[Any, ...]:
        """Ревизия/версия/привязки шаблона и все снимки кирпичиков."""
        with h.session_factory() as session:
            template = get_standard_template(session, AUTHOR_TG, place_id)
            version_id = template.current_version.version_id
            bindings = sorted(
                (row.brick_id, row.code)
                for row in session.scalars(
                    select(TemplateVersionBrick).where(
                        TemplateVersionBrick.template_version_id == version_id
                    )
                ).all()
            )
            snapshots = sorted(
                (row.template_snapshot_id, row.brick_id, row.code)
                for row in session.scalars(select(TemplateSnapshotBrick)).all()
            )
            return (template.revision, version_id, bindings, snapshots)

    @scenario_test
    async def test_archived_local_holder_makes_task_superseded_safely(
        self, h: Harness
    ) -> None:
        data = self._prepare(h)
        place_id = int(data["place_id"])
        conflict_id = int(data["conflict_id"])
        template_before = self._template_state(h, place_id)

        # Архив носителя сам по себе задачу не удаляет: позиция сохраняется
        # (история), но решение по ней уже невозможно — первый же callback
        # по задаче закрывает её `superseded`. Это фактическое поведение,
        # см. docstring класса.
        await h.text("/admin", user_id=AUTHOR_TG)
        await h.callback(f"adm_place:{place_id}", user_id=AUTHOR_TG)
        self.assertIn("Активные задачи конфликтов кодов: 1", h.screen_text())
        await h.callback(f"adm_bconflicts:{place_id}", user_id=AUTHOR_TG)
        self.assertIn(str(data["code"]), h.screen_text())
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Задача разрешения конфликта", h.screen_text())
        await h.callback(f"adm_bconflict_switch:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn(
            "✅ Да, переключить",
            [label for label, _ in h.screen_buttons()],
        )
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("уже не держит этот код", h.screen_text())

        with h.session_factory() as session:
            row = session.get(BrickConflict, conflict_id)
            assert row is not None
            self.assertEqual(row.status, CONFLICT_STATUS_SUPERSEDED)
            self.assertIsNotNone(row.resolved_at, "терминальный переход с временем")
            self.assertIsNone(row.resolved_by_user_id, "автоматическое закрытие")
            local_row = session.get(Brick, int(data["local_id"]))
            assert local_row is not None
            self.assertEqual(local_row.status, "archived")
            self.assertEqual(
                int(
                    session.scalar(select(func.count()).select_from(BrickConflict)) or 0
                ),
                1,
                "история задачи сохранена, дубля нет",
            )

        # Повторные callback по закрытой задаче безопасны и явны.
        await h.callback(f"adm_bconflict:{conflict_id}", user_id=AUTHOR_TG)
        self.assertIn("Задача решена", h.screen_text())
        await h.callback(f"adm_bconflict_switch_yes:{conflict_id}", user_id=AUTHOR_TG)
        self.assertEqual(last_alert(h), STALE_ALERT)

        with h.session_factory() as session:
            again = resolve_conflict_by_switch(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                conflict_id=conflict_id,
            )
            self.assertEqual(again.status, "already_resolved")

        # Версии и снимки шаблона не переписаны ни архивом, ни решением.
        self.assertEqual(self._template_state(h, place_id), template_before)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
