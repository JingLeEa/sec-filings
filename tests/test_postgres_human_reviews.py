"""Human decisions and refresh jobs must survive without local output files."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest

from artifact_support import MemoryArtifactStore
from sec_disclosure.pipelines import review_jobs
from sec_disclosure.review_overrides import read_reviews
from sec_disclosure.storage import human_reviews
from sec_disclosure.storage.artifacts import ArtifactStorageError, PostgresArtifactStore, PostgresWorkspace
from sec_ui import config, data, review, review_storage
from test_disclosure_manual_review import disclosure
from test_review_refresh import pipeline

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(monkeypatch, tmp_path):
    store = MemoryArtifactStore()
    store.env_file = None
    with PostgresWorkspace(human_reviews.NAMESPACE, store=store) as workspace:
        monkeypatch.setenv("SEC_UI_DATA_SOURCE", "auto")
        monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "legacy"))
        monkeypatch.setattr(human_reviews, "document_lock", lambda path: nullcontext())
        monkeypatch.setattr(review_jobs, "document_lock", lambda path: nullcontext())
        monkeypatch.setattr(review_storage, "_workspace", lambda *args: workspace)
        yield workspace, tmp_path / "legacy"


def alignment():
    return {"company": "amd", "previous_year": 2024, "current_year": 2025,
            "match_id": "amd_2024_2025_M0001", "previous_ids": ["old"], "current_ids": ["new"],
            "previous_text": "Prior disclosure.", "current_text": "Changed disclosure.", "text_source": "full",
            "source_file": "needs_review.json", "change_label": "Not labelled", "methods": {"llm": None}}


def test_default_reviews_and_queue_use_only_postgres(database):
    workspace, legacy = database
    row = disclosure(901, ["model_requested_review"])
    saved = review.save_disclosure_review(row, "Operations & Capacity", "Keep this note.", expected=None)
    change = review.save_review(alignment(), "Modified", "Human decision.", expected=None)
    queue = config.manual_reviews_dir() / "pipeline_refresh"
    job = review_jobs.enqueue(queue, stage="disclosure", company="amd", previous=2024,
        record_id=row["disclosure_id"], saved=saved,
        settings={"input_source": "postgres", "reviews_dir": "/__sec_postgres__/old-process/manual_reviews"})
    assert review.load_disclosure_reviews("amd", 2024)[row["disclosure_id"]] == saved
    assert review.load_reviews("amd", 2024, 2025)[change["match_id"]] == change
    assert read_reviews(config.manual_reviews_dir(), "amd", "disclosure", 2024)[row["disclosure_id"]] == saved
    def execute(job, output, update):
        assert job["settings"]["reviews_dir"] == str(queue.parent)
        update(phase="Checked")
    review_jobs.process_queue(queue, runner=execute, capture_output=True)
    assert review_jobs.jobs(queue)[0]["status"] == "complete"
    assert review_jobs.job_path(queue, job["job_id"]).artifact_store is workspace.store
    assert (queue / "worker.log").exists()
    assert not legacy.exists()


def test_stale_session_and_database_failure_preserve_saved_choice(database, monkeypatch):
    workspace, legacy = database
    record = alignment()
    saved = review.save_review(record, "Modified", "First decision", expected=None)
    with pytest.raises(data.DataError, match="another session"):
        review.save_review(record, "Unchanged", "Stale decision", expected=None)
    original_put = workspace.store.put

    def fail(*args, **kwargs):
        raise ArtifactStorageError("Database unavailable")

    monkeypatch.setattr(workspace.store, "put", fail)
    with pytest.raises(data.DataError, match="PostgreSQL"):
        review.save_review(record, "Unchanged", "Do not fall back", expected=saved)
    monkeypatch.setattr(workspace.store, "put", original_put)
    assert review.load_reviews("amd", 2024, 2025)[saved["match_id"]] == saved
    assert not legacy.exists()


def test_legacy_import_merges_missing_ids_without_replacing_postgres(database):
    workspace, legacy = database
    saved = review.save_review(alignment(), "Modified", "New PostgreSQL choice", expected=None)
    target = review.review_path("amd", 2024, 2025)
    document = json.loads(target.read_text())
    old = deepcopy(saved)
    old["notes"] = "Old local choice"
    second = {**old, "match_id": "another"}
    document["reviews"] = {saved["match_id"]: old, "another": second}
    source = legacy / "amd/2024-2025/manual_reviews.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(document))
    before = source.read_bytes()
    old_job = review_jobs.enqueue(legacy / "pipeline_refresh", stage="alignment", company="amd",
        previous=2024, current=2025, record_id=saved["match_id"], saved=old,
        settings={"input_source": "files", "reviews_dir": str(legacy)})
    old_job_path = review_jobs.job_path(legacy / "pipeline_refresh", old_job["job_id"])
    job_before = old_job_path.read_bytes()
    human_reviews.migrate_local(legacy, workspace.root / "manual_reviews")
    result = review.load_reviews("amd", 2024, 2025)
    assert result[saved["match_id"]] == saved
    assert result["another"] == second
    migrated = review_jobs.jobs(workspace.root / "manual_reviews/pipeline_refresh")[0]
    assert migrated["job_id"] == old_job["job_id"]
    assert migrated["status"] == "failed"  # File work cannot silently resume writing locally.
    assert migrated["settings"]["input_source"] == "postgres"
    assert migrated["settings"]["reviews_dir"] == str(config.manual_reviews_dir())
    human_reviews.migrate_local(legacy, workspace.root / "manual_reviews")
    assert source.read_bytes() == before
    assert old_job_path.read_bytes() == job_before
    assert review.load_reviews("amd", 2024, 2025) == result


def test_pipeline_reads_database_decisions_without_local_fallback(database, monkeypatch):
    workspace, legacy = database
    saved = review.save_review(alignment(), "Modified", "Shared with pipeline", expected=None)

    def get(store, key):
        assert store.namespace == human_reviews.NAMESPACE
        return workspace.store.get(key)

    monkeypatch.setattr(PostgresArtifactStore, "get", get)
    assert read_reviews(legacy, "amd", "alignment", 2024, 2025,
                        input_source="postgres")[saved["match_id"]] == saved
    assert not legacy.exists()


@pytest.mark.skipif(os.environ.get("SEC_REVIEW_DB_INTEGRATION") != "1", reason="Requires local PostgreSQL")
def test_real_postgres_restart_background_refresh_and_shared_locks(pipeline, monkeypatch, tmp_path):
    import psycopg
    from psycopg import sql
    from concurrent.futures import ThreadPoolExecutor
    from sec_disclosure.storage.disclosures import connection_arguments, store_disclosures

    arguments = connection_arguments()
    info = psycopg.conninfo.conninfo_to_dict(arguments.get("conninfo", ""),
                **{key: value for key, value in arguments.items() if key != "conninfo"})
    assert info.get("host", "") in {"", "localhost", "127.0.0.1", "::1"}
    database = "sec_review_test_" + uuid4().hex[:12]
    with psycopg.connect(**arguments, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        active = None
        try:
            info["dbname"] = database
            monkeypatch.setenv("DATABASE_URL", psycopg.conninfo.make_conninfo(**info))
            monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
            monkeypatch.delenv("SEC_UI_PIPELINE_ENV_FILE", raising=False)
            review_storage._workspace.clear()
            with psycopg.connect(**connection_arguments()) as conn:
                for schema in sorted((ROOT / "db/init").glob("*.sql")):
                    conn.execute(schema.read_text())
            case = pipeline
            folder = case.root / "disclosures/amd/2024"
            store_disclosures(folder, "amd", 2024, require_complete=True)
            row = json.loads((folder / "disclosures.json").read_text())["disclosures"][0]
            row["fiscal_year"] = 2024
            # Simulate a legacy review saved before this change.
            monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
            old = review.save_disclosure_review(row, "Operations & Capacity", "Legacy note", expected=None)
            monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
            before = {str(p): p.read_bytes() for p in case.root.rglob("*") if p.is_file()}
            root = config.manual_reviews_dir()
            active = review_storage._workspace(
                hashlib.sha256(json.dumps(connection_arguments(), sort_keys=True).encode()).hexdigest(),
                str(config.legacy_manual_reviews_dir().resolve()), None)
            assert review.load_disclosure_reviews("amd", 2024)[row["disclosure_id"]] == old
            saved = review.save_disclosure_review(row, "Technology & AI", "Survives app restart", expected=old)
            queue = root / "pipeline_refresh"
            job = review_jobs.enqueue(queue, stage="disclosure", company="amd", previous=2024,
                record_id=row["disclosure_id"], saved=saved, settings={"input_source": "postgres",
                    "reviews_dir": str(root), "data_dir": str(case.root), "disclosures_dir": str(case.root / "disclosures"),
                    "alignments_dir": str(case.root / "alignments"), "cache_namespace": "main"})
            with review_jobs.worker_lock(queue) as acquired:
                assert acquired
                assert review_jobs.worker_running(queue)
                review_jobs.process_queue(queue, runner=lambda *args: pytest.fail("Second worker entered"))
            monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "1")
            worker = review_jobs.launch_worker(queue)
            assert worker.wait(timeout=30) == 0
            finished = review_jobs.jobs(queue)[0]
            assert finished["status"] == "complete", finished.get("error")
            assert finished["affected_pairs"] == []  # No alignment/model call is needed for a single filing.
            assert (queue / "worker.log").exists()
            with psycopg.connect(**connection_arguments()) as conn:
                label = conn.execute("SELECT taxonomy FROM disclosures WHERE disclosure_id=%s", (row["disclosure_id"],)).fetchone()[0]
                assert label == "Technology & AI"
            active.__exit__()
            active = None
            review_storage._workspace.clear()
            # A new resource simulates closing and reopening the application.
            assert review.load_disclosure_reviews("amd", 2024)[row["disclosure_id"]] == saved
            active = review_storage._workspace(
                hashlib.sha256(json.dumps(connection_arguments(), sort_keys=True).encode()).hexdigest(),
                str(config.legacy_manual_reviews_dir().resolve()), None)
            assert review_jobs.jobs(config.manual_reviews_dir() / "pipeline_refresh")[0]["job_id"] == job["job_id"]
            import streamlit as st
            from streamlit.testing.v1 import AppTest
            st.cache_data.clear()
            at = AppTest.from_file(str(ROOT / "app/pages/5_Manual_Review.py"), default_timeout=60)
            at.session_state["manual_section"] = "Disclosure"
            at.session_state["review_scope"] = "AI approved"
            at.run()
            assert not at.exception and not at.error
            at.selectbox(key="disclosure_selected").set_value(row["disclosure_id"]).run()
            assert not at.exception and not at.error
            assert at.text_area(key="disclosure_notes").value == "Survives app restart"
            assert any("Choices and notes are saved to PostgreSQL" in caption.value for caption in at.caption)
            # Two independent workspace connections must retain different records in one document.
            def write_record(index):
                with human_reviews.workspace(prefix="/__sec_postgres__/human-reviews-" + uuid4().hex) as other:
                    path = other.root / "manual_reviews/concurrency.json"
                    with human_reviews.document_lock(path):
                        payload = json.loads(path.read_text()) if path.exists() else {}
                        payload[str(index)] = index
                        path.write_text(json.dumps(payload))
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(write_record, range(2)))
            assert json.loads((active.root / "manual_reviews/concurrency.json").read_text()) == {"0": 0, "1": 1}
            assert {str(p): p.read_bytes() for p in case.root.rglob("*") if p.is_file()} == before
        finally:
            if active is not None:
                active.__exit__()
            review_storage._workspace.clear()
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
