# tests/test_poster2_domain.py
"""
Poster 2.0 S1: домен единого входа и личного шаблона без площадки.

Проверяются миграции 15/16 на заполненной временной БД (данные, FK,
триггеры, идемпотентность), сохранность прежних черновиков, анонс по личному
шаблону без `place_id`, разовое место без записи в справочник,
автоподстановка только в кирпичики снимка, идемпотентность первого личного
шаблона, системный `[адрес]`, IDOR и optimistic locking.

Реальная `Poster/post_bot.db`, `.env`, Telegram и сеть не используются:
каждый тест поднимает собственный временный SQLite-файл.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import unittest

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from announcement_store import (
    ORIGIN_PERSONAL,
    ORIGIN_PLACE,
    begin_announcement_publication,
    build_announcement_fields,
    build_announcement_preview,
    build_announcement_render_payload,
    get_announcement_draft,
    mark_announcement_publication_sent,
    save_announcement_value,
    start_announcement_wizard,
)
from announcement_wizard import (
    AnnouncementConflictError,
    AnnouncementNotFoundError,
    AnnouncementPublicationError,
    AnnouncementStageError,
    WizardStage,
    snapshot_brick_ids_by_code,
)
from brick_store import (
    BrickConflictError,
    check_code_availability,
    create_personal_brick,
    get_system_brick,
    get_visible_brick,
    list_system_bricks,
    update_personal_brick,
)
from brick_types import SYSTEM_BRICKS_BY_CODE
from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from models import Brick, Place
from template_store import (
    ensure_initial_personal_template,
    list_personal_templates,
    update_template_body,
)
from template_types import (
    BUILTIN_TEMPLATE,
    BUILTIN_TEMPLATE_VERSION,
    INITIAL_PERSONAL_TEMPLATE_NAME,
)
from tests.stage19a_helpers import make_engine, temp_db_path
from tests.stage23_helpers import OWNER_TG, TemplateDatabaseTestCase
from tests.stage27_helpers import OTHER_TG, AnnouncementDatabaseTestCase
from utils.tg_html import escape_html

SENT_PROGRESS = {
    "fingerprint": "fp-legacy",
    "step": 2,
    "ids": [501, 502],
    "total": 2,
}
EMPTY_PROGRESS = {"fingerprint": "fp-legacy", "step": 0, "total": 0}


def _seed_version_thirteen(path: str) -> None:
    """
    Заполняет БД версии 13 связанными строками до миграций 15/16.

    Пишутся реальные пользователь/место/название, два доставленных черновика
    (с адресами доставки и без), значение и дополнительный кирпичик. Это и
    есть «заполненная временная БД»: миграция 16 перестраивает таблицу, и
    данные обязаны пережить перестройку.
    """
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, ?, '2026-01-01 00:00:00')",
            (OWNER_TG,),
        )
        connection.execute(
            "INSERT INTO places (id, created_by_user_id, created_at, address, url) "
            "VALUES (1, 1, '2026-01-01 00:00:00', 'ул. Старая, 7', "
            "'https://example.com/old')"
        )
        connection.execute(
            "INSERT INTO place_names "
            "(id, place_id, name, name_search, is_canonical, created_by_user_id, "
            " created_at) "
            "VALUES (1, 1, 'Старый дом культуры', 'старый дом культуры', 1, 1, "
            "'2026-01-01 00:00:00')"
        )
        for draft_id, progress in ((1, SENT_PROGRESS), (2, EMPTY_PROGRESS)):
            connection.execute(
                """
                INSERT INTO announcement_drafts (
                    id, author_user_id, place_id, place_name_id,
                    place_display_name, place_address, place_url,
                    template_id, template_version, template_snapshot_id,
                    stage, revision, idempotency_key, photos,
                    publication_status, publication_text, publication_photos,
                    publication_progress, publication_error,
                    publication_started_at, published_at, created_at, updated_at
                ) VALUES (
                    ?, 1, 1, 1, 'Старый дом культуры', 'ул. Старая, 7',
                    'https://example.com/old', NULL, NULL, NULL,
                    'completed', 2, NULL, '[]',
                    'sent', '<b>Старый анонс</b>', '[]', ?, NULL,
                    '2026-01-02 10:00:00', '2026-01-02 10:05:00',
                    '2026-01-02 09:00:00', '2026-01-02 10:05:00'
                )
                """,
                (draft_id, json.dumps(progress, sort_keys=True)),
            )
        # Значение и дополнительный кирпичик ссылаются на системные brick_id
        # миграции 6 — их сохраняет и миграция 16.
        connection.execute(
            "INSERT INTO announcement_values "
            "(id, draft_id, brick_id, state, value_json, created_at, updated_at) "
            "VALUES (1, 1, 1, 'valid', '{\"text\": \"Старое название\"}', "
            "'2026-01-02 09:10:00', '2026-01-02 09:10:00')"
        )
        connection.execute(
            "INSERT INTO announcement_extras "
            "(id, draft_id, brick_id, section, position, occurrence, "
            " created_at, updated_at) "
            "VALUES (1, 1, 9, 'footer', 'end', 1, "
            "'2026-01-02 09:11:00', '2026-01-02 09:11:00')"
        )
        connection.commit()
    finally:
        connection.close()


def _scalar(path: str, statement: str):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement).fetchone()
    finally:
        connection.close()


class _MigrationTestCase(unittest.TestCase):
    """Временная БД версии 13 с данными; тесты — только в наследниках."""

    def setUp(self) -> None:
        self.path = temp_db_path("poster2-migrations-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:13]), 13)
        _seed_version_thirteen(self.path)


class MigrationFifteenAndSixteenTests(_MigrationTestCase):
    """Миграции 15–17 на заполненной временной БД версии 13."""

    def test_migration_keeps_rows_links_checks_and_passes_integrity(self) -> None:
        self.assertEqual(LATEST_VERSION, 18)
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertEqual(
                connection.execute(
                    "SELECT max(version) FROM schema_migrations"
                ).fetchone()[0],
                LATEST_VERSION,
            )
            # Таблицы-копии миграции 16 удалены: остатков перестройки нет.
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(
                {name for name in tables if name.endswith("_backup")}, set()
            )
            row = connection.execute(
                "SELECT origin, place_id, place_display_name, publication_status, "
                "publication_progress, live_message_ids_json, place_address "
                "FROM announcement_drafts WHERE id = 1"
            ).fetchone()
            self.assertEqual(row[0], "place")
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], "Старый дом культуры")
            self.assertEqual(row[3], "sent")
            self.assertEqual(json.loads(row[4]), SENT_PROGRESS)
            self.assertIsNone(row[5])
            self.assertEqual(row[6], "ул. Старая, 7")
            # Снимки/связи и прогресс второй строки тоже на месте.
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM announcement_drafts"
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT draft_id, brick_id, state FROM announcement_values"
                ).fetchall(),
                [(1, 1, "valid")],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT draft_id, brick_id, section, position "
                    "FROM announcement_extras"
                ).fetchall(),
                [(1, 9, "footer", "end")],
            )
            # Индексы и триггеры пересозданы вместе с таблицей.
            names = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN "
                    "('index', 'trigger')"
                )
            }
            self.assertTrue(
                {
                    "ix_announcement_drafts_author_updated",
                    "ux_announcement_drafts_snapshot",
                    "ux_announcement_drafts_idempotency",
                    "ix_announcement_drafts_publication_status",
                    "trg_announcement_drafts_revision_increases",
                    "trg_announcement_drafts_identity_immutable",
                    "trg_announcement_drafts_template_immutable",
                    "trg_announcement_values_identity_immutable",
                    "trg_announcement_extras_identity_immutable",
                    "trg_announcement_drafts_published_immutable",
                }
                <= names
            )
        finally:
            connection.close()

        # Повторный запуск ничего не меняет и не дублирует.
        before = _scalar(
            self.path,
            "SELECT count(*) || ':' || max(version) FROM announcement_drafts, "
            "schema_migrations",
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        after = _scalar(
            self.path,
            "SELECT count(*) || ':' || max(version) FROM announcement_drafts, "
            "schema_migrations",
        )
        self.assertEqual(before, after)

    def test_origin_is_immutable_and_new_checks_hold(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE announcement_drafts SET origin='personal', revision=3 "
                    "WHERE id = 1"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                # personal-черновик без площадки требует пустых place_*.
                connection.execute(
                    "INSERT INTO announcement_drafts ("
                    "author_user_id, place_id, place_display_name, origin, stage, "
                    "revision, created_at, updated_at) VALUES ("
                    "1, 1, 'Место', 'personal', 'place', 1, "
                    "'2026-01-03 00:00:00', '2026-01-03 00:00:00')"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                # place-черновик без площадки недопустим и после 16.
                connection.execute(
                    "INSERT INTO announcement_drafts ("
                    "author_user_id, place_id, place_display_name, origin, stage, "
                    "revision, created_at, updated_at) VALUES ("
                    "1, NULL, NULL, 'place', 'place', 1, "
                    "'2026-01-03 00:00:00', '2026-01-03 00:00:00')"
                )
            connection.execute(
                "INSERT INTO announcement_drafts ("
                "author_user_id, place_id, place_display_name, place_address, "
                "place_url, origin, template_id, template_version, "
                "template_snapshot_id, stage, revision, live_message_ids_json, "
                "created_at, updated_at) VALUES ("
                "1, NULL, NULL, NULL, NULL, 'personal', NULL, NULL, NULL, "
                "'questions', 1, '[700, 701]', "
                "'2026-01-03 00:00:00', '2026-01-03 00:00:00')"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                # JSON-массив обязателен и для живым ID.
                connection.execute(
                    "UPDATE announcement_drafts SET live_message_ids_json='{}' "
                    "WHERE origin='personal'"
                )
            connection.rollback()
        finally:
            connection.close()

    def test_address_brick_gets_free_id_when_user_brick_took_ten(self) -> None:
        # База версии 14: пользовательский кирпичик уже занял id=10, поэтому
        # жёсткий `id=10` в миграции 15 дал бы UNIQUE constraint failed.
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:14]), 14)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO bricks (id, code, title, type, scope, source, "
                "rules_json, status, owner_user_id, created_by_user_id, "
                "created_at, updated_at, revision) "
                "VALUES (10, 'свой', 'Свой кирпичик', 'short_text', 'personal', "
                "'ask', NULL, 'active', 1, 1, '2026-01-02 00:00:00', "
                "'2026-01-02 00:00:00', 1)"
            )
            connection.commit()
        finally:
            connection.close()

        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.session.close)

        address = get_system_brick(self.session, "адрес")
        self.assertIsNotNone(address)
        assert address is not None
        self.assertEqual(address.id, 11)
        self.assertEqual(address.scope, "system")
        self.assertEqual(address.source, "place")
        user_brick = self.session.get(Brick, 10)
        self.assertIsNotNone(user_brick)
        assert user_brick is not None
        self.assertEqual(user_brick.code, "свой")
        # Описание нового кирпичика пустое: старые строки не переписаны.
        first_brick = self.session.get(Brick, 1)
        assert first_brick is not None
        self.assertIsNone(first_brick.description)


class LegacyDraftCompatibilityTests(_MigrationTestCase):
    """Доставленные анонсы после всех миграций читаются сервисом без потерь.

    «Легаси» здесь — не таблица `drafts` (она ушла в архив миграцией 17), а
    строки `announcement_drafts` без живых адресов медиаблока, как их
    оставляли версии до Stage 14.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.session = self.Session()
        self.addCleanup(self.session.close)

    def test_legacy_place_draft_keeps_origin_and_progress(self) -> None:
        draft = get_announcement_draft(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=1
        )
        self.assertEqual(draft.origin, ORIGIN_PLACE)
        self.assertFalse(draft.is_personal)
        self.assertEqual(draft.place_id, 1)
        self.assertEqual(draft.place_display_name, "Старый дом культуры")
        self.assertTrue(draft.publication_sent)
        self.assertEqual(draft.delivered_message_ids, (501, 502))
        self.assertEqual(draft.delivery_fingerprint, "fp-legacy")
        self.assertTrue(draft.can_edit_after_delivery)
        # Живой медиаблок ещё не использовался: множество пустое, но это не
        # повод считать доставку потерянной.
        self.assertEqual(draft.live_message_ids, ())

    def test_empty_live_ids_do_not_resend_completed_post(self) -> None:
        # Пустые/отсутствующие живые ID не запускают отправку заново:
        # `sent` остаётся `sent`, повторная фиксация идемпотентна.
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE announcement_drafts SET "
                    "live_message_ids_json='[]' WHERE id = 1"
                )
            )
        before = get_announcement_draft(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=1
        )
        again = mark_announcement_publication_sent(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=1,
            message_ids=(999,),
        )
        self.assertTrue(again.publication_sent)
        self.assertEqual(again.revision, before.revision)
        self.assertEqual(again.delivered_message_ids, (501, 502))
        self.assertEqual(again.published_at, before.published_at)
        self.assertEqual(again.live_message_ids, ())

    def test_sent_draft_without_addresses_cannot_be_resent(self) -> None:
        draft = get_announcement_draft(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=2
        )
        self.assertTrue(draft.publication_sent)
        self.assertEqual(draft.delivered_message_ids, ())
        self.assertFalse(draft.can_edit_after_delivery)
        # Правка и пересборка запрещены: адресов прошлой версии нет, дубль
        # создать нельзя.
        with self.assertRaises(AnnouncementPublicationError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=2,
                brick_id=1,
                value="Новое название",
            )
        with self.assertRaises(AnnouncementPublicationError):
            begin_announcement_publication(
                self.session, actor_telegram_user_id=OWNER_TG, draft_id=2
            )
        self.session.rollback()
        stored = get_announcement_draft(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=2
        )
        self.assertEqual(stored.revision, draft.revision)
        self.assertTrue(stored.publication_sent)


