# place_invite_store.py
"""
Ссылки «создать анонс площадки» (Poster 2.2 U4b, миграция 23).

Администратор площадки (или суперадминистратор) открывает карточку места в
«Управление → Площадки» и получает ссылку

    https://t.me/<бот>?start=<token>

Любой человек, открывший её, сразу попадает в конструктор анонса с этой
площадкой и её стандартным шаблоном — без выбора площадки и без выбора
шаблона. Ссылка НЕ даёт прав: `place_admins` по ней не создаётся, и
редактировать площадку/шаблон открывший не может.

Правила слоя:

- токен случайный (URL-safe, 22 символа) и НЕ содержит `place_id`:
  payload ссылки не раскрывает внутренние ключи и не перебирается;
- на площадку не больше одной действующей ссылки (частичный уникальный
  индекс); выдача идемпотентна — повторный вход в раздел не создаёт дублей;
- перевыпуск отзывает прежнюю ссылку (`revoked_at`) и создаёт новую;
- открытие ссылки проверяет и токен, и существование площадки: отозванный
  или подделанный токен, а также удалённая площадка дают один и тот же
  безопасный отказ.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from models import Place, PlaceInviteLink, User, utcnow
from permissions import can_manage_place
from place_store import get_place_details

logger = logging.getLogger(__name__)

# 16 случайных байт → 22 символа base64url: ~128 бит энтропии и уверенно
# влезает в лимит Telegram на payload `/start` (64 символа).
TOKEN_BYTES = 16
TOKEN_LENGTH = 22
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,22}$")
MAX_TOKEN_ATTEMPTS = 5


class PlaceInviteError(ValueError):
    """Некорректный запрос ссылки (неизвестная площадка, неверный id)."""


class PlaceInvitePermissionError(PermissionError):
    """Выдавать/перевыпускать ссылку может только управляющий площадкой."""


@dataclass(frozen=True)
class InviteLinkView:
    """Действующая ссылка площадки для экрана и открытия по payload."""

    link_id: int
    token: str
    place_id: int
    place_name: str
    created_at: datetime


@dataclass(frozen=True)
class InviteOpenResult:
    """
    Результат открытия ссылки.

    `status`:
    - `ok` — ссылка действительна, площадка существует;
    - `invalid` — токена нет, он отозван, подделан или площадка удалена.
      Наружу это ОДИН ответ: ссылка не должна подсказывать, что именно не
      так, иначе перебор токенов становится осмысленным.
    """

    status: str
    invite: InviteLinkView | None = None


def generate_token() -> str:
    """Случайный URL-safe токен без связи с идентификаторами площадки."""
    return secrets.token_urlsafe(TOKEN_BYTES)[:TOKEN_LENGTH]


def is_valid_token_shape(token: object) -> bool:
    """Похож ли payload на наш токен (формат проверяется до запроса в БД)."""
    return isinstance(token, str) and bool(TOKEN_RE.match(token))


def _view(session: Session, row: PlaceInviteLink) -> InviteLinkView | None:
    """Снимок ссылки с актуальным названием площадки (None — места нет)."""
    details = get_place_details(session, row.place_id)
    if details is None:
        return None
    return InviteLinkView(
        link_id=row.id,
        token=row.token,
        place_id=row.place_id,
        place_name=details.canonical_name,
        created_at=row.created_at,
    )


def get_active_invite(session: Session, place_id: int) -> InviteLinkView | None:
    """Действующая ссылка площадки (None — ссылки ещё нет или она отозвана)."""
    row = session.scalar(
        select(PlaceInviteLink).where(
            PlaceInviteLink.place_id == place_id,
            PlaceInviteLink.revoked_at.is_(None),
        )
    )
    return _view(session, row) if row is not None else None


def ensure_invite(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
) -> InviteLinkView:
    """
    Действующая ссылка площадки; при отсутствии — создаётся.

    Повторный вызов НЕ создаёт вторую ссылку: раздел можно открывать сколько
    угодно раз. Права проверяются по БД при каждом вызове.
    """
    actor = _require_manager(session, actor_telegram_user_id, place_id)
    existing = get_active_invite(session, place_id)
    if existing is not None:
        return existing
    return _create_link(session, place_id=place_id, actor=actor)


def regenerate_invite(
    session: Session,
    *,
    actor_telegram_user_id: int,
    place_id: int,
) -> InviteLinkView:
    """
    Перевыпуск: прежняя действующая ссылка отзывается, выдаётся новая.

    Отозванная строка остаётся аудитом и больше не открывает конструктор.
    """
    actor = _require_manager(session, actor_telegram_user_id, place_id)
    try:
        session.execute(
            update(PlaceInviteLink)
            .where(
                PlaceInviteLink.place_id == place_id,
                PlaceInviteLink.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        link = _create_link(session, place_id=place_id, actor=actor)
    except Exception:
        session.rollback()
        raise
    return link


def open_invite(session: Session, token: object) -> InviteOpenResult:
    """
    Открытие ссылки по payload `/start <token>`.

    Отозванный, подделанный, отсутствующий токен и удалённая площадка дают
    один и тот же `invalid`: по ответу нельзя узнать, существует ли токен.
    """
    if not is_valid_token_shape(token):
        return InviteOpenResult(status="invalid")
    row = session.scalar(
        select(PlaceInviteLink).where(PlaceInviteLink.token == str(token))
    )
    if row is None or row.revoked_at is not None:
        return InviteOpenResult(status="invalid")
    if session.get(Place, row.place_id) is None:
        return InviteOpenResult(status="invalid")
    view = _view(session, row)
    if view is None:
        return InviteOpenResult(status="invalid")
    return InviteOpenResult(status="ok", invite=view)


def _require_manager(
    session: Session, actor_telegram_user_id: int, place_id: int
) -> User:
    """Права: управляющий площадкой (суперадмин или её админ) и место живое."""
    if not isinstance(place_id, int) or isinstance(place_id, bool) or place_id <= 0:
        raise PlaceInviteError("Некорректный идентификатор площадки.")
    if (
        not isinstance(actor_telegram_user_id, int)
        or isinstance(actor_telegram_user_id, bool)
        or actor_telegram_user_id <= 0
    ):
        raise PlaceInvitePermissionError("Ссылку выдаёт администратор площадки.")
    if session.get(Place, place_id) is None:
        raise PlaceInviteError("Площадка не найдена.")
    if not can_manage_place(session, actor_telegram_user_id, place_id):
        raise PlaceInvitePermissionError("Ссылку выдаёт администратор площадки.")
    actor = session.scalar(
        select(User).where(User.telegram_user_id == actor_telegram_user_id)
    )
    if actor is None:
        raise PlaceInvitePermissionError("Ссылку выдаёт администратор площадки.")
    return actor


def _create_link(session: Session, *, place_id: int, actor: User) -> InviteLinkView:
    """
    Вставляет новую действующую ссылку, обходя редкую коллизию токена.

    Уникальный индекс «одна активная ссылка на площадку» страхует гонку:
    проигравший откатывается и возвращает уже созданную победителем ссылку.
    """
    last_error: Exception | None = None
    for _attempt in range(MAX_TOKEN_ATTEMPTS):
        row = PlaceInviteLink(
            token=generate_token(),
            place_id=place_id,
            created_by_user_id=actor.id,
            created_at=utcnow(),
        )
        session.add(row)
        try:
            session.commit()
        except (IntegrityError, OperationalError) as exc:
            session.rollback()
            last_error = exc
            existing = get_active_invite(session, place_id)
            if existing is not None:
                return existing
            continue
        except Exception:
            session.rollback()
            raise
        logger.info("Выдана ссылка на создание анонса площадки %s.", place_id)
        view = _view(session, row)
        if view is None:  # pragma: no cover — защита от неверного состояния
            raise PlaceInviteError("Ссылка не сохранилась.")
        return view
    raise PlaceInviteError(
        "Не удалось выдать ссылку из-за одновременной записи."
    ) from last_error
