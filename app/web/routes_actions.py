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
    return _queue_ingest_run(
        request=request,
        session=session,
        run_key="manual-8k-ingest",
        task_name="manual-ingest-8k",
        flash_label="8-K",
        triggered_by="manual_8k",
    )


@router.post("/ingest-form4-now")
async def ingest_form4_now(request: Request, session: Session = Depends(get_session)):
    await validate_csrf(request)
    return _queue_ingest_run(
        request=request,
        session=session,
        run_key="manual-form4-ingest",
        task_name="manual-ingest-form4",
        flash_label="Form 4",
        triggered_by="manual_form4",
    )


@router.post("/repair-now")
async def repair_now(request: Request, session: Session = Depends(get_session)):
    await validate_csrf(request)
    return _queue_ingest_run(
        request=request,
        session=session,
        run_key="repair:recent",
        task_name="orchestrate-repair-recent",
        flash_label="repair",
        triggered_by="repair",
        priority=BrokerPriority.P3,
        source_name="manual-repair",
        success_message="Queued a rolling repair run.",
    )


def _queue_ingest_run(
    *,
    request: Request,
    session: Session,
    run_key: str,
    task_name: str,
    flash_label: str,
    triggered_by: str,
    priority: BrokerPriority = BrokerPriority.P1,
    source_name: str = "manual-ingest",
    success_message: str | None = None,
):
    broker = request.app.state.broker

    if not broker.start_run(run_key):
        flash(request, "warning", f"A {flash_label} ingest run is already queued or running.")
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    run = IngestRun(run_key="", triggered_by=triggered_by, status="queued")
    session.add(run)
    session.flush()
    run.run_key = f"{run_key}-{run.id}"
    session.add(run)
    enqueue_result = broker.enqueue(
        task_name=task_name,
        priority=priority,
        job_key=run_key,
        source_name=source_name,
        payload={
            "run_id": run.id,
            "run_started": True,
        },
    )
    if not enqueue_result.accepted:
        broker.finish_run(run_key)
        session.delete(run)
        session.commit()
        flash(request, "warning", f"A {flash_label} ingest run is already queued or running.")
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    session.commit()
    flash(
        request,
        "success",
        success_message or f"Queued a manual {flash_label} ingest run.",
    )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
