from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.db import open_session
from app.models import Alert, DeliveryAttempt, Filing, StageError
from app.services.summarize.base import (
    RewriteResult,
    SummaryRewriteFailure,
    SummaryRewriteInput,
)
from app.services.summarize.openai_backend import OpenAIResponsesSummaryRewriter
from tests.conftest import extract_csrf_token
from tests.test_phase2_eight_k import (
    create_phase2_client,
)
from tests.test_phase2_eight_k import (
    seed_watchlist_and_destination as seed_phase2_watchlist_and_destination,
)
from tests.test_phase3_form4 import (
    create_phase3_client,
)
from tests.test_phase3_form4 import (
    seed_watchlist_and_destination as seed_phase3_watchlist_and_destination,
)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = {
        "APP_HOST": "127.0.0.1",
        "APP_PORT": 8000,
        "DATA_DIR": tmp_path,
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'phase6.db').as_posix()}",
        "SEC_USER_AGENT": "SEC Alert Test test@example.com",
        "SEC_POLL_INTERVAL_SECONDS": 60,
        "SEC_RATE_LIMIT_RPS": 10,
        "OPENAI_API_KEY": "test-key",
        "OPENAI_MODEL": "gpt-5-mini-2025-08-07",
        "SCHEDULER_ENABLED": False,
        "TESTING": True,
    }
    defaults.update(overrides)
    return Settings(**defaults)


class FakeResponsesClient:
    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeSummaryRewriter:
    def __init__(self, scripted_results: list[RewriteResult | Exception]) -> None:
        self.scripted_results = list(scripted_results)
        self.calls: list[SummaryRewriteInput] = []

    def is_active(self) -> bool:
        return True

    def rewrite(self, rewrite_input: SummaryRewriteInput):
        self.calls.append(rewrite_input)
        if not self.scripted_results:
            raise AssertionError("No fake rewrite result queued.")
        result = self.scripted_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_openai_rewriter_builds_responses_request_with_store_false_and_json_schema_format(
    tmp_path: Path,
):
    response = SimpleNamespace(
        status="completed",
        output=[],
        output_text=(
            '{"headline":"Readable headline","context":"Readable context",'
            '"category":"negative"}'
        ),
        output_parsed=None,
        incomplete_details=None,
    )
    client = FakeResponsesClient(response=response)
    rewriter = OpenAIResponsesSummaryRewriter(
        make_settings(tmp_path),
        responses_client=client,
    )

    result = rewriter.rewrite(
        SummaryRewriteInput(
            filing_accession="0000320193-26-000100",
            form_type="8-K",
            issuer_name="Apple Inc.",
            issuer_ticker="AAPL",
            deterministic_headline="Apple filed 8-K highlighting 4.02.",
            deterministic_context="Deterministic scoring marked this filing as negative.",
            score=-2.0,
            confidence="high",
            reasons=["8-K Item 4.02 detected"],
            prompt_payload={
                "item_numbers": ["4.02", "9.01"],
                "body_excerpt": "Sample excerpt",
                "exhibit_titles": ["Auditor letter"],
            },
        )
    )

    assert result is not None
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["store"] is False
    assert request["model"] == "gpt-5-mini-2025-08-07"
    assert request["text_format"].__name__ == "RewriteSchema"
    assert request["input"][0]["role"] == "system"
    assert request["input"][1]["role"] == "user"
    user_payload = request["input"][1]["content"]
    assert "deterministic_headline" in user_payload
    assert "Apple filed 8-K highlighting 4.02." in user_payload
    assert "Sample excerpt" in user_payload


def test_create_app_without_openai_key_uses_deterministic_only(tmp_path: Path):
    client, _fake_slack, _fixture_sec_client = create_phase2_client(tmp_path)
    with client:
        assert client.app.state.summary_rewriter.is_active() is False


