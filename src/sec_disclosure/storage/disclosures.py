"""Load retained disclosure records into PostgreSQL without importing alignment data."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[3]
SUPPORTED_SCHEMAS = {"3", "4"}
KNOWN = {"disclosure_id", "company", "fiscal_year", "item", "section", "sections", "topic", "summary",
         "content", "taxonomy", "sources", "verification"}

UPSERT_FILING = """
INSERT INTO filings (company, fiscal_year, form_type) VALUES (%s, %s, '10-K')
ON CONFLICT (company, fiscal_year, form_type) DO UPDATE SET company = EXCLUDED.company
RETURNING filing_id
"""
UPSERT_DISCLOSURE = """
INSERT INTO disclosures (disclosure_id, filing_id, item, section, sections, topic, summary, content, taxonomy,
                         review_status, sources, verification, extra)
VALUES (%(disclosure_id)s, %(filing_id)s, %(item)s, %(section)s, %(sections)s, %(topic)s, %(summary)s,
        %(content)s, %(taxonomy)s, %(review_status)s, %(sources)s::jsonb, %(verification)s::jsonb, %(extra)s::jsonb)
ON CONFLICT (disclosure_id) DO UPDATE SET
    filing_id = EXCLUDED.filing_id, item = EXCLUDED.item, section = EXCLUDED.section, sections = EXCLUDED.sections,
    topic = EXCLUDED.topic, summary = EXCLUDED.summary, content = EXCLUDED.content, taxonomy = EXCLUDED.taxonomy,
    review_status = EXCLUDED.review_status, sources = EXCLUDED.sources, verification = EXCLUDED.verification,
    extra = EXCLUDED.extra