class PersonalWithoutPlaceTests(TemplateDatabaseTestCase):
    """Анонс по личному шаблону создаётся без площадки и без чтения `Place`."""

    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_user(OWNER_TG, username="personal-owner")
        self.template = self.make_personal_template(
            self.owner,
            "Личный без места",
            header="**[название]**",
            body="[дата] // [время_начала]",
            footer="[контакт]",
        )

    def test_personal_draft_has_no_place_and_needs_no_place_row(self) -> None:
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(Place)), 0
        )
        self.assertIsNone(self.template.place_id)
        draft = start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=self.template.template_id,
        )
        self.assertEqual(draft.origin, ORIGIN_PERSONAL)
        self.assertTrue(draft.is_personal)
        self.assertIsNone(draft.place_id)
        self.assertIsNone(draft.place_name_id)
        self.assertIsNone(draft.place_display_name)
        self.assertIsNone(draft.place_address)
        self.assertIsNone(draft.place_url)
        self.assertEqual(draft.stage, WizardStage.QUESTIONS)
        self.assertIsNotNone(draft.template_snapshot)
        assert draft.template_snapshot is not None
        self.assertEqual(draft.template_snapshot.source_kind, "personal")
        # Вопросы строятся по снимку, места в них нет.
        fields = build_announcement_fields(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=draft.draft_id
        )
        self.assertEqual(
            [field.code for field in fields],
            ["название", "дата", "время_начала", "контакт"],
        )

    def test_personal_mode_rejects_place_and_standard_template(self) -> None:
        with self.assertRaises(AnnouncementStageError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                origin=ORIGIN_PERSONAL,
                template_id=self.template.template_id,
                place_id=1,
            )
        with self.assertRaises(AnnouncementStageError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                origin="unknown",
                template_id=self.template.template_id,
            )
        with self.assertRaises(AnnouncementNotFoundError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                origin=ORIGIN_PERSONAL,
                template_id=999,
            )


