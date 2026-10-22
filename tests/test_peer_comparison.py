import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.agents.peer_comparison import (
    attach_alignment_context,
    load_company,
    make_candidates,
    run,
    validate_response,
)


def disclosure(ticker, did, item, content, sid):
    return {
        "disclosure_id": did,
        "item": item,
        "summary": content,
        "topic": "Supplier concentration",
        "taxonomy": "Supply Chain & Third Parties",
        "content": content,
        "sources": [{
            "paragraph_id": f"{ticker.lower()}_P1",
            "source_url": f"https://www.sec.gov/{ticker.lower()}",
            "sentences": [{"sentence_id": sid, "text": content}],
        }],
    }


class PeerComparisonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = disclosure("AAA", "aaa_2024_1A_D001", "1A", "We depend on a single chip supplier.", "a1")
        self.peer = disclosure("BBB", "bbb_2024_1A_D001", "1A", "A sole semiconductor supplier creates risk.", "b1")
        self.peer2 = disclosure("BBB", "bbb_2024_1A_D002", "1A", "Our second supplier provides components.", "b2")
        self.other_item = disclosure("BBB", "bbb_2024_7_D001", "7", "We report operating results.", "b3")
        for ticker, rows in (("aaa", [self.target]), ("bbb", [self.peer, self.peer2, self.other_item])):
            folder = self.root / "disclosures" / ticker / "2024"
            folder.mkdir(parents=True)
            (folder / "disclosures.json").write_text(json.dumps({
                "company": ticker, "fiscal_year": "2024", "disclosures": rows
            }))

    def tearDown(self):
        self.tmp.cleanup()

    def _inputs(self):
        targets = load_company(self.root / "disclosures", "aaa", "2024")
        peers = {"bbb": load_company(self.root / "disclosures", "bbb", "2024")}
        ranked = make_candidates(targets, peers, 3)[self.target["disclosure_id"]]["bbb"]
        return targets[0], ranked

    def test_same_item_candidates_and_multiple_peer_matches(self):
        target, ranked = self._inputs()
        self.assertEqual({row["disclosure_id"] for row in ranked},
                         {self.peer["disclosure_id"], self.peer2["disclosure_id"]})
        result = validate_response({"comparisons": [
            {"peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
             "relationship": "same_topic", "comparison": "Both identify reliance on a single supplier.",
             "target_sentence_ids": ["a1"], "peer_sentence_ids": ["b1"]},
            {"peer_ticker": "BBB", "peer_disclosure_id": self.peer2["disclosure_id"],
             "relationship": "related_topic", "comparison": "The peer describes a second supplier as mitigation.",
             "target_sentence_ids": ["a1"], "peer_sentence_ids": ["b2"]},
        ], "no_match": []}, target, {"bbb": ranked}, ["bbb"])
        self.assertEqual(result[0]["status"], "compared")
        self.assertEqual(len(result[0]["comparisons"]), 2)
        self.assertEqual(result[0]["comparisons"][0]["evidence"][0]["sentences"][0]["sentence_id"], "a1")

    def test_rejects_citation_from_another_disclosure(self):
        target, ranked = self._inputs()
        with self.assertRaisesRegex(ValueError, "sentence IDs"):
            validate_response({"comparisons": [{
                "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
                "relationship": "same_topic", "comparison": "Comparison.",
                "target_sentence_ids": ["b1"], "peer_sentence_ids": ["b1"],
            }], "no_match": []}, target, {"bbb": ranked}, ["bbb"])

    def test_alignment_trajectory_context_attaches_to_both_members(self):
        targets, ranked = self._inputs()
        report_dir = self.root / "alignments" / "bbb" / "2023-2024"
        report_dir.mkdir(parents=True)
        report = {"company": "bbb", "previous_year": "2023", "current_year": "2024",
                  "run_complete": True, "alignments": [{
                      "match_id": "bbb_2023_2024_M1", "previous_ids": ["bbb_2023_1A_D001"],
                      "current_ids": [self.peer["disclosure_id"]], "relationship": "one_to_one",
                      "status": "ai_verified", "review_reasons": [], "explanation": "Risk continues.",
                      "previous_disclosures": [{"disclosure_id": "bbb_2023_1A_D001", "summary": "Old text"}],
                      "current_disclosures": [{"disclosure_id": self.peer["disclosure_id"], "summary": "New text"}],
                      "change_analysis": {"final_taxonomy": "Reworded"},
                      "materiality_analysis": {"materiality": "Medium"},
                  }]}
        (report_dir / "alignments.json").write_text(json.dumps(report))
        attach_alignment_context(ranked, self.root / "alignments", "bbb", "2024")
        enriched = next(row for row in ranked if row["disclosure_id"] == self.peer["disclosure_id"])
        self.assertEqual(enriched["year_over_year_context"][0]["window"], "2023-2024")
        self.assertEqual(enriched["year_over_year_context"][0]["change_analysis"]["final_taxonomy"], "Reworded")

    def test_dry_run_creates_no_files(self):
        args = Namespace(ticker="aaa", peers=["bbb"], year="2024", items=["1A"],
                         disclosures_dir=self.root / "disclosures", alignments_dir=self.root / "alignments",
                         data_dir=self.root, output_dir=None, top_k=3, dry_run=True, env_file=None,
                         max_requests=100, max_tokens=100, max_prompt_chars=10000, timeout=10)
        result = run(args)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.root / "peer_comparison").exists())

    def test_live_workflow_writes_cited_report_and_resumes_from_cache(self):
        output = self.root / "output"
        response = json.dumps({"comparisons": [{
            "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
            "relationship": "same_topic", "comparison": "Both describe reliance on a single supplier.",
            "target_sentence_ids": ["a1"], "peer_sentence_ids": ["b1"],
        }], "no_match": []})
        completion = CompletionResult(text=response, model="test-model", finish_reason="stop",
                                     usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        args = Namespace(ticker="aaa", peers=["bbb"], year="2024", items=["1A"],
                         disclosures_dir=self.root / "disclosures", alignments_dir=self.root / "alignments",
                         data_dir=self.root, output_dir=output, top_k=3, dry_run=False, env_file=None,
                         max_requests=10, max_validation_attempts=2, max_tokens=100,
                         max_prompt_chars=10000, timeout=10)
        with patch("sec_disclosure.agents.peer_comparison.load_config",
                   return_value=SimpleNamespace(model="test-model", base_url="https://llm.example")), \
             patch("sec_disclosure.agents.peer_comparison.request_completion", return_value=completion) as request:
            first = run(args)
            second = run(args)
        self.assertTrue(first["run_complete"])
        self.assertTrue(second["run_complete"])
        self.assertEqual(request.call_count, 1)
        report = json.loads((output / "peer_comparison.json").read_text())
        self.assertEqual(report["comparisons"][0]["peer_results"][0]["status"], "compared")
        self.assertEqual(report["token_usage"]["total_tokens"], 15)

    def test_invalid_agent_citations_get_one_bounded_correction(self):
        output = self.root / "repair-output"
        invalid = json.dumps({"comparisons": [{
            "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
            "relationship": "same_topic", "comparison": "Comparison.",
            "target_sentence_ids": ["not-a-real-id"], "peer_sentence_ids": ["b1"],
        }], "no_match": []})
        valid = json.dumps({"comparisons": [{
            "peer_ticker": "BBB", "peer_disclosure_id": self.peer["disclosure_id"],
            "relationship": "same_topic", "comparison": "Both describe reliance on one supplier.",
            "target_sentence_ids": ["a1"], "peer_sentence_ids": ["b1"],
        }], "no_match": []})
        completions = [
            CompletionResult(invalid, "test-model", "stop", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}),
            CompletionResult(valid, "test-model", "stop", {"prompt_tokens": 15, "completion_tokens": 5, "total_tokens": 20}),
        ]
        args = Namespace(ticker="aaa", peers=["bbb"], year="2024", items=["1A"],
                         disclosures_dir=self.root / "disclosures", alignments_dir=self.root / "alignments",
                         data_dir=self.root, output_dir=output, top_k=3, dry_run=False, env_file=None,
                         max_requests=10, max_validation_attempts=2, max_tokens=100,
                         max_prompt_chars=10000, timeout=10)
        with patch("sec_disclosure.agents.peer_comparison.load_config",
                   return_value=SimpleNamespace(model="test-model", base_url="https://llm.example")), \
             patch("sec_disclosure.agents.peer_comparison.request_completion", side_effect=completions) as request:
            report = run(args)
        self.assertTrue(report["run_complete"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(report["token_usage"]["total_tokens"], 35)
        errors = list((output / "validation_errors").glob("*.json"))
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
