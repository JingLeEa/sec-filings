import csv
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.pipelines import run_disclosure_pipeline as pipeline


def filing(year):
    return f"""<html><body>
    <div>Item 1. Business</div><div>Overview</div>
    <div>We manufacture memory. Our products changed in {year}.</div>
    <div>Item 1A. Risk Factors</div><div>Demand Risks</div>
    <div>Demand changed in {year}.</div>
    <div>Item 1B. Unresolved Staff Comments</div><div>None.</div>
    <div>Item 7. Management's Discussion and Analysis</div><div>Results</div>
    <div>Revenue changed in {year}.</div>
    <div>Item 7A. Market Risk</div><div>None.</div>
    <div>Item 8. Financial Statements</div><div>Accounting</div>
    <div>Accounting disclosures changed in {year}.</div>
    <div>Item 9. Changes in Accountants</div><div>None.</div>
    <div>Item 15. Exhibits</div><div>Exhibit List</div>
    <div>Exhibits changed in {year}.</div>
    <div>Item 16. Form 10-K Summary</div><div>None.</div>
    </body></html>"""


class DisclosurePipelineTests(unittest.TestCase):
    def arguments(self, root):
        return ["--ticker", "MU", "--previous-year", "2024", "--current-year", "2025",
                "--company", "Micron", "--industry", "Semiconductors",
                "--user-agent", "Test test@example.com", "--data-dir", str(root)]

    def test_all_three_scripts_run_with_real_extraction_comparison_and_export(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            for selected in (None, ["7"], ["1", "15"]):
                with self.subTest(items=selected):
                    destination = root / ("default" if selected is None else "_".join(selected))
                    arguments = self.arguments(destination)
                    if selected is not None:
                        arguments.extend(["--items", *selected])
                    def load_filing(args):
                        return filing(args.year), f"https://example.test/mu-{args.year}.htm", args.company
                    with patch.object(pipeline.sec_10k_extractor, "load_filing_from_sec_api", side_effect=load_filing) as fetch:
                        with redirect_stdout(io.StringIO()):
                            self.assertEqual(pipeline.main(arguments), 0)
                    self.assertEqual([call.args[0].year for call in fetch.call_args_list], ["2024", "2025"])
                    expected = set(selected or ["1", "1A", "7", "8"])
                    for year in ("2024", "2025"):
                        chunks = json.loads((destination / "raw" / "mu" / year / f"{year}_chunks.json").read_text())
                        self.assertEqual({record["item"] for record in chunks}, expected)
                        self.assertTrue(all(record["company"] == "mu" for record in chunks))
                    diff = json.loads((destination / "comparison/mu/2024_vs_2025/all_items_diff.json").read_text())
                    self.assertEqual({item["item"] for item in diff["items"]}, expected)
                    output = destination / "disclosure_output/mu/2024_vs_2025/all_items_diff"
                    with (output / "disclosure_annotations.tsv").open(newline="") as handle:
                        rows = list(csv.DictReader(handle, delimiter="\t"))
                    self.assertEqual(len(rows), len(expected))
                    self.assertEqual({row["Item"] for row in rows}, expected)
                    self.assertTrue(all(row["Company"] == "Micron" for row in rows))
                    self.assertTrue(all(row["Industry"] == "Semiconductors" for row in rows))
                    for row in rows:
                        self.assertEqual(len(row), 20)
                        self.assertIn("2024", row["Previous Disclosure Text"])
                        self.assertIn("2025", row["Current Disclosure Text"])
                        self.assertNotIn("We manufacture memory.", row["Previous Disclosure Text"])
                        self.assertIn(row["Previous Disclosure Text"], row["Previous Original Paragraph"])
                        self.assertIn(row["Current Disclosure Text"], row["Current Original Paragraph"])
                        self.assertEqual(row["Rationale / Evidence"], "")
                        if row["Item"] == "1":
                            self.assertIn("We manufacture memory.", row["Previous Original Paragraph"])
                            self.assertIn("We manufacture memory.", row["Current Original Paragraph"])
                    self.assertTrue((output / "paste_into_sheets.html").is_file())
                    self.assertTrue((output / "paste_into_sheets.tsv").is_file())

    def test_stops_on_each_failed_step_without_running_later_steps(self):
        with TemporaryDirectory() as temporary:
            for failure in range(4):
                with self.subTest(failed_step=failure):
                    extract_results = [2 if failure == 0 else 0, 2 if failure == 1 else 0]
                    with patch.object(pipeline.sec_10k_extractor, "main", side_effect=extract_results) as extract, \
                         patch.object(pipeline.compare_item_changes, "main", return_value=2 if failure == 2 else 0) as compare, \
                         patch.object(pipeline.export_disclosure_annotations, "main", return_value=2 if failure == 3 else 0) as export:
                        output, errors = io.StringIO(), io.StringIO()
                        with redirect_stdout(output), redirect_stderr(errors):
                            self.assertEqual(pipeline.main(self.arguments(Path(temporary))), 2)
                        self.assertEqual(extract.call_count, 1 if failure == 0 else 2)
                        self.assertEqual(compare.call_count, int(failure >= 2))
                        self.assertEqual(export.call_count, int(failure >= 3))
                        self.assertIn("Pipeline stopped", errors.getvalue())
                        self.assertNotIn("Pipeline complete", output.getvalue())

    def test_comparison_system_exit_stops_before_export(self):
        with TemporaryDirectory() as temporary:
            with patch.object(pipeline.sec_10k_extractor, "main", return_value=0), \
                 patch.object(pipeline.compare_item_changes, "main", side_effect=SystemExit("Title map not found")), \
                 patch.object(pipeline.export_disclosure_annotations, "main") as export:
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(pipeline.main(self.arguments(Path(temporary))), 2)
                export.assert_not_called()
                self.assertIn("Title map not found", errors.getvalue())

    def test_uses_environment_contact_and_validates_before_extracting(self):
        basic = ["--ticker", "mu", "--previous-year", "2024", "--current-year", "2025"]
        with patch.dict(os.environ, {"SEC_USER_AGENT": "Example example@example.com"}):
            args = pipeline.parse_args(basic)
            self.assertEqual(args.user_agent, "Example example@example.com")
            self.assertEqual(args.ticker, "MU")
            self.assertEqual(args.items, ["1", "1A", "7", "8"])
            for extra in (["--previous-year", "2025"], ["--max-chars", "0"], ["--title-map", "bad"]):
                with self.subTest(arguments=extra), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    pipeline.parse_args(basic + extra)
                self.assertEqual(error.exception.code, 2)
        with patch.dict(os.environ, {"SEC_USER_AGENT": ""}), \
             patch.object(pipeline.sec_10k_extractor, "main") as extract, \
             redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pipeline.main(basic)
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
