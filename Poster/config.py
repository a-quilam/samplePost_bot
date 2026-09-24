# config.py

import os

from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получение токена бота из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен в .env файле.")