def test_eight_k_successful_rewrite_updates_display_fields_and_payload(tmp_path: Path):
    rewriter = FakeSummaryRewriter(
        [
            RewriteResult(
                headline="Apple 8-K flagged as accounting-risk signal.",
                context="Readable rewrite for operators with the same negative direction.",
                category="negative",
                model="gpt-5-mini-2025-08-07",
                generated_at=datetime.now(UTC),
            )
        ]
    )
    client, fake_slack, _fixture_sec_client = create_phase2_client(
        tmp_path,
        summary_rewriter=rewriter,
    )
    with client:
        seed_phase2_watchlist_and_destination()
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
            filing = session.query(Filing).one()
            alert = session.query(Alert).one()
            assert filing.summary_headline is not None
            assert filing.openai_headline == "Apple 8-K flagged as accounting-risk signal."
            assert (
                filing.openai_context
                == "Readable rewrite for operators with the same negative direction."
            )
            assert filing.openai_category == "negative"
            assert filing.score == -2.0
            assert filing.confidence == "high"
            assert any("4.02" in reason for reason in filing.reasons or [])
            assert alert.headline == filing.openai_headline

        detail_page = client.get("/filings/1")
        assert "Summary source" in detail_page.text
        assert "openai" in detail_page.text
        assert "Category" in detail_page.text
        settings_page = client.get("/settings")
        assert "OPENAI_MODEL" in settings_page.text
        assert "OpenAI rewrite active" in settings_page.text
        assert len(rewriter.calls) == 1
        assert len(fake_slack.sent_payloads) == 1
        assert (
            fake_slack.sent_payloads[0]["payload"]["headline"]
            == "Apple 8-K flagged as accounting-risk signal."
        )
        assert (
            fake_slack.sent_payloads[0]["payload"]["context"]
            == "Readable rewrite for operators with the same negative direction."
        )


def test_form4_successful_rewrite_updates_display_fields(tmp_path: Path):
    rewriter = FakeSummaryRewriter(
        [
            RewriteResult(
                headline="Apple insider buy filing with aligned reporters.",
                context="Readable rewrite keeps the positive insider signal intact.",
                category="positive",
                model="gpt-4.1-mini-2025-04-14",
                generated_at=datetime.now(UTC),
            )
        ]
    )
    client, fake_slack, _fixture_sec_client = create_phase3_client(
        tmp_path,
        summary_rewriter=rewriter,
    )
    with client:
        seed_phase3_watchlist_and_destination()
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
            assert filing.openai_headline == "Apple insider buy filing with aligned reporters."
            assert (
                filing.openai_context
                == "Readable rewrite keeps the positive insider signal intact."
            )
            assert filing.openai_category == "positive"
            assert filing.score == 1.0
            assert filing.confidence == "high"

        assert len(rewriter.calls) == 1
        assert len(fake_slack.sent_payloads) == 1
        assert (
            fake_slack.sent_payloads[0]["payload"]["headline"]
            == "Apple insider buy filing with aligned reporters."
        )


