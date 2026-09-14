#!/usr/bin/env python3
"""Append original paragraph context to an annotation CSV by chunk ID.

This is intentionally a post-processing helper. It does not change extracted
chunk IDs, so previously distributed annotation files can still be used.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


PREVIOUS_YEAR_COL = "Previous Fiscal Year"
CURRENT_YEAR_COL = "Current Fiscal Year"
PREVIOUS_ID_COL = "Previous Paragraph / Chunk ID"
CURRENT_ID_COL = "Current Paragraph / Chunk ID"
PREVIOUS_PARAGRAPH_COL = "Previous Original Paragraph"
CURRENT_PARAGRAPH_COL = "Current Original Paragraph"
COMPANY_COL = "Company"

AUDIT_COLUMNS = [
    "Previous Context IDs",
    "Current Context IDs",
    "Previous Paragraph Lookup Status",
    "Current Paragraph Lookup Status",
]

COMPANY_ALIASES = {
    "nvidia": "nvda",
    "nvidia_corporation": "nvda",
    "jpmorgan": "jpm",
    "jpmorgan_chase": "jpm",
    "jpmorgan_chase_co": "jpm",
}

BULLET_RE = re.compile(r"^\s*(?:[•‣▪▫◦●○]|\*\s+|-\s+)")
ID_RE = re.compile(r"((?:19|20)\d{2}_[A-Z0-9]+_P\d{3,})(?:_[A-Z0-9]+)?", re.IGNORECASE)


def load_chunks(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Chunk JSON not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse chunk JSON {path}: {exc}") from None
    if not isinstance(data, list):
        raise SystemExit(f"Expected chunk JSON to be a list: {path}")
    return data


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def normalize_company(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return COMPANY_ALIASES.get(normalized, normalized)


def normalize_year(value: str) -> str:
    match = re.search(r"(?:19|20)\d{2}", value.strip())
    return match.group(0) if match else ""


def is_bullet(record: dict[str, Any]) -> bool:
    return bool(BULLET_RE.match(str(record.get("text", ""))))


def same_section(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        str(left.get("year", "")) == str(right.get("year", ""))
        and str(left.get("item", "")) == str(right.get("item", ""))
        and str(left.get("item_title", "")) == str(right.get("item_title", ""))
    )


def context_span(records: list[dict[str, Any]], position: int) -> tuple[int, int]:
    """Return inclusive start/end positions for paragraph context.

    If the target is a bullet, include the contiguous bullet list and the
    immediate lead-in paragraph when it is in the same extracted section.
    If the target is a normal paragraph, include immediately following bullets.
    """
    target = records[position]
    start = position
    end = position

    if is_bullet(target):
        while start > 0 and same_section(records[start - 1], target) and is_bullet(records[start - 1]):
            start -= 1
        if start > 0 and same_section(records[start - 1], target) and not is_bullet(records[start - 1]):
            start -= 1
        while end + 1 < len(records) and same_section(records[end + 1], target) and is_bullet(records[end + 1]):
            end += 1
    else:
        while end + 1 < len(records) and same_section(records[end + 1], target) and is_bullet(records[end + 1]):
            end += 1

    return start, end


class ChunkCache:
    def __init__(self, chunks_root: Path, default_company: str = "") -> None:
        self.chunks_root = chunks_root
        self.default_company = normalize_company(default_company)
        self._records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._index: dict[tuple[str, str], dict[str, int]] = {}

    def records_for(self, company: str, year: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        normalized_company = self.default_company or normalize_company(company)
        normalized_year = normalize_year(year)
        if not normalized_company:
            raise SystemExit("Company is required. Pass --company, or include a Company column.")
        if not normalized_year:
            raise SystemExit(f"Could not read fiscal year from value: {year!r}")

        key = (normalized_company, normalized_year)
        if key not in self._records:
            path = self.chunks_root / normalized_company / normalized_year / f"{normalized_year}_chunks.json"
            records = load_chunks(path)
            self._records[key] = records
            self._index[key] = {str(record.get("id", "")): index for index, record in enumerate(records)}
        return self._records[key], self._index[key]


def extract_first_id(value: str) -> str:
    match = ID_RE.search(value or "")
    return match.group(1) if match else ""


def paragraph_context_for_id(
    cache: ChunkCache,
    *,
    company: str,
    year: str,
    chunk_id: str,
) -> tuple[str, str, str]:
    chunk_id = extract_first_id(chunk_id)
    if not chunk_id:
        return "", "", "no_id"

    records, index = cache.records_for(company, year)
    position = index.get(chunk_id)
    if position is None:
        return "", "", "id_not_found"

    start, end = context_span(records, position)
    span_records = records[start : end + 1]
    context_ids = ", ".join(str(record.get("id", "")) for record in span_records)
    context_text = "\n".join(str(record.get("text", "")).strip() for record in span_records if str(record.get("text", "")).strip())
    if start == end:
        status = "single_chunk"
    elif is_bullet(records[position]):
        status = "bullet_with_list_context"
    else:
        status = "paragraph_with_following_bullets"
    return context_text, context_ids, status


def resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    by_name = {normalize_header(fieldname): fieldname for fieldname in fieldnames}
    required = [PREVIOUS_YEAR_COL, CURRENT_YEAR_COL, PREVIOUS_ID_COL, CURRENT_ID_COL]
    resolved: dict[str, str] = {}
    for column in required + [COMPANY_COL, PREVIOUS_PARAGRAPH_COL, CURRENT_PARAGRAPH_COL]:
        actual = by_name.get(normalize_header(column))
        if actual:
            resolved[column] = actual
    for column in required:
        if column not in resolved:
            raise SystemExit(f"Required column missing from CSV: {column}")
    return resolved


def row_value(row: dict[str, str], column_map: dict[str, str], column: str) -> str:
    actual = column_map.get(column)
    return row.get(actual, "") if actual else ""


def enrich_rows(
    rows: list[dict[str, str]],
    fieldnames: list[str],
    *,
    chunks_root: Path,
    default_company: str,
    column_map: dict[str, str],
) -> tuple[list[dict[str, str]], dict[str, int]]:
    cache = ChunkCache(chunks_root, default_company)
    previous_paragraph_col = column_map.get(PREVIOUS_PARAGRAPH_COL, PREVIOUS_PARAGRAPH_COL)
    current_paragraph_col = column_map.get(CURRENT_PARAGRAPH_COL, CURRENT_PARAGRAPH_COL)
    stats = {status: 0 for status in ("single_chunk", "bullet_with_list_context", "paragraph_with_following_bullets", "no_id", "id_not_found")}
    enriched: list[dict[str, str]] = []

    for row in rows:
        converted = dict(row)
        company = default_company or row_value(converted, column_map, COMPANY_COL)

        previous_text, previous_ids, previous_status = paragraph_context_for_id(
            cache,
            company=company,
            year=row_value(converted, column_map, PREVIOUS_YEAR_COL),
            chunk_id=row_value(converted, column_map, PREVIOUS_ID_COL),
        )
        current_text, current_ids, current_status = paragraph_context_for_id(
            cache,
            company=company,
            year=row_value(converted, column_map, CURRENT_YEAR_COL),
            chunk_id=row_value(converted, column_map, CURRENT_ID_COL),
        )

        converted[previous_paragraph_col] = previous_text
        converted[current_paragraph_col] = current_text
        converted["Previous Context IDs"] = previous_ids
        converted["Current Context IDs"] = current_ids
        converted["Previous Paragraph Lookup Status"] = previous_status
        converted["Current Paragraph Lookup Status"] = current_status
        stats[previous_status] = stats.get(previous_status, 0) + 1
        stats[current_status] = stats.get(current_status, 0) + 1
        enriched.append(converted)

    if previous_paragraph_col not in fieldnames:
        fieldnames.append(previous_paragraph_col)
    if current_paragraph_col not in fieldnames:
        fieldnames.append(current_paragraph_col)
    for column in AUDIT_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    return enriched, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill original paragraph context in an annotation CSV from chunk IDs.")
    parser.add_argument("input_csv", type=Path, help="Annotation CSV, e.g. data/include_paragraph/nvidia.csv")
    parser.add_argument("--chunks-root", default=Path("data/raw"), type=Path, help="Root containing <company>/<year>/<year>_chunks.json. Default: data/raw.")
    parser.add_argument("--company", default="", help="Company chunk folder override, e.g. nvda.")
    parser.add_argument("--output-csv", type=Path, help="Output CSV path. Default: <input_name>_with_paragraphs.csv beside the input file.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_csv = args.output_csv or args.input_csv.with_name(f"{args.input_csv.stem}_with_paragraphs.csv")

    with args.input_csv.open(encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV has no header row: {args.input_csv}")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    column_map = resolve_columns(fieldnames)
    enriched_rows, stats = enrich_rows(
        rows,
        fieldnames,
        chunks_root=args.chunks_root,
        default_company=args.company,
        column_map=column_map,
    )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched_rows)

    print(f"Input rows: {len(rows)}")
    print(f"Output CSV: {output_csv}")
    for status, count in sorted(stats.items()):
        if count:
            print(f"{status}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
