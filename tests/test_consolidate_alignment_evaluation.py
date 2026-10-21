"""Pooled evaluation scores are recomputed from source confusion counts."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "consolidate_alignment_evaluation", SCRIPTS / "consolidate_alignment_evaluation.py"
)
assert SPEC is not None and SPEC.loader is not None
consolidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(consolidate)


def group(labels, values):
    matrix = {label: {} for label in labels}
    for reference, candidate, count in values:
        matrix[reference][candidate] = count
    compared = sum(count for _, _, count in values)
    return {"eligible_rows": compared, "reference_completed": compared,
            "reference_pending": 0, "missing_candidate_rows": [],
            "missing_candidate_predictions": [], "compared": compared,
            "confusion_matrix": matrix}


def pair(reference_model, change_values, materiality_values):
    change = group(consolidate.CHANGE_LABELS, change_values)
    materiality = group(consolidate.MATERIALITY_LABELS, materiality_values)
    return {
        "comparison": {"company": "amd", "reference_model": reference_model,
                       "candidate_model": "Qwen"},
        "agreement": {
            "change_analysis": {key: change for key in consolidate.CHANGE_METHODS.values()},
            "materiality_analysis": {"model_rows": materiality},
        },
        "source": "synthetic",
    }


class ConsolidationTests(unittest.TestCase):
    def test_pools_counts_before_calculating_f1(self):
        pairs = [
            pair("DeepSeek", [("Modified", "Modified", 9), ("Reworded", "Reworded", 1)],
                 [("Yes", "Yes", 9), ("No", "No", 1)]),
            pair("x-test-2", [("Modified", "Reworded", 1), ("Reworded", "Reworded", 1)],
                 [("Yes", "No", 1), ("No", "No", 1)]),
        ]
        summary = consolidate.summarize(pairs)
        change = summary["change_analysis"]["llm"]["substantive_change"]
        materiality = summary["materiality_analysis"]
        self.assertEqual(summary["comparison_count"], 2)
        self.assertEqual(summary["reference_models"], {"DeepSeek": 1, "x-test-2": 1})
        self.assertEqual(change["compared"], 12)
        self.assertEqual(change["missed_substantive"], 1)
        self.assertEqual(change["per_label"]["Substantive"]["recall"], 0.9)
        self.assertEqual(materiality["per_label"]["Yes"]["recall"], 0.9)
        self.assertEqual(consolidate.percent(1, 10), "10.0%")
        detailed = consolidate.detailed_change_table(summary["change_analysis"], binary=True)
        self.assertTrue(any("| LLM | Substantive | 10 | 9 |" in row for row in detailed))
        self.assertTrue(any("| LLM | **Macro F1** |" in row for row in detailed))
        taxonomy = consolidate.detailed_change_table(summary["change_analysis"], binary=False)
        self.assertFalse(any("| New |" in row for row in taxonomy))

    def test_unmapped_semantic_label_reduces_binary_coverage(self):
        pairs = [pair("DeepSeek", [("Modified", "Modified", 1),
                                      ("Modified", "Relocated", 1)],
                      [("Yes", "Yes", 2)])]
        binary = consolidate.summarize(pairs)["change_analysis"]["semantic"]["substantive_change"]
        self.assertEqual(binary["compared"], 1)
        self.assertEqual(binary["total_compared"], 2)
        self.assertEqual(binary["prediction_coverage_rate"], 0.5)

    def test_reference_pending_rows_explain_eligible_gap(self):
        entry = pair("DeepSeek", [("Modified", "Modified", 1)], [("Yes", "Yes", 1)])
        materiality = entry["agreement"]["materiality_analysis"]["model_rows"]
        materiality["eligible_rows"] = 3
        materiality["reference_pending"] = 2
        result = consolidate.summarize([entry])["materiality_analysis"]
        self.assertEqual((result["eligible_rows"], result["reference_pending"],
                          result["compared"]), (3, 2, 1))
        self.assertEqual(result["coverage_rate"], 0.3333)

    def test_missing_companion_file_is_rejected(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "amd" / "2023-2024"
            folder.mkdir(parents=True)
            (folder / "agreement.json").write_text(json.dumps({"comparison": {}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Incomplete evaluation output"):
                consolidate.read_pairs(Path(temporary))

    def test_missing_year_pair_is_reported(self):
        first = pair("DeepSeek", [("Modified", "Modified", 1)], [("Yes", "Yes", 1)])
        first["comparison"].update(company="amd", previous_year="2023", current_year="2024")
        second = pair("DeepSeek", [("Modified", "Modified", 1)], [("Yes", "Yes", 1)])
        second["comparison"].update(company="amd", previous_year="2024", current_year="2025")
        third = pair("DeepSeek", [("Modified", "Modified", 1)], [("Yes", "Yes", 1)])
        third["comparison"].update(company="gs", previous_year="2024", current_year="2025")
        self.assertEqual(consolidate.missing_pairs([first, second, third]), ["GS 2023-2024"])


if __name__ == "__main__":
    unittest.main()
