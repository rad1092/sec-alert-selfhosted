from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.models import Filing

RewriteCategory = Literal["positive", "negative", "neutral", "mixed", "informational"]


class RewriteSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline: str = Field(min_length=1)
    context: str = Field(min_length=1)
    category: RewriteCategory


@dataclass(slots=True)
class SummaryRewriteInput:
    filing_accession: str | None
    form_type: str
    issuer_name: str | None
    issuer_ticker: str | None
    deterministic_headline: str
    deterministic_context: str
    score: float | None
    confidence: str | None
    reasons: list[str]
    prompt_payload: dict[str, Any]


@dataclass(slots=True)
class RewriteResult:
    headline: str
    context: str
    category: RewriteCategory
    model: str
    generated_at: datetime


@dataclass(slots=True)
class EffectiveSummary:
    source: Literal["deterministic", "openai"]
    headline: str | None
    context: str | None
    category: RewriteCategory | None


class SummaryRewriteFailure(Exception):
    def __init__(
        self,
        *,
        error_class: str,
        message: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.message = message
        self.retryable = retryable


class SummaryRewriter(Protocol):
    def is_active(self) -> bool: ...

    def rewrite(self, rewrite_input: SummaryRewriteInput) -> RewriteResult | None: ...


class NullSummaryRewriter:
    def is_active(self) -> bool:
        return False

    def rewrite(self, rewrite_input: SummaryRewriteInput) -> RewriteResult | None:  # noqa: ARG002
        return None


def effective_summary_for_filing(filing: Filing) -> EffectiveSummary:
    if filing.openai_headline and filing.openai_context:
        return EffectiveSummary(
            source="openai",
            headline=filing.openai_headline,
            context=filing.openai_context,
            category=filing.openai_category,
        )
    return EffectiveSummary(
        source="deterministic",
        headline=filing.summary_headline,
        context=filing.summary_context,
        category=None,
    )


def clear_openai_fields(filing: Filing) -> None:
    filing.openai_headline = None
    filing.openai_context = None
    filing.openai_category = None
    filing.openai_model = None
    filing.openai_generated_at = None


def now_utc() -> datetime:
    return datetime.now(UTC)
