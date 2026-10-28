# tests/test_poster2_r1_brick_codes.py
"""
R1 (Poster 2.1), дефект №30: глобальный код не конфликтует с локальными.

До правки `check_code_availability(scope="system")` считал занятым ЛЮБОЙ
кирпичик с тем же кодом, включая place/personal. Из-за этого одобрение
глобального предложения падало на проверке кода, если такой код уже был у
места.

Теперь для системной области конфликт — это только внутрисистемный дубль
(`system_row`) и зарезервированные системные коды. Поведение place/personal
не изменилось, исключение S1 (неизменённый собственный код легаси-строки)
сохранено и проверяется на базе, где легаси-строка появилась ДО миграции 15.

Граница R1 (согласовано с менеджером): если несистемных носителей кода ДВА
и больше (например, place-кирпичик и личный источник одного кода), INSERT
системного кирпичика отклоняет триггер `trg_bricks_reserve_codes_insert` —
его исключение Stage 26 рассчитано ровно на одного носителя. Полная модель
конфликтов «глобальный выигрывает + разрешение локальных дублей» — этап R3
с миграцией, поэтому сценарий множественных носителей здесь не проверяется.

Временная SQLite (tests.stage22a_helpers, tests.stage19a_helpers): настоящая
`post_bot.db` не открывается.
"""

from __future__ import annotations

import os
import shutil
import unittest

from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from brick_proposal_store import (
    PROPOSAL_STATUS_APPROVED,
    approve_brick_proposal,
    create_brick_proposal,
)
from brick_store import (
    check_code_availability,
    create_personal_brick,
    create_place_brick,
    get_system_brick,
)
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Brick
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage22a_helpers import BrickDatabaseTestCase


class SystemCodeAvailabilityTests(BrickDatabaseTestCase):
    """scope="system": свои дубли заняты, чужие области не мешают."""

    def test_place_brick_with_same_code_does_not_block_system_scope(self) -> None:
        owner = self.make_user(3_100_001, username="code-owner")
        place = self.make_place("Место с ценой")
        self.make_place_admin(place, owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            place_id=place.id,
            code="цена",
            title="Цена места",
            value_type="number",
            rules={"min": 0, "integer": True},
        )

        availability = check_code_availability(self.session, "цена", scope="system")

        self.assertTrue(availability.available, availability.message)
        self.assertIsNone(availability.reason)
        self.assertEqual(availability.code, "цена")

    def test_personal_brick_with_same_code_does_not_block_system_scope(self) -> None:
        owner = self.make_user(3_100_002, username="code-owner-2")
        create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code="цена",
            title="Моя цена",
            value_type="number",
            rules={},
        )

        self.assertTrue(
            check_code_availability(self.session, "цена", scope="system").available
        )

    def test_system_duplicate_stays_busy(self) -> None:
        self.assertIsNotNone(get_system_brick(self.session, "дата"))
        availability = check_code_availability(self.session, "дата", scope="system")
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "duplicate")

    def test_reserved_system_code_stays_busy(self) -> None:
        """`название` существует системно и не может быть занято повторно."""
        availability = check_code_availability(
            self.session, " НАЗВАНИЕ ", scope="system"
        )
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "duplicate")

    def test_forbidden_code_stays_forbidden_in_system_scope(self) -> None:
        """`фото` зарезервировано отдельно от системных кодов — во всех областях."""
        availability = check_code_availability(self.session, "фото", scope="system")
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "forbidden")

    def test_archived_system_brick_keeps_code(self) -> None:
        """Архивный системный кирпичик по-прежнему занимает свой код."""
        row = get_system_brick(self.session, "дата", include_archived=True)
        assert row is not None
        with self.engine.begin() as connection:
            connection.execute(
                Brick.__table__.update()
                .where(Brick.id == row.id)
                .values(status="archived")
            )
        self.session.expire_all()
        availability = check_code_availability(self.session, "дата", scope="system")
        self.assertFalse(availability.available)
        self.assertEqual(availability.reason, "duplicate")

    def test_place_and_personal_behaviour_unchanged(self) -> None:
        owner = self.make_user(3_100_003, username="place-owner")
        other = self.make_user(3_100_004, username="other")
        place = self.make_place("Место")
        self.make_place_admin(place, owner)
        create_place_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            place_id=place.id,
            code="цена",
            title="Цена места",
            value_type="number",
            rules={},
        )
        create_personal_brick(
            self.session,
            actor_telegram_user_id=owner.telegram_user_id,
            code="цена",
            title="Моя цена",
            value_type="number",
            rules={},
        )

        # Внутри области дубль запрещён, как и раньше.
        same_place = check_code_availability(
            self.session, "цена", scope="place", place_id=place.id
        )
        self.assertFalse(same_place.available)
        self.assertEqual(same_place.reason, "duplicate")
        same_person = check_code_availability(
            self.session, "цена", scope="personal", owner_user_id=owner.id
        )
        self.assertFalse(same_person.available)
        self.assertEqual(same_person.reason, "duplicate")
        # Другая область/другой владелец — свободно.
        other_place = self.make_place("Другое место")
        self.assertTrue(
            check_code_availability(
                self.session, "цена", scope="place", place_id=other_place.id
            ).available
        )
        self.assertTrue(
            check_code_availability(
                self.session, "цена", scope="personal", owner_user_id=other.id
            ).available
        )
        # Системные коды по-прежнему зарезервированы для несистемных областей.
        reserved_for_place = check_code_availability(
            self.session, "название", scope="place", place_id=place.id
        )
        self.assertFalse(reserved_for_place.available)
        self.assertEqual(reserved_for_place.reason, "system_reserved")


