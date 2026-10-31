# tests/test_poster2_r2_owner_tester.py
"""
R2 — независимая проверка требований владельца (poster-tester).

Ничего не импортируется из «ожидаемых» констант продукта: эталон анонса,
эталоны v2/v3 и имя стартовой копии записаны здесь ЛИТЕРАЛЬНО. Проверки идут
через настоящее приложение `bot.build_application`, FakeBot и временную SQLite
(`tests.integration_harness`) либо через миграции на временной БД. Реальная
`post_bot.db`, `.env`, сеть и Telegram не используются.

Покрыто:
1. BUILTIN v3: жирный заголовок, пустая строка, жирное «дата // время»,
   пустая строка, жирная КЛИКАБЕЛЬНАЯ площадка, нежирный адрес строкой ниже,
   пустая строка, описание, пустая строка, контакт — побайтово в отправленном
   сообщении; пропуски сохраняют `//`; пустой шаблон → «.».
2. Миграция 18 на заполненной БД версии 17: обновляются только нетронутые
   копии, имя меняется вместе с содержимым, правки не трогаются, повторный
   запуск ничего не меняет, FK/integrity целы.
3. Новый пользователь (/start) получает «Дефолтный шаблон» с текстом v3.
4. №25 «Управление → Шаблоны»: ни один путь раздела не запускает анонс;
   удаление с историей — понятный отказ без потери данных; подделки отклонены.
5. №26 «Управление → Площадки»: карточка, права трёх ролей, IDOR, создание
   места со стандартом v3 без анонса, регресс `pl_pick` → анонс, stale.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import unittest
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from migrations import DEFAULT_MIGRATIONS, LATEST_VERSION, run_migrations
from tests.stage19a_helpers import make_engine, temp_db_path

try:
    from handlers.announcement_wizard import CONVERSATION_NAME
    from models import (
        AnnouncementDraft,
        Place,
        PlaceAdmin,
        Template,
        TemplateSnapshot,
        TemplateVersion,
        utcnow,
    )
    from place_state import load_place_session
    from place_store import add_alternative_name, create_place
    from template_state import load_template_session
    from template_store import (
        create_personal_template,
        get_personal_template,
        get_standard_template,
        list_personal_templates,
        render_template_version,
    )
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        scenario_test,
    )
    from user_store import get_or_create_user

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001
    INTEGRATION_IMPORT_ERROR = str(exc)


# --- Эталоны владельца, записанные буквально ---------------------------------

OWNER_REFERENCE = (
    "<b>Летний концерт</b>\n"
    "\n"
    "<b>12.07.2027 // 19:00</b>\n"
    '<b><a href="https://example.com/place">Дом культуры</a></b>\n'
    "ул. Ленина, 1\n"
    "\n"
    "Большой зал, вход свободный.\n"
    "\n"
    "@contact"
)

V3_HEADER = "**[название]**"
V3_BODY = (
    "\n"
    "**[дата] // [время_начала]**\n"
    "\n"
    "**[место]([ссылка_места])**\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
V3_FOOTER = "\n[контакт]"

# Эталон v4 (Poster 2.0 U1a): после заголовка пустая строка, дата/время и
# площадка — соседние строки; тело — только описание, подвал — только контакт.
V4_HEADER = (
    "**[название]**\n\n**[дата] // [время_начала]**\n"
    "**[место]([ссылка_места])**\n[адрес]"
)
V4_BODY = "\n[описание]"
V4_FOOTER = "\n[контакт]"

V2_HEADER = "**[название]**"
V2_BODY = (
    "[дата] // [время_начала]\n"
    "[место]([ссылка_места])\n"
    "[адрес]\n"
    "\n"
    "[описание]"
)
V2_FOOTER = "[контакт]"


def _join(header: str, body: str, footer: str) -> str:
    """Склейка секций в единый `content` — формула backfill миграции 24."""
    if not (header.strip() or body.strip() or footer.strip()):
        return header + body + footer
    text = header.strip()
    if text and (body.strip() or footer.strip()):
        text += "\n"
    text += body
    if body.strip() and footer.strip():
        text += "\n"
    return text + footer


V4_CONTENT = _join(V4_HEADER, V4_BODY, V4_FOOTER)

OLD_INITIAL_NAME = "Мой шаблон"
NEW_INITIAL_NAME = "Дефолтный шаблон"

OTHER_TG = 8_930_002
SUPERADMIN_TG = 8_930_003
AW_QUESTIONS_PHASE = 2
PICK_CALLBACK = re.compile(r"^(tp|pl)_pick:\d+$")


# --- Помощники ---------------------------------------------------------------


def _alert(h) -> str:
    answers = h.answers()
    if not answers:
        return ""
    record = answers[-1]
    args = record.get("args") or ()
    return str(args[0]) if args else str(record.get("text") or "")


def _callbacks(h) -> list[str]:
    return [data for _label, data in h.screen_buttons() if data]


def _drafts(h) -> int:
    with h.session_factory() as session:
        return int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0)


def _seed_place(
    h,
    *,
    name: str = "Дом культуры",
    actor_tg: int = DEFAULT_USER_ID,
    admin: bool = False,
    address: str | None = "ул. Тестовая, 1",
    url: str | None = "https://example.com/place",
) -> int:
    with h.session_factory() as session:
        user = get_or_create_user(session, actor_tg, username=f"owner-{actor_tg}")
        place = create_place(
            session,
            name,
            city="Тестовый город",
            created_by_user_id=user.id,
            address=address,
            url=url,
        )
        if admin:
            session.add(
                PlaceAdmin(
                    place_id=place.id,
                    user_id=user.id,
                    assigned_by_user_id=None,
                    assigned_at=utcnow(),
                )
            )
        session.commit()
        return int(place.id)


def _personal_template_id(h, name: str = "Личный") -> int:
    with h.session_factory() as session:
        get_or_create_user(session, DEFAULT_USER_ID, username="owner")
        template = create_personal_template(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            name=name,
            content="\n".join(p for p in ("", "[описание]", "") if p),
        )
        session.commit()
        return int(template.template_id)


def _values(session) -> dict[int, str]:
    from brick_store import get_system_brick

    raw = {
        "название": "Летний концерт",
        "дата": "12.07.2027",
        "время_начала": "19:00",
        "место": "Дом культуры",
        "ссылка_места": "https://example.com/place",
        "адрес": "ул. Ленина, 1",
        "описание": "Большой зал, вход свободный.",
        "контакт": "@contact",
    }
    values: dict[int, str] = {}
    for code, value in raw.items():
        brick = get_system_brick(session, code)
        assert brick is not None, f"нет системного кирпичика {code}"
        values[int(brick.id)] = value
    return values


# ============================================================================
# 1. BUILTIN v3 — побайтово в реально отправленном сообщении
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DeliveredReferenceTests(unittest.TestCase):
    """Анонс по стандарту места уходит в чат ровно эталоном владельца."""

    @scenario_test
    async def test_place_announcement_post_is_byte_exact(self, h):
        place_id = _seed_place(h, name="Дом культуры", address="ул. Ленина, 1")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        # Вопросы: название → дата → время → описание → контакт.
        for answer in (
            "Летний концерт",
            "12.07.2027",
            "19:00",
            "Большой зал, вход свободный.",
            "@contact",
        ):
            await h.text(answer)
        no_photo = [data for data in _callbacks(h) if data.startswith("aw_photo_none:")]
        self.assertTrue(no_photo, f"нет кнопки «Без фотографий»: {h.screen_buttons()}")
        await h.callback(no_photo[0])

        posts = h.posts()
        self.assertTrue(posts, "анонс не отправлен")
        delivered = posts[-1]
        self.assertEqual(delivered, OWNER_REFERENCE)
        # Структура по строкам: жирность и пустые строки на своих местах.
        lines = (delivered or "").split("\n")
        # v4: между заголовком и «дата // время» ровно одна пустая строка.
        self.assertEqual(len(lines), 9, f"строки: {lines!r}")
        for index in (0, 2, 3):
            self.assertTrue(
                lines[index].startswith("<b>"), f"строка {index}: {lines[index]!r}"
            )
            self.assertTrue(
                lines[index].endswith("</b>"), f"строка {index}: {lines[index]!r}"
            )
        self.assertEqual(
            lines[3], '<b><a href="https://example.com/place">Дом культуры</a></b>'
        )
        self.assertEqual(lines[4], "ул. Ленина, 1")
        self.assertNotIn("<b>", lines[4], "адрес не должен быть жирным")
        for index in (1, 5, 7):
            self.assertEqual(lines[index], "", f"строка {index} должна быть пустой")
        self.assertNotIn("**", delivered)
        self.assertNotIn("[", delivered)
        self.assertNotIn("]", delivered)

    @scenario_test
    async def test_skipped_bricks_keep_literal_separator_no_markup(self, h):
        """Пропуск всех значений не стирает `//` и не оставляет сырой разметки."""
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        await h.callback(f"pl_pick:{place_id}")
        for _step in range(12):
            skips = [data for data in _callbacks(h) if data.startswith("aw_qskip:")]
            nones = [
                data for data in _callbacks(h) if data.startswith("aw_photo_none:")
            ]
            if skips:
                await h.callback(skips[0])
            elif nones:
                await h.callback(nones[0])
            else:
                break
        delivered = h.posts()[-1] or ""
        self.assertIn("//", delivered, "разделитель обязан сохраниться буквально")
        self.assertNotIn("**", delivered)
        self.assertNotIn("[", delivered)
        self.assertNotIn("]", delivered)

    @scenario_test
    async def test_empty_custom_template_gives_single_dot(self, h):
        """Пустой шаблон без литералов → ровно «.» (не пустое сообщение)."""
        from announcement_store import (
            build_announcement_preview,
            start_announcement_wizard,
        )
        from brick_store import get_system_brick

        with h.session_factory() as session:
            get_or_create_user(session, DEFAULT_USER_ID, username="owner")
            template = create_personal_template(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                name="Только текст",
                content="\n".join(p for p in ("", "[контакт]", "") if p),
            )
            session.commit()
            draft = start_announcement_wizard(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                origin="personal",
                template_id=template.template_id,
            )
            preview = build_announcement_preview(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                draft_id=draft.draft_id,
            )
            self.assertEqual(preview.html, ".")
            contact = get_system_brick(session, "контакт")
            self.assertIsNotNone(contact)


# ============================================================================
# 2. Миграция 18 на заполненной БД версии 17
# ============================================================================


def _sql(path: str, statement: str, parameters: tuple = ()) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(statement, parameters).fetchall()
    finally:
        connection.close()


def _write(path: str, statement: str, parameters: tuple = ()) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(statement, parameters)
        connection.commit()
    finally:
        connection.close()


class MigrationEighteenOwnerTests(unittest.TestCase):
    """Требование 2: обновляются только доказуемо нетронутые копии v2."""

    def setUp(self) -> None:
        self.assertEqual(LATEST_VERSION, 24)
        self.path = temp_db_path("poster-r2-owner-m18-")
        self.addCleanup(shutil.rmtree, os.path.dirname(self.path), True)
        self.engine = make_engine(self.path)
        self.addCleanup(self.engine.dispose)
        self.assertEqual(run_migrations(self.engine, DEFAULT_MIGRATIONS[:17]), 17)
        _write(
            self.path,
            "INSERT INTO users (id, telegram_user_id, created_at) "
            "VALUES (1, 111, '2026-01-01 00:00:00')",
        )
        _write(
            self.path,
            "INSERT INTO places (id, created_by_user_id, created_at) "
            "VALUES (1, 1, '2026-01-01 00:00:00')",
        )
        self._seed()

    # — заполнение реалистичными копиями —
    def _personal(
        self,
        template_id: int,
        *,
        name: str = OLD_INITIAL_NAME,
        body: str = V2_BODY,
        header: str = V2_HEADER,
        footer: str = V2_FOOTER,
        builtin: int | None = 2,
        revision: int = 1,
    ) -> None:
        _write(
            self.path,
            "INSERT INTO templates (id, kind, owner_user_id, name, revision, "
            "created_by_user_id, created_at, updated_at) "
            "VALUES (?, 'personal', 1, ?, ?, 1, '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')",
            (template_id, name, revision),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, '2026-01-01 00:00:00', ?)",
            (template_id, template_id, revision, header, body, footer, builtin),
        )

    def _standard(self, template_id: int) -> None:
        _write(
            self.path,
            "INSERT INTO templates (id, kind, place_id, revision, created_at, "
            "updated_at) VALUES (?, 'standard', 1, 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (template_id,),
        )
        _write(
            self.path,
            "INSERT INTO template_versions (id, template_id, version, header, "
            "body, footer, created_by_user_id, created_at, builtin_version) "
            "VALUES (?, ?, 1, ?, ?, ?, NULL, '2026-01-01 00:00:00', 2)",
            (template_id, template_id, V2_HEADER, V2_BODY, V2_FOOTER),
        )

    def _seed(self) -> None:
        # 1 — нетронутая стартовая копия v2 (обновляется + переименовывается).
        self._personal(1)
        # 2 — нетронутый стандарт места (обновляется, имя остаётся NULL).
        self._standard(2)
        # 3 — тронутое человеком тело (не трогается, имя сохраняется).
        self._personal(3, body=V2_BODY + "\n\nмоя подпись")
        # 4 — переименованная, но нетронутая по тексту копия (не трогается).
        self._personal(4, name="Копия для концертов")

    def test_only_untouched_copies_get_v4_and_rename_happens_with_it(self) -> None:
        # Возвращается LATEST_VERSION: R3 добавил миграцию 19, R4 — миграцию
        # 20 (обе no-op для этого теста), логика миграции 18 не менялась.
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)

        # Нетронутая личная копия: новая версия ровно с текстом v3, старая цела.
        rows = _sql(
            self.path,
            "SELECT version, content, builtin_version "
            "FROM template_versions WHERE template_id = 1 ORDER BY version",
        )
        self.assertEqual(len(rows), 3, "история: v2, v3 и новая v4")
        self.assertEqual(rows[0], (1, _join(V2_HEADER, V2_BODY, V2_FOOTER), 2))
        self.assertEqual(rows[1], (2, _join(V3_HEADER, V3_BODY, V3_FOOTER), 3))
        self.assertEqual(rows[2], (3, V4_CONTENT, 4))
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 1"),
            [(NEW_INITIAL_NAME, 3)],
            "имя меняется в той же миграции, что и содержимое",
        )

        # Нетронутый стандарт: v3, name остаётся NULL, ревизия 2.
        standard = _sql(
            self.path,
            "SELECT version, content FROM template_versions "
            "WHERE template_id = 2 ORDER BY version",
        )
        self.assertEqual(len(standard), 3)
        self.assertEqual(standard[1], (2, _join(V3_HEADER, V3_BODY, V3_FOOTER)))
        self.assertEqual(standard[2], (3, V4_CONTENT))
        self.assertEqual(
            _sql(self.path, "SELECT name, revision FROM templates WHERE id = 2"),
            [(None, 3)],
        )

        # Тронутая и переименованная копии не изменились вообще.
        for template_id, expected_name in (
            (3, OLD_INITIAL_NAME),
            (4, "Копия для концертов"),
        ):
            with self.subTest(template_id=template_id):
                self.assertEqual(
                    _sql(
                        self.path,
                        "SELECT name, revision FROM templates WHERE id = ?",
                        (template_id,),
                    ),
                    [(expected_name, 1)],
                )
                self.assertEqual(
                    _sql(
                        self.path,
                        "SELECT version, builtin_version FROM template_versions "
                        "WHERE template_id = ?",
                        (template_id,),
                    ),
                    [(1, 2)],
                )
        # Ни одного переименования без новой версии.
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id FROM templates WHERE name = ? AND revision = 1",
                (NEW_INITIAL_NAME,),
            ),
            [],
        )

    def test_repeat_run_is_noop_and_schema_is_clean(self) -> None:
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        templates = _sql(
            self.path, "SELECT id, name, revision FROM templates ORDER BY id"
        )
        versions = _sql(
            self.path,
            "SELECT id, template_id, version, content, builtin_version "
            "FROM template_versions ORDER BY id",
        )
        self.assertEqual(run_migrations(self.engine), LATEST_VERSION)
        self.assertEqual(
            _sql(self.path, "SELECT id, name, revision FROM templates ORDER BY id"),
            templates,
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT id, template_id, version, content, builtin_version "
                "FROM template_versions ORDER BY id",
            ),
            versions,
        )
        self.assertEqual(
            _sql(
                self.path,
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'migration_%'",
            ),
            [],
            "временная таблица-очередь не остаётся",
        )
        self.assertEqual(_sql(self.path, "PRAGMA foreign_key_check"), [])
        self.assertEqual(_sql(self.path, "PRAGMA integrity_check"), [("ok",)])
        # У каждого шаблона есть текущая версия (указатель не «висит»).
        self.assertEqual(
            _sql(
                self.path,
                "SELECT count(*) FROM templates AS t WHERE NOT EXISTS "
                "(SELECT 1 FROM template_versions AS v "
                "WHERE v.template_id = t.id AND v.version = t.revision)",
            ),
            [(0,)],
        )

    def test_migrated_copy_renders_owner_reference(self) -> None:
        run_migrations(self.engine)
        from brick_store import list_system_bricks

        session = sessionmaker(bind=self.engine, expire_on_commit=False)()
        self.addCleanup(session.close)
        by_code = {item.code: item for item in list_system_bricks(session)}
        values = {
            by_code["название"].id: "Летний концерт",
            by_code["дата"].id: "12.07.2027",
            by_code["время_начала"].id: "19:00",
            by_code["место"].id: "Дом культуры",
            by_code["ссылка_места"].id: "https://example.com/place",
            by_code["адрес"].id: "ул. Ленина, 1",
            by_code["описание"].id: "Большой зал, вход свободный.",
            by_code["контакт"].id: "@contact",
        }
        rendered = render_template_version(
            session,
            actor_telegram_user_id=111,
            template_id=1,
            values_by_id=values,
        )
        self.assertEqual(rendered, OWNER_REFERENCE)


# ============================================================================
# 3. Новый пользователь
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class NewUserDefaultsTests(unittest.TestCase):
    @scenario_test
    async def test_start_creates_default_template_with_v4_content(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            rows = list_personal_templates(session, DEFAULT_USER_ID)
            self.assertEqual(len(rows), 1)
            template = rows[0]
            self.assertEqual(template.name, NEW_INITIAL_NAME)
            version = template.current_version
            self.assertEqual(version.content, V4_CONTENT)
            self.assertIsNone(version.extended_content)
            rendered = render_template_version(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=template.template_id,
                values_by_id=_values(session),
            )
        self.assertEqual(rendered, OWNER_REFERENCE)
        # Повторный /start не выдаёт вторую копию.
        await h.text("/start")
        with h.session_factory() as session:
            self.assertEqual(len(list_personal_templates(session, DEFAULT_USER_ID)), 1)


# ============================================================================
# 4. №25 «Управление → Шаблоны» — только управление
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class TemplatesSectionNeverStartsAnnouncementTests(unittest.TestCase):
    @scenario_test
    async def test_no_path_from_section_starts_announcement(self, h):
        """Ни один экран/кнопка раздела не даёт старта анонса и не создаёт черновик."""
        template_id = _personal_template_id(h, name="Управляемый")
        await h.text("/start")
        # Сохранённая фаза выбора из соседнего входа не должна возобновиться.
        await h.callback("pick_template")
        self.assertIn(f"tp_pick:{template_id}", _callbacks(h))

        await h.callback("manage_templates")
        self.assertEqual(
            (load_template_session(DEFAULT_USER_ID) or {}).get("phase"), "hub"
        )
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        await h.callback(f"tp_open:{template_id}")

        seen: set[str] = set()
        for _round in range(12):
            current = _callbacks(h)
            pending = [
                data
                for data in current
                if data.startswith("tp_")
                and data not in seen
                and not data.startswith(("tp_delete_yes", "tp_reset_yes"))
            ]
            if not pending:
                break
            data = pending[0]
            seen.add(data)
            await h.callback(data)
            self.assertIsNone(
                h.dialog_state(name=CONVERSATION_NAME),
                f"кнопка {data} открыла конструктор анонса",
            )
            self.assertEqual(_drafts(h), 0, f"кнопка {data} создала черновик")
            self.assertFalse(
                any(PICK_CALLBACK.match(item) for item in _callbacks(h)),
                f"после {data} появился старт анонса: {_callbacks(h)}",
            )
            if data.startswith(("tp_reset", "tp_delete")):
                # Подтверждения не нажимаем: возвращаемся в карточку.
                await h.callback(f"tp_open:{template_id}")
        self.assertGreater(len(seen), 4, "карточка проверена не полностью")
        self.assertEqual(_drafts(h), 0)

    @scenario_test
    async def test_delete_with_history_is_refused_without_data_loss(self, h):
        await h.text("/start")
        with h.session_factory() as session:
            template = list_personal_templates(session, DEFAULT_USER_ID)[0]
            template_id = int(template.template_id)
            snapshots_before = int(
                session.scalar(
                    select(func.count(TemplateSnapshot.id)).where(
                        TemplateSnapshot.source_template_id == template_id
                    )
                )
                or 0
            )
            versions_before = int(
                session.scalar(
                    select(func.count(TemplateVersion.id)).where(
                        TemplateVersion.template_id == template_id
                    )
                )
                or 0
            )
        self.assertGreaterEqual(snapshots_before, 1, "у стартовой копии есть снимок")

        await h.callback("manage_templates")
        await h.callback(f"tp_open:{template_id}")
        await h.callback(f"tp_delete:{template_id}")
        await h.callback(f"tp_delete_yes:{template_id}")

        self.assertIn("нельзя удалить", h.screen_text(), "нужен понятный отказ")
        with h.session_factory() as session:
            self.assertIsNotNone(
                get_personal_template(session, DEFAULT_USER_ID, template_id)
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateSnapshot.id)).where(
                            TemplateSnapshot.source_template_id == template_id
                        )
                    )
                    or 0
                ),
                snapshots_before,
            )
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateVersion.id)).where(
                            TemplateVersion.template_id == template_id
                        )
                    )
                    or 0
                ),
                versions_before,
            )
            self.assertEqual(
                int(session.scalar(select(func.count(AnnouncementDraft.id))) or 0), 0
            )
        state = load_template_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "card", "пользователь остаётся в карточке")
        self.assertIn(f"tp_delete:{template_id}", _callbacks(h))

    @scenario_test
    async def test_forged_callbacks_are_rejected(self, h):
        place_id = _seed_place(h, name="Стандарт", admin=True)
        personal_id = _personal_template_id(h, name="Личный")
        with h.session_factory() as session:
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            assert standard is not None
            standard_id = int(standard.template_id)
            versions_before = int(
                session.scalar(
                    select(func.count(TemplateVersion.id)).where(
                        TemplateVersion.template_id == standard_id
                    )
                )
                or 0
            )

        await h.text("/start")
        await h.callback("manage_templates")
        await h.callback(f"tp_open:{personal_id}")
        # Подделка: удаление стандарта из карточки личного шаблона.
        await h.callback(f"tp_delete:{standard_id}")
        self.assertIn("устарела", _alert(h).lower())
        await h.callback(f"tp_delete_yes:{personal_id}")
        self.assertIn("устарела", _alert(h).lower())
        with h.session_factory() as session:
            self.assertIsNotNone(
                get_personal_template(session, DEFAULT_USER_ID, personal_id)
            )
            standard = get_standard_template(session, DEFAULT_USER_ID, place_id)
            self.assertIsNotNone(standard)
            assert standard is not None
            self.assertEqual(standard.template_id, standard_id)
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(TemplateVersion.id)).where(
                            TemplateVersion.template_id == standard_id
                        )
                    )
                    or 0
                ),
                versions_before,
                "стандарт места не переписан",
            )


# ============================================================================
# 5. №26 «Управление → Площадки» — справочник
# ============================================================================


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class PlaceHandbookOwnerTests(unittest.TestCase):
    @scenario_test
    async def test_card_shows_name_aliases_address_and_link(self, h):
        place_id = _seed_place(
            h,
            name="Дом культуры",
            address="ул. Ленина, 1",
            url="https://example.com/place",
        )
        with h.session_factory() as session:
            user = get_or_create_user(session, DEFAULT_USER_ID, username="owner")
            add_alternative_name(session, place_id, "ДК", created_by_user_id=user.id)
            session.commit()

        await h.text("/start")
        await h.callback("manage_places")
        text = h.screen_text()
        self.assertIn("Дом культуры", text)
        self.assertFalse(
            any(PICK_CALLBACK.match(item) for item in _callbacks(h)),
            "справочник не должен предлагать старт анонса",
        )
        self.assertEqual(_drafts(h), 0)

        await h.callback(f"plm_open:{place_id}")
        card = h.screen_text()
        self.assertIn("Дом культуры", card)
        self.assertIn("Альтернативные названия: 1", card)
        self.assertIn("ул. Ленина, 1", card)
        self.assertIn("https://example.com/place", card)

    @scenario_test
    async def test_three_roles_actions(self, h):
        own = _seed_place(h, name="Своё", admin=True)
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)

        # Админ места: правка есть, назначения админов и заявки нет.
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{own}")
        buttons = _callbacks(h)
        self.assertIn(f"plm_edit:{own}", buttons)
        self.assertNotIn(f"plm_admins:{own}", buttons)
        self.assertNotIn(f"plm_admin_request:{own}", buttons)

        # Не-админ: только просмотр и «Я админ места».
        await h.callback("plm_back")
        await h.callback(f"plm_open:{foreign}")
        buttons = _callbacks(h)
        self.assertNotIn(f"plm_edit:{foreign}", buttons)
        self.assertNotIn(f"plm_admins:{foreign}", buttons)
        self.assertIn(f"plm_admin_request:{foreign}", buttons)
        await h.callback(f"plm_admin_request:{foreign}")
        self.assertIn("Я админ места", h.screen_text())
        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(PlaceAdmin.id)).where(
                            PlaceAdmin.place_id == foreign
                        )
                    )
                    or 0
                ),
                0,
                "кнопка заявки не выдаёт права",
            )

        # Суперадмин: правка и администраторы любого места.
        with patch("config.SUPERADMIN_USER_IDS", (SUPERADMIN_TG,)):
            await h.text("/start", user_id=SUPERADMIN_TG)
            await h.callback("manage_places", user_id=SUPERADMIN_TG)
            await h.callback(f"plm_open:{foreign}", user_id=SUPERADMIN_TG)
            buttons = _callbacks(h)
            self.assertIn(f"plm_edit:{foreign}", buttons)
            self.assertIn(f"plm_admins:{foreign}", buttons)
            self.assertNotIn(f"plm_admin_request:{foreign}", buttons)

    @scenario_test
    async def test_idor_callbacks_are_denied(self, h):
        foreign = _seed_place(h, name="Чужое", actor_tg=OTHER_TG)
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{foreign}")
        # Карточка открыта тем же id: проверяется именно роль, а не совпадение.
        await h.callback(f"plm_edit:{foreign}")
        self.assertIn("Редактирование доступно администратору места", _alert(h))
        await h.callback(f"plm_admins:{foreign}")
        self.assertIn("только суперадминистратору", _alert(h))
        from admin_state import load_admin_session

        self.assertIsNone(load_admin_session(DEFAULT_USER_ID))
        state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "manage_card")
        with h.session_factory() as session:
            self.assertEqual(
                int(
                    session.scalar(
                        select(func.count(Template.id)).where(
                            Template.place_id == foreign, Template.kind == "standard"
                        )
                    )
                    or 0
                ),
                1,
                "стандарт чужого места не тронут",
            )

    @scenario_test
    async def test_stale_callbacks_on_new_phases_change_nothing(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback(f"plm_open:{place_id}")
        before = load_place_session(DEFAULT_USER_ID)
        # Пагинация списка и выдачи неприменима в фазе карточки.
        await h.callback("plm_page:8")
        self.assertIn("устарела", _alert(h).lower())
        await h.callback("plm_spage:8")
        self.assertIn("устарела", _alert(h).lower())
        # Stale-кнопки не меняют состояние: всё ещё карточка того же места.
        after_stale = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(after_stale, before or {}, "состояние не сброшено")
        self.assertEqual(after_stale.get("phase"), "manage_card")
        self.assertEqual(after_stale.get("place_id"), place_id)

        # `plm_back` в карточке — легитимная навигация, а не stale:
        # возвращает в список справочника и не запускает анонс.
        await h.callback("plm_back")
        back_state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(
            back_state.get("phase"), "manage_list", "plm_back ведёт в список"
        )
        self.assertNotIn("place_id", back_state)
        self.assertIn("Площадки", h.screen_text())
        self.assertEqual(_drafts(h), 0)

    @scenario_test
    async def test_created_place_gets_v4_standard_without_announcement(self, h):
        await h.text("/start")
        await h.callback("manage_places")
        await h.callback("plm_add")
        self.assertIn("Анонс не запускается", h.screen_text())
        await h.text("Новая площадка R2")
        await h.callback("pl_create_confirm")
        await h.text("Москва")

        self.assertIn("✅ Место добавлено", h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(_drafts(h), 0)
        with h.session_factory() as session:
            place = session.scalar(select(Place).order_by(Place.id.desc()))
            assert place is not None
            standard = get_standard_template(session, DEFAULT_USER_ID, int(place.id))
            self.assertIsNotNone(standard)
            assert standard is not None
            version = standard.current_version
            self.assertEqual(version.content, V4_CONTENT)
            self.assertIsNone(version.extended_content)
            rendered = render_template_version(
                session,
                actor_telegram_user_id=DEFAULT_USER_ID,
                template_id=standard.template_id,
                values_by_id=_values(session),
            )
        self.assertEqual(rendered, OWNER_REFERENCE)
        state = load_place_session(DEFAULT_USER_ID) or {}
        self.assertEqual(state.get("phase"), "manage_card")

    @scenario_test
    async def test_pick_place_tap_still_starts_announcement(self, h):
        place_id = _seed_place(h, name="Дом культуры")
        await h.text("/start")
        await h.callback("pick_place")
        self.assertIn(f"pl_pick:{place_id}", _callbacks(h))
        await h.callback(f"pl_pick:{place_id}")
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_QUESTIONS_PHASE)
        self.assertIn("Вопрос 1", h.screen_text())
        self.assertEqual(_drafts(h), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