"""


class DisclosureStorageError(RuntimeError):
    """Database failure whose message is safe to print without connection credentials."""


@dataclass(frozen=True)
class DisclosureImport:
    company: str
    year: int
    filing_id: int
    accepted: int
    review: int
    stored: int


def disclosure_row(record: dict, filing_id: int, review_status: str) -> dict:
    return {
        "disclosure_id": record["disclosure_id"], "filing_id": filing_id, "item": str(record["item"]),
        "section": record.get("section"), "sections": list(record.get("sections") or []),
        "topic": record.get("topic"), "summary": record.get("summary"), "content": record["content"],
        "taxonomy": record["taxonomy"], "review_status": review_status,
        "sources": json.dumps(record.get("sources") or [], ensure_ascii=False),
        "verification": json.dumps(record.get("verification") or {}, ensure_ascii=False),
        "extra": json.dumps({k: v for k, v in record.items() if k not in KNOWN}, ensure_ascii=False),
    }


def validate_report(report: dict, name: str, company: str, year: int) -> list[str]:
    problems = []
    if str(report.get("schema_version")) not in SUPPORTED_SCHEMAS:
        problems.append(f"{name}: unsupported schema_version {report.get('schema_version')!r}; expected 3 or 4")
    if str(report.get("company")) != company or str(report.get("fiscal_year")) != str(year):
        problems.append(f"{name}: file is for {report.get('company')} {report.get('fiscal_year')}, not {company} {year}")
    return problems


def read_disclosures(folder: Path, company: str, year: int, *, require_complete: bool = False):
    """Validate both retained sets before connecting or replacing any database rows."""
    if require_complete:
        usage = json.loads((folder / "token_usage.json").read_text(encoding="utf-8"))
        if usage.get("run_complete") is not True:
            raise ValueError(f"{company} {year}: finish disclosure extraction before importing its snapshot")
    loaded, problems, ids = [], [], []
    for name, status in (("disclosures.json", "accepted"), ("review_candidates.json", "review_candidate")):
        report = json.loads((folder / name).read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError(f"{name}: report must be an object")
        problems.extend(validate_report(report, name, company, year))
        records = report.get("disclosures")
        if not isinstance(records, list):
            raise ValueError(f"{name}: disclosures must be an array")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"{name}: each disclosure must be an object")
            for field in ("disclosure_id", "item", "content", "taxonomy"):
                if not isinstance(record.get(field), str) or not record[field].strip():
                    raise ValueError(f"{name}: disclosure requires a nonempty {field}")
            for field, expected in (("company", company), ("fiscal_year", str(year))):
                if field in record and str(record[field]) != expected:
                    raise ValueError(f"{name}: disclosure {record['disclosure_id']} has the wrong {field}")
            prefix = f"{company}_{year}_{record['item']}_D"
            if not record["disclosure_id"].startswith(prefix) or not record["disclosure_id"][len(prefix):].isdigit():
                raise ValueError(f"{name}: disclosure ID does not belong to {company} {year} Item {record['item']}")
            ids.append(record["disclosure_id"])
        loaded.append((status, records))
    if len(ids) != len(set(ids)):
        problems.append("a disclosure_id appears more than once across the two files")
    if problems:
        raise ValueError("; ".join(problems))
    return loaded


def connection_arguments(env_file: Path | None = None) -> dict:
    """Read database settings without changing the environment or interpolating secrets."""
    path = Path(env_file).expanduser() if env_file is not None else ROOT / ".env"
    if env_file is not None and not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}

    def setting(name, default=None):
        return os.environ.get(name, values.get(name) if values.get(name) is not None else default)

    if setting("DATABASE_URL"):
        return {"conninfo": setting("DATABASE_URL"), "connect_timeout": 10}
    return {"host": setting("POSTGRES_HOST", "localhost"), "port": setting("POSTGRES_PORT", "5432"),
            "dbname": setting("POSTGRES_DB", "sec_disclosure"), "user": setting("POSTGRES_USER", "sec"),
            "password": setting("POSTGRES_PASSWORD", "sec_local_pw"), "connect_timeout": 10}


def store_disclosures(folder: Path, company: str, year: int, *, env_file: Path | None = None,
                      require_complete: bool = False, replace: bool = False) -> DisclosureImport:
    """Atomically import a filing; replacement removes only obsolete disclosures for that filing."""
    loaded = read_disclosures(Path(folder), company, year, require_complete=require_complete or replace)
    arguments = connection_arguments(env_file)
    try:
        import psycopg
    except ImportError:
        raise DisclosureStorageError("psycopg is not installed. Run: .venv/bin/python -m pip install -r requirements.txt") from None
    try:
        with psycopg.connect(**arguments) as conn, conn.cursor() as cur:
            cur.execute(UPSERT_FILING, (company, year))
            filing_id = cur.fetchone()[0]
            for status, records in loaded:
                cur.executemany(UPSERT_DISCLOSURE, [disclosure_row(r, filing_id, status) for r in records])
            if replace:
                ids = [r["disclosure_id"] for _, records in loaded for r in records]
                cur.execute("DELETE FROM disclosures WHERE filing_id = %s "
                            "AND NOT (disclosure_id = ANY(%s::text[]))", (filing_id, ids))
            cur.execute("SELECT count(*) FROM disclosures WHERE filing_id = %s", (filing_id,))
            stored = cur.fetchone()[0]
            if replace and stored != sum(len(records) for _, records in loaded):
                raise ValueError("Imported disclosure snapshot has an inconsistent row count")
    except Exception as error:
        # Driver errors can include a DSN or credentials; keep them out of logs.
        raise DisclosureStorageError(f"PostgreSQL disclosure import failed ({type(error).__name__}). "
            "Check that the database is running, the tables exist and POSTGRES_* or DATABASE_URL settings are correct. "
            "Saved extraction JSON is available for retry.") from None
    return DisclosureImport(company, year, filing_id, len(loaded[0][1]), len(loaded[1][1]), stored)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", required=True)
    parser.add_argument("--year", required=True, type=int)
    parser.add_argument("--disclosures-dir", type=Path, default=ROOT / "data" / "disclosures",
                        help="Folder containing <company>/<year>/")
    parser.add_argument("--output-dir", type=Path, help="Exact extraction folder; overrides --disclosures-dir")
    parser.add_argument("--env-file", type=Path, help="Database settings file (default: repository .env)")
    parser.add_argument("--replace", action="store_true", help="Sync the completed filing snapshot, removing obsolete disclosure IDs")
    parser.add_argument("--dry-run", action="store_true", help="Validate files without accessing PostgreSQL")
    args = parser.parse_args(argv)
    company = args.company.lower()
    folder = args.output_dir or args.disclosures_dir / company / str(args.year)
    try:
        if args.dry_run:
            loaded = read_disclosures(folder, company, args.year, require_complete=args.replace)
            print(f"Dry run: {company} {args.year}: {len(loaded[0][1])} accepted, "
                  f"{len(loaded[1][1])} review candidates. Nothing written.")
            return 0
        result = store_disclosures(folder, company, args.year, env_file=args.env_file, replace=args.replace)
        print(f"Done. {company} {args.year}: {result.stored} disclosures in PostgreSQL "
              f"({result.accepted} accepted, {result.review} review candidates).")
        if result.stored != result.accepted + result.review:
            print("Database contains obsolete disclosures; rerun with --replace after extraction completes.", file=sys.stderr)
            return 2
        return 0
    except (OSError, ValueError, KeyError, TypeError, DisclosureStorageError) as error:
        print(f"Disclosure import error: {error}", file=sys.stderr)
        return 1
