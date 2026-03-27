from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik}.json"


@dataclass
class SubmissionFiling:
    accession_number: str
    form_type: str
    filed_date: date | None
    accepted_at: datetime | None
    primary_document: str
    issuer_cik: str
    issuer_ticker: str | None
    issuer_name: str | None
    items_hint: str | None

    @property
    def archive_data_cik(self) -> str:
        return str(int(self.issuer_cik))

    @property
    def accession_nodash(self) -> str:
        return self.accession_number.replace("-", "")

    @property
    def detail_index_url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{self.archive_data_cik}/"
            f"{self.accession_nodash}/{self.accession_number}-index.html"
        )

    @property
    def primary_document_url(self) -> str:
        return (
            f"https://www.sec.gov/Archives/edgar/data/{self.archive_data_cik}/"
            f"{self.accession_nodash}/{self.primary_document}"
        )


def submissions_url(cik: str) -> str:
    return SUBMISSIONS_URL_TEMPLATE.format(cik=cik)


def parse_recent_8k_filings(payload: dict, *, issuer_cik: str) -> list[SubmissionFiling]:
    filings = payload.get("filings", {}).get("recent", {})
    forms = filings.get("form", [])
    accessions = filings.get("accessionNumber", [])
    filing_dates = filings.get("filingDate", [])
    accepted_times = filings.get("acceptanceDateTime", [])
    primary_documents = filings.get("primaryDocument", [])
    items = filings.get("items", [])

    count = min(
        len(forms),
        len(accessions),
        len(filing_dates),
        len(accepted_times),
        len(primary_documents),
    )
    issuer_name = payload.get("name")
    issuer_ticker = payload.get("tickers", [None])[0]

    rows: list[SubmissionFiling] = []
    for index in range(count):
        form_type = forms[index]
        if form_type not in {"8-K", "8-K/A"}:
            continue
        accession_number = accessions[index]
        primary_document = primary_documents[index]
        if not accession_number or not primary_document:
            continue
        rows.append(
            SubmissionFiling(
                accession_number=accession_number,
                form_type=form_type,
                filed_date=_parse_date(filing_dates[index]),
                accepted_at=_parse_datetime(accepted_times[index]),
                primary_document=primary_document,
                issuer_cik=issuer_cik,
                issuer_ticker=issuer_ticker,
                issuer_name=issuer_name,
                items_hint=items[index] if index < len(items) else None,
            )
        )
    return rows


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
