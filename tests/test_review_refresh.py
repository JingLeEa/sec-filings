"""Human decisions reach real downstream stages without live API calls."""
from copy import deepcopy
import io
import json
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import pytest

from sec_disclosure.agents import alignment_runtime, disclosure_alignment, materiality, materiality_runtime
from sec_disclosure.agents.change_analysis import effective_change_analysis
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.llm.config import LLMConfig
from sec_disclosure.llm.disclosures import digest, write_json
from sec_disclosure.pipelines import review_jobs, review_refresh
from sec_disclosure.review_overrides import apply_alignment_reviews, apply_disclosure_reports, read_reviews
from sec_ui import data, review

import test_disclosure_alignment as alignment_fixture
import test_materiality as materiality_fixture


@pytest.fixture
def pipeline(monkeypatch):
    case = alignment_fixture.AlignmentTests("runTest")
    case.setUp()
    root = case.root
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(root / "reviews"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(root / "disclosures"))
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(root / "alignments"))
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "0")
    for path in (root / "disclosures").rglob("disclosures.json"):
        report = json.loads(path.read_text())
        for row in report["disclosures"]:
            row["verification"]["source_unit_count"] = 2
        write_json(path, report)
    for path in (root / "disclosures").rglob("*.json"):
        report = json.loads(path.read_text())
        if "input_hashes" in report:
            report["schema_version"] = "4"
            write_json(path, report)
    yield case
    case.doCleanups()


def settings(root):
    return {"input_source": "files", "data_dir": str(root), "disclosures_dir": str(root / "disclosures"),
            "alignments_dir": str(root / "alignments"), "reviews_dir": str(root / "reviews"), "request_interval": 0}


def queue_job(case, stage, record, saved):
    queue = case.root / "reviews/pipeline_refresh"
    job = review_jobs.enqueue(queue, stage=stage, company="amd",
        previous=2024 if stage == "disclosure" else 2023,
        current=None if stage == "disclosure" else 2024,
        record_id=record["disclosure_id"] if stage == "disclosure" else record["match_id"],
        saved=saved, settings=settings(case.root))
    return queue, job


