#!/usr/bin/env python3
"""Download a SEC 10-K filing through the SEC API and extract cleaned Item sections.

The pipeline is intentionally small and dependency-free:

    SEC ticker/year -> filing HTML -> cleaned text -> Items 1, 1A, 7, 8 -> chunks

Outputs are JSON and TXT files with stable paragraph IDs such as nvda_2024_1_P001.
Items incorporated from a separate HTML annual report are resolved through the
filing index and the report's own section headings/TOC.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, urljoin, urlparse
from urllib.request import Request, urlopen


SUPPORTED_ITEMS = ("1", "1A", "7", "8", "15")
DEFAULT_ITEMS = ("1", "1A", "7", "8")
ITEM_OUTPUT_ORDER = SUPPORTED_ITEMS
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_BASE = "https://data.sec.gov/submissions"
AUTO_ITEM15_POINTER_MAX_CHARS = 1800
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
SENTENCE_BULLET_RE = re.compile(r"^\s*(?:[•‣▪▫◦●○]|\*\s+|-\s+)")
FOOTNOTE_REF_RE = re.compile(r"\[\[FNREF:(\d{1,3})\]\]")
PERIOD_TOKEN = "<PERIOD>"
COMMON_ABBREVIATIONS = (
    "Co.",
    "Corp.",
    "Dr.",
    "Inc.",
    "Jr.",
    "Ltd.",
    "Mr.",
    "Mrs.",
    "Ms.",
    "No.",
    "Prof.",
    "Sr.",
    "U.S.",
    "U.K.",
    "e.g.",
    "i.e.",
)
TITLE_CONNECTOR_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
CONTEXTUAL_CHILD_TITLES = {
    "overview",
    "market trends",
    "competition",
    "customers",
    "products",
    "services",
    "financial performance",
    "operating performance",
}


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
    mixed_bold: bool = False
    font_size: float | None = None
    segments: tuple[BlockSegment, ...] = ()


@dataclass(frozen=True)
class FilingStructureEvent:
    index: int
    kind: str
    text: str
    rows: tuple[tuple[str, ...], ...] = ()
    style: str = ""
    bold: bool = False
    cell_bold: tuple[tuple[bool, ...], ...] = ()
    cell_styles: tuple[tuple[str, ...], ...] = ()
    mixed_bold: bool = False
    font_size: float | None = None


@dataclass(frozen=True)
class TocEntry:
    title: str
    normalized: str
    match_key: str
    page: str = ""
    level: int = 0


@dataclass(frozen=True)
class InlineItemReference:
    item: str
    title: str
    pages: tuple[int, ...] = ()


@dataclass(frozen=True)
class SectionHeading:
    title: str
    font_size: float | None = None
    caps_priority: int = 0


@dataclass(frozen=True)
class SentenceUnit:
    text: str
    bullet_level: int | None = None
    bullet_indent_pt: float | None = None


@dataclass(frozen=True)
class BlockSegment:
    text: str
    bullet_level: int | None = None
    bullet_indent_pt: float | None = None


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
        self.style_stack: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_by_name = {name.lower(): value or "" for name, value in attrs}
        style = attrs_by_name.get("style", "")
        if tag in self.DROP_TAGS:
            self.drop_stack.append(tag)
            return
        if self.drop_stack:
            return
        if tag != "br":
            self.style_stack.append((tag, style))
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
        for index in range(len(self.style_stack) - 1, -1, -1):
            if self.style_stack[index][0] == tag:
                del self.style_stack[index]
                break

    def handle_data(self, data: str) -> None:
        if self.drop_stack:
            return
        data = html.unescape(data).replace("\xa0", " ")
        if data.strip():
            if is_superscript_footnote_marker(data, self.style_stack):
                data = footnote_marker_text(data)
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
                    "bold": False,
                    "plain": False,
                    "font_size": None,
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
                style = str(block["style"])
                self.blocks.append(
                    FilingBlock(
                        index=len(self.blocks) + 1,
                        tag=str(block["tag"]),
                        text=text,
                        style=style,
                        bold=bool(block["bold"]),
                        mixed_bold=bool(block["bold"] and block["plain"]),
                        font_size=block["font_size"],  # type: ignore[arg-type]
                        segments=(BlockSegment(text, bullet_indent_pt=bullet_indent_from_style(text, style)),),
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
        if is_superscript_footnote_marker(data, self.style_stack):
            data = footnote_marker_text(data)
        self._append(data)
        if self.block_stack and any(char.isalnum() for char in data):
            key = "bold" if is_bold_text(self.style_stack) else "plain"
            self.block_stack[-1][key] = True
            font_size = current_font_size(self.style_stack)
            if font_size is not None:
                existing = self.block_stack[-1]["font_size"]
                self.block_stack[-1]["font_size"] = max(existing, font_size) if existing is not None else font_size

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
        self.table_cell_bold: list[list[bool]] = []
        self.table_cell_styles: list[list[str]] = []
        self.current_row_bold: list[bool] = []
        self.current_row_styles: list[str] = []
        self.current_cell_bold = False
        self.current_cell_style = ""
        self.table_font_size: float | None = None
        self.current_cell_font_size: float | None = None
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
                self.table_cell_bold = []
                self.table_cell_styles = []
                self.table_font_size = None
            self.table_depth += 1
            return

        if self.table_depth:
            if tag == "tr":
                self.current_row = []
                self.current_row_bold = []
                self.current_row_styles = []
            elif tag in {"td", "th"}:
                self.current_cell_parts = []
                self.current_cell_bold = tag == "th"
                self.current_cell_style = style
                self.current_cell_font_size = None
            elif tag == "br" and self.current_cell_parts is not None:
                self.current_cell_parts.append(" ")
            return

        if tag in self.BLOCK_TAGS:
            self.block_stack.append(
                {
                    "tag": tag,
                    "style": style,
                    "parts": [],
                    "bold": False,
                    "plain": False,
                    "font_size": None,
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
                    self.current_row_bold.append(self.current_cell_bold)
                    self.current_row_styles.append(self.current_cell_style)
                    if self.current_cell_font_size is not None:
                        self.table_font_size = (
                            max(self.table_font_size, self.current_cell_font_size)
                            if self.table_font_size is not None
                            else self.current_cell_font_size
                        )
                self.current_cell_parts = None
                self.current_cell_style = ""
                self.current_cell_font_size = None
            elif tag == "tr" and self.current_row is not None:
                if any(cell for cell in self.current_row):
                    self.table_rows.append(self.current_row)
                    self.table_cell_bold.append(self.current_row_bold)
                    self.table_cell_styles.append(self.current_row_styles)
                self.current_row = None
            elif tag == "table":
                self.table_depth -= 1
                if self.table_depth == 0:
                    rows = tuple(tuple(cell for cell in row) for row in self.table_rows)
                    text = clean_block_text(" ".join(cell for row in rows for cell in row if cell))
                    if text:
                        self.events.append(
                            FilingStructureEvent(index=len(self.events) + 1, kind="table", text=text, rows=rows,
                                                 cell_bold=tuple(tuple(row) for row in self.table_cell_bold),
                                                 cell_styles=tuple(tuple(row) for row in self.table_cell_styles),
                                                 font_size=self.table_font_size)
                        )
                    self.table_font_size = None
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
                        style=str(block["style"]),
                        bold=bool(block["bold"]),
                        mixed_bold=bool(block["bold"] and block["plain"]),
                        font_size=block["font_size"],  # type: ignore[arg-type]
                    )
                )

        self._pop_style(tag)

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        data = html.unescape(data).replace("\xa0", " ")
        if not data.strip():
            return
        if is_superscript_footnote_marker(data, self.style_stack):
            data = footnote_marker_text(data)

        if self.table_depth:
            if self.current_cell_parts is not None:
                self.current_cell_parts.append(data)
                current_style = self._current_style()
                if current_style and current_style not in self.current_cell_style:
                    self.current_cell_style = f"{self.current_cell_style} {current_style}".strip()
                if is_bold_style(self._current_style()) or any(tag in {"b", "strong"} for tag, _ in self.style_stack):
                    self.current_cell_bold = True
                font_size = current_font_size(self.style_stack)
                if font_size is not None:
                    self.current_cell_font_size = (
                        max(self.current_cell_font_size, font_size)
                        if self.current_cell_font_size is not None
                        else font_size
                    )
        else:
            self._append_to_block(data)
            if self.block_stack and any(char.isalnum() for char in data):
                key = "bold" if is_bold_text(self.style_stack) else "plain"
                self.block_stack[-1][key] = True
                font_size = current_font_size(self.style_stack)
                if font_size is not None:
                    existing = self.block_stack[-1]["font_size"]
                    self.block_stack[-1]["font_size"] = max(existing, font_size) if existing is not None else font_size

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


def is_bold_text(style_stack: list[tuple[str, str]]) -> bool:
    """Resolve emphasis for this text run, including a child's normal override."""
    for tag, style in reversed(style_stack):
        weights = re.findall(r"font-weight\s*:\s*(bold|normal|[1-9]00)\b", style, re.IGNORECASE)
        if weights:
            return weights[-1].lower() in {"bold", "700", "800", "900"}
        if tag in {"b", "strong"}:
            return True
    return False


