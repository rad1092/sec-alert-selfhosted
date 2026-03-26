from __future__ import annotations

from dataclasses import dataclass


@dataclass
class NotificationResult:
    status: str
    detail: str
    response_code: int | None = None
