"""Loaders and flatteners for the synthesis agent and peer comparison agent outputs.

Both agents are still being finished by teammates, so this module is the only place that knows
their JSON layout. Missing files and unexpected layouts raise DataError with a plain message.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from . import config, data as ui_data
from .data import DataError


# --------------------------------------------------------------------------- synthesis


def list_synthesis_files() -> list:
    """Synthesis output JSON files found in the configured folders."""
    found = []
    for folder in config.synthesis_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                head = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(head, dict) and "report" in head and "source_catalog" in head:
                found.append(path)
    return found


def load_synthesis(path) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DataError(f"Could not read {path.name}: {error}") from error
    return _normalise_synthesis(data, path.name)


def _normalise_synthesis(data, name: str) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("report"), dict):
        raise DataError(f"{name} does not look like a synthesis output (no 'report' section).")
    data.setdefault("source_catalog", {})
    data.setdefault("financial_trends", [])
    data.setdefault("financial_metrics", [])
    data.setdefault("validation", {})
    data.setdefault("input_summary", {})
    data.setdefault("limitations", [])
    data["report"].setdefault("key_findings", [])
    return data


def _synthesis_query(sql: str, parameters=()) -> list:
    try:
        with ui_data._postgres_connection() as conn, conn.cursor() as cursor:
            conn.read_only = True
            cursor.execute("SELECT to_regclass(%s)", ("synthesis_reports",))
            if cursor.fetchone()[0] is None:
                return []
            cursor.execute(sql, parameters)
            return cursor.fetchall()
    except DataError:
        raise
    except Exception:
        raise DataError("Could not read synthesis reports from PostgreSQL. Check the database connection and schema.") from None


def list_synthesis_reports() -> list[dict]:
    """Database report metadata, newest first, without fetching every report body."""
    rows = _synthesis_query(
        "SELECT company, years, generated_at FROM synthesis_reports "
        "ORDER BY generated_at DESC, company, years"
    )
    return [dict(company=company, years=years, generated_at=generated_at)
            for company, years, generated_at in rows]


def load_synthesis_report(company: str, years: list[int]) -> dict:
    """Read the same complete output that the synthesis backend saves."""
    rows = _synthesis_query(
        "SELECT report FROM synthesis_reports WHERE company=%s AND years=%s::integer[]",
        (company.upper(), list(years)),
    )
    if not rows:
        raise DataError("The selected synthesis report is no longer available. Reload saved reports.")
    return _normalise_synthesis(rows[0][0], f"{company.upper()} synthesis report")


def synthesis_label(path, data=None) -> str:
    if data is None:
        try:
            data = load_synthesis(path)
        except DataError:
            return Path(path).name
    years = data.get("years") or []
    span = f"{min(years)}-{max(years)}" if years else "years not stated"
    return f"{data.get('company', Path(path).stem)} {span}"


def evidence_for(data: dict, evidence_id: str) -> dict | None:
    return data.get("source_catalog", {}).get(evidence_id)


def metric_for(data: dict, evidence_id: str) -> dict | None:
    """Financial (XBRL) evidence IDs point at a reported figure rather than a disclosure."""
    if not str(evidence_id).startswith("xbrl:"):
        return None
    for metric in data.get("financial_metrics", []):
        if metric.get("id") == evidence_id:
            return metric
    return None


def metric_text(metric: dict) -> str:
    when = metric.get("instant_date") or metric.get("period_end_date") or metric.get("year", "")
    return (f"{metric.get('label') or metric.get('concept', '')}: {format_value(metric.get('value'), metric.get('unit', ''))} "
            f"(FY{metric.get('year', '')}, {when}, {metric.get('form_type', '10-K')})")


def synthesis_evidence_rows(data: dict) -> list:
    """One row per cited sentence, linking each key finding to its sources."""
    rows = []
    catalog = data.get("source_catalog", {})
    for number, finding in enumerate(data.get("report", {}).get("key_findings", []), 1):
        for evidence_id in finding.get("evidence_ids", []):
            entry = catalog.get(evidence_id)
            metric = metric_for(data, evidence_id) if not entry else None
            if metric:
                rows.append({"finding_no": number, "finding": finding.get("title", ""), "evidence_id": evidence_id,
                             "company": metric.get("company", ""), "change_label": "XBRL financial figure",
                             "sentence_text": metric_text(metric), "note": "Reported financial figure"})
                continue
            if not entry:
                rows.append({"finding_no": number, "finding": finding.get("title", ""), "evidence_id": evidence_id,
                             "note": "Evidence ID not found in the source catalog"})
                continue
            materiality = entry.get("materiality") or {}
            sources = entry.get("sources") or [{}]
            for source in sources:
                rows.append({
                    "finding_no": number, "finding": finding.get("title", ""), "evidence_id": evidence_id,
                    "company": entry.get("company", ""), "previous_year": entry.get("previous_year", ""),
                    "current_year": entry.get("current_year", ""), "change_label": entry.get("taxonomy", ""),
                    "alignment_status": entry.get("alignment_status", ""), "relationship": entry.get("relationship", ""),
                    "materiality": materiality.get("materiality", ""), "key_change": materiality.get("key_change", ""),
                    "disclosure_id": source.get("disclosure_id", ""), "sentence_id": source.get("sentence_id", ""),
                    "sentence_text": source.get("text", ""), "source_url": source.get("source_url", ""),
                    "note": "",
                })
    return rows


def synthesis_markdown(data: dict) -> str:
    report = data.get("report", {})
    catalog = data.get("source_catalog", {})
    years = data.get("years") or []
    lines = [f"# {data.get('company', 'Company')} disclosure synthesis"]
    if years:
        lines.append(f"Fiscal years {min(years)} to {max(years)}")
    lines += ["", "## Executive summary", "", report.get("executive_summary", "Not provided."), ""]
    findings = report.get("key_findings", [])
    if findings:
        lines += ["## Key findings", ""]
    for number, finding in enumerate(findings, 1):
        lines += [f"### {number}. {finding.get('title', 'Untitled')}", "", finding.get("analysis", ""), ""]
        ids = finding.get("evidence_ids", [])
        if ids:
            lines.append("Evidence:")
        for evidence_id in ids:
            entry = catalog.get(evidence_id) or {}
            metric = metric_for(data, evidence_id)
            if metric:
                lines.append(f"- {evidence_id}: {metric_text(metric)}")
                continue
            lines.append(f"- {evidence_id} ({entry.get('previous_year', '?')} to {entry.get('current_year', '?')}, "
                         f"{entry.get('taxonomy', 'unlabelled')})")
            for source in entry.get("sources", []):
                lines.append(f"  - {source.get('sentence_id', '')}: {source.get('source_url', '')}")
        lines.append("")
    for key, title in (("longitudinal_analysis", "Longitudinal analysis"), ("financial_analysis", "Financial analysis")):
        if report.get(key):
            lines += [f"## {title}", "", report[key], ""]
    limits = list(report.get("limitations") or []) if isinstance(report.get("limitations"), list) else (
        [report["limitations"]] if report.get("limitations") else [])
    limits += [x for x in data.get("limitations", []) if x not in limits]
    if limits:
        lines += ["## Limitations", ""] + [f"- {x}" for x in limits] + [""]
    validation = data.get("validation") or {}
    if validation:
        lines += ["## Validation", "", f"- Structural and citation checks passed: {validation.get('valid')}",
                  f"- {validation.get('note', '')}", ""]
    lines.append("Generated by the synthesis agent. Statements are model-generated and cite the evidence IDs above.")
    return "\n".join(lines)


def format_value(value, unit: str) -> str:
    if value is None:
        return ""
    if unit == "USD" and isinstance(value, (int, float)):
        return f"${value / 1e9:,.2f}B" if abs(value) >= 1e9 else f"${value / 1e6:,.1f}M"
    if isinstance(value, float):
        return f"{value:,.3f}" + (f" {unit}" if unit else "")
    return f"{value}" + (f" {unit}" if unit and unit != "USD" else "")


def trend_rows(data: dict) -> list:
    rows = []
    for trend in data.get("financial_trends", []):
        if trend.get("type") == "year_over_year_change":
            rows.append({"Measure": trend.get("label", trend.get("concept", "")),
                         "Period": f"{trend.get('from_year')} to {trend.get('to_year')}",
                         "From": format_value(trend.get("old_value"), trend.get("unit", "")),
                         "To": format_value(trend.get("new_value"), trend.get("unit", "")),
                         "Change": f"{trend['growth_pct']:+.1f}%" if isinstance(trend.get("growth_pct"), (int, float)) else "",
                         "Sources": "; ".join(trend.get("source_ids", []))})
        else:
            rows.append({"Measure": trend.get("name", trend.get("label", "")), "Period": str(trend.get("year", "")),
                         "From": "", "To": format_value(trend.get("value"), trend.get("unit", "")),
                         "Change": trend.get("formula", ""), "Sources": "; ".join(trend.get("source_ids", []))})
    return rows


# --------------------------------------------------------------------------- peer comparison


def list_peer_files() -> list:
    root = config.peer_dir()
    if not root.is_dir():
        return []
    return sorted({*root.glob("*/*/peer_comparison.json"), *root.glob("*/*/*/peer_comparison.json")})


def load_peer(path) -> dict:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DataError(f"Could not read {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("comparisons"), list):
        raise DataError(f"{path.name} does not look like a peer comparison output (no 'comparisons' list).")
    data.setdefault("peers", [])
    return data


def peer_label(path, data=None) -> str:
    path = Path(path)
    if data is None:
        try:
            data = load_peer(path)
        except DataError:
            return f"{path.parent.parent.name} {path.parent.name}"
    return f"{data.get('target', path.parent.parent.name)} {data.get('fiscal_year', path.parent.name)} vs {', '.join(data.get('peers', []))}"


PEER_STATUS_LABELS = {
    "compared": "Compared",
    "no_candidate_retrieved": "No candidate retrieved",
    "no_comparable_candidate": "Candidates reviewed, none comparable",
}


def _latest_context(contexts) -> str:
    """Change label of the target disclosure in the most recent saved year-over-year window."""
    if isinstance(contexts, dict):
        contexts = [contexts]
    for entry in reversed(contexts or []):
        if not isinstance(entry, dict):
            continue
        label = entry.get("change_taxonomy") or (entry.get("change_analysis") or {}).get("final_taxonomy")
        if label:
            return f"{label} ({entry.get('window', '')})".strip()
    return ""


def peer_rows(data: dict) -> list:
    """One row per (target disclosure, peer comparison or non-comparison)."""
    rows = []
    for job in data.get("comparisons", []):
        context = _latest_context(job.get("target_year_over_year_context"))
        for peer in job.get("peer_results", []):
            base = {"target_ticker": job.get("target_ticker", data.get("target", "")),
                    "fiscal_year": data.get("fiscal_year", ""), "item": job.get("item", ""),
                    "target_disclosure_id": job.get("target_disclosure_id", ""), "target_topic": job.get("target_topic", ""),
                    "peer_ticker": peer.get("peer_ticker", ""), "status": peer.get("status", ""),
                    "status_label": PEER_STATUS_LABELS.get(peer.get("status", ""), peer.get("status", "")),
                    "target_change_label": context}
            comparisons = peer.get("comparisons") or []
            if not comparisons:
                rows.append({**base, "peer_disclosure_id": "", "relationship": "", "comparison": peer.get("rationale", ""),
                             "target_sentence_ids": "", "peer_sentence_ids": "", "source_urls": ""})
            for comp in comparisons:
                target_ids, peer_ids, urls = [], [], []
                for citation in comp.get("evidence", []):
                    for sentence in citation.get("sentences", []):
                        (target_ids if citation.get("side") == "target" else peer_ids).append(sentence.get("sentence_id", ""))
                        if sentence.get("source_url"):
                            urls.append(sentence["source_url"])
                rows.append({**base, "peer_disclosure_id": comp.get("peer_disclosure_id", ""),
                             "relationship": comp.get("relationship", ""), "comparison": comp.get("comparison", ""),
                             "target_sentence_ids": "; ".join(target_ids), "peer_sentence_ids": "; ".join(peer_ids),
                             "source_urls": "; ".join(dict.fromkeys(urls))})
    return rows


def peer_markdown(data: dict) -> str:
    lines = [f"# Peer comparison: {data.get('target', '')} {data.get('fiscal_year', '')}",
             f"Peers: {', '.join(data.get('peers', []))}", ""]
    if not data.get("run_complete", True):
        lines += [f"Partial run: {data.get('jobs_completed', '?')} of {data.get('jobs_planned', '?')} disclosures compared.", ""]
    for job in data.get("comparisons", []):
        lines += [f"## Item {job.get('item', '')}: {job.get('target_topic') or job.get('target_disclosure_id', '')}", "",
                  f"Target disclosure: {job.get('target_disclosure_id', '')}", ""]
        for peer in job.get("peer_results", []):
            label = PEER_STATUS_LABELS.get(peer.get("status", ""), peer.get("status", ""))
            lines.append(f"### {peer.get('peer_ticker', '')} ({label})")
            if peer.get("rationale"):
                lines.append(peer["rationale"])
            for comp in peer.get("comparisons") or []:
                lines += ["", f"{comp.get('comparison', '')}", "", f"- Relationship: {comp.get('relationship', '')}",
                          f"- Peer disclosure: {comp.get('peer_disclosure_id', '')}"]
                for citation in comp.get("evidence", []):
                    for sentence in citation.get("sentences", []):
                        lines.append(f"- {citation.get('side', '')} sentence {sentence.get('sentence_id', '')}: {sentence.get('source_url', '')}")
            lines.append("")
    lines.append("Peer statements are model-generated analysis, not human-verified conclusions. "
                 "'No candidate retrieved' is not evidence that a peer does not discuss the topic.")
    return "\n".join(lines)


def rows_to_csv(rows: list) -> str:
    if not rows:
        return ""
    fields = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