def current_font_size(style_stack: list[tuple[str, str]]) -> float | None:
    for _, style in reversed(style_stack):
        match = re.search(r"font-size\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*(pt|px)", style, re.IGNORECASE)
        if not match:
            continue
        return css_length_to_pt(float(match.group(1)), match.group(2))
    return None


def css_length_to_pt(value: float, unit: str) -> float:
    return value * 0.75 if unit.lower() == "px" else value


def is_superscript_footnote_marker(data: str, style_stack: list[tuple[str, str]]) -> bool:
    marker = data.strip()
    if not re.fullmatch(r"\d{1,3}", marker):
        return False
    style = " ".join(style for _, style in style_stack)
    font_size = current_font_size(style_stack)
    if font_size is not None and font_size <= 7:
        return bool(re.search(r"(?:top\s*:\s*-\s*[0-9.]+pt|vertical-align\s*:\s*(?:super|baseline))", style, re.I))
    return bool(re.search(r"vertical-align\s*:\s*super", style, re.I))


def footnote_marker_text(data: str) -> str:
    return f" [[FNREF:{data.strip()}]] "


def footnote_definition(block: FilingBlock) -> tuple[str, str] | None:
    text = clean_block_text(block.text)
    marker_match = FOOTNOTE_REF_RE.match(text)
    numbered_match = re.match(r"^(\d{1,3})(?:\s+|(?=[A-Z]))(.+)$", text)
    if marker_match:
        number = marker_match.group(1)
        note = text[marker_match.end():].strip()
    elif numbered_match:
        number = numbered_match.group(1)
        note = numbered_match.group(2).strip()
    else:
        return None
    if block.font_size is not None and block.font_size > 8.5:
        return None
    if len(note) < 8 or is_subheader_block(block) or parse_item_heading(text):
        return None
    return number, note


def replace_footnote_reference(
    text: str,
    number: str,
    note: str | None = None,
    *,
    replace_unresolved: bool = False,
) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group(1) != number:
            return match.group(0)
        if note is not None:
            return f" [footnote {number}: {note}] "
        if replace_unresolved:
            return f" [footnote {number}] "
        return match.group(0)

    return clean_block_text(FOOTNOTE_REF_RE.sub(repl, text))


def replace_footnote_reference_in_block(
    block: FilingBlock,
    number: str,
    note: str | None = None,
    *,
    replace_unresolved: bool = False,
) -> FilingBlock:
    segments = tuple(
        replace(
            segment,
            text=replace_footnote_reference(
                segment.text, number, note, replace_unresolved=replace_unresolved,
            ),
        )
        for segment in block_segments(block)
    )
    return replace(
        block,
        text=replace_footnote_reference(block.text, number, note, replace_unresolved=replace_unresolved),
        segments=segments,
    )


def inline_footnote_references(blocks: list[FilingBlock]) -> list[FilingBlock]:
    output: list[FilingBlock] = []
    waiting: dict[str, list[int]] = {}

    for block in blocks:
        definition = footnote_definition(block)
        if definition and definition[0] in waiting:
            number, note = definition
            for output_index in waiting.pop(number):
                output[output_index] = replace_footnote_reference_in_block(output[output_index], number, note)
            continue

        output_index = len(output)
        output.append(block)
        for number in FOOTNOTE_REF_RE.findall(block.text):
            waiting.setdefault(number, []).append(output_index)

    for number, output_indexes in waiting.items():
        for output_index in output_indexes:
            output[output_index] = replace_footnote_reference_in_block(
                output[output_index], number, replace_unresolved=True,
            )

    return output


def resolve_section_footnotes(section_blocks: dict[str, list[FilingBlock]]) -> dict[str, list[FilingBlock]]:
    return {item: inline_footnote_references(blocks) for item, blocks in section_blocks.items()}


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


def item_blocks_with_layout_headings(html_text: str, blocks: list[FilingBlock]) -> list[FilingBlock]:
    """Recover single-row Item labels without retaining financial table data.

    Keep the existing block stream/indices for filings whose Item labels are
    already outside tables. Only switch streams when a table adds an Item.
    """
    existing = {heading[0] for block in blocks if (heading := parse_item_heading(block.text))}
    events = html_to_structure_events(html_text)
    table_items = {
        heading[0] for event in events if event.kind == "table" and len(event.rows) == 1
        if (heading := parse_item_heading(event.text)) and heading[1]
    }
    if not table_items - existing:
        return blocks
    result = []
    for event in events:
        if should_drop_line(event.text):
            continue
        if event.kind == "table":
            if len(event.rows) != 1 or not parse_item_heading(event.text):
                continue
            result.append(FilingBlock(event.index, "div", event.text, bold=True))
        else:
            result.append(FilingBlock(event.index, event.kind, event.text, style=event.style, bold=event.bold,
                                      mixed_bold=event.mixed_bold, font_size=event.font_size,
                                      segments=(BlockSegment(event.text, bullet_indent_pt=bullet_indent_from_style(event.text, event.style)),)))
    return result


