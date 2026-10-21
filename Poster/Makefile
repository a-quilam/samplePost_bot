# Проверки проекта Poster. Выполнять из каталога Poster:
#   make check   — все проверки подряд (линтинг, типы, тесты)
#   make accept  — релизная приёмка (= check: стиль, типы, unit- и
#                  интеграционные тесты, критические сценарии экрана,
#                  smoke-запуск, миграции; без живого Telegram)
#   make format  — автоформатирование (black + автоисправления ruff)
#
# PYTHON указывает на интерпретатор окружения с установленными
# requirements.txt и requirements-dev.txt.

PYTHON ?= python

.PHONY: check accept lint types test format run

check: lint types test

accept: check

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m black --check .

types:
	$(PYTHON) -m mypy .

test:
	$(PYTHON) -W ignore::DeprecationWarning -m unittest discover -s tests

format:
	$(PYTHON) -m black .
	$(PYTHON) -m ruff check --fix .

run:
	$(PYTHON) bot.py
