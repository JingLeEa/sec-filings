"""Offline tests for the disclosure and alignment loaders (no database needed)."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alignments = load("load_alignments_to_postgres")
disclosures = load("load_disclosures_to_postgres")

ALIGNMENT = {
    "match_id": "amd_2024_2025_M0005", "previous_ids": ["amd_2024_1A_D061"], "current_ids": ["amd_2025_1A_D086", "amd_2025_1A_D087"],
    "relationship": "one_to_many", "status": "ai_verified", "explanation": "x", "evidence": [{"disclosure_id": "amd_2024_1A_D061", "sentences": []}],
    "review_reasons": [], "verifier_review_reason": "", "match_method": "llm",
    "previous_disclosures": [{"disclosure_id": "amd_2024_1A_D061", "item": "1A", "section": "Risk Factors", "taxonomy": "Technology & AI", "summary": "s", "extraction_status": "source_validated"}],
    "current_disclosures": [{"disclosure_id": "amd_2025_1A_D086", "item": "1A", "section": "Risk Factors", "taxonomy": "Technology & AI", "summary": "s", "extraction_status": "source_validated"}],
    "change_analysis": {"status": "not_started", "lexical": None, "semantic": None, "llm": None, "final_taxonomy": None},
    "exact_match": {"policy": "p"},
}


class AlignmentTests(unittest.TestCase):
    def test_alignment_row_keeps_unknown_fields_in_extra(self):
        row = alignments.alignment_row(ALIGNMENT, 7, "alignments")
        self.assertEqual(row["run_id"], 7)
        self.assertEqual(json.loads(row["extra"]), {"exact_match": {"policy": "p"}})
        self.assertIsNone(row["grouping"])
        self.assertEqual(json.loads(row["change_analysis"])["status"], "not_started")

    def test_member_rows_one_per_id_and_side_with_metadata_when_known(self):
        rows = alignments.member_rows(ALIGNMENT, 7)
        self.assertEqual([(r["side"], r["disclosure_id"]) for r in rows],
                         [("previous", "amd_2024_1A_D061"), ("current", "amd_2025_1A_D086"), ("current", "amd_2025_1A_D087")])
        self.assertEqual(rows[0]["taxonomy"], "Technology & AI")
        self.assertIsNone(rows[2]["taxonomy"])      # no metadata supplied for D087

    def test_validate_report_rejects_wrong_schema_and_scope(self):
        report = {"schema_version": "7", "company": "amd", "previous_year": "2023", "current_year": "2024",
                  "alignment_count": 0, "alignments": []}
        problems = alignments.validate_report(report, "alignments", "amd", 2024, 2025)
        self.assertEqual(len(problems), 2)


class DisclosureTests(unittest.TestCase):
    def test_disclosure_row(self):
        record = {"disclosure_id": "amd_2025_1A_D001", "item": "1A", "section": "Risk Factors", "sections": ["Risk Factors"],
                  "topic": "t", "summary": "s", "content": "c", "taxonomy": "Technology & AI", "sources": [{"a": 1}], "verification": {}, "future": 1}
        row = disclosures.disclosure_row(record, 3, "review_candidate")
        self.assertEqual(row["review_status"], "review_candidate")
        self.assertEqual(json.loads(row["extra"]), {"future": 1})

    def test_validate_report(self):
        good = {"schema_version": "3", "company": "amd", "fiscal_year": "2025"}
        self.assertEqual(disclosures.validate_report(good, "disclosures.json", "amd", 2025), [])
        self.assertEqual(len(disclosures.validate_report(dict(good, schema_version="2"), "disclosures.json", "amd", 2025)), 1)


if __name__ == "__main__":
    unittest.main()