class FilingDocumentIndex(HTMLParser):
    """Read document types and links from the SEC filing index."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.documents: list[dict[str, str]] = []
        self.cells: list[str] | None = None
        self.parts: list[str] | None = None
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.cells, self.links = [], []
        elif tag == "td" and self.cells is not None:
            self.parts = []
        elif tag == "a" and self.cells is not None and len(self.cells) == 2:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_data(self, text: str) -> None:
        if self.parts is not None:
            self.parts.append(text)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.parts is not None and self.cells is not None:
            self.cells.append(clean_block_text("".join(self.parts)))
            self.parts = None
        elif tag == "tr" and self.cells is not None:
            if len(self.cells) >= 4 and self.links:
                self.documents.append({"description": self.cells[1], "document": self.cells[2],
                                       "type": self.cells[3], "href": self.links[0]})
            self.cells, self.parts = None, None


def filing_index_source(source: str) -> str:
    if is_url(source):
        parsed = urlparse(source)
        match = re.fullmatch(r"/Archives/edgar/data/\d+/(\d{18})/[^/]+", parsed.path)
        if parsed.hostname not in {"www.sec.gov", "sec.gov"} or not match:
            raise ValueError("Automatic report discovery needs a SEC archive filing URL or a local filing index.")
        accession = match.group(1)
        name = f"{accession[:10]}-{accession[10:12]}-{accession[12:]}-index.htm"
        return urljoin(source, name)
    indexes = list(Path(source).parent.glob("*-index.htm"))
    if len(indexes) != 1:
        raise ValueError("The local filing references another report. Keep its SEC *-index.htm and report HTML beside it, or use the SEC ticker/year command.")
    return str(indexes[0])


def read_html_source(source: str, user_agent: str) -> str:
    if is_url(source):
        if not user_agent.strip():
            raise ValueError("Referenced-report downloads require --user-agent or SEC_USER_AGENT.")
        return fetch_url_bytes(source, user_agent).decode("utf-8", errors="replace")
    return Path(source).read_text(encoding="utf-8", errors="replace")


def discover_referenced_report(source: str, user_agent: str) -> tuple[str, dict[str, str]]:
    index_source = filing_index_source(source)
    parser = FilingDocumentIndex()
    parser.feed(read_html_source(index_source, user_agent))
    candidates = [doc for doc in parser.documents if re.fullmatch(r"EX-13(?:\.\d+)?", doc["type"], re.I)]
    if not candidates:
        candidates = [doc for doc in parser.documents if doc["type"].upper().startswith("EX-")
                      and re.search(r"(?:annual|financial) report", doc["description"], re.I)]
    if len(candidates) != 1:
        choices = ", ".join(f"{doc['type']} {doc['document']}" for doc in candidates) or "none"
        raise ValueError(f"Cannot uniquely identify the referenced annual/financial report in {index_source}: {choices}.")
    document = candidates[0]
    href = document["href"]
    if is_url(source):
        report_source = normalize_source(urljoin(index_source, href))
        base = source.rsplit("/", 1)[0] + "/"
        if not report_source.startswith(base) or ".." in urlparse(report_source).path.split("/"):
            raise ValueError("Referenced report must belong to the same SEC filing directory.")
    else:
        parsed = urlparse(normalize_source(urljoin("https://www.sec.gov", href)))
        if parsed.hostname not in {"www.sec.gov", "sec.gov"} or ".." in href.split("/"):
            raise ValueError("Unrecognized report link in the local filing index.")
        report_source = str(Path(source).parent / Path(parsed.path).name)
    if Path(urlparse(report_source).path).suffix.lower() not in {".htm", ".html", ".xhtml"}:
        raise ValueError(f"Referenced report is not HTML: {report_source}. PDF/page-only references need explicit extraction support.")
    return report_source, {**document, "index_source": index_source}


def report_reference(blocks: list[FilingBlock]) -> dict | None:
    """Recognize an Item supplied by incorporation, not incidental prose links."""
    text = " ".join(block.text for block in blocks)
    if len(text) > 1800 or not re.search(r"incorporat\w*\b.{0,80}\breference\b", text, re.I):
        return None
    if not re.search(r"\binformation in response to this item\b", text, re.I):
        return None
    if not re.search(r"\b(?:annual|financial) report\b", text, re.I):
        return None
    titles = re.findall(r'"([^"\n]+)"', normalize_typography(text))
    paths = []
    for title in titles:
        if re.search(r"\b(?:annual|financial) report\b", title, re.I):
            continue
        path = [part.strip().rstrip(".") for part in re.split(r"\s+-\s+", title)]
        if all(path) and path not in paths:
            paths.append(path)
    if not paths:
        raise ValueError(f"The Item refers to another report without named sections. Cannot resolve a page-only or unquoted reference automatically: {text}")
    return {"reference_text": text, "section_paths": paths,
            "internal_item_references": re.findall(r"this report under Item\s+(\d+[A-Z]?)", text, re.I)}


def report_title_key(title: str) -> str:
    title = re.sub(r"\s*\(continued\)\s*$", "", title, flags=re.I)
    title = re.sub(r"^Note\s+\d+\s*[.:\-]\s*", "", title, flags=re.I)
    return normalize_heading_title(title)


def report_outline(events: list[FilingStructureEvent], paths: list[list[str]]) -> list[dict]:
    """Use the report's own TOC (or explicit HTML heading levels) for ranges."""
    required = {report_title_key(title) for path in paths for title in path}
    toc = None
    titles: dict[str, dict] = {}
    for position, event in enumerate(events):
        if event.kind != "table" or len(event.rows) < 4:
            continue
        entries: dict[str, dict] = {}
        toc_end = position
        for table_position in range(position, min(position + 4, len(events))):
            table = events[table_position]
            if table.kind != "table":
                break
            toc_end = table_position
            add_toc_title_entries(table, entries)
            if required <= entries.keys():
                toc, titles = toc_end, entries
                break
        if toc is not None:
            break

    nodes = []
    seen = set()
    for position, event in enumerate(events):
        if toc is not None and position <= toc:
            continue
        if toc is None:
            if not re.fullmatch(r"h[1-6]", event.kind):
                continue
            matches = [(event.text, int(event.kind[1]), 1, event.font_size)]
        elif event.kind == "table":
            # A genuine heading can precede numeric data in the same table.
            # Stop at the first data row so labels inside the table (e.g.
            # "Deposits:") cannot open a new document section.
            matches = []
            for row in event.rows:
                cells = [cell for cell in row if cell]
                if not cells:
                    continue
                if len(cells) != 1 or report_title_key(cells[0]) not in titles:
                    break
                matches.append((cells[0], titles[report_title_key(cells[0])]["level"], 1, event.font_size))
        else:
            entry = titles.get(report_title_key(event.text))
            matches = [(event.text, entry["level"], 1, event.font_size)] if entry and not starts_with_bullet(event.text) else []
            if not matches and position + 1 < len(events):
                next_event = events[position + 1]
                combined = f"{event.text} {next_event.text}"
                entry = titles.get(report_title_key(combined))
                if (
                    entry
                    and event.kind != "table"
                    and next_event.kind != "table"
                    and event.bold
                    and next_event.bold
                    and not starts_with_bullet(event.text)
                    and not starts_with_bullet(next_event.text)
                ):
                    font_size = max(
                        value for value in (event.font_size, next_event.font_size) if value is not None
                    ) if event.font_size is not None or next_event.font_size is not None else None
                    matches = [(entry["title"], entry["level"], 2, font_size)]
        for title, level, heading_span, font_size in matches:
            if is_period_comparison_label(title):
                continue
            key = report_title_key(title)
            previous = next((index for index in range(len(nodes) - 1, -1, -1) if nodes[index]["key"] == key), None)
            if previous is not None:
                # The same title repeated inside its still-open section is a
                # running header. A title repeated after another peer/parent
                # is a distinct section and must be disambiguated by its path.
                if not any(node["level"] <= level for node in nodes[previous + 1:]):
                    continue
            seen.add(key)
            nodes.append({"title": title, "key": key, "start": position, "level": level, "font_size": font_size,
                          "heading_end": position + heading_span})
    if not required <= seen:
        missing = ", ".join(sorted(required - seen))
        raise ValueError(f"Cannot locate referenced report headings: {missing}. A usable report TOC or HTML h1-h6 headings is required.")
    for index, node in enumerate(nodes):
        node["end"] = next((later["start"] for later in nodes[index + 1:]
                            if later["level"] <= node["level"]), len(events))
    return nodes


def add_toc_title_entries(event: FilingStructureEvent, entries: dict[str, dict]) -> None:
    for row_index, row in enumerate(event.rows):
        for cell_index, cell in enumerate(row):
            if not is_plausible_toc_title(cell):
                continue
            bold = bool(event.cell_bold and event.cell_bold[row_index][cell_index])
            key = report_title_key(cell)
            entry = entries.setdefault(key, {"title": cell, "level": 1})
            entry["level"] = min(entry["level"], 0 if bold else 1)


def referenced_section_ranges(nodes: list[dict], paths: list[list[str]]) -> list[dict]:
    result = []
    for path in paths:
        chain = []
        for title in path:
            matches = [node for node in nodes if node["key"] == report_title_key(title)
                       and (not chain or chain[-1]["start"] <= node["start"] < chain[-1]["end"])]
            if len(matches) != 1:
                raise ValueError(f"Ambiguous referenced report heading: {title}")
            node = matches[0]
            if chain and not chain[-1]["start"] <= node["start"] < chain[-1]["end"]:
                raise ValueError(f"Cannot establish report section hierarchy for {' / '.join(path)}")
            chain.append(node)
        node = chain[-1]
        if node["end"] <= node["start"]:
            raise ValueError(f"Cannot determine the end of referenced section {node['title']}")
        result.append(dict(node))
    return result


