"""Peer UI readiness, safe process launch, and report rendering without APIs."""
import importlib.util
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sec_ui import data, peer_runs, pipeline_runs, reports
from sec_disclosure.agents import peer_comparison as agent
from sec_disclosure.storage import peer_comparison as storage
from test_peer_comparison_pipeline import inputs


class PeerRunUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for context in (patch.object(pipeline_runs, "ROOT", self.root),
                        patch.object(pipeline_runs, "active_run", return_value=None),
                        patch.object(agent, "dotenv_values", return_value={})):
            context.start()
            self.addCleanup(context.stop)
        self.value = peer_runs.selection("AAA", ["BBB"], 2024, ["1A"], "all")

    def test_readiness_uses_backend_evidence_checks_without_model_requests(self):
        with patch.object(storage, "read_peer_inputs", return_value=inputs()) as read, \
             patch.object(agent, "request_completion") as model:
            ready = peer_runs.check_inputs(self.value)
        self.assertEqual(ready, {"target_disclosures": 1, "all_target_disclosures": 1,
                                 "peer_disclosures": {"BBB": 1}})
        self.assertEqual(read.call_args.args, ("aaa", ["bbb"], 2024))
        model.assert_not_called()
        self.assertFalse((self.root / "data").exists())

    def test_material_mode_requires_material_target_and_all_mode_does_not(self):
        material = {**self.value, "target_selection": "material_changes"}
        records = inputs()
        with patch.object(storage, "read_peer_inputs", return_value=records), \
             self.assertRaisesRegex(peer_runs.RunError, "materiality"):
            peer_runs.check_inputs(material)
        records["alignments"]["aaa"][("2023", "2024", "alignments.json")] = {
            "company": "aaa", "previous_year": "2023", "current_year": "2024", "run_complete": True,
            "alignments": [{"previous_ids": [], "current_ids": ["aaa_2024_1A_D001"],
                            "materiality_analysis": {"materiality": "Yes"}}],
        }
        with patch.object(storage, "read_peer_inputs", return_value=records):
            self.assertEqual(peer_runs.check_inputs(material)["target_disclosures"], 1)

    def test_missing_inputs_block_launch_before_any_files_or_process(self):
        with patch.object(storage, "read_peer_inputs", side_effect=storage.PeerInputError("BBB missing")), \
             patch.object(peer_runs.subprocess, "Popen") as spawn, \
             self.assertRaisesRegex(peer_runs.RunError, "BBB missing"):
            peer_runs.start_run(self.value)
        spawn.assert_not_called()
        self.assertFalse((self.root / "data/peer_runs").exists())

    def test_launch_rechecks_inputs_and_invokes_only_peer_pipeline(self):
        process = SimpleNamespace(pid=12345, wait=lambda: 0)
        with patch.object(peer_runs, "check_inputs", return_value={"target_disclosures": 1}) as check, \
             patch.object(peer_runs.subprocess, "Popen", return_value=process) as spawn, \
             patch.object(peer_runs, "Thread") as thread:
            job = peer_runs.start_run(self.value)
            check.assert_called_once_with(self.value)
            command = spawn.call_args.args[0]
            self.assertEqual(Path(command[2]).name, "run_peer_comparison_pipeline.py")
            self.assertEqual(command[3:], peer_runs.command_arguments(self.value))
            self.assertNotIn("--save-local-output", command)
            self.assertEqual(spawn.call_args.kwargs["cwd"], self.root)
            self.assertEqual(job["status"], "running")
            with patch.object(pipeline_runs, "_pid_alive", return_value=True), self.assertRaises(peer_runs.RunError):
                peer_runs.start_run(self.value)
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        self.assertEqual(peer_runs.read_run(self.value)["status"], "complete")

    def test_active_main_pipeline_blocks_peer_launch(self):
        with patch.object(pipeline_runs, "active_run", return_value={"ticker": "aaa"}), \
             patch.object(peer_runs, "check_inputs") as check, self.assertRaisesRegex(peer_runs.RunError, "main pipeline"):
            peer_runs.start_run(self.value)
        check.assert_not_called()

    def test_invalid_and_changed_selections_cannot_reuse_the_same_check(self):
        for peers in ([], ["AAA"], ["bbb", "BBB"], ["../BBB"]):
            with self.subTest(peers=peers), self.assertRaises(peer_runs.RunError):
                peer_runs.selection("AAA", peers, 2024)
        original = peer_runs.selection_key(self.value)
        for changed in ({**self.value, "year": 2025}, {**self.value, "peers": ["CCC"]},
                        {**self.value, "items": ["7"]}, {**self.value, "target_selection": "material_changes"}):
            self.assertNotEqual(original, peer_runs.selection_key(changed))

    def test_database_report_lookup_keeps_peer_sets_items_and_selection_separate(self):
        report = {"comparisons": []}
        with patch.object(peer_runs, "_query", return_value=[(report,)]) as query:
            self.assertIs(peer_runs.load_report(self.value), report)
        self.assertEqual(query.call_args.args[2], ("AAA", 2024, ["BBB"], ["1A"], "all"))

    def test_progress_filters_current_attempt_and_ignores_other_selection(self):
        wrong = {"target_selection": "material_changes", "items": ["1A"], "jobs_completed": 99}
        current = {"target_selection": "all", "items": ["1A"], "jobs_completed": 2, "jobs_planned": 5}
        job = {**self.value, "started_at": "2026-10-31T10:00:00+00:00"}
        with patch.object(peer_runs, "_query", return_value=[(json.dumps(row).encode(),) for row in (wrong, current)]) as query:
            self.assertEqual(peer_runs.progress(job)["jobs_completed"], 2)
        self.assertIn("modified_at >=", query.call_args.args[1])
        self.assertEqual(query.call_args.args[2][2], job["started_at"])

    def test_query_failures_never_expose_connection_secrets(self):
        with patch.object(data, "_postgres_connection", side_effect=RuntimeError("password=secret")), \
             self.assertRaises(peer_runs.RunError) as raised:
            peer_runs.saved_reports()
        self.assertNotIn("secret", str(raised.exception))

    def test_local_exports_with_a_peer_set_subfolder_remain_viewable(self):
        report = self.root / "aaa/2024/bbb/peer_comparison.json"
        report.parent.mkdir(parents=True)
        report.write_text('{"comparisons": []}')
        with patch.object(reports.config, "peer_dir", return_value=self.root):
            self.assertEqual(reports.list_peer_files(), [report])


