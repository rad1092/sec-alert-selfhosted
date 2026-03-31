from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, date, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
CIK_PATTERN = re.compile(r"\b\d{10}\b")
ACCESSION_PATTERN = re.compile(r"\d{10}-\d{2}-\d{6}")

TRUE_VALUES = {"1", "true", "y", "yes"}
XML_TYPE_NAMES = {"4", "4/A"}


class OwnershipXmlParseError(ValueError):
    pass


@dataclass(slots=True)
class DetailDocument:
    url: str
    filename: str
    document_type: str
    description: str


@dataclass(slots=True)
class Form4DetailMetadata:
    detail_url: str
    accession_number: str | None
    form_type: str | None
    filed_date: date | None
    accepted_at: datetime | None
    issuer_cik: str | None
    issuer_name: str | None
    issuer_ticker: str | None
    documents: list[DetailDocument]


@dataclass(slots=True)
class ParsedForm4:
    issuer_cik: str | None
    issuer_name: str | None
    issuer_ticker: str | None
    reporter_names: list[str]
    normalized_payload: dict


class Form4Parser:
    def parse(self, detail_index_html: str, ownership_xml: str) -> ParsedForm4:
        detail_metadata = parse_form4_detail_page(detail_index_html, detail_url="")
        try:
            root = ET.fromstring(ownership_xml)
        except ET.ParseError as exc:
            raise OwnershipXmlParseError("Unable to parse Form 4 ownership XML.") from exc

        payload = {
            "payload_version": 1,
            "parser_mode": "xml",
            "schema_version": _text(_find_child(root, "schemaVersion")),
            "document_type": _text(_find_child(root, "documentType")),
            "period_of_report": _text(_find_child(root, "periodOfReport")),
            "remarks": _text(_find_child(root, "remarks")),
            "issuer": self._parse_issuer(root, detail_metadata),
            "reporting_owners": self._parse_reporting_owners(root),
            "non_derivative_transactions": self._parse_non_derivative_transactions(root),
            "non_derivative_holdings": self._parse_non_derivative_holdings(root),
            "derivative_transactions": self._parse_derivative_transactions(root),
            "derivative_holdings": self._parse_derivative_holdings(root),
            "footnotes": self._parse_footnotes(root),
            "unknown_elements": [],
            "warnings": [],
        }
        payload["multi_reporting_owner"] = len(payload["reporting_owners"]) > 1
        payload["owner_count"] = len(payload["reporting_owners"])
        payload["tenb5_1"] = self._parse_tenb5_one(root, payload)

        reporter_names = [
            owner["name"]
            for owner in payload["reporting_owners"]
            if isinstance(owner.get("name"), str) and owner["name"]
        ]

        return ParsedForm4(
            issuer_cik=payload["issuer"].get("cik"),
            issuer_name=payload["issuer"].get("name"),
            issuer_ticker=payload["issuer"].get("ticker"),
            reporter_names=reporter_names,
            normalized_payload=payload,
        )

    def _parse_issuer(
        self,
        root: ET.Element,
        detail_metadata: Form4DetailMetadata,
    ) -> dict[str, str | None]:
        issuer = _find_child(root, "issuer")
        parsed = {
            "cik": _normalize_cik(_text(_find_child(issuer, "issuerCik"))),
            "name": _text(_find_child(issuer, "issuerName")),
            "ticker": _text(_find_child(issuer, "issuerTradingSymbol")),
            "foreign_trading_symbol": _text(
                _find_child(issuer, "issuerForeignTradingSymbol")
            ),
        }
        if not parsed["cik"]:
            parsed["cik"] = detail_metadata.issuer_cik
        if not parsed["name"]:
            parsed["name"] = detail_metadata.issuer_name
        if not parsed["ticker"]:
            parsed["ticker"] = detail_metadata.issuer_ticker
        return parsed

    def _parse_reporting_owners(self, root: ET.Element) -> list[dict]:
        owners: list[dict] = []
        for index, owner in enumerate(_find_children(root, "reportingOwner")):
            owner_id = _find_child(owner, "reportingOwnerId")
            relationship = _find_child(owner, "reportingOwnerRelationship")
            non_us_flag = _truthy(_text(_find_child(owner, "rptOwnerNonUSAddressFlag")))
            non_us_state = _text(_find_child(owner, "rptOwnerNonUSStateTerritory"))
            owners.append(
                {
                    "owner_index": index,
                    "cik": _normalize_cik(_text(_find_child(owner_id, "rptOwnerCik"))),
                    "name": _text(_find_child(owner_id, "rptOwnerName")),
                    "roles": {
                        "is_director": _truthy(_text(_find_child(relationship, "isDirector"))),
                        "is_officer": _truthy(_text(_find_child(relationship, "isOfficer"))),
                        "officer_title": _text(_find_child(relationship, "officerTitle")),
                        "is_ten_percent_owner": _truthy(
                            _text(_find_child(relationship, "isTenPercentOwner"))
                        ),
                        "is_other": _truthy(_text(_find_child(relationship, "isOther"))),
                        "other_text": _text(_find_child(relationship, "otherText")),
                    },
                    "non_us_address_flag": non_us_flag,
                    "non_us_state_territory": non_us_state,
                    "country": _text(_find_child(owner, "rptOwnerCountry")),
                }
            )
        return owners

    def _parse_non_derivative_transactions(self, root: ET.Element) -> list[dict]:
        table = _find_child(root, "nonDerivativeTable")
        return [
            self._parse_non_derivative_transaction(node, row_index=index)
            for index, node in enumerate(_find_children(table, "nonDerivativeTransaction"))
        ]

    def _parse_non_derivative_holdings(self, root: ET.Element) -> list[dict]:
        table = _find_child(root, "nonDerivativeTable")
        return [
            self._parse_non_derivative_holding(node, row_index=index)
            for index, node in enumerate(_find_children(table, "nonDerivativeHolding"))
        ]

    def _parse_derivative_transactions(self, root: ET.Element) -> list[dict]:
        table = _find_child(root, "derivativeTable")
        return [
            self._parse_derivative_transaction(node, row_index=index)
            for index, node in enumerate(_find_children(table, "derivativeTransaction"))
        ]

    def _parse_derivative_holdings(self, root: ET.Element) -> list[dict]:
        table = _find_child(root, "derivativeTable")
        return [
            self._parse_derivative_holding(node, row_index=index)
            for index, node in enumerate(_find_children(table, "derivativeHolding"))
        ]

    def _parse_non_derivative_transaction(self, node: ET.Element, *, row_index: int) -> dict:
        security_title = _extract_container(_find_child(node, "securityTitle"))
        transaction_date = _extract_container(_find_child(node, "transactionDate"))
        deemed_execution_date = _extract_container(_find_child(node, "deemedExecutionDate"))
        transaction_coding = _find_child(node, "transactionCoding")
        transaction_amounts = _find_child(node, "transactionAmounts")
        post_transaction_amounts = _find_child(node, "postTransactionAmounts")
        ownership_nature = _find_child(node, "ownershipNature")
        transaction_shares = _extract_container(
            _find_child(transaction_amounts, "transactionShares")
        )
        transaction_price = _extract_container(
            _find_child(transaction_amounts, "transactionPricePerShare")
        )
        acquired_disposed = _extract_container(
            _find_child(transaction_amounts, "transactionAcquiredDisposedCode")
        )
        post_holding = _extract_container(
            _find_child(post_transaction_amounts, "sharesOwnedFollowingTransaction")
        )
        direct_or_indirect = _extract_container(
            _find_child(ownership_nature, "directOrIndirectOwnership")
        )
        nature_of_ownership = _extract_container(
            _find_child(ownership_nature, "natureOfOwnership")
        )

        field_footnotes = {
            "security_title": security_title["footnote_ids"],
            "transaction_date": transaction_date["footnote_ids"],
            "deemed_execution_date": deemed_execution_date["footnote_ids"],
            "shares": transaction_shares["footnote_ids"],
            "price_per_share": transaction_price["footnote_ids"],
            "acquired_disposed_code": acquired_disposed["footnote_ids"],
            "shares_owned_following_transaction": post_holding["footnote_ids"],
            "direct_or_indirect": direct_or_indirect["footnote_ids"],
            "nature_of_ownership": nature_of_ownership["footnote_ids"],
        }

        return {
            "row_index": row_index,
            "security_title": security_title["value"],
            "transaction_date": transaction_date["value"],
            "deemed_execution_date": deemed_execution_date["value"],
            "transaction_form_type": _text(
                _find_child(transaction_coding, "transactionFormType")
            ),
            "transaction_code": _text(_find_child(transaction_coding, "transactionCode")),
            "equity_swap_involved": _truthy(
                _text(_find_child(transaction_coding, "equitySwapInvolved"))
            ),
            "transaction_timeliness": _text(
                _find_child(transaction_coding, "transactionTimeliness")
            ),
            "shares": transaction_shares["value"],
            "price_per_share": transaction_price["value"],
            "acquired_disposed_code": acquired_disposed["value"],
            "shares_owned_following_transaction": post_holding["value"],
            "ownership": {
                "direct_or_indirect": direct_or_indirect["value"],
                "nature_of_ownership": nature_of_ownership["value"],
            },
            "footnote_ids": _flatten_footnote_ids(field_footnotes),
            "field_footnotes": field_footnotes,
            "owner_refs": [],
            "unknown_elements": _unknown_child_names(
                node,
                {
                    "securityTitle",
                    "transactionDate",
                    "deemedExecutionDate",
                    "transactionCoding",
                    "transactionAmounts",
                    "postTransactionAmounts",
                    "ownershipNature",
                },
            ),
        }

    def _parse_non_derivative_holding(self, node: ET.Element, *, row_index: int) -> dict:
        security_title = _extract_container(_find_child(node, "securityTitle"))
        post_transaction_amounts = _find_child(node, "postTransactionAmounts")
        ownership_nature = _find_child(node, "ownershipNature")
        post_holding = _extract_container(
            _find_child(post_transaction_amounts, "sharesOwnedFollowingTransaction")
        )
        direct_or_indirect = _extract_container(
            _find_child(ownership_nature, "directOrIndirectOwnership")
        )
        nature_of_ownership = _extract_container(
            _find_child(ownership_nature, "natureOfOwnership")
        )

        field_footnotes = {
            "security_title": security_title["footnote_ids"],
            "shares_owned_following_transaction": post_holding["footnote_ids"],
            "direct_or_indirect": direct_or_indirect["footnote_ids"],
            "nature_of_ownership": nature_of_ownership["footnote_ids"],
        }

        return {
            "row_index": row_index,
            "security_title": security_title["value"],
            "shares_owned_following_transaction": post_holding["value"],
            "ownership": {
                "direct_or_indirect": direct_or_indirect["value"],
                "nature_of_ownership": nature_of_ownership["value"],
            },
            "footnote_ids": _flatten_footnote_ids(field_footnotes),
            "field_footnotes": field_footnotes,
            "owner_refs": [],
            "unknown_elements": _unknown_child_names(
                node,
                {"securityTitle", "postTransactionAmounts", "ownershipNature"},
            ),
        }

    def _parse_derivative_transaction(self, node: ET.Element, *, row_index: int) -> dict:
        security_title = _extract_container(_find_child(node, "securityTitle"))
        conversion_price = _extract_container(_find_child(node, "conversionOrExercisePrice"))
        transaction_date = _extract_container(_find_child(node, "transactionDate"))
        deemed_execution_date = _extract_container(_find_child(node, "deemedExecutionDate"))
        transaction_coding = _find_child(node, "transactionCoding")
        transaction_amounts = _find_child(node, "transactionAmounts")
        underlying_security_node = _find_child(node, "underlyingSecurity")
        post_transaction_amounts = _find_child(node, "postTransactionAmounts")
        ownership_nature = _find_child(node, "ownershipNature")
        transaction_shares = _extract_container(
            _find_child(transaction_amounts, "transactionShares")
        )
        transaction_price = _extract_container(
            _find_child(transaction_amounts, "transactionPricePerShare")
        )
        acquired_disposed = _extract_container(
            _find_child(transaction_amounts, "transactionAcquiredDisposedCode")
        )
        exercisable_date = _extract_container(_exercise_date_element(node))
        expiration_date = _extract_container(_find_child(node, "expirationDate"))
        underlying_security = _extract_container(
            _find_child(underlying_security_node, "underlyingSecurityTitle")
        )
        underlying_shares = _extract_container(
            _find_child(underlying_security_node, "underlyingSecurityShares")
        )
        post_holding = _extract_container(
            _find_child(post_transaction_amounts, "sharesOwnedFollowingTransaction")
        )
        direct_or_indirect = _extract_container(
            _find_child(ownership_nature, "directOrIndirectOwnership")
        )
        nature_of_ownership = _extract_container(
            _find_child(ownership_nature, "natureOfOwnership")
        )

        field_footnotes = {
            "security_title": security_title["footnote_ids"],
            "conversion_or_exercise_price": conversion_price["footnote_ids"],
            "transaction_date": transaction_date["footnote_ids"],
            "deemed_execution_date": deemed_execution_date["footnote_ids"],
            "shares": transaction_shares["footnote_ids"],
            "price_per_share": transaction_price["footnote_ids"],
            "acquired_disposed_code": acquired_disposed["footnote_ids"],
            "exercise_date": exercisable_date["footnote_ids"],
            "expiration_date": expiration_date["footnote_ids"],
            "underlying_security_title": underlying_security["footnote_ids"],
            "underlying_security_shares": underlying_shares["footnote_ids"],
            "shares_owned_following_transaction": post_holding["footnote_ids"],
            "direct_or_indirect": direct_or_indirect["footnote_ids"],
            "nature_of_ownership": nature_of_ownership["footnote_ids"],
        }

        return {
            "row_index": row_index,
            "security_title": security_title["value"],
            "conversion_or_exercise_price": conversion_price["value"],
            "transaction_date": transaction_date["value"],
            "deemed_execution_date": deemed_execution_date["value"],
            "transaction_form_type": _text(
                _find_child(transaction_coding, "transactionFormType")
            ),
            "transaction_code": _text(_find_child(transaction_coding, "transactionCode")),
            "equity_swap_involved": _truthy(
                _text(_find_child(transaction_coding, "equitySwapInvolved"))
            ),
            "transaction_timeliness": _text(
                _find_child(transaction_coding, "transactionTimeliness")
            ),
            "shares": transaction_shares["value"],
            "price_per_share": transaction_price["value"],
            "acquired_disposed_code": acquired_disposed["value"],
            "exercise_date": exercisable_date["value"],
            "expiration_date": expiration_date["value"],
            "underlying_security_title": underlying_security["value"],
            "underlying_security_shares": underlying_shares["value"],
            "shares_owned_following_transaction": post_holding["value"],
            "ownership": {
                "direct_or_indirect": direct_or_indirect["value"],
                "nature_of_ownership": nature_of_ownership["value"],
            },
            "footnote_ids": _flatten_footnote_ids(field_footnotes),
            "field_footnotes": field_footnotes,
            "owner_refs": [],
            "unknown_elements": _unknown_child_names(
                node,
                {
                    "securityTitle",
                    "conversionOrExercisePrice",
                    "transactionDate",
                    "deemedExecutionDate",
                    "transactionCoding",
                    "transactionAmounts",
                    "exerciseDate",
                    "expirationDate",
                    "underlyingSecurity",
                    "postTransactionAmounts",
                    "ownershipNature",
                },
            ),
        }

    def _parse_derivative_holding(self, node: ET.Element, *, row_index: int) -> dict:
        security_title = _extract_container(_find_child(node, "securityTitle"))
        conversion_price = _extract_container(_find_child(node, "conversionOrExercisePrice"))
        exercisable_date = _extract_container(_exercise_date_element(node))
        expiration_date = _extract_container(_find_child(node, "expirationDate"))
        underlying_security_node = _find_child(node, "underlyingSecurity")
        post_transaction_amounts = _find_child(node, "postTransactionAmounts")
        ownership_nature = _find_child(node, "ownershipNature")
        underlying_security = _extract_container(
            _find_child(underlying_security_node, "underlyingSecurityTitle")
        )
        underlying_shares = _extract_container(
            _find_child(underlying_security_node, "underlyingSecurityShares")
        )
        post_holding = _extract_container(
            _find_child(post_transaction_amounts, "sharesOwnedFollowingTransaction")
        )
        direct_or_indirect = _extract_container(
            _find_child(ownership_nature, "directOrIndirectOwnership")
        )
        nature_of_ownership = _extract_container(
            _find_child(ownership_nature, "natureOfOwnership")
        )

        field_footnotes = {
            "security_title": security_title["footnote_ids"],
            "conversion_or_exercise_price": conversion_price["footnote_ids"],
            "exercise_date": exercisable_date["footnote_ids"],
            "expiration_date": expiration_date["footnote_ids"],
            "underlying_security_title": underlying_security["footnote_ids"],
            "underlying_security_shares": underlying_shares["footnote_ids"],
            "shares_owned_following_transaction": post_holding["footnote_ids"],
            "direct_or_indirect": direct_or_indirect["footnote_ids"],
            "nature_of_ownership": nature_of_ownership["footnote_ids"],
        }

        return {
            "row_index": row_index,
            "security_title": security_title["value"],
            "conversion_or_exercise_price": conversion_price["value"],
            "exercise_date": exercisable_date["value"],
            "expiration_date": expiration_date["value"],
            "underlying_security_title": underlying_security["value"],
            "underlying_security_shares": underlying_shares["value"],
            "shares_owned_following_transaction": post_holding["value"],
            "ownership": {
                "direct_or_indirect": direct_or_indirect["value"],
                "nature_of_ownership": nature_of_ownership["value"],
            },
            "footnote_ids": _flatten_footnote_ids(field_footnotes),
            "field_footnotes": field_footnotes,
            "owner_refs": [],
            "unknown_elements": _unknown_child_names(
                node,
                {
                    "securityTitle",
                    "conversionOrExercisePrice",
                    "exerciseDate",
                    "expirationDate",
                    "underlyingSecurity",
                    "postTransactionAmounts",
                    "ownershipNature",
                },
            ),
        }

    def _parse_footnotes(self, root: ET.Element) -> dict[str, str]:
        footnotes: dict[str, str] = {}
        for footnote in root.iter():
            if _local_name(footnote.tag) != "footnote":
                continue
            footnote_id = footnote.attrib.get("id")
            if footnote_id:
                footnotes[footnote_id] = " ".join(
                    text.strip() for text in footnote.itertext() if text.strip()
                )
        return footnotes

    def _parse_tenb5_one(self, root: ET.Element, payload: dict) -> dict:
        checkbox = False
        for node in root.iter():
            name = _local_name(node.tag).lower()
            if "10b5" not in name:
                continue
            if _truthy((node.text or "").strip()):
                checkbox = True
                break

        remarks = payload.get("remarks") or ""
        footnotes: dict[str, str] = payload.get("footnotes") or {}
        supporting_footnotes = [
            footnote_id for footnote_id, text in footnotes.items() if _mentions_tenb5_one(text)
        ]
        adoption_date = _extract_adoption_date(remarks)
        if adoption_date is None:
            for footnote_id in supporting_footnotes:
                adoption_date = _extract_adoption_date(footnotes[footnote_id])
                if adoption_date is not None:
                    break

        return {
            "checkbox": checkbox,
            "mentioned_in_remarks": _mentions_tenb5_one(remarks),
            "mentioned_in_footnotes": bool(supporting_footnotes),
            "supporting_footnote_ids": supporting_footnotes,
            "adoption_date": adoption_date,
        }


