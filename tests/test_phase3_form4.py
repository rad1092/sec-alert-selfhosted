from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import open_session
from app.main import create_app
from app.models import Alert, DeliveryAttempt, Destination, Filing, StageError, WatchlistEntry
from app.services.scoring.form4 import Form4Scorer
from app.services.sec.client import FixtureSecClient
from app.services.sec.form4 import (
    DetailDocument,
    Form4DetailMetadata,
    Form4Parser,
    _parse_documents,
    locate_ownership_xml,
)
from app.services.sec.latest_ownership import LATEST_OWNERSHIP_URL, parse_ownership_candidates
from app.services.sec.resolver import COMPANY_TICKERS_URL
from tests.conftest import extract_csrf_token

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
FORM4_FIXTURES = FIXTURES / "form4"

BUY_DETAIL_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000200/0000320193-26-000200-index.html"
)
BUY_XML_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000200/form4-multi-buy.xml"
)
LIVE_XSL_DETAIL_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000204/0000320193-26-000204-index.html"
)
LIVE_XSL_XML_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000204/xslF345X05/wk-form4_1772053959.xml"
)
MISSING_XML_DETAIL_URL = (
    "https://www.sec.gov/Archives/edgar/data/320193/"
    "000032019326000205/0000320193-26-000205-index.html"
)


def load_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def load_json(*parts: str) -> dict:
    return json.loads(load_text(*parts))


class FakeSlackNotifier:
    channel = "slack"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent_payloads: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def send_test_message(self, destination):
        return self.send_alert(destination=destination, payload={"headline": "test"})

    def send_alert(self, destination, payload: dict):
        destination_name = getattr(destination, "name", destination)
        self.sent_payloads.append({"destination_name": destination_name, "payload": payload})
        if self.fail:
            from app.services.notify.base import NotificationResult

            return NotificationResult(status="failed", detail="Synthetic Slack failure.")
        from app.services.notify.base import NotificationResult

        return NotificationResult(
            status="sent",
            detail="Synthetic Slack success.",
            response_code=200,
        )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_HOST="127.0.0.1",
        APP_PORT=8000,
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'phase3.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        SEC_POLL_INTERVAL_SECONDS=60,
        SEC_RATE_LIMIT_RPS=10,
        OPENAI_API_KEY=None,
        OPENAI_MODEL=None,
        SCHEDULER_ENABLED=False,
        TESTING=True,
    )


def make_fixture_client(feed_case: str = "buy_multi_reporter") -> FixtureSecClient:
    text_map = {
        LATEST_OWNERSHIP_URL: load_text("form4", feed_case, "ownership-feed.xml"),
    }
    for case in (
        "buy_multi_reporter",
        "sale_simple",
        "mixed_m_f",
        "derivative_heavy",
        "tenb5_1_case",
        "live_xsl_case",
    ):
        case_dir = FORM4_FIXTURES / case
        detail_url = _detail_url_for_case(case)
        xml_url = _xml_url_for_case(case)
        text_map[detail_url] = (case_dir / "detail-index.html").read_text(encoding="utf-8")
        text_map[xml_url] = (case_dir / "ownership.xml").read_text(encoding="utf-8")

    return FixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map=text_map,
    )


