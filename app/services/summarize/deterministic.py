from __future__ import annotations

from dataclasses import dataclass

from app.services.scoring.eight_k import ScoreBundle
from app.services.sec.eight_k import ParsedEightK


@dataclass
class SummaryBundle:
    headline: str
    context: str


class DeterministicEightKSummarizer:
    def summarize(
        self,
        *,
        issuer_name: str | None,
        issuer_ticker: str | None,
        form_type: str,
        parsed: ParsedEightK,
        score_bundle: ScoreBundle,
    ) -> SummaryBundle:
        display_name = issuer_ticker or issuer_name or "Issuer"
        items = ", ".join(parsed.item_numbers) if parsed.item_numbers else "unclassified sections"
        if score_bundle.score < 0:
            direction = "negative"
        elif score_bundle.score > 0:
            direction = "positive"
        else:
            direction = "neutral"

        headline = f"{display_name} filed {form_type} highlighting {items}."
        if score_bundle.reasons:
            context = (
                f"Deterministic scoring marked this filing as {direction} "
                f"({score_bundle.score:.2f}) because {score_bundle.reasons[0].lower()}."
            )
        else:
            context = (
                f"Deterministic scoring marked this filing as {direction} "
                f"({score_bundle.score:.2f}) with no strong keyword or item signals."
            )
        return SummaryBundle(headline=headline, context=context)
