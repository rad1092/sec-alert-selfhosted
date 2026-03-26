from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("SEC_USER_AGENT", "SEC Alert Test test@example.com")
os.environ.setdefault("APP_HOST", "127.0.0.1")

from app.config import Settings
from app.main import create_app


def extract_csrf_token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_HOST="127.0.0.1",
        APP_PORT=8000,
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        SEC_POLL_INTERVAL_SECONDS=60,
        SEC_RATE_LIMIT_RPS=2,
        SCHEDULER_ENABLED=False,
        TESTING=True,
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
