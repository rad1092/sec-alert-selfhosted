from __future__ import annotations

import contextvars
import logging
import re
from collections.abc import Iterable

from app.config import Settings

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id",
    default="-",
)


AUTHORIZATION_PATTERN = re.compile(
    r"(authorization\s*[:=]\s*)(.+)",
    re.IGNORECASE,
)


class SensitiveDataFilter(logging.Filter):
    def __init__(self, secrets_to_redact: Iterable[str] | None = None) -> None:
        super().__init__()
        self.secrets_to_redact = [item for item in (secrets_to_redact or []) if item]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        message = AUTHORIZATION_PATTERN.sub(r"\1[REDACTED]", message)
        for secret in self.secrets_to_redact:
            message = message.replace(secret, "[REDACTED]")
        record.msg = message
        record.args = ()
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def configure_logging(settings: Settings) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.addFilter(RequestIdFilter())
    secrets_to_redact = []
    if settings.slack_webhook_url is not None:
        secrets_to_redact.append(settings.slack_webhook_url.get_secret_value())
    if settings.alert_webhook_url is not None:
        secrets_to_redact.append(settings.alert_webhook_url.get_secret_value())
    if settings.alert_webhook_secret is not None:
        secrets_to_redact.append(settings.alert_webhook_secret.get_secret_value())
    if settings.smtp_username is not None:
        secrets_to_redact.append(settings.smtp_username.get_secret_value())
    if settings.smtp_password is not None:
        secrets_to_redact.append(settings.smtp_password.get_secret_value())
    handler.addFilter(SensitiveDataFilter(secrets_to_redact))
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(request_id)s] %(name)s - %(message)s",
    )
    handler.setFormatter(formatter)
    root.addHandler(handler)