class PersonalOneOffPlaceTests(AnnouncementDatabaseTestCase):
    """Кирпичик «Место» личного шаблона принимает разовое имя без записи."""

    def setUp(self) -> None:
        super().setUp()
        self.template = self.make_personal_template(
            self.owner,
            "Личный с местом",
            header="**[название]**",
            body="[место]\n[адрес]",
            footer="",
        )
        self.draft = start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=self.template.template_id,
        )
        assert self.draft.template_snapshot is not None
        self.brick_ids = snapshot_brick_ids_by_code(self.draft.template_snapshot)

    def test_one_off_place_is_not_written_to_directory(self) -> None:
        places_before = self.session.scalar(select(func.count()).select_from(Place))
        names_before = _scalar(self.path, "SELECT count(*) FROM place_names")[0]
        fields = build_announcement_fields(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=self.draft.draft_id
        )
        place_field = next(field for field in fields if field.code == "место")
        self.assertEqual(place_field.status, "missing")

        saved = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=self.draft.draft_id,
            brick_id=self.brick_ids["место"],
            value="Разовая площадка у пруда",
            expected_revision=self.draft.revision,
        )
        self.assertEqual(saved.origin, ORIGIN_PERSONAL)
        self.assertIsNone(saved.place_id)
        self.assertEqual(saved.place_display_name, None)
        # Значение живёт только в announcement_values.
        place_value = saved.value_for(self.brick_ids["место"])
        assert place_value is not None
        self.assertEqual(place_value.payload, {"text": "Разовая площадка у пруда"})
        self.assertEqual(
            self.session.scalar(select(func.count()).select_from(Place)),
            places_before,
        )
        self.assertEqual(
            _scalar(self.path, "SELECT count(*) FROM place_names")[0], names_before
        )
        preview = build_announcement_preview(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=saved.draft_id,
        )
        self.assertIn("Разовая площадка у пруда", preview.html)
        self.assertEqual(preview.draft.origin, ORIGIN_PERSONAL)

    def test_autofill_only_touches_bricks_of_the_snapshot(self) -> None:
        # Личный черновик: в снимке есть `[адрес]`, но площадки нет,
        # поэтому автоподстановка ничего не придумывает.
        payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=self.draft.draft_id
        )
        self.assertIn(self.brick_ids["адрес"], payload.definitions_by_id)
        self.assertNotIn(self.brick_ids["адрес"], payload.values_by_id)

        # Стандарт с `[адрес]`: адрес карточки подставляется автоматически.
        known = self.start_at_questions()
        known_ids = snapshot_brick_ids_by_code(known.template_snapshot)
        self.assertIn("адрес", known_ids)
        known_payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=known.draft_id
        )
        self.assertEqual(
            known_payload.values_by_id[known_ids["адрес"]], "ул. Тестовая, 1"
        )

        # Стандарт без `[адрес]`: кирпичика нет в снимке, и адрес карточки
        # не появляется в значениях рендера.
        update_template_body(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            template_id=self.standard.template_id,
            expected_revision=self.standard.revision,
            content="Только [название]",
        )
        self.session.expire_all()
        without = self.start_at_questions()
        without_ids = snapshot_brick_ids_by_code(without.template_snapshot)
        self.assertNotIn("адрес", without_ids)
        self.assertNotIn("адрес_места", without_ids)
        without_payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=without.draft_id
        )
        self.assertNotIn(self.brick_ids["адрес"], without_payload.values_by_id)
        self.assertNotIn(
            SYSTEM_BRICKS_BY_CODE["адрес_места"].id,
            without_payload.values_by_id,
        )


