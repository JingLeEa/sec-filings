#!/usr/bin/env python3
"""Download a SEC 10-K HTML filing and extract cleaned Item sections.

The pipeline is intentionally small and dependency-free:

    SEC 10-K HTML URL/path -> cleaned text -> Items 1, 1A, 7, 8 -> chunks

Outputs are JSON and TXT files with stable paragraph IDs such as 2024_P001.
"""

from __future__ import annotations

import argparse
import hashlib
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
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


DEFAULT_ITEMS = ("1", "1A", "7", "8")
ITEM_ENDS = {
    "1": ("1A", "1B", "1C", "2"),
    "1A": ("1B", "1C", "2"),
    "7": ("7A", "8"),
    "8": ("9", "9A", "9B", "9C"),
}
ITEM_TITLES = {
    "1": "Business",
    "1A": "Risk Factors",
    "7": "Management's Discussion and Analysis",
    "8": "Financial Statements and Supplementary Data",
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


def clean_block_text(text: str) -> str:
    text = html.unescape(text).replace("\xa0", " ")
    text = replace_curly_apostrophes(text)
    text = text.replace("•", "• ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def replace_curly_apostrophes(text: str) -> str:
    return text.replace("‘", "'").replace("’", "'")


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


def download_filing(url: str, raw_dir: Path, user_agent: str) -> Path:
    raw_dir.mkdir(parents=True, exist_ok=True)
    parsed = urlparse(url)
    name = Path(parsed.path).name or "filing.html"
    if not name.lower().endswith((".htm", ".html", ".txt")):
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
        name = f"filing_{digest}.html"
    destination = raw_dir / name

    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Encoding": "identity",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    with urlopen(request, timeout=60) as response:
        body = response.read()
    destination.write_bytes(body)
    return destination


def html_to_clean_text(html_text: str) -> str:
    parser = FilingTextExtractor()
    parser.feed(html_text)
    parser.close()
    text = parser.get_text()
    text = html.unescape(text)
    text = replace_curly_apostrophes(text)
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
        r"(1A|1B|1C|7A|9A|9B|9C|1|2|3|4|5|6|7|8|9)"
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
        r"(1A|1B|1C|7A|9A|9B|9C|1|2|3|4|5|6|7|8|9)"
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
            end_index = next((index for index, heading_item in headings if index > start_index and heading_item in end_items), None)
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
            end = next((heading for heading in headings if heading.start > start.end and heading.item in end_items), None)
            if end is None:
                continue
            content = text[start.end : end.start].strip()
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


def is_subheader_block(block: FilingBlock) -> bool:
    text = block.text.strip()
    if not text or parse_item_heading(text) or should_drop_line(text):
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


def build_records(
    sections: dict[str, str],
    year: str,
    company: str,
    source: str,
    max_chars: int,
    min_chars: int,
) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    sequence = 1
    for item in DEFAULT_ITEMS:
        if item not in sections:
            continue
        for chunk_index, chunk in enumerate(split_into_chunks(sections[item], max_chars=max_chars, min_chars=min_chars), start=1):
            records.append(
                {
                    "id": f"{year}_P{sequence:03d}",
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
            sequence += 1
    return records


def build_records_from_section_blocks(
    section_blocks: dict[str, list[FilingBlock]],
    year: str,
    company: str,
    source: str,
    max_chars: int,
) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    sequence = 1

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
                        "id": f"{year}_P{sequence:03d}",
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
                sequence += 1
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
    parser = argparse.ArgumentParser(description="Download and extract Item 1, 1A, 7, and 8 from a SEC 10-K HTML filing.")
    parser.add_argument("source", help="SEC filing HTML URL or a local .html/.htm/.txt file.")
    parser.add_argument("--year", help="Year prefix for chunk IDs, e.g. 2024. Inferred when possible.")
    parser.add_argument("--out-dir", default="output", help="Directory for extracted TXT/JSON files.")
    parser.add_argument("--raw-dir", default="data/raw", help="Directory for downloaded HTML files.")
    parser.add_argument("--max-chars", type=int, default=1800, help="Maximum characters per disclosure chunk.")
    parser.add_argument("--min-chars", type=int, default=120, help="Small paragraphs are merged until roughly this size.")
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("SEC_USER_AGENT", "simple-sec-extractor/0.1 contact@example.com"),
        help="SEC download User-Agent. Prefer setting SEC_USER_AGENT='Name email@example.com'.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    source = normalize_source(args.source)

    if is_url(source):
        filing_path = download_filing(source, Path(args.raw_dir), args.user_agent)
    else:
        filing_path = Path(source)
        if not filing_path.exists():
            print(f"Input file not found: {filing_path}", file=sys.stderr)
            return 2

    raw_html = filing_path.read_text(encoding="utf-8", errors="replace")
    blocks = html_to_blocks(raw_html)
    section_blocks = extract_section_blocks(blocks)
    clean_text = html_to_clean_text(raw_html)
    company, filename_year = infer_company_year_from_filename(filing_path)
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

    print(f"Source file: {filing_path}")
    print(f"Company: {company}")
    print(f"Year: {year}")
    print(f"Output dir: {output_dir}")
    print(f"Sections extracted: {', '.join(sections) if sections else 'none'}")
    print(f"Chunks written: {len(records)}")
    return 0 if sections else 1


if __name__ == "__main__":
    raise SystemExit(main())
