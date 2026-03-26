from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import Settings, get_settings
from app.db import configure_database, dispose_database, init_database
from app.logging import configure_logging, request_id_var
from app.services.alerts import AlertDeliveryService
from app.services.broker import SecRequestBroker
from app.services.ingest import EightKIngestService
from app.services.locks import SingletonProcessLock
from app.services.notify.slack import SlackNotifier
from app.services.scheduler import SchedulerService
from app.services.scoring.eight_k import EightKScorer
from app.services.sec.client import SecHttpClient
from app.services.sec.eight_k import EightKParser
from app.services.sec.resolver import TickerResolver
from app.services.summarize.deterministic import DeterministicEightKSummarizer
from app.services.worker import BrokerWorker
from app.web.routes_actions import router as actions_router
from app.web.routes_dashboard import router as dashboard_router
from app.web.routes_destinations import router as destinations_router
from app.web.routes_filings import router as filings_router
from app.web.routes_settings import router as settings_router
from app.web.routes_watchlist import router as watchlist_router


def create_app(
    settings: Settings | None = None,
    *,
    service_overrides: dict[str, Any] | None = None,
) -> FastAPI:
    resolved_settings = settings or get_settings()
    overrides = service_overrides or {}
    configure_logging(resolved_settings)
    resolved_settings.ensure_runtime_paths()
    configure_database(resolved_settings.database_url)

    process_lock = SingletonProcessLock(Path(resolved_settings.data_dir) / "app.lock")
    broker = overrides.get("broker") or SecRequestBroker(
        rate_limit_rps=resolved_settings.sec_rate_limit_rps
    )
    scheduler = SchedulerService(resolved_settings, broker)
    slack_notifier = overrides.get("slack_notifier") or SlackNotifier(resolved_settings)
    sec_client = overrides.get("sec_client") or SecHttpClient(resolved_settings, broker)
    resolver = overrides.get("resolver") or TickerResolver(
        resolved_settings.data_dir,
        sec_client,
    )
    parser = overrides.get("eight_k_parser") or EightKParser()
    scorer = overrides.get("eight_k_scorer") or EightKScorer()
    summarizer = overrides.get("summarizer") or DeterministicEightKSummarizer()
    alert_delivery = overrides.get("alert_delivery") or AlertDeliveryService(slack_notifier)
    ingest_service = overrides.get("ingest_service") or EightKIngestService(
        sec_client=sec_client,
        resolver=resolver,
        parser=parser,
        scorer=scorer,
        summarizer=summarizer,
        alert_delivery=alert_delivery,
    )
    worker = overrides.get("worker") or BrokerWorker(broker)

    def handle_manual_ingest(job) -> None:
        try:
            ingest_service.run_manual_ingest(job.payload.get("run_id"))
        finally:
            broker.finish_run("manual-8k-ingest")

    def handle_reparse(job) -> None:
        filing_id = job.payload["filing_id"]
        try:
            ingest_service.reparse_filing(filing_id)
        finally:
            broker.finish_run(f"reparse-8k-{filing_id}")

    worker.register_handler("manual-ingest-8k", handle_manual_ingest)
    worker.register_handler("reparse-8k", handle_reparse)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        process_lock.acquire()
        init_database()
        app.state.settings = resolved_settings
        app.state.broker = broker
        app.state.scheduler = scheduler
        app.state.worker = worker
        app.state.process_lock = process_lock
        app.state.slack_notifier = slack_notifier
        app.state.sec_client = sec_client
        app.state.resolver = resolver
        app.state.ingest_service = ingest_service
        scheduler.start()
        worker.start()
        try:
            yield
        finally:
            scheduler.shutdown()
            worker.shutdown()
            process_lock.release()
            if hasattr(sec_client, "close"):
                sec_client.close()
            dispose_database()

    app = FastAPI(title=resolved_settings.app_name, lifespan=lifespan)
    app.add_middleware(SessionMiddleware, secret_key=resolved_settings.session_secret)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "web" / "static")),
        name="static",
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok"})

    app.include_router(dashboard_router)
    app.include_router(actions_router)
    app.include_router(watchlist_router)
    app.include_router(destinations_router)
    app.include_router(filings_router)
    app.include_router(settings_router)

    return app


app = create_app()
