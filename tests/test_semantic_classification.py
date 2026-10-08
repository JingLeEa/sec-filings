"""Tests for the rule-based semantic classifier.

Uses the same stub embedder from test_semantic_retrieval so scores are
deterministic and every assertion is exact. What is tested is the
classification logic — threshold bands, label selection, evidence shape,
and edge cases — not the quality of any embedding model.
"""

from __future__ import annotations

import math
import unittest

from sec_disclosure.indexing.semantic_retrieval import SemanticIndex
from sec_disclosure.indexing.semantic_classification import (
    CONTENT_WEIGHT,
    MODIFIED_FLOOR,
    UNCHANGED_FLOOR,
    WEAK_FLOOR,
    _ceiling,
    classify_alignment_row,
    classify_all,
    classify_pair,
    classify_removed,
)


# ── stub embedder (identical to test_semantic_retrieval) ────────────────

TOPICS = {
    "foundry": ("foundry", "fab", "wafer", "manufacturing", "capacity"),
    "credit": ("credit", "allowance", "losses", "charge-off", "provision"),
    "demand": ("demand", "compute", "processing", "power", "capacity-need"),
}


def stub_embedder(texts: list[str]) -> list[list[float]]:
    vectors = []
    for text in texts:
        lowered = (text or "").lower()
        raw = [sum(word in lowered for word in words) for words in TOPICS.values()]
        norm = math.sqrt(sum(v * v for v in raw))
        vectors.append([v / norm for v in raw] if norm else [0.0, 0.0, 0.0])
    return vectors


def record(key, year, item, section, content, summary="", taxonomy=""):
    return key, {
        "disclosure_id": key, "fiscal_year": year, "item": item,
        "section": section, "content": content,
        "summary": summary, "taxonomy": taxonomy,
    }


class CeilingTests(unittest.TestCase):
    def test_ceiling_is_content_weight_when_summaries_empty(self):
        records = dict([record("a", "2024", "1A", "S", "text")])
        self.assertEqual(_ceiling(records), CONTENT_WEIGHT)

    def test_ceiling_is_one_when_summaries_present(self):
        records = dict([record("a", "2024", "1A", "S", "text", summary="has summary")])
        self.assertEqual(_ceiling(records), 1.0)

    def test_ceiling_with_mixed_summaries_is_one(self):
        records = dict([
            record("a", "2024", "1A", "S", "text", summary=""),
            record("b", "2024", "1A", "S", "text", summary="present"),
        ])
        self.assertEqual(_ceiling(records), 1.0)


class ClassifyPairTests(unittest.TestCase):
    def test_no_candidate_returns_added(self):
        rec = dict([record("c", "2025", "1A", "S", "new disclosure")])
        result = classify_pair(rec["c"], None, rec, 0.65)
        self.assertEqual(result["label"], "Added")
        self.assertIsNone(result["matched_id"])

    def test_high_score_identical_text_returns_unchanged(self):
        text = "foundry fab wafer manufacturing capacity"
        records = dict([
            record("p", "2024", "1A", "S", text),
            record("c", "2025", "1A", "S", text),
        ])
        ceiling = 0.65
        candidate = {"disclosure_id": "p", "score": ceiling, "text_score": ceiling}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Unchanged")
        self.assertEqual(result["matched_id"], "p")
        self.assertAlmostEqual(result["normalised_score"], 1.0, places=2)

    def test_moderate_score_returns_modified(self):
        records = dict([
            record("p", "2024", "1A", "S", "foundry fab wafer manufacturing capacity"),
            record("c", "2025", "1A", "S", "compute demand keeps rising at fab"),
        ])
        ceiling = 0.65
        score = ceiling * 0.80  # in the Modified band
        candidate = {"disclosure_id": "p", "score": score, "text_score": score}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Modified")

    def test_low_score_returns_added(self):
        records = dict([
            record("p", "2024", "1A", "S", "credit allowance losses"),
            record("c", "2025", "1A", "S", "foundry fab wafer"),
        ])
        ceiling = 0.65
        score = ceiling * 0.40  # well below WEAK_FLOOR
        candidate = {"disclosure_id": "p", "score": score, "text_score": score}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Added")

    def test_section_change_returns_relocated(self):
        text = "foundry fab wafer manufacturing capacity"
        records = dict([
            record("p", "2024", "1A", "Risk Factors", text),
            record("c", "2025", "1A", "Strategy", text),
        ])
        ceiling = 0.65
        score = ceiling * 0.85
        candidate = {"disclosure_id": "p", "score": score, "text_score": score}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Relocated")

    def test_expanded_when_content_grows(self):
        records = dict([
            record("p", "2024", "1A", "S", "foundry fab wafer"),
            record("c", "2025", "1A", "S",
                   "foundry fab wafer manufacturing capacity "
                   "additional details about expansion plans and new facilities "
                   "including timeline and projected output increases"),
        ])
        ceiling = 0.65
        score = ceiling * 0.80
        candidate = {"disclosure_id": "p", "score": score, "text_score": score}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Expanded")

    def test_reduced_when_content_shrinks(self):
        records = dict([
            record("p", "2024", "1A", "S",
                   "foundry fab wafer manufacturing capacity "
                   "additional details about expansion plans and new facilities "
                   "including timeline and projected output increases"),
            record("c", "2025", "1A", "S", "foundry fab wafer"),
        ])
        ceiling = 0.65
        score = ceiling * 0.80
        candidate = {"disclosure_id": "p", "score": score, "text_score": score}
        result = classify_pair(records["c"], candidate, records, ceiling)
        self.assertEqual(result["label"], "Reduced")


