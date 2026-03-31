from __future__ import annotations

import os
import sqlite3
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import Settings

DEFAULT_RELEASE_VERSION = "0.1.0"
DEFAULT_RELEASE_TARGET = "v0.2.0"

DiagnosticStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    build_date: str
    build_sha: str | None = None
    build_source: str = "local"
    target_version: str = DEFAULT_RELEASE_TARGET

    @property
    def label(self) -> str:
        parts = [self.version, self.build_date]
        if self.build_sha:
            parts.append(self.build_sha[:12])
        return " • ".join(part for part in parts if part)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    key: str
    title: str
    status: DiagnosticStatus
    message: str
    detail: str | None = None
    required: bool = True


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read_pyproject_version(repo_root: Path | None = None) -> str:
    root = repo_root or _repo_root()
    pyproject_path = root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)
    except FileNotFoundError:
        return DEFAULT_RELEASE_VERSION
    return str(pyproject.get("project", {}).get("version", DEFAULT_RELEASE_VERSION))


def load_release_info(
    env: os._Environ[str] | dict[str, str] | None = None,
    *,
    repo_root: Path | None = None,
) -> ReleaseInfo:
    env_map = env or os.environ
    version = env_map.get("APP_VERSION") or _read_pyproject_version(repo_root)
    build_date = env_map.get("APP_BUILD_DATE") or "local"
    build_sha = env_map.get("APP_BUILD_SHA") or None
    build_source = env_map.get("APP_BUILD_SOURCE") or (
        "release" if build_date != "local" else "local"
    )
    target_version = env_map.get("RELEASE_TARGET_VERSION") or DEFAULT_RELEASE_TARGET
    return ReleaseInfo(
        version=version,
        build_date=build_date,
        build_sha=build_sha,
        build_source=build_source,
        target_version=target_version,
    )


def _path_write_check(path: Path, label: str) -> DiagnosticCheck:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=True):
            pass
    except OSError as exc:
        return DiagnosticCheck(
            key=f"{label.lower().replace(' ', '_')}_writable",
            title=label,
            status="fail",
            message=f"{label} is not writable.",
            detail=str(exc),
            required=True,
        )
    return DiagnosticCheck(
        key=f"{label.lower().replace(' ', '_')}_writable",
        title=label,
        status="pass",
        message=f"{label} is writable.",
        detail=str(path),
        required=True,
    )


def _sqlite_check(path: Path) -> DiagnosticCheck:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
                connection.execute("PRAGMA user_version;")
        else:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.stem}.",
                suffix=".sqlite-check",
                delete=True,
            ):
                pass
    except (OSError, sqlite3.Error) as exc:
        return DiagnosticCheck(
            key="sqlite_path",
            title="SQLite database",
            status="fail",
            message="SQLite database path is not readable or writable.",
            detail=str(exc),
            required=True,
        )
    return DiagnosticCheck(
        key="sqlite_path",
        title="SQLite database",
        status="pass",
        message="SQLite database path is writable.",
        detail=str(path),
        required=True,
    )


def _optional_check(
    *,
    key: str,
    title: str,
    configured: bool,
    message_if_configured: str,
    message_if_missing: str,
    detail: str | None = None,
) -> DiagnosticCheck:
    if configured:
        return DiagnosticCheck(
            key=key,
            title=title,
            status="pass",
            message=message_if_configured,
            detail=detail,
            required=False,
        )
    return DiagnosticCheck(
        key=key,
        title=title,
        status="warn",
        message=message_if_missing,
        detail=detail,
        required=False,
    )


