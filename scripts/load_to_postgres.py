#!/usr/bin/env python3
"""Load extracted chunks and sentences (and optional embeddings) into Postgres.

Reads the preprocessing output for one company and fiscal year:

    data/raw/<company>/<year>/<year>_chunks.json
    data/raw/<company>/<year>/<year>_chunk_sentences.json

and writes it to the tables defined in db/init/01_schema.sql.

Safe to rerun: rows are keyed by the pipeline's own chunk and sentence IDs, so a
second run updates the same rows instead of duplicating them.

Examples (run from the repository root, with the database running):

    python scripts/load_to_postgres.py --company unh --year 2025
    python scripts/load_to_postgres.py --company unh --year 2025 --replace
    python scripts/load_to_postgres.py --company unh --year 2025 \
        --embeddings-cache data/embeddings --embedding-model bge-m3

No network access to SEC or any LLM is needed; this only talks to the local database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]

CHUNK_KNOWN = {
    "id", "company", "year", "item", "item_default_title", "item_title",
    "section_path", "item_chunk_index", "text", "html_list_depth", "source",
}
SENTENCE_KNOWN = {
    "id", "chunk_id", "company", "year", "item", "item_default_title", "item_title",
    "section_path", "item_chunk_index", "sentence_index", "bullet_level",
    "bullet_indent_pt", "html_list_depth", "source_block_index", "text", "source",
}


# --------------------------------------------------------------------------- #
# Pure helpers (no database needed; covered by tests/test_load_to_postgres.py)
# --------------------------------------------------------------------------- #

def _none_if_empty(value: Any) -> Any:
    return None if value in ("", None) else value


def _extra(record: dict, known: set[str]) -> str:
    return json.dumps({k: v for k, v in record.items() if k not in known}, ensure_ascii=False)


def chunk_row(record: dict, filing_id: int) -> dict:
    return {
        "chunk_id": record["id"],
        "filing_id": filing_id,
        "item": str(record["item"]),
        "item_default_title": record.get("item_default_title"),
        "item_title": record.get("item_title"),
        "section_path": list(record.get("section_path") or []),
        "item_chunk_index": int(record["item_chunk_index"]),
        "text": record["text"],
        "html_list_depth": _none_if_empty(record.get("html_list_depth")),
        "source": record.get("source"),
        "extra": _extra(record, CHUNK_KNOWN),
    }


def sentence_row(record: dict, filing_id: int) -> dict:
    return {
        "sentence_id": record["id"],
        "chunk_id": record["chunk_id"],
        "filing_id": filing_id,
        "item": str(record["item"]),
        "item_title": record.get("item_title"),
        "section_path": list(record.get("section_path") or []),
        "sentence_index": int(record["sentence_index"]),
        "text": record["text"],
        "bullet_level": _none_if_empty(record.get("bullet_level")),
        "bullet_indent_pt": _none_if_empty(record.get("bullet_indent_pt")),
        "html_list_depth": _none_if_empty(record.get("html_list_depth")),
        "source_block_index": _none_if_empty(
            None if record.get("source_block_index") is None else str(record.get("source_block_index"))
        ),
        "source": record.get("source"),
        "extra": _extra(record, SENTENCE_KNOWN),
    }


def validate_inputs(chunks: list[dict], sentences: list[dict]) -> list[str]:
    """Return a list of problems; an empty list means the files are consistent."""
    problems: list[str] = []
    chunk_ids = [c["id"] for c in chunks]
    if len(chunk_ids) != len(set(chunk_ids)):
        problems.append("duplicate chunk IDs in chunks file")
    sentence_ids = [s["id"] for s in sentences]
    if len(sentence_ids) != len(set(sentence_ids)):
        problems.append("duplicate sentence IDs in sentences file")
    known = set(chunk_ids)
    orphans = [s["id"] for s in sentences if s["chunk_id"] not in known]
    if orphans:
        problems.append(f"{len(orphans)} sentences point to a chunk that is not in the chunks file (first: {orphans[0]})")
    return problems


def embedding_cache_key(model: str, text: str) -> str:
    """Same key the semantic-retrieval cache uses (llm/embeddings.py on the semantic-retrieval branch)."""
    return hashlib.sha256(f"{model}\u0000{text}".encode("utf-8")).hexdigest()


def vector_literal(vector: Iterable[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def build_dsn() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    user = os.environ.get("POSTGRES_USER", "sec")
    password = os.environ.get("POSTGRES_PASSWORD", "sec_local_pw")
    db = os.environ.get("POSTGRES_DB", "sec_disclosure")
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


# --------------------------------------------------------------------------- #
# Database work
# --------------------------------------------------------------------------- #

UPSERT_FILING = """
INSERT INTO filings (company, fiscal_year, form_type, source)
VALUES (%(company)s, %(fiscal_year)s, %(form_type)s, %(source)s)
ON CONFLICT (company, fiscal_year, form_type) DO UPDATE SET source = EXCLUDED.source, loaded_at = now()
RETURNING filing_id
"""

UPSERT_CHUNK = """
INSERT INTO chunks (chunk_id, filing_id, item, item_default_title, item_title, section_path,
                    item_chunk_index, text, html_list_depth, source, extra)
