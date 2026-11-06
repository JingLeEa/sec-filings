"""Backend launch and progress mapping for the dashboard, without external requests."""
import json
import importlib.util
import os
import signal
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sec_ui import pipeline_runs
from artifact_support import MemoryArtifactStore


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
        self.history_store = MemoryArtifactStore()
        self.history_patch = patch.object(pipeline_runs, '_history_store', return_value=self.history_store)
        self.history_patch.start()
        self.addCleanup(self.history_patch.stop)

    def running_job(self):
        folder = pipeline_runs.run_folder('wfc', [2023, 2024, 2025])
        folder.mkdir(parents=True, exist_ok=True)
        job = {'ticker': 'wfc', 'years': [2023, 2024, 2025], 'run_id': 'active-wfc',
               'status': 'running', 'pid': 12345}
        pipeline_runs._write_json(folder / 'ui_run.json', job)
        return folder, job

    def test_history_resume_rechecks_completed_progress_before_launching_or_overwriting_metadata(self):
        folder, job = self.running_job()
        job.update(status='stopped', exit_code=1)
        pipeline_runs._write_json(folder / 'ui_run.json', job)
        original = (folder / 'ui_run.json').read_bytes()
        with patch.object(pipeline_runs, 'active_run', return_value=None), \
             patch.object(pipeline_runs, 'read_run', return_value=(job, {'full_pipeline_complete': True})), \
             patch.object(pipeline_runs.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(pipeline_runs.RunError, 'already completed'):
                pipeline_runs.start_run('WFC', job['years'], resume_only=True)
        launch.assert_not_called()
        self.assertEqual((folder / 'ui_run.json').read_bytes(), original)

    def test_start_rejects_starter_results_before_creating_a_run_or_process(self):
        progress = {'full_pipeline_complete': True, 'completion_source': 'starter_data', 'stages': []}
        with patch.object(pipeline_runs, 'read_run', return_value=(None, progress)), \
             patch.object(pipeline_runs.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(pipeline_runs.RunError, 'Starter results'):
                pipeline_runs.start_run('MU', [2023, 2024, 2025])
        launch.assert_not_called()
        self.assertFalse((pipeline_runs.ROOT / 'data').exists())

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_stop_targets_only_the_verified_runner_and_resume_reuses_cached_work(self):
        folder, job = self.running_job()
        cache = folder / 'saved-response.json'
        cache.write_text('{"completed": true}')
        with patch.object(pipeline_runs, '_pid_alive', return_value=True), \
             patch.object(pipeline_runs, '_pipeline_process_matches', return_value=True), \
             patch.object(pipeline_runs.os, 'getpgid', return_value=12345), \
             patch.object(pipeline_runs.os, 'getpgrp', return_value=999), \
             patch.object(pipeline_runs.os, 'killpg') as stop, patch.object(pipeline_runs, 'Thread') as thread:
            result = pipeline_runs.stop_run('WFC', job['years'], run_id=job['run_id'])
            self.assertEqual(result['status'], 'stopping')
            self.assertTrue(result['stopped_by_user'])
            stop.assert_called_once_with(12345, signal.SIGTERM)
            with self.assertRaises(pipeline_runs.RunError):
                pipeline_runs.start_run('amd', [2023, 2024])
            pipeline_runs.stop_run('WFC', job['years'], run_id=job['run_id'])
            self.assertEqual(stop.call_count, 1)
            with patch.object(pipeline_runs, '_pid_alive', return_value=False):
                thread.call_args.kwargs['target'](*thread.call_args.kwargs['args'])

        stopped = pipeline_runs._read_json(folder / 'ui_run.json')
        self.assertEqual(stopped['status'], 'stopped')
        self.assertTrue(stopped['stopped_by_user'])
        self.assertEqual(cache.read_text(), '{"completed": true}')
        with patch.object(pipeline_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=54321)) as popen, \
             patch.object(pipeline_runs, 'Thread'):
            resumed = pipeline_runs.start_run('wfc', job['years'])
        self.assertIn('--retry-failed', popen.call_args.args[0])
        self.assertNotIn('--no-resume', popen.call_args.args[0])
        self.assertTrue(resumed['retry_failed'])
        self.assertEqual(cache.read_text(), '{"completed": true}')

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_stop_refuses_changed_runs_and_unrelated_processes(self):
        folder, job = self.running_job()
        with patch.object(pipeline_runs, '_pid_alive', return_value=True), \
             patch.object(pipeline_runs, '_pipeline_process_matches', return_value=False), \
             patch.object(pipeline_runs.os, 'killpg') as stop:
            with self.assertRaisesRegex(pipeline_runs.RunError, 'run changed'):
                pipeline_runs.stop_run('wfc', job['years'], run_id='old-run')
            with self.assertRaisesRegex(pipeline_runs.RunError, 'Cannot verify'):
                pipeline_runs.stop_run('wfc', job['years'], run_id=job['run_id'])
            stop.assert_not_called()
        self.assertEqual(pipeline_runs._read_json(folder / 'ui_run.json')['status'], 'running')

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_stop_does_not_signal_a_shared_terminal_group(self):
        _, job = self.running_job()
        with patch.object(pipeline_runs, '_pid_alive', return_value=True), \
             patch.object(pipeline_runs, '_pipeline_process_matches', return_value=True), \
             patch.object(pipeline_runs.os, 'getpgid', return_value=999), \
             patch.object(pipeline_runs.os, 'killpg') as stop:
            with self.assertRaisesRegex(pipeline_runs.RunError, 'own process group'):
                pipeline_runs.stop_run('wfc', job['years'], run_id=job['run_id'])
            stop.assert_not_called()

    @unittest.skipUnless(os.name == 'posix', 'POSIX process groups')
    def test_failed_stop_restores_running_controls(self):
        folder, job = self.running_job()
        with patch.object(pipeline_runs, '_pid_alive', return_value=True), \
             patch.object(pipeline_runs, '_pipeline_process_matches', return_value=True), \
             patch.object(pipeline_runs.os, 'getpgid', return_value=12345), \
             patch.object(pipeline_runs.os, 'getpgrp', return_value=999), \
             patch.object(pipeline_runs.os, 'killpg', side_effect=PermissionError):
            with self.assertRaisesRegex(pipeline_runs.RunError, 'permissions'):
                pipeline_runs.stop_run('wfc', job['years'], run_id=job['run_id'])
        self.assertEqual(pipeline_runs._read_json(folder / 'ui_run.json'), job)

    def test_stop_process_verification_checks_company_years_and_script(self):
        script = pipeline_runs.ROOT / 'scripts/run_full_pipeline.py'
        valid = ['python', '-u', str(script), '--ticker', 'wfc', '--years', '2023', '2024', '2025']
        for command, expected in ((valid, True), ([*valid[:4], 'amd', *valid[5:]], False),
                                  ([*valid[:6], '2022'], False), (['python', 'unrelated.py'], False)):
            with self.subTest(command=command), patch.object(pipeline_runs.psutil, 'Process') as process:
                process.return_value.cmdline.return_value = command
                self.assertEqual(pipeline_runs._pipeline_process_matches(12345, 'wfc', (2023, 2024, 2025)), expected)

    def test_status_and_process_verification_leave_a_real_worker_running(self):
        root = pipeline_runs.ROOT / 'checkout with spaces'
        script = root / 'scripts/run_full_pipeline.py'
        script.parent.mkdir(parents=True)
        script.write_text('import sys\nsys.stdin.buffer.read(1)\n', encoding='utf-8')
        command = [sys.executable, '-u', str(script), '--ticker', 'wfc', '--years', '2023', '2024']
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **pipeline_runs._launch_options()) as process:
            try:
                with patch.object(pipeline_runs, 'ROOT', root):
                    self.assertTrue(pipeline_runs._pid_alive(process.pid))
                    self.assertIsNone(process.poll())
                    self.assertTrue(pipeline_runs._pipeline_process_matches(process.pid, 'wfc', (2023, 2024)))
                    self.assertFalse(pipeline_runs._pipeline_process_matches(process.pid, 'amd', (2023, 2024)))
                    self.assertIsNone(process.poll())
            finally:
                process.communicate(b'x', timeout=10)

    def test_database_progress_uses_portable_keys(self):
        documents = {
            'disclosures/wfc/2023/token_usage.json': {'planned_batches': 4, 'pending_batches': ['last']},
            'alignments/wfc/2023-2024/candidate_matches.json': {'a': [], 'b': []},
            'alignments/wfc/2023-2024/jobs/matching_1.json': {'anchors': ['a']},
        }
        with patch.object(pipeline_runs, '_postgres_documents', return_value={
                **documents, pipeline_runs._status_key('wfc', [2023, 2024]): {}}) as read:
            row = pipeline_runs.stage_progress('wfc', [2023, 2024], 'extract_disclosures_and_save')[0]
            self.assertEqual((row['done'], row['total']), (3, 4))
            row = pipeline_runs.stage_progress('wfc', [2023, 2024], 'align_and_save_2023_2024')[0]
            self.assertEqual((row['done'], row['total']), (1, 2))
            for call in read.call_args_list:
                self.assertTrue(all('\\' not in key for key in call.args[0]))
                self.assertNotIn('\\', call.args[1] or '')

    def test_stop_terminates_only_a_verified_real_runner(self):
        script = pipeline_runs.ROOT / 'scripts/run_full_pipeline.py'
        script.parent.mkdir(parents=True)
        script.write_text('import sys\nsys.stdin.buffer.read(1)\n', encoding='utf-8')
        folder, job = self.running_job()
        command = [sys.executable, '-u', str(script), '--ticker', 'wfc', '--years', '2023', '2024', '2025']
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, **pipeline_runs._launch_options()) as process:
            try:
                job['pid'] = process.pid
                pipeline_runs._write_json(folder / 'ui_run.json', job)
                with patch.object(pipeline_runs, 'Thread') as thread:
                    stopped = pipeline_runs.stop_run('wfc', job['years'], run_id=job['run_id'])
                    self.assertEqual(stopped['status'], 'stopping')
                    process.wait(timeout=10)
                    thread.call_args.kwargs['target'](*thread.call_args.kwargs['args'])
                saved = pipeline_runs._read_json(folder / 'ui_run.json')
                self.assertEqual(saved['status'], 'stopped')
                self.assertTrue(saved['stopped_by_user'])
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)

    def test_completion_monitor_preserves_the_user_stop_reason(self):
        folder, job = self.running_job()
        job.update(status='stopping', stopped_by_user=True)
        pipeline_runs._write_json(folder / 'ui_run.json', job)
        pipeline_runs._finish_job(SimpleNamespace(wait=lambda: -15), folder, job['run_id'])
        saved = pipeline_runs._read_json(folder / 'ui_run.json')
        self.assertEqual((saved['status'], saved['exit_code']), ('stopped', -15))
        self.assertTrue(saved['stopped_by_user'])

    def test_stopped_run_allows_another_company_or_year_selection_without_losing_progress(self):
        folder, job = self.running_job()
        job.update(status='stopped', stopped_by_user=True)
        pipeline_runs._write_json(folder / 'ui_run.json', job)
        saved_job = (folder / 'ui_run.json').read_bytes()
        checkpoint = folder / 'saved-checkpoint.json'
        checkpoint.write_text('{"completed":15}')
        for ticker, years in (('NVDA', [2021, 2022]), ('WFC', [2022, 2023])):
            with self.subTest(ticker=ticker, years=years), \
                 patch.object(pipeline_runs, '_pid_alive', return_value=False), \
                 patch.object(pipeline_runs.subprocess, 'Popen', return_value=SimpleNamespace(pid=54321)) as popen, \
                 patch.object(pipeline_runs, 'Thread'):
                started = pipeline_runs.start_run(ticker, years)
                self.assertEqual(started['ticker'], ticker.lower())
                self.assertEqual(started['years'], years)
                self.assertNotIn('--retry-failed', popen.call_args.args[0])
                self.assertEqual((folder / 'ui_run.json').read_bytes(), saved_job)
                self.assertEqual(checkpoint.read_text(), '{"completed":15}')

    def test_database_progress_only_marks_completed_save_steps(self):
        progress = {"stages": [
            {"stage": "save_text_2023", "phase": "preprocessing", "status": "complete"},
            {"stage": "text_2024", "phase": "preprocessing", "status": "running"},
            {"stage": "api_tables_and_save", "phase": "preprocessing", "status": "running"},
            {"stage": "align_and_save_2023_2024", "phase": "alignment", "status": "complete"},
            {"stage": "align_and_save_2024_2025", "phase": "alignment", "status": "running"},
            {"stage": "llm_changes_2023_2024", "phase": "change-analysis", "status": "complete"},
        ]}
        phases = {phase["phase"]: phase for phase in pipeline_runs.phase_progress([2023, 2024, 2025], progress)}
        self.assertEqual((phases["preprocessing"]["saved"], phases["preprocessing"]["total"]), (1, 6))
        self.assertEqual(phases["preprocessing"]["status"], "running")
        self.assertEqual(phases["preprocessing"]["active"], ["text_2024", "api_tables_and_save"])
        self.assertEqual(phases["extraction"]["status"], "waiting")
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
            if os.name == 'nt':
                self.assertEqual(popen.call_args.kwargs['creationflags'],
                                 subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            else:
                self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(popen.call_args.kwargs['env']['PYTHONIOENCODING'], 'utf-8')
            self.assertEqual(job["status"], "running")
            self.assertNotIn("--retry-failed", command)
            self.assertFalse(job["retry_failed"])
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
        relative = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
        python = pipeline_runs.ROOT / '.venv' / relative
        python.parent.mkdir(parents=True)
        python.write_text("")
        with patch.object(pipeline_runs.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)) as popen, \
             patch.object(pipeline_runs, "Thread"):
            pipeline_runs.start_run("unh", [2023, 2024])
        self.assertEqual(popen.call_args.args[0][0], str(python))

    def test_other_platform_virtual_environment_is_never_executed(self):
        relative = 'bin/python' if os.name == 'nt' else 'Scripts/python.exe'
        foreign = pipeline_runs.ROOT / '.venv' / relative
        foreign.parent.mkdir(parents=True)
        foreign.write_text('', encoding='utf-8')
        self.assertEqual(pipeline_runs._pipeline_python(), sys.executable)

    def test_explicit_failed_request_retry_is_passed_to_runner_and_recorded(self):
        with patch.object(pipeline_runs.subprocess, "Popen", return_value=SimpleNamespace(pid=12345)) as popen, \
             patch.object(pipeline_runs, "Thread"):
            job = pipeline_runs.start_run("nvda", [2024, 2025], retry_failed=True)
        self.assertIn("--retry-failed", popen.call_args.args[0])
        self.assertTrue(job["retry_failed"])

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

    def test_saved_years_do_not_hide_a_running_or_failed_table_merge(self):
        progress = {'stages': [
            {'stage': name, 'phase': 'preprocessing', 'status': 'complete'}
            for name in ('save_text_2023', 'save_text_2024', 'save_xbrl_2023', 'save_xbrl_2024')
        ]}
        batch_entry = {'stage': 'api_tables_and_save', 'phase': 'preprocessing', 'status': 'running'}
        progress['stages'].append(batch_entry)
        for status in ('running', 'stopped', 'complete'):
            batch_entry['status'] = status
            phase = pipeline_runs.phase_progress([2023, 2024], progress)[0]
            self.assertEqual((phase['saved'], phase['status']), (4, status))

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
            patch.dict(os.environ, {"SEC_UI_BOOTSTRAP_TEST_DATA": "0"}),
            patch.object(pipeline_runs, "ROOT", Path(self.temp.name)),
            patch.object(pipeline_runs, "_postgres_documents", return_value={}),
            patch.object(pipeline_runs, "_history_documents", return_value={}),
            patch.object(pipeline_runs, '_history_store', return_value=MemoryArtifactStore()),
        ):
            context.start()
            self.addCleanup(context.stop)

    def page(self):
        from streamlit.testing.v1 import AppTest

        path = Path(__file__).resolve().parents[1] / "app/pages/0_Run_Pipeline.py"
        page = AppTest.from_file(str(path), default_timeout=20).run()
        self.assertFalse(page.exception, [error.value for error in page.exception])
        return page

    def history_documents(self):
        return {
            pipeline_runs.HISTORY_PREFIX + 'wfc.json': {
                'ticker': 'wfc', 'years': [2023, 2024], 'run_id': 'wfc-past',
                'status': 'stopped', 'exit_code': 1,
                'started_at': '2026-11-04T23:10:00Z', 'completed_at': '2026-11-04T23:20:00Z',
                'activity_log': 'Materiality request timed out.',
                'progress': {'stages': [{'stage': 'materiality_2023_2024', 'status': 'stopped', 'exit_code': 1}]}},
            pipeline_runs.HISTORY_PREFIX + 'amd.json': {
                'ticker': 'amd', 'years': [2022, 2023], 'run_id': 'amd-past',
                'status': 'complete', 'started_at': '2026-11-03T01:00:00Z',
                'completed_at': '2026-11-03T02:00:00Z',
                'progress': {'full_pipeline_complete': True}},
        }

    def test_history_filters_show_past_company_years_singapore_time_and_details(self):
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()):
            page = self.page()
            self.assertEqual([tab.label for tab in page.tabs], ['Run setup & progress', 'Run history'])
            table = page.dataframe[0].value
            self.assertEqual(table['Company'].tolist(), ['WFC', 'AMD'])
            self.assertEqual(table.iloc[0]['Fiscal years'], '2023, 2024')
            self.assertEqual(table.iloc[0]['Started (SGT)'], '05 Nov 2026, 07:10:00')
            self.assertTrue(any(code.value == 'Materiality request timed out.' for code in page.code))
            self.assertTrue(any(button.proto.label == 'Download run history CSV'
                                for button in page.get('download_button')))
            page.selectbox(key='pipeline_history_status').select('Completed').run()
            self.assertEqual(page.dataframe[0].value['Company'].tolist(), ['AMD'])
            page.multiselect(key='pipeline_history_years').select(2024).run()
            self.assertTrue(any(info.value == 'No runs match these filters.' for info in page.info))
            page.multiselect(key='pipeline_history_years').unselect(2024).run()
            page.selectbox(key='pipeline_history_status').select('All statuses').run()
            page.selectbox(key='pipeline_history_company').select('WFC').run()
            self.assertEqual(page.dataframe[0].value['Company'].tolist(), ['WFC'])
        self.assertFalse(page.exception)

    def test_history_selection_fills_run_setup_without_starting_pipeline(self):
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            page.selectbox(key='pipeline_history_run').select('amd-past').run()
            page.button(key='pipeline_history_load').click().run()
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'AMD')
            self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2022, 2023')
            self.assertFalse(page.exception)
        start.assert_not_called()

    def test_run_setup_defaults_to_newest_history_row_and_reads_its_progress(self):
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()), \
             patch.object(pipeline_runs, 'read_run', return_value=(None, None)) as read, \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'WFC')
            self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024')
            self.assertTrue(any('WFC | FY2023, FY2024' in caption.value for caption in page.caption))
            self.assertTrue(all(call.args == ('WFC', [2023, 2024]) for call in read.call_args_list))
        start.assert_not_called()

    def test_run_setup_prefers_latest_actual_run_over_newer_starter_imports(self):
        documents = self.history_documents()
        documents['pipeline_runs/amd/2023-2024-2025/status.json'] = {
            'ticker': 'amd', 'years': [2023, 2024, 2025],
            'started_at': '2026-11-06T01:00:00Z', 'completed_at': '2026-11-06T01:00:00Z',
            'full_pipeline_complete': True, 'completion_source': 'starter_data', 'stages': []}
        with patch.object(pipeline_runs, '_history_documents', return_value=documents), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
        self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'WFC')
        self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024')
        # History still shows every entry in chronological order.
        self.assertEqual(page.dataframe[0].value['Company'].tolist(), ['AMD', 'WFC', 'AMD'])
        start.assert_not_called()

    def test_saved_actual_checkpoint_takes_priority_over_starter_data(self):
        documents = self.history_documents()
        documents['pipeline_runs/swks/2023-2024-2025/status.json'] = {
            'ticker': 'swks', 'years': [2023, 2024, 2025],
            'started_at': '2026-11-05T01:00:00Z', 'full_pipeline_complete': True, 'stages': []}
        documents['pipeline_runs/amd/2023-2024-2025/status.json'] = {
            'ticker': 'amd', 'years': [2023, 2024, 2025],
            'started_at': '2026-11-06T01:00:00Z',
            'full_pipeline_complete': True, 'completion_source': 'starter_data', 'stages': []}
        with patch.object(pipeline_runs, '_history_documents', return_value=documents):
            page = self.page()
        self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'SWKS')
        self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024, 2025')

    def test_only_starter_history_remains_available_as_fallback(self):
        documents = {'pipeline_runs/nvda/2023-2024-2025/status.json': {
            'ticker': 'nvda', 'years': [2023, 2024, 2025],
            'started_at': '2026-11-06T01:00:00Z',
            'full_pipeline_complete': True, 'completion_source': 'starter_data', 'stages': []}}
        with patch.object(pipeline_runs, '_history_documents', return_value=documents):
            page = self.page()
        self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'NVDA')
        self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024, 2025')

    def test_latest_history_default_does_not_overwrite_edits_when_history_refreshes(self):
        documents = self.history_documents()
        with patch.object(pipeline_runs, '_history_documents', side_effect=lambda: documents):
            page = self.page()
            page.text_input(key='pipeline_setup_ticker').set_value('NVDA').run()
            page.text_input(key='pipeline_setup_years').set_value('2021, 2022').run()
            documents[pipeline_runs.HISTORY_PREFIX + 'new.json'] = {
                'run_id': 'newest', 'ticker': 'usb', 'years': [2024, 2025],
                'started_at': '2026-11-05T23:00:00Z', 'status': 'complete'}
            page.run()
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'NVDA')
            self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2021, 2022')
            self.assertFalse(page.exception)

    def test_explicit_url_selection_takes_precedence_over_history_default(self):
        from streamlit.testing.v1 import AppTest

        path = Path(__file__).resolve().parents[1] / 'app/pages/0_Run_Pipeline.py'
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()):
            page = AppTest.from_file(str(path), default_timeout=20)
            page.query_params.update(ticker='CAT', years='2020, 2021')
            page.run()
            self.assertFalse(page.exception)
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'CAT')
            self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2020, 2021')

    def test_empty_history_keeps_the_existing_sample_defaults(self):
        page = self.page()
        self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'AMD')
        self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024, 2025')

    def test_history_selection_cannot_change_setup_during_an_active_run(self):
        active = {'ticker': 'usb', 'years': [2023, 2024], 'run_id': 'active', 'status': 'running'}
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()), \
             patch.object(pipeline_runs, 'active_run', return_value=active):
            page = self.page()
            self.assertTrue(page.button(key='pipeline_history_load').disabled)
            self.assertTrue(page.button(key='pipeline_history_resume').disabled)
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'USB')

    def test_failed_history_run_resumes_its_company_years_without_changing_other_history(self):
        documents = self.history_documents()
        with patch.object(pipeline_runs, '_history_documents', return_value=documents), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            page.text_input(key='pipeline_setup_ticker').set_value('NVDA').run()
            page.text_input(key='pipeline_setup_years').set_value('2021, 2022').run()
            self.assertFalse(page.button(key='pipeline_history_resume').disabled)
            page.button(key='pipeline_history_resume').click().run()
            self.assertFalse(page.exception)
            self.assertEqual(page.text_input(key='pipeline_setup_ticker').value, 'WFC')
            self.assertEqual(page.text_input(key='pipeline_setup_years').value, '2023, 2024')
            self.assertTrue(any('Resumed WFC | FY2023, FY2024' in item.value for item in page.success))
            self.assertEqual(documents[pipeline_runs.HISTORY_PREFIX + 'wfc.json']['status'], 'stopped')
        start.assert_called_once_with('WFC', [2023, 2024], resume_only=True, retry_failed=False)

    def test_old_failed_history_cannot_resume_after_a_later_attempt_completed(self):
        documents = self.history_documents()
        documents[pipeline_runs.HISTORY_PREFIX + 'later.json'] = {
            'ticker': 'wfc', 'years': [2023, 2024], 'run_id': 'wfc-completed',
            'status': 'complete', 'started_at': '2026-11-05T01:00:00Z',
            'progress': {'full_pipeline_complete': True}}
        with patch.object(pipeline_runs, '_history_documents', return_value=documents), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            page.selectbox(key='pipeline_history_run').select('wfc-past').run()
            self.assertTrue(page.button(key='pipeline_history_resume').disabled)
            self.assertTrue(any('completed in a later attempt' in item.value for item in page.info))
            page.selectbox(key='pipeline_history_run').select('wfc-completed').run()
            self.assertFalse(any(button.key == 'pipeline_history_resume' for button in page.button))
        start.assert_not_called()

    def test_current_database_completion_blocks_history_resume_even_before_history_refresh(self):
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()), \
             patch.object(pipeline_runs, 'read_run', return_value=(None, {'full_pipeline_complete': True})), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            self.assertTrue(page.button(key='pipeline_history_resume').disabled)
        start.assert_not_called()

    def test_history_resume_uses_latest_attempt_retry_option_and_resets_for_a_new_failure(self):
        documents = self.history_documents()
        latest = documents[pipeline_runs.HISTORY_PREFIX + 'wfc.json']
        latest['activity_log'] = 'An earlier API attempt failed. Use --retry-failed for another attempt.'
        with patch.object(pipeline_runs, '_history_documents', side_effect=lambda: documents), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            self.assertTrue(page.button(key='pipeline_history_resume').disabled)
            page.checkbox(key='pipeline_history_retry_WFC_2023-2024_wfc-past').check().run()
            self.assertFalse(page.button(key='pipeline_history_resume').disabled)
            latest['run_id'] = 'new-failure'
            page.run()
            self.assertTrue(page.button(key='pipeline_history_resume').disabled)
            page.checkbox(key='pipeline_history_retry_WFC_2023-2024_new-failure').check().run()
            page.button(key='pipeline_history_resume').click().run()
            self.assertFalse(page.exception)
        start.assert_called_once_with('WFC', [2023, 2024], resume_only=True, retry_failed=True)

    def test_history_resume_automatically_permits_retry_after_exhausted_stage_retries(self):
        documents = self.history_documents()
        documents[pipeline_runs.HISTORY_PREFIX + 'wfc.json']['progress']['stages'][0]['retry_exhausted'] = True
        with patch.object(pipeline_runs, '_history_documents', return_value=documents), \
             patch.object(pipeline_runs, 'start_run') as start:
            page = self.page()
            page.button(key='pipeline_history_resume').click().run()
            self.assertFalse(page.exception)
        start.assert_called_once_with('WFC', [2023, 2024], resume_only=True, retry_failed=True)

    def test_history_resume_failure_stays_visible_without_automatic_relaunch(self):
        with patch.object(pipeline_runs, '_history_documents', return_value=self.history_documents()), \
             patch.object(pipeline_runs, 'start_run', side_effect=pipeline_runs.RunError('Database unavailable')) as start:
            page = self.page()
            page.button(key='pipeline_history_resume').click().run()
            self.assertTrue(any(item.value == 'Database unavailable' for item in page.error))
            self.assertFalse(page.exception)
        self.assertEqual(start.call_count, 1)

    def test_history_filters_remain_selected_when_a_run_status_changes(self):
        documents = self.history_documents()
        with patch.object(pipeline_runs, '_history_documents', side_effect=lambda: documents):
            page = self.page()
            page.selectbox(key='pipeline_history_status').select('Failed').run()
            documents[pipeline_runs.HISTORY_PREFIX + 'wfc.json']['status'] = 'complete'
            documents[pipeline_runs.HISTORY_PREFIX + 'wfc.json']['progress'] = {'full_pipeline_complete': True}
            page.run()
            self.assertFalse(page.exception)
            self.assertEqual(page.selectbox(key='pipeline_history_status').value, 'Failed')
            self.assertTrue(any(info.value == 'No runs match these filters.' for info in page.info))

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

    def test_failed_ai_request_requires_explicit_checkbox_before_resume(self):
        progress = {"stages": [{"stage": "materiality_2024_2025", "phase": "materiality", "status": "stopped"}]}
        job = {"status": "stopped", "run_id": "failed-ai", "exit_code": 1}
        log = "Materiality stopped: An earlier API attempt failed. Use --retry-failed for another paid attempt."
        with patch.object(pipeline_runs, "read_run", return_value=(job, progress)), \
             patch.object(pipeline_runs, "log_tail", return_value=log), \
             patch.object(pipeline_runs, "start_run") as start:
            page = self.page()
            page.checkbox(key="confirm_multi_year").check().run()
            resume = next(button for button in page.button if button.label == "Resume pipeline")
            self.assertTrue(resume.disabled)
            self.assertTrue(any("Successful responses and completed stages will be reused" in message.value
                                for message in page.warning))
            self.assertFalse(any("charges" in message.value for message in page.warning))
            retry = next(box for box in page.checkbox if box.label == "Retry failed AI requests on resume")
            retry.check().run()
            resume = next(button for button in page.button if button.label == "Resume pipeline")
            self.assertFalse(resume.disabled)
            resume.click().run()
        start.assert_called_once_with("AMD", [2023, 2024, 2025], retry_failed=True)
        self.assertFalse(page.exception)

    def test_new_failed_invocation_does_not_reuse_the_previous_retry_checkbox(self):
        progress = {"stages": [{"stage": "materiality_2024_2025", "phase": "materiality", "status": "stopped"}]}
        job = {"status": "stopped", "run_id": "attempt-one", "exit_code": 1}
        with patch.object(pipeline_runs, "read_run", side_effect=lambda *args: (dict(job), progress)), \
             patch.object(pipeline_runs, "log_tail", return_value="Use --retry-failed for another attempt."):
            page = self.page()
            page.checkbox(key="confirm_multi_year").check().run()
            next(box for box in page.checkbox if box.label == "Retry failed AI requests on resume").check().run()
            job["run_id"] = "attempt-two"
            page.run()
            self.assertFalse(next(box for box in page.checkbox if box.label == "Retry failed AI requests on resume").value)
            self.assertTrue(next(button for button in page.button if button.label == "Resume pipeline").disabled)
        self.assertFalse(page.exception)


if __name__ == "__main__":
    unittest.main()
