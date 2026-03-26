from __future__ import annotations

import logging
from datetime import UTC, date, datetime

from sqlalchemy import select

from app.db import open_session
from app.models import Filing, IngestRun, SourceCursor, StageError, WatchlistEntry
from app.services.alerts import AlertDeliveryService
from app.services.broker import BrokerPriority, SecRequestBroker
from app.services.scoring.eight_k import EightKScorer
from app.services.scoring.form4 import Form4Scorer
from app.services.sec.eight_k import EightKParser
from app.services.sec.form4 import Form4Parser, locate_ownership_xml, parse_form4_detail_page
from app.services.sec.latest_ownership import (
    INGESTIBLE_FORM4_TYPES,
    LATEST_OWNERSHIP_URL,
    OWNERSHIP_DISCOVERY_FILTER_KEY,
    OwnershipCandidate,
    parse_ownership_candidates,
)
from app.services.sec.resolver import ResolvedIssuer, TickerResolver, normalize_cik
from app.services.sec.submissions import SubmissionFiling, parse_recent_8k_filings, submissions_url
from app.services.summarize.deterministic import DeterministicEightKSummarizer
from app.services.summarize.form4 import DeterministicForm4Summarizer

logger = logging.getLogger(__name__)


class BaseIngestService:
    def __init__(
        self,
        *,
        resolver: TickerResolver,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        self.resolver = resolver
        self.alert_delivery = alert_delivery

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
    ) -> None:
        with open_session() as session:
            session.add(
                StageError(
                    stage=stage,
                    source_name=source_name,
                    filing_accession=filing_accession,
                    error_class=error_class,
                    message=message,
                    is_retryable=False,
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

    def _ensure_alert_for_filing_id(self, filing_id: int, *, deliver: bool) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            self._ensure_alert(session, filing, deliver=deliver)
            session.commit()

    def _ensure_alert(self, session, filing: Filing, *, deliver: bool) -> None:
        alert, created = self.alert_delivery.ensure_alert(session, filing)
        alert.headline = filing.summary_headline
        session.add(alert)
        session.flush()
        if deliver and created:
            self.alert_delivery.deliver_slack_once(session, filing, alert)


class EightKIngestService(BaseIngestService):
    def __init__(
        self,
        *,
        sec_client,
        resolver: TickerResolver,
        parser: EightKParser,
        scorer: EightKScorer,
        summarizer: DeterministicEightKSummarizer,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        super().__init__(resolver=resolver, alert_delivery=alert_delivery)
        self.sec_client = sec_client
        self.parser = parser
        self.scorer = scorer
        self.summarizer = summarizer

    def run_manual_ingest(self, run_id: int | None = None) -> None:
        self._mark_run_status(run_id, status="running")
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
                    self._ingest_watchlist_entry(resolved)
                except Exception as exc:
                    self._record_error(
                        stage="manual_ingest",
                        source_name="submissions",
                        filing_accession=None,
                        error_class=exc.__class__.__name__,
                        message=str(exc),
                    )
            self._mark_run_status(run_id, status="completed")
        except Exception as exc:
            self._mark_run_status(run_id, status="failed", notes=str(exc))
            raise

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

    def _ingest_watchlist_entry(self, resolved: ResolvedIssuer) -> None:
        payload = self.sec_client.get_json(submissions_url(resolved.issuer_cik))
        candidates = parse_recent_8k_filings(payload, issuer_cik=resolved.issuer_cik)
        for candidate in candidates:
            try:
                with open_session() as session:
                    filing = self._upsert_filing(session, candidate)
                    filing_id = filing.id
                    should_process = (
                        filing.parser_status != "success" or filing.summary_headline is None
                    )
                    session.commit()

                if should_process:
                    self._process_filing_id(filing_id, force_reparse=False, deliver=True)
                else:
                    self._ensure_alert_for_filing_id(filing_id, deliver=False)
            except Exception as exc:
                self._record_error(
                    stage="ingest_submission",
                    source_name="submissions",
                    filing_accession=candidate.accession_number,
                    error_class=exc.__class__.__name__,
                    message=str(exc),
                )

    def _upsert_filing(self, session, candidate: SubmissionFiling) -> Filing:
        filing = session.scalar(
            select(Filing).where(Filing.accession_number == candidate.accession_number)
        )
        if filing is None:
            filing = Filing(
                accession_number=candidate.accession_number,
                form_type=candidate.form_type,
                is_amendment=candidate.form_type.endswith("/A"),
                filed_date=candidate.filed_date,
                accepted_at=candidate.accepted_at,
                issuer_cik=candidate.issuer_cik,
                issuer_ticker=candidate.issuer_ticker,
                issuer_name=candidate.issuer_name,
                detail_url=candidate.detail_index_url,
                source_url=candidate.primary_document_url,
                parser_status="pending",
                scoring_status="pending",
                summarization_status="pending",
            )
            session.add(filing)
            session.flush()
            return filing

        filing.form_type = candidate.form_type
        filing.is_amendment = candidate.form_type.endswith("/A")
        filing.filed_date = candidate.filed_date
        filing.accepted_at = candidate.accepted_at
        filing.issuer_cik = candidate.issuer_cik
        filing.issuer_ticker = candidate.issuer_ticker
        filing.issuer_name = candidate.issuer_name
        filing.detail_url = candidate.detail_index_url
        filing.source_url = candidate.primary_document_url
        session.add(filing)
        session.flush()
        return filing

    def _process_filing_id(self, filing_id: int, *, force_reparse: bool, deliver: bool) -> None:
        with open_session() as session:
            filing = session.get(Filing, filing_id)
            if filing is None:
                return
            detail_url = filing.detail_url
            source_url = filing.source_url
            accession_number = filing.accession_number

        if not detail_url or not source_url:
            self._record_error(
                stage="detail_fetch",
                source_name="submissions",
                filing_accession=accession_number,
                error_class="MissingUrlError",
                message="Detail or primary document URL missing for filing.",
            )
            return

        detail_html = self.sec_client.get_text(detail_url)
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
            session.add(filing)
            session.flush()

            self._ensure_alert(session, filing, deliver=deliver and not force_reparse)
            session.commit()


class Form4IngestService(BaseIngestService):
    def __init__(
        self,
        *,
        broker: SecRequestBroker,
        sec_client,
        resolver: TickerResolver,
        parser: Form4Parser,
        scorer: Form4Scorer,
        summarizer: DeterministicForm4Summarizer,
        alert_delivery: AlertDeliveryService,
    ) -> None:
        super().__init__(resolver=resolver, alert_delivery=alert_delivery)
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

            feed_xml = self.sec_client.get_text(LATEST_OWNERSHIP_URL)
            candidates = parse_ownership_candidates(feed_xml)
            self.broker.mark_source_success("latest_ownership")
            self._mark_discovery_polled()

            cursor_tuple = self._current_cursor_tuple()
            older_candidates = 0
            enqueued = 0
            for candidate in candidates:
                if candidate.form_type not in INGESTIBLE_FORM4_TYPES:
                    continue
                if cursor_tuple is not None and candidate.cursor_tuple() <= cursor_tuple:
                    older_candidates += 1
                    if older_candidates >= 10:
                        break
                    continue
                older_candidates = 0
                enqueue_result = self.broker.enqueue(
                    task_name="process-form4-accession",
                    priority=BrokerPriority.P1,
                    job_key=f"form4-accession:{candidate.accession_number}",
                    source_name="latest_ownership",
                    payload=candidate.to_payload(),
                )
                if enqueue_result.accepted:
                    enqueued += 1

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

    def process_accession(self, payload: dict[str, str | None]) -> None:
        candidate = OwnershipCandidate.from_payload(payload)
        accession = candidate.accession_number
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
            self._advance_cursor(confirmed)

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
            session.add(filing)
            session.commit()

    def _is_watched_issuer(self, issuer_cik: str) -> bool:
        with open_session() as session:
            entries = session.scalars(
                select(WatchlistEntry).where(WatchlistEntry.enabled.is_(True))
            ).all()
        normalized = normalize_cik(issuer_cik)
        if normalized is None:
            return False
        for entry in entries:
            if normalize_cik(entry.manual_cik_override) == normalized:
                return True
            if normalize_cik(entry.issuer_cik) == normalized:
                return True
        return False

    def _current_cursor_tuple(self) -> tuple[datetime, date, str] | None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == "latest_ownership",
                    SourceCursor.filter_key == OWNERSHIP_DISCOVERY_FILTER_KEY,
                )
            )
            if cursor is None:
                return None
            accepted_at = cursor.accepted_at or datetime.min.replace(tzinfo=UTC)
            filed_date = cursor.filed_date or datetime.min.date()
            accession_number = cursor.accession_number or ""
            return (accepted_at, filed_date, accession_number)

    def _mark_discovery_polled(self) -> None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == "latest_ownership",
                    SourceCursor.filter_key == OWNERSHIP_DISCOVERY_FILTER_KEY,
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name="latest_ownership",
                    filter_key=OWNERSHIP_DISCOVERY_FILTER_KEY,
                )
                session.add(cursor)
                session.flush()
            cursor.last_polled_at = datetime.now(UTC)
            session.add(cursor)
            session.commit()

    def _advance_cursor(self, candidate: OwnershipCandidate) -> None:
        with open_session() as session:
            cursor = session.scalar(
                select(SourceCursor).where(
                    SourceCursor.source_name == "latest_ownership",
                    SourceCursor.filter_key == OWNERSHIP_DISCOVERY_FILTER_KEY,
                )
            )
            if cursor is None:
                cursor = SourceCursor(
                    source_name="latest_ownership",
                    filter_key=OWNERSHIP_DISCOVERY_FILTER_KEY,
                )
                session.add(cursor)
                session.flush()

            existing_tuple = (
                cursor.accepted_at or datetime.min.replace(tzinfo=UTC),
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
