# tests/test_poster2_h1_stability_formatting.py
"""
Poster 2.3 H1: стабильность конструктора и поддержка Telegram-форматирования.

Сценарии (все через настоящее приложение `Application.process_update`,
FakeBot и временную SQLite):

1. рестарт службы посреди конструктора теряет in-memory состояние PTB —
   следующий текст продолжает сценарий и сохраняется в значение шага, а не
   проваливается в подсказку главного меню; второй ответ уходит уже в
   следующий шаг (диалог действительно живой);
2. рестарт на шаге фото: снимок и альбом сохраняются в черновик;
3. resume не срабатывает в группе (там прежняя подсказка) и без активного
   черновика (обычный текст вне конструктора — прежняя подсказка);
4. явный выход (`/cancel`, `/start`, `/help`, «🏠 В меню») закрывает приём
   ввода и ДО, и ПОСЛЕ рестарта: следующий текст/фото получает подсказку
   меню, сохранённые значения и фотографии не меняются;
5. битые данные (удалённый черновик, мусор в снимке) безопасно дают
   подсказку вместо падения;
6. entity `date_time` не ломает HTML-пайплайн: при захвате ввода она
   деградирует в читаемый текст, allow-list пропускает только безопасный
   `<tg-time>`;
7. цитаты (`blockquote`, `expandable_blockquote`) переживают весь путь:
   захват → БД → чтение → рендер, в том числе рядом с bold/spoiler/code,
   при редактировании и при доставке с фото и без.

Реальная `post_bot.db`, `.env` и сеть Telegram не используются.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from html.parser import HTMLParser

from sqlalchemy import delete, select, update
from telegram import MessageEntity

from utils.parsers import APP_TZ
from utils.tg_html import (
    capture_text,
    entities_to_html,
    sanitize_html,
    split_html,
)

try:
    from announcement_store import get_announcement_draft
    from handlers.announcement_wizard import AW_PREVIEW, CONVERSATION_NAME
    from models import AnnouncementDraft, AnnouncementValue, UserState
    from tests.integration_harness import (
        DEFAULT_USER_ID,
        HARNESS_IMPORT_ERROR,
        make_photo_update,
        scenario_test,
    )
    from tests.test_stage27b_announcement_ui import (
        _callback,
        _choose_personal,
        _latest_draft,
        _no_photos,
        _open_edit,
        _seed_personal_template,
        _seed_place,
        _start_to_template,
    )

    INTEGRATION_IMPORT_ERROR = HARNESS_IMPORT_ERROR
except Exception as exc:  # noqa: BLE001 — причина попадает в skip-сообщение
    INTEGRATION_IMPORT_ERROR = str(exc)


OUTSIDE_TEXT = "Воспользуйтесь кнопками меню"
OUTSIDE_PHOTO = "Фото добавляются в анонсе"

# Теги, которые Telegram допускает в parse_mode=HTML (allow-list Poster).
ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "blockquote",
        "a",
        "span",
        "tg-spoiler",
        "tg-emoji",
        "tg-time",
    }
)


class _TagValidator(HTMLParser):
    """Проверяет, что HTML состоит из разрешённых тегов и сбалансирован."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):  # noqa: ANN001, ANN201
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"запрещённый тег <{tag}>")
        self.stack.append(tag)

    def handle_endtag(self, tag):  # noqa: ANN001, ANN201
        if not self.stack:
            self.problems.append(f"лишний </{tag}>")
            return
        if self.stack[-1] != tag:
            self.problems.append(f"нарушен порядок: <{self.stack[-1]}> и </{tag}>")
            return
        self.stack.pop()

    def handle_entityref(self, name):  # noqa: ANN001, ANN201
        if name not in {"amp", "lt", "gt", "quot", "apos", "nbsp", "#39"}:
            self.problems.append(f"неожиданная сущность &{name};")


