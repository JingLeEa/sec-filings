"""Offline tests for synthesis coverage, aggregation, and reasoning-model output."""

import json
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sec_disclosure.agents.synthesis import (
    SynthesisCalls,
    _parse_json_object,
    _write_json,
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

FINANCIAL_COLUMNS = (
    "company", "source_filing_year", "form_type", "filing_id",
    "extraction_id", "taxonomy_year", "concept", "label", "amount",
    "unit", "concept_period_type", "metric_scope", "value_index",
    "value_text", "period_start_date", "period_end_date", "instant_date",
    "dimensions", "status", "decimals", "precision",
    "decimals_missing", "precision_missing",
)

SQL_NULL = object()


def financial_row(source_year, value, *, period_type="duration", start=None, end=None,
                  instant=None, decimals=SQL_NULL, precision=SQL_NULL,
                  unit="USD", scope="company_wide"):
    decimals_missing = decimals is SQL_NULL
    precision_missing = precision is SQL_NULL
    if decimals_missing:
        decimals = None
    if precision_missing:
        precision = None
    return (
        "EXAMPLE", source_year, "10-K", source_year, source_year, source_year,
        "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenue", value, unit, period_type, scope, 0, str(value),
        start, end, instant, [], "reported", decimals, precision,
        decimals_missing, precision_missing,
    )


class FinancialCursor:
    description = [SimpleNamespace(name=name) for name in FINANCIAL_COLUMNS]

    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql, parameters):
        self.sql = sql
        self.parameters = parameters

    def fetchall(self):
        return self.rows


