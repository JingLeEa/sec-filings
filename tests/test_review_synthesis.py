"""Manual decisions refresh synthesis without stale findings or stale labels."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from sec_disclosure.agents import synthesis
from sec_disclosure.pipelines import review_jobs, review_refresh
from sec_disclosure.review_overrides import StaleReview
from sec_ui import reports


def synthesis_options(root):
    return dict(env_file=None, data_dir=root, limit_per_pair=500, max_changes=50,
                finding_target=None, finding_batch_size=10, finding_workers=3,
                timeout=180, max_tokens=7000, allow_empty=True)


def empty_database(monkeypatch):
    connections = [MagicMock(), MagicMock()]
    for connection in connections:
        connection.__enter__.return_value = connection
    monkeypatch.setattr(synthesis, "load_config", lambda *_: SimpleNamespace())
    monkeypatch.setattr(synthesis, "synthesis_connection", Mock(side_effect=connections))
    monkeypatch.setattr(synthesis, "fetch_change_evidence", Mock(return_value=[]))
    monkeypatch.setattr(synthesis, "fetch_financial_metrics", Mock(return_value=[]))
    calls = Mock(side_effect=AssertionError("Empty synthesis must not call a model"))
    monkeypatch.setattr(synthesis.SynthesisCalls, "complete", calls)
    save = Mock()
    monkeypatch.setattr(synthesis, "save_synthesis_report", save)
    return connections, save, calls


def test_last_material_change_removed_replaces_stale_findings_without_model_calls(monkeypatch, tmp_path):
    connections, save, calls = empty_database(monkeypatch)
    path = tmp_path / "synthesis/AMD_2023_2024_synthesis_output.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"report": {"key_findings": [{"title": "Stale finding"}]}}))
    guard = Mock()
    result = synthesis.run_from_database("AMD", [2023, 2024], before_save=guard, **synthesis_options(tmp_path))
    document = json.loads(result.read_text())
    assert document["report"]["key_findings"] == []
    assert document["validation"]["valid"] is True
    assert document["coverage_summary"]["selected_material_changes"] == 0
    assert document["token_usage"]["attempted_requests"] == 0
    assert json.loads(save.call_args.args[3]) == document
    guard.assert_called_once_with(connections[1])
    calls.assert_not_called()


def test_synthesis_revision_guard_rejects_publication_and_keeps_previous_report(monkeypatch, tmp_path):
    _, save, _ = empty_database(monkeypatch)
    path = tmp_path / "synthesis/AMD_2023_2024_synthesis_output.json"
    path.parent.mkdir()
    path.write_text('{"previous":true}')
    with pytest.raises(StaleReview, match="changed"):
        synthesis.run_from_database("AMD", [2023, 2024],
            before_save=Mock(side_effect=StaleReview("Inputs changed")), **synthesis_options(tmp_path))
    save.assert_not_called()
    assert json.loads(path.read_text()) == {"previous": True}


def test_existing_synthesis_scopes_only_include_affected_comparisons(monkeypatch):
    import psycopg
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("synthesis_reports",)
    cursor.fetchall.return_value = [([2022, 2023],), ([2023, 2024, 2025],), ([2024, 2025],)]
    monkeypatch.setattr(psycopg, "connect", Mock(return_value=connection))
    refresh = object.__new__(review_refresh.Refresh)
    refresh.env_file, refresh.company, refresh.pairs = None, "amd", [(2023, 2024)]
    assert refresh.synthesis_scopes([2022, 2023, 2024, 2025]) == [(2023, 2024, 2025)]


def test_busy_company_pipeline_defers_review_and_next_worker_retries(tmp_path):
    job = review_jobs.enqueue(tmp_path, stage="alignment", company="amd", previous=2023, current=2024,
        record_id="amd_M001", saved={"reviewed_at": "now"}, settings={"input_source": "files"})
    review_jobs.process_queue(tmp_path, runner=Mock(side_effect=review_refresh.PipelineBusy("Pipeline active")))
    queued = review_jobs.jobs(tmp_path)[0]
    assert queued["status"] == "queued" and queued["error"] is None
    assert queued["review"] == job["review"]
    review_jobs.process_queue(tmp_path, runner=lambda job, output, update: None)
    assert review_jobs.jobs(tmp_path)[0]["status"] == "complete"


def test_background_worker_waits_for_company_pipeline_without_losing_review(monkeypatch, tmp_path):
    review_jobs.enqueue(tmp_path, stage="alignment", company="amd", previous=2023, current=2024,
        record_id="amd_M001", saved={"reviewed_at": "now"}, settings={"input_source": "files"})
    sleep = Mock()
    monkeypatch.setattr(review_jobs.time, "sleep", sleep)
    runner = Mock(side_effect=[review_refresh.PipelineBusy("Pipeline active"), None])
    review_jobs.process_queue(tmp_path, runner=runner, wait_for_pipeline=True)
    assert review_jobs.jobs(tmp_path)[0]["status"] == "complete"
    assert runner.call_count == 2
    sleep.assert_called_once_with(3)


def test_synthesis_report_loader_reads_new_database_results_each_time(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "postgres")
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(tmp_path))
    document = {"company": "AMD", "years": [2023, 2024], "report": {"key_findings": [],
                "executive_summary": "Updated after review"}, "source_catalog": {}}
    (tmp_path / "old.json").write_text(json.dumps({**document, "report": {"executive_summary": "Stale file"}}))
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [("synthesis_reports",), (deepcopy(document),),
                                  ({**deepcopy(document), "report": {"executive_summary": "Second review"}},)]
    cursor.fetchall.return_value = [("AMD", [2023, 2024])]
    monkeypatch.setattr(reports, "_postgres_connection", Mock(return_value=connection))
    entries = reports.list_synthesis_files()
    assert len(entries) == 1 and isinstance(entries[0], reports.DatabaseSynthesis)
    assert reports.load_synthesis(entries[0])["report"]["executive_summary"] == "Updated after review"
    assert reports.load_synthesis(entries[0])["report"]["executive_summary"] == "Second review"
