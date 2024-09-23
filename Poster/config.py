# config.py

import os
from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получение токена бота из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Получение ID чата для отправки постов на согласование
REVIEW_CHAT_ID = os.getenv("REVIEW_CHAT_ID")

# Получение списка администраторов из переменных окружения
# Предполагается, что ADMIN_IDS хранятся в виде "123456789,987654321"
ADMIN_IDS = [int(admin_id.strip()) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id.strip().isdigit()]

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN не установлен в .env файле.")

if not REVIEW_CHAT_ID:
    raise ValueError("REVIEW_CHAT_ID не установлен в .env файле.")

if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS не установлены или пусты в .env файле.")
