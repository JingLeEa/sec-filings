"""Persistence for complete synthesis reports."""

from __future__ import annotations

from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[3]


class SynthesisStorageError(RuntimeError):
    """A synthesis report could not be saved to PostgreSQL."""


def save_synthesis_report(conn, company: str, years: list[int], report_json: str) -> None:
    """Create the report table when needed and atomically upsert the latest report."""
    try:
        with conn.cursor() as cur:
            cur.execute((ROOT / "db" / "init" / "06_synthesis_reports.sql").read_text(
                encoding="utf-8"
            ))
            cur.execute(
                """
                INSERT INTO synthesis_reports (company, years, report)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (company, years) DO UPDATE SET
                    report = EXCLUDED.report,
                    generated_at = now()
                """,
                (company.upper(), years, report_json),
            )
    except psycopg.Error as error:
        raise SynthesisStorageError(
            "Could not save synthesis report to PostgreSQL; check that db/init/06_synthesis_reports.sql "
            "has been applied and the database user can write to synthesis_reports."
        ) from error
