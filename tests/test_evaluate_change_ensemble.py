"""The ensemble decision is scored against the same rows as LLM-only."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

BASELINE_SPEC = importlib.util.spec_from_file_location(
    "evaluate_alignment_predictions", SCRIPTS / "evaluate_alignment_predictions.py"
)
assert BASELINE_SPEC is not None and BASELINE_SPEC.loader is not None
baseline_evaluator = importlib.util.module_from_spec(BASELINE_SPEC)
BASELINE_SPEC.loader.exec_module(baseline_evaluator)

ENSEMBLE_SPEC = importlib.util.spec_from_file_location(
    "evaluate_change_ensemble", SCRIPTS / "evaluate_change_ensemble.py"
)
assert ENSEMBLE_SPEC is not None and ENSEMBLE_SPEC.loader is not None
ensemble = importlib.util.module_from_spec(ENSEMBLE_SPEC)
ENSEMBLE_SPEC.loader.exec_module(ensemble)


def row(match_id, *, change, lexical=None, semantic=None):
    return {
        "match_id": match_id,
        "status": "ai_verified",
        "relationship": "one_to_one",
        "previous_ids": [f"old_{match_id}"],
        "current_ids": [f"new_{match_id}"],
        "evidence": [],
        "change_analysis": {
            "status": "completed" if change is not None else "not_started",
            "llm": change,
            "lexical": {"change_taxonomy": lexical} if lexical else None,
            "semantic": {"label": semantic} if semantic else None,
        },
    }


class ChangeEnsembleTests(unittest.TestCase):
    def test_majority_rule_and_fallbacks(self):
        self.assertEqual(ensemble.majority_decision("Modified", "Reduced", "Reduced"),
                         ("Reduced", "majority_overrides_llm"))
        self.assertEqual(ensemble.majority_decision("Modified", "Reduced", "Unchanged"),
                         ("Modified", "three_way_tie_fallback"))
        self.assertEqual(ensemble.majority_decision("Modified", None, "Reduced"),
                         ("Modified", "missing_vote_fallback"))
        self.assertEqual(ensemble.majority_decision("Modified", "Modified", "Reduced"),
                         ("Modified", "majority_supports_llm"))
        self.assertEqual(ensemble.majority_decision("Modified", "Relocated", "Relocated"),
                         ("Modified", "unsupported_label_fallback"))

    def test_pairwise_improvements_and_harms(self):
        cases = [
            ("M1", "Expanded", "Modified", "Expanded", "Expanded"),
            ("M2", "Modified", "Modified", "Reduced", "Reduced"),
            ("M3", "Reworded", "Modified", "Reworded", "Reworded"),
            ("M4", "Reworded", "Reworded", "Modified", "Modified"),
            ("M5", "Unchanged", "Unchanged", "Reduced", "Modified"),
        ]
        reference_rows = [row(match_id, change=truth) for match_id, truth, *_ in cases]
        candidate_rows = [row(match_id, change=llm, lexical=lexical, semantic=semantic)
                          for match_id, _, llm, lexical, semantic in cases]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference_path = root / "reference.json"
            candidate_path = root / "candidate.json"
            for path, rows in ((reference_path, reference_rows), (candidate_path, candidate_rows)):
                path.write_text(json.dumps({"run_complete": True, "company": "amd",
                                            "previous_year": "2023", "current_year": "2024",
                                            "alignments": rows}), encoding="utf-8")
            result = ensemble.evaluate_pair(reference_path, candidate_path)
            baseline = baseline_evaluator.evaluate_pair(reference_path, candidate_path)

        self.assertEqual(result["compared"], 5)
        self.assertEqual(result["vote_paths"]["majority_overrides_llm"], 4)
        self.assertEqual(result["vote_paths"]["three_way_tie_fallback"], 1)
        self.assertEqual(result["paired_exact"]["majority_improved"], 2)
        self.assertEqual(result["paired_exact"]["majority_harmed"], 2)
        self.assertEqual(result["paired_binary"]["majority_improved"], 1)
        self.assertEqual(result["paired_binary"]["majority_harmed"], 1)
        for reference in ensemble.CHANGE_LABELS:
            for candidate in ensemble.CHANGE_LABELS:
                self.assertEqual(
                    result["llm_only"]["confusion_matrix"][reference].get(candidate, 0),
                    baseline["change_analysis"]["model_rows"]["confusion_matrix"][reference][candidate],
                )

    def test_mismatched_alignment_evidence_is_rejected(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left.json"
            right = root / "right.json"
            for path, evidence in ((left, []), (right, ["different"])):
                entry = row("M1", change="Modified")
                entry["evidence"] = evidence
                path.write_text(json.dumps({"run_complete": True, "company": "amd",
                                            "previous_year": "2023", "current_year": "2024",
                                            "alignments": [entry]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "membership changed"):
                ensemble.evaluate_pair(left, right)

    def test_stale_existing_llm_report_is_flagged(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "amd" / "2023-2024" / "agreement.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "comparison": {"reference_file": "reference.json", "candidate_file": "candidate.json"},
                "change_analysis": {"model_rows": {"confusion_matrix": {
                    "Modified": {"Modified": 2},
                }}},
            }), encoding="utf-8")
            pair = {"company": "amd", "year_pair": "2023-2024",
                    "reference_file": "reference.json", "candidate_file": "candidate.json",
                    "llm_only": {"confusion_matrix": {"Modified": {"Modified": 1}}}}
            self.assertEqual(ensemble.stale_existing_reports([pair], root), ["AMD 2023-2024"])

    def test_report_inputs_use_recorded_reference_path(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "alternate_reference" / "qcom" / "2023-2024" / "alignments.json"
            candidate = root / "candidate" / "qcom" / "2023-2024" / "alignments.json"
            for path in (reference, candidate):
                path.parent.mkdir(parents=True)
                path.write_text("{}", encoding="utf-8")
            report = root / "evaluation" / "qcom" / "2023-2024" / "agreement.json"
            report.parent.mkdir(parents=True)
            report.write_text(json.dumps({"comparison": {
                "company": "qcom", "previous_year": "2023", "current_year": "2024",
                "reference_file": str(reference), "candidate_file": str(candidate),
            }}), encoding="utf-8")
            args = ensemble.parse_args(["--evaluation-root", str(root / "evaluation"),
                                        "--ticker", "qcom"])
            self.assertEqual(ensemble.input_paths(args), [(reference, candidate)])


if __name__ == "__main__":
    unittest.main()