def assert_valid_html(case: unittest.TestCase, html: str) -> None:
    """HTML собран из разрешённых тегов и все теги закрыты."""
    validator = _TagValidator()
    validator.feed(html)
    validator.close()
    case.assertEqual(validator.problems, [], f"HTML сломан: {html!r}")
    case.assertEqual(validator.stack, [], f"незакрытые теги: {html!r}")


def ent(type_: str, offset: int, length: int, **kwargs) -> MessageEntity:
    return MessageEntity(type=type_, offset=offset, length=length, **kwargs)


def _stored_value(h, draft_id: int, brick_id: int) -> dict | None:
    """Сохранённое значение кирпичика или None (значение ещё не принято)."""
    with h.session_factory() as session:
        row = session.scalar(
            select(AnnouncementValue).where(
                AnnouncementValue.draft_id == draft_id,
                AnnouncementValue.brick_id == brick_id,
            )
        )
    if row is None or not row.value_json:
        return None
    return json.loads(row.value_json)


def _stored_payload(h, draft_id: int, brick_id: int) -> dict:
    """Сохранённое значение кирпичика; отсутствие — ошибка теста."""
    payload = _stored_value(h, draft_id, brick_id)
    if payload is None:
        raise AssertionError("значение кирпичика не сохранено")
    return payload


def _details(h, draft_id: int):
    with h.session_factory() as session:
        return get_announcement_draft(
            session,
            actor_telegram_user_id=DEFAULT_USER_ID,
            draft_id=draft_id,
        )


async def _open_text_question(h, specs=None):
    """Открывает вопрос конструктора по личному шаблону."""
    # `/start` регистрирует пользователя (track_user) — сиды ссылаются на него.
    await h.text("/start")
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, bricks = _seed_personal_template(
        h,
        name="H1 стабильность",
        specs=specs or [("main", "Текст", "long_text", {})],
    )
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    return bricks


