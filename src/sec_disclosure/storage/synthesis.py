"""Persistence for complete synthesis reports."""

from __future__ import annotations

from contextlib import contextmanager
from sec_disclosure.storage.artifacts import ArtifactPath as Path

import psycopg

from .disclosures import connection_arguments


ROOT = Path(__file__).resolve().parents[3]


class SynthesisStorageError(RuntimeError):
    """A synthesis report could not be saved to PostgreSQL."""


@contextmanager
def synthesis_connection(env_file: Path | None = None):
    """Treat connection and transaction-commit failures as failed synthesis runs."""
    try:
        with psycopg.connect(**connection_arguments(env_file)) as conn:
            yield conn
    except psycopg.Error:
        raise SynthesisStorageError(
            "Could not read or commit synthesis data in PostgreSQL. Check the database "
            "connection and permissions; saved model responses are retained for retry."
        ) from None


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
