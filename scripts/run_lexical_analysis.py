#!/usr/bin/env python3
"""Populate lexical change analysis for a saved SEC disclosure alignment."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sec_disclosure.agents.disclosure_alignment import (
    load_alignment_report,
    write_alignment_json_reports,
)

from sec_disclosure.analysis.lexical_analysis import compute_lexical_metrics


def _disclosures_path(root: Path, ticker: str, year: int) -> Path:
    candidates = [
        root / ticker.lower() / str(year) / "disclosures.json",
        root / ticker.upper() / str(year) / "disclosures.json",
        root / str(year) / "disclosures.json",
        root / "disclosures.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _load_disclosures(root: Path, ticker: str, year: int) -> dict[str, dict]:
    path = _disclosures_path(root, ticker, year)
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(row["disclosure_id"]): row
        for row in payload.get("disclosures", [])
        if row.get("disclosure_id") is not None
    }


def _member_text(
    ids: list[str],
    lookup: dict[str, dict],
    metadata: list[dict],
    evidence: list[dict],
) -> str:
    meta_by_id = {str(row.get("disclosure_id")): row for row in metadata}
    evidence_by_id: dict[str, list[str]] = {}
    for citation in evidence:
        evidence_by_id.setdefault(str(citation.get("disclosure_id")), []).extend(
            sentence.get("text", "")
            for sentence in citation.get("sentences", [])
            if sentence.get("text")
        )
    parts = []
    for disclosure_id in ids:
        record = lookup.get(str(disclosure_id), {})
        text = record.get("content") or record.get("text")
        if isinstance(text, list):
            text = " ".join(str(item) for item in text)
        if not text:
            text = meta_by_id.get(str(disclosure_id), {}).get("summary", "")
        if not text:
            text = " ".join(evidence_by_id.get(str(disclosure_id), []))
        if text:
            parts.append(str(text))
    return "\n".join(parts)


def run(
    ticker: str,
    previous_year: int,
    current_year: int,
    alignments_dir: Path,
    disclosures_dir: Path,
) -> dict:
    report = load_alignment_report(alignments_dir)
    if (
        str(report.get("company", "")).lower() != ticker.lower()
        or int(report.get("previous_year", -1)) != previous_year
        or int(report.get("current_year", -1)) != current_year
    ):
        raise ValueError(
            "Alignment report company or years do not match the requested comparison."
        )
    previous = _load_disclosures(disclosures_dir, ticker, previous_year)
    current = _load_disclosures(disclosures_dir, ticker, current_year)

    for row in report.get("alignments", []):
        previous_text = _member_text(
            row.get("previous_ids", []),
            previous,
            row.get("previous_disclosures", []),
            row.get("evidence", []),
        )
        current_text = _member_text(
            row.get("current_ids", []),
            current,
            row.get("current_disclosures", []),
            row.get("evidence", []),
        )

        metrics = compute_lexical_metrics(previous_text, current_text)

        unmatched_override = {
            "introduced_disclosure": "New",
            "removed_disclosure": "Removed",
        }.get(row.get("unmatched_type"))

        if unmatched_override:
            metrics["change_taxonomy"] = unmatched_override

        analysis = row.setdefault(
            "change_analysis",
            {
                "status": "not_started",
                "lexical": None,
                "semantic": None,
                "llm": None,
                "final_taxonomy": None,
            },
        )
        analysis["status"] = "in_progress"
        analysis["lexical"] = metrics

    write_alignment_json_reports(alignments_dir, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True, type=int)
    parser.add_argument("--current-year", required=True, type=int)
    parser.add_argument("--alignments-dir", required=True, type=Path)
    parser.add_argument("--disclosures-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = run(
            args.ticker,
            args.previous_year,
            args.current_year,
            args.alignments_dir,
            args.disclosures_dir,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Lexical analysis failed: {exc}", file=sys.stderr)
        return 2
    print(
        f"Processed {len(report['alignments'])} rows for {args.ticker.upper()} "
        f"{args.previous_year}-{args.current_year}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())