def parse_form4_detail_page(detail_index_html: str, *, detail_url: str) -> Form4DetailMetadata:
    soup = BeautifulSoup(detail_index_html, "html.parser")
    documents = _parse_documents(soup, detail_url)
    page_text = soup.get_text(" ", strip=True)
    info_map = _extract_info_map(soup)

    accession_number = _normalize_accession(
        info_map.get("accession number")
        or info_map.get("accession")
        or _regex_group(ACCESSION_PATTERN, page_text)
    )
    form_type = (
        info_map.get("form type")
        or info_map.get("submission type")
        or _extract_form_type_from_documents(documents)
    )
    filed_date = _parse_date(info_map.get("filing date") or info_map.get("filed as of date"))
    accepted_at = _parse_datetime(info_map.get("accepted") or info_map.get("accepted date"))

    issuer_cik = _normalize_cik(info_map.get("issuer cik") or _regex_group(CIK_PATTERN, page_text))
    issuer_name = info_map.get("issuer name")
    issuer_ticker = info_map.get("issuer trading symbol") or info_map.get("issuer ticker")

    return Form4DetailMetadata(
        detail_url=detail_url,
        accession_number=accession_number,
        form_type=form_type,
        filed_date=filed_date,
        accepted_at=accepted_at,
        issuer_cik=issuer_cik,
        issuer_name=issuer_name,
        issuer_ticker=issuer_ticker,
        documents=documents,
    )


