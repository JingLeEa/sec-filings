import io
import json
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from sec_disclosure.pipelines import run_full_pipeline as runner
from artifact_support import MemoryArtifactStore, workspace_factory


class FullPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        self.artifacts = MemoryArtifactStore()
        patch.object(runner, 'PostgresWorkspace', side_effect=workspace_factory(self.artifacts)).start()
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(runner, "dotenv_values", return_value={}).start()
        self.arguments = ["--ticker", "AMD", "--years", "2023", "2024", "2025",
                          "--data-dir", str(self.root), "--user-agent", "Test test@example.com",
                          "--skip-synthesis"]

    def test_each_stage_saves_and_materiality_is_imported_after_classification(self):
        stages = runner.build_stages(runner.parse_args(self.arguments))
        scripts = [stage.script for stage in stages]
        self.assertNotIn("extract_html_metrics.py", scripts)
        self.assertNotIn("run_aligned_change_analysis.py", scripts)
        self.assertNotIn("--no-db", [arg for stage in stages for arg in stage.arguments])
        raw_loads = [stage for stage in stages if stage.script == "load_to_postgres.py"]
        self.assertEqual(len(raw_loads), 3)
        for stage in raw_loads:
            self.assertIn("--sync", stage.arguments)
            self.assertNotIn("--replace", stage.arguments)
        for index, stage in enumerate(stages):
            if stage.script == "classify_materiality.py":
                self.assertEqual(stages[index + 1].script, "load_alignments_to_postgres.py")
                self.assertEqual(stage.arguments[stage.arguments.index("--alignment-dir") + 1],
                                 stages[index + 1].arguments[stages[index + 1].arguments.index("--dir") + 1])
        names = [stage.name for stage in stages]
        xbrl_saves = [stage for stage in stages if stage.script == "load_xbrl_to_postgres.py"]
        self.assertEqual([stage.name for stage in xbrl_saves],
                         ["save_xbrl_2023", "save_xbrl_2024", "save_xbrl_2025"])
        self.assertEqual(
            [Path(stage.arguments[stage.arguments.index("--file") + 1]).name for stage in xbrl_saves],
            [f"amd_{year}_api_metrics_with_values.json" for year in (2023, 2024, 2025)],
        )
        self.assertLess(names.index("api_tables_and_save"), names.index("extract_disclosures_and_save"))
        self.assertLess(names.index("align_and_save_2024_2025"), names.index("llm_changes_2023_2024"))
        self.assertLess(names.index("save_changes_2024_2025"), names.index("materiality_2023_2024"))

    def test_text_preprocessing_inherits_extractor_recovery_defaults(self):
        from sec_disclosure.extraction.sec_10k_extractor import parse_args as extractor_args

        stages = runner.build_stages(runner.parse_args(self.arguments))
        for stage in stages:
            for flag in ("--agent-page-recovery", "--no-agent-page-recovery", "--agent-page-cache-dir"):
                self.assertNotIn(flag, stage.arguments)
            if stage.script == "extract_filings.py":
                args = extractor_args(list(stage.arguments))
                self.assertTrue(args.agent_page_recovery)
                self.assertFalse(args.no_agent_fallback)
                self.assertEqual(args.agent_page_cache_dir, Path("data/filing_page_cache"))
                self.assertNotIn("--sec-timeout", stage.arguments)
                self.assertNotIn("--sec-timeouts", stage.arguments)
                self.assertEqual(args.sec_timeouts, [60, 180, 300])

    def test_every_downstream_stage_reads_postgres_and_llm_results_are_saved(self):
        stages = runner.build_stages(runner.parse_args(self.arguments))
        for stage in stages:
            if stage.script in {'extract_disclosures.py', 'align_disclosures.py',
                                'classify_materiality.py'}:
                self.assertEqual(stage.arguments[stage.arguments.index('--input-source') + 1], 'postgres')
            if stage.name.startswith('llm_changes_'):
                self.assertIn('--store-postgres', stage.arguments)
                self.assertNotIn('--no-store-postgres', stage.arguments)

    def test_from_stage_skips_earlier_stages_and_can_select_a_single_phase(self):
        args = runner.parse_args(self.arguments + ['--from-stage', 'alignment', '--until', 'alignment'])
        stages = runner.build_stages(args)
        self.assertEqual([s.name for s in stages], ['align_and_save_2023_2024', 'align_and_save_2024_2025'])
        args = runner.parse_args(self.arguments + ['--from-stage', 'materiality'])
        self.assertTrue(all(s.phase == 'materiality' for s in runner.build_stages(args)))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.parse_args(self.arguments + ['--from-stage', 'materiality', '--until', 'extraction'])

    def test_default_extraction_starts_three_years_with_six_batches_each_and_eighteen_api_slots(self):
        args = runner.parse_args(self.arguments)
        stages = runner.build_stages(args)
        extraction = next(stage for stage in stages if stage.name == "extract_disclosures_and_save")
        for flag, expected in (("--workers", "3"), ("--batch-workers", "6"),
                               ("--max-concurrent-requests", "18")):
            self.assertEqual(extraction.arguments[extraction.arguments.index(flag) + 1], expected)
        alignment = next(stage for stage in stages if stage.phase == "alignment")
        materiality = next(stage for stage in stages if stage.script == "classify_materiality.py")
        self.assertEqual(alignment.arguments[alignment.arguments.index("--workers") + 1], "9")
        self.assertEqual(materiality.arguments[materiality.arguments.index("--concurrency") + 1], "9")

    def test_failed_text_extraction_is_not_retried_or_saved_by_runner(self):
        for code in (2, 75):
            with self.subTest(code=code), patch.object(runner, "preflight"), \
                 patch.object(runner, "execute_stage", return_value=code) as run, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.main(self.arguments + ["--until", "preprocessing"]), code)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0].script, "extract_filings.py")

    def test_extraction_api_cap_tracks_selected_year_and_batch_workers_unless_explicit(self):
        for options, expected in ((["--workers", "2", "--batch-workers", "4"], 8),
                                  (["--year-workers", "3", "--batch-workers", "6", "--max-concurrent-requests", "5"], 5),
                                  (["--years", "2024", "--until", "extraction"], 6)):
            with self.subTest(options=options):
                args = runner.parse_args(self.arguments + options)
                self.assertEqual(args.max_concurrent_requests, expected)

    def test_each_concurrency_control_requires_a_positive_value(self):
        for flag in ("--workers", "--batch-workers", "--max-concurrent-requests",
                     "--alignment-workers", "--alignment-pair-workers",
                     "--alignment-max-concurrent-requests", "--materiality-concurrency",
                     "--materiality-pair-workers", "--materiality-max-concurrent-requests",
                     "--synthesis-finding-workers", "--synthesis-max-changes",
                     "--synthesis-finding-batch-size"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runner.parse_args(self.arguments + [flag, "0"])

    def test_synthesis_workers_cannot_exceed_soclaas_limit(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.parse_args(self.arguments + ["--synthesis-finding-workers", "21"])

    def test_synthesis_workload_controls_are_forwarded_to_the_final_stage(self):
        args = runner.parse_args(self.arguments + [
            "--synthesis-finding-workers", "18",
            "--synthesis-max-changes", "180",
        ])
        self.assertEqual(args.synthesis_finding_workers, 18)
        self.assertEqual(args.synthesis_max_changes, 180)

    def test_dry_run_makes_no_subprocess_database_or_model_calls(self):
        with patch.object(runner, "preflight") as preflight, \
             patch.object(runner, "execute_stage") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(self.arguments + ["--dry-run"]), 0)
        preflight.assert_not_called()
        run.assert_not_called()
        self.assertIn("save_materiality_2024_2025", output.getvalue())
        self.assertIn("Materiality: up to 2 parallel pairs x 9 workers per pair; shared API cap 18.", output.getvalue())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_complete_run_records_success_after_all_stages(self):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", return_value=0) as run, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 0)
        state = json.loads(self.progress_path.read_text())
        self.assertTrue(state["complete"])
        self.assertFalse(state["full_pipeline_complete"])
        self.assertEqual(state["synthesis_status"], "skipped")
        self.assertEqual(state["until"], "materiality")
        self.assertEqual(state["extraction_concurrency"], {"year_workers": 3,
                                                         "batch_workers_per_year": 6,
                                                         "max_concurrent_requests": 18})
        self.assertEqual(state["alignment_concurrency"], {"pair_workers": 2,
                                                        "workers_per_pair": 9,
                                                        "max_concurrent_requests": 18})
        self.assertEqual(state["materiality_concurrency"], {"pair_workers": 2,
                                                          "workers_per_pair": 9,
                                                          "max_concurrent_requests": 18})
        self.assertEqual(run.call_count, len(state["stages"]))
        self.assertTrue(all(stage["status"] == "complete" for stage in state["stages"]))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_storage_failure_stops_before_any_subsequent_stage(self):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", side_effect=[0, 1]) as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(runner.main(self.arguments), 1)
        self.assertEqual(run.call_count, 2)
        self.assertIn("save_text_2023", errors.getvalue())
        state = json.loads(self.progress_path.read_text())
        self.assertFalse(state["complete"])

    def test_request_pause_stops_without_reclassifying_or_claiming_success(self):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", return_value=2) as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 2)
        self.assertEqual(run.call_count, 1)

    def test_preflight_failure_does_not_start_extraction(self):
        with patch.object(runner, "preflight", side_effect=ValueError("PostgreSQL unavailable")), \
             patch.object(runner, "execute_stage") as run, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 1)
        run.assert_not_called()

    def test_every_stopping_point_includes_its_final_database_save(self):
        endpoints = {"preprocessing": (10, "save_xbrl_2025"),
                     "extraction": (11, "extract_disclosures_and_save"),
                     "alignment": (13, "align_and_save_2024_2025"),
                     "change-analysis": (17, "save_changes_2024_2025"),
                     "materiality": (21, "save_materiality_2024_2025")}
        for target, (count, last) in endpoints.items():
            with self.subTest(until=target):
                stages = runner.build_stages(runner.parse_args(self.arguments + ["--until", target]))
                self.assertEqual(len(stages), count)
                self.assertEqual(stages[-1].name, last)
                self.assertTrue(all(runner.PHASES.index(stage.phase) <= runner.PHASES.index(target)
                                    for stage in stages))

    def test_extraction_stop_executes_and_saves_without_starting_alignment(self):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", return_value=0) as run, \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(self.arguments + ["--until", "extraction"]), 0)
        commands = [call.args[0].command for call in run.call_args_list]
        self.assertEqual(len(commands), 11)
        self.assertEqual(Path(commands[-1][1]).name, "extract_disclosures.py")
        self.assertIn("--store-postgres", commands[-1])
        self.assertFalse(any(Path(command[1]).name == "align_disclosures.py" for command in commands))
        self.assertIn("completed through extraction", output.getvalue())
        state = json.loads(self.progress_path.read_text())
        self.assertTrue(state["complete"])
        self.assertFalse(state["full_pipeline_complete"])
        self.assertEqual(state["until"], "extraction")

    def test_selected_dry_run_excludes_later_stages(self):
        with patch.object(runner, "preflight") as preflight, \
             patch.object(runner, "execute_stage") as run, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(self.arguments + ["--until", "extraction", "--dry-run"]), 0)
        self.assertIn("extract_disclosures_and_save", output.getvalue())
        self.assertNotIn("align_disclosures.py", output.getvalue())
        self.assertNotIn("classify_materiality.py", output.getvalue())
        preflight.assert_not_called()
        run.assert_not_called()

    def test_single_year_is_supported_until_extraction_but_requires_a_pair_for_alignment(self):
        for target in ("preprocessing", "extraction"):
            with self.subTest(until=target):
                args = runner.parse_args(self.arguments + ["--years", "2024", "--until", target])
                self.assertEqual(args.years, [2024])
                self.assertTrue(runner.build_stages(args))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            runner.parse_args(self.arguments + ["--years", "2024", "--until", "alignment"])

    def test_preprocessing_preflight_does_not_require_llm_configuration_or_alignment_tables(self):
        args = runner.parse_args(self.arguments + ["--until", "preprocessing"])
        with patch.dict(os.environ, {"SEC_API_KEY": "dummy-key"}), \
             patch.object(runner, "load_config") as llm, \
             patch.object(runner, "connection_arguments", return_value={"dbname": "test"}), \
             patch.object(runner.importlib.util, "find_spec", return_value=object()) as modules, \
             patch("psycopg.connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = [(name, name) for name in ("filings", "chunks", "sentences")]
            runner.preflight(args)
        llm.assert_not_called()
        self.assertEqual([call.args[0] for call in modules.call_args_list], ["lxml", "psycopg"])
        self.assertEqual(cursor.execute.call_args.args[1][0], ["filings", "chunks", "sentences"])

    def test_alignment_capacity_tracks_pair_and_worker_overrides(self):
        for options, expected in (([], 18), (["--alignment-pair-workers", "1"], 9),
                                  (["--alignment-workers", "4"], 8),
                                  (["--years", "2024", "2025"], 9),
                                  (["--alignment-max-concurrent-requests", "5"], 5)):
            with self.subTest(options=options):
                args = runner.parse_args(self.arguments + options)
                self.assertEqual(args.alignment_max_concurrent_requests, expected)

    def test_materiality_capacity_tracks_pair_and_worker_overrides(self):
        for options, expected in (([], 18), (["--materiality-pair-workers", "1"], 9),
                                  (["--materiality-concurrency", "4"], 8),
                                  (["--materiality-workers", "3"], 6),
                                  (["--years", "2024", "2025"], 9),
                                  (["--materiality-max-concurrent-requests", "5"], 5)):
            with self.subTest(options=options):
                args = runner.parse_args(self.arguments + options)
                self.assertEqual(args.materiality_max_concurrent_requests, expected)

    def test_materiality_pairs_overlap_share_api_slots_and_save_after_each_pair_finishes(self):
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        calls, events = [], []

        def classify(arguments, *, request_pacer):
            pair = arguments[arguments.index("--previous-year") + 1]
            self.assertEqual(arguments[arguments.index("--concurrency") + 1], "9")
            with lock:
                calls.append(request_pacer)
            barrier.wait()
            with lock:
                events.append((pair, "classified"))
            return 0

        def subprocess_run(command, **kwargs):
            if Path(command[1]).name == "load_alignments_to_postgres.py":
                pair = command[command.index("--previous-year") + 1]
                with lock:
                    events.append((pair, "saved"))
            return SimpleNamespace(returncode=0)

        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", return_value=0), \
             patch("sec_disclosure.agents.materiality.main", side_effect=classify), \
             patch.object(runner, "run_script", side_effect=subprocess_run), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 0)
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], calls[1])
        self.assertEqual(calls[0].interval, 2.0)
        self.assertEqual(calls[0].cooldown, 60.0)
        for _ in range(18):
            self.assertTrue(calls[0].slots.acquire(blocking=False))
        self.assertFalse(calls[0].slots.acquire(blocking=False))
        for _ in range(18):
            calls[0].slots.release()
        for pair in ("2023", "2024"):
            # Each pair also has a change-analysis save before materiality starts.
            self.assertLess(events.index((pair, "classified")),
                            max(i for i, event in enumerate(events) if event == (pair, "saved")))

    def test_materiality_failure_preserves_the_other_pairs_database_save(self):
        barrier = threading.Barrier(2, timeout=5)

        def classify(arguments, *, request_pacer):
            barrier.wait()
            return 2 if arguments[arguments.index("--previous-year") + 1] == "2023" else 0

        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", return_value=0), \
             patch("sec_disclosure.agents.materiality.main", side_effect=classify), \
             patch.object(runner, "run_script", return_value=SimpleNamespace(returncode=0)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 2)
        state = json.loads(self.progress_path.read_text())
        statuses = {stage["stage"]: stage["status"] for stage in state["stages"]}
        self.assertEqual(statuses["materiality_2023_2024"], "stopped")
        self.assertNotIn("save_materiality_2023_2024", statuses)
        self.assertEqual(statuses["save_materiality_2024_2025"], "complete")
        self.assertFalse(state["complete"])

    def test_alignment_and_change_pairs_really_overlap_and_share_one_limiter_per_phase(self):
        barriers = {phase: threading.Barrier(2) for phase in ("alignment", "change-analysis")}
        lock = threading.Lock()
        calls, events = [], []

        def align(arguments, *, request_pacer):
            phase = "change-analysis" if "--change-analysis-only" in arguments else "alignment"
            pair = arguments[arguments.index("--previous-year") + 1]
            self.assertEqual(arguments[arguments.index("--workers") + 1], "9")
            if phase == "alignment":
                self.assertIn("--store-postgres", arguments)
            with lock:
                calls.append((phase, pair, request_pacer))
            # A sequential implementation cannot pass this barrier.
            barriers[phase].wait(timeout=5)
            if phase == "change-analysis":
                with lock:
                    events.append((pair, "llm_done"))
            return 0

        def subprocess_run(command, **kwargs):
            script = Path(command[1]).name
            if script == "load_alignments_to_postgres.py":
                pair = command[command.index("--previous-year") + 1]
                with lock:
                    events.append((pair, "save"))
            return SimpleNamespace(returncode=0)

        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", side_effect=align), \
             patch.object(runner, "run_script", side_effect=subprocess_run), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments + ["--until", "change-analysis"]), 0)
        for phase in barriers:
            phase_calls = [call for call in calls if call[0] == phase]
            self.assertEqual({pair for _, pair, _ in phase_calls}, {"2023", "2024"})
            self.assertIs(phase_calls[0][2], phase_calls[1][2])
            self.assertEqual(phase_calls[0][2].interval, 2.0)
            slots = phase_calls[0][2].slots
            admitted = 0
            try:
                for _ in range(18):
                    self.assertTrue(slots.acquire(blocking=False))
                    admitted += 1
                self.assertFalse(slots.acquire(blocking=False))
            finally:
                for _ in range(admitted):
                    slots.release()
        for pair in ("2023", "2024"):
            self.assertLess(events.index((pair, "llm_done")), events.index((pair, "save")))

    def test_pair_pause_drains_the_other_pair_then_stops_before_change_analysis(self):
        barrier = threading.Barrier(2)
        def align(arguments, *, request_pacer):
            barrier.wait(timeout=5)
            return 2 if arguments[arguments.index("--previous-year") + 1] == "2023" else 0
        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", side_effect=align) as alignment, \
             patch.object(runner, "run_script", return_value=SimpleNamespace(returncode=0)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments + ["--until", "change-analysis"]), 2)
        self.assertEqual(alignment.call_count, 2)
        state = json.loads(self.progress_path.read_text())
        self.assertFalse(state["complete"])
        self.assertFalse(any(stage["phase"] == "change-analysis" for stage in state["stages"]))
        pair_stages = [stage for stage in state["stages"] if stage["phase"] == "alignment"]
        self.assertCountEqual([stage["status"] for stage in pair_stages], ["stopped", "complete"])

    @property
    def progress_path(self):
        return self.artifacts.path("pipeline_runs/amd/2023-2024-2025/status.json")

    def run_mock_pipeline(self, extra=(), *, effect=None):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", side_effect=effect, return_value=0) as execute, \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            code = runner.main(self.arguments + list(extra))
        return code, [call.args[0].name for call in execute.call_args_list], output.getvalue()

    def test_resume_retries_an_interrupted_save_without_redownloading_completed_text(self):
        def fail_save(stage, **kwargs):
            return 1 if stage.name == "save_text_2023" else 0

        self.assertEqual(self.run_mock_pipeline(["--until", "preprocessing"], effect=fail_save)[0], 1)
        # A killed process may leave "running" instead of "stopped".
        state = json.loads(self.progress_path.read_text())
        original_start = state["started_at"]
        state["stages"][-1]["status"] = "running"
        state["stages"][-1].pop("exit_code")
        self.progress_path.write_text(json.dumps(state))
        code, executed, output = self.run_mock_pipeline(["--until", "preprocessing"])
        self.assertEqual(code, 0)
        self.assertNotIn("text_2023", executed)
        self.assertEqual(executed[0], "save_text_2023")
        self.assertEqual(len(executed), 9)
        self.assertIn("skipping 1 completed stages", output)
        state = json.loads(self.progress_path.read_text())
        self.assertEqual(len(state["stages"]), 10)
        self.assertEqual(state["started_at"], original_start)

    def test_resume_only_retries_the_failed_materiality_pair_and_its_save(self):
        barrier = threading.Barrier(2, timeout=5)

        def fail_one_pair(stage, **kwargs):
            if stage.script == "classify_materiality.py":
                barrier.wait()
                return 2 if stage.comparison[0] == "2023" else 0
            return 0

        self.assertEqual(self.run_mock_pipeline(effect=fail_one_pair)[0], 2)
        legacy = json.loads(self.progress_path.read_text())
        original_start = legacy["started_at"]
        for entry in legacy["stages"]:
            entry.pop("command_sha256", None)
        self.progress_path.write_text(json.dumps(legacy))
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed, ["materiality_2023_2024", "save_materiality_2023_2024"])
        state = json.loads(self.progress_path.read_text())
        self.assertFalse(state["full_pipeline_complete"])
        self.assertEqual(state["synthesis_status"], "skipped")
        self.assertEqual(state["started_at"], original_start)
        self.assertEqual(len(state["stages"]), 21)
        self.assertEqual(len(state["skipped_stages"]), 19)
        self.assertTrue(all(entry["command_sha256"] for entry in state["stages"]))

    def test_resume_extends_until_and_preserves_progress_across_narrower_selections(self):
        self.assertEqual(self.run_mock_pipeline(["--until", "extraction"])[0], 0)
        code, executed, _ = self.run_mock_pipeline(["--until", "alignment"])
        self.assertEqual(code, 0)
        self.assertCountEqual(executed, ["align_and_save_2023_2024", "align_and_save_2024_2025"])
        code, executed, _ = self.run_mock_pipeline(["--from-stage", "change-analysis"])
        self.assertEqual(code, 0)
        self.assertEqual(len(executed), 8)
        self.assertTrue(all(not name.startswith(("text_", "align_and_save_")) for name in executed))
        # Visiting an earlier endpoint must not discard the later completions.
        self.assertEqual(self.run_mock_pipeline(["--until", "extraction"])[1], [])
        self.assertEqual(self.run_mock_pipeline()[1], [])

    def test_completed_resume_needs_no_credentials_database_or_stage_calls(self):
        self.assertEqual(self.run_mock_pipeline()[0], 0)
        with patch.object(runner, "preflight") as preflight, \
             patch.object(runner, "execute_stage") as execute, redirect_stdout(io.StringIO()) as output:
            # SEC_USER_AGENT is absent, and there is no --user-agent argument.
            self.assertEqual(runner.main(self.arguments[:-3] + ["--skip-synthesis"]), 0)
        preflight.assert_not_called()
        execute.assert_not_called()
        self.assertIn("all selected stages are complete", output.getvalue())

    def test_resume_preflight_checks_only_unfinished_stages(self):
        self.assertEqual(self.run_mock_pipeline(["--until", "change-analysis"])[0], 0)
        with patch.object(runner, "preflight") as preflight, \
             patch.object(runner, "execute_stage", return_value=0), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(self.arguments[:-3] + ["--skip-synthesis"]), 0)
        pending = preflight.call_args.kwargs["stages"]
        self.assertEqual(len(pending), 4)
        self.assertTrue(all(stage.phase == "materiality" for stage in pending))

    def test_pending_database_save_does_not_require_sec_or_llm_configuration(self):
        args = runner.parse_args(self.arguments[:-3] + ["--skip-synthesis"])
        stage = next(stage for stage in runner.build_stages(args) if stage.name == "save_materiality_2023_2024")
        with patch.object(runner, "load_config") as llm, \
             patch.object(runner, "connection_arguments", return_value={"dbname": "test"}), \
             patch.object(runner.importlib.util, "find_spec", return_value=object()) as modules, \
             patch("psycopg.connect") as connect:
            cursor = connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
            cursor.fetchall.return_value = []
            runner.preflight(args, stages=[stage])
        llm.assert_not_called()
        self.assertEqual([call.args[0] for call in modules.call_args_list], ["psycopg"])

    def test_rerunning_llm_change_analysis_invalidates_its_save_and_materiality(self):
        self.assertEqual(self.run_mock_pipeline()[0], 0)
        state = json.loads(self.progress_path.read_text())
        next(entry for entry in state["stages"] if entry["stage"] == "llm_changes_2023_2024")["status"] = "stopped"
        self.progress_path.write_text(json.dumps(state))
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed, ["llm_changes_2023_2024", "save_changes_2023_2024",
                                    "materiality_2023_2024", "save_materiality_2023_2024"])

    def test_legacy_enrichment_checkpoints_are_ignored_and_saved_llm_labels_are_reused(self):
        self.assertEqual(self.run_mock_pipeline(["--until", "alignment"])[0], 0)
        state = json.loads(self.progress_path.read_text())
        for pair in ("2023_2024", "2024_2025"):
            state["stages"].extend([
                {"stage": "llm_changes_" + pair, "status": "complete", "exit_code": 0},
                {"stage": "lexical_semantic_" + pair, "status": "stopped", "exit_code": 1},
            ])
        self.progress_path.write_text(json.dumps(state))
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertCountEqual(executed, [f"{prefix}_{pair}" for pair in ("2023_2024", "2024_2025")
                                        for prefix in ("save_changes", "materiality", "save_materiality")])
        self.assertFalse(any(entry["stage"].startswith("lexical_semantic_")
                             for entry in json.loads(self.progress_path.read_text())["stages"]))

    def test_changing_concurrency_preserves_completed_results(self):
        self.assertEqual(self.run_mock_pipeline()[0], 0)
        code, executed, _ = self.run_mock_pipeline([
            "--workers", "1", "--batch-workers", "2", "--alignment-workers", "3",
            "--materiality-concurrency", "4", "--user-agent", "Different contact@example.com",
        ])
        self.assertEqual(code, 0)
        self.assertEqual(executed, [])

    def test_existing_local_progress_moves_to_postgres_and_new_output_locations_reuse_it(self):
        args = runner.parse_args(self.arguments)
        legacy = self.root / 'pipeline_runs/amd/2023-2024-2025/status.json'
        legacy.parent.mkdir(parents=True)
        stages = runner.build_stages(args)
        state = {'ticker': 'amd', 'years': args.years, 'stages': [
            {'stage': stage.name, 'status': 'complete', 'exit_code': 0,
             'command_sha256': runner.stage_signature(stage)} for stage in stages]}
        legacy.write_text(json.dumps(state))
        original = legacy.read_bytes()
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(executed, [])
        self.assertEqual(legacy.read_bytes(), original)
        self.assertTrue(self.progress_path.exists())
        self.assertEqual(self.run_mock_pipeline(['--data-dir', str(self.root / 'another-machine')])[1], [])

    def test_local_export_is_opt_in_and_does_not_include_request_or_progress_cache(self):
        def write_results(stage, **kwargs):
            if stage.name == 'extract_disclosures_and_save':
                from sec_disclosure.storage.artifacts import ArtifactPath
                data = ArtifactPath(stage.arguments[stage.arguments.index('--data-dir') + 1])
                runner.write_json(data / 'disclosures/amd/2023/disclosures.json', {'disclosures': []})
                runner.write_json(data / 'disclosures/amd/2023/requests/cached.json', {'text': 'cached'})
            return 0
        self.assertEqual(self.run_mock_pipeline(effect=write_results)[0], 0)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.run_mock_pipeline(['--save-local-output'])[0], 0)
        output = self.root / 'disclosures/amd/2023'
        self.assertTrue((output / 'disclosures.json').is_file())
        self.assertFalse((output / 'requests').exists())
        self.assertFalse((self.root / 'pipeline_runs').exists())

    def test_no_resume_executes_selected_stages_and_invalidates_later_completions(self):
        self.assertEqual(self.run_mock_pipeline()[0], 0)
        code, executed, _ = self.run_mock_pipeline([
            "--from-stage", "alignment", "--until", "alignment", "--no-resume",
        ])
        self.assertEqual(code, 0)
        self.assertCountEqual(executed, ["align_and_save_2023_2024", "align_and_save_2024_2025"])
        state = json.loads(self.progress_path.read_text())
        self.assertEqual(len(state["stages"]), 13)
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 0)
        self.assertEqual(len(executed), 8)
        self.assertFalse(any(name.startswith(("text_", "align_and_save_")) for name in executed))

    def test_resume_dry_run_shows_skips_without_modifying_checkpoint_or_running_work(self):
        self.assertEqual(self.run_mock_pipeline(["--until", "change-analysis"])[0], 0)
        before = self.progress_path.read_bytes()
        code, executed, output = self.run_mock_pipeline(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(executed, [])
        self.assertEqual(output.count("[cached; skipped]"), 17)
        self.assertIn("18/21 materiality_2023_2024", output)
        self.assertEqual(self.progress_path.read_bytes(), before)
        code, executed, output = self.run_mock_pipeline(["--dry-run", "--no-resume"])
        self.assertEqual(code, 0)
        self.assertNotIn("[cached; skipped]", output)
        self.assertEqual(self.progress_path.read_bytes(), before)

    def test_invalid_progress_does_not_silently_restart_paid_work(self):
        self.progress_path.parent.mkdir(parents=True)
        self.progress_path.write_text("{invalid json")
        code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 1)
        self.assertEqual(executed, [])
        self.assertEqual(self.progress_path.read_text(), "{invalid json")
        self.assertEqual(self.run_mock_pipeline(["--until", "preprocessing", "--no-resume"])[0], 0)

    def test_two_invocations_cannot_overwrite_the_same_pipeline_checkpoint(self):
        with runner.invocation_lock(self.progress_path.parent.parent):
            code, executed, _ = self.run_mock_pipeline()
        self.assertEqual(code, 1)
        self.assertEqual(executed, [])
        self.assertFalse(self.progress_path.exists())
        self.assertEqual(self.run_mock_pipeline(["--until", "preprocessing"])[0], 0)


if __name__ == "__main__":
    unittest.main()
