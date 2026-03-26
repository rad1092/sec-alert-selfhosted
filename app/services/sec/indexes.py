from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services.sec.resolver import normalize_cik

EASTERN_TZ = ZoneInfo("America/New_York")
ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")
ACCESSION_NODASH_PATTERN = re.compile(r"(?<!\d)(\d{18})(?!\d)")
MASTER_INDEX_BASE = "https://www.sec.gov/Archives/edgar/daily-index"


@dataclass(slots=True)
class MasterIndexRow:
    cik: str
    company_name: str
    form_type: str
    filed_date: date
    filename: str

    @property
    def accession_number(self) -> str | None:
        match = ACCESSION_PATTERN.search(self.filename)
        if match is not None:
            return match.group(0)
        nodash_match = ACCESSION_NODASH_PATTERN.search(self.filename)
        if nodash_match is None:
            return None
        raw = nodash_match.group(1)
        return f"{raw[:10]}-{raw[10:12]}-{raw[12:]}"

    @property
    def archive_data_cik(self) -> str | None:
        normalized = normalize_cik(self.cik)
        if normalized is None:
            return None
        return str(int(normalized))

    @property
    def detail_index_url(self) -> str | None:
        accession_number = self.accession_number
        archive_data_cik = self.archive_data_cik
        if accession_number is None or archive_data_cik is None:
            return None
        accession_nodash = accession_number.replace("-", "")
        return (
            f"https://www.sec.gov/Archives/edgar/data/{archive_data_cik}/"
            f"{accession_nodash}/{accession_number}-index.html"
        )


def daily_master_index_url(day: date) -> str:
    quarter = ((day.month - 1) // 3) + 1
    return (
        f"{MASTER_INDEX_BASE}/{day.year}/QTR{quarter}/"
        f"master.{day.strftime('%Y%m%d')}.idx"
    )


def parse_master_index(text: str) -> list[MasterIndexRow]:
    rows: list[MasterIndexRow] = []
    for line in text.splitlines():
        if line.count("|") != 4:
            continue
        cik, company_name, form_type, filed_date, filename = [
            cell.strip() for cell in line.split("|")
        ]
        try:
            parsed_date = date.fromisoformat(filed_date)
        except ValueError:
            continue
        normalized_cik = normalize_cik(cik)
        if normalized_cik is None:
            continue
        rows.append(
            MasterIndexRow(
                cik=normalized_cik,
                company_name=company_name,
                form_type=form_type.upper(),
                filed_date=parsed_date,
                filename=filename,
            )
        )
    return rows


def current_eastern_datetime() -> datetime:
    return datetime.now(tz=UTC).astimezone(EASTERN_TZ)


def previous_business_days(count: int, *, now: datetime | None = None) -> list[date]:
    current = (now or current_eastern_datetime()).date()
    days: list[date] = []
    cursor = current - timedelta(days=1)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def backfill_business_days(
    *,
    now: datetime | None = None,
    days: int = 30,
) -> list[date]:
    current = (now or current_eastern_datetime()).date()
    start = current - timedelta(days=days)
    end = current - timedelta(days=1)
    results: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            results.append(cursor)
        cursor += timedelta(days=1)
    return results