VALUES (%(chunk_id)s, %(filing_id)s, %(item)s, %(item_default_title)s, %(item_title)s, %(section_path)s,
        %(item_chunk_index)s, %(text)s, %(html_list_depth)s, %(source)s, %(extra)s::jsonb)
ON CONFLICT (chunk_id) DO UPDATE SET
    item = EXCLUDED.item, item_default_title = EXCLUDED.item_default_title,
    item_title = EXCLUDED.item_title, section_path = EXCLUDED.section_path,
    item_chunk_index = EXCLUDED.item_chunk_index, text = EXCLUDED.text,
    html_list_depth = EXCLUDED.html_list_depth, source = EXCLUDED.source, extra = EXCLUDED.extra
"""

UPSERT_SENTENCE = """
INSERT INTO sentences (sentence_id, chunk_id, filing_id, item, item_title, section_path, sentence_index,
                       text, bullet_level, bullet_indent_pt, html_list_depth, source_block_index, source, extra)
VALUES (%(sentence_id)s, %(chunk_id)s, %(filing_id)s, %(item)s, %(item_title)s, %(section_path)s,
        %(sentence_index)s, %(text)s, %(bullet_level)s, %(bullet_indent_pt)s, %(html_list_depth)s,
        %(source_block_index)s, %(source)s, %(extra)s::jsonb)
ON CONFLICT (sentence_id) DO UPDATE SET
    chunk_id = EXCLUDED.chunk_id, item = EXCLUDED.item, item_title = EXCLUDED.item_title,
    section_path = EXCLUDED.section_path, sentence_index = EXCLUDED.sentence_index,
    text = EXCLUDED.text, bullet_level = EXCLUDED.bullet_level,
    bullet_indent_pt = EXCLUDED.bullet_indent_pt, html_list_depth = EXCLUDED.html_list_depth,
    source_block_index = EXCLUDED.source_block_index, source = EXCLUDED.source, extra = EXCLUDED.extra
