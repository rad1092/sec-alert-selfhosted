from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette import status

from app.db import get_session
from app.models import IngestRun
from app.security import flash, validate_csrf
from app.services.broker import BrokerPriority

router = APIRouter(prefix="/actions", tags=["actions"])


@router.post("/ingest-now")
async def ingest_now(request: Request, session: Session = Depends(get_session)):
    await validate_csrf(request)
    broker = request.app.state.broker
    run_key = "manual-8k-ingest"

    if not broker.start_run(run_key):
        flash(request, "warning", "An 8-K ingest run is already queued or running.")
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    run = IngestRun(run_key="", triggered_by="manual", status="queued")
    session.add(run)
    session.flush()
    run.run_key = f"manual-8k-ingest-{run.id}"
    session.add(run)
    enqueue_result = broker.enqueue(
        task_name="manual-ingest-8k",
        priority=BrokerPriority.P1,
        job_key="manual-8k-ingest",
        source_name="manual-ingest",
        payload={"run_id": run.id},
    )
    if not enqueue_result.accepted:
        broker.finish_run(run_key)
        session.delete(run)
        session.commit()
        flash(request, "warning", "An 8-K ingest run is already queued or running.")
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    session.commit()
    flash(request, "success", "Queued a manual 8-K ingest run.")
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