class LegacyCodeExceptionTests(unittest.TestCase):
    """
    Исключение S1: несистемная строка, появившаяся ДО того, как её код стал
    системным, сохраняет право на собственный текущий код.

    Строка вставляется прямым SQL на базе версии 14 — сервис такие данные
    создать уже не даст (код `адрес` системный с миграции 15).
    """

    # Легаси-строка места с кодом будущего системного кирпичика.
    LEGACY_PLACE_SQL = (
        "INSERT INTO bricks (id, code, title, type, scope, source, "
        "rules_json, status, place_id, created_by_user_id, "
        "created_at, updated_at, revision) VALUES "
        "(10, 'адрес', 'Адрес площадки', 'short_text', 'place', 'ask', NULL, "
        "'active', 1, 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', 1)"
    )
    OWNER_TG = 3_300_001

    def setUp(self) -> None:
        self.path = temp_db_path("poster-r1-legacy-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        # До миграции 15 `адрес` ещё не системный: строка вставляется без
        # конфликта, как в боевой базе до обновления.
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:14]), 14)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, telegram_user_id, created_at) "
                    "VALUES (1, :tg, '2026-01-01 00:00:00')"
                ),
                {"tg": self.OWNER_TG},
            )
            connection.execute(
                text(
                    "INSERT INTO places (id, created_by_user_id, created_at, address) "
                    "VALUES (1, 1, '2026-01-01 00:00:00', 'ул. Старая, 7')"
                )
            )
            connection.execute(text(self.LEGACY_PLACE_SQL))
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)

    def test_unchanged_legacy_place_code_stays_available(self) -> None:
        legacy = self.session.get(Brick, 10)
        assert legacy is not None
        self.assertEqual(legacy.code, "адрес")
        self.assertIsNotNone(get_system_brick(self.session, "адрес"))

        unchanged = check_code_availability(
            self.session,
            "адрес",
            scope="place",
            place_id=legacy.place_id,
            exclude_brick_id=legacy.id,
        )

        self.assertTrue(unchanged.available, unchanged.message)
        self.assertIsNone(unchanged.reason)

    def test_other_place_cannot_take_promoted_code(self) -> None:
        """Исключение S1 не распространяется на чужие строки и новые коды."""
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO places (id, created_by_user_id, created_at) "
                    "VALUES (2, 1, '2026-01-03 00:00:00')"
                )
            )

        foreign = check_code_availability(
            self.session, "адрес", scope="place", place_id=2
        )

        self.assertFalse(foreign.available)
        self.assertEqual(foreign.reason, "system_reserved")


class GlobalProposalApprovalTests(BrickDatabaseTestCase):
    """Одобрение глобального предложения при локальном носителе того же кода."""

    def _prepare(self, *, place_code: str | None = None):
        author = self.make_user(3_200_001, username="author")
        self.make_user(self.SUPERADMIN_TG, username="boss")
        place = self.make_place("Место автора", created_by_user_id=author.id)
        self.make_place_admin(place, author)
        if place_code is not None:
            create_place_brick(
                self.session,
                actor_telegram_user_id=author.telegram_user_id,
                place_id=place.id,
                code=place_code,
                title="Кирпичик места",
                value_type="number",
                rules={},
            )
        personal = create_personal_brick(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            code="цена",
            title="Цена",
            value_type="number",
            rules={"min": 0},
        )
        return author, place, personal

    def test_global_proposal_with_single_non_system_holder_is_approved(self) -> None:
        """
        Ровно один несистемный носитель кода (личный источник) — одобрение
        глобального предложения проходит и создаёт системный кирпичик.
        """
        author, _place, personal = self._prepare()
        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            source_brick_id=personal.id,
            scope="system",
        )
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        assert result.brick is not None
        self.assertEqual(result.brick.code, "цена")
        self.assertEqual(result.brick.scope, "system")
        self.assertIsNotNone(get_system_brick(self.session, "цена"))

    def test_global_proposal_is_approved_while_place_has_other_code(self) -> None:
        """
        Кирпичик места с ДРУГИМ кодом не мешает: локальные области живут
        отдельно, проверка system смотрит только систему.

        Один и тот же код у места и у личного источника — два носителя, это
        этап R3 (триггер резервирования разрешает ровно одного), см. шапку.
        """
        author, _place, personal = self._prepare(place_code="цена_места")
        self.assertTrue(
            check_code_availability(self.session, "цена", scope="system").available
        )

        proposal = create_brick_proposal(
            self.session,
            actor_telegram_user_id=author.telegram_user_id,
            source_brick_id=personal.id,
            scope="system",
        )
        result = approve_brick_proposal(
            self.session,
            actor_telegram_user_id=self.SUPERADMIN_TG,
            proposal_id=proposal.id,
        )

        self.assertEqual(result.status, PROPOSAL_STATUS_APPROVED, result.message)
        assert result.brick is not None
        self.assertEqual(result.brick.code, "цена")
        # Местный кирпичик не переписан и остался в своей области.
        place_bricks = list(
            self.session.scalars(select(Brick).where(Brick.scope == "place"))
        )
        self.assertEqual([brick.code for brick in place_bricks], ["цена_места"])