"""

UPSERT_EMBEDDING = """
INSERT INTO sentence_embeddings (sentence_id, model_id, part_index, part_text, embedding)
VALUES (%s, %s, 0, NULL, %s::vector)
ON CONFLICT (sentence_id, model_id, part_index) DO UPDATE SET embedding = EXCLUDED.embedding, created_at = now()
"""


def load_embeddings(cur, sentences: list[dict], model_name: str, cache_dir: Path) -> tuple[int, int]:
    """Copy vectors from the on-disk embedding cache into sentence_embeddings.

    Sentences without a cached vector are skipped (they simply have no embedding yet).
    Returns (loaded, missing).
    """
    cur.execute("SELECT model_id, dimension FROM embedding_models WHERE name = %s", (model_name,))
    found = cur.fetchone()
    if not found:
        raise SystemExit(f"Embedding model '{model_name}' is not in embedding_models. Add it first (name + dimension).")
    model_id, dimension = found
    loaded = missing = 0
    batch: list[tuple] = []
    for sentence in sentences:
        path = cache_dir / f"{embedding_cache_key(model_name, sentence['text'])}.json"
        if not path.is_file():
            missing += 1
            continue
        vector = json.loads(path.read_text(encoding="utf-8"))
        if len(vector) != dimension:
            raise SystemExit(
                f"Vector for {sentence['id']} has {len(vector)} dimensions but model '{model_name}' is registered with {dimension}."
            )
        batch.append((sentence["id"], model_id, vector_literal(vector)))
        loaded += 1
    if batch:
        cur.executemany(UPSERT_EMBEDDING, batch)
    return loaded, missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--company", required=True, help="Company prefix used in the IDs, e.g. unh")
    parser.add_argument("--year", required=True, type=int, help="Fiscal year, e.g. 2025")
    parser.add_argument("--data-dir", default=str(ROOT / "data" / "raw"),
                        help="Folder that contains <company>/<year>/ (default: data/raw)")
    parser.add_argument("--chunks", help="Override path to the chunks JSON")
    parser.add_argument("--sentences", help="Override path to the sentences JSON")
    parser.add_argument("--form-type", default="10-K")
    parser.add_argument("--replace", action="store_true",
                        help="Delete this filing's existing rows first (use after the extractor changed and IDs moved)")
    parser.add_argument("--embeddings-cache", help="Folder of cached embeddings (default location used by the pipeline: data/embeddings)")
    parser.add_argument("--embedding-model", default="bge-m3", help="Model name to read from the cache (default: bge-m3)")
    parser.add_argument("--dry-run", action="store_true", help="Check the input files only; do not touch the database")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
    except ImportError:
        pass

    folder = Path(args.data_dir) / args.company / str(args.year)
    chunks_path = Path(args.chunks) if args.chunks else folder / f"{args.year}_chunks.json"
    sentences_path = Path(args.sentences) if args.sentences else folder / f"{args.year}_chunk_sentences.json"
    for path in (chunks_path, sentences_path):
        if not path.is_file():
            print(f"Missing input file: {path}", file=sys.stderr)
            return 1

    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    sentences = json.loads(sentences_path.read_text(encoding="utf-8"))
    problems = validate_inputs(chunks, sentences)
    print(f"Read {len(chunks)} chunks and {len(sentences)} sentences from {folder}")
    if problems:
        for problem in problems:
            print(f"PROBLEM: {problem}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("Dry run: inputs are consistent. Nothing written.")
        return 0

    try:
        import psycopg
    except ImportError:
        print("psycopg is not installed. Run: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1

    source = (chunks[0].get("source") if chunks else None)
    with psycopg.connect(build_dsn()) as conn:
        with conn.cursor() as cur:
            if args.replace:
                cur.execute("DELETE FROM filings WHERE company = %s AND fiscal_year = %s AND form_type = %s",
                            (args.company, args.year, args.form_type))
                print(f"--replace: removed {cur.rowcount} existing filing row(s) and everything under them")
            cur.execute(UPSERT_FILING, {"company": args.company, "fiscal_year": args.year,
                                        "form_type": args.form_type, "source": source})
            filing_id = cur.fetchone()[0]
            cur.executemany(UPSERT_CHUNK, [chunk_row(c, filing_id) for c in chunks])
            cur.executemany(UPSERT_SENTENCE, [sentence_row(s, filing_id) for s in sentences])
            if args.embeddings_cache:
                loaded, missing = load_embeddings(cur, sentences, args.embedding_model, Path(args.embeddings_cache))
                print(f"Embeddings ({args.embedding_model}): {loaded} loaded, {missing} sentences had no cached vector")
            cur.execute("SELECT count(*) FROM chunks WHERE filing_id = %s", (filing_id,))
            n_chunks = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sentences WHERE filing_id = %s", (filing_id,))
            n_sentences = cur.fetchone()[0]
    print(f"Done. {args.company} {args.year} (filing_id {filing_id}): {n_chunks} chunks, {n_sentences} sentences in the database.")
    if n_chunks != len(chunks) or n_sentences != len(sentences):
        print("WARNING: the database holds rows from an earlier load that are no longer in the files. "
              "Rerun with --replace to make it match exactly.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
