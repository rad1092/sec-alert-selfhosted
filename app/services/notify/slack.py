from __future__ import annotations

from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.models import Destination
from app.services.notify.base import NotificationResult


class SlackNotifier:
    channel = "slack"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def is_configured(self) -> bool:
        return self.settings.slack_webhook_url is not None

    def allowed(self) -> bool:
        if self.settings.slack_webhook_url is None:
            return False
        parsed = urlparse(self.settings.slack_webhook_url.get_secret_value())
        return parsed.scheme == "https"

    def send_test_message(self, destination: Destination) -> NotificationResult:
        return self.send_alert(
            destination=destination,
            payload={
                "headline": f"[SEC Alert] Slack test message for destination '{destination.name}'.",
                "context": "This is a delivery test for the configured Slack destination.",
                "score": None,
                "confidence": None,
                "reasons": [],
                "source_url": None,
                "ticker": None,
                "issuer_name": None,
                "form_type": None,
                "filed_at": None,
            },
        )

    def send_alert(self, destination: Destination, payload: dict) -> NotificationResult:
        if self.settings.slack_webhook_url is None:
            return NotificationResult(
                status="failed",
                detail="SLACK_WEBHOOK_URL is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        if not self.allowed():
            return NotificationResult(
                status="failed",
                detail="Slack webhook URL must use https.",
                retryable=False,
                error_class="InvalidConfiguration",
            )
        try:
            response = httpx.post(
                self.settings.slack_webhook_url.get_secret_value(),
                json={"text": self._format_text(payload, destination.name)},
                timeout=10.0,
            )
        except httpx.TimeoutException:
            return NotificationResult(
                status="failed",
                detail="Slack request timed out.",
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
        if response.status_code >= 400:
            return NotificationResult(
                status="failed",
                detail=f"Slack responded with HTTP {response.status_code}.",
                response_code=response.status_code,
                retryable=response.status_code >= 500,
                error_class="HttpError",
            )
        return NotificationResult(
            status="sent",
            detail="Slack message sent successfully.",
            response_code=response.status_code,
        )

    def _format_text(self, payload: dict, destination_name: str) -> str:
        headline = payload.get("headline") or "SEC alert"
        context = payload.get("context") or ""
        reasons = payload.get("reasons") or []
        source_url = payload.get("source_url") or ""
        score = payload.get("score")
        confidence = payload.get("confidence")
        form_type = payload.get("form_type") or ""
        issuer_name = payload.get("issuer_name") or payload.get("ticker") or "Issuer"

        lines = [
            f"[SEC Alert:{destination_name}] {issuer_name} {form_type}".strip(),
            headline,
        ]
        if context:
            lines.append(context)
        if score is not None or confidence is not None:
            lines.append(f"Score: {score} | Confidence: {confidence}")
        if reasons:
            lines.append("Reasons: " + "; ".join(reasons))
        if source_url:
            lines.append(f"Source: {source_url}")
        return "\n".join(line for line in lines if line)
