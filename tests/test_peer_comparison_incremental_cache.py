"""Incremental Item selections reuse only evidence-compatible peer jobs."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from artifact_support import MemoryArtifactStore, workspace_factory
from sec_disclosure.agents import peer_comparison as agent
from sec_disclosure.llm.client import CompletionResult, LLMError
from sec_disclosure.pipelines import run_peer_comparison_pipeline as pipeline
from test_peer_comparison import disclosure


class IncrementalPeerCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MemoryArtifactStore()
        self.config = SimpleNamespace(model="test-model", base_url="https://example.test")
        self.saved = {}
        text = {"1": "We manufacture semiconductor processors for cloud customers.",
                "1A": "Cybersecurity attacks threaten customer information and data security.",
                "7": "Loan credit losses increased as customer repayments declined.",
                "8": "Loan credit losses and repayment allowances reflect accounting estimates.",
                "15": "Loan credit loss allowances require estimates of customer repayments."}
        self.inputs = {"disclosures": {}, "alignments": {"aaa": {}, "bbb": {}}}
        for ticker in ("aaa", "bbb"):
            self.inputs["disclosures"][ticker] = {"disclosures.json": {
                "company": ticker, "fiscal_year": "2024", "disclosures": [
                    disclosure(ticker, f"{ticker}_2024_{item}_D001", item, content, f"{ticker}-{item}-s1")
                    for item, content in text.items()]}}
        for context in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(agent, "dotenv_values", return_value={}),
            patch.object(agent, "load_config", side_effect=lambda _: self.config),
            patch.object(agent.time, "sleep"),
            patch.object(pipeline, "PostgresWorkspace", side_effect=workspace_factory(self.store)),
            patch.object(pipeline, "read_peer_inputs", side_effect=lambda *a, **k: self.inputs),
            patch.object(pipeline, "store_peer_report", side_effect=self.save_report),
        ):
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(agent, "request_completion", side_effect=self.complete)
        self.request = context.start()
        self.addCleanup(context.stop)

    def save_report(self, report, **kwargs):
        self.saved[(tuple(report["items"]), report["target_selection"])] = deepcopy(report)

    def complete(self, prompt, **kwargs):
        payload = json.loads(prompt)
        comparisons, no_match = [], []
        for ticker, candidates in payload["peer_candidates"].items():
            if not candidates:
                no_match.append({"peer_ticker": ticker, "reason": "No eligible candidate was retrieved."})
                continue
            candidate = candidates[0]
            comparisons.append({
                "peer_ticker": ticker, "peer_disclosure_id": candidate["disclosure_id"],
                "relationship": "same_topic", "comparison": "Both sources discuss this topic.",
                "target_sentence_ids": [payload["target"]["sentences"][0]["sentence_id"]],
                "peer_sentence_ids": [candidate["sentences"][0]["sentence_id"]],
            })
        return CompletionResult(json.dumps({"comparisons": comparisons, "no_match": no_match}),
                                "test-model", "stop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    def run_items(self, items, extra=(), expected=0):
        args = ["--ticker", "AAA", "--peers", "BBB", "--year", "2024", "--target-selection", "all",
                "--data-dir", str(self.root), "--workers", "2", "--items", *items, *extra]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.main(args), expected)
        return self.saved[(tuple(sorted(items)), "material_changes" if "material_changes" in extra else "all")]

    def called_items(self):
        return sorted(json.loads(call.args[0])["target"]["item"] for call in self.request.call_args_list)

    def mark_material(self, items):
        self.inputs["alignments"]["aaa"][("2023", "2024", "alignments.json")] = {
            "company": "aaa", "previous_year": "2023", "current_year": "2024", "run_complete": True,
            "alignments": [{"previous_ids": [], "current_ids": [f"aaa_2024_{item}_D001"],
                            "materiality_analysis": {"materiality": "Yes"}} for item in items],
        }

    def test_material_and_all_modes_use_separate_caches_even_for_identical_targets(self):
        self.mark_material(["1"])
        material = ["--target-selection", "material_changes"]
        self.run_items(["1"], material)
        self.request.reset_mock()
        report = self.run_items(["1"])
        self.assertEqual(self.called_items(), ["1"])
        self.assertEqual(report["jobs_reused"], 0)
        self.assertEqual(report["new_api_requests"], 1)
        self.assertEqual(len(self.saved), 2)
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1"], material)["jobs_reused"], 1)
        self.assertEqual(self.run_items(["1"])["jobs_reused"], 1)
        self.request.assert_not_called()

    def test_item_expansion_still_reuses_cache_within_material_mode(self):
        self.mark_material(["1", "1A"])
        material = ["--target-selection", "material_changes"]
        self.run_items(["1"], material)
        self.request.reset_mock()
        report = self.run_items(["1", "1A"], material)
        self.assertEqual(self.called_items(), ["1A"])
        self.assertEqual(report["jobs_reused"], 1)
        self.assertEqual(report["new_api_requests"], 1)

    def test_old_profile_cannot_be_adopted_across_target_selection_modes(self):
        self.mark_material(["1"])
        self.run_items(["1"], ["--target-selection", "material_changes"])
        self.legacy_cache()
        self.request.reset_mock()
        report = self.run_items(["1"])
        self.assertEqual(self.called_items(), ["1"])
        self.assertEqual(report["jobs_reused"], 0)

    def test_expanding_item1_reuses_it_and_only_requests_new_items(self):
        first = self.run_items(["1"])
        old = {key: row for key, row in self.store.rows.items() if "/runs/" in key}
        self.request.reset_mock()
        expanded = self.run_items(["1", "1A", "7", "8"])
        self.assertEqual(self.called_items(), ["15", "1A", "7", "8"])
        self.assertEqual(expanded["jobs_completed"], 5)
        self.assertEqual(expanded["jobs_reused"], 1)
        self.assertEqual(expanded["new_api_requests"], 4)
        self.assertEqual(expanded["token_usage"]["requests"], 4)
        self.assertEqual(self.saved[(("1",), "all")], first)
        self.assertTrue(all(self.store.rows[key] == value for key, value in old.items()))
        self.assertEqual(len({row["target_disclosure_id"] for row in expanded["comparisons"]}), 5)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_adding_item8_reuses_item7_but_recomputes_item15_when_candidates_expand(self):
        self.run_items(["7"])
        self.request.reset_mock()
        expanded = self.run_items(["7", "8"])
        self.assertEqual(self.called_items(), ["15", "8"])
        self.assertEqual(expanded["jobs_reused"], 1)
        self.assertEqual(expanded["jobs_completed"], 3)
        self.assertEqual(expanded["new_api_requests"], 2)

    def test_new_selection_does_not_include_unselected_historical_items(self):
        self.run_items(["1"])
        self.request.reset_mock()
        report = self.run_items(["1A"])
        self.assertEqual(self.called_items(), ["1A"])
        self.assertEqual({row["item"] for row in report["comparisons"]}, {"1A"})
        self.assertEqual(len(self.saved), 2)

    def test_changing_an_unrelated_export_item_does_not_invalidate_item1(self):
        self.run_items(["1"])
        for ticker in ("aaa", "bbb"):
            row = self.inputs["disclosures"][ticker]["disclosures.json"]["disclosures"][1]
            row["content"] += " A new cybersecurity incident occurred."
            row["sources"][0]["sentences"][0]["text"] = row["content"]
        self.request.reset_mock()
        report = self.run_items(["1", "1A"])
        self.assertEqual(self.called_items(), ["1A"])
        self.assertEqual(report["jobs_reused"], 1)

    def test_changed_peer_evidence_invalidates_the_affected_comparison(self):
        self.run_items(["1"])
        row = self.inputs["disclosures"]["bbb"]["disclosures.json"]["disclosures"][0]
        row["content"] += " Cloud demand has declined."
        row["sources"][0]["sentences"][0]["text"] = row["content"]
        self.request.reset_mock()
        report = self.run_items(["1", "1A"])
        self.assertEqual(self.called_items(), ["1", "1A"])
        self.assertEqual(report["jobs_reused"], 0)

    def test_changed_endpoint_model_or_policy_does_not_reuse_incompatible_jobs(self):
        self.run_items(["1"])
        self.config = SimpleNamespace(model="other-model", base_url="https://example.test")
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1"])["jobs_reused"], 0)
        self.request.assert_called_once()
        self.config = SimpleNamespace(model="other-model", base_url="https://other.test")
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1"])["jobs_reused"], 0)
        self.request.assert_called_once()
        self.request.reset_mock()
        with patch.object(agent, "POLICY_VERSION", "another-policy"):
            self.assertEqual(self.run_items(["1"])["jobs_reused"], 0)
        self.request.assert_called_once()

    def test_cached_jobs_are_reused_even_with_zero_new_request_budget(self):
        self.run_items(["1"])
        self.request.reset_mock()
        report = self.run_items(["1", "1A"], ["--max-requests", "0"], expected=2)
        self.request.assert_not_called()
        self.assertEqual(report["jobs_completed"], 1)
        self.assertEqual(report["jobs_reused"], 1)
        self.assertEqual(report["new_api_requests"], 0)

    def legacy_cache(self, *, corrupt_evidence=False):
        """Convert only test artifacts into the older, profile-only layout."""
        old_jobs = {}
        for key in list(self.store.rows):
            if "/shared_jobs" in key:
                self.store.delete(key)
            elif key.endswith("/manifest.json"):
                payload = json.loads(self.store.get(key)[1])
                payload.pop("job_cache_version", None)
                self.store.put(key, json.dumps(payload).encode())
            elif "/jobs/" in key and key.endswith(".json"):
                payload = json.loads(self.store.get(key)[1])
                for field in ("job_fingerprint", "response_text", "source_profile"):
                    payload.pop(field, None)
                if corrupt_evidence:
                    payload["result"]["peer_results"][0]["comparisons"][0]["evidence"][0]["sentences"][0]["text"] = "Wrong cached text."
                self.store.put(key, json.dumps(payload).encode())
                old_jobs[key] = self.store.get(key)[1]
        return old_jobs

    def test_compatible_pre_incremental_profile_is_adopted_without_rewriting_original_jobs(self):
        self.run_items(["1"])
        old_jobs = self.legacy_cache()
        self.request.reset_mock()
        report = self.run_items(["1", "1A"])
        self.assertEqual(self.called_items(), ["1A"])
        self.assertEqual(report["jobs_reused"], 1)
        self.assertTrue(all(self.store.get(key)[1] == value for key, value in old_jobs.items()))

    def test_legacy_profile_uses_the_successful_retry_attempt(self):
        failed_once = False

        def temporary_failure(prompt, **kwargs):
            nonlocal failed_once
            if not failed_once:
                failed_once = True
                raise LLMError("Temporary connection failure", retryable=True)
            return self.complete(prompt, **kwargs)

        self.request.side_effect = temporary_failure
        self.run_items(["1"])
        self.legacy_cache()
        self.request.side_effect = self.complete
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1", "1A"])["jobs_reused"], 1)
        self.assertEqual(self.called_items(), ["1A"])

    def test_inconsistent_legacy_evidence_is_not_reused(self):
        self.run_items(["1"])
        self.legacy_cache(corrupt_evidence=True)
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1", "1A"])["jobs_reused"], 0)
        self.assertEqual(self.called_items(), ["1", "1A"])

    def test_unverifiable_old_profile_is_not_reused_after_its_source_export_changes(self):
        self.run_items(["1"])
        self.legacy_cache()
        row = self.inputs["disclosures"]["aaa"]["disclosures.json"]["disclosures"][1]
        row["content"] += " A new incident occurred."
        self.request.reset_mock()
        self.assertEqual(self.run_items(["1", "1A"])["jobs_reused"], 0)
        self.assertEqual(self.called_items(), ["1", "1A"])


if __name__ == "__main__":
    unittest.main()
