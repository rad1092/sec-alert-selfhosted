from __future__ import annotations

from fastapi import APIRouter, Request

from app.web.helpers import render_template

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(request: Request):
    settings = request.app.state.settings
    sanitized = {
        "APP_HOST": settings.app_host,
        "APP_ALLOW_CONTAINER_BIND": settings.app_allow_container_bind,
        "APP_PORT": settings.app_port,
        "DATA_DIR": str(settings.data_dir),
        "DATABASE_URL": settings.database_url,
        "SEC_USER_AGENT": settings.sec_user_agent,
        "SEC_POLL_INTERVAL_SECONDS": settings.sec_poll_interval_seconds,
        "SEC_RATE_LIMIT_RPS": settings.sec_rate_limit_rps,
        "SEC_LIVE_8K_OVERLAP_ROWS": settings.sec_live_8k_overlap_rows,
        "SCHEDULER_ENABLED": settings.scheduler_enabled,
        "WATCHLIST_SOFT_CAP": settings.watchlist_soft_cap,
        "WATCHLIST_HARD_CAP": settings.watchlist_hard_cap,
        "SLACK_WEBHOOK_URL": settings.redacted_slack_webhook_url() or "Not configured",
        "ALERT_WEBHOOK_URL": settings.redacted_alert_webhook_url() or "Not configured",
        "ALERT_WEBHOOK_SECRET": "Configured" if settings.alert_webhook_secret else "Not configured",
        "LOCALHOST_WEBHOOK_TEST_MODE": settings.localhost_webhook_test_mode,
        "SMTP_HOST": settings.smtp_host or "Not configured",
        "SMTP_PORT": settings.smtp_port or "Not configured",
        "SMTP_USERNAME": "Configured" if settings.smtp_username else "Not configured",
        "SMTP_PASSWORD": "Configured" if settings.smtp_password else "Not configured",
        "SMTP_FROM": settings.smtp_from or "Not configured",
        "SMTP_TO": settings.smtp_to or "Not configured",
        "OPENAI_API_KEY": "Configured" if settings.openai_api_key else "Not configured",
        "OPENAI_MODEL": settings.openai_model or "Not configured",
    }
    status_sections = {
        "App mode": "Automatic polling" if settings.scheduler_enabled else "Manual-only",
        "OpenAI rewrite active": "yes" if request.app.state.summary_rewriter.is_active() else "no",
        "OPENAI_API_KEY": "Configured" if settings.openai_api_key else "Not configured",
        "OPENAI_MODEL": settings.openai_model or "Not configured",
        "Slack notifications": "Configured" if settings.slack_webhook_url else "Not configured",
        "Webhook notifications": "Configured" if settings.alert_webhook_url else "Not configured",
        "SMTP email": (
            "Configured"
            if settings.smtp_to and settings.smtp_from and settings.smtp_host
            else "Not configured"
        ),
    }
    return render_template(
        request,
        "settings.html",
        page_title="Settings",
        status_sections=status_sections,
        settings_map=sanitized,
        openai_rewrite_active=request.app.state.summary_rewriter.is_active(),
        broker_snapshot=request.app.state.broker.snapshot(),
        scheduler_snapshot=request.app.state.scheduler.snapshot(),
    )
