VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: install test lint up seed bench chaos live

install:
	python3.11 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests scripts
	$(VENV)/bin/mypy src/vasool
	$(VENV)/bin/mypy scripts

up:
	$(VENV)/bin/uvicorn vasool.api.main:app --host 0.0.0.0 --port 8000

seed:
	$(VENV)/bin/python scripts/seed.py

bench:
	$(VENV)/bin/python scripts/bench.py

chaos:
	$(VENV)/bin/python -m vasool.chaos

live:
	$(VENV)/bin/python scripts/live_demo.py
