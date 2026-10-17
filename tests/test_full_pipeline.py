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


class FullPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch.object(runner, "dotenv_values", return_value={}).start()
        self.arguments = ["--ticker", "AMD", "--years", "2023", "2024", "2025",
                          "--data-dir", str(self.root), "--user-agent", "Test test@example.com"]

    def test_each_stage_saves_and_materiality_is_imported_after_classification(self):
        stages = runner.build_stages(runner.parse_args(self.arguments))
        scripts = [stage.script for stage in stages]
        self.assertNotIn("extract_html_metrics.py", scripts)
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
        self.assertLess(names.index("api_tables_and_save"), names.index("extract_disclosures_and_save"))
        self.assertLess(names.index("align_and_save_2024_2025"), names.index("llm_changes_2023_2024"))
        self.assertLess(names.index("save_changes_2024_2025"), names.index("materiality_2023_2024"))

    def test_every_downstream_stage_reads_postgres_and_llm_results_are_saved_before_semantic_analysis(self):
        stages = runner.build_stages(runner.parse_args(self.arguments))
        for stage in stages:
            if stage.script in {'extract_disclosures.py', 'align_disclosures.py',
                                'run_aligned_change_analysis.py', 'classify_materiality.py'}:
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
                     "--materiality-pair-workers", "--materiality-max-concurrent-requests"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                runner.parse_args(self.arguments + [flag, "0"])

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
        state = json.loads((self.root / "pipeline_runs/amd/2023-2024-2025/status.json").read_text())
        self.assertTrue(state["complete"])
        self.assertTrue(state["full_pipeline_complete"])
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

    def test_storage_failure_stops_before_any_subsequent_stage(self):
        with patch.object(runner, "preflight"), \
             patch.object(runner, "execute_stage", side_effect=[0, 1]) as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(runner.main(self.arguments), 1)
        self.assertEqual(run.call_count, 2)
        self.assertIn("save_text_2023", errors.getvalue())
        state = json.loads((self.root / "pipeline_runs/amd/2023-2024-2025/status.json").read_text())
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
        endpoints = {"preprocessing": (7, "api_tables_and_save"),
                     "extraction": (8, "extract_disclosures_and_save"),
                     "alignment": (10, "align_and_save_2024_2025"),
                     "change-analysis": (16, "save_changes_2024_2025"),
                     "materiality": (20, "save_materiality_2024_2025")}
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
        self.assertEqual(len(commands), 8)
        self.assertEqual(Path(commands[-1][1]).name, "extract_disclosures.py")
        self.assertIn("--store-postgres", commands[-1])
        self.assertFalse(any(Path(command[1]).name == "align_disclosures.py" for command in commands))
        self.assertIn("completed through extraction", output.getvalue())
        state = json.loads((self.root / "pipeline_runs/amd/2023-2024-2025/status.json").read_text())
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
             patch.object(runner.subprocess, "run", side_effect=subprocess_run), \
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
             patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments), 2)
        state = json.loads((self.root / "pipeline_runs/amd/2023-2024-2025/status.json").read_text())
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
            if script in {"run_aligned_change_analysis.py", "load_alignments_to_postgres.py"}:
                pair = command[command.index("--previous-year") + 1]
                with lock:
                    events.append((pair, "enrich" if script == "run_aligned_change_analysis.py" else "save"))
            return SimpleNamespace(returncode=0)

        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", side_effect=align), \
             patch.object(runner.subprocess, "run", side_effect=subprocess_run), \
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
            self.assertLess(events.index((pair, "llm_done")), events.index((pair, "enrich")))
            self.assertLess(events.index((pair, "enrich")), events.index((pair, "save")))

    def test_pair_pause_drains_the_other_pair_then_stops_before_change_analysis(self):
        barrier = threading.Barrier(2)
        def align(arguments, *, request_pacer):
            barrier.wait(timeout=5)
            return 2 if arguments[arguments.index("--previous-year") + 1] == "2023" else 0
        with patch.object(runner, "preflight"), \
             patch("sec_disclosure.agents.disclosure_alignment.main", side_effect=align) as alignment, \
             patch.object(runner.subprocess, "run", return_value=SimpleNamespace(returncode=0)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(self.arguments + ["--until", "change-analysis"]), 2)
        self.assertEqual(alignment.call_count, 2)
        state = json.loads((self.root / "pipeline_runs/amd/2023-2024-2025/status.json").read_text())
        self.assertFalse(state["complete"])
        self.assertFalse(any(stage["phase"] == "change-analysis" for stage in state["stages"]))
        pair_stages = [stage for stage in state["stages"] if stage["phase"] == "alignment"]
        self.assertCountEqual([stage["status"] for stage in pair_stages], ["stopped", "complete"])


if __name__ == "__main__":
    unittest.main()
