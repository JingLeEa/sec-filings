"""Versioned, idempotent schema files for the chatbot's read model and the agent-output tables.

Files in db/init/ only run automatically when the Docker volume is first created. Each file below ends by
recording its version in schema_info; when the recorded version differs (or is missing), the file is run
again. Every statement in them is safe to repeat (IF NOT EXISTS / OR REPLACE / ADD COLUMN IF NOT EXISTS).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
INIT = ROOT / "db" / "init"
SCHEMAS: list[tuple[Path, str, str]] = [
    (INIT / "06_chatbot.sql", "chatbot_schema_version", "1"),
    (INIT / "07_peer_comparisons.sql", "peer_comparison_schema_version", "2"),
    (INIT / "08_synthesis_reports.sql", "synthesis_schema_version", "1"),
]


def outdated(versions: dict[str, str]) -> list[Path]:
    """Schema files whose recorded version is missing or different."""
    return [path for path, key, version in SCHEMAS if versions.get(key) != version]


def read_versions(cur) -> dict[str, str]:
    cur.execute("SELECT key, value FROM schema_info WHERE key = ANY(%s)", ([key for _, key, _ in SCHEMAS],))
    return {row[0] if isinstance(row, tuple) else row["key"]: row[1] if isinstance(row, tuple) else row["value"]
            for row in cur.fetchall()}


def apply_outdated(cur) -> list[str]:
    """Run every outdated schema file on this cursor (inside the caller's transaction); returns their names."""
    applied = []
    for path in outdated(read_versions(cur)):
        cur.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return applied