class InitialPersonalTemplateTests(AnnouncementDatabaseTestCase):
    """Первый личный шаблон выдаётся один раз и не воскресает сам."""

    def test_repeated_call_returns_same_template_without_duplicates(self) -> None:
        first = ensure_initial_personal_template(
            self.session, actor_telegram_user_id=OWNER_TG
        )
        second = ensure_initial_personal_template(
            self.session, actor_telegram_user_id=OWNER_TG
        )
        self.assertEqual(first.template_id, second.template_id)
        templates = list_personal_templates(self.session, OWNER_TG)
        self.assertEqual([item.template_id for item in templates], [first.template_id])
        self.assertEqual(templates[0].name, INITIAL_PERSONAL_TEMPLATE_NAME)
        # Копия — точный эталон §6.1.1 и отдельная от BUILTIN версия.
        version = templates[0].current_version
        self.assertEqual(version.header, BUILTIN_TEMPLATE.header)
        self.assertEqual(version.body, BUILTIN_TEMPLATE.body)
        self.assertEqual(version.footer, BUILTIN_TEMPLATE.footer)
        self.assertEqual(BUILTIN_TEMPLATE_VERSION, 3)
        # Чужой пользователь получает свою копию, а не шаблон владельца.
        other = ensure_initial_personal_template(
            self.session, actor_telegram_user_id=OTHER_TG
        )
        self.assertNotEqual(other.template_id, first.template_id)

    def test_deleted_template_is_not_resurrected(self) -> None:
        initial = ensure_initial_personal_template(
            self.session, actor_telegram_user_id=OWNER_TG
        )
        # Пользователь удалил шаблон: чистим его строки (в БД нет сервиса
        # удаления, поэтому фикстура делает это напрямую и без FK-каскада).
        self._delete_template(initial.template_id)
        self.assertEqual(list_personal_templates(self.session, OWNER_TG), [])

        # Рестарт процесса (новые движок/сессия и повторный прогон миграций)
        # не создаёт шаблон заново: самовосстановления нет.
        restarted = make_engine(self.path)
        self.addCleanup(restarted.dispose)
        self.assertEqual(run_migrations(restarted), LATEST_VERSION)
        session = sessionmaker(bind=restarted, expire_on_commit=False)()
        self.addCleanup(session.close)
        self.assertEqual(list_personal_templates(session, OWNER_TG), [])

        # Собственный шаблон пользователя остаётся единственным: повторный
        # вызов сервиса не добавляет второй «Мой шаблон».
        own = self.make_personal_template(
            self.owner, "Мой рабочий", body="Текст [название]"
        )
        again = ensure_initial_personal_template(
            session, actor_telegram_user_id=OWNER_TG
        )
        self.assertEqual(again.template_id, own.template_id)
        self.assertEqual(
            [item.template_id for item in list_personal_templates(session, OWNER_TG)],
            [own.template_id],
        )

    def _delete_template(self, template_id: int) -> None:
        connection = sqlite3.connect(self.path)
        try:
            version_ids = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM template_versions WHERE template_id = ?",
                    (template_id,),
                )
            ]
            connection.execute("DELETE FROM templates WHERE id = ?", (template_id,))
            for version_id in version_ids:
                connection.execute(
                    "DELETE FROM template_versions WHERE id = ?", (version_id,)
                )
                connection.execute(
                    "DELETE FROM template_version_bricks "
                    "WHERE template_version_id = ?",
                    (version_id,),
                )
            connection.commit()
        finally:
            connection.close()
        self.session.expire_all()


