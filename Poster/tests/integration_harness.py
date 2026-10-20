# tests/integration_harness.py
"""
Интеграционный каркас Stage 9.

Тесты проходят через НАСТОЯЩИЙ Application.process_update со всей цепочкой
регистрации из bot.build_application(): ConversationHandler, глобальные
команды, устаревшие кнопки, обработчик ошибок. Подменяются ТОЛЬКО внешние
границы:

- Telegram API → FakeBot (записывает вызовы, сеть недоступна);
- база данных → временный SQLite-файл (пользовательский post_bot.db
  не затрагивается вообще);
- часы → frozen_today() для относительных дат Stage 8 (utils.parsers).

Модуль НЕ начинается с «test» — unittest discover его не исполняет;
он используется только из test_stage9_*.py.
"""

import asyncio
import functools
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Coroutine, TypeVar

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

try:
    from telegram import (
        CallbackQuery,
        Chat,
        Message,
        MessageEntity,
        PhotoSize,
        Update,
        User,
    )
    from telegram.ext import ConversationHandler

    import bot as bot_module
    import errors as errors_module
    import handlers.drafts as drafts_module
    import handlers.jobs as jobs_module
    import handlers.post_creation as post_creation_module
    import utils.parsers as parsers_module
    import utils.screen as screen_module
    from base import Base
    from handlers.post_creation import POST_STEPS
    from models import Draft, UserState

    HARNESS_IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 — причина попадает в сообщение пропуска
    HARNESS_IMPORT_ERROR = str(e)


# Общие параметры «личного чата» однопользовательского бота
DEFAULT_CHAT_ID = 777
DEFAULT_USER_ID = 7

# Дата сообщений-фикстур (на разборы не влияет — только для объекта Message)
FIXTURE_DATE = datetime(2026, 1, 1, 12, 0)


class FakeBot:
    """
    Заглушка Telegram Bot: записывает все API-вызовы, сеть недоступна.

    - записи хранятся в self.calls в порядке вызовов;
    - fail_types — типы вызовов, падающие RuntimeError (имитация сбоя
      Telegram API для проверки устойчивости диалога);
    - errors_by_type — тип вызова -> исключение, которое БУДЕТ брошено
      (например BadRequest «Message to edit not found» для проверки
      восстановления экрана);
    - send_* возвращают фиктивные Message с растущим message_id — так
      продукт запоминает id экрана/превью (как от настоящего Telegram);
    - атрибута defaults нет намеренно: Message.reply_text проверяет его
      через hasattr и без него корректно обходится.
    """

    id = 999888777
    username = "stage9_fake_bot"
    is_bot = True
    first_name = "Тест-бот"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_types: set[str] = set()
        self.errors_by_type: dict[str, Exception] = {}
        self._next_message_id = 100

    def _record(self, kind: str, args: tuple, kwargs: dict) -> None:
        if kind in self.fail_types:
            raise RuntimeError(f"имитация сбоя Telegram API: {kind}")
        if kind in self.errors_by_type:
            raise self.errors_by_type[kind]
        self.calls.append({"type": kind, "args": args, **kwargs})

    def _make_message(self, kwargs: dict) -> Any:
        """Фиктивное сообщение бота (для возврата send_message/send_photo)."""
        self._next_message_id += 1
        pos_args: tuple = tuple(kwargs.get("args") or ())
        chat_id = kwargs.get("chat_id")
        if chat_id is None:
            chat_id = pos_args[0] if pos_args else DEFAULT_CHAT_ID
        text = kwargs.get("text")
        if text is None:
            text = pos_args[1] if len(pos_args) > 1 else kwargs.get("caption")
        message = Message(
            message_id=self._next_message_id,
            date=FIXTURE_DATE,
            chat=Chat(id=chat_id, type="private"),
            from_user=User(id=self.id, first_name=self.first_name, is_bot=True),
            text=text,
        )
        message.set_bot(self)  # type: ignore[arg-type]
        return message

    # —— жизненный цикл (Application.initialize/shutdown) ——
    async def initialize(self) -> bool:
        return True

    async def shutdown(self) -> bool:
        return True

    async def get_me(self) -> Any:
        return SimpleNamespace(
            id=self.id,
            is_bot=True,
            first_name=self.first_name,
            username=self.username,
        )

    # —— методы, которые реально зовут обработчики ——
    async def send_message(self, *args: Any, **kwargs: Any) -> Any:
        self._record("message", args, kwargs)
        return self._make_message({**kwargs, "args": args})

    async def send_photo(self, *args: Any, **kwargs: Any) -> Any:
        self._record("photo", args, kwargs)
        return self._make_message({**kwargs, "args": args})

    async def send_media_group(self, *args: Any, **kwargs: Any) -> list:
        self._record("media_group", args, kwargs)
        return [self._make_message({"chat_id": kwargs.get("chat_id")})]

    async def answer_callback_query(self, *args: Any, **kwargs: Any) -> None:
        self._record("callback_answer", args, kwargs)

    async def edit_message_text(self, *args: Any, **kwargs: Any) -> None:
        self._record("edit_message", args, kwargs)

    async def delete_message(self, *args: Any, **kwargs: Any) -> None:
        self._record("delete_message", args, kwargs)


