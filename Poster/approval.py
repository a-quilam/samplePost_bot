# approval.py
"""
Согласование постов: назначение ответственного и статусы решения.

Модуль содержит только работу с БД и разбор callback_data — без импорта
telegram и config, поэтому свободно тестируется (tests/test_approval.py).

Формат callback_data (лимит Telegram — 64 байта):
    responsible_<draft_id>_<telegram_id>  — выбор ответственного (review-чат)
    viewpost_<draft_id>                   — просмотр назначенного поста
    approvepost_<draft_id>                — согласовать
    declinepost_<draft_id>                — отклонить
"""

from sqlalchemy.orm import Session

from models import Draft, PostApproval

# Статусы согласования
STATUS_ASSIGNED = 'assigned'
STATUS_APPROVED = 'approved'
STATUS_DECLINED = 'declined'

# Префиксы callback_data
RESPONSIBLE_PREFIX = 'responsible_'
VIEW_PREFIX = 'viewpost_'
APPROVE_PREFIX = 'approvepost_'
DECLINE_PREFIX = 'declinepost_'


def parse_responsible_callback(data):
    """
    Разбирает responsible_<draft_id>_<telegram_id>.
    Возвращает (draft_id, telegram_id) либо None для неверного формата
    (в т.ч. старого: responsible_<telegram_id> без id поста).
    """
    if not data or not data.startswith(RESPONSIBLE_PREFIX):
        return None
    parts = data[len(RESPONSIBLE_PREFIX):].split('_')
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def parse_post_action(data):
    """
    Разбирает viewpost_/approvepost_/declinepost_<draft_id>.
    Возвращает (action, draft_id) либо None.
    action: 'view' | 'approve' | 'decline'.
    """
    for prefix, action in ((VIEW_PREFIX, 'view'),
                           (APPROVE_PREFIX, 'approve'),
                           (DECLINE_PREFIX, 'decline')):
        if data and data.startswith(prefix):
            tail = data[len(prefix):]
            if tail.isdigit():
                return action, int(tail)
    return None


def get_draft(session: Session, draft_id: int):
    """Пост по id — источник данных для уведомления ответственного."""
    return session.query(Draft).filter(Draft.id == draft_id).first()


def get_approval(session: Session, draft_id: int):
    """Запись согласования поста (или None, если пост не назначался)."""
    return session.query(PostApproval).filter(PostApproval.draft_id == draft_id).first()


def assign_responsible(session: Session, draft_id: int, telegram_id: int):
    """
    Назначает ответственного за конкретный пост.

    Идемпотентно: на пост — ровно одна запись (unique draft_id).
    Первое назначение побеждает, повторное нажатие не создаёт
    неконсистентного состояния.

    Возвращает (approval, outcome):
        'created'  — первое назначение записано;
        'same'     — этот ответственный уже назначен;
        'other'    — уже назначен другой ответственный;
        'no_draft' — пост не найден (approval=None).
    """
    if get_draft(session, draft_id) is None:
        return None, 'no_draft'

    existing = get_approval(session, draft_id)
    if existing is not None:
        if existing.responsible_telegram_id == telegram_id:
            return existing, 'same'
        return existing, 'other'

    approval = PostApproval(
        draft_id=draft_id,
        responsible_telegram_id=telegram_id,
        status=STATUS_ASSIGNED,
    )
    session.add(approval)
    session.commit()
    return approval, 'created'


def decide(session: Session, draft_id: int, by_telegram_id: int, new_status: str):
    """
    Фиксирует решение ответственного ('approved' или 'declined').

    Возвращает (approval, outcome):
        'ok'          — решение записано;
        'no_approval' — пост не назначался ответственному;
        'forbidden'   — нажал не назначенный ответственный;
        'already'     — решение уже принято (повторное/встречное нажатие);
        'bad_status'  — недопустимый статус (approval=None).
    """
    if new_status not in (STATUS_APPROVED, STATUS_DECLINED):
        return None, 'bad_status'

    approval = get_approval(session, draft_id)
    if approval is None:
        return None, 'no_approval'
    if approval.responsible_telegram_id != by_telegram_id:
        return approval, 'forbidden'
    if approval.status != STATUS_ASSIGNED:
        return approval, 'already'

    approval.status = new_status
    session.commit()
    return approval, 'ok'


def draft_to_post_data(draft: Draft) -> dict:
    """
    Поля поста из строки БД — в формате, ожидаемом build_post_summary().
    Данные берутся из САМОГО поста, а не из context.user_data нажавшего.
    """
    return {
        'title': draft.title,
        'date': draft.date,
        'time_start': draft.time_start,
        'time_end': draft.time_end,
        'place_name': draft.place_name,
        'text': draft.text,
        'contact': draft.contact,
        'place_url': draft.place_url,
        'image': draft.image,
    }
