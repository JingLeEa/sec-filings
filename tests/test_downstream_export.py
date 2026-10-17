"""Normalized joins, JSON output, missing-member handling and read-only access."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sec_disclosure.storage import exports


def disclosure(number=1, year=2023, status="accepted"):
    return {"disclosure_id": f"amd_{year}_7_D{number:03d}", "filing_id": 7, "item": "7",
            "section": "Results", "sections": ["Results"], "topic": "Technology", "summary": "Extracted summary",
            "content": f"Résumé – 原文 {number}", "taxonomy": "Technology & AI", "review_status": status,
            "sources": [{"paragraph_id": f"amd_{year}_7_P001", "sentences": []}],
            "verification": {"status": "source_validated"},
            "extra": {"related_disclosures": [], "content": "must not override normalized content"}}


def joined_reports():
    files = {name: {"export_schema_version": "1", "company": "amd", "fiscal_year": "2023",
                    "disclosure_count": 1, "disclosures": [exports._disclosure(disclosure(), "amd", 2023)]}
             for name in exports.REPORT_FILES["disclosures"]}
    return {("disclosures", 2023, 2023): files}


class DownstreamExportTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "export"
        self.reports = joined_reports()

    def test_filing_join_partitions_records_and_retains_evidence_and_extra_fields(self):
        cur = MagicMock()
        cur.fetchone.return_value = {"filing_id": 7}
        cur.fetchall.return_value = [disclosure(), disclosure(2, status="review_candidate")]
        reports = exports._disclosure_reports(cur, "amd", 2023)
        accepted = reports["disclosures.json"]["disclosures"]
        review = reports["review_candidates.json"]["disclosures"]
        self.assertEqual([row["review_status"] for row in accepted + review], ["accepted", "review_candidate"])
        self.assertEqual(accepted[0]["content"], disclosure()["content"])
        self.assertEqual(accepted[0]["sources"], disclosure()["sources"])
        self.assertEqual(accepted[0]["related_disclosures"], [])
        self.assertEqual(accepted[0]["fiscal_year"], "2023")
        self.assertNotIn("filing_id", accepted[0])
        self.assertNotIn("extra", accepted[0])
        self.assertIn("JOIN filings", cur.execute.call_args_list[1].args[0])
        cur.fetchall.return_value = []
        self.assertEqual(exports._disclosure_reports(cur, "amd", 2023)["disclosures.json"]["disclosures"], [])
        cur.fetchone.return_value = None
        with self.assertRaisesRegex(exports.DownstreamExportError, "No 10-K filing"):
            exports._disclosure_reports(cur, "amd", 2023)

    def test_alignment_join_preserves_group_and_review_rows_and_full_member_content(self):
        cur = MagicMock()
        cur.fetchone.return_value = {"run_id": 7, "schema_version": "8", "run_complete": True,
                                    "comparison_scope": "same_item", "note": None}
        decisions = [{"match_id": "M1", "source_file": "alignments", "run_id": 7,
                      "status": "ai_verified", "relationship": "many_to_one", "evidence": [{"sentences": []}],
                      "grouping": {"source_alignments": []}, "change_analysis": {"llm": "Modified"},
                      "extra": {"future": True}},
                     {"match_id": "M2", "source_file": "needs_review", "status": "needs_review",
                      "relationship": "previous_only", "change_analysis": {"status": "not_started"}, "extra": {}}]
        members = []
        for match_id, side, year, number in (("M1", "previous", 2023, 1), ("M1", "previous", 2023, 3),
                                             ("M1", "current", 2024, 1), ("M2", "previous", 2023, 2)):
            record = disclosure(number, year)
            members.append({"match_id": match_id, "side": side, "company": "amd", "fiscal_year": year,
                            "form_type": "10-K", "disclosure": record,
                            "member": {"disclosure_id": record["disclosure_id"], "summary": "Alignment summary",
                                       "taxonomy": None, "extraction_status": "source_validated"}})
        cur.fetchall.side_effect = [decisions, members]
        reports = exports._alignment_reports(cur, "amd", 2023, 2024)
        group = reports["alignments.json"]["alignments"][0]
        self.assertEqual(group["previous_ids"], ["amd_2023_7_D001", "amd_2023_7_D003"])
        self.assertEqual(group["current_ids"], ["amd_2024_7_D001"])
        self.assertEqual(group["previous_disclosures"][0]["content"], disclosure()["content"])
        self.assertEqual(group["previous_disclosures"][0]["summary"], "Alignment summary")
        self.assertEqual(group["previous_disclosures"][0]["taxonomy"], "Technology & AI")
        self.assertEqual(group["change_analysis"], {"llm": "Modified"})
        self.assertEqual(group["grouping"], {"source_alignments": []})
        self.assertTrue(group["future"])
        review = reports["needs_review.json"]["alignments"][0]
        self.assertEqual(review["current_disclosures"], [])
        self.assertEqual(reports["needs_review.json"]["alignment_count"], 1)
        self.assertEqual(reports["alignments.json"]["overall_counts"], {"ai_verified": 1, "needs_review": 1})
        # LEFT JOINs retain a missing or cross-filing member so it produces an error.
        for change in ({"disclosure": None}, {"fiscal_year": 2025}, {"company": "other"}):
            with self.subTest(change=change):
                cur.fetchall.side_effect = [decisions, [{**members[0], **change}, *members[1:]]]
                with self.assertRaisesRegex(exports.DownstreamExportError, "Import disclosures for both years"):
                    exports._alignment_reports(cur, "amd", 2023, 2024)
        cur.fetchone.return_value = None
        with self.assertRaisesRegex(exports.DownstreamExportError, "No alignment run"):
            exports._alignment_reports(cur, "amd", 2023, 2024)
        cur.fetchone.return_value = {"run_complete": False}
        with self.assertRaisesRegex(exports.DownstreamExportError, "incomplete"):
            exports._alignment_reports(cur, "amd", 2023, 2024)

    def test_writes_joined_json_and_exposes_python_api_without_source_files(self):
        with patch.object(exports, "_read_reports", return_value=self.reports) as reader:
            paths = exports.export_reports("AMD", [2023], self.output)
            reader.assert_called_once_with("amd", [("disclosures", 2023, 2023)], None)
            self.assertEqual(exports.get_disclosure_reports("amd", 2023), self.reports[("disclosures", 2023, 2023)])
        for path in paths:
            self.assertEqual(json.loads(path.read_bytes()), self.reports[("disclosures", 2023, 2023)][path.name])
            self.assertEqual(path.parent, self.output / "disclosures/amd/2023")
        with patch.object(exports, "_read_reports", return_value={("alignments", 2023, 2024): {"example": True}}):
            self.assertEqual(exports.get_alignment_reports("amd", 2023, 2024), {"example": True})

    def test_different_existing_output_prevents_all_writes_and_overwrite_is_explicit(self):
        target = self.output / "disclosures/amd/2023/review_candidates.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"different")
        with patch.object(exports, "_read_reports", return_value=self.reports):
            with self.assertRaisesRegex(exports.DownstreamExportError, "Different output"):
                exports.export_reports("amd", [2023], self.output)
            self.assertFalse(target.with_name("disclosures.json").exists())
            exports.export_reports("amd", [2023], self.output, overwrite=True)
            before = target.stat().st_mtime_ns
            exports.export_reports("amd", [2023], self.output)
            self.assertEqual(target.stat().st_mtime_ns, before)
        self.assertEqual(json.loads(target.read_bytes()), self.reports[("disclosures", 2023, 2023)][target.name])

    def test_missing_scope_produces_no_partial_export(self):
        with patch.object(exports, "_read_reports", side_effect=exports.DownstreamExportError("Missing alignment run")):
            with self.assertRaisesRegex(exports.DownstreamExportError, "Missing alignment"):
                exports.export_reports("amd", [2023, 2024], self.output)
        self.assertFalse(self.output.exists())

    def test_reads_in_one_repeatable_read_read_only_transaction_and_sanitizes_connection_errors(self):
        conn, cur = MagicMock(), MagicMock()
        conn.__enter__.return_value = conn
        cur.__enter__.return_value = cur
        conn.cursor.return_value = cur
        driver = SimpleNamespace(connect=MagicMock(return_value=conn),
                                 IsolationLevel=SimpleNamespace(REPEATABLE_READ="repeatable"))
        with patch.dict(sys.modules, {"psycopg": driver, "psycopg.rows": SimpleNamespace(dict_row="dict")}), \
             patch.object(exports, "connection_arguments", return_value={"dbname": "test"}), \
             patch.object(exports, "_disclosure_reports", return_value=self.reports[("disclosures", 2023, 2023)]):
            self.assertEqual(exports._read_reports("amd", list(self.reports), None), self.reports)
            self.assertTrue(conn.read_only)
            self.assertEqual(conn.isolation_level, "repeatable")
            driver.connect.assert_called_once_with(dbname="test", row_factory="dict")
            driver.connect.side_effect = RuntimeError("postgresql://sec:private-password@example/db")
            with self.assertRaises(exports.DownstreamExportError) as error:
                exports._read_reports("amd", list(self.reports), None)
            self.assertNotIn("private-password", str(error.exception))

    def test_selected_stages_and_year_pairs_are_validated_before_connecting(self):
        self.assertEqual(exports._scopes([2025, 2023, 2024, 2024], "alignments"),
                         [("alignments", 2023, 2024), ("alignments", 2024, 2025)])
        with patch.object(exports, "_read_reports") as reader:
            for company, years, stage in (("../amd", [2023], "all"), ("amd", [], "all"),
                                           ("amd", [2023], "alignments"), ("amd", [0], "all")):
                with self.subTest(company=company, years=years, stage=stage), self.assertRaises(ValueError):
                    exports.export_reports(company, years, self.output, stage=stage)
            with self.assertRaises(ValueError):
                exports.get_alignment_reports("amd", 2024, 2023)
            reader.assert_not_called()

    def test_cli_passes_settings_and_prints_actionable_errors(self):
        with patch.object(exports, "export_reports", return_value=[]) as export, redirect_stdout(io.StringIO()):
            self.assertEqual(exports.main(["--company", "amd", "--years", "2023", "2024",
                                           "--stage", "alignments", "--output-dir", str(self.output),
                                           "--env-file", "test.env", "--overwrite"]), 0)
        export.assert_called_once_with("amd", [2023, 2024], self.output, stage="alignments",
                                       env_file=Path("test.env"), overwrite=True)
        stderr = io.StringIO()
        with patch.object(exports, "export_reports", side_effect=exports.DownstreamExportError("Import its alignments first")), \
             redirect_stderr(stderr):
            self.assertEqual(exports.main(["--company", "amd", "--years", "2023", "2024"]), 1)
        self.assertIn("Import its alignments first", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
