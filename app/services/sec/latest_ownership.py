from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

LATEST_OWNERSHIP_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=only&count=40&output=atom"
)
OWNERSHIP_DISCOVERY_FILTER_KEY = "owner=only;types=3,4,5"
OWNERSHIP_FORMS = {"3", "3/A", "4", "4/A", "5", "5/A"}
INGESTIBLE_FORM4_TYPES = {"4", "4/A"}

ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
DATETIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


@dataclass(slots=True)
class OwnershipCandidate:
    accession_number: str
    form_type: str
    detail_url: str
    filed_date: date | None
    accepted_at: datetime | None

    def cursor_tuple(self) -> tuple[datetime, date, str]:
        accepted_at = self.accepted_at or datetime.min.replace(tzinfo=UTC)
        filed_date = self.filed_date or date.min
        return (accepted_at, filed_date, self.accession_number)

    def to_payload(self) -> dict[str, str | None]:
        return {
            "accession_number": self.accession_number,
            "form_type": self.form_type,
            "detail_url": self.detail_url,
            "filed_date": self.filed_date.isoformat() if self.filed_date else None,
            "accepted_at": self.accepted_at.isoformat() if self.accepted_at else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, str | None]) -> OwnershipCandidate:
        return cls(
            accession_number=payload["accession_number"] or "",
            form_type=payload["form_type"] or "",
            detail_url=payload["detail_url"] or "",
            filed_date=_parse_date(payload.get("filed_date")),
            accepted_at=_parse_datetime(payload.get("accepted_at")),
        )


def parse_ownership_candidates(feed_xml: str) -> list[OwnershipCandidate]:
    root = ET.fromstring(feed_xml)
    candidates: list[OwnershipCandidate] = []

    entries = list(root.findall(".//{*}entry"))
    if not entries:
        entries = list(root.findall(".//item"))

    for entry in entries:
        form_type = _extract_form_type(entry)
        if form_type not in OWNERSHIP_FORMS:
            continue

        detail_url = _extract_detail_url(entry)
        accession_number = _extract_accession_number(entry)
        if not detail_url or not accession_number:
            continue

        candidates.append(
            OwnershipCandidate(
                accession_number=accession_number,
                form_type=form_type,
                detail_url=detail_url,
                filed_date=_extract_filed_date(entry),
                accepted_at=_extract_accepted_at(entry),
            )
        )

    return candidates


def _extract_form_type(entry: ET.Element) -> str | None:
    for category in entry.findall(".//{*}category"):
        term = category.attrib.get("term")
        if term in OWNERSHIP_FORMS:
            return term

    for candidate in _iter_text_values(entry):
        normalized = candidate.strip().upper()
        if normalized in OWNERSHIP_FORMS:
            return normalized
        if normalized.startswith("4/A"):
            return "4/A"

    text = " ".join(_iter_text_values(entry))
    match = re.search(r"\b(3/A|4/A|5/A|3|4|5)\b", text, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).upper()


def _extract_detail_url(entry: ET.Element) -> str | None:
    for link in entry.findall(".//{*}link"):
        href = link.attrib.get("href")
        rel = (link.attrib.get("rel") or "").lower()
        if href and (not rel or rel == "alternate"):
            return href

    for child in entry:
        if _local_name(child.tag) == "link":
            text = (child.text or "").strip()
            if text:
                return text

    return None


def _extract_accession_number(entry: ET.Element) -> str | None:
    for tag_name in ("accession-number", "accessionNumber"):
        for child in entry.findall(f".//{{*}}{tag_name}"):
            text = (child.text or "").strip()
            if ACCESSION_PATTERN.fullmatch(text):
                return text

    text = " ".join(_iter_text_values(entry))
    match = ACCESSION_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0)


def _extract_filed_date(entry: ET.Element) -> date | None:
    for tag_name in ("filing-date", "filingDate", "updated", "pubDate"):
        for child in entry.findall(f".//{{*}}{tag_name}"):
            parsed = _parse_date((child.text or "").strip())
            if parsed is not None:
                return parsed
            parsed_dt = _parse_datetime((child.text or "").strip())
            if parsed_dt is not None:
                return parsed_dt.date()

    text = " ".join(_iter_text_values(entry))
    match = DATE_PATTERN.search(text)
    if match is None:
        return None
    return _parse_date(match.group(0))


def _extract_accepted_at(entry: ET.Element) -> datetime | None:
    for tag_name in ("acceptance-datetime", "acceptanceDateTime", "accepted"):
        for child in entry.findall(f".//{{*}}{tag_name}"):
            parsed = _parse_datetime((child.text or "").strip())
            if parsed is not None:
                return parsed

    text = " ".join(_iter_text_values(entry))
    match = DATETIME_PATTERN.search(text)
    if match is None:
        return None
    return _parse_datetime(match.group(0))


def _iter_text_values(element: ET.Element):
    for node in element.iter():
        text = (node.text or "").strip()
        if text:
            yield text


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        try:
            return parsedate_to_datetime(value).date()
        except (TypeError, ValueError):
            return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    try:
        if normalized.endswith("Z"):
            return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        if len(normalized) == 14 and normalized.isdigit():
            return datetime.strptime(normalized, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        try:
            parsed = parsedate_to_datetime(normalized)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed


def make_absolute_detail_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag
