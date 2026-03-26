from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.db import open_session
from app.models import Filing, IngestRun, SourceCursor, StageError, WatchlistEntry
from app.services.alerts import AlertDeliveryService
from app.services.broker import BrokerPriority, SecRequestBroker
from app.services.scoring.eight_k import EightKScorer
from app.services.scoring.form4 import Form4Scorer
from app.services.sec.eight_k import EightKParser, locate_primary_eight_k_document_url
from app.services.sec.form4 import Form4Parser, locate_ownership_xml, parse_form4_detail_page
from app.services.sec.indexes import MasterIndexRow, backfill_business_days, previous_business_days
from app.services.sec.latest_ownership import (
    INGESTIBLE_FORM4_TYPES,
    LATEST_OWNERSHIP_URL,
    OWNERSHIP_DISCOVERY_FILTER_KEY,
    OwnershipCandidate,
    parse_ownership_candidates,
)
from app.services.sec.resolver import ResolvedIssuer, TickerResolver, normalize_cik
from app.services.sec.submissions import SubmissionFiling, parse_recent_8k_filings, submissions_url
from app.services.summarize.base import (
    NullSummaryRewriter,
    SummaryRewriteFailure,
    SummaryRewriteInput,
    SummaryRewriter,
    clear_openai_fields,
    effective_summary_for_filing,
)
from app.services.summarize.deterministic import DeterministicEightKSummarizer
from app.services.summarize.form4 import DeterministicForm4Summarizer

logger = logging.getLogger(__name__)

MANUAL_FORM4_CURSOR_SOURCE = "latest_ownership"
MANUAL_FORM4_CURSOR_FILTER = OWNERSHIP_DISCOVERY_FILTER_KEY
LIVE_8K_CURSOR_SOURCE = "live-8k-submissions"
LIVE_FORM4_CURSOR_SOURCE = "live-form4-ownership-page1"
LIVE_FORM4_CURSOR_FILTER = "feed:owner-only:page1"
REPAIR_CURSOR_SOURCE = "repair-daily-master"
P3_CHUNK_SIZE = 25
REPAIR_FORM_TYPES = {"8-K", "8-K/A", "4", "4/A"}


