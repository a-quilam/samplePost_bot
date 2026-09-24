# Dockerfile — сборка и запуск бота Poster
# Сборка из каталога Poster:  docker compose up -d --build
# (либо отдельно: docker build -t post_bot .)

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока не изменится requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код приложения (.env в образ НЕ попадает — см. .dockerignore,
# секреты передаются на этапе запуска через env_file в docker-compose.yml)
COPY . .

# Непривилегированный пользователь + каталоги для данных (volume) и логов
RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data /app/logs \
    && chown -R botuser:botuser /app
USER botuser

CMD ["python", "bot.py"]
