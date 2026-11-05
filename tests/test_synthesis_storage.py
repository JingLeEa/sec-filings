"""A synthesis report is published only after PostgreSQL commits it."""

from contextlib import redirect_stderr, redirect_stdout
from contextlib import contextmanager
from decimal import Decimal
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
        self.metrics, self.financial_diagnostics = [], []
        self.usage = {"attempted_requests": 2, "reported_tokens": {"total_tokens": 25}}
        self.read_conn, self.write_conn = MagicMock(), MagicMock()
        for conn in (self.read_conn, self.write_conn):
            conn.__enter__.return_value = conn

    def run_synthesis(self):
        def financial_facts(*args, diagnostics):
            diagnostics.extend(self.financial_diagnostics)
            return self.metrics

        def model(request, calls):
            self.model_request = request
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
             patch.object(synthesis, "fetch_financial_metrics", side_effect=financial_facts), \
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

    def test_financial_versions_and_mandatory_notes_are_saved_in_postgres_and_report(self):
        self.metrics = [{'id': 'xbrl:chosen', 'value': Decimal('8393000000'), 'value_text': '8393000000'}]
        self.financial_diagnostics = [{
            'concept': 'us-gaap:NetCashProvidedByUsedInOperatingActivities',
            'year': 2023, 'unit': 'USD', 'concept_period_type': 'duration',
            'period_start_date': '2023-01-01', 'period_end_date': '2023-12-31',
            'latest_source_filing_year': 2025, 'selected_id': 'xbrl:chosen',
            'resolution': 'selected_latest_filing', 'candidates': [
                {'id': 'xbrl:older', 'source_filing_year': 2023, 'value': Decimal('8447000000'), 'value_text': '8447000000'},
                {'id': 'xbrl:chosen', 'source_filing_year': 2025, 'value': Decimal('8393000000'), 'value_text': '8393000000'}]}]
        code, errors = self.run_synthesis()
        self.assertEqual(code, 0, errors)
        serialized = self.write_conn.cursor.return_value.__enter__.return_value.execute.call_args.args[1][2]
        stored = json.loads(serialized)
        self.assertEqual(stored, json.loads(self.report.read_text()))
        self.assertEqual(self.model_request['financial_fact_diagnostics'], self.financial_diagnostics)
        self.assertEqual([item['value_text'] for item in stored['financial_fact_diagnostics'][0]['candidates']],
                         ['8447000000', '8393000000'])
        self.assertTrue(any('latest selected FY2025' in note for note in stored['report']['limitations']))
        self.assertTrue(any('latest selected FY2025' in note for note in stored['limitations']))

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
    def test_report_loader_handles_small_year_values_in_integer_array_column(self):
        from sec_ui import reports
        with psycopg.connect(**storage.connection_arguments()) as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TEMP TABLE synthesis_reports (
                    company text, years integer[], report jsonb) ON COMMIT DROP""")
                cur.execute("INSERT INTO synthesis_reports VALUES (%s, ARRAY[2023,2024,2025], %s::jsonb)",
                            ("TEST", json.dumps({"company": "TEST", "years": [2023, 2024, 2025],
                             "report": {"executive_summary": "Saved report", "key_findings": []}})))

            @contextmanager
            def connection():
                yield conn

            with patch.object(reports, "_postgres_connection", side_effect=connection):
                result = reports.load_synthesis(reports.DatabaseSynthesis("TEST", (2023, 2024, 2025)))
            self.assertEqual(result["report"]["executive_summary"], "Saved report")
            self.assertEqual(result["years"], [2023, 2024, 2025])

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
            with conn.cursor() as cur:
                cur.execute("UPDATE alignments SET change_analysis=change_analysis || %s::jsonb",
                            (json.dumps({"final_taxonomy": "Reduced"}),))
            changes = fetch_change_evidence(conn, "AMD", 2024, 2025)
            self.assertEqual(changes[0]["taxonomy"], "Reduced")
            human_evidence = [{"disclosure_id": "synthetic_D001", "sentences": [
                {"sentence_id": "synthetic_S001", "text": "Human-reviewed source text.",
                 "paragraph_id": "synthetic_P001", "source_url": "https://example.test"}]}]
            with conn.cursor() as cur:
                cur.execute("UPDATE alignments SET extra=extra || %s::jsonb", (json.dumps({
                    "manual_review": {"source_fingerprint": "test", "source_evidence": human_evidence}}),))
            changes = fetch_change_evidence(conn, "AMD", 2024, 2025)
            self.assertEqual(changes[0]["source_evidence"][0]["text"], "Human-reviewed source text.")


if __name__ == "__main__":
    unittest.main()
