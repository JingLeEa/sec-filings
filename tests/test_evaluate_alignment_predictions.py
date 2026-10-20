"""Agreement reporting for production versus OpenAI alignment classifications."""

import importlib.util
import json
import unittest
from collections import Counter
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_alignment_predictions.py"
SPEC = importlib.util.spec_from_file_location("evaluate_alignment_predictions", SCRIPT)
evaluate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluate)


def row(match_id, status, change, materiality, strength=None, confidence=None):
    return {
        "match_id": match_id, "status": status,
        "relationship": "one_to_one", "previous_ids": [f"old_{match_id}"],
        "current_ids": [f"new_{match_id}"],
        "change_analysis": {"status": "completed" if change is not None else "not_started",
                            "llm": change},
        "materiality_analysis": {"materiality": materiality,
                                 "materiality_score": strength,
                                 "confidence_score": confidence,
                                 "reason": f"Explanation for {match_id}." if materiality else None},
    }


class EvaluateAlignmentPredictionsTests(unittest.TestCase):
    def test_default_directories(self):
        args = evaluate.parse_args(["--ticker", "amd"])
        self.assertEqual(args.reference_root, Path("data/alignments_for_eval_soclaas"))
        self.assertEqual(args.candidate_root, Path("data/alignments"))
        self.assertEqual(args.output_root, Path("data/evaluation_soclaas"))
        self.assertEqual(args.model, "DeepSeek")

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "reference" / "amd" / "2023-2024" / "alignments.json"
        self.candidate = self.root / "candidate" / "amd" / "2023-2024" / "alignments.json"
        reference_rows = [
            row("M1", "ai_verified", "Modified", "Yes", 0.8, 0.9),
            row("M2", "ai_verified", "Reworded", "No", 0.2, 0.9),
            row("M3", "ai_verified", None, None),
            row("M4", "unmatched", "Removed", "Uncertain", 0.5, 0.4),
            row("M5", "auto_matched", "Unchanged", "No", 0.0, 1.0),
        ]
        candidate_rows = [
            row("M1", "ai_verified", "Modified", "Yes", 0.7, 0.8),
            row("M2", "ai_verified", "Modified", "Yes", 0.7, 0.8),
            row("M3", "ai_verified", "Modified", "No", 0.2, 0.8),
            row("M4", "unmatched", "Removed", "No", 0.2, 0.9),
            row("M5", "auto_matched", "Unchanged", "No", 0.0, 1.0),
        ]
        candidate_rows[0]["change_analysis"].update({
            "lexical": {"change_taxonomy": "Expanded"},
            "semantic": {"label": "Relocated"},
        })
        candidate_rows[1]["change_analysis"].update({
            "lexical": {"change_taxonomy": "Reworded"},
            "semantic": {"label": "Unchanged"},
        })
        for path, rows in ((self.reference, reference_rows), (self.candidate, candidate_rows)):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"company": "amd", "previous_year": "2023",
                                        "current_year": "2024", "run_complete": True,
                                        "alignments": rows}), encoding="utf-8")

    def test_partial_coverage_and_agreement(self):
        report = evaluate.evaluate_pair(self.reference, self.candidate)
        change = report["change_analysis"]["model_rows"]
        self.assertEqual((change["eligible_rows"], change["reference_completed"],
                          change["compared"], change["matches"]), (3, 2, 2, 1))
        self.assertEqual(change["agreement_rate"], 0.5)
        self.assertEqual(change["confusion_matrix"]["Reworded"]["Modified"], 1)
        self.assertEqual(change["label_counts"]["reference"]["Reworded"], 1)
        self.assertEqual(change["label_counts"]["candidate"]["Modified"], 2)
        self.assertEqual(change["difference_counts"], [
            {"candidate": "Modified", "reference": "Reworded", "count": 1}])
        self.assertEqual(change["disagreements"][0]["match_id"], "M2")
        substantive = change["substantive_change"]
        self.assertEqual((substantive["compared"], substantive["matches"],
                          substantive["false_substantive"], substantive["missed_substantive"]),
                         (2, 1, 1, 0))
        self.assertEqual(substantive["per_label"]["Substantive"]["precision"], 0.5)
        self.assertEqual(substantive["per_label"]["Substantive"]["recall"], 1.0)
        self.assertEqual(report["change_analysis"]["deterministic_rows"]["matches"], 2)

        lexical = report["change_analysis"]["lexical_model_rows"]
        self.assertEqual((lexical["compared"], lexical["matches"]), (2, 1))
        self.assertEqual(lexical["per_label"]["Modified"]["recall"], 0.0)
        self.assertEqual(lexical["per_label"]["Reworded"]["recall"], 1.0)
        self.assertEqual(lexical["substantive_change"]["matches"], 2)

        semantic = report["change_analysis"]["semantic_model_rows"]
        self.assertEqual((semantic["compared"], semantic["matches"]), (2, 0))
        self.assertEqual(semantic["confusion_matrix"]["Modified"]["Relocated"], 1)
        self.assertEqual(semantic["label_counts"]["candidate"]["Relocated"], 1)
        self.assertIn({"candidate": "Relocated", "reference": "Modified", "count": 1},
                      semantic["difference_counts"])
        self.assertEqual(semantic["per_label"]["Modified"]["reference_count"], 1)
        self.assertEqual(semantic["substantive_change"]["total_compared"], 2)
        self.assertEqual(semantic["substantive_change"]["compared"], 1)
        self.assertEqual(semantic["substantive_change"]["prediction_coverage_rate"], 0.5)
        self.assertEqual(semantic["substantive_change"]["unmapped_prediction_counts"],
                         {"Relocated": 1})

        materiality = report["materiality_analysis"]["model_rows"]
        self.assertEqual((materiality["eligible_rows"], materiality["reference_completed"],
                          materiality["compared"], materiality["matches"]), (4, 3, 3, 1))
        self.assertEqual(materiality["confusion_matrix"]["No"]["Yes"], 1)
        self.assertEqual(materiality["confusion_matrix"]["Uncertain"]["No"], 1)
        self.assertEqual(materiality["difference_counts"], [
            {"candidate": "No", "reference": "Uncertain", "count": 1},
            {"candidate": "Yes", "reference": "No", "count": 1}])
        self.assertEqual(materiality["per_label"]["Yes"], {
            "reference_count": 1, "candidate_count": 2,
            "precision": 0.5, "recall": 1.0, "f1": 0.6667})
        self.assertEqual(materiality["per_label"]["Uncertain"]["precision"], None)
        self.assertEqual(materiality["yes_no_macro_f1_vs_reference"], 0.3333)
        self.assertEqual(materiality["macro_f1_vs_reference"], 0.2222)
        self.assertEqual(materiality["score_mae"]["materiality_score"]["mean_absolute_error"], 0.3)
        self.assertEqual(report["materiality_analysis"]["deterministic_rows"]["matches"], 1)
        self.assertIn("not accuracy", report["comparison"]["interpretation"])

    def test_cli_writes_report_without_changing_inputs(self):
        reference_bytes, candidate_bytes = self.reference.read_bytes(), self.candidate.read_bytes()
        output = self.root / "evaluation"
        with redirect_stdout(StringIO()) as captured:
            result = evaluate.main(["--ticker", "amd", "--model", "OpenAI",
                                    "--reference-root", str(self.root / "reference"),
                                    "--candidate-root", str(self.root / "candidate"),
                                    "--output-root", str(output)])
        self.assertEqual(result, 0)
        self.assertIn("change 1/2", captured.getvalue())
        self.assertIn("materiality 1/3", captured.getvalue())
        self.assertIn("Change taxonomy label counts", captured.getvalue())
        self.assertIn("Change taxonomy disagreements (Qwen -> OpenAI)", captured.getvalue())
        self.assertIn("Substantive change detection", captured.getvalue())
        self.assertIn("Lexical exact-taxonomy precision / recall / F1", captured.getvalue())
        self.assertIn("Semantic exact-taxonomy precision / recall / F1", captured.getvalue())
        self.assertIn("Relocated", captured.getvalue())
        self.assertIn("false substantive: 1", captured.getvalue())
        self.assertIn("Materiality disagreements (Qwen -> OpenAI)", captured.getvalue())
        self.assertIn("Materiality precision / recall / F1", captured.getvalue())
        self.assertIn("Yes/No macro F1 (primary; labels with OpenAI support): 0.333",
                      captured.getvalue())
        self.assertIn("Three-label macro F1 (Yes/No/Uncertain): 0.222", captured.getvalue())
        result_dir = output / "amd" / "2023-2024"
        self.assertTrue((result_dir / "agreement.json").is_file())
        metrics = json.loads((result_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(metrics["comparison"]["company"], "amd")
        self.assertEqual(metrics["change_analysis"]["llm"]["substantive_change"]
                         ["per_label"]["Substantive"]["precision"], 0.5)
        self.assertEqual(metrics["change_analysis"]["lexical"]["substantive_change"]
                         ["macro_f1_vs_reference"], 1.0)
        self.assertEqual(metrics["change_analysis"]["semantic"]["substantive_change"]
                         ["prediction_coverage_rate"], 0.5)
        self.assertEqual(metrics["materiality_analysis"]["per_label"]["Yes"]["recall"], 1.0)
        self.assertEqual(metrics["materiality_analysis"]["yes_no_macro_f1_vs_reference"], 0.3333)
        self.assertEqual(metrics["materiality_analysis"]["macro_f1_vs_reference"], 0.2222)
        self.assertNotIn("difference_counts", metrics["change_analysis"]["llm"])
        self.assertEqual((result_dir / "report.txt").read_text(encoding="utf-8"),
                         captured.getvalue())
        self.assertEqual(self.reference.read_bytes(), reference_bytes)
        self.assertEqual(self.candidate.read_bytes(), candidate_bytes)

    def test_cli_labels_reference_model_without_changing_metrics(self):
        output = self.root / "deepseek_evaluation"
        with redirect_stdout(StringIO()) as captured:
            result = evaluate.main(["--ticker", "amd", "--model", "DeepSeek V4.1 Flash",
                                    "--reference-root", str(self.root / "reference"),
                                    "--candidate-root", str(self.root / "candidate"),
                                    "--output-root", str(output)])
        self.assertEqual(result, 0)
        report_dir = output / "amd" / "2023-2024"
        report = captured.getvalue()
        self.assertIn("Qwen -> DeepSeek V4.1 Flash", report)
        self.assertIn("DeepSeek V4.1 Flash reference", report)
        self.assertNotIn("OpenAI", report)
        agreement = json.loads((report_dir / "agreement.json").read_text(encoding="utf-8"))
        metrics = json.loads((report_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(agreement["comparison"]["reference_model"], "DeepSeek V4.1 Flash")
        self.assertEqual(metrics["comparison"]["reference_model"], "DeepSeek V4.1 Flash")
        self.assertEqual(metrics["materiality_analysis"]["yes_no_macro_f1_vs_reference"], 0.3333)
        self.assertEqual(metrics["materiality_analysis"]["macro_f1_vs_reference"], 0.2222)

    def test_missing_method_prediction_is_reported(self):
        document = json.loads(self.candidate.read_text(encoding="utf-8"))
        del document["alignments"][1]["change_analysis"]["semantic"]
        self.candidate.write_text(json.dumps(document), encoding="utf-8")
        report = evaluate.evaluate_pair(self.reference, self.candidate)
        semantic = report["change_analysis"]["semantic_model_rows"]
        self.assertEqual(semantic["reference_completed"], 2)
        self.assertEqual(semantic["compared"], 1)
        self.assertEqual(semantic["missing_candidate_predictions"], ["M2"])

    def test_rejects_changed_alignment_membership(self):
        document = json.loads(self.candidate.read_text(encoding="utf-8"))
        document["alignments"][0]["current_ids"] = ["different"]
        self.candidate.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not comparable"):
            evaluate.evaluate_pair(self.reference, self.candidate)

    def test_rejects_duplicate_match_ids(self):
        document = json.loads(self.reference.read_text(encoding="utf-8"))
        document["alignments"].append(document["alignments"][0])
        self.reference.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate match_id"):
            evaluate.evaluate_pair(self.reference, self.candidate)

    def test_metrics_are_undefined_when_reference_has_no_completed_rows(self):
        document = json.loads(self.reference.read_text(encoding="utf-8"))
        for item in document["alignments"]:
            item["materiality_analysis"]["materiality"] = None
        self.reference.write_text(json.dumps(document), encoding="utf-8")
        report = evaluate.evaluate_pair(self.reference, self.candidate)
        metrics = report["materiality_analysis"]["model_rows"]
        self.assertEqual(metrics["compared"], 0)
        self.assertIsNone(metrics["yes_no_macro_f1_vs_reference"])
        self.assertIsNone(metrics["macro_f1_vs_reference"])
        with redirect_stdout(StringIO()) as captured:
            evaluate.print_materiality_metrics(metrics)
        self.assertIn("Yes/No macro F1 (primary; labels with OpenAI support): -",
                      captured.getvalue())

    def test_yes_no_macro_ignores_uncertain_class_but_counts_its_false_positives(self):
        confusion = {label: Counter() for label in evaluate.MATERIALITY_LABELS}
        confusion["Yes"]["Yes"] = 1
        confusion["No"]["No"] = 1
        confusion["Uncertain"]["No"] = 1
        self.assertEqual(evaluate.yes_no_macro_f1(confusion), 0.8333)

    def test_substantive_metrics_collapse_taxonomy_without_discarding_errors(self):
        confusion = {label: {other: 0 for other in evaluate.CHANGE_LABELS}
                     for label in evaluate.CHANGE_LABELS}
        confusion["Expanded"]["Modified"] = 2
        confusion["Reduced"]["Unchanged"] = 1
        confusion["Reworded"]["Modified"] = 3
        confusion["Unchanged"]["Reworded"] = 4
        metrics = evaluate.substantive_change_metrics({"compared": 10,
                                                       "confusion_matrix": confusion})
        self.assertEqual((metrics["matches"], metrics["missed_substantive"],
                          metrics["false_substantive"]), (6, 1, 3))
        self.assertEqual(metrics["agreement_rate"], 0.6)
        self.assertEqual(metrics["per_label"]["Substantive"], {
            "reference_count": 3, "candidate_count": 5,
            "precision": 0.4, "recall": 0.6667, "f1": 0.5})
        self.assertEqual(metrics["macro_f1_vs_reference"], 0.5833)
        self.assertNotIn("New", metrics["label_mapping"])
        self.assertNotIn("Removed", metrics["label_mapping"])


if __name__ == "__main__":
    unittest.main()
