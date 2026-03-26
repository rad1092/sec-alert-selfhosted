from __future__ import annotations

import secrets
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(default="SEC Alert Self-Hosted", alias="APP_NAME")
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    sec_user_agent: str = Field(alias="SEC_USER_AGENT")
    sec_poll_interval_seconds: int = Field(default=60, alias="SEC_POLL_INTERVAL_SECONDS")
    sec_rate_limit_rps: float = Field(default=2.0, alias="SEC_RATE_LIMIT_RPS")
    sec_live_8k_overlap_rows: int = Field(
        default=20,
        alias="SEC_LIVE_8K_OVERLAP_ROWS",
    )
    slack_webhook_url: SecretStr | None = Field(default=None, alias="SLACK_WEBHOOK_URL")
    alert_webhook_url: SecretStr | None = Field(default=None, alias="ALERT_WEBHOOK_URL")
    alert_webhook_secret: SecretStr | None = Field(
        default=None,
        alias="ALERT_WEBHOOK_SECRET",
    )
    localhost_webhook_test_mode: bool = Field(
        default=False,
        alias="LOCALHOST_WEBHOOK_TEST_MODE",
    )
    smtp_host: str | None = Field(default=None, alias="SMTP_HOST")
    smtp_port: int | None = Field(default=None, alias="SMTP_PORT")
    smtp_username: SecretStr | None = Field(default=None, alias="SMTP_USERNAME")
    smtp_password: SecretStr | None = Field(default=None, alias="SMTP_PASSWORD")
    smtp_from: str | None = Field(default=None, alias="SMTP_FROM")
    smtp_to: str | None = Field(default=None, alias="SMTP_TO")
    openai_api_key: SecretStr | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str | None = Field(default=None, alias="OPENAI_MODEL")
    scheduler_enabled: bool = Field(default=False, alias="SCHEDULER_ENABLED")
    session_secret: str = Field(
        default_factory=lambda: secrets.token_urlsafe(32),
        alias="SESSION_SECRET",
    )
    watchlist_soft_cap: int = Field(default=25, alias="WATCHLIST_SOFT_CAP")
    watchlist_hard_cap: int = Field(default=50, alias="WATCHLIST_HARD_CAP")
    testing: bool = Field(default=False, alias="TESTING")

    @field_validator("app_host")
    @classmethod
    def validate_localhost_only(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in LOCAL_HOSTS:
            raise ValueError("APP_HOST must be 127.0.0.1 or localhost in v1.")
        return normalized

    @field_validator("sec_user_agent")
    @classmethod
    def validate_sec_user_agent(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("SEC_USER_AGENT is required.")
        return normalized

    @field_validator("sec_rate_limit_rps")
    @classmethod
    def validate_sec_rate_limit(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("SEC_RATE_LIMIT_RPS must be greater than zero.")
        if value > 10:
            raise ValueError("SEC_RATE_LIMIT_RPS cannot exceed 10 requests/second.")
        return value

    @field_validator("watchlist_soft_cap", "watchlist_hard_cap")
    @classmethod
    def validate_positive_cap(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("Watchlist caps must be positive integers.")
        return value

    @field_validator("sec_live_8k_overlap_rows")
    @classmethod
    def validate_live_overlap_rows(cls, value: int) -> int:
        if value < 5 or value > 100:
            raise ValueError("SEC_LIVE_8K_OVERLAP_ROWS must be between 5 and 100.")
        return value

    @field_validator("smtp_port")
    @classmethod
    def validate_smtp_port(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value <= 0:
            raise ValueError("SMTP_PORT must be greater than zero.")
        return value

    @field_validator("openai_api_key", mode="before")
    @classmethod
    def normalize_openai_api_key(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            secret = value.get_secret_value().strip()
            return SecretStr(secret) if secret else None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @field_validator("openai_model", mode="before")
    @classmethod
    def normalize_openai_model(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None
        return value

    @model_validator(mode="after")
    def apply_defaults(self) -> Settings:
        if self.watchlist_soft_cap > self.watchlist_hard_cap:
            raise ValueError("WATCHLIST_SOFT_CAP cannot exceed WATCHLIST_HARD_CAP.")
        if self.database_url is None:
            database_path = self.data_dir / "sec_alert.db"
            self.database_url = f"sqlite:///{database_path.resolve().as_posix()}"
        if not self.database_url.startswith("sqlite:///"):
            raise ValueError("DATABASE_URL must use sqlite:/// in v1.")
        return self

    @property
    def sqlite_path(self) -> Path:
        assert self.database_url is not None
        return Path(self.database_url.replace("sqlite:///", "", 1))

    def ensure_runtime_paths(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        sqlite_path = self.sqlite_path
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite_path.open("a", encoding="utf-8"):
            pass

    def redacted_slack_webhook_url(self) -> str | None:
        if self.slack_webhook_url is None:
            return None
        parsed = urlparse(self.slack_webhook_url.get_secret_value())
        return f"{parsed.scheme}://{parsed.netloc}/..."

    def redacted_alert_webhook_url(self) -> str | None:
        if self.alert_webhook_url is None:
            return None
        parsed = urlparse(self.alert_webhook_url.get_secret_value())
        return f"{parsed.scheme}://{parsed.netloc}/..."


def get_settings() -> Settings:
    return Settings()
