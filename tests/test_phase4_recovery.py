from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import open_session
from app.main import create_app
from app.models import (
    Alert,
    DeliveryAttempt,
    Destination,
    Filing,
    IngestRun,
    StageError,
    WatchlistEntry,
)
from app.services.broker import BrokerPriority, SecRequestBroker
from app.services.notify.base import NotificationResult
from app.services.scheduler import SchedulerService
from app.services.sec.client import ScriptedFixtureSecClient
from app.services.sec.indexes import daily_master_index_url, parse_master_index
from app.services.sec.latest_ownership import LATEST_OWNERSHIP_URL
from app.services.sec.resolver import COMPANY_TICKERS_URL
from app.services.sec.submissions import submissions_url
from app.services.worker import BrokerWorker
from tests.conftest import extract_csrf_token

FIXTURES = Path(__file__).parent / "fixtures" / "sec"
AAPL_CIK = "0000320193"


def load_text(*parts: str) -> str:
    return FIXTURES.joinpath(*parts).read_text(encoding="utf-8")


def load_json(*parts: str) -> dict:
    return json.loads(load_text(*parts))


class FakeSlackNotifier:
    channel = "slack"

    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def send_test_message(self, destination):
        return self.send_alert(destination=destination, payload={"headline": "test"})

    def send_alert(self, destination, payload: dict):
        destination_name = getattr(destination, "name", destination)
        self.sent_payloads.append({"destination_name": destination_name, "payload": payload})
        return NotificationResult(
            status="sent",
            detail="Synthetic Slack success.",
            response_code=200,
        )


def make_settings(tmp_path: Path, *, overlap_rows: int = 20) -> Settings:
    return Settings(
        APP_HOST="127.0.0.1",
        APP_PORT=8000,
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'phase4.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        SEC_POLL_INTERVAL_SECONDS=60,
        SEC_RATE_LIMIT_RPS=10,
        SEC_LIVE_8K_OVERLAP_ROWS=overlap_rows,
        OPENAI_API_KEY=None,
        OPENAI_MODEL=None,
        SCHEDULER_ENABLED=False,
        TESTING=True,
    )


def detail_url(cik: str, accession: str) -> str:
    archive_cik = str(int(cik))
    accession_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/"
        f"{accession_nodash}/{accession}-index.html"
    )


def primary_url(cik: str, accession: str, filename: str) -> str:
    archive_cik = str(int(cik))
    accession_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{archive_cik}/{accession_nodash}/{filename}"


def build_submissions_payload(rows: list[dict]) -> dict:
    return {
        "name": "Apple Inc.",
        "tickers": ["AAPL"],
        "filings": {
            "recent": {
                "form": [row["form_type"] for row in rows],
                "accessionNumber": [row["accession_number"] for row in rows],
                "filingDate": [row["filed_date"] for row in rows],
                "acceptanceDateTime": [row["accepted_at"] for row in rows],
                "primaryDocument": [row["primary_document"] for row in rows],
                "items": [row.get("items", "") for row in rows],
            }
        },
    }


def ownership_feed(entries: list[dict[str, str]]) -> str:
    entry_xml = []
    for entry in entries:
        entry_xml.append(
            f"""
            <entry>
              <title>{entry['form_type']} filing</title>
              <category term="{entry['form_type']}" />
              <link href="{entry['detail_url']}" />
              <accession-number>{entry['accession_number']}</accession-number>
              <filing-date>{entry['filed_date']}</filing-date>
              <acceptance-datetime>{entry['accepted_at']}</acceptance-datetime>
            </entry>
            """.strip()
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entry_xml)
        + "</feed>"
    )


def master_index(*rows: str) -> str:
    return "\n".join(
        [
            "Description|Header|Ignored|Ignored|Ignored",
            *rows,
        ]
    )


def make_phase4_client(
    tmp_path: Path,
    *,
    sec_client: ScriptedFixtureSecClient,
    overlap_rows: int = 20,
):
    settings = make_settings(tmp_path, overlap_rows=overlap_rows)
    fake_slack = FakeSlackNotifier()
    app = create_app(
        settings,
        service_overrides={
            "sec_client": sec_client,
            "slack_notifier": fake_slack,
        },
    )
    return TestClient(app), fake_slack


def seed_watchlist_and_destination(*, ticker: str = "AAPL", enabled: bool = True) -> None:
    with open_session() as session:
        session.add(
            Destination(
                name="Primary Slack",
                destination_type="slack",
                enabled=True,
                config_label="env:SLACK_WEBHOOK_URL",
            )
        )
        session.add(
            WatchlistEntry(
                ticker=ticker,
                issuer_cik=AAPL_CIK,
                issuer_name="Apple Inc.",
                enabled=enabled,
            )
        )
        session.commit()


