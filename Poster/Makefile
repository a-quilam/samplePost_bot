# Проверки проекта Poster. Выполнять из каталога Poster:
#   make check   — все проверки подряд (линтинг, типы, тесты)
#   make format  — автоформатирование (black + автоисправления ruff)
#
# PYTHON указывает на интерпретатор окружения с установленными
# requirements.txt и requirements-dev.txt.

PYTHON ?= python

.PHONY: check lint types test format run

check: lint types test

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
