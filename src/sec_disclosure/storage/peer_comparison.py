"""Require PostgreSQL peer inputs and persist comparison reports atomically."""

from __future__ import annotations

import json
from sec_disclosure.storage.artifacts import ArtifactPath as Path

import psycopg
from psycopg.rows import dict_row

from .disclosures import connection_arguments
from .exports import DownstreamExportError, _alignment_reports, _disclosure_reports


ROOT = Path(__file__).resolve().parents[3]


class PeerInputError(ValueError):
    """A requested ticker/year is unavailable for peer comparison."""


class PeerComparisonStorageError(RuntimeError):
    """A peer report could not be committed to PostgreSQL."""


def read_peer_inputs(ticker: str, peers: list[str], year: int, *, env_file=None) -> dict:
    """Check every company before reading evidence in one consistent transaction.

    A raw filing alone does not qualify: each requested company must have stored
    disclosures for the same fiscal year. No local-file fallback is permitted.
    """
    tickers = [ticker.lower(), *(peer.lower() for peer in peers)]
    try:
        with psycopg.connect(**connection_arguments(env_file), row_factory=dict_row) as conn:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT f.company, count(d.disclosure_id) AS disclosure_count
                    FROM filings f LEFT JOIN disclosures d ON d.filing_id = f.filing_id
                    WHERE lower(f.company) = ANY(%s) AND f.fiscal_year = %s AND f.form_type = '10-K'
                    GROUP BY f.company
                """, (tickers, year))
                available = {}
                for row in cur.fetchall():
                    key = row["company"].lower()
                    if key in available:
                        raise PeerInputError(f"Multiple PostgreSQL company labels match {key.upper()}; resolve them before comparison.")
                    available[key] = row
                missing = [name.upper() for name in tickers if name not in available]
                empty = [name.upper() for name in tickers
                         if name in available and not available[name]["disclosure_count"]]
                problems = []
                if missing:
                    problems.append(f"no PostgreSQL 10-K filing for {', '.join(missing)} in {year}")
                if empty:
                    problems.append(f"no PostgreSQL disclosures for {', '.join(empty)} in {year}")
                if problems:
                    raise PeerInputError("Peer comparison blocked: " + "; ".join(problems)
                                         + ". Run scripts/run_full_pipeline.py for each missing ticker/year first.")

                disclosures = {name: _disclosure_reports(cur, available[name]["company"], year)
                               for name in tickers}
                cur.execute("""
                    SELECT company, previous_year, current_year FROM alignment_runs
                    WHERE lower(company) = ANY(%s) AND run_complete = TRUE
                      AND ((previous_year = %s AND current_year = %s)
                           OR (previous_year = %s AND current_year = %s))
                    ORDER BY company, previous_year, current_year
                """, (tickers, year - 1, year, year, year + 1))
                windows = cur.fetchall()
                alignments = {name: {} for name in tickers}
                for window in windows:
                    company, previous, current = (window["company"], window["previous_year"], window["current_year"])
                    reports = _alignment_reports(cur, company, previous, current)
                    alignments[company.lower()].update({(str(previous), str(current), filename): report
                                                       for filename, report in reports.items()})
                return {"disclosures": disclosures, "alignments": alignments}
    except PeerInputError:
        raise
    except DownstreamExportError as error:
        raise PeerInputError(str(error)) from None
    except psycopg.Error:
        raise PeerInputError("Cannot read peer inputs from PostgreSQL. Check the database connection and apply db/init schemas. "
                             "Both tickers must have stored disclosures; local JSON is not used as a fallback.") from None


def store_peer_report(report: dict, *, env_file=None) -> None:
    """Upsert the report and usage; connection exit must commit before success."""
    target = report["target"].upper()
    peers = sorted(peer.upper() for peer in report["peers"])
    items = sorted(set(report["items"]))
    try:
        with psycopg.connect(**connection_arguments(env_file)) as conn:
            with conn.cursor() as cur:
                cur.execute((ROOT / "db/init/07_peer_comparison_reports.sql").read_text(encoding="utf-8"))
                cur.execute("""
                    INSERT INTO peer_comparison_reports
                        (target_ticker, fiscal_year, peer_tickers, items, target_selection, run_complete, report)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (target_ticker, fiscal_year, peer_tickers, items, target_selection)
                    DO UPDATE SET run_complete = EXCLUDED.run_complete,
                        report = EXCLUDED.report, saved_at = now()
                """, (target, int(report["fiscal_year"]), peers, items, report["target_selection"],
                      report["run_complete"], json.dumps(report, ensure_ascii=False)))
    except psycopg.Error:
        raise PeerComparisonStorageError("Could not commit peer comparison to PostgreSQL. Check the connection and "
                                         "peer_comparison_reports permissions; cached comparison jobs are retained for retry.") from None
