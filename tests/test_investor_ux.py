from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.db import open_session
from app.models import Alert, Filing, IngestRun, StageError, WatchlistEntry


def seed_watchlist_entry(*, ticker: str = "AAPL", enabled: bool = True) -> None:
    with open_session() as session:
        session.add(
            WatchlistEntry(
                ticker=ticker,
                issuer_cik="0000320193",
                issuer_name="Apple Inc.",
                enabled=enabled,
            )
        )
        session.commit()


def seed_signal(
    *,
    accession: str,
    filed_date: date,
    headline: str,
    score: float = -2.0,
    confidence: str = "high",
    ticker: str = "AAPL",
) -> None:
    with open_session() as session:
        filing = Filing(
            accession_number=accession,
            form_type="8-K",
            is_amendment=False,
            filed_date=filed_date,
            accepted_at=datetime.now(UTC),
            issuer_cik="0000320193",
            issuer_ticker=ticker,
            issuer_name="Apple Inc.",
            parser_status="success",
            scoring_status="success",
            summarization_status="success",
            normalized_payload={
                "item_numbers": ["4.02"],
                "exhibit_titles": ["Auditor letter"],
                "cleaned_body": "Sample filing body for investor UX coverage.",
            },
            score=score,
            confidence=confidence,
            reasons=["8-K Item 4.02 detected"],
            summary_headline=headline,
            summary_context="Deterministic context for the investor-facing detail page.",
            detail_url="https://example.test/detail",
            source_url="https://example.test/source",
        )
        session.add(filing)
        session.flush()
        session.add(Alert(filing_id=filing.id, status="skipped", headline=headline))
        session.commit()


def seed_form4_success(*, accession: str, updated_at: datetime) -> None:
    with open_session() as session:
        session.add(
            Filing(
                accession_number=accession,
                form_type="4",
                is_amendment=False,
                filed_date=updated_at.date(),
                accepted_at=updated_at,
                issuer_cik="0000320193",
                issuer_ticker="AAPL",
                issuer_name="Apple Inc.",
                parser_status="success",
                scoring_status="success",
                summarization_status="success",
                reporter_names=["Alex Buyer"],
                normalized_payload={"owner_count": 1},
                score=1.0,
                confidence="medium",
                reasons=["Form 4 purchase"],
                summary_headline="Recovered Form 4 filing",
                summary_context="Recovered parsing after a prior issue.",
                detail_url="https://example.test/form4-detail",
                source_url="https://example.test/form4.xml",
                updated_at=updated_at,
            )
        )
        session.commit()