def locate_ownership_xml(detail_metadata: Form4DetailMetadata) -> str | None:
    candidates = ordered_ownership_xml_candidates(detail_metadata)
    if not candidates:
        return None
    return candidates[0].url


def ordered_ownership_xml_candidates(detail_metadata: Form4DetailMetadata) -> list[DetailDocument]:
    ordered: list[DetailDocument] = []
    seen_urls: set[str] = set()

    def add_candidate(document: DetailDocument) -> None:
        if document.url in seen_urls:
            return
        ordered.append(document)
        seen_urls.add(document.url)

    for document in detail_metadata.documents:
        if (
            document.document_type.upper() in XML_TYPE_NAMES
            and document.filename.lower().endswith(".xml")
        ):
            add_candidate(document)
    for document in detail_metadata.documents:
        if (
            document.document_type.upper() in XML_TYPE_NAMES
            and document.url.lower().endswith(".xml")
        ):
            add_candidate(document)
    for document in detail_metadata.documents:
        if not _document_is_xml(document):
            continue
        candidate_text = _document_candidate_text(document)
        if "ownership" in candidate_text or "xml" in candidate_text or "data" in candidate_text:
            add_candidate(document)
    for document in detail_metadata.documents:
        if _document_is_xml(document):
            add_candidate(document)
    return ordered


