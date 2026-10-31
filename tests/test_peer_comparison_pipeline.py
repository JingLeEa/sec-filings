"""Database prerequisites must pass before peer comparison can call a model."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from sec_disclosure.agents import peer_comparison as agent
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.pipelines import run_peer_comparison_pipeline as pipeline
from sec_disclosure.storage import peer_comparison as storage
from test_peer_comparison import disclosure
from artifact_support import MemoryArtifactStore, workspace_factory


def inputs():
    return {"disclosures": {company: {"disclosures.json": {
        "company": company, "fiscal_year": "2024", "disclosures": [disclosure(
            company, f"{company}_2024_1A_D001", "1A", "We depend on a single chip supplier.", f"{company}-s1")],
    }} for company in ("aaa", "bbb")}, "alignments": {"aaa": {}, "bbb": {}}}


def report(complete=True):
    return {"target": "AAA", "fiscal_year": "2024", "peers": ["BBB"], "items": ["1A"],
            "target_selection": "all", "run_complete": complete, "comparisons": [],
            "token_usage": {"requests": 1, "total_tokens": 15}}


class PeerComparisonPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.artifacts = MemoryArtifactStore()
        workspace = patch.object(pipeline, 'PostgresWorkspace', side_effect=workspace_factory(self.artifacts))
        workspace.start()
        self.addCleanup(workspace.stop)
        self.args = ["--ticker", "AAA", "--peers", "BBB", "--year", "2024",
                     "--data-dir", str(self.root), "--target-selection", "all", "--items", "1A"]
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.dotenv = patch.object(agent, "dotenv_values", return_value={})
        self.dotenv.start()
        self.addCleanup(self.dotenv.stop)

    def test_missing_peer_blocks_cached_and_new_jobs_before_outputs_configuration_or_storage(self):
        with patch.object(pipeline, "read_peer_inputs", side_effect=storage.PeerInputError("BBB missing")), \
             patch.object(agent, "run") as compare, patch.object(agent, "load_config") as config, \
             patch.object(agent, "request_completion") as request, \
             patch.object(pipeline, "store_peer_report") as save, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(pipeline.main(self.args), 1)
        self.assertIn("BBB missing", errors.getvalue())
        for call in (compare, config, request, save):
            call.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_dry_run_reads_database_without_model_calls_or_writes(self):
        with patch.object(pipeline, "read_peer_inputs", return_value=inputs()) as read, \
             patch.object(agent, "load_config") as config, \
             patch.object(agent, "request_completion") as request, \
             patch.object(pipeline, "store_peer_report") as save, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(pipeline.main(self.args + ["--dry-run"]), 0)
        read.assert_called_once_with("aaa", ["bbb"], 2024, env_file=None)
        for call in (config, request, save):
            call.assert_not_called()
        self.assertIn('"input_source": "postgres"', output.getvalue())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_peer_report_is_stored_only_after_comparison(self):
        events = []
        with patch.object(pipeline, "read_peer_inputs", side_effect=lambda *a, **k: events.append("checked") or inputs()), \
             patch.object(agent, "run", side_effect=lambda *a, **k: events.append("compared") or report()), \
             patch.object(pipeline, "store_peer_report", side_effect=lambda *a, **k: events.append("saved")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(self.args), 0)
        self.assertEqual(events, ["checked", "compared", "saved"])

    def test_database_inputs_produce_a_cited_report_and_resume_without_new_model_calls(self):
        response = CompletionResult(json.dumps({"comparisons": [{
            "peer_ticker": "BBB", "peer_disclosure_id": "bbb_2024_1A_D001",
            "relationship": "same_topic", "comparison": "Both describe supplier concentration.",
            "target_sentence_ids": ["aaa-s1"], "peer_sentence_ids": ["bbb-s1"],
        }], "no_match": []}), "test-model", "stop",
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        with patch.object(pipeline, "read_peer_inputs", return_value=inputs()) as read, \
             patch.object(agent, "load_config", return_value=SimpleNamespace(model="test-model", base_url="https://example.test")), \
             patch.object(agent, "request_completion", return_value=response) as request, \
             patch.object(pipeline, "store_peer_report") as save, redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(self.args), 0)
            self.assertEqual(list(self.root.iterdir()), [])
            self.assertEqual(pipeline.main(self.args + ['--save-local-output']), 0)
        self.assertEqual(read.call_count, 2)
        request.assert_called_once()
        self.assertEqual(save.call_count, 2)
        stored = save.call_args.args[0]
        self.assertEqual(stored["input_source"], "postgres")
        self.assertEqual(stored["token_usage"]["total_tokens"], 15)
        evidence = stored["comparisons"][0]["peer_results"][0]["comparisons"][0]["evidence"]
        self.assertEqual(evidence[0]["sentences"][0]["sentence_id"], "aaa-s1")
        self.assertFalse((self.root / "disclosures").exists())
        exported = self.root / 'peer_comparison/aaa/2024/bbb'
        self.assertTrue((exported / 'peer_comparison.json').is_file())
        self.assertTrue((exported / 'token_usage.json').is_file())
        for cache in ('requests', 'jobs', 'manifest.json', 'runs'):
            self.assertFalse((exported / cache).exists())

    def test_storage_failure_does_not_claim_success(self):
        with patch.object(pipeline, "read_peer_inputs", return_value=inputs()), \
             patch.object(agent, "run", return_value=report()), \
             patch.object(pipeline, "store_peer_report", side_effect=storage.PeerComparisonStorageError("Save failed")), \
             redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(pipeline.main(self.args), 1)
        self.assertIn("Save failed", errors.getvalue())
        self.assertNotIn("saved to PostgreSQL", output.getvalue())

    def test_partial_comparison_is_saved_but_has_nonzero_exit_status(self):
        with patch.object(pipeline, "read_peer_inputs", return_value=inputs()), \
             patch.object(agent, "run", return_value=report(False)), \
             patch.object(pipeline, "store_peer_report") as save, redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(self.args), 2)
        self.assertFalse(save.call_args.args[0]["run_complete"])

    def test_original_peer_entry_point_uses_the_same_database_gate(self):
        with patch.object(pipeline, "read_peer_inputs", side_effect=storage.PeerInputError("Ticker missing")), \
             patch.object(agent, "request_completion") as request, redirect_stderr(io.StringIO()):
            self.assertEqual(agent.main(self.args), 1)
        request.assert_not_called()

    def test_direct_agent_run_also_requires_postgres_even_when_local_json_exists(self):
        args = agent.parse_args(self.args)
        folder = self.root / "disclosures/aaa/2024"
        folder.mkdir(parents=True)
        (folder / "disclosures.json").write_text(json.dumps(inputs()["disclosures"]["aaa"]["disclosures.json"]))
        with patch.object(agent, "read_peer_inputs", side_effect=storage.PeerInputError("BBB missing")), \
             patch.object(agent, "request_completion") as request:
            with self.assertRaisesRegex(storage.PeerInputError, "BBB missing"):
                agent.run(args)
        request.assert_not_called()

    def test_scope_and_workers_are_validated_before_database_access(self):
        for options in (["--peers", "AAA"], ["--peers", "BBB", "bbb"], ["--workers", "21"],
                        ["--workers", "0"], ["--year", "1899"], ["--timeout", "nan"]):
            with self.subTest(options=options), patch.object(pipeline, "read_peer_inputs") as read, \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                pipeline.main(self.args + options)
            read.assert_not_called()

    def test_defaults_keep_peer_comparison_separate_and_isolate_peer_sets(self):
        args = agent.parse_args(self.args)
        self.assertEqual(args.workers, 6)
        self.assertEqual(args.output_dir, self.root / "peer_comparison/aaa/2024/bbb")
        other = agent.parse_args(self.args + ["--peers", "CCC"])
        self.assertNotEqual(args.output_dir, other.output_dir)


class PeerComparisonStorageTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.cursor = self.conn.cursor.return_value.__enter__.return_value

    def read(self):
        with patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", return_value=self.conn):
            return storage.read_peer_inputs("aaa", ["bbb"], 2024)

    def test_missing_target_peer_and_wrong_year_are_blocked_before_loading_evidence(self):
        for rows, missing in (([], "AAA, BBB"), ([{"company": "aaa", "disclosure_count": 1}], "BBB"),
                              ([{"company": "bbb", "disclosure_count": 1}], "AAA")):
            with self.subTest(missing=missing), patch.object(storage, "_disclosure_reports") as load:
                self.cursor.fetchall.return_value = rows
                with self.assertRaisesRegex(storage.PeerInputError, missing):
                    self.read()
                load.assert_not_called()
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("f.fiscal_year = %s", query)
        self.assertEqual(parameters, (["aaa", "bbb"], 2024))

    def test_raw_filing_without_disclosures_is_not_enough(self):
        self.cursor.fetchall.return_value = [{"company": "aaa", "disclosure_count": 1},
                                            {"company": "bbb", "disclosure_count": 0}]
        with self.assertRaisesRegex(storage.PeerInputError, "no PostgreSQL disclosures for BBB"):
            self.read()

    def test_both_companies_are_read_in_one_consistent_read_only_transaction(self):
        self.cursor.fetchall.side_effect = [[{"company": "aaa", "disclosure_count": 1},
                                             {"company": "bbb", "disclosure_count": 1}], []]
        with patch.object(storage, "_disclosure_reports", side_effect=lambda cur, company, year: {"company": company}):
            loaded = self.read()
        self.assertEqual(set(loaded["disclosures"]), {"aaa", "bbb"})
        self.assertEqual(loaded["alignments"], {"aaa": {}, "bbb": {}})
        self.assertTrue(self.conn.read_only)
        self.assertEqual(self.conn.isolation_level, psycopg.IsolationLevel.REPEATABLE_READ)

    def test_database_error_is_safe_and_does_not_fall_back_to_local_files(self):
        with patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", side_effect=psycopg.OperationalError("password=secret-value")):
            with self.assertRaises(storage.PeerInputError) as error:
                storage.read_peer_inputs("aaa", ["bbb"], 2024)
        self.assertIn("not used as a fallback", str(error.exception))
        self.assertNotIn("secret-value", str(error.exception))

    def test_report_upsert_includes_token_usage_and_commits(self):
        with patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", return_value=self.conn):
            storage.store_peer_report(report())
        query, parameters = self.cursor.execute.call_args.args
        self.assertIn("ON CONFLICT", query)
        self.assertEqual(parameters[:6], ("AAA", 2024, ["BBB"], ["1A"], "all", True))
        self.assertEqual(json.loads(parameters[6])["token_usage"]["total_tokens"], 15)
        self.assertEqual(self.conn.__exit__.call_args.args, (None, None, None))

    def test_commit_failure_is_reported_and_does_not_expose_credentials(self):
        self.conn.__exit__.side_effect = psycopg.OperationalError("password=secret-value")
        with patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", return_value=self.conn):
            with self.assertRaises(storage.PeerComparisonStorageError) as error:
                storage.store_peer_report(report())
        self.assertNotIn("secret-value", str(error.exception))


if __name__ == "__main__":
    unittest.main()
