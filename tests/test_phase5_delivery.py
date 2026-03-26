from __future__ import annotations

import json
import smtplib
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import open_session
from app.main import create_app
from app.models import Alert, DeliveryAttempt, Destination, Filing, WatchlistEntry
from app.services.notify.base import NotificationResult
from app.services.notify.smtp import SmtpNotifier
from app.services.notify.webhook import WebhookNotifier
from app.services.sec.client import FixtureSecClient
from app.services.sec.resolver import COMPANY_TICKERS_URL
from app.services.sec.submissions import submissions_url
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
        return self.send_alert(destination, {"headline": "test"})

    def send_alert(self, destination, payload: dict):
        destination_name = getattr(destination, "name", destination)
        self.sent_payloads.append({"destination_name": destination_name, "payload": payload})
        if self.fail:
            return NotificationResult(status="failed", detail="Synthetic Slack failure.")
        return NotificationResult(
            status="sent",
            detail="Synthetic Slack success.",
            response_code=200,
        )


class FakeSMTPConnection:
    def __init__(
        self,
        *,
        supports_starttls: bool = True,
        login_error: Exception | None = None,
        send_error: Exception | None = None,
        ssl_mode: bool = False,
    ) -> None:
        self.supports_starttls = supports_starttls
        self.login_error = login_error
        self.send_error = send_error
        self.ssl_mode = ssl_mode
        self.started_tls = False
        self.login_calls: list[tuple[str, str]] = []
        self.sent_messages = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def ehlo(self):
        return None

    def has_extn(self, extension: str) -> bool:
        return extension.lower() == "starttls" and self.supports_starttls

    def starttls(self):
        if not self.supports_starttls:
            raise smtplib.SMTPNotSupportedError("STARTTLS unavailable")
        self.started_tls = True

    def login(self, username: str, password: str):
        if self.login_error is not None:
            raise self.login_error
        self.login_calls.append((username, password))

    def send_message(self, message):
        if self.send_error is not None:
            raise self.send_error
        self.sent_messages.append(message)


def make_smtp_factories(
    *,
    supports_starttls: bool = True,
    login_error: Exception | None = None,
    send_error: Exception | None = None,
):
    plain_connections: list[FakeSMTPConnection] = []
    ssl_connections: list[FakeSMTPConnection] = []

    def smtp_factory(host, port, timeout=10):  # noqa: ARG001
        connection = FakeSMTPConnection(
            supports_starttls=supports_starttls,
            login_error=login_error,
            send_error=send_error,
            ssl_mode=False,
        )
        plain_connections.append(connection)
        return connection

    def smtp_ssl_factory(host, port, timeout=10):  # noqa: ARG001
        connection = FakeSMTPConnection(
            supports_starttls=True,
            login_error=login_error,
            send_error=send_error,
            ssl_mode=True,
        )
        ssl_connections.append(connection)
        return connection

    return smtp_factory, smtp_ssl_factory, plain_connections, ssl_connections


def make_settings(
    tmp_path: Path,
    **overrides,
) -> Settings:
    defaults = {
        "APP_HOST": "127.0.0.1",
        "APP_PORT": 8000,
        "DATA_DIR": tmp_path,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'phase5.db').as_posix()}",
        "SEC_USER_AGENT": "SEC Alert Test test@example.com",
        "SEC_POLL_INTERVAL_SECONDS": 60,
        "SEC_RATE_LIMIT_RPS": 10,
        "OPENAI_API_KEY": None,
        "OPENAI_MODEL": None,
        "SCHEDULER_ENABLED": False,
        "TESTING": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


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


def create_phase5_client(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    slack_notifier=None,
    webhook_notifier=None,
    smtp_notifier=None,
):
    resolved_settings = settings or make_settings(tmp_path)
    app = create_app(
        resolved_settings,
        service_overrides={
            "sec_client": make_fixture_client(),
            **({"slack_notifier": slack_notifier} if slack_notifier is not None else {}),
            **({"webhook_notifier": webhook_notifier} if webhook_notifier is not None else {}),
            **({"smtp_notifier": smtp_notifier} if smtp_notifier is not None else {}),
        },
    )
    return TestClient(app)


def seed_watchlist() -> None:
    with open_session() as session:
        session.add(
            WatchlistEntry(
                ticker="AAPL",
                issuer_cik=AAPL_CIK,
                issuer_name="Apple Inc.",
                enabled=True,
            )
        )
        session.commit()


def seed_destination(channel: str, *, enabled: bool = True, name: str | None = None) -> None:
    labels = {
        "slack": "env:SLACK_WEBHOOK_URL",
        "webhook": "env:ALERT_WEBHOOK_URL",
        "smtp": "env:SMTP_TO",
    }
    with open_session() as session:
        session.add(
            Destination(
                name=name or channel.title(),
                destination_type=channel,
                enabled=enabled,
                config_label=labels[channel],
            )
        )
        session.commit()


