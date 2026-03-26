from __future__ import annotations

from dataclasses import dataclass

from app.services.scoring.form4 import Form4ScoreBundle
from app.services.sec.form4 import ParsedForm4


@dataclass(slots=True)
class Form4SummaryBundle:
    headline: str
    context: str


class DeterministicForm4Summarizer:
    def summarize(
        self,
        *,
        issuer_name: str | None,
        issuer_ticker: str | None,
        form_type: str,
        parsed: ParsedForm4,
        score_bundle: Form4ScoreBundle,
    ) -> Form4SummaryBundle:
        display_name = issuer_ticker or issuer_name or parsed.issuer_name or "Issuer"
        reporter_names = parsed.reporter_names
        reporter_label = _reporter_label(reporter_names)
        payload = parsed.normalized_payload
        tenb5_one = payload.get("tenb5_1", {})

        if score_bundle.score > 0:
            headline = f"Form 4: insider purchase reported for {display_name}."
        elif score_bundle.score < 0:
            headline = f"Form 4: insider sale reported for {display_name}."
        elif any(
            "conflicting signed transaction directions" in reason.lower()
            for reason in score_bundle.reasons
        ):
            headline = f"Form 4: mixed insider transactions reported for {display_name}."
        else:
            headline = f"Form 4: insider ownership change reported for {display_name}."

        row_count = len(payload.get("non_derivative_transactions") or []) + len(
            payload.get("derivative_transactions") or []
        )
        context = (
            f"{reporter_label} filed {form_type} with {row_count} transaction row"
            f"{'' if row_count == 1 else 's'}."
        )
        if score_bundle.reasons:
            primary_reason = score_bundle.reasons[0].lower()
            context += (
                " Deterministic scoring marked it "
                f"{score_bundle.confidence} confidence because {primary_reason}."
            )
        if (
            tenb5_one.get("checkbox")
            or tenb5_one.get("mentioned_in_remarks")
            or tenb5_one.get("mentioned_in_footnotes")
        ):
            context += " Rule 10b5-1 context was captured."
            if tenb5_one.get("adoption_date"):
                context += f" Adoption date noted: {tenb5_one['adoption_date']}."
        return Form4SummaryBundle(headline=headline, context=context)


def _reporter_label(reporter_names: list[str]) -> str:
    if not reporter_names:
        return "An insider"
    if len(reporter_names) == 1:
        return reporter_names[0]
    return f"{reporter_names[0]} and {len(reporter_names) - 1} others"
