#!/usr/bin/env python3
"""Export materiality label disagreements with source disclosures for review."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from evaluate_alignment_predictions import TICKER, YEAR_PAIR, evaluate_pair, load_rows


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


def source_disclosures(root, ticker, years):
    records = {}
    for year in years:
        for name in ("disclosures.json", "review_candidates.json"):
            path = root / ticker / year / name
            if not path.is_file():
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            if document.get("company") != ticker or str(document.get("fiscal_year")) != year:
                raise ValueError(f"Wrong company or year in {path}")
            for record in document["disclosures"]:
                records.setdefault(record["disclosure_id"], record)
    return records


def quote(value):
    return "\n".join("> " + html.escape(line, quote=False) for line in str(value).splitlines()) or "> (empty)"


def score(value):
    return f"{value:.2f}" if type(value) in {int, float} else "n/a"


def disclosure_sections(row, records):
    parts = []
    citations = {entry["disclosure_id"]: entry.get("sentences", [])
                 for entry in row.get("evidence", [])}
    for side, title in (("previous", "Previous"), ("current", "Current")):
        parts.append(f"### {title} disclosures")
        ids = row[f"{side}_ids"]
        if not ids:
            parts.append("None.\n")
            continue
        metadata = {entry["disclosure_id"]: entry for entry in row.get(f"{side}_disclosures", [])}
        for disclosure_id in ids:
            record = records.get(disclosure_id, {})
            details = metadata.get(disclosure_id, {})
            item = record.get("item") or details.get("item") or "n/a"
            section = record.get("section") or details.get("section") or "n/a"
            parts.append(f"#### {html.escape(disclosure_id)}")
            parts.append(f"Item {html.escape(str(item))}; section: {html.escape(str(section))}\n")
            summary = details.get("summary") or record.get("summary")
            if summary:
                parts.append("**Disclosure summary**\n")
                parts.append(quote(summary) + "\n")
            content = record.get("content")
            source = "saved disclosure content"
            if not isinstance(content, str) or not content.strip():
                content = "\n".join(sentence["text"] for sentence in citations.get(disclosure_id, [])
                                    if isinstance(sentence.get("text"), str))
                source = "alignment citations (fallback)"
            if not content:
                content = "Text unavailable in disclosure files and alignment citations."
                source = "unavailable"
            parts.append(f"**Selected text** ({source})\n")
            parts.append(quote(content) + "\n")
            sentence_ids = [sentence.get("sentence_id") for sentence in citations.get(disclosure_id, [])
                            if sentence.get("sentence_id")]
            if sentence_ids:
                parts.append("Cited sentence IDs: " + ", ".join(html.escape(key) for key in sentence_ids) + "\n")
    return parts


def render_report(ticker, pair, reference_path, candidate_path, records, reference_model="DeepSeek"):
    comparison = evaluate_pair(reference_path, candidate_path, reference_model=reference_model)
    _, reference = load_rows(reference_path)
    _, candidate = load_rows(candidate_path)
    groups = comparison["materiality_analysis"]
    differing = {entry["match_id"] for group in ("model_rows", "deterministic_rows")
                 for entry in groups[group]["disagreements"]}
    rows = [row for row in reference.values() if row["match_id"] in differing]
    counts = groups["model_rows"]
    model_differences = counts["disagreements_count"]
    deterministic_differences = groups["deterministic_rows"]["disagreements_count"]
    model_name = html.escape(reference_model)
    parts = [f"# Materiality disagreements: {ticker.upper()} {pair}", "",
             f"{model_name} is the reference model, not human-verified ground truth. "
             "Only rows with completed, different materiality labels are listed.", "",
             f"Disagreements: {len(rows)} ({model_differences} model, "
             f"{deterministic_differences} deterministic); compared model rows: {counts['compared']}; "
             f"{model_name} model rows completed: {counts['reference_completed']}/{counts['eligible_rows']}.", "",
             f"| # | Match ID | Status | Qwen | {model_name} |", "|---:|---|---|---|---|"]
    for number, row in enumerate(rows, 1):
        other = candidate[row["match_id"]]
        qwen = other["materiality_analysis"]["materiality"]
        reference_label = row["materiality_analysis"]["materiality"]
        parts.append(f"| {number} | [<code>{html.escape(row['match_id'])}</code>](#case-{number}) "
                     f"| {html.escape(row['status'])} | {qwen} | {reference_label} |")
    if not rows:
        parts.extend(["", "No completed materiality label disagreements found."])
    for number, row in enumerate(rows, 1):
        other = candidate[row["match_id"]]
        reference_analysis = row["materiality_analysis"]
        qwen = other["materiality_analysis"]
        parts.extend(["", f'<a id="case-{number}"></a>', f"## {number}. {html.escape(row['match_id'])}", "",
                      f"Status: **{html.escape(row['status'])}**; "
                      f"relationship: **{html.escape(row['relationship'])}**", ""])
        for name, analysis in ((model_name, reference_analysis), ("Qwen", qwen)):
            parts.extend([f"**{name}: {analysis['materiality']}** "
                          f"(strength {score(analysis.get('materiality_score'))}; "
                          f"confidence {score(analysis.get('confidence_score'))})", "",
                          quote(analysis.get("reason") or "No reason saved."), ""])
        parts.extend(disclosure_sections(row, records))
    return "\n".join(parts).rstrip() + "\n", len(rows)


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


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
            target = args.output_root / ticker / pair / "materiality_disagreements.md"
            atomic_text(target, markdown)
            print(f"Exported {count} materiality disagreements to {target}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Materiality disagreement export stopped: {error}") from error
