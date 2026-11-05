"""Import bundled reports into missing PostgreSQL scopes, without local output or model calls."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile

from . import alignments, disclosures, peer_comparisons
from .chatbot_schema import apply_outdated
from .pipeline_snapshots import publish
from .alignment_cleanup import cleanup_existing, replace_document
from .artifacts import artifact_lock_key
from sec_disclosure.alignment_policy import exclude_from_reports

ROOT = Path(__file__).resolve().parents[3]
BUNDLE = ROOT / "starter_data"
COMPLETION_MARKER_VERSION = 1


class StarterDataError(RuntimeError):
    """A safe-to-display startup failure; connection credentials are never included."""


def scope_key(scope):
    return (scope["kind"], scope["company"].lower(), tuple(scope["years"]),
            tuple(scope.get("peers", [])), tuple(scope.get("items", [])), scope.get("target_selection", ""))


def read_manifest(bundle=BUNDLE):
    manifest = json.loads((Path(bundle) / "manifest.json").read_text(encoding='utf-8'))
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


def mark_completed_starter_reports(cur, manifest, available):
    """Backfill report-completion markers without replacing real run progress."""
    cur.execute('SELECT company,previous_year,current_year FROM alignment_runs WHERE run_complete IS TRUE')
    completed_pairs = {(company.lower(), previous, current) for company, previous, current in cur.fetchall()}
    candidates = []
    scopes = {scope_key(entry['scope']) for entry in manifest['files']}
    for scope in sorted(scopes):
        kind, company, years, *_ = scope
        if kind != 'synthesis' or len(years) < 2:
            continue
        pairs = list(zip(years, years[1:]))
        required = {scope}
        required.update(('disclosures', company, (year,), (), (), '') for year in years)
        required.update(('alignments', company, pair, (), (), '') for pair in pairs)
        if required <= available and all((company, *pair) in completed_pairs for pair in pairs):
            candidates.append((company, years))
    if not candidates:
        return 0
    cur.execute('SELECT pg_advisory_xact_lock(80808008)')
    cur.execute((ROOT / 'db/init/08_pipeline_artifacts.sql').read_text(encoding='utf-8'))
    imported_at = datetime.now(timezone.utc).isoformat()
    written = 0
    for company, years in candidates:
        # A running pipeline owns this company even before its first checkpoint.
        cur.execute('SELECT pg_try_advisory_xact_lock(%s)',
                    (artifact_lock_key('main', 'pipeline_runs/' + company),))
        if not cur.fetchone()[0]:
            continue
        marker = {'ticker': company, 'years': list(years), 'complete': True,
                  'full_pipeline_complete': True, 'until': 'synthesis',
                  'completion_source': 'starter_data', 'completion_marker_version': COMPLETION_MARKER_VERSION,
                  'source_commit': manifest['source_commit'], 'started_at': imported_at,
                  'last_invocation_at': imported_at, 'completed_at': imported_at, 'stages': []}
        key = f"pipeline_runs/{company}/{'-'.join(map(str, years))}/status.json"
        cur.execute('''INSERT INTO pipeline_artifacts(namespace,path,kind)
            SELECT 'main',path,'directory' FROM unnest(%s::text[]) AS path
            ON CONFLICT(namespace,path) DO NOTHING''',
            ([str(parent) for parent in PurePosixPath(key).parents],))
        cur.execute('''INSERT INTO pipeline_artifacts(namespace,path,kind,content)
            VALUES ('main',%s,'file',%s) ON CONFLICT(namespace,path) DO NOTHING''',
            (key, json.dumps(marker).encode('utf-8')))
        written += cur.rowcount
    return written


def seed_connection(conn, bundle=BUNDLE):
    """One atomic import; repeated launches only inspect existing scope keys."""
    manifest = read_manifest(bundle)
    imported = Counter()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(80808009)")
        wanted = {scope_key(entry["scope"]) for entry in manifest["files"]}
        pending = set()
        excluded = cleanup_existing(cur, pending=pending)
        available = existing_scopes(cur)
        missing = wanted - available
        if not missing:
            markers = mark_completed_starter_reports(cur, manifest, available)
            return {"imported": {}, "skipped": len(wanted), "companies": manifest["companies"],
                    "excluded_alignments": excluded, "cleanup_pending": sorted(pending),
                    "completion_markers": markers}
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
        markers = mark_completed_starter_reports(cur, manifest, existing_scopes(cur))
        return {"imported": dict(imported), "skipped": len(wanted) - sum(imported.values()),
                "companies": manifest["companies"], "excluded_alignments": excluded, "cleanup_pending": sorted(pending),
                "completion_markers": markers}


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
