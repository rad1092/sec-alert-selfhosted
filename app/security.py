from __future__ import annotations

import secrets
from typing import Any

from fastapi import HTTPException, Request, status

CSRF_SESSION_KEY = "_csrf_token"
FLASH_SESSION_KEY = "_flash_messages"


def ensure_csrf_token(request: Request) -> str:
    token = request.session.get(CSRF_SESSION_KEY)
    if token is None:
        token = secrets.token_urlsafe(24)
        request.session[CSRF_SESSION_KEY] = token
    return token


async def validate_csrf(request: Request) -> None:
    form = await request.form()
    supplied_token = form.get("csrf_token") or request.headers.get("x-csrf-token")
    expected_token = request.session.get(CSRF_SESSION_KEY)
    if not expected_token or supplied_token != expected_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid CSRF token.",
        )


def flash(request: Request, level: str, message: str) -> None:
    messages: list[dict[str, str]] = request.session.get(FLASH_SESSION_KEY, [])
    messages.append({"level": level, "message": message})
    request.session[FLASH_SESSION_KEY] = messages


def pop_flashes(request: Request) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = request.session.get(FLASH_SESSION_KEY, [])
    request.session[FLASH_SESSION_KEY] = []
    return messages


def _current_section(path: str) -> str:
    if path.startswith("/watchlist"):
        return "watchlist"
    if path.startswith("/destinations"):
        return "notifications"
    if path.startswith("/advanced") or path.startswith("/errors") or path.startswith("/settings"):
        return "advanced"
    return "inbox"


def template_defaults(request: Request, **context: Any) -> dict[str, Any]:
    release_info = getattr(request.app.state, "release_info", None)
    return {
        "csrf_token": ensure_csrf_token(request),
        "flash_messages": pop_flashes(request),
        "current_path": request.url.path,
        "current_section": _current_section(request.url.path),
        "release_info": release_info,
        "release_label": getattr(release_info, "label", "Unreleased"),
        "release_version": getattr(release_info, "version", "0.1.0"),
        "release_build_date": getattr(release_info, "build_date", "local"),
        "release_build_sha": getattr(release_info, "build_sha", None),
        **context,
    }
