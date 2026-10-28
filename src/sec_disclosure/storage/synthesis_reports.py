"""Validate and atomically store synthesis agent reports (*_synthesis_output.json) in PostgreSQL.

A report covers one company over a span of fiscal years. Reloading the same company and span replaces it.
Both the current agent output (src/sec_disclosure/agents/synthesis.py, written to data/synthesis/) and the
earlier synthesis_dev output are accepted: they share the report, trend and source-catalog fields.
"""
from __future__ import annotations

import argparse
import hashlib
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
    report_id: int
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


def rows_for(report: dict) -> tuple[dict, list[dict], list[dict]]:
    body = report["report"]
    years = sorted(int(y) for y in report["years"])
    head = {
        "company": str(report["company"]).lower(), "start_year": years[0], "end_year": years[-1], "years": years,
        "executive_summary": body["executive_summary"].strip(),
        "longitudinal_analysis": body.get("longitudinal_analysis"),
        "financial_analysis": body.get("financial_analysis"),
        "limitations": [str(x) for x in body.get("limitations") or []],
        "valid": (report.get("validation") or {}).get("valid"),
        "validation": json.dumps(report.get("validation") or {}, ensure_ascii=False, default=str),
        "coverage": json.dumps({"input_summary": report.get("input_summary"),
                                "coverage_summary": report.get("coverage_summary"),
                                "coverage_gaps": report.get("coverage_gaps")}, ensure_ascii=False, default=str),
        "financial_metrics": json.dumps(report.get("financial_metrics") or [], ensure_ascii=False, default=str),
    }
    findings = []
    for ordinal, finding in enumerate(body["key_findings"], start=1):
        ids = [str(i) for i in finding["evidence_ids"]]
        findings.append({"ordinal": ordinal, "title": finding["title"].strip(), "analysis": finding["analysis"].strip(),
                         "evidence_ids": ids, "match_ids": [i for i in ids if MATCH_ID.match(i)],
                         "metric_ids": [i for i in ids if i.startswith("xbrl:")]})
    trends = []
    for ordinal, trend in enumerate(report.get("financial_trends") or [], start=1):
        trends.append({"ordinal": ordinal, "kind": trend["type"],
                       "metric": trend.get("label") or trend.get("name") or trend.get("concept") or "metric",
                       "concept": trend.get("concept"), "unit": trend.get("unit"), "year": trend.get("year"),
                       "from_year": trend.get("from_year"), "to_year": trend.get("to_year"),
                       "old_value": _number(trend.get("old_value")), "new_value": _number(trend.get("new_value")),
                       "value": _number(trend.get("value")), "growth_pct": _number(trend.get("growth_pct")),
                       "formula": trend.get("formula"), "source_ids": [str(s) for s in trend.get("source_ids") or []]})
    return head, findings, trends


INSERT_REPORT = """
INSERT INTO synthesis_reports (company, start_year, end_year, years, executive_summary, longitudinal_analysis,
    financial_analysis, limitations, valid, validation, coverage, financial_metrics, source_file, source_sha256)
VALUES (%(company)s, %(start_year)s, %(end_year)s, %(years)s, %(executive_summary)s, %(longitudinal_analysis)s,
    %(financial_analysis)s, %(limitations)s, %(valid)s, %(validation)s::jsonb, %(coverage)s::jsonb,
    %(financial_metrics)s::jsonb, %(source_file)s, %(source_sha256)s)
RETURNING report_id"""
INSERT_FINDING = """
INSERT INTO synthesis_findings (report_id, ordinal, title, analysis, evidence_ids, match_ids, metric_ids)
VALUES (%(report_id)s, %(ordinal)s, %(title)s, %(analysis)s, %(evidence_ids)s, %(match_ids)s, %(metric_ids)s)"""
INSERT_TREND = """
INSERT INTO synthesis_trends (report_id, ordinal, kind, metric, concept, unit, year, from_year, to_year, old_value,
    new_value, value, growth_pct, formula, source_ids)
VALUES (%(report_id)s, %(ordinal)s, %(kind)s, %(metric)s, %(concept)s, %(unit)s, %(year)s, %(from_year)s,
    %(to_year)s, %(old_value)s, %(new_value)s, %(value)s, %(growth_pct)s, %(formula)s, %(source_ids)s)"""


def store_synthesis_report(path: Path, *, env_file: Path | None = None) -> SynthesisImport:
    """Replace one (company, year span) report and its findings/trends in a single transaction."""
    report, raw = read_report(path)
    head, findings, trends = rows_for(report)
    head.update(source_file=str(path), source_sha256=hashlib.sha256(raw).hexdigest())
    try:
        import psycopg
    except ImportError:
        raise SynthesisStorageError("psycopg is not installed. Run: pip install -r requirements.txt") from None
    try:
        with psycopg.connect(**connection_arguments(env_file)) as conn, conn.cursor() as cur:
            apply_outdated(cur)
            cur.execute("DELETE FROM synthesis_reports WHERE company = %s AND start_year = %s AND end_year = %s",
                        (head["company"], head["start_year"], head["end_year"]))
            cur.execute(INSERT_REPORT, head)
            report_id = cur.fetchone()[0]
            if findings:
                cur.executemany(INSERT_FINDING, [{**f, "report_id": report_id} for f in findings])
            if trends:
                cur.executemany(INSERT_TREND, [{**t, "report_id": report_id} for t in trends])
    except Exception as error:
        raise SynthesisStorageError(
            f"PostgreSQL synthesis import failed ({type(error).__name__}). Check that the database is running "
            "and POSTGRES_* or DATABASE_URL settings are correct.") from None
    return SynthesisImport(head["company"], head["start_year"], head["end_year"], report_id, len(findings),
                           len(trends), head["valid"])


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
            print(f"Done. {r.company} {r.start_year}-{r.end_year} (report_id {r.report_id}): {r.findings} findings, "
                  f"{r.trends} financial trends" + ("" if r.valid is not False else ", agent validation FAILED") + ".")
        except (OSError, ValueError, KeyError, TypeError, SynthesisStorageError) as error:
            print(f"Synthesis import error ({path}): {error}", file=sys.stderr)
            status = 1
    return status
