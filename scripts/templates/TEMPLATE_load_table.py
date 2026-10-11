#!/usr/bin/env python3
"""TEMPLATE: load one JSON file (a list of records) into a Postgres table.

How to use it:
  1. Copy this file to scripts/load_<your_stage>_to_postgres.py
  2. Change TABLE, KNOWN_FIELDS and record_row() so they match your JSON and your table
     (the table itself is created by your db/init/*.sql file, see db/templates/TEMPLATE_new_table.sql).
  3. Run it:  python scripts/load_<your_stage>_to_postgres.py --file path/to/your.json --dry-run
              python scripts/load_<your_stage>_to_postgres.py --file path/to/your.json

It works as written for the example_items table, so you can try it first with a small JSON like:
  [{"item_id": "a1", "name": "first", "score": 0.5, "tags": ["x"]}]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Shared connection settings (reads .env / POSTGRES_* the same way as the other loaders).
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS if (SCRIPTS / "load_to_postgres.py").exists() else SCRIPTS.parent))
from load_to_postgres import ROOT, build_dsn  # noqa: E402

TABLE = "example_items"                       # CHANGE: your table name
KNOWN_FIELDS = {"item_id", "name", "score", "tags"}   # CHANGE: JSON fields that have their own column

UPSERT = """
INSERT INTO example_items (item_id, name, score, tags, extra)
VALUES (%(item_id)s, %(name)s, %(score)s, %(tags)s, %(extra)s::jsonb)
ON CONFLICT (item_id) DO UPDATE SET
    name = EXCLUDED.name, score = EXCLUDED.score, tags = EXCLUDED.tags, extra = EXCLUDED.extra
"""                                           # CHANGE: columns and the key after ON CONFLICT


def record_row(record: dict) -> dict:
    """Turn one JSON record into the values for one table row. CHANGE to match your fields."""
    return {
        "item_id": record["item_id"],
        "name": record["name"],
        "score": record.get("score"),                 # .get gives None (NULL) when the field is missing
        "tags": list(record.get("tags") or []),
        # Fields you did not map are kept here instead of being lost.
        "extra": json.dumps({k: v for k, v in record.items() if k not in KNOWN_FIELDS}, ensure_ascii=False),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--file", required=True, help="JSON file containing a list of records")
    parser.add_argument("--dry-run", action="store_true", help="Check the file only; do not touch the database")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    records = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rows = [record_row(r) for r in records]               # fails here, before the database, if a field is missing
    print(f"Read {len(rows)} records from {args.file}")
    if args.dry_run:
        print("Dry run: file is readable. Nothing written.")
        return 0

    import psycopg
    with psycopg.connect(build_dsn()) as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, rows)                     # insert new rows, update existing ones with the same key
        cur.execute(f"SELECT count(*) FROM {TABLE}")
        print(f"Done. {TABLE} now has {cur.fetchone()[0]} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