def test_rewrite_failures_fall_back_without_blocking_delivery(tmp_path: Path):
    failure_cases = [
        ("OpenAIRefusal", False),
        ("OpenAIIncomplete", False),
        ("RewriteSchemaValidationError", False),
        ("APITimeoutError", True),
    ]
    for error_class, retryable in failure_cases:
        rewriter_case = FakeSummaryRewriter(
            [
                SummaryRewriteFailure(
                    error_class=error_class,
                    message="Synthetic rewrite failure.",
                    retryable=retryable,
                )
            ]
        )
        client_case, fake_slack_case, _fixture = create_phase2_client(
            tmp_path / error_class,
            summary_rewriter=rewriter_case,
        )
        with client_case:
            seed_phase2_watchlist_and_destination()
            response = client_case.get("/")
            csrf_token = extract_csrf_token(response.text)
            client_case.post(
                "/actions/ingest-now",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            assert client_case.app.state.worker.wait_for_idle(timeout=5.0)
            with open_session() as session:
                filing = session.query(Filing).one()
                errors = session.query(StageError).all()
                assert filing.summary_headline is not None
                assert filing.openai_headline is None
                assert len(errors) == 1
                assert errors[0].error_class == error_class
                assert errors[0].is_retryable is retryable
                assert session.query(Alert).count() == 1
                assert session.query(DeliveryAttempt).count() == 1
            assert len(fake_slack_case.sent_payloads) == 1


def test_completed_response_with_empty_output_falls_back_cleanly(tmp_path: Path):
    empty_response = SimpleNamespace(
        status="completed",
        output=[],
        output_text="",
        output_parsed=None,
        incomplete_details=None,
    )
    client = FakeResponsesClient(response=empty_response)
    rewriter = OpenAIResponsesSummaryRewriter(
        make_settings(tmp_path),
        responses_client=client,
    )

    try:
        rewriter.rewrite(
            SummaryRewriteInput(
                filing_accession="0000320193-26-000100",
                form_type="8-K",
                issuer_name="Apple Inc.",
                issuer_ticker="AAPL",
                deterministic_headline="A headline",
                deterministic_context="A context",
                score=-2.0,
                confidence="high",
                reasons=["8-K Item 4.02 detected"],
                prompt_payload={
                    "item_numbers": ["4.02"],
                    "body_excerpt": "x",
                    "exhibit_titles": [],
                },
            )
        )
    except SummaryRewriteFailure as exc:
        assert exc.error_class == "OpenAIEmptyOutput"
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("Expected a SummaryRewriteFailure for empty output.")


def test_repeated_ingest_is_idempotent_and_does_not_recall_rewriter(tmp_path: Path):
    rewriter = FakeSummaryRewriter(
        [
            RewriteResult(
                headline="Stable rewritten headline.",
                context="Stable rewritten context.",
                category="negative",
                model="gpt-5-mini-2025-08-07",
                generated_at=datetime.now(UTC),
            )
        ]
    )
    client, fake_slack, _fixture_sec_client = create_phase2_client(
        tmp_path,
        summary_rewriter=rewriter,
    )
    with client:
        seed_phase2_watchlist_and_destination()
        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        for _ in range(2):
            client.post(
                "/actions/ingest-now",
                data={"csrf_token": csrf_token},
                follow_redirects=True,
            )
            assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            assert session.query(Filing).count() == 1
            assert session.query(Alert).count() == 1
            assert session.query(DeliveryAttempt).count() == 1
        assert len(rewriter.calls) == 1
        assert len(fake_slack.sent_payloads) == 1


def test_reparse_rewrites_same_filing_without_new_delivery(tmp_path: Path):
    rewriter = FakeSummaryRewriter(
        [
            RewriteResult(
                headline="Initial rewritten headline.",
                context="Initial rewritten context.",
                category="negative",
                model="gpt-5-mini-2025-08-07",
                generated_at=datetime.now(UTC),
            ),
            RewriteResult(
                headline="Updated rewritten headline after reparse.",
                context="Updated rewritten context after reparse.",
                category="mixed",
                model="gpt-5-mini-2025-08-07",
                generated_at=datetime.now(UTC),
            ),
        ]
    )
    client, fake_slack, _fixture_sec_client = create_phase2_client(
        tmp_path,
        summary_rewriter=rewriter,
    )
    with client:
        seed_phase2_watchlist_and_destination()
        response = client.get("/")
        csrf_token = extract_csrf_token(response.text)
        client.post("/actions/ingest-now", data={"csrf_token": csrf_token}, follow_redirects=True)
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.query(Filing).one()
            filing_id = filing.id
            alert = session.query(Alert).one()
            original_alert_headline = alert.headline

        detail_response = client.get(f"/filings/{filing_id}")
        detail_csrf = extract_csrf_token(detail_response.text)
        client.post(
            f"/filings/{filing_id}/reparse",
            data={"csrf_token": detail_csrf},
            follow_redirects=True,
        )
        assert client.app.state.worker.wait_for_idle(timeout=5.0)

        with open_session() as session:
            filing = session.get(Filing, filing_id)
            alert = session.query(Alert).one()
            assert filing is not None
            assert filing.openai_headline == "Updated rewritten headline after reparse."
            assert filing.openai_context == "Updated rewritten context after reparse."
            assert filing.openai_category == "mixed"
            assert alert.headline == original_alert_headline
            assert session.query(DeliveryAttempt).count() == 1
        assert len(rewriter.calls) == 2
        assert len(fake_slack.sent_payloads) == 1
