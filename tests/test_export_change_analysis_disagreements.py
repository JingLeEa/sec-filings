"""Markdown export of LLM change-taxonomy disagreements without API calls."""

import importlib.util
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export_change_analysis_disagreements.py"
SPEC = importlib.util.spec_from_file_location("export_change_analysis_disagreements", SCRIPT)
export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(export)


def alignment(match_id, status, previous_ids, current_ids, label, *, evidence=None):
    return {
        "match_id": match_id, "status": status,
        "relationship": "one_to_one" if previous_ids and current_ids else "previous_only",
        "previous_ids": previous_ids, "current_ids": current_ids,
        "previous_disclosures": [{"disclosure_id": key, "item": "1A", "section": "Risks",
                                  "summary": f"Summary for {key}"} for key in previous_ids],
        "current_disclosures": [{"disclosure_id": key, "item": "1A", "section": "Risks",
                                 "summary": f"Summary for {key}"} for key in current_ids],
        "evidence": evidence or [],
        "change_analysis": {"status": "completed" if label else "not_started", "llm": label},
        "materiality_analysis": {"materiality": "No"},
    }


class ChangeAnalysisDisagreementExportTests(unittest.TestCase):
    def test_default_directories(self):
        args = export.parse_args(["--ticker", "amd"])
        self.assertEqual(args.reference_root, Path("data/alignments_for_eval_soclaas"))
        self.assertEqual(args.output_root, Path("data/evaluation_soclaas"))
        self.assertEqual(args.model, "DeepSeek")

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "reference" / "amd" / "2023-2024" / "alignments.json"
        self.candidate = self.root / "candidate" / "amd" / "2023-2024" / "alignments.json"
        citation = [{"disclosure_id": "old_3", "sentences": [
            {"sentence_id": "S3", "text": "Fallback cited text."}]}]
        reference_rows = [
            alignment("M1", "ai_verified", ["old_1"], ["new_1"], "Modified"),
            alignment("M2", "ai_verified", ["old_2"], ["new_2"], "Unchanged"),
            alignment("M3", "ai_verified", ["old_3"], ["new_3"], "Expanded", evidence=citation),
            alignment("M4", "unmatched", ["old_4"], [], "Removed"),
            alignment("M5", "ai_verified", ["old_5"], ["new_5"], None),
        ]
        candidate_rows = [
            alignment("M1", "ai_verified", ["old_1"], ["new_1"], "Reworded"),
            alignment("M2", "ai_verified", ["old_2"], ["new_2"], "Unchanged"),
            alignment("M3", "ai_verified", ["old_3"], ["new_3"], "Modified", evidence=citation),
            alignment("M4", "unmatched", ["old_4"], [], "New"),
            alignment("M5", "ai_verified", ["old_5"], ["new_5"], "Modified"),
        ]
        for path, rows in ((self.reference, reference_rows), (self.candidate, candidate_rows)):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"company": "amd", "previous_year": "2023",
                                        "current_year": "2024", "run_complete": True,
                                        "alignments": rows}), encoding="utf-8")
        self.disclosures = self.root / "disclosures"
        for year, ids in (("2023", ["old_1", "old_2"]), ("2024", ["new_1", "new_2"])):
            folder = self.disclosures / "amd" / year
            folder.mkdir(parents=True)
            (folder / "disclosures.json").write_text(json.dumps({
                "company": "amd", "fiscal_year": year,
                "disclosures": [{"disclosure_id": key, "item": "1A", "section": "Risks",
                                 "content": f"Complete selected disclosure text for {key}."} for key in ids],
            }), encoding="utf-8")

    def test_export_only_completed_llm_differences_with_source_text(self):
        source_bytes = self.reference.read_bytes(), self.candidate.read_bytes()
        output_root = self.root / "output"
        with redirect_stdout(StringIO()) as captured:
            result = export.main(["--ticker", "amd", "--year-pair", "2023-2024",
                                  "--model", "OpenAI",
                                  "--reference-root", str(self.root / "reference"),
                                  "--candidate-root", str(self.root / "candidate"),
                                  "--disclosures-dir", str(self.disclosures),
                                  "--output-root", str(output_root)])
        self.assertEqual(result, 0)
        self.assertIn("Exported 2 LLM change-analysis disagreements", captured.getvalue())
        markdown = (output_root / "amd" / "2023-2024" /
                    "change_analysis_disagreements.md").read_text()
        self.assertIn("Complete selected disclosure text for old_1.", markdown)
        self.assertIn("Complete selected disclosure text for new_1.", markdown)
        self.assertIn("Fallback cited text.", markdown)
        self.assertIn("alignment citations (fallback)", markdown)
        self.assertIn("**OpenAI LLM: Modified**", markdown)
        self.assertIn("**Qwen LLM: Reworded**", markdown)
        self.assertIn("| Different |", markdown)
        self.assertIn("| Same |", markdown)
        self.assertNotIn("M2", markdown)
        self.assertNotIn("M4", markdown)
        self.assertNotIn("M5", markdown)
        self.assertEqual((self.reference.read_bytes(), self.candidate.read_bytes()), source_bytes)

    def test_rejects_different_alignment_membership(self):
        document = json.loads(self.candidate.read_text(encoding="utf-8"))
        document["alignments"][0]["current_ids"] = ["other"]
        self.candidate.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not comparable"):
            export.render_report("amd", "2023-2024", self.reference, self.candidate, {})

    def test_no_disagreements_is_explicit(self):
        document = json.loads(self.candidate.read_text(encoding="utf-8"))
        reference = json.loads(self.reference.read_text(encoding="utf-8"))
        for candidate_row, reference_row in zip(document["alignments"], reference["alignments"]):
            candidate_row["change_analysis"] = reference_row["change_analysis"]
        self.candidate.write_text(json.dumps(document), encoding="utf-8")
        markdown, count = export.render_report("amd", "2023-2024", self.reference, self.candidate, {})
        self.assertEqual(count, 0)
        self.assertIn("No completed LLM change-taxonomy disagreements found.", markdown)

    def test_unmapped_substantive_class_is_not_called_agreement(self):
        document = json.loads(self.candidate.read_text(encoding="utf-8"))
        document["alignments"][0]["change_analysis"]["llm"] = "New"
        self.candidate.write_text(json.dumps(document), encoding="utf-8")
        markdown, count = export.render_report("amd", "2023-2024", self.reference, self.candidate, {})
        self.assertEqual(count, 2)
        self.assertIn("| New | Modified | n/a |", markdown)


if __name__ == "__main__":
    unittest.main()