def test_scheduler_enqueue_methods_coalesce(tmp_path: Path):
    settings = make_settings(tmp_path)
    broker = SecRequestBroker(rate_limit_rps=2)
    scheduler = SchedulerService(settings, broker)

    scheduler.enqueue_live_8k()
    scheduler.enqueue_live_8k()
    scheduler.enqueue_live_form4()
    scheduler.enqueue_live_form4()
    scheduler.enqueue_repair_recent()
    scheduler.enqueue_repair_recent()

    snapshot = broker.snapshot()
    assert snapshot["backlog_size"] == 3
    assert snapshot["queued_by_priority"]["P2"] == 2
    assert snapshot["queued_by_priority"]["P3"] == 1


def test_live_8k_overlap_recheck_is_idempotent(tmp_path: Path):
    accessions = [
        "0000320193-26-000105",
        "0000320193-26-000104",
        "0000320193-26-000103",
        "0000320193-26-000102",
        "0000320193-26-000101",
        "0000320193-26-000106",
    ]
    rows_run1 = [
        {
            "accession_number": accessions[0],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100500",
            "primary_document": "aapl-live-105.htm",
            "items": "4.02,9.01",
        },
        {
            "accession_number": accessions[1],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100400",
            "primary_document": "aapl-live-104.htm",
            "items": "4.02,9.01",
        },
        {
            "accession_number": accessions[2],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100300",
            "primary_document": "aapl-live-103.htm",
            "items": "4.02,9.01",
        },
        {
            "accession_number": accessions[3],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100200",
            "primary_document": "aapl-live-102.htm",
            "items": "4.02,9.01",
        },
        {
            "accession_number": accessions[4],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100100",
            "primary_document": "aapl-live-101.htm",
            "items": "4.02,9.01",
        },
    ]
    rows_run2 = [
        {
            "accession_number": accessions[5],
            "form_type": "8-K",
            "filed_date": "2026-03-25",
            "accepted_at": "20260325100600",
            "primary_document": "aapl-live-106.htm",
            "items": "4.02,9.01",
        },
        *rows_run1[:4],
    ]
    text_map = {}
    for row in rows_run1 + [rows_run2[0]]:
        url = detail_url(AAPL_CIK, row["accession_number"])
        primary = primary_url(AAPL_CIK, row["accession_number"], row["primary_document"])
        text_map[url] = load_text("eight_k", "aapl_402", "detail-index.html")
        text_map[primary] = load_text("eight_k", "aapl_402", "primary.html")

    sec_client = ScriptedFixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map=text_map,
        json_sequences={
            submissions_url(AAPL_CIK): [
                build_submissions_payload(rows_run1),
                build_submissions_payload(rows_run2),
            ]
        },
    )
    client, fake_slack = make_phase4_client(tmp_path, sec_client=sec_client, overlap_rows=5)
    with client:
        seed_watchlist_and_destination()

        client.app.state.scheduler.enqueue_live_8k()
        assert client.app.state.worker.wait_for_idle(timeout=5.0)
        client.app.state.scheduler.enqueue_live_8k()
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 6
            assert session.query(Alert).count() == 6
            assert session.query(DeliveryAttempt).count() == 6

        assert len(fake_slack.sent_payloads) == 6


def test_live_form4_overlap_recheck_is_idempotent(tmp_path: Path):
    buy_detail = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000200/0000320193-26-000200-index.html"
    )
    sale_detail = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000201/0000320193-26-000201-index.html"
    )
    buy_xml = (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000200/form4-multi-buy.xml"
    )
    sale_xml = (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000201/aapl-form4-sale.xml"
    )
    sec_client = ScriptedFixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map={
            buy_detail: load_text("form4", "buy_multi_reporter", "detail-index.html"),
            buy_xml: load_text("form4", "buy_multi_reporter", "ownership.xml"),
            sale_detail: load_text("form4", "sale_simple", "detail-index.html"),
            sale_xml: load_text("form4", "sale_simple", "ownership.xml"),
        },
        text_sequences={
            LATEST_OWNERSHIP_URL: [
                ownership_feed(
                    [
                        {
                            "form_type": "4",
                            "detail_url": buy_detail,
                            "accession_number": "0000320193-26-000200",
                            "filed_date": "2026-03-26",
                            "accepted_at": "2026-03-26T09:31:00Z",
                        }
                    ]
                ),
                ownership_feed(
                    [
                        {
                            "form_type": "4",
                            "detail_url": sale_detail,
                            "accession_number": "0000320193-26-000201",
                            "filed_date": "2026-03-26",
                            "accepted_at": "2026-03-26T09:45:00Z",
                        },
                        {
                            "form_type": "4",
                            "detail_url": buy_detail,
                            "accession_number": "0000320193-26-000200",
                            "filed_date": "2026-03-26",
                            "accepted_at": "2026-03-26T09:31:00Z",
                        },
                    ]
                ),
            ]
        },
    )
    client, fake_slack = make_phase4_client(tmp_path, sec_client=sec_client)
    with client:
        seed_watchlist_and_destination()

        client.app.state.scheduler.enqueue_live_form4()
        assert client.app.state.worker.wait_for_idle(timeout=5.0)
        client.app.state.scheduler.enqueue_live_form4()
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 2
            assert session.query(Alert).count() == 2
            assert session.query(DeliveryAttempt).count() == 2

        assert len(fake_slack.sent_payloads) == 2