def _parse_documents(soup: BeautifulSoup, detail_url: str) -> list[DetailDocument]:
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header_map = _header_map_for_document_table(rows)
        if header_map is None:
            continue
        documents = _parse_document_rows(rows, detail_url=detail_url, header_map=header_map)
        if documents:
            return documents
    return _parse_document_rows(
        soup.find_all("tr"),
        detail_url=detail_url,
        header_map=None,
    )


def _parse_document_rows(
    rows,
    *,
    detail_url: str,
    header_map: dict[str, int] | None,
) -> list[DetailDocument]:
    documents: list[DetailDocument] = []
    for row in rows:
        link = row.find("a", href=True)
        if link is None:
            continue
        href = link["href"].strip()
        if not href:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"])]
        if len(cells) < 2:
            continue
        if header_map is None:
            filename = cells[1] if len(cells) > 1 else link.get_text(" ", strip=True)
            description = cells[2] if len(cells) > 2 else ""
            document_type = cells[3] if len(cells) > 3 else ""
        else:
            filename = _cell_value(cells, header_map.get("document")) or link.get_text(
                " ", strip=True
            )
            description = _cell_value(cells, header_map.get("description")) or ""
            document_type = _cell_value(cells, header_map.get("type")) or ""
        documents.append(
            DetailDocument(
                url=urljoin(detail_url, href),
                filename=filename or link.get_text(" ", strip=True),
                description=description,
                document_type=document_type,
            )
        )
    return documents


