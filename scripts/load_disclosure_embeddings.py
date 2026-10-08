#!/usr/bin/env python3
"""Copy cached disclosure embeddings into Postgres (disclosure_embeddings table).

The semantic-retrieval stage embeds three texts per disclosure (content, summary, section)
and saves each vector in an on-disk cache, one JSON file per (model, text) named
sha256("<model>\\0<text>"). This script reads the disclosures already loaded into the database,
looks each text up in that cache and stores the vector. It makes no API calls and does not
create embeddings; a disclosure whose text is not in the cache is simply skipped.

Run load_disclosures_to_postgres.py first. Example (database running, from the repo root):

    python scripts/load_disclosure_embeddings.py --company amd --year 2025
    python scripts/load_disclosure_embeddings.py --company amd --year 2025 --embeddings-cache data/embeddings --model bge-m3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from load_to_postgres import ROOT, build_dsn, embedding_cache_key, vector_literal  # noqa: E402

FIELDS = ("content", "summary", "section")

UPSERT = """
INSERT INTO disclosure_embeddings (disclosure_id, model_id, field, embedding)
VALUES (%s, %s, %s, %s::vector)
ON CONFLICT (disclosure_id, model_id, field) DO UPDATE SET embedding = EXCLUDED.embedding, created_at = now()
"""


def field_texts(row: dict, fields=FIELDS) -> dict[str, str]:
    """Texts that were embedded for one disclosure. Empty texts are never embedded."""
    return {f: row[f] for f in fields if row.get(f) and str(row[f]).strip()}


def find_vector(cache_dir: Path, model: str, text: str) -> list[float] | None:
    path = cache_dir / f"{embedding_cache_key(model, text)}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--embeddings-cache", default=str(ROOT / "data" / "embeddings"))
    parser.add_argument("--model", default="bge-m3", help="Model name used for the cache keys (must exist in embedding_models)")
    parser.add_argument("--fields", nargs="+", choices=FIELDS, default=list(FIELDS))
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass
    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    cache_dir = Path(args.embeddings_cache)
    if not cache_dir.is_dir():
        print(f"Embedding cache folder not found: {cache_dir}", file=sys.stderr)
        return 1

    with psycopg.connect(build_dsn()) as conn, conn.cursor() as cur:
        cur.execute("SELECT model_id, dimension FROM embedding_models WHERE name = %s", (args.model,))
        found = cur.fetchone()
        if not found:
            print(f"Model '{args.model}' is not in embedding_models. Add it first (name + dimension).", file=sys.stderr)
            return 1
        model_id, dimension = found
        cur.execute(
            """SELECT d.disclosure_id, d.content, d.summary, d.section
               FROM disclosures d JOIN filings f USING (filing_id)
               WHERE f.company = %s AND f.fiscal_year = %s ORDER BY d.disclosure_id""",
            (args.company, args.year))
        disclosures = [dict(zip(("disclosure_id", "content", "summary", "section"), r)) for r in cur.fetchall()]
        if not disclosures:
            print(f"No disclosures for {args.company} {args.year} in the database. "
                  "Run load_disclosures_to_postgres.py first.", file=sys.stderr)
            return 1

        loaded = {f: 0 for f in args.fields}
        missing = {f: 0 for f in args.fields}
        batch = []
        for row in disclosures:
            for field, text in field_texts(row, args.fields).items():
                vector = find_vector(cache_dir, args.model, text)
                if vector is None:
                    missing[field] += 1
                    continue
                if len(vector) != dimension:
                    print(f"Vector for {row['disclosure_id']} ({field}) has {len(vector)} dimensions but model "
                          f"'{args.model}' is registered with {dimension}.", file=sys.stderr)
                    return 1
                batch.append((row["disclosure_id"], model_id, field, vector_literal(vector)))
                loaded[field] += 1
        cur.executemany(UPSERT, batch)

    print(f"{args.company} {args.year}: {len(disclosures)} disclosures, model {args.model}")
    for field in args.fields:
        print(f"  {field:8s} {loaded[field]} loaded, {missing[field]} not in the cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
