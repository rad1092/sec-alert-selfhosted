from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Alert, Destination, Filing, IngestRun, StageError, WatchlistEntry
from app.release import summarize_diagnostics
from app.services.summarize.base import effective_summary_for_filing
from app.web.helpers import render_template

router = APIRouter()

HISTORICAL_WINDOW_DAYS = 7
ERROR_TITLES = {
    "manual_ingest": "A live filing could not be imported cleanly.",
    "reparse": "A saved filing could not be reparsed cleanly.",
    "form4_xml_locator": "The SEC detail page did not expose a usable ownership XML document.",
    "form4_parser": "A Form 4 was found, but the ownership XML could not be parsed.",
    "repair": "A recent repair sweep hit a filing it could not finish.",
    "backfill": "Historical catch-up work hit a filing it could not finish.",
}
ERROR_HELP = {
    "manual_ingest": "New signals may be delayed until the next manual run or repair sweep.",
    "reparse": "The saved filing remains available, but refreshed summary details may lag.",
    "form4_xml_locator": "This usually affects a specific filing rather than the whole watchlist.",
    "form4_parser": (
        "The filing was discovered, but investor-facing details could not be extracted yet."
    ),
    "repair": "Recent missed filings may need another repair pass once the blocking issue is gone.",
    "backfill": "Older historical signals may be incomplete until the backfill is rerun.",
}
RUN_LABELS = {
    "manual_8k": "Manual 8-K check",
    "manual_form4": "Manual Form 4 check",
    "repair": "Recent repair",
    "manual_backfill": "Manual watchlist backfill",
    "watchlist_create": "New watchlist catch-up",
    "watchlist_enable": "Re-enabled watchlist catch-up",
}


def _snapshot_value(snapshot: object, key: str, default=None):
    if isinstance(snapshot, dict):
        return snapshot.get(key, default)
    return getattr(snapshot, key, default)


def _normalize_broker_snapshot(snapshot: object) -> dict[str, object]:
    queued = _snapshot_value(snapshot, "queued_by_priority", {}) or {}
    if not isinstance(queued, dict):
        queued = {
            "P1": getattr(queued, "P1", 0),
            "P2": getattr(queued, "P2", 0),
            "P3": getattr(queued, "P3", 0),
        }
    return {
        "backlog_size": _snapshot_value(snapshot, "backlog_size", 0),
        "oldest_queued_age_seconds": _snapshot_value(snapshot, "oldest_queued_age_seconds", 0),
        "queued_by_priority": queued,
        "recent_403_count": _snapshot_value(snapshot, "recent_403_count", 0),
        "recent_429_count": _snapshot_value(snapshot, "recent_429_count", 0),
    }


def _normalize_scheduler_snapshot(snapshot: object) -> dict[str, object]:
    return {
        "enabled": _snapshot_value(snapshot, "enabled", False),
        "started": _snapshot_value(snapshot, "started", False),
        "jobs": _snapshot_value(snapshot, "jobs", []) or [],
        "last_enqueued_at": _snapshot_value(snapshot, "last_enqueued_at", {}) or {},
    }


def _normalize_worker_snapshot(snapshot: object) -> dict[str, object]:
    return {
        "started": _snapshot_value(snapshot, "started", False),
        "idle": _snapshot_value(snapshot, "idle", True),
        "active_job_key": _snapshot_value(snapshot, "active_job_key", None),
    }


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "Not yet"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_date(value: date | None) -> str:
    if value is None:
        return "-"
    return value.isoformat()


def _relative_time(value: datetime | None) -> str:
    if value is None:
        return "Not yet"
    delta = datetime.now(UTC) - value.astimezone(UTC)
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _is_historical(filed_date: date | None) -> bool:
    if filed_date is None:
        return False
    return filed_date < (_today_utc() - timedelta(days=HISTORICAL_WINDOW_DAYS))


def _direction_for_score(score: float | None) -> str:
    if score is None:
        return "pending"
    if score > 0:
        return "positive"
    if score < 0:
        return "negative"
    return "neutral"


def _direction_label(direction: str) -> str:
    labels = {
        "positive": "Positive",
        "negative": "Negative",
        "neutral": "Neutral",
        "pending": "Pending",
    }
    return labels.get(direction, direction.title())


def _delivery_label(status: str | None) -> str:
    mapping = {
        "delivered": "Delivered",
        "delivery_failed": "Delivery failed",
        "skipped": "Saved locally",
        "pending": "Pending",
    }
    return mapping.get(status or "", status or "-")


def _headline_for_alert(alert: Alert, filing: Filing | None) -> str:
    if alert.headline:
        return alert.headline
    if filing is not None:
        effective = effective_summary_for_filing(filing)
        if effective.headline:
            return effective.headline
    return "Untitled signal"


