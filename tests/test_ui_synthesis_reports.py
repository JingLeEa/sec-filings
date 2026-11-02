"""Database synthesis discovery and the report viewer, without model requests."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from streamlit.testing.v1 import AppTest

from sec_ui import data, reports


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/synthesis/AMD_2023_2025_synthesis_output.json"
SAVED = dict(company="AMD", years=[2024, 2025],
             generated_at=datetime(2026, 10, 31, 12, 27, tzinfo=timezone.utc))


class SynthesisStorageUiTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = ("synthesis_reports",)
        context = patch.object(data, "_postgres_connection", return_value=self.connection)
        context.start()
        self.addCleanup(context.stop)
        self.connection.__enter__.return_value = self.connection

    def test_discovery_uses_metadata_only_and_newest_first(self):
        self.cursor.fetchall.return_value = [(SAVED["company"], SAVED["years"], SAVED["generated_at"])]
        self.assertEqual(reports.list_synthesis_reports(), [SAVED])
        sql = self.cursor.execute.call_args.args[0]
        self.assertIn("ORDER BY generated_at DESC", sql)
        self.assertIn("SELECT company, years, generated_at", sql)
        self.assertTrue(self.connection.read_only)

    def test_load_selects_exact_company_and_years_and_keeps_evidence(self):
        payload = json.loads(FIXTURE.read_text())
        payload["years"] = [2024, 2025]
        self.cursor.fetchall.return_value = [(payload,)]
        loaded = reports.load_synthesis_report("amd", [2024, 2025])
        self.assertEqual(loaded["source_catalog"], payload["source_catalog"])
        self.assertEqual(loaded["report"], payload["report"])
        self.assertEqual(self.cursor.execute.call_args.args[1], ("AMD", [2024, 2025]))
        # psycopg infers smallint[] for fiscal years; the stored column is integer[].
        self.assertIn("years=%s::integer[]", self.cursor.execute.call_args.args[0])

    def test_missing_table_returns_no_reports(self):
        self.cursor.fetchone.return_value = (None,)
        self.assertEqual(reports.list_synthesis_reports(), [])
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_deleted_report_has_a_useful_error(self):
        self.cursor.fetchall.return_value = []
        with self.assertRaisesRegex(data.DataError, "no longer available"):
            reports.load_synthesis_report("AMD", [2024, 2025])

    def test_invalid_database_payload_is_rejected(self):
        self.cursor.fetchall.return_value = [({"report": []},)]
        with self.assertRaisesRegex(data.DataError, "no 'report' section"):
            reports.load_synthesis_report("AMD", [2024, 2025])

    def test_query_errors_do_not_expose_connection_details(self):
        self.cursor.execute.side_effect = RuntimeError("password=secret")
        with self.assertRaisesRegex(data.DataError, "Check the database") as caught:
            reports.list_synthesis_reports()
        self.assertNotIn("secret", str(caught.exception))


class SynthesisPageTests(unittest.TestCase):
    def setUp(self):
        for context in (
            patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "auto"}),
            patch.object(reports, "list_synthesis_files", return_value=[]),
            patch.object(reports, "list_synthesis_reports", return_value=[SAVED]),
        ):
            context.start()
            self.addCleanup(context.stop)
        self.payload = json.loads(FIXTURE.read_text())
        self.payload["years"] = [2024, 2025]
        context = patch.object(reports, "load_synthesis_report", return_value=self.payload)
        self.load = context.start()
        self.addCleanup(context.stop)

    def page(self):
        page = AppTest.from_file(str(ROOT / "app/pages/3_Synthesis_Report.py"), default_timeout=20).run()
        self.assertFalse(page.exception, [error.value for error in page.exception])
        return page

    def test_database_report_renders_without_local_export_and_keeps_downloads(self):
        page = self.page()
        self.assertEqual(page.selectbox(key="sel_synthesis").value, "AMD 2024-2025 (PostgreSQL)")
        self.load.assert_called_once_with("AMD", [2024, 2025])
        self.assertTrue(any("Report source: PostgreSQL" in row.value for row in page.caption))
        self.assertEqual({metric.label: metric.value for metric in page.metric}["Citation check"], "Passed")
        self.assertEqual(len(page.get("download_button")), 3)
        self.assertTrue(any("amd_2023_2024_M0269" in row.value for row in page.markdown))

    def test_user_can_switch_from_database_to_local_report(self):
        with patch.object(reports, "list_synthesis_files", return_value=[FIXTURE]):
            page = self.page()
            picker = page.selectbox(key="sel_synthesis")
            self.assertEqual(len(picker.options), 2)
            picker.select(picker.options[1]).run()
        self.assertFalse(page.exception)
        self.assertTrue(any("Report source: local file" in row.value for row in page.caption))

    def test_different_year_selections_remain_separate(self):
        saved = [dict(SAVED, years=[2023, 2024, 2025]), dict(SAVED, years=[2023, 2025])]
        with patch.object(reports, "list_synthesis_reports", return_value=saved):
            page = self.page()
        self.assertEqual(page.selectbox(key="sel_synthesis").options,
                         ["AMD 2023-2025 (PostgreSQL)", "AMD 2023, 2025 (PostgreSQL)"])

    def test_files_mode_does_not_connect_to_postgres(self):
        with patch.dict(os.environ, {"SEC_UI_DATA_SOURCE": "files"}), \
             patch.object(reports, "list_synthesis_reports") as discover, \
             patch.object(reports, "list_synthesis_files", return_value=[FIXTURE]):
            page = self.page()
        discover.assert_not_called()
        self.load.assert_not_called()
        self.assertTrue(any("local file" in row.value for row in page.caption))

    def test_database_error_keeps_local_report_available(self):
        with patch.object(reports, "list_synthesis_reports", side_effect=data.DataError("Database unavailable")), \
             patch.object(reports, "list_synthesis_files", return_value=[FIXTURE]):
            page = self.page()
        self.assertTrue(any("Database unavailable" in row.value for row in page.warning))
        self.assertEqual(len(page.get("download_button")), 3)

    def test_no_reports_explains_how_to_create_one(self):
        with patch.object(reports, "list_synthesis_reports", return_value=[]):
            page = self.page()
        self.assertTrue(any("No synthesis output" in row.value for row in page.info))
        self.assertTrue(any("pipeline through synthesis" in row.value for row in page.markdown))

    def test_report_removed_after_listing_shows_error(self):
        self.load.side_effect = data.DataError("The selected synthesis report is no longer available.")
        page = self.page()
        self.assertTrue(any("no longer available" in row.value for row in page.error))


if __name__ == "__main__":
    unittest.main()
