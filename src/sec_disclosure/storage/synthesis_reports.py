"""Validate and atomically store synthesis agent reports (*_synthesis_output.json) in PostgreSQL.

A report covers one company over a span of fiscal years. Reports go into the synthesis agent's own table,
synthesis_reports(company, years, report jsonb); reloading the same company and years replaces the report.
Both the current agent output (src/sec_disclosure/agents/synthesis.py, written to data/synthesis/) and the
earlier synthesis_dev output are accepted: they share the report, trend and source-catalog fields.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .chatbot_schema import apply_outdated
from .disclosures import connection_arguments

REPORT_TEXT_FIELDS = ("executive_summary", "longitudinal_analysis", "financial_analysis")
TREND_KINDS = {"year_over_year_change", "calculated_metric"}
MATCH_ID = re.compile(r"^[a-z]{1,6}_\d{4}_\d{4}_M\d{3,5}$")


class SynthesisStorageError(RuntimeError):
    """Database failure with a message safe to print without credentials."""


@dataclass(frozen=True)
class SynthesisImport:
    company: str
    start_year: int
    end_year: int
    report_key: str
    findings: int
    trends: int
    valid: bool | None


def resolve_reports(path: Path) -> list[Path]:
    """A report file, or every *_synthesis_output.json directly inside a folder."""
    path = Path(path)
    return sorted(path.glob("*_synthesis_output.json")) if path.is_dir() else [path]


def _number(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"not a number: {value!r}") from None


def read_report(path: Path) -> tuple[dict, bytes]:
    """Parse and validate the whole report before any database work."""
    raw = Path(path).read_bytes()
    report = json.loads(raw.decode("utf-8"))
    problems: list[str] = []
    if not isinstance(report, dict):
        raise ValueError("synthesis output must be a JSON object")
    company = str(report.get("company") or "").strip().lower()
    years = report.get("years")
    if not company:
        problems.append("company is missing")
    if not isinstance(years, list) or len(years) < 2 or not all(str(y).isdigit() for y in years):
        problems.append("years must list at least two fiscal years")
    body = report.get("report")
    if not isinstance(body, dict):
        raise ValueError("; ".join(problems + ["report must be an object"]))
    if not str(body.get("executive_summary") or "").strip():
        problems.append("report.executive_summary is empty")
    for field in REPORT_TEXT_FIELDS[1:]:
        if body.get(field) is not None and not isinstance(body.get(field), str):
            problems.append(f"report.{field} must be text")
    if not isinstance(body.get("limitations", []), list):
        problems.append("report.limitations must be a list")
    findings = body.get("key_findings")
    if not isinstance(findings, list):
        problems.append("report.key_findings must be a list")
        findings = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict) or not str(finding.get("title") or "").strip() \
                or not str(finding.get("analysis") or "").strip() or not isinstance(finding.get("evidence_ids"), list):
            problems.append(f"key_findings[{index}] needs title, analysis and an evidence_ids list")
            continue
        for match_id in (i for i in finding["evidence_ids"] if isinstance(i, str) and MATCH_ID.match(i)):
            if not match_id.startswith(f"{company}_"):
                problems.append(f"key_findings[{index}] cites another company's alignment {match_id}")
    trends = report.get("financial_trends", [])
    if not isinstance(trends, list):
        problems.append("financial_trends must be a list")
        trends = []
    for index, trend in enumerate(trends):
        if not isinstance(trend, dict) or trend.get("type") not in TREND_KINDS:
            problems.append(f"financial_trends[{index}] has an unknown type")
            continue
        try:
            for key in ("old_value", "new_value", "value", "growth_pct"):
                _number(trend.get(key))
        except ValueError as error:
            problems.append(f"financial_trends[{index}]: {error}")
    if problems:
        raise ValueError("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))
    return report, raw


def report_key(company: str, years: list[int]) -> str:
    """The chatbot views' id for a report, e.g. 'amd_2023_2024_2025'."""
    return "_".join([company.lower(), *(str(y) for y in years)])


# Same upsert as the synthesis agent's storage/synthesis.py: one row per (COMPANY, years), whole report as JSONB.
UPSERT_REPORT = """
INSERT INTO synthesis_reports (company, years, report)
VALUES (%(company)s, %(years)s, %(report)s::jsonb)
ON CONFLICT (company, years) DO UPDATE SET report = EXCLUDED.report, generated_at = now()"""


def store_synthesis_report(path: Path, *, env_file: Path | None = None) -> SynthesisImport:
    """Upsert one report file into the synthesis agent's synthesis_reports table (the chatbot reads it via views).

    The pipeline already saves every report it generates; this is for report files produced elsewhere
    (a teammate's run, test_data/synthesis samples).
    """
    report, raw = read_report(path)
    company = str(report["company"]).strip().upper()
    years = sorted(int(y) for y in report["years"])
    try:
        import psycopg
    except ImportError:
        raise SynthesisStorageError("psycopg is not installed. Run: pip install -r requirements.txt") from None
    try:
        with psycopg.connect(**connection_arguments(env_file)) as conn, conn.cursor() as cur:
            apply_outdated(cur)
            cur.execute(UPSERT_REPORT, {"company": company, "years": years, "report": raw.decode("utf-8")})
    except Exception as error:
        raise SynthesisStorageError(
            f"PostgreSQL synthesis import failed ({type(error).__name__}). Check that the database is running "
            "and POSTGRES_* or DATABASE_URL settings are correct.") from None
    return SynthesisImport(company.lower(), years[0], years[-1], report_key(company, years),
                           len(report["report"]["key_findings"]), len(report.get("financial_trends") or []),
                           (report.get("validation") or {}).get("valid"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path,
                        help="*_synthesis_output.json files, or folders holding them (e.g. data/synthesis)")
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--dry-run", action="store_true", help="Validate reports without connecting to PostgreSQL")
    args = parser.parse_args(argv)
    status = 0
    files = [f for p in args.paths for f in resolve_reports(p)]
    if not files:
        print("No *_synthesis_output.json files found.", file=sys.stderr)
        return 1
    for path in files:
        try:
            if args.dry_run:
                report, _ = read_report(path)
                print(f"Dry run OK: {path} ({len(report['report']['key_findings'])} findings, "
                      f"{len(report.get('financial_trends') or [])} trends).")
                continue
            r = store_synthesis_report(path, env_file=args.env_file)
            print(f"Done. {r.company} {r.start_year}-{r.end_year} [{r.report_key}]: {r.findings} findings, "
                  f"{r.trends} financial trends" + ("" if r.valid is not False else ", agent validation FAILED") + ".")
        except (OSError, ValueError, KeyError, TypeError, SynthesisStorageError) as error:
            print(f"Synthesis import error ({path}): {error}", file=sys.stderr)
            status = 1
    return status
