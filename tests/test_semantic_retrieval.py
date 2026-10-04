"""Tests for the embedding-backed index, using a deterministic stub embedder.

No network and no API key: the stub maps text to vectors by a fixed topic
vocabulary, so "similar" means something controllable and every assertion is
exact. What is tested is the retrieval contract - ordering, Item scoping, the
scoring blend, reverse retrieval, empty-summary degradation - not the quality
of any particular embedding model.
"""

from __future__ import annotations

import math
import unittest

from sec_disclosure.indexing.semantic_retrieval import SemanticIndex

# Three orthogonal "topics". A text's vector is the normalised mix of the topic
# words it contains, so paraphrases sharing no words still land close together.
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
    return key, {"disclosure_id": key, "fiscal_year": year, "item": item, "section": section,
                 "content": content, "summary": summary, "taxonomy": taxonomy}


class SemanticIndexTests(unittest.TestCase):
    def setUp(self):
        self.records = dict([
            record("p1", "2024", "1A", "Risk Factors",
                   "Our foundry and fab capacity depends on wafer manufacturing."),
            record("p2", "2024", "1A", "Risk Factors",
                   "The allowance for credit losses reflects provision and charge-off trends."),
            record("p3", "2024", "7", "MD&A",
                   "Demand for compute and processing power continues."),
            record("c1", "2025", "1A", "Risk Factors",
                   "Wafer manufacturing capacity in our fab network remains constrained."),
            record("c2", "2025", "1A", "Risk Factors",
                   "Credit losses and the related provision increased."),
        ])
        self.index = SemanticIndex(self.records, embedder=stub_embedder)

    def test_finds_the_topical_counterpart_first(self):
        hits = self.index.search("", "2024", anchor=self.records["c1"], limit=3)
        self.assertEqual(hits[0]["disclosure_id"], "p1")

    def test_search_is_scoped_to_one_item(self):
        # p3 is the strongest "demand" match but lives in Item 7, so an Item 1A
        # search must never return it.
        hits = self.index.search("", "2024", item="1A", limit=10)
        self.assertNotIn("p3", [hit["disclosure_id"] for hit in hits])

    def test_search_requires_an_item(self):
        with self.assertRaises(ValueError):
            self.index.search("wafer", "2024")

    def test_anchor_item_must_match_requested_item(self):
        with self.assertRaises(ValueError):
            self.index.search("", "2024", item="7", anchor=self.records["c1"])

    def test_year_filter_excludes_same_year_records(self):
        hits = self.index.search("", "2024", anchor=self.records["c1"], limit=10)
        self.assertTrue(all(self.records[h["disclosure_id"]]["fiscal_year"] == "2024" for h in hits))

    def test_empty_summary_degrades_to_content_only(self):
        # Every summary is "" here, so the summary term contributes zero and the
        # base score is bounded by the 0.65 content weight.
        hits = self.index.search("", "2024", anchor=self.records["c1"], limit=1)
        self.assertLessEqual(hits[0]["text_score"], 0.65 + 1e-6)

    def test_taxonomy_bonus_applies_only_with_an_anchor(self):
        records = dict([
            record("p", "2024", "1A", "S", "foundry fab wafer", taxonomy="Manufacturing"),
            record("c", "2025", "1A", "S", "foundry fab wafer", taxonomy="Manufacturing"),
        ])
        index = SemanticIndex(records, embedder=stub_embedder)
        with_anchor = index.search("", "2024", anchor=records["c"], limit=1)[0]
        self.assertGreater(with_anchor["score"], with_anchor["text_score"])

    def test_candidates_cover_both_directions(self):
        candidates = self.index.candidates("2024", "2025", top_k=1)
        self.assertEqual(set(candidates), {"c1", "c2"})
        self.assertEqual(candidates["c1"][0]["disclosure_id"], "p1")
        self.assertEqual(candidates["c2"][0]["disclosure_id"], "p2")

    def test_reverse_retrieval_adds_a_marked_candidate(self):
        # Two previous-year disclosures both map to one current-year disclosure.
        # With top_k=1 the forward pass can only keep one of them; the other has
        # to arrive through reverse retrieval, which is exactly the merge case
        # reverse retrieval exists for.
        records = dict([
            record("p_a", "2024", "1A", "S", "foundry fab wafer capacity"),
            record("p_b", "2024", "1A", "S", "manufacturing capacity wafer foundry"),
            record("c_merged", "2025", "1A", "S", "fab wafer foundry manufacturing capacity"),
        ])
        candidates = SemanticIndex(records, embedder=stub_embedder).candidates("2024", "2025", top_k=1)
        bucket = candidates["c_merged"]
        self.assertEqual({hit["disclosure_id"] for hit in bucket}, {"p_a", "p_b"})
        self.assertEqual([hit.get("retrieval_direction") for hit in bucket].count("reverse"), 1)

    def test_empty_index_returns_nothing(self):
        index = SemanticIndex({}, embedder=stub_embedder)
        self.assertEqual(index.search("", "2024", item="1A"), [])


class ParaphraseTests(unittest.TestCase):
    """The case this stage exists for: a counterpart sharing no content words.

    The anchor and the true match have no vocabulary in common, while a decoy
    shares a word. Term overlap would point at the decoy; meaning points at the
    true match, and this index must follow meaning.
    """

    def setUp(self):
        self.records = dict([
            record("p_true", "2024", "1", "Strategy",
                   "Processing power requirements continue to grow."),
            record("p_decoy", "2024", "1", "Strategy",
                   "Compute infrastructure spending on foundry fab wafer manufacturing stayed high."),
            record("c", "2025", "1", "Strategy",
                   "Compute demand keeps rising."),
        ])

    def test_ranks_the_word_disjoint_paraphrase_first(self):
        index = SemanticIndex(self.records, embedder=stub_embedder)
        hits = index.search("", "2024", anchor=self.records["c"], limit=2)
        self.assertEqual(hits[0]["disclosure_id"], "p_true")

    def test_decoy_sharing_a_word_still_ranks_below(self):
        index = SemanticIndex(self.records, embedder=stub_embedder)
        hits = index.search("", "2024", anchor=self.records["c"], limit=2)
        self.assertEqual(hits[1]["disclosure_id"], "p_decoy")
        self.assertGreater(hits[0]["score"], hits[1]["score"])


if __name__ == "__main__":
    unittest.main()
