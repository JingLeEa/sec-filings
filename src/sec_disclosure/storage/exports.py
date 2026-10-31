"""Join normalized PostgreSQL tables into JSON reports for downstream analysis."""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from sec_disclosure.storage.artifacts import ArtifactPath as Path
import re
import sys
import tempfile

from .disclosures import connection_arguments


REPORT_FILES = {
    "disclosures": ("disclosures.json", "review_candidates.json"),
    "alignments": ("alignments.json", "needs_review.json"),
}


class DownstreamExportError(RuntimeError):
    """Missing normalized data or a safe-to-print database export failure."""


def _company(value):
    value = value.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", value):
        raise ValueError("Provide the company slug used by extraction, for example amd")
    return value


def _scopes(years, stage):
    years = list(years)
    if not years or any(type(year) is not int or not 1900 <= year <= 2200 for year in years):
        raise ValueError("Provide four-digit fiscal years between 1900 and 2200")
    years = sorted(set(years))
    if stage not in {"all", "disclosures", "alignments"}:
        raise ValueError("Stage must be all, disclosures or alignments")
    if stage == "alignments" and len(years) < 2:
        raise ValueError("Alignment export requires at least two selected years")
    scopes = [("disclosures", year, year) for year in years] if stage != "alignments" else []
    if stage != "disclosures":
        scopes.extend(("alignments", previous, current) for previous, current in zip(years, years[1:]))
    return scopes


def _disclosure(row, company, year):
    # Typed columns take precedence over any colliding keys in extra.
    result = dict(row.get("extra") or {})
    result.update({key: value for key, value in row.items() if key not in {"extra", "filing_id"}})
    result.update(company=company, fiscal_year=str(year))
    return result


def _disclosure_reports(cur, company, year):
    cur.execute("SELECT filing_id FROM filings WHERE company=%s AND fiscal_year=%s AND form_type='10-K'",
                (company, year))
    if cur.fetchone() is None:
        raise DownstreamExportError(f"No 10-K filing is loaded for {company} {year}. Import its disclosures first.")
    cur.execute("""
        SELECT d.*, f.company, f.fiscal_year
        FROM disclosures d JOIN filings f ON f.filing_id=d.filing_id
        WHERE f.company=%s AND f.fiscal_year=%s AND f.form_type='10-K'
        ORDER BY d.disclosure_id
    """, (company, year))
    partition = {"accepted": [], "review_candidate": []}
    for row in cur.fetchall():
        partition[row["review_status"]].append(_disclosure(row, company, year))
    return {name: {"export_schema_version": "1", "company": company, "fiscal_year": str(year),
                   "disclosure_count": len(partition[status]), "disclosures": partition[status]}
            for name, status in zip(REPORT_FILES["disclosures"], partition)}


def _alignment_reports(cur, company, previous, current):
    cur.execute("""
        SELECT run_id, schema_version, run_complete, comparison_scope, note
        FROM alignment_runs WHERE company=%s AND previous_year=%s AND current_year=%s
    """, (company, previous, current))
    run = cur.fetchone()
    if run is None:
        raise DownstreamExportError(f"No alignment run is loaded for {company} {previous}-{current}. Import its alignments first.")
    if not run["run_complete"]:
        raise DownstreamExportError(f"Alignment run {company} {previous}-{current} is incomplete.")
    cur.execute("SELECT * FROM alignments WHERE run_id=%s ORDER BY match_id", (run["run_id"],))
    decisions = cur.fetchall()
    cur.execute("""
        SELECT m.match_id, m.side, to_jsonb(m) - 'run_id' - 'match_id' - 'side' AS member,
               to_jsonb(d) AS disclosure, f.company, f.fiscal_year, f.form_type
        FROM alignment_runs r
        JOIN alignments a ON a.run_id=r.run_id
        JOIN alignment_members m ON m.run_id=a.run_id AND m.match_id=a.match_id
        LEFT JOIN disclosures d ON d.disclosure_id=m.disclosure_id
        LEFT JOIN filings f ON f.filing_id=d.filing_id
        WHERE r.run_id=%s
        ORDER BY m.match_id, m.side, m.disclosure_id
    """, (run["run_id"],))
    members = {row["match_id"]: {"previous": [], "current": []} for row in decisions}
    missing = []
    for row in cur.fetchall():
        metadata = row["member"]
        year = previous if row["side"] == "previous" else current
        if (row["disclosure"] is None or row["company"] != company or
                row["fiscal_year"] != year or row["form_type"] != "10-K"):
            missing.append(metadata["disclosure_id"])
            continue
        disclosure = _disclosure(row["disclosure"], company, year)
        # Keep alignment-time metadata when supplied, filling absent metadata
        # from the full disclosure. Text/evidence always come from disclosures.
        disclosure.update({key: value for key, value in metadata.items() if value is not None})
        disclosure["extraction_status"] = metadata.get("extraction_status")
        members[row["match_id"]][row["side"]].append(disclosure)
    if missing:
        ids = sorted(set(missing))
        raise DownstreamExportError(
            f"Alignment {company} {previous}-{current} has {len(ids)} disclosure IDs missing from the expected filings "
            f"({', '.join(ids[:5])}). Import disclosures for both years before exporting; no partial reports were written.")
    partition = {"alignments": [], "needs_review": []}
    for row in decisions:
        if row["source_file"] not in partition:
            raise DownstreamExportError(f"Invalid alignment source_file for {row['match_id']}")
        record = dict(row.get("extra") or {})
        record.update({key: value for key, value in row.items() if key not in {"extra", "run_id", "source_file"}})
        for side in ("previous", "current"):
            details = members[row["match_id"]][side]
            record[side + "_ids"] = [detail["disclosure_id"] for detail in details]
            record[side + "_disclosures"] = details
        partition[row["source_file"]].append(record)
    counts = dict(Counter(row["status"] for row in decisions))
    common = {"export_schema_version": "1", "source_schema_version": run["schema_version"],
              "company": company, "previous_year": str(previous), "current_year": str(current),
              "run_complete": run["run_complete"], "comparison_scope": run["comparison_scope"],
              "note": run["note"], "overall_counts": counts}
    return {name: {**common, "alignment_count": len(partition[source]), "alignments": partition[source]}
            for name, source in zip(REPORT_FILES["alignments"], partition)}


