from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import Filing, IngestRun, WatchlistEntry
from app.security import flash, validate_csrf
from app.services.broker import BrokerPriority
from app.web.helpers import render_template

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("")
def list_watchlist(request: Request, session: Session = Depends(get_session)):
    stored_entries = session.scalars(
        select(WatchlistEntry).order_by(WatchlistEntry.created_at.desc())
    ).all()
    settings = request.app.state.settings
    entries = []
    for entry in stored_entries:
        resolved_cik = entry.manual_cik_override or entry.issuer_cik
        filing_filters = [Filing.issuer_ticker == entry.ticker]
        if resolved_cik:
            filing_filters.append(Filing.issuer_cik == resolved_cik)
        last_filing_date = session.scalar(
            select(func.max(Filing.filed_date)).where(or_(*filing_filters))
        )
        latest_backfill_run = session.scalar(
            select(IngestRun)
            .where(IngestRun.run_key.like(f"backfill:watchlist:{entry.id}:%"))
            .order_by(IngestRun.created_at.desc())
            .limit(1)
        )
        hints: list[str] = []
        hints.append("Enabled" if entry.enabled else "Paused")
        if entry.manual_cik_override:
            hints.append("Manual CIK override active")
        if latest_backfill_run and latest_backfill_run.status in {"queued", "running"}:
            hints.append("30-day backfill likely running")
        elif latest_backfill_run and latest_backfill_run.status == "completed":
            hints.append("30-day backfill completed")
        if last_filing_date is None:
            hints.append("No filings yet")
        entries.append(
            {
                "id": entry.id,
                "ticker": entry.ticker,
                "issuer_display": entry.issuer_name or entry.ticker,
                "resolved_cik": resolved_cik,
                "enabled": entry.enabled,
                "last_filing_date": (
                    last_filing_date.isoformat() if last_filing_date else "No filings yet"
                ),
                "hint_summary": " • ".join(hints),
                "backfill_status": (
                    latest_backfill_run.status if latest_backfill_run else "not started"
                ),
                "manual_override": entry.manual_cik_override,
            }
        )
    return render_template(
        request,
        "watchlist.html",
        page_title="Watchlist",
        entries=entries,
        watchlist_soft_cap=settings.watchlist_soft_cap,
        watchlist_hard_cap=settings.watchlist_hard_cap,
    )


@router.post("")
async def create_watchlist_entry(
    request: Request,
    ticker: str = Form(...),
    issuer_cik: str = Form(default=""),
    manual_cik_override: str = Form(default=""),
    issuer_name: str = Form(default=""),
    enabled: bool = Form(default=False),
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    settings = request.app.state.settings
    existing_count = len(session.scalars(select(WatchlistEntry)).all())
    if existing_count >= settings.watchlist_hard_cap:
        flash(
            request,
            "error",
            f"Watchlist hard cap reached ({settings.watchlist_hard_cap}).",
        )
        return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)

    normalized_ticker = ticker.strip().upper()
    if not normalized_ticker:
        flash(request, "error", "Ticker is required.")
        return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)

    entry = WatchlistEntry(
        ticker=normalized_ticker,
        issuer_cik=issuer_cik.strip() or None,
        manual_cik_override=manual_cik_override.strip() or None,
        issuer_name=issuer_name.strip() or None,
        enabled=enabled,
    )
    session.add(entry)
    session.commit()
    if entry.enabled:
        _queue_backfill_run(
            request=request,
            session=session,
            entry_id=entry.id,
            trigger="watchlist_create",
        )

    if existing_count + 1 >= settings.watchlist_soft_cap:
        flash(
            request,
            "warning",
            f"Watchlist has reached the validated limit of {settings.watchlist_soft_cap} issuers.",
        )
    else:
        flash(request, "success", f"Added {normalized_ticker} to the watchlist.")
    return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{entry_id}/toggle")
async def toggle_watchlist_entry(
    request: Request,
    entry_id: int,
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    entry = session.get(WatchlistEntry, entry_id)
    if entry is not None:
        was_enabled = entry.enabled
        entry.enabled = not entry.enabled
        session.add(entry)
        session.commit()
        if not was_enabled and entry.enabled:
            _queue_backfill_run(
                request=request,
                session=session,
                entry_id=entry.id,
                trigger="watchlist_enable",
            )
        flash(request, "success", f"Updated {entry.ticker}.")
    return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{entry_id}/backfill-now")
async def backfill_watchlist_entry(
    request: Request,
    entry_id: int,
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    entry = session.get(WatchlistEntry, entry_id)
    if entry is None:
        return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)

    queued = _queue_backfill_run(
        request=request,
        session=session,
        entry_id=entry.id,
        trigger="manual_backfill",
    )
    if queued:
        flash(request, "success", f"Queued a backfill run for {entry.ticker}.")
    else:
        flash(request, "warning", f"A backfill run is already queued for {entry.ticker}.")
    return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{entry_id}/delete")
async def delete_watchlist_entry(
    request: Request,
    entry_id: int,
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    entry = session.get(WatchlistEntry, entry_id)
    if entry is not None:
        ticker = entry.ticker
        session.delete(entry)
        session.commit()
        flash(request, "success", f"Deleted {ticker} from the watchlist.")
    return RedirectResponse("/watchlist", status_code=status.HTTP_303_SEE_OTHER)


def _queue_backfill_run(
    *,
    request: Request,
    session: Session,
    entry_id: int,
    trigger: str,
) -> bool:
    broker = request.app.state.broker
    run_key = f"backfill:watchlist:{entry_id}"
    if not broker.start_run(run_key):
        return False

    run = IngestRun(run_key="", triggered_by=trigger, status="queued")
    session.add(run)
    session.flush()
    run.run_key = f"{run_key}:{run.id}"
    session.add(run)
    enqueue_result = broker.enqueue(
        task_name="backfill-watchlist-chunk",
        priority=BrokerPriority.P3,
        job_key=f"backfill:watchlist:{entry_id}:step:0",
        source_name="watchlist-backfill",
        payload={
            "run_id": run.id,
            "run_key": run_key,
            "entry_id": entry_id,
            "remaining_days": [],
            "current_day": None,
            "offset": 0,
            "matched": 0,
            "enqueued": 0,
            "trigger": trigger,
        },
    )
    if not enqueue_result.accepted:
        broker.finish_run(run_key)
        session.delete(run)
        session.commit()
        return False
    session.commit()
    return True
