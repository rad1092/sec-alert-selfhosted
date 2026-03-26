from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import Destination


@dataclass
class NotificationResult:
    status: str
    detail: str
    response_code: int | None = None
    retryable: bool = False
    error_class: str | None = None


class DeliveryNotifier(Protocol):
    channel: str

    def is_configured(self) -> bool: ...

    def send_test_message(self, destination: Destination) -> NotificationResult: ...

    def send_alert(
        self,
        destination: Destination,
        payload: dict,
    ) -> NotificationResult: ...
