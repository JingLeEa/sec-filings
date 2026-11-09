"""Explicit starter updates preserve human decisions and non-starter work."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
from unittest.mock import patch
from uuid import uuid4

import pytest

from sec_disclosure.storage import starter_data as seed, starter_refresh as refresh


def test_report_provenance_detects_edits_without_changing_source():
    original = {"report": {"executive_summary": "Starter"}, "years": [2023, 2024]}
    tagged = refresh.tag_report({"payload": original, "source_path": "starter_data/commit/synthesis/report.json"})
    assert refresh.owned_report(tagged)
    assert refresh.report_body(tagged) == original and refresh.PROVENANCE not in original
    tagged["report"] = {"executive_summary": "My own analysis"}
    assert not refresh.owned_report(tagged)


def test_report_provenance_rejects_unknown_sources():
    tagged = refresh.tag_report({"payload": {"report": {}}, "source_path": "my-own-run/report.json"})
    assert not refresh.owned_report(tagged)


@pytest.mark.parametrize("enabled", [False, True])
def test_refresh_command_forwards_explicit_option_and_reports_protected_work(enabled, capsys):
    spec = importlib.util.spec_from_file_location("seed_cli", seed.ROOT / "scripts/seed_starter_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = {"companies": ["amd"], "imported": {}, "skipped": 5,
              "refreshed": {"synthesis": 1}, "unchanged": 2, "protected_scopes": 2,
              "protected_companies": {"amd": "human reviews exist"}}
    with patch.object(module, "seed_database", return_value=result) as run:
        assert module.main(["--refresh"] if enabled else []) == 0
    assert run.call_args.kwargs.get("refresh", False) is enabled
    output = capsys.readouterr().out
    assert ("Preserved AMD" in output) is enabled


def make_bundle(folder, version):
    folder.mkdir()
    documents = []
    for year in (2023, 2024):
        report = {"schema_version": 4, "company": "amd", "fiscal_year": year, "run_complete": True,
                  "disclosures": [{"disclosure_id": f"amd_{year}_1_D001", "item": "1", "content": f"Version {version}",
                    "summary": f"Starter summary {version}", "taxonomy": "Strategy & Business Model"}]}
        scope = {"kind": "disclosures", "company": "amd", "years": [year]}
        documents.append((f"disclosures/amd/{year}/disclosures.json", report, scope))
    alignment = {"schema_version": seed.alignments.SUPPORTED_SCHEMA, "company": "amd", "previous_year": 2023,
                 "current_year": 2024, "run_complete": True, "alignment_count": 1, "alignments": [{
                     "match_id": "amd_2023_2024_M0001", "status": "ai_verified", "relationship": "one_to_one",
                     "previous_ids": ["amd_2023_1_D001"], "current_ids": ["amd_2024_1_D001"],
                     "explanation": f"Starter match {version}"}]}
    documents.append(("alignments/amd/2023-2024/alignments.json", alignment,
                      {"kind": "alignments", "company": "amd", "years": [2023, 2024]}))
    synthesis = {"company": "AMD", "years": [2023, 2024], "report": {
        "executive_summary": f"Starter synthesis {version}", "key_findings": []},
        "financial_metrics": [] if version == 1 else [{"label": "Assets", "values": [{"value": version}]}]}
    documents.append(("synthesis/amd_2023_2024_synthesis_output.json", synthesis,
                      {"kind": "synthesis", "company": "amd", "years": [2023, 2024]}))
    peer = {"schema_version": seed.peer_comparisons.SUPPORTED_SCHEMA, "target": "AMD", "fiscal_year": 2024,
            "peers": ["NVDA"], "items": ["1"], "target_selection": "all", "comparisons": [],
            "jobs_completed": 0, "jobs_planned": 0, "run_complete": True, "note": f"Starter peers {version}"}
    documents.append(("peer_comparison/amd/2024/peer_comparison.json", peer,
                      {"kind": "peer_comparison", "company": "amd", "years": [2024],
                       "peers": ["nvda"], "items": ["1"], "target_selection": "all"}))
    manifest = {"version": 1, "companies": ["amd"], "source_commit": f"source-{version}", "files": []}
    with tarfile.open(folder / "reports.tar.gz", "w:gz") as archive:
        for name, report, scope in documents:
            raw = json.dumps(report).encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
            entry = {"path": name, "scope": scope, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            if scope["kind"] in {"synthesis", "peer_comparison"}:
                known = {refresh.digest(report)}
                for previous in folder.parent.glob("*/manifest.json"):
                    for older in json.loads(previous.read_text())["files"]:
                        if older["path"] == name:
                            known.update(older.get("known_payload_sha256", []))
                entry["known_payload_sha256"] = sorted(known)
            manifest["files"].append(entry)
    manifest["archive_sha256"] = hashlib.sha256((folder / "reports.tar.gz").read_bytes()).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return folder


@pytest.mark.skipif(os.environ.get("SEC_STARTER_DB_INTEGRATION") != "1", reason="Requires isolated local PostgreSQL")
def test_refresh_updates_legacy_starters_atomically_and_preserves_reviews_edits_and_active_runs(tmp_path):
    import psycopg
    from psycopg import sql
    from sec_disclosure.storage.artifacts import artifact_lock_key

    arguments = seed.disclosures.connection_arguments()
    info = psycopg.conninfo.conninfo_to_dict(arguments.get("conninfo", ""),
            **{k: v for k, v in arguments.items() if k != "conninfo"})
    assert info.get("host", "") in {"", "localhost", "127.0.0.1", "::1"}, "Use local PostgreSQL"
    database = "sec_starter_refresh_test_" + uuid4().hex[:12]
    # A libpq conninfo can contain dbname too; use one flattened configuration.
    target = {**info, "dbname": database}
    old = make_bundle(tmp_path / "old", 1)
    newer = make_bundle(tmp_path / "new", 2)
    newest = make_bundle(tmp_path / "newest", 3)
    with psycopg.connect(**arguments, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with psycopg.connect(**target) as conn:
                for schema in sorted((seed.ROOT / "db/init").glob("*.sql")):
                    conn.execute(schema.read_text())

            def load(bundle, **options):
                with psycopg.connect(**target) as conn:
                    return seed.seed_connection(conn, bundle, **options)

            assert sum(load(old)["imported"].values()) == 5
            with psycopg.connect(**target) as conn:
                # Model an installation created before provenance tags existed.
                conn.execute("UPDATE synthesis_reports SET report=report-%s", (refresh.PROVENANCE,))
                conn.execute("UPDATE peer_comparison_reports SET report=report-%s", (refresh.PROVENANCE,))
                legacy = conn.execute("SELECT report FROM synthesis_reports").fetchone()[0]
                conn.execute("UPDATE synthesis_reports SET report=jsonb_set(report,'{report,executive_summary}',%s::jsonb)",
                             (json.dumps("My edited legacy report"),))
            assert load(newer, refresh=True)["refreshed"] == {}
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT report->'report'->>'executive_summary' FROM synthesis_reports").fetchone()[0] == "My edited legacy report"
                conn.execute("UPDATE synthesis_reports SET report=%s::jsonb", (json.dumps(legacy),))
            assert load(newer)["imported"] == {}  # Ordinary startup never refreshes.
            updated = load(newer, refresh=True)
            assert updated["refreshed"] == {"disclosures": 2, "alignments": 1, "synthesis": 1, "peer_comparison": 1}
            assert updated["protected_scopes"] == 0
            assert load(newer, refresh=True)["unchanged"] == 5
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT DISTINCT content FROM disclosures").fetchall() == [("Version 2",)]
                report = conn.execute("SELECT report FROM synthesis_reports").fetchone()[0]
                assert report["financial_metrics"] and refresh.owned_report(report)
                peer = conn.execute("SELECT report FROM peer_comparison_reports").fetchone()[0]
                assert peer["note"] == "Starter peers 2" and refresh.owned_report(peer)

            review = {"company": "amd", "reviews": {"amd_2023_2024_M0001": {"notes": "Keep my decision"}}}
            review_path = "manual_reviews/amd/2023-2024/manual_reviews.json"
            with psycopg.connect(**target) as conn:
                conn.execute("INSERT INTO pipeline_artifacts(namespace,path,kind,content) "
                             "VALUES ('human-reviews',%s,'file',%s)", (review_path, json.dumps(review).encode()))
            protected = load(newest, refresh=True)
            assert protected["refreshed"] == {} and protected["protected_scopes"] == 5
            with psycopg.connect(**target) as conn:
                saved = conn.execute("SELECT content FROM pipeline_artifacts WHERE namespace='human-reviews' AND path=%s",
                                     (review_path,)).fetchone()[0]
                assert json.loads(bytes(saved)) == review
                conn.execute("DELETE FROM pipeline_artifacts WHERE namespace='human-reviews' AND path=%s", (review_path,))
                conn.execute("UPDATE disclosures SET summary='My own edit' WHERE disclosure_id='amd_2023_1_D001'")
            assert load(newest, refresh=True)["refreshed"] == {}
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT summary FROM disclosures WHERE disclosure_id='amd_2023_1_D001'").fetchone()[0] == "My own edit"
                conn.execute("UPDATE disclosures SET summary='Starter summary 2' WHERE disclosure_id='amd_2023_1_D001'")
                conn.execute("UPDATE synthesis_reports SET report=jsonb_set(report,'{report,executive_summary}',%s::jsonb)",
                             (json.dumps("My newer pipeline report"),))
            assert load(newest, refresh=True)["refreshed"] == {}
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT report->'report'->>'executive_summary' FROM synthesis_reports").fetchone()[0] == "My newer pipeline report"
                conn.execute("UPDATE synthesis_reports SET report=%s::jsonb", (json.dumps(report),))
            for namespace, key in (("main", "pipeline_runs/amd"),
                                   ("human-reviews", "document:" + review_path),
                                   ("peer", "peer_comparison/amd/2024/nvda")):
                with psycopg.connect(**target) as running:
                    lock = artifact_lock_key(namespace, key)
                    running.execute("SELECT pg_advisory_lock(%s)", (lock,))
                    assert load(newest, refresh=True)["refreshed"] == {}
                    running.execute("SELECT pg_advisory_unlock(%s)", (lock,))
            job_path = "manual_reviews/pipeline_refresh/jobs/pending.json"
            with psycopg.connect(**target) as conn:
                conn.execute("INSERT INTO pipeline_artifacts(namespace,path,kind,content) "
                             "VALUES ('human-reviews',%s,'file',%s)",
                             (job_path, json.dumps({"company": "amd", "status": "queued"}).encode()))
            assert load(newest, refresh=True)["refreshed"] == {}
            with psycopg.connect(**target) as conn:
                conn.execute("DELETE FROM pipeline_artifacts WHERE namespace='human-reviews' AND path=%s", (job_path,))
            with psycopg.connect(**target) as conn:
                conn.execute("INSERT INTO pipeline_artifacts(namespace,path,kind,content) "
                             "VALUES ('main','pipeline_runs/amd/own-run/status.json','file',%s)",
                             (json.dumps({"status": "stopped", "complete": False}).encode(),))
            assert load(newest, refresh=True)["refreshed"] == {}
            with psycopg.connect(**target) as conn:
                conn.execute("DELETE FROM pipeline_artifacts WHERE namespace='main' AND path='pipeline_runs/amd/own-run/status.json'")
            replace = refresh.replace_scope

            def interrupted(*args):
                replace(*args)
                raise RuntimeError("Interrupted update")

            with patch.object(refresh, "replace_scope", side_effect=interrupted):
                with pytest.raises(RuntimeError, match="Interrupted"):
                    load(newest, refresh=True)
            with psycopg.connect(**target) as conn:
                assert conn.execute("SELECT report->'report'->>'executive_summary' FROM synthesis_reports").fetchone()[0] == "Starter synthesis 2"
                assert conn.execute("SELECT DISTINCT content FROM disclosures").fetchall() == [("Version 2",)]
            assert sum(load(newest, refresh=True)["refreshed"].values()) == 5
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
