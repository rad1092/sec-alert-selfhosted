from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Alert, Destination, StageError, WatchlistEntry
from app.web.helpers import render_template

router = APIRouter()


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    watchlist_count = session.scalar(select(func.count()).select_from(WatchlistEntry)) or 0
    alerts_count = session.scalar(select(func.count()).select_from(Alert)) or 0
    errors_count = session.scalar(select(func.count()).select_from(StageError)) or 0
    destinations_count = session.scalar(select(func.count()).select_from(Destination)) or 0

    return render_template(
        request,
        "dashboard.html",
        page_title="Dashboard",
        watchlist_count=watchlist_count,
        alerts_count=alerts_count,
        errors_count=errors_count,
        destinations_count=destinations_count,
        broker_snapshot=request.app.state.broker.snapshot(),
        scheduler_snapshot=request.app.state.scheduler.snapshot(),
    )


@router.get("/alerts")
def recent_alerts(request: Request, session: Session = Depends(get_session)):
    alerts = session.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(20)).all()
    return render_template(
        request,
        "alerts.html",
        page_title="Recent Alerts",
        alerts=alerts,
    )


@router.get("/errors")
def recent_errors(request: Request, session: Session = Depends(get_session)):
    errors = session.scalars(
        select(StageError).order_by(StageError.created_at.desc()).limit(20)
    ).all()
    return render_template(
        request,
        "errors.html",
        page_title="Recent Errors",
        errors=errors,
    )
