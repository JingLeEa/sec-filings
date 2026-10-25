"""Validate and atomically store peer-comparison agent reports (peer_comparison.json, schema "2") in PostgreSQL.

Partial runs (``run_complete: false``) are accepted and flagged: they hold every target disclosure the agent
finished, and the chatbot says when an answer comes from a partial run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from .disclosures import ROOT, connection_arguments

SUPPORTED_SCHEMA = "2"
STATUSES = {"compared", "no_comparable_candidate", "no_candidate_retrieved"}
RELATIONSHIPS = {"same_topic", "related_topic"}
ITEMS = {"1", "1A", "7", "8", "15"}
SCHEMA_SQL = ROOT / "db" / "init" / "07_peer_comparisons.sql"


class PeerComparisonStorageError(RuntimeError):
    """Database failure with a message safe to print without credentials."""


@dataclass(frozen=True)
class PeerComparisonImport:
    target: str
    fiscal_year: int
    peers: list[str]
    peer_run_id: int
    run_complete: bool
    targets: int
    results: int
    comparisons: int
    evidence: int


def resolve_report(path: Path) -> Path:
    path = Path(path)
    return path / "peer_comparison.json" if path.is_dir() else path


def read_report(path: Path) -> tuple[dict, bytes]:
    """Parse and validate the whole report before any database work."""
    raw = resolve_report(path).read_bytes()
    report = json.loads(raw.decode("utf-8"))
    problems: list[str] = []
    if not isinstance(report, dict):
        raise ValueError("peer_comparison.json must contain a JSON object")
    if str(report.get("schema_version")) != SUPPORTED_SCHEMA:
        problems.append(f"schema_version {report.get('schema_version')!r}, expected {SUPPORTED_SCHEMA!r}")
    target = str(report.get("target", "")).lower()
    peers = sorted({str(p).lower() for p in report.get("peers") or []})
    if not target or not peers or target in peers:
        problems.append("report needs a target and at least one peer different from the target")
    if not str(report.get("fiscal_year", "")).isdigit():
        problems.append("fiscal_year must be a year")
    jobs = report.get("comparisons")
    if not isinstance(jobs, list):
        raise ValueError("; ".join(problems + ["comparisons must be an array"]))
    if report.get("jobs_completed") != len(jobs):
        problems.append("jobs_completed does not match the number of saved comparisons")
    year = str(report.get("fiscal_year"))
    seen_targets: set[str] = set()
    for job in jobs:
        tid = job.get("target_disclosure_id") if isinstance(job, dict) else None
        if not isinstance(tid, str) or not tid.startswith(f"{target}_{year}_") or tid in seen_targets:
            problems.append(f"invalid or duplicate target_disclosure_id {tid!r}")
            continue
        seen_targets.add(tid)
        if str(job.get("item")) not in ITEMS:
            problems.append(f"{tid}: invalid item {job.get('item')!r}")
        results = job.get("peer_results")
        if not isinstance(results, list) or {str(r.get("peer_ticker", "")).lower() for r in results} != set(peers):
            problems.append(f"{tid}: peer_results must cover every peer exactly")
            continue
        for result in results:
            peer = str(result.get("peer_ticker", "")).lower()
            if result.get("status") not in STATUSES:
                problems.append(f"{tid}/{peer}: unknown status {result.get('status')!r}")
            comparisons = result.get("comparisons") or []
            if (result.get("status") == "compared") != bool(comparisons):
                problems.append(f"{tid}/{peer}: status and comparisons disagree")
            for comparison in comparisons:
                pid = comparison.get("peer_disclosure_id")
                if not isinstance(pid, str) or not pid.startswith(f"{peer}_{year}_"):
                    problems.append(f"{tid}/{peer}: peer_disclosure_id {pid!r} is not a {peer} {year} disclosure")
                if comparison.get("relationship") not in RELATIONSHIPS:
                    problems.append(f"{tid}/{peer}: invalid relationship")
                if not str(comparison.get("comparison") or "").strip():
                    problems.append(f"{tid}/{peer}: empty comparison text")
                sides = {e.get("side") for e in comparison.get("evidence") or []}
                if sides != {"target", "peer"}:
                    problems.append(f"{tid}/{peer}: evidence must cite both the target and the peer")
    if problems:
        raise ValueError("; ".join(problems[:10]) + (f" (+{len(problems) - 10} more)" if len(problems) > 10 else ""))
    return report, raw


def rows_for(report: dict) -> tuple[list[dict], list[dict], list[tuple[dict, list[dict]]]]:
    targets, results, comparisons = [], [], []
    for job in report["comparisons"]:
        tid = job["target_disclosure_id"]
        targets.append({"target_disclosure_id": tid, "item": str(job["item"]), "target_topic": job.get("target_topic"),
                        "target_summary": job.get("target_summary"),
                        "target_source_status": job.get("target_source_status"),
                        "year_over_year": json.dumps(job.get("target_year_over_year_context") or [], ensure_ascii=False)})
        for result in job["peer_results"]:
            peer = result["peer_ticker"].lower()
            results.append({"target_disclosure_id": tid, "peer": peer, "status": result["status"],
                            "rationale": result.get("rationale") or None,
                            "agent_warnings": [str(w) for w in result.get("agent_warnings") or []]})
            for comparison in result.get("comparisons") or []:
                evidence = []
                for block in comparison["evidence"]:
                    for ordinal, sentence in enumerate(block.get("sentences") or [], start=1):
                        evidence.append({"side": block["side"], "ticker": str(block.get("ticker", "")).lower(),
                                         "disclosure_id": block["disclosure_id"], "ordinal": ordinal,
                                         "sentence_id": sentence["sentence_id"],
                                         "paragraph_id": sentence.get("paragraph_id"),
                                         "source_url": sentence.get("source_url"), "text": sentence["text"],
                                         "visible_in_prompt": sentence.get("visible_in_prompt")})
                comparisons.append(({"target_disclosure_id": tid, "peer": peer,
                                     "peer_disclosure_id": comparison["peer_disclosure_id"],
                                     "relationship": comparison["relationship"], "comparison": comparison["comparison"],
                                     "peer_topic": comparison.get("peer_topic"),
                                     "peer_summary": comparison.get("peer_summary"),
                                     "peer_taxonomy": comparison.get("peer_taxonomy"),
                                     "peer_source_status": comparison.get("peer_source_status"),
                                     "retrieval_score": comparison.get("retrieval_score")}, evidence))
    return targets, results, comparisons


INSERT_TARGET = """
INSERT INTO peer_comparison_targets (peer_run_id, target_disclosure_id, item, target_topic, target_summary,
                                     target_source_status, year_over_year)
