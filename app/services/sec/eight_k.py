from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

ITEM_PATTERN = re.compile(r"\bitem\s+(\d+\.\d+)\b", re.IGNORECASE)


@dataclass
class ParsedEightK:
    item_numbers: list[str]
    cleaned_body: str
    exhibit_titles: list[str]


@dataclass
class EightKDetailDocument:
    url: str
    filename: str
    description: str
    document_type: str


class EightKParser:
    def parse(self, detail_index_html: str, primary_document_text: str) -> ParsedEightK:
        detail_soup = BeautifulSoup(detail_index_html, "html.parser")
        primary_soup = BeautifulSoup(primary_document_text, "html.parser")

        item_numbers = self._extract_item_numbers(detail_soup, primary_soup)
        exhibit_titles = self._extract_exhibit_titles(detail_soup)
        cleaned_body = self._clean_text(primary_soup.get_text(" ", strip=True))

        return ParsedEightK(
            item_numbers=item_numbers,
            cleaned_body=cleaned_body,
            exhibit_titles=exhibit_titles,
        )

    def _extract_item_numbers(
        self,
        detail_soup: BeautifulSoup,
        primary_soup: BeautifulSoup,
    ) -> list[str]:
        items: list[str] = []

        for heading in detail_soup.find_all(class_="infoHead"):
            if heading.get_text(" ", strip=True).lower() != "items":
                continue
            sibling = heading.find_next_sibling()
            if sibling:
                items.extend(self._normalize_items(sibling.get_text(" ", strip=True)))

        items.extend(self._normalize_items(primary_soup.get_text(" ", strip=True)))

        deduped: list[str] = []
        for item in items:
            if item not in deduped:
                deduped.append(item)
        return deduped

    def _extract_exhibit_titles(self, detail_soup: BeautifulSoup) -> list[str]:
        exhibits: list[str] = []
        for row in detail_soup.find_all("tr"):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
            if len(cells) < 4:
                continue
            candidate_type = cells[3].upper() if len(cells) > 3 else cells[2].upper()
            if candidate_type.startswith("EX-"):
                exhibits.append(cells[1] or cells[2])
        return exhibits

    def _normalize_items(self, text: str) -> list[str]:
        return [match.group(1) for match in ITEM_PATTERN.finditer(text)]

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()


def parse_eight_k_detail_documents(
    detail_index_html: str,
    *,
    detail_url: str,
) -> list[EightKDetailDocument]:
    soup = BeautifulSoup(detail_index_html, "html.parser")
    documents: list[EightKDetailDocument] = []
    for row in soup.find_all("tr"):
        if row.find("td") is None:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        if len(cells) >= 5:
            description = cells[1]
            filename = cells[2]
            document_type = cells[3].upper()
        else:
            description = cells[0]
            filename = cells[1]
            document_type = cells[2].upper()

        link = row.find("a", href=True)
        if link is not None:
            url = urljoin(detail_url, link["href"].strip())
        elif filename:
            url = urljoin(detail_url, filename)
        else:
            continue

        documents.append(
            EightKDetailDocument(
                url=url,
                filename=filename,
                description=description,
                document_type=document_type,
            )
        )
    return documents


def locate_primary_eight_k_document_url(
    detail_index_html: str,
    *,
    detail_url: str,
    form_type: str,
) -> str | None:
    documents = parse_eight_k_detail_documents(detail_index_html, detail_url=detail_url)
    preferred_types = {form_type.upper()}
    if form_type.upper() == "8-K/A":
        preferred_types.add("8-K")
    else:
        preferred_types.add("8-K/A")

    for document in documents:
        if document.document_type in preferred_types:
            return document.url
    for document in documents:
        if document.filename.lower().endswith((".htm", ".html", ".txt")):
            return document.url
    return None
