#!/usr/bin/env python3
"""Convert annotation CSV chunk IDs to the latest extracted chunk IDs.

The script keeps the original CSV columns, updates the previous/current
paragraph ID columns when a disclosure text is found in the latest chunks, and
adds audit columns describing each match.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PREVIOUS_ID_COL = "Previous Paragraph / Chunk ID"
CURRENT_ID_COL = "Current Paragraph / Chunk ID"
PREVIOUS_TEXT_COL = "Previous Disclosure Text"
CURRENT_TEXT_COL = "Current Disclosure Text"
PREVIOUS_SECTION_COL = "Previous Section / Subsection"
CURRENT_SECTION_COL = "Current Section / Subsection"
ITEM_COL = "Item"

AUDIT_COLUMNS = [
    "Original Previous Paragraph / Chunk ID",
    "Original Current Paragraph / Chunk ID",
    "Previous ID Conversion Status",
    "Current ID Conversion Status",
    "Previous ID Match Count",
    "Current ID Match Count",
]


def load_chunks(path: Path) -> list[dict[str, Any]]:
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Chunk JSON not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse chunk JSON {path}: {exc}") from None

    if not isinstance(records, list):
        raise SystemExit(f"Expected chunk JSON to be a list: {path}")
    return records


def normalize_text(text: str) -> str:
    text = (
        text.replace("\u00a0", " ")
        .replace("‘", "'")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("–", "-")
        .replace("—", "-")
    )
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def normalize_item(value: str) -> str:
    value = re.sub(r"^item\s+", "", value.strip(), flags=re.IGNORECASE)
    return value.upper().replace(" ", "")


def build_search_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    search_records: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        text = str(record.get("text", ""))
        if not text.strip():
            continue
        search_records.append(
            {
                "position": position,
                "id": str(record.get("id", "")),
                "item": normalize_item(str(record.get("item", ""))),
                "item_title": str(record.get("item_title", "")).strip(),
                "normalized_text": normalize_text(text),
            }
        )
    return search_records


def find_latest_id(
    disclosure_text: str,
    old_id: str,
    item: str,
    item_title: str,
    records: list[dict[str, Any]],
) -> tuple[str, str, int]:
    normalized_disclosure = normalize_text(disclosure_text)
    if not normalized_disclosure:
        return old_id, "no_text", 0

    normalized_item = normalize_item(item)
    item_title = item_title.strip()

    candidates = records
    if normalized_item:
        item_candidates = [record for record in candidates if record["item"] == normalized_item]
        if item_candidates:
            candidates = item_candidates

    if item_title:
        title_candidates = [record for record in candidates if record["item_title"] == item_title]
        if title_candidates:
            candidates = title_candidates

    matches = [record for record in candidates if normalized_disclosure in record["normalized_text"]]
    if not matches:
        multi_chunk_match = find_multi_chunk_match(normalized_disclosure, candidates)
        if multi_chunk_match:
            return multi_chunk_match[0], "exact_text_match_multi_chunk", multi_chunk_match[1]
        return old_id, "no_match", 0

    matches.sort(key=lambda record: (record["id"] != old_id, record["position"]))
    latest_id = matches[0]["id"]
    if len(matches) == 1:
        status = "exact_text_match"
    elif latest_id == old_id:
        status = "multiple_matches_kept_same_id"
    else:
        status = "multiple_matches_used_first"
    return latest_id, status, len(matches)


def find_multi_chunk_match(normalized_disclosure: str, records: list[dict[str, Any]]) -> tuple[str, int] | None:
    if not normalized_disclosure or not records:
        return None

    text_parts: list[str] = []
    spans: list[tuple[int, int, str]] = []
    cursor = 0
    for record in sorted(records, key=lambda value: value["position"]):
        if text_parts:
            text_parts.append(" ")
            cursor += 1

        text = record["normalized_text"]
        start = cursor
        text_parts.append(text)
        cursor += len(text)
        spans.append((start, cursor, record["id"]))

    joined_text = "".join(text_parts)
    start = joined_text.find(normalized_disclosure)
    if start == -1:
        return None

    end = start + len(normalized_disclosure)
    matched_ids = [record_id for span_start, span_end, record_id in spans if span_end > start and span_start < end]
    if not matched_ids:
        return None
    if len(matched_ids) == 1:
        return matched_ids[0], 1
    return f"{matched_ids[0]} - {matched_ids[-1]}", len(matched_ids)


def convert_rows(
    rows: list[dict[str, str]],
    previous_records: list[dict[str, Any]],
    current_records: list[dict[str, Any]],
) -> tuple[list[dict[str, str]], Counter[str]]:
    previous_search = build_search_records(previous_records)
    current_search = build_search_records(current_records)
    statuses: Counter[str] = Counter()

    converted_rows: list[dict[str, str]] = []
    for row in rows:
        converted = dict(row)
        original_previous_id = converted.get(PREVIOUS_ID_COL, "")
        original_current_id = converted.get(CURRENT_ID_COL, "")
        converted["Original Previous Paragraph / Chunk ID"] = original_previous_id
        converted["Original Current Paragraph / Chunk ID"] = original_current_id

        previous_id, previous_status, previous_count = find_latest_id(
            disclosure_text=converted.get(PREVIOUS_TEXT_COL, ""),
            old_id=original_previous_id,
            item=converted.get(ITEM_COL, ""),
            item_title=converted.get(PREVIOUS_SECTION_COL, ""),
            records=previous_search,
        )
        current_id, current_status, current_count = find_latest_id(
            disclosure_text=converted.get(CURRENT_TEXT_COL, ""),
            old_id=original_current_id,
            item=converted.get(ITEM_COL, ""),
            item_title=converted.get(CURRENT_SECTION_COL, ""),
            records=current_search,
        )

        converted[PREVIOUS_ID_COL] = previous_id
        converted[CURRENT_ID_COL] = current_id
        converted["Previous ID Conversion Status"] = previous_status
        converted["Current ID Conversion Status"] = current_status
        converted["Previous ID Match Count"] = str(previous_count)
        converted["Current ID Match Count"] = str(current_count)
        statuses[f"previous:{previous_status}"] += 1
        statuses[f"current:{current_status}"] += 1
        converted_rows.append(converted)

    return converted_rows, statuses


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert annotation CSV IDs to latest chunk IDs.")
    parser.add_argument("input_csv", type=Path, help="Annotation CSV to convert.")
    parser.add_argument("--previous-json", required=True, type=Path, help="Latest previous-year chunks JSON.")
    parser.add_argument("--current-json", required=True, type=Path, help="Latest current-year chunks JSON.")
    parser.add_argument("--output-csv", type=Path, help="Converted CSV path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_csv = args.output_csv or args.input_csv.with_name(f"{args.input_csv.stem}_converted.csv")

    with args.input_csv.open(newline="", encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise SystemExit(f"CSV has no header row: {args.input_csv}")
        fieldnames = list(reader.fieldnames)
        rows = list(reader)

    for column in [PREVIOUS_ID_COL, CURRENT_ID_COL, PREVIOUS_TEXT_COL, CURRENT_TEXT_COL]:
        if column not in fieldnames:
            raise SystemExit(f"Required column missing from CSV: {column}")

    previous_records = load_chunks(args.previous_json)
    current_records = load_chunks(args.current_json)
    converted_rows, statuses = convert_rows(rows, previous_records, current_records)

    output_fieldnames = fieldnames + [column for column in AUDIT_COLUMNS if column not in fieldnames]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=output_fieldnames)
        writer.writeheader()
        writer.writerows(converted_rows)

    print(f"Input rows: {len(rows)}")
    print(f"Output CSV: {output_csv}")
    for status, count in sorted(statuses.items()):
        print(f"{status}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