class AddressCodeCollisionTests(unittest.TestCase):
    """Миграция 15 не падает, если код `адрес` уже занят несистемным кирпичиком."""

    _LEGACY_SQL = {
        "personal": (
            "INSERT INTO bricks (id, code, title, type, scope, source, "
            "rules_json, status, owner_user_id, created_by_user_id, "
            "created_at, updated_at, revision) VALUES "
            "(10, 'адрес', 'Мой адрес', 'short_text', 'personal', 'ask', NULL, "
            "'{status}', 1, 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', 1)"
        ),
        "place": (
            "INSERT INTO bricks (id, code, title, type, scope, source, "
            "rules_json, status, place_id, created_by_user_id, "
            "created_at, updated_at, revision) VALUES "
            "(10, 'адрес', 'Адрес площадки', 'short_text', 'place', 'ask', NULL, "
            "'{status}', 1, 1, '2026-01-02 00:00:00', '2026-01-02 00:00:00', 1)"
        ),
    }

    def _migrate_with_legacy_address(self, scope: str, status: str) -> None:
        self.path = temp_db_path("poster2-address-collision-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:14]), 14)

        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO users (id, telegram_user_id, created_at) "
                "VALUES (1, ?, '2026-01-01 00:00:00')",
                (OWNER_TG,),
            )
            connection.execute(
                "INSERT INTO places (id, created_by_user_id, created_at, address) "
                "VALUES (1, 1, '2026-01-01 00:00:00', 'ул. Старая, 7')"
            )
            connection.execute(self._LEGACY_SQL[scope].format(status=status))
            connection.commit()
        finally:
            connection.close()

        # Главное утверждение дефекта: миграция проходит, а не падает с
        # `system brick code is reserved`.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            # Легаси-строка сохранена без изменений: id, код, область, статус.
            legacy = connection.execute(
                "SELECT id, code, scope, status, title FROM bricks WHERE id = 10"
            ).fetchone()
            self.assertEqual(legacy, (10, "адрес", scope, status, legacy[4]))
            # Системный кирпичик создан отдельной строкой с собственным ID.
            system_row = connection.execute(
                "SELECT id, code, title, source FROM bricks "
                "WHERE scope = 'system' AND code = 'адрес'"
            ).fetchall()
            self.assertEqual(len(system_row), 1)
            self.assertEqual(system_row[0][1:], ("адрес", "Адрес", "place"))
            self.assertNotEqual(system_row[0][0], 10)
            # Триггер резервирования восстановлен дословно.
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='trg_bricks_reserve_codes_insert'"
            ).fetchone()
            self.assertIsNotNone(trigger)
            self.assertIn("system brick code is reserved", trigger[0])
        finally:
            connection.close()

        self.session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(self.session.close)
        self.session.expire_all()

        # Системный `[адрес]` доступен и берёт значение из карточки места.
        system = get_system_brick(self.session, "адрес")
        assert system is not None
        self.assertEqual(system.scope, "system")
        self.assertIn(
            "адрес", [brick.code for brick in list_system_bricks(self.session)]
        )
        # Легаси-кирпичик не сломан: виден владельцу/в контексте места.
        place_id = 1 if scope == "place" else None
        legacy_view = get_visible_brick(
            self.session,
            10,
            viewer_telegram_user_id=OWNER_TG,
            place_id=place_id,
            include_archived=True,
        )
        assert legacy_view is not None
        self.assertEqual(legacy_view.code, "адрес")
        self.assertEqual(legacy_view.scope, scope)
        self.assertEqual(legacy_view.status, status)

    def test_personal_address_code_active_migrates(self) -> None:
        self._migrate_with_legacy_address("personal", "active")

    def test_personal_address_code_archived_migrates(self) -> None:
        self._migrate_with_legacy_address("personal", "archived")

    def test_place_address_code_active_migrates(self) -> None:
        self._migrate_with_legacy_address("place", "active")

    def test_reserved_code_trigger_still_blocks_new_system_code(self) -> None:
        self._migrate_with_legacy_address("personal", "active")
        # Инвариант future-записей не ослаблен: новый системный код поверх
        # несистемного по-прежнему запрещён триггером.
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "INSERT INTO bricks (id, code, title, type, scope, source, "
                "rules_json, status, owner_user_id, created_by_user_id, "
                "created_at, updated_at, revision) VALUES (99, 'мой2', "
                "'Мой 2', 'short_text', 'personal', 'ask', NULL, 'active', 1, 1, "
                "'2026-01-03 00:00:00', '2026-01-03 00:00:00', 1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO bricks (id, code, title, type, scope, source, "
                    "rules_json, status, created_at, updated_at, revision) "
                    "VALUES (100, 'мой2', 'Системный', 'short_text', 'system', "
                    "'ask', NULL, 'active', '2026-01-03 00:00:00', "
                    "'2026-01-03 00:00:00', 1)"
                )
        finally:
            connection.close()

    def test_legacy_owner_can_rename_but_not_create_new_reserved_code(self) -> None:
        self._migrate_with_legacy_address("personal", "active")
        # Владелец легаси-кирпичика меняет название, не меняя свой код:
        # обещание docs/migrations.md (S1) обязано выполняться.
        updated = update_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=10,
            expected_revision=1,
            code="адрес",
            title="Адрес студии",
            rules=None,
        )
        self.assertEqual(updated.status, "updated", updated.message)
        assert updated.brick is not None
        self.assertEqual(updated.brick.code, "адрес")
        self.assertEqual(updated.brick.title, "Адрес студии")
        self.assertEqual(updated.brick.scope, "personal")

        # Новый кирпичик с системным кодом по-прежнему запрещён — и в личной,
        # и в местной области, и при смене кода на `адрес`.
        with self.assertRaises(BrickConflictError):
            create_personal_brick(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                code="адрес",
                title="Второй адрес",
                value_type="short_text",
            )
        place_availability = check_code_availability(
            self.session, "адрес", scope="place", place_id=1
        )
        self.assertFalse(place_availability.available)
        self.assertEqual(place_availability.reason, "system_reserved")
        other = create_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            code="свой_адрес",
            title="Свой адрес",
            value_type="short_text",
        )
        renamed = update_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=other.id,
            expected_revision=1,
            code="адрес",
            title="Свой адрес",
            rules=None,
        )
        self.assertEqual(renamed.status, "conflict")

        # Системный `[адрес]` пользователю не принадлежит и не редактируется.
        system = get_system_brick(self.session, "адрес")
        assert system is not None
        self.assertNotEqual(system.id, 10)
        own_system = update_personal_brick(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            brick_id=system.id,
            expected_revision=system.revision,
            code="адрес",
            title="Подмена",
            rules=None,
        )
        self.assertEqual(own_system.status, "not_found")


