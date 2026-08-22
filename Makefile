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
	$(VENV)/bin/ruff check src tests
	$(VENV)/bin/mypy src/vasool

up:
	@echo "not implemented"

seed:
	@echo "not implemented"

bench:
	@echo "not implemented"

chaos:
	@echo "not implemented"

live:
	@echo "not implemented"
