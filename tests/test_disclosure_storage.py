import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sec_disclosure.storage import disclosures as storage


class DisclosureStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.env_file = self.folder / "database.env"
        self.env_file.write_text("POSTGRES_PASSWORD=literal-${OTHER_SECRET}\nPOSTGRES_PORT=6543\n")
        self.records = [
            {"disclosure_id": f"amd_2025_1A_D{index:03d}", "company": "amd", "fiscal_year": "2025",
             "item": "1A", "content": f"Evidence {index}.", "taxonomy": "Technology & AI",
             "sources": [{"paragraph_id": f"amd_2025_1A_P{index:03d}", "sentences": [
                 {"sentence_id": f"amd_2025_1A_P{index:03d}_S001", "text": f"Evidence {index}."}]}],
             "verification": {"status": "source_validated" if index == 1 else "needs_review"},
             "related_disclosures": [{"disclosure_ids": ["amd_2025_1A_D002"], "reason": "Related subject."}]}
            for index in (1, 2)
        ]
        self.write_reports()
        self.connection = MagicMock()
        self.connection.__enter__.return_value = self.connection
        self.cursor = MagicMock()
        self.cursor.__enter__.return_value = self.cursor
        self.connection.cursor.return_value = self.cursor
        self.cursor.fetchone.side_effect = [(7,), (2,)]
        self.connect = MagicMock(return_value=self.connection)
        self.driver = SimpleNamespace(connect=self.connect)

    def write_reports(self, version="4", complete=True, review=None):
        for filename, records in (("disclosures.json", self.records[:1]),
                                  ("review_candidates.json", self.records[1:] if review is None else review)):
            (self.folder / filename).write_text(json.dumps({"schema_version": version, "company": "amd",
                "fiscal_year": "2025", "disclosures": records}))
        (self.folder / "token_usage.json").write_text(json.dumps({"run_complete": complete}))

    def store(self, **kwargs):
        with patch.dict(sys.modules, {"psycopg": self.driver}), patch.dict(os.environ, {}, clear=True):
            return storage.store_disclosures(self.folder, "amd", 2025, env_file=self.env_file, **kwargs)

    def test_both_schemas_import_accepted_and_review_with_evidence_and_related_links(self):
        for version in ("3", "4"):
            with self.subTest(version=version):
                self.write_reports(version=version)
                self.cursor.fetchone.side_effect = [(7,), (2,)]
                self.cursor.executemany.reset_mock()
                result = self.store(require_complete=True)
                self.assertEqual((result.accepted, result.review, result.stored), (1, 1, 2))
                rows = [call.args[1][0] for call in self.cursor.executemany.call_args_list]
                self.assertEqual([row["review_status"] for row in rows], ["accepted", "review_candidate"])
                self.assertEqual(json.loads(rows[0]["sources"]), self.records[0]["sources"])
                self.assertEqual(json.loads(rows[0]["extra"])["related_disclosures"], self.records[0]["related_disclosures"])
                self.assertEqual(self.connection.__exit__.call_args.args, (None, None, None))

    def test_snapshot_replacement_deletes_only_obsolete_ids_for_the_selected_filing(self):
        self.store(replace=True)
        delete = next(call for call in self.cursor.execute.call_args_list if call.args[0].startswith("DELETE"))
        self.assertIn("filing_id = %s", delete.args[0])
        self.assertEqual(delete.args[1], (7, [r["disclosure_id"] for r in self.records]))
        self.assertFalse(any(statement in call.args[0] for call in self.cursor.execute.call_args_list
                             for statement in ("INSERT INTO alignments", "UPDATE alignments", "DELETE FROM alignments")))

    def test_partial_snapshot_and_duplicate_records_are_rejected_before_database_access(self):
        self.write_reports(complete=False)
        with self.assertRaisesRegex(ValueError, "finish disclosure extraction"):
            self.store(replace=True)
        self.write_reports(review=self.records[:1])
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.store()
        self.connect.assert_not_called()

    def test_database_failure_rolls_back_and_does_not_print_credentials(self):
        self.cursor.executemany.side_effect = RuntimeError("password=private-database-secret")
        with self.assertRaises(storage.DisclosureStorageError) as error:
            self.store(replace=True)
        self.assertNotIn("private-database-secret", str(error.exception))
        self.assertIs(self.connection.__exit__.call_args.args[0], RuntimeError)
        self.assertFalse(any(call.args[0].startswith("DELETE") for call in self.cursor.execute.call_args_list))

    def test_snapshot_failure_rolls_back_normalized_records_in_the_same_transaction(self):
        with patch.object(storage, 'publish', side_effect=RuntimeError('snapshot write failed')):
            with self.assertRaises(storage.DisclosureStorageError):
                self.store(replace=True)
        self.assertIs(self.connection.__exit__.call_args.args[0], RuntimeError)

    def test_cross_filing_id_is_rejected_before_it_can_overwrite_another_filing(self):
        self.records[0]["disclosure_id"] = "mu_2025_1A_D001"
        self.write_reports()
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.store(replace=True)
        self.connect.assert_not_called()

    def test_environment_settings_preserve_literal_password_and_exported_values_win(self):
        with patch.dict(os.environ, {"POSTGRES_PORT": "7654", "OTHER_SECRET": "expanded"}, clear=True):
            before = dict(os.environ)
            args = storage.connection_arguments(self.env_file)
            self.assertEqual(args["password"], "literal-${OTHER_SECRET}")
            self.assertEqual(args["port"], "7654")
            self.assertEqual(dict(os.environ), before)
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://example.test/db"}, clear=True):
            self.assertEqual(storage.connection_arguments(self.env_file),
                             {"conninfo": "postgresql://example.test/db", "connect_timeout": 10})

    def test_dry_run_reads_exact_output_folder_without_connecting_or_loading_env(self):
        with patch.object(storage, "connection_arguments") as settings, \
             patch.dict(sys.modules, {"psycopg": self.driver}), redirect_stdout(io.StringIO()):
            code = storage.main(["--company", "AMD", "--year", "2025", "--output-dir", str(self.folder),
                                 "--replace", "--dry-run", "--env-file", str(self.folder / "missing.env")])
        self.assertEqual(code, 0)
        settings.assert_not_called()
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