VALUES (%(peer_run_id)s, %(target_disclosure_id)s, %(item)s, %(target_topic)s, %(target_summary)s,
        %(target_source_status)s, %(year_over_year)s::jsonb)"""
INSERT_RESULT = """
INSERT INTO peer_comparison_results (peer_run_id, target_disclosure_id, peer, status, rationale, agent_warnings)
VALUES (%(peer_run_id)s, %(target_disclosure_id)s, %(peer)s, %(status)s, %(rationale)s, %(agent_warnings)s)"""
INSERT_COMPARISON = """
INSERT INTO peer_comparisons (peer_run_id, target_disclosure_id, peer, peer_disclosure_id, relationship, comparison,
                              peer_topic, peer_summary, peer_taxonomy, peer_source_status, retrieval_score)
VALUES (%(peer_run_id)s, %(target_disclosure_id)s, %(peer)s, %(peer_disclosure_id)s, %(relationship)s,
        %(comparison)s, %(peer_topic)s, %(peer_summary)s, %(peer_taxonomy)s, %(peer_source_status)s,
        %(retrieval_score)s)
RETURNING comparison_id"""
INSERT_EVIDENCE = """
INSERT INTO peer_comparison_evidence (comparison_id, side, ticker, disclosure_id, ordinal, sentence_id, paragraph_id,
                                      source_url, text, visible_in_prompt)
