from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import open_session
from app.main import create_app
from app.models import Alert, DeliveryAttempt, Destination, Filing, StageError, WatchlistEntry
from app.services.scoring.eight_k import EightKScorer
from app.services.sec.client import FixtureSecClient
from app.services.sec.eight_k import EightKParser
from app.services.sec.resolver import COMPANY_TICKERS_URL, TickerResolver
from app.services.sec.submissions import parse_recent_8k_filings, submissions_url
from tests.conftest import extract_csrf_token

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
AAPL_CIK = "0000320193"
AAPL_DETAIL_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000100/0000320193-26-000100-index.html"
)
AAPL_PRIMARY_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000100/aapl-20260325x8k402.htm"
)


def load_text(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


def load_json(*parts: str) -> dict:
    return json.loads(load_text(*parts))


class FakeSlackNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent_payloads: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def send_test_message(self, destination_name: str):
        return self.send_alert(destination_name=destination_name, payload={"headline": "test"})

    def send_alert(self, destination_name: str, payload: dict):
        self.sent_payloads.append({"destination_name": destination_name, "payload": payload})
        if self.fail:
            from app.services.notify.base import NotificationResult

            return NotificationResult(status="failed", detail="Synthetic Slack failure.")
        from app.services.notify.base import NotificationResult

        return NotificationResult(
            status="sent", detail="Synthetic Slack success.", response_code=200
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_HOST="127.0.0.1",
        APP_PORT=8000,
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'phase2.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        SEC_POLL_INTERVAL_SECONDS=60,
        SEC_RATE_LIMIT_RPS=10,
        SCHEDULER_ENABLED=False,
        TESTING=True,
    )


def make_fixture_client() -> FixtureSecClient:
    return FixtureSecClient(
        json_map={
            COMPANY_TICKERS_URL: load_json("company_tickers.json"),
            submissions_url(AAPL_CIK): load_json("eight_k", "aapl_402", "submissions.json"),
        },
        text_map={
            AAPL_DETAIL_URL: load_text("eight_k", "aapl_402", "detail-index.html"),
            AAPL_PRIMARY_URL: load_text("eight_k", "aapl_402", "primary.html"),
        },
    )


def create_phase2_client(tmp_path: Path, *, fail_slack: bool = False):
    settings = make_settings(tmp_path)
    fixture_sec_client = make_fixture_client()
    fake_slack = FakeSlackNotifier(fail=fail_slack)
    app = create_app(
        settings,
        service_overrides={
            "sec_client": fixture_sec_client,
            "slack_notifier": fake_slack,
        },
    )
    return TestClient(app), fake_slack, fixture_sec_client


def seed_watchlist_and_destination() -> None:
    with open_session() as session:
        session.add(
            Destination(
                name="Primary Slack",
                destination_type="slack",
                enabled=True,
                config_label="env:SLACK_WEBHOOK_URL",
            )
        )
        session.add(WatchlistEntry(ticker="AAPL", enabled=True))
        session.commit()


def test_resolver_prefers_manual_override(tmp_path: Path):
    client = make_fixture_client()
    resolver = TickerResolver(tmp_path, client)
    entry = WatchlistEntry(
        ticker="AAPL",
        issuer_cik=None,
        manual_cik_override="789019",
        issuer_name="Manual Name",
        enabled=True,
    )
    resolved = resolver.resolve(entry)
    assert resolved is not None
    assert resolved.issuer_cik == "0000789019"
    assert resolved.resolution_source == "manual_cik_override"


def test_resolver_prefers_stored_cik_over_lookup(tmp_path: Path):
    client = make_fixture_client()
    resolver = TickerResolver(tmp_path, client)
    entry = WatchlistEntry(
        ticker="AAPL",
        issuer_cik="0000789019",
        manual_cik_override=None,
        issuer_name="Stored Name",
        enabled=True,
    )
    resolved = resolver.resolve(entry)
    assert resolved is not None
    assert resolved.issuer_cik == "0000789019"
    assert resolved.resolution_source == "stored_issuer_cik"


def test_submissions_filters_only_8k():
    payload = load_json("eight_k", "aapl_402", "submissions.json")
    filings = parse_recent_8k_filings(payload, issuer_cik=AAPL_CIK)
    assert len(filings) == 1
    assert filings[0].form_type == "8-K"
    assert filings[0].accession_number == "0000320193-26-000100"


def test_parser_and_scorer_cover_negative_and_positive_fixtures():
    parser = EightKParser()
    scorer = EightKScorer()

    negative = parser.parse(
        load_text("eight_k", "aapl_402", "detail-index.html"),
        load_text("eight_k", "aapl_402", "primary.html"),
    )
    negative_score = scorer.score(negative)
    assert "4.02" in negative.item_numbers
    assert "Auditor letter" in negative.exhibit_titles
    assert negative_score.score == -2.0
    assert any("4.02" in reason for reason in negative_score.reasons)

    positive = parser.parse(
        load_text("eight_k", "msft_502", "detail-index.html"),
        load_text("eight_k", "msft_502", "primary.html"),
    )
    positive_score = scorer.score(positive)
    assert "5.02" in positive.item_numbers
    assert "Employment agreement" in positive.exhibit_titles
    assert positive_score.score == 1.0
    assert any("5.02" in reason for reason in positive_score.reasons)


def test_manual_ingest_end_to_end_and_idempotency(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase2_client(tmp_path)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        run_response = client.post(
            "/actions/ingest-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert run_response.status_code == 200
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filings = session.query(Filing).all()
            alerts = session.query(Alert).all()
            attempts = session.query(DeliveryAttempt).all()
            watchlist = session.query(WatchlistEntry).one()

            assert len(filings) == 1
            assert len(alerts) == 1
            assert len(attempts) == 1
            filing = filings[0]
            assert filing.score == -2.0
            assert filing.confidence == "high"
            assert filing.summary_headline is not None
            assert filing.summary_context is not None
            assert filing.normalized_payload["item_numbers"] == ["4.02", "9.01"]
            assert filing.normalized_payload["exhibit_titles"] == ["Auditor letter"]
            assert watchlist.issuer_cik == AAPL_CIK
            assert watchlist.issuer_name == "Apple Inc."

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        repeat_response = client.post(
            "/actions/ingest-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert repeat_response.status_code == 200
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 1
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1

        assert len(fake_slack.sent_payloads) == 1
        assert COMPANY_TICKERS_URL in fixture_sec_client.calls
        assert submissions_url(AAPL_CIK) in fixture_sec_client.calls


def test_reparse_updates_existing_filing_without_new_alert_or_delivery(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase2_client(tmp_path)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/ingest-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        fixture_sec_client.text_map[AAPL_PRIMARY_URL] = (
            load_text("eight_k", "aapl_402", "primary.html")
            + "\n<p>The company also announced guidance raised for the current fiscal year.</p>"
        )

        with open_session() as session:
            filing = session.query(Filing).one()
            filing_id = filing.id

        assert filing_id is not None

        detail_response = client.get(f"/filings/{filing_id}")
        detail_csrf = extract_csrf_token(detail_response.text)
        reparse_response = client.post(
            f"/filings/{filing_id}/reparse",
            data={"csrf_token": detail_csrf},
            follow_redirects=True,
        )
        assert reparse_response.status_code == 200
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.get(Filing, filing_id)
            assert filing is not None
            assert any("guidance raised" in reason.lower() for reason in filing.reasons or [])
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1

        assert len(fake_slack.sent_payloads) == 1


def test_slack_failure_is_isolated_from_ingest(tmp_path: Path):
    client, fake_slack, _fixture_sec_client = create_phase2_client(tmp_path, fail_slack=True)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/ingest-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.query(Filing).one()
            alert = session.query(Alert).one()
            attempts = session.query(DeliveryAttempt).all()
            errors = session.query(StageError).all()

            assert filing.summary_headline is not None
            assert alert.status == "delivery_failed"
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert any(error.stage == "slack_delivery" for error in errors)

        assert len(fake_slack.sent_payloads) == 1
