# utils/validators.py

import re
from datetime import datetime
from urllib.parse import urlparse

def validate_date(date_text: str) -> bool:
    """
    Проверяет, соответствует ли строка формату даты ДД.ММ или ДД.ММ.ГГГГ.
    """
    try:
        # Попытка распарсить дату с годом
        datetime.strptime(date_text, '%d.%m.%Y')
        return True
    except ValueError:
        try:
            # Попытка распарсить дату без года
            datetime.strptime(date_text, '%d.%m')
            return True
        except ValueError:
            return False

def validate_time(time_text: str) -> bool:
    """
    Проверяет, соответствует ли строка формату времени ЧЧ:ММ.
    """
    try:
        datetime.strptime(time_text, '%H:%M')
        return True
    except ValueError:
        return False

def validate_url(url_text: str) -> bool:
    """
    Проверяет, является ли строка действительным URL.
    """
    url_pattern = re.compile(
        r'^(https?://)'  # http:// или https://
        r'(([A-Za-z0-9-]+\.)+[A-Za-z]{2,})'  # Доменное имя
        r'(:\d+)?'  # Порт (опционально)
        r'(/.*)?$'    # Путь (опционально)
    )
    return re.match(url_pattern, url_text) is not None
