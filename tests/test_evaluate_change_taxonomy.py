import copy
import csv
import io
import json
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from sec_disclosure.evaluation.evaluate_change_taxonomy import (
    ACCEPTED_STATUSES, LABELS, REQUIRED_COLUMNS, GoldRow, calculate_metrics,
    evaluate_rows, filing_accession, infer_annotation_ticker, load_gold, load_predictions,
    main, normalize_text, resolve_annotation_path,
)
from sec_disclosure.evaluation.report import summarize_alignment_inventory


def sentence(side, position, text, item="1A", ticker="amd"):
    year = "2023" if side == "previous" else "2024"
    return {"sentence_id": f"{ticker}_{year}_{item}_P{position:03d}_S001", "text": text}


def alignment(mid, before, after, label="Modified", status="ai_verified", item="1A", ticker="amd"):
    previous_id, current_id = f"{ticker}_2023_{item}_D{mid}", f"{ticker}_2024_{item}_D{mid}"
    return {
        "match_id": mid, "previous_ids": [previous_id] if before else [],
        "current_ids": [current_id] if after else [],
        "relationship": "one_to_one" if before and after else "previous_only" if before else "current_only",
        "status": status,
        "previous_disclosures": [{"disclosure_id": previous_id, "item": item}] if before else [],
        "current_disclosures": [{"disclosure_id": current_id, "item": item}] if after else [],
        "evidence": ([{"disclosure_id": previous_id, "sentences": before}] if before else [])
                    + ([{"disclosure_id": current_id, "sentences": after}] if after else []),
        "change_analysis": {"status": "not_started", "lexical": None, "semantic": None,
                            "llm": None, "final_taxonomy": label},
    }


def gold(record="r1", before="Sentence A.", after="Sentence B.", label="Modified",
         previous_id="", current_id="", item="1A"):
    return GoldRow(record, "amd", "2023", "2024", item, label, previous_id, current_id, before, after)


def document(rows, ticker="amd"):
    return {"company": ticker, "previous_year": "2023", "current_year": "2024", "alignments": rows}


