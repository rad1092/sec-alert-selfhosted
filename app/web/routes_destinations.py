from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import DeliveryAttempt, Destination
from app.security import flash, validate_csrf
from app.web.helpers import render_template

router = APIRouter(prefix="/destinations", tags=["destinations"])

CHANNEL_METADATA = {
    "slack": {
        "title": "Slack",
        "config_label": "env:SLACK_WEBHOOK_URL",
        "description": "Slack stays the reference delivery path.",
    },
    "webhook": {
        "title": "Generic Webhook",
        "config_label": "env:ALERT_WEBHOOK_URL",
        "description": "Webhook URL comes from env only and supports optional HMAC signing.",
    },
    "smtp": {
        "title": "SMTP Email",
        "config_label": "env:SMTP_TO",
        "description": "SMTP recipient and credentials come from env only.",
    },
}


@router.get("")
def list_destinations(request: Request, session: Session = Depends(get_session)):
    destinations = session.scalars(
        select(Destination).order_by(Destination.created_at.desc())
    ).all()
    destinations_by_type = {
        destination.destination_type: destination for destination in destinations
    }
    recent_attempts = session.scalars(
        select(DeliveryAttempt).order_by(DeliveryAttempt.created_at.desc()).limit(20)
    ).all()
    cards = []
    for channel in ("slack", "webhook", "smtp"):
        metadata = CHANNEL_METADATA[channel]
        notifier = getattr(request.app.state, f"{channel}_notifier")
        cards.append(
            {
                "channel": channel,
                "title": metadata["title"],
                "config_label": metadata["config_label"],
                "description": metadata["description"],
                "destination": destinations_by_type.get(channel),
                "configured": notifier.is_configured(),
            }
        )
    return render_template(
        request,
        "destinations.html",
        page_title="Destinations",
        destination_cards=cards,
        destinations=destinations,
        recent_attempts=recent_attempts,
    )


@router.post("/slack")
async def upsert_slack_destination(
    request: Request,
    name: str = Form(...),
    enabled: bool = Form(default=False),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
):
    return await _upsert_destination(
        request,
        session=session,
        channel="slack",
        name=name,
        enabled=enabled,
        notes=notes,
    )


@router.post("/webhook")
async def upsert_webhook_destination(
    request: Request,
    name: str = Form(...),
    enabled: bool = Form(default=False),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
):
    return await _upsert_destination(
        request,
        session=session,
        channel="webhook",
        name=name,
        enabled=enabled,
        notes=notes,
    )


@router.post("/smtp")
async def upsert_smtp_destination(
    request: Request,
    name: str = Form(...),
    enabled: bool = Form(default=False),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
):
    return await _upsert_destination(
        request,
        session=session,
        channel="smtp",
        name=name,
        enabled=enabled,
        notes=notes,
    )


@router.post("/slack/test")
async def test_slack_destination(request: Request, session: Session = Depends(get_session)):
    return await _test_destination(request, session=session, channel="slack")


@router.post("/webhook/test")
async def test_webhook_destination(request: Request, session: Session = Depends(get_session)):
    return await _test_destination(request, session=session, channel="webhook")


@router.post("/smtp/test")
async def test_smtp_destination(request: Request, session: Session = Depends(get_session)):
    return await _test_destination(request, session=session, channel="smtp")


async def _upsert_destination(
    request: Request,
    *,
    session: Session,
    channel: str,
    name: str,
    enabled: bool,
    notes: str,
):
    await validate_csrf(request)
    metadata = CHANNEL_METADATA[channel]
    destination = session.scalar(
        select(Destination).where(Destination.destination_type == channel),
    )
    if destination is None:
        destination = Destination(
            name=name.strip() or metadata["title"],
            destination_type=channel,
            enabled=enabled,
            config_label=metadata["config_label"],
            notes=notes.strip() or None,
        )
    else:
        destination.name = name.strip() or destination.name
        destination.enabled = enabled
        destination.notes = notes.strip() or None
        destination.config_label = metadata["config_label"]
    session.add(destination)
    session.commit()
    flash(request, "success", f"Saved {metadata['title']} destination metadata.")
    return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)


async def _test_destination(
    request: Request,
    *,
    session: Session,
    channel: str,
):
    await validate_csrf(request)
    destination = session.scalar(
        select(Destination).where(Destination.destination_type == channel),
    )
    if destination is None:
        flash(request, "error", f"Create a {CHANNEL_METADATA[channel]['title']} destination first.")
        return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)

    result = request.app.state.alert_delivery.send_test_message(session, destination)
    session.commit()
    level = "success" if result.status == "sent" else "warning"
    flash(request, level, result.detail)
    return RedirectResponse("/destinations", status_code=status.HTTP_303_SEE_OTHER)
