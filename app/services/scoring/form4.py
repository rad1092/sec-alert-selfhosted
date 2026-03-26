from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.sec.form4 import ParsedForm4

POSITIVE_CODES = {"P"}
NEGATIVE_CODES = {"S"}
NEUTRAL_CODES = {"M", "F", "A", "G", "J"}
LOW_CONFIDENCE_CODES = {"C", "E", "H", "O", "X", "L", "W", "Z", "U", "K", "I", "D", "V"}


@dataclass(slots=True)
class Form4ScoreBundle:
    score: float
    confidence: str
    reasons: list[str]


class Form4Scorer:
    def score(self, parsed: ParsedForm4) -> Form4ScoreBundle:
        rows = self._all_transaction_rows(parsed)
        signed_directions: list[int] = []
        score = Decimal("0")
        reasons: list[str] = []
        saw_unknown = False

        for row in rows:
            code = (row.get("transaction_code") or "").upper()
            if code in POSITIVE_CODES:
                score += Decimal("1.0")
                signed_directions.append(1)
                reasons.append("Form 4 code P detected")
            elif code in NEGATIVE_CODES:
                score -= Decimal("1.0")
                signed_directions.append(-1)
                reasons.append("Form 4 code S detected")
            elif code in NEUTRAL_CODES:
                reasons.append(f"Form 4 code {code} treated as neutral")
            elif code in LOW_CONFIDENCE_CODES:
                reasons.append(f"Form 4 code {code} treated as neutral low-confidence")
            elif code:
                saw_unknown = True
                reasons.append(f"Unknown transaction code {code} treated as neutral")

        unique_directions = set(signed_directions)
        if len(unique_directions) > 1:
            score = Decimal("0")
            reasons.append("Conflicting signed transaction directions reduced confidence")

        normalized_payload = parsed.normalized_payload
        reporter_count = int(normalized_payload.get("owner_count") or 0)
        if reporter_count > 1 and len(unique_directions) == 1 and score != 0:
            reasons.append("Multiple reporting owners aligned")

        if normalized_payload.get("tenb5_1", {}).get("checkbox"):
            reasons.append("Rule 10b5-1 indication captured as context")

        clamped = max(Decimal("-2.0"), min(Decimal("2.0"), score))
        confidence = self._confidence(
            score=clamped,
            signed_directions=unique_directions,
            reporter_count=reporter_count,
            saw_unknown=saw_unknown,
        )
        deduped_reasons = []
        for reason in reasons:
            if reason not in deduped_reasons:
                deduped_reasons.append(reason)
        return Form4ScoreBundle(
            score=float(clamped),
            confidence=confidence,
            reasons=deduped_reasons,
        )

    def _all_transaction_rows(self, parsed: ParsedForm4) -> list[dict]:
        payload = parsed.normalized_payload
        rows = list(payload.get("non_derivative_transactions") or [])
        rows.extend(payload.get("derivative_transactions") or [])
        return rows

    def _confidence(
        self,
        *,
        score: Decimal,
        signed_directions: set[int],
        reporter_count: int,
        saw_unknown: bool,
    ) -> str:
        if len(signed_directions) > 1:
            return "low"
        if score == 0:
            return "low"
        if reporter_count > 1 and not saw_unknown:
            return "high"
        return "high" if abs(score) >= Decimal("1.0") else "medium"
