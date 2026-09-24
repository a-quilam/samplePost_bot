# tests/test_approval.py
"""
Юнит-тесты логики согласования (approval.py) и экранирования.

Запуск из каталога Poster:
    python -m unittest discover -s tests -v

Тесты работают с БД в памяти и НЕ трогают post_bot.db.
"""

import unittest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from base import Base
import models  # noqa: F401  — регистрация таблиц в Base.metadata
from models import Draft, PostApproval
from approval import (
    STATUS_APPROVED,
    STATUS_ASSIGNED,
    STATUS_DECLINED,
    assign_responsible,
    decide,
    draft_to_post_data,
    get_approval,
    parse_post_action,
    parse_responsible_callback,
)
from utils.formatter import escape_markdown, format_text

# Импорт обработчиков требует config (.env с токеном) и telegram —
# при недоступности соответствующие тесты пропускаются, а не падают.
try:
    from telegram.ext import CallbackQueryHandler
    from handlers.post_creation import build_post_summary, post_creation_handlers
    from handlers.approval import approval_handlers
    from handlers.callbacks import callbacks_handlers
    HANDLERS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HANDLERS_IMPORT_ERROR = str(e)


def make_session():
    """Сессия над БД в памяти (отдельный engine, к post_bot.db не подключается)."""
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_draft(session, user_id=111, title='Тестовый пост'):
    draft = Draft(user_id=user_id, title=title)
    session.add(draft)
    session.commit()
    return draft


class ParseCallbackTests(unittest.TestCase):
    """Разбор callback_data: в том числе передача id поста."""

    def test_responsible_ok(self):
        self.assertEqual(parse_responsible_callback('responsible_5_777'), (5, 777))

    def test_responsible_old_format_rejected(self):
        # старый формат без id поста не должен назначать «вслепую»
        self.assertIsNone(parse_responsible_callback('responsible_777'))

    def test_responsible_invalid(self):
        self.assertIsNone(parse_responsible_callback('responsible_a_b'))
        self.assertIsNone(parse_responsible_callback('responsible_1_2_3'))
        self.assertIsNone(parse_responsible_callback('other_1_2'))
        self.assertIsNone(parse_responsible_callback(None))
        self.assertIsNone(parse_responsible_callback(''))

    def test_post_action_ok(self):
        self.assertEqual(parse_post_action('viewpost_9'), ('view', 9))
        self.assertEqual(parse_post_action('approvepost_9'), ('approve', 9))
        self.assertEqual(parse_post_action('declinepost_9'), ('decline', 9))
        self.assertEqual(parse_post_action('publishpost_9'), ('publish', 9))

    def test_post_action_invalid(self):
        self.assertIsNone(parse_post_action('viewpost_'))
        self.assertIsNone(parse_post_action('viewpost_x'))
        self.assertIsNone(parse_post_action('responsible_1_2'))
        self.assertIsNone(parse_post_action('editdraft_1'))


class AssignTests(unittest.TestCase):
    """Назначение ответственного: по конкретному посту, идемпотентно."""
    def setUp(self):
        self.session = make_session()
        self.draft = make_draft(self.session)

    def tearDown(self):
        engine = self.session.get_bind()
        self.session.close()
        engine.dispose()

    def test_assign_creates_single_row(self):
        approval, outcome = assign_responsible(self.session, self.draft.id, 100)
        self.assertEqual(outcome, 'created')
        self.assertEqual(approval.status, STATUS_ASSIGNED)
        self.assertEqual(approval.draft_id, self.draft.id)
        self.assertEqual(self.session.query(PostApproval).count(), 1)

    def test_repeat_click_same_person_is_idempotent(self):
        assign_responsible(self.session, self.draft.id, 100)
        approval, outcome = assign_responsible(self.session, self.draft.id, 100)
        self.assertEqual(outcome, 'same')
        self.assertEqual(approval.responsible_telegram_id, 100)
        self.assertEqual(self.session.query(PostApproval).count(), 1)

    def test_repeat_click_other_person_rejected(self):
        assign_responsible(self.session, self.draft.id, 100)
        approval, outcome = assign_responsible(self.session, self.draft.id, 200)
        self.assertEqual(outcome, 'other')
        self.assertEqual(approval.responsible_telegram_id, 100)
        self.assertEqual(self.session.query(PostApproval).count(), 1)

    def test_assign_two_drafts_independent(self):
        other = make_draft(self.session, title='Второй пост')
        a1, o1 = assign_responsible(self.session, self.draft.id, 100)
        a2, o2 = assign_responsible(self.session, other.id, 200)
        self.assertEqual((o1, o2), ('created', 'created'))
        self.assertEqual(a1.responsible_telegram_id, 100)
        self.assertEqual(a2.responsible_telegram_id, 200)
        self.assertEqual(self.session.query(PostApproval).count(), 2)

    def test_assign_missing_draft(self):
        approval, outcome = assign_responsible(self.session, 9999, 100)
        self.assertEqual(outcome, 'no_draft')
        self.assertIsNone(approval)
        self.assertEqual(self.session.query(PostApproval).count(), 0)


