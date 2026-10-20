"""Evaluation inputs retain alignments while excluding prior predictions."""

import importlib.util
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_alignments_for_eval.py"
SPEC = importlib.util.spec_from_file_location("prepare_alignments_for_eval", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class PrepareAlignmentsForEvalTests(unittest.TestCase):
    def test_default_output_root_is_under_data(self):
        args = prepare.parse_args(["--ticker", "amd"])
        self.assertEqual(args.output_root, Path("data/alignments_for_eval_soclaas"))

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source" / "amd" / "2023-2024" / "alignments.json"
        self.source.parent.mkdir(parents=True)
        self.document = {
            "company": "amd", "previous_year": "2023", "current_year": "2024",
            "run_complete": True,
            "included_statuses": ["ai_verified", "auto_matched", "unmatched"],
            "change_analysis_summary": {"taxonomy_counts": {"Modified": 1}},
            "alignments": [{
                "match_id": "amd_2023_2024_M0001", "status": "ai_verified",
                "relationship": "one_to_one", "previous_ids": ["old"], "current_ids": ["new"],
                "explanation": "Same topic", "evidence": [{"text": "Source sentence"}],
                "grouping": {"source_alignments": [{"match_id": "old_group", "materiality_analysis": {"materiality": "Yes"}}]},
                "change_analysis": {"status": "completed", "llm": "Modified", "lexical": {"score": 0.8}},
                "materiality_analysis": {
                    "materiality": "Yes", "reason": "Old answer", "key_change": "Old change",
                },
            }],
        }
        self.source.write_text(json.dumps(self.document), encoding="utf-8")

    def run_script(self, *extra):
        with redirect_stdout(StringIO()):
            return prepare.main(["--ticker", "amd", "--input-root", str(self.root / "source"),
                                 "--output-root", str(self.root / "alignments_for_eval"), *extra])

    def test_scrubs_predictions_without_changing_source_or_alignment_decisions(self):
        self.assertEqual(self.run_script(), 0)
        output = self.root / "alignments_for_eval" / "amd" / "2023-2024" / "alignments.json"
        result = json.loads(output.read_text(encoding="utf-8"))
        row = result["alignments"][0]
        self.assertNotIn("change_analysis_summary", result)
        self.assertEqual(row["materiality_analysis"], prepare.EMPTY_MATERIALITY_ANALYSIS)
        self.assertNotIn("key_change", row["materiality_analysis"])
        self.assertNotIn("materiality_analysis", row["grouping"]["source_alignments"][0])
        self.assertEqual(row["change_analysis"], prepare.EMPTY_CHANGE_ANALYSIS)
        self.assertEqual(row["previous_ids"], ["old"])
        self.assertEqual(row["evidence"], [{"text": "Source sentence"}])
        self.assertEqual(json.loads(self.source.read_text(encoding="utf-8")), self.document)
        self.assertEqual(self.run_script(), 0)

    def test_input_dir_selects_company_directory(self):
        with redirect_stdout(StringIO()):
            code = prepare.main(["--ticker", "amd", "--input-dir", str(self.root / "source"),
                                 "--output-root", str(self.root / "alignments_for_eval")])
        self.assertEqual(code, 0)
        self.assertTrue((self.root / "alignments_for_eval" / "amd" / "2023-2024" / "alignments.json").is_file())

    def test_existing_different_file_requires_overwrite(self):
        self.run_script()
        self.document["alignments"][0]["explanation"] = "Updated alignment"
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "--overwrite"):
            self.run_script()
        self.assertEqual(self.run_script("--overwrite"), 0)

    def test_rejects_incomplete_alignment(self):
        self.document["run_complete"] = False
        self.source.write_text(json.dumps(self.document), encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "completed alignment"):
            self.run_script()

    def test_direct_source_file_uses_report_identity(self):
        custom_source = self.root / "custom_alignment_snapshot.json"
        custom_source.write_text(json.dumps(self.document), encoding="utf-8")
        with redirect_stdout(StringIO()):
            code = prepare.main(["--source-file", str(custom_source),
                                 "--output-root", str(self.root / "alignments_for_eval")])
        self.assertEqual(code, 0)
        output = self.root / "alignments_for_eval" / "amd" / "2023-2024" / "alignments.json"
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["alignments"][0]["change_analysis"],
                         prepare.EMPTY_CHANGE_ANALYSIS)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["alignments"][0]["materiality_analysis"],
                         prepare.EMPTY_MATERIALITY_ANALYSIS)
        self.assertEqual(json.loads(custom_source.read_text(encoding="utf-8")), self.document)

    def test_direct_source_cannot_overwrite_itself(self):
        target = self.root / "alignments_for_eval" / "amd" / "2023-2024" / "alignments.json"
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps(self.document), encoding="utf-8")
        with redirect_stdout(StringIO()), self.assertRaisesRegex(SystemExit, "must differ"):
            prepare.main(["--source-file", str(target),
                          "--output-root", str(self.root / "alignments_for_eval")])


if __name__ == "__main__":
    unittest.main()
