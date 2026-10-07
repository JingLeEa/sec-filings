import io
import json
import shutil
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from sec_disclosure.agents import disclosure_alignment as module
from sec_disclosure.agents import alignment_runtime
from sec_disclosure.agents.alignment_graph import AlignmentAgentGraph, GRAPH_POLICY
from langgraph.checkpoint.sqlite import SqliteSaver
from sec_disclosure.agents.alignment_repair import VerificationRepair
from sec_disclosure.agents.alignment_data import AlignmentData
from sec_disclosure.agents.alignment_exact import exact_matches, normalize_text
from sec_disclosure.indexing.disclosure_retrieval import DisclosureIndex
from sec_disclosure.llm.client import CompletionResult, LLMError
from sec_disclosure.llm.config import LLMConfig
from sec_disclosure.llm.disclosures import digest, write_json


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for year in ("2023", "2024"):
            raw = self.root / "raw/amd" / year
            output = self.root / "disclosures/amd" / year
            paragraphs, units, disclosures = [], [], []
            for number, topic in enumerate(("EPYC server revenue", "Patent litigation", "Debt maturity"), 1):
                pid = f"amd_{year}_7_P{number:03d}"
                texts = [f"{topic} was reported in {year}.", "The disclosure includes the policy and results."]
                paragraph = {"id": pid, "company": "amd", "year": year, "item": "7", "item_title": topic, "text": " ".join(texts)}
                paragraphs.append(paragraph)
                sentences = [{"sentence_id": f"{pid}_S{i:03d}", "text": text} for i, text in enumerate(texts, 1)]
                units.extend({"id": s["sentence_id"], "text": s["text"], "chunk_id": pid} for s in sentences)
                disclosures.append({"disclosure_id": f"amd_{year}_7_D{number:03d}", "company": "amd", "fiscal_year": year,
                    "item": "7", "section": topic, "summary": topic, "content": paragraph["text"],
                    "taxonomy": "Financial & Capital Resources", "verification": {"status": "source_validated", "review_reasons": []},
                    "sources": [{"paragraph_id": pid, "original_paragraph": paragraph["text"], "selected_text": paragraph["text"],
                                 "sentences": sentences, "source_url": "https://example.test/filing"}]})
            write_json(raw / f"{year}_chunks.json", paragraphs)
            write_json(raw / f"{year}_chunk_sentences.json", units)
            common = {"company": "amd", "fiscal_year": year, "input_hashes": {str(path): digest(path.read_bytes()) for path in raw.glob("*.json")}}
            for name, field, rows in (("disclosures", "disclosures", disclosures), ("review_candidates", "disclosures", []),
                                      ("unassigned_sources", "sources", []), ("excluded_sources", "excluded", [])):
                write_json(output / f"{name}.json", {**common, field: rows})
            write_json(output / "token_usage.json", {"run_complete": True, "reported_tokens": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def data(self):
        return AlignmentData(self.root, "amd", ("2023", "2024"))

    def set_fixture_item(self, year, number, item):
        """Change authoritative Item metadata while preserving the fixture's source IDs."""
        raw = self.root / "raw/amd" / year
        path = raw / f"{year}_chunks.json"
        paragraphs = json.loads(path.read_text())
        paragraphs[number - 1]["item"] = item
        write_json(path, paragraphs)
        for path in (self.root / "disclosures/amd" / year).glob("*.json"):
            document = json.loads(path.read_text())
            if "input_hashes" not in document:
                continue
            document["input_hashes"] = {str(p): digest(p.read_bytes()) for p in raw.glob("*.json")}
            for row in document.get("disclosures", []):
                if row["disclosure_id"] == f"amd_{year}_7_D{number:03d}":
                    row["item"] = item
            write_json(path, document)

    def run_cli(self, *args, response=None):
        with patch.object(module, "load_config", return_value=LLMConfig("https://example.test/v1", "fake-secret", "default")), \
             patch.object(alignment_runtime, "request_completion", side_effect=response or self.response) as api, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = module.main(["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024", "--data-dir", str(self.root), *args])
        return status, api.call_count

    def response(self, prompt, **kwargs):
        payload = json.loads(prompt)
        if "anchors" in payload:
            result = {"action": "propose", "matches": [{"current_id": key, "previous_ids": [key.replace("2024", "2023")],
                                                        "rationale": "Same specific reported topic."} for key in payload["anchors"]]}
        else:
            records = {r["disclosure_id"]: r for r in payload["records"]}
            result = {"action": "finalize", "alignments": []}
            for group in payload["proposed_groups"]:
                result["alignments"].append({"previous_ids": group["previous_ids"], "current_ids": group["current_ids"],
                    "explanation": "Both disclosures discuss the same specific reported topic.",
                    "evidence": [{"disclosure_id": key, "sentence_ids": [records[key]["sentences"][0]["sentence_id"]]}
                                 for key in group["previous_ids"] + group["current_ids"]], "needs_review": False, "review_reason": ""})
        return CompletionResult(json.dumps(result), "test-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

    def output(self, name):
        return json.loads((self.root / "alignments/amd/2023-2024" / name).read_text())

    def saved_report(self):
        return module.load_alignment_report(self.root / "alignments/amd/2023-2024")

    def unmatched_rows(self, side=None):
        return [row for row in self.output("alignments.json")["alignments"]
                if row["status"] == "unmatched" and (side is None or row[side + "_ids"])]

    def test_alternative_disclosure_root_reuses_raw_sources_and_tracks_input_hashes(self):
        alternative = self.root / "disclosures_subsection"
        shutil.copytree(self.root / "disclosures", alternative)
        shutil.rmtree(self.root / "disclosures")
        self.assertEqual(self.run_cli("--disclosures-dir", str(alternative)), (0, 2))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        inputs = self.output("manifest.json")["input_hashes"]
        self.assertTrue(any(str(alternative) in path for path in inputs))
        self.assertFalse(any(str(self.root / "disclosures") + "/" in path for path in inputs))
        self.assertEqual(self.run_cli("--disclosures-dir", str(alternative)), (0, 0))

    def test_full_run_source_evidence_and_cached_rerun(self):
        self.assertEqual(self.run_cli(), (0, 2))
        report = self.saved_report()
        self.assertTrue(report["run_complete"])
        self.assertEqual(report["counts"], {"ai_verified": 3})
        self.assertEqual(report["coverage"]["pending_disclosure_ids"], [])
        self.assertEqual(next(iter(report["alignments"][0])), "match_id")
        self.assertEqual(report["schema_version"], "8")
        self.assertEqual(report["comparison_scope"], "same_item")
        for name in ("alignments.json", "needs_review.json"):
            self.assertNotIn("change_counts", self.output(name))
            self.assertNotIn('"change_type"', json.dumps(self.output(name)))
        self.assertNotIn('"change_type"', module.VERIFICATION_PROMPT)
        for side in ("previous", "current"):
            self.assertEqual(self.unmatched_rows(side), [])
        self.assertEqual(self.run_cli(), (0, 0))
        usage = self.output("token_usage.json")
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(set(usage["by_agent"]), {"matching", "verification"})
        self.assertEqual(usage["extraction_tokens_by_year"]["2023"]["total_tokens"], 15)

    def test_unlimited_api_retries_recover_rate_limits_and_keep_unknown_usage_reserves(self):
        attempts = 0

        def response(prompt, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 8:
                status = 503 if attempts <= 4 else 429
                raise LLMError("Temporary API failure.", status_code=status, retryable=True)
            return self.response(prompt, **kwargs)

        options = ("--fault-tolerant", "--max-retries", "-1", "--retry-backoff", "2", "--rate-limit-cooldown", "0")
        with patch.object(alignment_runtime.time, "sleep") as sleep:
            self.assertEqual(self.run_cli(*options, response=response), (0, 10))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])
        usage = self.output("token_usage.json")
        self.assertEqual(usage["requests_with_unknown_usage"], 8)
        self.assertFalse(usage["usage_complete"])
        self.assertEqual(len(usage["unknown_usage_budget_reserve"]["attempts"]), 8)
        self.assertEqual(usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"], 0)
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 250)
        self.assertGreater(usage["budget_accounted_tokens"], 250)
        self.assertEqual(self.run_cli(*options), (0, 0))

    def test_runtime_resume_caches_latest_attempt_after_three_digits(self):
        runtime = self.concurrency_runtime(fault_tolerant=True, max_retries=-1)
        result = CompletionResult("{}", "test-model", "stop",
                                  {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        with patch.object(alignment_runtime, "request_completion", return_value=result), redirect_stdout(io.StringIO()):
            runtime.call("matching", "job", "prompt", "system")
        completed = runtime.requests[0]
        key = completed["request_hash"]
        (runtime.directory / f"{key}_attempt_001.json").unlink()
        completed["attempt"] = 1000
        write_json(runtime.directory / f"{key}_attempt_1000.json", completed)
        failed = {**completed, "attempt": 999, "status": "failed", "retryable": True,
                  "error": "Temporary API failure.", "status_code": 503}
        failed.pop("result")
        write_json(runtime.directory / f"{key}_attempt_999.json", failed)
        resumed = self.concurrency_runtime(fault_tolerant=True, max_retries=-1)
        with patch.object(alignment_runtime, "request_completion") as api:
            self.assertEqual(resumed.call("matching", "job", "prompt", "system"), completed["result"])
            api.assert_not_called()

    def test_fault_tolerant_api_retries_are_bounded_and_resume_automatically(self):
        def response(prompt, **kwargs):
            raise LLMError("Temporary API failure.", status_code=503, retryable=True)

        options = ("--fault-tolerant", "--max-retries", "1", "--retry-backoff", "0")
        self.assertEqual(self.run_cli(*options, response=response), (1, 2))
        self.assertFalse(self.saved_report()["run_complete"])
        self.assertEqual(self.run_cli(*options), (0, 2))
        self.assertTrue(self.saved_report()["run_complete"])
        self.assertEqual(len(self.output("token_usage.json")["unknown_usage_budget_reserve"]["attempts"]), 2)

    def test_unlimited_api_retries_stop_at_request_and_token_caps(self):
        def response(prompt, **kwargs):
            raise LLMError("Temporary API failure.", status_code=503, retryable=True)

        options = ("--fault-tolerant", "--max-retries", "-1", "--retry-backoff", "0")
        self.assertEqual(self.run_cli(*options, "--max-new-requests", "3", response=response), (2, 3))
        self.assertEqual(self.run_cli(*options, "--max-requests", "4", response=response), (2, 1))
        reserve = self.output("token_usage.json")["unknown_usage_budget_reserve"]["estimated_tokens"]
        self.assertEqual(self.run_cli(*options, "--max-total-tokens", str(reserve), response=response), (2, 0))
        self.assertEqual(self.run_cli(*options, "--max-requests", "-1", "--max-total-tokens", "-1"), (0, 2))

    def test_unlimited_api_retries_do_not_retry_permanent_errors(self):
        for status in (401, 403, 429):
            def response(prompt, **kwargs):
                raise LLMError("Permanent API failure.", status_code=status, retryable=False)

            with self.subTest(status=status):
                self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "-1", "--retry-backoff", "0",
                                              "--output-dir", str(self.root / f"permanent_{status}"), response=response), (1, 1))

    def test_fault_tolerance_keeps_successful_missing_usage_unacknowledged(self):
        def response(prompt, **kwargs):
            result = self.response(prompt, **kwargs)
            return CompletionResult(result.text, result.model, result.finish_reason, None)

        self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "-1", response=response), (2, 1))
        usage = self.output("token_usage.json")
        self.assertEqual(usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"], 1)
        self.assertEqual(usage["unknown_usage_budget_reserve"]["attempts"], [])

    def test_retry_and_unlimited_budget_options_validate_before_requests(self):
        for options in (("--max-retries", "-2"), ("--max-requests", "-2"), ("--max-total-tokens", "0"),
                        ("--retry-backoff", "-1"), ("--retry-backoff", "nan")):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                self.run_cli(*options)
        self.assertFalse((self.root / "alignments").exists())

    def test_model_selects_search_and_context_then_finishes(self):
        def response(prompt, **kwargs):
            p = json.loads(prompt)
            if "anchors" in p and not p["history"]:
                action = {"action": "search", "queries": [{"year": "2023", "query": "EPYC server revenue"}], "reason": "Check another candidate."}
            elif "anchors" in p and len(p["history"]) == 1:
                action = {"action": "context", "disclosure_ids": ["amd_2023_7_D001"], "reason": "Read original context."}
            else:
                return self.response(prompt, **kwargs)
            return CompletionResult(json.dumps(action), "test-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_cli(response=response), (0, 4))
        trace = self.output("traces/matching_001.json")
        self.assertEqual([entry["action"]["action"] for entry in trace], ["search", "context", "propose"])
        self.assertEqual(trace[0]["observation"]["search_results"][0]["scope"], "same_item_including_review_and_audit")
        self.assertEqual(trace[0]["observation"]["search_results"][0]["item"], "7")

    def graph_state(self, job_id, output=None):
        path = (output or self.root / "alignments/amd/2023-2024") / "graph/checkpoints.sqlite"
        with SqliteSaver.from_conn_string(str(path)) as saver:
            return saver.get_tuple({"configurable": {"thread_id": job_id, "checkpoint_ns": ""}}).checkpoint["channel_values"]

    def search_first_response(self, prompt, **kwargs):
        payload = json.loads(prompt)
        if "anchors" in payload and not payload["history"]:
            return CompletionResult(json.dumps({"action": "search", "queries": [{"year": "2023", "query": "EPYC"}]}),
                                    "test-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        return self.response(prompt, **kwargs)

    def test_langgraph_persists_search_and_resumes_without_repeating_tools(self):
        self.assertEqual(self.run_cli("--max-new-requests", "1", response=self.search_first_response), (2, 1))
        state = self.graph_state("matching_001")
        self.assertEqual(state["step"], 1)
        self.assertEqual(state["trace"][0]["action"]["action"], "search")
        self.assertNotIn("fake-secret", json.dumps(state))
        with patch.object(AlignmentData, "tool", side_effect=AssertionError("Completed search was repeated")):
            self.assertEqual(self.run_cli(response=self.search_first_response), (0, 2))
        self.assertEqual(self.output("manifest.json")["orchestration"]["policy"], GRAPH_POLICY)
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 375)

    def test_langgraph_restores_visible_evidence_after_restart(self):
        # The tool adds a real previous disclosure omitted from the initial
        # candidate window. Its visibility must survive the next CLI invocation.
        target = "amd_2023_7_D003"
        original = DisclosureIndex.candidates

        def candidates(index, *args):
            return {key: [hit for hit in hits if hit["disclosure_id"] != target]
                    for key, hits in original(index, *args).items()}

        with patch.object(DisclosureIndex, "candidates", candidates):
            self.assertEqual(self.run_cli("--max-new-requests", "1", response=self.search_first_response), (2, 1))
            self.assertIn(target, self.graph_state("matching_001")["visible"])
            with patch.object(AlignmentData, "tool", side_effect=AssertionError("Search was repeated")):
                self.assertEqual(self.run_cli(response=self.search_first_response), (0, 2))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})

    def test_langgraph_retains_accepted_verifier_rows_without_revalidation_on_resume(self):
        self.assertEqual(self.run_cli("--max-new-requests", "2", response=self.invalid_citation_response), (2, 2))
        state = self.graph_state("verification_001")
        self.assertEqual(len(state["repair"]["accepted"]), 2)
        self.assertEqual(state["step"], 1)
        original = VerificationRepair.receive

        def only_pending(repair, action, visible):
            for row in action["alignments"]:
                self.assertNotIn("amd_2023_7_D002", row["previous_ids"])
                self.assertNotIn("amd_2023_7_D003", row["previous_ids"])
            return original(repair, action, visible)

        with patch.object(VerificationRepair, "receive", only_pending):
            self.assertEqual(self.run_cli(response=self.invalid_citation_response), (0, 1))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})

    def test_langgraph_recovers_after_response_before_node_checkpoint_without_repaying(self):
        original = alignment_runtime.Runtime.call
        interrupted = False

        def crash_after_response(runtime, *args):
            nonlocal interrupted
            result = original(runtime, *args)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(alignment_runtime.Runtime, "call", crash_after_response), self.assertRaises(KeyboardInterrupt):
            self.run_cli()
        self.assertEqual(self.graph_state("matching_001")["step"], 0)
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 125)
        self.assertEqual(self.run_cli(), (0, 1))
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 250)

    def test_langgraph_recovers_after_model_checkpoint_before_validation(self):
        with patch.object(AlignmentAgentGraph, "validate_action", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_cli()
        state = self.graph_state("matching_001")
        self.assertEqual(state["step"], 1)
        self.assertEqual(state["action"]["action"], "propose")
        original = alignment_runtime.Runtime.call

        def reject_repeated_matching(runtime, role, *args):
            self.assertNotEqual(role, "matching", "Checkpointed model node must not be executed again")
            return original(runtime, role, *args)

        with patch.object(alignment_runtime.Runtime, "call", reject_repeated_matching):
            self.assertEqual(self.run_cli(), (0, 1))

    def test_langgraph_revalidation_archives_database_and_legacy_manifest_is_supported(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        manifest = self.output("manifest.json")
        del manifest["orchestration"]  # A pre-LangGraph manifest.
        write_json(output / "manifest.json", manifest)
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(self.run_cli("--revalidate-cache"), (0, 0))
        archived = list((output / "archives").glob("*/graph/checkpoints.sqlite"))
        self.assertEqual(len(archived), 1)
        with sqlite3.connect(archived[0]) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 250)

    def test_langgraph_offline_flag_never_resumes_a_pending_live_model_call(self):
        self.assertEqual(self.run_cli("--max-steps", "1", response=self.invalid_citation_response), (0, 2))
        source = self.root / "alignments/amd/2023-2024"
        target = self.root / "repaired"
        args = ("--repair-from", str(source), "--output-dir", str(target))
        self.assertEqual(self.run_cli(*args, "--max-total-tokens", "1"), (2, 0))
        before = self.graph_state("verification_001", target)
        self.assertEqual(self.run_cli(*args, "--offline-repair"), (2, 0))
        self.assertEqual(self.graph_state("verification_001", target), before)
        self.assertEqual(self.run_cli(*args, response=self.invalid_citation_response), (0, 1))

    def test_two_final_exports_partition_rows_and_defer_unmatched_until_complete(self):
        self.make_exact()
        data = self.data()
        decisions = exact_matches(data) + [
            self.supported_decision(["amd_2023_7_D002"], ["amd_2024_7_D002"], data),
            self.supported_decision(["amd_2023_7_D003"], [], data),
            module.review_row({"previous_ids": [], "current_ids": ["amd_2024_7_D003"]}, "Verification did not finish.")]
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        output = self.root / "alignments/amd/2023-2024"
        report = module.save_report(output, data, {}, [], [], decisions, runtime, complete=True)
        exported = []
        for name, statuses, count in (("alignments.json", {"ai_verified", "auto_matched", "unmatched"}, 3),
                                      ("needs_review.json", {"needs_review"}, 1)):
            doc = self.output(name)
            self.assertEqual(doc["alignment_count"], count)
            self.assertEqual(set(doc["included_statuses"]), statuses)
            self.assertEqual(doc["overall_counts"], report["counts"])
            self.assertEqual(sum(doc["counts"].values()), count)
            for row in doc["alignments"]:
                self.assertIn(row["status"], statuses)
                self.assertIn(row, report["alignments"])
                self.assertEqual(next(iter(row)), "match_id")
                self.assertEqual(row["change_analysis"], module.empty_change_analysis())
                self.assertNotIn("change_type", row)
                exported.append(row["match_id"])
        self.assertCountEqual(exported, [r["match_id"] for r in report["alignments"]])
        self.assertEqual(len(exported), len(set(exported)))
        exact = next(r for r in self.output("alignments.json")["alignments"] if r["status"] == "auto_matched")
        self.assertEqual(exact["match_method"], "exact_text")
        self.assertEqual(self.unmatched_rows()[0]["unmatched_type"], "removed_disclosure")
        self.assertEqual(self.saved_report(), report)
        self.assertTrue(all(not (output / name).exists() for name in module.LEGACY_RESULT_FILES))
        report["run_complete"] = False
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.unmatched_rows(), [])
        self.assertEqual(self.output("alignments.json")["counts"], {"auto_matched": 1, "ai_verified": 1})
        self.assertFalse(self.output("alignments.json")["run_complete"])
        self.assertEqual(self.output("needs_review.json")["alignment_count"], 2)
        self.assertTrue(all("unmatched_type" not in row for row in report["alignments"]))
        report["run_complete"] = True
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.unmatched_rows()[0]["unmatched_type"], "removed_disclosure")
        self.assertEqual(self.output("needs_review.json")["alignment_count"], 1)
        json_only = self.root / "json_only"
        module.write_alignment_json_reports(json_only, report)
        self.assertEqual({path.name for path in json_only.glob("*.json")}, {"alignments.json", "needs_review.json"})
        self.assertFalse(list(json_only.glob("*.md")))

    def test_change_analysis_survives_cached_rerun_but_not_changed_decision(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        report = self.saved_report()
        analysis = {"status": "completed", "lexical": {"test_score": 0.8}, "semantic": None,
                    "llm": None, "final_taxonomy": ["test_label"]}
        report["alignments"][0]["change_analysis"] = deepcopy(analysis)
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.run_cli(), (0, 0))
        saved = self.saved_report()
        self.assertEqual(saved["alignments"][0]["change_analysis"], analysis)
        self.assertEqual(self.output("alignments.json")["alignments"][0]["change_analysis"], analysis)
        self.assertEqual(saved["alignments"][1]["change_analysis"], module.empty_change_analysis())
        row = saved["alignments"][0]
        row.pop("change_analysis")
        row["explanation"] = "A changed alignment decision must be classified again."
        module.write_alignment_json_reports(output, saved)
        self.assertEqual(self.output("alignments.json")["alignments"][0]["change_analysis"], module.empty_change_analysis())

    def test_review_change_analysis_survives_cached_rerun(self):
        self.assertEqual(self.run_cli("--max-steps", "1", response=self.invalid_citation_response), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        report = self.saved_report()
        row = next(row for row in report["alignments"] if row["status"] == "needs_review")
        match_id = row["match_id"]
        analysis = {**module.empty_change_analysis(), "status": "completed", "lexical": {"score": 0.5}}
        row["change_analysis"] = analysis
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.run_cli("--max-steps", "1", response=self.invalid_citation_response), (0, 0))
        saved = next(row for row in self.output("needs_review.json")["alignments"] if row["match_id"] == match_id)
        self.assertEqual(saved["change_analysis"], analysis)

    def test_unmatched_change_analysis_survives_partial_reports_during_cached_rerun(self):
        def response(prompt, **kwargs):
            result = self.response(prompt, **kwargs)
            action = json.loads(result.text)
            for row in action.get("matches", []):
                row["previous_ids"] = []
            return CompletionResult(json.dumps(action), result.model, result.finish_reason, result.usage)
        self.assertEqual(self.run_cli(response=response), (0, 3))
        output = self.root / "alignments/amd/2023-2024"
        report = self.saved_report()
        self.assertEqual(report["counts"], {"unmatched": 6})
        row = report["alignments"][0]
        match_id = row["match_id"]
        analysis = {**module.empty_change_analysis(), "status": "completed", "lexical": {"score": 0.5}}
        row["change_analysis"] = analysis
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.run_cli(response=response), (0, 0))
        saved = next(row for row in self.output("alignments.json")["alignments"] if row["match_id"] == match_id)
        self.assertEqual(saved["change_analysis"], analysis)

    def test_legacy_combined_report_migrates_and_archives_redundant_views(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        report = self.saved_report()
        report["schema_version"] = "7"
        analysis = {**module.empty_change_analysis(), "status": "completed", "lexical": {"score": 0.5}}
        report["alignments"][0]["change_analysis"] = analysis
        write_json(output / "alignments.json", report)
        legacy = {}
        for name in module.LEGACY_RESULT_FILES:
            write_json(output / name, {"old_view": name})
            legacy[name] = (output / name).read_bytes()
        loaded = module.load_alignment_report(output)
        loaded["alignments"][0]["change_analysis"] = module.empty_change_analysis()
        module.write_alignment_json_reports(output, loaded)
        self.assertEqual(self.saved_report()["schema_version"], "8")
        self.assertEqual(self.saved_report()["alignments"][0]["change_analysis"], analysis)
        archive = next((output / "archives").glob("legacy_result_views_*"))
        for name, content in legacy.items():
            self.assertFalse((output / name).exists())
            self.assertEqual((archive / name).read_bytes(), content)
        snapshot = {path: path.read_bytes() for path in output.rglob("*") if path.is_file()}
        module.write_alignment_json_reports(output, self.saved_report())
        self.assertEqual(snapshot, {path: path.read_bytes() for path in output.rglob("*") if path.is_file()})

    def test_saved_partition_rejects_missing_mismatched_or_duplicate_review_rows(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        review = self.output("needs_review.json")
        (output / "needs_review.json").unlink()
        with self.assertRaises(FileNotFoundError):
            module.load_alignment_report(output)
        changed = deepcopy(review)
        changed["current_year"] = "2025"
        write_json(output / "needs_review.json", changed)
        with self.assertRaisesRegex(ValueError, "same saved report"):
            module.load_alignment_report(output)
        changed = deepcopy(review)
        changed["alignments"] = [deepcopy(self.output("alignments.json")["alignments"][0])]
        changed["alignments"][0]["status"] = "needs_review"
        write_json(output / "needs_review.json", changed)
        with self.assertRaisesRegex(ValueError, "duplicate match IDs"):
            module.load_alignment_report(output)

    def test_pause_resume_and_manifest_reject_changed_inputs(self):
        self.assertEqual(self.run_cli("--max-new-requests", "1"), (2, 1))
        self.assertFalse(self.saved_report()["run_complete"])
        self.assertEqual(self.run_cli(), (0, 1))
        self.assertEqual(self.run_cli("--top-k", "2"), (1, 0))

    def test_dry_run_needs_no_api_and_writes_nothing(self):
        self.assertEqual(self.run_cli("--dry-run"), (0, 0))
        self.assertFalse((self.root / "alignments").exists())

    def test_stale_source_rejected_before_spending(self):
        path = self.root / "raw/amd/2023/2023_chunks.json"
        path.write_text(path.read_text() + " ")
        self.assertEqual(self.run_cli(), (1, 0))

    def test_invented_citations_and_wrong_year_matches_rejected(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        with self.assertRaises(ValueError):
            module.validate_matches({"action": "propose", "matches": [{"current_id": after, "previous_ids": [after], "rationale": "wrong"}]}, set(data.records), [after], data)
        row = {"previous_ids": [before], "current_ids": [after], "explanation": "Test", "needs_review": False,
               "evidence": [{"disclosure_id": before, "sentence_ids": ["invented"]}]}
        with self.assertRaises(ValueError):
            module.validate_alignments({"action": "finalize", "alignments": [row]}, set(data.records), {before, after}, data)

    def test_split_merge_components_and_disjoint_coverage(self):
        data = self.data()
        matches = [{"current_id": "amd_2024_7_D001", "previous_ids": ["amd_2023_7_D001", "amd_2023_7_D002"]},
                   {"current_id": "amd_2024_7_D002", "previous_ids": ["amd_2023_7_D002"]}]
        groups = module.connected_groups(data, matches)
        self.assertEqual(len(groups), 3)
        self.assertEqual(module.relationship(groups[0]["previous_ids"], groups[0]["current_ids"]), "many_to_many")
        self.assertEqual(sum(len(g["previous_ids"] + g["current_ids"]) for g in groups), 6)

    def test_candidates_filter_items_in_both_directions_even_for_identical_text(self):
        data = self.data()
        data.records["amd_2023_7_D001"]["item"] = "1"
        data.records["amd_2023_7_D001"]["taxonomy"] = "Technology & AI"
        index = DisclosureIndex(data.records)
        hits = index.search("", "2023", anchor=data.records["amd_2024_7_D001"])
        self.assertNotIn("amd_2023_7_D001", [h["disclosure_id"] for h in hits])
        self.assertTrue(all(h["same_item"] for h in hits))
        for key, hits in index.candidates("2023", "2024", top_k=1).items():
            self.assertTrue(all(data.records[h["disclosure_id"]]["item"] == data.records[key]["item"] for h in hits))
        # There is no Item 1 counterpart in 2024: do not fall back to another Item.
        self.assertEqual(index.search("", "2024", anchor=data.records["amd_2023_7_D001"]), [])
        with self.assertRaises(ValueError):
            index.search("EPYC", "2023")

    def test_search_audit_context_and_pagination_stay_in_exact_item(self):
        data = self.data()
        one, one_a, other = "amd_2023_7_D001", "amd_2023_7_D002", "amd_2023_7_D003"
        data.records[one]["item"] = "1"
        data.records[one_a]["item"] = "1A"
        data.audit = {key + "_AUDIT": {**data.records[key], "disclosure_id": key + "_AUDIT"} for key in (one, one_a, other)}
        data.index, data.audit_index = DisclosureIndex(data.records), DisclosureIndex(data.audit)
        query = "EPYC server revenue Patent litigation Debt maturity"
        result = data.search("2023", query, item="1")
        self.assertEqual([h["disclosure_id"] for h in result["hits"]], [one])
        self.assertEqual([h["disclosure_id"] for h in result["audit_hits"]], [one + "_AUDIT"])
        self.assertEqual(data.search("2023", query, offset=1, item="1")["hits"], [])
        visible = {one}
        data.tool({"action": "search", "queries": [{"year": "2023", "query": query}]}, visible, item="1")
        self.assertEqual(visible, {one})
        with self.assertRaisesRegex(ValueError, "cannot change"):
            data.tool({"action": "search", "queries": [{"year": "2023", "query": query, "item": "1A"}]}, visible, item="1")
        with self.assertRaisesRegex(ValueError, "different SEC Item"):
            data.tool({"action": "context", "disclosure_ids": [one_a]}, {one, one_a}, item="1")

    def test_cross_item_matching_and_verification_are_rejected_and_repairable(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        data.records[before]["item"], data.records[after]["item"] = "1", "1A"
        matches = [{"current_id": after, "previous_ids": [before], "rationale": "Identical topic."}]
        with self.assertRaisesRegex(ValueError, "one SEC Item"):
            module.validate_matches({"action": "propose", "matches": matches}, set(data.records), [after], data)
        with self.assertRaises(ValueError):
            module.connected_groups(data, matches)
        row = {"previous_ids": [before], "current_ids": [after], "explanation": "Identical topic.", "needs_review": False,
               "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in (before, after)]}
        state = VerificationRepair(data, {after}, module.validate_alignments, module.review_row)
        state.receive({"action": "finalize", "alignments": [row]}, set(data.records))
        self.assertFalse(state.accepted)
        self.assertEqual(state.events[0]["errors"][0]["error_type"], "cross_item_alignment")
        self.assertEqual(state.pending, {after})
        correction = state.correction_payload({"proposed_groups": [{"previous_ids": [], "current_ids": [after]}]})
        self.assertEqual(correction["item"], "1A")
        self.assertEqual([r["disclosure_id"] for r in correction["records"]], [after])
        self.assertEqual(state.results()[0]["status"], "needs_review")

    def test_same_item_one_to_many_is_accepted_as_one_group(self):
        data = self.data()
        before, after = ["amd_2023_7_D001"], ["amd_2024_7_D001", "amd_2024_7_D002"]
        row = {"previous_ids": before, "current_ids": after, "explanation": "One topic split into two disclosures.",
               "needs_review": False, "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in before + after]}
        decisions = module.validate_verification_response({"action": "finalize", "alignments": [row]}, set(data.records), set(before + after), data)
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        report = module.save_report(self.root / "split", data, {}, [], [], decisions, runtime, complete=False)
        split = next(r for r in report["alignments"] if r["relationship"] == "one_to_many")
        self.assertEqual(split["status"], "ai_verified")
        self.assertEqual(split["current_ids"], after)
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [])

    def supported_decision(self, before, after, data=None):
        data = data or self.data()
        proposal = {"previous_ids": before, "current_ids": after, "explanation": "Supported topic correspondence.",
                    "needs_review": False, "review_reason": "", "evidence": [
                        {"disclosure_id": key, "sentence_ids": list(data.records[key]["sentence_map"])[:1]}
                        for key in before + after]}
        return module.validate_alignments({"action": "finalize", "alignments": [proposal]},
                                          set(data.records), set(before + after), data)[0]

    def legacy_single_sentence(self, number=1, other_flags=(), *, warning="fewer_than_two_source_units"):
        path = self.root / "disclosures/amd/2023/disclosures.json"
        doc = json.loads(path.read_text())
        key = f"amd_2023_7_D{number:03d}"
        record = next(d for d in doc["disclosures"] if d["disclosure_id"] == key)
        doc["disclosures"].remove(record)
        write_json(path, doc)
        source = record["sources"][0]
        source["sentences"] = source["sentences"][:1]
        source["selected_text"] = record["content"] = source["sentences"][0]["text"]
        record["verification"] = {"status": "needs_review", "source_unit_count": 1,
                                  "review_reasons": [warning, *other_flags], "model_review_reason": ""}
        path = path.with_name("review_candidates.json")
        doc = json.loads(path.read_text())
        doc["disclosures"].append(record)
        write_json(path, doc)
        return key

    def test_alignment_ignores_only_retired_length_warning_on_legacy_input(self):
        key = self.legacy_single_sentence()
        boundary = self.legacy_single_sentence(2, ["section_continues_across_batch_boundary"])
        paths = list((self.root / "disclosures").rglob("*.json"))
        original = {p: p.read_bytes() for p in paths}
        data = self.data()
        self.assertEqual(data.records[key]["extraction_status"], "source_validated")
        self.assertEqual(data.view(key)["extraction_review_reasons"], [])
        self.assertEqual(data.records[boundary]["extraction_status"], "needs_review")
        self.assertEqual(data.view(boundary)["extraction_review_reasons"], ["section_continues_across_batch_boundary"])
        self.assertEqual({p: p.read_bytes() for p in paths}, original)

    def test_alignment_waives_unassigned_neighbor_flag_without_expanding_evidence(self):
        key = self.legacy_single_sentence(warning="unassigned_context_in_source_paragraph")
        flagged = self.legacy_single_sentence(2, ["possible_missing_table_context"], warning="unassigned_context_in_source_paragraph")
        data = self.data()
        self.assertEqual(data.records[key]["extraction_status"], "source_validated")
        self.assertEqual(data.view(key)["extraction_review_reasons"], [])
        self.assertEqual(len(data.records[key]["sentence_map"]), 1)
        self.assertEqual(data.records[flagged]["extraction_status"], "needs_review")
        self.assertEqual(data.view(flagged)["extraction_review_reasons"], ["possible_missing_table_context"])
        row = self.supported_decision([key], ["amd_2024_7_D001"], data)
        row.update(status="needs_review", review_reasons=["input_extraction_needs_review"])
        refreshed = module.apply_extraction_policy_to_alignments([row], data)[0]
        self.assertEqual(refreshed["status"], "ai_verified")
        self.assertEqual(refreshed["evidence"], row["evidence"])

    def test_refresh_length_policy_does_not_clear_other_alignment_concerns(self):
        key = self.legacy_single_sentence()
        data = self.data()
        base = self.supported_decision([key], ["amd_2024_7_D001"], data)
        base.update(status="needs_review", review_reasons=["input_extraction_needs_review"])
        original = deepcopy(base)
        cleared = module.apply_extraction_policy_to_alignments([base], data)[0]
        self.assertEqual(cleared["status"], "ai_verified")
        self.assertEqual(cleared["review_reasons"], [])
        self.assertEqual(base, original)
        for reason in ("missing_review_metadata", "verifier_requested_review", "conflicting_alignment_assignment",
                       "invalid_or_omitted_verifier_proposal", "agent_did_not_finish"):
            row = deepcopy(base)
            row["review_reasons"].append(reason)
            result = module.apply_extraction_policy_to_alignments([row], data)[0]
            self.assertEqual(result["status"], "needs_review")
            self.assertIn(reason, result["review_reasons"])
        invalid = deepcopy(base)
        invalid["evidence"][0]["sentences"][0]["text"] = "Invented evidence."
        self.assertEqual(module.apply_extraction_policy_to_alignments([invalid], data)[0]["status"], "needs_review")

    def test_offline_source_policy_refresh_is_idempotent_and_preserves_evidence(self):
        key = self.legacy_single_sentence(other_flags=["unassigned_context_in_source_paragraph"])
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        report = self.saved_report()
        row = next(r for r in report["alignments"] if key in r["previous_ids"])
        row.update(status="needs_review", review_reasons=["input_extraction_needs_review"])
        row["previous_disclosures"][0]["extraction_status"] = "needs_review"
        module.write_alignment_reports(output, report, self.output("token_usage.json"))
        before = (output / "alignments.json").read_bytes()
        protected = {p: p.read_bytes() for p in (self.root / "disclosures").rglob("*.json")}
        protected.update({p: p.read_bytes() for p in output.rglob("*.json") if p.parent.name in {"requests", "jobs", "traces"}
                          or p.name in {"manifest.json", "token_usage.json"}})
        with patch.object(module, "load_config", side_effect=AssertionError("No API config needed")), \
             patch.object(alignment_runtime, "request_completion", side_effect=AssertionError("No API calls allowed")), \
             redirect_stdout(io.StringIO()):
            argv = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024", "--data-dir", str(self.root),
                    "--refresh-extraction-policy"]
            self.assertEqual(module.main(argv), 0)
            refreshed = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            self.assertEqual(module.main(argv), 0)
            self.assertEqual({p: p.read_bytes() for p in output.rglob("*") if p.is_file()}, refreshed)
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})
        saved = self.saved_report()
        self.assertEqual(saved["counts"], {"ai_verified": 3})
        for old, new in zip(report["alignments"], saved["alignments"]):
            for field in ("match_id", "previous_ids", "current_ids", "evidence", "explanation"):
                self.assertEqual(old[field], new[field])
        archive = next((output / "archives").glob("before_extraction_policy_*"))
        self.assertEqual((archive / "alignments.json").read_bytes(), before)
        self.assertEqual(self.output("extraction_policy_summary.json")["additional_api_tokens"], 0)

    def test_overlapping_matches_become_one_to_many_with_original_evidence(self):
        data = self.data()
        before, first, second = "amd_2023_7_D001", "amd_2024_7_D001", "amd_2024_7_D002"
        rows = [self.supported_decision([before], [key], data) for key in (first, second)]
        for i, row in enumerate(rows):
            row.update(match_id=f"M{i + 1:04d}", status="needs_review", review_reasons=["conflicting_alignment_assignment"])
            row["explanation"] = f"Original rationale {i}."
        untouched = deepcopy(rows)
        grouped = module.consolidate_alignments(rows, data)
        self.assertEqual(rows, untouched)
        self.assertEqual(len(grouped), 1)
        row = grouped[0]
        self.assertEqual(row["match_id"], "M0001")
        self.assertEqual(row["previous_ids"], [before])
        self.assertEqual(row["current_ids"], [first, second])
        self.assertEqual(row["relationship"], "one_to_many")
        self.assertEqual(row["status"], "ai_verified")
        self.assertEqual(row["review_reasons"], [])
        self.assertEqual(len(row["evidence"]), 3)
        self.assertEqual(row["grouping"]["source_alignments"], rows)
        self.assertEqual(row["explanation"], "Original rationale 0.\n\nOriginal rationale 1.")
        self.assertEqual(module.consolidate_alignments(grouped, data), grouped)

    def test_many_to_one_retains_extraction_metadata_and_verifier_flags(self):
        data = self.data()
        first, second, after = "amd_2023_7_D001", "amd_2023_7_D002", "amd_2024_7_D001"
        data.records[first]["extraction_status"] = "needs_review"
        rows = [self.supported_decision([key], [after], data) for key in (first, second)]
        rows[1].update(status="needs_review", review_reasons=["missing_review_metadata", "verifier_requested_review"],
                       metadata_repairs=["needs_review", "review_reason"], verifier_review_reason="Missing fields.")
        # Keep distinct valid citations for the shared disclosure, deduplicating only identical ones.
        record = data.records[after]
        source = record["sources"][0]
        rows[1]["evidence"][1]["sentences"] = [{**source["sentences"][1], "paragraph_id": source["paragraph_id"],
                                                "source_url": source["source_url"]}]
        row = module.consolidate_alignments(rows, data)[0]
        self.assertEqual(row["relationship"], "many_to_one")
        self.assertEqual(row["status"], "needs_review")
        self.assertEqual(set(row["review_reasons"]), {"input_extraction_needs_review", "missing_review_metadata", "verifier_requested_review"})
        self.assertEqual(row["metadata_repairs"], ["needs_review", "review_reason"])
        self.assertEqual(len(row["evidence"][-1]["sentences"]), 2)

    def test_many_to_many_chain_is_not_inferred_from_overlapping_rows(self):
        a, b, x, y = "amd_2023_7_D001", "amd_2023_7_D002", "amd_2024_7_D001", "amd_2024_7_D002"
        rows = [self.supported_decision([a], [x]), self.supported_decision([a], [y]), self.supported_decision([b], [y])]
        self.assertEqual(module.consolidate_alignments(rows, self.data()), rows)

    def overlapping_decisions(self, data):
        a, b, c = [f"amd_2023_7_D{i:03d}" for i in (1, 2, 3)]
        x, y, z = [f"amd_2024_7_D{i:03d}" for i in (1, 2, 3)]
        return [self.supported_decision([a], [x, y], data),
                self.supported_decision([b], [y, z], data),
                self.supported_decision([c], [], data)]

    def test_verifier_accepts_shared_ids_across_matched_rows_with_exact_coverage(self):
        data = self.data()
        rows = self.overlapping_decisions(data)
        proposals = [{"previous_ids": r["previous_ids"], "current_ids": r["current_ids"],
                      "explanation": r["explanation"], "needs_review": False, "review_reason": "",
                      "evidence": [{"disclosure_id": e["disclosure_id"],
                                    "sentence_ids": [s["sentence_id"] for s in e["sentences"]]} for e in r["evidence"]]}
                     for r in rows]
        action = {"action": "finalize", "alignments": proposals}
        self.assertEqual(module.validate_alignments(action, set(data.records), set(data.records), data), rows)
        repair = VerificationRepair(data, set(data.records), module.validate_alignments, module.review_row)
        repair.receive(action, set(data.records))
        self.assertFalse(repair.pending)
        self.assertEqual(repair.results(), rows)
        # Repeated IDs within a single row remain invalid.
        broken = deepcopy(action)
        broken["alignments"][0]["current_ids"].append(broken["alignments"][0]["current_ids"][0])
        with self.assertRaisesRegex(ValueError, "unique allowed IDs"):
            module.validate_alignments(broken, set(data.records), set(data.records), data)

    def test_supported_overlapping_groups_preserve_links_and_survive_report_refresh(self):
        data = self.data()
        decisions = self.overlapping_decisions(data)
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        output = self.root / "alignments/amd/2023-2024"
        report = module.save_report(output, data, {}, [], [], decisions, runtime, complete=True)
        y = "amd_2024_7_D002"
        self.assertEqual(report["counts"], {"ai_verified": 2, "unmatched": 1})
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [])
        self.assertEqual(report["coverage"]["permitted_overlap_disclosure_ids"], [y])
        matched = [r for r in report["alignments"] if r["status"] == "ai_verified"]
        for original, saved in zip(decisions[:2], matched):
            for key in ("previous_ids", "current_ids", "evidence", "explanation"):
                self.assertEqual(saved[key], original[key])
        self.assertNotIn(("amd_2023_7_D001", "amd_2024_7_D003"),
                         {(a, b) for r in matched for a in r["previous_ids"] for b in r["current_ids"]})
        saved = deepcopy(report)
        module.write_alignment_reports(output, report, runtime.report())
        self.assertEqual(report, saved)
        self.assertEqual(self.output("needs_review.json")["alignments"], [])
        # Permission to share Y between matches never permits declaring Y unmatched too.
        additional = self.supported_decision([], [y], data)
        additional["match_id"] = "additional_unmatched_proposal"
        report["alignments"].append(additional)
        module.write_alignment_json_reports(output, report)
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [y])
        self.assertTrue(all(r["status"] == "needs_review" for r in report["alignments"] if y in r["current_ids"]))
        self.assertEqual(self.unmatched_rows("current"), [])

    def test_overlap_policy_clears_only_conflicts_and_rejects_invalid_shared_evidence(self):
        data = self.data()
        rows = self.overlapping_decisions(data)[:2]
        for row in rows:
            row.update(status="needs_review", review_reasons=["conflicting_alignment_assignment"])
        rows[1]["review_reasons"].extend(["missing_review_metadata", "verifier_requested_review"])
        rows[1]["metadata_repairs"] = ["needs_review", "review_reason"]
        module.mark_assignment_conflicts(rows, data)
        self.assertEqual(rows[0]["status"], "ai_verified")
        self.assertEqual(rows[1]["status"], "needs_review")
        self.assertEqual(rows[1]["review_reasons"], ["missing_review_metadata", "verifier_requested_review"])
        self.assertEqual(rows[1]["metadata_repairs"], ["needs_review", "review_reason"])
        for failure in ("text", "missing_citation", "year", "item", "unfinished"):
            with self.subTest(failure=failure):
                data = self.data()
                rows = self.overlapping_decisions(data)[:2]
                if failure == "text":
                    rows[1]["evidence"][0]["sentences"][0]["text"] = "Invented quote."
                elif failure == "missing_citation":
                    rows[1]["evidence"].pop()
                elif failure in ("year", "item"):
                    data.records[rows[1]["previous_ids"][0]]["fiscal_year" if failure == "year" else "item"] = "wrong"
                else:
                    rows[1].update(status="needs_review", review_reasons=["agent_did_not_finish"])
                _, conflicts = module.mark_assignment_conflicts(rows, data)
                self.assertEqual(set(conflicts), {"amd_2024_7_D002"})
                self.assertTrue(all("conflicting_alignment_assignment" in r["review_reasons"] for r in rows))

    def test_offline_overlap_refresh_preserves_groups_ids_caches_and_tokens(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        data, usage = self.data(), self.output("token_usage.json")
        report = module.save_report(output, data, {}, [], [], self.overlapping_decisions(data),
                                    SimpleNamespace(report=lambda: usage), complete=True)
        report.pop("assignment_policy")
        report["schema_version"] = "4"
        report["coverage"].pop("permitted_overlap_disclosure_ids")
        report["coverage"]["conflicting_disclosure_ids"] = ["amd_2024_7_D002"]
        for row in report["alignments"]:
            if row["previous_ids"] and row["current_ids"]:
                row.update(status="needs_review", review_reasons=["conflicting_alignment_assignment"])
        report["counts"] = {"needs_review": 2, "unmatched": 1}
        write_json(output / "alignments.json", report)
        before = (output / "alignments.json").read_bytes()
        protected = {p: p.read_bytes() for p in output.rglob("*.json") if p.parent.name in {"jobs", "requests", "traces"}
                     or p.name in {"manifest.json", "token_usage.json"}}
        with patch.object(module, "load_config", side_effect=AssertionError("No API configuration needed")), \
             patch.object(alignment_runtime, "request_completion", side_effect=AssertionError("No API calls")), \
             redirect_stdout(io.StringIO()):
            argv = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                    "--data-dir", str(self.root), "--regroup-only"]
            self.assertEqual(module.main(argv), 0)
            snapshot = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            self.assertEqual(module.main(argv), 0)
            self.assertEqual(snapshot, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})
        refreshed = self.saved_report()
        self.assertEqual(refreshed["counts"], {"ai_verified": 2, "unmatched": 1})
        for old, new in zip(report["alignments"], refreshed["alignments"]):
            for key in ("match_id", "previous_ids", "current_ids", "evidence", "explanation"):
                self.assertEqual(old[key], new[key])
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})
        archive = next((output / "archives").glob("before_overlap_grouping_*"))
        self.assertEqual((archive / "alignments.json").read_bytes(), before)
        self.assertEqual(self.output("regrouping_summary.json")["additional_api_tokens"], 0)

    def test_contradiction_or_failed_proposal_blocks_whole_component(self):
        a, x, y = "amd_2023_7_D001", "amd_2024_7_D001", "amd_2024_7_D002"
        valid = [self.supported_decision([a], [x]), self.supported_decision([a], [y])]
        unmatched = self.supported_decision([a], [])
        failed = module.review_row({"previous_ids": [a], "current_ids": []}, "Unfinished verification.")
        for extra in (unmatched, failed):
            rows = valid + [extra]
            self.assertEqual(module.consolidate_alignments(rows, self.data()), rows)

    def test_regrouping_rejects_wrong_year_item_and_invalid_evidence(self):
        a, x, y = "amd_2023_7_D001", "amd_2024_7_D001", "amd_2024_7_D002"
        for error in ("text", "missing_citation", "year", "item"):
            with self.subTest(error=error):
                data = self.data()
                rows = [self.supported_decision([a], [x], data), self.supported_decision([a], [y], data)]
                if error == "text":
                    rows[1]["evidence"][0]["sentences"][0]["text"] = "Invented quotation."
                elif error == "missing_citation":
                    rows[1]["evidence"].pop()
                elif error == "year":
                    data.records[y]["fiscal_year"] = "2023"
                else:
                    data.records[y]["item"] = "1A"
                self.assertEqual(module.consolidate_alignments(rows, data), rows)

    def test_full_workflow_consolidates_verifier_overlap_without_new_requests(self):
        def response(prompt, **kwargs):
            completion = self.response(prompt, **kwargs)
            payload = json.loads(prompt)
            if "anchors" not in payload:
                action = json.loads(completion.text)
                extra = deepcopy(action["alignments"][0])
                extra["current_ids"] = action["alignments"][1]["current_ids"]
                extra["evidence"] = [extra["evidence"][0], action["alignments"][1]["evidence"][1]]
                action["alignments"][1]["previous_ids"] = []
                action["alignments"][1]["current_ids"] = []
                # Preserve the second previous ID as a supported no-counterpart decision.
                action["alignments"][1]["previous_ids"] = ["amd_2023_7_D002"]
                action["alignments"][1]["evidence"] = action["alignments"][1]["evidence"][:1]
                action["alignments"].append(extra)
                return CompletionResult(json.dumps(action), "test-model", "stop", completion.usage)
            return completion
        self.assertEqual(self.run_cli(response=response), (0, 2))
        report = self.saved_report()
        self.assertEqual(report["counts"], {"ai_verified": 2, "unmatched": 1})
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [])
        self.assertEqual(report["grouping_summary"]["consolidated_groups"], 1)

    def test_regroup_only_archives_preserves_ids_and_never_loads_api_config(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        data = self.data()
        decisions = [self.supported_decision(["amd_2023_7_D001"], [key], data)
                     for key in ("amd_2024_7_D001", "amd_2024_7_D002")]
        usage = self.output("token_usage.json")
        with patch.object(module, "consolidate_alignments", side_effect=lambda rows, data: deepcopy(rows)):
            module.save_report(output, data, {}, [], [], decisions, SimpleNamespace(report=lambda: usage), complete=True)
        original = (output / "alignments.json").read_bytes()
        protected = {p: p.read_bytes() for p in output.rglob("*.json") if p.parent.name in {"jobs", "requests", "traces"}
                     or p.name in {"token_usage.json", "manifest.json"}}
        argv = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024", "--data-dir", str(self.root), "--regroup-only"]
        with patch.object(module, "load_config", side_effect=AssertionError("Must not read API config")), \
             patch.object(alignment_runtime, "request_completion", side_effect=AssertionError("Must not call API")), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(argv), 0)
            first = {p: p.read_bytes() for p in output.rglob("*") if p.is_file()}
            self.assertEqual(module.main(argv), 0)
            self.assertEqual(first, {p: p.read_bytes() for p in output.rglob("*") if p.is_file()})
        self.assertEqual(protected, {p: p.read_bytes() for p in protected})
        archive = next((output / "archives").glob("before_overlap_grouping_*"))
        self.assertEqual((archive / "alignments.json").read_bytes(), original)
        merged = next(row for row in self.saved_report()["alignments"] if "grouping" in row)
        self.assertEqual(merged["match_id"], merged["grouping"]["source_alignments"][0]["match_id"])
        self.assertEqual(len(merged["current_disclosures"]), 2)
        self.assertEqual(self.output("regrouping_summary.json")["additional_api_tokens"], 0)
        self.assertEqual(self.output("needs_review.json")["alignments"],
                         [r for r in self.saved_report()["alignments"] if r["status"] == "needs_review"])

    def test_regroup_only_refuses_legacy_scope_and_stale_inputs(self):
        self.assertEqual(self.run_cli(), (0, 2))
        output = self.root / "alignments/amd/2023-2024"
        original = (output / "alignments.json").read_bytes()
        manifest_path = output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for error in ("legacy", "stale"):
            broken = deepcopy(manifest)
            if error == "legacy":
                broken.pop("comparison_scope")
            else:
                broken["input_hashes"][next(iter(broken["input_hashes"]))] = "stale"
            write_json(manifest_path, broken)
            self.assertEqual(self.run_cli("--regroup-only"), (1, 0))
            self.assertEqual((output / "alignments.json").read_bytes(), original)
            self.assertFalse((output / "archives").exists())

    def test_full_workflow_and_absence_searches_use_separate_item_jobs(self):
        for year in ("2023", "2024"):
            self.set_fixture_item(year, 1, "1")
            self.set_fixture_item(year, 2, "1A")
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            self.assertTrue(all(r["item"] == payload["item"] for r in payload["records"]))
            for search in payload.get("automatic_absence_searches", []):
                self.assertEqual(search["item"], payload["item"])
                self.assertTrue(all(h["record"]["item"] == payload["item"] for h in search["hits"] + search["audit_hits"]))
            original = self.response(prompt, **kwargs)
            action = json.loads(original.text)
            if payload["item"] == "1" and "anchors" in payload:
                action["matches"][0]["previous_ids"] = []
            return CompletionResult(json.dumps(action), original.model, original.finish_reason, original.usage)
        self.assertEqual(self.run_cli(response=response), (0, 7))
        report = self.saved_report()
        self.assertEqual(report["counts"], {"unmatched": 2, "ai_verified": 2})
        for row in report["alignments"]:
            self.assertEqual(len({r["item"] for r in row["previous_disclosures"] + row["current_disclosures"]}), 1)
        self.assertEqual(self.run_cli(response=response), (0, 0))

    def test_step_limit_preserves_tokens_and_review(self):
        def malformed(*args, **kwargs):
            return CompletionResult("invalid json", "test-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_cli("--max-steps", "1", response=malformed), (0, 3))
        report = self.saved_report()
        self.assertEqual(report["counts"], {"needs_review": 6})
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 375)
        for side in ("previous", "current"):
            self.assertEqual(sum(len(row[side + "_ids"]) for row in self.unmatched_rows(side)), 0)

    def test_unmatched_absence_flag_is_retained_until_global_report_checks(self):
        data = self.data()
        for year, relation in (("2023", "previous_only"), ("2024", "current_only")):
            with self.subTest(year=year):
                key = f"amd_{year}_7_D001"
                row = {"previous_ids": [key] if year == "2023" else [], "current_ids": [key] if year == "2024" else [],
                       "explanation": "No counterpart found.", "needs_review": False,
                       "evidence": [{"disclosure_id": key, "sentence_ids": list(data.records[key]["sentence_map"])[:1]}]}
                result = module.validate_alignments({"action": "finalize", "alignments": [row]}, {key}, {key}, data)
                self.assertEqual(result[0]["relationship"], relation)
                self.assertEqual(result[0]["status"], "needs_review")
                self.assertEqual(result[0]["review_reasons"], ["absence_not_proven_by_retrieval"])
                self.assertNotIn("change_type", result[0])

    def test_token_budget_stops_before_call(self):
        self.assertEqual(self.run_cli("--max-total-tokens", "1"), (2, 0))
        self.assertFalse(self.saved_report()["run_complete"])

    def test_unknown_usage_blocks_further_paid_calls(self):
        def unknown(prompt, **kwargs):
            response = self.response(prompt, **kwargs)
            return CompletionResult(response.text, response.model, response.finish_reason, None)
        self.assertEqual(self.run_cli(response=unknown), (2, 1))
        report = self.output("token_usage.json")
        self.assertFalse(report["usage_complete"])
        self.assertEqual(report["requests_with_unknown_usage"], 1)

    def test_retry_failed_reserves_unknown_usage_and_continues_later_jobs(self):
        def failed(*args, **kwargs):
            raise RuntimeError("Simulated interrupted API call")
        self.assertEqual(self.run_cli(response=failed), (1, 1))
        directory = self.root / "alignments/amd/2023-2024/requests"
        path = next(directory.glob("*.json"))
        original = path.read_bytes()
        record = json.loads(original)
        reserve = len((record["system_prompt"] + record["prompt"]).encode()) + record["max_tokens"]
        self.assertEqual(self.run_cli("--retry-failed"), (0, 2))
        usage = self.output("token_usage.json")
        self.assertTrue(usage["run_complete"])
        self.assertFalse(usage["usage_complete"])
        self.assertEqual(usage["requests_with_unknown_usage"], 1)
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(usage["unknown_usage_budget_reserve"]["estimated_tokens"], reserve)
        self.assertEqual(usage["budget_accounted_tokens"], 250 + reserve)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.run_cli("--retry-failed"), (0, 0))

    def test_retry_failed_budget_includes_both_prior_attempt_and_next_request(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli(response=lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        directory = self.root / "alignments/amd/2023-2024/requests"
        record = json.loads(next(directory.glob("*.json")).read_text())
        reserve = len((record["system_prompt"] + record["prompt"]).encode()) + record["max_tokens"]
        self.assertEqual(self.run_cli("--retry-failed", "--max-total-tokens", str(2 * reserve - 1)), (2, 0))
        usage = self.output("token_usage.json")
        self.assertEqual(usage["budget_accounted_tokens"], reserve)
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 0)

    def test_retry_failed_cannot_acknowledge_successful_response_without_usage(self):
        def unknown(prompt, **kwargs):
            response = self.response(prompt, **kwargs)
            return CompletionResult(response.text, response.model, response.finish_reason, None)
        self.assertEqual(self.run_cli(response=unknown), (2, 1))
        self.assertEqual(self.run_cli("--retry-failed"), (2, 0))
        reserve = self.output("token_usage.json")["unknown_usage_budget_reserve"]
        self.assertEqual(reserve["estimated_tokens"], 0)
        self.assertEqual(reserve["unacknowledged_attempt_count"], 1)

    def test_retry_failed_does_not_cover_new_unknown_usage_in_same_invocation(self):
        def failed(*args, **kwargs):
            raise RuntimeError("Simulated interrupted API call")
        def unknown(prompt, **kwargs):
            response = self.response(prompt, **kwargs)
            return CompletionResult(response.text, response.model, response.finish_reason, None)
        self.assertEqual(self.run_cli(response=failed), (1, 1))
        self.assertEqual(self.run_cli("--retry-failed", response=unknown), (2, 1))
        usage = self.output("token_usage.json")
        self.assertEqual(usage["requests_with_unknown_usage"], 2)
        self.assertEqual(usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"], 1)

    def test_global_conflicts_flag_every_affected_row(self):
        data = self.data()
        old = "amd_2023_7_D001"
        decisions = []
        for current in ("amd_2024_7_D001", "amd_2024_7_D002"):
            decisions.append({"previous_ids": [old], "current_ids": [current], "relationship": "one_to_one",
                "change_type": "Modified", "explanation": "Candidate relationship.", "evidence": [],
                "status": "ai_verified", "review_reasons": [], "verifier_review_reason": ""})
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        output = self.root / "conflict-test"
        report = module.save_report(output, data, {}, [], [], decisions, runtime, complete=False)
        affected = [row for row in report["alignments"] if old in row["previous_ids"]]
        self.assertEqual(len(affected), 2)
        self.assertTrue(all(row["status"] == "needs_review" for row in affected))
        self.assertTrue(all("conflicting_alignment_assignment" in row["review_reasons"] for row in affected))
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [old])
        self.assertEqual(decisions[0]["review_reasons"], [])
        # A legacy cached label must not leak into either JSON report or Markdown.
        self.assertNotIn('"change_type"', json.dumps(report))
        self.assertNotIn("change_counts", report)
        self.assertNotIn('"change_type"', (output / "needs_review.json").read_text())
        self.assertNotIn("Modified", (output / "alignments.md").read_text())
        self.assertEqual(decisions[0]["change_type"], "Modified")

    def test_tool_access_is_limited_to_comparison_years_and_visible_ids(self):
        data = self.data()
        with self.assertRaises(ValueError):
            data.tool({"action": "search", "queries": [{"year": "2030", "query": "patents"}]}, set(), item="7")
        with self.assertRaises(ValueError):
            data.tool({"action": "context", "disclosure_ids": ["amd_2023_7_D001"]}, set(), item="7")
        with self.assertRaises(ValueError):
            json.loads('{"action":"search","action":"context"}', object_pairs_hook=alignment_runtime.unique_object)

    def test_interrupted_request_does_not_silently_repeat(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_cli(response=lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(self.output("token_usage.json")["requests_with_unknown_usage"], 1)

    def test_malformed_record_is_corrected_inside_bounded_agent_loop(self):
        def malformed_record(prompt, **kwargs):
            p = json.loads(prompt)
            if "anchors" in p and not p["history"]:
                return CompletionResult('{"action":"propose","matches":["not an object"]}', "test-model", "stop",
                                        {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
            return self.response(prompt, **kwargs)
        self.assertEqual(self.run_cli(response=malformed_record), (0, 3))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        self.assertIn("must be an object", self.output("traces/matching_001.json")[0]["validation_error"])

    def test_revalidation_reuses_responses_without_losing_accounting(self):
        self.assertEqual(self.run_cli(), (0, 2))
        path = self.root / "alignments/amd/2023-2024/manifest.json"
        old = json.loads(path.read_text())
        old["implementation_hashes"]["disclosure_alignment.py"] = "older-implementation"
        write_json(path, old)
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(self.run_cli("--revalidate-cache"), (0, 0))
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        self.assertTrue(list((path.parent / "archives").glob("*/jobs/*.json")))
        self.assertEqual(self.run_cli("--revalidate-cache", "--top-k", "2"), (1, 0))

    def test_verifier_preserves_valid_rows_and_quarantines_other_rows(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        row = {"previous_ids": [before], "current_ids": [after], "explanation": "Same underlying topic.",
               "needs_review": False, "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in (before, after)]}
        # Repeating a valid citation is harmless; unknown citations are not.
        row["evidence"].append(dict(row["evidence"][0]))
        required = {before, after, "amd_2023_7_D002", "amd_2024_7_D002"}
        result = module.validate_verification_response({"action": "finalize", "alignments": [row, "malformed"]}, set(data.records), required, data)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["status"], "ai_verified")
        self.assertEqual(len(result[0]["evidence"]), 2)
        self.assertTrue(all(r["status"] == "needs_review" and "change_type" not in r for r in result[1:]))
        self.assertTrue(all(r["review_reasons"] == ["invalid_or_omitted_verifier_proposal"] for r in result[1:]))

    def test_legacy_change_label_does_not_affect_alignment_validation(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        data.records[after]["content"] += " The revenue amount has changed."
        row = {"previous_ids": [before], "current_ids": [after], "change_type": "Unchanged",
               "explanation": "Both concern the same server revenue topic.", "needs_review": False,
               "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in (before, after)]}
        result = module.validate_alignments({"action": "finalize", "alignments": [row]}, set(data.records), {before, after}, data)
        self.assertEqual(result[0]["status"], "ai_verified")
        self.assertEqual(result[0]["review_reasons"], [])
        self.assertNotIn("change_type", result[0])

    def test_missing_review_metadata_still_requires_review(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        row = {"previous_ids": [before], "current_ids": [after], "explanation": "Same underlying topic.",
               "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in (before, after)]}
        result = module.validate_verification_response({"action": "finalize", "alignments": [row]}, set(data.records), {before, after}, data)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["relationship"], "one_to_one")
        self.assertEqual(result[0]["previous_ids"], [before])
        self.assertEqual(result[0]["current_ids"], [after])
        self.assertTrue(all(r["status"] == "needs_review" for r in result))
        self.assertIn("missing_review_metadata", result[0]["review_reasons"])
        self.assertEqual(result[0]["metadata_repairs"], ["needs_review", "review_reason"])

    def test_full_workflow_exports_supported_unmatched_but_not_invalid_proposals(self):
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            result = json.loads(self.response(prompt, **kwargs).text)
            if "anchors" in payload:
                for row in result["matches"]:
                    row["previous_ids"] = []
                    row["rationale"] = "No counterpart found."
            else:
                for row in result["alignments"]:
                    row.update(explanation="No counterpart found in the supplied evidence and search results.",
                               needs_review=True, review_reason="Absence cannot be proven by retrieval.")
                    if "amd_2024_7_D002" in row["current_ids"]:
                        del row["evidence"]  # Missing citations require a correction, not a no-match export.
            return CompletionResult(json.dumps(result), "test-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_cli(response=response), (0, 6))
        report = self.saved_report()
        self.assertEqual(len(report["alignments"]), 6)
        self.assertEqual(report["counts"], {"unmatched": 5, "needs_review": 1})
        combined = self.output("alignments.json")
        self.assertEqual(combined["alignment_count"], 5)
        self.assertEqual({r["relationship"] for r in combined["alignments"]}, {"previous_only", "current_only"})
        self.assertEqual(combined["alignments"], [r for r in report["alignments"] if r["status"] == "unmatched"])
        review = self.output("needs_review.json")["alignments"]
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0]["current_ids"], ["amd_2024_7_D002"])
        for side, year, expected_count in (("previous", "2023", 3), ("current", "2024", 2)):
            rows = self.unmatched_rows(side)
            self.assertEqual(len(rows), expected_count)
            self.assertEqual(sum(len(row[side + "_ids"]) for row in rows), expected_count)
            self.assertNotIn('"change_type"', json.dumps(rows))
            for row in rows:
                self.assertEqual(next(iter(row)), "match_id")
                self.assertIn(row, report["alignments"])
                self.assertTrue(row["evidence"])
                self.assertEqual(row["status"], "unmatched")
                self.assertEqual(row["review_reasons"], [])
                self.assertIn("absence_not_proven_by_retrieval", row["unmatched_notes"])
                self.assertEqual(row["relationship"], f"{side}_only")
                self.assertEqual(row["unmatched_type"], "removed_disclosure" if side == "previous" else "introduced_disclosure")
        self.assertEqual(self.run_cli(response=response), (0, 0))
        # An incomplete run must clear old exports; later jobs could find a match.
        report["run_complete"] = False
        module.write_alignment_json_reports(self.root / "alignments/amd/2023-2024", report)
        self.assertFalse(self.output("alignments.json")["run_complete"])
        self.assertEqual(self.unmatched_rows(), [])
        self.assertEqual(self.output("needs_review.json")["alignment_count"], 6)

    def test_unmatched_export_requires_unique_assignment_and_complete_evidence(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        unmatched = {"previous_ids": [before], "current_ids": [], "explanation": "No counterpart found.",
                     "needs_review": False, "evidence": [{"disclosure_id": before, "sentence_ids": list(data.records[before]["sentence_map"])[:1]}]}
        matched = {"previous_ids": [before], "current_ids": [after], "explanation": "Same topic.",
                   "needs_review": False, "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in (before, after)]}
        decisions = []
        for row in (unmatched, matched):
            required = set(row["previous_ids"] + row["current_ids"])
            decisions.extend(module.validate_alignments({"action": "finalize", "alignments": [row]}, set(data.records), required, data))
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        output = self.root / "alignments/amd/2023-2024"
        report = module.save_report(output, data, {}, [], [], decisions, runtime, complete=True)
        self.assertEqual(self.unmatched_rows("previous"), [])
        self.assertIn(before, report["coverage"]["conflicting_disclosure_ids"])
        self.assertTrue(all(r["status"] == "needs_review" for r in report["alignments"] if before in r["previous_ids"]))
        # Even a legacy report without conflict flags must not export reused IDs.
        report["coverage"]["conflicting_disclosure_ids"] = []
        for row in report["alignments"]:
            row["review_reasons"] = [flag for flag in row["review_reasons"] if flag != "conflicting_alignment_assignment"]
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.unmatched_rows("previous"), [])
        # A sole row still requires selected original evidence for every member.
        single = next(row for row in report["alignments"] if row["previous_ids"] == [before] and not row["current_ids"])
        single["evidence"] = []
        report["alignments"] = [single]
        module.write_alignment_json_reports(output, report)
        self.assertEqual(self.unmatched_rows("previous"), [])
        self.assertEqual(single["status"], "needs_review")

    def test_unmatched_export_counts_all_disclosures_in_a_group(self):
        data = self.data()
        keys = ["amd_2023_7_D001", "amd_2023_7_D002"]
        data.records[keys[1]]["extraction_status"] = "needs_review"
        row = {"previous_ids": keys, "current_ids": [], "explanation": "Neither topic has a counterpart.",
               "needs_review": False, "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in keys]}
        decisions = module.validate_alignments({"action": "finalize", "alignments": [row]}, set(data.records), set(keys), data)
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        module.save_report(self.root / "alignments/amd/2023-2024", data, {}, [], [], decisions, runtime, complete=True)
        rows = self.unmatched_rows("previous")
        self.assertEqual(len(rows), 1)
        self.assertEqual(sum(len(row["previous_ids"]) for row in rows), 2)
        self.assertEqual(rows[0]["previous_ids"], keys)
        self.assertEqual(rows[0]["status"], "unmatched")
        self.assertEqual(rows[0]["unmatched_type"], "removed_disclosure")
        self.assertIn("input_extraction_needs_review", rows[0]["unmatched_notes"])

    def test_legacy_unmatched_report_refresh_preserves_notes_and_is_idempotent(self):
        data = self.data()
        key = "amd_2023_7_D001"
        raw = {"previous_ids": [key], "current_ids": [], "explanation": "No counterpart found.",
               "evidence": [{"disclosure_id": key, "sentence_ids": list(data.records[key]["sentence_map"])[:1]}]}
        # Missing review metadata is repairable; retain the exact audit, without making it an alignment review.
        decisions = module.validate_verification_response({"action": "finalize", "alignments": [raw]}, {key}, {key}, data)
        original = json.loads(json.dumps(decisions[0]))
        runtime = SimpleNamespace(report=lambda: {"reported_tokens": {"total_tokens": 0}, "requests_with_unknown_usage": 0})
        output = self.root / "alignments/amd/2023-2024"
        report = module.save_report(output, data, {}, [], [], decisions, runtime, complete=True)
        row = next(r for r in report["alignments"] if r["previous_ids"] == [key])
        self.assertEqual(row["status"], "unmatched")
        self.assertEqual(row["review_reasons"], [])
        self.assertEqual(row["unmatched_notes"], original["review_reasons"])
        for field in ("evidence", "explanation", "metadata_repairs", "verifier_review_reason"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(decisions[0], original)
        self.assertEqual(report["counts"], {"unmatched": 1, "needs_review": 5})
        self.assertEqual(len(self.output("needs_review.json")["alignments"]), 5)
        self.assertEqual(len(self.unmatched_rows("previous")), 1)
        saved = json.dumps(report)
        module.write_alignment_reports(output, report, runtime.report())
        self.assertEqual(json.dumps(report), saved)
        # Schema-v2 saved reports receive exactly the same classification without model calls.
        row.update(status="needs_review", review_reasons=row.pop("unmatched_notes"))
        report["schema_version"] = "2"
        module.write_alignment_reports(output, report, runtime.report())
        self.assertEqual(json.dumps(report), saved)

    def test_unmatched_status_is_revoked_if_evidence_or_assignment_becomes_unresolved(self):
        data = self.data()
        key = "amd_2023_7_D001"
        raw = {"previous_ids": [key], "current_ids": [], "explanation": "No counterpart found.", "needs_review": False,
               "evidence": [{"disclosure_id": key, "sentence_ids": list(data.records[key]["sentence_map"])[:1]}]}
        rows = module.validate_alignments({"action": "finalize", "alignments": [raw]}, {key}, {key}, data)
        base = {"run_complete": True, "alignments": rows,
                "coverage": {"pending_disclosure_ids": [], "conflicting_disclosure_ids": []}}
        module.refresh_report_statuses(base)
        self.assertEqual(base["alignments"][0]["status"], "unmatched")
        for cause in ("pending", "conflict", "omission", "unfinished", "missing_evidence"):
            with self.subTest(cause=cause):
                report = json.loads(json.dumps(base))
                row = report["alignments"][0]
                if cause in ("pending", "conflict"):
                    field = "pending_disclosure_ids" if cause == "pending" else "conflicting_disclosure_ids"
                    report["coverage"][field] = [key]
                elif cause == "missing_evidence":
                    row["evidence"] = []
                else:
                    row["review_reasons"] = ["invalid_or_omitted_verifier_proposal" if cause == "omission" else "agent_did_not_finish"]
                module.refresh_report_statuses(report)
                self.assertEqual(row["status"], "needs_review")
                self.assertNotIn("unmatched_notes", row)

    def invalid_citation_response(self, prompt, **kwargs):
        response = self.response(prompt, **kwargs)
        payload, action = json.loads(prompt), json.loads(response.text)
        if "anchors" not in payload and not payload.get("repair_mode"):
            action["alignments"][0]["evidence"][0]["sentence_ids"] = ["invented_sentence"]
        return CompletionResult(json.dumps(action), response.model, response.finish_reason, response.usage)

    def test_targeted_correction_keeps_valid_rows_and_records_exact_error(self):
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("repair_mode"):
                self.assertEqual(set(payload["required_ids"]), {"amd_2023_7_D001", "amd_2024_7_D001"})
                self.assertEqual(len(payload["preserved_proposals"]), 2)
                self.assertEqual({r["disclosure_id"] for r in payload["records"]}, set(payload["required_ids"]))
                issue = payload["validation_errors"][0]["errors"][0]
                self.assertEqual(issue["field"], "evidence[0].sentence_ids")
                self.assertEqual(issue["invalid_value"], ["invented_sentence"])
            return self.invalid_citation_response(prompt, **kwargs)
        self.assertEqual(self.run_cli(response=response), (0, 3))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        events = self.output("validation_errors.json")["jobs"][0]["events"]
        rejected = [e for e in events if e["outcome"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["resolution"], "covered_by_valid_proposal")
        self.assertEqual(self.run_cli(response=response), (0, 0))

    def test_omissions_receive_targeted_correction(self):
        def response(prompt, **kwargs):
            original = self.response(prompt, **kwargs)
            payload, action = json.loads(prompt), json.loads(original.text)
            if "anchors" not in payload and not payload.get("repair_mode"):
                action["alignments"].pop()
            elif payload.get("repair_mode"):
                self.assertEqual(set(payload["required_ids"]), {"amd_2023_7_D003", "amd_2024_7_D003"})
                self.assertEqual({e["errors"][0]["error_type"] for e in payload["validation_errors"]}, {"omitted_disclosure"})
            return CompletionResult(json.dumps(action), original.model, original.finish_reason, original.usage)
        self.assertEqual(self.run_cli(response=response), (0, 3))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})

    def test_partial_verification_survives_budget_pause_and_resumes(self):
        self.assertEqual(self.run_cli("--max-new-requests", "2", response=self.invalid_citation_response), (2, 2))
        partial = self.saved_report()
        self.assertFalse(partial["run_complete"])
        self.assertEqual(partial["counts"], {"needs_review": 2, "ai_verified": 2})
        self.assertEqual(self.run_cli(response=self.invalid_citation_response), (0, 1))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 375)

    def test_partial_verification_survives_turn_limit(self):
        self.assertEqual(self.run_cli("--max-steps", "1", response=self.invalid_citation_response), (0, 2))
        report = self.saved_report()
        self.assertEqual(report["counts"], {"needs_review": 2, "ai_verified": 2})
        pending = [r for r in report["alignments"] if r["status"] == "needs_review"]
        self.assertTrue(all(r["validation_errors"][0]["errors"][0]["invalid_value"] == ["invented_sentence"] for r in pending))
        self.assertEqual(self.unmatched_rows("previous"), [])

    def test_missing_metadata_does_not_bypass_bad_citations(self):
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        row = {"previous_ids": [before], "current_ids": [after], "explanation": "Same topic.",
               "evidence": [{"disclosure_id": k, "sentence_ids": ["invented"]} for k in (before, after)]}
        state = VerificationRepair(data, {before, after}, module.validate_alignments, module.review_row)
        state.receive({"action": "finalize", "alignments": [row]}, set(data.records))
        self.assertEqual(state.accepted, [])
        self.assertEqual(state.pending, {before, after})
        self.assertEqual(state.events[0]["outcome"], "rejected")

    def test_wrong_review_metadata_type_is_not_silently_coerced(self):
        data = self.data()
        keys = ["amd_2023_7_D001", "amd_2024_7_D001"]
        row = {"previous_ids": keys[:1], "current_ids": keys[1:], "explanation": "Same topic.", "needs_review": "false",
               "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in keys]}
        state = VerificationRepair(data, set(keys), module.validate_alignments, module.review_row)
        state.receive({"action": "finalize", "alignments": [row]}, set(data.records))
        self.assertFalse(state.accepted)
        self.assertEqual(state.events[0]["errors"][0]["field"], "needs_review")
        self.assertEqual(state.events[0]["errors"][0]["invalid_value"], "false")

    def test_search_and_context_remain_available_during_correction(self):
        def response(prompt, **kwargs):
            p = json.loads(prompt)
            if p.get("repair_mode") and not p["history"]:
                action = {"action": "search", "queries": [{"year": "2023", "query": "EPYC"}]}
            elif p.get("repair_mode") and len(p["history"]) == 1:
                action = {"action": "context", "disclosure_ids": ["amd_2023_7_D001"]}
            else:
                return self.invalid_citation_response(prompt, **kwargs)
            return CompletionResult(json.dumps(action), "test-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_cli(response=response), (0, 5))
        trace = self.output("traces/verification_001.json")
        self.assertEqual([entry["action"]["action"] for entry in trace], ["finalize", "search", "context", "finalize"])
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})

    def test_repair_errors_are_per_proposal_and_preserved_rows_cannot_be_overwritten(self):
        data = self.data()
        keys = ["amd_2023_7_D001", "amd_2024_7_D001"]
        row = {"previous_ids": keys[:1], "current_ids": keys[1:], "explanation": "Original explanation.", "needs_review": False,
               "evidence": [{"disclosure_id": k, "sentence_ids": list(data.records[k]["sentence_map"])[:1]} for k in keys]}
        state = VerificationRepair(data, set(data.records), module.validate_alignments, module.review_row)
        state.receive({"action": "finalize", "alignments": [row]}, set(data.records))
        row["explanation"] = "Attempt to overwrite a preserved decision."
        row["evidence"] = []
        state.receive({"action": "finalize", "alignments": [row, "bad object"]}, set(data.records))
        self.assertEqual(len(state.accepted), 1)
        self.assertEqual(state.accepted[0]["explanation"], "Original explanation.")
        remaining = state.results()[1:]
        self.assertTrue(all(all(e["errors"][0]["error_type"] == "omitted_disclosure" for e in r["validation_errors"]) for r in remaining))

    def test_saved_run_offline_repair_and_live_resume_keep_separate_usage(self):
        self.assertEqual(self.run_cli("--max-steps", "1", response=self.invalid_citation_response), (0, 2))
        source = self.root / "alignments/amd/2023-2024"
        hashes = {p: digest(p.read_bytes()) for p in source.rglob("*.json")}
        target = self.root / "repaired"
        args = ("--repair-from", str(source), "--output-dir", str(target))
        self.assertEqual(self.run_cli(*args, "--offline-repair"), (2, 0))
        report = module.load_alignment_report(target)
        self.assertEqual(report["counts"], {"needs_review": 2, "ai_verified": 2})
        self.assertEqual(self.run_cli(*args), (0, 1))
        usage = json.loads((target / "token_usage.json").read_text())
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 125)
        self.assertEqual(usage["source_run_reported_tokens"]["total_tokens"], 250)
        self.assertTrue(all(digest(p.read_bytes()) == h for p, h in hashes.items()))
        self.assertEqual(self.run_cli(*args), (0, 0))
        path = source / "matching_proposals.json"
        path.write_text(path.read_text() + " ")
        self.assertEqual(self.run_cli(*args), (1, 0))

    def set_fixture_text(self, year, number, texts):
        raw = self.root / "raw/amd" / year
        pid = f"amd_{year}_7_P{number:03d}"
        sentences = [{"sentence_id": f"{pid}_S{i:03d}", "text": text} for i, text in enumerate(texts, 1)]
        content = " ".join(texts)
        path = raw / f"{year}_chunks.json"
        paragraphs = json.loads(path.read_text())
        paragraphs[number - 1]["text"] = content
        write_json(path, paragraphs)
        path = raw / f"{year}_chunk_sentences.json"
        units = [s for s in json.loads(path.read_text()) if s["chunk_id"] != pid]
        units.extend({"id": s["sentence_id"], "text": s["text"], "chunk_id": pid} for s in sentences)
        write_json(path, units)
        for path in (self.root / "disclosures/amd" / year).glob("*.json"):
            document = json.loads(path.read_text())
            if "input_hashes" not in document:
                continue
            document["input_hashes"] = {str(p): digest(p.read_bytes()) for p in raw.glob("*.json")}
            for row in document.get("disclosures", []):
                if row["disclosure_id"] == f"amd_{year}_7_D{number:03d}":
                    row["content"] = content
                    row["sources"][0].update(original_paragraph=content, selected_text=content, sentences=sentences)
            write_json(path, document)

    def make_exact(self, number=1):
        self.set_fixture_text("2023", number, [f"Policy {number} applies.", "It remains in force."])
        self.set_fixture_text("2024", number, [f"◦ Policy {number} applies.", "It  remains\tin force."])

    def test_exact_normalization_only_bullets_and_whitespace(self):
        self.assertEqual(normalize_text("  •  Revenue\tgrew.\n\n- Costs fell."), "Revenue grew. Costs fell.")
        for changed in ("Revenue grew!", "revenue grew.", "Revenue did not grow.", "Revenue grew. More text."):
            self.assertNotEqual(normalize_text("Revenue grew."), normalize_text(changed))
        for a, b in (("2024 revenue $10.", "2025 revenue $10."), ("$10", "$11"),
                     ("- 10", "10"), ("1. Policy", "Policy"), ("A-B", "AB"), ("A B", "AB")):
            self.assertNotEqual(normalize_text(a), normalize_text(b))

    def test_all_exact_skips_both_agents_and_api_config(self):
        for number in (1, 2, 3):
            self.make_exact(number)
        with patch.object(module, "load_config", side_effect=AssertionError("No API config needed")), \
             patch.object(alignment_runtime, "request_completion", side_effect=AssertionError("No API needed")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(module.main(["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024", "--data-dir", str(self.root)]), 0)
        report = self.saved_report()
        self.assertEqual(report["counts"], {"auto_matched": 3})
        self.assertEqual(report["coverage"]["pending_disclosure_ids"], [])
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 0)
        self.assertEqual(self.output("proposed_groups.json"), [])
        self.assertEqual(self.run_cli(), (0, 0))
        self.assertEqual(self.run_cli("--regroup-only"), (0, 0))
        self.assertEqual(self.saved_report()["counts"], {"auto_matched": 3})

    def test_mixed_exact_route_resume_and_repair(self):
        self.make_exact()
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            if "anchors" in payload:
                self.assertNotIn("amd_2024_7_D001", payload["anchors"])
            else:
                self.assertNotIn("amd_2024_7_D001", payload["required_ids"])
                self.assertNotIn("amd_2023_7_D001", payload["required_ids"])
                self.assertEqual(len(payload["established_exact_links"]), 1)
            return self.response(prompt, **kwargs)
        self.assertEqual(self.run_cli("--max-new-requests", "1", response=response), (2, 1))
        self.assertEqual(self.saved_report()["counts"]["auto_matched"], 1)
        self.assertEqual(self.run_cli(response=response), (0, 1))
        self.assertEqual(self.saved_report()["counts"], {"auto_matched": 1, "ai_verified": 2})
        target = self.root / "repair"
        self.assertEqual(self.run_cli("--repair-from", str(self.root / "alignments/amd/2023-2024"),
                                     "--output-dir", str(target), "--offline-repair"), (0, 0))
        self.assertEqual(module.load_alignment_report(target)["counts"], {"auto_matched": 1, "ai_verified": 2})

    def test_exact_full_content_unique_and_same_item(self):
        self.make_exact()
        data = self.data()
        before, after = "amd_2023_7_D001", "amd_2024_7_D001"
        self.assertEqual(len(exact_matches(data)), 1)
        data.records[after]["summary"] = "A completely different model summary"
        self.assertEqual(len(exact_matches(data)), 1)
        data.records[after]["content"] += " Additional sentence."
        self.assertEqual(exact_matches(data), [])
        data = self.data()
        data.records[after]["item"] = "1A"
        self.assertEqual(exact_matches(data), [])
        for year in ("2023", "2024"):
            data = self.data()
            data.records[f"amd_{year}_7_D002"]["content"] = data.records[before]["content"]
            self.assertEqual(exact_matches(data), [])

    def test_exact_source_warnings_and_proof_are_preserved(self):
        self.make_exact()
        data = self.data()
        key = "amd_2024_7_D001"
        data.records[key]["extraction_status"] = "needs_review"
        rows = exact_matches(data)
        self.assertEqual(rows[0]["review_reasons"], ["input_extraction_needs_review"])
        self.assertEqual(rows[0]["status"], "needs_review")
        self.assertTrue(module.supported_match(rows[0], data))
        rows[0]["exact_match"]["normalized_text_sha256"] = "bad"
        self.assertFalse(module.supported_match(rows[0], data))

    def test_exact_pair_remains_available_for_additional_split(self):
        self.make_exact()
        def response(prompt, **kwargs):
            result = self.response(prompt, **kwargs)
            payload, action = json.loads(prompt), json.loads(result.text)
            if "anchors" in payload:
                self.assertIn("amd_2023_7_D001", {r["disclosure_id"] for r in payload["records"]})
                action["matches"][0]["previous_ids"].append("amd_2023_7_D001")
                return CompletionResult(json.dumps(action), result.model, result.finish_reason, result.usage)
            return result
        self.assertEqual(self.run_cli(response=response), (0, 2))
        report = self.saved_report()
        self.assertEqual(report["counts"].get("needs_review", 0), 0)
        self.assertEqual(report["coverage"]["conflicting_disclosure_ids"], [])
        self.assertIn("amd_2023_7_D001", report["coverage"]["permitted_overlap_disclosure_ids"])
        self.assertTrue(any(r.get("match_method") == "exact_text" for r in report["alignments"]))

    def test_exact_plus_llm_star_preserves_provenance(self):
        self.make_exact()
        data = self.data()
        exact = exact_matches(data)[0]
        payload = {"proposed_groups": [{"previous_ids": exact["previous_ids"], "current_ids": ["amd_2024_7_D002"]}],
                   "records": [data.view(key, full=True) for key in data.records]}
        llm = module.validate_alignments(json.loads(self.response(json.dumps(payload)).text), set(data.records), {"amd_2024_7_D002"}, data)[0]
        merged = module.consolidate_alignments([exact, llm], data)[0]
        self.assertEqual(merged["match_method"], "exact_text_and_llm")
        self.assertEqual(merged["status"], "ai_verified")
        self.assertNotIn("exact_match", merged)
        self.assertEqual(merged["grouping"]["source_alignments"][0]["exact_match"], exact["exact_match"])

    def test_verifier_cannot_declare_established_exact_member_unmatched(self):
        self.make_exact()
        data = self.data()
        exact_ids = {"amd_2023_7_D001", "amd_2024_7_D001"}
        keys = ["amd_2024_7_D001", "amd_2024_7_D002"]
        payload = {"proposed_groups": [{"previous_ids": [], "current_ids": keys}],
                   "records": [data.view(key, full=True) for key in data.records]}
        with self.assertRaisesRegex(ValueError, "already has an established"):
            module.validate_residual_alignments(json.loads(self.response(json.dumps(payload)).text), set(data.records),
                                                {keys[1]}, data, exact_ids)

    def test_legacy_scope_cannot_resume_or_import_old_cross_item_decisions(self):
        self.assertEqual(self.run_cli(), (0, 2))
        folder = self.root / "alignments/amd/2023-2024"
        path = folder / "manifest.json"
        manifest = json.loads(path.read_text())
        del manifest["comparison_scope"]
        write_json(path, manifest)
        hashes = {p: digest(p.read_bytes()) for p in folder.rglob("*.json")}
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(self.run_cli("--revalidate-cache"), (1, 0))
        self.assertEqual(self.run_cli("--repair-from", str(folder), "--output-dir", str(self.root / "repaired"), "--offline-repair"), (1, 0))
        self.assertFalse((self.root / "repaired").exists())
        self.assertTrue(all(digest(p.read_bytes()) == value for p, value in hashes.items()))

    def parallel_items(self):
        # Distinct Items create independent real matching and verification jobs.
        for year in ("2023", "2024"):
            self.set_fixture_item(year, 2, "3")
            self.set_fixture_item(year, 3, "8")

    def test_parallel_matching_and_verification_overlap_and_remain_deterministic(self):
        self.parallel_items()
        barriers = {role: threading.Barrier(3) for role in ("matching", "verification")}
        lock, completed_matching = threading.Lock(), set()
        output = self.root / "alignments/amd/2023-2024"
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            role = "matching" if "anchors" in payload else "verification"
            if role == "verification":
                with lock:
                    self.assertEqual(len(completed_matching), 3)
                self.assertEqual(len(list((output / "jobs").glob("matching_*.json"))), 3)
            barriers[role].wait(timeout=5)
            result = self.response(prompt, **kwargs)
            if role == "matching":
                with lock:
                    completed_matching.update(payload["anchors"])
            return result
        flags = ("--workers", "3", "--batch-size", "1", "--request-interval", "0")
        self.assertEqual(self.run_cli(*flags, response=response), (0, 6))
        parallel = self.saved_report()
        self.assertTrue(parallel["run_complete"])
        self.assertEqual(parallel["counts"], {"ai_verified": 3})
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 750)
        self.assertEqual(self.output("token_usage.json")["in_flight_budget_reserve"]["attempt_count"], 0)
        with sqlite3.connect(output / "graph/checkpoints.sqlite") as connection:
            self.assertEqual(len(connection.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()), 6)
        # Changing only execution concurrency does not invalidate cached work.
        self.assertEqual(self.run_cli("--workers", "1", "--batch-size", "1"), (0, 0))
        serial_output = self.root / "serial"
        self.assertEqual(self.run_cli("--batch-size", "1", "--output-dir", str(serial_output)), (0, 6))
        self.assertEqual(parallel["alignments"], module.load_alignment_report(serial_output)["alignments"])
        self.assertEqual(self.output("matching_proposals.json"), json.loads((serial_output / "matching_proposals.json").read_text()))

    def test_parallel_pause_drains_saved_jobs_and_resumes_without_duplicate_calls(self):
        self.parallel_items()
        barrier = threading.Barrier(2)
        def response(prompt, **kwargs):
            barrier.wait(timeout=5)
            return self.response(prompt, **kwargs)
        flags = ("--workers", "3", "--batch-size", "1", "--request-interval", "0")
        self.assertEqual(self.run_cli(*flags, "--max-new-requests", "2", response=response), (2, 2))
        self.assertFalse(self.saved_report()["run_complete"])
        output = self.root / "alignments/amd/2023-2024"
        saved = list((output / "jobs").glob("matching_*.json"))
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(json.loads(path.read_text())["result"] for path in saved))
        self.assertEqual(self.output("token_usage.json")["requests_with_unknown_usage"], 0)
        self.assertEqual(self.run_cli(*flags), (0, 4))
        self.assertEqual(self.saved_report()["counts"], {"ai_verified": 3})
        self.assertEqual(len(list((output / "requests").glob("*.json"))), 6)

    def test_parallel_failed_request_retains_other_admitted_work_for_retry(self):
        self.parallel_items()
        barrier = threading.Barrier(3)
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            barrier.wait(timeout=5)
            if payload["anchors"] == ["amd_2024_7_D001"]:
                raise LLMError("Temporary server failure.", status_code=503, retryable=True)
            return self.response(prompt, **kwargs)
        flags = ("--workers", "3", "--batch-size", "1", "--request-interval", "0")
        self.assertEqual(self.run_cli(*flags, response=response), (1, 3))
        self.assertFalse(self.saved_report()["run_complete"])
        output = self.root / "alignments/amd/2023-2024"
        self.assertEqual(len(list((output / "jobs").glob("matching_*.json"))), 2)
        self.assertEqual(self.output("token_usage.json")["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(self.output("token_usage.json")["requests_with_unknown_usage"], 1)
        self.assertEqual(self.run_cli(*flags, "--retry-failed"), (0, 4))
        self.assertTrue(self.saved_report()["run_complete"])
        usage = self.output("token_usage.json")
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 750)
        self.assertEqual(usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"], 0)
        self.assertEqual(len(usage["unknown_usage_budget_reserve"]["attempts"]), 1)

    def concurrency_runtime(self, **limits):
        args = SimpleNamespace(workers=3, request_interval=0, rate_limit_cooldown=0, retry_failed=False,
            max_new_requests=None, max_requests=100, max_total_tokens=10000, max_steps=4,
            max_tokens=100, max_prompt_chars=10000, timeout=5)
        for key, value in limits.items():
            setattr(args, key, value)
        return alignment_runtime.Runtime(self.root / "runtime", LLMConfig("https://example.test/v1", "fake-secret", "default"), args)

    def test_parallel_token_budget_reserves_active_calls_without_unknown_usage(self):
        runtime = self.concurrency_runtime(max_total_tokens=105)
        entered, release = threading.Event(), threading.Event()
        def response(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return CompletionResult("{}", "test-model", "stop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        with patch.object(alignment_runtime, "request_completion", side_effect=response) as api, redirect_stdout(io.StringIO()), \
             ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(runtime.call, "matching", "one", "abc", "xy")
            try:
                self.assertTrue(entered.wait(timeout=5))
                report = runtime.report()
                self.assertEqual(report["budget_accounted_tokens"], 105)
                self.assertEqual(report["in_flight_budget_reserve"], {"attempt_count": 1, "estimated_tokens": 105})
                self.assertEqual(report["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"], 0)
                others = [executor.submit(runtime.call, "matching", str(i), "different", "xy") for i in (2, 3)]
                for future in others:
                    with self.assertRaisesRegex(alignment_runtime.RunLimit, "token budget"):
                        future.result(timeout=5)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(runtime.report()["budget_accounted_tokens"], 2)
        self.assertEqual(runtime.report()["in_flight_budget_reserve"]["attempt_count"], 0)

    def test_parallel_total_request_cap_is_atomic(self):
        runtime = self.concurrency_runtime(max_requests=1)
        entered, release = threading.Event(), threading.Event()
        def response(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return CompletionResult("{}", "test-model", "stop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        with patch.object(alignment_runtime, "request_completion", side_effect=response) as api, redirect_stdout(io.StringIO()), \
             ThreadPoolExecutor(max_workers=3) as executor:
            first = executor.submit(runtime.call, "matching", "one", "first", "system")
            try:
                self.assertTrue(entered.wait(timeout=5))
                others = [executor.submit(runtime.call, "matching", str(i), str(i), "system") for i in (2, 3)]
                for future in others:
                    with self.assertRaisesRegex(alignment_runtime.RunLimit, "request limit"):
                        future.result(timeout=5)
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(runtime.new_requests, 1)

    def test_parallel_identical_prompts_reuse_one_paid_response(self):
        runtime = self.concurrency_runtime()
        barrier = threading.Barrier(3)
        entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
        original_wait, wait_count = runtime.condition.wait, 0
        def wait(*args, **kwargs):
            nonlocal wait_count
            wait_count += 1  # Called while holding the shared condition lock.
            if wait_count == 2:
                waiting.set()
            return original_wait(*args, **kwargs)
        def response(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return CompletionResult("{}", "test-model", "stop", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        def call(number):
            barrier.wait(timeout=5)
            return runtime.call("matching", str(number), "same prompt", "same system")
        with patch.object(alignment_runtime, "request_completion", side_effect=response) as api, \
             patch.object(runtime.condition, "wait", side_effect=wait), \
             redirect_stdout(io.StringIO()), ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(call, number) for number in range(3)]
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(waiting.wait(timeout=5))
                self.assertEqual(api.call_count, 1)
            finally:
                release.set()
            results = [future.result(timeout=5) for future in futures]
        self.assertEqual(api.call_count, 1)
        self.assertEqual(results, [results[0]] * 3)
        self.assertEqual(len(runtime.requests), 1)
        self.assertEqual(runtime.report()["budget_accounted_tokens"], 2)


if __name__ == "__main__":
    unittest.main()
