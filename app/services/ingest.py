from __future__ import annotations

import logging

from sqlalchemy import select

from app.db import open_session
from app.models import Filing, IngestRun, StageError, WatchlistEntry
from app.services.alerts import AlertDeliveryService
from app.services.scoring.eight_k import EightKScorer
from app.services.sec.eight_k import EightKParser
from app.services.sec.resolver import ResolvedIssuer, TickerResolver
from app.services.sec.submissions import SubmissionFiling, parse_recent_8k_filings, submissions_url
from app.services.summarize.deterministic import DeterministicEightKSummarizer

logger = logging.getLogger(__name__)


class EightKIngestService:
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
        self.sec_client = sec_client
        self.resolver = resolver
        self.parser = parser
        self.scorer = scorer
        self.summarizer = summarizer
        self.alert_delivery = alert_delivery

    def run_manual_ingest(self, run_id: int | None = None) -> None:
        with open_session() as session:
            run = session.get(IngestRun, run_id) if run_id is not None else None
            if run is not None:
                run.status = "running"
                session.add(run)
                session.commit()

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

            if run_id is not None:
                with open_session() as session:
                    run = session.get(IngestRun, run_id)
                    if run is not None:
                        run.status = "completed"
                        session.add(run)
                        session.commit()
        except Exception as exc:
            if run_id is not None:
                with open_session() as session:
                    run = session.get(IngestRun, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.notes = str(exc)
                        session.add(run)
                        session.commit()
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
