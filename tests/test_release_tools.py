from __future__ import annotations

import sqlite3
from pathlib import Path

from app.cli.release import _restore_stage, _smoke_settings, _write_backup_stage, _write_sha256
from app.config import Settings
from app.release import DEFAULT_RELEASE_TARGET, build_release_diagnostics, load_release_info


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        APP_HOST="127.0.0.1",
        DATA_DIR=tmp_path / "data",
        DATABASE_URL=f"sqlite:///{(tmp_path / 'data' / 'release.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        TESTING=True,
    )


def test_load_release_info_defaults_to_pyproject_and_default_target():
    info = load_release_info(env={})
    assert info.version
    assert info.target_version == DEFAULT_RELEASE_TARGET
    assert info.build_date == "local"


def test_build_release_diagnostics_reports_expected_release_checks(tmp_path: Path):
    settings = make_settings(tmp_path)
    checks = build_release_diagnostics(settings)
    titles = {check.title for check in checks}
    assert "Release metadata" in titles
    assert "SEC user agent" in titles
    assert "SQLite database" in titles
    assert "Watchlist caps" in titles


def test_smoke_settings_uses_temp_runtime_paths(tmp_path: Path):
    settings = make_settings(tmp_path)
    smoke_settings = _smoke_settings(settings, tmp_path / "smoke-root")
    assert smoke_settings.data_dir == tmp_path / "smoke-root" / "data"
    assert smoke_settings.sqlite_path == tmp_path / "smoke-root" / "data" / "smoke.db"
    assert smoke_settings.scheduler_enabled is False
    assert smoke_settings.testing is True


def test_write_backup_stage_and_restore_stage_round_trip(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    Path(".env").write_text("SEC_USER_AGENT=Backup Test test@example.com\n", encoding="utf-8")

    settings = make_settings(workspace)
    settings.ensure_runtime_paths()
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample (value) VALUES ('before')")
        conn.commit()
    extra_file = settings.data_dir / "nested" / "note.txt"
    extra_file.parent.mkdir(parents=True, exist_ok=True)
    extra_file.write_text("runtime-data", encoding="utf-8")

    stage = workspace / "stage"
    stage.mkdir()
    manifest = _write_backup_stage(stage, settings, include_env=True)

    assert manifest["env_included"] is True
    assert (stage / ".env").exists()
    assert (stage / "database.sqlite").exists()
    assert (stage / "data" / "nested" / "note.txt").read_text(encoding="utf-8") == "runtime-data"

    Path(".env").write_text("SEC_USER_AGENT=modified\n", encoding="utf-8")
    extra_file.write_text("changed", encoding="utf-8")
    with sqlite3.connect(settings.sqlite_path) as conn:
        conn.execute("DELETE FROM sample")
        conn.commit()

    _restore_stage(stage, settings)

    assert Path(".env").read_text(encoding="utf-8").startswith("SEC_USER_AGENT=Backup Test")
    assert extra_file.read_text(encoding="utf-8") == "runtime-data"
    with sqlite3.connect(settings.sqlite_path) as conn:
        row = conn.execute("SELECT value FROM sample").fetchone()
    assert row == ("before",)


def test_write_sha256_creates_expected_format(tmp_path: Path):
    archive = tmp_path / "bundle.zip"
    checksum = tmp_path / "bundle.sha256"
    archive.write_bytes(b"release-bundle")

    _write_sha256(archive, checksum)

    text = checksum.read_text(encoding="utf-8")
    assert archive.name in text
    assert len(text.split()[0]) == 64
