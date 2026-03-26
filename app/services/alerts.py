from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert, DeliveryAttempt, Destination, Filing, StageError
from app.services.notify.slack import SlackNotifier


class AlertDeliveryService:
    def __init__(self, slack_notifier: SlackNotifier) -> None:
        self.slack_notifier = slack_notifier

    def ensure_alert(self, session: Session, filing: Filing) -> tuple[Alert, bool]:
        alert = session.scalar(select(Alert).where(Alert.filing_id == filing.id))
        if alert is not None:
            return alert, False
        alert = Alert(
            filing_id=filing.id,
            status="pending",
            headline=filing.summary_headline,
        )
        session.add(alert)
        session.flush()
        return alert, True

    def deliver_slack_once(self, session: Session, filing: Filing, alert: Alert) -> None:
        destination = session.scalar(
            select(Destination).where(
                Destination.destination_type == "slack",
                Destination.enabled.is_(True),
            )
        )
        if destination is None:
            alert.status = "skipped"
            return

        existing_attempt = session.scalar(
            select(DeliveryAttempt).where(
                DeliveryAttempt.alert_id == alert.id,
                DeliveryAttempt.destination_id == destination.id,
                DeliveryAttempt.channel == "slack",
            )
        )
        if existing_attempt is not None:
            return

        result = self.slack_notifier.send_alert(
            destination_name=destination.name,
            payload={
                "ticker": filing.issuer_ticker,
                "issuer_name": filing.issuer_name,
                "form_type": filing.form_type,
                "filed_at": filing.accepted_at.isoformat() if filing.accepted_at else None,
                "score": filing.score,
                "confidence": filing.confidence,
                "headline": filing.summary_headline,
                "context": filing.summary_context,
                "reasons": filing.reasons or [],
                "source_url": filing.source_url,
                "reporter_names": filing.reporter_names or [],
                "reporter_count": len(filing.reporter_names or []),
            },
        )
        session.add(
            DeliveryAttempt(
                alert_id=alert.id,
                destination_id=destination.id,
                channel="slack",
                status=result.status,
                response_code=result.response_code,
                error_message=result.detail if result.status != "sent" else None,
            )
        )

        if result.status == "sent":
            alert.status = "delivered"
            return

        alert.status = "delivery_failed"
        session.add(
            StageError(
                stage="slack_delivery",
                source_name="slack",
                filing_accession=filing.accession_number,
                error_class="SlackDeliveryError",
                message=result.detail,
                is_retryable=False,
            )
        )
