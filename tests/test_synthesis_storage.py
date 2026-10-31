"""A synthesis report is published only after PostgreSQL commits it."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import psycopg

from sec_disclosure.agents import synthesis
from sec_disclosure.storage import synthesis as storage
from sec_disclosure.agents.synthesis_retrieval import fetch_change_evidence


class SynthesisStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "synthesis/AMD_2024_2025_synthesis_output.json"
        self.change = {
            "company": "amd", "previous_year": 2024, "current_year": 2025,
            "match_id": "change-1", "taxonomy": "Modified", "alignment_status": "ai_verified",
            "relationship": "one_to_one", "materiality": {"materiality": "Yes"},
            "members": [], "source_evidence": [], "evidence_count": 0,
        }
        self.result = {"report": {"key_findings": [{"evidence_ids": ["change-1"]}]}}
        self.usage = {"attempted_requests": 2, "reported_tokens": {"total_tokens": 25}}
        self.read_conn, self.write_conn = MagicMock(), MagicMock()
        for conn in (self.read_conn, self.write_conn):
            conn.__enter__.return_value = conn

    def run_synthesis(self):
        def model(request, calls):
            calls.usage_path.parent.mkdir(parents=True, exist_ok=True)
            calls.usage_path.write_text(json.dumps(self.usage))
            # A completed model response remains reusable after database errors.
            calls.cache_dir.mkdir()
            (calls.cache_dir / "completed.json").write_text('{"text":"cached response"}')
            return self.result

        with patch.object(synthesis, "load_config", return_value=object()), \
             patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", side_effect=[self.read_conn, self.write_conn]), \
             patch.object(synthesis, "fetch_change_evidence", return_value=[self.change]), \
             patch.object(synthesis, "fetch_financial_metrics", return_value=[]), \
             patch.object(synthesis, "synthesize", side_effect=model), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            code = synthesis.main(["--company", "AMD", "--years", "2024", "2025",
                                   "--data-dir", str(self.root)])
        return code, errors.getvalue()

    def test_committed_report_and_usage_match_the_portable_json(self):
        code, errors = self.run_synthesis()
        self.assertEqual(code, 0, errors)
        stored = self.write_conn.cursor.return_value.__enter__.return_value.execute.call_args
        self.assertIn("INSERT INTO synthesis_reports", stored.args[0])
        company, years, serialized = stored.args[1]
        self.assertEqual((company, years), ("AMD", [2024, 2025]))
        self.assertEqual(json.loads(serialized), json.loads(self.report.read_text()))
        self.assertEqual(json.loads(serialized)["token_usage"], self.usage)
        self.assertEqual(self.write_conn.__exit__.call_args.args, (None, None, None))

    def test_failed_commit_keeps_cached_model_work_and_publishes_no_report(self):
        self.write_conn.__exit__.side_effect = psycopg.OperationalError("password=private-test-secret")
        code, errors = self.run_synthesis()
        self.assertEqual(code, 1)
        self.assertIn("Could not read or commit synthesis data", errors)
        self.assertNotIn("private-test-secret", errors)
        self.assertFalse(self.report.exists())
        self.assertTrue((self.root / "synthesis/cache/completed.json").exists())

    def test_failed_upsert_rolls_back_and_publishes_no_report(self):
        cursor = self.write_conn.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = [None, psycopg.OperationalError("Write failed")]
        code, errors = self.run_synthesis()
        self.assertEqual(code, 1)
        self.assertIn("Could not save synthesis report", errors)
        self.assertFalse(self.report.exists())
        self.assertIs(self.write_conn.__exit__.call_args.args[0], storage.SynthesisStorageError)

    def test_connection_failure_is_reported_without_exposing_credentials(self):
        with patch.object(storage, "connection_arguments", return_value={}), \
             patch.object(psycopg, "connect", side_effect=psycopg.OperationalError("password=private-test-secret")):
            with self.assertRaises(storage.SynthesisStorageError) as error:
                with storage.synthesis_connection():
                    self.fail("A failed connection cannot yield a session")
        self.assertNotIn("private-test-secret", str(error.exception))


@unittest.skipUnless(os.environ.get("SEC_SYNTHESIS_DB_INTEGRATION") == "1", "Requires PostgreSQL")
class SynthesisRetrievalPostgresTests(unittest.TestCase):
    def test_llm_only_change_label_is_retrieved_when_enrichment_fields_are_null(self):
        # Temporary tables shadow the production relations for this connection;
        # all test records disappear when the transaction ends.
        with psycopg.connect(**storage.connection_arguments()) as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TEMP TABLE alignment_runs (
                    run_id integer, company text, previous_year integer,
                    current_year integer, run_complete boolean) ON COMMIT DROP""")
                cur.execute("""CREATE TEMP TABLE alignments (
                    run_id integer, match_id text, status text, relationship text,
                    change_analysis jsonb, extra jsonb, evidence jsonb) ON COMMIT DROP""")
                cur.execute("""CREATE TEMP TABLE alignment_members (
                    run_id integer, match_id text, side text, disclosure_id text,
                    item text, section text, taxonomy text, summary text,
                    extraction_status text) ON COMMIT DROP""")
                cur.execute("INSERT INTO alignment_runs VALUES (1, 'amd', 2024, 2025, TRUE)")
                cur.execute("""INSERT INTO alignments VALUES
                    (1, 'change-1', 'ai_verified', 'one_to_one', %s::jsonb, %s::jsonb, '[]'::jsonb)""", (
                    json.dumps({"status": "completed", "llm": "Expanded", "lexical": None,
                                "semantic": None, "final_taxonomy": None}),
                    json.dumps({"materiality_analysis": {"materiality": "Yes", "materiality_score": 0.9}}),
                ))
            changes = fetch_change_evidence(conn, "AMD", 2024, 2025)
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["taxonomy"], "Expanded")
            self.assertEqual(changes[0]["materiality"]["materiality"], "Yes")


if __name__ == "__main__":
    unittest.main()
