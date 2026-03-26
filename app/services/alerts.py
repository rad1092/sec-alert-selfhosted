from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, DeliveryAttempt, Destination, Filing, StageError
from app.services.notify.base import DeliveryNotifier, NotificationResult
from app.services.summarize.base import effective_summary_for_filing

CHANNEL_ORDER = {"slack": 0, "webhook": 1, "smtp": 2}


class AlertDeliveryService:
    def __init__(self, notifiers: list[DeliveryNotifier]) -> None:
        self.notifiers = {notifier.channel: notifier for notifier in notifiers}

    def ensure_alert(self, session: Session, filing: Filing) -> tuple[Alert, bool]:
        alert = session.scalar(select(Alert).where(Alert.filing_id == filing.id))
        if alert is not None:
            return alert, False
        alert = Alert(
            filing_id=filing.id,
            status="pending",
            headline=effective_summary_for_filing(filing).headline,
        )
        session.add(alert)
        session.flush()
        return alert, True

    def deliver_alert_once(self, session: Session, filing: Filing, alert: Alert) -> None:
        destinations = self._enabled_destinations(session)
        if not destinations:
            alert.status = "skipped"
            session.add(alert)
            return

        payload = self.build_payload(filing)
        for destination in destinations:
            existing_attempt = session.scalar(
                select(DeliveryAttempt).where(
                    DeliveryAttempt.alert_id == alert.id,
                    DeliveryAttempt.destination_id == destination.id,
                    DeliveryAttempt.channel == destination.destination_type,
                )
            )
            if existing_attempt is not None:
                continue
            self._send_and_log_attempt(
                session,
                destination=destination,
                payload=payload,
                alert=alert,
                filing_accession=filing.accession_number,
            )
        self._set_aggregate_alert_status(session, alert, destinations)

    def send_test_message(
        self,
        session: Session,
        destination: Destination,
    ) -> NotificationResult:
        result = self._dispatch(destination, payload=None, test=True)
        session.add(
            DeliveryAttempt(
                destination_id=destination.id,
                alert_id=None,
                channel=destination.destination_type,
                status=result.status,
                response_code=result.response_code,
                retryable=result.retryable,
                error_class=result.error_class,
                error_message=result.detail if result.status != "sent" else None,
            )
        )
        return result

    def build_payload(self, filing: Filing) -> dict:
        summary = effective_summary_for_filing(filing)
        return {
            "ticker": filing.issuer_ticker,
            "issuer_name": filing.issuer_name,
            "form_type": filing.form_type,
            "filed_at": filing.accepted_at.isoformat() if filing.accepted_at else None,
            "score": filing.score,
            "confidence": filing.confidence,
            "headline": summary.headline,
            "context": summary.context,
            "reasons": filing.reasons or [],
            "source_url": filing.source_url,
            "reporter_names": filing.reporter_names or [],
            "reporter_count": len(filing.reporter_names or []),
        }

    def _enabled_destinations(self, session: Session) -> list[Destination]:
        destinations = session.scalars(
            select(Destination).where(Destination.enabled.is_(True))
        ).all()
        destinations = [
            destination
            for destination in destinations
            if destination.destination_type in self.notifiers
        ]
        return sorted(
            destinations,
            key=lambda destination: CHANNEL_ORDER.get(destination.destination_type, 99),
        )

    def _dispatch(
        self,
        destination: Destination,
        *,
        payload: dict | None,
        test: bool,
    ) -> NotificationResult:
        notifier = self.notifiers.get(destination.destination_type)
        if notifier is None:
            return NotificationResult(
                status="failed",
                detail=f"Unsupported destination type '{destination.destination_type}'.",
                retryable=False,
                error_class="UnsupportedDestination",
            )
        try:
            if test:
                return notifier.send_test_message(destination)
            assert payload is not None
            return notifier.send_alert(destination, payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            return NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=False,
                error_class=exc.__class__.__name__,
            )

    def _send_and_log_attempt(
        self,
        session: Session,
        *,
        destination: Destination,
        payload: dict,
        alert: Alert,
        filing_accession: str | None,
    ) -> NotificationResult:
        notifier = self.notifiers.get(destination.destination_type)
        unexpected_exception = False
        try:
            if notifier is None:
                result = NotificationResult(
                    status="failed",
                    detail=f"Unsupported destination type '{destination.destination_type}'.",
                    retryable=False,
                    error_class="UnsupportedDestination",
                )
            else:
                result = notifier.send_alert(destination, payload)
        except Exception as exc:  # pragma: no cover - defensive guard
            unexpected_exception = True
            result = NotificationResult(
                status="failed",
                detail=str(exc),
                retryable=False,
                error_class=exc.__class__.__name__,
            )

        session.add(
            DeliveryAttempt(
                alert_id=alert.id,
                destination_id=destination.id,
                channel=destination.destination_type,
                status=result.status,
                response_code=result.response_code,
                retryable=result.retryable,
                error_class=result.error_class,
                error_message=result.detail if result.status != "sent" else None,
            )
        )
        if unexpected_exception:
            session.add(
                StageError(
                    stage="delivery_dispatch",
                    source_name=destination.destination_type,
                    filing_accession=filing_accession,
                    error_class=result.error_class or "DeliveryDispatchError",
                    message=result.detail,
                    is_retryable=False,
                )
            )
        return result

    def _set_aggregate_alert_status(
        self,
        session: Session,
        alert: Alert,
        destinations: list[Destination],
    ) -> None:
        session.flush()
        if not destinations:
            alert.status = "skipped"
            session.add(alert)
            return

        attempts = session.scalars(
            select(DeliveryAttempt).where(DeliveryAttempt.alert_id == alert.id)
        ).all()
        successful_channels = {
            attempt.channel for attempt in attempts if attempt.status == "sent"
        }
        attempted_channels = {attempt.channel for attempt in attempts}
        enabled_channels = {destination.destination_type for destination in destinations}
        if successful_channels & enabled_channels:
            alert.status = "delivered"
        elif attempted_channels & enabled_channels:
            alert.status = "delivery_failed"
        else:
            alert.status = "skipped"
        session.add(alert)
