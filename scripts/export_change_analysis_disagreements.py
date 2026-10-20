#!/usr/bin/env python3
"""Export LLM change-taxonomy disagreements with source disclosures for review."""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_alignment_predictions import (
    CHANGE_BINARY_GROUP, TICKER, YEAR_PAIR, evaluate_pair, load_rows,
)
from export_materiality_disagreements import atomic_text, disclosure_sections, source_disclosures


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", action="append", required=True,
                        help="Company ticker; repeat for several companies.")
    parser.add_argument("--year-pair", action="append",
                        help="YYYY-YYYY comparison; repeat or omit to discover available pairs.")
    parser.add_argument("--reference-root", type=Path,
                        default=Path("data/alignments_for_eval_soclaas"))
    parser.add_argument("--candidate-root", type=Path, default=Path("data/alignments"))
    parser.add_argument("--disclosures-dir", type=Path, default=Path("data/disclosures"))
    parser.add_argument("--output-root", type=Path, default=Path("data/evaluation_soclaas"))
    parser.add_argument("--model", default="DeepSeek", help="Reference model name for the report.")
    args = parser.parse_args(argv)
    args.ticker = list(dict.fromkeys(ticker.lower() for ticker in args.ticker))
    if any(not TICKER.fullmatch(ticker) for ticker in args.ticker):
        parser.error("Tickers must be safe company directory names.")
    if args.year_pair and any(not YEAR_PAIR.fullmatch(pair) or pair[:4] >= pair[5:]
                              for pair in args.year_pair):
        parser.error("Year pairs must contain increasing four-digit years.")
    if args.output_root.resolve() in {args.reference_root.resolve(), args.candidate_root.resolve()}:
        parser.error("Output root must differ from the alignment roots.")
    return args


def render_report(ticker, pair, reference_path, candidate_path, records, reference_model="DeepSeek"):
    comparison = evaluate_pair(reference_path, candidate_path, reference_model=reference_model)
    _, reference = load_rows(reference_path)
    _, candidate = load_rows(candidate_path)
    model = comparison["change_analysis"]["model_rows"]
    differing = {entry["match_id"] for entry in model["disagreements"]}
    rows = [row for row in reference.values() if row["match_id"] in differing]
    model_name = html.escape(reference_model)
    parts = [
        f"# LLM change-analysis disagreements: {ticker.upper()} {pair}", "",
        f"{model_name} is the reference model, not human-verified ground truth. "
        "Only completed `ai_verified` rows with different LLM change-taxonomy labels are listed.", "",
        f"Disagreements: {len(rows)}; compared LLM rows: {model['compared']}; "
        f"{model_name} LLM rows completed: {model['reference_completed']}/{model['eligible_rows']}.", "",
        f"| # | Match ID | Qwen LLM | {model_name} LLM | Substantive class |",
        "|---:|---|---|---|---|",
    ]
    for number, row in enumerate(rows, 1):
        other = candidate[row["match_id"]]
        qwen = other["change_analysis"]["llm"]
        reference_label = row["change_analysis"]["llm"]
        left_class = CHANGE_BINARY_GROUP.get(qwen)
        right_class = CHANGE_BINARY_GROUP.get(reference_label)
        class_result = ("n/a" if left_class is None or right_class is None else
                        "Different" if left_class != right_class else "Same")
        parts.append(f"| {number} | [<code>{html.escape(row['match_id'])}</code>](#case-{number}) "
                     f"| {html.escape(qwen)} | {html.escape(reference_label)} | {class_result} |")
    if not rows:
        parts.extend(["", "No completed LLM change-taxonomy disagreements found."])
    for number, row in enumerate(rows, 1):
        other = candidate[row["match_id"]]
        reference_label = row["change_analysis"]["llm"]
        qwen = other["change_analysis"]["llm"]
        parts.extend([
            "", f'<a id="case-{number}"></a>', f"## {number}. {html.escape(row['match_id'])}", "",
            f"Status: **{html.escape(row['status'])}**; "
            f"relationship: **{html.escape(row['relationship'])}**", "",
            f"**{model_name} LLM: {html.escape(reference_label)}**; "
            f"substantive class: {CHANGE_BINARY_GROUP.get(reference_label, 'n/a')}", "",
            f"**Qwen LLM: {html.escape(qwen)}**; "
            f"substantive class: {CHANGE_BINARY_GROUP.get(qwen, 'n/a')}", "",
        ])
        parts.extend(disclosure_sections(row, records))
    return "\n".join(parts).rstrip() + "\n", len(rows)


def main(argv=None):
    args = parse_args(argv)
    for ticker in args.ticker:
        directory = args.reference_root / ticker
        if args.year_pair:
            pairs = list(dict.fromkeys(args.year_pair))
        elif directory.is_dir():
            pairs = sorted(path.name for path in directory.iterdir()
                           if path.is_dir() and YEAR_PAIR.fullmatch(path.name))
        else:
            pairs = []
        if not pairs:
            raise ValueError(f"No evaluation year-pairs found for {ticker} in {directory}")
        for pair in pairs:
            reference_path = directory / pair / "alignments.json"
            candidate_path = args.candidate_root / ticker / pair / "alignments.json"
            records = source_disclosures(args.disclosures_dir, ticker, pair.split("-"))
            markdown, count = render_report(ticker, pair, reference_path, candidate_path,
                                            records, reference_model=args.model)
            target = args.output_root / ticker / pair / "change_analysis_disagreements.md"
            atomic_text(target, markdown)
            print(f"Exported {count} LLM change-analysis disagreements to {target}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"LLM change-analysis disagreement export stopped: {error}") from error