async def _open_photo_step(h):
    """Черновик, остановленный на шаге «Фотографии анонса»."""
    await h.text("/start")
    place_id, _alias_id = _seed_place(h)
    template_id, _revision, _bricks = _seed_personal_template(h)
    await _start_to_template(h, place_id)
    await _choose_personal(h, template_id)
    await h.text("Текст")
    return _latest_draft(h)


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ResumeAfterRestartTests(unittest.TestCase):
    """Авто-resume активного черновика в личном чате."""

    @scenario_test
    async def test_text_after_restart_is_saved_into_step(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        self.assertIn("Вопрос 1 из 1", h.screen_text())

        # Рестарт службы: состояние ConversationHandler и user_data потеряны,
        # черновик и UI-снимок остаются в SQLite.
        await h.restart()
        await h.text("Текст после рестарта")

        self.assertNotIn(
            OUTSIDE_TEXT,
            h.screen_text(),
            "ввод провалился в подсказку главного меню вместо продолжения",
        )
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(payload["text"], "Текст после рестарта")
        # Диалог продолжен: следующий текст уже обслуживается конструктором.
        self.assertIsNotNone(h.dialog_state(name=CONVERSATION_NAME))
        self.assertEqual(h.dialog_state(name=CONVERSATION_NAME), AW_PREVIEW)

    @scenario_test
    async def test_two_steps_after_restart(self, h) -> None:
        """После resume диалог живой: второй текст идёт в следующий шаг."""
        bricks = await _open_text_question(
            h,
            specs=[
                ("first", "Первый", "long_text", {}),
                ("second", "Второй", "long_text", {}),
            ],
        )
        draft = _latest_draft(h)
        await h.restart()
        await h.text("Ответ один")
        await h.text("Ответ два")
        self.assertEqual(
            _stored_payload(h, draft.id, bricks["first"])["text"], "Ответ один"
        )
        self.assertEqual(
            _stored_payload(h, draft.id, bricks["second"])["text"], "Ответ два"
        )
        self.assertNotIn(OUTSIDE_TEXT, h.screen_text())

    @scenario_test
    async def test_formatted_text_after_restart_keeps_entities(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.text(
            "Жирный ввод",
            entities=[ent(MessageEntity.BOLD, 0, 6)],
        )
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(payload["text"], "Жирный ввод")
        self.assertEqual(payload["html"], "<b>Жирный</b> ввод")

    @scenario_test
    async def test_group_chat_keeps_outside_hint(self, h) -> None:
        await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        # В группе конструктор не продолжается автоматически.
        await h.text("Текст в группе", chat_type="group")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_details(h, draft.id).value_for(1))

    @scenario_test
    async def test_group_photo_after_restart_keeps_hint(self, h) -> None:
        """Группа: авто-resume не включается и для фото."""
        await _open_photo_step(h)
        await h.restart()
        h._update_seq += 1
        await h.feed(
            make_photo_update(
                "group-photo",
                chat_id=555,
                user_id=h.user_id,
                update_id=h._update_seq,
                message_id=701,
                chat_type="group",
            )
        )
        self.assertIn(OUTSIDE_PHOTO, h.screen_text())
        self.assertEqual(list(_latest_draft(h).photos or []), [])

    @scenario_test
    async def test_without_active_draft_keeps_outside_hint(self, h) -> None:
        await h.text("/start")
        await h.text("Обычный текст вне конструктора")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, 1, 1))

    @scenario_test
    async def test_after_cancel_draft_is_not_resumed(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.text("/cancel")
        await h.text("Текст после отмены")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))

    @scenario_test
    async def test_cancel_before_restart_keeps_hint(self, h) -> None:
        """Контроль: без рестарта `/cancel` уже снимал приём ввода."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.text("/cancel")
        await h.text("Текст после отмены")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))

    @scenario_test
    async def test_photo_step_after_restart_keeps_photos(self, h) -> None:
        """Черновик «в состоянии ввода фото» не теряет присланный снимок."""
        draft = await _open_photo_step(h)
        self.assertIn("Фотографии анонса", h.screen_text())

        await h.restart()
        await h.photo("h1-after-restart")
        self.assertIn("h1-after-restart", _details(h, draft.id).photos)
        self.assertNotIn(OUTSIDE_PHOTO, h.screen_text())

    @scenario_test
    async def test_album_after_restart_keeps_photos(self, h) -> None:
        """Альбом после рестарта: все снимки партии в черновике."""
        await _open_photo_step(h)
        await h.restart()
        await h.photo("album-1", media_group_id="g-1")
        await h.photo("album-2", media_group_id="g-1")
        photos = _latest_draft(h).photos
        self.assertIn("album-1", photos)
        self.assertIn("album-2", photos)
        self.assertNotIn(OUTSIDE_PHOTO, h.screen_text())

    @scenario_test
    async def test_deleted_draft_falls_back_to_outside_hint(self, h) -> None:
        """Устаревший снимок (черновик удалён) не оживляет конструктор."""
        await _open_text_question(h)
        draft = _latest_draft(h)
        with h.session_factory() as session:
            session.execute(
                delete(AnnouncementValue).where(AnnouncementValue.draft_id == draft.id)
            )
            session.execute(
                delete(AnnouncementDraft).where(AnnouncementDraft.id == draft.id)
            )
            session.commit()
        await h.restart()
        await h.text("Текст в удалённый черновик")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))
        # Снимок снят: следующий текст тоже не пытается продолжить шаг.
        await h.text("Ещё один текст")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))

    @scenario_test
    async def test_corrupted_snapshot_does_not_crash(self, h) -> None:
        """Битые данные: мусор в resume-снимке → подсказка вместо падения."""
        await _open_text_question(h)
        with h.session_factory() as session:
            session.execute(
                update(UserState).values(
                    payload=json.dumps(
                        {"announcement_resume": {"mode": "???", "draft_id": "x"}}
                    )
                )
            )
            session.commit()
        await h.restart()
        await h.text("Текст при битом снимке")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(h.dialog_state(name=CONVERSATION_NAME))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class ExplicitExitAfterRestartTests(unittest.TestCase):
    """
    Явный выход после рестарта: глобальные экраны меню закрывают ввод.

    После перезапуска службы у PTB нет состояния ConversationHandler, поэтому
    `/cancel`, `/start`, `/help` и «🏠 В меню» обслуживают обработчики
    `handlers/main_menu.py`. Без снятия resume-снимка следующий текст
    записывался бы в значение шага и затирал сохранённое.
    """

    @scenario_test
    async def test_cancel_after_restart_keeps_hint(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.text("/cancel")
        await h.text("Текст после отмены и рестарта")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(
            _stored_value(h, draft.id, bricks["main"]),
            "текст после /cancel попал в значение черновика",
        )

    @scenario_test
    async def test_cancel_after_restart_does_not_overwrite_saved_value(self, h) -> None:
        """Потеря данных: отмена не должна затирать уже сохранённое значение."""
        await h.text("/start")
        place_id, _alias = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Ценное значение")
        await _no_photos(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        await h.callback(_callback(h, "aw_qedit"))
        draft = _latest_draft(h)
        self.assertEqual(
            _stored_payload(h, draft.id, bricks["main"])["text"], "Ценное значение"
        )

        await h.restart()
        await h.text("/cancel")
        await h.text("Случайный текст после отмены")

        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertEqual(
            _stored_payload(h, draft.id, bricks["main"])["text"],
            "Ценное значение",
            "текст после /cancel затёр сохранённое значение черновика",
        )

    @scenario_test
    async def test_cancel_after_restart_does_not_accept_photo(self, h) -> None:
        """Рестарт на шаге фото → `/cancel` → фото не принимается."""
        await _open_photo_step(h)
        await h.restart()
        await h.text("/cancel")
        await h.photo("photo-after-cancel")
        self.assertIn(OUTSIDE_PHOTO, h.screen_text())
        self.assertEqual(list(_latest_draft(h).photos or []), [])

    @scenario_test
    async def test_start_after_restart_keeps_hint(self, h) -> None:
        """Рестарт → /start (меню) → текст не уходит в черновик."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.text("/start")
        await h.text("Текст после /start")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))

    @scenario_test
    async def test_help_after_restart_keeps_hint(self, h) -> None:
        """Рестарт → /help (справка) → текст не уходит в черновик."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.text("/help")
        await h.text("Текст после справки")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))

    @scenario_test
    async def test_main_menu_button_after_restart_keeps_hint(self, h) -> None:
        """Рестарт → «🏠 В меню» → текст не уходит в черновик."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.callback("main_menu")
        await h.text("Текст после выхода в меню")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))

    @scenario_test
    async def test_drafts_after_restart_keeps_hint(self, h) -> None:
        """Контроль: «📂 Мои анонсы» снимает снимок (эта ветка уже верна)."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.restart()
        await h.callback("drafts")
        await h.text("Текст после списка анонсов")
        self.assertIn(OUTSIDE_TEXT, h.screen_text())
        self.assertIsNone(_stored_value(h, draft.id, bricks["main"]))


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class DateTimeEntityTests(unittest.TestCase):
    """`date_time` не ломает HTML и деградирует в читаемый текст."""

    MOMENT = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)

    def test_date_time_degrades_to_readable_date_and_time(self) -> None:
        html_text = entities_to_html(
            "Встреча 05.11.2026",
            [
                ent(
                    MessageEntity.DATE_TIME,
                    offset=8,
                    length=10,
                    unix_time=self.MOMENT,
                    date_time_format="d",
                )
            ],
        )
        self.assertEqual(html_text, "Встреча 05.11.2026")
        self.assertNotIn("tg-time", html_text)

    def test_date_time_with_time_format_and_weekday(self) -> None:
        moment = datetime(2026, 11, 5, 19, 30, tzinfo=APP_TZ)
        text = "05.11 19:30"
        html_text = entities_to_html(
            text,
            [
                ent(
                    MessageEntity.DATE_TIME,
                    0,
                    len(text),
                    unix_time=moment,
                    date_time_format="wt",
                )
            ],
        )
        # Читаемый текст: день недели, дата и время в формате приложения.
        self.assertIn("четверг", html_text)
        self.assertIn("19:30", html_text)
        self.assertNotIn("<tg-time", html_text)

    def test_date_time_without_unix_time_keeps_captured_text(self) -> None:
        raw = capture_text("завтра в 19:00", [ent(MessageEntity.DATE_TIME, 0, 6)])
        self.assertEqual(raw.html, "завтра в 19:00")
        self.assertEqual(str(raw), "завтра в 19:00")
        assert_valid_html(self, raw.html)

    def test_date_time_in_readable_time_zone(self) -> None:
        """19:00 UTC → 22:00 в Europe/Moscow: предсказуемое локальное время."""
        raw = capture_text(
            "Встреча 05.11.2026 19:00",
            [
                ent(
                    MessageEntity.DATE_TIME,
                    8,
                    16,
                    unix_time=self.MOMENT,
                    date_time_format="dt",
                )
            ],
        )
        self.assertNotIn("<tg-time", raw.html, "отправка <tg-time> ломает разметку")
        assert_valid_html(self, raw.html)
        self.assertIn("22:00", raw.html)
        self.assertIn("05.11.2026", raw.html)
        # Plain-текст значения остаётся исходным (источник истины).
        self.assertEqual(str(raw), "Встреча 05.11.2026 19:00")

    def test_unknown_format_falls_back_predictably(self) -> None:
        html_text = entities_to_html(
            "05.11.2026",
            [
                ent(
                    MessageEntity.DATE_TIME,
                    0,
                    10,
                    unix_time=self.MOMENT,
                    date_time_format="%%%",
                )
            ],
        )
        assert_valid_html(self, html_text)
        self.assertRegex(html_text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}")

    def test_date_time_nested_in_bold_keeps_formatting(self) -> None:
        html_text = entities_to_html(
            "05.11.2026",
            [
                ent(MessageEntity.BOLD, 0, 10),
                ent(
                    MessageEntity.DATE_TIME,
                    0,
                    10,
                    unix_time=self.MOMENT,
                    date_time_format="d",
                ),
            ],
        )
        self.assertTrue(html_text.startswith("<b>"))
        self.assertTrue(html_text.endswith("</b>"))

    def test_date_time_nested_in_blockquote_and_bold(self) -> None:
        text = "05.11.2026"
        html_text = entities_to_html(
            text,
            [
                ent(MessageEntity.EXPANDABLE_BLOCKQUOTE, 0, len(text)),
                ent(MessageEntity.BOLD, 0, len(text)),
                ent(
                    MessageEntity.DATE_TIME,
                    0,
                    len(text),
                    unix_time=self.MOMENT,
                    date_time_format="D",
                ),
            ],
        )
        assert_valid_html(self, html_text)
        self.assertTrue(html_text.startswith("<blockquote expandable><b>"), html_text)
        self.assertIn("5 ноября 2026", html_text)
        self.assertNotIn("tg-time", html_text)

    def test_user_text_is_escaped_next_to_date_time(self) -> None:
        text = "<script> 05.11"
        html_text = entities_to_html(
            text,
            [
                ent(
                    MessageEntity.DATE_TIME,
                    9,
                    5,
                    unix_time=self.MOMENT,
                    date_time_format="d",
                )
            ],
        )
        assert_valid_html(self, html_text)
        self.assertNotIn("<script>", html_text)
        self.assertIn("&lt;script&gt;", html_text)

    def test_capture_text_with_date_time_does_not_break_html(self) -> None:
        raw = capture_text(
            "Старт 05.11.2026",
            [
                ent(
                    MessageEntity.DATE_TIME,
                    6,
                    10,
                    unix_time=self.MOMENT,
                    date_time_format="d",
                )
            ],
        )
        self.assertEqual(str(raw), "Старт 05.11.2026")
        self.assertNotIn("tg-time", raw.html)

    def test_allow_list_keeps_safe_tg_time_and_drops_unsafe_attributes(self) -> None:
        safe = sanitize_html(
            '<tg-time unix="1762363200" format="dt">05.11.2026 19:00</tg-time>'
        )
        self.assertEqual(
            str(safe),
            '<tg-time unix="1762363200" format="dt">05.11.2026 19:00</tg-time>',
        )
        unsafe = sanitize_html(
            '<tg-time unix="--1" format="zzz" onclick="x">05.11</tg-time>'
        )
        self.assertEqual(str(unsafe), "<tg-time>05.11</tg-time>")
        # Идемпотентность: повторная проверка ничего не меняет.
        self.assertEqual(str(sanitize_html(str(safe))), str(safe))

    def test_split_does_not_break_tg_time(self) -> None:
        html_text = '<tg-time unix="1762363200" format="d">05.11.2026</tg-time>'
        # Лимит больше самого тега: токен-тег неделим (меньше него разрез
        # невозможен в принципе — как и для `<a href=...>`).
        chunks = split_html(f"{html_text} {html_text}", 70)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertEqual(chunk.count("<tg-time"), chunk.count("</tg-time>"))
            self.assertTrue(chunk.rstrip().endswith("</tg-time>"), chunk)
            self.assertIn('format="d"', chunk, "атрибуты переоткрыты")

    @scenario_test
    async def test_date_time_value_saved_into_draft(self, h) -> None:
        """Сквозной путь: entity → значение → БД → экран без падения."""
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        await h.text(
            "Старт 05.11.2026",
            entities=[
                ent(
                    MessageEntity.DATE_TIME,
                    6,
                    10,
                    unix_time=self.MOMENT,
                    date_time_format="d",
                )
            ],
        )
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertNotIn("tg-time", payload.get("html") or "")
        assert_valid_html(self, payload.get("html") or "")
        self.assertEqual(payload["text"], "Старт 05.11.2026")
        # Экран перерисован, а не упал в ошибку.
        self.assertNotIn("Ошибка", h.screen_text())


@unittest.skipIf(
    INTEGRATION_IMPORT_ERROR is not None,
    f"интеграционное окружение недоступно: {INTEGRATION_IMPORT_ERROR}",
)
class BlockquoteRegressionTests(unittest.TestCase):
    """Цитаты в значении: захват, БД, рендер, правка, доставка."""

    @scenario_test
    async def test_multiple_blockquotes_round_trip_through_db(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        first, second = "Первая цитата", "Вторая цитата"
        text = f"{first}\n{second}"
        await h.text(
            text,
            entities=[
                ent(MessageEntity.BLOCKQUOTE, 0, len(first)),
                ent(MessageEntity.BLOCKQUOTE, len(first) + 1, len(second)),
            ],
        )
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(
            payload["html"],
            "<blockquote>Первая цитата</blockquote>\n"
            "<blockquote>Вторая цитата</blockquote>",
        )
        assert_valid_html(self, payload["html"])
        # Раунд-трип: чтение из БД и allow-list не меняют сохранённый HTML.
        stored = _details(h, draft.id).value_for(bricks["main"])
        assert stored is not None and stored.payload is not None
        self.assertEqual(stored.payload["html"], payload["html"])
        self.assertEqual(str(sanitize_html(payload["html"])), payload["html"])

    @scenario_test
    async def test_expandable_quote_next_to_bold_spoiler_and_code(self, h) -> None:
        bricks = await _open_text_question(h)
        draft = _latest_draft(h)
        text = "Жирно скрыто код склад"
        # Смещения считаются по тексту, а не «на глаз»: у каждой сущности
        # своя длина, и вложенная цитата начинается после пробела.
        bold, spoiler, code, quote = "Жирно", "скрыто", "код", "склад"
        entities = [
            ent(MessageEntity.EXPANDABLE_BLOCKQUOTE, 0, len(text)),
            ent(MessageEntity.BOLD, 0, len(bold)),
            ent(MessageEntity.SPOILER, len(bold) + 1, len(spoiler)),
            ent(MessageEntity.CODE, len(bold) + len(spoiler) + 2, len(code)),
            ent(MessageEntity.BLOCKQUOTE, len(text) - len(quote), len(quote)),
        ]
        await h.text(text, entities=entities)
        payload = _stored_payload(h, draft.id, bricks["main"])
        self.assertEqual(
            payload["html"],
            "<blockquote expandable><b>Жирно</b> "
            "<tg-spoiler>скрыто</tg-spoiler> <code>код</code> "
            "<blockquote>склад</blockquote></blockquote>",
        )
        assert_valid_html(self, payload["html"])
        stored = _details(h, draft.id).value_for(bricks["main"])
        assert stored is not None and stored.payload is not None
        self.assertEqual(stored.payload["html"], payload["html"])
        self.assertEqual(str(sanitize_html(payload["html"])), payload["html"])

    @scenario_test
    async def test_quote_is_kept_while_editing_value(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, bricks = _seed_personal_template(h)
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        await h.text("Старая цитата", entities=[ent(MessageEntity.BLOCKQUOTE, 0, 13)])
        await _no_photos(h)
        await _open_edit(h)
        await h.callback(_callback(h, "aw_qmanage"))
        await h.callback(_callback(h, "aw_qgoto"))
        self.assertIn(
            "Текущее значение: <blockquote>Старая цитата</blockquote>",
            h.screen_text(),
        )
        await h.callback(_callback(h, "aw_qedit"))
        await h.text("Новая цитата", entities=[ent(MessageEntity.BLOCKQUOTE, 0, 12)])
        payload = _stored_payload(h, _latest_draft(h).id, bricks["main"])
        self.assertEqual(payload["html"], "<blockquote>Новая цитата</blockquote>")

    @scenario_test
    async def test_quote_in_announcement_without_photos(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Цитата без фото",
            specs=[("main", "Основное", "short_text", {})],
            body="Анонс: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        first, second = "Первая", "Вторая"
        await h.text(
            f"{first}\n{second}",
            entities=[
                ent(MessageEntity.BLOCKQUOTE, 0, len(first)),
                ent(MessageEntity.EXPANDABLE_BLOCKQUOTE, len(first) + 1, len(second)),
            ],
        )
        await _no_photos(h)
        delivered = h.posts()[-1] or ""
        assert_valid_html(self, delivered)
        self.assertIn("<blockquote>Первая</blockquote>", delivered)
        self.assertIn("<blockquote expandable>Вторая</blockquote>", delivered)

    @scenario_test
    async def test_expandable_quote_in_announcement_with_photo(self, h) -> None:
        await h.text("/start")
        place_id, _alias_id = _seed_place(h)
        template_id, _revision, _bricks = _seed_personal_template(
            h,
            name="Цитата с фото",
            specs=[("main", "Основное", "short_text", {})],
            body="Анонс: [main]",
        )
        await _start_to_template(h, place_id)
        await _choose_personal(h, template_id)
        text = "Жирно склад"
        await h.text(
            text,
            entities=[
                ent(MessageEntity.EXPANDABLE_BLOCKQUOTE, 0, len(text)),
                ent(MessageEntity.BOLD, 0, 5),
                ent(MessageEntity.BLOCKQUOTE, 6, 5),
            ],
        )
        await h.photo("h1-quote-photo")
        records = [r for r in h.post_records() if r["type"] == "photo"]
        self.assertTrue(records, "анонс с фото не доставлен")
        caption = records[-1]["caption"] or ""
        self.assertEqual(records[-1]["parse_mode"], "HTML")
        assert_valid_html(self, caption)
        self.assertIn("<b>Жирно</b>", caption)
        self.assertIn("<blockquote>склад</blockquote>", caption)
        self.assertIn("expandable", caption)