class ChangeTaxonomyEvaluationTests(unittest.TestCase):
    def test_selected_pipeline_uses_named_benchmark_and_rereads_inputs_each_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            annotations = root / "data/annotation"
            annotations.mkdir(parents=True)
            annotation = annotations / "amd.csv"
            # A different company's benchmark must not interfere with AMD discovery.
            (annotations / "nvda.csv").write_text("unrelated benchmark")
            columns = (*REQUIRED_COLUMNS, "Item")
            values = dict.fromkeys(columns, "")
            values.update({"Record ID": "r1", "Company": "AMD", "Previous Fiscal Year": "2023",
                           "Current Fiscal Year": "2024", "Previous Disclosure Text": "Sentence A.",
                           "Current Disclosure Text": "Sentence B.", "Change Taxonomy": "Modified", "Item": "1A"})

            def save_annotation():
                with annotation.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    writer.writerow(values)

            save_annotation()
            source = root / "data/alignments/amd/2023-2024/alignments_result.json"
            source.parent.mkdir(parents=True)
            prediction = document([alignment("m1", [sentence("previous", 1, "Sentence A.")],
                                             [sentence("current", 2, "Sentence B.")])])
            source.write_text(json.dumps(prediction))
            output = root / "data/evaluation/amd"

            def run():
                originals = {path: path.read_bytes() for path in (annotation, source)}
                with patch("sec_disclosure.evaluation.evaluate_change_taxonomy.PROJECT_ROOT", root):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["--ticker", "AMD"]), 0)
                self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
                return json.loads((output / "summary.json").read_text()), (output / "evaluation_report.md").read_text()

            first, first_report = run()
            self.assertEqual(first["inputs"][0]["path"], str(annotation))
            self.assertEqual(first["overall"]["accuracy"], 1)
            self.assertIn("100.00%", first_report)
            values["Change Taxonomy"] = "Expanded"
            save_annotation()
            second, second_report = run()
            self.assertEqual(second["overall"]["accuracy"], 0)
            self.assertIn("0.00%", second_report)
            self.assertNotEqual(first_report, second_report)
            prediction["alignments"][0]["change_analysis"]["final_taxonomy"] = "Expanded"
            source.write_text(json.dumps(prediction))
            third, third_report = run()
            self.assertEqual(third["overall"]["accuracy"], 1)
            self.assertIn("100.00%", third_report)
            self.assertNotEqual(second_report, third_report)

    def test_annotation_discovery_fallback_and_ambiguity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_name = root / "old annotation filename.csv"
            old_name.write_text("benchmark")
            self.assertEqual(resolve_annotation_path(root, "amd"), old_name)
            (root / "nvda.csv").write_text("different benchmark")
            with self.assertRaisesRegex(ValueError, "amd.csv.*missing"):
                resolve_annotation_path(root, "amd")
            with self.assertRaisesRegex(ValueError, "Multiple annotation CSVs.*--ticker"):
                resolve_annotation_path(root, None)
            named = root / "amd.csv"
            old_name.rename(named)
            self.assertEqual(resolve_annotation_path(root, "amd"), named)
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "No annotation CSV found"):
                resolve_annotation_path(Path(directory), "amd")

    def test_micron_pipeline_selects_display_name_csv_and_infers_mu_without_defaults(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            annotations = root / "data/annotation"
            annotations.mkdir(parents=True)
            annotation = annotations / "micron.csv"
            unrelated = annotations / "amd.csv"
            unrelated.write_text("Company\nAMD\n")
            columns = (*REQUIRED_COLUMNS, "Item")
            row = dict.fromkeys(columns, "")
            row.update({"Record ID": "r1", "Company": "Micron", "Previous Fiscal Year": "2023",
                        "Current Fiscal Year": "2024", "Previous Disclosure Text": "Sentence A.",
                        "Current Disclosure Text": "Sentence B.", "Change Taxonomy": "Modified", "Item": "1A",
                        "Previous Paragraph / Chunk ID": "MU_2023_Item1A_P001_S01",
                        "Current Paragraph / Chunk ID": "MU_2024_Item1A_P002_S01"})
            introduced = dict(row, **{"Record ID": "r2", "Previous Disclosure Text": "",
                                     "Current Disclosure Text": "Introduced text.", "Change Taxonomy": "New",
                                     "Previous Paragraph / Chunk ID": "", "Current Paragraph / Chunk ID": ""})
            with annotation.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows([row, introduced])
            source = root / "data/alignments/mu/2023-2024/alignments_result.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(document([
                alignment("m1", [sentence("previous", 1, "Sentence A.", ticker="mu")],
                          [sentence("current", 2, "Sentence B.", ticker="mu")], ticker="mu"),
                alignment("new", [], [sentence("current", 3, "Introduced text.", ticker="mu")],
                          "New", "unmatched", ticker="mu"),
            ], ticker="mu")))
            original_alignment = source.with_name("alignments.json")
            original_alignment.write_text(source.read_text())
            originals = {path: path.read_bytes() for path in (annotation, source, original_alignment)}
            output = root / "data/evaluation/mu"
            for argv in (["--ticker", "MU"], [], ["--annotation", str(annotation)]):
                # With a sole annotation, infer MU from source IDs despite Company=Micron.
                if not argv:
                    unrelated.unlink()
                with patch("sec_disclosure.evaluation.evaluate_change_taxonomy.PROJECT_ROOT", root):
                    with redirect_stdout(io.StringIO()):
                        self.assertEqual(main(argv), 0)
                summary = json.loads((output / "summary.json").read_text())
                self.assertEqual(summary["inputs"][0]["path"], str(annotation))
                self.assertEqual(summary["overall"]["benchmark_rows"], 2)
                self.assertEqual(summary["overall"]["scored_rows"], 2)
                self.assertEqual(summary["overall"]["accuracy"], 1)
                self.assertTrue((output / "evaluation_report.md").read_text().startswith("# MU change taxonomy evaluation\n"))
                with (output / "row_results.csv").open(newline="") as stream:
                    self.assertEqual({record["company"] for record in csv.DictReader(stream)}, {"mu"})
            self.assertFalse((root / "data/evaluation/amd").exists())
            self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))

    def test_explicit_company_label_maps_unidentified_rows_and_rejects_conflicting_ids(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.csv"
            columns = (*REQUIRED_COLUMNS, "Item")
            row = dict.fromkeys(columns, "")
            row.update({"Record ID": "r1", "Company": "Micron Technology", "Previous Fiscal Year": "2023",
                        "Current Fiscal Year": "2024", "Current Disclosure Text": "Introduced text.",
                        "Change Taxonomy": "New", "Item": "1A"})

            def save():
                with path.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    writer.writerow(row)

            save()
            with self.assertRaisesRegex(ValueError, "Cannot infer a ticker"):
                infer_annotation_ticker(path)
            with self.assertRaisesRegex(ValueError, "No benchmark rows for mu"):
                load_gold(path, "mu", set())
            selected = load_gold(path, "mu", set(), company_label="Micron Technology")
            self.assertEqual(selected[0].company, "mu")
            row["Current Paragraph / Chunk ID"] = "AMD_2024_Item1A_P002_S01"
            save()
            with self.assertRaisesRegex(ValueError, "source ID contradicts company/year"):
                load_gold(path, "mu", set(), company_label="Micron Technology")
            # A populated row with a blank Company must not be selected by an absent override.
            row["Company"] = ""
            row["Current Paragraph / Chunk ID"] = ""
            save()
            with self.assertRaisesRegex(ValueError, "No benchmark rows for mu"):
                load_gold(path, "mu", set())

    def test_mixed_company_annotations_are_scoped_and_require_explicit_ticker(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "benchmark.csv"
            columns = (*REQUIRED_COLUMNS, "Item")
            row = dict.fromkeys(columns, "")
            row.update({"Record ID": "r1", "Company": "Micron", "Previous Fiscal Year": "2023",
                        "Current Fiscal Year": "2024", "Current Disclosure Text": "Introduced text.",
                        "Change Taxonomy": "New", "Item": "1A",
                        "Current Paragraph / Chunk ID": "MU_2024_Item1A_P002_S01"})
            other = dict(row, **{"Company": "AMD", "Current Paragraph / Chunk ID": "AMD_2024_Item1A_P002_S01"})
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerows([row, other])
            with self.assertRaisesRegex(ValueError, "does not identify exactly one ticker"):
                infer_annotation_ticker(path)
            selected = load_gold(path, "mu", set())
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0].company, "mu")

    def run_rows(self, rows, benchmark):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "alignments_result.json"
            original = json.dumps(document(rows))
            path.write_text(original, encoding="utf-8")
            predictions, occurrences = load_predictions([path], "amd")
            results = evaluate_rows(benchmark, predictions, occurrences)
            self.assertEqual(path.read_text(), original)
            return results

    def test_same_pair_compares_only_final_taxonomy(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A.")],
                        [sentence("current", 2, "Sentence B.")], "Expanded")
        row["change_analysis"]["llm"] = {"taxonomy": "Modified"}
        result = self.run_rows([row], [gold()])[0]
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["predicted_taxonomy"], "Expanded")
        self.assertFalse(result["correct"])
        # A populated final label is usable despite an unchanged stage status.
        self.assertEqual(row["change_analysis"]["status"], "not_started")

    def test_a_to_c_is_not_scored_against_gold_a_to_b(self):
        rows = [alignment("m1", [sentence("previous", 1, "Sentence A.")],
                          [sentence("current", 2, "Sentence C.")], "Expanded"),
                alignment("m2", [sentence("previous", 3, "Sentence D.")],
                          [sentence("current", 4, "Sentence B.")])]
        results = self.run_rows(rows, [gold()])
        self.assertEqual(results[0]["status"], "pair_mismatch")
        self.assertEqual(calculate_metrics(results)["scored_rows"], 0)
        self.assertIsNone(calculate_metrics(results)["accuracy"])

    def test_missing_counterpart_citation_is_not_proof_of_mismatch(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A.")],
                        [sentence("current", 2, "Sentence C.")])
        result = self.run_rows([row], [gold()])[0]
        self.assertEqual(result["status"], "unmapped_evidence")

    def test_renumbered_ids_use_verified_text_not_coordinates(self):
        row = alignment("m1", [sentence("previous", 7, "Sentence A.")],
                        [sentence("current", 8, "Sentence B.")])
        result = self.run_rows([row], [gold(previous_id="AMD_2023_Item1A_P001_S01",
                                          current_id="AMD_2024_Item1A_P002_S01")])[0]
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["previous_lookup"], "exact_text")
        self.assertEqual(result["previous_sentence_id"], "amd_2023_1A_P007_S001")

    def test_matching_id_with_different_text_is_not_accepted(self):
        row = alignment("m1", [sentence("previous", 1, "Revenue was $10 million.")],
                        [sentence("current", 2, "Sentence B.")])
        result = self.run_rows([row], [gold(before="Revenue was $11 million.",
                                          previous_id="AMD_2023_Item1A_P001_S01")])[0]
        self.assertEqual(result["status"], "unmapped_evidence")

    def test_duplicate_text_requires_occurrence_identity(self):
        rows = [alignment("m1", [sentence("previous", 1, "Sentence A.")], [sentence("current", 2, "Sentence B.")]),
                alignment("m2", [sentence("previous", 3, "Sentence A.")], [sentence("current", 2, "Sentence B.")])]
        uncertain = self.run_rows(rows, [gold()])[0]
        self.assertEqual(uncertain["status"], "ambiguous_sentence")
        resolved = self.run_rows(rows, [gold(previous_id="AMD_2023_Item1A_P001_S01")])[0]
        self.assertEqual(resolved["status"], "scored")
        self.assertEqual(resolved["previous_lookup"], "id_and_text")

    def test_gold_label_cannot_choose_between_overlapping_predictions(self):
        before, after = [sentence("previous", 1, "Sentence A.")], [sentence("current", 2, "Sentence B.")]
        rows = [alignment("m1", before, after, "Modified"), alignment("m2", before, after, "Expanded")]
        self.assertEqual(self.run_rows(rows, [gold()])[0]["status"], "ambiguous_prediction")

    def test_mixed_gold_group_is_excluded_without_majority_vote(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A."), sentence("previous", 3, "Sentence X.")],
                        [sentence("current", 2, "Sentence B."), sentence("current", 4, "Sentence Y.")])
        results = self.run_rows([row], [gold(), gold("r2", "Sentence X.", "Sentence Y.", "Reworded")])
        self.assertEqual([result["status"] for result in results], ["mixed_gold_group"] * 2)
        self.assertEqual(calculate_metrics(results)["per_class"]["Modified"]["benchmark_count"], 1)

    def test_one_sided_cases_require_matching_comparison_but_not_correct_label(self):
        rows = [alignment("new", [], [sentence("current", 1, "Introduced text.")], "Modified", "unmatched"),
                alignment("removed", [sentence("previous", 2, "Deleted text.")], [], "Removed", "unmatched")]
        results = self.run_rows(rows, [gold("new", "", "Introduced text.", "New"),
                                      gold("removed", "Deleted text.", "", "Removed")])
        self.assertEqual([result["status"] for result in results], ["scored", "scored"])
        self.assertEqual([result["correct"] for result in results], [False, True])
        paired = alignment("paired", [sentence("previous", 3, "Previous text.")],
                           [sentence("current", 1, "Introduced text.")], "New")
        self.assertEqual(self.run_rows([paired], [gold("new", "", "Introduced text.", "New")])[0]["status"], "pair_mismatch")

    def test_null_label_is_not_inferred_from_relationship_or_other_stages(self):
        row = alignment("new", [], [sentence("current", 1, "Introduced text.")], None, "unmatched")
        row["change_analysis"]["lexical"] = {"label": "New"}
        row["change_analysis"]["llm"] = {"label": "New"}
        self.assertEqual(self.run_rows([row], [gold("new", "", "Introduced text.", "New")])[0]["status"], "missing_taxonomy")

    def test_review_cases_and_multilabel_predictions_are_not_scored(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A.")], [sentence("current", 2, "Sentence B.")], status="needs_review")
        self.assertEqual(self.run_rows([row], [gold()])[0]["status"], "needs_review")
        row["status"] = "ai_verified"
        row["change_analysis"]["final_taxonomy"] = ["Modified", "Expanded"]
        self.assertEqual(self.run_rows([row], [gold()])[0]["status"], "invalid_taxonomy")

    def test_unchanged_alias_and_singleton_labels_are_explicitly_supported(self):
        row = alignment("m1", [sentence("previous", 1, "Same text.")], [sentence("current", 2, "Same text.")], [" unchanged "])
        result = self.run_rows([row], [gold(before="Same text.", after="Same text.", label="Reworded")])[0]
        self.assertTrue(result["correct"])

    def test_grouping_provenance_does_not_invent_cross_links(self):
        row = alignment("group", [sentence("previous", 1, "Sentence A.")], [sentence("current", 2, "Sentence B.")])
        row["previous_ids"].append("old_other")
        row["previous_disclosures"].append({"disclosure_id": "old_other", "item": "1A"})
        row["current_ids"].append("new_other")
        row["current_disclosures"].append({"disclosure_id": "new_other", "item": "1A"})
        row["grouping"] = {"source_alignments": [
            {"previous_ids": [row["previous_ids"][0]], "current_ids": ["new_other"]},
            {"previous_ids": ["old_other"], "current_ids": [row["current_ids"][0]]},
        ]}
        self.assertEqual(self.run_rows([row], [gold()])[0]["status"], "pair_mismatch")

    def test_same_text_in_another_item_is_not_a_match(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A.", "8")], [sentence("current", 2, "Sentence B.", "8")], item="8")
        self.assertEqual(self.run_rows([row], [gold()])[0]["status"], "missing_prediction")

    def test_missing_comparison_is_reported(self):
        self.assertEqual(evaluate_rows([gold()], {}, {})[0]["status"], "missing_prediction")

    def test_sec_filing_identity_resolves_filing_year_vs_fiscal_year(self):
        before, after = sentence("previous", 1, "Sentence A."), sentence("current", 2, "Sentence B.")
        before["source_url"] = "https://www.sec.gov/Archives/edgar/data/2488/000000248824000012/amd-20231230.htm"
        after["source_url"] = "https://www.sec.gov/Archives/edgar/data/2488/000000248825000012/amd-20241228.htm"
        benchmark = gold()
        benchmark.previous_year, benchmark.current_year = "2024", "2025"
        benchmark.previous_filing_url = "https://www.sec.gov/Archives/edgar/data/2488/0000002488-24-000012-index.html"
        benchmark.current_filing_url = "https://www.sec.gov/Archives/edgar/data/2488/0000002488-25-000012-index.html"
        result = self.run_rows([alignment("m1", [before], [after])], [benchmark])[0]
        self.assertEqual(result["status"], "scored")
        self.assertEqual(result["previous_year"], "2024")
        self.assertEqual(result["comparison_previous_year"], "2023")
        self.assertEqual(result["comparison_current_year"], "2024")
        self.assertEqual(result["period_matching"], "filing_accessions")

    def test_different_filings_never_match_recurring_text_despite_equal_year_columns(self):
        before, after = sentence("previous", 1, "Sentence A."), sentence("current", 2, "Sentence B.")
        before["source_url"] = "https://www.sec.gov/Archives/edgar/data/2488/000000248824000012/amd-20231230.htm"
        after["source_url"] = "https://www.sec.gov/Archives/edgar/data/2488/000000248825000012/amd-20241228.htm"
        benchmark = gold()
        benchmark.previous_filing_url = "https://www.sec.gov/Archives/edgar/data/2488/0000002488-23-000047-index.html"
        benchmark.current_filing_url = "https://www.sec.gov/Archives/edgar/data/2488/0000002488-24-000012-index.html"
        result = self.run_rows([alignment("m1", [before], [after])], [benchmark])[0]
        self.assertEqual(result["status"], "missing_prediction")
        self.assertEqual(result["period_matching"], "missing_filing_pair")
        self.assertEqual(result["candidate_match_ids"], "")

    def test_accessions_handle_index_and_archive_url_forms(self):
        self.assertEqual(filing_accession("https://www.sec.gov/Archives/edgar/data/2488/0000002488-24-000012-index.html"), "000000248824000012")
        self.assertEqual(filing_accession("https://www.sec.gov/Archives/edgar/data/2488/000000248824000012/amd.htm"), "000000248824000012")
        self.assertEqual(filing_accession(""), "")

    def test_formatting_normalization_preserves_financial_changes(self):
        self.assertEqual(normalize_text("◦Text\u00a0 ‘quoted’."), "Text 'quoted'.")
        self.assertNotEqual(normalize_text("Revenue was $10 million in 2023."),
                            normalize_text("Revenue was $11 million in 2024."))

    def test_metrics_and_coverage_have_independent_denominators(self):
        rows = [dict(status="scored", gold_taxonomy="New", predicted_taxonomy="New"),
                dict(status="scored", gold_taxonomy="Modified", predicted_taxonomy="New"),
                dict(status="pair_mismatch", gold_taxonomy="Modified", predicted_taxonomy="")]
        metrics = calculate_metrics(rows)
        self.assertEqual(metrics["accuracy"], 0.5)
        self.assertAlmostEqual(metrics["coverage"], 2 / 3)
        self.assertEqual(metrics["per_class"]["New"]["precision"], 0.5)
        self.assertAlmostEqual(metrics["per_class"]["New"]["f1"], 2 / 3)
        self.assertAlmostEqual(metrics["macro_f1"], (2 / 3) / 6)
        self.assertAlmostEqual(metrics["per_class"]["Modified"]["coverage"], 0.5)
        empty = calculate_metrics([])
        self.assertIsNone(empty["accuracy"])
        self.assertIsNone(empty["macro_f1"])
        self.assertEqual(sum(sum(row.values()) for row in metrics["confusion_matrix"].values()), 2)

    def test_conflicting_sentence_evidence_and_duplicate_predictions_fail(self):
        row = alignment("m1", [sentence("previous", 1, "Sentence A.")], [sentence("current", 2, "Sentence B.")])
        conflicting = alignment("m2", [sentence("previous", 1, "Conflicting text.")], [sentence("current", 3, "Other text.")])
        with self.assertRaisesRegex(ValueError, "Contradictory text"):
            self.run_rows([row, conflicting], [gold()])
        with self.assertRaisesRegex(ValueError, "duplicate match_id"):
            self.run_rows([row, copy.deepcopy(row)], [gold()])

    def test_cli_writes_auditable_outputs_without_mutating_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            annotation = root / "benchmark.csv"
            columns = (*REQUIRED_COLUMNS, "Column 9")
            values = {name: "" for name in columns}
            values.update({"Record ID": "r1", "Company": "AMD", "Previous Fiscal Year": "2023",
                           "Current Fiscal Year": "2024", "Previous Disclosure Text": 'A, "quoted".\nNext line.',
                           "Current Disclosure Text": "Sentence B.", "Change Taxonomy": "Modified", "Column 9": "1A"})
            with annotation.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerow(values)
            source = root / "results/amd/2023-2024/alignments_result.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(document([alignment("m1", [sentence("previous", 1, values["Previous Disclosure Text"])],
                                                               [sentence("current", 2, "Sentence B.")])])), encoding="utf-8")
            originals = {path: path.read_bytes() for path in (annotation, source)}
            output = root / "evaluation"
            argv = ["--annotation", str(annotation), "--results-dir", str(root / "results"), "--output-dir", str(output)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            first = {path.name: path.read_bytes() for path in output.iterdir()}
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
            self.assertEqual(first, {path.name: path.read_bytes() for path in output.iterdir()})
            self.assertTrue(all(path.read_bytes() == content for path, content in originals.items()))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["overall"]["scored_rows"], 1)
            self.assertEqual(summary["by_period"]["2023-2024"]["accuracy"], 1)
            self.assertEqual(summary["by_item"]["1A"]["accuracy"], 1)
            self.assertEqual(summary["available_comparisons"]["benchmark_rows"], 1)
            self.assertEqual(summary["coverage_accounting"]["scored_two_sided_rows"], 1)
            self.assertEqual(summary["alignment_inventory"][0]["scored_alignment_groups"], 1)
            self.assertEqual(set(first), {"summary.json", "evaluation_report.html", "evaluation_report.md", "row_results.csv",
                                          "per_class_metrics.csv", "confusion_matrix.csv"})
            report = (output / "evaluation_report.html").read_text()
            self.assertIn("1 ÷ 1 × 100 = 100.0%", report)
            self.assertIn("The denominator comes from the <strong>annotation CSV</strong>", report)
            self.assertIn("Groups supplying scored rows", report)
            self.assertIn("uniform mapped gold label", report)
            markdown = (output / "evaluation_report.md").read_text()
            self.assertTrue(markdown.startswith("# AMD change taxonomy evaluation\n"))
            self.assertIn("1 ÷ 1 × 100 = 100.0%", markdown)
            self.assertIn("| Metric | Value | Basis |", markdown)
            self.assertIn("| Gold / Predicted |", markdown)
            self.assertIn("| Input file | SHA-256 |", markdown)
            self.assertIn("```text\nSaved alignment:", markdown)
            self.assertIn("[Full JSON summary](summary.json)", markdown)
            self.assertNotIn("<style>", markdown)
            self.assertNotIn("<table>", markdown)
            with (output / "row_results.csv").open(newline="") as stream:
                self.assertEqual(next(csv.DictReader(stream))["previous_text"], values["Previous Disclosure Text"])
            with (output / "confusion_matrix.csv").open(newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2 * len(LABELS) ** 2)

    def test_alignment_inventory_expands_deduplicates_and_retains_original_links(self):
        split = alignment("split", [sentence("previous", 1, "Sentence A.")],
                          [sentence("current", 2, "Sentence B.")])
        split["relationship"] = "one_to_many"
        split["current_ids"].append("new_extra")
        split["current_disclosures"].append({"disclosure_id": "new_extra", "item": "1A"})
        repeat = copy.deepcopy(split)
        repeat["match_id"] = "repeat"
        repeat["relationship"] = "one_to_one"
        repeat["current_ids"] = repeat["current_ids"][:1]
        repeat["current_disclosures"] = repeat["current_disclosures"][:1]
        chain = alignment("chain", [sentence("previous", 3, "Sentence C.")],
                          [sentence("current", 4, "Sentence D.")])
        chain["relationship"] = "many_to_many"
        chain["previous_ids"].append("old_other")
        chain["previous_disclosures"].append({"disclosure_id": "old_other", "item": "1A"})
        chain["current_ids"].append("new_other")
        chain["current_disclosures"].append({"disclosure_id": "new_other", "item": "1A"})
        chain["grouping"] = {"source_alignments": [
            {"previous_ids": [chain["previous_ids"][0]], "current_ids": ["new_other"]},
            {"previous_ids": ["old_other"], "current_ids": [chain["current_ids"][0]]},
        ]}
        introduced = alignment("new", [], [sentence("current", 5, "Introduced.")], "New", "unmatched")
        review = alignment("review", [sentence("previous", 6, "Review A.")],
                           [sentence("current", 7, "Review B.")], status="needs_review")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "alignments_result.json"
            path.write_text(json.dumps(document([split, repeat, chain, introduced, review])))
            predictions, _ = load_predictions([path], "amd")
            scored = [{"company": "amd", "comparison_previous_year": "2023", "comparison_current_year": "2024",
                       "match_id": "split", "status": "scored"}]
            inventory = summarize_alignment_inventory(predictions, scored, ACCEPTED_STATUSES)[0]
        self.assertEqual(inventory["finalized_groups"], 4)
        self.assertEqual(inventory["review_groups"], 1)
        self.assertEqual(inventory["pair_entries"], 7)
        self.assertEqual(inventory["unique_expanded_pairs"], 6)
        self.assertEqual(inventory["duplicate_pair_entries"], 1)
        self.assertEqual(inventory["unique_supported_pairs"], 4)
        self.assertEqual(inventory["unsupported_expanded_pairs"], 2)
        self.assertEqual(inventory["unique_one_sided_cases"], 1)
        self.assertEqual(inventory["scored_alignment_groups"], 1)

    def test_report_handles_unavailable_comparisons_and_escapes_input_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            annotation = root / "benchmark<&>.csv"
            columns = (*REQUIRED_COLUMNS, "Item")
            row = dict.fromkeys(columns, "")
            row.update({"Record ID": "r1", "Company": "AMD", "Previous Fiscal Year": "2021",
                        "Current Fiscal Year": "2022", "Current Disclosure Text": "Introduced.",
                        "Change Taxonomy": "New", "Item": "1A"})
            with annotation.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=columns)
                writer.writeheader()
                writer.writerow(row)
            source = root / "results/amd/2023-2024/alignments_result.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps(document([
                alignment("new", [], [sentence("current", 1, "Introduced.")], "New", "unmatched")
            ])))
            output = root / "evaluation"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--annotation", str(annotation), "--results-dir", str(root / "results"),
                                       "--output-dir", str(output)]), 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["coverage_accounting"]["unavailable_benchmark_rows"], 1)
            self.assertEqual(summary["available_comparisons"]["benchmark_rows"], 0)
            self.assertIsNone(summary["available_comparisons"]["coverage"])
            self.assertIsNone(summary["overall"]["accuracy"])
            report = (output / "evaluation_report.html").read_text()
            self.assertIn("Unavailable: 0 annotation rows in this scope", report)
            self.assertIn("benchmark&lt;&amp;&gt;.csv", report)
            self.assertNotIn("benchmark<&>.csv", report)
            markdown = (output / "evaluation_report.md").read_text()
            self.assertIn("Unavailable: 0 annotation rows in this scope", markdown)
            self.assertIn("benchmark&lt;&amp;&gt;.csv", markdown)
            self.assertNotIn("benchmark<&>.csv", markdown)

    def test_annotation_rejects_duplicate_record_ids_and_unknown_labels(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "gold.csv"
            columns = (*REQUIRED_COLUMNS, "Item")
            row = {name: "" for name in columns}
            row.update({"Record ID": "r1", "Company": "AMD", "Previous Fiscal Year": "2023",
                        "Current Fiscal Year": "2024", "Current Disclosure Text": "New text.", "Change Taxonomy": "New", "Item": "1A"})
            def write(rows):
                with path.open("w", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(rows)
            write([row, row])
            with self.assertRaisesRegex(ValueError, "duplicate Record ID"):
                load_gold(path, "amd", set())
            row["Change Taxonomy"] = "Unsupported"
            write([row])
            with self.assertRaisesRegex(ValueError, "unknown Change Taxonomy"):
                load_gold(path, "amd", set())


if __name__ == "__main__":
    unittest.main()
