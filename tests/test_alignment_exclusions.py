"""Only unresolved one-sided final results leave the pipeline and starter data."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from uuid import uuid4

import pytest

from sec_disclosure.alignment_policy import exclude_from_reports, exclude_unreviewable, unreviewable_alignment
from sec_disclosure.storage import alignments, starter_data
from sec_disclosure.storage.alignment_cleanup import cleanup_existing, replace_document
from sec_disclosure.storage.pipeline_snapshots import publish


def reports():
    def row(number, before, after, status, label=None):
        return {"match_id": f"amd_2024_2025_M{number:04d}", "previous_ids": before, "current_ids": after,
                "status": status, "relationship": "one_to_one" if before and after else "previous_only" if before else "current_only",
                "change_analysis": {"status": "completed" if label else "not_started", "llm": label}}
    rows = [row(1, ["amd_2024_7_D001"], ["amd_2025_7_D001"], "needs_review"),
            row(2, ["amd_2024_7_D002"], [], "needs_review"),
            row(3, [], ["amd_2025_7_D002"], "needs_review"),
            row(4, [], ["amd_2025_7_D003"], "unmatched", "New"),
            row(5, ["amd_2024_7_D003"], [], "unmatched", "Removed")]
    common = {"company": "amd", "previous_year": "2024", "current_year": "2025", "schema_version": "8",
              "run_complete": True, "error": None, "comparison_scope": "same_item",
              "overall_counts": dict(Counter(row["status"] for row in rows)),
              "coverage": {"input_disclosures": 6, "represented_disclosures": 6,
                           "pending_disclosure_ids": ["amd_2025_7_D002"], "conflicting_disclosure_ids": []}}
    result = {}
    for name, status in (("alignments.json", "unmatched"), ("needs_review.json", "needs_review")):
        selected = [row for row in rows if row["status"] == status]
        result[name] = {**deepcopy(common), "alignments": selected, "alignment_count": len(selected), "counts": {status: len(selected)}}
    return result


def test_exclusions_keep_valid_absences_and_two_sided_reviews_with_consistent_totals():
    pair = reports()
    accepted = deepcopy(pair["alignments.json"]["alignments"])
    removed = exclude_from_reports(pair)
    assert len(removed) == 2
    assert pair["alignments.json"]["alignments"] == accepted
    assert len(pair["needs_review.json"]["alignments"]) == 1
    for report in pair.values():
        assert report["overall_counts"] == {"needs_review": 1, "unmatched": 2}
        assert report["coverage"]["input_disclosures"] == 6
        assert report["coverage"]["represented_disclosures"] == 4
        assert report["coverage"]["excluded_disclosure_ids"] == ["amd_2024_7_D002", "amd_2025_7_D002"]
        assert report["coverage"]["pending_disclosure_ids"] == []
        assert len(report["excluded_alignments"]) == 2
        assert not any(unreviewable_alignment(row) for row in report["alignments"])
    before = deepcopy(pair)
    assert exclude_from_reports(pair) == []
    assert pair == before


def test_partial_work_is_retained_for_resume():
    report = reports()["needs_review.json"]
    report["run_complete"] = False
    before = deepcopy(report)
    assert exclude_unreviewable(report) == []
    assert report == before


def test_legacy_storage_import_cleans_both_partitions_before_database_mapping(tmp_path):
    pair = reports()
    for name, report in pair.items():
        (tmp_path / name).write_text(json.dumps(report))
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    imported = alignments.read_alignments(tmp_path, "amd", 2024, 2025)
    assert len(imported["needs_review"]["alignments"]) == 1
    assert len(imported["alignments"]["alignments"]) == 2
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original


def test_shipped_starter_data_excludes_819_unresolved_cases_and_keeps_3118_new_removed():
    manifest = starter_data.read_manifest()
    assert manifest["excluded_unreviewable_alignments"] == 819
    scopes = {starter_data.scope_key(entry["scope"]) for entry in manifest["files"] if entry["scope"]["kind"] == "alignments"}
    unmatched = 0
    for _, documents in starter_data.report_groups(starter_data.BUNDLE, manifest, scopes):
        for document in documents.values():
            report = document["payload"]
            assert report["alignment_count"] == len(report["alignments"])
            assert not any(unreviewable_alignment(row) for row in report["alignments"])
            unmatched += sum(row["status"] == "unmatched" for row in report["alignments"])
    assert unmatched == 3118


@pytest.mark.skipif(os.environ.get("SEC_STARTER_DB_INTEGRATION") != "1", reason="Requires local PostgreSQL")
def test_postgres_cleanup_is_atomic_idempotent_and_preserves_human_decisions(monkeypatch):
    import psycopg
    from psycopg import sql
    from sec_disclosure.storage.disclosures import connection_arguments
    from sec_disclosure.storage.artifacts import PostgresArtifactStore
    from sec_disclosure.storage.pipeline_snapshots import read_snapshots

    arguments = connection_arguments()
    info = psycopg.conninfo.conninfo_to_dict(arguments.get("conninfo", ""),
        **{key: value for key, value in arguments.items() if key != "conninfo"})
    assert info.get("host", "") in {"", "localhost", "127.0.0.1", "::1"}
    database = "sec_exclusion_test_" + uuid4().hex[:12]
    with psycopg.connect(**arguments, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            info["dbname"] = database
            monkeypatch.setenv("DATABASE_URL", psycopg.conninfo.make_conninfo(**info))
            with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
                for path in sorted((starter_data.ROOT / "db/init").glob("*.sql")):
                    cur.execute(path.read_text())
                cur.execute("INSERT INTO alignment_runs(company,previous_year,current_year,schema_version,run_complete) "
                            "VALUES ('amd',2024,2025,'8',true) RETURNING run_id")
                run_id = cur.fetchone()[0]
                documents = {}
                for name, report in reports().items():
                    cur.executemany(alignments.INSERT_ALIGNMENT,
                        [alignments.alignment_row(row, run_id, name.removesuffix(".json")) for row in report["alignments"]])
                    cur.executemany(alignments.INSERT_MEMBER,
                        [member for row in report["alignments"] for member in alignments.member_rows(row, run_id)])
                    replace_document(documents, name, report)
                replace_document(documents, "manifest.json", {"preserve": "audit"})
                publish(cur, "amd", "alignments", 2024, 2025, documents)
                cur.execute("INSERT INTO pipeline_artifacts(namespace,path,kind,content) VALUES ('human-reviews','notes','file',%s)",
                            (b'Existing human labels and notes',))
            with PostgresArtifactStore("main").lock("pipeline_runs/amd"):
                with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
                    pending = set()
                    assert cleanup_existing(cur, pending=pending) == 0
                    assert pending == {"amd"}
            with pytest.raises(RuntimeError, match="Rollback"):
                with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
                    assert cleanup_existing(cur) == 2
                    raise RuntimeError("Rollback")
            with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM alignments")
                assert cur.fetchone()[0] == 5
            # A detached worker waits for an active pipeline, then finishes the
            # cleanup without another page visit or any local output/log files.
            import subprocess
            import sys
            import time
            with PostgresArtifactStore("main").lock("pipeline_runs/amd"):
                worker = subprocess.Popen([sys.executable, str(starter_data.ROOT / "scripts/cleanup_alignment_reviews.py"),
                    "--wait", "--poll-interval", "0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    for _ in range(100):
                        stored = PostgresArtifactStore("human-reviews").get("manual_reviews/pipeline_refresh/exclusion_cleanup.json")
                        if stored and json.loads(stored[1])["status"] == "waiting":
                            break
                        time.sleep(0.1)
                    else:
                        pytest.fail("Cleanup worker did not wait for the active pipeline")
                except BaseException:
                    worker.terminate()
                    worker.wait(timeout=10)
                    raise
            assert worker.wait(timeout=30) == 0
            status = json.loads(PostgresArtifactStore("human-reviews").get(
                "manual_reviews/pipeline_refresh/exclusion_cleanup.json")[1])
            assert status["status"] == "complete" and status["excluded_alignments"] == 2
            with psycopg.connect(**connection_arguments()) as conn, conn.cursor() as cur:
                assert cleanup_existing(cur) == 0
                cur.execute("SELECT status,count(*) FROM alignments GROUP BY status")
                assert dict(cur.fetchall()) == {"needs_review": 1, "unmatched": 2}
                cur.execute("SELECT count(*) FROM alignment_members")
                assert cur.fetchone()[0] == 4
                cur.execute("SELECT content FROM pipeline_artifacts WHERE namespace='human-reviews' AND path='notes'")
                assert bytes(cur.fetchone()[0]) == b'Existing human labels and notes'
            snapshot = read_snapshots("amd", [("alignments", 2024, 2025)])["alignments", "2024", "2025"]
            assert snapshot["documents"]["manifest.json"]["payload"] == {"preserve": "audit"}
            for name in reports():
                document = snapshot["documents"][name]
                raw = (json.dumps(document["payload"], indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
                assert document["sha256"] == hashlib.sha256(raw).hexdigest()
                assert not any(unreviewable_alignment(row) for row in document["payload"]["alignments"])
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