def _build_alert_rows(session: Session) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    alert_pairs = session.execute(
        select(Alert, Filing)
        .outerjoin(Filing, Alert.filing_id == Filing.id)
        .order_by(Alert.created_at.desc())
        .limit(300)
    ).all()
    for alert, filing in alert_pairs:
        score = filing.score if filing is not None else None
        direction = _direction_for_score(score)
        filed_date = filing.filed_date if filing is not None else None
        ticker = "-"
        issuer_name = None
        form_type = "-"
        summary_source = "deterministic"
        if filing is not None:
            ticker = filing.issuer_ticker or filing.issuer_name or "-"
            issuer_name = filing.issuer_name
            form_type = filing.form_type
            summary_source = effective_summary_for_filing(filing).source
        rows.append(
            {
                "alert": alert,
                "filing": filing,
                "filing_id": filing.id if filing is not None else None,
                "ticker": ticker,
                "issuer_name": issuer_name,
                "form_type": form_type,
                "filed_date": filed_date,
                "filed_date_display": _format_date(filed_date),
                "direction": direction,
                "direction_label": _direction_label(direction),
                "score_display": "-" if score is None else f"{score:.1f}",
                "confidence": (
                    filing.confidence
                    if filing is not None and filing.confidence
                    else "-"
                ),
                "headline": _headline_for_alert(alert, filing),
                "created_at_display": _format_dt(alert.created_at),
                "delivery_label": _delivery_label(alert.status),
                "historical": _is_historical(filed_date),
                "summary_source": summary_source,
                "status": alert.status,
            }
        )
    return rows


def _filter_alert_rows(
    rows: list[dict[str, object]],
    *,
    ticker: str | None,
    form_type: str | None,
    direction: str | None,
    historical: str,
) -> tuple[list[dict[str, object]], int]:
    normalized_ticker = (ticker or "").strip().upper()
    normalized_form = (form_type or "").strip().upper()
    normalized_direction = (direction or "").strip().lower()
    filtered: list[dict[str, object]] = []
    hidden_historical = 0
    for row in rows:
        row_ticker = str(row["ticker"] or "").upper()
        row_form = str(row["form_type"] or "").upper()
        row_direction = str(row["direction"] or "").lower()
        if normalized_ticker and normalized_ticker not in row_ticker:
            continue
        if normalized_form and normalized_form != row_form:
            continue
        if normalized_direction and normalized_direction != row_direction:
            continue
        if historical != "show" and bool(row["historical"]):
            hidden_historical += 1
            continue
        filtered.append(row)
    return filtered, hidden_historical


def _run_label(triggered_by: str) -> str:
    return RUN_LABELS.get(triggered_by, triggered_by.replace("_", " ").title())


def _summarize_run(run: IngestRun) -> dict[str, str]:
    status = run.status.replace("_", " ").title()
    note = run.notes or "No extra notes recorded."
    finished_at = run.finished_at or run.updated_at
    return {
        "label": _run_label(run.triggered_by),
        "status": status,
        "finished_at_display": _format_dt(finished_at),
        "finished_at_relative": _relative_time(finished_at),
        "note": note,
    }


def _summarize_error(error: StageError) -> dict[str, str | bool]:
    title = ERROR_TITLES.get(error.stage, error.message)
    help_text = ERROR_HELP.get(
        error.stage,
        "The filing pipeline kept running, but this one item needs operator attention.",
    )
    filing_bits = []
    if error.filing_accession:
        filing_bits.append(error.filing_accession)
    if error.source_name:
        filing_bits.append(error.source_name)
    source_label = " • ".join(filing_bits) if filing_bits else "General pipeline issue"
    return {
        "title": title,
        "detail": error.message,
        "help_text": help_text,
        "source_label": source_label,
        "stage": error.stage,
        "retryable": error.is_retryable,
        "created_at_display": _format_dt(error.created_at),
    }


def _destination_summary(request: Request, destinations: list[Destination]) -> tuple[int, int]:
    configured_enabled = 0
    enabled = 0
    notifiers = {
        "slack": request.app.state.slack_notifier,
        "webhook": request.app.state.webhook_notifier,
        "smtp": request.app.state.smtp_notifier,
    }
    for destination in destinations:
        if not destination.enabled:
            continue
        enabled += 1
        notifier = notifiers.get(destination.destination_type)
        if notifier is not None and notifier.is_configured():
            configured_enabled += 1
    return enabled, configured_enabled


