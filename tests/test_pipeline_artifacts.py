"""Cache/progress persistence, checkpoint recovery, and explicit local exports."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from artifact_support import MemoryArtifactStore
from sec_disclosure.storage.artifacts import ArtifactPath, PostgresWorkspace, checkpoint_saver, ArtifactStorageError
from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.table_extraction.extract_item8_xbrl_api import write_json as write_api_json
from sec_disclosure.table_extraction.extract_10k_tables_xbrl import write_result


class PipelineArtifactTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryArtifactStore()
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)

    def test_json_and_binary_outputs_commit_without_creating_local_files(self):
        with PostgresWorkspace('test', store=self.store) as workspace:
            root = workspace.root
            write_json(root / 'requests/completed.json', {'result': 'saved'})
            write_api_json({'metrics': [1, 2]}, root / 'reports/api.json')
            write_result({'summary': {}}, root / 'reports/tables.json')
            (root / 'download.zip').write_bytes(b'\x00\xff\x01')
            with (root / 'reports/evidence.txt').open('w', encoding='utf-8') as stream:
                stream.write('Evidence – 原文\n')
            self.assertEqual(json.loads((root / 'requests/completed.json').read_text()), {'result': 'saved'})
            self.assertEqual((root / 'download.zip').read_bytes(), b'\x00\xff\x01')
            self.assertEqual((root / 'reports/evidence.txt').read_text(), 'Evidence – 原文\n')
            self.assertFalse(Path(str(root)).exists())
        self.assertEqual(list(self.local.iterdir()), [])
        self.assertIn('requests/completed.json', self.store.rows)

    def test_atomic_replacement_and_directory_move_preserve_cached_content(self):
        with PostgresWorkspace('test', store=self.store) as workspace:
            root = workspace.root
            write_json(root / 'jobs/a.json', {'complete': True})
            (root / 'jobs').rename(root / 'archive')
            self.assertFalse((root / 'jobs').exists())
            self.assertEqual(json.loads((root / 'archive/a.json').read_text()), {'complete': True})
            self.assertEqual([p.name for p in root.glob('*.json')], [])
            self.assertEqual([p.name for p in root.rglob('*.json')], ['a.json'])

    def test_checkpoint_recovers_in_another_workspace_and_shared_writes_are_not_lost(self):
        with PostgresWorkspace('test', store=self.store) as workspace:
            database = workspace.root / 'graph/checkpoints.sqlite'
            with checkpoint_saver(database) as saver:
                with saver.cursor() as cursor:
                    cursor.execute('CREATE TABLE test_jobs (id INTEGER PRIMARY KEY)')
            def save(index):
                with checkpoint_saver(database) as shared:
                    with shared.cursor() as cursor:
                        cursor.execute('INSERT INTO test_jobs VALUES (?)', (index,))
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(save, range(18)))
            self.assertFalse(Path(str(database)).exists())
        self.assertIn('graph/checkpoints.sqlite', self.store.rows)
        with PostgresWorkspace('test', store=self.store) as workspace:
            with checkpoint_saver(workspace.root / 'graph/checkpoints.sqlite') as saver:
                self.assertEqual(saver.conn.execute('SELECT count(*) FROM test_jobs').fetchone()[0], 18)

    def test_parallel_graph_jobs_share_a_checkpoint_safely_and_recover(self):
        from threading import Lock
        from types import SimpleNamespace
        from sec_disclosure.agents.alignment_graph import AlignmentAgentGraph
        with PostgresWorkspace('test', store=self.store) as workspace:
            runtime = SimpleNamespace(output=workspace.root, args=SimpleNamespace(max_steps=4),
                                      checkpoint_setup_lock=Lock())
            data = SimpleNamespace(item_for=lambda scope: '1')
            def run(index):
                return AlignmentAgentGraph(runtime, 'matching', str(index), 'test system',
                                           {'item': '1'}, data, [], offline=True).run()
            with ThreadPoolExecutor(max_workers=9) as pool:
                results = list(pool.map(run, range(36)))
            self.assertTrue(all(result[0] is None and result[1] == [] for result in results))
        with PostgresWorkspace('test', store=self.store) as workspace:
            with checkpoint_saver(workspace.root / 'graph/checkpoints.sqlite') as saver:
                self.assertEqual(saver.conn.execute('SELECT count(DISTINCT thread_id) FROM checkpoints').fetchone()[0], 36)

    def test_optional_export_copies_reports_and_usage_but_excludes_all_cache(self):
        destination = self.local / 'export'
        with PostgresWorkspace('test', store=self.store) as workspace:
            root = workspace.root
            for name in ('reports/result.json', 'reports/token_usage.json', 'reports/manifest.json',
                         'reports/requests/request.json', 'reports/jobs/job.json',
                         'reports/graph/checkpoints.sqlite', 'pipeline_runs/a/status.json',
                         'reports/filing_page_cache/fingerprint/map.json'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{}')
            self.assertFalse(destination.exists())
            self.assertEqual(workspace.export(destination, source=root / 'reports'), 2)
        self.assertEqual(sorted(p.name for p in destination.iterdir()), ['result.json', 'token_usage.json'])

    def test_dry_run_does_not_create_or_change_cache_entries(self):
        before = dict(self.store.rows)
        with PostgresWorkspace('test', store=self.store, read_only=True) as workspace:
            self.assertFalse((workspace.root / 'missing.json').exists())
        self.assertEqual(self.store.rows, before)

    def test_old_cache_is_imported_without_rewriting_local_files_or_using_them_again(self):
        source = self.local / 'legacy'
        (source / 'requests').mkdir(parents=True)
        original = source / 'requests/call.json'
        original.write_text('{"completed":true}')
        with PostgresWorkspace('test', store=self.store) as workspace:
            self.assertTrue(workspace.import_local(source))
            imported = ArtifactPath(original)
            self.assertEqual(json.loads(imported.read_text()), {'completed': True})
            imported.write_text('{"completed":"updated in database"}')
            self.assertEqual(original.read_text(), '{"completed":true}')
        with PostgresWorkspace('test', store=self.store) as workspace:
            self.assertFalse(workspace.import_local(source))
            self.assertEqual(json.loads(ArtifactPath(original).read_text())['completed'], 'updated in database')

    def test_import_preserves_newer_postgres_artifacts_and_creates_missing_directories(self):
        source = self.local / 'legacy'
        (source / 'requests').mkdir(parents=True)
        (source / 'requests/call.json').write_text('{"source":"old local cache"}')
        (source / 'requests/new.json').write_text('{"source":"import me"}')
        self.store.put('requests/call.json', b'{"source":"newer database cache"}')
        with PostgresWorkspace('test', store=self.store) as workspace:
            self.assertTrue(workspace.import_local(source))
            self.assertEqual(json.loads((workspace.root / 'requests/call.json').read_bytes())['source'], 'newer database cache')
            self.assertEqual(json.loads((workspace.root / 'requests/new.json').read_bytes())['source'], 'import me')
            self.assertTrue((workspace.root / 'requests').is_dir())

    def test_peer_usage_reads_one_database_snapshot_and_excludes_nested_files(self):
        from unittest.mock import patch
        from sec_disclosure.agents.peer_comparison import _summarize_usage
        with PostgresWorkspace('test', store=self.store) as workspace:
            requests = workspace.root / 'requests'
            for name, value in (
                ('completed.json', {'status': 'completed', 'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}),
                ('failed.json', {'status': 'failed'}),
                ('started.json', {'status': 'started'}),
                ('nested/ignored.json', {'status': 'completed', 'usage': {'prompt_tokens': 99, 'completion_tokens': 99, 'total_tokens': 198}}),
            ):
                write_json(requests / name, value)
            (requests / 'invalid.json').write_bytes(b'\xff')
            with patch.object(self.store, 'files', wraps=self.store.files) as files, \
                 patch.object(self.store, 'get', side_effect=AssertionError('per-request database read')):
                self.assertEqual(_summarize_usage(requests), {
                    'requests': 3, 'prompt_tokens': 7, 'completion_tokens': 3,
                    'total_tokens': 10, 'unknown_usage_requests': 2,
                })
                files.assert_called_once_with('requests')

    def test_imported_extraction_cache_replays_postgres_inputs_but_rejects_changed_hashes(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from unittest.mock import patch
        from sec_disclosure.llm import disclosures
        from sec_disclosure.llm.config import LLMConfig
        from sec_disclosure.storage.pipeline_snapshots import file_document
        from test_parallel_extraction import ParallelExtractionTests
        fixture = ParallelExtractionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        args = ['--ticker', 'AMD', '--year', '2023', '--no-store-postgres', '--request-interval', '0', '--input-source', 'files']
        config = LLMConfig('https://example.test/v1', 'fake-key', 'default')
        with patch.object(disclosures, 'load_config', return_value=config), \
             patch.object(disclosures, 'request_completion', side_effect=fixture.response) as requests, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(disclosures.main(args + ['--data-dir', str(fixture.root)]), 0)
            original = (fixture.root / 'disclosures/amd/2023/disclosures.json').read_bytes()
            calls = requests.call_count
            with PostgresWorkspace('test', store=self.store) as workspace:
                workspace.import_local(fixture.root)
                documents = {name: file_document(workspace.root / 'raw/amd/2023' / name)
                             for name in ('2023_chunks.json', '2023_chunk_sentences.json')}
                snapshots = {('raw', '2023', '2023'): {'documents': documents}}
                pg_args = args + ['--data-dir', str(workspace.root), '--input-source', 'postgres']
                with patch('sec_disclosure.storage.pipeline_snapshots.read_snapshots', return_value=snapshots):
                    self.assertEqual(disclosures.main(pg_args), 0)
                    self.assertEqual(requests.call_count, calls)
                    self.assertEqual((workspace.root / 'disclosures/amd/2023/disclosures.json').read_bytes(), original)
                    documents['2023_chunks.json']['sha256'] = '0' * 64
                    self.assertEqual(disclosures.main(pg_args), 1)
                    self.assertEqual(requests.call_count, calls)
            self.assertEqual((fixture.root / 'disclosures/amd/2023/disclosures.json').read_bytes(), original)

    def test_unregistered_database_paths_never_fall_back_to_disk(self):
        path = ArtifactPath('/__sec_postgres__/inactive/requests/test.json')
        self.assertEqual(path.name, 'test.json')
        self.assertEqual(str(path.resolve()), str(path))
        for operation in (path.exists, path.read_bytes, lambda: path.write_text('{}')):
            with self.assertRaises(ArtifactStorageError):
                operation()

    def test_closed_legacy_wal_checkpoint_imports_and_recovers_in_memory(self):
        source = self.local / 'legacy'
        source.mkdir()
        database = source / 'checkpoints.sqlite'
        conn = sqlite3.connect(database)
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE saved_jobs (id INTEGER)')
        conn.execute('INSERT INTO saved_jobs VALUES (7)')
        conn.commit()
        conn.close()
        original = database.read_bytes()
        with PostgresWorkspace('test', store=self.store) as workspace:
            workspace.import_local(source)
            with checkpoint_saver(workspace.root / 'checkpoints.sqlite') as saver:
                self.assertEqual(saver.conn.execute('SELECT id FROM saved_jobs').fetchone()[0], 7)
        self.assertEqual(database.read_bytes(), original)

    def test_imported_path_aliases_are_limited_to_data_and_leave_configuration_local(self):
        source = self.local / 'legacy'
        raw = source / 'raw/amd/2024/records.json'
        raw.parent.mkdir(parents=True)
        raw.write_text('[]')
        config = source / 'settings.env'
        config.write_text('SETTING=local-configuration')
        with PostgresWorkspace('test', store=self.store) as workspace:
            workspace.import_local(source, prefixes=['raw/amd'])
            self.assertEqual(ArtifactPath(raw).artifact_key, 'raw/amd/2024/records.json')
            self.assertFalse(hasattr(ArtifactPath(config), 'artifact_store'))
            self.assertEqual(ArtifactPath(config).read_text(), 'SETTING=local-configuration')

    def test_offline_xbrl_pipeline_and_merge_use_postgres_artifacts_without_disk_writes(self):
        from contextlib import redirect_stdout, redirect_stderr
        import io
        from test_run_api_metrics_pipeline import setup
        from sec_disclosure.table_extraction import run_api_metrics_pipeline as api_pipeline
        from sec_disclosure.table_extraction import merge_api_metrics
        fixture = self.local / 'legacy-inputs'
        fixture.mkdir()
        filing, response, output, common, _, _ = setup(fixture)
        before = {str(path): path.read_bytes() for path in fixture.rglob('*') if path.is_file()}
        with PostgresWorkspace('api-test', store=self.store) as workspace, \
             redirect_stdout(io.StringIO()) as log, redirect_stderr(io.StringIO()) as errors:
            workspace.import_local(fixture)
            code = api_pipeline.main(['--filing', str(filing), '--xbrl-json', str(response),
                '--company', 'Intel', '--offline', '--no-db', *common,
                '--sec-cache', str(workspace.root / 'sec_cache')])
            self.assertEqual(code, 0, log.getvalue() + errors.getvalue())
            self.assertEqual(merge_api_metrics.main(['--company', 'Intel', '--years', '2023',
                '--input-dir', str(output)]), 0, log.getvalue() + errors.getvalue())
            self.assertTrue(ArtifactPath(output / 'intel_2023_api_metrics_with_values.json').is_file())
        after = {str(path): path.read_bytes() for path in fixture.rglob('*') if path.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