def test_master_index_row_derives_accession_from_archive_path():
    text = master_index(
        "0000320193|Apple Inc.|8-K|2026-03-25|"
        "edgar/data/320193/000032019326000100/aapl-20260325x8k402.htm"
    )
    rows = parse_master_index(text)
    assert len(rows) == 1
    assert rows[0].accession_number == "0000320193-26-000100"
    assert rows[0].detail_index_url == detail_url(AAPL_CIK, "0000320193-26-000100")


def test_repair_recovers_missed_recent_8k_and_form4(tmp_path: Path, monkeypatch):
    repair_days = [date(2026, 3, 25), date(2026, 3, 24)]
    monkeypatch.setattr(
        "app.services.ingest.previous_business_days",
        lambda count: repair_days[:count],
    )

    form4_detail = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000204/0000320193-26-000204-index.html"
    )
    form4_xml = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000204/xslF345X05/wk-form4_1772053959.xml"
    )
    sec_client = ScriptedFixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map={
            daily_master_index_url(repair_days[0]): master_index(
                "0000320193|Apple Inc.|8-K|2026-03-25|"
                "edgar/data/320193/000032019326000100/aapl-20260325x8k402.htm"
            ),
            daily_master_index_url(repair_days[1]): master_index(
                "0000320193|Apple Inc.|4|2026-03-24|"
                "edgar/data/320193/000032019326000204/xslF345X05/wk-form4_1772053959.xml"
            ),
            detail_url(AAPL_CIK, "0000320193-26-000100"): load_text(
                "eight_k", "aapl_402", "detail-index.html"
            ),
            primary_url(AAPL_CIK, "0000320193-26-000100", "aapl-20260325x8k402.htm"): load_text(
                "eight_k", "aapl_402", "primary.html"
            ),
            form4_detail: load_text("form4", "live_xsl_case", "detail-index.html"),
            form4_xml: load_text("form4", "live_xsl_case", "ownership.xml"),
        },
    )
    client, fake_slack = make_phase4_client(tmp_path, sec_client=sec_client)
    with client:
        seed_watchlist_and_destination()
        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/repair-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 2
            assert session.query(Alert).count() == 2
            assert session.query(DeliveryAttempt).count() == 2
            runs = session.query(IngestRun).order_by(IngestRun.id.asc()).all()
            assert any(run.triggered_by == "repair" for run in runs)

        assert len(fake_slack.sent_payloads) == 2