class AddressBrickAndDescriptionTests(AnnouncementDatabaseTestCase):
    """Системный `[адрес]` и `description` кирпичика."""

    def test_address_brick_is_system_and_renders_from_place_card(self) -> None:
        codes = [brick.code for brick in list_system_bricks(self.session)]
        self.assertEqual(len(codes), 10)
        self.assertIn("адрес", codes)
        # Старый код не переименован: сохранённые снимки продолжают работать.
        self.assertIn("адрес_места", codes)
        address = get_system_brick(self.session, "адрес")
        assert address is not None
        self.assertEqual(address.title, "Адрес")
        self.assertEqual(address.value_type, "short_text")
        self.assertEqual(address.source, "place")

        draft = self.start_at_questions()
        payload = build_announcement_render_payload(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=draft.draft_id
        )
        brick_ids = snapshot_brick_ids_by_code(draft.template_snapshot)
        self.assertEqual(payload.values_by_id[brick_ids["адрес"]], "ул. Тестовая, 1")
        preview = build_announcement_preview(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=draft.draft_id
        )
        self.assertIn("ул. Тестовая, 1", preview.html)

    def test_description_is_stored_and_never_injected_as_html(self) -> None:
        raw = '<b>Адрес</b> & "кавычки"'
        self.assertIsNone(
            self.session.scalar(select(Brick.description).where(Brick.code == "адрес"))
        )
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE bricks SET description=:d WHERE code='адрес'"),
                {"d": raw},
            )
        self.session.expire_all()
        stored = self.session.scalar(
            select(Brick.description).where(Brick.code == "адрес")
        )
        # Описание хранится дословно: это не шаблон и не разметка.
        self.assertEqual(stored, raw)

        draft = self.start_at_questions()
        preview = build_announcement_preview(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=draft.draft_id
        )
        self.assertNotIn("Адрес</b>", preview.html)
        self.assertNotIn("кавычки", preview.html)
        # Для вывода в интерфейс текст экранируется целиком.
        self.assertEqual(
            escape_html(raw), "&lt;b&gt;Адрес&lt;/b&gt; &amp; &quot;кавычки&quot;"
        )


