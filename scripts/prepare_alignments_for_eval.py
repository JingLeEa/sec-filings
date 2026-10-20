#!/usr/bin/env python3
"""Copy finalized alignments without change or materiality predictions."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PREDICTION_FIELDS = {
    "change_analysis",
    "change_analysis_summary",
    "materiality_analysis",
    "materiality_summary",
}
EMPTY_CHANGE_ANALYSIS = {
    "status": "not_started",
    "lexical": None,
    "semantic": None,
    "llm": None,
    "final_taxonomy": None,
}
EMPTY_MATERIALITY_ANALYSIS = {
    "materiality": None,
    "materiality_score": None,
    "confidence_score": None,
    "reason": None,
}
YEAR_PAIR = re.compile(r"\d{4}-\d{4}\Z")
TICKER = re.compile(r"[a-z0-9][a-z0-9.-]*\Z")


def strip_predictions(value):
    """Remove previous predictions, including any nested alignment snapshots."""
    if isinstance(value, dict):
        return {key: strip_predictions(item) for key, item in value.items()
                if key not in PREDICTION_FIELDS}
    if isinstance(value, list):
        return [strip_predictions(item) for item in value]
    return value


def prepare_document(document, ticker, year_pair):
    if not isinstance(document, dict) or document.get("run_complete") is not True:
        raise ValueError("Evaluation requires a completed alignment report.")
    previous_year, current_year = year_pair.split("-")
    if (document.get("company"), str(document.get("previous_year")),
            str(document.get("current_year"))) != (ticker, previous_year, current_year):
        raise ValueError("Alignment report company or years do not match its path.")
    if document.get("included_statuses") != ["ai_verified", "auto_matched", "unmatched"]:
        raise ValueError("Evaluation requires the finalized alignments.json partition.")
    rows = document.get("alignments")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Alignment report must contain a list of alignment rows.")
    ids = [row.get("match_id") for row in rows]
    if any(not isinstance(match_id, str) or not match_id for match_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("Alignment rows must have unique, nonempty match_id values.")

    cleaned = strip_predictions(document)
    for row in cleaned["alignments"]:
        row["change_analysis"] = EMPTY_CHANGE_ANALYSIS.copy()
        row["materiality_analysis"] = EMPTY_MATERIALITY_ANALYSIS.copy()
    return cleaned


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-file", type=Path,
                        help="Path to one source alignments.json; company and years come from the file.")
    source.add_argument("--ticker", action="append",
                        help="Company ticker under --input-root; repeat to include more companies.")
    parser.add_argument("--year-pair", action="append",
                        help="Optional YYYY-YYYY pair; repeat to select multiple pairs.")
    parser.add_argument("--input-dir", "--input-root", dest="input_root", type=Path,
                        default=Path("data/alignments"),
                        help="Directory containing <ticker>/<year-pair>/alignments.json.")
    parser.add_argument("--output-root", type=Path,
                        default=Path("data/alignments_for_eval_soclaas"))
    parser.add_argument("--overwrite", action="store_true",
                        help="Replace an existing cleaned file if the source has changed.")
    args = parser.parse_args(argv)
    args.ticker = list(dict.fromkeys(ticker.lower() for ticker in args.ticker or []))
    if any(not TICKER.fullmatch(ticker) for ticker in args.ticker):
        parser.error("Each ticker must be a safe company directory name.")
    if args.source_file and args.year_pair:
        parser.error("--year-pair applies only when selecting --ticker under --input-root.")
    if args.year_pair and any(not YEAR_PAIR.fullmatch(pair) or pair[:4] >= pair[5:]
                              for pair in args.year_pair):
        parser.error("Each --year-pair must contain increasing four-digit years.")
    if args.input_root.resolve() == args.output_root.resolve():
        parser.error("Input and output roots must differ.")
    return args


def source_files(args):
    if args.source_file:
        document = json.loads(args.source_file.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Source alignments.json must contain a JSON object.")
        ticker = document.get("company")
        previous_year, current_year = document.get("previous_year"), document.get("current_year")
        pair = f"{previous_year}-{current_year}"
        if (not isinstance(ticker, str) or not TICKER.fullmatch(ticker)
                or not YEAR_PAIR.fullmatch(pair) or pair[:4] >= pair[5:]):
            raise ValueError("Source alignment has an invalid company or year pair.")
        yield args.source_file, ticker, pair, document
        return

    for ticker in args.ticker:
        company_dir = args.input_root / ticker
        if args.year_pair:
            pairs = list(dict.fromkeys(args.year_pair))
        elif company_dir.is_dir():
            pairs = sorted(path.name for path in company_dir.iterdir()
                           if path.is_dir() and YEAR_PAIR.fullmatch(path.name))
        else:
            pairs = []
        if not pairs:
            raise ValueError(f"No alignment year pairs found for {ticker} in {company_dir}.")
        for pair in pairs:
            source = company_dir / pair / "alignments.json"
            if not source.is_file():
                raise ValueError(f"Missing alignment report: {source}")
            yield source, ticker, pair, json.loads(source.read_text(encoding="utf-8"))


def main(argv=None):
    args = parse_args(argv)
    prepared = 0
    try:
        for source, ticker, pair, document in source_files(args):
            cleaned = prepare_document(document, ticker, pair)
            target = args.output_root / ticker / pair / "alignments.json"
            if source.resolve() == target.resolve():
                raise ValueError("Source and cleaned output paths must differ.")
            if target.exists():
                existing = json.loads(target.read_text(encoding="utf-8"))
                if existing == cleaned:
                    print(f"Unchanged: {target}")
                    prepared += 1
                    continue
                if not args.overwrite:
                    raise ValueError(f"Existing benchmark input differs: {target}; use --overwrite to replace it.")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(cleaned, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(target)
            print(f"Prepared {len(cleaned['alignments'])} alignment rows: {target}")
            prepared += 1
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"Evaluation preparation stopped: {error}") from error
    print(f"Prepared {prepared} alignment file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