class DecideTests(unittest.TestCase):
    """Решение ответственного: только назначенный, финальное, без дублей."""

    def setUp(self):
        self.session = make_session()
        self.draft = make_draft(self.session)
        assign_responsible(self.session, self.draft.id, 100)

    def tearDown(self):
        engine = self.session.get_bind()
        self.session.close()
        engine.dispose()

    def test_approve_ok(self):
        approval, outcome = decide(self.session, self.draft.id, 100, STATUS_APPROVED)
        self.assertEqual(outcome, 'ok')
        self.assertEqual(approval.status, STATUS_APPROVED)

    def test_decline_ok(self):
        approval, outcome = decide(self.session, self.draft.id, 100, STATUS_DECLINED)
        self.assertEqual(outcome, 'ok')
        self.assertEqual(approval.status, STATUS_DECLINED)

    def test_forbidden_for_other_user(self):
        _, outcome = decide(self.session, self.draft.id, 200, STATUS_APPROVED)
        self.assertEqual(outcome, 'forbidden')
        # статус не изменился — нажатие посторонним неконсистентно
        self.assertEqual(get_approval(self.session, self.draft.id).status, STATUS_ASSIGNED)

    def test_double_decision_consistent(self):
        decide(self.session, self.draft.id, 100, STATUS_APPROVED)
        # повторное нажатие той же кнопки
        _, outcome = decide(self.session, self.draft.id, 100, STATUS_APPROVED)
        self.assertEqual(outcome, 'already')
        # встречное нажатие не меняет принятое решение
        _, outcome = decide(self.session, self.draft.id, 100, STATUS_DECLINED)
        self.assertEqual(outcome, 'already')
        self.assertEqual(get_approval(self.session, self.draft.id).status, STATUS_APPROVED)
        self.assertEqual(self.session.query(PostApproval).count(), 1)

    def test_decision_without_assignment(self):
        other = make_draft(self.session, title='Не назначался')
        _, outcome = decide(self.session, other.id, 100, STATUS_APPROVED)
        self.assertEqual(outcome, 'no_approval')

    def test_bad_status_rejected(self):
        _, outcome = decide(self.session, self.draft.id, 100, 'published')
        self.assertEqual(outcome, 'bad_status')
        self.assertEqual(get_approval(self.session, self.draft.id).status, STATUS_ASSIGNED)


class DraftToPostDataTests(unittest.TestCase):
    def test_keys_match_summary_format(self):
        session = make_session()
        try:
            draft = make_draft(session, title='Заголовок')
            data = draft_to_post_data(draft)
            expected = {
                'title', 'date', 'time_start', 'time_end', 'place_name',
                'text', 'contact', 'place_url', 'image',
            }
            self.assertEqual(set(data.keys()), expected)
            self.assertEqual(data['title'], 'Заголовок')
            self.assertIsNone(data['date'])  # незаполненные поля — None, сводка покажет «Не указано»
        finally:
            engine = session.get_bind()
            session.close()
            engine.dispose()


class DoubleEscapeGuardTests(unittest.TestCase):
    """Защита от возврата двойного экранирования (анализ БД: затронутых записей — 0)."""

    def test_format_text_does_not_add_backslashes(self):
        formatted = format_text('Встреча "в 12.00" - вход свободный')
        self.assertNotIn('\\', formatted)

    def test_escape_applied_once_at_render(self):
        self.assertEqual(escape_markdown('a_b'), 'a\\_b')
        self.assertEqual(escape_markdown('5.00'), '5\\.00')


@unittest.skipIf(
    HANDLERS_IMPORT_ERROR is not None,
    f"импорт обработчиков недоступен: {HANDLERS_IMPORT_ERROR}",
)
class HandlerConsistencyTests(unittest.TestCase):
    """Состав обработчиков и непересечение с ConversationHandler."""

    def test_handler_counts(self):
        self.assertEqual(len(post_creation_handlers()), 1)
        self.assertEqual(len(approval_handlers()), 5)  # + публикация (publishpost)
        self.assertEqual(len(callbacks_handlers()), 1)

    def test_approval_callbacks_not_clash_with_conversation(self):
        conv = post_creation_handlers()[0]
        conv_patterns = []
        groups = list(conv.states.values()) + [list(conv.entry_points)]
        for handlers in groups:
            for h in handlers:
                if isinstance(h, CallbackQueryHandler) and h.pattern is not None:
                    conv_patterns.append(h.pattern)

        self.assertTrue(conv_patterns, "в ConversationHandler нет CallbackQueryHandler?")

        approval_samples = [
            'responsible_1_2', 'viewpost_1', 'approvepost_1', 'declinepost_1',
            'publishpost_1',
        ]
        for sample in approval_samples:
            for pat in conv_patterns:
                self.assertIsNone(
                    pat.search(sample),
                    f"callback '{sample}' перехватывается ConversationHandler: {pat.pattern}",
                )

    def test_conversation_callbacks_not_clash_with_approval(self):
        """
        approval_handlers регистрируются ДО ConversationHandler — их паттерны
        не должны перехватывать callback'и диалога (иначе редактирование
        черновика и сбор фото не работают).
        """
        approval_patterns = [h.pattern for h in approval_handlers() if h.pattern is not None]
        self.assertTrue(approval_patterns)

        conversation_samples = [
            'editdraft_1', 'skip', 'media_done',
            'save_draft', 'send_for_approval', 'edit_post',
            'edit_title', 'cancel_edit',
        ]
        for sample in conversation_samples:
            for pat in approval_patterns:
                self.assertIsNone(
                    pat.search(sample),
                    f"callback '{sample}' перехватывается approval_handlers: {pat.pattern}",
                )

    def test_summary_escapes_user_values(self):
        summary = build_post_summary({'title': 'Скидка 50%_*'}, heading="h")
        self.assertIn('\\*', summary)   # '*' пользователя экранирован
        self.assertIn('\\_', summary)   # '_' пользователя экранирован

    def test_summary_none_safe(self):
        summary = build_post_summary({}, heading="h")  # все поля отсутствуют
        self.assertIn('Не указано', summary)


if __name__ == '__main__':
    unittest.main()
