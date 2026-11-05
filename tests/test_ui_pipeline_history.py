"""Run history preserves attempts without starting real pipelines or using a database."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from artifact_support import MemoryArtifactStore
from sec_ui import data, pipeline_runs


@pytest.fixture
def history(monkeypatch, tmp_path):
    store = MemoryArtifactStore()
    monkeypatch.setattr(pipeline_runs, 'ROOT', tmp_path)
    monkeypatch.setattr(pipeline_runs, '_history_store', lambda: store)
    monkeypatch.setattr(pipeline_runs, '_postgres_documents', lambda *args: {})
    monkeypatch.setattr(pipeline_runs, '_history_documents', lambda: {
        key: json.loads(content) for key, content in store.files('pipeline_run_history')})
    monkeypatch.setattr(pipeline_runs, '_pid_alive', lambda pid: False)
    monkeypatch.setattr(pipeline_runs, 'Thread', MagicMock())
    return store


def attempt(run_id='old', **updates):
    return {'ticker': 'wfc', 'years': [2023, 2024], 'run_id': run_id,
            'started_at': '2026-11-04T01:00:00+00:00',
            'completed_at': '2026-11-04T02:00:00+00:00',
            'status': 'stopped', 'exit_code': 1, **updates}


def test_resume_keeps_failed_attempt_and_saved_log_after_local_files_are_gone(history, monkeypatch):
    launch = MagicMock(return_value=SimpleNamespace(pid=12345))
    monkeypatch.setattr(pipeline_runs.subprocess, 'Popen', launch)
    first = pipeline_runs.start_run('WFC', [2024, 2023])
    folder = pipeline_runs.run_folder('wfc', first['years'])
    (folder / first['log_file']).write_text('First attempt failed at materiality.\n')
    pipeline_runs._finish_job(SimpleNamespace(wait=lambda: 1), folder, first['run_id'])
    first_key = pipeline_runs.HISTORY_PREFIX + first['run_id'] + '.json'
    original_failed_record = history.get(first_key)[1]

    second = pipeline_runs.start_run('WFC', [2023, 2024], retry_failed=True)
    progress = {'ticker': 'wfc', 'years': [2023, 2024], 'last_invocation_at': second['started_at'],
                'full_pipeline_complete': True, 'stages': [
                    {'stage': 'synthesis_and_save', 'status': 'complete'}]}
    status_key = pipeline_runs._status_key('wfc', second['years'])
    monkeypatch.setattr(pipeline_runs, '_postgres_documents', lambda *args: {status_key: progress})
    pipeline_runs._finish_job(SimpleNamespace(wait=lambda: 0), folder, second['run_id'])
    assert first['run_id'] != second['run_id']
    assert '--retry-failed' in launch.call_args.args[0]
    assert history.get(first_key)[1] == original_failed_record
    assert not list((pipeline_runs.ROOT / 'data').rglob('pipeline_run_history*'))

    for path in folder.iterdir():
        path.unlink()
    records, warning = pipeline_runs.read_run_history()
    assert warning is None
    assert [(row['run_id'], row['display_status']) for row in records] == [
        (second['run_id'], 'Completed'), (first['run_id'], 'Failed')]
    assert pipeline_runs.history_log(records[1]) == 'First attempt failed at materiality.'
    assert records[0]['last_stage'] == 'synthesis_and_save'


def test_database_failure_prevents_launch_and_leaves_safe_error(history, monkeypatch):
    launch = MagicMock()
    monkeypatch.setattr(pipeline_runs.subprocess, 'Popen', launch)
    monkeypatch.setattr(history, 'put', MagicMock(side_effect=RuntimeError('private connection details')))
    with pytest.raises(pipeline_runs.RunError, match='Could not save run history to PostgreSQL'):
        pipeline_runs.start_run('WFC', [2023, 2024])
    launch.assert_not_called()
    job = pipeline_runs._read_json(pipeline_runs.run_folder('wfc', [2023, 2024]) / 'ui_run.json')
    assert job['status'] == 'stopped'
    assert 'private connection details' not in job['error']


def test_restart_without_process_monitor_preserves_completed_checkpoint_before_resume(history, monkeypatch):
    monkeypatch.setattr(pipeline_runs.subprocess, 'Popen', MagicMock(return_value=SimpleNamespace(pid=12345)))
    first = pipeline_runs.start_run('WFC', [2023, 2024])
    completed = {'last_invocation_at': first['started_at'], 'full_pipeline_complete': True,
                 'completed_at': first['started_at'], 'stages': []}
    key = pipeline_runs._status_key('wfc', [2023, 2024])
    monkeypatch.setattr(pipeline_runs, '_postgres_documents', lambda *args: {key: completed})
    # Simulate Streamlit restarting: the old runner finished without _finish_job.
    second = pipeline_runs.start_run('WFC', [2023, 2024])
    monkeypatch.setattr(pipeline_runs, '_postgres_documents', lambda *args: {})
    pipeline_runs._finish_job(SimpleNamespace(wait=lambda: 1), pipeline_runs.run_folder('wfc', [2023, 2024]), second['run_id'])
    rows, _ = pipeline_runs.read_run_history()
    assert [(row['run_id'], row['display_status']) for row in rows] == [
        (second['run_id'], 'Failed'), (first['run_id'], 'Completed')]


def test_restart_without_stop_monitor_archives_stopped_attempt_before_resume(history, monkeypatch):
    monkeypatch.setattr(pipeline_runs.subprocess, 'Popen', MagicMock(return_value=SimpleNamespace(pid=12345)))
    first = pipeline_runs.start_run('WFC', [2023, 2024])
    first.update(status='stopping', stopped_by_user=True)
    pipeline_runs._write_job(pipeline_runs.run_folder('wfc', [2023, 2024]), first)
    pipeline_runs.start_run('WFC', [2023, 2024])
    rows, _ = pipeline_runs.read_run_history()
    old = next(row for row in rows if row['run_id'] == first['run_id'])
    assert old['display_status'] == 'Stopped by user'


def test_launch_error_and_stop_outcome_are_retained_in_postgres(history, monkeypatch):
    monkeypatch.setattr(pipeline_runs.subprocess, 'Popen', MagicMock(side_effect=OSError('launch failed')))
    with pytest.raises(pipeline_runs.RunError, match='Could not start pipeline'):
        pipeline_runs.start_run('WFC', [2023, 2024])
    rows, _ = pipeline_runs.read_run_history()
    assert len(rows) == 1
    assert rows[0]['display_status'] == 'Failed'
    assert rows[0]['error'] == 'Could not start pipeline (OSError).'

    stopped = attempt('stopped', stopped_by_user=True, exit_code=-15)
    pipeline_runs._record_history(stopped)
    rows, _ = pipeline_runs.read_run_history()
    assert next(row for row in rows if row['run_id'] == 'stopped')['display_status'] == 'Stopped by user'


def test_new_checkpoint_does_not_relabel_an_older_failure(history, monkeypatch):
    old = attempt()
    pipeline_runs._record_history(old)
    latest = attempt('new', started_at='2026-11-04T03:00:00+00:00',
                     completed_at='2026-11-04T04:00:00+00:00', status='complete', exit_code=0)
    pipeline_runs._record_history(latest)
    docs = {key: json.loads(content) for key, content in history.files('pipeline_run_history')}
    docs[pipeline_runs._status_key('wfc', [2023, 2024])] = {
        'last_invocation_at': '2026-11-04T03:00:01+00:00', 'full_pipeline_complete': True,
        'stages': [{'stage': 'synthesis_and_save', 'status': 'complete'}]}
    monkeypatch.setattr(pipeline_runs, '_history_documents', lambda: docs)
    rows, _ = pipeline_runs.read_run_history()
    assert [(row['run_id'], row['display_status']) for row in rows] == [('new', 'Completed'), ('old', 'Failed')]


def test_terminal_database_record_wins_over_stale_local_launcher(history):
    completed = attempt(status='complete', exit_code=0, progress={'full_pipeline_complete': True})
    pipeline_runs._record_history(completed)
    # History stores the original completed snapshot; local launcher can be stale after restart.
    folder = pipeline_runs.run_folder('wfc', [2023, 2024])
    folder.mkdir(parents=True)
    stale = attempt(status='running', pid=12345)
    stale.pop('completed_at')
    pipeline_runs._write_json(folder / 'ui_run.json', stale)
    rows, _ = pipeline_runs.read_run_history()
    assert len(rows) == 1
    assert rows[0]['display_status'] == 'Completed'


def test_failed_checkpoint_explains_a_dead_runner_with_stale_running_metadata(history, monkeypatch):
    folder = pipeline_runs.run_folder('wfc', [2023, 2024])
    folder.mkdir(parents=True)
    stale = attempt(status='running', pid=12345)
    stale.pop('completed_at')
    pipeline_runs._write_json(folder / 'ui_run.json', stale)
    monkeypatch.setattr(pipeline_runs, '_history_documents', lambda: {
        pipeline_runs._status_key('wfc', [2023, 2024]): {
            'last_invocation_at': stale['started_at'],
            'stages': [{'stage': 'text_2023', 'status': 'stopped', 'exit_code': 2}]}})
    rows, _ = pipeline_runs.read_run_history()
    assert len(rows) == 1
    assert rows[0]['display_status'] == 'Failed'


def test_checkpoint_discovery_and_corrupt_records_are_isolated(history, monkeypatch):
    docs = {
        'pipeline_runs/amd/2023-2024/status.json': {
            'last_invocation_at': '2026-11-04T23:10:00Z', 'complete': True, 'until': 'alignment',
            'stages': [None, {'stage': 'align_and_save_2023_2024', 'status': 'complete'}]},
        'pipeline_runs/usb/2023-2024/status.json': {'ticker': None, 'years': []},
        pipeline_runs.HISTORY_PREFIX + 'invalid.json': {'ticker': None},
        pipeline_runs.HISTORY_PREFIX + 'invalid-id.json': attempt(run_id=[]),
        pipeline_runs.HISTORY_PREFIX + 'valid.json': attempt(progress=[], status=[]),
    }
    monkeypatch.setattr(pipeline_runs, '_history_documents', lambda: docs)
    rows, warning = pipeline_runs.read_run_history()
    assert warning is None
    assert len(rows) == 2
    assert rows[0]['ticker'] == 'amd'
    assert rows[0]['display_status'] == 'Completed to alignment'
    assert rows[0]['last_stage'] == 'align_and_save_2023_2024'
    assert pipeline_runs.history_time_label(rows[0]['started_at']) == '05 Nov 2026, 07:10:00'


def test_database_unavailable_shows_local_history_with_warning(history, monkeypatch):
    folder = pipeline_runs.run_folder('wfc', [2023, 2024])
    folder.mkdir(parents=True)
    pipeline_runs._write_json(folder / 'ui_run.json', attempt())
    monkeypatch.setattr(pipeline_runs, '_history_documents', MagicMock(side_effect=pipeline_runs.RunError('Database unavailable')))
    rows, warning = pipeline_runs.read_run_history()
    assert warning == 'Database unavailable'
    assert len(rows) == 1
    assert rows[0]['ticker'] == 'wfc'


def test_reader_is_read_only_and_limits_query_to_run_metadata(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ('pipeline_artifacts',)
    cursor.fetchall.return_value = [('invalid', b'{bad'), ('run', b'[]'), ('good', b'{"ticker":"amd"}')]
    monkeypatch.setattr(data, '_postgres_connection', lambda: connection)
    assert pipeline_runs._history_documents() == {'good': {'ticker': 'amd'}}
    assert connection.read_only is True
    sql, params = cursor.execute.call_args.args
    assert "kind='file'" in sql and 'status\\.json$' in sql
    assert params == ('main', pipeline_runs.HISTORY_PREFIX)