def bind_bot(update: Update, bot: FakeBot) -> None:
    """
    Привязывает бота к update и всем вложенным объектам.

    В PTB вручную собранные Update/Message/CallbackQuery не знают бота:
    шорты (reply_text, query.answer, edit_message_text) берут его через
    get_bot() и без set_bot падают с RuntimeError.
    """
    # FakeBot подменяет Bot по duck-typing — для set_bot нужен Any
    bound: Any = bot
    update.set_bot(bound)
    if update.message is not None:
        update.message.set_bot(bound)
    if update.edited_message is not None:
        update.edited_message.set_bot(bound)
    query = update.callback_query
    if query is not None:
        query.set_bot(bound)
        if query.message is not None:
            query.message.set_bot(bound)


def make_text_update(
    text: str,
    *,
    chat_id: int = DEFAULT_CHAT_ID,
    user_id: int = DEFAULT_USER_ID,
    update_id: int = 1,
    message_id: int = 1,
    entities: list[MessageEntity] | None = None,
) -> Update:
    """
    Текстовое сообщение.

    entities=None — «автоматика» (командам добавляется bot_command-entity);
    явный список, включая пустой, задаёт ровно его — так в тестах передаётся
    форматирование пользовательского ввода.
    """
    if entities is None:
        entities = []
        if text.startswith("/"):
            entities = [
                MessageEntity(
                    type=MessageEntity.BOT_COMMAND, offset=0, length=len(text)
                )
            ]
    message = Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, first_name="Тест", is_bot=False),
        text=text,
        entities=entities,
    )
    return Update(update_id=update_id, message=message)


def make_callback_update(
    data: str,
    *,
    chat_id: int = DEFAULT_CHAT_ID,
    user_id: int = DEFAULT_USER_ID,
    update_id: int = 2,
    message_id: int = 2,
) -> Update:
    """Нажатие inline-кнопки (сообщение бота как контекст callback'а)."""
    user = User(id=user_id, first_name="Тест", is_bot=False)
    message = Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=chat_id, type="private"),
        from_user=user,
        text=None,
    )
    query = CallbackQuery(
        id=f"cb{update_id}",
        chat_instance="ci",
        from_user=user,
        message=message,
        data=data,
    )
    return Update(update_id=update_id, callback_query=query)


def make_photo_update(
    file_id: str,
    *,
    media_group_id: str | None = None,
    chat_id: int = DEFAULT_CHAT_ID,
    user_id: int = DEFAULT_USER_ID,
    update_id: int = 3,
    message_id: int = 3,
) -> Update:
    """Фото-сообщение (опционально — часть альбома)."""
    message = Message(
        message_id=message_id,
        date=FIXTURE_DATE,
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, first_name="Тест", is_bot=False),
        photo=[
            PhotoSize(
                file_id=file_id, file_unique_id=f"u_{file_id}", width=100, height=100
            )
        ],
        media_group_id=media_group_id,
    )
    return Update(update_id=update_id, message=message)


