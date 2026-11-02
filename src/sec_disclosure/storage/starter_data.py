"""Import bundled reports into missing PostgreSQL scopes, without local output or model calls."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import tarfile

from . import alignments, disclosures, peer_comparisons
from .chatbot_schema import apply_outdated
from .pipeline_snapshots import publish
from .alignment_cleanup import cleanup_existing, replace_document
from sec_disclosure.alignment_policy import exclude_from_reports

ROOT = Path(__file__).resolve().parents[3]
BUNDLE = ROOT / "starter_data"


class StarterDataError(RuntimeError):
    """A safe-to-display startup failure; connection credentials are never included."""


def scope_key(scope):
    return (scope["kind"], scope["company"].lower(), tuple(scope["years"]),
            tuple(scope.get("peers", [])), tuple(scope.get("items", [])), scope.get("target_selection", ""))


def read_manifest(bundle=BUNDLE):
    manifest = json.loads((Path(bundle) / "manifest.json").read_text())
    if manifest.get("version") != 1 or not manifest.get("files") or not manifest.get("source_commit"):
        raise ValueError("The bundled starter-data manifest is invalid.")
    paths = [entry["path"] for entry in manifest["files"]]
    if len(paths) != len(set(paths)):
        raise ValueError("The starter-data manifest contains duplicate reports.")
    return manifest


def existing_scopes(cur):
    found = set()
    cur.execute("SELECT DISTINCT f.company, f.fiscal_year FROM filings f JOIN disclosures d USING (filing_id) "
                "WHERE f.form_type='10-K'")
    found.update(("disclosures", company.lower(), (year,), (), (), "") for company, year in cur.fetchall())
    cur.execute("SELECT company, previous_year, current_year FROM alignment_runs")
    found.update(("alignments", company.lower(), (previous, current), (), (), "")
                 for company, previous, current in cur.fetchall())
    cur.execute("SELECT company, years FROM synthesis_reports")
    found.update(("synthesis", company.lower(), tuple(years), (), (), "") for company, years in cur.fetchall())
    cur.execute("SELECT target_ticker, fiscal_year, peer_tickers, items, target_selection FROM peer_comparison_reports")
    found.update(("peer_comparison", target.lower(), (year,), tuple(sorted(p.lower() for p in peers)),
                  tuple(sorted(items)), selection) for target, year, peers, items, selection in cur.fetchall())
    return found


def report_groups(bundle, manifest, needed):
    """Stream and verify reports in memory; never extract the archive onto disk."""
    archive_path = Path(bundle) / "reports.tar.gz"
    with archive_path.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if archive_hash != manifest["archive_sha256"]:
        raise ValueError("The bundled starter-data archive checksum does not match.")
    expected = {entry["path"]: entry for entry in manifest["files"]}
    seen, current, documents = set(), None, {}
    with tarfile.open(archive_path, mode="r|gz") as archive:
        for member in archive:
            entry = expected.get(member.name)
            if entry is None or member.name in seen or not member.isfile() or member.size != entry["bytes"]:
                raise ValueError("Unexpected or invalid starter-data archive entry.")
            seen.add(member.name)
            key = scope_key(entry["scope"])
            if key not in needed:
                continue
            if current is not None and current != key:
                yield current, documents
                documents = {}
            current = key
            raw = archive.extractfile(member).read()
            if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                raise ValueError("A bundled report checksum does not match.")
            documents[Path(member.name).name] = {
                "payload": json.loads(raw), "sha256": entry["sha256"],
                "source_path": f"starter_data/{manifest['source_commit']}/{member.name}"}
    if seen != expected.keys():
        raise ValueError("A bundled starter-data report is missing.")
    if current is not None:
        yield current, documents


def import_disclosures(cur, company, year, documents):
    cur.execute("INSERT INTO filings (company, fiscal_year, form_type) VALUES (%s,%s,'10-K') "
                "ON CONFLICT DO NOTHING", (company, year))
    cur.execute("SELECT filing_id FROM filings WHERE company=%s AND fiscal_year=%s AND form_type='10-K' FOR UPDATE",
                (company, year))
    filing_id = cur.fetchone()[0]
    cur.execute("SELECT 1 FROM disclosures WHERE filing_id=%s LIMIT 1", (filing_id,))
    if cur.fetchone() is not None:
        return False
    # A real snapshot can be empty intentionally; do not replace it with examples.
    cur.execute("SELECT 1 FROM pipeline_stage_snapshots WHERE company=%s AND kind='disclosures' "
                "AND previous_year=%s AND current_year=%s", (company, year, year))
    if cur.fetchone() is not None:
        return False
    for name, status in (("disclosures.json", "accepted"), ("review_candidates.json", "review_candidate")):
        report = documents.get(name, {}).get("payload")
        if report is None:
            continue
        if problems := disclosures.validate_report(report, name, company, year):
            raise ValueError("; ".join(problems))
        cur.executemany(disclosures.UPSERT_DISCLOSURE, [
            disclosures.disclosure_row(row, filing_id, status) for row in report["disclosures"]])
    publish(cur, company, "disclosures", year, year, documents)
    return True


def import_alignments(cur, company, years, documents):
    previous, current = years
    reports = {name: documents[name]["payload"] for name in ("alignments.json", "needs_review.json") if name in documents}
    if exclude_from_reports(reports):
        for name, report in reports.items():
            replace_document(documents, name, report)
    report = documents["alignments.json"]["payload"]
    cur.execute("INSERT INTO alignment_runs (company,previous_year,current_year,schema_version,run_complete,comparison_scope,note) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING run_id",
                (company, previous, current, str(report["schema_version"]), bool(report.get("run_complete")),
                 report.get("comparison_scope"), report.get("note")))
    if (created := cur.fetchone()) is None:
        return False
    run_id = created[0]
    for name in ("alignments.json", "needs_review.json"):
        if name not in documents:
            continue
        report = documents[name]["payload"]
        if problems := alignments.validate_report(report, name, company, previous, current):
            raise ValueError("; ".join(problems))
        records = report["alignments"]
        cur.executemany(alignments.INSERT_ALIGNMENT, [
            alignments.alignment_row(row, run_id, name.removesuffix(".json")) for row in records])
        cur.executemany(alignments.INSERT_MEMBER, [member for row in records for member in alignments.member_rows(row, run_id)])
    publish(cur, company, "alignments", previous, current, documents)
    return True


def import_report(cur, kind, document):
    report = document["payload"]
    if kind == "synthesis":
        cur.execute("INSERT INTO synthesis_reports (company,years,report) VALUES (%s,%s,%s::jsonb) "
                    "ON CONFLICT DO NOTHING RETURNING company", (report["company"].upper(), sorted(map(int, report["years"])),
                                                               json.dumps(report, ensure_ascii=False)))
        return cur.fetchone() is not None
    peer_comparisons.validate_report(report)
    cur.execute("INSERT INTO peer_comparison_reports "
                "(target_ticker,fiscal_year,peer_tickers,items,target_selection,run_complete,report) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING RETURNING saved_at",
                (*peer_comparisons.source_key(report), bool(report.get("run_complete")), json.dumps(report, ensure_ascii=False)))
    if (created := cur.fetchone()) is None:
        return False
    peer_comparisons._insert_run(cur, report, source_file=document["source_path"],
                                 sha256=document["sha256"], saved_at=created[0])
    return True


def seed_connection(conn, bundle=BUNDLE):
    """One atomic import; repeated launches only inspect existing scope keys."""
    manifest = read_manifest(bundle)
    imported = Counter()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(80808009)")
        wanted = {scope_key(entry["scope"]) for entry in manifest["files"]}
        pending = set()
        excluded = cleanup_existing(cur, pending=pending)
        missing = wanted - existing_scopes(cur)
        if not missing:
            return {"imported": {}, "skipped": len(wanted), "companies": manifest["companies"],
                    "excluded_alignments": excluded, "cleanup_pending": sorted(pending)}
        apply_outdated(cur)
        for key, documents in report_groups(bundle, manifest, missing):
            kind, company, years, *_ = key
            if kind in {"disclosures", "alignments"}:
                marker = {"source": "test-data", "source_commit": manifest["source_commit"], "browse_only": True}
                documents["starter_data.json"] = {"payload": marker,
                    "sha256": hashlib.sha256(json.dumps(marker, sort_keys=True).encode()).hexdigest(),
                    "source_path": "starter_data/manifest.json"}
                saved = (import_disclosures(cur, company, years[0], documents) if kind == "disclosures" else
                         import_alignments(cur, company, years, documents))
            else:
                saved = import_report(cur, kind, next(iter(documents.values())))
            imported[kind] += bool(saved)
        return {"imported": dict(imported), "skipped": len(wanted) - sum(imported.values()),
                "companies": manifest["companies"], "excluded_alignments": excluded, "cleanup_pending": sorted(pending)}


def seed_database(*, bundle=BUNDLE, env_file=None):
    try:
        import psycopg
        arguments = disclosures.connection_arguments(env_file)
        arguments["connect_timeout"] = 3
        with psycopg.connect(**arguments) as conn:
            return seed_connection(conn, bundle)
    except Exception as error:
        raise StarterDataError(f"Starter data could not be loaded into PostgreSQL ({type(error).__name__}). "
                               "Start Docker/PostgreSQL with docker compose up -d and apply db/init schemas, "
                               "then reload the app. Existing results were retained; the import was rolled back.") from None
