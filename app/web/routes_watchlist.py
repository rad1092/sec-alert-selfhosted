from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import WatchlistEntry
from app.security import flash, validate_csrf
from app.web.helpers import render_template

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("")
def list_watchlist(request: Request, session: Session = Depends(get_session)):
    entries = session.scalars(
        select(WatchlistEntry).order_by(WatchlistEntry.created_at.desc())
    ).all()
    settings = request.app.state.settings
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
        entry.enabled = not entry.enabled
        session.add(entry)
        session.commit()
        flash(request, "success", f"Updated {entry.ticker}.")
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
