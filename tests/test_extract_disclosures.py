import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.llm import disclosures as module
from sec_disclosure.llm import disclosure_boundaries as boundary
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
        return {"topic": "Operating results", "summary": "Results improved in 2024.", "taxonomy": taxonomy,
                "unit_ids": ids, "review_reason": ""}

    def response(self, prompt, **kwargs):
        document, _ = json.JSONDecoder().raw_decode(prompt)
        if document.get("task") == "consolidate_subsection":
            return CompletionResult('{"merges":[],"relationships":[]}', "provider-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
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

    def test_batches_and_disclosures_cannot_cross_subsections_of_same_item(self):
        self.add_paragraph("7", ["Gaming revenue declined.", "Lower demand reduced revenue."], section="Results > Gaming")
        self.add_paragraph("7", ["Embedded revenue increased.", "More orders drove growth."], section="Results > Embedded")
        self.write_inputs()
        filing = self.filing()
        batches = module.make_batches(filing)
        for batch in batches:
            self.assertEqual({(r["item"], r["section"]) for r in batch["rows"]}, {(batch["item"], batch["section"])})
            self.assertFalse(batch["boundary_paragraphs"])
            payload = json.loads(module.make_prompt(filing, batch))
            self.assertEqual(payload["section"], batch["section"])
            self.assertEqual(payload["extraction_scope"], "single_subsection")
        gaming = next(b for b in batches if b["section"] == "Results > Gaming")
        with self.assertRaisesRegex(ValueError, "single subsection"):
            module.assemble_disclosure(filing, self.proposal([7, 9]), gaming)
        with self.assertRaisesRegex(ValueError, "single subsection"):
            module.assemble_disclosure(filing, self.proposal([9, 10]), gaming)
        mixed = {**gaming, "rows": gaming["rows"] + batches[-1]["rows"]}
        with self.assertRaisesRegex(ValueError, "exactly one Item and subsection"):
            module.make_prompt(filing, mixed)
        self.assertEqual(self.run_command(), (0, 4))
        saved = self.read_output("disclosures.json")
        self.assertEqual(saved["extraction_scope"], "single_subsection")
        self.assertTrue(all(len(d["sections"]) == 1 and "single_subsection" in d["verification"]["checks"]
                            for d in saved["disclosures"]))

    def test_one_subsection_can_produce_multiple_focused_disclosures(self):
        def split_topics(prompt, **kwargs):
            document = json.loads(prompt)
            records = []
            for i, row in enumerate(document["paragraphs"], 1):
                proposal = self.proposal([sid for sid, _ in row["sentences"]])
                proposal["topic"] = f"Topic {i}"
                records.append(proposal)
            return CompletionResult(json.dumps({"disclosures": records, "excluded": []}), "provider-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        self.assertEqual(self.run_command(responder=split_topics), (0, 2))
        item = [d for d in self.read_output("disclosures.json")["disclosures"] if d["item"] == "7"]
        self.assertEqual(len(item), 2)
        self.assertEqual([d["topic"] for d in item], ["Topic 1", "Topic 2"])
        self.assertEqual([len(d["sources"]) for d in item], [1, 1])

    def test_missing_topic_is_quarantined_instead_of_silently_accepting_broad_summary(self):
        missing = self.proposal([1, 2])
        missing.pop("topic")
        parsed = module.parse_proposals(json.dumps({"disclosures": [missing, self.proposal([3, 4])]}), {1, 2, 3, 4})
        self.assertEqual(len(parsed["disclosures"]), 1)
        self.assertEqual(parsed["unassigned"], [1, 2])
        self.assertIn("topic", parsed["invalid"][0]["reason"])

    def test_sentence_subsection_must_match_parent_before_api_call(self):
        self.sentences[0]["item_title"] = "Different subsection"
        self.write_inputs()
        self.assertEqual(self.run_command(), (1, 0))

    def test_mixed_paragraph_and_sentence_selection_keeps_original_context(self):
        filing = self.filing()
        result = module.assemble_disclosure(filing, self.proposal([1, 2, 3]), module.make_batches(filing)[0])
        self.assertEqual(result["sources"][1]["selection"], "sentences")
        self.assertEqual(result["sources"][1]["original_paragraph"], self.paragraphs[1]["text"])
        self.assertNotIn("Higher costs", result["content"])
        self.assertEqual(result["verification"]["source_unit_count"], 3)

    def test_single_unit_disclosure_passes_source_validation(self):
        filing = self.filing()
        result = module.assemble_disclosure(filing, self.proposal([1]), module.make_batches(filing)[0])
        self.assertEqual(result["verification"]["status"], "source_validated")
        self.assertEqual(result["verification"]["review_reasons"], [])
        self.assertEqual(result["verification"]["source_unit_count"], 1)
        self.assertEqual(result["content"], self.sentences[0]["text"])

    def test_single_sentence_keeps_other_extraction_warnings(self):
        filing = self.filing()
        batch = module.make_batches(filing)[0]
        batch["boundary_paragraphs"] = [self.paragraphs[0]["id"]]
        proposal = self.proposal([1])
        proposal["review_reason"] = "The relevant table is missing."
        result = module.assemble_disclosure(filing, proposal, batch)
        self.assertEqual(result["verification"]["status"], "needs_review")
        self.assertEqual(set(result["verification"]["review_reasons"]),
                         {"boundary_check_pending", "model_requested_review"})

    def test_saved_length_warning_is_waived_without_mutating_original(self):
        original = {"status": "needs_review", "source_unit_count": 1,
                    "review_reasons": ["fewer_than_two_source_units"], "model_review_reason": ""}
        current = module.apply_source_unit_policy(original)
        self.assertEqual(current["status"], "source_validated")
        self.assertEqual(current["review_reasons"], [])
        self.assertEqual(current["ignored_review_reasons"], ["fewer_than_two_source_units"])
        self.assertEqual(original["status"], "needs_review")
        self.assertEqual(original["review_reasons"], ["fewer_than_two_source_units"])
        self.assertEqual(module.apply_source_unit_policy(current), current)
        for extra in ({"review_reasons": ["fewer_than_two_source_units", "possible_missing_table_context"]},
                      {"model_review_reason": "A relevant detail may be missing."},
                      {"source_unit_count": 0}):
            self.assertEqual(module.apply_source_unit_policy(original | extra)["status"], "needs_review")

    def test_quarantines_invented_duplicate_ids_and_unknown_taxonomy(self):
        invalid = [
            {"disclosures": [self.proposal([])], "excluded": []},
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
        self.assertEqual(report["review_candidates"], 0)
        self.assertEqual(report["source_validated_disclosures"], 2)
        disclosures = self.read_output("disclosures.json")["disclosures"]
        for disclosure in disclosures:
            self.assertEqual(disclosure["verification"]["review_reasons"], [])
            self.assertIn("unassigned_context_in_source_paragraph", disclosure["verification"]["ignored_review_reasons"])
        unassigned = self.read_output("unassigned_sources.json")["sources"]
        self.assertEqual({row["sentence_id"] for row in unassigned}, {self.sentences[0]["id"], self.sentences[4]["id"]})
        self.assertTrue(all(row["text"] in row["original_paragraph"] for row in unassigned))
        selected = {s["sentence_id"] for d in disclosures for source in d["sources"] for s in source["sentences"]}
        self.assertFalse(selected & {row["sentence_id"] for row in unassigned})
        self.assertEqual(self.read_output("excluded_sources.json")["excluded"], [])
        self.assertEqual(self.run_command(), (0, 0))

    def test_unassigned_neighbors_do_not_clear_other_extraction_concerns(self):
        for other in ("spans_multiple_sections", "section_continues_across_batch_boundary",
                      "possible_missing_table_context", "model_requested_review"):
            original = {"status": "needs_review", "source_unit_count": 2,
                        "review_reasons": ["unassigned_context_in_source_paragraph", other],
                        "model_review_reason": ""}
            current = module.apply_source_unit_policy(original)
            self.assertEqual(current["review_reasons"], [other])
            self.assertEqual(current["status"], "needs_review")
            self.assertEqual(original["review_reasons"], ["unassigned_context_in_source_paragraph", other])

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

    def test_colon_filter_checks_original_paragraph_sentence_count_and_ending(self):
        intro = "The following table summarizes changes in Goodwill:"
        cases = [
            ([intro], True),
            (["Our obligations are as follows:\t\n"], True),
            (["The following table summarizes our $2 billion purchase commitments:"], True),
            (["See the following table."], False),
            (["The following table summarizes Goodwill"], False),
            (["Revenue: $10 million."], False),
            ([intro, "No goodwill impairment was recorded."], False),
            (["No goodwill impairment was recorded.", intro], False),
            ([intro, "Shares of common stock outstanding were as follows:"], False),
            # Navigation is excluded separately; this still has two original sentences.
            (["See accompanying notes to the Consolidated Financial Statements.", intro], False),
        ]
        expected, retained = set(), set()
        for texts, excluded in cases:
            self.add_paragraph("8", texts)
            (expected if excluded else retained).add(self.paragraphs[-1]["id"])
        self.write_inputs()
        filing = self.filing()
        exclusions = [r for r in filing.filtered if r["reason"] == module.TABLE_INTRODUCTION_REASON]
        self.assertEqual({r["paragraph_id"] for r in exclusions}, expected)
        self.assertTrue(retained <= {r["paragraph_id"] for r in filing.rows})
        for record in exclusions:
            self.assertEqual(record["text"], filing.paragraphs[record["paragraph_id"]]["text"])
            self.assertEqual(record["original_paragraph"], record["text"])

    def test_colon_paragraphs_excluded_before_model_with_exact_audit_and_coverage(self):
        intro = "The following table summarizes changes in Goodwill:"
        fact = "During 2024 the Company concluded there was no goodwill impairment."
        self.add_paragraph("8", [intro, fact])
        mixed_parent = self.paragraphs[-1]
        self.add_paragraph("8", ["Shares of common stock outstanding were as follows:"], section="Table only")
        self.write_inputs()
        filtered_ids = {self.sentences[8]["id"]}

        def check_prompt(prompt, **kwargs):
            document = json.loads(prompt)
            for field in ("paragraphs", "context_before", "context_after"):
                self.assertTrue(all(number != 9 for row in document[field] for number, _ in row["sentences"]))
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_command(responder=check_prompt), (0, 2))
        records = (self.read_output("disclosures.json")["disclosures"]
                   + self.read_output("review_candidates.json")["disclosures"])
        sources = [s for d in records for s in d["sources"]]
        selected_ids = {u["sentence_id"] for s in sources for u in s["sentences"]}
        self.assertFalse(filtered_ids & selected_ids)
        self.assertTrue({self.sentences[6]["id"], self.sentences[7]["id"]} <= selected_ids)
        selected = next(s for s in sources if s["paragraph_id"] == mixed_parent["id"])
        self.assertEqual(selected["selected_text"], mixed_parent["text"])
        self.assertEqual(selected["original_paragraph"], mixed_parent["text"])
        self.assertEqual(selected["selection"], "paragraph")
        excluded = self.read_output("excluded_sources.json")["excluded"]
        self.assertEqual({x["sentence_id"] for x in excluded}, filtered_ids)
        self.assertTrue(all(x["reason"] == module.TABLE_INTRODUCTION_REASON and x["method"] == "deterministic_filter" for x in excluded))
        self.assertTrue(all(x["text"] in x["original_paragraph"] for x in excluded))
        reviews = self.read_output("review_candidates.json")["disclosures"]
        self.assertEqual(len(reviews), 1)
        self.assertIn("possible_missing_table_context", reviews[0]["verification"]["review_reasons"])
        coverage = self.read_output("token_usage.json")["coverage"]
        self.assertEqual(coverage["input_sentence_records"], 9)
        self.assertEqual(coverage["disclosure_source_units"], 8)
        self.assertEqual(coverage["excluded_source_units"], 1)
        self.assertEqual(coverage["table_introduction_source_units_excluded"], 1)
        self.assertEqual(coverage["unverified_model_exclusions"], 0)
        self.assertEqual(coverage["unassigned_source_units_for_review"], 0)
        self.assertEqual(coverage["unprocessed_sentence_ids"], [])
        self.assertEqual(self.run_command(), (0, 0))

    def test_table_reference_and_fact_inside_one_unit_are_kept_with_existing_warnings(self):
        text = "The following table summarizes Goodwill: During 2024 there was no goodwill impairment."
        self.add_paragraph("8", [text], section="Goodwill")
        self.write_inputs()
        self.assertEqual(self.run_command(), (0, 3))
        row = self.read_output("review_candidates.json")["disclosures"][0]
        self.assertEqual(row["content"], text)
        self.assertEqual(row["verification"]["source_unit_count"], 1)
        self.assertIn("possible_missing_table_context", row["verification"]["review_reasons"])
        self.assertEqual(self.read_output("excluded_sources.json")["excluded"], [])

    def test_post_extraction_filter_excludes_intro_selected_from_longer_paragraph(self):
        intro = "Total future purchase commitments were as follows:  \n"
        fact = "The Company works with suppliers on the timing of payments and deliveries."
        self.add_paragraph("8", [intro, fact], section="Commitments")
        mixed_parent = self.paragraphs[-1]
        intro_id, fact_id = (s["id"] for s in self.sentences[-2:])
        self.add_paragraph("8", ["Our obligations are as follows:"], section="Commitments")
        self.write_inputs()

        def split_intro(prompt, **kwargs):
            document = json.loads(prompt)
            if document["section"] != "Commitments":
                return self.response(prompt, **kwargs)
            selections = [self.proposal([n]) for row in document["paragraphs"] for n, _ in row["sentences"]]
            selections[0]["review_reason"] = "Table values are absent."
            return CompletionResult(json.dumps({"disclosures": selections, "excluded": []}),
                                    "provider-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

        self.assertEqual(self.run_command(responder=split_intro), (0, 3))
        saved = self.read_output("disclosures.json")["disclosures"]
        self.assertEqual(self.read_output("review_candidates.json")["disclosures"], [])
        selected_ids = {s["sentence_id"] for d in saved for p in d["sources"] for s in p["sentences"]}
        self.assertNotIn(intro_id, selected_ids)
        self.assertIn(fact_id, selected_ids)
        kept = next(d for d in saved if d["section"] == "Commitments")
        self.assertEqual(kept["content"], fact)
        self.assertEqual(kept["disclosure_id"], "amd_2024_8_D002")
        self.assertEqual(kept["sources"][0]["original_paragraph"], mixed_parent["text"])
        exclusions = self.read_output("excluded_sources.json")["excluded"]
        post = [r for r in exclusions if r["reason"] == module.POST_EXTRACTION_REASON]
        self.assertEqual(len(post), 1)
        self.assertEqual(post[0]["sentence_id"], intro_id)
        self.assertEqual(post[0]["text"], intro)
        self.assertEqual(post[0]["original_paragraph"], mixed_parent["text"])
        self.assertEqual(post[0]["method"], "deterministic_post_extraction_filter")
        self.assertEqual(post[0]["extraction_batch_ids"], ["batch_003"])
        self.assertEqual(post[0]["source_proposal_ids"], ["batch_003_d001"])
        coverage = self.read_output("token_usage.json")["coverage"]
        self.assertEqual(coverage["input_sentence_records"], 9)
        self.assertEqual(coverage["disclosure_source_units"], 7)
        self.assertEqual(coverage["excluded_source_units"], 2)
        self.assertEqual(coverage["table_introduction_source_units_excluded"], 1)
        self.assertEqual(coverage["post_extraction_colon_disclosures_excluded"], 1)
        self.assertEqual(coverage["unverified_model_exclusions"], 0)
        self.assertEqual(coverage["unassigned_source_units_for_review"], 0)
        self.assertEqual(coverage["unprocessed_sentence_ids"], [])
        self.assertEqual(self.run_command(responder=split_intro), (0, 0))
        self.assertEqual(self.read_output("excluded_sources.json")["excluded"], exclusions)

    def test_post_extraction_filter_uses_selected_evidence_not_summary_or_parent_count(self):
        self.add_paragraph("8", ["No goodwill impairment was recorded.", "The following table summarizes Goodwill:"],
                           section="Two sentences")
        self.add_paragraph("8", ["See the following table."], section="One sentence with period")
        self.add_paragraph("8", ["Commitments:", "Suppliers coordinate delivery schedules."], section="No table keywords")
        self.write_inputs()

        def respond(prompt, **kwargs):
            document = json.loads(prompt)
            ids = [n for row in document["paragraphs"] for n, _ in row["sentences"]]
            proposals = ([self.proposal([n]) for n in ids] if document["section"] == "No table keywords"
                         else [self.proposal(ids)])
            for p in proposals:
                p["summary"] = "Model summary ends with a colon:"
            return CompletionResult(json.dumps({"disclosures": proposals, "excluded": []}),
                                    "provider-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

        self.assertEqual(self.run_command(responder=respond), (0, 5))
        saved = (self.read_output("disclosures.json")["disclosures"]
                 + self.read_output("review_candidates.json")["disclosures"])
        self.assertTrue({"Two sentences", "One sentence with period", "No table keywords"} <= {d["section"] for d in saved})
        self.assertEqual(next(d for d in saved if d["section"] == "Two sentences")["verification"]["source_unit_count"], 2)
        exclusions = self.read_output("excluded_sources.json")["excluded"]
        self.assertEqual(len(exclusions), 1)
        self.assertEqual(exclusions[0]["text"], "Commitments:")
        self.assertEqual(exclusions[0]["reason"], module.POST_EXTRACTION_REASON)

    def test_all_single_sentence_colon_paragraphs_need_no_grouping_requests(self):
        self.paragraphs, self.sentences = [], []
        self.add_paragraph("8", ["The following table summarizes Goodwill:"])
        self.add_paragraph("8", ["Shares of common stock outstanding were as follows:"])
        self.write_inputs()
        self.assertEqual(self.run_command(), (0, 0))
        usage = self.read_output("token_usage.json")
        self.assertTrue(usage["run_complete"])
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 0)
        self.assertEqual(usage["coverage"]["disclosure_source_units"], 0)
        self.assertEqual(usage["coverage"]["table_introduction_source_units_excluded"], 2)
        self.assertEqual(usage["coverage"]["unprocessed_sentence_ids"], [])
        self.assertEqual(self.read_output("disclosures.json")["disclosures"], [])
        self.assertEqual(self.read_output("review_candidates.json")["disclosures"], [])

    def test_source_filter_policy_and_implementation_are_manifested(self):
        self.assertEqual(self.run_command(), (0, 2))
        manifest = self.read_output("manifest.json")
        self.assertEqual(manifest["table_introduction_policy"], module.TABLE_INTRODUCTION_POLICY)
        self.assertEqual(manifest["post_extraction_filter_policy"], module.POST_EXTRACTION_FILTER_POLICY)
        self.assertTrue(manifest["source_filter_hash"])
        self.assertEqual(self.read_output("excluded_sources.json")["table_introduction_policy"], module.TABLE_INTRODUCTION_POLICY)
        self.assertEqual(self.read_output("excluded_sources.json")["post_extraction_filter_policy"], module.POST_EXTRACTION_FILTER_POLICY)
        manifest["source_filter_hash"] = "different-policy-implementation"
        module.write_json(self.root / "disclosures/amd/2024/manifest.json", manifest)
        self.assertEqual(self.run_command(), (1, 0))

    def split_fixture(self):
        for paragraph in self.paragraphs[:2]:
            paragraph["text"] = " ".join(["Long source sentence " * 30, "More context."])
        for unit in self.sentences[:4]:
            unit["text"] = "Long source sentence " * 30 if unit["sentence_index"] == 1 else "More context."
        self.write_inputs()

    def test_split_sections_wait_for_boundary_check(self):
        self.split_fixture()
        filing = self.filing()
        batches = module.make_batches(filing, max_chars=1000)
        self.assertTrue(batches[0]["boundary_paragraphs"])
        result = module.assemble_disclosure(filing, self.proposal([1, 2]), batches[0])
        self.assertIn("boundary_check_pending", result["verification"]["review_reasons"])

    def boundary_response(self, prompt, *, merge=False, unresolved=False, **kwargs):
        document, _ = json.JSONDecoder().raw_decode(prompt)
        if document.get("task") != "check_disclosure_boundary":
            return self.response(prompt, **kwargs)
        candidates = document["candidates"]
        if merge:
            groups = [{"candidate_ids": [c["candidate_id"] for c in candidates], "resolved": True,
                       "reason": "The introduction and explanation describe the same result.",
                       **{k: candidates[0][k] for k in ("topic", "summary", "taxonomy")}}]
        else:
            groups = [{"candidate_ids": [c["candidate_id"]], "resolved": not unresolved,
                       "reason": "Missing antecedent beyond supplied context." if unresolved else "This is a complete independent topic."}
                      for c in candidates]
        return CompletionResult(json.dumps({"groups": groups}), "provider-model", "stop",
                                {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

    def test_shared_context_has_one_owner_and_never_crosses_sections(self):
        self.split_fixture()
        filing = self.filing()
        batches = module.make_batches(filing, 1000)
        self.assertEqual(len(batches), 3)
        self.assertEqual(batches[0]["context_after"], batches[1]["rows"])
        self.assertEqual(batches[1]["context_before"], batches[0]["rows"])
        self.assertEqual(batches[1]["context_after"], [])
        self.assertEqual(batches[2]["context_before"], [])
        owned = [i for b in batches for r in b["rows"] for i, _ in r["sentences"]]
        self.assertEqual(sorted(owned), list(filing.units))
        self.assertEqual(len(owned), len(set(owned)))
        prompt = json.loads(module.make_prompt(filing, batches[0]))
        context_id = prompt["context_after"][0]["sentences"][0][0]
        parsed = module.parse_proposals(json.dumps({"disclosures": [self.proposal([context_id])]}), {1, 2})
        self.assertTrue(parsed["invalid"])
        mixed = {**batches[0], "context_after": batches[2]["rows"]}
        with self.assertRaisesRegex(ValueError, "one Item and subsection"):
            module.make_prompt(filing, mixed)
        # A repeated heading after another subsection is not shared context.
        self.add_paragraph("7", ["A later noncontiguous Results subsection."])
        self.write_inputs()
        self.assertEqual(len(boundary.boundary_specs(module.make_batches(self.filing(), 1000))), 1)

    def test_shared_context_is_bounded_and_reports_when_limited(self):
        self.split_fixture()
        with patch.object(boundary, "CONTEXT_CHARS_PER_SIDE", 350):
            batches = module.make_batches(self.filing(), 1000)
        self.assertTrue(batches[0]["context_limited"])
        for batch in batches:
            for key in ("context_before", "context_after"):
                self.assertLessEqual(len(json.dumps(batch[key], ensure_ascii=False, separators=(",", ":"))), 350)

    def test_boundary_check_resume_does_not_clear_pending_or_double_charge(self):
        self.split_fixture()
        options = ("--batch-chars", "1000")
        self.assertEqual(self.run_command(*options, "--max-requests", "3", responder=self.boundary_response), (2, 3))
        report = self.read_output("token_usage.json")
        self.assertFalse(report["run_complete"])
        self.assertEqual(report["pending_boundary_checks"], ["boundary_001"])
        self.assertEqual(report["review_candidates"], 2)
        self.assertEqual(self.run_command(*options, responder=self.boundary_response), (0, 2))
        self.assertEqual(self.run_command(*options, responder=self.boundary_response), (0, 0))
        report = self.read_output("token_usage.json")
        self.assertEqual(report["reported_tokens"]["total_tokens"], 625)
        self.assertEqual(report["by_stage"]["boundary_check"]["requests"], 1)
        self.assertEqual(report["by_stage"]["consolidation"]["requests"], 1)
        self.assertEqual(report["review_candidates"], 0)
        disclosures = self.read_output("disclosures.json")["disclosures"]
        self.assertEqual(len(disclosures), 3)
        source_ids = [s["sentence_id"] for d in disclosures for p in d["sources"] for s in p["sentences"]]
        self.assertCountEqual(source_ids, [s["id"] for s in self.sentences])
        self.assertEqual(disclosures[0]["verification"]["boundary_checks"][0]["status"], "resolved")
        self.assertTrue(self.read_output("boundary_checks.json")["boundaries"][0]["input_hash"])

    def test_boundary_merge_preserves_exact_evidence_and_other_warnings(self):
        self.split_fixture()
        mixed_unit = "See the following table. Revenue increased by $10 million."
        self.paragraphs[0]["text"] = self.paragraphs[0]["text"].replace("More context.", mixed_unit)
        self.sentences[1]["text"] = mixed_unit
        self.write_inputs()

        def merge_with_warning(prompt, **kwargs):
            response = self.boundary_response(prompt, merge=True, **kwargs)
            payload = json.loads(response.text)
            if "disclosures" in payload:
                payload["disclosures"][0]["review_reason"] = "Table values were not extracted."
            return CompletionResult(json.dumps(payload), response.model, response.finish_reason, response.usage)

        self.assertEqual(self.run_command("--batch-chars", "1000", responder=merge_with_warning), (0, 4))
        records = self.read_output("review_candidates.json")["disclosures"]
        merged = next(d for d in records if d["item"] == "7")
        self.assertEqual(merged["verification"]["source_unit_count"], 4)
        self.assertEqual(merged["content"], "\n\n".join(p["text"] for p in self.paragraphs[:2]))
        self.assertEqual(set(merged["verification"]["review_reasons"]), {"possible_missing_table_context", "model_requested_review"})
        self.assertEqual(merged["verification"]["extraction_batch_ids"], ["batch_001", "batch_002"])
        self.assertEqual(self.read_output("token_usage.json")["coverage"]["disclosure_source_units"], 6)

    def test_post_extraction_filter_runs_after_boundary_merge_and_replays_on_resume(self):
        self.split_fixture()
        intro = "The following table summarizes commitments:"
        self.paragraphs[0]["text"] = self.paragraphs[0]["text"].replace("More context.", intro)
        self.sentences[1]["text"] = intro
        self.write_inputs()

        def split_then_merge(prompt, **kwargs):
            document = json.loads(prompt)
            if document.get("task") == "check_disclosure_boundary":
                # The export filter must not remove the candidate from the
                # internal boundary evidence before it can join useful facts.
                self.assertTrue(any(c["unit_ids"] == [2] for c in document["candidates"]))
                return self.boundary_response(prompt, merge=True, **kwargs)
            ids = [n for row in document["paragraphs"] for n, _ in row["sentences"]]
            if ids != [1, 2]:
                return self.response(prompt, **kwargs)
            return CompletionResult(json.dumps({"disclosures": [self.proposal([n]) for n in ids], "excluded": []}),
                                    "provider-model", "stop", {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

        options = ("--batch-chars", "1000")
        self.assertEqual(self.run_command(*options, "--max-requests", "3", responder=split_then_merge), (2, 3))
        self.assertEqual(self.read_output("token_usage.json")["coverage"]["post_extraction_colon_disclosures_excluded"], 1)
        self.assertEqual(self.run_command(*options, responder=split_then_merge), (0, 1))
        self.assertEqual(self.read_output("excluded_sources.json")["excluded"], [])
        reviews = self.read_output("review_candidates.json")["disclosures"]
        merged = next(d for d in reviews if d["item"] == "7")
        self.assertEqual(merged["verification"]["source_unit_count"], 4)
        self.assertIn(intro, merged["content"])
        self.assertEqual(self.read_output("token_usage.json")["coverage"]["post_extraction_colon_disclosures_excluded"], 0)
        self.assertEqual(self.run_command(*options, responder=split_then_merge), (0, 0))

    def test_unresolved_boundary_keeps_candidates_with_precise_reason(self):
        self.split_fixture()
        responder = lambda prompt, **kwargs: self.boundary_response(prompt, unresolved=True, **kwargs)
        self.assertEqual(self.run_command("--batch-chars", "1000", responder=responder), (0, 5))
        report = self.read_output("token_usage.json")
        self.assertTrue(report["run_complete"])
        self.assertEqual(report["unresolved_boundary_groups"], 2)
        records = self.read_output("review_candidates.json")["disclosures"]
        self.assertEqual(len(records), 2)
        for record in records:
            verification = record["verification"]
            self.assertEqual(verification["review_reasons"], ["boundary_context_unresolved"])
            self.assertIn("Missing antecedent", verification["boundary_checks"][0]["reason"])

    def test_boundary_response_requires_complete_nonduplicated_candidate_coverage(self):
        candidates = {"a": {"proposal": self.proposal([1])}, "b": {"proposal": self.proposal([2])}}
        valid = {"groups": [{"candidate_ids": [key], "resolved": True, "reason": "Complete topic."} for key in candidates]}
        self.assertEqual(len(boundary.parse_boundary_response(json.dumps(valid), candidates)), 2)
        variants = [
            {"groups": valid["groups"][:1]},
            {"groups": valid["groups"] + valid["groups"][:1]},
            {"groups": [{**valid["groups"][0], "candidate_ids": ["invented"]}, valid["groups"][1]]},
            {"groups": [{**valid["groups"][0], "resolved": "true"}, valid["groups"][1]]},
            {"groups": [{**valid["groups"][0], "reason": ""}, valid["groups"][1]]},
            {"groups": [{**valid["groups"][0], "candidate_ids": ["a", "b"]}]},  # Missing merge metadata.
            {"groups": [{**valid["groups"][0], "summary": "Invented revision."}, valid["groups"][1]]},
            {"groups": [{**valid["groups"][0], "unit_ids": [99]}, valid["groups"][1]]},
        ]
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ValueError):
                boundary.parse_boundary_response(json.dumps(value), candidates)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            boundary.parse_boundary_response('{"groups":[],"groups":[]}', candidates)

    def test_invalid_boundary_is_atomic_and_retry_usage_is_counted(self):
        self.split_fixture()

        def omit_boundary(prompt, **kwargs):
            document, _ = json.JSONDecoder().raw_decode(prompt)
            response = self.boundary_response(prompt, **kwargs)
            if document.get("task") == "check_disclosure_boundary":
                return CompletionResult('{"groups":[]}', response.model, "stop", response.usage)
            return response

        options = ("--batch-chars", "1000")
        self.assertEqual(self.run_command(*options, responder=omit_boundary), (1, 4))
        report = self.read_output("token_usage.json")
        self.assertFalse(report["run_complete"])
        self.assertIn("exactly once", report["failed_boundary_checks"][0]["error"])
        self.assertEqual(report["coverage"]["disclosure_source_units"], 6)
        self.assertEqual(report["review_candidates"], 2)
        self.assertEqual(self.run_command(*options, responder=self.boundary_response), (1, 0))
        self.assertEqual(self.run_command(*options, "--retry-failed", responder=self.boundary_response), (0, 2))
        report = self.read_output("token_usage.json")
        self.assertEqual(report["reported_tokens"]["total_tokens"], 750)
        self.assertEqual(report["by_stage"]["boundary_check"]["requests"], 2)

    def test_truncated_boundary_response_is_not_accepted_even_if_json_is_valid(self):
        self.split_fixture()

        def truncated(prompt, **kwargs):
            payload = json.loads(prompt)
            response = self.boundary_response(prompt, **kwargs)
            if payload.get("task") == "check_disclosure_boundary":
                return CompletionResult(response.text, response.model, "length", response.usage)
            return response

        self.assertEqual(self.run_command("--batch-chars", "1000", responder=truncated), (1, 4))
        report = self.read_output("token_usage.json")
        self.assertFalse(report["run_complete"])
        self.assertEqual(report["review_candidates"], 2)
        self.assertEqual(report["reported_tokens"]["total_tokens"], 500)
        self.assertIn("length", report["failed_boundary_checks"][0]["error"])

    def test_boundary_api_error_preserves_unknown_usage_and_exact_failure(self):
        self.split_fixture()

        def unavailable(prompt, **kwargs):
            if json.loads(prompt).get("task") == "check_disclosure_boundary":
                raise module.LLMError("Boundary provider unavailable.")
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_command("--batch-chars", "1000", responder=unavailable), (1, 4))
        report = self.read_output("token_usage.json")
        self.assertFalse(report["run_complete"])
        self.assertEqual(report["by_stage"]["boundary_check"]["requests_with_unknown_usage"], 1)
        self.assertFalse(report["usage_complete"])
        self.assertIn("provider unavailable", report["failed_boundary_checks"][0]["error"])
        self.assertEqual(report["coverage"]["disclosure_source_units"], 6)

    def test_chained_boundary_merges_are_replayed_before_next_check(self):
        self.paragraphs, self.sentences = [], []
        for _ in range(4):
            self.add_paragraph("7", ["A shared topic and context. " * 25])
        self.write_inputs()
        prompts = []

        def merge(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            if payload.get("task") == "check_disclosure_boundary":
                prompts.append(payload)
            return self.boundary_response(prompt, merge=True, **kwargs)

        options = ("--batch-chars", "1000")
        self.assertEqual(self.run_command(*options, "--max-requests", "5", responder=merge), (2, 5))
        self.assertEqual(self.run_command(*options, responder=merge), (0, 2))
        self.assertEqual([len(p["selected_evidence"]) for p in prompts], [2, 3, 4])
        records = self.read_output("disclosures.json")["disclosures"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["verification"]["source_unit_count"], 4)
        self.assertEqual(len(records[0]["verification"]["boundary_checks"]), 3)
        self.assertEqual(self.run_command(*options, responder=merge), (0, 0))
        self.assertEqual(self.read_output("token_usage.json")["reported_tokens"]["total_tokens"], 875)

    def test_stale_boundary_cache_does_not_validate_changed_proposals(self):
        self.split_fixture()
        options = ("--batch-chars", "1000")
        self.assertEqual(self.run_command(*options, responder=self.boundary_response), (0, 5))
        cache = self.root / "disclosures/amd/2024/requests/batch_001_attempt_001.json"
        saved = json.loads(cache.read_text())
        result = json.loads(saved["result"]["text"])
        result["disclosures"][0]["summary"] = "A corrected saved proposal."
        saved["result"]["text"] = json.dumps(result)
        cache.write_text(json.dumps(saved))
        self.assertEqual(self.run_command(*options, responder=self.boundary_response), (1, 0))
        self.assertIn("differs", self.read_output("token_usage.json")["failed_boundary_checks"][0]["error"])
        self.assertEqual(self.run_command(*options, "--retry-failed", responder=self.boundary_response), (0, 2))

    def test_oversized_boundary_is_not_silently_approved_or_sent_unbounded(self):
        self.split_fixture()
        with patch.object(module, "MAX_BOUNDARY_PROMPT_CHARS", 500), \
             patch.object(boundary, "MAX_BOUNDARY_PROMPT_CHARS", 500):
            self.assertEqual(self.run_command("--batch-chars", "1000", responder=self.boundary_response), (1, 3))
        report = self.read_output("token_usage.json")
        self.assertFalse(report["run_complete"])
        self.assertIn("exceeds", report["failed_boundary_checks"][0]["error"])
        self.assertEqual(report["review_candidates"], 2)

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
