from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import Alert, DeliveryAttempt, Filing
from app.security import flash, validate_csrf
from app.services.broker import BrokerPriority
from app.web.helpers import render_template

router = APIRouter(prefix="/filings", tags=["filings"])
FORM4_TYPES = {"4", "4/A"}


@router.get("/{filing_id}")
def filing_detail(
    filing_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    filing = session.get(Filing, filing_id)
    if filing is None:
        raise HTTPException(status_code=404, detail="Filing not found.")
    alert = session.scalar(select(Alert).where(Alert.filing_id == filing.id))
    attempts = []
    if alert is not None:
        attempts = session.scalars(
            select(DeliveryAttempt)
            .where(DeliveryAttempt.alert_id == alert.id)
            .order_by(DeliveryAttempt.created_at.desc())
        ).all()
    return render_template(
        request,
        "filing_detail.html",
        page_title=f"Filing {filing.accession_number}",
        filing=filing,
        alert=alert,
        attempts=attempts,
    )


@router.post("/{filing_id}/reparse")
async def reparse_filing(
    filing_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    await validate_csrf(request)
    filing = session.get(Filing, filing_id)
    if filing is None:
        raise HTTPException(status_code=404, detail="Filing not found.")

    task_name = "reparse-form4" if filing.form_type in FORM4_TYPES else "reparse-8k"
    run_prefix = "reparse-form4" if filing.form_type in FORM4_TYPES else "reparse-8k"
    run_key = f"{run_prefix}-{filing.id}"
    if not request.app.state.broker.start_run(run_key):
        flash(request, "warning", f"Reparse already queued for {filing.accession_number}.")
        return RedirectResponse(f"/filings/{filing.id}", status_code=status.HTTP_303_SEE_OTHER)

    enqueue_result = request.app.state.broker.enqueue(
        task_name=task_name,
        priority=BrokerPriority.P1,
        job_key=run_key,
        source_name="manual-reparse",
        payload={"filing_id": filing.id},
    )
    if enqueue_result.accepted:
        flash(request, "success", f"Queued reparse for {filing.accession_number}.")
    else:
        request.app.state.broker.finish_run(run_key)
        flash(request, "warning", f"Reparse already queued for {filing.accession_number}.")
    return RedirectResponse(f"/filings/{filing.id}", status_code=status.HTTP_303_SEE_OTHER)
