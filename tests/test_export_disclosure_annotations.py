import copy
import csv
import io
import json
import unittest
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory

from export_disclosure_annotations import COLUMNS, DEFAULT_MATCH_THRESHOLD, fill_original_paragraphs, load_paragraphs, main, make_annotations, pair_sentences, quoted_tsv, section_title_key, similarity


def sentence(source_id, text):
    return {"source_id": source_id, "sentence": text}


def comparison():
    return {
        "company": "mu", "old_year": "2024", "new_year": "2025",
        "items": [{
            "item": "1", "item_default_title": "Business",
            "item_titles": [{
                "old_item_title": "Old heading", "new_item_title": "New heading",
                "item_title": "2024: Old heading -> 2025: New heading", "title_match": "manual",
                "2024_only": [
                    sentence("2024_1_P001", 'Micron says "Hello".\tDRAM β\nLine two.'),
                    sentence("2024_1_P001", "Another sentence."),
                    sentence("2024_1_P002", "A different paragraph."),
                ],
                "2025_only": [sentence("2025_1_P009", "Current disclosure.")],
            }, {
                "old_item_title": "", "new_item_title": "New section",
                "2024_only": [],
                "2025_only": [sentence("2025_1_P010", "Regulators opened an investigation.")],
            }, {
                "old_item_title": "Old section", "new_item_title": "",
                "2024_only": [sentence("2024_1_P003", "Archived leases expired.")],
                "2025_only": [],
            }],
        }, {
            "item": "15", "item_default_title": "Exhibits", "item_titles": [{
                "item_title": "Exhibits", "2024_only": [],
                "2025_only": [sentence("2025_15_P001", "An exhibit.")],
            }],
        }],
    }


def write_chunk_files(document, root):
    paths = {}
    for year in (document["old_year"], document["new_year"]):
        grouped = {}
        for item in document["items"]:
            for group in item["item_titles"]:
                for entry in group[year + "_only"]:
                    chunk = grouped.setdefault(entry["source_id"], {
                        "id": entry["source_id"], "year": year, "company": document["company"],
                        "item": item["item"], "text": "Unchanged background sentence.\n",
                    })
                    chunk["text"] += entry["sentence"] + "\n"
        path = root / document["company"] / year / f"{year}_chunks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(list(grouped.values())), encoding="utf-8")
        paths[year] = path
    return paths


def heading_comparison(old_title, new_title, item="8"):
    return {
        "company": "mu", "old_year": "2024", "new_year": "2025",
        "items": [{"item": item, "item_default_title": "Financial Statements", "item_titles": [{
            "item_title": old_title, "old_item_title": old_title, "new_item_title": "", "title_match": "old_title_only",
            "2024_only": [sentence("2024_8_P001", "Deferred taxes are recognized."), sentence("2024_8_P002", "Tax expense increased during fiscal 2024.")],
            "2025_only": [],
        }, {
            "item_title": new_title, "old_item_title": "", "new_item_title": new_title, "title_match": "new_title_only",
            "2024_only": [],
            "2025_only": [sentence("2025_8_P009", "Deferred taxes are recognized."), sentence("2025_8_P010", "Tax expense increased during fiscal 2025.")],
        }]}],
    }


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_payload = False
        self.payload_text = ""
        self.row_sizes = []
        self.cells = None

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "clipboard-data":
            self.in_payload = True
        if tag == "tr":
            self.cells = 0
        if tag in ("td", "th") and self.cells is not None:
            self.cells += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_payload = False
        if tag == "tr":
            self.row_sizes.append(self.cells)
            self.cells = None

    def handle_data(self, data):
        if self.in_payload:
            self.payload_text += data


