from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import DeliveryAttempt, Destination
from app.security import flash, validate_csrf
from app.services.notify.slack import SlackNotifier
from app.web.helpers import render_template

router = APIRouter(prefix="/destinations", tags=["destinations"])


@router.get("")
def list_destinations(request: Request, session: Session = Depends(get_session)):
    destinations = session.scalars(
        select(Destination).order_by(Destination.created_at.desc())
    ).all()
    return render_template(
        request,
        "destinations.html",
        page_title="Destinations",
        destinations=destinations,
        slack_configured=request.app.state.slack_notifier.is_configured(),
    )


@router.post("")
async def upsert_slack_destination(
    request: Request,
    name: str = Form(...),
    enabled: bool = Form(default=False),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    destination = session.scalar(
        select(Destination).where(Destination.destination_type == "slack"),
    )
    if destination is None:
        destination = Destination(
            name=name.strip() or "Primary Slack",
            destination_type="slack",
            enabled=enabled,
            config_label="env:SLACK_WEBHOOK_URL",
            notes=notes.strip() or None,
        )
    else:
        destination.name = name.strip() or destination.name
        destination.enabled = enabled
        destination.notes = notes.strip() or None
        destination.config_label = "env:SLACK_WEBHOOK_URL"
    session.add(destination)
    session.commit()
    flash(request, "success", "Saved Slack destination metadata.")
    return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/slack/test")
async def test_slack_destination(
    request: Request,
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    destination = session.scalar(
        select(Destination).where(Destination.destination_type == "slack"),
    )
    if destination is None:
        flash(request, "error", "Create a Slack destination first.")
        return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)

    notifier: SlackNotifier = request.app.state.slack_notifier
    result = notifier.send_test_message(destination.name)
    attempt = DeliveryAttempt(
        destination_id=destination.id,
        alert_id=None,
        channel="slack",
        status=result.status,
        response_code=result.response_code,
        error_message=result.detail if result.status != "sent" else None,
    )
    session.add(attempt)
    session.commit()

    level = "success" if result.status == "sent" else "warning"
    flash(request, level, result.detail)
    return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)
