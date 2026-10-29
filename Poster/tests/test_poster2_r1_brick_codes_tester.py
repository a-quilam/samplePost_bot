# tests/test_poster2_r1_brick_codes_tester.py
"""
R1 (Poster 2.1), дефект №30 — независимые проверки тестировщика.

Проверяется ТРЕБОВАНИЕ владельца, а не реализация:

1. глобальное предложение кирпичика с кодом, который уже существует как
   кирпичик МЕСТА (ровно один несистемный носитель), одобряется успешно;
2. внутрисистемный дубль по-прежнему отклоняется;
3. системные резервы («название», «фото») запрещены во всех областях;
4. поведение place/personal-уникальности не изменилось;
5. при ДВУХ и более несистемных носителях кода одобрение проходит, местные
   носители не переписываются, а по местам создаются задачи конфликта
   (модель R3 заменила временное ограничение R1 `status="error"`).

Ни один сценарий не открывает реальную `post_bot.db`: используется временная
SQLite из `tests.stage22a_helpers` / `tests.stage19a_helpers`, а сценарии
кабинета идут через `Application.process_update` + FakeBot.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from sqlalchemy import func, select, text

from brick_proposal_store import (
    PROPOSAL_STATUS_APPROVED,
    PROPOSAL_STATUS_PENDING,
    approve_brick_proposal,
    create_brick_proposal,
)
from brick_store import (
    BrickConflictError,
    check_code_availability,
    create_personal_brick,
    create_place_brick,
    create_system_brick,
    get_system_brick,
)
from brick_types import BrickValidationError
from models import Brick, BrickProposal, PlaceAdmin, utcnow
from place_store import create_place
from tests.stage22a_helpers import BrickDatabaseTestCase
from user_store import get_or_create_user

try:
    from tests.integration_harness import (
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )

    INTEGRATION_ERROR = HARNESS_IMPORT_ERROR
except Exception as e:  # noqa: BLE001
    INTEGRATION_ERROR = str(e)


AUTHOR_TG = 7_910_001
SUPERADMIN_TG = 7_910_002
OTHER_TG = 7_910_003


def ensure_user(session_or_h, telegram_user_id: int, username: str | None = None):
    """Пользователь в переданной сессии или в сессии Harness."""
    if hasattr(session_or_h, "session_factory"):
        with session_or_h.session_factory() as session:
            return get_or_create_user(
                session,
                telegram_user_id,
                username=username,
                full_name=f"Пользователь {telegram_user_id}",
            )
    return get_or_create_user(
        session_or_h,
        telegram_user_id,
        username=username,
        full_name=f"Пользователь {telegram_user_id}",
    )


def place_brick_sql(
    *,
    brick_id: int,
    code: str,
    place_id: int,
    created_by_user_id: int,
    title: str = "Местный кирпичик",
) -> str:
    """
    Прямой INSERT кирпичика места (подготовка данных).

    Нужен только для состояний, которые сервис создать не даёт, — ровно один
    несистемный носитель системного кода. Образец: tests/test_poster2_domain.py
    (AddressCodeCollisionTests._LEGACY_SQL).
    """
    return (
        "INSERT INTO bricks (id, code, title, type, scope, source, rules_json, "
        "status, place_id, created_by_user_id, created_at, updated_at, revision) "
        f"VALUES ({brick_id}, '{code}', '{title}', 'short_text', 'place', 'ask', "
        f"NULL, 'active', {place_id}, {created_by_user_id}, "
        "'2026-01-02 00:00:00', '2026-01-02 00:00:00', 1)"
    )


# --- доступность кода ------------------------------------------------------


class SystemScopeAvailabilityTests(BrickDatabaseTestCase):
    """
    `scope="system"` не считает занятым код, существующий только у места
    или у личного кирпичика (корень дефекта №30).
    """

    def _place_with_brick(self, *, tg: int, code: str):
        owner = self.make_user(tg, username=f"owner{tg}")
        place = self.make_place(f"Место {tg}")
        self.make_place_admin(place, owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            place_id=place.id,
            code=code,
            title="Кирпичик места",
            value_type="short_text",
            rules={},
        )
        return owner, place

    def test_place_only_holder_does_not_block_system_code(self) -> None:
        """Ровно один несистемный носитель — кирпичик места."""
        self._place_with_brick(tg=3_900_001, code="цена_места")

        availability = check_code_availability(
            self.session, "цена_места", scope="system"
        )

        self.assertTrue(availability.available, availability.message)
        self.assertIsNone(availability.reason)

    def test_two_places_with_same_code_do_not_block_system_code(self) -> None:
        """Даже несколько мест с одинаковым кодом не мешают системной области."""
        self._place_with_brick(tg=3_900_002, code="цена_места")
        owner = self.make_user(3_900_004, username="owner2")
        other_place = self.make_place("Второе место")
        self.make_place_admin(other_place, owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            place_id=other_place.id,
            code="цена_места",
            title="Кирпичик другого места",
            value_type="short_text",
            rules={},
        )

        self.assertTrue(
            check_code_availability(
                self.session, "цена_места", scope="system"
            ).available
        )

    def test_personal_only_holder_does_not_block_system_code(self) -> None:
        owner = self.make_user(3_900_003, username="personal-owner")
        create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code="цена_лично",
            title="Личный",
            value_type="short_text",
            rules={},
        )

        self.assertTrue(
            check_code_availability(
                self.session, "цена_лично", scope="system"
            ).available
        )

    def test_internal_system_duplicate_stays_busy(self) -> None:
        self.assertIsNotNone(get_system_brick(self.session, "дата"))
        availability = check_code_availability(self.session, "дата", scope="system")
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "duplicate")

    def test_system_code_creation_is_blocked_by_place_holder_at_insert(self) -> None:
        """
        Наблюдение: доступность говорит «свободно», но триггер БД не даёт
        создать системный кирпичик поверх одного носителя места.
        Исход явный (BrickConflictError), не тишина и не «создано».
        """
        self._place_with_brick(tg=3_900_005, code="цена_места")
        self.make_user(self.SUPERADMIN_TG, username="boss")

        self.assertTrue(
            check_code_availability(
                self.session, "цена_места", scope="system"
            ).available
        )
        with self.assertRaises(BrickConflictError):
            create_system_brick(
                self.session,
                actor_telegram_user_id=self.SUPERADMIN_TG,
                code="цена_места",
                title="Глобальный",
                value_type="short_text",
                rules={},
            )
        self.assertIsNone(get_system_brick(self.session, "цена_места"))

    def test_reserved_system_names_stay_busy_in_all_scopes(self) -> None:
        """
        `название` занят системой, `фото` запрещён — во всех трёх областях.

        Фактические reason (сверено с поведением `check_code_availability`):
        - `название` / system → `duplicate` (системная строка уже есть),
          `название` / place|personal → `system_reserved`;
        - `фото` → `forbidden` во всех областях (отдельный запрет, не резерв).
        """
        owner_row = self.make_user(3_900_006, username="scope-owner")
        place = self.make_place("Место резервов")
        self.make_place_admin(place, owner_row)

        expected = {
            ("название", "system"): {"duplicate"},
            ("название", "place"): {"system_reserved"},
            ("название", "personal"): {"system_reserved"},
            ("фото", "system"): {"forbidden"},
            ("фото", "place"): {"forbidden"},
            ("фото", "personal"): {"forbidden"},
        }
        for (code, scope), reasons in expected.items():
            with self.subTest(code=code, scope=scope):
                kwargs: dict[str, Any] = {}
                if scope == "place":
                    kwargs["place_id"] = place.id
                elif scope == "personal":
                    kwargs["owner_user_id"] = owner_row.id
                availability = check_code_availability(
                    self.session, code, scope=scope, **kwargs
                )
                self.assertFalse(availability.available)
                self.assertIn(availability.reason, reasons)

    def test_reserved_code_cannot_be_created_via_services(self) -> None:
        """
        Сервисы не дают обойти резервы: явная ошибка, кирпичик не создан.

        Фактические исключения сервисов: `название` → BrickConflictError
        (проверка доступности), `фото` → BrickCodeError (валидация кода,
        подкласс BrickValidationError). Оба — явный отказ, не молчание.
        """
        owner = self.make_user(3_900_007, username="reserve-owner")
        place = self.make_place("Место сервисов")
        self.make_place_admin(place, owner)
        self.make_user(self.SUPERADMIN_TG, username="boss")

        for code in ("название", "фото"):
            with self.subTest(code=code, scope="personal"):
                with self.assertRaises((BrickConflictError, BrickValidationError)):
                    create_personal_brick(
                        self.session,
                        actor_telegram_user_id=owner.telegram_user_id,
                        code=code,
                        title="Попытка",
                        value_type="short_text",
                        rules={},
                    )
            with self.subTest(code=code, scope="place"):
                with self.assertRaises((BrickConflictError, BrickValidationError)):
                    create_place_brick(
                        self.session,
                        actor_telegram_user_id=owner.telegram_user_id,
                        place_id=place.id,
                        code=code,
                        title="Попытка",
                        value_type="short_text",
                        rules={},
                    )

        # Главное: ни один кирпичик не создан и системный резерв не переписан.
        self.session.expire_all()
        occupied = list(
            self.session.scalars(
                select(Brick).where(Brick.code.in_(("название", "фото")))
            )
        )
        self.assertEqual(
            [(row.code, row.scope) for row in occupied],
            [("название", "system")],
            "резервный код не должен получить новый носитель",
        )
        self.assertIsNone(get_system_brick(self.session, "фото"))

    def test_place_and_personal_uniqueness_unchanged(self) -> None:
        """Дубль внутри области запрещён, в другой области — разрешён."""
        owner = self.make_user(3_900_008, username="uniq-owner")
        other = self.make_user(3_900_009, username="uniq-other")
        place = self.make_place("Место уникальности")
        self.make_place_admin(place, owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            place_id=place.id,
            code="цена_uniq",
            title="Цена места",
            value_type="short_text",
            rules={},
        )

        self.assertEqual(
            check_code_availability(
                self.session, "цена_uniq", scope="place", place_id=place.id
            ).reason,
            "duplicate",
        )
        with self.assertRaises(BrickConflictError):
            create_place_brick(
                self.session,
                actor_telegram_user_id=owner.telegram_user_id,
                place_id=place.id,
                code="цена_uniq",
                title="Дубль",
                value_type="short_text",
                rules={},
            )

        # Личная область того же человека свободна (разные области).
        self.assertTrue(
            check_code_availability(
                self.session, "цена_uniq", scope="personal", owner_user_id=owner.id
            ).available
        )
        create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code="цена_uniq",
            title="Личная цена",
            value_type="short_text",
            rules={},
        )
        self.assertEqual(
            check_code_availability(
                self.session, "цена_uniq", scope="personal", owner_user_id=owner.id
            ).reason,
            "duplicate",
        )
        # Другой владелец — свободно.
        self.assertTrue(
            check_code_availability(
                self.session, "цена_uniq", scope="personal", owner_user_id=other.id
            ).available
        )


# --- одобрение глобальных предложений --------------------------------------


class GlobalApprovalTests(BrickDatabaseTestCase):
    """Одобрение глобального предложения при одном/двух несистемных носителях."""

    def _author_with_personal(self, *, tg: int, code: str) -> tuple[Any, Any]:
        author = self.make_user(tg, username=f"author{tg}")
        personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            code=code,
            title="Личный источник",
            value_type="number",
            rules={"min": 0},
        )
        return author, personal

    def _system_proposal(self, author, personal) -> int:
        return int(
            create_brick_proposal(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                source_brick_id=personal.id,
                scope="system",
            ).id
        )

    def _proposal_id_by_code(self, code: str) -> int:
        """ID единственного предложения с таким кодом (подготовка прямым SQL)."""
        value = self.session.scalar(
            select(BrickProposal.id).where(BrickProposal.code == code)
        )
        assert value is not None, f"нет предложения с кодом {code!r}"
        return int(value)

    def _system_code_count(self, code: str) -> int:
        value = self.session.scalar(
            select(func.count())
            .select_from(Brick)
            .where(Brick.code == code, Brick.scope == "system")
        )
        return int(value or 0)

    def test_personal_only_holder_is_approved(self) -> None:
        """Кейс «личный кирпичик» — единственный несистемный носитель."""
        author, personal = self._author_with_personal(tg=3_910_001, code="цена_личная")
        self.make_user(self.SUPERADMIN_TG, username="boss")
        proposal_id = self._system_proposal(author, personal)

        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal_id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        assert result.brick is not None
        self.assertEqual(result.brick.scope, "system")
        self.assertEqual(result.brick.code, "цена_личная")
        self.assertIsNotNone(get_system_brick(self.session, "цена_личная"))
        # Исходный личный кирпичик не переписан.
        source_row = self.session.get(Brick, personal.id)
        assert source_row is not None
        self.assertEqual(source_row.scope, "personal")

    def test_place_holder_with_different_code_does_not_block_approval(self) -> None:
        """Кирпичик места с другим кодом не мешает глобальному одобрению."""
        author, personal = self._author_with_personal(tg=3_910_002, code="цена_глоб")
        place = self.make_place("Место автора")
        self.make_place_admin(place, author)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="цена_местная",
            title="Местная цена",
            value_type="short_text",
            rules={},
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        proposal_id = self._system_proposal(author, personal)

        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal_id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        assert result.brick is not None
        self.assertEqual(result.brick.code, "цена_глоб")
        # Местный кирпичик остался в своей области и не переименован.
        place_codes = list(
            self.session.scalars(select(Brick.code).where(Brick.scope == "place"))
        )
        self.assertEqual(place_codes, ["цена_местная"])

    def test_place_holder_of_same_code_gets_conflict_task(self) -> None:
        """
        R3 заменил ограничение R1 «два носителя → error» на модель конфликта.

        Глобальное предложение одобряется (явный успех, а не тихий отказ),
        местный кирпичик остаётся нетронутым, а по месту создаётся задача
        разрешения конфликта. Требование R1 «исход явный» сохранено.
        """
        author, personal = self._author_with_personal(tg=3_910_003, code="цена_двойная")
        place = self.make_place("Место с тем же кодом")
        self.make_place_admin(place, author)
        create_place_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            place_id=place.id,
            code="цена_двойная",
            title="Местная цена",
            value_type="number",
            rules={},
        )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        proposal_id = self._system_proposal(author, personal)

        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal_id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        self.assertIsNotNone(result.brick)
        self.assertEqual(len(result.conflicts), 1)
        assert result.message
        self.assertIn("задачи разрешения конфликта", result.message)
        # Системный кирпичик создан, местный не переписан.
        self.assertIsNotNone(get_system_brick(self.session, "цена_двойная"))
        self.session.expire_all()
        place_codes = list(
            self.session.scalars(select(Brick.code).where(Brick.scope == "place"))
        )
        self.assertEqual(place_codes, ["цена_двойная"])
        proposal = self.session.get(BrickProposal, proposal_id)
        assert proposal is not None
        self.assertEqual(proposal.status, PROPOSAL_STATUS_APPROVED)
        self.assertIsNotNone(proposal.approved_brick_id)

    def test_internal_system_duplicate_is_conflict(self) -> None:
        """
        Внутрисистемный дубль остаётся отклонённым: предложение с кодом уже
        существующего системного кирпичика получает `conflict`, а не одобрение.

        Состояние «pending-снимок на занятый системный код» сервис создать не
        даёт (это проверяет следующий тест), поэтому строка готовится прямым
        INSERT — триггеры источника при этом соблюдены: личный кирпичик
        активен и принадлежит автору.
        """
        author, personal = self._author_with_personal(
            tg=3_910_004, code="дубль_системы"
        )
        # Системный `дата` уже существует из миграции; снимок ссылается на него
        # по коду, источник — собственный активный личный кирпичик автора.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO brick_proposals (author_user_id, scope, place_id, "
                    "source_brick_id, title, code, type, source, rules_json, status, "
                    "created_at) VALUES (:author_id, 'system', NULL, :src, "
                    "'Дата мероприятия', 'дата', 'date', 'ask', NULL, 'pending', "
                    "'2026-01-05 00:00:00')"
                ),
                {"author_id": author.id, "src": personal.id},
            )
        self.make_user(self.SUPERADMIN_TG, username="boss")
        self.session.expire_all()
        proposal_id = self._proposal_id_by_code("дата")
        system_before = get_system_brick(self.session, "дата")
        assert system_before is not None

        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal_id,
        )

        self.assertEqual(result.status, "conflict", result.message)
        self.assertIsNone(result.brick)
        self.assertFalse(result.ok)
        self.session.expire_all()
        proposal = self.session.get(BrickProposal, proposal_id)
        assert proposal is not None
        self.assertEqual(proposal.status, PROPOSAL_STATUS_PENDING)
        # Системный кирпичик не переписан и не задублирован.
        system_after = get_system_brick(self.session, "дата")
        assert system_after is not None
        self.assertEqual(system_after.id, system_before.id)
        self.assertEqual(self._system_code_count("дата"), 1)

    def test_proposal_creation_rejects_system_duplicate(self) -> None:
        """
        Повторное предложение того же кода после глобального одобрения
        отклоняется уже на создании снимка (код занят в системе).

        Это реальный путь: исходный личный кирпичик сохраняет свой код и
        остаётся активным после одобрения, поэтому второе предложение из него
        технически возможно — и должно получить явный конфликт.
        """
        from brick_proposal_store import ProposalConflictError

        author, personal = self._author_with_personal(tg=3_910_005, code="карточка")
        self.make_user(self.SUPERADMIN_TG, username="boss")
        first_id = self._system_proposal(author, personal)
        first = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=first_id,
        )
        self.assertEqual(first.status, PROPOSAL_STATUS_APPROVED, first.message)
        self.assertIsNotNone(get_system_brick(self.session, "карточка"))

        with self.assertRaises(ProposalConflictError):
            create_brick_proposal(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                source_brick_id=personal.id,
                scope="system",
            )
        # Второго системного кирпичика не появилось.
        self.assertEqual(self._system_code_count("карточка"), 1)


# --- пользовательский сценарий: одобрение через кабинет --------------------


@unittest.skipIf(
    INTEGRATION_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_ERROR}",
)
class GlobalApprovalUiTests(unittest.TestCase):
    """То же требование №30, но через настоящий UI кабинета (FakeBot)."""

    def setUp(self) -> None:
        self._patchers: list[Any] = [
            patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)),
            patch("config.SUPERADMIN_CONTACT_URL", None),
        ]
        for patcher in self._patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _author_with_personal(self, h, *, code: str):
        with h.session_factory() as session:
            author = get_or_create_user(
                session, AUTHOR_TG, username="author", full_name="Автор"
            )
            get_or_create_user(
                session, SUPERADMIN_TG, username="boss", full_name="Босс"
            )
            place = create_place(session, "Место автора", created_by_user_id=author.id)
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=author.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
            session.commit()
            personal = create_personal_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                code=code,
                title="Личный источник",
                value_type="short_text",
                rules={},
            )
            return int(place.id), int(personal.id)

    def _proposal(self, h, personal_id: int) -> int:
        with h.session_factory() as session:
            return int(
                create_brick_proposal(
                    session,
                    actor_telegram_user_id=AUTHOR_TG,
                    source_brick_id=personal_id,
                    scope="system",
                ).id
            )

    async def _approve_via_ui(self, h, proposal_id: int) -> None:
        await h.text("/admin", user_id=SUPERADMIN_TG)
        await h.callback("adm_bprop_global", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_open:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve:{proposal_id}", user_id=SUPERADMIN_TG)
        await h.callback(f"adm_bprop_approve_yes:{proposal_id}", user_id=SUPERADMIN_TG)

    @scenario_test
    async def test_ui_approves_global_proposal_with_personal_holder(self, h):
        _place_id, personal_id = self._author_with_personal(h, code="глоб_ui")
        proposal_id = self._proposal(h, personal_id)

        await self._approve_via_ui(h, proposal_id)

        with h.session_factory() as session:
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            self.assertEqual(proposal.status, "approved")
            system = session.scalar(
                select(Brick).where(Brick.code == "глоб_ui", Brick.scope == "system")
            )
            self.assertIsNotNone(system)
        self.assertIn("Одобрено", h.screen_text())

    @scenario_test
    async def test_ui_two_holders_approves_and_creates_conflict_task(self, h):
        """
        R3: два носителя больше не отказ. Кабинет показывает явное «Одобрено»
        с числом задач конфликта, местный кирпичик и шаблон не переписаны.
        """
        place_id, personal_id = self._author_with_personal(h, code="глоб_двойной")
        with h.session_factory() as session:
            create_place_brick(
                session,
                actor_telegram_user_id=AUTHOR_TG,
                place_id=place_id,
                code="глоб_двойной",
                title="Местный",
                value_type="short_text",
                rules={},
            )
        proposal_id = self._proposal(h, personal_id)

        await self._approve_via_ui(h, proposal_id)

        text = h.screen_text()
        self.assertIn("✅ Одобрено", text)
        self.assertIn("задачи разрешения конфликта", text)
        self.assertNotIn("Не удалось одобрить", text)
        with h.session_factory() as session:
            proposal = session.get(BrickProposal, proposal_id)
            assert proposal is not None
            self.assertEqual(proposal.status, "approved")
            self.assertIsNotNone(
                session.scalar(
                    select(Brick).where(
                        Brick.code == "глоб_двойной", Brick.scope == "system"
                    )
                )
            )
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(Brick)
                    .where(
                        Brick.code == "глоб_двойной",
                        Brick.scope == "place",
                        Brick.place_id == place_id,
                    )
                ),
                1,
            )
