from __future__ import annotations

from fastapi import APIRouter, Request

from app.web.helpers import render_template

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(request: Request):
    settings = request.app.state.settings
    sanitized = {
        "APP_HOST": settings.app_host,
        "APP_PORT": settings.app_port,
        "DATA_DIR": str(settings.data_dir),
        "DATABASE_URL": settings.database_url,
        "SEC_USER_AGENT": settings.sec_user_agent,
        "SEC_POLL_INTERVAL_SECONDS": settings.sec_poll_interval_seconds,
        "SEC_RATE_LIMIT_RPS": settings.sec_rate_limit_rps,
        "SCHEDULER_ENABLED": settings.scheduler_enabled,
        "WATCHLIST_SOFT_CAP": settings.watchlist_soft_cap,
        "WATCHLIST_HARD_CAP": settings.watchlist_hard_cap,
        "SLACK_WEBHOOK_URL": settings.redacted_slack_webhook_url() or "Not configured",
    }
    return render_template(
        request,
        "settings.html",
        page_title="Settings",
        settings_map=sanitized,
        broker_snapshot=request.app.state.broker.snapshot(),
        scheduler_snapshot=request.app.state.scheduler.snapshot(),
    )
