#!/usr/bin/env python3
"""Convert annotation IDs and append original paragraph text.

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

from sec_disclosure.annotation.convert_annotation_ids import build_search_records, find_latest_id


PREVIOUS_YEAR_COL = "Previous Fiscal Year"
CURRENT_YEAR_COL = "Current Fiscal Year"
PREVIOUS_ID_COL = "Previous Paragraph / Chunk ID"
CURRENT_ID_COL = "Current Paragraph / Chunk ID"
PREVIOUS_TEXT_COL = "Previous Disclosure Text"
CURRENT_TEXT_COL = "Current Disclosure Text"
PREVIOUS_SECTION_COL = "Previous Section / Subsection"
CURRENT_SECTION_COL = "Current Section / Subsection"
PREVIOUS_PARAGRAPH_COL = "Previous Original Paragraph"
CURRENT_PARAGRAPH_COL = "Current Original Paragraph"
COMPANY_COL = "Company"
ITEM_COL = "Item"

AUDIT_COLUMNS = [
    "Original Previous Paragraph / Chunk ID",
    "Original Current Paragraph / Chunk ID",
    "Previous ID Conversion Status",
    "Current ID Conversion Status",
    "Previous ID Match Count",
    "Current ID Match Count",
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


class ChunkCache:
    def __init__(self, chunks_root: Path, default_company: str = "") -> None:
        self.chunks_root = chunks_root
        self.default_company = normalize_company(default_company)
        self._records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._index: dict[tuple[str, str], dict[str, int]] = {}
        self._search_records: dict[tuple[str, str], list[dict[str, Any]]] = {}

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

    def search_records_for(self, company: str, year: str) -> list[dict[str, Any]]:
        records, _ = self.records_for(company, year)
        normalized_company = self.default_company or normalize_company(company)
        normalized_year = normalize_year(year)
        key = (normalized_company, normalized_year)
        if key not in self._search_records:
            self._search_records[key] = build_search_records(records)
        return self._search_records[key]


def extract_ids(value: str) -> list[str]:
    return [match.group(1) for match in ID_RE.finditer(value or "")]


def paragraph_context_for_id(
    cache: ChunkCache,
    *,
    company: str,
    year: str,
    chunk_id_value: str,
) -> tuple[str, str, str]:
    chunk_ids = extract_ids(chunk_id_value)
    if not chunk_ids:
        return "", "", "no_id"

    records, index = cache.records_for(company, year)
    positions = [index[chunk_id] for chunk_id in chunk_ids if chunk_id in index]
    if not positions:
        return "", "", "id_not_found"
    if len(positions) != len(chunk_ids):
        status_suffix = "_partial"
    else:
        status_suffix = ""

    span_records = [records[position] for position in sorted(positions)]
    context_ids = ", ".join(str(record.get("id", "")) for record in span_records)
    context_text = "\n".join(str(record.get("text", "")).strip() for record in span_records if str(record.get("text", "")).strip())
    status = "single_chunk" if len(span_records) == 1 else "multi_chunk"
    return context_text, context_ids, status + status_suffix


def resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    by_name = {normalize_header(fieldname): fieldname for fieldname in fieldnames}
    required = [PREVIOUS_YEAR_COL, CURRENT_YEAR_COL, PREVIOUS_ID_COL, CURRENT_ID_COL]
    resolved: dict[str, str] = {}
    optional = [
        COMPANY_COL,
        ITEM_COL,
        PREVIOUS_TEXT_COL,
        CURRENT_TEXT_COL,
        PREVIOUS_SECTION_COL,
        CURRENT_SECTION_COL,
        PREVIOUS_PARAGRAPH_COL,
        CURRENT_PARAGRAPH_COL,
    ]
    for column in required + optional:
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
    stats: dict[str, int] = {}
    enriched: list[dict[str, str]] = []

    for row in rows:
        converted = dict(row)
        company = default_company or row_value(converted, column_map, COMPANY_COL)
        original_previous_id = row_value(converted, column_map, PREVIOUS_ID_COL)
        original_current_id = row_value(converted, column_map, CURRENT_ID_COL)
        converted["Original Previous Paragraph / Chunk ID"] = original_previous_id
        converted["Original Current Paragraph / Chunk ID"] = original_current_id

        previous_id, previous_conversion_status, previous_match_count = convert_id_for_row(
            cache,
            row=converted,
            column_map=column_map,
            company=company,
            year_col=PREVIOUS_YEAR_COL,
            id_col=PREVIOUS_ID_COL,
            text_col=PREVIOUS_TEXT_COL,
            section_col=PREVIOUS_SECTION_COL,
        )
        current_id, current_conversion_status, current_match_count = convert_id_for_row(
            cache,
            row=converted,
            column_map=column_map,
            company=company,
            year_col=CURRENT_YEAR_COL,
            id_col=CURRENT_ID_COL,
            text_col=CURRENT_TEXT_COL,
            section_col=CURRENT_SECTION_COL,
        )

        previous_text, previous_ids, previous_status = paragraph_context_for_id(
            cache,
            company=company,
            year=row_value(converted, column_map, PREVIOUS_YEAR_COL),
            chunk_id_value=previous_id,
        )
        current_text, current_ids, current_status = paragraph_context_for_id(
            cache,
            company=company,
            year=row_value(converted, column_map, CURRENT_YEAR_COL),
            chunk_id_value=current_id,
        )

        converted[column_map[PREVIOUS_ID_COL]] = previous_id
        converted[column_map[CURRENT_ID_COL]] = current_id
        converted[previous_paragraph_col] = previous_text
        converted[current_paragraph_col] = current_text
        converted["Previous ID Conversion Status"] = previous_conversion_status
        converted["Current ID Conversion Status"] = current_conversion_status
        converted["Previous ID Match Count"] = str(previous_match_count)
        converted["Current ID Match Count"] = str(current_match_count)
        converted["Previous Context IDs"] = previous_ids
        converted["Current Context IDs"] = current_ids
        converted["Previous Paragraph Lookup Status"] = previous_status
        converted["Current Paragraph Lookup Status"] = current_status
        for status in (
            f"previous_conversion:{previous_conversion_status}",
            f"current_conversion:{current_conversion_status}",
            f"previous_paragraph:{previous_status}",
            f"current_paragraph:{current_status}",
        ):
            stats[status] = stats.get(status, 0) + 1
        enriched.append(converted)

    if previous_paragraph_col not in fieldnames:
        fieldnames.append(previous_paragraph_col)
    if current_paragraph_col not in fieldnames:
        fieldnames.append(current_paragraph_col)
    for column in AUDIT_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)
    return enriched, stats


def convert_id_for_row(
    cache: ChunkCache,
    *,
    row: dict[str, str],
    column_map: dict[str, str],
    company: str,
    year_col: str,
    id_col: str,
    text_col: str,
    section_col: str,
) -> tuple[str, str, int]:
    old_id = row_value(row, column_map, id_col)
    disclosure_text = row_value(row, column_map, text_col)
    if not disclosure_text.strip() or text_col not in column_map:
        return old_id, "no_text", 0
    records = cache.search_records_for(company, row_value(row, column_map, year_col))
    return find_latest_id(
        disclosure_text=disclosure_text,
        old_id=old_id,
        item=row_value(row, column_map, ITEM_COL),
        item_title=row_value(row, column_map, section_col),
        records=records,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert annotation IDs and fill original paragraph text.")
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