class FinancialConnection:
    def __init__(self, rows):
        self.last_cursor = FinancialCursor(rows)

    def cursor(self):
        return self.last_cursor


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

    def test_financial_values_and_trend_calculations_preserve_decimal_precision(self):
        connection = FinancialConnection([
            financial_row(
                2023, Decimal("9007199254740992.1234"),
                start=date(2022, 1, 1), end=date(2022, 12, 31),
            ),
            financial_row(
                2024, Decimal("9007199254740993.1234"),
                start=date(2023, 1, 1), end=date(2023, 12, 31),
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2023, 2024])
        self.assertEqual(metrics[0]["value"], Decimal("9007199254740992.1234"))
        self.assertEqual(metrics[1]["value"], Decimal("9007199254740993.1234"))
        self.assertEqual(metrics[0]["value_text"], "9007199254740992.1234")
        self.assertEqual(metrics[1]["value_text"], "9007199254740993.1234")
        trend = next(
            item for item in calculate_trends(metrics)
            if item["type"] == "year_over_year_change"
        )
        self.assertEqual(trend["old_value"], Decimal("9007199254740992.1234"))
        self.assertEqual(trend["new_value"], Decimal("9007199254740993.1234"))
        self.assertIsInstance(trend["growth_pct"], Decimal)

    def test_decimal_values_convert_to_json_numbers_only_when_written(self):
        exact_value = Decimal("9007199254740993.1234")
        result = {
            "financial_metrics": [{
                "value": exact_value,
                "value_text": "9007199254740993.1234",
            }],
            "financial_trends": [{
                "old_value": Decimal("100.25"),
                "growth_pct": Decimal("2.5"),
            }],
        }
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            _write_json(output, result)
            written = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(
            written["financial_metrics"][0]["value"], float(exact_value)
        )
        self.assertEqual(
            written["financial_metrics"][0]["value_text"],
            "9007199254740993.1234",
        )
        self.assertEqual(
            written["financial_trends"][0],
            {"old_value": 100.25, "growth_pct": 2.5},
        )
        self.assertIs(result["financial_metrics"][0]["value"], exact_value)
        self.assertEqual(result["financial_metrics"][0]["value"], exact_value)

    def test_financial_retrieval_reads_loaded_xbrl_context(self):
        connection = FinancialConnection([
            financial_row(
                2024, 120.0, start=date(2023, 1, 1), end=date(2023, 12, 31),
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2024])
        self.assertIn("FROM xbrl_value_context", connection.last_cursor.sql)
        self.assertEqual(metrics[0]["year"], 2023)
        self.assertEqual(metrics[0]["source_filing_year"], 2024)
        self.assertEqual(metrics[0]["value"], 120.0)
        self.assertEqual(metrics[0]["period_type"], "FY")

    def test_instant_fact_reporting_year_comes_from_instant_date(self):
        connection = FinancialConnection([
            financial_row(2025, 80.0, period_type="instant", instant=date(2024, 12, 31)),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2025])
        self.assertEqual(metrics[0]["year"], 2024)
        self.assertEqual(metrics[0]["source_filing_year"], 2025)
        self.assertEqual(metrics[0]["instant_date"], "2024-12-31")

    def test_comparative_annual_facts_from_one_filing_keep_each_reporting_year(self):
        connection = FinancialConnection([
            financial_row(
                2024, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
            ),
            financial_row(
                2024, 120.0, start=date(2023, 1, 1), end=date(2023, 12, 31),
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2024])
        self.assertEqual([metric["year"] for metric in metrics], [2022, 2023])
        self.assertEqual([metric["source_filing_year"] for metric in metrics], [2024, 2024])
        self.assertIn("period_end_date - period_start_date BETWEEN 300 AND 380",
                      connection.last_cursor.sql)
        self.assertIn("concept_period_type = 'instant' AND instant_date IS NOT NULL",
                      connection.last_cursor.sql)

    def test_equivalent_facts_across_filing_years_are_deduplicated_deterministically(self):
        connection = FinancialConnection([
            financial_row(
                2024, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
                decimals="-6",
            ),
            financial_row(
                2023, 100, start=date(2022, 1, 1), end=date(2022, 12, 31),
                decimals="-6",
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2023, 2024])
        self.assertEqual(len(metrics), 1)
        self.assertEqual(metrics[0]["year"], 2022)
        self.assertEqual(metrics[0]["source_filing_year"], 2023)
        self.assertEqual(metrics[0]["value"], 100.0)
        self.assertEqual(metrics[0]["decimals"], "-6")
        self.assertFalse(metrics[0]["decimals_missing"])

    def test_same_period_facts_with_different_units_are_separate_records(self):
        connection = FinancialConnection([
            financial_row(
                2023, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
                unit="USD",
            ),
            financial_row(
                2024, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
                unit="shares",
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2023, 2024])
        self.assertEqual(len(metrics), 2)
        self.assertEqual({metric["unit"] for metric in metrics}, {"USD", "shares"})

    def test_conflicting_facts_for_same_reporting_period_across_filings_fail(self):
        connection = FinancialConnection([
            financial_row(
                2023, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
            ),
            financial_row(
                2024, 110.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
            ),
        ])
        with self.assertRaisesRegex(ValueError, "Conflicting financial facts"):
            fetch_financial_metrics(connection, "EXAMPLE", [2023, 2024])

    def test_accuracy_metadata_difference_is_conservatively_ambiguous(self):
        for first_accuracy, second_accuracy in (
            ({"decimals": "-6"}, {"decimals": "-3"}),
            ({"decimals": SQL_NULL}, {"decimals": None}),
            ({"precision": "6"}, {"precision": 6}),
        ):
            with self.subTest(first=first_accuracy, second=second_accuracy):
                connection = FinancialConnection([
                    financial_row(
                        2023, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
                        **first_accuracy,
                    ),
                    financial_row(
                        2024, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
                        **second_accuracy,
                    ),
                ])
                with self.assertRaisesRegex(ValueError, "Conflicting financial facts"):
                    fetch_financial_metrics(connection, "EXAMPLE", [2023, 2024])

    def test_trend_years_follow_comparative_reporting_periods(self):
        connection = FinancialConnection([
            financial_row(
                2024, 100.0, start=date(2022, 1, 1), end=date(2022, 12, 31),
            ),
            financial_row(
                2024, 120.0, start=date(2023, 1, 1), end=date(2023, 12, 31),
            ),
        ])
        metrics = fetch_financial_metrics(connection, "EXAMPLE", [2024])
        trends = calculate_trends(metrics)
        trend = next(item for item in trends if item["type"] == "year_over_year_change")
        self.assertEqual((trend["from_year"], trend["to_year"]), (2022, 2023))
        self.assertEqual((trend["old_value"], trend["new_value"]), (100.0, 120.0))

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