def test_backfill_recovers_historical_accessions_for_new_and_reenabled_watchlist(
    tmp_path: Path,
    monkeypatch,
):
    backfill_days = [date(2026, 3, 20), date(2026, 3, 19)]
    monkeypatch.setattr("app.services.ingest.backfill_business_days", lambda: backfill_days)

    form4_detail = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000204/0000320193-26-000204-index.html"
    )
    form4_xml = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000204/xslF345X05/wk-form4_1772053959.xml"
    )
    sec_client = ScriptedFixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map={
            daily_master_index_url(backfill_days[0]): master_index(
                "0000320193|Apple Inc.|8-K|2026-03-20|"
                "edgar/data/320193/000032019326000100/aapl-20260325x8k402.htm"
            ),
            daily_master_index_url(backfill_days[1]): master_index(
                "0000320193|Apple Inc.|4|2026-03-19|"
                "edgar/data/320193/000032019326000204/xslF345X05/wk-form4_1772053959.xml"
            ),
            detail_url(AAPL_CIK, "0000320193-26-000100"): load_text(
                "eight_k", "aapl_402", "detail-index.html"
            ),
            primary_url(AAPL_CIK, "0000320193-26-000100", "aapl-20260325x8k402.htm"): load_text(
                "eight_k", "aapl_402", "primary.html"
            ),
            form4_detail: load_text("form4", "live_xsl_case", "detail-index.html"),
            form4_xml: load_text("form4", "live_xsl_case", "ownership.xml"),
        },
    )
    client, fake_slack = make_phase4_client(tmp_path, sec_client=sec_client)
    with client:
        with open_session() as session:
            session.add(
                Destination(
                    name="Primary Slack",
                    destination_type="slack",
                    enabled=True,
                    config_label="env:SLACK_WEBHOOK_URL",
                )
            )
            session.commit()

        response = client.get("/watchlist")
        csrf_token = extract_csrf_token(response.text)
        create_response = client.post(
            "/watchlist",
            data={
                "csrf_token": csrf_token,
                "ticker": "AAPL",
                "issuer_cik": AAPL_CIK,
                "manual_cik_override": "",
                "issuer_name": "Apple Inc.",
                "enabled": "on",
            },
            follow_redirects=True,
        )
        assert create_response.status_code == 200
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        toggle_csrf = extract_csrf_token(create_response.text)
        client.post("/watchlist/1/toggle", data={"csrf_token": toggle_csrf}, follow_redirects=True)
        pause_csrf = extract_csrf_token(client.get("/watchlist").text)
        client.post("/watchlist/1/toggle", data={"csrf_token": pause_csrf}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 2
            assert session.query(Alert).count() == 2
            assert session.query(DeliveryAttempt).count() == 2
            runs = session.query(IngestRun).order_by(IngestRun.id.asc()).all()
            assert any(run.triggered_by == "watchlist_create" for run in runs)
            assert any(run.triggered_by == "watchlist_enable" for run in runs)

        assert len(fake_slack.sent_payloads) == 2


def test_repair_revalidates_recent_form4_stage_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("app.services.ingest.previous_business_days", lambda count: [])

    accession = "0000320193-26-000204"
    form4_detail = detail_url(AAPL_CIK, accession)
    form4_xml = (
        "https://www.sec.gov/Archives/edgar/data/320193/"
        "000032019326000204/xslF345X05/wk-form4_1772053959.xml"
    )
    sec_client = ScriptedFixtureSecClient(
        json_map={COMPANY_TICKERS_URL: load_json("company_tickers.json")},
        text_map={
            form4_detail: load_text("form4", "live_xsl_case", "detail-index.html"),
            form4_xml: load_text("form4", "live_xsl_case", "ownership.xml"),
        },
    )
    client, fake_slack = make_phase4_client(tmp_path, sec_client=sec_client)
    with client:
        seed_watchlist_and_destination()
        recent_error_time = datetime.now(UTC) - timedelta(hours=3)
        with open_session() as session:
            session.add(
                StageError(
                    stage="form4_accession",
                    source_name="latest_ownership",
                    filing_accession=accession,
                    error_class="OwnershipXmlParseError",
                    message="Synthetic recent Form 4 parse failure.",
                    is_retryable=False,
                    created_at=recent_error_time,
                    updated_at=recent_error_time,
                )
            )
            session.commit()

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/repair-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 1
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1
            filing = session.query(Filing).one()
            assert filing.accession_number == accession
            assert filing.parser_status == "success"
            repair_run = session.query(IngestRun).filter_by(triggered_by="repair").one()
            assert "revalidated_form4=1" in (repair_run.notes or "")
            assert "recovered_form4=1" in (repair_run.notes or "")

        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/repair-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 1
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1

        assert len(fake_slack.sent_payloads) == 1


def test_p3_chunking_does_not_starve_p1_or_p2():
    broker = SecRequestBroker(rate_limit_rps=10)
    worker = BrokerWorker(broker)
    order: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def handle_p3(job):
        order.append(job.job_key)
        started.set()
        release.wait(timeout=1.0)
        if job.job_key == "p3-step-1":
            broker.enqueue(task_name="p3", priority=BrokerPriority.P3, job_key="p3-step-2")

    def handle_p2(job):
        order.append(job.job_key)

    def handle_p1(job):
        order.append(job.job_key)

    worker.register_handler("p3", handle_p3)
    worker.register_handler("p2", handle_p2)
    worker.register_handler("p1", handle_p1)
    broker.enqueue(task_name="p3", priority=BrokerPriority.P3, job_key="p3-step-1")
    worker.start()
    try:
        assert started.wait(timeout=1.0)
        broker.enqueue(task_name="p2", priority=BrokerPriority.P2, job_key="p2-step-1")
        broker.enqueue(task_name="p1", priority=BrokerPriority.P1, job_key="p1-step-1")
        release.set()
        assert worker.wait_for_idle(timeout=2.0)
    finally:
        worker.shutdown()

    assert order == ["p3-step-1", "p1-step-1", "p2-step-1", "p3-step-2"]
