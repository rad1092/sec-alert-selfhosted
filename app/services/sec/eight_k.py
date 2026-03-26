from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

ITEM_PATTERN = re.compile(r"\bitem\s+(\d+\.\d+)\b", re.IGNORECASE)


@dataclass
class ParsedEightK:
    item_numbers: list[str]
    cleaned_body: str
    exhibit_titles: list[str]


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
