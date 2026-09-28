import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.llm import disclosures as module
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.llm.config import LLMConfig


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input_dir = self.root / "raw/amd/2024"
        self.input_dir.mkdir(parents=True)
        self.paragraphs, self.sentences = [], []
        self.add_paragraph("7", ["Revenue rose in 2024.", "Higher demand drove growth."])
        self.add_paragraph("7", ["Operating income rose.", "Higher costs offset part of the increase."])
        self.add_paragraph("8", ["Debt matures in 2030.", "Interest is fixed."])
        self.write_inputs()

    def add_paragraph(self, item, texts, section="Results"):
        number = sum(p["item"] == item for p in self.paragraphs) + 1
        paragraph = {"id": f"amd_2024_{item}_P{number:03d}", "company": "amd", "year": "2024",
                     "item": item, "item_title": section, "section_path": [section],
                     "source": "https://example.test/filing.htm", "source_block_index": len(self.paragraphs),
                     "text": " ".join(texts)}
        self.paragraphs.append(paragraph)
        for i, text in enumerate(texts, 1):
            self.sentences.append({**paragraph, "id": f"{paragraph['id']}_S{i:03d}",
                                   "chunk_id": paragraph["id"], "sentence_index": i, "text": text})

    def write_inputs(self):
        (self.input_dir / "2024_chunks.json").write_text(json.dumps(self.paragraphs))
        (self.input_dir / "2024_chunk_sentences.json").write_text(json.dumps(self.sentences))

    def filing(self):
        return module.load_filing(self.input_dir / "2024_chunks.json",
                                  self.input_dir / "2024_chunk_sentences.json", "amd", "2024")

    def proposal(self, ids, taxonomy="Financial & Capital Resources"):
        return {"summary": "Results improved in 2024.", "taxonomy": taxonomy,
                "unit_ids": ids, "review_reason": ""}

    def response(self, prompt, **kwargs):
        document, _ = json.JSONDecoder().raw_decode(prompt)
        ids = [number for row in document["paragraphs"] for number, _ in row["sentences"]]
        return CompletionResult(json.dumps({"disclosures": [self.proposal(ids)], "excluded": []}),
                                "provider-model", "stop",
                                {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125,
                                 "completion_tokens_details": {"reasoning_tokens": 5},
                                 "prompt_tokens_details": {"cached_tokens": 20}})

    def run_command(self, *extra, responder=None):
        with patch.object(module, "load_config", return_value=LLMConfig("https://example.test/v1", "fake-key", "default")), \
             patch.object(module, "request_completion", side_effect=responder or self.response) as request, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = module.main(["--ticker", "amd", "--year", "2024", "--data-dir", str(self.root), *extra])
        return status, request.call_count

    def read_output(self, name):
        return json.loads((self.root / "disclosures/amd/2024" / name).read_text())

    def test_item_boundaries_and_exact_full_paragraph_evidence(self):
        filing = self.filing()
        batches = module.make_batches(filing)
        self.assertEqual([b["item"] for b in batches], ["7", "8"])
        result = module.assemble_disclosure(filing, self.proposal([1, 2, 3, 4]), batches[0])
        self.assertEqual(result["content"], "\n\n".join(p["text"] for p in self.paragraphs[:2]))
        self.assertEqual([source["selection"] for source in result["sources"]], ["paragraph", "paragraph"])
        self.assertEqual(result["verification"]["status"], "source_validated")
        with self.assertRaisesRegex(ValueError, "Item boundaries"):
            module.assemble_disclosure(filing, self.proposal([1, 5]), batches[0])

    def test_mixed_paragraph_and_sentence_selection_keeps_original_context(self):
        filing = self.filing()
        result = module.assemble_disclosure(filing, self.proposal([1, 2, 3]), module.make_batches(filing)[0])
        self.assertEqual(result["sources"][1]["selection"], "sentences")
        self.assertEqual(result["sources"][1]["original_paragraph"], self.paragraphs[1]["text"])
        self.assertNotIn("Higher costs", result["content"])
        self.assertEqual(result["verification"]["source_unit_count"], 3)

    def test_single_unit_candidate_is_retained_for_review(self):
        filing = self.filing()
        result = module.assemble_disclosure(filing, self.proposal([1]), module.make_batches(filing)[0])
        self.assertEqual(result["verification"]["status"], "needs_review")
        self.assertIn("fewer_than_two_source_units", result["verification"]["review_reasons"])

    def test_quarantines_invented_duplicate_ids_and_unknown_taxonomy(self):
        invalid = [
            {"disclosures": [self.proposal([1, 99])], "excluded": []},
            {"disclosures": [self.proposal([1, 1, 2])], "excluded": []},
            {"disclosures": [self.proposal([1, 2], "invented-label")], "excluded": []},
            {"disclosures": [self.proposal([1, 2])], "excluded": [{"unit_ids": [1], "reason": "duplicate"}]},
        ]
        for response in invalid:
            with self.subTest(response=response):
                result = module.parse_proposals(json.dumps(response), {1, 2})
                self.assertTrue(result["invalid"])
                self.assertEqual(result["disclosures"], [])
                self.assertEqual(result["unassigned"], [1, 2])

    def test_omitted_units_are_retained_for_review_without_another_request(self):
        def omit(prompt, **kwargs):
            response = self.response(prompt, **kwargs)
            document = json.loads(response.text)
            document["disclosures"][0]["unit_ids"].pop(0)
            return CompletionResult(json.dumps(document), response.model, "stop", response.usage)
        self.assertEqual(self.run_command(responder=omit), (0, 2))
        report = self.read_output("token_usage.json")
        self.assertEqual(report["coverage"]["unassigned_source_units_for_review"], 2)
        self.assertEqual(report["coverage"]["unprocessed_sentence_ids"], [])
        self.assertEqual(report["review_candidates"], 2)
        self.assertEqual(self.run_command(), (0, 0))

    def test_omitted_empty_exclusions_and_empty_exclusion_record_are_safe(self):
        for value in ({"disclosures": [self.proposal([1, 2])]},
                      {"disclosures": [self.proposal([1, 2])], "excluded": [{"unit_ids": [], "reason": "none"}]}):
            result = module.parse_proposals(json.dumps(value), {1, 2})
            self.assertEqual(result["unassigned"], [])
            self.assertEqual(result["invalid"], [])

    def test_input_mismatch_rejected_before_api_request(self):
        self.sentences[0]["text"] = "An invented sentence."
        self.write_inputs()
        status, calls = self.run_command()
        self.assertEqual((status, calls), (1, 0))

    def test_empty_headers_are_excluded_with_reason(self):
        self.add_paragraph("8", [""])
        self.write_inputs()
        filing = self.filing()
        self.assertEqual(filing.filtered[0]["reason"], "empty_header_or_sentence")
        self.assertEqual(sum(len(row["sentences"]) for row in filing.rows), 6)

    def test_split_sections_have_boundary_review_flags(self):
        for paragraph in self.paragraphs[:2]:
            paragraph["text"] = " ".join(["Long source sentence " * 30, "More context."])
        for unit in self.sentences[:4]:
            unit["text"] = "Long source sentence " * 30 if unit["sentence_index"] == 1 else "More context."
        self.write_inputs()
        filing = self.filing()
        batches = module.make_batches(filing, max_chars=1000)
        self.assertTrue(batches[0]["boundary_paragraphs"])
        result = module.assemble_disclosure(filing, self.proposal([1, 2]), batches[0])
        self.assertIn("section_continues_across_batch_boundary", result["verification"]["review_reasons"])

    def test_resume_uses_cached_responses_and_counts_tokens_once(self):
        self.assertEqual(self.run_command("--max-requests", "1"), (2, 1))
        self.assertEqual(self.run_command(), (0, 1))
        self.assertEqual(self.run_command(), (0, 0))
        report = self.read_output("token_usage.json")
        self.assertEqual(report["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(report["by_item"]["8"]["total_tokens"], 125)
        self.assertEqual(report["included_token_details"]["reasoning_completion_tokens"], 10)
        self.assertEqual(report["coverage"]["unprocessed_sentence_ids"], [])
        self.assertEqual(report["source_validated_disclosures"], 2)
        self.assertEqual(report["returned_models"], ["provider-model"])

    def test_failed_json_tokens_are_saved_and_retry_is_explicit(self):
        def malformed(prompt, **kwargs):
            return CompletionResult("not JSON", "provider-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110})
        self.assertEqual(self.run_command(responder=malformed), (1, 1))
        self.assertEqual(self.read_output("token_usage.json")["reported_tokens"]["total_tokens"], 110)
        self.assertEqual(self.run_command(), (1, 0))
        self.assertEqual(self.run_command("--retry-failed"), (0, 2))
        report = self.read_output("token_usage.json")
        self.assertEqual(report["attempted_requests"], 3)
        self.assertEqual(report["reported_tokens"]["total_tokens"], 360)

    def test_missing_usage_is_unknown_not_zero_cost(self):
        report = module.usage_report([{"item": "7", "result": {"model": "default", "usage": None}}], 1)
        self.assertFalse(report["usage_complete"])
        self.assertEqual(report["requests_with_unknown_usage"], 1)
        self.assertIsNone(report["monetary_cost"])
        self.assertIsNone(report["included_token_details"]["cached_prompt_tokens"])
        self.assertIsNone(report["included_token_details"]["reasoning_completion_tokens"])

    def test_changed_input_cannot_reuse_old_request_cache(self):
        self.assertEqual(self.run_command(), (0, 2))
        self.paragraphs[0]["source_block_index"] = 99
        self.write_inputs()
        self.assertEqual(self.run_command(), (1, 0))

    def test_dry_run_never_calls_api_or_writes_outputs(self):
        self.assertEqual(self.run_command("--dry-run"), (0, 0))
        self.assertFalse((self.root / "disclosures").exists())

    def test_model_exclusions_are_explicitly_unverified_and_preserve_context(self):
        def exclude(prompt, **kwargs):
            document, _ = json.JSONDecoder().raw_decode(prompt)
            ids = [number for row in document["paragraphs"] for number, _ in row["sentences"]]
            response = {"disclosures": [], "excluded": [{"unit_ids": ids, "reason": "Model chose to exclude."}]}
            return CompletionResult(json.dumps(response), "provider-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_command(responder=exclude), (0, 2))
        exclusions = self.read_output("excluded_sources.json")["excluded"]
        self.assertEqual(len(exclusions), 6)
        self.assertTrue(all(row["verification_status"] == "needs_review" for row in exclusions))
        self.assertEqual(exclusions[0]["original_paragraph"], self.paragraphs[0]["text"])
        self.assertEqual(self.read_output("token_usage.json")["coverage"]["unverified_model_exclusions"], 6)


if __name__ == "__main__":
    unittest.main()