class AccessAndConflictTests(AnnouncementDatabaseTestCase):
    """Чужой личный шаблон недоступен; CAS-конфликт не теряет данные."""

    def test_other_user_personal_template_is_not_found(self) -> None:
        template = self.make_personal_template(
            self.owner, "Личный владельца", body="Текст [название]"
        )
        with self.assertRaises(AnnouncementNotFoundError):
            start_announcement_wizard(
                self.session,
                actor_telegram_user_id=OTHER_TG,
                origin=ORIGIN_PERSONAL,
                template_id=template.template_id,
            )
        # Владелец по-прежнему может начать свой анонс.
        draft = start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=template.template_id,
        )
        self.assertEqual(draft.origin, ORIGIN_PERSONAL)

    def test_stale_revision_cannot_overwrite_personal_draft(self) -> None:
        template = self.make_personal_template(
            self.owner, "Личный CAS", body="[название]"
        )
        draft = start_announcement_wizard(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            origin=ORIGIN_PERSONAL,
            template_id=template.template_id,
        )
        assert draft.template_snapshot is not None
        brick_ids = snapshot_brick_ids_by_code(draft.template_snapshot)
        first = save_announcement_value(
            self.session,
            actor_telegram_user_id=OWNER_TG,
            draft_id=draft.draft_id,
            brick_id=brick_ids["название"],
            value="Первое",
            expected_revision=draft.revision,
        )
        with self.assertRaises(AnnouncementConflictError):
            save_announcement_value(
                self.session,
                actor_telegram_user_id=OWNER_TG,
                draft_id=draft.draft_id,
                brick_id=brick_ids["название"],
                value="Второе",
                expected_revision=draft.revision,
            )
        self.session.rollback()
        stored = get_announcement_draft(
            self.session, actor_telegram_user_id=OWNER_TG, draft_id=draft.draft_id
        )
        self.assertEqual(stored.revision, first.revision)
        stored_value = stored.value_for(brick_ids["название"])
        assert stored_value is not None
        self.assertEqual(stored_value.payload, {"text": "Первое"})


if __name__ == "__main__":
    unittest.main()
