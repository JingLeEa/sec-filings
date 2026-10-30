"""Synthesis ordering, PostgreSQL failure propagation, and pipeline resume."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sec_disclosure.pipelines import run_full_pipeline as pipeline
from artifact_support import MemoryArtifactStore, workspace_factory


class TestSynthesisIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.arguments = ["--ticker", "AMD", "--years", "2023", "2024", "2025",
                          "--data-dir", str(self.root)]
        self.artifacts = MemoryArtifactStore()
        workspace = patch.object(pipeline, 'PostgresWorkspace', side_effect=workspace_factory(self.artifacts))
        workspace.start()
        self.addCleanup(workspace.stop)
        self.progress = self.artifacts.path("pipeline_runs/amd/2023-2024-2025/status.json")
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.dotenv = patch.object(pipeline, "dotenv_values", return_value={})
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)

    def run_pipeline(self, extra=(), *, synthesis_exit_code=0, failed_stage=None):
        executed = []
        execute = pipeline.execute_stage

        def run_stage(stage, **kwargs):
            executed.append(stage.name)
            if stage.name == failed_stage:
                return 1
            return execute(stage, **kwargs) if stage.phase == "synthesis" else 0

        with patch.object(pipeline, "preflight") as preflight, \
             patch.object(pipeline, "execute_stage", side_effect=run_stage), \
             patch.object(pipeline, "run_script",
                          return_value=SimpleNamespace(returncode=synthesis_exit_code)) as subprocess_run, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = pipeline.main(self.arguments + list(extra))
        return code, executed, subprocess_run, preflight, output.getvalue()

    def state(self):
        return json.loads(self.progress.read_text())

    def test_default_run_ends_with_synthesis_after_materiality_saves_and_excludes_peers(self):
        args = pipeline.parse_args(self.arguments)
        stages = pipeline.build_stages(args)
        self.assertEqual(args.until, "synthesis")
        self.assertEqual(len(stages), 22)
        self.assertEqual(stages[-1].name, "synthesis_and_save")
        self.assertFalse(any("peer" in stage.script for stage in stages))
        self.assertFalse(any(stage.script == "run_aligned_change_analysis.py" for stage in stages))

        code, executed, run, _, _ = self.run_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed[-1], "synthesis_and_save")
        for pair in ("2023_2024", "2024_2025"):
            self.assertLess(executed.index("save_materiality_" + pair), executed.index("synthesis_and_save"))
        run.assert_called_once()
        state = self.state()
        self.assertTrue(state["complete"])
        self.assertTrue(state["full_pipeline_complete"])
        self.assertEqual(state["synthesis_status"], "complete")
        self.assertEqual(state["stages"][-1]["phase"], "synthesis")
        self.assertEqual(state["synthesis_concurrency"], {
            "finding_workers": 18, "finding_batch_size": 10, "max_changes": 50,
            "max_concurrent_requests": 5, "final_report_workers": 1,
        })

    def test_synthesis_save_failure_stops_completion_and_resume_runs_only_synthesis(self):
        code, _, run, _, _ = self.run_pipeline(synthesis_exit_code=1)
        self.assertEqual(code, 1)
        run.assert_called_once()
        state = self.state()
        self.assertFalse(state["complete"])
        self.assertFalse(state["full_pipeline_complete"])
        self.assertEqual(state["synthesis_status"], "failed")
        self.assertEqual(state["stages"][-1]["status"], "stopped")

        code, executed, _, preflight, _ = self.run_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed, ["synthesis_and_save"])
        self.assertEqual([stage.name for stage in preflight.call_args.kwargs["stages"]], executed)
        self.assertTrue(self.state()["full_pipeline_complete"])

    def test_completed_synthesis_is_skipped_without_database_or_model_calls(self):
        self.assertEqual(self.run_pipeline()[0], 0)
        code, executed, run, preflight, _ = self.run_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed, [])
        run.assert_not_called()
        preflight.assert_not_called()

    def test_materiality_save_failure_prevents_synthesis(self):
        code, executed, run, _, _ = self.run_pipeline(failed_stage="save_materiality_2024_2025")
        self.assertEqual(code, 1)
        self.assertNotIn("synthesis_and_save", executed)
        run.assert_not_called()
        self.assertFalse(self.state()["complete"])
        self.assertFalse(self.state()["full_pipeline_complete"])

    def test_materiality_endpoint_and_legacy_skip_flag_stop_before_synthesis(self):
        for extra in (["--until", "materiality"], ["--skip-synthesis"]):
            with self.subTest(extra=extra):
                code, executed, run, _, _ = self.run_pipeline(extra)
                self.assertEqual(code, 0)
                self.assertNotIn("synthesis_and_save", executed)
                run.assert_not_called()
                self.assertFalse(self.state()["full_pipeline_complete"])
        self.assertEqual(self.run_pipeline()[1], ["synthesis_and_save"])

    def test_synthesis_can_start_directly_from_saved_postgres_inputs(self):
        code, executed, run, _, _ = self.run_pipeline(["--from-stage", "synthesis"])
        self.assertEqual(code, 0)
        self.assertEqual(executed, ["synthesis_and_save"])
        command = run.call_args.args[0]
        self.assertEqual(Path(command[1]).name, "run_synthesis.py")
        self.assertEqual(command[command.index("--company") + 1], "AMD")

    def test_configured_workload_and_environment_are_forwarded(self):
        env_file = self.root / "settings.env"
        env_file.write_text("")
        code, _, run, _, _ = self.run_pipeline([
            "--from-stage", "synthesis", "--synthesis-finding-workers", "20",
            "--synthesis-max-changes", "180", "--synthesis-finding-batch-size", "5",
            "--env-file", str(env_file),
        ])
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        for flag, value in (("--finding-workers", "20"), ("--max-changes", "180"),
                            ("--finding-batch-size", "5"), ("--env-file", str(env_file))):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(self.state()["synthesis_concurrency"]["max_concurrent_requests"], 20)

    def test_changing_workers_preserves_completion_but_workload_reruns_synthesis(self):
        self.assertEqual(self.run_pipeline()[0], 0)
        self.assertEqual(self.run_pipeline(["--synthesis-finding-workers", "2"])[1], [])
        self.assertEqual(self.run_pipeline(["--synthesis-max-changes", "180"])[1], ["synthesis_and_save"])

    def test_rerunning_materiality_invalidates_synthesis_and_preserves_upstream(self):
        self.assertEqual(self.run_pipeline()[0], 0)
        code, _, _, _, _ = self.run_pipeline([
            "--from-stage", "materiality", "--until", "materiality", "--no-resume",
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("synthesis_and_save", [entry["stage"] for entry in self.state()["stages"]])
        self.assertEqual(self.run_pipeline()[1], ["synthesis_and_save"])

    def test_resaving_xbrl_invalidates_only_synthesis(self):
        self.assertEqual(self.run_pipeline()[0], 0)
        state = self.state()
        next(entry for entry in state["stages"] if entry["stage"] == "save_xbrl_2024")["status"] = "stopped"
        self.progress.write_text(json.dumps(state))
        self.assertEqual(self.run_pipeline()[1], ["save_xbrl_2024", "synthesis_and_save"])

    def test_dry_run_includes_synthesis_without_writes_or_service_calls(self):
        code, executed, run, preflight, output = self.run_pipeline(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(executed, [])
        run.assert_not_called()
        preflight.assert_not_called()
        self.assertIn("22/22 synthesis_and_save", output)
        self.assertNotIn("run_aligned_change_analysis.py", output)
        self.assertNotIn("--embedding-model", output)
        self.assertIn("up to 5 simultaneous requests", output)
        self.assertFalse(self.progress.exists())

    def test_synthesis_preflight_checks_llm_and_xbrl_without_sec_or_snapshot_requirements(self):
        args = pipeline.parse_args(self.arguments + ["--from-stage", "synthesis"])
        with patch.object(pipeline, "load_config") as config, \
             patch.object(pipeline, "connection_arguments", return_value={}), \
             patch.object(pipeline.importlib.util, "find_spec", return_value=object()), \
             patch("psycopg.connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = []
            pipeline.preflight(args)
        config.assert_called_once()
        required = cursor.execute.call_args.args[1][0]
        self.assertIn("xbrl_value_context", required)
        self.assertNotIn("pipeline_stage_snapshots", required)


if __name__ == "__main__":
    unittest.main()