def referenced_report_blocks(events: list[FilingStructureEvent], nodes: list[dict],
                             ranges: list[dict], exclusions: list[dict]) -> list[FilingBlock]:
    headings = {node["start"]: node for node in nodes}
    known_titles = {node["key"] for node in nodes}
    heading_continuations = {
        position
        for node in nodes
        for position in range(node["start"] + 1, node.get("heading_end", node["start"] + 1))
    }
    blocks = []
    for position, event in enumerate(events):
        if not any(node["start"] <= position < node["end"] for node in ranges):
            continue
        if any(node["start"] <= position < node["end"] for node in exclusions):
            continue
        if position in heading_continuations:
            continue
        node = headings.get(position)
        if node:
            blocks.append(toc_header_block(event.index, node["title"], node.get("font_size")))
            continue
        if is_table_caption(event.text) or (event.kind == "table" and is_period_comparison_label(event.text)):
            # Table footnotes and period comparisons belong to the enclosing
            # topic. Neither a caption nor a standalone pair of periods should
            # replace that topic (or leave an incidental table legend active).
            parents = [node for node in nodes if node["start"] <= position < node["end"]]
            if parents:
                parent = max(parents, key=lambda node: (node["level"], node["start"]))
                title = re.sub(r"\s*\(continued\)\s*$", "", parent["title"], flags=re.I)
                blocks.append(toc_header_block(event.index, title, parent.get("font_size")))
            continue
        if is_period_comparison_label(event.text):
            parents = [node for node in nodes if node["start"] <= position < node["end"]]
            if parents:
                parent = max(parents, key=lambda node: (node["level"], node["start"]))
                title = re.sub(r"\s*\(continued\)\s*$", "", parent["title"], flags=re.I)
                blocks.append(toc_header_block(event.index, title, parent.get("font_size")))
            blocks.append(FilingBlock(event.index, event.kind, event.text, style=event.style, bold=event.bold,
                                      mixed_bold=event.mixed_bold, font_size=event.font_size,
                                      segments=(BlockSegment(event.text, bullet_indent_pt=bullet_indent_from_style(event.text, event.style)),)))
            continue
        if should_drop_line(event.text):
            continue
        if report_title_key(event.text) in known_titles and not starts_with_bullet(event.text):
            block = FilingBlock(event.index, event.kind, event.text, style=event.style, bold=event.bold,
                                mixed_bold=event.mixed_bold, font_size=event.font_size,
                                segments=(BlockSegment(event.text, bullet_indent_pt=bullet_indent_from_style(event.text, event.style)),))
            if is_contextual_child_title(event.text) and is_subheader_block(block):
                blocks.append(block)
            continue
        if event.kind == "table":
            # A one-cell, one-row prose heading is a layout table. Financial
            # tables and repeated page/company footer rows remain excluded.
            cells = [cell for row in event.rows for cell in row if cell]
            note_header = note_header_from_layout_table(event)
            if note_header:
                blocks.append(toc_header_block(event.index, note_header, event.font_size))
                continue
            narrative_rows = narrative_layout_table_rows(event)
            if narrative_rows:
                for row_text in narrative_rows:
                    blocks.append(
                        FilingBlock(
                            event.index,
                            "div",
                            row_text,
                            font_size=event.font_size,
                            segments=(BlockSegment(row_text),),
                        )
                    )
                continue
            if len(event.rows) == 1 and len(cells) == 1:
                block = FilingBlock(event.index, "div", cells[0], bold=True, font_size=event.font_size)
                if is_subheader_block(block):
                    blocks.append(block)
            continue
        blocks.append(FilingBlock(event.index, event.kind, event.text, style=event.style, bold=event.bold,
                                  mixed_bold=event.mixed_bold, font_size=event.font_size,
                                  segments=(BlockSegment(event.text, bullet_indent_pt=bullet_indent_from_style(event.text, event.style)),)))
    return merge_continued_blocks(blocks)


def note_header_from_layout_table(event: FilingStructureEvent) -> str:
    """Recover financial statement note headings stored as one-row layout tables."""
    nonempty_rows = [[cell for cell in row if cell] for row in event.rows]
    if len(nonempty_rows) != 1 or len(nonempty_rows[0]) < 2:
        return ""
    first, *rest = nonempty_rows[0]
    note_match = re.fullmatch(r"(Note\s+\d+[A-Z]?)\s*:?", first.strip(), re.IGNORECASE)
    if not note_match:
        return ""
    title = " ".join(cell.strip(" :") for cell in rest if cell.strip(" :"))
    if not title or page_numbers_from_text(title) or re.search(r"[$%]|\b\d{4}\b", title):
        return ""
    return f"{note_match.group(1)}: {title}"


def narrative_layout_table_rows(event: FilingStructureEvent) -> list[str]:
    nonempty_rows = [[cell for cell in row if cell] for row in event.rows]
    if not nonempty_rows or not all(len(row) == 1 for row in nonempty_rows):
        return []
    row_texts = [row[0] for row in nonempty_rows]
    if not any(starts_with_bullet(text) for text in row_texts):
        return []
    return [text for text in row_texts if starts_with_bullet(text)]


def blocks_for_exact_title(events: list[FilingStructureEvent], title: str) -> list[FilingBlock]:
    key = report_title_key(title)
    starts = []
    for position, event in enumerate(events):
        if (
            event.kind != "table"
            and report_title_key(event.text) == key
            and (event.bold or event.font_size is not None or re.fullmatch(r"h[1-6]", event.kind))
        ):
            starts.append((position, event.font_size))
        elif (
            event.kind == "table"
            and len(event.rows) == 1
            and len([cell for row in event.rows for cell in row if cell]) == 1
            and report_title_key(event.text) == key
        ):
            starts.append((position, event.font_size))
    if not starts:
        return []
    start, font_size = starts[0]
    end = next(
        (
            position
            for position, event in enumerate(events[start + 1 :], start=start + 1)
            if event.kind != "table" and (is_terminal_section_heading(event.text) or parse_item_heading(event.text))
        ),
        len(events),
    )
    node = {"title": title, "key": key, "start": start, "end": end, "level": 0,
            "heading_end": start + 1, "font_size": font_size}
    return referenced_report_blocks(events, [node], [node], [])


