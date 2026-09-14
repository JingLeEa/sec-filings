#!/usr/bin/env python3
"""Download a SEC 10-K filing through the SEC API and extract cleaned Item sections.

The pipeline is intentionally small and dependency-free:

    SEC ticker/year -> filing HTML -> cleaned text -> Items 1, 1A, 7, 8, 15 -> chunks

Outputs are JSON and TXT files with stable paragraph IDs such as 2024_1_P001.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_ITEMS = ("1", "1A", "7", "8", "15")
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
ITEM_ENDS = {
    "1": ("1A", "1B", "1C", "2"),
    "1A": ("1B", "1C", "2"),
    "7": ("7A", "8"),
    "8": ("9", "9A", "9B", "9C"),
    "15": ("16",),
}
ITEM_TITLES = {
    "1": "Business",
    "1A": "Risk Factors",
    "7": "Management's Discussion and Analysis",
    "8": "Financial Statements and Supplementary Data",
    "15": "Exhibits and Financial Statement Schedules",
}
BULLET_PREFIX_RE = re.compile(r"^\s*(?:[•‣▪▫◦●○]|\*\s+|-\s+)")


@dataclass(frozen=True)
class Heading:
    item: str
    title: str
    start: int
    end: int
    line: str


@dataclass(frozen=True)
class FilingBlock:
    index: int
    tag: str
    text: str
    style: str = ""
    bold: bool = False


@dataclass(frozen=True)
class FilingStructureEvent:
    index: int
    kind: str
    text: str
    rows: tuple[tuple[str, ...], ...] = ()
    bold: bool = False


@dataclass(frozen=True)
class TocEntry:
    title: str
    normalized: str
    match_key: str
    page: str = ""


class FilingTextExtractor(HTMLParser):
    """Turn HTML into readable text while dropping tables and chrome."""

    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "center",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "tr",
        "ul",
    }
    DROP_TAGS = {"script", "style", "noscript", "table", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.drop_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self.DROP_TAGS:
            self.drop_stack.append(tag)
            return
        if self.drop_stack:
            return
        if tag == "li":
            self._newline()
            self.parts.append("- ")
        elif tag in self.BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_stack:
            if tag == self.drop_stack[-1]:
                self.drop_stack.pop()
            return
        if tag in self.BLOCK_TAGS:
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.drop_stack:
            return
        data = html.unescape(data).replace("\xa0", " ")
        if data.strip():
            self.parts.append(data)

    def get_text(self) -> str:
        return "".join(self.parts)

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")


class FilingBlockExtractor(HTMLParser):
    """Collect filing paragraphs as displayed HTML blocks."""

    BLOCK_TAGS = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6"}
    DROP_TAGS = {"script", "style", "noscript", "table", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[FilingBlock] = []
        self.block_stack: list[dict[str, object]] = []
        self.drop_depth = 0
        self.style_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_by_name = {name.lower(): value or "" for name, value in attrs}
        style = attrs_by_name.get("style", "")

        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag in self.DROP_TAGS or is_hidden_style(style):
            self.drop_depth = 1
            return

        self.style_stack.append((tag, style))

        if tag in self.BLOCK_TAGS:
            self.block_stack.append(
                {
                    "tag": tag,
                    "style": style,
                    "parts": [],
                    "bold": is_bold_style(style),
                }
            )
        elif tag == "br":
            self._append("\n")
        elif tag == "li":
            self._append("\n- ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.drop_depth:
            return
        if tag == "br":
            self._append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return

        if tag in self.BLOCK_TAGS and self.block_stack:
            block = self.block_stack.pop()
            text = clean_block_text("".join(block["parts"]))  # type: ignore[arg-type]
            if text and not should_drop_line(text):
                self.blocks.append(
                    FilingBlock(
                        index=len(self.blocks) + 1,
                        tag=str(block["tag"]),
                        text=text,
                        style=str(block["style"]),
                        bold=bool(block["bold"]),
                    )
                )

        for index in range(len(self.style_stack) - 1, -1, -1):
            if self.style_stack[index][0] == tag:
                del self.style_stack[index]
                break

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        data = html.unescape(data).replace("\xa0", " ")
        if not data.strip():
            return
        self._append(data)
        if self.block_stack and is_bold_style(self._current_style()):
            self.block_stack[-1]["bold"] = True

    def _append(self, value: str) -> None:
        if self.block_stack:
            parts = self.block_stack[-1]["parts"]
            assert isinstance(parts, list)
            parts.append(value)

    def _current_style(self) -> str:
        return " ".join(style for _, style in self.style_stack)


class FilingStructureExtractor(HTMLParser):
    """Collect block text plus table cells for TOC-guided section labels."""

    BLOCK_TAGS = FilingBlockExtractor.BLOCK_TAGS
    DROP_TAGS = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[FilingStructureEvent] = []
        self.block_stack: list[dict[str, object]] = []
        self.table_depth = 0
        self.table_rows: list[list[str]] = []
        self.current_row: list[str] | None = None
        self.current_cell_parts: list[str] | None = None
        self.drop_depth = 0
        self.style_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_by_name = {name.lower(): value or "" for name, value in attrs}
        style = attrs_by_name.get("style", "")

        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag in self.DROP_TAGS or is_hidden_style(style):
            self.drop_depth = 1
            return

        self.style_stack.append((tag, style))

        if tag == "table":
            if self.table_depth == 0:
                self.table_rows = []
            self.table_depth += 1
            return

        if self.table_depth:
            if tag == "tr":
                self.current_row = []
            elif tag in {"td", "th"}:
                self.current_cell_parts = []
            elif tag == "br" and self.current_cell_parts is not None:
                self.current_cell_parts.append(" ")
            return

        if tag in self.BLOCK_TAGS:
            self.block_stack.append(
                {
                    "tag": tag,
                    "parts": [],
                    "bold": is_bold_style(style),
                }
            )
        elif tag == "br":
            self._append_to_block("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.drop_depth:
            return
        if tag == "br":
            if self.table_depth and self.current_cell_parts is not None:
                self.current_cell_parts.append(" ")
            else:
                self._append_to_block("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return

        if self.table_depth:
            if tag in {"td", "th"} and self.current_cell_parts is not None:
                cell = clean_block_text("".join(self.current_cell_parts))
                if self.current_row is not None:
                    self.current_row.append(cell)
                self.current_cell_parts = None
            elif tag == "tr" and self.current_row is not None:
                if any(cell for cell in self.current_row):
                    self.table_rows.append(self.current_row)
                self.current_row = None
            elif tag == "table":
                self.table_depth -= 1
                if self.table_depth == 0:
                    rows = tuple(tuple(cell for cell in row) for row in self.table_rows)
                    text = clean_block_text(" ".join(cell for row in rows for cell in row if cell))
                    if text:
                        self.events.append(
                            FilingStructureEvent(index=len(self.events) + 1, kind="table", text=text, rows=rows)
                        )
            self._pop_style(tag)
            return

        if tag in self.BLOCK_TAGS and self.block_stack:
            block = self.block_stack.pop()
            text = clean_block_text("".join(block["parts"]))  # type: ignore[arg-type]
            if text:
                self.events.append(
                    FilingStructureEvent(
                        index=len(self.events) + 1,
                        kind=str(block["tag"]),
                        text=text,
                        bold=bool(block["bold"]),
                    )
                )

        self._pop_style(tag)

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        data = html.unescape(data).replace("\xa0", " ")
        if not data.strip():
            return

        if self.table_depth:
            if self.current_cell_parts is not None:
                self.current_cell_parts.append(data)
        else:
            self._append_to_block(data)
            if self.block_stack and is_bold_style(self._current_style()):
                self.block_stack[-1]["bold"] = True

    def _append_to_block(self, value: str) -> None:
        if self.block_stack:
            parts = self.block_stack[-1]["parts"]
            assert isinstance(parts, list)
            parts.append(value)

    def _current_style(self) -> str:
        return " ".join(style for _, style in self.style_stack)

    def _pop_style(self, tag: str) -> None:
        for index in range(len(self.style_stack) - 1, -1, -1):
            if self.style_stack[index][0] == tag:
                del self.style_stack[index]
                break


def clean_block_text(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    text = normalize_typography(text)
    text = text.replace("•", "• ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_typography(text: str) -> str:
    return (
        text.replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )


def is_hidden_style(style: str) -> bool:
    compact = re.sub(r"\s+", "", style.lower())
    return "display:none" in compact or "visibility:hidden" in compact


def is_bold_style(style: str) -> bool:
    compact = re.sub(r"\s+", "", style.lower())
    return "font-weight:700" in compact or "font-weight:bold" in compact


def html_to_blocks(html_text: str) -> list[FilingBlock]:
    parser = FilingBlockExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.blocks


def html_to_structure_events(html_text: str) -> list[FilingStructureEvent]:
    parser = FilingStructureExtractor()
    parser.feed(html_text)
    parser.close()
    return parser.events


def is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def normalize_source(source: str) -> str:
    """Accept plain SEC URLs and common pasted Markdown link forms."""
    source = source.strip()
    markdown_match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", source)
    if markdown_match:
        source = markdown_match.group(2)

    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"} and parsed.netloc.endswith("sec.gov") and parsed.path == "/ix":
        doc = parse_qs(parsed.query).get("doc", [""])[0]
        if doc.startswith("/Archives/"):
            source = f"{parsed.scheme}://{parsed.netloc}{doc}"
    return source


def fetch_url_bytes(url: str, user_agent: str, accept: str = "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8") -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
            "Accept": accept,
        },
    )
    with urlopen(request, timeout=60) as response:
        return response.read()


def fetch_json(url: str, user_agent: str) -> dict:
    data = fetch_url_bytes(url, user_agent, accept="application/json")
    try:
        result = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"SEC returned non-JSON data for {url}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected SEC JSON response for {url}")
    return result


def normalize_cik(value: str) -> str:
    digits = re.sub(r"^CIK", "", value.strip(), flags=re.IGNORECASE)
    if not re.fullmatch(r"\d{1,10}", digits) or int(digits) == 0:
        raise ValueError("CIK must contain 1-10 digits.")
    return f"{int(digits):010d}"


def resolve_ticker_cik(ticker: str, user_agent: str) -> str:
    wanted = ticker.strip().upper()
    if not wanted:
        raise ValueError("Ticker cannot be blank.")
    directory = fetch_json(SEC_TICKERS_URL, user_agent)
    matches = [
        entry
        for entry in directory.values()
        if isinstance(entry, dict) and str(entry.get("ticker", "")).upper() == wanted
    ]
    if len(matches) != 1:
        raise ValueError(f"Ticker {wanted!r} did not resolve uniquely. Use --cik instead.")
    return normalize_cik(str(matches[0]["cik_str"]))


def submissions_rows(columnar: dict) -> list[dict]:
    count = len(columnar.get("accessionNumber", []))
    required = ("accessionNumber", "form", "reportDate", "primaryDocument")
    if any(len(columnar.get(key, [])) != count for key in required):
        raise RuntimeError("SEC submissions arrays have inconsistent lengths.")
    return [
        {key: value[index] for key, value in columnar.items() if isinstance(value, list) and index < len(value)}
        for index in range(count)
    ]


def sec_document_url(cik: str, filing: dict) -> str:
    accession = str(filing.get("accessionNumber", ""))
    document = str(filing.get("primaryDocument", ""))
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError(f"Invalid SEC accession number: {accession}")
    if not document or document.startswith("/") or ".." in document.split("/"):
        raise ValueError("SEC filing has an invalid primary document path.")
    return f"{SEC_ARCHIVES_BASE}/{int(cik)}/{accession.replace('-', '')}/{quote(document, safe='/')}"


def discover_10k_filing(cik: str, fiscal_year: str, user_agent: str) -> dict:
    submissions = fetch_json(f"{SEC_SUBMISSIONS_BASE}/CIK{cik}.json", user_agent)
    filings = submissions.get("filings", {})
    recent = filings.get("recent", {})
    rows = submissions_rows(recent)
    history = list(filings.get("files", []))

    candidates = matching_10k_rows(rows, fiscal_year)
    loaded_history: list[str] = []
    if not candidates and history:
        for entry in history:
            name = str(entry.get("name", ""))
            if not re.fullmatch(r"CIK\d{10}-submissions-\d+\.json", name):
                raise RuntimeError(f"Unrecognized SEC historical submissions filename: {name}")
            historical = fetch_json(f"{SEC_SUBMISSIONS_BASE}/{name}", user_agent)
            rows.extend(submissions_rows(historical.get("filings", {}).get("recent", historical)))
            loaded_history.append(name)
            candidates = matching_10k_rows(rows, fiscal_year)
            if candidates:
                break

    if not candidates:
        available = ", ".join(
            sorted({str(row.get("reportDate", "")) for row in rows if row.get("form") == "10-K" and row.get("reportDate")})
        )
        raise ValueError(f"No original 10-K was found for fiscal year {fiscal_year}. Available report dates: {available}")
    if len(candidates) > 1:
        choices = "; ".join(
            f"{row.get('accessionNumber')} report {row.get('reportDate')} filed {row.get('filingDate')}"
            for row in candidates
        )
        raise ValueError(f"Multiple 10-K filings match fiscal year {fiscal_year}: {choices}. Use a direct filing URL.")
    return candidates[0]


def matching_10k_rows(rows: list[dict], fiscal_year: str) -> list[dict]:
    return [
        row
        for row in rows
        if row.get("form") == "10-K" and str(row.get("reportDate", "")).startswith(fiscal_year)
    ]


def load_filing_from_sec_api(args: argparse.Namespace) -> tuple[str, str, str]:
    if not args.user_agent:
        raise ValueError("SEC API extraction requires --user-agent or SEC_USER_AGENT with your name/email.")
    if not args.year:
        raise ValueError("SEC API extraction requires --year, e.g. --year 2024.")
    cik = resolve_ticker_cik(args.ticker, args.user_agent) if args.ticker else normalize_cik(args.cik)
    filing = discover_10k_filing(cik, args.year, args.user_agent)
    source_url = sec_document_url(cik, filing)
    company = (args.company or args.ticker or args.cik or "unknown").lower()
    html_text = fetch_url_bytes(source_url, args.user_agent).decode("utf-8", errors="replace")
    return html_text, source_url, company


def html_to_clean_text(html_text: str) -> str:
    parser = FilingTextExtractor()
    parser.feed(html_text)
    parser.close()
    text = parser.get_text()
    text = html.unescape(text)
    text = normalize_typography(text)
    text = text.replace("\r", "\n").replace("\t", " ")
    text = re.sub(r"[ \f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_item(item: str) -> str:
    return item.upper().replace(" ", "")


def find_item_headings(text: str) -> list[Heading]:
    item_pattern = re.compile(
        r"^\s*(?:part\s+[ivxlcdm]+\s*)?"
        r"item\s+"
        r"(1A|1B|1C|7A|9A|9B|9C|15|16|1|2|3|4|5|6|7|8|9)"
        r"\s*[\.\-:)]?\s*"
        r"([A-Z][A-Za-z0-9 ,;&'’()/.-]{0,120})?\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    headings: list[Heading] = []
    for match in item_pattern.finditer(text):
        line = match.group(0).strip()
        if not is_probable_heading(line):
            continue
        item = normalize_item(match.group(1))
        title = (match.group(2) or "").strip()
        headings.append(Heading(item=item, title=title, start=match.start(), end=match.end(), line=line))
    return headings


def parse_item_heading(text: str) -> tuple[str, str] | None:
    match = re.fullmatch(
        r"\s*(?:part\s+[ivxlcdm]+\s*)?"
        r"item\s+"
        r"(1A|1B|1C|7A|9A|9B|9C|15|16|1|2|3|4|5|6|7|8|9)"
        r"\s*[\.\-:)]?\s*"
        r"(.{0,140})\s*",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    line = re.sub(r"\s+", " ", text).strip()
    if not is_probable_heading(line):
        return None
    return normalize_item(match.group(1)), match.group(2).strip()


def is_probable_heading(line: str) -> bool:
    cleaned = re.sub(r"\s+", " ", line).strip()
    if len(cleaned) > 160:
        return False
    if cleaned.count(".") > 3:
        return False
    if re.search(r"\b(page|see|included|contained|above|below)\b", cleaned, re.IGNORECASE):
        return False
    return True


def is_terminal_section_heading(text: str) -> bool:
    return bool(re.fullmatch(r"\s*signatures?\s*", text, re.IGNORECASE))


def terminal_section_start(text: str, start_position: int) -> int | None:
    match = re.search(r"^\s*signatures?\s*$", text[start_position:], re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return start_position + match.start()


def extract_item15_toc_section_blocks(html_text: str) -> list[FilingBlock] | None:
    events = html_to_structure_events(html_text)
    item_range = item15_event_range(events)
    if item_range is None:
        return None

    start_index, end_index = item_range
    toc_table_index, toc_entries = find_item15_toc(events, start_index, end_index)
    if toc_table_index is None or not is_supported_item15_toc(toc_entries):
        return None

    entries_by_key = {entry.match_key: entry for entry in toc_entries}
    seen_toc_headers: set[str] = set()
    blocks: list[FilingBlock] = []
    for event in events[start_index + 1 : end_index]:
        if event.index == toc_table_index:
            continue
        if event.kind == "table":
            blocks.extend(toc_header_blocks_from_table(event, entries_by_key, seen_toc_headers))
            continue
        if event.text.casefold() == "table of contents" or should_drop_line(event.text):
            continue
        if parse_item_heading(event.text):
            continue

        toc_entry = entries_by_key.get(toc_match_key(event.text))
        if toc_entry:
            if toc_entry.match_key not in seen_toc_headers:
                seen_toc_headers.add(toc_entry.match_key)
                blocks.append(toc_header_block(event.index, toc_entry.title))
        else:
            blocks.append(
                FilingBlock(
                    index=event.index,
                    tag="toc_text",
                    text=event.text,
                    style="",
                    bold=False,
                )
            )

    return blocks or None


def item15_event_range(events: list[FilingStructureEvent]) -> tuple[int, int] | None:
    starts = [index for index, event in enumerate(events) if event.kind != "table" and parse_item_heading(event.text) == ("15", "Exhibits, Financial Statement Schedules.")]
    if not starts:
        starts = [
            index
            for index, event in enumerate(events)
            if event.kind != "table" and (heading := parse_item_heading(event.text)) is not None and heading[0] == "15"
        ]
    if not starts:
        return None

    candidates: list[tuple[int, int, int]] = []
    for start_index in starts:
        end_index = next(
            (
                index
                for index, event in enumerate(events[start_index + 1 :], start=start_index + 1)
                if event.kind != "table"
                and (
                    is_terminal_section_heading(event.text)
                    or ((heading := parse_item_heading(event.text)) is not None and heading[0] in ITEM_ENDS["15"])
                )
            ),
            len(events),
        )
        text_length = sum(len(event.text) for event in events[start_index + 1 : end_index] if event.kind != "table")
        candidates.append((text_length, start_index, end_index))

    _, start_index, end_index = max(candidates, key=lambda candidate: candidate[0])
    return start_index, end_index


def find_item15_toc(
    events: list[FilingStructureEvent],
    start_index: int,
    end_index: int,
) -> tuple[int | None, list[TocEntry]]:
    toc_index = next(
        (
            index
            for index, event in enumerate(events[start_index + 1 : end_index], start=start_index + 1)
            if event.kind != "table" and event.text.casefold() == "table of contents"
        ),
        None,
    )
    if toc_index is None:
        return None, []

    for index, event in enumerate(events[toc_index + 1 : end_index], start=toc_index + 1):
        if event.kind == "table":
            return event.index, toc_entries_from_table(event)
    return None, []


def toc_entries_from_table(event: FilingStructureEvent) -> list[TocEntry]:
    entries: list[TocEntry] = []
    seen: set[str] = set()
    for row in event.rows:
        cells = [cell for cell in row if cell]
        for index, cell in enumerate(cells):
            if not is_plausible_toc_title(cell):
                continue
            page = ""
            if index + 1 < len(cells) and is_page_number(cells[index + 1]):
                page = cells[index + 1]
            elif index > 0 and is_page_number(cells[index - 1]):
                page = cells[index - 1]

            title = canonical_toc_title(cell)
            normalized = normalize_heading_title(title)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            entries.append(TocEntry(title=title, normalized=normalized, match_key=toc_match_key(title), page=page))
    return entries


def is_supported_item15_toc(entries: list[TocEntry]) -> bool:
    if len(entries) < 4:
        return False
    normalized_titles = {entry.normalized for entry in entries}
    narrative_markers = {
        "management s discussion and analysis",
        "executive overview",
        "firmwide risk management",
        "critical accounting estimates used by the firm",
    }
    return bool(normalized_titles & narrative_markers)


def toc_header_blocks_from_table(
    event: FilingStructureEvent,
    entries_by_key: dict[str, TocEntry],
    seen_toc_headers: set[str],
) -> list[FilingBlock]:
    blocks: list[FilingBlock] = []
    for row in event.rows:
        for cell in row:
            entry = entries_by_key.get(toc_match_key(cell))
            if entry is None or entry.match_key in seen_toc_headers:
                continue
            seen_toc_headers.add(entry.match_key)
            blocks.append(toc_header_block(event.index, entry.title))
    return blocks


def toc_header_block(index: int, title: str) -> FilingBlock:
    return FilingBlock(index=index, tag="toc_header", text=title, style="", bold=True)


def canonical_toc_title(title: str) -> str:
    title = re.sub(r"\s+", " ", normalize_typography(title)).strip()
    return title.strip(" :")


def normalize_heading_title(title: str) -> str:
    title = canonical_toc_title(title).casefold()
    title = title.replace("&", "and")
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def toc_match_key(title: str) -> str:
    title = re.sub(r"\([^)]*\)", " ", canonical_toc_title(title)).casefold()
    title = title.replace("&", "and")
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def is_plausible_toc_title(text: str) -> bool:
    text = canonical_toc_title(text)
    if not text or is_page_number(text):
        return False
    if len(text) > 130:
        return False
    if re.search(r"\b(jpmorgan chase & co\.|form 10-k|page)\b", text, re.IGNORECASE):
        return False
    return bool(re.search(r"[A-Za-z]", text))


def is_page_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,4}", text.strip()))


def extract_section_blocks(
    blocks: list[FilingBlock],
    items: Iterable[str] = DEFAULT_ITEMS,
) -> dict[str, list[FilingBlock]]:
    headings: list[tuple[int, str]] = []
    for index, block in enumerate(blocks):
        heading = parse_item_heading(block.text)
        if heading:
            headings.append((index, heading[0]))

    sections: dict[str, list[FilingBlock]] = {}
    for item in [normalize_item(value) for value in items]:
        candidates: list[tuple[int, list[FilingBlock]]] = []
        end_items = set(ITEM_ENDS[item])
        starts = [heading for heading in headings if heading[1] == item]

        for start_index, _ in starts:
            end_candidates = [index for index, heading_item in headings if index > start_index and heading_item in end_items]
            if item == "15":
                end_candidates.extend(
                    index
                    for index, block in enumerate(blocks)
                    if index > start_index and is_terminal_section_heading(block.text)
                )
            end_index = min(end_candidates) if end_candidates else None
            if end_index is None and item == "15":
                end_index = len(blocks)
            if end_index is None:
                continue
            candidate = [
                block
                for block in blocks[start_index + 1 : end_index]
                if block.text and not should_drop_line(block.text) and parse_item_heading(block.text) is None
            ]
            length = sum(len(block.text) for block in candidate)
            if length:
                candidates.append((length, candidate))

        if candidates:
            sections[item] = max(candidates, key=lambda candidate: candidate[0])[1]

    return sections


def extract_sections(text: str, items: Iterable[str] = DEFAULT_ITEMS) -> dict[str, str]:
    headings = find_item_headings(text)
    sections: dict[str, str] = {}

    for item in [normalize_item(value) for value in items]:
        candidates: list[tuple[int, str]] = []
        end_items = set(ITEM_ENDS[item])
        starts = [heading for heading in headings if heading.item == item]

        for start in starts:
            end_candidates = [heading.start for heading in headings if heading.start > start.end and heading.item in end_items]
            if item == "15":
                terminal_start = terminal_section_start(text, start.end)
                if terminal_start is not None:
                    end_candidates.append(terminal_start)
            end_position = min(end_candidates) if end_candidates else None
            if end_position is None and item == "15":
                end_position = len(text)
            if end_position is None:
                continue
            content = text[start.end : end_position].strip()
            content = cleanup_section_text(content)
            if content:
                candidates.append((len(content), content))

        if candidates:
            sections[item] = max(candidates, key=lambda candidate: candidate[0])[1]

    return sections


def section_blocks_to_text(section_blocks: dict[str, list[FilingBlock]]) -> dict[str, str]:
    sections: dict[str, str] = {}
    for item, blocks in section_blocks.items():
        lines: list[str] = []
        for block in blocks:
            if is_subheader_block(block):
                if lines and lines[-1]:
                    lines.append("")
                lines.append(block.text)
                lines.append("")
            else:
                lines.append(block.text)
                lines.append("")
        sections[item] = "\n".join(lines).strip()
    return sections


def merge_section_blocks(section_blocks: dict[str, list[FilingBlock]]) -> dict[str, list[FilingBlock]]:
    return {item: merge_continued_blocks(blocks) for item, blocks in section_blocks.items()}


def merge_continued_blocks(blocks: list[FilingBlock]) -> list[FilingBlock]:
    merged: list[FilingBlock] = []
    pending: FilingBlock | None = None

    for block in blocks:
        if pending is None:
            pending = block
            continue

        if should_merge_with_next_block(pending, block):
            pending = FilingBlock(
                index=pending.index,
                tag=pending.tag,
                text=join_continued_text(pending.text, block.text),
                style=pending.style,
                bold=pending.bold,
            )
        else:
            merged.append(pending)
            pending = block

    if pending is not None:
        merged.append(pending)
    return merged


def should_merge_with_next_block(previous: FilingBlock, current: FilingBlock) -> bool:
    if is_subheader_block(previous) or is_subheader_block(current):
        return False
    if starts_with_bullet(current.text):
        return True
    return not ends_with_sentence_terminal(previous.text)


def starts_with_bullet(text: str) -> bool:
    return bool(BULLET_PREFIX_RE.match(text))


def ends_with_sentence_terminal(text: str) -> bool:
    return bool(re.search(r"[.!?]\s*[\"')\]]*$", text.strip()))


def join_continued_text(previous: str, current: str) -> str:
    previous = previous.rstrip()
    current = current.lstrip()
    if starts_with_bullet(current):
        return f"{previous}\n{current}".strip()
    if previous.endswith("-"):
        return previous + current
    return f"{previous} {current}".strip()


def cleanup_section_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if should_drop_line(line):
            continue
        lines.append(line)

    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def should_drop_line(line: str) -> bool:
    if not line:
        return False
    if is_repeated_filing_header(line):
        return True
    if re.fullmatch(r"\d{1,4}", line):
        return True
    if re.fullmatch(r"[-–—_ ]{3,}", line):
        return True
    if re.fullmatch(r"table of contents", line, re.IGNORECASE):
        return True
    if re.fullmatch(r"index", line, re.IGNORECASE):
        return True
    if re.fullmatch(r"part\s+[ivxlcdm]+", line, re.IGNORECASE):
        return True
    return False


def is_repeated_filing_header(line: str) -> bool:
    cleaned = re.sub(r"\s+", " ", normalize_typography(line)).strip()
    if re.fullmatch(r"\(?continued\)?", cleaned, re.IGNORECASE):
        return True
    if re.fullmatch(r"notes to (the )?consolidated financial statements", cleaned, re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z0-9&.,' -]+ (corporation|corp\.?|company|co\.?) and subsidiaries", cleaned, re.IGNORECASE):
        return True
    return False


def is_subheader_block(block: FilingBlock) -> bool:
    if block.tag == "toc_header":
        return True
    if block.tag == "toc_text":
        return False

    text = block.text.strip()
    if not text or parse_item_heading(text) or should_drop_line(text):
        return False
    if starts_with_bullet(text):
        return False
    if len(text) > 140:
        return False
    if len(text.split()) > 14:
        return False
    if re.search(r"[.!?]$", text):
        return False
    if re.search(r"\b(or|and|the|a|an|of|to|for|with|from|in|on)\b", text, re.IGNORECASE) and not block.bold:
        return False
    return block.bold or block.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or looks_like_title(text)


def looks_like_title(text: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z'’&-]*", text)
    if not words:
        return False
    title_words = sum(1 for word in words if word[:1].isupper() or word.isupper())
    return title_words / len(words) >= 0.65


def split_into_chunks(text: str, max_chars: int = 1800, min_chars: int = 120) -> list[str]:
    paragraphs = [normalize_paragraph(part) for part in re.split(r"\n\s*\n+", text)]
    paragraphs = [part for part in paragraphs if part]

    merged: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        if len(buffer) < min_chars:
            buffer = f"{buffer}\n\n{paragraph}".strip() if buffer else paragraph
            continue
        merged.append(buffer)
        buffer = paragraph
    if buffer:
        merged.append(buffer)

    chunks: list[str] = []
    for paragraph in merged:
        chunks.extend(split_long_paragraph(paragraph, max_chars=max_chars))
    return [chunk for chunk in chunks if chunk.strip()]


def normalize_paragraph(paragraph: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in paragraph.splitlines()]
    lines = [line for line in lines if line and not should_drop_line(line)]
    return " ".join(lines).strip()


def split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]

    sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", paragraph)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if not current:
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    final_chunks: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            final_chunks.append(chunk)
        else:
            final_chunks.extend(textwrap.wrap(chunk, width=max_chars, break_long_words=False, break_on_hyphens=False))
    return final_chunks


def infer_year(text: str, source: str | None = None) -> str:
    patterns = [
        r"CONFORMED PERIOD OF REPORT:\s*(20\d{2})\d{4}",
        r"fiscal year ended\s+[A-Za-z]+\s+\d{1,2},\s+(20\d{2})",
        r"year ended\s+[A-Za-z]+\s+\d{1,2},\s+(20\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    if source:
        match = re.search(r"(20\d{2})", source)
        if match:
            return match.group(1)
    return "unknown"


def infer_company_year_from_filename(path: Path) -> tuple[str, str | None]:
    """Infer company/year from names like nvda-20230129.htm."""
    stem = path.stem
    match = re.match(r"([A-Za-z0-9]+)-(20\d{2})", stem)
    if match:
        return match.group(1).lower(), match.group(2)

    company = re.split(r"[-_]", stem, maxsplit=1)[0].lower()
    return (company or "unknown", None)


def make_chunk_id(year: str, item: str, item_chunk_index: int) -> str:
    return f"{year}_{item}_P{item_chunk_index:03d}"


def build_records(
    sections: dict[str, str],
    year: str,
    company: str,
    source: str,
    max_chars: int,
    min_chars: int,
) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    for item in DEFAULT_ITEMS:
        if item not in sections:
            continue
        for chunk_index, chunk in enumerate(split_into_chunks(sections[item], max_chars=max_chars, min_chars=min_chars), start=1):
            records.append(
                {
                    "id": make_chunk_id(year, item, chunk_index),
                    "company": company,
                    "year": year,
                    "item": item,
                    "item_default_title": ITEM_TITLES[item],
                    "item_title": ITEM_TITLES[item],
                    "item_chunk_index": chunk_index,
                    "text": chunk,
                    "source": source,
                }
            )
    return records


def build_records_from_section_blocks(
    section_blocks: dict[str, list[FilingBlock]],
    year: str,
    company: str,
    source: str,
    max_chars: int,
) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []

    for item in DEFAULT_ITEMS:
        if item not in section_blocks:
            continue

        current_subheader = ITEM_TITLES[item]
        item_chunk_index = 1
        for block in section_blocks[item]:
            if is_subheader_block(block):
                current_subheader = block.text
                continue

            for text in split_long_paragraph(block.text, max_chars=max_chars):
                records.append(
                    {
                        "id": make_chunk_id(year, item, item_chunk_index),
                        "company": company,
                        "year": year,
                        "item": item,
                        "item_default_title": ITEM_TITLES[item],
                        "item_title": current_subheader,
                        "item_chunk_index": item_chunk_index,
                        "source_block_index": block.index,
                        "text": text,
                        "source": source,
                    }
                )
                item_chunk_index += 1

    return records


def write_outputs(records: list[dict[str, str | int]], sections: dict[str, str], out_dir: Path, year: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / f"{year}_chunks.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    txt_lines = []
    for record in records:
        txt_lines.append(f"[{record['id']}] Item {record['item']} - {record['item_title']}")
        txt_lines.append(str(record["text"]))
        txt_lines.append("")
    (out_dir / f"{year}_chunks.txt").write_text("\n".join(txt_lines).strip() + "\n", encoding="utf-8")

    for item, section_text in sections.items():
        safe_item = item.lower().replace("a", "a")
        (out_dir / f"{year}_item_{safe_item}.txt").write_text(section_text + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract Item 1, 1A, 7, 8, and 15 from a SEC 10-K filing.")
    parser.add_argument("source", nargs="?", help="Optional SEC filing HTML URL or local .html/.htm/.txt file.")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--ticker", help="SEC ticker for API extraction, e.g. NVDA.")
    identity.add_argument("--cik", help="SEC CIK for API extraction.")
    parser.add_argument("--company", help="Company folder/name override. Defaults to ticker or filing filename.")
    parser.add_argument("--year", help="Fiscal year / chunk ID prefix, e.g. 2024. Required for API extraction.")
    parser.add_argument("--out-dir", default="data/raw", help="Directory for extracted TXT/JSON files. Default: data/raw.")
    parser.add_argument("--raw-dir", help=argparse.SUPPRESS)
    parser.add_argument("--max-chars", type=int, default=1800, help="Maximum characters per disclosure chunk.")
    parser.add_argument("--min-chars", type=int, default=120, help="Small paragraphs are merged until roughly this size.")
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_USER_AGENT", ""),
        help="SEC download User-Agent. Prefer setting SEC_USER_AGENT='Name email@example.com'.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    api_mode = bool(args.ticker or args.cik)

    try:
        if api_mode:
            if args.source:
                raise ValueError("Use either --ticker/--cik API extraction or a source file/URL, not both.")
            raw_html, source, company = load_filing_from_sec_api(args)
            filename_year = None
            source_label = f"Source URL: {source}"
        else:
            if not args.source:
                raise ValueError("Provide --ticker/--cik with --year, or provide a local filing path / SEC filing URL.")
            source = normalize_source(args.source)
            if is_url(source):
                if urlparse(source).netloc.endswith("sec.gov") and not args.user_agent:
                    raise ValueError("SEC URL extraction requires --user-agent or SEC_USER_AGENT with your name/email.")
                raw_html = fetch_url_bytes(source, args.user_agent).decode("utf-8", errors="replace")
                company, filename_year = infer_company_year_from_filename(Path(urlparse(source).path))
                source_label = f"Source URL: {source}"
            else:
                filing_path = Path(source)
                if not filing_path.exists():
                    print(f"Input file not found: {filing_path}", file=sys.stderr)
                    return 2
                raw_html = filing_path.read_text(encoding="utf-8", errors="replace")
                company, filename_year = infer_company_year_from_filename(filing_path)
                source_label = f"Source file: {filing_path}"
            company = (args.company or company).lower()
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.raw_dir:
        print("Warning: --raw-dir is deprecated and ignored; source HTML is no longer saved.", file=sys.stderr)

    blocks = html_to_blocks(raw_html)
    section_blocks = extract_section_blocks(blocks)
    item15_toc_blocks = extract_item15_toc_section_blocks(raw_html)
    if item15_toc_blocks:
        section_blocks["15"] = item15_toc_blocks
    section_blocks = merge_section_blocks(section_blocks)
    clean_text = html_to_clean_text(raw_html)
    year = args.year or filename_year or infer_year(raw_html + "\n" + clean_text, source)

    if section_blocks:
        sections = section_blocks_to_text(section_blocks)
        records = build_records_from_section_blocks(
            section_blocks=section_blocks,
            year=year,
            company=company,
            source=source,
            max_chars=args.max_chars,
        )
    else:
        sections = extract_sections(clean_text)
        records = build_records(
            sections=sections,
            year=year,
            company=company,
            source=source,
            max_chars=args.max_chars,
            min_chars=args.min_chars,
        )

    missing = [item for item in DEFAULT_ITEMS if item not in sections]
    if missing:
        print(f"Warning: could not find Item(s): {', '.join(missing)}", file=sys.stderr)

    output_dir = Path(args.out_dir) / company / year
    write_outputs(records, sections, output_dir, year)

    print(source_label)
    print(f"Company: {company}")
    print(f"Year: {year}")
    print(f"Output dir: {output_dir}")
    print(f"Sections extracted: {', '.join(sections) if sections else 'none'}")
    print(f"Chunks written: {len(records)}")
    return 0 if sections else 1


if __name__ == "__main__":
    raise SystemExit(main())
