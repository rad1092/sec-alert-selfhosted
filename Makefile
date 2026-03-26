PYTHON = uv run --python 3.12

.PHONY: up down test lint fmt migrate

up:
	$(PYTHON) uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

down:
	@echo "Stop the running uvicorn process from your terminal session."

test:
	$(PYTHON) pytest

lint:
	$(PYTHON) ruff check .

fmt:
	$(PYTHON) ruff format .

migrate:
	$(PYTHON) alembic upgrade head