def follow_report_references(section_blocks: dict[str, list[FilingBlock]], source: str,
                             user_agent: str, include_overlaps: bool = False) -> tuple[dict, dict]:
    references = {item: reference for item, blocks in section_blocks.items()
                  if (reference := report_reference(blocks)) is not None}
    if not references:
        return section_blocks, {}
    report_source, document = discover_referenced_report(source, user_agent)
    events = html_to_structure_events(read_html_source(report_source, user_agent))
    paths = [path for ref in references.values() for path in ref["section_paths"]]
    nodes = report_outline(events, paths)
    selections = {item: referenced_section_ranges(nodes, ref["section_paths"]) for item, ref in references.items()}
    result = dict(section_blocks)
    audit = {}
    for item, ranges in selections.items():
        exclusions = []
        if not include_overlaps:
            exclusions = [other for other_item, other_ranges in selections.items() if other_item != item
                          for other in other_ranges
                          if any(parent["start"] <= other["start"] and other["end"] <= parent["end"]
                                 and (parent["start"], parent["end"]) != (other["start"], other["end"])
                                 for parent in ranges)]
        blocks = referenced_report_blocks(events, nodes, ranges, exclusions)
        # A selected section may be numeric-only, but an entire referenced Item
        # must not silently become empty after table removal.
        if not any(not is_subheader_block(block) for block in blocks):
            raise ValueError(f"Referenced Item {item} contains no extractable narrative in {report_source}.")
        result[item] = blocks
        audit[item] = {**references[item], "source": report_source, "document_type": document["type"],
                       "filing_index": document["index_source"], "sections": ranges,
                       "excluded_sections": exclusions}
    return result, audit


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
                blocks.append(toc_header_block(event.index, toc_entry.title, font_size=event.font_size, toc_level=toc_entry.level))
        else:
            blocks.append(
                FilingBlock(
                    index=event.index,
                    tag=event.kind,
                    text=event.text,
                    style=event.style,
                    bold=event.bold,
                    mixed_bold=event.mixed_bold,
                    font_size=event.font_size,
                    segments=(BlockSegment(event.text, bullet_indent_pt=bullet_indent_from_style(event.text, event.style)),),
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
    raw_entries: list[tuple[str, str, str, str, int, int]] = []
    seen: set[str] = set()
    for row_index, row in enumerate(event.rows):
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
            raw_entries.append((title, normalized, toc_match_key(title), page, row_index, index))

    levels = toc_entry_levels(event, raw_entries)
    return [
        TocEntry(title=title, normalized=normalized, match_key=match_key, page=page, level=levels[position])
        for position, (title, normalized, match_key, page, _, _) in enumerate(raw_entries)
    ]


def toc_entry_levels(event: FilingStructureEvent, raw_entries: list[tuple[str, str, str, str, int, int]]) -> list[int]:
    indents = [toc_cell_indent(event, row_index, cell_index) for *_, row_index, cell_index in raw_entries]
    if any(indent is not None for indent in indents):
        indents = [0.0 if indent is None else indent for indent in indents]
    distinct_indents: list[float] = []
    for indent in indents:
        if indent is None:
            continue
        if not any(abs(indent - known) <= 0.1 for known in distinct_indents):
            distinct_indents.append(indent)
    distinct_indents.sort()
    if len(distinct_indents) > 1:
        return [
            next(
                (level for level, known in enumerate(distinct_indents) if indent is not None and abs(indent - known) <= 0.1),
                0,
            )
            for indent in indents
        ]

    return [0 for _ in raw_entries]


def toc_cell_indent(event: FilingStructureEvent, row_index: int, cell_index: int) -> float | None:
    if not event.cell_styles or row_index >= len(event.cell_styles) or cell_index >= len(event.cell_styles[row_index]):
        return None
    style = event.cell_styles[row_index][cell_index]
    padding_left = css_style_length_pt(style, "padding-left")
    margin_left = css_style_length_pt(style, "margin-left")
    text_indent = css_style_length_pt(style, "text-indent")
    base_indent = max(value for value in (padding_left, margin_left, 0.0) if value is not None)
    effective_indent = base_indent + (text_indent or 0.0)
    return effective_indent if effective_indent > 0.1 else None


def css_style_length_pt(style: str, property_name: str) -> float | None:
    match = re.search(rf"{property_name}\s*:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*(pt|px)", style, re.IGNORECASE)
    if not match:
        return None
    return css_length_to_pt(float(match.group(1)), match.group(2))


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
            blocks.append(toc_header_block(event.index, entry.title, font_size=event.font_size, toc_level=entry.level))
    return blocks


def toc_font_size_for_level(level: int) -> float:
    return 100.0 - level


def toc_header_font_size(font_size: float | None, toc_level: int | None) -> float | None:
    return toc_font_size_for_level(toc_level) if toc_level is not None else font_size


def toc_header_block(
    index: int,
    title: str,
    font_size: float | None = None,
    toc_level: int | None = None,
) -> FilingBlock:
    return FilingBlock(
        index=index,
        tag="toc_header",
        text=title,
        style="",
        bold=True,
        font_size=toc_header_font_size(font_size, toc_level),
    )


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
    value = text.strip()
    return bool(re.fullmatch(r"\d{1,3}", value)) and int(value) <= 500


def parse_item_reference_cell(text: str, allow_bare: bool = False) -> str | None:
    text = canonical_toc_title(text)
    if allow_bare:
        bare_match = re.fullmatch(r"(1A|1B|1C|7A|9A|9B|9C|15|16|1|2|3|4|5|6|7|8|9)", text, re.IGNORECASE)
        if bare_match:
            return normalize_item(bare_match.group(1))
    match = re.fullmatch(
        r"(?:item\s*)?(1A|1B|1C|7A|9A|9B|9C|15|16|1|2|3|4|5|6|7|8|9)\.",
        text,
        re.IGNORECASE,
    )
    return normalize_item(match.group(1)) if match else None


def page_numbers_from_text(text: str) -> tuple[int, ...]:
    pages: set[int] = set()
    for start, end in re.findall(r"\b(\d{1,4})\s*-\s*(\d{1,4})\b", text):
        first, last = int(start), int(end)
        if first <= last <= 500 and last - first <= 300:
            pages.update(range(first, last + 1))
    for value in re.findall(r"\b\d{1,4}\b", text):
        page = int(value)
        if page <= 500:
            pages.add(page)
    return tuple(sorted(pages))


def row_page_numbers(cells: list[str]) -> tuple[int, ...]:
    page_cells = [cell for cell in cells if re.search(r"\bpages?\b|\b\d{1,4}\s*-\s*\d{1,4}\b", cell, re.I)]
    return page_numbers_from_text(" ".join(page_cells))


def title_cell_from_reference_row(cells: list[str], item_cell_index: int) -> str:
    candidates = []
    for index, cell in enumerate(cells):
        if index == item_cell_index:
            continue
        if parse_item_reference_cell(cell, allow_bare=True) or not is_plausible_toc_title(cell):
            continue
        if page_numbers_from_text(cell) and not re.search(r"[A-Za-z]", cell):
            continue
        if re.fullmatch(r"pages?\s+[\d,\- ]+", cell, re.IGNORECASE):
            continue
        candidates.append(cell)
    return canonical_toc_title(candidates[0]) if candidates else ""


def item_references_from_tables(events: list[FilingStructureEvent],
                                wanted_items: set[str]) -> dict[str, list[InlineItemReference]]:
    references: dict[str, list[InlineItemReference]] = {item: [] for item in wanted_items}
    for event in events:
        if event.kind != "table" or len(event.rows) < 2:
            continue
        table_text = event.text.casefold()
        has_item_column = any(
            re.fullmatch(r"item(?:\s+number)?", cell.strip(), re.IGNORECASE)
            for row in event.rows
            for cell in row
        )
        item_label_count = sum(
            1
            for row in event.rows
            for cell in row
            if parse_item_reference_cell(cell, allow_bare=False)
        )
        if not (
            "item number" in table_text
            or "cross-reference" in table_text
            or ("table of contents" in table_text and "item" in table_text and "page" in table_text)
            or item_label_count >= 3
        ):
            continue

        current_item = None
        for row in event.rows:
            cells = [canonical_toc_title(cell) for cell in row if canonical_toc_title(cell)]
            if not cells:
                continue
            item_cells = [(index, item) for index, cell in enumerate(cells)
                          if (item := parse_item_reference_cell(cell, allow_bare=has_item_column)) is not None]
            if item_cells:
                index, item = item_cells[0]
                current_item = item if item in wanted_items else None
                if current_item is None:
                    continue
                title = title_cell_from_reference_row(cells, index)
                if title:
                    references[current_item].append(
                        InlineItemReference(current_item, title.rstrip(":"), row_page_numbers(cells))
                    )
                continue

            if current_item is None:
                continue
            pages = row_page_numbers(cells)
            title = title_cell_from_reference_row(cells, -1)
            if title and pages:
                references[current_item].append(InlineItemReference(current_item, title.rstrip(":"), pages))

    return {item: refs for item, refs in references.items() if refs}


def toc_entries_by_page(events: list[FilingStructureEvent]) -> dict[int, list[TocEntry]]:
    by_page: dict[int, list[TocEntry]] = {}
    for event in events[:150]:
        if event.kind != "table" or len(event.rows) < 4:
            continue
        if not is_toc_like_table(event):
            continue
        entries = toc_entries_from_table(event)
        if len(entries) < 3:
            continue
        for entry in entries:
            if entry.page and is_page_number(entry.page):
                by_page.setdefault(int(entry.page), []).append(entry)
    return by_page


def is_toc_like_table(event: FilingStructureEvent) -> bool:
    if "table of contents" in event.text.casefold():
        return True

    nonempty_rows = [[cell for cell in row if cell] for row in event.rows]
    if any(len(row) <= 3 and any(cell.casefold() == "page" for cell in row) for row in nonempty_rows[:3]):
        return True

    title_page_rows = 0
    for row in nonempty_rows:
        if 1 < len(row) <= 3 and any(is_page_number(cell) for cell in row[1:]):
            if is_plausible_toc_title(row[0]) and "$" not in " ".join(row):
                title_page_rows += 1
    return title_page_rows >= 4


def item_reference_titles(events: list[FilingStructureEvent],
                          refs: list[InlineItemReference],
                          item: str,
                          excluded_pages: set[int] | None = None) -> list[str]:
    titles: list[str] = []
    seen: set[str] = set()
    by_page = toc_entries_by_page(events)
    excluded_pages = excluded_pages or set()

    def add(title: str) -> None:
        key = report_title_key(title)
        if key and key not in seen:
            seen.add(key)
            titles.append(canonical_toc_title(title).rstrip(":"))

    for ref in refs:
        add(ref.title)
        for page in ref.pages:
            if page in excluded_pages:
                continue
            for entry in by_page.get(page, []):
                add(entry.title)

    if item == "7":
        add("Management's Discussion and Analysis")
    elif item == "8":
        add("Financial Statements and Supplemental Details")
        add("Financial Statements and Supplementary Data")
    elif item == "15":
        add("Exhibits")
        add("Exhibit Index")
        add("Exhibits and Financial Statement Schedules")
    elif item in ITEM_TITLES:
        add(ITEM_TITLES[item])
    return titles


def extract_indexed_section_blocks(
    html_text: str,
    items: Iterable[str] = DEFAULT_ITEMS,
) -> dict[str, list[FilingBlock]]:
    """Fallback for filings organized by cross-reference index or multi-column TOC."""
    wanted = {normalize_item(item) for item in items}
    events = html_to_structure_events(html_text)
    references = item_references_from_tables(events, wanted)
    sections: dict[str, list[FilingBlock]] = {}
    pages_by_item = {
        item: {page for ref in refs for page in ref.pages}
        for item, refs in references.items()
    }

    for item, refs in references.items():
        blocks: list[FilingBlock] = []
        seen_blocks: set[tuple[int, str]] = set()
        excluded_pages = (
            set().union(*(pages for other, pages in pages_by_item.items() if other != item))
            if item == "1"
            else pages_by_item.get("8", set()) | pages_by_item.get("15", set()) if item == "7"
            else set()
        )
        for title in item_reference_titles(events, refs, item, excluded_pages=excluded_pages):
            try:
                nodes = report_outline(events, [[title]])
                ranges = referenced_section_ranges(nodes, [[title]])
            except ValueError:
                for block in blocks_for_exact_title(events, title):
                    marker = (block.index, block.text)
                    if marker in seen_blocks:
                        continue
                    seen_blocks.add(marker)
                    blocks.append(block)
                continue
            for block in referenced_report_blocks(events, nodes, ranges, []):
                marker = (block.index, block.text)
                if marker in seen_blocks:
                    continue
                seen_blocks.add(marker)
                blocks.append(block)
        if any(not is_subheader_block(block) for block in blocks):
            sections[item] = sorted(blocks, key=lambda block: block.index)

    remove_broad_item_overlaps(sections)
    return sections


def remove_broad_item_overlaps(sections: dict[str, list[FilingBlock]]) -> None:
    """Remove exact source-block overlaps where broad Items absorb later Items."""
    for broad_item, narrower_items in (("1", ("1A", "7", "8", "15")), ("7", ("8", "15"))):
        if broad_item not in sections:
            continue
        narrower_markers = {
            (block.index, block.text)
            for item in narrower_items
            for block in sections.get(item, [])
        }
        sections[broad_item] = [
            block
            for block in sections[broad_item]
            if (block.index, block.text) not in narrower_markers
        ]


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


def section_reference_text(blocks: list[FilingBlock]) -> str:
    return clean_block_text(" ".join(block.text for block in blocks if block.text))


def item15_reference_trigger(text: str) -> bool:
    """Return True when an extracted item points readers into Item 15 material."""
    normalized = re.sub(r"\s+", " ", normalize_typography(text)).strip().casefold()
    if not normalized:
        return False

    financial_target = bool(re.search(
        r"\b(?:consolidated\s+financial\s+statements?|financial\s+statements?|"
        r"notes?\s+thereto|notes?\s+to\s+(?:the\s+)?consolidated\s+financial\s+statements?|"
        r"accounting\s+pronouncements?)\b",
        normalized,
    ))
    explicit_item15 = bool(re.search(r"\b(?:part\s+iv,\s*)?item\s+15\b", normalized))
    if explicit_item15 and financial_target:
        return True

    if len(normalized) > AUTO_ITEM15_POINTER_MAX_CHARS:
        return False

    pointer_phrase = bool(re.search(
        r"\b(?:appears?\s+on\s+pages?|included\s+on\s+pages?|set\s+forth|"
        r"information\s+required\s+by\s+this\s+item|read\s+in\s+conjunction|"
        r"included\s+in\s+this\s+annual\s+report)\b",
        normalized,
    ))
    page_reference = bool(re.search(r"\bpages?\s+\d{1,4}", normalized))
    glossary_target = "glossary of terms" in normalized
    return pointer_phrase and (financial_target or glossary_target or page_reference)


def should_auto_include_item15(section_blocks: dict[str, list[FilingBlock]], requested_items: Iterable[str]) -> bool:
    requested = {normalize_item(item) for item in requested_items}
    if "15" in requested:
        return False
    return any(
        item15_reference_trigger(section_reference_text(section_blocks.get(item, [])))
        for item in ("7", "8")
    )


def should_auto_include_item15_from_sections(sections: dict[str, str], requested_items: Iterable[str]) -> bool:
    requested = {normalize_item(item) for item in requested_items}
    if "15" in requested:
        return False
    return any(item15_reference_trigger(sections.get(item, "")) for item in ("7", "8"))


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
                # Both inputs have already been classified as narrative. Do
                # not let inherited emphasis turn the growing list into a title.
                tag="merged_text",
                text=join_continued_text(pending.text, block.text),
                style=pending.style,
                bold=pending.bold,
                mixed_bold=pending.mixed_bold or block.mixed_bold or pending.bold != block.bold,
                font_size=max(
                    value for value in (pending.font_size, block.font_size) if value is not None
                ) if pending.font_size is not None or block.font_size is not None else None,
                segments=block_segments(pending) + block_segments(block),
            )
        else:
            merged.append(pending)
            pending = block

    if pending is not None:
        merged.append(pending)
    return merged


def should_merge_with_next_block(previous: FilingBlock, current: FilingBlock) -> bool:
    if any(is_table_caption(block.text) or is_period_comparison_label(block.text)
           for block in (previous, current)):
        return False
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
    # Micron-style page footers can otherwise look like headings because the
    # title heuristic sees only the uppercase K in "59 |2025 10-K". Require
    # the entire page/report label so narrative references to 10-Ks survive.
    page = r"(?:page\s+)?\d{1,4}"
    report = r"(?:19|20)\d{2}\s+(?:form\s+)?10\s*-\s*K(?:/A)?"
    if re.fullmatch(rf"(?:{page}\s*\|\s*{report}|{report}\s*\|\s*{page})", cleaned, re.IGNORECASE):
        return True
    if re.fullmatch(r"\(?continued\)?", cleaned, re.IGNORECASE):
        return True
    if re.fullmatch(r"notes to (the )?consolidated financial statements", cleaned, re.IGNORECASE):
        return True
    if re.fullmatch(r"[A-Z0-9&.,' -]+ (corporation|corp\.?|company|co\.?) and subsidiaries", cleaned, re.IGNORECASE):
        return True
    return False


def is_table_caption(text: str) -> bool:
    """Recognize numbered captions, preserving sentences such as 'Table 2 presents...'."""
    return bool(re.match(r"^Table\s+(?:\d+(?:\.\d+)*[A-Za-z]?|[IVXLCDM]+)\s*(?::|[\-–—]|\.(?!\d))\s*\S",
                         text.strip(), re.IGNORECASE)) and not ends_with_sentence_terminal(text)


def is_period_comparison_label(text: str) -> bool:
    """Match only standalone period pairs, not sentences or topic headings."""
    period = (r"(?:(?:(?:full\s+)?(?:fiscal\s+)?year|fiscal|FY)\s*|"
              r"(?:first|second|third|fourth)\s+quarter\s+|Q[1-4]\s+)?(?:19|20)\d{2}")
    comparison = r"(?:vs\.?|versus|compared\s+(?:with|to))"
    return bool(re.fullmatch(rf"{period}\s+{comparison}\s+{period}\s*[:.]?",
                             text.strip(), re.IGNORECASE))


def is_period_comparison_heading(block: FilingBlock) -> bool:
    """Treat standalone period comparison labels as section context."""
    return is_period_comparison_label(block.text)


def is_subheader_block(block: FilingBlock) -> bool:
    if is_table_caption(block.text):
        return False
    if is_period_comparison_label(block.text):
        return is_period_comparison_heading(block)
    if block.tag == "toc_header":
        return True
    if block.tag in {"toc_text", "merged_text"}:
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
    # Bold financial labels often introduce a sentence and its bullet list.
    # Keep those introductions in the narrative, without banning real colon
    # headings such as "Sources of Revenue:" or TOC-confirmed titles.
    if text.endswith(":") and block.tag not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        if block.mixed_bold and not looks_like_title(text):
            return False
        if "," in text and re.search(r"\b[a-z][a-z'-]+\b", text):
            return False
        if re.search(r"\b(reflecting|driven by|due to|because of|as follows|the following|"
                     r"includes?|consists? of|comprised of)\s*:$", text, re.IGNORECASE):
            return False
        if re.search(r"\b(reflecting|driven by|due to|because of)\b.+:$", text, re.IGNORECASE):
            return False
    if block.font_size is not None and block.font_size >= 10 and looks_like_title(text):
        return True
    if re.search(r"\b(or|and|the|a|an|of|to|for|with|from|in|on)\b", text, re.IGNORECASE) and not block.bold:
        return False
    return block.bold or block.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} or looks_like_title(text)


