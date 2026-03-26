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
        return self.send_alert(
            destination_name=destination_name,
            payload={
                "headline": f"[SEC Alert] Slack test message for destination '{destination_name}'.",
                "context": "This is a Phase 2 Slack delivery smoke test.",
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

    def send_alert(self, destination_name: str, payload: dict) -> NotificationResult:
        if self.settings.slack_webhook_url is None:
            return NotificationResult(status="skipped", detail="SLACK_WEBHOOK_URL is not set.")
        if not self.allowed():
            return NotificationResult(
                status="failed",
                detail="Slack webhook URL must use https.",
            )
        response = httpx.post(
            self.settings.slack_webhook_url.get_secret_value(),
            json={"text": self._format_text(payload, destination_name)},
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
