from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.models import WatchlistEntry
from app.services.sec.client import SecHttpClient

COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def normalize_cik(value: str | int | None) -> str | None:
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)


@dataclass
class ResolvedIssuer:
    ticker: str
    issuer_cik: str
    issuer_name: str | None
    resolution_source: str


class TickerResolver:
    def __init__(self, cache_dir: Path, sec_client: SecHttpClient) -> None:
        self.sec_client = sec_client
        self.cache_path = cache_dir / "company_tickers.json"
        self._mapping: dict[str, ResolvedIssuer] | None = None

    def resolve(self, entry: WatchlistEntry) -> ResolvedIssuer | None:
        manual = normalize_cik(entry.manual_cik_override)
        if manual:
            return ResolvedIssuer(
                ticker=entry.ticker.upper(),
                issuer_cik=manual,
                issuer_name=entry.issuer_name,
                resolution_source="manual_cik_override",
            )

        stored = normalize_cik(entry.issuer_cik)
        if stored:
            return ResolvedIssuer(
                ticker=entry.ticker.upper(),
                issuer_cik=stored,
                issuer_name=entry.issuer_name,
                resolution_source="stored_issuer_cik",
            )

        mapping = self._load_mapping()
        return mapping.get(entry.ticker.upper())

    def _load_mapping(self) -> dict[str, ResolvedIssuer]:
        if self._mapping is not None:
            return self._mapping

        if self.cache_path.exists():
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        else:
            payload = self.sec_client.download_json(COMPANY_TICKERS_URL)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        mapping: dict[str, ResolvedIssuer] = {}
        for item in payload.values():
            ticker = str(item["ticker"]).upper()
            cik = normalize_cik(item["cik_str"])
            if cik is None:
                continue
            mapping[ticker] = ResolvedIssuer(
                ticker=ticker,
                issuer_cik=cik,
                issuer_name=item.get("title"),
                resolution_source="company_tickers_json",
            )
        self._mapping = mapping
        return mapping
