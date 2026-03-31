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
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("OPENAI_MODEL", None)

from app.config import Settings
from app.main import create_app
from app.services.sec.client import FixtureSecClient, SecTextResponse


class LenientFixtureSecClient(FixtureSecClient):
    def get_json(self, url: str) -> dict:
        try:
            return super().get_json(url)
        except KeyError:
            return {"filings": {"recent": {}}, "name": None, "tickers": []}

    def get_text(self, url: str) -> str:
        return self.get_text_response(url).text

    def get_text_response(self, url: str) -> SecTextResponse:
        try:
            return super().get_text_response(url)
        except KeyError:
            if url.endswith(".idx"):
                text = "Description|Header|Ignored|Ignored|Ignored\n"
            elif url.endswith("output=atom"):
                text = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
                )
            else:
                text = ""
            return SecTextResponse(
                text=text,
                status_code=200,
                content_type="text/plain",
                final_url=url,
                body_length=len(text),
            )


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
        OPENAI_API_KEY=None,
        OPENAI_MODEL=None,
        SCHEDULER_ENABLED=False,
        TESTING=True,
    )


@pytest.fixture()
def client(settings: Settings):
    app = create_app(
        settings,
        service_overrides={"sec_client": LenientFixtureSecClient()},
    )
    with TestClient(app) as test_client:
        yield test_client