def _utc_or_min(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _form4_transaction_codes(normalized_payload: dict[str, Any] | None) -> list[str]:
    if not normalized_payload:
        return []
    codes: list[str] = []
    for key in ("non_derivative_transactions", "derivative_transactions"):
        for row in normalized_payload.get(key) or []:
            code = row.get("transaction_code")
            if code and code not in codes:
                codes.append(code)
    return codes


def _form4_tenb5_one_context(normalized_payload: dict[str, Any] | None) -> dict[str, Any]:
    if not normalized_payload:
        return {}
    tenb5_one = normalized_payload.get("tenb5_1") or {}
    return {
        "checkbox": tenb5_one.get("checkbox"),
        "mentioned_in_remarks": tenb5_one.get("mentioned_in_remarks"),
        "mentioned_in_footnotes": tenb5_one.get("mentioned_in_footnotes"),
        "adoption_date": tenb5_one.get("adoption_date"),
    }


@dataclass(slots=True)
class EightKFilingTarget:
    accession_number: str
    form_type: str
    filed_date: date | None
    accepted_at: datetime | None
    issuer_cik: str
    issuer_ticker: str | None
    issuer_name: str | None
    detail_url: str | None
    source_url: str | None

    @classmethod
    def from_submission(cls, candidate: SubmissionFiling) -> EightKFilingTarget:
        return cls(
            accession_number=candidate.accession_number,
            form_type=candidate.form_type,
            filed_date=candidate.filed_date,
            accepted_at=candidate.accepted_at,
            issuer_cik=candidate.issuer_cik,
            issuer_ticker=candidate.issuer_ticker,
            issuer_name=candidate.issuer_name,
            detail_url=candidate.detail_index_url,
            source_url=candidate.primary_document_url,
        )

    @classmethod
    def from_index_row(cls, row: MasterIndexRow) -> EightKFilingTarget | None:
        accession_number = row.accession_number
        detail_url = row.detail_index_url
        if accession_number is None or detail_url is None:
            return None
        return cls(
            accession_number=accession_number,
            form_type=row.form_type,
            filed_date=row.filed_date,
            accepted_at=None,
            issuer_cik=row.cik,
            issuer_ticker=None,
            issuer_name=row.company_name,
            detail_url=detail_url,
            source_url=None,
        )

    def cursor_tuple(self) -> tuple[datetime, date, str]:
        return (
            _utc_or_min(self.accepted_at),
            self.filed_date or date.min,
            self.accession_number,
        )


class BaseIngestService:
    def __init__(
        self,
        *,
        settings: Settings,
        resolver: TickerResolver,
        alert_delivery: AlertDeliveryService,
        summary_rewriter: SummaryRewriter | None = None,
    ) -> None:
        self.settings = settings
        self.resolver = resolver
        self.alert_delivery = alert_delivery
        self.summary_rewriter = summary_rewriter or NullSummaryRewriter()

    def _mark_run_status(
        self,
        run_id: int | None,
        *,
        status: str,
        notes: str | None = None,
    ) -> None:
        if run_id is None:
            return
        with open_session() as session:
            run = session.get(IngestRun, run_id)
            if run is None:
                return
            run.status = status
            if status == "running":
                run.started_at = datetime.now(UTC)
            if status in {"completed", "failed"}:
                run.finished_at = datetime.now(UTC)
            if notes is not None:
                run.notes = notes
            session.add(run)
            session.commit()

    def _record_error(
        self,
        *,
        stage: str,
        source_name: str | None,
        filing_accession: str | None,
        error_class: str,
        message: str,
        is_retryable: bool = False,
    ) -> None:
        with open_session() as session:
            session.add(
                StageError(
                    stage=stage,
                    source_name=source_name,
                    filing_accession=filing_accession,
                    error_class=error_class,
                    message=message,
                    is_retryable=is_retryable,
                )
            )
            session.commit()

    def _persist_resolution(self, entry_id: int, resolved: ResolvedIssuer) -> None:
        with open_session() as session:
            entry = session.get(WatchlistEntry, entry_id)
            if entry is None:
                return
            entry.issuer_cik = resolved.issuer_cik
            if resolved.issuer_name and not entry.issuer_name:
                entry.issuer_name = resolved.issuer_name
            session.add(entry)
            session.commit()

    def _resolve_enabled_watchlist_entries(self) -> dict[str, list[int]]:
        watched: dict[str, list[int]] = {}
        with open_session() as session:
            entries = session.scalars(
                select(WatchlistEntry).where(WatchlistEntry.enabled.is_(True))
            ).all()

        for entry in entries:
            resolved = self.resolver.resolve(entry)
            if resolved is None:
                self._record_error(
                    stage="resolver",
                    source_name="company_tickers_json",
                    filing_accession=None,
                    error_class="ResolutionError",
                    message=f"Unable to resolve issuer CIK for {entry.ticker}.",
                )
                continue
            self._persist_resolution(entry.id, resolved)
            watched.setdefault(resolved.issuer_cik, []).append(entry.id)
        return watched

    def watched_cik_set(self) -> set[str]:
        return set(self._resolve_enabled_watchlist_entries())

    def _ensure_alert_for_filing_id(self, filing_id: int, *, deliver: bool) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            self._ensure_alert(session, filing, deliver=deliver)
            session.commit()

    def _ensure_alert(self, session, filing: Filing, *, deliver: bool) -> None:
        alert, created = self.alert_delivery.ensure_alert(session, filing)
        if created and alert.headline is None:
            alert.headline = effective_summary_for_filing(filing).headline
        session.add(alert)
        session.flush()
        if deliver and created:
            self.alert_delivery.deliver_alert_once(session, filing, alert)

    def _clear_openai_summary_state(self, filing_id: int) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            clear_openai_fields(filing)
            session.add(filing)
            session.commit()

    def _apply_optional_rewrite(
        self,
        *,
        filing: Filing,
        rewrite_input: SummaryRewriteInput,
    ) -> None:
        clear_openai_fields(filing)
        if not self.summary_rewriter.is_active():
            return

        try:
            rewrite = self.summary_rewriter.rewrite(rewrite_input)
        except SummaryRewriteFailure as exc:
            self._record_error(
                stage="openai_rewrite",
                source_name="openai",
                filing_accession=rewrite_input.filing_accession,
                error_class=exc.error_class,
                message=exc.message,
                is_retryable=exc.retryable,
            )
            return

        if rewrite is None:
            return

        filing.openai_headline = rewrite.headline
        filing.openai_context = rewrite.context
        filing.openai_category = rewrite.category
        filing.openai_model = rewrite.model
        filing.openai_generated_at = rewrite.generated_at


class EightKIngestService(BaseIngestService):
    def __init__(
        self,
        *,
        settings: Settings,
        broker: SecRequestBroker,
        sec_client,
        resolver: TickerResolver,
        parser: EightKParser,
        scorer: EightKScorer,
        summarizer: DeterministicEightKSummarizer,
        summary_rewriter: SummaryRewriter,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        super().__init__(
            settings=settings,
            resolver=resolver,
            alert_delivery=alert_delivery,
            summary_rewriter=summary_rewriter,
        )
        self.broker = broker
        self.sec_client = sec_client
        self.parser = parser
        self.scorer = scorer
        self.summarizer = summarizer

    def run_manual_ingest(self, run_id: int | None = None) -> None:
        self._mark_run_status(run_id, status="running")
        discovered = 0
        try:
            with open_session() as session:
                entries = session.scalars(
                    select(WatchlistEntry).where(WatchlistEntry.enabled.is_(True))
                ).all()

            for entry in entries:
                try:
                    resolved = self.resolver.resolve(entry)
                    if resolved is None:
                        self._record_error(
                            stage="resolver",
                            source_name="company_tickers_json",
                            filing_accession=None,
                            error_class="ResolutionError",
                            message=f"Unable to resolve issuer CIK for {entry.ticker}.",
                        )
                        continue
                    self._persist_resolution(entry.id, resolved)
                    discovered += self._manual_ingest_watchlist_entry(resolved)
                except Exception as exc:
                    self._record_error(
                        stage="manual_ingest",
                        source_name="submissions",
                        filing_accession=None,
                        error_class=exc.__class__.__name__,
                        message=str(exc),
                    )
            self._mark_run_status(
                run_id,
                status="completed",
                notes=f"Processed {discovered} 8-K candidate(s).",
            )
        except Exception as exc:
            self._mark_run_status(run_id, status="failed", notes=str(exc))
            raise

    def orchestrate_live_poll(self) -> None:
        watched = self._resolve_enabled_watchlist_entries()
        for issuer_cik in sorted(watched):
            self.broker.enqueue(
                task_name="poll-live-8k-submissions-issuer",
                priority=BrokerPriority.P2,
                job_key=f"poll:live:8k:issuer:{issuer_cik}",
                source_name="live-8k-submissions",
                payload={"issuer_cik": issuer_cik},
            )

    def poll_live_issuer(self, issuer_cik: str) -> None:
        payload = self.sec_client.get_json(submissions_url(issuer_cik))
        candidates = [
            EightKFilingTarget.from_submission(candidate)
            for candidate in parse_recent_8k_filings(payload, issuer_cik=issuer_cik)
        ]
        candidates.sort(key=lambda candidate: candidate.cursor_tuple(), reverse=True)
        window_size = min(len(candidates), self.settings.sec_live_8k_overlap_rows)
        window = candidates[:window_size]
        cursor_tuple = self._current_cursor_tuple(issuer_cik)
        overflow = bool(
            cursor_tuple is not None
            and len(candidates) >= self.settings.sec_live_8k_overlap_rows
            and window
            and all(candidate.cursor_tuple() > cursor_tuple for candidate in window)
        )

        for candidate in window:
            if cursor_tuple is not None and candidate.cursor_tuple() <= cursor_tuple:
                continue
            self.register_discovered_filing(
                candidate,
                enqueue_processing=True,
                deliver=True,
            )

        self._mark_8k_poll_success(issuer_cik)
        if overflow:
            self._record_error(
                stage="live_8k_overlap",
                source_name=f"{LIVE_8K_CURSOR_SOURCE}:{issuer_cik}",
                filing_accession=None,
                error_class="OverlapWindowWarning",
                message=(
                    "Live 8-K cursor fell outside the configured overlap window; "
                    "repair may be needed to recover older filings."
                ),
                is_retryable=True,
            )
            return
        if window:
            self._advance_8k_cursor(issuer_cik, window[0])

    def register_discovered_filing(
        self,
        target: EightKFilingTarget,
        *,
        enqueue_processing: bool,
        deliver: bool,
    ) -> int | None:
        with open_session() as session:
            filing = self._upsert_filing(session, target)
            filing_id = filing.id
            should_process = (
                filing.parser_status != "success"
                or filing.summary_headline is None
                or filing.source_url is None
            )
            session.commit()

        if filing_id is None:
            return None
        if should_process:
            if enqueue_processing:
                self._enqueue_process_filing(filing_id, target.accession_number)
            else:
                self._process_filing_id(filing_id, force_reparse=False, deliver=deliver)
        else:
            self._ensure_alert_for_filing_id(filing_id, deliver=False)
        return filing_id

    def process_filing(self, filing_id: int) -> None:
        self._process_filing_id(filing_id, force_reparse=False, deliver=True)

    def reparse_filing(self, filing_id: int) -> None:
        try:
            self._process_filing_id(filing_id, force_reparse=True, deliver=False)
        except Exception as exc:
            self._record_error(
                stage="reparse",
                source_name="manual-reparse",
                filing_accession=None,
                error_class=exc.__class__.__name__,
                message=str(exc),
            )
            raise

    def _manual_ingest_watchlist_entry(self, resolved: ResolvedIssuer) -> int:
        payload = self.sec_client.get_json(submissions_url(resolved.issuer_cik))
        candidates = parse_recent_8k_filings(payload, issuer_cik=resolved.issuer_cik)
        discovered = 0
        for candidate in candidates:
            try:
                target = EightKFilingTarget.from_submission(candidate)
                self.register_discovered_filing(
                    target,
                    enqueue_processing=False,
                    deliver=True,
                )
                discovered += 1
            except Exception as exc:
                self._record_error(
                    stage="ingest_submission",
                    source_name="submissions",
                    filing_accession=candidate.accession_number,
                    error_class=exc.__class__.__name__,
                    message=str(exc),
                )
        return discovered

    def _enqueue_process_filing(self, filing_id: int, accession_number: str) -> None:
        self.broker.enqueue(
            task_name="process-8k-filing",
            priority=BrokerPriority.P1,
            job_key=f"process:8k:{accession_number}",
            source_name="8k-processing",
            payload={"filing_id": filing_id, "accession_number": accession_number},
        )

    def _current_cursor_tuple(self, issuer_cik: str) -> tuple[datetime, date, str] | None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == LIVE_8K_CURSOR_SOURCE,
                    SourceCursor.filter_key == f"issuer-cik:{issuer_cik}",
                )
            )
            if cursor is None:
                return None
            return (
                _utc_or_min(cursor.accepted_at),
                cursor.filed_date or date.min,
                cursor.accession_number or "",
            )

    def _mark_8k_poll_success(self, issuer_cik: str) -> None:
        self.broker.mark_source_success(f"live-8k-submissions:{issuer_cik}")
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == LIVE_8K_CURSOR_SOURCE,
                    SourceCursor.filter_key == f"issuer-cik:{issuer_cik}",
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name=LIVE_8K_CURSOR_SOURCE,
                    filter_key=f"issuer-cik:{issuer_cik}",
                )
                session.add(cursor)
                session.flush()
            cursor.last_polled_at = datetime.now(UTC)
            session.add(cursor)
            session.commit()

    def _advance_8k_cursor(self, issuer_cik: str, target: EightKFilingTarget) -> None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == LIVE_8K_CURSOR_SOURCE,
                    SourceCursor.filter_key == f"issuer-cik:{issuer_cik}",
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name=LIVE_8K_CURSOR_SOURCE,
                    filter_key=f"issuer-cik:{issuer_cik}",
                )
                session.add(cursor)
                session.flush()
            existing_tuple = (
                _utc_or_min(cursor.accepted_at),
                cursor.filed_date or date.min,
                cursor.accession_number or "",
            )
            if target.cursor_tuple() > existing_tuple:
                cursor.accepted_at = target.accepted_at
                cursor.filed_date = target.filed_date
                cursor.accession_number = target.accession_number
            cursor.last_polled_at = datetime.now(UTC)
            session.add(cursor)
            session.commit()

    def _upsert_filing(self, session, target: EightKFilingTarget) -> Filing:
        filing = session.scalar(
            select(Filing).where(Filing.accession_number == target.accession_number)
        )
        if filing is None:
            filing = Filing(
                accession_number=target.accession_number,
                form_type=target.form_type,
                is_amendment=target.form_type.endswith("/A"),
                filed_date=target.filed_date,
                accepted_at=target.accepted_at,
                issuer_cik=target.issuer_cik,
                issuer_ticker=target.issuer_ticker,
                issuer_name=target.issuer_name,
                detail_url=target.detail_url,
                source_url=target.source_url,
                parser_status="pending",
                scoring_status="pending",
                summarization_status="pending",
            )
            session.add(filing)
            session.flush()
            return filing

        filing.form_type = target.form_type
        filing.is_amendment = target.form_type.endswith("/A")
        filing.filed_date = target.filed_date
        filing.accepted_at = target.accepted_at
        filing.issuer_cik = target.issuer_cik
        filing.issuer_ticker = target.issuer_ticker or filing.issuer_ticker
        filing.issuer_name = target.issuer_name or filing.issuer_name
        filing.detail_url = target.detail_url or filing.detail_url
        filing.source_url = target.source_url or filing.source_url
        session.add(filing)
        session.flush()
        return filing

    def _process_filing_id(self, filing_id: int, *, force_reparse: bool, deliver: bool) -> None:
        if force_reparse:
            self._clear_openai_summary_state(filing_id)
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            detail_url = filing.detail_url
            source_url = filing.source_url
            accession_number = filing.accession_number
            form_type = filing.form_type

        if not detail_url:
            self._record_error(
                stage="detail_fetch",
                source_name="8k-detail",
                filing_accession=accession_number,
                error_class="MissingUrlError",
                message="Detail URL missing for filing.",
            )
            return

        detail_html = self.sec_client.get_text(detail_url)
        if not source_url:
            source_url = locate_primary_eight_k_document_url(
                detail_html,
                detail_url=detail_url,
                form_type=form_type,
            )
            if source_url is None:
                self._record_error(
                    stage="detail_fetch",
                    source_name="8k-detail",
                    filing_accession=accession_number,
                    error_class="MissingPrimaryDocumentError",
                    message="Unable to locate the primary 8-K filing document.",
                )
                return

        primary_document = self.sec_client.get_text(source_url)
        parsed = self.parser.parse(detail_html, primary_document)
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            score_bundle = self.scorer.score(parsed)
            summary = self.summarizer.summarize(
                issuer_name=filing.issuer_name,
                issuer_ticker=filing.issuer_ticker,
                form_type=filing.form_type,
                parsed=parsed,
                score_bundle=score_bundle,
            )

            filing.source_url = source_url
            filing.normalized_payload = {
                "item_numbers": parsed.item_numbers,
                "cleaned_body": parsed.cleaned_body,
                "exhibit_titles": parsed.exhibit_titles,
                "detail_index_url": filing.detail_url,
                "primary_document_url": filing.source_url,
            }
            filing.parser_status = "success"
            filing.scoring_status = "success"
            filing.summarization_status = "success"
            filing.score = score_bundle.score
            filing.confidence = score_bundle.confidence
            filing.reasons = score_bundle.reasons
            filing.summary_headline = summary.headline
            filing.summary_context = summary.context
            self._apply_optional_rewrite(
                filing=filing,
                rewrite_input=SummaryRewriteInput(
                    filing_accession=filing.accession_number,
                    form_type=filing.form_type,
                    issuer_name=filing.issuer_name,
                    issuer_ticker=filing.issuer_ticker,
                    deterministic_headline=summary.headline,
                    deterministic_context=summary.context,
                    score=score_bundle.score,
                    confidence=score_bundle.confidence,
                    reasons=score_bundle.reasons,
                    prompt_payload={
                        "item_numbers": parsed.item_numbers,
                        "body_excerpt": (parsed.cleaned_body or "")[:1200],
                        "exhibit_titles": parsed.exhibit_titles,
                    },
                ),
            )
            session.add(filing)
            session.flush()

            self._ensure_alert(session, filing, deliver=deliver and not force_reparse)
            session.commit()


