"""Opt-in peer pipeline checks in an isolated PostgreSQL test database."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import psycopg
from psycopg import sql

from sec_disclosure.agents import peer_comparison as agent
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.pipelines import run_peer_comparison_pipeline as pipeline
from sec_disclosure.storage import peer_comparison as storage
from sec_disclosure.storage import disclosures as disclosure_storage
from sec_disclosure.storage.artifacts import PostgresWorkspace, PostgresArtifactStore, checkpoint_saver
from test_peer_comparison import disclosure


@unittest.skipUnless(os.environ.get("SEC_PEER_DB_INTEGRATION") == "1", "Requires PostgreSQL")
class PeerComparisonPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = "sec_peer_test_" + uuid.uuid4().hex
        base = storage.connection_arguments()
        cls.admin_arguments = {**base, "dbname": "postgres", "autocommit": True}
        cls.arguments = {**base, "dbname": cls.database}
        with psycopg.connect(**cls.admin_arguments) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.database)))
        cls.addClassCleanup(cls.drop_database)
        with psycopg.connect(**cls.arguments) as conn:
            for name in ("01_schema.sql", "02_disclosures_alignments.sql", "04_xbrl_data.sql",
                         "05_pipeline_stage_snapshots.sql", "06_synthesis_reports.sql", "07_peer_comparison_reports.sql"):
                conn.execute((storage.ROOT / "db/init" / name).read_text())

    @classmethod
    def drop_database(cls):
        with psycopg.connect(**cls.admin_arguments) as conn:
            conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(cls.database)))

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = patch.object(storage, "connection_arguments", return_value=self.arguments)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        artifacts_settings = patch.object(disclosure_storage, 'connection_arguments', return_value=self.arguments)
        artifacts_settings.start()
        self.addCleanup(artifacts_settings.stop)
        self.dotenv = patch.object(agent, "dotenv_values", return_value={})
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)
        self.args = ["--ticker", "AAA", "--peers", "BBB", "--year", "2024", "--items", "1A",
                     "--target-selection", "all", "--data-dir", str(self.root)]
        with psycopg.connect(**self.arguments) as conn:
            conn.execute("TRUNCATE filings, alignment_runs, peer_comparison_reports RESTART IDENTITY CASCADE")
            for ticker in ("aaa", "bbb"):
                self.insert_disclosure(conn, ticker, 2024)

    def insert_disclosure(self, conn, ticker, year):
        filing = conn.execute("INSERT INTO filings (company, fiscal_year) VALUES (%s,%s) RETURNING filing_id",
                              (ticker, year)).fetchone()[0]
        record = disclosure(ticker, f"{ticker}_{year}_1A_D001", "1A", "We depend on a single chip supplier.", f"{ticker}-{year}-s1")
        conn.execute("""INSERT INTO disclosures
            (disclosure_id, filing_id, item, topic, summary, content, taxonomy, review_status, sources)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'accepted',%s::jsonb)""", (
            record["disclosure_id"], filing, record["item"], record["topic"], record["summary"],
            record["content"], record["taxonomy"], json.dumps(record["sources"])))

    def test_both_companies_load_from_normalized_postgres_without_local_inputs(self):
        loaded = storage.read_peer_inputs("aaa", ["bbb"], 2024)
        self.assertEqual(set(loaded["disclosures"]), {"aaa", "bbb"})
        rows = agent.load_company(None, "aaa", "2024", reports=loaded["disclosures"]["aaa"])
        self.assertEqual(rows[0]["sentences"][0]["sentence_id"], "aaa-2024-s1")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_batch_cache_import_preserves_existing_bytes_and_recovers_partial_import(self):
        source = self.root / 'legacy'
        (source / 'requests').mkdir(parents=True)
        original = {f'requests/{index:03}.bin': bytes([index % 256, 0, 255]) for index in range(270)}
        for key, value in original.items():
            (source / key).write_bytes(value)
        namespace = 'batch-' + uuid.uuid4().hex
        with PostgresWorkspace(namespace) as workspace:
            cached = workspace.root / 'requests/000.bin'
            cached.parent.mkdir(parents=True)
            cached.write_bytes(b'newer cache')
            self.assertTrue(workspace.import_local(source))
            rows = dict(workspace.store.files('requests'))
            self.assertEqual(rows, {**original, 'requests/000.bin': b'newer cache'})
            self.assertFalse(workspace.import_local(source))
            self.assertEqual(workspace.store.files('request'), [])
        self.assertEqual({key: (source / key).read_bytes() for key in original}, original)

    def test_missing_ticker_wrong_year_and_raw_only_filing_are_rejected(self):
        for ticker, peers, year in (("aaa", ["ccc"], 2024), ("ccc", ["bbb"], 2024), ("aaa", ["bbb"], 2025)):
            with self.subTest(ticker=ticker, peers=peers, year=year), self.assertRaises(storage.PeerInputError):
                storage.read_peer_inputs(ticker, peers, year)
        with psycopg.connect(**self.arguments) as conn:
            conn.execute("DELETE FROM disclosures WHERE disclosure_id LIKE 'bbb_%%'")
        with self.assertRaisesRegex(storage.PeerInputError, "no PostgreSQL disclosures for BBB"):
            storage.read_peer_inputs("aaa", ["bbb"], 2024)

    def test_completed_materiality_and_llm_context_are_read_from_the_database(self):
        with psycopg.connect(**self.arguments) as conn:
            self.insert_disclosure(conn, "aaa", 2023)
            run_id = conn.execute("""INSERT INTO alignment_runs
                (company, previous_year, current_year, schema_version, run_complete)
                VALUES ('aaa',2023,2024,'8',TRUE) RETURNING run_id""").fetchone()[0]
            conn.execute("""INSERT INTO alignments
                (run_id, match_id, status, relationship, source_file, change_analysis, extra)
                VALUES (%s,'change-1','ai_verified','one_to_one','alignments',%s::jsonb,%s::jsonb)""", (
                run_id, json.dumps({"status": "completed", "llm": "Expanded", "final_taxonomy": None}),
                json.dumps({"materiality_analysis": {"materiality": "Yes", "materiality_score": 0.9}})))
            for side, year in (("previous", 2023), ("current", 2024)):
                conn.execute("""INSERT INTO alignment_members
                    (run_id, match_id, side, disclosure_id) VALUES (%s,'change-1',%s,%s)""",
                    (run_id, side, f"aaa_{year}_1A_D001"))
        loaded = storage.read_peer_inputs("aaa", ["bbb"], 2024)
        rows = agent.load_company(None, "aaa", "2024", reports=loaded["disclosures"]["aaa"])
        agent.attach_alignment_context(rows, None, "aaa", "2024", reports=loaded["alignments"]["aaa"])
        selected = agent.select_material_change_targets(rows)
        self.assertEqual(len(selected), 1)
        context = agent._compact_alignment_context(selected[0]["year_over_year_context"])
        self.assertEqual(context[0]["change_taxonomy"], "Expanded")

    def test_pipeline_stores_report_usage_and_resumes_then_blocks_if_peer_is_removed(self):
        response = CompletionResult(json.dumps({"comparisons": [], "no_match": [
            {"peer_ticker": "BBB", "reason": "The retrieved disclosures are not comparable."}]}),
            "test-model", "stop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        with patch.object(agent, "load_config", return_value=SimpleNamespace(model="test-model", base_url="https://example.test")), \
             patch.object(agent, "request_completion", return_value=response) as request, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(pipeline.main(self.args), 0)
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual(pipeline.main(self.args + ['--data-dir', str(self.root / 'other-machine')]), 0)
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual(pipeline.main(self.args + ['--save-local-output']), 0)
            exported = self.root / 'peer_comparison/aaa/2024/bbb'
            self.assertEqual(sorted(p.name for p in exported.iterdir()), ['peer_comparison.json', 'token_usage.json'])
            with psycopg.connect(**self.arguments) as conn:
                rows = conn.execute("SELECT run_complete, report FROM peer_comparison_reports").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0][0])
                self.assertEqual(rows[0][1]["token_usage"]["total_tokens"], 15)
                self.assertEqual(rows[0][1]["input_source"], "postgres")
                conn.execute("DELETE FROM filings WHERE company='bbb'")
            self.assertEqual(pipeline.main(self.args), 1)
        request.assert_called_once()

    def test_graph_checkpoints_recover_from_postgres_without_sqlite_files(self):
        with PostgresWorkspace('graph-test') as workspace:
            database = workspace.root / 'graph/checkpoints.sqlite'
            with checkpoint_saver(database) as saver:
                with saver.cursor() as cursor:
                    cursor.execute('CREATE TABLE saved_jobs (id INTEGER)')
                    cursor.execute('INSERT INTO saved_jobs VALUES (7)')
            self.assertFalse(Path(str(database)).exists())
        with PostgresWorkspace('graph-test') as workspace:
            with checkpoint_saver(workspace.root / 'graph/checkpoints.sqlite') as saver:
                self.assertEqual(saver.conn.execute('SELECT id FROM saved_jobs').fetchone()[0], 7)

    def test_company_lock_prevents_overlapping_runs_in_different_cache_profiles(self):
        first = PostgresArtifactStore('first-profile')
        second = PostgresArtifactStore('second-profile')
        with first.lock('pipeline_runs/amd'):
            with self.assertRaisesRegex(ValueError, 'already running'):
                with second.lock('pipeline_runs/amd'):
                    self.fail('Both cache profiles obtained the same company lock')
        with second.lock('pipeline_runs/amd'):
            pass

    def test_main_pipeline_runs_real_agents_and_resumes_without_any_local_cache(self):
        import importlib.util
        import sys
        from contextlib import ExitStack
        from sec_disclosure.pipelines import run_full_pipeline as full
        from sec_disclosure.extraction import sec_10k_extractor as text
        from sec_disclosure.llm import disclosures
        from sec_disclosure.llm.config import LLMConfig
        from sec_disclosure.llm.request_pacing import RequestPacer
        from sec_disclosure.agents import disclosure_alignment, alignment_runtime, materiality, materiality_runtime, synthesis
        from sec_disclosure.storage import alignments, synthesis as synthesis_storage
        from test_parallel_extraction import ParallelExtractionTests
        from test_materiality import MaterialityTests
        from test_synthesis_agent import FINAL_REPORT

        name = 'sec_disclosure._raw_storage_loader'
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, storage.ROOT / 'scripts/load_to_postgres.py')
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        raw = sys.modules[name]
        arguments = dict(self.arguments)
        conninfo = arguments.pop('conninfo', '')
        dsn = psycopg.conninfo.make_conninfo(conninfo, **arguments)
        config = LLMConfig('https://example.test/v1', 'fake-key', 'test-model')
        html = '<html><body>' + ''.join(
            f'<h1>Item {item}. {title}</h1><p>{content}</p>' for item, title, content in (
                ('1', 'Business', 'We manufacture processors for industrial computing applications. Our customers use these processors in critical operating systems.'),
                ('1A', 'Risk Factors', 'We rely on sole suppliers for important semiconductor components. We maintain inventory to reduce interruptions in manufacturing.'),
                ('7', 'Management Discussion and Analysis', 'Our operating results reflect strong demand for computing products. We invest in manufacturing capacity to serve our customers.'),
                ('8', 'Financial Statements', 'Our debt obligations mature over the next several years. We maintain sufficient liquidity to meet these financial commitments.'))
        ) + '</body></html>'
        original_execute = full.execute_stage

        def execute(stage, **kwargs):
            # XBRL's own loader has separate integration coverage; this fixture
            # exercises narrative preprocessing and every agent through synthesis.
            if stage.script in {'run_api_metrics_batch.py', 'load_xbrl_to_postgres.py'}:
                return 0
            return original_execute(stage, **kwargs)

        def synthesize_reply(prompt, **kwargs):
            payload = json.loads(prompt)
            if 'material_changes' in payload:
                body = json.dumps({'key_findings': [
                    {'title': 'Supported change', 'analysis': 'The supplied disclosure evidence supports this change.',
                     'evidence_ids': [row['id']]} for row in payload['material_changes']]})
            else:
                body = FINAL_REPORT
            return CompletionResult(body, 'test-model', 'stop',
                                    {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

        def alignment_reply(prompt, **kwargs):
            payload = json.loads(prompt)
            if 'anchors' in payload:
                body = {'action': 'propose', 'matches': [
                    {'current_id': key, 'previous_ids': [key.replace('_2025_', '_2024_')],
                     'rationale': 'The disclosures describe the same operating topic.'} for key in payload['anchors']]}
            elif payload.get('change_analysis'):
                body = {'action': 'classify', 'analyses': [
                    {'match_id': row['match_id'], 'llm': 'Expanded'} for row in payload['alignments']]}
            else:
                records = {row['disclosure_id']: row for row in payload['records']}
                body = {'action': 'finalize', 'alignments': [
                    {'previous_ids': group['previous_ids'], 'current_ids': group['current_ids'],
                     'explanation': 'Both disclosures concern the same operating topic.',
                     'evidence': [{'disclosure_id': key, 'sentence_ids': [records[key]['sentences'][0]['sentence_id']]}
                                  for key in group['previous_ids'] + group['current_ids']],
                     'needs_review': False, 'review_reason': ''} for group in payload['proposed_groups']]}
            return CompletionResult(json.dumps(body), 'test-model', 'stop',
                                    {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15})

        def filing(args):
            content = html
            if args.year == '2025':
                content = content.replace('industrial computing applications', 'industrial computing and cloud applications')
                content = content.replace('strong demand for computing products', 'increasing cloud demand for computing products')
            return content, f'https://example.test/{args.year}.htm', 'amd'

        args = ['--ticker', 'AMD', '--years', '2024', '2025', '--data-dir', str(self.root),
                '--user-agent', 'Test test@example.com']
        with ExitStack() as stack:
            for module in (full, disclosures, disclosure_alignment, materiality, synthesis):
                stack.enter_context(patch.object(module, 'load_config', return_value=config))
            for module in (full, alignments, synthesis_storage):
                stack.enter_context(patch.object(module, 'connection_arguments', return_value=self.arguments))
            stack.enter_context(patch.object(full, 'dotenv_values', return_value={}))
            stack.enter_context(patch.dict(os.environ, {'SEC_API_KEY': 'fake-key'}))
            stack.enter_context(patch.object(raw, 'build_dsn', return_value=dsn))
            stack.enter_context(patch.object(full, 'execute_stage', side_effect=execute))
            stack.enter_context(patch.object(RequestPacer, 'acquire'))
            stack.enter_context(patch.object(text, 'load_filing_from_sec_api', side_effect=filing))
            stack.enter_context(patch.object(alignment_runtime, 'request_completion', side_effect=alignment_reply))
            extraction = stack.enter_context(patch.object(disclosures, 'request_completion', side_effect=ParallelExtractionTests().response))
            classification = stack.enter_context(patch.object(materiality_runtime, 'request_completion', side_effect=MaterialityTests().response))
            summary = stack.enter_context(patch.object(synthesis, 'request_completion', side_effect=synthesize_reply))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            errors = stack.enter_context(redirect_stderr(io.StringIO()))
            self.assertEqual(full.main(args), 0, output.getvalue() + errors.getvalue())
            self.assertEqual(list(self.root.iterdir()), [])
            calls = (extraction.call_count, classification.call_count, summary.call_count)
            self.assertTrue(all(calls))
            self.assertEqual(full.main(args + ['--data-dir', str(self.root / 'other-machine')]), 0)
            self.assertEqual((extraction.call_count, classification.call_count, summary.call_count), calls)
            self.assertEqual(list(self.root.iterdir()), [])
        with psycopg.connect(**self.arguments) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM synthesis_reports WHERE company='AMD'").fetchone()[0], 1)
            payload = conn.execute("SELECT report FROM synthesis_reports WHERE company='AMD'").fetchone()[0]
            self.assertGreater(payload['token_usage']['attempted_requests'], 0)
            cached = conn.execute("SELECT count(*) FROM pipeline_artifacts WHERE namespace='main' AND position('requests/' in path)>0").fetchone()[0]
            self.assertGreater(cached, 0)


if __name__ == "__main__":
    unittest.main()
