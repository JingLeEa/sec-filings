#!/usr/bin/env python3
"""Extract two 10-Ks, compare disclosures, and export Google Sheets TSV in one run.

Example:
    python3 scripts/run_disclosure_pipeline.py --ticker MU --company Micron \
        --previous-year 2024 --current-year 2025 --industry Semiconductors \
        --user-agent "Your Name your.email@example.com"

This runner calls the extraction, lexical comparison, and disclosure annotation
entry points in order and stops if a step fails. Items 1, 1A, 7, and 8 are
selected by default.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from sec_disclosure.annotation import export_disclosure_annotations
from sec_disclosure.comparison import lexical_diff as compare_item_changes
from sec_disclosure.extraction import sec_10k_extractor


def fiscal_year(value: str) -> str:
    if not re.fullmatch(r"\d{4}", value):
        raise argparse.ArgumentTypeError("Use a four-digit fiscal year, e.g. 2024.")
    return value


def ticker(value: str) -> str:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", value):
        raise argparse.ArgumentTypeError("Use a ticker such as MU or BRK-B.")
    return value


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Must be a positive integer.") from None
    if number < 1:
        raise argparse.ArgumentTypeError("Must be a positive integer.")
    return number


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--ticker", required=True, type=ticker)
    parser.add_argument("--previous-year", required=True, type=fiscal_year)
    parser.add_argument("--current-year", required=True, type=fiscal_year)
    parser.add_argument("--company", help="Company label in the TSV; folders continue to use the ticker.")
    parser.add_argument("--industry", help="Industry label in the TSV.")
    parser.add_argument("--annotator", help="Annotator name in the TSV.")
    parser.add_argument("--split", help="Dataset split label in the TSV.")
    parser.add_argument("--items", nargs="+", type=str.upper, choices=sec_10k_extractor.SUPPORTED_ITEMS,
                        default=["1", "1A", "7", "8"], help="Default: 1 1A 7 8 (excludes Item 15).")
    parser.add_argument("--user-agent", default=os.environ.get("SEC_USER_AGENT", ""),
                        help="Your name/email for SEC requests; defaults to SEC_USER_AGENT.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"),
                        help="Root for raw/, comparison/, and disclosure_output/. Default: data.")
    parser.add_argument("--max-chars", type=positive_int, default=1800, help="Extraction chunk size. Default: 1800.")
    parser.add_argument("--title-map", action="append", default=[], metavar="ITEM::OLD_TITLE::NEW_TITLE",
                        help="Optional manually verified section-title mapping; repeat for multiple mappings.")
    args = parser.parse_args(argv)
    if int(args.previous_year) >= int(args.current_year):
        parser.error("--previous-year must be earlier than --current-year.")
    if not args.user_agent.strip():
        parser.error("Provide --user-agent with your name/email, or set SEC_USER_AGENT.")
    try:
        compare_item_changes.parse_title_map_args(args.title_map)
    except SystemExit as exc:
        parser.error(str(exc))
    return args


def run_step(label: str, entry_point: Callable[[list[str]], int], arguments: list[str]) -> None:
    print(label, flush=True)
    try:
        code = entry_point(arguments)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            raise RuntimeError(f"{label} failed: {exc.code}") from None
        code = 0
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"{label} failed: {exc}") from exc
    if code != 0:
        raise RuntimeError(f"{label} failed (exit code {code}). See the error above.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_dir = args.data_dir.expanduser()
    company_folder = args.ticker.lower()
    pair = f"{args.previous_year}_vs_{args.current_year}"
    raw_dir = data_dir / "raw"
    comparison_dir = data_dir / "comparison"
    output_dir = data_dir / "disclosure_output" / company_folder / pair / "all_items_diff"
    try:
        for step, year in enumerate((args.previous_year, args.current_year), start=1):
            run_step(f"Step {step}/4: Extract {args.ticker} fiscal {year}", sec_10k_extractor.main, [
                "--ticker", args.ticker, "--company", company_folder, "--year", year,
                "--out-dir", str(raw_dir), "--user-agent", args.user_agent,
                "--max-chars", str(args.max_chars), "--items", *args.items,
            ])

        comparison_args = [
            str(raw_dir / company_folder / args.previous_year / f"{args.previous_year}_chunks.json"),
            str(raw_dir / company_folder / args.current_year / f"{args.current_year}_chunks.json"),
            "--out-dir", str(comparison_dir), "--company", company_folder,
            "--old-year", args.previous_year, "--new-year", args.current_year,
        ]
        for mapping in args.title_map:
            comparison_args.extend(["--title-map", mapping])
        run_step("Step 3/4: Compare disclosures", compare_item_changes.main, comparison_args)

        # The comparison contains only the items selected during extraction.
        # No exporter filter is needed, so missing/empty sections retain the
        # original extractor's warning behavior instead of failing the export.
        export_args = [
            "--input", str(comparison_dir / company_folder / pair / "all_items_diff.json"),
            "--output-dir", str(output_dir),
        ]
        for option in ("company", "industry", "annotator", "split"):
            value = getattr(args, option)
            if value is not None:
                export_args.extend([f"--{option}", value])
        run_step(f"Step 4/4: Export the {len(export_disclosure_annotations.COLUMNS)}-column TSV", export_disclosure_annotations.main, export_args)
    except RuntimeError as exc:
        print(f"Pipeline stopped: {exc}", file=sys.stderr)
        return 2

    print(f"Pipeline complete. Open: {output_dir / 'paste_into_sheets.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
