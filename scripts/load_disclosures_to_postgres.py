#!/usr/bin/env python3
"""Load extracted disclosures (disclosures.json + review_candidates.json) into Postgres.

Reads one company and fiscal year of disclosure extraction output (extraction schema "3"):

    data/disclosures/<company>/<year>/disclosures.json         -> review_status 'accepted'
    data/disclosures/<company>/<year>/review_candidates.json   -> review_status 'review_candidate'

and writes it to the disclosures table (see db/init/02_disclosures_alignments.sql).
Safe to rerun: rows are keyed by disclosure_id and updated in place.

Example (database running, from the repository root):

    python scripts/load_disclosures_to_postgres.py --company amd --year 2025
    python scripts/load_disclosures_to_postgres.py --company amd --year 2025 --disclosures-dir data/disclosures_run2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_to_postgres import ROOT, build_dsn  # noqa: E402  (shared connection settings)

SUPPORTED_SCHEMA = "3"
KNOWN = {"disclosure_id", "company", "fiscal_year", "item", "section", "sections", "topic", "summary",
         "content", "taxonomy", "sources", "verification"}

UPSERT_FILING = """
INSERT INTO filings (company, fiscal_year, form_type) VALUES (%s, %s, '10-K')
ON CONFLICT (company, fiscal_year, form_type) DO UPDATE SET company = EXCLUDED.company
RETURNING filing_id
"""
UPSERT_DISCLOSURE = """
INSERT INTO disclosures (disclosure_id, filing_id, item, section, sections, topic, summary, content, taxonomy,
                         review_status, sources, verification, extra)
VALUES (%(disclosure_id)s, %(filing_id)s, %(item)s, %(section)s, %(sections)s, %(topic)s, %(summary)s,
        %(content)s, %(taxonomy)s, %(review_status)s, %(sources)s::jsonb, %(verification)s::jsonb, %(extra)s::jsonb)
ON CONFLICT (disclosure_id) DO UPDATE SET
    filing_id = EXCLUDED.filing_id, item = EXCLUDED.item, section = EXCLUDED.section, sections = EXCLUDED.sections,
    topic = EXCLUDED.topic, summary = EXCLUDED.summary, content = EXCLUDED.content, taxonomy = EXCLUDED.taxonomy,
    review_status = EXCLUDED.review_status, sources = EXCLUDED.sources, verification = EXCLUDED.verification,
    extra = EXCLUDED.extra
"""


def disclosure_row(record: dict, filing_id: int, review_status: str) -> dict:
    return {
        "disclosure_id": record["disclosure_id"],
        "filing_id": filing_id,
        "item": str(record["item"]),
        "section": record.get("section"),
        "sections": list(record.get("sections") or []),
        "topic": record.get("topic"),
        "summary": record.get("summary"),
        "content": record["content"],
        "taxonomy": record["taxonomy"],
        "review_status": review_status,
        "sources": json.dumps(record.get("sources") or [], ensure_ascii=False),
        "verification": json.dumps(record.get("verification") or {}, ensure_ascii=False),
        "extra": json.dumps({k: v for k, v in record.items() if k not in KNOWN}, ensure_ascii=False),
    }


def validate_report(report: dict, name: str, company: str, year: int) -> list[str]:
    problems = []
    if str(report.get("schema_version")) != SUPPORTED_SCHEMA:
        problems.append(f"{name}: schema_version {report.get('schema_version')!r}, expected {SUPPORTED_SCHEMA!r}")
    if str(report.get("company")) != company or str(report.get("fiscal_year")) != str(year):
        problems.append(f"{name}: file is for {report.get('company')} {report.get('fiscal_year')}, not {company} {year}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--disclosures-dir", default=str(ROOT / "data" / "disclosures"),
                        help="Folder that contains <company>/<year>/ (default: data/disclosures)")
    parser.add_argument("--dry-run", action="store_true", help="Check the files only; do not touch the database")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    folder = Path(args.disclosures_dir) / args.company / str(args.year)
    inputs = (("disclosures.json", "accepted"), ("review_candidates.json", "review_candidate"))
    loaded: list[tuple[str, list[dict]]] = []
    problems: list[str] = []
    for name, status in inputs:
        path = folder / name
        if not path.is_file():
            print(f"Missing input file: {path}", file=sys.stderr)
            return 1
        report = json.loads(path.read_text(encoding="utf-8"))
        problems += validate_report(report, name, args.company, args.year)
        loaded.append((status, report.get("disclosures", [])))
    ids = [r["disclosure_id"] for _, records in loaded for r in records]
    if len(ids) != len(set(ids)):
        problems.append("a disclosure_id appears more than once across the two files")
    print(f"Read {len(ids)} disclosures from {folder} "
          f"({len(loaded[0][1])} accepted, {len(loaded[1][1])} review candidates)")
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

    with psycopg.connect(build_dsn()) as conn, conn.cursor() as cur:
        cur.execute(UPSERT_FILING, (args.company, args.year))
        filing_id = cur.fetchone()[0]
        for status, records in loaded:
            cur.executemany(UPSERT_DISCLOSURE, [disclosure_row(r, filing_id, status) for r in records])
        cur.execute("SELECT count(*) FROM disclosures WHERE filing_id = %s", (filing_id,))
        n = cur.fetchone()[0]
    print(f"Done. {args.company} {args.year} (filing_id {filing_id}): {n} disclosures in the database.")
    if n != len(ids):
        print("WARNING: the database holds disclosures from an earlier load that are no longer in the files.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