def _header_map_for_document_table(rows) -> dict[str, int] | None:
    for row in rows:
        headers = row.find_all("th")
        if not headers:
            continue
        normalized_headers = [_normalize_header(cell.get_text(" ", strip=True)) for cell in headers]
        if not {"document", "description", "type"}.issubset(set(normalized_headers)):
            continue
        return {header: index for index, header in enumerate(normalized_headers)}
    return None


def _normalize_header(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    if normalized in {"document/format files", "document format files"}:
        return "document"
    return normalized


def _cell_value(cells: list[str], index: int | None) -> str | None:
    if index is None or index >= len(cells):
        return None
    value = cells[index].strip()
    return value or None


def _document_is_xml(document: DetailDocument) -> bool:
    return document.url.lower().endswith(".xml") or document.filename.lower().endswith(".xml")


def _document_candidate_text(document: DetailDocument) -> str:
    return f"{document.filename} {document.description} {document.url}".lower()


def _extract_info_map(soup: BeautifulSoup) -> dict[str, str]:
    info_map: dict[str, str] = {}
    for label in soup.find_all(class_="infoHead"):
        key = label.get_text(" ", strip=True).lower()
        sibling = label.find_next_sibling()
        if sibling is None:
            continue
        value = sibling.get_text(" ", strip=True)
        if value:
            info_map[key] = value
    return info_map


def _extract_form_type_from_documents(documents: list[DetailDocument]) -> str | None:
    for document in documents:
        candidate = document.document_type.upper()
        if candidate in XML_TYPE_NAMES:
            return candidate
    return None


def _find_child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    for child in node:
        if _local_name(child.tag) == name:
            return child
    return None


def _find_children(node: ET.Element | None, name: str) -> list[ET.Element]:
    if node is None:
        return []
    return [child for child in node if _local_name(child.tag) == name]


def _text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = " ".join(piece.strip() for piece in node.itertext() if piece.strip())
    return text or None


def _truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in TRUE_VALUES


def _extract_container(node: ET.Element | None) -> dict[str, list[str] | str | None]:
    if node is None:
        return {"value": None, "footnote_ids": []}

    footnote_ids: list[str] = []
    value: str | None = None

    for child in node:
        local = _local_name(child.tag)
        if local == "footnoteId":
            footnote_id = child.attrib.get("id")
            if footnote_id:
                footnote_ids.append(footnote_id)
            continue
        if local == "value":
            if value is None:
                value = _text(child)
            footnote_ids.extend(_extract_container(child)["footnote_ids"])
            continue
        if value is None:
            value = _text(child)
        for nested in child.iter():
            if _local_name(nested.tag) == "footnoteId":
                footnote_id = nested.attrib.get("id")
                if footnote_id:
                    footnote_ids.append(footnote_id)

    if value is None:
        value = (node.text or "").strip() or None

    deduped = []
    for footnote_id in footnote_ids:
        if footnote_id not in deduped:
            deduped.append(footnote_id)
    return {"value": value, "footnote_ids": deduped}


def _exercise_date_element(node: ET.Element) -> ET.Element | None:
    exercise_date = _find_child(node, "exerciseDate")
    if exercise_date is None:
        return None
    nested_value = _find_child(exercise_date, "value")
    if nested_value is not None:
        return nested_value
    return exercise_date


def _flatten_footnote_ids(field_footnotes: dict[str, list[str]]) -> list[str]:
    values: list[str] = []
    for footnote_ids in field_footnotes.values():
        for footnote_id in footnote_ids:
            if footnote_id not in values:
                values.append(footnote_id)
    return values


def _unknown_child_names(node: ET.Element, known_names: set[str]) -> list[str]:
    unknown = []
    for child in node:
        name = _local_name(child.tag)
        if name not in known_names and name not in unknown:
            unknown.append(name)
    return unknown


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _normalize_cik(value: str | None) -> str | None:
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if not digits:
        return None
    return digits.zfill(10)


def _normalize_accession(value: str | None) -> str | None:
    if not value:
        return None
    match = ACCESSION_PATTERN.search(value)
    if match is None:
        return None
    return match.group(0)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    match = DATE_PATTERN.search(value)
    if match is None:
        return None
    try:
        return date.fromisoformat(match.group(0))
    except ValueError:
        return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    candidates = [normalized]
    if " " in normalized and "T" not in normalized:
        candidates.append(normalized.replace(" ", "T"))
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    return None


def _regex_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    return match.group(0)


def _mentions_tenb5_one(text: str | None) -> bool:
    if not text:
        return False
    normalized = text.lower()
    return "10b5-1" in normalized or "10b5 1" in normalized or "10b5one" in normalized


def _extract_adoption_date(text: str | None) -> str | None:
    if not text:
        return None
    match = DATE_PATTERN.search(text)
    if match is None:
        return None
    return match.group(0)
