from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import create_app
from app.release import (
    DiagnosticCheck,
    ReleaseInfo,
    build_release_diagnostics,
    load_release_info,
    summarize_diagnostics,
)
from app.services.summarize.base import NullSummaryRewriter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sec-alert-release")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Validate the current environment.")
    subparsers.add_parser("smoke", help="Run a lightweight local smoke test.")

    backup_parser = subparsers.add_parser("backup", help="Create a backup archive.")
    backup_parser.add_argument("--output-dir", default="backups")
    backup_parser.add_argument("--no-env", action="store_true")

    restore_parser = subparsers.add_parser("restore", help="Restore from a backup archive.")
    restore_parser.add_argument("--archive", required=True)

    bundle_parser = subparsers.add_parser(
        "release-bundle",
        help="Create a versioned source bundle with checksum.",
    )
    bundle_parser.add_argument("--version", default=load_release_info().target_version)
    bundle_parser.add_argument("--output-dir", default="dist/releases")
    bundle_parser.add_argument("--allow-dirty", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        return run_doctor()
    if args.command == "smoke":
        return run_smoke()
    if args.command == "backup":
        return run_backup(Path(args.output_dir), include_env=not args.no_env)
    if args.command == "restore":
        return run_restore(Path(args.archive))
    if args.command == "release-bundle":
        return run_release_bundle(
            version=args.version,
            output_dir=Path(args.output_dir),
            allow_dirty=bool(args.allow_dirty),
        )
    raise AssertionError(f"Unhandled command: {args.command}")


def run_doctor() -> int:
    settings = get_settings()
    settings.ensure_runtime_paths()
    release_info = load_release_info()
    checks = build_release_diagnostics(settings, release_info)
    _print_checks(checks, release_info)
    return 1 if any(check.status == "fail" for check in checks) else 0


def run_smoke() -> int:
    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="sec-alert-smoke-") as tmpdir:
        smoke_root = Path(tmpdir)
        smoke_settings = _smoke_settings(settings, smoke_root)
        release_info = load_release_info()
        checks = build_release_diagnostics(smoke_settings, release_info)
        if any(check.status == "fail" for check in checks):
            _print_checks(checks, release_info)
            return 1

        app = create_app(
            smoke_settings,
            service_overrides={
                "summary_rewriter": NullSummaryRewriter(),
            },
        )
        with TestClient(app) as client:
            for path in ["/healthz", "/", "/settings", "/watchlist", "/destinations", "/advanced"]:
                response = client.get(path)
                if response.status_code != 200:
                    print(f"Smoke failed: {path} returned HTTP {response.status_code}")
                    return 1
        print("Smoke passed: app booted and core pages rendered successfully.")
        return 0


def run_backup(output_dir: Path, *, include_env: bool = True) -> int:
    settings = get_settings()
    settings.ensure_runtime_paths()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    archive_path = output_dir / f"sec-alert-backup-{timestamp}.zip"
    manifest_path = output_dir / f"sec-alert-backup-{timestamp}.manifest.json"
    sha_path = output_dir / f"sec-alert-backup-{timestamp}.sha256"

    with tempfile.TemporaryDirectory(prefix="sec-alert-backup-") as tmpdir:
        stage = Path(tmpdir)
        manifest = _write_backup_stage(stage, settings, include_env=include_env)
        manifest["archive_name"] = archive_path.name
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        _zip_directory(stage, archive_path)
        _write_sha256(archive_path, sha_path)

    print(f"Backup archive created: {archive_path}")
    print(f"Checksum written: {sha_path}")
    return 0


def run_restore(archive: Path) -> int:
    settings = get_settings()
    settings.ensure_runtime_paths()
    if not archive.exists():
        print(f"Backup archive not found: {archive}")
        return 1

    with tempfile.TemporaryDirectory(prefix="sec-alert-restore-") as tmpdir:
        stage = Path(tmpdir)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(stage)
        _restore_stage(stage, settings)

    print("Restore completed successfully.")
    return 0


