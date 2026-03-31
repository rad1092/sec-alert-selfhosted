PYTHON = uv run --python 3.12
VERSION ?= $(shell $(PYTHON) python -c "import tomllib, pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text(encoding='utf-8'))['project']['version'])")
BACKUP_ARCHIVE ?=

.PHONY: dev up down test lint fmt migrate doctor smoke backup restore release-bundle

dev:
	$(PYTHON) uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --reload

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

doctor:
	$(PYTHON) python -m app.cli.release doctor

smoke:
	$(PYTHON) python -m app.cli.release smoke

backup:
	$(PYTHON) python -m app.cli.release backup

restore:
	$(PYTHON) python -m app.cli.release restore --archive "$(BACKUP_ARCHIVE)"

release-bundle:
	$(PYTHON) python -m app.cli.release release-bundle --version "$(VERSION)"