VALUES (%(comparison_id)s, %(side)s, %(ticker)s, %(disclosure_id)s, %(ordinal)s, %(sentence_id)s, %(paragraph_id)s,
        %(source_url)s, %(text)s, %(visible_in_prompt)s)
ON CONFLICT DO NOTHING"""


def store_peer_comparison(path: Path, *, env_file: Path | None = None) -> PeerComparisonImport:
    """Replace one (target, year, peer set) run and all its rows in a single transaction."""
    report, raw = read_report(path)
    targets, results, comparisons = rows_for(report)
    target, year = report["target"].lower(), int(report["fiscal_year"])
    peers = sorted({p.lower() for p in report["peers"]})
    policies = {k: report[k] for k in ("candidate_policy", "alignment_context_policy", "agent_policy") if k in report}
    try:
        import psycopg
    except ImportError:
        raise PeerComparisonStorageError("psycopg is not installed. Run: pip install -r requirements.txt") from None
    try:
        with psycopg.connect(**connection_arguments(env_file)) as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.peer_comparison_pairs')")
            if cur.fetchone()[0] is None:
                cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
            cur.execute("DELETE FROM peer_comparison_runs WHERE target = %s AND fiscal_year = %s AND peers = %s",
                        (target, year, peers))
            cur.execute("""
                INSERT INTO peer_comparison_runs (target, fiscal_year, peers, items, schema_version, run_complete,
                    jobs_completed, jobs_planned, policies, token_usage, source_file, source_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s) RETURNING peer_run_id""",
                        (target, year, peers, [str(i) for i in report.get("items") or []], str(report["schema_version"]),
                         bool(report.get("run_complete")), int(report["jobs_completed"]),
                         int(report.get("jobs_planned") or report["jobs_completed"]), json.dumps(policies),
                         json.dumps(report.get("token_usage") or {}), str(resolve_report(path)),
                         hashlib.sha256(raw).hexdigest()))
            run_id = cur.fetchone()[0]
            for row in targets:
                row["peer_run_id"] = run_id
            for row in results:
                row["peer_run_id"] = run_id
            if targets:
                cur.executemany(INSERT_TARGET, targets)
            if results:
                cur.executemany(INSERT_RESULT, results)
            evidence_count = 0
            for comparison, evidence in comparisons:
                cur.execute(INSERT_COMPARISON, {**comparison, "peer_run_id": run_id})
                comparison_id = cur.fetchone()[0]
                if evidence:
                    cur.executemany(INSERT_EVIDENCE, [{**e, "comparison_id": comparison_id} for e in evidence])
                    evidence_count += len(evidence)
    except Exception as error:
        raise PeerComparisonStorageError(
            f"PostgreSQL peer-comparison import failed ({type(error).__name__}). Check that the database is running "
            "and POSTGRES_* or DATABASE_URL settings are correct.") from None
    return PeerComparisonImport(target, year, peers, run_id, bool(report.get("run_complete")), len(targets),
                                len(results), len(comparisons), evidence_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path,
                        help="peer_comparison.json files or their folders, e.g. data/peer_comparison/nvda/2024")
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--dry-run", action="store_true", help="Validate reports without connecting to PostgreSQL")
    args = parser.parse_args(argv)
    status = 0
    for path in args.paths:
        try:
            if args.dry_run:
                report, _ = read_report(path)
                print(f"Dry run OK: {resolve_report(path)} ({len(report['comparisons'])} target disclosures).")
                continue
            r = store_peer_comparison(path, env_file=args.env_file)
            print(f"Done. {r.target} {r.fiscal_year} vs {', '.join(r.peers)} (peer_run_id {r.peer_run_id}, "
                  f"{'complete' if r.run_complete else 'PARTIAL'}): {r.targets} targets, {r.results} peer results, "
                  f"{r.comparisons} comparisons, {r.evidence} cited sentences.")
        except (OSError, ValueError, KeyError, TypeError, PeerComparisonStorageError) as error:
            print(f"Peer comparison import error ({path}): {error}", file=sys.stderr)
            status = 1
    return status
