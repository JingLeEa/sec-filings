"""Validate and atomically store completed alignment snapshots in PostgreSQL."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from sec_disclosure.storage.artifacts import ArtifactPath as Path

from .disclosures import ROOT, connection_arguments
from .pipeline_snapshots import ALIGNMENT_FILES, folder_documents, publish


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


class AlignmentStorageError(RuntimeError):
    """Database failure with a message safe to print without credentials."""


@dataclass(frozen=True)
class AlignmentImport:
    company: str
    previous_year: int
    current_year: int
    run_id: int
    stored: int
    members: int
    finalized: int
    review: int


def read_alignments(folder: Path, company: str, previous: int, current: int, *, require_complete: bool = True):
    """Validate the entire snapshot before connecting or replacing a saved run."""
    reports, problems, ids = {}, [], []
    for name in ("alignments", "needs_review"):
        report = json.loads((folder / f"{name}.json").read_text(encoding="utf-8"))
        if not isinstance(report, dict) or not isinstance(report.get("alignments"), list):
            raise ValueError(f"{name}.json: expected a report object with an alignments array")
        problems.extend(validate_report(report, name, company, previous, current))
        if require_complete and (report.get("run_complete") is not True or report.get("error")):
            problems.append(f"{name}: finish alignment before importing its snapshot")
        for record in report["alignments"]:
            if not isinstance(record, dict):
                raise ValueError(f"{name}: each alignment must be an object")
            match_id = record.get("match_id")
            prefix = f"{company}_{previous}_{current}_M"
            if not isinstance(match_id, str) or not match_id.startswith(prefix) or not match_id[len(prefix):].isdigit():
                raise ValueError(f"{name}: match ID does not belong to {company} {previous}-{current}")
            allowed = {"needs_review"} if name == "needs_review" else {"auto_matched", "ai_verified", "unmatched"}
            if record.get("status") not in allowed:
                raise ValueError(f"{name}: alignment status is outside its report partition")
            if record.get("relationship") not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many", "previous_only", "current_only"}:
                raise ValueError(f"{name}: invalid alignment relationship")
            for side, year in (("previous", previous), ("current", current)):
                members = record.get(side + "_ids")
                if (not isinstance(members, list) or any(not isinstance(key, str) or not key.startswith(f"{company}_{year}_") for key in members)
                        or len(set(members)) != len(members)):
                    raise ValueError(f"{name}: invalid or cross-filing {side} disclosure IDs")
                details = record.get(side + "_disclosures", [])
                if not isinstance(details, list) or any(not isinstance(detail, dict) or detail.get("disclosure_id") not in members for detail in details):
                    raise ValueError(f"{name}: invalid {side} disclosure metadata")
            ids.append(match_id)
        reports[name] = report
    shared = ("schema_version", "company", "previous_year", "current_year", "comparison_scope", "run_complete", "error", "overall_counts")
    if any(reports["alignments"].get(key) != reports["needs_review"].get(key) for key in shared):
        problems.append("alignments.json and needs_review.json must belong to the same saved run")
    if len(ids) != len(set(ids)):
        problems.append("a match_id appears more than once across the two files")
    if problems:
        raise ValueError("; ".join(problems))
    return reports


def store_alignments(folder: Path, company: str, previous: int, current: int, *, env_file: Path | None = None,
                     require_complete: bool = True) -> AlignmentImport:
    """Replace one comparison's decisions and members in a single transaction."""
    reports = read_alignments(Path(folder), company, previous, current, require_complete=require_complete)
    # Finish mapping before any DELETE; malformed evidence cannot erase a prior run.
    rows = [(name, record) for name, report in reports.items() for record in report["alignments"]]
    alignment_rows = [alignment_row(record, 0, name) for name, record in rows]
    members = [member for _, record in rows for member in member_rows(record, 0)]
    arguments = connection_arguments(env_file)
    try:
        import psycopg
    except ImportError:
        raise AlignmentStorageError("psycopg is not installed. Run: .venv/bin/python -m pip install -r requirements.txt") from None
    try:
        with psycopg.connect(**arguments) as conn, conn.cursor() as cur:
            head = reports["alignments"]
            cur.execute("""
                INSERT INTO alignment_runs (company, previous_year, current_year, schema_version, run_complete,
                                            comparison_scope, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (company, previous_year, current_year) DO UPDATE SET
                    schema_version=EXCLUDED.schema_version, run_complete=EXCLUDED.run_complete,
                    comparison_scope=EXCLUDED.comparison_scope, note=EXCLUDED.note, loaded_at=now()
                RETURNING run_id
            """, (company, previous, current, str(head["schema_version"]),
                  bool(head["run_complete"] and reports["needs_review"]["run_complete"]), head.get("comparison_scope"), head.get("note")))
            run_id = cur.fetchone()[0]
            cur.execute("DELETE FROM alignments WHERE run_id = %s", (run_id,))
            for row in alignment_rows + members:
                row["run_id"] = run_id
            if alignment_rows:
                cur.executemany(INSERT_ALIGNMENT, alignment_rows)
            if members:
                cur.executemany(INSERT_MEMBER, members)
            cur.execute("SELECT count(*) FROM alignments WHERE run_id = %s", (run_id,))
            stored = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM alignment_members WHERE run_id = %s", (run_id,))
            member_count = cur.fetchone()[0]
            if stored != len(alignment_rows) or member_count != len(members):
                raise ValueError("Imported alignment snapshot has inconsistent row counts")
            publish(cur, company, "alignments", previous, current,
                    folder_documents(folder, ALIGNMENT_FILES, include_change_cache=True))
    except Exception as error:
        raise AlignmentStorageError(f"PostgreSQL alignment import failed ({type(error).__name__}). "
            "Check that the database is running, the tables exist and POSTGRES_* or DATABASE_URL settings are correct. "
            "Saved alignment JSON is available for retry.") from None
    return AlignmentImport(company, previous, current, run_id, stored, member_count,
                           len(reports["alignments"]["alignments"]), len(reports["needs_review"]["alignments"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True)
    parser.add_argument("--previous-year", type=int, required=True)
    parser.add_argument("--current-year", type=int, required=True)
    parser.add_argument("--dir", type=Path, help="Exact folder containing the two final reports")
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--dry-run", action="store_true", help="Validate reports without connecting to PostgreSQL")
    args = parser.parse_args(argv)
    company = args.company.lower()
    folder = args.dir or ROOT / "data" / "alignments" / company / f"{args.previous_year}-{args.current_year}"
    try:
        if args.dry_run:
            reports = read_alignments(folder, company, args.previous_year, args.current_year)
            print(f"Dry run: {sum(len(r['alignments']) for r in reports.values())} alignments. Nothing written.")
            return 0
        result = store_alignments(folder, company, args.previous_year, args.current_year, env_file=args.env_file)
        print(f"Done. {company} {args.previous_year}-{args.current_year} (run_id {result.run_id}): "
              f"{result.stored} alignments, {result.members} member rows in PostgreSQL.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AlignmentStorageError) as error:
        print(f"Alignment import error: {error}", file=sys.stderr)
        return 1