class DisclosureExporterTests(unittest.TestCase):
    def test_item8_note_variants_match_and_keep_original_headings(self):
        for old_title, new_title in [
            ("Note 25. Income Taxes", "income taxes"),
            ("Income Taxes", "NOTE 25. INCOME TAXES"),
            ("Note 25. Income Taxes", "Note 26. Income Taxes"),
            ("Note 25 – Income Taxes", "Income Taxes:"),
            ("Note No. 25: Income Taxes", "income taxes"),
            ("Note (25) Income Taxes", "INCOME  TAXES"),
            ("Note 25.Income Taxes", "Income Taxes"),
        ]:
            with self.subTest(old=old_title, new=new_title):
                document = heading_comparison(old_title, new_title)
                original = copy.deepcopy(document)
                stats = {}
                records = make_annotations(document, stats=stats)
                self.assertEqual(document, original)
                self.assertEqual(len(records), 1)
                self.assertEqual(stats["unchanged_sentence_pairs_removed"], 1)
                self.assertEqual(records[0]["Previous Section / Subsection"], old_title)
                self.assertEqual(records[0]["Current Section / Subsection"], new_title)
                self.assertEqual(records[0]["Previous Paragraph / Chunk ID"], "2024_8_P002")
                self.assertEqual(records[0]["Current Paragraph / Chunk ID"], "2025_8_P010")
                self.assertNotIn("Deferred taxes", records[0]["Previous Disclosure Text"])

    def test_heading_normalization_does_not_merge_different_topics_or_strip_other_numbers(self):
        for old_title, new_title in [
            ("Note 25. Income Taxes", "Interest Expense"),
            ("Note 25. Income Taxes", "Income Taxes and Other Taxes"),
            ("Note 25.", "Note 26."),
            ("Note 25 -", "Note 26 -"),
            ("Level 2 Investments", "Level 3 Investments"),
        ]:
            with self.subTest(old=old_title, new=new_title):
                self.assertNotEqual(section_title_key("8", old_title), section_title_key("8", new_title))
                # Different headings remain distinct metadata, but the reference
                # matcher may now find moved/rewritten sentences across them.
                records = make_annotations(heading_comparison(old_title, new_title))
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["Previous Section / Subsection"], old_title)
                self.assertEqual(records[0]["Current Section / Subsection"], new_title)
        self.assertNotEqual(section_title_key("1", "Note 25. Income Taxes"), section_title_key("1", "Income Taxes"))
        self.assertEqual(section_title_key("7", "INCOME TAXES"), section_title_key("7", "income taxes"))
        document = heading_comparison("Income Taxes", "income taxes")
        document["items"].append({"item": "7", "item_titles": [document["items"][0]["item_titles"].pop()]})
        self.assertEqual(len(make_annotations(document)), 4)

    def test_equivalent_heading_duplicate_sentences_are_removed_by_occurrence_count(self):
        document = heading_comparison("Income Taxes", "Note 25. Income Taxes")
        previous = document["items"][0]["item_titles"][0]["2024_only"]
        previous.append(sentence("2024_8_P003", "Deferred taxes are recognized."))
        stats = {}
        records = make_annotations(document, stats=stats)
        self.assertEqual(len(records), 2)
        self.assertEqual(stats["unchanged_sentence_pairs_removed"], 1)
        repeated = [r for r in records if r["Previous Disclosure Text"] == "Deferred taxes are recognized."]
        self.assertEqual(len(repeated), 1)
        self.assertEqual(repeated[0]["Previous Paragraph / Chunk ID"], "2024_8_P003")
        self.assertEqual(repeated[0]["Current Disclosure Text"], "")

    def test_explicit_title_map_is_not_overridden_by_an_automatic_note_alias(self):
        document = heading_comparison("Income Taxes", "Note 25. Income Taxes")
        groups = document["items"][0]["item_titles"]
        groups[0]["title_match"] = "manual"
        groups[0]["new_item_title"] = "Other Taxes"
        groups[0]["2025_only"] = [sentence("2025_8_P020", "Tax expense increased during fiscal 2025.")]
        records = make_annotations(document)
        paired = [r for r in records if r["Previous Disclosure Text"] and r["Current Disclosure Text"]]
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["Current Section / Subsection"], "Other Taxes")
        self.assertEqual(paired[0]["Current Paragraph / Chunk ID"], "2025_8_P020")
        automatic = [r for r in records if r["Current Section / Subsection"] == "Note 25. Income Taxes"]
        self.assertEqual(len(automatic), 1)
        self.assertTrue(all(not r["Previous Disclosure Text"] for r in automatic))
        # The identical sentence moved to the automatic heading is unchanged.
        self.assertFalse(any(r["Previous Disclosure Text"] == "Deferred taxes are recognized." for r in records))

    def test_cli_exports_note_alias_match_with_original_paragraph_context(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = heading_comparison("Income Taxes", "Note 25. Income Taxes")
            source = root / "input.json"
            source.write_text(json.dumps(document))
            write_chunk_files(document, root / "raw")
            output = root / "export"
            messages = io.StringIO()
            with redirect_stdout(messages):
                code = main(["--input", str(source), "--chunks-root", str(root / "raw"), "--output-dir", str(output)])
            self.assertEqual(code, 0)
            with (output / "disclosure_annotations.tsv").open(newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(len(rows), 1)
            self.assertEqual(len(rows[0]), 20)
            self.assertEqual(rows[0]["Current Section / Subsection"], "Note 25. Income Taxes")
            self.assertIn("Unchanged background sentence.", rows[0]["Previous Original Paragraph"])
            self.assertIn("Tax expense increased during fiscal 2025.", rows[0]["Current Original Paragraph"])
            self.assertIn("Additional unchanged sentence pairs removed: 1", messages.getvalue())

    def test_sentence_rows_preserve_text_ids_titles_and_unmatched_sides(self):
        document = comparison()
        original = copy.deepcopy(document)
        records = make_annotations(document, items=["1"], company="Micron", industry="Semiconductors")
        self.assertEqual(document, original)
        self.assertEqual(len(records), 6)
        first = records[0]
        self.assertEqual(list(first), COLUMNS)
        self.assertEqual(first["Company"], "Micron")
        self.assertEqual(first["Industry"], "Semiconductors")
        self.assertEqual(first["Split"], "")
        self.assertEqual(first["Previous Section / Subsection"], "Old heading")
        self.assertEqual(first["Previous Paragraph / Chunk ID"], "2024_1_P001")
        self.assertEqual(first["Previous Disclosure Text"], 'Micron says "Hello".\tDRAM β\nLine two.')
        self.assertEqual(records[1]["Previous Paragraph / Chunk ID"], "2024_1_P001")
        self.assertEqual(records[1]["Previous Disclosure Text"], "Another sentence.")
        self.assertEqual(records[2]["Previous Paragraph / Chunk ID"], "2024_1_P002")
        current = next(r for r in records if r["Current Paragraph / Chunk ID"] == "2025_1_P009")
        self.assertEqual(current["Current Section / Subsection"], "New heading")
        self.assertEqual(current["Current Disclosure Text"], "Current disclosure.")
        for record in records:
            blank_columns = COLUMNS[8:11] if not record["Previous Disclosure Text"] else COLUMNS[11:14]
            self.assertEqual([record[column] for column in blank_columns], ["", "", ""])
            self.assertEqual([record[column] for column in COLUMNS[14:]], [""] * 6)
            self.assertNotIn(";", record["Previous Paragraph / Chunk ID"])
            self.assertNotIn(";", record["Current Paragraph / Chunk ID"])

    def test_matches_reordered_sentences_by_wording_with_insertions_and_removals(self):
        old = [
            sentence("2024_1_P004", "We increased investment in DRAM production capacity in 2024."),
            sentence("2024_1_P004", "Employee training was delivered in person."),
            sentence("2024_1_P005", "We expanded our sales offices across the European market."),
        ]
        new = [
            sentence("2025_1_P030", "We expanded our sales offices across the Asian market."),
            sentence("2025_1_P031", "A hurricane disrupted the coastal warehouse."),
            sentence("2025_1_P032", "We increased investment in DRAM production capacity in 2025."),
        ]
        pairs = pair_sentences(old, new, 0.75)
        self.assertEqual(pairs, [(old[0], new[2]), (old[1], None), (old[2], new[0]), (None, new[1])])

    def test_reference_tie_breaking_is_one_to_one_and_preserves_duplicates(self):
        old = [sentence("2024_1_P001", "We invested in production capacity in 2024.")]
        new = [
            sentence("2025_1_P001", "We invested in production capacity in 2025."),
            sentence("2025_1_P002", "We invested in production capacity in 2026."),
        ]
        # The reference's descending candidate order resolves equal scores in
        # favour of the later current occurrence; it has no ambiguity margin.
        self.assertEqual(pair_sentences(old, new, 0.75), [(old[0], new[1]), (None, new[0])])
        # Identical text and chunk IDs are still two separate occurrences.
        duplicates = [old[0], copy.deepcopy(old[0])]
        pairs = pair_sentences(duplicates, [new[0]], 0.75)
        self.assertIsNone(pairs[0][1])
        self.assertIs(pairs[0][0], duplicates[0])
        self.assertIs(pairs[1][0], duplicates[1])
        self.assertIs(pairs[1][1], new[0])

    def test_matching_crosses_subsections_but_not_items_and_preserves_occurrences(self):
        document = comparison()
        group = document["items"][0]["item_titles"][0]
        old_text = "We increased investment in DRAM production capacity in 2024."
        new_text = "We increased investment in DRAM production capacity in 2025."
        group["2024_only"] = [sentence("2024_1_P004", old_text), sentence("2024_1_P004", "A second unrelated disclosure.")]
        group["2025_only"] = []
        document["items"][0]["item_titles"][1]["2025_only"] = [sentence("2025_1_P020", new_text)]
        # An exact copy in another Item cannot consume the previous sentence.
        document["items"][1]["item_titles"][0]["2025_only"] = [sentence("2025_15_P001", old_text)]
        records = make_annotations(document)
        self.assertEqual(records[0]["Previous Disclosure Text"], old_text)
        self.assertEqual(records[0]["Current Disclosure Text"], new_text)
        self.assertEqual(records[0]["Previous Section / Subsection"], "Old heading")
        self.assertEqual(records[0]["Current Section / Subsection"], "New section")
        for year, side in (("2024", "Previous"), ("2025", "Current")):
            expected = Counter((item["item"], entry["source_id"], entry["sentence"])
                               for item in document["items"] for section in item["item_titles"]
                               for entry in section[year + "_only"])
            actual = Counter((row["Item"], row[side + " Paragraph / Chunk ID"], row[side + " Disclosure Text"])
                             for row in records if row[side + " Disclosure Text"])
            self.assertEqual(actual, expected)

    def test_match_threshold_rejects_weak_matches_and_invalid_values(self):
        old = [sentence("old", "Revenue increased in 2024.")]
        new = [sentence("new", "Revenue increased in 2025.")]
        self.assertEqual(pair_sentences(old, new, 0.75), [(old[0], new[0])])
        self.assertEqual(pair_sentences(old, new, 1.0), [(old[0], None), (None, new[0])])
        self.assertEqual(pair_sentences(old, new, 0), [(old[0], new[0])])
        for value in (-0.1, 1.1, float("nan")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "match-threshold"):
                make_annotations(comparison(), match_threshold=value)

    def test_mbu_and_ebu_annual_revenue_updates_match_using_reference_scores(self):
        old = [
            sentence("2023_1_P014", "MBU reported revenue of $3.63 billion in 2023, $7.26 billion in 2022, and $7.20 billion in 2021."),
            sentence("2023_1_P016", "EBU reported revenue of $3.64 billion in 2023, $5.24 billion in 2022, and $4.21 billion in 2021."),
        ]
        new = [
            sentence("2024_1_P035", "MBU reported revenue of $6.35 billion in 2024, $3.63 billion in 2023, and $7.26 billion in 2022."),
            sentence("2024_1_P040", "EBU reported revenue of $4.61 billion 2024, $3.64 billion in 2023, and $5.24 billion in 2022."),
        ]
        self.assertEqual(DEFAULT_MATCH_THRESHOLD, 0.55)
        # Captured from filing_sentence_annotator, without masking numbers/years.
        self.assertAlmostEqual(similarity(old[0]["sentence"], new[0]["sentence"]), 0.6955, places=4)
        self.assertAlmostEqual(similarity(old[1]["sentence"], new[1]["sentence"]), 0.7074, places=4)
        self.assertEqual(pair_sentences(old, new, DEFAULT_MATCH_THRESHOLD), [(old[0], new[0]), (old[1], new[1])])
        self.assertEqual(pair_sentences(old[:1], new[:1], 0.75), [(old[0], None), (None, new[0])])

    def test_exact_sentences_are_reserved_before_rewrites_even_across_sections(self):
        old = [
            sentence("old0", "We invest in manufacturing capacity."),
            sentence("old1", "We invest in research facilities."),
        ]
        new = [
            sentence("new0", "We invest in manufacturing facilities."),
            sentence("new1", old[0]["sentence"]),
        ]
        old[0]["_section_title"] = "Old heading"
        new[1]["_section_title"] = "Relocated heading"
        self.assertEqual(pair_sentences(old, new, DEFAULT_MATCH_THRESHOLD), [(old[0], new[1]), (old[1], new[0])])
        document = heading_comparison("Old heading", "Relocated heading")
        groups = document["items"][0]["item_titles"]
        groups[0]["2024_only"], groups[1]["2025_only"] = old, new
        stats = {}
        rows = make_annotations(document, stats=stats)
        self.assertEqual(stats["unchanged_sentence_pairs_removed"], 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Previous Paragraph / Chunk ID"], "old1")
        self.assertEqual(rows[0]["Current Paragraph / Chunk ID"], "new0")

    def test_section_bonus_ranks_candidates_but_does_not_lower_threshold(self):
        old = [
            {**sentence("old0", "Revenue increased in 2024."), "_section_title": "Sales"},
            {**sentence("old1", "Revenue increased in 2024."), "_section_title": "Elsewhere"},
        ]
        new = [{**sentence("new0", "Revenue increased in 2025."), "_section_title": "Sales"}]
        self.assertEqual(pair_sentences(old, new, DEFAULT_MATCH_THRESHOLD), [(old[0], new[0]), (old[1], None)])
        threshold = similarity(old[0]["sentence"], new[0]["sentence"]) + 0.01
        self.assertEqual(pair_sentences(old, new, threshold), [(old[0], None), (old[1], None), (None, new[0])])

    def test_cli_outputs_tsv_and_clipboard_payload_with_identical_cells(self):
        document = comparison()
        document["items"][0]["item_titles"][0]["2025_only"][0]["sentence"] = '=1+1\r\n</script><script>alert("x")</script>'
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "all_items_diff.json", root / "export"
            source.write_text(json.dumps(document), encoding="utf-8")
            paths = write_chunk_files(document, root / "raw")
            original_bytes = {year: path.read_bytes() for year, path in paths.items()}
            before = source.read_bytes()
            with redirect_stdout(io.StringIO()):
                code = main(["--input", str(source), "--output-dir", str(output), "--items", "1", "--chunks-root", str(root / "raw")])
            self.assertEqual(code, 0)
            self.assertEqual(source.read_bytes(), before)
            with (output / "disclosure_annotations.tsv").open(newline="") as handle:
                headered = list(csv.reader(handle, delimiter="\t", strict=True))
            with (output / "paste_into_sheets.tsv").open(newline="") as handle:
                rows = list(csv.reader(handle, delimiter="\t", strict=True))
            self.assertEqual(headered, [COLUMNS, *rows])
            self.assertEqual(len(rows), 6)
            self.assertTrue(all(len(row) == 20 for row in headered))
            self.assertEqual(headered[0][-4:], ["Materiality", "Rationale / Evidence", "Previous Original Paragraph", "Current Original Paragraph"])
            unsafe_row = next(row for row in rows if row[12] == "2025_1_P009")
            self.assertEqual(unsafe_row[13], "'" + document["items"][0]["item_titles"][0]["2025_only"][0]["sentence"])
            for year, path in paths.items():
                self.assertEqual(path.read_bytes(), original_bytes[year])
                originals = {chunk["id"]: chunk["text"] for chunk in json.loads(path.read_text())}
                id_index, paragraph_index = (9, 18) if year == "2024" else (12, 19)
                for row in rows:
                    self.assertEqual(row[paragraph_index], originals[row[id_index]] if row[id_index] else "")
                    self.assertEqual(row[17], "")
            page = (output / "paste_into_sheets.html").read_text()
            self.assertNotIn('</script><script>alert("x")', page)
            parser = PageParser()
            parser.feed(page)
            self.assertEqual(parser.row_sizes, [20] * 7)
            payload = json.loads(parser.payload_text)
            self.assertEqual(list(csv.reader(io.StringIO(payload["rows"]["text"], newline=""), delimiter="\t")), rows)
            self.assertEqual(list(csv.reader(io.StringIO(payload["with_headers"]["text"], newline=""), delimiter="\t")), headered)
            for key, expected_rows in (("rows", 6), ("with_headers", 7)):
                cells = PageParser()
                cells.feed(payload[key]["html"])
                self.assertEqual(cells.row_sizes, [20] * expected_rows)

    def test_empty_diff_exports_headers_without_stale_rows(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "item_1_diff.json", root / "export"
            source.write_text(json.dumps(comparison()), encoding="utf-8")
            write_chunk_files(comparison(), root / "raw")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--input", str(source), "--output-dir", str(output), "--chunks-root", str(root / "raw")]), 0)
                document = comparison()
                document["items"][0]["item_titles"] = []
                document["items"] = document["items"][:1]
                source.write_text(json.dumps(document), encoding="utf-8")
                self.assertEqual(main(["--input", str(source), "--output-dir", str(output)]), 0)
            self.assertEqual((output / "paste_into_sheets.tsv").read_text(), "")
            self.assertEqual((output / "disclosure_annotations.tsv").read_text(), quoted_tsv([COLUMNS]))

    def test_original_paragraph_joins_only_chunks_with_same_item_source_and_block(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "2024_chunks.json"
            chunks = [
                {"id": "2024_1_P001", "item": "1", "source_block_index": 10, "source": "filing", "text": "First sentence."},
                {"id": "2024_1_P002", "item": "1", "source_block_index": 10, "source": "filing", "text": "Second sentence with unchanged context."},
                {"id": "2024_1_P003", "item": "1", "source_block_index": 11, "source": "filing", "text": "Another paragraph."},
                {"id": "2024_7_P001", "item": "7", "source_block_index": 10, "source": "filing", "text": "Another item."},
                {"id": "2024_1_P004", "item": "1", "source_block_index": 10, "source": "other", "text": "Another source."},
                {"id": "2024_1_P005", "item": "1", "text": "Legacy chunk without block metadata."},
            ]
            path.write_text(json.dumps(chunks), encoding="utf-8")
            index = load_paragraphs(path, "2024", "mu")
            full = "First sentence. Second sentence with unchanged context."
            self.assertEqual(index["2024_1_P001"]["paragraph"], full)
            self.assertEqual(index["2024_1_P002"]["paragraph"], full)
            for chunk in chunks[2:]:
                self.assertEqual(index[chunk["id"]]["paragraph"], chunk["text"])

    def test_explicit_chunk_paths_populate_only_present_side(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = {"company": "mu", "old_year": "2024", "new_year": "2025", "items": [{
                "item": "1", "item_titles": [{"item_title": "Business", "2024_only": [],
                "2025_only": [sentence("2025_1_P001", 'Micron\'s "platform" expanded - significantly.')]}],
            }]}
            current = root / "current.json"
            full_paragraph = "Unchanged introduction. Micron’s “platform” expanded — significantly. Unchanged conclusion."
            current.write_text(json.dumps([{"id": "2025_1_P001", "year": "2025", "company": "mu", "item": "1", "text": full_paragraph}]))
            records = make_annotations(document)
            fill_original_paragraphs(records, document, root / "input.json", previous_json=root / "missing.json", current_json=current)
            self.assertEqual(records[0]["Previous Original Paragraph"], "")
            self.assertEqual(records[0]["Current Original Paragraph"], full_paragraph)
            self.assertEqual(records[0]["Rationale / Evidence"], "")

    def test_missing_or_stale_context_does_not_replace_successful_export(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = comparison()
            source, output = root / "input.json", root / "export"
            source.write_text(json.dumps(document), encoding="utf-8")
            output.mkdir()
            saved = output / "paste_into_sheets.tsv"
            saved.write_text("Keep the previous successful export.")
            for problem in ("missing_file", "missing_id", "wrong_text", "wrong_year", "wrong_company", "wrong_item", "duplicate_id"):
                with self.subTest(problem=problem):
                    paths = write_chunk_files(document, root / "raw")
                    chunks = json.loads(paths["2024"].read_text())
                    if problem == "missing_id":
                        chunks[0]["id"] = "missing-reference"
                    elif problem == "wrong_text":
                        chunks[0]["text"] = "Unrelated stale extraction."
                    elif problem == "wrong_year":
                        chunks[0]["year"] = "2023"
                    elif problem == "wrong_company":
                        chunks[0]["company"] = "nvda"
                    elif problem == "wrong_item":
                        chunks[0]["item"] = "8"
                    elif problem == "duplicate_id":
                        chunks.append(copy.deepcopy(chunks[0]))
                    paths["2024"].write_text(json.dumps(chunks))
                    previous_path = root / "missing.json" if problem == "missing_file" else paths["2024"]
                    errors = io.StringIO()
                    with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                        code = main(["--input", str(source), "--output-dir", str(output), "--previous-json", str(previous_path), "--current-json", str(paths["2025"])])
                    self.assertEqual(code, 2)
                    self.assertIn("Error:", errors.getvalue())
                    self.assertEqual(saved.read_text(), "Keep the previous successful export.")

    def test_invalid_data_reports_error_without_replacing_outputs(self):
        bad_sentence = comparison()
        del bad_sentence["items"][0]["item_titles"][0]["2024_only"][0]["source_id"]
        invalid_inputs = [[], {}, {**comparison(), "old_year": "unknown"}, bad_sentence]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / "input.json", root / "export"
            output.mkdir()
            saved = output / "paste_into_sheets.tsv"
            saved.write_text("Previous successful export", encoding="utf-8")
            for invalid in invalid_inputs:
                with self.subTest(invalid=invalid):
                    source.write_text(json.dumps(invalid), encoding="utf-8")
                    errors = io.StringIO()
                    with redirect_stderr(errors):
                        self.assertEqual(main(["--input", str(source), "--output-dir", str(output)]), 2)
                    self.assertIn("Error:", errors.getvalue())
                    self.assertEqual(saved.read_text(), "Previous successful export")

    def test_missing_item_filter_fails_instead_of_silently_exporting_nothing(self):
        with self.assertRaisesRegex(ValueError, "absent from input"):
            make_annotations(comparison(), items=["7"])


if __name__ == "__main__":
    unittest.main()
