from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.models import Destination
from app.services.notify.base import NotificationResult


class WebhookNotifier:
    channel = "webhook"

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._client = httpx.Client(transport=transport, timeout=10.0)

    def is_configured(self) -> bool:
        return self.settings.alert_webhook_url is not None

    def send_test_message(self, destination: Destination) -> NotificationResult:
        return self.send_alert(
            destination,
            payload={
                "ticker": None,
                "issuer_name": None,
                "form_type": None,
                "filed_at": None,
                "score": None,
                "confidence": None,
                "headline": (
                    f"[SEC Alert] Webhook test message for destination "
                    f"'{destination.name}'."
                ),
                "context": "This is a delivery test for the configured webhook destination.",
                "reasons": [],
                "source_url": None,
                "reporter_names": [],
                "reporter_count": 0,
            },
        )

    def send_alert(self, destination: Destination, payload: dict) -> NotificationResult:
        if self.settings.alert_webhook_url is None:
            return NotificationResult(
                status="failed",
                detail="ALERT_WEBHOOK_URL is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        url = self.settings.alert_webhook_url.get_secret_value()
        allowed, detail = self._validate_url(url)
        if not allowed:
            return NotificationResult(
                status="failed",
                detail=detail,
                retryable=False,
                error_class="InvalidConfiguration",
            )

        raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.settings.alert_webhook_secret is not None:
            timestamp = str(int(time.time()))
            signature = hmac.new(
                self.settings.alert_webhook_secret.get_secret_value().encode("utf-8"),
                f"{timestamp}.".encode() + raw_body,
                hashlib.sha256,
            ).hexdigest()
            headers["X-SEC-Alert-Timestamp"] = timestamp
            headers["X-SEC-Alert-Signature"] = f"sha256={signature}"

        try:
            response = self._client.post(url, content=raw_body, headers=headers)
        except httpx.TimeoutException:
            return NotificationResult(
                status="failed",
                detail="Webhook request timed out.",
                retryable=True,
                error_class="TimeoutError",
            )
        except httpx.HTTPError as exc:
            return NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=True,
                error_class=exc.__class__.__name__,
            )

        if 200 <= response.status_code < 300:
            return NotificationResult(
                status="sent",
                detail="Webhook delivered successfully.",
                response_code=response.status_code,
            )
        return NotificationResult(
            status="failed",
            detail=f"Webhook responded with HTTP {response.status_code}.",
            response_code=response.status_code,
            retryable=response.status_code >= 500,
            error_class="HttpError",
        )

    def close(self) -> None:
        self._client.close()

    def _validate_url(self, url: str) -> tuple[bool, str]:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "https":
            return True, ""
        if (
            self.settings.localhost_webhook_test_mode
            and parsed.scheme == "http"
            and host in {"localhost", "127.0.0.1"}
        ):
            return True, ""
        return (
            False,
            "Webhook URL must use https unless LOCALHOST_WEBHOOK_TEST_MODE "
            "allows localhost http.",
        )