def create_phase3_client(
    tmp_path: Path,
    *,
    fail_slack: bool = False,
    feed_case: str = "buy_multi_reporter",
    summary_rewriter=None,
):
    settings = make_settings(tmp_path)
    fixture_sec_client = make_fixture_client(feed_case=feed_case)
    fake_slack = FakeSlackNotifier(fail=fail_slack)
    service_overrides = {
        "sec_client": fixture_sec_client,
        "slack_notifier": fake_slack,
    }
    if summary_rewriter is not None:
        service_overrides["summary_rewriter"] = summary_rewriter
    app = create_app(
        settings,
        service_overrides=service_overrides,
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


def test_ownership_feed_parser_recognizes_form4_candidates():
    candidates = parse_ownership_candidates(
        load_text("form4", "buy_multi_reporter", "ownership-feed.xml")
    )
    assert [candidate.form_type for candidate in candidates] == ["4", "5", "3"]
    assert candidates[0].accession_number == "0000320193-26-000200"


def test_parse_documents_handles_live_and_legacy_detail_table_orders():
    live_documents = _parse_documents(
        BeautifulSoup(load_text("form4", "live_xsl_case", "detail-index.html"), "html.parser"),
        LIVE_XSL_DETAIL_URL,
    )
    assert len(live_documents) == 1
    assert live_documents[0].filename == "wk-form4_1772053959.html"
    assert live_documents[0].description == "FORM 4"
    assert live_documents[0].document_type == "4"
    assert live_documents[0].url == LIVE_XSL_XML_URL

    legacy_documents = _parse_documents(
        BeautifulSoup(load_text("form4", "buy_multi_reporter", "detail-index.html"), "html.parser"),
        BUY_DETAIL_URL,
    )
    assert len(legacy_documents) == 1
    assert legacy_documents[0].filename == "form4-multi-buy.xml"
    assert legacy_documents[0].description == "OWNERSHIP DOCUMENT"
    assert legacy_documents[0].document_type == "4"


def test_locate_ownership_xml_handles_live_href_direct_xml_and_ex99_fallback():
    live_metadata = Form4DetailMetadata(
        detail_url=LIVE_XSL_DETAIL_URL,
        accession_number="0000320193-26-000204",
        form_type="4",
        filed_date=None,
        accepted_at=None,
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        issuer_ticker="AAPL",
        documents=_parse_documents(
            BeautifulSoup(load_text("form4", "live_xsl_case", "detail-index.html"), "html.parser"),
            LIVE_XSL_DETAIL_URL,
        ),
    )
    assert locate_ownership_xml(live_metadata) == LIVE_XSL_XML_URL

    direct_xml_metadata = Form4DetailMetadata(
        detail_url=BUY_DETAIL_URL,
        accession_number="0000320193-26-000200",
        form_type="4",
        filed_date=None,
        accepted_at=None,
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        issuer_ticker="AAPL",
        documents=_parse_documents(
            BeautifulSoup(
                load_text("form4", "buy_multi_reporter", "detail-index.html"),
                "html.parser",
            ),
            BUY_DETAIL_URL,
        ),
    )
    assert locate_ownership_xml(direct_xml_metadata) == BUY_XML_URL

    prefer_raw_xml_metadata = Form4DetailMetadata(
        detail_url=LIVE_XSL_DETAIL_URL,
        accession_number="0001628280-26-011664",
        form_type="4",
        filed_date=None,
        accepted_at=None,
        issuer_cik="0001005229",
        issuer_name="Acme United Corp.",
        issuer_ticker="ACU",
        documents=[
            DetailDocument(
                url=(
                    "https://www.sec.gov/Archives/edgar/data/1005229/"
                    "000162828026011664/xslF345X05/wk-form4_1772053936.xml"
                ),
                filename="wk-form4_1772053936.html",
                description="FORM 4",
                document_type="4",
            ),
            DetailDocument(
                url=(
                    "https://www.sec.gov/Archives/edgar/data/1005229/"
                    "000162828026011664/wk-form4_1772053936.xml"
                ),
                filename="wk-form4_1772053936.xml",
                description="FORM 4",
                document_type="4",
            ),
        ],
    )
    assert (
        locate_ownership_xml(prefer_raw_xml_metadata)
        == "https://www.sec.gov/Archives/edgar/data/1005229/"
        "000162828026011664/wk-form4_1772053936.xml"
    )

    ex99_metadata = Form4DetailMetadata(
        detail_url=_detail_url_for_case("derivative_heavy"),
        accession_number="0000789019-26-000210",
        form_type="4",
        filed_date=None,
        accepted_at=None,
        issuer_cik="0000789019",
        issuer_name="Microsoft Corporation",
        issuer_ticker="MSFT",
        documents=_parse_documents(
            BeautifulSoup(
                load_text("form4", "derivative_heavy", "detail-index.html"),
                "html.parser",
            ),
            _detail_url_for_case("derivative_heavy"),
        ),
    )
    assert locate_ownership_xml(ex99_metadata) == _xml_url_for_case("derivative_heavy")

    amended_metadata = Form4DetailMetadata(
        detail_url="https://www.sec.gov/Archives/edgar/data/320193/test-amendment-index.html",
        accession_number="0000320193-26-000299",
        form_type="4/A",
        filed_date=None,
        accepted_at=None,
        issuer_cik="0000320193",
        issuer_name="Apple Inc.",
        issuer_ticker="AAPL",
        documents=[
            DetailDocument(
                url="https://www.sec.gov/Archives/edgar/data/320193/test-amendment.xml",
                filename="test-amendment.xml",
                description="FORM 4/A",
                document_type="4/A",
            )
        ],
    )
    assert (
        locate_ownership_xml(amended_metadata)
        == "https://www.sec.gov/Archives/edgar/data/320193/test-amendment.xml"
    )


def test_form4_parser_and_scorer_cover_multi_reporter_sale_and_neutral_cases():
    parser = Form4Parser()
    scorer = Form4Scorer()

    buy = parser.parse(
        load_text("form4", "buy_multi_reporter", "detail-index.html"),
        load_text("form4", "buy_multi_reporter", "ownership.xml"),
    )
    buy_score = scorer.score(buy)
    assert buy.issuer_cik == "0000320193"
    assert buy.normalized_payload["owner_count"] == 2
    assert buy.normalized_payload["multi_reporting_owner"] is True
    assert buy.normalized_payload["reporting_owners"][0]["name"] == "Alex Buyer"
    assert buy.normalized_payload["reporting_owners"][1]["name"] == "Jordan Buyer"
    assert len(buy.normalized_payload["non_derivative_transactions"]) == 1
    assert buy_score.score == 1.0
    assert any("Multiple reporting owners aligned" in reason for reason in buy_score.reasons)

    sale = parser.parse(
        load_text("form4", "sale_simple", "detail-index.html"),
        load_text("form4", "sale_simple", "ownership.xml"),
    )
    sale_score = scorer.score(sale)
    assert sale_score.score == -1.0
    assert sale_score.confidence == "high"

    mixed = parser.parse(
        load_text("form4", "mixed_m_f", "detail-index.html"),
        load_text("form4", "mixed_m_f", "ownership.xml"),
    )
    mixed_score = scorer.score(mixed)
    assert mixed_score.score == 0.0
    assert mixed_score.confidence == "low"


def test_form4_parser_handles_derivative_heavy_and_tenb5_one_cases():
    parser = Form4Parser()
    scorer = Form4Scorer()

    derivative = parser.parse(
        load_text("form4", "derivative_heavy", "detail-index.html"),
        load_text("form4", "derivative_heavy", "ownership.xml"),
    )
    assert derivative.normalized_payload["issuer"]["foreign_trading_symbol"] == "MSFTY"
    assert derivative.normalized_payload["reporting_owners"][0]["non_us_address_flag"] is True
    assert derivative.normalized_payload["derivative_transactions"][0]["unknown_elements"] == [
        "customFutureField"
    ]
    assert len(derivative.normalized_payload["derivative_holdings"]) == 1

    tenb5_one = parser.parse(
        load_text("form4", "tenb5_1_case", "detail-index.html"),
        load_text("form4", "tenb5_1_case", "ownership.xml"),
    )
    tenb5_score = scorer.score(tenb5_one)
    assert tenb5_one.normalized_payload["tenb5_1"]["checkbox"] is True
    assert tenb5_one.normalized_payload["tenb5_1"]["mentioned_in_remarks"] is True
    assert tenb5_one.normalized_payload["tenb5_1"]["supporting_footnote_ids"] == ["F1"]
    assert tenb5_one.normalized_payload["tenb5_1"]["adoption_date"] == "2026-01-15"
    assert tenb5_score.score == -1.0
    assert any("10b5-1" in reason for reason in tenb5_score.reasons)


def test_form4_manual_ingest_end_to_end_and_idempotency(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase3_client(tmp_path)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        run_response = client.post(
            "/actions/ingest-form4-now",
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
            assert filing.form_type == "4"
            assert filing.issuer_cik == "0000320193"
            assert filing.reporter_names == ["Alex Buyer", "Jordan Buyer"]
            assert filing.normalized_payload["owner_count"] == 2
            assert filing.normalized_payload["multi_reporting_owner"] is True
            assert len(filing.normalized_payload["non_derivative_transactions"]) == 1
            assert filing.summary_headline is not None
            assert filing.summary_context is not None
            assert watchlist.issuer_cik == "0000320193"

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        repeat_response = client.post(
            "/actions/ingest-form4-now",
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
        assert LATEST_OWNERSHIP_URL in fixture_sec_client.calls
        assert BUY_DETAIL_URL in fixture_sec_client.calls
        assert BUY_XML_URL in fixture_sec_client.calls


def test_form4_manual_ingest_succeeds_with_live_style_detail_fixture(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase3_client(
        tmp_path,
        feed_case="live_xsl_case",
    )
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        run_response = client.post(
            "/actions/ingest-form4-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert run_response.status_code == 200
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.query(Filing).one()
            assert filing.detail_url == LIVE_XSL_DETAIL_URL
            assert filing.source_url == LIVE_XSL_XML_URL
            assert filing.parser_status == "success"
            assert filing.summary_headline is not None
            assert filing.summary_context is not None

        assert len(fake_slack.sent_payloads) == 1
        assert LIVE_XSL_DETAIL_URL in fixture_sec_client.calls
        assert LIVE_XSL_XML_URL in fixture_sec_client.calls


def test_form4_reparse_updates_existing_filing_without_new_alert_or_delivery(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase3_client(tmp_path)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post(
            "/actions/ingest-form4-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        fixture_sec_client.text_map[BUY_XML_URL] = load_text(
            "form4", "buy_multi_reporter", "ownership.xml"
        ).replace(
            "Joint filing by more than one reporting person.",
            "Joint filing by more than one reporting person. Rule 10b5-1 noted on 2026-02-01.",
        )

        with open_session() as session:
            filing = session.query(Filing).one()
            filing_id = filing.id

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
            assert filing.normalized_payload["tenb5_1"]["mentioned_in_remarks"] is True
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1

        assert len(fake_slack.sent_payloads) == 1


def test_form4_reparse_succeeds_with_live_style_detail_fixture(tmp_path: Path):
    client, fake_slack, fixture_sec_client = create_phase3_client(
        tmp_path,
        feed_case="live_xsl_case",
    )
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post(
            "/actions/ingest-form4-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.query(Filing).one()
            filing_id = filing.id

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
            assert filing.source_url == LIVE_XSL_XML_URL
            assert filing.parser_status == "success"
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1

        assert len(fake_slack.sent_payloads) == 1
        assert fixture_sec_client.calls.count(LIVE_XSL_XML_URL) >= 2


def test_form4_reparse_missing_xml_marks_failure_cleanly(tmp_path: Path):
    settings = make_settings(tmp_path)
    fixture_sec_client = FixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map={
            MISSING_XML_DETAIL_URL: load_text("form4", "missing_xml_live", "detail-index.html"),
        },
    )
    fake_slack = FakeSlackNotifier()
    app = create_app(
        settings,
        service_overrides={
            "sec_client": fixture_sec_client,
            "slack_notifier": fake_slack,
        },
    )
    client = TestClient(app)
    with client:
        with open_session() as session:
            filing = Filing(
                accession_number="0000320193-26-000205",
                form_type="4",
                detail_url=MISSING_XML_DETAIL_URL,
                source_url=None,
                filed_date=date(2026, 3, 26),
                accepted_at=datetime(2026, 3, 26, 9, 31, tzinfo=UTC),
                issuer_cik="0000320193",
                issuer_name="Apple Inc.",
                issuer_ticker="AAPL",
                parser_status="success",
                scoring_status="success",
                summarization_status="success",
                summary_headline="Existing headline",
                summary_context="Existing context",
            )
            session.add(filing)
            session.commit()
            filing_id = filing.id

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
            error = session.query(StageError).order_by(StageError.id.desc()).first()
            assert filing is not None
            assert filing.parser_status == "failed"
            assert filing.scoring_status == "skipped"
            assert filing.summarization_status == "skipped"
            assert error is not None
            assert error.stage == "reparse"
            assert error.error_class == "MissingXmlError"
            assert "Unable to locate ownership XML during reparse." in error.message

        assert len(fake_slack.sent_payloads) == 0


def test_form4_slack_failure_is_isolated_from_ingest(tmp_path: Path):
    client, fake_slack, _fixture_sec_client = create_phase3_client(tmp_path, fail_slack=True)
    with client:
        seed_watchlist_and_destination()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post(
            "/actions/ingest-form4-now",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.query(Filing).one()
            alert = session.query(Alert).one()
            attempts = session.query(DeliveryAttempt).all()
            assert filing.summary_headline is not None
            assert alert.status == "delivery_failed"
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].retryable is False
            assert attempts[0].error_class is None or attempts[0].error_class == "HttpError"

        assert len(fake_slack.sent_payloads) == 1


def _detail_url_for_case(case: str) -> str:
    if case == "buy_multi_reporter":
        return BUY_DETAIL_URL
    if case == "sale_simple":
        return (
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019326000201/0000320193-26-000201-index.html"
        )
    if case == "mixed_m_f":
        return (
            "https://www.sec.gov/Archives/edgar/data/320193/"
            "000032019326000202/0000320193-26-000202-index.html"
        )
    if case == "derivative_heavy":
        return (
            "https://www.sec.gov/Archives/edgar/data/789019/"
            "000078901926000210/0000789019-26-000210-index.html"
        )
    if case == "live_xsl_case":
        return LIVE_XSL_DETAIL_URL
    return (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000203/0000320193-26-000203-index.html"
    )


def _xml_url_for_case(case: str) -> str:
    if case == "buy_multi_reporter":
        return BUY_XML_URL
    if case == "sale_simple":
        return (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000201/aapl-form4-sale.xml"
        )
    if case == "mixed_m_f":
        return (
            "https://www.sec.gov/Archives/edgar/data/320193/000032019326000202/aapl-form4-mixed.xml"
        )
    if case == "derivative_heavy":
        return (
            "https://www.sec.gov/Archives/edgar/data/789019/"
            "000078901926000210/msft-ownership-data.xml"
        )
    if case == "live_xsl_case":
        return LIVE_XSL_XML_URL
    return "https://www.sec.gov/Archives/edgar/data/320193/000032019326000203/aapl-form4-10b5.xml"
