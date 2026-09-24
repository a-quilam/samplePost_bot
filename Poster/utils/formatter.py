# utils/formatter.py

import re

def escape_markdown(text: str) -> str:
    """
    Экранирует специальные символы MarkdownV2.
    Вызывать ТОЛЬКО в момент отправки сообщения (не при сохранении в БД).
    """
    # Обратный слэш экранируем первым, чтобы не задвоить уже вставленные '\'
    escape_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '=', '|', '{', '}', '.', '!']
    for char in escape_chars:
        text = text.replace(char, '\\' + char)
    return text

def replace_hyphens(text: str) -> str:
    """
    Заменяет дефисы на тире только в определённых контекстах, избегая замены внутри слов.
    """
    # Заменяем дефисы, окружённые пробелами или символами препинания на тире
    text = re.sub(r'(?<=\s)-(?=\s)', '—', text)
    # Дополнительные правила замены можно добавить по необходимости
    return text

def replace_quotes(text: str) -> str:
    """
    Заменяет внешние кавычки на ёлочки («»), а внутренние на английские (" ").
    """
    # Сначала заменим все двойные кавычки на маркеры
    text = re.sub(r'"', '«', text, count=1)  # Первая кавычка в строке
    text = re.sub(r'"', '»', text, count=1)  # Вторая кавычка в строке

    # Если есть дополнительные кавычки, заменим их на английские
    text = re.sub(r'"', '"', text)

    return text

def format_text(text: str) -> str:
    """
    Типографика текста (кавычки, тире) БЕЗ экранирования Markdown.
    Результат хранится в user_data и в БД как есть;
    экранирование выполняется escape_markdown() только при отправке.
    """
    if text is None:
        return ''
    text = replace_quotes(text)
    text = replace_hyphens(text)
    return text