def providers(case, observed):
    """Delegate to existing protocol fixtures, recording materiality's actual inputs."""
    def align(prompt, **kwargs):
        payload = json.loads(prompt)
        if "anchors" in payload:
            observed.append(("matching", payload))
            result = {"action": "propose", "matches": [{"current_id": key,
                "previous_ids": [key.replace("_2025_", "_2024_") if "_2025_" in key else key.replace("_2024_", "_2023_")],
                "rationale": "Same specific reported topic."} for key in payload["anchors"]]}
            return CompletionResult(json.dumps(result), "test-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        observed.append(("change" if payload.get("change_analysis") else "verification", payload))
        return case.response(prompt, **kwargs)

    def classify(prompt, **kwargs):
        payload = json.loads(prompt)
        observed.append(("materiality", payload))
        return materiality_fixture.MaterialityTests("runTest").response(prompt, **kwargs)

    return align, classify


def run_offline(case, queue, observed):
    from sec_disclosure.llm import config, embeddings
    alignment_provider, materiality_provider = providers(case, observed)
    fake = LLMConfig("https://example.test/v1", "fake-secret", "default")
    with patch.object(disclosure_alignment, "load_config", return_value=fake), \
         patch.object(config, "load_config", return_value=fake), \
         patch.object(materiality, "load_config", return_value=fake), \
         patch.object(alignment_runtime, "request_completion", side_effect=alignment_provider), \
         patch.object(materiality_runtime, "request_completion", side_effect=materiality_provider), \
         patch.object(embeddings, "make_embedder", return_value=lambda texts: [[1.0, 0.0] for _ in texts]), \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        review_jobs.process_queue(queue)


def clone_year(case, original, year):
    raw = case.root / f"raw/amd/{year}"
    for path in (case.root / f"raw/amd/{original}").glob("*.json"):
        payload = json.loads(path.read_text().replace(str(original), str(year)))
        write_json(raw / path.name.replace(str(original), str(year)), payload)
    for path in (case.root / f"disclosures/amd/{original}").glob("*.json"):
        payload = json.loads(path.read_text().replace(str(original), str(year)))
        if "input_hashes" in payload:
            payload["input_hashes"] = {str(p): digest(p.read_bytes()) for p in raw.glob("*.json")}
        write_json(case.root / f"disclosures/amd/{year}" / path.name, payload)


def test_disclosure_review_refreshes_both_adjacent_pairs_through_materiality(pipeline):
    case = pipeline
    clone_year(case, 2023, 2025)
    path = case.root / "disclosures/amd/2024/disclosures.json"
    report = json.loads(path.read_text())
    row = report["disclosures"][0]
    row["verification"].update(status="needs_review", review_reasons=["model_requested_review"],
                                model_review_reason="Confirm disclosure type.")
    report["disclosures"].remove(row)
    write_json(path, report)
    review_path = path.with_name("review_candidates.json")
    candidates = json.loads(review_path.read_text())
    candidates["disclosures"].append(row)
    write_json(review_path, candidates)
    saved = review.save_disclosure_review({**row, "fiscal_year": 2024}, "Operations & Capacity", "Human decision.", expected=None)
    queue, job = queue_job(case, "disclosure", row, saved)
    observed = []
    run_offline(case, queue, observed)
    finished = review_jobs.jobs(queue)[0]
    assert finished["status"] == "complete", finished.get("error")
    assert finished["affected_pairs"] == [[2023, 2024], [2024, 2025]]
    corrected = next(row for row in json.loads(path.read_text())["disclosures"] if row["disclosure_id"] == saved["disclosure_id"])
    assert corrected["taxonomy"] == "Operations & Capacity"
    assert corrected["llm_disclosure_type"] == "Financial & Capital Resources"
    assert corrected["verification"]["status"] == "source_validated"
    assert corrected["human_review"]["previous_verification"]["review_reasons"] == ["model_requested_review"]
    for previous, current in finished["affected_pairs"]:
        output = case.root / f"alignments/amd/{previous}-{current}"
        report = disclosure_alignment.load_alignment_report(output)
        assert all(row["change_analysis"]["final_taxonomy"] for row in report["alignments"])
        assert all("materiality_analysis" in row for row in report["alignments"])
        member = next(member for row in report["alignments"] for side in ("previous", "current")
                      for member in row[side + "_disclosures"] if member["disclosure_id"] == saved["disclosure_id"])
        assert member["taxonomy"] == "Operations & Capacity"
    assert sum(kind == "materiality" for kind, _ in observed) == 2


def test_alignment_review_keeps_human_label_and_reruns_only_downstream(pipeline):
    case = pipeline
    assert case.run_cli(change_analysis=True)[0] == 0
    record = data.load_comparison("amd", 2023, 2024)["records"][0]
    saved = review.save_review(record, "Expanded", "Human change label.", expected=None)
    queue, job = queue_job(case, "alignment", record, saved)
    observed = []
    run_offline(case, queue, observed)
    finished = review_jobs.jobs(queue)[0]
    assert finished["status"] == "complete", finished.get("error")
    assert finished["affected_pairs"] == [[2023, 2024]]
    assert not any(kind in {"matching", "verification"} for kind, _ in observed)
    result = next(row for row in case.saved_report()["alignments"] if row["match_id"] == record["match_id"])
    assert result["change_analysis"]["llm"] == "Modified"
    assert result["change_analysis"]["final_taxonomy"] == "Expanded"
    assert "materiality_analysis" in result
    assert any(row["change_analysis_reference"]["final_taxonomy"] == "Expanded"
               for kind, payload in observed if kind == "materiality" for row in payload["rows"])


def test_human_reviewed_needs_review_group_reaches_materiality_without_fake_ai_approval(pipeline):
    case = pipeline
    assert case.run_cli("--max-steps", "1", response=case.invalid_citation_response)[0] == 0
    record = next(row for row in data.load_comparison("amd", 2023, 2024)["records"]
                  if row["source_file"] == "needs_review.json" and (row["previous_text"] or row["current_text"]))
    saved = review.save_review(record, "Modified", "Human reviewed selected source.", expected=None)
    queue, job = queue_job(case, "alignment", record, saved)
    observed = []
    run_offline(case, queue, observed)
    finished = review_jobs.jobs(queue)[0]
    assert finished["status"] == "complete", finished.get("error")
    result = next(row for row in case.saved_report()["alignments"] if row["match_id"] == record["match_id"])
    assert result["status"] == "needs_review"
    assert result["change_analysis"]["llm"] is None
    assert result["change_analysis"]["final_taxonomy"] == "Modified"
    assert "materiality_analysis" in result


def test_affected_pairs_include_existing_nonadjacent_comparisons_only_for_reviewed_year():
    assert review_refresh.affected_pairs(2024, [2022, 2023, 2024, 2025], [(2022, 2024)]) == [
        (2022, 2024), (2023, 2024), (2024, 2025)]


def test_queue_is_idempotent_retries_failures_and_resumes_interrupted_jobs(tmp_path):
    arguments = dict(stage="disclosure", company="amd", previous=2024, record_id="amd_2024_7_D001",
                     saved={"reviewed_at": "2026-10-29T00:00:00Z"}, settings={"input_source": "files"})
    job = review_jobs.enqueue(tmp_path, **arguments)
    assert review_jobs.enqueue(tmp_path, **arguments)["job_id"] == job["job_id"]
    attempts = []

    def fail(job, output, update):
        attempts.append(job["attempts"])
        raise RuntimeError("Temporary failure")

    review_jobs.process_queue(tmp_path, runner=fail)
    assert review_jobs.jobs(tmp_path)[0]["status"] == "failed"
    review_jobs.retry(tmp_path, job["job_id"])
    review_jobs.process_queue(tmp_path, runner=lambda *args: None)
    assert review_jobs.jobs(tmp_path)[0]["status"] == "complete"
    restored = review_jobs.jobs(tmp_path)[0]
    restored["status"] = "running"
    write_json(review_jobs.job_path(tmp_path, job["job_id"]), restored)
    review_jobs.process_queue(tmp_path, runner=lambda *args: None)
    assert review_jobs.jobs(tmp_path)[0]["attempts"] == 3


def test_changed_evidence_does_not_inherit_a_human_label_after_match_id_changes(pipeline):
    case = pipeline
    assert case.run_cli(change_analysis=True)[0] == 0
    record = data.load_comparison("amd", 2023, 2024)["records"][0]
    saved = review.save_review(record, "Expanded", "", expected=None)
    report = case.saved_report()
    row = report["alignments"][0]
    row["match_id"] = "amd_2023_2024_M0999"
    row["change_analysis"] = disclosure_alignment.empty_change_analysis()
    copied = apply_alignment_reviews(report, case.data().records, {saved["match_id"]: saved})
    assert copied["alignments"][0]["manual_review"]["change_label"] == "Expanded"
    assert effective_change_analysis(copied["alignments"][0])["final_taxonomy"] == "Expanded"
    assert effective_change_analysis(copied["alignments"][0])["llm"] == "Modified"
    records = deepcopy(case.data().records)
    records[row["current_ids"][0]]["content"] += " Changed evidence."
    stale = apply_alignment_reviews(report, records, {saved["match_id"]: saved})
    assert "manual_review" not in stale["alignments"][0]


def test_failed_materiality_keeps_review_and_retry_finishes_downstream(pipeline):
    case = pipeline
    assert case.run_cli(change_analysis=True)[0] == 0
    record = data.load_comparison("amd", 2023, 2024)["records"][0]
    saved = review.save_review(record, "Expanded", "Do not overwrite.", expected=None)
    queue, job = queue_job(case, "alignment", record, saved)
    with patch.object(materiality, "main", side_effect=RuntimeError("Temporary service failure")):
        run_offline(case, queue, [])
    failed = review_jobs.jobs(queue)[0]
    assert failed["status"] == "failed"
    assert "Temporary service failure" in failed["error"]
    assert review.load_reviews("amd", 2023, 2024)[record["match_id"]] == saved
    review_jobs.retry(queue, job["job_id"])
    observed = []
    run_offline(case, queue, observed)
    assert review_jobs.jobs(queue)[0]["status"] == "complete"
    assert not any(kind in {"matching", "verification"} for kind, _ in observed)
    result = next(row for row in case.saved_report()["alignments"] if row["match_id"] == record["match_id"])
    assert result["change_analysis"]["final_taxonomy"] == "Expanded"
    assert "materiality_analysis" in result


def test_pipeline_rejects_superseded_review_without_model_requests(pipeline):
    case = pipeline
    path = case.root / "disclosures/amd/2024/disclosures.json"
    row = json.loads(path.read_text())["disclosures"][0]
    display = {**row, "fiscal_year": 2024}
    first = review.save_disclosure_review(display, "Operations & Capacity", "", expected=None)
    queue, old = queue_job(case, "disclosure", row, first)
    second = review.save_disclosure_review(display, "Technology & AI", "New decision.", expected=first)
    _, new = queue_job(case, "disclosure", row, second)
    observed = []
    run_offline(case, queue, observed)
    status = {job["job_id"]: job["status"] for job in review_jobs.jobs(queue)}
    assert status[old["job_id"]] == "superseded"
    assert status[new["job_id"]] == "complete"
    assert sum(kind == "matching" for kind, _ in observed) == 1


def test_postgres_refresh_publishes_consistent_snapshots_and_normalized_inputs(pipeline, monkeypatch):
    """Exercise the PostgreSQL handoff with an in-memory transactional store."""
    from sec_disclosure.storage import pipeline_snapshots, disclosures, alignments
    case = pipeline
    row = json.loads((case.root / "disclosures/amd/2024/disclosures.json").read_text())["disclosures"][0]
    saved = review.save_disclosure_review({**row, "fiscal_year": 2024}, "Operations & Capacity", "Human.", expected=None)
    queue, job = queue_job(case, "disclosure", row, saved)
    job["settings"]["input_source"] = "postgres"
    write_json(review_jobs.job_path(queue, job["job_id"]), job)
    database = case.database_inputs()
    for value in database.values():
        value["revision"] = review_refresh.fingerprint(value["documents"])
    publications = []

    def read(company, scopes, **kwargs):
        assert company == "amd"
        return {tuple(map(str, scope)): deepcopy(database[tuple(map(str, scope))]) for scope in scopes}

    def revision(self, kind, previous, current):
        return database.get((kind, str(previous), str(current)), {}).get("revision")

    def publish(kind, output, company, previous, current, expected):
        for (co, input_kind, first, last), expected_revision in expected.items():
            assert co == company == "amd"
            assert database.get((input_kind, str(first), str(last)), {}).get("revision") == expected_revision
        names = pipeline_snapshots.DISCLOSURE_FILES if kind == "disclosures" else pipeline_snapshots.ALIGNMENT_FILES
        documents = pipeline_snapshots.folder_documents(output, names, include_change_cache=kind == "alignments")
        database[(kind, str(previous), str(current))] = {"documents": documents,
                                                      "revision": review_refresh.fingerprint(documents)}
        publications.append((kind, previous, current))

    def store_disclosures(output, company, year, **kwargs):
        loaded = disclosures.read_disclosures(output, company, year, require_complete=True)
        mapped = [disclosures.disclosure_row(record, 1, status) for status, rows in loaded for record in rows]
        assert all(json.loads(record["sources"]) for record in mapped)
        publish("disclosures", output, company, year, year, kwargs["expected_snapshots"])

    def store_alignments(output, company, previous, current, **kwargs):
        reports = alignments.read_alignments(output, company, previous, current)
        rows = [alignments.alignment_row(record, 1, name) for name, report in reports.items() for record in report["alignments"]]
        assert rows
        publish("alignments", output, company, previous, current, kwargs["expected_snapshots"])

    monkeypatch.setattr(pipeline_snapshots, "read_snapshots", read)
    monkeypatch.setattr(review_refresh, "read_snapshots", read)
    monkeypatch.setattr(review_refresh.Refresh, "revision", revision)
    monkeypatch.setattr(review_refresh.Refresh, "scopes", lambda self: ([2023, 2024], []))
    monkeypatch.setattr(disclosures, "store_disclosures", store_disclosures)
    monkeypatch.setattr(alignments, "store_alignments", store_alignments)
    run_offline(case, queue, [])
    finished = review_jobs.jobs(queue)[0]
    assert finished["status"] == "complete", finished.get("error")
    assert publications[0] == ("disclosures", 2024, 2024)
    assert all(kind == "alignments" and (previous, current) == (2023, 2024)
               for kind, previous, current in publications[1:])
    result = database[("alignments", "2023", "2024")]["documents"]["alignments.json"]["payload"]
    assert all("materiality_analysis" in row for row in result["alignments"])


def test_snapshot_guard_refuses_to_overwrite_newer_inputs():
    from sec_disclosure.storage.pipeline_snapshots import check_revisions, PipelineInputError
    from unittest.mock import Mock
    cursor = Mock()
    cursor.fetchone.return_value = ("newer-revision",)
    with pytest.raises(PipelineInputError, match="changed"):
        check_revisions(cursor, {("amd", "disclosures", 2024, 2024): "old-revision"})


def test_actual_background_worker_applies_review_without_comparison_or_model_calls(pipeline, monkeypatch):
    import shutil
    case = pipeline
    shutil.rmtree(case.root / "disclosures/amd/2023")
    path = case.root / "disclosures/amd/2024/disclosures.json"
    row = json.loads(path.read_text())["disclosures"][0]
    saved = review.save_disclosure_review({**row, "fiscal_year": 2024}, "Operations & Capacity", "Human.", expected=None)
    queue, job = queue_job(case, "disclosure", row, saved)
    monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "1")
    worker = review_jobs.launch_worker(queue)
    assert worker.wait(timeout=20) == 0
    finished = review_jobs.jobs(queue)[0]
    assert finished["status"] == "complete", finished.get("error")
    assert finished["affected_pairs"] == []
    assert "no comparison" in finished["phase"]
    corrected = next(item for item in json.loads(path.read_text())["disclosures"] if item["disclosure_id"] == row["disclosure_id"])
    assert corrected["taxonomy"] == "Operations & Capacity"


def test_unrelated_review_does_not_cancel_other_required_comparisons(pipeline):
    case = pipeline
    row = json.loads((case.root / "disclosures/amd/2024/disclosures.json").read_text())["disclosures"][0]
    view = {**row, "fiscal_year": 2024}
    saved = review.save_disclosure_review(view, "Operations & Capacity", "", expected=None)
    queue, job = queue_job(case, "disclosure", row, saved)
    runner = review_refresh.Refresh(job, queue / "test-run", lambda **values: None)
    other = json.loads((case.root / "disclosures/amd/2023/disclosures.json").read_text())["disclosures"][0]
    review.save_disclosure_review({**other, "fiscal_year": 2023}, "Technology & AI", "Another filing.", expected=None)
    runner.check_current()
    review.save_disclosure_review(view, "Technology & AI", "Replaced this review.", expected=saved)
    with pytest.raises(review_refresh.SupersededReview):
        runner.check_current()
