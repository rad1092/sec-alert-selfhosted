from __future__ import annotations

from dataclasses import dataclass

from app.services.sec.eight_k import ParsedEightK

BASE_ITEM_SCORES = {
    "1.03": -2.0,
    "2.04": -1.5,
    "2.05": -1.0,
    "2.06": -1.25,
    "3.01": -2.0,
    "4.02": -2.0,
}

KEYWORD_ADJUSTMENTS = {
    "guidance_raised": (1.0, ["guidance raised", "guidance increased"]),
    "guidance_cut": (-1.25, ["guidance cut", "guidance withdrawn"]),
    "share_repurchase": (0.75, ["share repurchase", "repurchase program"]),
    "dividend_increase": (0.5, ["dividend increase"]),
    "secondary_offering": (-0.75, ["secondary offering", "at the market offering", "atm offering"]),
    "investigation": (-1.0, ["investigation", "subpoena"]),
    "cybersecurity_incident": (-1.0, ["cybersecurity incident"]),
    "material_weakness": (-0.75, ["material weakness"]),
    "liquidity_covenant": (-1.0, ["covenant waiver"]),
    "definitive_merger_agreement": (0.5, ["definitive merger agreement"]),
    "going_concern_bankruptcy": (-2.0, ["going concern", "bankruptcy"]),
}


@dataclass
class ScoreBundle:
    score: float
    confidence: str
    reasons: list[str]


class EightKScorer:
    def score(self, parsed: ParsedEightK) -> ScoreBundle:
        score = 0.0
        reasons: list[str] = []
        body = parsed.cleaned_body.lower()

        for item in parsed.item_numbers:
            if item in BASE_ITEM_SCORES:
                score += BASE_ITEM_SCORES[item]
                reasons.append(f"8-K Item {item} detected")

        if "5.02" in parsed.item_numbers:
            if self._contains_departure_language(body):
                score += -1.0
                reasons.append("8-K Item 5.02 CEO/CFO departure language detected")
            elif self._contains_appointment_language(body):
                score += 0.25
                reasons.append("8-K Item 5.02 appointment language detected")

        for _category, (adjustment, phrases) in KEYWORD_ADJUSTMENTS.items():
            matched_phrase = next((phrase for phrase in phrases if phrase in body), None)
            if matched_phrase is not None:
                score += adjustment
                reasons.append(f"Keyword detected: {matched_phrase}")

        score = max(-2.0, min(2.0, round(score, 2)))
        confidence = self._confidence(score, reasons)
        return ScoreBundle(score=score, confidence=confidence, reasons=reasons)

    def _contains_departure_language(self, body: str) -> bool:
        titles = ("chief executive officer", "ceo", "chief financial officer", "cfo")
        departure = ("resigned", "resignation", "terminated", "retired", "step down", "ceased")
        return any(title in body for title in titles) and any(word in body for word in departure)

    def _contains_appointment_language(self, body: str) -> bool:
        titles = ("chief executive officer", "ceo", "chief financial officer", "cfo")
        appointment = ("appointed", "named", "elected")
        return any(title in body for title in titles) and any(word in body for word in appointment)

    def _confidence(self, score: float, reasons: list[str]) -> str:
        if abs(score) >= 1.5:
            return "high"
        if reasons:
            return "medium"
        return "low"