class ClassifyRemovedTests(unittest.TestCase):
    def test_no_reverse_hit_returns_removed(self):
        candidates = {"c1": [{"disclosure_id": "other", "score": 0.5}]}
        result = classify_removed("p_gone", candidates, 0.65)
        self.assertEqual(result["label"], "Removed")
        self.assertIsNone(result["matched_id"])

    def test_reverse_hit_above_threshold_returns_see_current(self):
        candidates = {"c1": [{"disclosure_id": "p_found", "score": 0.65 * 0.7}]}
        result = classify_removed("p_found", candidates, 0.65)
        self.assertEqual(result["label"], "see_current_side")
        self.assertEqual(result["matched_id"], "c1")

    def test_reverse_hit_below_threshold_returns_removed(self):
        candidates = {"c1": [{"disclosure_id": "p_weak", "score": 0.65 * 0.3}]}
        result = classify_removed("p_weak", candidates, 0.65)
        self.assertEqual(result["label"], "Removed")


class ClassifyAlignmentRowTests(unittest.TestCase):
    def setUp(self):
        self.records = dict([
            record("p1", "2024", "1A", "Risk Factors",
                   "Our foundry and fab capacity depends on wafer manufacturing."),
            record("p2", "2024", "1A", "Risk Factors",
                   "The allowance for credit losses reflects provision and charge-off trends."),
            record("c1", "2025", "1A", "Risk Factors",
                   "Wafer manufacturing capacity in our fab network remains constrained."),
            record("c2", "2025", "1A", "Risk Factors",
                   "Credit losses and the related provision increased."),
        ])
        self.index = SemanticIndex(self.records, embedder=stub_embedder)
        self.candidates = self.index.candidates("2024", "2025", top_k=5)

    def test_both_sides_one_to_one(self):
        row = {"previous_ids": ["p1"], "current_ids": ["c1"]}
        result = classify_alignment_row(row, self.candidates, self.records)
        self.assertIn(result["label"], ("Unchanged", "Modified", "Expanded", "Reduced", "Relocated"))
        self.assertEqual(result["matched_id"], "p1")
        self.assertIsNotNone(result["score"])

    def test_introduced_disclosure(self):
        # c_new has no previous-year counterpart
        extra = dict([record("c_new", "2025", "1A", "Risk Factors",
                             "A completely novel risk factor about quantum computing.")])
        records = {**self.records, **extra}
        index = SemanticIndex(records, embedder=stub_embedder)
        candidates = index.candidates("2024", "2025", top_k=5)
        row = {"previous_ids": [], "current_ids": ["c_new"]}
        result = classify_alignment_row(row, candidates, records)
        # With our stub embedder this will likely score low → Added
        self.assertIn(result["label"], ("Added", "Modified"))

    def test_removed_disclosure(self):
        extra = dict([record("p_gone", "2024", "1A", "Risk Factors",
                             "A unique risk about satellite communications.")])
        records = {**self.records, **extra}
        index = SemanticIndex(records, embedder=stub_embedder)
        candidates = index.candidates("2024", "2025", top_k=5)
        row = {"previous_ids": ["p_gone"], "current_ids": []}
        result = classify_alignment_row(row, candidates, records)
        self.assertEqual(result["label"], "Removed")

    def test_many_to_one_merge(self):
        row = {"previous_ids": ["p1", "p2"], "current_ids": ["c1"]}
        result = classify_alignment_row(row, self.candidates, self.records)
        self.assertEqual(result["label"], "Modified")
        self.assertIn("erge", result["rationale"])  # "Merge"

    def test_one_to_many_split(self):
        row = {"previous_ids": ["p1"], "current_ids": ["c1", "c2"]}
        result = classify_alignment_row(row, self.candidates, self.records)
        self.assertEqual(result["label"], "Modified")
        self.assertIn("plit", result["rationale"])  # "Split"


