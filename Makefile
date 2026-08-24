VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

# Optional: a fresh clone has no .env (gitignored, only .env.example is committed) and every
# target below still works with none -- stub LLM, no live Razorpay client, SQLite defaults.
# When .env does exist, load it into every recipe's environment automatically so `make up`
# actually picks up VASOOL_LLM=live without a manual `source .env` first -- forgetting that
# step is what silently serves stub mode with no error, three separate times now.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: install test lint up seed bench chaos live load

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

load:
	$(VENV)/bin/python scripts/load_test.py
