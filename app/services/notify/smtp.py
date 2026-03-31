from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import getaddresses

from app.config import Settings
from app.models import Destination
from app.services.notify.base import NotificationResult


class SmtpNotifier:
    channel = "smtp"

    def __init__(
        self,
        settings: Settings,
        *,
        smtp_factory=None,
        smtp_ssl_factory=None,
    ) -> None:
        self.settings = settings
        self._smtp_factory = smtp_factory or smtplib.SMTP
        self._smtp_ssl_factory = smtp_ssl_factory or smtplib.SMTP_SSL

    def is_configured(self) -> bool:
        return all(
            [
                self.settings.smtp_host,
                self.settings.smtp_port,
                self.settings.smtp_from,
                self._recipient_list(),
            ]
        )

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
                "headline": f"[SEC Alert] SMTP test message for destination '{destination.name}'.",
                "context": "This is a delivery test for the configured SMTP destination.",
                "reasons": [],
                "source_url": None,
                "reporter_names": [],
                "reporter_count": 0,
            },
        )

    def send_alert(self, destination: Destination, payload: dict) -> NotificationResult:
        validation_error = self._validate_configuration()
        if validation_error is not None:
            return validation_error

        recipients = self._recipient_list()
        if not recipients:
            return NotificationResult(
                status="failed",
                detail="SMTP_TO must contain at least one recipient.",
                retryable=False,
                error_class="MissingConfiguration",
            )

        message = EmailMessage()
        message["From"] = self.settings.smtp_from or ""
        message["To"] = ", ".join(recipients)
        message["Subject"] = self._build_subject(payload)
        message.set_content(self._build_body(payload))

        try:
            self._send(message, recipients)
        except smtplib.SMTPNotSupportedError:
            return NotificationResult(
                status="failed",
                detail="SMTP server does not support STARTTLS for authenticated delivery.",
                retryable=False,
                error_class="SMTPNotSupportedError",
            )
        except smtplib.SMTPAuthenticationError as exc:
            return NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=False,
                error_class="SMTPAuthenticationError",
            )
        except smtplib.SMTPException as exc:
            return NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=True,
                error_class=exc.__class__.__name__,
            )
        except OSError as exc:
            return NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=True,
                error_class=exc.__class__.__name__,
            )

        return NotificationResult(
            status="sent",
            detail="SMTP message sent successfully.",
            response_code=250,
        )

    def _validate_configuration(self) -> NotificationResult | None:
        if not self.settings.smtp_host:
            return NotificationResult(
                status="failed",
                detail="SMTP_HOST is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        if not self.settings.smtp_port:
            return NotificationResult(
                status="failed",
                detail="SMTP_PORT is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        if not self.settings.smtp_from:
            return NotificationResult(
                status="failed",
                detail="SMTP_FROM is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        if not self.settings.smtp_to:
            return NotificationResult(
                status="failed",
                detail="SMTP_TO is not set.",
                retryable=False,
                error_class="MissingConfiguration",
            )
        return None

    def _recipient_list(self) -> list[str]:
        raw_value = self.settings.smtp_to or ""
        normalized = raw_value.replace(";", ",").replace("\n", ",").replace("\r", ",")
        parsed = []
        for _name, addr in getaddresses([normalized]):
            cleaned = addr.strip()
            if cleaned and "@" in cleaned:
                parsed.append(cleaned)
        return list(dict.fromkeys(parsed))

    def _send(self, message: EmailMessage, recipients: list[str]) -> None:
        host = self.settings.smtp_host or ""
        port = self.settings.smtp_port or 0
        username = (
            self.settings.smtp_username.get_secret_value()
            if self.settings.smtp_username is not None
            else None
        )
        password = (
            self.settings.smtp_password.get_secret_value()
            if self.settings.smtp_password is not None
            else None
        )
        is_local_host = host.lower() in {"localhost", "127.0.0.1"}

        if port == 465:
            with self._smtp_ssl_factory(host, port, timeout=10) as smtp:
                smtp.ehlo()
                if username and password:
                    smtp.login(username, password)
                smtp.send_message(message, to_addrs=recipients)
            return

        with self._smtp_factory(host, port, timeout=10) as smtp:
            smtp.ehlo()
            if hasattr(smtp, "has_extn") and smtp.has_extn("starttls"):
                smtp.starttls()
                smtp.ehlo()
            elif username and password and not is_local_host:
                raise smtplib.SMTPNotSupportedError("STARTTLS is required for authenticated SMTP.")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message, to_addrs=recipients)

    def _build_subject(self, payload: dict) -> str:
        issuer = payload.get("ticker") or payload.get("issuer_name") or "Issuer"
        form_type = payload.get("form_type") or "SEC alert"
        headline = payload.get("headline") or "SEC alert"
        return f"[SEC Alert] {issuer} {form_type} - {headline}"

    def _build_body(self, payload: dict) -> str:
        lines = [
            payload.get("headline") or "SEC alert",
            payload.get("context") or "",
        ]
        score = payload.get("score")
        confidence = payload.get("confidence")
        if score is not None or confidence is not None:
            lines.append(f"Score: {score} | Confidence: {confidence}")
        reasons = payload.get("reasons") or []
        if reasons:
            lines.append("Reasons: " + "; ".join(reasons))
        source_url = payload.get("source_url")
        if source_url:
            lines.append(f"Source: {source_url}")
        return "\n".join(line for line in lines if line)
