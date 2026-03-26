from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.broker import SecRequestBroker

logger = logging.getLogger(__name__)

ALLOWED_SEC_HOSTS = {"www.sec.gov", "sec.gov", "data.sec.gov"}


class SecHttpClient:
    def __init__(
        self,
        settings: Settings,
        broker: SecRequestBroker,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.http_client = http_client or httpx.Client(
            follow_redirects=True,
            timeout=20.0,
            headers={"User-Agent": settings.sec_user_agent},
        )

    def close(self) -> None:
        self.http_client.close()

    def get_json(self, url: str) -> dict:
        response = self._request("GET", url)
        return response.json()

    def get_text(self, url: str) -> str:
        response = self._request("GET", url)
        return response.text

    def download_json(self, url: str) -> dict:
        return self.get_json(url)

    def _request(self, method: str, url: str) -> httpx.Response:
        self._validate_url(url)
        backoff = 0.25
        last_error: Exception | None = None
        response: httpx.Response | None = None

        for attempt in range(3):
            self._wait_for_budget()
            try:
                response = self.http_client.request(method, url)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise RuntimeError(f"SEC request failed for {url}") from exc

            if response.status_code in {403, 429}:
                self.broker.record_http_status(response.status_code)

            if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                logger.warning(
                    "Transient SEC response %s for %s; retrying.",
                    response.status_code,
                    url,
                )
                time.sleep(backoff)
                backoff *= 2
                continue

            response.raise_for_status()
            return response

        if response is not None:
            response.raise_for_status()
        raise RuntimeError(f"SEC request failed for {url}") from last_error

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("SEC URL must be http or https.")
        if parsed.netloc.lower() not in ALLOWED_SEC_HOSTS:
            raise ValueError(f"SEC URL host is not allowed: {parsed.netloc}")

    def _wait_for_budget(self) -> None:
        while not self.broker.can_issue_request():
            time.sleep(0.05)


class FixtureSecClient:
    def __init__(
        self,
        json_map: Mapping[str, dict] | None = None,
        text_map: Mapping[str, str] | None = None,
    ) -> None:
        self.json_map = dict(json_map or {})
        self.text_map = dict(text_map or {})
        self.calls: list[str] = []

    def close(self) -> None:
        return None

    def download_json(self, url: str) -> dict:
        return self.get_json(url)

    def get_json(self, url: str) -> dict:
        self.calls.append(url)
        if url not in self.json_map:
            raise KeyError(f"No JSON fixture for {url}")
        return self.json_map[url]

    def get_text(self, url: str) -> str:
        self.calls.append(url)
        if url not in self.text_map:
            raise KeyError(f"No text fixture for {url}")
        return self.text_map[url]