def post_ingest_now(client: TestClient) -> None:
    response = client.get("/")
    csrf_token = extract_csrf_token(response.text)
    run_response = client.post(
        "/actions/ingest-now",
        data={"csrf_token": csrf_token},
        follow_redirects=True,
    )
    assert run_response.status_code == 200
    assert client.app.state.worker.wait_for_idle(timeout=5.0)


def test_webhook_test_send_logs_attempt_and_hmac(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204, request=request)

    settings = make_settings(
        tmp_path,
        ALERT_WEBHOOK_URL="https://example.test/hook",
        ALERT_WEBHOOK_SECRET="top-secret",
    )
    webhook_notifier = WebhookNotifier(
        settings,
        transport=httpx.MockTransport(handler),
    )
    client = create_phase5_client(
        tmp_path,
        settings=settings,
        slack_notifier=FakeSlackNotifier(),
        webhook_notifier=webhook_notifier,
    )
    with client:
        seed_destination("webhook", name="Primary Webhook")
        response = client.get("/destinations")
        csrf_token = extract_csrf_token(response.text)
        test_response = client.post(
            "/destinations/webhook/test",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert test_response.status_code == 200
        with open_session() as session:
            attempts = session.query(DeliveryAttempt).all()
            assert len(attempts) == 1
            assert attempts[0].alert_id is None
            assert attempts[0].channel == "webhook"
            assert attempts[0].status == "sent"
        assert len(requests) == 1
        assert requests[0].headers["X-SEC-Alert-Timestamp"].isdigit()
        assert requests[0].headers["X-SEC-Alert-Signature"].startswith("sha256=")


def test_webhook_localhost_requires_test_mode(tmp_path: Path):
    destination = Destination(name="Webhook", destination_type="webhook", enabled=True)
    locked_settings = make_settings(tmp_path, ALERT_WEBHOOK_URL="http://localhost:9999/hook")
    locked_notifier = WebhookNotifier(locked_settings)
    locked_result = locked_notifier.send_test_message(destination)
    assert locked_result.status == "failed"
    assert locked_result.retryable is False

    open_settings = make_settings(
        tmp_path,
        ALERT_WEBHOOK_URL="http://localhost:9999/hook",
        LOCALHOST_WEBHOOK_TEST_MODE=True,
    )
    open_notifier = WebhookNotifier(
        open_settings,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    )
    open_result = open_notifier.send_test_message(destination)
    assert open_result.status == "sent"


def test_smtp_test_send_success_uses_ssl_path_and_logs_attempt(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        SMTP_HOST="smtp.example.test",
        SMTP_PORT=465,
        SMTP_FROM="alerts@example.test",
        SMTP_TO="ops@example.test",
    )
    smtp_factory, smtp_ssl_factory, plain_connections, ssl_connections = make_smtp_factories()
    smtp_notifier = SmtpNotifier(
        settings,
        smtp_factory=smtp_factory,
        smtp_ssl_factory=smtp_ssl_factory,
    )
    client = create_phase5_client(
        tmp_path,
        settings=settings,
        slack_notifier=FakeSlackNotifier(),
        smtp_notifier=smtp_notifier,
    )
    with client:
        seed_destination("smtp", name="Primary SMTP")
        response = client.get("/destinations")
        csrf_token = extract_csrf_token(response.text)
        test_response = client.post(
            "/destinations/smtp/test",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        assert test_response.status_code == 200
        assert not plain_connections
        assert len(ssl_connections) == 1
        assert len(ssl_connections[0].sent_messages) == 1
        with open_session() as session:
            attempts = session.query(DeliveryAttempt).all()
            assert len(attempts) == 1
            assert attempts[0].alert_id is None
            assert attempts[0].channel == "smtp"
            assert attempts[0].status == "sent"


def test_smtp_non_local_authenticated_without_starttls_fails_closed(tmp_path: Path):
    settings = make_settings(
        tmp_path,
        SMTP_HOST="smtp.example.test",
        SMTP_PORT=587,
        SMTP_FROM="alerts@example.test",
        SMTP_TO="ops@example.test",
        SMTP_USERNAME="user",
        SMTP_PASSWORD="pass",
    )
    smtp_factory, smtp_ssl_factory, _plain_connections, _ssl_connections = make_smtp_factories(
        supports_starttls=False,
    )
    notifier = SmtpNotifier(
        settings,
        smtp_factory=smtp_factory,
        smtp_ssl_factory=smtp_ssl_factory,
    )
    destination = Destination(name="SMTP", destination_type="smtp", enabled=True)
    result = notifier.send_test_message(destination)
    assert result.status == "failed"
    assert result.retryable is False
    assert result.error_class == "SMTPNotSupportedError"


def test_multichannel_delivery_keeps_ingest_success_and_is_idempotent(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    settings = make_settings(
        tmp_path,
        ALERT_WEBHOOK_URL="https://example.test/hook",
    )
    client = create_phase5_client(
        tmp_path,
        settings=settings,
        slack_notifier=FakeSlackNotifier(),
        webhook_notifier=WebhookNotifier(settings, transport=httpx.MockTransport(handler)),
    )
    with client:
        seed_watchlist()
        seed_destination("slack", name="Primary Slack")
        seed_destination("webhook", name="Primary Webhook")
        seed_destination("smtp", enabled=False, name="Primary SMTP")

        post_ingest_now(client)
        post_ingest_now(client)

        with open_session() as session:
            filing = session.query(Filing).one()
            alert = session.query(Alert).one()
            attempts = session.query(DeliveryAttempt).order_by(DeliveryAttempt.id.asc()).all()
            assert filing.summary_headline is not None
            assert alert.status == "delivered"
            assert len(attempts) == 2
            assert {attempt.channel for attempt in attempts} == {"slack", "webhook"}
            webhook_attempt = next(attempt for attempt in attempts if attempt.channel == "webhook")
            assert webhook_attempt.status == "failed"
            assert webhook_attempt.retryable is True


def test_enabled_but_unconfigured_smtp_records_failed_attempt_without_breaking_ingest(
    tmp_path: Path,
):
    settings = make_settings(
        tmp_path,
        SMTP_HOST="smtp.example.test",
        SMTP_PORT=587,
        SMTP_FROM="alerts@example.test",
    )
    client = create_phase5_client(
        tmp_path,
        settings=settings,
        slack_notifier=FakeSlackNotifier(fail=True),
    )
    with client:
        seed_watchlist()
        seed_destination("smtp", name="Primary SMTP")

        post_ingest_now(client)

        with open_session() as session:
            filing = session.query(Filing).one()
            alert = session.query(Alert).one()
            attempts = session.query(DeliveryAttempt).all()
            assert filing.summary_headline is not None
            assert alert.status == "delivery_failed"
            assert len(attempts) == 1
            assert attempts[0].channel == "smtp"
            assert attempts[0].status == "failed"
            assert attempts[0].retryable is False
            assert attempts[0].error_class == "MissingConfiguration"


def test_no_enabled_destinations_marks_alert_skipped(tmp_path: Path):
    client = create_phase5_client(tmp_path, slack_notifier=FakeSlackNotifier())
    with client:
        seed_watchlist()
        post_ingest_now(client)

        with open_session() as session:
            alert = session.query(Alert).one()
            assert alert.status == "skipped"
            assert session.query(DeliveryAttempt).count() == 0


def test_destinations_routes_and_page_show_configured_state_and_attempts(tmp_path: Path):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    settings = make_settings(
        tmp_path,
        ALERT_WEBHOOK_URL="https://example.test/hook",
        SMTP_HOST="smtp.example.test",
        SMTP_PORT=465,
        SMTP_FROM="alerts@example.test",
        SMTP_TO="ops@example.test",
    )
    smtp_factory, smtp_ssl_factory, _plain_connections, _ssl_connections = make_smtp_factories()
    client = create_phase5_client(
        tmp_path,
        settings=settings,
        slack_notifier=FakeSlackNotifier(),
        webhook_notifier=WebhookNotifier(settings, transport=httpx.MockTransport(handler)),
        smtp_notifier=SmtpNotifier(
            settings,
            smtp_factory=smtp_factory,
            smtp_ssl_factory=smtp_ssl_factory,
        ),
    )
    with client:
        page = client.get("/destinations")
        csrf_token = extract_csrf_token(page.text)
        client.post(
            "/destinations/webhook",
            data={"csrf_token": csrf_token, "name": "Webhook", "enabled": "on", "notes": "ops"},
            follow_redirects=True,
        )
        csrf_token = extract_csrf_token(client.get("/destinations").text)
        client.post(
            "/destinations/smtp",
            data={"csrf_token": csrf_token, "name": "SMTP", "enabled": "on", "notes": "mail"},
            follow_redirects=True,
        )
        csrf_token = extract_csrf_token(client.get("/destinations").text)
        client.post(
            "/destinations/webhook/test",
            data={"csrf_token": csrf_token},
            follow_redirects=True,
        )
        page = client.get("/destinations")
        assert "Runtime config is configured." in page.text
        assert "Recent Delivery Attempts" in page.text
        assert "webhook" in page.text.lower()