def looks_like_title(text: str) -> bool:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'’&.-]*", text)
    scored_words = [word for word in words if word.casefold() not in TITLE_CONNECTOR_WORDS]
    if not scored_words:
        return False
    title_words = sum(1 for word in scored_words if is_title_case_word(word))
    return title_words / len(scored_words) >= 0.65


def is_title_case_word(word: str) -> bool:
    if word[:1].isupper() or word.isupper():
        return True
    # Product-style tokens such as x86 and xPU often appear in SEC headings,
    # even though they do not start with an uppercase letter.
    return bool(re.search(r"[A-Z0-9]", word[1:]))


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


def split_extraction_sentences(text: str) -> list[str]:
    return [unit.text for unit in split_extraction_sentence_units(text)]


def split_extraction_sentence_units(text: str) -> list[SentenceUnit]:
    text = normalize_typography(text).strip()
    if not text:
        return []
    text = re.sub(r"([^\s\n])\s*([•‣▪▫◦●○])\s*", r"\1\n\2", text)
    protected = text
    for abbreviation in COMMON_ABBREVIATIONS:
        protected = protected.replace(abbreviation, abbreviation.replace(".", PERIOD_TOKEN))

    sentences: list[SentenceUnit] = []
    for raw_line in protected.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        bullet_level = bullet_level_from_line(raw_line)
        if bullet_level is not None:
            sentence = line.replace(PERIOD_TOKEN, ".").strip()
            sentence = re.sub(r"\s+", " ", sentence)
            sentences.append(SentenceUnit(sentence, bullet_level))
            continue
        if sentences and sentences[-1].bullet_level is not None:
            continuation = line.replace(PERIOD_TOKEN, ".").strip()
            continuation = re.sub(r"\s+", " ", continuation)
            if continuation:
                sentences[-1] = SentenceUnit(
                    f"{sentences[-1].text} {continuation}",
                    sentences[-1].bullet_level,
                    sentences[-1].bullet_indent_pt,
                )
            continue
        for part in re.split(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])", line):
            sentence = part.replace(PERIOD_TOKEN, ".").strip()
            sentence = re.sub(r"\s+", " ", sentence)
            if sentence:
                sentences.append(SentenceUnit(sentence))
    return sentences