def run_release_bundle(
    *,
    version: str,
    output_dir: Path,
    allow_dirty: bool = False,
) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    _assert_clean_git_tree(repo_root, allow_dirty=allow_dirty)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_path = output_dir / f"sec-alert-self-hosted-{version}.zip"
    manifest_path = output_dir / f"sec-alert-self-hosted-{version}.manifest.json"
    sha_path = output_dir / f"sec-alert-self-hosted-{version}.sha256"

    commit_sha = _git_output(repo_root, ["rev-parse", "HEAD"])
    created_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    manifest = {
        "version": version,
        "commit_sha": commit_sha,
        "created_at": created_at,
        "source": "git archive",
        "product": "sec-alert-self-hosted",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    subprocess.run(
        [
            "git",
            "archive",
            "--format=zip",
            f"--prefix=sec-alert-self-hosted-{version}/",
            f"--output={archive_path}",
            "HEAD",
        ],
        cwd=repo_root,
        check=True,
    )
    _write_sha256(archive_path, sha_path)
    print(f"Release bundle created: {archive_path}")
    print(f"Checksum written: {sha_path}")
    print(f"Manifest written: {manifest_path}")
    return 0


def _smoke_settings(settings: Settings, smoke_root: Path) -> Settings:
    smoke_data_dir = smoke_root / "data"
    smoke_db = smoke_data_dir / "smoke.db"
    return settings.model_copy(
        update={
            "data_dir": smoke_data_dir,
            "database_url": f"sqlite:///{smoke_db.as_posix()}",
            "scheduler_enabled": False,
            "testing": True,
        }
    )


def _print_checks(checks: Iterable[DiagnosticCheck], release_info: ReleaseInfo) -> None:
    summary = summarize_diagnostics(list(checks))
    release_label = release_info.label.replace(" • ", " | ")
    print(f"Release: {release_label} [{release_info.build_source}]")
    print(
        "Diagnostics: "
        f"{summary['pass']} pass, {summary['warn']} warn, {summary['fail']} fail"
    )
    for check in checks:
        prefix = {
            "pass": "OK",
            "warn": "WARN",
            "fail": "FAIL",
        }[check.status]
        print(f"{prefix}: {check.title} - {check.message}")
        if check.detail:
            print(f"    {check.detail}")


def _write_backup_stage(stage: Path, settings: Settings, *, include_env: bool) -> dict[str, object]:
    manifest: dict[str, object] = {
        "product": "sec-alert-self-hosted",
        "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ"),
        "release": load_release_info().label,
        "database_url": settings.database_url,
        "data_dir": str(settings.data_dir),
        "include_env": include_env,
    }
    if include_env:
        env_path = Path(".env")
        if env_path.exists():
            shutil.copy2(env_path, stage / ".env")
            manifest["env_included"] = True
        else:
            manifest["env_included"] = False
    else:
        manifest["env_included"] = False

    db_source = settings.sqlite_path
    db_target = stage / "database.sqlite"
    if db_source.exists():
        _copy_sqlite_database(db_source, db_target)
        manifest["database_included"] = True
    else:
        manifest["database_included"] = False

    data_stage = stage / "data"
    data_stage.mkdir(parents=True, exist_ok=True)
    if settings.data_dir.exists():
        for path in settings.data_dir.rglob("*"):
            if path.is_dir():
                continue
            if path.name in {db_source.name, "app.lock"}:
                continue
            relative = path.relative_to(settings.data_dir)
            target = data_stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return manifest


def _restore_stage(stage: Path, settings: Settings) -> None:
    env_source = stage / ".env"
    if env_source.exists():
        env_target = Path(".env")
        if env_target.exists():
            env_backup = env_target.with_suffix(".env.before-restore")
            shutil.copy2(env_target, env_backup)
        shutil.copy2(env_source, env_target)

    db_source = stage / "database.sqlite"
    if db_source.exists():
        db_target = settings.sqlite_path
        db_target.parent.mkdir(parents=True, exist_ok=True)
        if db_target.exists():
            db_backup = db_target.with_suffix(".before-restore.sqlite")
            shutil.copy2(db_target, db_backup)
        shutil.copy2(db_source, db_target)

    data_source = stage / "data"
    if data_source.exists():
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        for path in data_source.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(data_source)
            target = settings.data_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _copy_sqlite_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_conn, sqlite3.connect(target) as target_conn:
        source_conn.backup(target_conn)


def _zip_directory(stage: Path, archive_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_dir():
                continue
            zf.write(path, path.relative_to(stage).as_posix())


def _write_sha256(archive_path: Path, sha_path: Path) -> None:
    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sha_path.write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")


def _assert_clean_git_tree(repo_root: Path, *, allow_dirty: bool) -> None:
    if allow_dirty:
        return
    status = _git_output(repo_root, ["status", "--porcelain"])
    if status.strip():
        raise SystemExit(
            "Release bundle requires a clean git tree. Commit or pass --allow-dirty."
        )


def _git_output(repo_root: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
