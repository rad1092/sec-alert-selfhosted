from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging import SensitiveDataFilter


def test_settings_require_sec_user_agent(tmp_path: Path):
    with pytest.raises(ValidationError):
        Settings(
            APP_HOST="127.0.0.1",
            DATA_DIR=tmp_path,
            DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            SEC_USER_AGENT="",
        )


def test_settings_reject_non_localhost(tmp_path: Path):
    with pytest.raises(ValidationError):
        Settings(
            APP_HOST="0.0.0.0",
            DATA_DIR=tmp_path,
            DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            SEC_USER_AGENT="SEC Alert Test test@example.com",
        )


def test_settings_reject_rate_limit_above_ten(tmp_path: Path):
    with pytest.raises(ValidationError):
        Settings(
            APP_HOST="127.0.0.1",
            DATA_DIR=tmp_path,
            DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            SEC_USER_AGENT="SEC Alert Test test@example.com",
            SEC_RATE_LIMIT_RPS=11,
        )


def test_settings_default_8k_overlap_rows(tmp_path: Path):
    settings = Settings(
        APP_HOST="127.0.0.1",
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
    )
    assert settings.sec_live_8k_overlap_rows == 20


def test_settings_reject_invalid_8k_overlap_rows(tmp_path: Path):
    with pytest.raises(ValidationError):
        Settings(
            APP_HOST="127.0.0.1",
            DATA_DIR=tmp_path,
            DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            SEC_USER_AGENT="SEC Alert Test test@example.com",
            SEC_LIVE_8K_OVERLAP_ROWS=4,
        )


def test_settings_treat_blank_openai_values_as_unconfigured(tmp_path: Path):
    settings = Settings(
        APP_HOST="127.0.0.1",
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
        OPENAI_API_KEY="   ",
        OPENAI_MODEL="   ",
    )
    assert settings.openai_api_key is None
    assert settings.openai_model is None


def test_settings_ensure_runtime_paths(tmp_path: Path):
    settings = Settings(
        APP_HOST="127.0.0.1",
        DATA_DIR=tmp_path,
        DATABASE_URL=f"sqlite:///{(tmp_path / 'nested' / 'test.db').as_posix()}",
        SEC_USER_AGENT="SEC Alert Test test@example.com",
    )
    settings.ensure_runtime_paths()
    assert (tmp_path / "nested" / "test.db").exists()


def test_sensitive_data_filter_redacts_auth_and_secret():
    secret = "https://hooks.slack.com/services/secret"
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=f"Authorization: Bearer abc123 {secret}",
        args=(),
        exc_info=None,
    )
    log_filter = SensitiveDataFilter([secret])
    assert log_filter.filter(record) is True
    assert "abc123" not in record.msg
    assert secret not in record.msg
    assert "[REDACTED]" in record.msg