def bullet_level_from_line(line: str) -> int | None:
    if not SENTENCE_BULLET_RE.match(line):
        return None
    indent = len(line) - len(line.lstrip(" \t"))
    return max(1, indent // 2 + 1)


def bullet_indent_from_style(text: str, style: str) -> float | None:
    if not SENTENCE_BULLET_RE.match(text):
        return None
    match = re.search(r"padding-left\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*pt", style, re.IGNORECASE)
    return float(match.group(1)) if match else None


def bullet_level_from_indent(indent: float, stack: list[tuple[float, int]], tolerance: float = 0.1) -> int:
    if not stack:
        stack.append((indent, 1))
        return 1
    if indent > stack[-1][0] + tolerance:
        level = stack[-1][1] + 1
        stack.append((indent, level))
        return level
    while stack and indent < stack[-1][0] - tolerance:
        stack.pop()
    if stack and abs(indent - stack[-1][0]) <= tolerance:
        return stack[-1][1]
    level = (stack[-1][1] + 1) if stack else 1
    stack.append((indent, level))
    return level


def block_segments(block: FilingBlock) -> tuple[BlockSegment, ...]:
    if block.segments:
        return block.segments
    return (BlockSegment(block.text, bullet_indent_pt=bullet_indent_from_style(block.text, block.style)),)


def sentence_units_from_block(block: FilingBlock, indent_stack: list[tuple[float, int]] | None = None) -> list[SentenceUnit]:
    units: list[SentenceUnit] = []
    indent_stack = indent_stack if indent_stack is not None else []
    for segment in block_segments(block):
        segment_level = segment.bullet_level
        if segment.bullet_indent_pt is not None:
            segment_level = bullet_level_from_indent(segment.bullet_indent_pt, indent_stack)
        for unit in split_extraction_sentence_units(segment.text):
            bullet_level = segment_level if segment_level is not None else unit.bullet_level
            bullet_indent_pt = segment.bullet_indent_pt if segment.bullet_indent_pt is not None else unit.bullet_indent_pt
            if bullet_level is None and units and units[-1].bullet_level is not None:
                units[-1] = SentenceUnit(f"{units[-1].text} {unit.text}", units[-1].bullet_level, units[-1].bullet_indent_pt)
            else:
                units.append(SentenceUnit(unit.text, bullet_level, bullet_indent_pt))
    return units


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


def make_chunk_id(company: str, year: str, item: str, item_chunk_index: int) -> str:
    return f"{company.lower()}_{year}_{item}_P{item_chunk_index:03d}"


def make_sentence_id(chunk_id: str, sentence_index: int) -> str:
    return f"{chunk_id}_S{sentence_index:03d}"


def is_contextual_child_title(title: str) -> bool:
    return normalize_heading_title(title) in CONTEXTUAL_CHILD_TITLES


def all_caps_heading_priority(title: str) -> int:
    words = re.findall(r"[A-Za-z][A-Za-z&.-]*", title)
    if not words:
        return 0
    letters = "".join(re.findall(r"[A-Za-z]", title))
    if not letters or any(char.islower() for char in letters):
        return 0
    # Short all-caps tokens are often acronyms such as CCG, DCAI, MD&A.
    # Treat multi-word all-caps headings, or longer all-caps words like
    # OVERVIEW, as visual hierarchy signals.
    return int(len(words) >= 2 or len(letters) >= 8)


def closes_heading_level(current: SectionHeading, previous: SectionHeading, tolerance: float = 0.1) -> bool:
    if current.font_size is None:
        return False
    if previous.font_size is None:
        return True
    if current.font_size > previous.font_size + tolerance:
        return True
    if current.font_size < previous.font_size - tolerance:
        return False
    return current.caps_priority >= previous.caps_priority


def section_path_title(path: list[SectionHeading], fallback: str) -> str:
    return " > ".join(heading.title for heading in path) if path else fallback


def section_path_values(path: list[SectionHeading], fallback: str) -> list[str]:
    return [heading.title for heading in path] if path else [fallback]


def update_section_path(path: list[SectionHeading], block: FilingBlock) -> list[SectionHeading]:
    """Keep useful parent context for generic repeated child headings."""
    header = re.sub(r"\s+", " ", normalize_typography(block.text)).strip()
    if not header:
        return path
    current = SectionHeading(header, block.font_size, all_caps_heading_priority(header))
    if is_period_comparison_label(header):
        parent_path = list(path)
        while parent_path and is_period_comparison_label(parent_path[-1].title):
            parent_path.pop()
        return [*parent_path, current] if parent_path else [current]
    if block.font_size is not None:
        next_path = list(path)
        while next_path and closes_heading_level(current, next_path[-1]):
            next_path.pop()
        return [*next_path, current]
    if is_contextual_child_title(header):
        parent_path = list(path)
        while parent_path and is_contextual_child_title(parent_path[-1].title):
            parent_path.pop()
        return [*parent_path, current] if parent_path else [current]
    return [current]


def build_records(
    sections: dict[str, str],
    year: str,
    company: str,
    source: str,
    max_chars: int,
    min_chars: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in ITEM_OUTPUT_ORDER:
        if item not in sections:
            continue
        for chunk_index, chunk in enumerate(split_into_chunks(sections[item], max_chars=max_chars, min_chars=min_chars), start=1):
            records.append(
                {
                    "id": make_chunk_id(company, year, item, chunk_index),
                    "company": company,
                    "year": year,
                    "item": item,
                    "item_default_title": ITEM_TITLES[item],
                    "item_title": ITEM_TITLES[item],
                    "section_path": [ITEM_TITLES[item]],
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
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for item in ITEM_OUTPUT_ORDER:
        if item not in section_blocks:
            continue

        section_path: list[SectionHeading] = []
        bullet_indent_stack: list[tuple[float, int]] = []
        item_chunk_index = 1
        for block in section_blocks[item]:
            if is_table_caption(block.text):
                continue
            if is_subheader_block(block):
                section_path = update_section_path(section_path, block)
                bullet_indent_stack = []
                continue

            item_title = section_path_title(section_path, ITEM_TITLES[item])
            section_values = section_path_values(section_path, ITEM_TITLES[item])
            sentence_units = sentence_units_from_block(block, bullet_indent_stack)
            if not any(unit.bullet_level is not None for unit in sentence_units):
                bullet_indent_stack = []
            records.append(
                {
                    "id": make_chunk_id(company, year, item, item_chunk_index),
                    "company": company,
                    "year": year,
                    "item": item,
                    "item_default_title": ITEM_TITLES[item],
                    "item_title": item_title,
                    "section_path": section_values,
                    "item_chunk_index": item_chunk_index,
                    "source_block_index": block.index,
                    "text": block.text,
                    "_sentence_units": [
                        {"text": unit.text, "bullet_level": unit.bullet_level, "bullet_indent_pt": unit.bullet_indent_pt}
                        for unit in sentence_units
                    ],
                    "source": source,
                }
            )
            item_chunk_index += 1

    return records


def build_sentence_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sentence_records: list[dict[str, Any]] = []
    for record in records:
        chunk_id = str(record["id"])
        raw_units = record.get("_sentence_units")
        units = (
            [SentenceUnit(str(unit["text"]), unit.get("bullet_level"), unit.get("bullet_indent_pt")) for unit in raw_units]
            if isinstance(raw_units, list)
            else split_extraction_sentence_units(str(record.get("text", "")))
        )
        for sentence_index, sentence in enumerate(units, start=1):
            sentence_records.append(
                {
                    "id": make_sentence_id(chunk_id, sentence_index),
                    "chunk_id": chunk_id,
                    "company": record["company"],
                    "year": record["year"],
                    "item": record["item"],
                    "item_default_title": record["item_default_title"],
                    "item_title": record["item_title"],
                    "section_path": record.get("section_path", []),
                    "item_chunk_index": record["item_chunk_index"],
                    "sentence_index": sentence_index,
                    "bullet_level": sentence.bullet_level,
                    "bullet_indent_pt": sentence.bullet_indent_pt,
                    "source_block_index": record.get("source_block_index", ""),
                    "text": sentence.text,
                    "source": record["source"],
                }
            )
    return sentence_records


def write_outputs(records: list[dict[str, Any]], sections: dict[str, str], out_dir: Path, year: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    public_records = [{key: value for key, value in record.items() if not key.startswith("_")} for record in records]
    (out_dir / f"{year}_chunks.json").write_text(json.dumps(public_records, indent=2, ensure_ascii=False), encoding="utf-8")
    txt_lines = []
    for record in records:
        txt_lines.append(f"[{record['id']}] Item {record['item']} - {record['item_title']}")
        txt_lines.append(str(record["text"]))
        txt_lines.append("")
    (out_dir / f"{year}_chunks.txt").write_text("\n".join(txt_lines).strip() + "\n", encoding="utf-8")

    sentence_records = build_sentence_records(records)
    (out_dir / f"{year}_chunk_sentences.json").write_text(
        json.dumps(sentence_records, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    sentence_lines = []
    for record in sentence_records:
        sentence_lines.append(f"[{record['id']}] Parent {record['chunk_id']} - Item {record['item']} - {record['item_title']}")
        sentence_lines.append(str(record["text"]))
        sentence_lines.append("")
    (out_dir / f"{year}_chunk_sentences.txt").write_text(
        "\n".join(sentence_lines).strip() + "\n" if sentence_lines else "", encoding="utf-8",
    )

    for item, section_text in sections.items():
        safe_item = item.lower().replace("a", "a")
        (out_dir / f"{year}_item_{safe_item}.txt").write_text(section_text + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract SEC 10-K Item sections into cleaned chunks.")
    parser.add_argument("source", nargs="?", help="Optional SEC filing HTML URL or local .html/.htm/.txt file.")
    identity = parser.add_mutually_exclusive_group()
    identity.add_argument("--ticker", help="SEC ticker for API extraction, e.g. NVDA.")
    identity.add_argument("--cik", help="SEC CIK for API extraction.")
    parser.add_argument("--company", help="Company folder/name override. Defaults to ticker or filing filename.")
    parser.add_argument("--year", help="Fiscal year / chunk ID prefix, e.g. 2024. Required for API extraction.")
    parser.add_argument(
        "--items", nargs="+", type=normalize_item, choices=SUPPORTED_ITEMS, default=DEFAULT_ITEMS,
        help="Items to extract (space-separated). Default: 1 1A 7 8. Item 15 can be requested explicitly or auto-included when Item 7/8 points to it.",
    )
    parser.add_argument("--out-dir", default="data/raw", help="Directory for extracted TXT/JSON files. Default: data/raw.")
    parser.add_argument("--raw-dir", help=argparse.SUPPRESS)
    parser.add_argument("--no-follow-references", action="store_true",
                        help="Inspect only the primary filing; do not resolve Items incorporated from another report.")
    parser.add_argument("--no-item15-toc", action="store_true",
                        help="Disable Item 15 TOC-guided extraction and use normal body/header extraction instead.")
    parser.add_argument("--include-reference-overlaps", action="store_true",
                        help="Keep referenced subsections in both Items. By default, a subsection assigned to a more specific requested Item is excluded from the broader Item.")
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

    requested_items = list(dict.fromkeys(normalize_item(item) for item in args.items))
    active_items = list(requested_items)
    blocks = item_blocks_with_layout_headings(raw_html, html_to_blocks(raw_html))
    section_blocks = extract_section_blocks(blocks, items=requested_items)
    missing_items = [item for item in requested_items if item not in section_blocks]
    if missing_items:
        indexed_blocks = extract_indexed_section_blocks(raw_html, items=missing_items)
        section_blocks.update({item: blocks for item, blocks in indexed_blocks.items() if item not in section_blocks})

    needs_item15 = "15" in requested_items or should_auto_include_item15(section_blocks, requested_items)
    if needs_item15:
        if "15" not in active_items:
            active_items.append("15")
        item15_toc_blocks = None if args.no_item15_toc else extract_item15_toc_section_blocks(raw_html)
        if item15_toc_blocks:
            section_blocks["15"] = item15_toc_blocks
        elif "15" not in section_blocks:
            direct_item15 = extract_section_blocks(blocks, items=("15",))
            section_blocks.update(direct_item15)
            if "15" not in section_blocks:
                indexed_item15 = extract_indexed_section_blocks(raw_html, items=("15",))
                section_blocks.update(indexed_item15)
    section_blocks = merge_section_blocks(section_blocks)
    reference_audit = {}
    if not args.no_follow_references:
        try:
            section_blocks, reference_audit = follow_report_references(
                section_blocks, source, args.user_agent, include_overlaps=args.include_reference_overlaps,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"Error resolving incorporated report: {exc}", file=sys.stderr)
            return 2
    section_blocks = resolve_section_footnotes(section_blocks)
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
        sections = extract_sections(clean_text, items=requested_items)
        if should_auto_include_item15_from_sections(sections, requested_items):
            if "15" not in active_items:
                active_items.append("15")
            sections = extract_sections(clean_text, items=active_items)
        records = build_records(
            sections=sections,
            year=year,
            company=company,
            source=source,
            max_chars=args.max_chars,
            min_chars=args.min_chars,
        )

    missing = [item for item in active_items if item not in sections]
    if missing:
        print(f"Warning: could not find Item(s): {', '.join(missing)}", file=sys.stderr)

    for record in records:
        if record["item"] in reference_audit:
            record["source"] = reference_audit[record["item"]]["source"]
    output_dir = Path(args.out_dir) / company / year
    write_outputs(records, sections, output_dir, year)
    (output_dir / f"{year}_sources.json").write_text(json.dumps(
        {"primary_source": source, "referenced_items": reference_audit}, indent=2, ensure_ascii=False,
    ) + "\n", encoding="utf-8")

    print(source_label)
    print(f"Company: {company}")
    print(f"Year: {year}")
    print(f"Output dir: {output_dir}")
    print(f"Sections extracted: {', '.join(sections) if sections else 'none'}")
    print(f"Chunks written: {len(records)}")
    for item, reference in reference_audit.items():
        print(f"Item {item}: {reference['document_type']} {reference['source']}")
        if reference["excluded_sections"]:
            print("  Excluded subsections assigned to other requested Items: " + ", ".join(
                section["title"] for section in reference["excluded_sections"]))
    return 0 if sections else 1


if __name__ == "__main__":
    raise SystemExit(main())
