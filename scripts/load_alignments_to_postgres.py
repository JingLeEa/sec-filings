#!/usr/bin/env python3
"""Load disclosure alignment results (alignments.json + needs_review.json) into Postgres.

Reads the two final files of one alignment run (alignment schema "8"):

    data/alignments/<company>/<previous>-<current>/alignments.json
    data/alignments/<company>/<previous>-<current>/needs_review.json

and writes them to alignment_runs, alignments and alignment_members
(see db/init/02_disclosures_alignments.sql).

Match IDs are not stable across fresh model runs, so a rerun replaces that
comparison's rows completely instead of merging with the old ones.

Example (database running, from the repository root):

    python scripts/load_alignments_to_postgres.py --company amd --previous-year 2024 --current-year 2025
    python scripts/load_alignments_to_postgres.py --company amd --previous-year 2024 --current-year 2025 \
        --dir tests/fixtures/alignments/amd/2024-2025
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_to_postgres import ROOT, build_dsn  # noqa: E402  (shared connection settings)

SUPPORTED_SCHEMA = "8"
KNOWN = {
    "match_id", "previous_ids", "current_ids", "relationship", "status", "match_method", "explanation",
    "verifier_review_reason", "review_reasons", "unmatched_type", "unmatched_notes", "evidence",
    "grouping", "validation_errors", "change_analysis", "previous_disclosures", "current_disclosures",
}


def alignment_row(record: dict, run_id: int, source_file: str) -> dict:
    return {
        "run_id": run_id,
        "match_id": record["match_id"],
        "status": record["status"],
        "relationship": record["relationship"],
        "match_method": record.get("match_method"),
        "explanation": record.get("explanation"),
        "verifier_review_reason": record.get("verifier_review_reason"),
        "review_reasons": list(record.get("review_reasons") or []),
        "unmatched_type": record.get("unmatched_type"),
        "unmatched_notes": list(record.get("unmatched_notes") or []),
        "source_file": source_file,
        "evidence": json.dumps(record.get("evidence") or [], ensure_ascii=False),
        "grouping": json.dumps(record["grouping"], ensure_ascii=False) if record.get("grouping") is not None else None,
        "validation_errors": (json.dumps(record["validation_errors"], ensure_ascii=False)
                              if record.get("validation_errors") is not None else None),
        "change_analysis": json.dumps(record.get("change_analysis") or {}, ensure_ascii=False),
        "extra": json.dumps({k: v for k, v in record.items() if k not in KNOWN}, ensure_ascii=False),
    }


def member_rows(record: dict, run_id: int) -> list[dict]:
    rows = []
    for side, ids_key, meta_key in (("previous", "previous_ids", "previous_disclosures"),
                                    ("current", "current_ids", "current_disclosures")):
        meta = {m["disclosure_id"]: m for m in record.get(meta_key) or []}
        for disclosure_id in record.get(ids_key) or []:
            m = meta.get(disclosure_id, {})
            rows.append({
                "run_id": run_id, "match_id": record["match_id"], "side": side,
                "disclosure_id": disclosure_id, "item": m.get("item"), "section": m.get("section"),
                "taxonomy": m.get("taxonomy"), "summary": m.get("summary"),
                "extraction_status": m.get("extraction_status"),
            })
    return rows


def validate_report(report: dict, name: str, company: str, previous: int, current: int) -> list[str]:
    problems = []
    if str(report.get("schema_version")) != SUPPORTED_SCHEMA:
        problems.append(f"{name}: schema_version {report.get('schema_version')!r}, expected {SUPPORTED_SCHEMA!r}")
    if str(report.get("company")) != company or str(report.get("previous_year")) != str(previous) \
            or str(report.get("current_year")) != str(current):
        problems.append(f"{name}: file is for {report.get('company')} {report.get('previous_year')}-"
                        f"{report.get('current_year')}, not {company} {previous}-{current}")
    if report.get("alignment_count") != len(report.get("alignments", [])):
        problems.append(f"{name}: alignment_count does not match the number of rows")
    return problems


INSERT_ALIGNMENT = """
INSERT INTO alignments (run_id, match_id, status, relationship, match_method, explanation, verifier_review_reason,
                        review_reasons, unmatched_type, unmatched_notes, source_file, evidence, grouping,
                        validation_errors, change_analysis, extra)