# Ключи записей ПРЯМЫХ send_message из send_post (и его частей): ровно
# chat_id/text/parse_mode[/reply_markup], без «лишних» параметров. Ответы
# бота на сообщения пользователя (Message.reply_text) всегда передают
# полный пакет параметров (reply_parameters, entities, read_timeout, ...) —
# по этому набору ключей пост и отличается от ответа/лид-сообщения
# (у лид-сообщения нет parse_mode вовсе).
POST_MESSAGE_KEYS = frozenset(
    {"type", "args", "chat_id", "text", "parse_mode", "reply_markup"}
)

# ЭКРАН (utils/screen.render_screen): тот же send_message, что и у поста,
# плюс маркер disable_web_page_preview=True — по нему экран и отличается
# от поста (send_post этот параметр не передаёт).


def is_screen_record(record: dict) -> bool:
    """
    Экран (постоянное сообщение управления): send или edit с маркером
    disable_web_page_preview=True. Значение важно: PTB передаёт этот ключ
    (со значением None) во всех Message.reply_* — различаем именно True.
    """
    if record.get("disable_web_page_preview") is not True:
        return False
    return record["type"] in ("message", "edit_message")


def is_post_message(record: dict) -> bool:
    """Сообщение поста (превью/готовый пост), а не ответ на сообщение юзера."""
    return (
        record["type"] == "message"
        and "parse_mode" in record
        and frozenset(record) <= POST_MESSAGE_KEYS
    )


def call_text(record: dict) -> str | None:
    """Текст записанного вызова send_message/edit_message_text."""
    if record.get("text") is not None:
        return record["text"]
    args = record.get("args") or ()
    return args[0] if args else None


def inline_buttons(markup: Any) -> list[tuple[str, str | None]]:
    """Плоский список (текст, callback_data) inline-клавиатуры."""
    if markup is None:
        return []
    return [
        (button.text, button.callback_data)
        for row in markup.inline_keyboard
        for button in row
    ]


@contextmanager
def frozen_today(year: int, month: int, day: int):
    """
    Замораживает «сейчас» для парсера дат Stage 8 (utils.parsers).

    parse_date_flexible берёт текущую дату только через
    datetime.now(APP_TZ) — подменяется класс datetime в модуле parsers;
    конструктор datetime при этом работает как прежде (наследник).
    """

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(year, month, day, 12, 0, 0, tzinfo=tz)

    original = parsers_module.datetime
    parsers_module.datetime = FrozenDateTime  # type: ignore[misc]
    try:
        yield
    finally:
        parsers_module.datetime = original  # type: ignore[misc]


