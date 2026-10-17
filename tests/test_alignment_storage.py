"""Snapshot validation and opt-in Docker checks for automatic alignment storage."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import uuid

from sec_disclosure.storage import alignments as storage


ROOT = Path(__file__).resolve().parents[1]


def write_reports(folder):
    finalized = {"match_id": "amd_2023_2024_M0001", "status": "ai_verified", "relationship": "one_to_one",
                 "previous_ids": ["amd_2023_7_D001"], "current_ids": ["amd_2024_7_D001"],
                 "change_analysis": {"status": "completed", "lexical": None, "semantic": None,
                                     "llm": "Modified", "final_taxonomy": None},
                 "evidence": [{"disclosure_id": "amd_2023_7_D001", "sentences": []}],
                 "grouping": {"source_alignments": [{"match_method": "llm"}]}, "future": {"keep": True}}
    review = {"match_id": "amd_2023_2024_M0002", "status": "needs_review", "relationship": "previous_only",
              "previous_ids": ["amd_2023_7_D002"], "current_ids": [], "review_reasons": ["uncertain_source"],
              "change_analysis": {"status": "not_started"}}
    common = {"schema_version": "8", "company": "amd", "previous_year": "2023", "current_year": "2024",
              "comparison_scope": "same_item", "run_complete": True, "error": None,
              "overall_counts": {"ai_verified": 1, "needs_review": 1}}
    for name, rows in (("alignments", [finalized]), ("needs_review", [review])):
        (folder / f"{name}.json").write_text(json.dumps({**common, "alignment_count": len(rows), "alignments": rows}))
    return finalized, review


class AlignmentStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.records = write_reports(self.folder)
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.cur = MagicMock()
        self.cur.__enter__.return_value = self.cur
        self.conn.cursor.return_value = self.cur
        self.cur.fetchone.side_effect = [(7,), (2,), (3,)]
        self.connect = MagicMock(return_value=self.conn)
        self.driver = SimpleNamespace(connect=self.connect)

    def store(self):
        with patch.dict(sys.modules, {"psycopg": self.driver}), \
             patch.object(storage, "connection_arguments", return_value={"dbname": "test"}):
            return storage.store_alignments(self.folder, "amd", 2023, 2024)

    def test_finalized_and_review_decisions_include_analysis_evidence_and_members(self):
        result = self.store()
        self.assertEqual((result.stored, result.finalized, result.review, result.members), (2, 1, 1, 3))
        rows = self.cur.executemany.call_args_list[0].args[1]
        self.assertEqual(json.loads(rows[0]["change_analysis"]), self.records[0]["change_analysis"])
        self.assertEqual(json.loads(rows[0]["grouping"]), self.records[0]["grouping"])
        self.assertEqual(json.loads(rows[0]["extra"]), {"future": {"keep": True}})
        self.assertEqual([row["source_file"] for row in rows], ["alignments", "needs_review"])
        self.assertEqual(self.conn.__exit__.call_args.args, (None, None, None))

    def test_partial_mismatched_and_duplicate_snapshots_never_connect(self):
        for change in ({"run_complete": False}, {"company": "mu"}, {"overall_counts": {}},
                       {"error": "unfinished change analysis"}):
            with self.subTest(change=change):
                write_reports(self.folder)
                path = self.folder / "needs_review.json"
                report = json.loads(path.read_text())
                report.update(change)
                path.write_text(json.dumps(report))
                with self.assertRaises(ValueError):
                    self.store()
        write_reports(self.folder)
        path = self.folder / "needs_review.json"
        report = json.loads(path.read_text())
        report["alignments"][0]["match_id"] = self.records[0]["match_id"]
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.store()
        self.connect.assert_not_called()

    def test_wrong_year_members_are_rejected_before_replacing_a_run(self):
        path = self.folder / "alignments.json"
        report = json.loads(path.read_text())
        report["alignments"][0]["current_ids"] = ["amd_2025_7_D001"]
        path.write_text(json.dumps(report))
        with self.assertRaisesRegex(ValueError, "cross-filing"):
            self.store()
        self.connect.assert_not_called()

    def test_insert_failure_and_count_mismatch_roll_back_without_exposing_credentials(self):
        self.cur.executemany.side_effect = RuntimeError("postgresql://sec:secret-password@localhost/db")
        with self.assertRaises(storage.AlignmentStorageError) as error:
            self.store()
        self.assertNotIn("secret-password", str(error.exception))
        self.assertIs(self.conn.__exit__.call_args.args[0], RuntimeError)
        self.cur.executemany.side_effect = None
        self.cur.fetchone.side_effect = [(7,), (1,), (3,)]
        with self.assertRaises(storage.AlignmentStorageError):
            self.store()
        self.assertIs(self.conn.__exit__.call_args.args[0], ValueError)

    def test_dry_run_does_not_connect_or_read_environment_file(self):
        with patch.object(storage, "connection_arguments") as settings, redirect_stdout(io.StringIO()):
            self.assertEqual(storage.main(["--company", "AMD", "--previous-year", "2023", "--current-year", "2024",
                                            "--dir", str(self.folder), "--env-file", "missing.env", "--dry-run"]), 0)
        settings.assert_not_called()


@unittest.skipUnless(os.environ.get("SEC_ALIGNMENT_DB_INTEGRATION") == "1", "Requires local Docker PostgreSQL")
class AlignmentPostgresTests(unittest.TestCase):
    @classmethod
    def sql(cls, statement, database=None):
        command = ["docker", "compose", "exec", "-T", "db", "sh", "-c",
                   'exec psql -X -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -A -t', "sh", database or cls.database]
        return subprocess.run(command, cwd=ROOT, input=statement, text=True, capture_output=True,
                              check=True, timeout=30).stdout.strip()

    @classmethod
    def setUpClass(cls):
        cls.database = "sec_alignment_test_" + uuid.uuid4().hex
        cls.sql(f'CREATE DATABASE "{cls.database}";', "postgres")
        cls.addClassCleanup(cls.sql, f'DROP DATABASE "{cls.database}";', "postgres")
        for name in ("01_schema.sql", "02_disclosures_alignments.sql"):
            cls.sql((ROOT / "db/init" / name).read_text())

    def setUp(self):
        environment = patch.dict(os.environ, {"POSTGRES_DB": self.database, "DATABASE_URL": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        write_reports(self.folder)
        self.sql("TRUNCATE filings, alignment_runs RESTART IDENTITY CASCADE;")

    def test_import_is_idempotent_and_late_failure_preserves_previous_snapshot(self):
        first = storage.store_alignments(self.folder, "amd", 2023, 2024)
        second = storage.store_alignments(self.folder, "amd", 2023, 2024)
        self.assertEqual(first.run_id, second.run_id)
        self.assertEqual(self.sql("SELECT count(*) FROM alignment_runs;"), "1")
        self.assertEqual(self.sql("SELECT count(*) FROM alignments;"), "2")
        self.assertEqual(self.sql("SELECT count(*) FROM alignment_members;"), "3")

        original = self.sql("SELECT change_analysis::text FROM alignments ORDER BY match_id;")
        mapper = storage.member_rows

        def invalid_member(*args):
            rows = mapper(*args)
            if rows:
                rows[0]["side"] = "invalid"
            return rows

        with patch.object(storage, "member_rows", side_effect=invalid_member), self.assertRaises(storage.AlignmentStorageError):
            storage.store_alignments(self.folder, "amd", 2023, 2024)
        self.assertEqual(self.sql("SELECT change_analysis::text FROM alignments ORDER BY match_id;"), original)
        self.assertEqual(self.sql("SELECT count(*) FROM alignment_members;"), "3")

    def test_export_joins_normalized_rows_after_sources_are_removed_and_reads_sql_updates(self):
        from sec_disclosure.storage import disclosures, exports

        for year in (2023, 2024):
            folder = self.folder / "inputs" / str(year)
            folder.mkdir(parents=True)
            records = [{"disclosure_id": f"amd_{year}_7_D{number:03d}", "item": "7",
                        "content": f"Résumé – 原文 {number}", "taxonomy": "Technology & AI",
                        "summary": "Disclosure summary", "section": None,
                        "sources": [{"paragraph_id": f"amd_{year}_7_P001", "sentences": []}],
                        "verification": {"status": "needs_review" if number == 2 else "source_validated"},
                        "related_disclosures": [{"disclosure_ids": [], "reason": "Example"}]}
                       for number in (3, 1, 2)]
            for name, rows in (("disclosures.json", records[:2]), ("review_candidates.json", records[2:])):
                report = {"schema_version": "4", "company": "amd", "fiscal_year": str(year),
                          "policies": {"not_needed": True}, "disclosures": rows}
                (folder / name).write_text(json.dumps(report, ensure_ascii=False))
            (folder / "token_usage.json").write_text('{"run_complete": true}')
            disclosures.store_disclosures(folder, "amd", year, replace=True)

        path = self.folder / "alignments.json"
        report = json.loads(path.read_text())
        report["alignments"][0].update(previous_ids=["amd_2023_7_D003", "amd_2023_7_D001"],
                                       relationship="many_to_one")
        path.write_text(json.dumps(report))
        storage.store_alignments(self.folder, "amd", 2023, 2024)
        self.assertEqual(self.sql("SELECT to_regclass('pipeline_json_artifacts');"), "")
        self.sql("UPDATE alignments SET change_analysis=jsonb_build_object('llm', 'Updated in SQL') "
                 "WHERE match_id='amd_2023_2024_M0001';")
        self.sql("UPDATE disclosures SET content='Updated disclosure text from SQL' "
                 "WHERE disclosure_id='amd_2023_7_D001';")
        shutil.rmtree(self.folder)

        reports = exports.get_disclosure_reports("amd", 2023)
        accepted = reports["disclosures.json"]["disclosures"]
        self.assertEqual([row["disclosure_id"] for row in accepted], ["amd_2023_7_D001", "amd_2023_7_D003"])
        self.assertEqual(accepted[0]["content"], "Updated disclosure text from SQL")
        self.assertTrue(accepted[0]["sources"])
        self.assertTrue(accepted[0]["related_disclosures"])
        self.assertEqual(reports["review_candidates.json"]["disclosures"][0]["review_status"], "review_candidate")
        self.assertNotIn("policies", reports["disclosures.json"])
        reports = exports.get_alignment_reports("amd", 2023, 2024)
        group = reports["alignments.json"]["alignments"][0]
        self.assertEqual(group["previous_ids"], ["amd_2023_7_D001", "amd_2023_7_D003"])
        self.assertEqual(group["previous_disclosures"][0]["content"], "Updated disclosure text from SQL")
        self.assertEqual(group["current_disclosures"][0]["content"], "Résumé – 原文 1")
        self.assertEqual(group["change_analysis"], {"llm": "Updated in SQL"})
        self.assertEqual(group["future"], {"keep": True})
        self.assertTrue(group["evidence"])
        self.assertTrue(group["grouping"])
        self.assertEqual(reports["needs_review.json"]["alignments"][0]["current_disclosures"], [])
        with TemporaryDirectory() as output:
            paths = exports.export_reports("amd", [2023, 2024], Path(output))
            self.assertEqual(len(paths), 6)
            self.assertEqual(json.loads((Path(output) / "alignments/amd/2023-2024/alignments.json").read_text()),
                             reports["alignments.json"])
        self.sql("DELETE FROM disclosures WHERE disclosure_id='amd_2023_7_D001';")
        with TemporaryDirectory() as output:
            destination = Path(output) / "must-not-be-created"
            with self.assertRaisesRegex(exports.DownstreamExportError, "Import disclosures for both years"):
                exports.export_reports("amd", [2023, 2024], destination)
            self.assertFalse(destination.exists())

    def test_combined_command_saves_both_stages_and_cached_rerun_uses_no_model_calls(self):
        import test_parallel_extraction as extraction_tests
        from sec_disclosure.agents import alignment_runtime

        fixture = extraction_tests.ParallelExtractionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for year in ("2023", "2024"):
            for suffix in ("chunks", "chunk_sentences"):
                path = fixture.root / "raw/amd" / year / f"{year}_{suffix}.json"
                rows = json.loads(path.read_text())
                rows[0]["text"] = "Revenue rose."
                path.write_text(json.dumps(rows))
        with patch.object(alignment_runtime, "request_completion", side_effect=AssertionError("Exact alignments need no model calls")):
            self.assertEqual(fixture.run_cli("--align", years=("2023", "2024"), store_postgres=True)[:2], (0, 4))
            self.assertEqual(fixture.run_cli("--align", years=("2023", "2024"), store_postgres=True)[:2], (0, 0))
        self.assertEqual(self.sql("SELECT count(*) FROM disclosures;"), "4")
        self.assertEqual(self.sql("SELECT count(*) FROM alignment_runs;"), "1")
        self.assertEqual(self.sql("SELECT count(*) FROM alignments;"), "2")
        self.assertEqual(self.sql("SELECT count(*) FROM alignment_members;"), "4")
        self.assertEqual(self.sql("SELECT count(*) FROM alignments WHERE change_analysis->>'llm' = 'Unchanged';"), "2")


if __name__ == "__main__":
    unittest.main()