VALUES (%(run_id)s, %(match_id)s, %(status)s, %(relationship)s, %(match_method)s, %(explanation)s,
        %(verifier_review_reason)s, %(review_reasons)s, %(unmatched_type)s, %(unmatched_notes)s, %(source_file)s,
        %(evidence)s::jsonb, %(grouping)s::jsonb, %(validation_errors)s::jsonb, %(change_analysis)s::jsonb,
        %(extra)s::jsonb)
"""
INSERT_MEMBER = """
INSERT INTO alignment_members (run_id, match_id, side, disclosure_id, item, section, taxonomy, summary, extraction_status)
VALUES (%(run_id)s, %(match_id)s, %(side)s, %(disclosure_id)s, %(item)s, %(section)s, %(taxonomy)s, %(summary)s,
        %(extraction_status)s)
ON CONFLICT DO NOTHING
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", required=True)
    parser.add_argument("--previous-year", required=True, type=int)
    parser.add_argument("--current-year", required=True, type=int)
    parser.add_argument("--dir", help="Folder containing alignments.json and needs_review.json "
                                      "(default: data/alignments/<company>/<previous>-<current>)")
    parser.add_argument("--dry-run", action="store_true", help="Check the files only; do not touch the database")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    folder = Path(args.dir) if args.dir else ROOT / "data" / "alignments" / args.company / f"{args.previous_year}-{args.current_year}"
    reports = {}
    for name in ("alignments", "needs_review"):
        path = folder / f"{name}.json"
        if not path.is_file():
            print(f"Missing input file: {path}", file=sys.stderr)
            return 1
        reports[name] = json.loads(path.read_text(encoding="utf-8"))

    problems = []
    for name, report in reports.items():
        problems += validate_report(report, name, args.company, args.previous_year, args.current_year)
    ids = [a["match_id"] for r in reports.values() for a in r["alignments"]]
    if len(ids) != len(set(ids)):
        problems.append("a match_id appears more than once across the two files")
    total = len(ids)
    print(f"Read {total} alignment rows from {folder} "
          f"({len(reports['alignments']['alignments'])} in alignments.json, {len(reports['needs_review']['alignments'])} in needs_review.json)")
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Dry run: inputs are consistent. Nothing written.")
        return 0

    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    head = reports["alignments"]
    with psycopg.connect(build_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO alignment_runs (company, previous_year, current_year, schema_version, run_complete,
                                           comparison_scope, note)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (company, previous_year, current_year) DO UPDATE SET
                   schema_version = EXCLUDED.schema_version, run_complete = EXCLUDED.run_complete,
                   comparison_scope = EXCLUDED.comparison_scope, note = EXCLUDED.note, loaded_at = now()
               RETURNING run_id""",
            (args.company, args.previous_year, args.current_year, str(head["schema_version"]),
             bool(head["run_complete"] and reports["needs_review"]["run_complete"]),
             head.get("comparison_scope"), head.get("note")))
        run_id = cur.fetchone()[0]
        cur.execute("DELETE FROM alignments WHERE run_id = %s", (run_id,))  # members go with them
        for name, report in reports.items():
            records = report["alignments"]
            cur.executemany(INSERT_ALIGNMENT, [alignment_row(r, run_id, name) for r in records])
            cur.executemany(INSERT_MEMBER, [m for r in records for m in member_rows(r, run_id)])
        cur.execute("SELECT count(*) FROM alignments WHERE run_id = %s", (run_id,))
        n = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM alignment_members WHERE run_id = %s", (run_id,))
        members = cur.fetchone()[0]
    print(f"Done. {args.company} {args.previous_year}-{args.current_year} (run_id {run_id}): {n} alignments, {members} member rows.")
    return 0 if n == total else 2


if __name__ == "__main__":
    raise SystemExit(main())
