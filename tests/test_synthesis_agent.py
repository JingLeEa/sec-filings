"""Offline tests for synthesis coverage, aggregation, and reasoning-model output."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from sec_disclosure.agents.synthesis import (
    SynthesisCalls,
    _parse_json_object,
    calculate_trends,
    synthesize,
)
from sec_disclosure.agents.synthesis_retrieval import fetch_financial_metrics


class FakeCalls:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, stage, prompt, system_prompt):
        self.prompts.append((stage, prompt, system_prompt))
        return self.responses.pop(0)

    def invalidate_last(self):
        pass


def finding(identifier):
    return json.dumps({
        "key_findings": [{
            "title": f"Change {identifier}",
            "analysis": f"Analysis for {identifier}",
            "evidence_ids": [identifier],
        }]
    })


FINAL_REPORT = json.dumps({
    "executive_summary": "Overall synthesis of all changes.",
    "longitudinal_analysis": "The supported changes evolve across the years.",
    "financial_analysis": "No financial facts were supplied.",
    "limitations": ["Financial facts were unavailable."],
})


class SynthesisAgentTests(unittest.TestCase):
    def request(self):
        return {
            "company": "EXAMPLE",
            "years": [2023, 2024],
            "material_changes": [{"id": "c1"}, {"id": "c2"}],
            "financial_metrics": [],
            "finding_target": 2,
            "finding_batch_size": 1,
        }

    def test_final_report_synthesizes_findings_once_and_receives_financial_data_once(self):
        calls = FakeCalls([finding("c1"), finding("c2"), FINAL_REPORT])
        result = synthesize(self.request(), calls)
        self.assertEqual(result["validation"]["material_change_coverage"], 2)
        self.assertEqual([call[0] for call in calls.prompts],
                         ["findings-1", "findings-2", "final-report"])
        for _, prompt, _ in calls.prompts[:2]:
            self.assertNotIn("financial_metrics", prompt)
        final_prompt = json.loads(calls.prompts[2][1])
        self.assertEqual(len(final_prompt["batch_findings"]), 2)
        self.assertEqual(final_prompt["financial_metrics"], [])
        self.assertEqual(result["report"]["executive_summary"],
                         "Overall synthesis of all changes.")
        self.assertIn("No company-wide XBRL financial facts",
                      result["report"]["financial_analysis"])
        self.assertIn("Company-wide XBRL financial facts were unavailable for the requested periods.",
                      result["report"]["limitations"])

    def test_invalid_evidence_retries_and_fails_closed(self):
        calls = FakeCalls([
            json.dumps({"key_findings": [{
                "title": "Wrong", "analysis": "Unsupported",
                "evidence_ids": ["not-a-real-id"],
            }]}),
            json.dumps({"key_findings": [{
                "title": "Still wrong", "analysis": "Still unsupported",
                "evidence_ids": ["not-a-real-id"],
            }]}),
        ])
        request = self.request()
        request["material_changes"] = [{"id": "c1"}]
        request["finding_target"] = 1
        with self.assertRaisesRegex(ValueError, "failed validation"):
            synthesize(request, calls)
        self.assertEqual(len(calls.prompts), 2)
        self.assertIn("validation_errors", calls.prompts[1][1])

    def test_invalid_evidence_is_repaired_on_retry(self):
        calls = FakeCalls([
            json.dumps({"key_findings": [{
                "title": "Wrong", "analysis": "Unsupported",
                "evidence_ids": ["not-a-real-id"],
            }]}),
            finding("c1"),
            FINAL_REPORT,
        ])
        request = self.request()
        request["material_changes"] = [{"id": "c1"}]
        request["finding_target"] = 1
        result = synthesize(request, calls)
        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(len(calls.prompts), 3)

    def test_reasoning_block_and_markdown_fence_are_ignored(self):
        text = '<think>{"not": "the answer"}</think>\n```json\n{"ok": true}\n```'
        self.assertEqual(_parse_json_object(text), {"ok": True})

    def test_calculated_trends_keep_source_ids_and_source_values(self):
        metrics = [
            {"id": "rev23", "year": 2023, "concept": "Revenue", "value": 100, "unit": "USD"},
            {"id": "rev24", "year": 2024, "concept": "Revenue", "value": 125, "unit": "USD"},
            {"id": "assets24", "year": 2024, "concept": "AssetsCurrent", "value": 80, "unit": "USD"},
            {"id": "liab24", "year": 2024, "concept": "LiabilitiesCurrent", "value": 40, "unit": "USD"},
        ]
        trends = calculate_trends(metrics)
        growth = next(item for item in trends if item["type"] == "year_over_year_change")
        self.assertEqual(growth["source_ids"], ["rev23", "rev24"])
        self.assertEqual(growth["growth_pct"], 25.0)
        self.assertTrue(all(item["source_values"] for item in trends))

    def test_financial_retrieval_reads_loaded_xbrl_context(self):
        class Cursor:
            description = [
                SimpleNamespace(name=name)
                for name in (
                    "company", "source_filing_year", "form_type", "filing_id",
                    "extraction_id", "taxonomy_year", "concept", "label", "amount",
                    "unit", "concept_period_type", "metric_scope", "value_index",
                    "value_text", "period_start_date", "period_end_date", "instant_date",
                    "dimensions", "status",
                )
            ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, parameters):
                self.sql = sql
                self.parameters = parameters

            def fetchall(self):
                return [(
                    "EXAMPLE", 2024, "10-K", 5, 8, 2024,
                    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                    "Revenue", 120.0, "USD", "duration", "company_wide",
                    0, "120.0", date(2023, 1, 1), date(2023, 12, 31),
                    None, [], "reported",
                )]

        class Connection:
            def cursor(self):
                self.last_cursor = Cursor()
                return self.last_cursor

        connection = Connection()
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2024])
        self.assertIn("FROM xbrl_value_context", connection.last_cursor.sql)
        self.assertEqual(metrics[0]["year"], 2024)
        self.assertEqual(metrics[0]["value"], 120.0)
        self.assertEqual(metrics[0]["period_type"], "FY")

    def test_shared_call_layer_reuses_cache_and_tracks_usage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = SimpleNamespace(model="test-model", base_url="https://example.test")
            calls = SynthesisCalls(config, root / "cache", root / "usage.json", 10, 100)
            result = SimpleNamespace(
                text='{"answer": 1}', model="test-model", finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
            original = __import__(
                "sec_disclosure.agents.synthesis", fromlist=["request_completion"]
            ).request_completion
            try:
                import sec_disclosure.agents.synthesis as module
                module.request_completion = lambda *args, **kwargs: result
                self.assertEqual(calls.complete("stage", "prompt", "system"), result.text)
                self.assertEqual(calls.complete("stage", "prompt", "system"), result.text)
            finally:
                module.request_completion = original
            usage = json.loads((root / "usage.json").read_text(encoding="utf-8"))
            self.assertEqual(usage["cached_responses"], 1)
            self.assertEqual(usage["reported_tokens"]["total_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
