"""Unit tests for the SQL-accuracy scorer (scripts/score_chatbot_sql.py)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import score_chatbot_sql as sc  # noqa: E402


def res(columns, rows, name="r"):
    return {"name": name, "columns": columns, "rows": [dict(zip(columns, r)) for r in rows], "total_rows": len(rows)}


class ScorerTests(unittest.TestCase):
    def test_count_accepts_a_per_run_breakdown_but_not_unrelated_sums(self):
        gold = res(["n"], [[10]])
        by_run = res(["company", "current_year", "n"], [["bac", 2024, 4], ["jpm", 2024, 6]])
        self.assertTrue(sc.score_count({"answer": "10 alignments"}, gold, [by_run])["data"])
        scores = res(["match_id", "materiality_score"], [[f"bac_2023_2024_M{i:04d}", 1.0] for i in range(10)])
        self.assertFalse(sc.score_count({"answer": ""}, gold, [scores])["data"])

    def test_ids_need_no_wrong_ids_and_full_list_or_matching_total(self):
        gold = res(["match_id"], [["a_2023_2024_M0001"], ["a_2023_2024_M0002"], ["a_2023_2024_M0003"]])
        partial = res(["match_id"], [["a_2023_2024_M0001"]])
        total = res(["company", "n"], [["a", 3]])
        self.assertFalse(sc.score_ids({"answer": ""}, gold, [partial])["data"])
        self.assertTrue(sc.score_ids({"answer": ""}, gold, [partial, total])["data"])
        wrong = res(["match_id"], [["a_2023_2024_M0001"], ["a_2023_2024_M0009"]])
        self.assertFalse(sc.score_ids({"answer": ""}, gold, [wrong, total])["data"])

    def test_topn_allows_any_order_among_ties(self):
        gold = res(["match_id", "score"], [["x1", 0.9], ["x2", 0.8], ["x3", 0.8], ["x4", 0.5]])
        q = {"n": 2, "order": "desc", "answer": ""}
        self.assertTrue(sc.score_topn(q, gold, [res(["match_id"], [["x1"], ["x3"]])])["data"])
        self.assertFalse(sc.score_topn(q, gold, [res(["match_id"], [["x2"], ["x3"]])])["data"])

    def test_groups_read_long_and_wide_tables(self):
        gold = res(["company", "change_type", "n"], [["bac", "New", 2], ["bac", "Removed", 1]])
        long = res(["company", "change_type", "n"], [["bac", "New", 2], ["bac", "Removed", 1]])
        wide = res(["company", "new", "removed"], [["bac", 2, 1]])
        self.assertTrue(sc.score_groups({"answer": ""}, gold, [long])["data"])
        self.assertTrue(sc.score_groups({"answer": ""}, gold, [wide])["data"])

    def test_value_tolerates_rounding_and_percent_scale(self):
        gold = res(["value"], [[46.119929]])
        self.assertTrue(sc.score_value({"answer": "46.1%"}, gold, [res(["value"], [[46.12]])])["data"])
        self.assertTrue(sc.score_value({"answer": "46.1%"}, gold, [res(["value"], [[46.12]])])["text"])
        self.assertTrue(sc.score_value({"answer": ""}, gold, [res(["ratio"], [[0.4612]])])["data"])

    def test_table_allows_extra_rows_and_top_answer(self):
        gold = res(["year", "margin"], [[2023, 1.77], [2024, 7.37]])
        more = res(["metric", "year", "value"], [["m", 2022, 5.4], ["m", 2023, 1.77], ["m", 2024, 7.37]])
        self.assertTrue(sc.score_table({"question": "How did it change?", "answer": ""}, gold, [more])["data"])
        ranked = res(["item", "pct"], [["1", 11.3], ["1A", 10.6]])
        q = {"question": "Which Item had the highest share?", "answer": "Item 1"}
        self.assertTrue(sc.score_table(q, ranked, [res(["item", "share"], [["1", 11.0]])])["data"])
        rows = res(["match_id", "item"], [["a_2023_2024_M0001", "1"]])
        self.assertFalse(sc.score_table(q, ranked, [rows])["data"])   # row-level results don't count as the top answer


if __name__ == "__main__":
    unittest.main()