def build_release_diagnostics(
    settings: Settings,
    release_info: ReleaseInfo | None = None,
) -> list[DiagnosticCheck]:
    release = release_info or load_release_info()
    checks = [
        DiagnosticCheck(
            key="release_info",
            title="Release metadata",
            status="pass",
            message=f"Version {release.version} ({release.build_date}).",
            detail=f"Source: {release.build_source}; target: {release.target_version}",
            required=False,
        ),
    ]
    if settings.app_host not in {"127.0.0.1", "localhost"} and not (
        settings.app_allow_container_bind and settings.app_host == "0.0.0.0"
    ):
        checks.append(
            DiagnosticCheck(
                key="app_host",
                title="App host",
                status="fail",
                message="APP_HOST must stay local-first for the supported release.",
                detail=settings.app_host,
                required=True,
            )
        )
    else:
        checks.append(
            DiagnosticCheck(
                key="app_host",
                title="App host",
                status="pass",
                message=f"App host is {settings.app_host}.",
                detail=(
                    "Container bind exception enabled."
                    if settings.app_allow_container_bind
                    else None
                ),
                required=True,
            )
        )

    checks.append(
        DiagnosticCheck(
            key="sec_user_agent",
            title="SEC user agent",
            status="pass",
            message="SEC_USER_AGENT is configured.",
            detail=settings.sec_user_agent,
            required=True,
        )
    )
    checks.append(_path_write_check(settings.data_dir, "Data directory"))
    checks.append(_sqlite_check(settings.sqlite_path))

    checks.append(
        _optional_check(
            key="slack_notifier",
            title="Slack notifications",
            configured=settings.slack_webhook_url is not None,
            message_if_configured="Slack destination is configured.",
            message_if_missing="Slack destination is not configured yet.",
            detail=settings.redacted_slack_webhook_url(),
        )
    )

    webhook_configured = settings.alert_webhook_url is not None
    webhook_valid = True
    webhook_detail = settings.redacted_alert_webhook_url()
    if webhook_configured:
        parsed = settings.alert_webhook_url.get_secret_value()
        webhook_valid = parsed.startswith("https://") or (
            settings.localhost_webhook_test_mode and parsed.startswith("http://localhost")
        ) or (
            settings.localhost_webhook_test_mode and parsed.startswith("http://127.0.0.1")
        )
    webhook_status = "pass" if webhook_configured and webhook_valid else "warn"
    checks.append(
        DiagnosticCheck(
            key="webhook_notifier",
            title="Webhook notifications",
            status=webhook_status,
            message=(
                "Webhook destination is configured."
                if webhook_status == "pass"
                else "Webhook destination is not configured or needs review."
            ),
            detail=webhook_detail,
            required=False,
        )
    )

    smtp_configured = all(
        [
            settings.smtp_host,
            settings.smtp_port,
            settings.smtp_from,
            settings.smtp_to,
        ]
    )
    checks.append(
        _optional_check(
            key="smtp_notifier",
            title="SMTP email",
            configured=smtp_configured,
            message_if_configured="SMTP destination is configured.",
            message_if_missing="SMTP destination is not configured yet.",
            detail=f"{settings.smtp_host}:{settings.smtp_port}" if smtp_configured else None,
        )
    )

    openai_configured = bool(settings.openai_api_key and settings.openai_model)
    openai_partial = bool(settings.openai_api_key or settings.openai_model) and (
        not openai_configured
    )
    openai_status: DiagnosticStatus = (
        "pass" if openai_configured else ("warn" if openai_partial else "warn")
    )
    checks.append(
        DiagnosticCheck(
            key="openai",
            title="OpenAI rewrite",
            status=openai_status,
            message=(
                "OpenAI rewrite is active."
                if openai_configured
                else "OpenAI rewrite is optional and currently not fully configured."
            ),
            detail=settings.openai_model if settings.openai_model else "Not configured",
            required=False,
        )
    )

    checks.append(
        DiagnosticCheck(
            key="watchlist_caps",
            title="Watchlist caps",
            status="pass",
            message=(
                f"Validated cap is {settings.watchlist_soft_cap}; hard cap is "
                f"{settings.watchlist_hard_cap}."
            ),
            detail=(
                "The first paid release still documents "
                "25 validated / 50 hard cap / 100 unsupported."
            ),
            required=False,
        )
    )
    return checks


def summarize_diagnostics(checks: list[DiagnosticCheck]) -> dict[str, int]:
    summary = {"pass": 0, "warn": 0, "fail": 0}
    for check in checks:
        summary[check.status] += 1
    summary["total"] = len(checks)
    return summary
