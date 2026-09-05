#!/usr/bin/env python3
"""Create compact item-wise year-over-year SEC disclosure diffs.

This is a preprocessing step for manual LLM review. It reads two chunk JSON
files produced by sec_10k_extractor.py, compares sentences within the same
company/item/item_title group, removes sentences that appear in both years, and
writes the remaining year-specific sentences to a separate comparison folder.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_OUT_DIR = "comparison"
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
PERIOD_TOKEN = "<PERIOD>"


def load_records(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Input file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Could not parse JSON file {path}: {exc}") from None

    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON list of chunk records: {path}")
    return data


def infer_field(records: list[dict[str, Any]], field: str, fallback: str) -> str:
    for record in records:
        value = record.get(field)
        if value not in (None, ""):
            return str(value)
    return fallback


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

    text = re.sub(r"\s*•\s*", "\n• ", text)
    protected = text
    for index, abbreviation in enumerate(COMMON_ABBREVIATIONS):
        protected = protected.replace(abbreviation, abbreviation.replace(".", PERIOD_TOKEN))

    parts: list[str] = []
    for line in protected.splitlines():
        line = line.strip()
        if not line:
            continue
        parts.extend(re.split(r"(?<=[.!?])\s+(?=[\"'“‘(\[]?[A-Z0-9])", line))

    sentences: list[str] = []
    for part in parts:
        sentence = part.replace(PERIOD_TOKEN, ".").strip()
        sentence = re.sub(r"\s+", " ", sentence)
        if sentence:
            sentences.append(sentence)
    return sentences


def normalize_sentence(sentence: str) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    for old, new in replacements.items():
        sentence = sentence.replace(old, new)
    sentence = re.sub(r"\s+", " ", sentence)
    return sentence.strip().casefold()


def group_records(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        item = str(record.get("item", "")).strip()
        item_title = str(record.get("item_title", "")).strip()
        if item and item_title and str(record.get("text", "")).strip():
            grouped[(item, item_title)].append(record)
    return grouped


def sentence_occurrences(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for record in records:
        for sentence_index, sentence in enumerate(split_sentences(str(record.get("text", ""))), start=1):
            occurrences.append(
                {
                    "source_id": record.get("id", ""),
                    "source_block_index": record.get("source_block_index"),
                    "sentence_index": sentence_index,
                    "sentence": sentence,
                    "normalized": normalize_sentence(sentence),
                }
            )
    return occurrences


def remove_shared_sentences(
    base: list[dict[str, Any]],
    other_counter: Counter[str],
) -> list[dict[str, Any]]:
    remaining: list[dict[str, Any]] = []
    other_counter = other_counter.copy()
    for occurrence in base:
        key = occurrence["normalized"]
        if other_counter[key] > 0:
            other_counter[key] -= 1
        else:
            remaining.append(strip_internal_fields(occurrence))
    return remaining


def strip_internal_fields(occurrence: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in occurrence.items() if key != "normalized"}


def item_sort_key(item: str) -> tuple[int, str]:
    match = re.match(r"(\d+)([A-Z]*)", item)
    if not match:
        return (999, item)
    return (int(match.group(1)), match.group(2))


def compare_records(
    old_records: list[dict[str, Any]],
    new_records: list[dict[str, Any]],
    old_year: str,
    new_year: str,
    company: str,
) -> dict[str, Any]:
    old_groups = group_records(old_records)
    new_groups = group_records(new_records)
    keys = sorted(set(old_groups) | set(new_groups), key=lambda key: (item_sort_key(key[0]), key[1].casefold()))

    items_by_number: dict[str, dict[str, Any]] = {}
    for item, item_title in keys:
        old_occurrences = sentence_occurrences(old_groups.get((item, item_title), []))
        new_occurrences = sentence_occurrences(new_groups.get((item, item_title), []))
        old_counter = Counter(occurrence["normalized"] for occurrence in old_occurrences)
        new_counter = Counter(occurrence["normalized"] for occurrence in new_occurrences)
        unchanged_count = sum((old_counter & new_counter).values())
        old_only = remove_shared_sentences(old_occurrences, new_counter)
        new_only = remove_shared_sentences(new_occurrences, old_counter)

        item_default_title = first_default_title(old_groups.get((item, item_title), []), new_groups.get((item, item_title), []))
        item_result = items_by_number.setdefault(
            item,
            {
                "item": item,
                "item_default_title": item_default_title,
                "item_titles": [],
                "totals": {
                    f"{old_year}_only_sentences": 0,
                    f"{new_year}_only_sentences": 0,
                    "unchanged_sentences_removed": 0,
                },
            },
        )

        if old_only or new_only:
            item_result["item_titles"].append(
                {
                    "item_title": item_title,
                    f"{old_year}_sentence_count": len(old_occurrences),
                    f"{new_year}_sentence_count": len(new_occurrences),
                    "unchanged_sentences_removed": unchanged_count,
                    f"{old_year}_only": old_only,
                    f"{new_year}_only": new_only,
                }
            )

        item_result["totals"][f"{old_year}_only_sentences"] += len(old_only)
        item_result["totals"][f"{new_year}_only_sentences"] += len(new_only)
        item_result["totals"]["unchanged_sentences_removed"] += unchanged_count

    items = [items_by_number[item] for item in sorted(items_by_number, key=item_sort_key)]
    return {
        "company": company,
        "old_year": old_year,
        "new_year": new_year,
        "comparison": f"{old_year}_vs_{new_year}",
        "method": "Exact normalized sentence matching within company/item/item_title groups.",
        "items": items,
        "totals": {
            f"{old_year}_only_sentences": sum(item["totals"][f"{old_year}_only_sentences"] for item in items),
            f"{new_year}_only_sentences": sum(item["totals"][f"{new_year}_only_sentences"] for item in items),
            "unchanged_sentences_removed": sum(item["totals"]["unchanged_sentences_removed"] for item in items),
        },
    }


def first_default_title(old_records: list[dict[str, Any]], new_records: list[dict[str, Any]]) -> str:
    for record in old_records + new_records:
        value = record.get("item_default_title")
        if value not in (None, ""):
            return str(value)
    return ""


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_text_report(path: Path, comparison: dict[str, Any], items: list[dict[str, Any]] | None = None) -> None:
    old_year = comparison["old_year"]
    new_year = comparison["new_year"]
    lines = [
        f"Company: {comparison['company']}",
        f"Comparison: {old_year} vs {new_year}",
        f"Method: {comparison['method']}",
        "",
    ]

    for item in items if items is not None else comparison["items"]:
        lines.append(f"Item {item['item']} - {item['item_default_title']}")
        lines.append(
            "Totals: "
            f"{old_year}-only {item['totals'][f'{old_year}_only_sentences']}, "
            f"{new_year}-only {item['totals'][f'{new_year}_only_sentences']}, "
            f"unchanged removed {item['totals']['unchanged_sentences_removed']}"
        )
        lines.append("")

        if not item["item_titles"]:
            lines.append("No sentence-level differences after exact duplicate removal.")
            lines.append("")
            continue

        for title_group in item["item_titles"]:
            lines.append(f"## {title_group['item_title']}")
            lines.append("")
            append_sentence_section(lines, f"Only in {old_year}", title_group[f"{old_year}_only"])
            append_sentence_section(lines, f"Only in {new_year}", title_group[f"{new_year}_only"])
            lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def append_sentence_section(lines: list[str], heading: str, occurrences: list[dict[str, Any]]) -> None:
    lines.append(f"{heading}: {len(occurrences)}")
    if not occurrences:
        lines.append("(none)")
        lines.append("")
        return
    for occurrence in occurrences:
        source_id = occurrence.get("source_id", "")
        sentence = occurrence.get("sentence", "")
        lines.append(f"[{source_id}] {sentence}")
    lines.append("")


def write_outputs(comparison: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(out_dir / "all_items_diff.json", comparison)
    write_text_report(out_dir / "all_items_diff.txt", comparison)

    for item in comparison["items"]:
        item_slug = item["item"].lower()
        item_data = {key: value for key, value in comparison.items() if key != "items"}
        item_data["items"] = [item]
        write_json(out_dir / f"item_{item_slug}_diff.json", item_data)
        write_text_report(out_dir / f"item_{item_slug}_diff.txt", comparison, items=[item])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare SEC extraction chunks across two consecutive years.")
    parser.add_argument("old_json", type=Path, help="Older year chunks JSON, e.g. output/nvda/2023/2023_chunks.json")
    parser.add_argument("new_json", type=Path, help="Newer year chunks JSON, e.g. output/nvda/2024/2024_chunks.json")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, type=Path, help="Base directory for comparison outputs.")
    parser.add_argument("--company", help="Company folder/name override. Defaults to the JSON company field.")
    parser.add_argument("--old-year", help="Older year override. Defaults to the JSON year field.")
    parser.add_argument("--new-year", help="Newer year override. Defaults to the JSON year field.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    old_records = load_records(args.old_json)
    new_records = load_records(args.new_json)

    company = args.company or infer_field(old_records, "company", infer_field(new_records, "company", "unknown"))
    old_year = args.old_year or infer_field(old_records, "year", "old")
    new_year = args.new_year or infer_field(new_records, "year", "new")

    comparison = compare_records(
        old_records=old_records,
        new_records=new_records,
        old_year=old_year,
        new_year=new_year,
        company=company,
    )
    output_dir = args.out_dir / company / f"{old_year}_vs_{new_year}"
    write_outputs(comparison, output_dir)

    print(f"Output dir: {output_dir}")
    print(f"Items compared: {len(comparison['items'])}")
    print(f"{old_year}-only sentences: {comparison['totals'][f'{old_year}_only_sentences']}")
    print(f"{new_year}-only sentences: {comparison['totals'][f'{new_year}_only_sentences']}")
    print(f"Unchanged sentences removed: {comparison['totals']['unchanged_sentences_removed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