def _read_reports(company, scopes, env_file):
    try:
        import psycopg
        from psycopg import IsolationLevel
        from psycopg.rows import dict_row
    except ImportError:
        raise DownstreamExportError("Install dependencies with: .venv/bin/python -m pip install -r requirements.txt") from None
    arguments = connection_arguments(env_file)
    try:
        with psycopg.connect(**arguments, row_factory=dict_row) as conn:
            conn.read_only = True
            conn.isolation_level = IsolationLevel.REPEATABLE_READ
            with conn.cursor() as cur:
                reports = {}
                for stage, first, last in scopes:
                    reports[(stage, first, last)] = (_disclosure_reports(cur, company, first) if stage == "disclosures"
                                                    else _alignment_reports(cur, company, first, last))
                return reports
    except DownstreamExportError:
        raise
    except Exception as error:
        # Driver exceptions can contain connection credentials.
        raise DownstreamExportError(f"PostgreSQL export failed ({type(error).__name__}). "
            "Check the connection and normalized tables. No database rows were changed.") from None


def get_disclosure_reports(company: str, year: int, *, env_file: Path | None = None) -> dict:
    """Join filings/disclosures and return accepted/review report objects, keyed by filename."""
    company = _company(company)
    scopes = _scopes([year], "disclosures")
    return _read_reports(company, scopes, env_file)[scopes[0]]


def get_alignment_reports(company: str, previous_year: int, current_year: int, *, env_file: Path | None = None) -> dict:
    """Join runs/decisions/members/disclosures/filings, including full member text and evidence."""
    company = _company(company)
    scopes = _scopes([previous_year, current_year], "alignments")
    if previous_year >= current_year:
        raise ValueError("Previous year must be earlier than current year")
    return _read_reports(company, scopes, env_file)[scopes[0]]


def export_reports(company: str, years, output_dir: Path, *, stage="all", env_file=None, overwrite=False) -> list[Path]:
    """Write joined reports beneath OUTPUT/{disclosures,alignments}/COMPANY/YEARS/."""
    company = _company(company)
    scopes = _scopes(years, stage)
    reports = _read_reports(company, scopes, env_file)
    targets = []
    for (stage_name, first, last), files in reports.items():
        folder = str(first) if stage_name == "disclosures" else f"{first}-{last}"
        for filename in REPORT_FILES[stage_name]:
            target = Path(output_dir) / stage_name / company / folder / filename
            raw = (json.dumps(files[filename], ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            if target.is_symlink() or target.exists() and not target.is_file():
                raise DownstreamExportError(f"Export destination is not a regular file: {target}")
            if target.exists() and target.read_bytes() != raw and not overwrite:
                raise DownstreamExportError(f"Different output already exists: {target}. Choose another --output-dir or use --overwrite.")
            targets.append((target, raw))
    # Validate every requested scope and destination before writing any report.
    for target, raw in targets:
        if target.exists() and target.read_bytes() == raw:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        temporary = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            if overwrite:
                temporary.replace(target)
            else:
                os.link(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return [target for target, _ in targets]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True, help="Company slug used by extraction")
    parser.add_argument("--years", nargs="+", type=int, required=True, help="Filing years; adjacent selections identify alignment pairs")
    parser.add_argument("--stage", choices=("all", "disclosures", "alignments"), default="all")
    parser.add_argument("--output-dir", type=Path, default=Path("data/downstream"))
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--overwrite", action="store_true", help="Replace different existing export files; identical files are reused")
    args = parser.parse_args(argv)
    try:
        paths = export_reports(args.company, args.years, args.output_dir.expanduser(), stage=args.stage,
                               env_file=args.env_file, overwrite=args.overwrite)
        for path in paths:
            print(path)
        print(f"Exported {len(paths)} joined JSON reports from normalized PostgreSQL tables.")
        return 0
    except (OSError, ValueError, DownstreamExportError) as error:
        print(f"Downstream export error: {error}", file=sys.stderr)
        return 1