@router.get("/")
def inbox(request: Request, session: Session = Depends(get_session)):
    watchlist_count = session.scalar(select(func.count()).select_from(WatchlistEntry)) or 0
    filings_count = session.scalar(select(func.count()).select_from(Filing)) or 0
    alerts_count = session.scalar(select(func.count()).select_from(Alert)) or 0
    destinations = session.scalars(
        select(Destination).order_by(Destination.created_at.desc())
    ).all()
    enabled_destinations, configured_destinations = _destination_summary(request, destinations)

    alert_rows = _build_alert_rows(session)
    visible_signal_rows, hidden_historical_count = _filter_alert_rows(
        alert_rows,
        ticker=None,
        form_type=None,
        direction=None,
        historical="hide",
    )
    recent_errors = session.scalars(
        select(StageError).order_by(StageError.created_at.desc()).limit(5)
    ).all()
    recent_runs = session.scalars(
        select(IngestRun).order_by(IngestRun.created_at.desc()).limit(6)
    ).all()
    latest_completed_run = session.scalar(
        select(IngestRun)
        .where(IngestRun.status == "completed")
        .order_by(IngestRun.finished_at.desc(), IngestRun.updated_at.desc())
        .limit(1)
    )

    broker_snapshot = _normalize_broker_snapshot(request.app.state.broker.snapshot())
    scheduler_snapshot = _normalize_scheduler_snapshot(request.app.state.scheduler.snapshot())
    worker_snapshot = _normalize_worker_snapshot(request.app.state.worker.snapshot())
    background_work_active = broker_snapshot["backlog_size"] > 0 or not worker_snapshot["idle"]
    scheduler_mode = (
        "Automatic polling on"
        if scheduler_snapshot["enabled"]
        else "Manual-only checks"
    )

    zero_state = None
    if watchlist_count == 0:
        zero_state = "no_watchlist"
    elif filings_count == 0 and (recent_errors or recent_runs):
        zero_state = "waiting_with_activity"
    elif filings_count == 0:
        zero_state = "waiting_for_first_signal"

    return render_template(
        request,
        "dashboard.html",
        page_title="Inbox",
        watchlist_count=watchlist_count,
        alerts_count=alerts_count,
        filings_count=filings_count,
        visible_signal_rows=visible_signal_rows[:8],
        hidden_historical_count=hidden_historical_count,
        recent_error_rows=[_summarize_error(error) for error in recent_errors],
        recent_run_rows=[_summarize_run(run) for run in recent_runs],
        latest_completed_run=_summarize_run(latest_completed_run) if latest_completed_run else None,
        enabled_destinations=enabled_destinations,
        configured_destinations=configured_destinations,
        no_external_notifications=enabled_destinations == 0 or configured_destinations == 0,
        background_work_active=background_work_active,
        scheduler_mode=scheduler_mode,
        broker_snapshot=broker_snapshot,
        scheduler_snapshot=scheduler_snapshot,
        worker_snapshot=worker_snapshot,
        zero_state=zero_state,
    )


@router.get("/alerts")
def recent_alerts(
    request: Request,
    ticker: str | None = Query(default=None),
    form: str | None = Query(default=None),
    direction: str | None = Query(default=None),
    historical: str = Query(default="hide"),
    session: Session = Depends(get_session),
):
    alert_rows = _build_alert_rows(session)
    filtered_rows, hidden_historical_count = _filter_alert_rows(
        alert_rows,
        ticker=ticker,
        form_type=form,
        direction=direction,
        historical=historical,
    )
    return render_template(
        request,
        "alerts.html",
        page_title="All Signals",
        alerts=filtered_rows[:100],
        hidden_historical_count=hidden_historical_count,
        filter_ticker=(ticker or "").strip().upper(),
        filter_form=(form or "").strip().upper(),
        filter_direction=(direction or "").strip().lower(),
        filter_historical=historical,
    )


@router.get("/errors")
def recent_errors(request: Request, session: Session = Depends(get_session)):
    errors = session.scalars(
        select(StageError).order_by(StageError.created_at.desc()).limit(30)
    ).all()
    return render_template(
        request,
        "errors.html",
        page_title="Recent Issues",
        errors=[_summarize_error(error) for error in errors],
    )


@router.get("/advanced")
def advanced(request: Request, session: Session = Depends(get_session)):
    recent_runs = session.scalars(
        select(IngestRun).order_by(IngestRun.created_at.desc()).limit(8)
    ).all()
    recent_errors = session.scalars(
        select(StageError).order_by(StageError.created_at.desc()).limit(8)
    ).all()
    settings = request.app.state.settings
    runtime_reference = {
        "APP_HOST": settings.app_host,
        "APP_PORT": settings.app_port,
        "SCHEDULER_ENABLED": settings.scheduler_enabled,
        "SEC_POLL_INTERVAL_SECONDS": settings.sec_poll_interval_seconds,
        "SEC_RATE_LIMIT_RPS": settings.sec_rate_limit_rps,
        "SEC_LIVE_8K_OVERLAP_ROWS": settings.sec_live_8k_overlap_rows,
        "OPENAI_MODEL": settings.openai_model or "Not configured",
        "OPENAI_API_KEY": "Configured" if settings.openai_api_key else "Not configured",
    }
    release_info = request.app.state.release_info
    diagnostics = request.app.state.release_diagnostics
    return render_template(
        request,
        "advanced.html",
        page_title="Advanced",
        broker_snapshot=_normalize_broker_snapshot(request.app.state.broker.snapshot()),
        scheduler_snapshot=_normalize_scheduler_snapshot(request.app.state.scheduler.snapshot()),
        worker_snapshot=_normalize_worker_snapshot(request.app.state.worker.snapshot()),
        recent_run_rows=[_summarize_run(run) for run in recent_runs],
        recent_error_rows=[_summarize_error(error) for error in recent_errors],
        runtime_reference=runtime_reference,
        release_info=release_info,
        release_diagnostics=diagnostics,
        release_diagnostics_summary=summarize_diagnostics(diagnostics),
    )
