#!/usr/bin/env python3
"""Embed each alignment's evidence text (bge-m3 via SoCLaaS) into chatbot_evidence_embeddings for pgvector search.

The chatbot works without this (it falls back to PostgreSQL full-text search); running it adds semantic
search for topic questions such as "what changed about climate risk". One vector per alignment side:
the original evidence sentences, or the disclosure summary when a side has no evidence text.

Needs SOCLAAS_BASE_URL / SOCLAAS_API_KEY / SOCLAAS_MODEL (env or ~/.config/soclaas/soclaas.env). The key is
read from the environment only and never printed. Vectors are cached under data/embeddings, so re-running
after a reload makes no new API calls for unchanged text.

    python scripts/embed_alignment_evidence.py                 # every loaded run
    python scripts/embed_alignment_evidence.py --company bac   # one company
    python scripts/embed_alignment_evidence.py --dry-run       # count texts, no API calls
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.chatbot.db import Database, DatabaseError  # noqa: E402
from sec_disclosure.chatbot.retrieval import vector_literal  # noqa: E402

MAX_CHARS = 6000
UPSERT = """
INSERT INTO chatbot_evidence_embeddings (run_id, match_id, side, model_id, text_hash, embedding)
VALUES (%s, %s, %s, %s, %s, %s::vector)
ON CONFLICT (run_id, match_id, side, model_id)
DO UPDATE SET text_hash = EXCLUDED.text_hash, embedding = EXCLUDED.embedding, created_at = now()
"""


def side_texts(db: Database, company: str | None) -> list[tuple[int, str, str, str]]:
    rows = db.query("evidence_texts", """
        SELECT run_id, match_id, coalesce(previous_text, previous_summary) AS previous,
               coalesce(current_text, current_summary) AS current
        FROM alignment_changes WHERE (%(company)s::text IS NULL OR company = %(company)s)
        ORDER BY run_id, match_id""", {"company": company}).rows
    out = []
    for row in rows:
        for side in ("previous", "current"):
            text = (row[side] or "").strip()
            if text:
                out.append((row["run_id"], row["match_id"], side, text[:MAX_CHARS]))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company")
    parser.add_argument("--model", default="bge-m3", help="Embedding model (must exist in embedding_models)")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        db = Database(args.env_file)
        db.ensure_schema()
        texts = side_texts(db, args.company.lower() if args.company else None)
        model = db.query("model", "SELECT model_id, dimension FROM embedding_models WHERE name = %s", (args.model,)).rows
    except DatabaseError as error:
        print(f"Database error: {error}", file=sys.stderr)
        return 1
    if not model:
        print(f"Model {args.model!r} is not in embedding_models.", file=sys.stderr)
        return 1
    existing = {(r["run_id"], r["match_id"], r["side"]): r["text_hash"] for r in db.query(
        "existing", "SELECT run_id, match_id, side, text_hash FROM chatbot_evidence_embeddings WHERE model_id = %s",
        (model[0]["model_id"],)).rows}
    todo = [(r, m, s, t, hashlib.sha256(t.encode()).hexdigest()) for r, m, s, t in texts]
    todo = [row for row in todo if existing.get(row[:3]) != row[4]]
    print(f"{len(texts)} evidence texts; {len(todo)} need embedding.")
    if args.dry_run or not todo:
        return 0

    from sec_disclosure.chatbot.config import load_embedding_config
    from sec_disclosure.llm.client import LLMError
    from sec_disclosure.llm.embeddings import EmbeddingCache, make_embedder
    config = load_embedding_config()
    if config is None:
        print("SoCLaaS settings are missing (SOCLAAS_BASE_URL, SOCLAAS_API_KEY, SOCLAAS_MODEL).", file=sys.stderr)
        return 1
    embed = make_embedder(config=config, model=args.model, batch_size=args.batch_size,
                          cache=EmbeddingCache(ROOT / "data" / "embeddings"), progress=True)
    try:
        vectors = embed([row[3] for row in todo])
    except LLMError as error:
        print(f"Embedding failed: {error}", file=sys.stderr)
        return 1
    dimension = model[0]["dimension"]
    bad = [i for i, v in enumerate(vectors) if len(v) != dimension]
    if bad:
        print(f"{len(bad)} vectors have the wrong dimension (expected {dimension}); nothing written.", file=sys.stderr)
        return 1
    import psycopg
    from sec_disclosure.storage.disclosures import connection_arguments
    with psycopg.connect(**connection_arguments(args.env_file)) as conn, conn.cursor() as cur:
        cur.executemany(UPSERT, [(r, m, s, model[0]["model_id"], h, vector_literal(v))
                                 for (r, m, s, _, h), v in zip(todo, vectors)])
    print(f"Stored {len(todo)} vectors in chatbot_evidence_embeddings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