def test_inbox_zero_states_and_local_notification_warning(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Start with one ticker" in response.text

    seed_watchlist_entry()
    response = client.get("/")
    assert "Your watchlist is ready" in response.text
    assert (
        "Signals are being created locally, but no external notification channel is "
        "configured."
    ) in response.text

    with open_session() as session:
        session.add(
            IngestRun(
                run_key="repair:recent:test",
                triggered_by="repair",
                status="running",
            )
        )
        session.add(
            StageError(
                stage="manual_ingest",
                source_name="submissions",
                filing_accession="0000320193-26-000100",
                error_class="ParseError",
                message="Synthetic dashboard issue.",
                is_retryable=False,
            )
        )
        session.commit()

    response = client.get("/")
    assert "Background work is underway" in response.text
    assert "What is blocking right now" in response.text


def test_alerts_hide_historical_by_default_and_show_with_filter(client):
    recent_day = datetime.now(UTC).date()
    old_day = recent_day - timedelta(days=30)
    seed_signal(
        accession="0000320193-26-000100",
        filed_date=recent_day,
        headline="Recent Apple filing",
        score=-2.0,
    )
    seed_signal(
        accession="0000320193-26-000101",
        filed_date=old_day,
        headline="Historical Apple filing",
        score=0.0,
    )

    response = client.get("/alerts")
    assert response.status_code == 200
    assert "Recent Apple filing" in response.text
    assert "Historical Apple filing" not in response.text
    assert "Hide older than 7 days" in response.text

    historical_response = client.get("/alerts?historical=show")
    assert historical_response.status_code == 200
    assert "Historical Apple filing" in historical_response.text
    assert "Historical" in historical_response.text


def test_filing_detail_prioritizes_signal_and_advanced_sections(client):
    seed_signal(
        accession="0000320193-26-000100",
        filed_date=datetime.now(UTC).date(),
        headline="Apple accounting signal",
    )

    response = client.get("/filings/1")
    assert response.status_code == 200
    assert "Why this was flagged" in response.text
    assert "Verify against the SEC" in response.text
    assert "OpenAI is presentation-only here." in response.text
    assert "Advanced details" in response.text
    assert "Parsed Payload" in response.text


def test_watchlist_page_uses_ticker_first_form_and_advanced_issuer_settings(client):
    response = client.get("/watchlist")
    assert response.status_code == 200
    assert "Add by ticker" in response.text
    assert "Advanced issuer settings" in response.text
    assert "30-day historical catch-up" in response.text


def test_navigation_restructures_to_inbox_notifications_and_advanced(client):
    seed_watchlist_entry()
    response = client.get("/")
    assert ">Inbox<" in response.text
    assert ">Watchlist<" in response.text
    assert ">Notifications<" in response.text
    assert ">Advanced<" in response.text
    assert ">Destinations<" not in response.text

    advanced_response = client.get("/advanced")
    assert advanced_response.status_code == 200
    assert "Queue health" in advanced_response.text
    assert "Raw ingest runs" in advanced_response.text
    assert "Doctor summary" in advanced_response.text

    errors_response = client.get("/errors")
    assert errors_response.status_code == 200
    assert "Recorded pipeline issues" in errors_response.text

    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "Technical Reference" in settings_response.text
    assert "Doctor summary" in settings_response.text


def test_issue_archive_uses_current_recovered_historical_states(client):
    seed_watchlist_entry()
    now = datetime.now(UTC)
    recovered_error_time = now - timedelta(hours=6)
    current_error_time = now - timedelta(hours=2)
    historical_error_time = now - timedelta(days=3)

    seed_form4_success(
        accession="0000320193-26-000210",
        updated_at=recovered_error_time + timedelta(minutes=20),
    )

    with open_session() as session:
        session.add_all(
            [
                StageError(
                    stage="form4_accession",
                    source_name="latest_ownership",
                    filing_accession="0000320193-26-000210",
                    error_class="OwnershipXmlParseError",
                    message="Recovered parse failure.",
                    is_retryable=False,
                    created_at=recovered_error_time,
                    updated_at=recovered_error_time,
                ),
                StageError(
                    stage="form4_accession",
                    source_name="latest_ownership",
                    filing_accession="0000320193-26-000211",
                    error_class="SecTransientResponseError",
                    message="Current SEC retryable response.",
                    is_retryable=True,
                    created_at=current_error_time,
                    updated_at=current_error_time,
                ),
                StageError(
                    stage="form4_xml_locator",
                    source_name="form4_detail",
                    filing_accession="0000320193-26-000212",
                    error_class="MissingXmlError",
                    message="Historical XML locator failure.",
                    is_retryable=False,
                    created_at=historical_error_time,
                    updated_at=historical_error_time,
                ),
            ]
        )
        session.commit()

    inbox_response = client.get("/")
    assert inbox_response.status_code == 200
    assert "Form 4 Health" in inbox_response.text
    assert "Needs review" in inbox_response.text
    assert "0000320193-26-000211" in inbox_response.text
    assert "0000320193-26-000210" not in inbox_response.text
    assert "0000320193-26-000212" not in inbox_response.text
    assert "No current blocking issues" not in inbox_response.text

    errors_response = client.get("/errors")
    assert errors_response.status_code == 200
    assert "0000320193-26-000210" in errors_response.text
    assert "0000320193-26-000211" in errors_response.text
    assert "0000320193-26-000212" in errors_response.text
    assert "Current" in errors_response.text
    assert "Recovered" in errors_response.text
    assert "Historical" in errors_response.text

    advanced_response = client.get("/advanced")
    assert advanced_response.status_code == 200
    assert "Trust summary" in advanced_response.text
    assert "Last successful parse" in advanced_response.text