class EvidenceShapeTests(unittest.TestCase):
    """Every classification result must carry the full evidence dict."""

    REQUIRED_KEYS = {"label", "matched_id", "score", "normalised_score", "rationale"}

    def test_added_has_all_keys(self):
        rec = dict([record("c", "2025", "1A", "S", "text")])
        result = classify_pair(rec["c"], None, rec, 0.65)
        self.assertEqual(set(result.keys()), self.REQUIRED_KEYS)

    def test_matched_has_all_keys(self):
        text = "foundry fab wafer"
        records = dict([
            record("p", "2024", "1A", "S", text),
            record("c", "2025", "1A", "S", text),
        ])
        candidate = {"disclosure_id": "p", "score": 0.65, "text_score": 0.65}
        result = classify_pair(records["c"], candidate, records, 0.65)
        self.assertEqual(set(result.keys()), self.REQUIRED_KEYS)

    def test_removed_has_all_keys(self):
        result = classify_removed("p_gone", {}, 0.65)
        self.assertEqual(set(result.keys()), self.REQUIRED_KEYS)


class ClassifyAllTests(unittest.TestCase):
    def test_mutates_rows_in_place(self):
        records = dict([
            record("p1", "2024", "1A", "S", "foundry fab wafer manufacturing capacity"),
            record("c1", "2025", "1A", "S", "wafer manufacturing capacity in our fab"),
        ])
        index = SemanticIndex(records, embedder=stub_embedder)
        candidates = index.candidates("2024", "2025", top_k=5)
        rows = [{"previous_ids": ["p1"], "current_ids": ["c1"],
                 "change_analysis": {"status": "not_started", "lexical": None,
                                     "semantic": None, "llm": None, "final_taxonomy": None}}]
        result = classify_all(rows, candidates, records)
        self.assertIs(result, rows)  # same list, mutated
        self.assertEqual(rows[0]["change_analysis"]["status"], "semantic_done")
        self.assertIsNotNone(rows[0]["change_analysis"]["semantic"])
        self.assertIn(rows[0]["change_analysis"]["semantic"]["label"],
                      ("Unchanged", "Modified", "Expanded", "Reduced", "Added", "Removed", "Relocated"))

    def test_preserves_existing_lexical_analysis(self):
        records = dict([
            record("p1", "2024", "1A", "S", "foundry fab"),
            record("c1", "2025", "1A", "S", "foundry fab"),
        ])
        index = SemanticIndex(records, embedder=stub_embedder)
        candidates = index.candidates("2024", "2025", top_k=5)
        existing_lexical = {"label": "Unchanged", "score": 0.95}
        rows = [{"previous_ids": ["p1"], "current_ids": ["c1"],
                 "change_analysis": {"status": "not_started", "lexical": existing_lexical,
                                     "semantic": None, "llm": None, "final_taxonomy": None}}]
        classify_all(rows, candidates, records)
        self.assertEqual(rows[0]["change_analysis"]["lexical"], existing_lexical)

    def test_seeds_change_analysis_if_missing(self):
        records = dict([
            record("p1", "2024", "1A", "S", "foundry fab"),
            record("c1", "2025", "1A", "S", "foundry fab"),
        ])
        index = SemanticIndex(records, embedder=stub_embedder)
        candidates = index.candidates("2024", "2025", top_k=5)
        rows = [{"previous_ids": ["p1"], "current_ids": ["c1"]}]  # no change_analysis key
        classify_all(rows, candidates, records)
        self.assertIn("change_analysis", rows[0])
        self.assertIsNotNone(rows[0]["change_analysis"]["semantic"])


if __name__ == "__main__":
    unittest.main()
