from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class WatchlistEntry(TimestampMixin, Base):
    __tablename__ = "watchlist_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False)
    issuer_cik: Mapped[str | None] = mapped_column(String(16))
    manual_cik_override: Mapped[str | None] = mapped_column(String(16))
    issuer_name: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Filing(TimestampMixin, Base):
    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint(
            "accession_number",
            "form_type",
            "issuer_cik",
            name="uq_filings_accession_form_issuer",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    accession_number: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    form_type: Mapped[str] = mapped_column(String(16), nullable=False)
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filed_date: Mapped[date | None] = mapped_column(Date)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    period_of_report: Mapped[date | None] = mapped_column(Date)
    issuer_cik: Mapped[str | None] = mapped_column(String(16))
    issuer_ticker: Mapped[str | None] = mapped_column(String(16))
    issuer_name: Mapped[str | None] = mapped_column(String(255))
    reporter_names: Mapped[list[str] | None] = mapped_column(JSON)
    source_url: Mapped[str | None] = mapped_column(Text)
    detail_url: Mapped[str | None] = mapped_column(Text)
    parser_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    scoring_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    summarization_status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    normalized_payload: Mapped[dict | None] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(String(16))
    reasons: Mapped[list[str] | None] = mapped_column(JSON)
    summary_headline: Mapped[str | None] = mapped_column(Text)
    summary_context: Mapped[str | None] = mapped_column(Text)

    alerts: Mapped[list[Alert]] = relationship(back_populates="filing")


class Alert(TimestampMixin, Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    filing_id: Mapped[int | None] = mapped_column(ForeignKey("filings.id"))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    headline: Mapped[str | None] = mapped_column(Text)

    filing: Mapped[Filing | None] = relationship(back_populates="alerts")
    delivery_attempts: Mapped[list[DeliveryAttempt]] = relationship(back_populates="alert")


class Destination(TimestampMixin, Base):
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    destination_type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    config_label: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    delivery_attempts: Mapped[list[DeliveryAttempt]] = relationship(back_populates="destination")


class DeliveryAttempt(TimestampMixin, Base):
    __tablename__ = "delivery_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(ForeignKey("alerts.id"))
    destination_id: Mapped[int | None] = mapped_column(ForeignKey("destinations.id"))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    response_code: Mapped[int | None]
    error_message: Mapped[str | None] = mapped_column(Text)

    alert: Mapped[Alert | None] = relationship(back_populates="delivery_attempts")
    destination: Mapped[Destination | None] = relationship(back_populates="delivery_attempts")


class SourceCursor(TimestampMixin, Base):
    __tablename__ = "source_cursors"
    __table_args__ = (
        UniqueConstraint("source_name", "filter_key", name="uq_source_cursors_source_filter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(64), nullable=False)
    filter_key: Mapped[str] = mapped_column(String(128), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    filed_date: Mapped[date | None] = mapped_column(Date)
    accession_number: Mapped[str | None] = mapped_column(String(32))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestRun(TimestampMixin, Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)


class StageError(TimestampMixin, Base):
    __tablename__ = "stage_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(64))
    filing_accession: Mapped[str | None] = mapped_column(String(32))
    error_class: Mapped[str] = mapped_column(String(128), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    is_retryable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