@unittest.skipUnless(importlib.util.find_spec("streamlit"), "Streamlit is not installed")
class PeerPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        fixture = Path(__file__).parent / "fixtures/peer_comparison"
        for context in (
            patch.object(pipeline_runs, "ROOT", Path(self.temp.name)),
            patch.object(pipeline_runs, "active_run", return_value=None),
            patch.object(peer_runs, "active_run", return_value=None),
            patch.object(peer_runs, "read_run", return_value=None),
            patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "files", "SEC_UI_PEER_DIR": str(fixture)}),
        ):
            context.start()
            self.addCleanup(context.stop)

    def page(self):
        from streamlit.testing.v1 import AppTest
        root = Path(__file__).resolve().parents[1]
        # Use the real multipage entrypoint so navigation links are registered.
        with patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "files",
                                     "SEC_UI_ALIGNMENTS_DIR": str(root / "tests/fixtures/alignments")}):
            page = AppTest.from_file(str(root / "app/Home.py"), default_timeout=20).run()
        page.switch_page("pages/4_Peer_Comparison.py").run()
        self.assertFalse(page.exception, [error.value for error in page.exception])
        return page

    def button(self, page, label):
        return next(button for button in page.button if button.label == label)

    def test_existing_reports_still_render_and_start_requires_readiness(self):
        page = self.page()
        self.assertTrue(self.button(page, "Start peer comparison").disabled)
        self.assertTrue(any("Prepare the target company and every peer" in info.value for info in page.info))
        self.assertTrue(any(link.label == "Open Run pipeline" for link in page.get("page_link")))
        metrics = {metric.label: metric.value for metric in page.metric}
        self.assertEqual(metrics["Peer comparisons made"], "3")
        self.assertTrue(any("Sample data" in row.value for row in page.warning))

    def test_missing_inputs_show_warning_and_cannot_launch(self):
        with patch.object(peer_runs, "check_inputs", side_effect=peer_runs.RunError("No saved INTC disclosures for 2024")), \
             patch.object(peer_runs, "start_run") as start:
            page = self.page()
            self.button(page, "Check prerequisites").click().run()
        self.assertTrue(any("INTC" in row.value for row in page.warning))
        self.assertTrue(self.button(page, "Start peer comparison").disabled)
        start.assert_not_called()

    def test_ready_selection_launches_and_edits_require_a_new_check(self):
        summary = {"target_disclosures": 12, "peer_disclosures": {"AMD": 10}}
        with patch.object(peer_runs, "check_inputs", return_value=summary), patch.object(peer_runs, "start_run") as start:
            page = self.page()
            self.button(page, "Check prerequisites").click().run()
            self.assertFalse(self.button(page, "Start peer comparison").disabled)
            self.button(page, "Start peer comparison").click().run()
            self.assertEqual(start.call_args.args[0]["ticker"], "nvda")
            self.assertEqual(start.call_args.args[0]["target_selection"], "material_changes")
            next(field for field in page.text_input if field.label == "Peer tickers").set_value("AMD").run()
            self.assertTrue(self.button(page, "Start peer comparison").disabled)

    def test_no_local_report_is_required_to_run_a_comparison(self):
        with patch.object(reports, "list_peer_files", return_value=[]):
            page = self.page()
        self.assertTrue(any("No peer comparison output" in row.value for row in page.info))
        self.assertIsNotNone(self.button(page, "Check prerequisites"))

    def test_saved_postgres_report_is_displayed_without_local_export(self):
        fixture = Path(__file__).parent / "fixtures/peer_comparison/NVDA/2024/peer_comparison.json"
        report = json.loads(fixture.read_text())
        reference = dict(ticker="NVDA", year=2024, peers=["AMD"], items=["1A"], target_selection="all")
        with patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "postgres"}), \
             patch.object(reports, "list_peer_files", return_value=[]), \
             patch.object(peer_runs, "saved_reports", return_value=[reference]), \
             patch.object(peer_runs, "load_report", return_value=report) as load:
            page = self.page()
        load.assert_called_once_with(reference)
        self.assertTrue(any("Report source: PostgreSQL" in row.value for row in page.caption))
        self.assertEqual(next(metric.value for metric in page.metric if metric.label == "Target disclosures"), "2")

    def test_running_comparison_shows_saved_counts_and_blocks_launch(self):
        job = {**peer_runs.selection("NVDA", ["AMD", "AVGO", "INTC"], 2024),
               "status": "running", "run_id": "running", "started_at": "2026-10-31T10:00:00+00:00"}
        with patch.object(peer_runs, "read_run", return_value=job), \
             patch.object(peer_runs, "progress", return_value={"jobs_completed": 3, "jobs_planned": 10}), \
             patch.object(peer_runs, "log_tail", return_value="Comparing saved disclosures"):
            page = self.page()
        self.assertTrue(any("3 of 10 target disclosures compared" in row.text for row in page.get("progress")))
        self.assertTrue(self.button(page, "Start peer comparison").disabled)
        self.assertTrue(any(status.label == "Peer comparison running" for status in page.get("status")))

    def test_partial_run_requires_new_readiness_check_before_resume(self):
        job = {**peer_runs.selection("NVDA", ["AMD", "AVGO", "INTC"], 2024), "status": "stopped", "run_id": "partial"}
        with patch.object(peer_runs, "read_run", return_value=job), \
             patch.object(peer_runs, "log_tail", return_value="Partial report saved"), \
             patch.object(peer_runs, "check_inputs", return_value={"target_disclosures": 12}):
            page = self.page()
            self.assertTrue(self.button(page, "Resume peer comparison").disabled)
            self.button(page, "Check prerequisites").click().run()
        self.assertFalse(self.button(page, "Resume peer comparison").disabled)
        self.assertTrue(any("stopped or is incomplete" in row.value for row in page.warning))

    def test_completion_refreshes_and_selects_the_new_database_report(self):
        value = peer_runs.selection("NVDA", ["AMD", "AVGO", "INTC"], 2024)
        job = {**value, "status": "complete", "run_id": "complete"}
        report = json.loads((Path(__file__).parent / "fixtures/peer_comparison/NVDA/2024/peer_comparison.json").read_text())
        old = dict(ticker="NVDA", year=2024, peers=["AMD"], items=["1A"], target_selection="all")
        with patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "postgres"}), \
             patch.object(peer_runs, "read_run", return_value=job), \
             patch.object(peer_runs, "log_tail", return_value=""), \
             patch.object(peer_runs, "saved_reports", return_value=[old, value]), \
             patch.object(peer_runs, "load_report", return_value=report) as load:
            page = self.page()
        self.assertEqual(load.call_args.args[0], value)
        self.assertTrue(any("Peer comparison complete" in row.value for row in page.success))


if __name__ == "__main__":
    unittest.main()