class Form4IngestService(BaseIngestService):
    def __init__(
        self,
        *,
        settings: Settings,
        broker: SecRequestBroker,
        sec_client,
        resolver: TickerResolver,
        parser: Form4Parser,
        scorer: Form4Scorer,
        summarizer: DeterministicForm4Summarizer,
        summary_rewriter: SummaryRewriter,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        super().__init__(
            settings=settings,
            resolver=resolver,
            alert_delivery=alert_delivery,
            summary_rewriter=summary_rewriter,
        )
        self.broker = broker
        self.sec_client = sec_client
        self.parser = parser
        self.scorer = scorer
        self.summarizer = summarizer

    def run_manual_ingest(self, run_id: int | None = None) -> None:
        self._mark_run_status(run_id, status="running")
        try:
            watched_ciks = self._resolve_enabled_watchlist_entries()
            if not watched_ciks:
                self._mark_run_status(
                    run_id,
                    status="completed",
                    notes="No watchlist entries resolved.",
                )
                return

            enqueued = self._queue_form4_candidates(
                source_name=MANUAL_FORM4_CURSOR_SOURCE,
                filter_key=MANUAL_FORM4_CURSOR_FILTER,
                advance_cursor=True,
                stop_on_older_candidates=True,
            )
            self._mark_run_status(
                run_id,
                status="completed",
                notes=f"Queued {enqueued} Form 4 accession job(s).",
            )
        except Exception as exc:
            self._record_error(
                stage="form4_manual_ingest",
                source_name="latest_ownership",
                filing_accession=None,
                error_class=exc.__class__.__name__,
                message=str(exc),
            )
            self._mark_run_status(run_id, status="failed", notes=str(exc))
            raise

    def orchestrate_live_poll(self) -> None:
        self.broker.enqueue(
            task_name="poll-live-form4-ownership-page1",
            priority=BrokerPriority.P2,
            job_key="poll:live:form4:page1",
            source_name=LIVE_FORM4_CURSOR_SOURCE,
            payload={},
        )

    def poll_live_page(self) -> None:
        self._queue_form4_candidates(
            source_name=LIVE_FORM4_CURSOR_SOURCE,
            filter_key=LIVE_FORM4_CURSOR_FILTER,
            advance_cursor=True,
            stop_on_older_candidates=False,
        )

    def enqueue_form4_candidate(
        self,
        candidate: OwnershipCandidate,
        *,
        source_name: str | None,
        filter_key: str | None,
    ) -> bool:
        payload = candidate.to_payload()
        payload["cursor_source_name"] = source_name
        payload["cursor_filter_key"] = filter_key
        enqueue_result = self.broker.enqueue(
            task_name="process-form4-accession",
            priority=BrokerPriority.P1,
            job_key=f"process:form4:{candidate.accession_number}",
            source_name=source_name or "form4-candidate",
            payload=payload,
        )
        return enqueue_result.accepted

    def process_accession(self, payload: dict[str, str | None]) -> None:
        candidate = OwnershipCandidate.from_payload(payload)
        accession = candidate.accession_number
        cursor_source_name = payload.get("cursor_source_name")
        cursor_filter_key = payload.get("cursor_filter_key")
        try:
            detail_html = self.sec_client.get_text(candidate.detail_url)
            detail_metadata = parse_form4_detail_page(
                detail_html,
                detail_url=candidate.detail_url,
            )
            confirmed = OwnershipCandidate(
                accession_number=detail_metadata.accession_number or accession,
                form_type=(detail_metadata.form_type or candidate.form_type or "").upper(),
                detail_url=candidate.detail_url,
                filed_date=detail_metadata.filed_date or candidate.filed_date,
                accepted_at=detail_metadata.accepted_at or candidate.accepted_at,
            )
            if cursor_source_name and cursor_filter_key:
                self._advance_cursor(confirmed, cursor_source_name, cursor_filter_key)

            if confirmed.form_type not in INGESTIBLE_FORM4_TYPES:
                return

            xml_url = locate_ownership_xml(detail_metadata)
            if xml_url is None:
                self._handle_missing_xml(confirmed, detail_metadata)
                return

            ownership_xml = self.sec_client.get_text(xml_url)
            parsed = self.parser.parse(detail_html, ownership_xml)
            issuer_cik = normalize_cik(parsed.issuer_cik or detail_metadata.issuer_cik)
            if issuer_cik is None:
                self._record_error(
                    stage="form4_parser",
                    source_name="ownership_xml",
                    filing_accession=confirmed.accession_number,
                    error_class="IssuerResolutionError",
                    message="Unable to resolve issuer CIK from detail page or ownership XML.",
                )
                return
            if not self._is_watched_issuer(issuer_cik):
                return

            self._store_parsed_form4(
                candidate=confirmed,
                detail_metadata=detail_metadata,
                xml_url=xml_url,
                parsed=parsed,
                deliver=True,
                force_reparse=False,
            )
        except Exception as exc:
            self._record_error(
                stage="form4_accession",
                source_name="latest_ownership",
                filing_accession=accession,
                error_class=exc.__class__.__name__,
                message=str(exc),
            )
            raise

    def reparse_filing(self, filing_id: int) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            detail_url = filing.detail_url
            existing_source_url = filing.source_url
            accession_number = filing.accession_number
            form_type = filing.form_type
            filed_date = filing.filed_date
            accepted_at = filing.accepted_at

        if form_type not in INGESTIBLE_FORM4_TYPES or not detail_url:
            return

        try:
            self._clear_openai_summary_state(filing_id)
            detail_html = self.sec_client.get_text(detail_url)
            detail_metadata = parse_form4_detail_page(detail_html, detail_url=detail_url)
            xml_url = existing_source_url or locate_ownership_xml(detail_metadata)
            if xml_url is None:
                self._record_error(
                    stage="reparse",
                    source_name="form4_detail",
                    filing_accession=accession_number,
                    error_class="MissingXmlError",
                    message="Unable to locate ownership XML during reparse.",
                )
                self._mark_filing_failed(filing_id)
                return

            ownership_xml = self.sec_client.get_text(xml_url)
            parsed = self.parser.parse(detail_html, ownership_xml)
            candidate = OwnershipCandidate(
                accession_number=accession_number,
                form_type=form_type,
                detail_url=detail_url,
                filed_date=detail_metadata.filed_date or filed_date,
                accepted_at=detail_metadata.accepted_at or accepted_at,
            )
            self._store_parsed_form4(
                candidate=candidate,
                detail_metadata=detail_metadata,
                xml_url=xml_url,
                parsed=parsed,
                deliver=False,
                force_reparse=True,
                existing_filing_id=filing_id,
            )
        except Exception as exc:
            self._record_error(
                stage="reparse",
                source_name="manual-reparse",
                filing_accession=accession_number,
                error_class=exc.__class__.__name__,
                message=str(exc),
            )
            raise

    def _queue_form4_candidates(
        self,
        *,
        source_name: str,
        filter_key: str,
        advance_cursor: bool,
        stop_on_older_candidates: bool,
    ) -> int:
        feed_xml = self.sec_client.get_text(LATEST_OWNERSHIP_URL)
        candidates = parse_ownership_candidates(feed_xml)
        self.broker.mark_source_success(source_name)
        self._mark_discovery_polled(source_name, filter_key)

        cursor_tuple = self._current_cursor_tuple(source_name, filter_key)
        older_candidates = 0
        enqueued = 0
        for candidate in candidates:
            if candidate.form_type not in INGESTIBLE_FORM4_TYPES:
                continue
            if cursor_tuple is not None and candidate.cursor_tuple() <= cursor_tuple:
                older_candidates += 1
                if stop_on_older_candidates and older_candidates >= 10:
                    break
                continue
            older_candidates = 0
            if self.enqueue_form4_candidate(
                candidate,
                source_name=source_name if advance_cursor else None,
                filter_key=filter_key if advance_cursor else None,
            ):
                enqueued += 1
        return enqueued

    def _handle_missing_xml(
        self,
        candidate: OwnershipCandidate,
        detail_metadata,
    ) -> None:
        issuer_cik = normalize_cik(detail_metadata.issuer_cik)
        if issuer_cik is not None and self._is_watched_issuer(issuer_cik):
            filing_id = self._upsert_failed_form4_filing(
                candidate=candidate,
                detail_metadata=detail_metadata,
                parser_message="Unable to locate ownership XML from filing detail page.",
            )
            self._mark_filing_failed(filing_id)
        self._record_error(
            stage="form4_xml_locator",
            source_name="form4_detail",
            filing_accession=candidate.accession_number,
            error_class="MissingXmlError",
            message="Unable to locate ownership XML from filing detail page.",
        )

    def _store_parsed_form4(
        self,
        *,
        candidate: OwnershipCandidate,
        detail_metadata,
        xml_url: str,
        parsed,
        deliver: bool,
        force_reparse: bool,
        existing_filing_id: int | None = None,
    ) -> None:
        issuer_cik = normalize_cik(parsed.issuer_cik or detail_metadata.issuer_cik)
        if issuer_cik is None:
            raise ValueError("Unable to resolve issuer CIK for Form 4 filing.")
        with open_session() as session:
            filing = self._upsert_form4_filing(
                session,
                candidate=candidate,
                detail_metadata=detail_metadata,
                parsed=parsed,
                xml_url=xml_url,
                existing_filing_id=existing_filing_id,
            )
            score_bundle = self.scorer.score(parsed)
            summary = self.summarizer.summarize(
                issuer_name=parsed.issuer_name or detail_metadata.issuer_name,
                issuer_ticker=parsed.issuer_ticker or detail_metadata.issuer_ticker,
                form_type=filing.form_type,
                parsed=parsed,
                score_bundle=score_bundle,
            )
            filing.parser_status = "success"
            filing.scoring_status = "success"
            filing.summarization_status = "success"
            filing.score = score_bundle.score
            filing.confidence = score_bundle.confidence
            filing.reasons = score_bundle.reasons
            filing.summary_headline = summary.headline
            filing.summary_context = summary.context
            self._apply_optional_rewrite(
                filing=filing,
                rewrite_input=SummaryRewriteInput(
                    filing_accession=filing.accession_number,
                    form_type=filing.form_type,
                    issuer_name=parsed.issuer_name or detail_metadata.issuer_name,
                    issuer_ticker=parsed.issuer_ticker or detail_metadata.issuer_ticker,
                    deterministic_headline=summary.headline,
                    deterministic_context=summary.context,
                    score=score_bundle.score,
                    confidence=score_bundle.confidence,
                    reasons=score_bundle.reasons,
                    prompt_payload={
                        "reporter_names": parsed.reporter_names,
                        "owner_count": parsed.normalized_payload.get("owner_count"),
                        "transaction_codes": _form4_transaction_codes(parsed.normalized_payload),
                        "tenb5_1": _form4_tenb5_one_context(parsed.normalized_payload),
                    },
                ),
            )
            filing.normalized_payload = parsed.normalized_payload
            filing.reporter_names = parsed.reporter_names
            filing.issuer_cik = issuer_cik
            filing.issuer_name = parsed.issuer_name or detail_metadata.issuer_name
            filing.issuer_ticker = parsed.issuer_ticker or detail_metadata.issuer_ticker
            session.add(filing)
            session.flush()
            self._ensure_alert(session, filing, deliver=deliver and not force_reparse)
            session.commit()

    def _upsert_form4_filing(
        self,
        session,
        *,
        candidate: OwnershipCandidate,
        detail_metadata,
        parsed,
        xml_url: str,
        existing_filing_id: int | None,
    ) -> Filing:
        filing = None
        if existing_filing_id is not None:
            filing = session.get(Filing, existing_filing_id)
        if filing is None:
            filing = session.scalar(
                select(Filing).where(Filing.accession_number == candidate.accession_number)
            )
        if filing is None:
            filing = Filing(
                accession_number=candidate.accession_number,
                form_type=candidate.form_type,
            )
            session.add(filing)
            session.flush()

        filing.form_type = candidate.form_type
        filing.is_amendment = candidate.form_type.endswith("/A")
        filing.filed_date = detail_metadata.filed_date or candidate.filed_date
        filing.accepted_at = detail_metadata.accepted_at or candidate.accepted_at
        filing.detail_url = candidate.detail_url
        filing.source_url = xml_url
        filing.issuer_cik = normalize_cik(parsed.issuer_cik or detail_metadata.issuer_cik)
        filing.issuer_name = parsed.issuer_name or detail_metadata.issuer_name
        filing.issuer_ticker = parsed.issuer_ticker or detail_metadata.issuer_ticker
        filing.reporter_names = parsed.reporter_names
        session.add(filing)
        session.flush()
        return filing

    def _upsert_failed_form4_filing(
        self,
        *,
        candidate: OwnershipCandidate,
        detail_metadata,
        parser_message: str,
    ) -> int:
        with open_session() as session:
            filing = session.scalar(
                select(Filing).where(Filing.accession_number == candidate.accession_number)
            )
            if filing is None:
                filing = Filing(
                    accession_number=candidate.accession_number,
                    form_type=candidate.form_type,
                )
                session.add(filing)
                session.flush()
            filing.form_type = candidate.form_type
            filing.is_amendment = candidate.form_type.endswith("/A")
            filing.filed_date = detail_metadata.filed_date or candidate.filed_date
            filing.accepted_at = detail_metadata.accepted_at or candidate.accepted_at
            filing.detail_url = candidate.detail_url
            filing.source_url = None
            filing.issuer_cik = normalize_cik(detail_metadata.issuer_cik)
            filing.issuer_name = detail_metadata.issuer_name
            filing.issuer_ticker = detail_metadata.issuer_ticker
            filing.parser_status = "failed"
            filing.scoring_status = "skipped"
            filing.summarization_status = "skipped"
            filing.summary_headline = None
            filing.summary_context = None
            clear_openai_fields(filing)
            filing.normalized_payload = {
                "payload_version": 1,
                "parser_mode": "xml",
                "warnings": [parser_message],
            }
            session.add(filing)
            session.commit()
            return filing.id

    def _mark_filing_failed(self, filing_id: int) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            filing.parser_status = "failed"
            filing.scoring_status = "skipped"
            filing.summarization_status = "skipped"
            clear_openai_fields(filing)
            session.add(filing)
            session.commit()

    def _is_watched_issuer(self, issuer_cik: str) -> bool:
        normalized = normalize_cik(issuer_cik)
        if normalized is None:
            return False
        return normalized in self.watched_cik_set()

    def _current_cursor_tuple(
        self,
        source_name: str,
        filter_key: str,
    ) -> tuple[datetime, date, str] | None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == source_name,
                    SourceCursor.filter_key == filter_key,
                )
            )
            if cursor is None:
                return None
            accepted_at = _utc_or_min(cursor.accepted_at)
            filed_date = cursor.filed_date or datetime.min.date()
            accession_number = cursor.accession_number or ""
            return (accepted_at, filed_date, accession_number)

    def _mark_discovery_polled(self, source_name: str, filter_key: str) -> None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == source_name,
                    SourceCursor.filter_key == filter_key,
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name=source_name,
                    filter_key=filter_key,
                )
                session.add(cursor)
                session.flush()
            cursor.last_polled_at = datetime.now(UTC)
            session.add(cursor)
            session.commit()

    def _advance_cursor(
        self,
        candidate: OwnershipCandidate,
        source_name: str,
        filter_key: str,
    ) -> None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == source_name,
                    SourceCursor.filter_key == filter_key,
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name=source_name,
                    filter_key=filter_key,
                )
                session.add(cursor)
                session.flush()

            existing_tuple = (
                _utc_or_min(cursor.accepted_at),
                cursor.filed_date or datetime.min.date(),
                cursor.accession_number or "",
            )
            if candidate.cursor_tuple() > existing_tuple:
                cursor.accepted_at = candidate.accepted_at
                cursor.filed_date = candidate.filed_date
                cursor.accession_number = candidate.accession_number
            cursor.last_polled_at = datetime.now(UTC)
            session.add(cursor)
            session.commit()


