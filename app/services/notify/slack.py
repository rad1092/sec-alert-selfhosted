from __future__ import annotations

from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.services.notify.base import NotificationResult


class SlackNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return self.settings.slack_webhook_url is not None

    def allowed(self) -> bool:
        if self.settings.slack_webhook_url is None:
            return False
        parsed = urlparse(self.settings.slack_webhook_url.get_secret_value())
        return parsed.scheme == "https"

    def send_test_message(self, destination_name: str) -> NotificationResult:
        if self.settings.slack_webhook_url is None:
            return NotificationResult(status="skipped", detail="SLACK_WEBHOOK_URL is not set.")
        if not self.allowed():
            return NotificationResult(
                status="failed",
                detail="Slack webhook URL must use https.",
            )
        response = httpx.post(
            self.settings.slack_webhook_url.get_secret_value(),
            json={"text": f"[SEC Alert] Slack test message for destination '{destination_name}'."},
            timeout=10.0,
        )
        if response.status_code >= 400:
            return NotificationResult(
                status="failed",
                detail=f"Slack responded with HTTP {response.status_code}.",
                response_code=response.status_code,
            )
        return NotificationResult(
            status="sent",
            detail="Slack test message sent successfully.",
            response_code=response.status_code,
        )
