from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.services.summarize.base import (
    NullSummaryRewriter,
    RewriteResult,
    RewriteSchema,
    SummaryRewriteFailure,
    SummaryRewriteInput,
    now_utc,
)

OPENAI_TIMEOUT_SECONDS = 5.0

SYSTEM_PROMPT = (
    "You rewrite filing summaries for a trader/operator dashboard. "
    "Improve readability and clarity only. "
    "Do not change score, confidence, reasons, factual direction, or material facts. "
    "Do not add investment advice. "
    "Keep the output concise and operational."
)


@dataclass(slots=True)
class ParsedResponseEnvelope:
    parsed: RewriteSchema | None
    status: str | None
    refusal: bool
    incomplete_reason: str | None
    usable_output: bool


class OpenAIResponsesSummaryRewriter:
    def __init__(
        self,
        settings: Settings,
        *,
        responses_client: Any | None = None,
    ) -> None:
        self._model = settings.openai_model
        self._responses_client = None
        self._active = bool(settings.openai_api_key and settings.openai_model)
        if not self._active:
            return

        if responses_client is not None:
            self._responses_client = responses_client
            return

        try:
            from openai import OpenAI
        except ImportError:
            self._active = False
            return

        self._responses_client = OpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            max_retries=0,
            timeout=OPENAI_TIMEOUT_SECONDS,
        ).responses

    def is_active(self) -> bool:
        return self._active and self._responses_client is not None and self._model is not None

    def rewrite(self, rewrite_input: SummaryRewriteInput) -> RewriteResult | None:
        if not self.is_active():
            return None
        if not hasattr(self._responses_client, "parse"):
            raise SummaryRewriteFailure(
                error_class="UnsupportedResponsesClient",
                message="OpenAI rewrite client does not support Responses parsing.",
                retryable=False,
            )

        try:
            response = self._responses_client.parse(
                model=self._model,
                store=False,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "issuer_name": rewrite_input.issuer_name,
                                "issuer_ticker": rewrite_input.issuer_ticker,
                                "form_type": rewrite_input.form_type,
                                "deterministic_headline": rewrite_input.deterministic_headline,
                                "deterministic_context": rewrite_input.deterministic_context,
                                "score": rewrite_input.score,
                                "confidence": rewrite_input.confidence,
                                "reasons": rewrite_input.reasons,
                                "filing_accession": rewrite_input.filing_accession,
                                "facts": rewrite_input.prompt_payload,
                            },
                            ensure_ascii=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
                text_format=RewriteSchema,
            )
        except ValidationError as exc:
            raise SummaryRewriteFailure(
                error_class="RewriteSchemaValidationError",
                message="OpenAI rewrite failed strict schema validation.",
                retryable=False,
            ) from exc
        except Exception as exc:
            raise self._classify_exception(exc) from exc

        try:
            envelope = self._parse_response(response)
        except ValidationError as exc:
            raise SummaryRewriteFailure(
                error_class="RewriteSchemaValidationError",
                message="OpenAI rewrite failed strict schema validation.",
                retryable=False,
            ) from exc
        if envelope.refusal:
            raise SummaryRewriteFailure(
                error_class="OpenAIRefusal",
                message="OpenAI rewrite refused the request.",
                retryable=False,
            )
        if envelope.status == "incomplete":
            raise SummaryRewriteFailure(
                error_class="OpenAIIncomplete",
                message=(
                    "OpenAI rewrite returned incomplete output"
                    + (
                        f" ({envelope.incomplete_reason})."
                        if envelope.incomplete_reason
                        else "."
                    )
                ),
                retryable=False,
            )
        if envelope.parsed is None:
            raise SummaryRewriteFailure(
                error_class="OpenAIEmptyOutput",
                message="OpenAI rewrite produced no usable structured output.",
                retryable=False,
            )

        return RewriteResult(
            headline=envelope.parsed.headline,
            context=envelope.parsed.context,
            category=envelope.parsed.category,
            model=self._model,
            generated_at=now_utc(),
        )

    def _parse_response(self, response: Any) -> ParsedResponseEnvelope:
        status = getattr(response, "status", None)
        incomplete_details = getattr(response, "incomplete_details", None)
        incomplete_reason = getattr(incomplete_details, "reason", None)

        refusal = False
        usable_output = False
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                content_type = getattr(content, "type", None)
                if content_type == "refusal":
                    refusal = True
                if content_type in {"output_text", "text"}:
                    usable_output = True

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            output_text = getattr(response, "output_text", None)
            if isinstance(output_text, str) and output_text.strip():
                parsed = RewriteSchema.model_validate_json(output_text)
                usable_output = True
        elif not isinstance(parsed, RewriteSchema):
            parsed = RewriteSchema.model_validate(parsed)
            usable_output = True
        else:
            usable_output = True

        return ParsedResponseEnvelope(
            parsed=parsed,
            status=status,
            refusal=refusal,
            incomplete_reason=incomplete_reason,
            usable_output=usable_output,
        )

    def _classify_exception(self, exc: Exception) -> SummaryRewriteFailure:
        error_class = exc.__class__.__name__
        status_code = getattr(exc, "status_code", None)
        if isinstance(exc, TimeoutError) or error_class in {
            "APITimeoutError",
            "APIConnectionError",
        }:
            return SummaryRewriteFailure(
                error_class=error_class,
                message="OpenAI rewrite request timed out or could not connect.",
                retryable=True,
            )
        if status_code == 429 or error_class == "RateLimitError":
            return SummaryRewriteFailure(
                error_class=error_class,
                message="OpenAI rewrite request was rate-limited.",
                retryable=True,
            )
        if isinstance(status_code, int) and status_code >= 500:
            return SummaryRewriteFailure(
                error_class=error_class,
                message="OpenAI rewrite failed with a server error.",
                retryable=True,
            )
        if isinstance(status_code, int) and status_code in {400, 401, 403, 404, 422}:
            return SummaryRewriteFailure(
                error_class=error_class,
                message=(
                    "OpenAI rewrite request used an invalid, unavailable, "
                    "or unsupported model/configuration."
                ),
                retryable=False,
            )
        return SummaryRewriteFailure(
            error_class=error_class,
            message="OpenAI rewrite request failed.",
            retryable=False,
        )


class DisabledOpenAIResponsesSummaryRewriter(NullSummaryRewriter):
    pass