class RecoveryService(BaseIngestService):
    def __init__(
        self,
        *,
        settings: Settings,
        broker: SecRequestBroker,
        sec_client,
        resolver: TickerResolver,
        eight_k_service: EightKIngestService,
        form4_service: Form4IngestService,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        super().__init__(
            settings=settings,
            resolver=resolver,
            alert_delivery=alert_delivery,
        )
        self.broker = broker
        self.sec_client = sec_client
        self.eight_k_service = eight_k_service
        self.form4_service = form4_service

    def queue_recent_repair(self, *, run_id: int | None, scheduled: bool) -> None:
        days = [day.isoformat() for day in previous_business_days(2)]
        self._mark_run_status(
            run_id,
            status="running",
            notes=f"Queued repair for {len(days)} business day(s).",
        )
        enqueue_result = self.broker.enqueue(
            task_name="repair-daily-index-chunk",
            priority=BrokerPriority.P3,
            job_key="repair:recent:step:0",
            source_name="repair-daily-master",
            payload={
                "run_id": run_id,
                "run_key": "repair:recent",
                "remaining_days": days,
                "current_day": None,
                "offset": 0,
                "scheduled": scheduled,
                "matched": 0,
                "enqueued": 0,
            },
        )
        if not enqueue_result.accepted:
            self._mark_run_status(
                run_id,
                status="completed",
                notes="Repair already queued or active.",
            )
            self.broker.finish_run("repair:recent")

    def queue_watchlist_backfill(
        self,
        *,
        entry_id: int,
        run_id: int | None,
        trigger: str,
    ) -> bool:
        run_key = f"backfill:watchlist:{entry_id}"
        if not self.broker.start_run(run_key):
            if run_id is not None:
                self._mark_run_status(
                    run_id,
                    status="completed",
                    notes="Backfill already queued or active.",
                )
            return False

        days = [day.isoformat() for day in backfill_business_days()]
        self._mark_run_status(
            run_id,
            status="running",
            notes=f"Queued 30-day backfill from trigger {trigger}.",
        )
        self.broker.enqueue(
            task_name="backfill-watchlist-chunk",
            priority=BrokerPriority.P3,
            job_key=f"backfill:watchlist:{entry_id}:step:0",
            source_name="watchlist-backfill",
            payload={
                "run_id": run_id,
                "run_key": run_key,
                "entry_id": entry_id,
                "remaining_days": days,
                "current_day": None,
                "offset": 0,
                "matched": 0,
                "enqueued": 0,
                "trigger": trigger,
            },
        )
        return True

    def process_repair_chunk(self, payload: dict[str, Any]) -> None:
        self._process_index_chunk(payload, mode="repair")

    def process_backfill_chunk(self, payload: dict[str, Any]) -> None:
        self._process_index_chunk(payload, mode="backfill")

    def _process_index_chunk(self, payload: dict[str, Any], *, mode: str) -> None:
        run_id = payload.get("run_id")
        run_key = payload["run_key"]
        remaining_days = [value for value in payload.get("remaining_days", []) if value]
        current_day = payload.get("current_day")
        offset = int(payload.get("offset", 0))
        matched = int(payload.get("matched", 0))
        enqueued = int(payload.get("enqueued", 0))
        entry_id = payload.get("entry_id")

        try:
            if mode == "backfill" and current_day is None and not remaining_days:
                remaining_days = [day.isoformat() for day in backfill_business_days()]

            if current_day is None:
                if not remaining_days:
                    self._mark_run_status(
                        run_id,
                        status="completed",
                        notes=f"matched={matched} enqueued={enqueued}",
                    )
                    self.broker.finish_run(run_key)
                    return
                current_day = remaining_days.pop(0)
                offset = 0

            watched_ciks = self._resolved_target_ciks(entry_id=entry_id)
            if not watched_ciks:
                self._mark_run_status(
                    run_id,
                    status="completed",
                    notes="No enabled/resolved watchlist targets remain.",
                )
                self.broker.finish_run(run_key)
                return

            day_value = date.fromisoformat(current_day)
            rows = self._load_filtered_index_rows(day_value)
            if mode == "backfill" and entry_id is not None:
                rows = [
                    row
                    for row in rows
                    if row.form_type in {"4", "4/A"} or row.cik in watched_ciks
                ]
            elif mode == "repair":
                rows = [
                    row
                    for row in rows
                    if row.form_type in {"4", "4/A"} or row.cik in watched_ciks
                ]

            chunk = rows[offset : offset + P3_CHUNK_SIZE]
            processed_in_chunk = 0
            for row in chunk:
                if processed_in_chunk > 0 and self.broker.has_queued_higher_priority_than(
                    BrokerPriority.P3
                ):
                    break
                matched += 1
                if self._process_index_row(row):
                    enqueued += 1
                processed_in_chunk += 1

            self._mark_run_status(
                run_id,
                status="running",
                notes=(
                    f"mode={mode} day={current_day} offset={offset + processed_in_chunk} "
                    f"matched={matched} enqueued={enqueued}"
                ),
            )
            self._mark_repair_day_progress(mode=mode, day=day_value)

            next_offset = offset + processed_in_chunk
            if next_offset < len(rows):
                self._enqueue_successor_chunk(
                    mode=mode,
                    run_id=run_id,
                    run_key=run_key,
                    entry_id=entry_id,
                    remaining_days=remaining_days,
                    current_day=current_day,
                    offset=next_offset,
                    matched=matched,
                    enqueued=enqueued,
                )
                return

            if remaining_days:
                self._enqueue_successor_chunk(
                    mode=mode,
                    run_id=run_id,
                    run_key=run_key,
                    entry_id=entry_id,
                    remaining_days=remaining_days,
                    current_day=None,
                    offset=0,
                    matched=matched,
                    enqueued=enqueued,
                )
                return

            self._mark_run_status(
                run_id,
                status="completed",
                notes=f"matched={matched} enqueued={enqueued}",
            )
            self.broker.finish_run(run_key)
        except Exception as exc:
            self._record_error(
                stage=f"{mode}_chunk",
                source_name="daily_master_index",
                filing_accession=None,
                error_class=exc.__class__.__name__,
                message=str(exc),
                is_retryable=True,
            )
            self._mark_run_status(run_id, status="failed", notes=str(exc))
            self.broker.finish_run(run_key)
            raise

    def _enqueue_successor_chunk(
        self,
        *,
        mode: str,
        run_id: int | None,
        run_key: str,
        entry_id: int | None,
        remaining_days: list[str],
        current_day: str | None,
        offset: int,
        matched: int,
        enqueued: int,
    ) -> None:
        task_name = "repair-daily-index-chunk" if mode == "repair" else "backfill-watchlist-chunk"
        job_prefix = "repair:recent" if mode == "repair" else f"backfill:watchlist:{entry_id}"
        if current_day is None:
            next_day = remaining_days[0] if remaining_days else "done"
            next_job_key = f"{job_prefix}:next-day:{next_day}"
        else:
            next_job_key = f"{job_prefix}:{current_day}:offset:{offset}"
        self.broker.enqueue(
            task_name=task_name,
            priority=BrokerPriority.P3,
            job_key=next_job_key,
            source_name="daily_master_index",
            payload={
                "run_id": run_id,
                "run_key": run_key,
                "entry_id": entry_id,
                "remaining_days": remaining_days,
                "current_day": current_day,
                "offset": offset,
                "matched": matched,
                "enqueued": enqueued,
            },
        )

    def _resolved_target_ciks(self, *, entry_id: int | None) -> set[str]:
        if entry_id is None:
            return self.watched_cik_set()
        with open_session() as session:
            entry = session.get(WatchlistEntry, entry_id)
        if entry is None or not entry.enabled:
            return set()
        resolved = self.resolver.resolve(entry)
        if resolved is None:
            self._record_error(
                stage="backfill_resolver",
                source_name="company_tickers_json",
                filing_accession=None,
                error_class="ResolutionError",
                message=f"Unable to resolve issuer CIK for {entry.ticker}.",
            )
            return set()
        self._persist_resolution(entry.id, resolved)
        return {resolved.issuer_cik}

    def _load_filtered_index_rows(self, day_value: date) -> list[MasterIndexRow]:
        from app.services.sec.indexes import daily_master_index_url, parse_master_index

        text = self.sec_client.get_text(daily_master_index_url(day_value))
        rows = parse_master_index(text)
        return [row for row in rows if row.form_type in REPAIR_FORM_TYPES]

    def _process_index_row(self, row: MasterIndexRow) -> bool:
        if row.form_type in {"8-K", "8-K/A"}:
            target = EightKFilingTarget.from_index_row(row)
            if target is None:
                return False
            return self.eight_k_service.register_discovered_filing(
                target,
                enqueue_processing=True,
                deliver=True,
            ) is not None

        detail_url = row.detail_index_url
        accession_number = row.accession_number
        if detail_url is None or accession_number is None:
            return False
        candidate = OwnershipCandidate(
            accession_number=accession_number,
            form_type=row.form_type,
            detail_url=detail_url,
            filed_date=row.filed_date,
            accepted_at=None,
        )
        return self.form4_service.enqueue_form4_candidate(
            candidate,
            source_name=None,
            filter_key=None,
        )

    def _mark_repair_day_progress(self, *, mode: str, day: date) -> None:
        if mode != "repair":
            return
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == REPAIR_CURSOR_SOURCE,
                    SourceCursor.filter_key == f"date:{day.isoformat()}",
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name=REPAIR_CURSOR_SOURCE,
                    filter_key=f"date:{day.isoformat()}",
                )
                session.add(cursor)
                session.flush()
            cursor.last_polled_at = datetime.now(UTC)
            cursor.filed_date = day
            session.add(cursor)
            session.commit()
