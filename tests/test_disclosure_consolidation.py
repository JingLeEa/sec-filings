import io
import json
import threading
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from sec_disclosure.llm import disclosure_consolidation as consolidation
from sec_disclosure.llm import disclosures
from sec_disclosure.llm.client import CompletionResult, LLMError
from sec_disclosure.llm.config import LLMConfig


TAXONOMY = "Financial & Capital Resources"


def completion(body, finish="stop"):
    return CompletionResult(json.dumps(body), "test-model", finish,
                            {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})


def merge(ids):
    return {"candidate_ids": ids, "topic": "Revenue and its causes",
            "summary": "Revenue rose because of higher demand.", "taxonomy": TAXONOMY,
            "reason": "The later explanation supplies the cause of the earlier revenue result."}


class ConsolidationTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.layout = [("7", "Results", [text]) for text in (
            "Revenue rose by $10 million in 2024.", "Debt matures in 2030.",
            "Operating costs increased in 2024.", "Cash dividends were paid in 2024.",
            "The revenue increase was caused by higher customer demand.")]
        self.write_inputs()

    def write_inputs(self, years=("2024",)):
        # Make each whole paragraph exceed half the minimum batching target.
        self.layout = [(item, section, [text if len(text) >= 650 else text + " Supporting context." * 35
                                       for text in texts]) for item, section, texts in self.layout]
        for year in years:
            folder = self.root / "raw/amd" / year
            folder.mkdir(parents=True, exist_ok=True)
            paragraphs, sentences = [], []
            for number, (item, section, texts) in enumerate(self.layout, 1):
                paragraph = {"id": f"amd_{year}_{item}_P{number:03d}", "company": "amd", "year": year,
                             "item": item, "item_title": section, "section_path": [section],
                             "source": f"https://example.test/{year}.htm", "text": " ".join(texts)}
                paragraphs.append(paragraph)
                sentences.extend({**paragraph, "id": f"{paragraph['id']}_S{index:03d}",
                                  "chunk_id": paragraph["id"], "sentence_index": index, "text": text}
                                 for index, text in enumerate(texts, 1))
            (folder / f"{year}_chunks.json").write_text(json.dumps(paragraphs))
            (folder / f"{year}_chunk_sentences.json").write_text(json.dumps(sentences))

    def response(self, prompt, **kwargs):
        payload, _ = json.JSONDecoder().raw_decode(prompt)
        if payload.get("task") == "consolidate_subsection":
            return completion({"merges": [], "relationships": []})
        if payload.get("task") == "check_disclosure_boundary":
            return completion({"groups": [{"candidate_ids": [candidate["candidate_id"]],
                                           "resolved": True, "reason": "Keep this independent topic."}
                                          for candidate in payload["candidates"]]})
        ids = [number for row in payload["paragraphs"] for number, _ in row["sentences"]]
        return completion({"disclosures": [{"topic": "Reported fact", "summary": "The filing reports this fact.",
                                            "taxonomy": TAXONOMY, "unit_ids": ids, "review_reason": ""}],
                           "excluded": []})

    def run_cli(self, *extra, responder=None, years=None):
        output = io.StringIO()
        year_options = ["--years", *years] if years else ["--year", "2024"]
        with patch.object(disclosures, "load_config", return_value=LLMConfig(
                "https://example.test/v1", "fake-key", "default")), \
             patch.object(disclosures, "request_completion", side_effect=responder or self.response) as api, \
             redirect_stdout(output), redirect_stderr(output):
            code = disclosures.main(["--ticker", "amd", *year_options, "--data-dir", str(self.root),
                                     "--batch-chars", "1000", "--request-interval", "0",
                                     "--rate-limit-cooldown", "0", "--response-retry-backoff", "0", *extra])
        self.last_output = output.getvalue()
        return code, api.call_count

    def read(self, name, year="2024"):
        return json.loads((self.root / "disclosures/amd" / year / name).read_text())

    def records(self):
        return self.read("disclosures.json")["disclosures"] + self.read("review_candidates.json")["disclosures"]

    def test_batch_one_and_five_merge_with_exact_evidence_and_lineage(self):
        prompts = []

        def respond(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") != "consolidate_subsection":
                return self.response(prompt, **kwargs)
            prompts.append(payload)
            self.assertEqual(kwargs["system_prompt"], consolidation.CONSOLIDATION_SYSTEM_PROMPT)
            self.assertEqual(kwargs["timeout"], 180)
            return completion({"merges": [merge(["batch_001_d001", "batch_005_d001"])], "relationships": []})

        self.assertEqual(self.run_cli(responder=respond), (0, 10))
        self.assertEqual(prompts[0]["batch_ids"], [f"batch_{i:03d}" for i in range(1, 6)])
        self.assertEqual(prompts[0]["selected_evidence"], [[i, row[2][0]] for i, row in enumerate(self.layout, 1)])
        records = self.records()
        self.assertEqual(len(records), 4)
        combined = next(d for d in records if d["verification"]["source_unit_count"] == 2)
        self.assertEqual(combined["content"], self.layout[0][2][0] + "\n\n" + self.layout[4][2][0])
        self.assertEqual(combined["verification"]["extraction_batch_ids"], ["batch_001", "batch_005"])
        self.assertEqual(combined["verification"]["source_proposal_ids"], ["batch_001_d001", "batch_005_d001"])
        self.assertEqual([s["text"] for d in records for p in d["sources"] for s in p["sentences"]].count(
            self.layout[0][2][0]), 1)
        self.assertCountEqual([s["text"] for d in records for p in d["sources"] for s in p["sentences"]],
                              [row[2][0] for row in self.layout])
        self.assertEqual(combined["verification"]["consolidation_checks"][0]["status"], "completed")
        self.assertEqual(self.read("token_usage.json")["by_stage"]["consolidation"]["requests"], 1)
        self.assertEqual(self.run_cli(responder=respond), (0, 0))
        self.assertEqual(self.read("token_usage.json")["reported_tokens"]["total_tokens"], 1250)

    def test_relationships_resolve_merged_endpoints_to_final_disclosure_ids(self):
        def respond(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") != "consolidate_subsection":
                return self.response(prompt, **kwargs)
            return completion({"merges": [merge(["batch_001_d001", "batch_005_d001"])],
                               "relationships": [
                                   {"candidate_ids": ["batch_003_d001", "batch_005_d001"],
                                    "reason": "Demand growth also increased operating costs."},
                                   {"candidate_ids": ["batch_001_d001", "batch_005_d001"],
                                    "reason": "These fragments were merged."}]})

        self.assertEqual(self.run_cli(responder=respond), (0, 10))
        links = self.read("disclosure_relationships.json")["relationships"]
        self.assertEqual(len(links), 1)  # A self-link after a merge is redundant.
        linked = [d for d in self.records() if d["related_disclosures"]]
        self.assertEqual(len(linked), 2)
        self.assertCountEqual(links[0]["disclosure_ids"], [d["disclosure_id"] for d in linked])
        for record in linked:
            self.assertEqual(record["related_disclosures"][0]["disclosure_ids"],
                             [d["disclosure_id"] for d in linked if d is not record])
            self.assertEqual(record["related_disclosures"][0]["reason"], links[0]["reason"])
        self.assertEqual(len(self.records()), 4)

    def test_unrelated_candidates_remain_separate_after_complete_pass(self):
        self.assertEqual(self.run_cli(), (0, 10))
        self.assertEqual(len(self.records()), 5)
        self.assertEqual(self.read("disclosure_relationships.json")["relationships"], [])
        self.assertTrue(self.read("token_usage.json")["run_complete"])

    def test_boundary_merge_can_join_a_distant_fragment_and_load_for_alignment(self):
        from sec_disclosure.agents.alignment_data import AlignmentData

        self.write_inputs(years=("2023", "2024"))

        def respond(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") == "check_disclosure_boundary" and payload["boundary_id"] == "boundary_001":
                ids = [c["candidate_id"] for c in payload["candidates"]]
                self.assertCountEqual(ids, ["batch_001_d001", "batch_002_d001"])
                return completion({"groups": [{**merge(ids), "resolved": True}]})
            if payload.get("task") == "consolidate_subsection":
                first = next(c for c in payload["candidates"] if c["unit_ids"] == [1, 2])
                self.assertTrue(first["candidate_id"].startswith("merged_"))
                return completion({"merges": [merge([first["candidate_id"], "batch_005_d001"])],
                                   "relationships": [{"candidate_ids": ["batch_003_d001", first["candidate_id"]],
                                                      "reason": "These results have related causes."}]})
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=respond, years=("2023", "2024")), (0, 20))
        data = AlignmentData(self.root, "amd", ("2023", "2024"))
        self.assertEqual(len(data.records), 6)
        for year in ("2023", "2024"):
            record = next(d for d in data.records.values() if d["fiscal_year"] == year
                          and d["verification"]["source_unit_count"] == 3)
            self.assertEqual(record["verification"]["extraction_batch_ids"], ["batch_001", "batch_002", "batch_005"])
            self.assertEqual(record["verification"]["source_proposal_ids"],
                             ["batch_001_d001", "batch_002_d001", "batch_005_d001"])
            self.assertEqual(record["content"], "\n\n".join(self.layout[i][2][0] for i in (0, 1, 4)))
            self.assertTrue(record["related_disclosures"])

    def test_subsections_items_and_noncontiguous_equal_headings_stay_separate(self):
        self.layout = [(item, section, [f"Independent fact {i}."]) for i, (item, section) in enumerate(
            [("7", "Results"), ("7", "Results"), ("7", "Results > Gaming"), ("7", "Results > Gaming"),
             ("7", "Results"), ("7", "Results"), ("8", "Results"), ("8", "Results")], 1)]
        self.write_inputs()
        scopes = []

        def respond(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") == "consolidate_subsection":
                scopes.append(payload)
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=respond), (0, 16))
        self.assertEqual([p["batch_ids"] for p in scopes],
                         [[f"batch_{i:03d}" for i in pair] for pair in ((1, 2), (3, 4), (5, 6), (7, 8))])
        for payload in scopes:
            self.assertEqual(len(payload["candidates"]), 2)
            self.assertEqual(len(payload["selected_evidence"]), 2)

    def test_failed_response_is_atomic_and_can_resume_without_reextracting(self):
        def invalid(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") == "consolidate_subsection":
                return completion({"merges": [merge(["batch_001_d001", "batch_005_d001"])],
                                   "relationships": [{"candidate_ids": ["batch_002_d001", "other_subsection"],
                                                      "reason": "An invalid out-of-scope link."}]})
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=invalid), (1, 10))
        self.assertEqual(len(self.records()), 5)
        self.assertEqual(self.read("disclosure_relationships.json")["relationships"], [])
        self.assertFalse(self.read("token_usage.json")["run_complete"])
        for record in self.records():
            self.assertIn("consolidation_failed", record["verification"]["review_reasons"])
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(self.run_cli("--retry-failed"), (0, 1))
        self.assertEqual(self.read("token_usage.json")["by_stage"]["consolidation"]["requests"], 2)
        self.assertEqual(self.read("token_usage.json")["reported_tokens"]["total_tokens"], 1375)

    def test_truncation_and_malformed_json_retry_with_saved_usage(self):
        attempts = 0

        def respond(prompt, **kwargs):
            nonlocal attempts
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            if payload.get("task") != "consolidate_subsection":
                return self.response(prompt, **kwargs)
            attempts += 1
            if attempts == 1:
                return CompletionResult("not JSON", "test-model", "stop",
                                        {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
            self.assertIn("Correct the previous response", prompt)
            return completion({"merges": [], "relationships": []}, "length" if attempts == 2 else "stop")

        self.assertEqual(self.run_cli("--fault-tolerant", "--retry-backoff", "0", responder=respond), (0, 12))
        self.assertEqual(attempts, 3)
        report = self.read("token_usage.json")
        self.assertEqual(report["by_stage"]["consolidation"]["requests"], 3)
        self.assertEqual(report["reported_tokens"]["total_tokens"], 1500)

    def test_exhausted_retries_continue_other_subsections(self):
        self.layout += [("7", "Other", ["Another independent fact."]), ("7", "Other", ["Its context."])]
        self.write_inputs()
        attempts = Counter()

        def respond(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            if payload.get("task") == "consolidate_subsection":
                attempts[payload["section"]] += 1
                if payload["section"] == "Results":
                    raise LLMError("Cannot connect to provider.", retryable=True)
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "2", "--retry-backoff", "0",
                                      responder=respond), (1, 16))
        self.assertEqual(attempts, {"Results": 3, "Other": 1})
        report = self.read("token_usage.json")
        self.assertEqual(report["requests_with_unknown_usage"], 3)
        self.assertEqual(len(report["failed_consolidations"]), 1)
        self.assertEqual(self.run_cli("--fault-tolerant", "--retry-backoff", "0"), (0, 1))

    def test_pending_consolidation_blocks_alignment_until_resume(self):
        self.write_inputs(years=("2023", "2024"))
        with patch.object(disclosures, "align_years", return_value=0) as align:
            self.assertEqual(self.run_cli("--align", "--max-requests", "9", years=("2023", "2024")), (2, 18))
            align.assert_not_called()
            for year in ("2023", "2024"):
                self.assertEqual(self.read("token_usage.json", year)["pending_consolidations"], ["consolidation_001"])
            self.assertEqual(self.run_cli("--align", years=("2023", "2024")), (0, 2))
            align.assert_called_once()

    def test_oversized_input_is_not_truncated_and_cap_can_increase_on_resume(self):
        self.assertEqual(self.run_cli("--consolidation-max-prompt-chars", "1"), (1, 9))
        audit = self.read("consolidation.json")["subsections"][0]
        self.assertIn("not truncated", audit["error"])
        self.assertEqual(len(audit["input"]["selected_evidence"]), 5)
        self.assertEqual(len(self.records()), 5)
        self.assertEqual(self.run_cli(), (0, 1))
        self.assertTrue(self.read("token_usage.json")["run_complete"])

    def test_version_three_cache_is_archived_and_paid_requests_reused(self):
        self.assertEqual(self.run_cli("--max-requests", "9"), (2, 9))
        folder = self.root / "disclosures/amd/2024"
        legacy = self.read("manifest.json")
        legacy = {key: value for key, value in legacy.items() if not key.startswith("consolidation_")}
        legacy["version"] = "3"
        (folder / "manifest.json").write_text(json.dumps(legacy))
        saved = {path: path.read_bytes() for path in (folder / "requests").glob("*.json")}
        self.assertEqual(self.run_cli(), (0, 1))
        self.assertEqual(self.read("manifest.pre_consolidation.json"), legacy)
        self.assertEqual(self.read("manifest.json")["version"], "4")
        for path, contents in saved.items():
            self.assertEqual(path.read_bytes(), contents)
        self.assertEqual(self.run_cli(), (0, 0))

    def test_legacy_cache_with_changed_model_is_rejected_without_writes(self):
        self.assertEqual(self.run_cli("--max-requests", "9"), (2, 9))
        folder = self.root / "disclosures/amd/2024"
        legacy = {key: value for key, value in self.read("manifest.json").items()
                  if not key.startswith("consolidation_")}
        legacy.update(version="3", requested_model="a-different-model")
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps(legacy))
        before = manifest.read_bytes()
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertEqual(manifest.read_bytes(), before)
        self.assertFalse((folder / "manifest.pre_consolidation.json").exists())

    def test_stale_consolidation_response_requires_another_attempt(self):
        self.assertEqual(self.run_cli(), (0, 10))
        path = self.root / "disclosures/amd/2024/requests/consolidation_001_attempt_001.json"
        cached = json.loads(path.read_text())
        cached["input_hash"] = "different-input"
        path.write_text(json.dumps(cached))
        self.assertEqual(self.run_cli(), (1, 0))
        self.assertIn("differs", self.read("token_usage.json")["failed_consolidations"][0]["error"])
        self.assertEqual(self.run_cli("--retry-failed"), (0, 1))

    def test_merging_preserves_unresolved_boundaries_and_other_review_concerns(self):
        self.layout[0][2].append("See the following table for revenue details.")
        self.write_inputs()

        def respond(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") == "consolidate_subsection":
                return completion({"merges": [merge(["batch_001_d001", "batch_005_d001"])], "relationships": []})
            response = self.response(prompt, **kwargs)
            body = json.loads(response.text)
            if payload.get("task") == "check_disclosure_boundary":
                for group in body["groups"]:
                    group.update(resolved=False, reason="Missing context requires human review.")
            else:
                body["disclosures"][0]["review_reason"] = "The company did not provide the missing amount."
            return completion(body)

        self.assertEqual(self.run_cli(responder=respond), (0, 10))
        combined = next(d for d in self.records() if d["verification"]["source_unit_count"] == 3)
        self.assertCountEqual(combined["verification"]["review_reasons"],
                              ["boundary_context_unresolved", "possible_missing_table_context", "model_requested_review"])
        self.assertIn("missing amount", combined["verification"]["model_review_reason"])
        self.assertEqual(len(combined["verification"]["boundary_checks"]), 2)

    def test_independent_subsections_share_extraction_workers_and_api_cap(self):
        self.layout = [("7", section, [f"Fact {number} in {section}."])
                       for section in ("A", "B", "C", "D") for number in (1, 2)]
        self.write_inputs()
        barrier = threading.Barrier(2, timeout=5)
        lock = threading.Lock()
        active = peak = 0
        started = 0

        def respond(prompt, **kwargs):
            nonlocal active, peak, started
            payload = json.loads(prompt)
            if payload.get("task") != "consolidate_subsection":
                return self.response(prompt, **kwargs)
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                return self.response(prompt, **kwargs)
            finally:
                with lock:
                    active -= 1

        self.assertEqual(self.run_cli("--batch-workers", "4", "--max-concurrent-requests", "2",
                                      responder=respond), (0, 16))
        self.assertEqual(started, 4)
        self.assertEqual(peak, 2)


class ConsolidationValidationTests(TestCase):
    def test_invalid_decisions_are_rejected(self):
        good = {"merges": [merge(["a", "b"])], "relationships": []}
        cases = []
        for ids in (["a"], ["a", "a"], ["a", "outside"], ["a", 3]):
            cases.append({"merges": [merge(ids)], "relationships": []})
        cases.append({"merges": [merge(["a", "b"]), merge(["b", "c"])], "relationships": []})
        for field, value in (("taxonomy", "unknown"), ("summary", ""), ("reason", "")):
            changed = deepcopy(good)
            changed["merges"][0][field] = value
            cases.append(changed)
        cases += [{"merges": [], "relationships": [{"candidate_ids": ["a", "b"], "reason": "related"}] * 2},
                  {"merges": []}, {"merges": {}, "relationships": []},
                  {**good, "extra": "unexpected"}]
        for body in cases:
            with self.subTest(body=body), self.assertRaises(ValueError):
                consolidation.parse_consolidation_response(json.dumps(body), {"a": {}, "b": {}, "c": {}})
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            consolidation.parse_consolidation_response('{"merges":[],"merges":[],"relationships":[]}', {})

    def test_relationship_does_not_require_a_merge(self):
        body = {"merges": [], "relationships": [{"candidate_ids": ["a", "b"], "reason": "Related independent risks."}]}
        self.assertEqual(consolidation.parse_consolidation_response(json.dumps(body), {"a": {}, "b": {}}), body)
