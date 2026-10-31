"""Backend launch and progress mapping for the dashboard, without external requests."""
import json
import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sec_ui import pipeline_runs


class PipelineRunUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root_patch = patch.object(pipeline_runs, "ROOT", Path(self.temp.name))
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.postgres_patch = patch.object(pipeline_runs, "_postgres_documents", return_value={})
        self.postgres_patch.start()
        self.addCleanup(self.postgres_patch.stop)

    def test_database_progress_only_marks_completed_save_steps(self):
        progress = {"stages": [
            {"stage": "save_text_2023", "phase": "preprocessing", "status": "complete"},
            {"stage": "text_2024", "phase": "preprocessing", "status": "running"},
            {"stage": "align_and_save_2023_2024", "phase": "alignment", "status": "complete"},
            {"stage": "align_and_save_2024_2025", "phase": "alignment", "status": "running"},
            {"stage": "llm_changes_2023_2024", "phase": "change-analysis", "status": "complete"},
        ]}
        phases = {phase["phase"]: phase for phase in pipeline_runs.phase_progress([2023, 2024, 2025], progress)}
        self.assertEqual((phases["preprocessing"]["saved"], phases["preprocessing"]["total"]), (1, 6))
        self.assertEqual(phases["preprocessing"]["status"], "running")
        self.assertEqual(phases["alignment"]["saved"], 1)
        self.assertEqual(phases["alignment"]["status"], "running")
        self.assertEqual((phases["change-analysis"]["saved"], phases["change-analysis"]["total"]), (1, 4))
        self.assertEqual(phases["materiality"]["saved"], 0)

    def test_extraction_progress_counts_batches_and_followup_checks(self):
        folder = pipeline_runs.ROOT / "data/disclosures/unh/2023"
        folder.mkdir(parents=True)
        (folder / "token_usage.json").write_text(json.dumps({
            "planned_batches": 10, "pending_batches": ["b8", "b9", "b10"], "failed_batches": [],
            "planned_boundary_checks": 2, "pending_boundary_checks": ["c1", "c2"],
            "planned_consolidations": 1, "pending_consolidations": ["d1"],
        }))
        rows = pipeline_runs.stage_progress("UNH", [2023, 2024], "extract_disclosures_and_save")
        self.assertEqual((rows[0]["done"], rows[0]["total"]), (7, 13))
        self.assertIn("7/10 batches", rows[0]["detail"])
        self.assertIsNone(rows[1]["total"])

    def test_alignment_progress_moves_from_matching_to_verification(self):
        folder = pipeline_runs.ROOT / "data/alignments/unh/2023-2024"
        jobs = folder / "jobs"
        jobs.mkdir(parents=True)
        (folder / "candidate_matches.json").write_text(json.dumps({str(i): [] for i in range(5)}))
        (jobs / "matching_001.json").write_text(json.dumps({"anchors": ["0", "1"]}))
        row = pipeline_runs.stage_progress("unh", [2023, 2024], "align_and_save_2023_2024")[0]
        self.assertEqual((row["done"], row["total"]), (2, 5))
        (jobs / "matching_002.json").write_text(json.dumps({"anchors": ["2", "3", "4"]}))
        (folder / "proposed_groups.json").write_text(json.dumps([{"group_id": i} for i in range(3)]))
        (jobs / "verification_001.json").write_text(json.dumps({
            "groups": [{"group_id": 0}], "result": [{}], "repair_pending": False,
        }))
        row = pipeline_runs.stage_progress("unh", [2023, 2024], "align_and_save_2023_2024")[0]
        self.assertEqual((row["done"], row["total"]), (1, 3))
        self.assertIn("Verified 1/3", row["detail"])

    def test_change_analysis_and_materiality_progress_use_saved_row_counts(self):
        folder = pipeline_runs.ROOT / "data/alignments/unh/2023-2024"
        (folder / "materiality").mkdir(parents=True)
        (folder / "alignments.json").write_text(json.dumps({
            "change_analysis_summary": {"eligible_alignments": 20,
                                        "by_status": {"completed": 12, "needs_review": 1}},
        }))
        (folder / "materiality/summary.json").write_text(json.dumps({
            "alignment_rows": 25, "classified_rows": 9,
        }))
        change = pipeline_runs.stage_progress("unh", [2023, 2024], "llm_changes_2023_2024")[0]
        materiality = pipeline_runs.stage_progress("unh", [2023, 2024], "materiality_2023_2024")[0]
        self.assertEqual((change["done"], change["total"]), (13, 20))
        self.assertEqual((materiality["done"], materiality["total"]), (9, 25))

    def test_start_launches_runner_and_records_exit_without_running_it_in_process(self):
        process = SimpleNamespace(pid=12345, wait=lambda: 0)
        with patch.object(pipeline_runs.subprocess, "Popen", return_value=process) as popen, \
             patch.object(pipeline_runs, "Thread") as thread:
            job = pipeline_runs.start_run("AMD", [2025, 2023, 2024])
            command = popen.call_args.args[0]
            self.assertEqual(command[1:3], ["-u", str(pipeline_runs.ROOT / "scripts/run_full_pipeline.py")])
            self.assertEqual(command[command.index("--years") + 1:command.index("--data-dir")],
                             ["2023", "2024", "2025"])
            self.assertEqual(popen.call_args.kwargs["cwd"], pipeline_runs.ROOT)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(job["status"], "running")
            with patch.object(pipeline_runs, "_pid_alive", return_value=True), self.assertRaises(pipeline_runs.RunError):
                pipeline_runs.start_run("amd", [2023, 2024, 2025])
            with patch.object(pipeline_runs, "_pid_alive", return_value=True), self.assertRaisesRegex(
                    pipeline_runs.RunError, "AMD is already running"):
                pipeline_runs.start_run("unh", [2023, 2024])
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])

        saved, progress = pipeline_runs.read_run("AMD", [2023, 2024, 2025])
        self.assertIsNone(progress)
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["exit_code"], 0)

    def test_start_prefers_project_virtual_environment(self):
        python = pipeline_runs.ROOT / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("")
        with patch.object(pipeline_runs.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)) as popen, \
             patch.object(pipeline_runs, "Thread"):
            pipeline_runs.start_run("unh", [2023, 2024])
        self.assertEqual(popen.call_args.args[0][0], str(python))

    def test_stopped_preflight_is_visible_without_status_file(self):
        process = SimpleNamespace(pid=12345, wait=lambda: 1)
        with patch.object(pipeline_runs.subprocess, "Popen", return_value=process), \
             patch.object(pipeline_runs, "Thread") as thread:
            pipeline_runs.start_run("NVDA", [2023, 2024])
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        job, progress = pipeline_runs.read_run("NVDA", [2023, 2024])
        self.assertEqual(job["status"], "stopped")
        self.assertIsNone(progress)

    def test_old_runner_status_is_not_shown_as_new_attempt(self):
        folder = pipeline_runs.run_folder("amd", [2023, 2024])
        folder.mkdir(parents=True)
        (folder / "ui_run.json").write_text(json.dumps({
            "run_id": "new", "status": "stopped", "started_at": "2026-10-29T12:00:00+00:00"
        }))
        (folder / "status.json").write_text(json.dumps({
            "last_invocation_at": "2026-10-28T12:00:00+00:00", "complete": True, "stages": []
        }))
        _, progress = pipeline_runs.read_run("amd", [2023, 2024])
        self.assertIsNone(progress)

    def test_invalid_selection_cannot_form_a_run_path(self):
        for ticker, years in (("A/B", [2023, 2024]), ("AMD", [2024]), ("AMD", [1899, 2025])):
            with self.subTest(ticker=ticker, years=years), self.assertRaises(pipeline_runs.RunError):
                pipeline_runs.run_folder(ticker, years)

    def test_active_run_ignores_stale_job(self):
        folder = pipeline_runs.run_folder("amd", [2023, 2024])
        folder.mkdir(parents=True)
        (folder / "ui_run.json").write_text(json.dumps({
            "ticker": "amd", "years": [2023, 2024], "status": "running", "pid": 99999999
        }))
        with patch.object(pipeline_runs, "_pid_alive", return_value=False):
            self.assertIsNone(pipeline_runs.active_run())
            with patch.object(pipeline_runs.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)), \
                 patch.object(pipeline_runs, "Thread"):
                self.assertEqual(pipeline_runs.start_run("nvda", [2023, 2024])["status"], "running")

    def test_database_progress_takes_precedence_over_stale_local_status(self):
        folder = pipeline_runs.run_folder("amd", [2023, 2024])
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps({"full_pipeline_complete": True, "stages": []}))
        current = {"stages": [{"stage": "text_2023", "phase": "preprocessing", "status": "running"}]}
        key = pipeline_runs._status_key("amd", [2023, 2024])
        with patch.object(pipeline_runs, "_postgres_documents", return_value={key: current}):
            _, progress = pipeline_runs.read_run("amd", [2023, 2024])
        self.assertEqual(progress, current)

    def test_database_checkpoints_do_not_read_stale_local_batches(self):
        folder = pipeline_runs.ROOT / "data/disclosures/amd/2023"
        folder.mkdir(parents=True)
        (folder / "token_usage.json").write_text(json.dumps({"planned_batches": 99, "run_complete": True}))
        status = pipeline_runs._status_key("amd", [2023, 2024])
        with patch.object(pipeline_runs, "_postgres_documents", return_value={status: {"stages": []}}):
            rows = pipeline_runs.stage_progress("amd", [2023, 2024], "extract_disclosures_and_save")
        self.assertIsNone(rows[0]["total"])
        with patch.object(pipeline_runs, "_postgres_documents", return_value={
            status: {"stages": []},
            "disclosures/amd/2023/token_usage.json": {
                "planned_batches": 10, "pending_batches": ["b9", "b10"], "failed_batches": [],
            },
        }):
            rows = pipeline_runs.stage_progress("amd", [2023, 2024], "extract_disclosures_and_save")
        self.assertEqual((rows[0]["done"], rows[0]["total"]), (8, 10))

    def test_database_alignment_jobs_are_counted_without_local_files(self):
        status = pipeline_runs._status_key("amd", [2023, 2024])
        prefix = "alignments/amd/2023-2024"
        documents = {
            status: {"stages": []},
            f"{prefix}/candidate_matches.json": {str(i): [] for i in range(5)},
            f"{prefix}/jobs/matching_001.json": {"anchors": ["0", "1"]},
        }
        with patch.object(pipeline_runs, "_postgres_documents", return_value=documents):
            row = pipeline_runs.stage_progress("amd", [2023, 2024], "align_and_save_2023_2024")[0]
        self.assertEqual((row["done"], row["total"]), (2, 5))
        documents[f"{prefix}/jobs/matching_002.json"] = {"anchors": ["2", "3", "4"]}
        documents[f"{prefix}/proposed_groups.json"] = [{"group_id": i} for i in range(3)]
        documents[f"{prefix}/jobs/verification_001.json"] = {"groups": [{}], "result": [{}]}
        with patch.object(pipeline_runs, "_postgres_documents", return_value=documents):
            row = pipeline_runs.stage_progress("amd", [2023, 2024], "align_and_save_2023_2024")[0]
        self.assertEqual((row["done"], row["total"]), (1, 3))

    def test_table_progress_waits_for_each_years_database_save(self):
        progress = {"stages": [{"stage": "api_tables_and_save", "phase": "preprocessing", "status": "complete"}]}
        phase = pipeline_runs.phase_progress([2023, 2024], progress)[0]
        self.assertEqual((phase["saved"], phase["total"]), (0, 4))
        progress["stages"].append({"stage": "save_xbrl_2023", "phase": "preprocessing", "status": "complete"})
        phase = pipeline_runs.phase_progress([2023, 2024], progress)[0]
        self.assertEqual((phase["saved"], phase["total"]), (1, 4))

    def test_synthesis_is_running_until_report_has_been_saved(self):
        progress = {"stages": [{"stage": "synthesis_and_save", "phase": "synthesis", "status": "running"}]}
        phase = pipeline_runs.phase_progress([2023, 2024], progress)[-1]
        self.assertEqual((phase["phase"], phase["status"], phase["saved"]), ("synthesis", "running", 0))
        progress["stages"][0]["status"] = "complete"
        phase = pipeline_runs.phase_progress([2023, 2024], progress)[-1]
        self.assertEqual((phase["status"], phase["saved"]), ("complete", 1))

    def test_database_progress_query_is_read_only_and_limited_to_checkpoints(self):
        from sec_ui import data

        self.postgres_patch.stop()
        key = pipeline_runs._status_key("amd", [2023, 2024])
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("pipeline_artifacts",)
        cursor.fetchall.return_value = [(key, json.dumps({"stages": []}).encode())]
        with patch.object(data, "_postgres_connection", return_value=connection):
            documents = pipeline_runs._postgres_documents([key], "alignments/amd/2023-2024/jobs/")
        self.assertTrue(connection.read_only)
        self.assertEqual(documents, {key: {"stages": []}})
        self.assertEqual(cursor.execute.call_args.args[1],
                         ("main", [key], "alignments/amd/2023-2024/jobs/"))
        self.assertIn("(matching|verification)", cursor.execute.call_args.args[0])

    def test_invalid_database_status_has_a_readable_error(self):
        from sec_ui import data

        self.postgres_patch.stop()
        key = pipeline_runs._status_key("amd", [2023, 2024])
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = ("pipeline_artifacts",)
        cursor.fetchall.return_value = [(key, b'{invalid')]
        with patch.object(data, "_postgres_connection", return_value=connection), \
             self.assertRaisesRegex(pipeline_runs.RunError, "Invalid pipeline progress"):
            pipeline_runs._postgres_documents([key])


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Streamlit is not installed")
class RunPipelinePageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for context in (
            patch.object(pipeline_runs, "ROOT", Path(self.temp.name)),
            patch.object(pipeline_runs, "_postgres_documents", return_value={}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def page(self):
        from streamlit.testing.v1 import AppTest

        path = Path(__file__).resolve().parents[1] / "app/pages/0_Run_Pipeline.py"
        page = AppTest.from_file(str(path), default_timeout=20).run()
        self.assertFalse(page.exception, [error.value for error in page.exception])
        return page

    def test_download_retry_remains_running_and_recovery_is_explained(self):
        progress = {"stages": [{"stage": "text_2023", "phase": "preprocessing", "status": "running"}]}
        with patch.object(pipeline_runs, "read_run", return_value=({"status": "running"}, progress)), \
             patch.object(pipeline_runs, "log_tail", return_value="SEC download timed out; retrying extraction with 180s timeout."):
            page = self.page()
        self.assertFalse(page.error)
        self.assertTrue(any("Missing sections are checked with AI" in message.value for message in page.info))
        self.assertTrue(any("retrying extraction with 180s" in code.value for code in page.code))
        self.assertTrue(any(status.state == "running" for status in page.get("status")))

    def test_synthesis_keeps_the_page_at_five_of_six_until_saved(self):
        years = [2023, 2024, 2025]
        progress = {"stages": [
            {"stage": name, "phase": phase, "status": "complete"}
            for phase, saves in pipeline_runs.database_saves(years).items()
            if phase != "synthesis" for _, name in saves
        ] + [{"stage": "synthesis_and_save", "phase": "synthesis", "status": "running"}]}
        with patch.object(pipeline_runs, "read_run", return_value=({"status": "running"}, progress)):
            page = self.page()
        self.assertFalse(page.success)
        self.assertTrue(any("Preparing the synthesis report" in message.value for message in page.info))
        self.assertTrue(any("5 of 6 stages complete" in row.text for row in page.get("progress")))

    def test_completed_database_run_shows_all_six_stages(self):
        years = [2023, 2024, 2025]
        status = {"full_pipeline_complete": True, "stages": [
            {"stage": name, "phase": phase, "status": "complete"}
            for phase, saves in pipeline_runs.database_saves(years).items() for _, name in saves
        ]}
        key = pipeline_runs._status_key("amd", years)
        with patch.object(pipeline_runs, "_postgres_documents", return_value={key: status}):
            page = self.page()
        self.assertTrue(any("All 6 stages" in message.value for message in page.success))
        self.assertTrue(any("6 of 6 stages complete" in row.text for row in page.get("progress")))
        self.assertTrue(any("6 of 6 outputs loaded" in row.value for row in page.caption))

    def test_exhausted_download_retries_show_stopped_and_resume(self):
        progress = {"stages": [{"stage": "text_2023", "phase": "preprocessing", "status": "stopped", "exit_code": 75}]}
        job = {"status": "stopped", "run_id": "timeout", "exit_code": 75}
        with patch.object(pipeline_runs, "read_run", return_value=(job, progress)):
            page = self.page()
        self.assertTrue(any("Pipeline stopped" in message.value for message in page.error))
        self.assertTrue(any(button.label == "Resume pipeline" for button in page.button))
        self.assertFalse(page.success)


if __name__ == "__main__":
    unittest.main()