class Harness:
    """
    Полное интеграционное окружение одного теста:

    - реальное приложение из bot.build_application();
    - FakeBot вместо Bot (без сети, без Updater);
    - временный SQLite-файл вместо post_bot.db (SessionLocal исправлен
      в модулях, которые его импортируют);
    - сброс троттлинга уведомлений об ошибках (errors._last_error_notice).

    Все *_feed-методы прогоняют update через Application.process_update —
    то есть через настоящие фильтры и ConversationHandler.
    """

    def __init__(
        self,
        chat_id: int = DEFAULT_CHAT_ID,
        user_id: int = DEFAULT_USER_ID,
    ) -> None:
        self.chat_id = chat_id
        self.user_id = user_id
        self.bot = FakeBot()
        self._tmp = tempfile.TemporaryDirectory(prefix="poster_stage9_")
        db_path = Path(self._tmp.name) / "stage9.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

        # Патчим внешнюю границу «база данных» во всех потребителях
        self._orig_session_local = (
            post_creation_module.SessionLocal,
            drafts_module.SessionLocal,
            jobs_module.SessionLocal,
            screen_module.SessionLocal,
        )
        post_creation_module.SessionLocal = self.session_factory
        drafts_module.SessionLocal = self.session_factory
        jobs_module.SessionLocal = self.session_factory
        screen_module.SessionLocal = self.session_factory

        # Уведомления об ошибках троттлятся по (chat_id, time.monotonic) —
        # без сброса тесты «повторно заметили бы» чужие уведомления
        self._orig_error_notice = dict(errors_module._last_error_notice)
        errors_module._last_error_notice.clear()

        self.app: Any = None
        self._update_seq = 0
        # message_id сообщений пользователя растут, как у настоящего Telegram:
        # очистка временного ввода (удаление сообщений) проверяется по id
        self._user_msg_seq = 0

    def _next_user_msg_id(self) -> int:
        self._user_msg_seq += 1
        return self._user_msg_seq

    async def start(self) -> "Harness":
        self.app = bot_module.build_application()
        # Updater при сборке получает реальный Bot — отключаем (в тестах
        # polling не нужен, а initialize() иначе позовёт getMe по сети)
        self.app.updater = None
        # Все вызовы Telegram API уходят в FakeBot; bot подменяется ДО
        # initialize(), иначе Application позовёт реальный Bot.initialize
        self.app.bot = self.bot
        await self.app.initialize()
        return self

    async def close(self) -> None:
        if self.app is not None:
            await self.app.shutdown()
        post_creation_module.SessionLocal = self._orig_session_local[0]
        drafts_module.SessionLocal = self._orig_session_local[1]
        jobs_module.SessionLocal = self._orig_session_local[2]
        screen_module.SessionLocal = self._orig_session_local[3]
        errors_module._last_error_notice.clear()
        errors_module._last_error_notice.update(self._orig_error_notice)
        self.engine.dispose()
        self._tmp.cleanup()

    # —— отправка update'ов через настоящий Application.process_update ——

    async def feed(self, update: Update) -> Update:
        bind_bot(update, self.bot)
        await self.app.process_update(update)
        return update

    async def text(
        self,
        text: str,
        *,
        chat_id: int | None = None,
        user_id: int | None = None,
        entities: list[MessageEntity] | None = None,
    ) -> Update:
        self._update_seq += 1
        return await self.feed(
            make_text_update(
                text,
                chat_id=chat_id if chat_id is not None else self.chat_id,
                user_id=user_id if user_id is not None else self.user_id,
                update_id=self._update_seq,
                message_id=self._next_user_msg_id(),
                entities=entities,
            )
        )

    async def callback(
        self, data: str, *, chat_id: int | None = None, user_id: int | None = None
    ) -> Update:
        self._update_seq += 1
        return await self.feed(
            make_callback_update(
                data,
                chat_id=chat_id if chat_id is not None else self.chat_id,
                user_id=user_id if user_id is not None else self.user_id,
                update_id=self._update_seq,
            )
        )

    async def photo(
        self,
        file_id: str,
        *,
        media_group_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
    ) -> Update:
        self._update_seq += 1
        return await self.feed(
            make_photo_update(
                file_id,
                media_group_id=media_group_id,
                chat_id=chat_id if chat_id is not None else self.chat_id,
                user_id=user_id if user_id is not None else self.user_id,
                update_id=self._update_seq,
                message_id=self._next_user_msg_id(),
            )
        )

    # —— состояние диалога и данные ——

    def conversation(self) -> Any:
        return next(
            h for h in self.app.handlers[0] if isinstance(h, ConversationHandler)
        )

    def dialog_state(
        self, *, chat_id: int | None = None, user_id: int | None = None
    ) -> Any:
        key = (
            chat_id if chat_id is not None else self.chat_id,
            user_id if user_id is not None else self.user_id,
        )
        return self.conversation()._conversations.get(key)

    def user_data(self, *, user_id: int | None = None) -> dict:
        return self.app.user_data.get(
            user_id if user_id is not None else self.user_id, {}
        )

    # —— выборки из FakeBot ——

    @property
    def calls(self) -> list[dict]:
        return self.bot.calls

    def replies(self) -> list[dict]:
        """
        Сообщения бота (промпты/ответы), НЕ являющиеся постами и НЕ
        являющиеся экранами: лид-сообщения, отдельные подсказки.
        """
        return [
            c
            for c in self.calls
            if c["type"] == "message"
            and not is_post_message(c)
            and not is_screen_record(c)
        ]

    def reply_texts(self) -> list[str]:
        return [call_text(c) or "" for c in self.replies()]

    # —— экран (постоянное сообщение управления) ——

    def screens(self) -> list[dict]:
        """Все вызовы отрисовки экрана (send и edit) в порядке вызовов."""
        return [c for c in self.calls if is_screen_record(c)]

    def screen(self) -> dict | None:
        """Последнее состояние экрана (его текст и клавиатура)."""
        records = self.screens()
        return records[-1] if records else None

    def screen_text(self) -> str:
        """Текст экрана на текущий момент ("" — экрана ещё не было)."""
        record = self.screen()
        return (call_text(record) or "") if record else ""

    def screen_texts(self) -> list[str]:
        """История текстов экрана в порядке отрисовки (send и edit)."""
        return [call_text(c) or "" for c in self.screens()]

    def screen_buttons(self) -> list[tuple[str, str | None]]:
        """Inline-кнопки экрана на текущий момент."""
        record = self.screen()
        if record is None:
            return []
        return inline_buttons(record.get("reply_markup"))

    def edits(self) -> list[dict]:
        """Все edit_message_text (перерисовки экрана и превью)."""
        return [c for c in self.calls if c["type"] == "edit_message"]

    def deletes(self) -> list[dict]:
        """Все delete_message (очистка временных сообщений)."""
        return [c for c in self.calls if c["type"] == "delete_message"]

    def deleted_ids(self) -> list[int]:
        """message_id всех удалённых сообщений (в порядке удаления)."""
        out: list[int] = []
        for c in self.deletes():
            pos_args: tuple = tuple(c.get("args") or ())
            message_id = pos_args[1] if len(pos_args) > 1 else c.get("message_id")
            if message_id is None:
                continue
            out.append(int(message_id))
        return out

    def posts(self) -> list[str | None]:
        """
        Тексты отправленных ПОСТОВ (превью и готовые) в порядке отправки:
        сообщения, фото с подписью и первые подписи альбомов.
        """
        out: list[str | None] = []
        for c in self.calls:
            if is_post_message(c):
                out.append(call_text(c))
            elif c["type"] == "photo":
                out.append(c.get("caption"))
            elif c["type"] == "media_group":
                first = c["media"][0]
                out.append(getattr(first, "caption", None))
        return out

    def post_records(self) -> list[dict]:
        """
        Сырые записи ПОСТОВ (превью/готовый) в порядке отправки: send_photo,
        send_media_group и прямые send_message из send_post. Экран управления
        и ответы бота сюда не попадают — после превью экран перерисовывается,
        и tests[-1] по всем calls был бы экраном, а не превью.
        """
        return [
            c
            for c in self.calls
            if c["type"] in ("photo", "media_group") or is_post_message(c)
        ]

    def answers(self) -> list[dict]:
        """Все answer_callback_query (защита от «висящего» спиннера)."""
        return [c for c in self.calls if c["type"] == "callback_answer"]

    # —— БД (временная) ——

    def drafts(self, *, user_id: int | None = None) -> list:
        session = self.session_factory()
        try:
            query = session.query(Draft)
            if user_id is not None:
                query = query.filter(Draft.user_id == user_id)
            return query.order_by(Draft.id).all()
        finally:
            session.close()

    def user_states(self) -> list:
        """Все строки таблицы user_states (экран + WIP-снимки)."""
        session = self.session_factory()
        try:
            return session.query(UserState).order_by(UserState.user_id).all()
        finally:
            session.close()

    # —— удобные последовательности ——

    async def start_dialog(self) -> None:
        """«✏️ Создать пост» → активный диалог, шаг 1."""
        await self.text("✏️ Создать пост")

    async def goto_step(self, index: int) -> None:
        """Начинает диалог и пропускает шаги до index (0-based)."""
        await self.start_dialog()
        for _ in range(index):
            await self.text("Пропустить")


async def goto_preview(h: Harness, *, title: str = "Заголовок для превью") -> None:
    """Доводит диалог до превью: заголовок + пропуски остальных полей."""
    await h.start_dialog()
    await h.text(title)
    for _ in range(len(POST_STEPS) - 2):
        await h.text("Пропустить")
    await h.text("Пропустить")  # шаг «Изображение» → сразу превью


_T = TypeVar("_T", bound=Callable[..., Coroutine[Any, Any, None]])


def scenario_test(method: _T) -> _T:
    """
    Декоратор async-теста для unittest: тело выполняется в одном event
    loop вместе с созданием и закрытием Harness (Application создаёт
    asyncio-примитивы — жить они должны в одной петле).
    """

    @functools.wraps(method)
    def wrapper(self: Any) -> None:
        async def run() -> None:
            harness = Harness()
            await harness.start()
            try:
                await method(self, harness)
            finally:
                await harness.close()

        asyncio.run(run())

    return wrapper  # type: ignore[return-value]
