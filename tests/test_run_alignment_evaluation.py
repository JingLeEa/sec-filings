"""The combined evaluation workflow is tested without contacting model APIs."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_alignment_evaluation.py"
SPEC = importlib.util.spec_from_file_location("run_alignment_evaluation", SCRIPT)
workflow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workflow)


class AlignmentEvaluationWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source_root = self.root / "source"
        self.alignment_root = self.root / "prepared"
        self.evaluation_root = self.root / "evaluated"
        self.source = self.source_root / "amd" / "2023-2024" / "alignments.json"
        self.target = self.alignment_root / "amd" / "2023-2024" / "alignments.json"
        self.source.parent.mkdir(parents=True)
        self.source.write_text(json.dumps({
            "company": "amd", "previous_year": "2023", "current_year": "2024",
            "run_complete": True,
            "included_statuses": ["ai_verified", "auto_matched", "unmatched"],
            "alignments": [{
                "match_id": "M1", "status": "ai_verified", "previous_ids": ["old_1"],
                "current_ids": ["new_1"], "explanation": "Same risk, wider scope.",
                "change_analysis": {"status": "completed", "llm": "Expanded"},
                "materiality_analysis": {"materiality": "Yes", "reason": "Source prediction"},
            }],
        }), encoding="utf-8")
        self.base = ["--ticker", "AMD", "--previous-year", "2023",
                     "--current-year", "2024", "--input-root", str(self.source_root),
                     "--alignment-root", str(self.alignment_root),
                     "--evaluation-root", str(self.evaluation_root)]

    def complete(self, argv):
        doc = json.loads(self.target.read_text(encoding="utf-8"))
        row = doc["alignments"][0]
        row["change_analysis"].update(status="completed", llm="Modified")
        row["materiality_analysis"].update(materiality="No", materiality_score=0.1,
                                            confidence_score=0.9, reason="No meaningful change")
        self.target.write_text(json.dumps(doc), encoding="utf-8")
        return 0

    def test_defaults_and_provider_roots(self):
        args = workflow.parse_args(["--ticker", "AMD", "--previous-year", "2023",
                                    "--current-year", "2024"])
        self.assertEqual((args.llm, args.ticker), ("soclaas", "amd"))
        self.assertEqual(workflow.provider_paths(args), (
            Path("data/alignments_for_eval_soclaas"), Path("data/evaluation_soclaas")))
        self.assertIn("x-test-2", workflow.classifier_argv(args, Path("prepared")))
        args.max_invalid_retries = 3
        self.assertIn("--max-invalid-retries", workflow.classifier_argv(args, Path("prepared")))
        for provider in ("openai_batch", "openai_standard"):
            args.llm = provider
            self.assertEqual(workflow.provider_paths(args), (
                Path(f"data/alignments_for_eval_{provider}"), Path(f"data/evaluation_{provider}")))
        args.llm = "openai_batch"
        args.max_invalid_retries = None
        self.assertNotIn("--max-invalid-retries", workflow.classifier_argv(args, Path("prepared")))

    def test_prepares_classifies_evaluates_and_resumes_without_overwriting(self):
        calls = []

        def classify(argv):
            calls.append(argv)
            self.assertIsNone(json.loads(self.target.read_text(encoding="utf-8"))
                              ["alignments"][0]["materiality_analysis"]["materiality"])
            return self.complete(argv)

        def evaluate(argv):
            calls.append(argv)
            return 0

        self.assertEqual(workflow.main(self.base, classifier=classify, evaluator=evaluate), 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("--reference-root", calls[1])
        self.assertEqual(calls[1][calls[1].index("--candidate-root") + 1], str(self.source_root))
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "x-test-2")
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))
                         ["alignments"][0]["change_analysis"]["llm"], "Modified")
        self.assertEqual(workflow.main(self.base, classifier=lambda argv: 0,
                                       evaluator=evaluate), 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(json.loads(self.target.read_text(encoding="utf-8"))
                         ["alignments"][0]["materiality_analysis"]["materiality"], "No")

    def test_partial_or_failed_classification_defers_evaluation(self):
        calls = []
        evaluator = lambda argv: calls.append(argv) or 0
        self.assertEqual(workflow.main(self.base + ["--max-new-rows", "1"],
                                       classifier=lambda argv: 0, evaluator=evaluator), 0)
        self.assertTrue(self.target.exists())
        self.assertEqual(calls, [])
        self.assertEqual(workflow.main(self.base, classifier=lambda argv: 2,
                                       evaluator=evaluator), 2)
        self.assertEqual(calls, [])

    def test_openai_options_and_active_batch(self):
        args = self.base + ["--llm", "openai_batch", "--model", "gpt-example",
                            "--reasoning-effort", "medium", "--batch-size", "4",
                            "--max-output-tokens", "12000", "--submit-only",
                            "--input-usd-per-million", "1", "--output-usd-per-million", "2"]
        calls = []

        def classify(argv):
            calls.append(argv)
            self.assertIn("--batch-input-usd-per-million", argv)
            self.assertIn("--batch-output-usd-per-million", argv)
            self.assertIn("--submit-only", argv)
            self.complete(argv)
            state = self.target.parent / "openai_eval"
            state.mkdir()
            (state / "active_batch.json").write_text("{}", encoding="utf-8")
            return 0

        self.assertEqual(workflow.main(args, classifier=classify,
                                       evaluator=lambda argv: calls.append(argv)), 0)
        self.assertEqual(len(calls), 1)

    def test_standard_model_and_configured_label(self):
        calls = []

        def classify(argv):
            calls.append(argv)
            self.assertIn("--workers", argv)
            self.assertIn("--input-usd-per-million", argv)
            self.complete(argv)
            state = self.target.parent / "openai_eval"
            state.mkdir()
            (state / "config.json").write_text('{"model":"gpt-example"}', encoding="utf-8")
            return 0

        command = self.base + ["--llm", "openai_standard", "--model", "gpt-example",
                               "--workers", "2", "--input-usd-per-million", "1",
                               "--output-usd-per-million", "2"]
        self.assertEqual(workflow.main(command, classifier=classify,
                                       evaluator=lambda argv: calls.append(argv) or 0), 0)
        self.assertEqual(calls[1][calls[1].index("--model") + 1], "gpt-example")

    def test_changed_source_is_rejected_without_destroying_results(self):
        self.assertEqual(workflow.main(self.base, classifier=self.complete,
                                       evaluator=lambda argv: 0), 0)
        original = self.target.read_bytes()
        source = json.loads(self.source.read_text(encoding="utf-8"))
        source["alignments"][0]["explanation"] = "Changed source"
        self.source.write_text(json.dumps(source), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Source alignments changed"):
            workflow.main(self.base, classifier=lambda argv: 0, evaluator=lambda argv: 0)
        self.assertEqual(self.target.read_bytes(), original)

    def test_cli_help(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True,
                                text=True, check=True)
        self.assertIn("--llm", result.stdout)
        self.assertIn("--model", result.stdout)


if __name__ == "__main__":
    unittest.main()
