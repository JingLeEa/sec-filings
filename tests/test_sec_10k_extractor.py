import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.extraction.sec_10k_extractor import (
    FilingBlock,
    build_sentence_records,
    build_records,
    build_records_from_section_blocks,
    discover_10k_filing,
    extract_indexed_section_blocks,
    extract_item15_toc_section_blocks,
    extract_section_blocks,
    extract_sections,
    html_to_blocks,
    html_to_clean_text,
    html_to_structure_events,
    infer_company_year_from_filename,
    is_subheader_block,
    merge_continued_blocks,
    make_chunk_id,
    main,
    normalize_source,
    parse_args,
    report_outline,
    referenced_section_ranges,
    resolve_section_footnotes,
    section_blocks_to_text,
    should_drop_line,
)
from pathlib import Path


class ExtractorTests(unittest.TestCase):
    def test_normalizes_sec_ix_viewer_url(self):
        source = "https://www.sec.gov/ix?doc=/Archives/edgar/data/0001045810/000104581023000017/nvda-20230129.htm"

        normalized = normalize_source(source)

        self.assertEqual(
            normalized,
            "https://www.sec.gov/Archives/edgar/data/0001045810/000104581023000017/nvda-20230129.htm",
        )

    def test_normalizes_pasted_markdown_link(self):
        source = (
            "[https://www.sec.gov/ix?doc=/Archives/edgar/data/0001045810/000104581023000017/nvda-20230129.htm]"
            "(https://www.sec.gov/ix?doc=/Archives/edgar/data/0001045810/000104581023000017/nvda-20230129.htm)"
        )

        normalized = normalize_source(source)

        self.assertEqual(
            normalized,
            "https://www.sec.gov/Archives/edgar/data/0001045810/000104581023000017/nvda-20230129.htm",
        )

    def test_infers_company_and_year_from_filing_filename(self):
        company, year = infer_company_year_from_filename(Path("data/raw/nvda-20230129.htm"))

        self.assertEqual(company, "nvda")
        self.assertEqual(year, "2023")

    def test_api_extraction_args_default_to_data_raw(self):
        args = parse_args(["--ticker", "NVDA", "--year", "2024"])

        self.assertEqual(args.ticker, "NVDA")
        self.assertEqual(args.year, "2024")
        self.assertEqual(args.items, ("1", "1A", "7", "8"))
        self.assertEqual(args.out_dir, "data/raw")
        self.assertIsNone(args.source)

    def test_cli_default_items_do_not_include_item15_without_reference_pointer(self):
        lines = [
            "Item 1. Business", "We manufacture memory products.",
            "Item 1A. Risk Factors", "Demand may decrease unexpectedly.",
            "Item 1B. Unresolved Staff Comments", "None.",
            "Item 7. Management's Discussion and Analysis", "Revenue increased this year.",
            "Item 7A. Market Risk", "Interest rates may change.",
            "Item 8. Financial Statements", "These statements include our subsidiaries.",
            "Item 9. Changes in Accountants", "None.",
            "Item 15. Exhibits", "The exhibits are listed here.",
            "Item 16. Form 10-K Summary", "None.",
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "mu-20240829.htm"
            source.write_text("".join(f"<div>{line}</div>" for line in lines), encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = main([str(source), "--out-dir", str(root / "output")])

            self.assertEqual(result, 0)
            output = root / "output" / "mu" / "2024"
            records = json.loads((output / "2024_chunks.json").read_text())
            self.assertEqual({record["item"] for record in records}, {"1", "1A", "7", "8"})
            self.assertFalse((output / "2024_item_15.txt").exists())

    def test_cli_auto_includes_item15_when_item8_points_to_financial_statements(self):
        lines = [
            "Item 1. Business", "We manufacture memory products.",
            "Item 1A. Risk Factors", "Demand may decrease unexpectedly.",
            "Item 1B. Unresolved Staff Comments", "None.",
            "Item 7. Management's Discussion and Analysis", "Revenue increased this year.",
            "Item 7A. Market Risk", "Interest rates may change.",
            "Item 8. Financial Statements",
            "The information required by this Item is set forth in our Consolidated Financial Statements "
            "and Notes thereto included in this Annual Report on Form 10-K.",
            "Item 9. Changes in Accountants", "None.",
            "Item 15. Exhibits", "The financial statement schedules are listed in Part IV.",
            "Item 16. Form 10-K Summary", "None.",
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "nvda-20240128.htm"
            source.write_text("".join(f"<div>{line}</div>" for line in lines), encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = main([str(source), "--out-dir", str(root / "output")])

            self.assertEqual(result, 0)
            output = root / "output" / "nvda" / "2024"
            records = json.loads((output / "2024_chunks.json").read_text())
            self.assertIn("15", {record["item"] for record in records})
            self.assertTrue((output / "2024_item_15.txt").exists())

    def test_cli_can_disable_item15_toc_extraction(self):
        lines = [
            "Item 1. Business", "We manufacture memory products.",
            "Item 1A. Risk Factors", "Demand may decrease unexpectedly.",
            "Item 1B. Unresolved Staff Comments", "None.",
            "Item 7. Management's Discussion and Analysis", "Revenue increased this year.",
            "Item 7A. Market Risk", "Interest rates may change.",
            "Item 8. Financial Statements",
            "The information required by this Item is set forth in our Consolidated Financial Statements "
            "and Notes thereto included in this Annual Report on Form 10-K.",
            "Item 9. Changes in Accountants", "None.",
            "Item 15. Exhibits", "The financial statement schedules are listed in Part IV.",
            "Signatures", "Signed by the registrant.",
        ]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "jpm-20251231.htm"
            source.write_text("".join(f"<div>{line}</div>" for line in lines), encoding="utf-8")

            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), patch(
                "sec_disclosure.extraction.sec_10k_extractor.extract_item15_toc_section_blocks"
            ) as item15_parser:
                result = main([str(source), "--out-dir", str(root / "output"), "--no-item15-toc"])

            self.assertEqual(result, 0)
            item15_parser.assert_not_called()
            output = root / "output" / "jpm" / "2025"
            records = json.loads((output / "2025_chunks.json").read_text())
            self.assertTrue(any(
                record["item"] == "15" and "financial statement schedules" in record["text"]
                for record in records
            ))

    def test_cli_selected_items_exclude_item15_in_both_extraction_paths(self):
        lines = [
            "Item 1. Business", "We manufacture memory products.",
            "Item 1A. Risk Factors", "Demand may decrease unexpectedly.",
            "Item 1B. Unresolved Staff Comments", "None.",
            "Item 7. Management's Discussion and Analysis", "Revenue increased this year.",
            "Item 7A. Market Risk", "Interest rates may change.",
            "Item 8. Financial Statements", "These statements include our subsidiaries.",
            "Item 9. Changes in Accountants", "None.",
            "Item 15. Exhibits", "The exhibits are listed here.",
            "Item 16. Form 10-K Summary", "None.",
        ]
        for tag in ("div", "center"):
            with self.subTest(tag=tag), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "mu-20240829.htm"
                source.write_text("".join(f"<{tag}>{line}</{tag}>" for line in lines), encoding="utf-8")
                errors = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(errors), patch(
                    "sec_disclosure.extraction.sec_10k_extractor.extract_item15_toc_section_blocks"
                ) as item15_parser:
                    result = main([
                        str(source), "--out-dir", str(root / "output"),
                        "--items", "1", "1a", "7", "8",
                    ])
                self.assertEqual(result, 0)
                self.assertEqual(errors.getvalue(), "")
                item15_parser.assert_not_called()
                output = root / "output" / "mu" / "2024"
                records = json.loads((output / "2024_chunks.json").read_text())
                self.assertEqual({record["item"] for record in records}, {"1", "1A", "7", "8"})
                self.assertNotIn("Item 15", (output / "2024_chunks.txt").read_text())
                self.assertFalse((output / "2024_item_15.txt").exists())
                for item in ("1", "1a", "7", "8"):
                    self.assertTrue((output / f"2024_item_{item}.txt").exists())

    def test_page_report_footers_are_dropped_before_heading_detection(self):
        labels = [
            "59 |2025 10-K", "11 |2024 10-K", "59|2025 10-K",
            " 59 \u00a0| 2025 10-K ", "2025 10-K | 59",
            "Page 59 | 2025 Form 10-K", "2025 FORM 10-K | Page 59",
            "59 | 2025 10–K", "59 | 2025 10 - k", "59 | 2025 10-K/A",
        ]
        for label in labels:
            with self.subTest(label=label):
                self.assertTrue(should_drop_line(label))
                self.assertFalse(is_subheader_block(FilingBlock(index=1, tag="div", text=label, bold=True)))

    def test_footer_filter_preserves_real_headings_and_report_references(self):
        for text in [
            "Critical Accounting Estimates", "Income Taxes", "Inventories",
            "2025 10-K Reporting Requirements", "Item 7. Management's Discussion and Analysis",
            "See page 59 | 2025 10-K for additional information.",
            "We filed our 2025 Form 10-K.", "59 |2025 10-K disclosure text follows.",
        ]:
            with self.subTest(text=text):
                self.assertFalse(should_drop_line(text))
        for text in ("Critical Accounting Estimates", "Income Taxes", "Inventories"):
            self.assertTrue(is_subheader_block(FilingBlock(index=1, tag="div", text=text, bold=True)))

    def test_footer_does_not_reset_section_title_or_break_continued_paragraph(self):
        html = """
        <html><body>
          <div>Item 7. Management's Discussion and Analysis</div>
          <div style="font-weight:700">Critical Accounting Estimates</div>
          <div>Revenue is recognized when</div>
          <div style="font-weight:700"><span>59 </span><span>|2025 10-K</span></div>
          <div>control transfers to the customer.</div>
          <div>Income taxes: We estimate taxes payable in multiple jurisdictions.</div>
          <div style="font-weight:700">Liquidity</div>
          <div>Cash balances increased during the fiscal year.</div>
          <div>Item 7A. Market Risk</div>
        </body></html>
        """
        blocks = html_to_blocks(html)
        self.assertNotIn("59 |2025 10-K", [block.text for block in blocks])
        sections = extract_section_blocks(blocks, items=("7",))
        sections["7"] = merge_continued_blocks(sections["7"])
        records = build_records_from_section_blocks(sections, year="2025", company="mu", source="sample", max_chars=1800)
        self.assertEqual([record["item_title"] for record in records], [
            "Critical Accounting Estimates", "Critical Accounting Estimates", "Liquidity",
        ])
        self.assertEqual(records[0]["text"], "Revenue is recognized when control transfers to the customer.")
        self.assertEqual(records[1]["text"], "Income taxes: We estimate taxes payable in multiple jurisdictions.")

    def test_inline_superscript_footnotes_are_resolved_in_prose(self):
        html = """
        <html><body>
          <div>Item 1. Business</div>
          <div>
            <span>We had 85,100</span>
            <span style="font-size:5.2pt;position:relative;top:-2.8pt;vertical-align:baseline">1</span>
            <span> people at year end.</span>
          </div>
          <div>Additional workforce discussion.</div>
          <div><span style="font-size:8pt">1 Employee headcount includes subsidiaries.</span></div>
          <div>Item 1A. Risk Factors</div>
        </body></html>
        """

        sections = extract_section_blocks(html_to_blocks(html), items=("1",))
        sections = resolve_section_footnotes(sections)
        records = build_records_from_section_blocks(
            sections, year="2025", company="intc", source="sample", max_chars=1800,
        )

        self.assertEqual(
            records[0]["text"],
            "We had 85,100 [footnote 1: Employee headcount includes subsidiaries.] people at year end.",
        )
        self.assertNotIn("1 Employee headcount", " ".join(record["text"] for record in records))

    def test_cli_omits_footer_from_json_and_txt_in_both_extraction_paths(self):
        lines = [
            "Item 7. Management's Discussion and Analysis", "Critical Accounting Estimates",
            "Estimates require management judgment.", "59 |2025 10-K",
            "Income taxes: We estimate taxes payable in multiple jurisdictions.",
            "Item 7A. Market Risk", "None.",
        ]
        for tag in ("div", "center"):
            with self.subTest(tag=tag), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "mu-20250828.htm"
                source.write_text("".join(f"<{tag}>{line}</{tag}>" for line in lines), encoding="utf-8")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(main([str(source), "--out-dir", str(root / "output"), "--items", "7"]), 0)
                output = root / "output" / "mu" / "2025"
                for name in ("2025_chunks.json", "2025_chunks.txt", "2025_item_7.txt"):
                    text = (output / name).read_text()
                    self.assertNotIn("59 |2025 10-K", text)
                    self.assertIn("Income taxes: We estimate taxes payable in multiple jurisdictions.", text)
                if tag == "div":
                    records = json.loads((output / "2025_chunks.json").read_text())
                    self.assertEqual({record["item_title"] for record in records}, {"Critical Accounting Estimates"})

    def test_discover_10k_filing_reads_sec_historical_submissions(self):
        from sec_disclosure.extraction import sec_10k_extractor

        responses = {
            "https://data.sec.gov/submissions/CIK0000019617.json": {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001628280-26-008131"],
                        "form": ["10-K"],
                        "reportDate": ["2025-12-31"],
                        "primaryDocument": ["jpm-20251231.htm"],
                    },
                    "files": [
                        {"name": "CIK0000019617-submissions-001.json"},
                        {"name": "CIK0000019617-submissions-002.json"},
                    ],
                }
            },
            "https://data.sec.gov/submissions/CIK0000019617-submissions-001.json": {
                "accessionNumber": ["0000019617-25-000316"],
                "form": ["10-K"],
                "reportDate": ["2024-12-31"],
                "primaryDocument": ["jpm-20241231.htm"],
            },
            "https://data.sec.gov/submissions/CIK0000019617-submissions-002.json": {
                "accessionNumber": ["0000019617-24-000001"],
                "form": ["10-K"],
                "reportDate": ["2023-12-31"],
                "primaryDocument": ["jpm-20231231.htm"],
            },
        }
        original_fetch_json = sec_10k_extractor.fetch_json
        fetched_urls = []

        def fake_fetch_json(url, user_agent):
            fetched_urls.append(url)
            return responses[url]

        sec_10k_extractor.fetch_json = fake_fetch_json
        try:
            filing = discover_10k_filing("0000019617", "2024", "tester@example.com")
        finally:
            sec_10k_extractor.fetch_json = original_fetch_json

        self.assertEqual(filing["accessionNumber"], "0000019617-25-000316")
        self.assertEqual(filing["primaryDocument"], "jpm-20241231.htm")
        self.assertNotIn("https://data.sec.gov/submissions/CIK0000019617-submissions-002.json", fetched_urls)

    def test_chunk_id_includes_item_and_resets_per_item(self):
        self.assertEqual(make_chunk_id("nvda", "2024", "1", 1), "nvda_2024_1_P001")
        self.assertEqual(make_chunk_id("NVDA", "2024", "1A", 1), "nvda_2024_1A_P001")
        self.assertEqual(make_chunk_id("nvda", "2024", "7", 12), "nvda_2024_7_P012")

    def test_block_records_are_not_split_by_max_chars_and_sentence_records_keep_bullets(self):
        long_bullet_group = (
            "The key product offerings are as follows:\n"
            "▪Series 2. Our first Series 2 products were brought to market in 2024. "
            "They support notebooks and desktops.\n"
            "Continuation text for the same bullet remains in the same paragraph group.\n"
            "▪Series 3. We released initial Series 3 processors in late 2025."
        )
        records = build_records_from_section_blocks(
            {"1": [FilingBlock(1, "div", "Key Products", font_size=12),
                   FilingBlock(2, "div", long_bullet_group, font_size=9)]},
            year="2025",
            company="intc",
            source="sample",
            max_chars=80,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "intc_2025_1_P001")
        self.assertEqual(records[0]["text"], long_bullet_group)

        sentence_records = build_sentence_records(records)
        self.assertEqual([record["id"] for record in sentence_records], [
            "intc_2025_1_P001_S001",
            "intc_2025_1_P001_S002",
            "intc_2025_1_P001_S003",
        ])
        self.assertEqual([record["chunk_id"] for record in sentence_records], ["intc_2025_1_P001"] * 3)
        self.assertEqual([record["bullet_level"] for record in sentence_records], [None, 1, 1])
        self.assertEqual(sentence_records[1]["text"], (
            "▪Series 2. Our first Series 2 products were brought to market in 2024. "
            "They support notebooks and desktops. Continuation text for the same bullet remains in the same paragraph group."
        ))

        styled_records = build_records_from_section_blocks(
            {"1": [FilingBlock(1, "div", "Key Products", font_size=12),
                   FilingBlock(2, "div", "▪Client CPUs. Top bullet.",
                               style="margin-bottom:3pt;padding-left:36pt;text-indent:-18pt", font_size=9),
                   FilingBlock(3, "div", "▪Intel Core Ultra. Parent bullet.",
                               style="margin-bottom:3pt;padding-left:72pt;text-indent:-18pt", font_size=9),
                   FilingBlock(4, "div", "▪Series 1. Child bullet.",
                               style="margin-bottom:3pt;padding-left:108pt;text-indent:-18pt", font_size=9)]},
            year="2025",
            company="intc",
            source="sample",
            max_chars=80,
        )
        styled_sentences = build_sentence_records(styled_records)
        self.assertEqual([record["bullet_level"] for record in styled_sentences], [1, 2, 3])
        self.assertEqual([record["bullet_indent_pt"] for record in styled_sentences], [36.0, 72.0, 108.0])

    def test_extracts_body_items_and_assigns_per_item_ids(self):
        html = """
        <html>
          <body>
            <table>
              <tr><td>Item 1. Business</td><td>5</td></tr>
              <tr><td>Item 1A. Risk Factors</td><td>9</td></tr>
              <tr><td>Item 7. Management's Discussion and Analysis</td><td>30</td></tr>
              <tr><td>Item 8. Financial Statements and Supplementary Data</td><td>60</td></tr>
              <tr><td>Item 15. Exhibits and Financial Statement Schedules</td><td>90</td></tr>
            </table>

            <h1>Item 1. Business</h1>
            <p>We operate a cloud analytics platform for enterprise customers.</p>
            <p>Our products include ingestion, modeling, and reporting tools.</p>

            <h1>Item 1A. Risk Factors</h1>
            <p>Our business depends on retaining customers and maintaining reliable systems.</p>

            <h1>Item 1B. Unresolved Staff Comments</h1>
            <p>None.</p>

            <h1>Item 7. Management's Discussion and Analysis</h1>
            <p>Revenue increased due to higher subscription demand.</p>

            <h1>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</h1>
            <p>Interest rate exposure was not material.</p>

            <h1>Item 8. Financial Statements and Supplementary Data</h1>
            <p>The consolidated financial statements are included in this report.</p>
            <table><tr><td>2024</td><td>100</td></tr></table>

            <h1>Item 9. Changes in and Disagreements with Accountants</h1>
            <p>None.</p>

            <h1>Item 15. Exhibits and Financial Statement Schedules</h1>
            <p>The financial statement schedules are omitted because they are not required.</p>
            <table><tr><td>Exhibit</td><td>10.1</td></tr></table>

            <h1>Signatures</h1>
            <p>Signed by the registrant.</p>
          </body>
        </html>
        """

        text = html_to_clean_text(html)
        sections = extract_sections(text, items=("1", "1A", "7", "8", "15"))
        records = build_records(sections, year="2024", company="nvda", source="sample", max_chars=500, min_chars=1)

        self.assertIn("1", sections)
        self.assertIn("1A", sections)
        self.assertIn("7", sections)
        self.assertIn("8", sections)
        self.assertIn("15", sections)
        self.assertNotIn("100", sections["8"])
        self.assertNotIn("10.1", sections["15"])
        self.assertNotIn("Signed by the registrant", sections["15"])
        self.assertEqual(records[0]["id"], "nvda_2024_1_P001")
        self.assertEqual(records[0]["company"], "nvda")
        self.assertEqual(
            [record["id"] for record in records],
            ["nvda_2024_1_P001", "nvda_2024_1A_P001", "nvda_2024_7_P001", "nvda_2024_8_P001", "nvda_2024_15_P001"],
        )

    def test_item_15_block_extraction_stops_at_signatures(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 15. Exhibits and Financial Statement Schedules</div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>Financial statement schedules are listed in this item.</span>
            </div>
            <table><tr><td>Exhibit table content</td></tr></table>
            <div style="font-weight:700">Signatures</div>
            <div>Signed by the registrant.</div>
          </body>
        </html>
        """

        blocks = html_to_blocks(html)
        section_blocks = extract_section_blocks(blocks, items=("15",))
        records = build_records_from_section_blocks(
            section_blocks,
            year="2024",
            company="jpm",
            source="sample",
            max_chars=500,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], "jpm_2024_15_P001")
        self.assertEqual(records[0]["item"], "15")
        self.assertEqual(records[0]["text"], "Financial statement schedules are listed in this item.")

    def test_div_blocks_become_chunks_with_subheader_titles(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 1. Business</div>
            <div style="margin-bottom:3pt;font-weight:700">Our Company</div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>NVIDIA’s “AI-training-as-a-service” platform — and software – pioneered accelerated computing.</span>
            </div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>We build full-stack computing infrastructure.</span>
            </div>

            <div style="font-weight:700">Item 1A. Risk Factors</div>
            <div style="font-weight:700">Risks Related to Our Business and Industry</div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>Demand can vary materially between periods.</span>
            </div>

            <div style="font-weight:700">Item 1B. Unresolved Staff Comments</div>
          </body>
        </html>
        """

        blocks = html_to_blocks(html)
        section_blocks = extract_section_blocks(blocks, items=("1", "1A"))
        sections = section_blocks_to_text(section_blocks)
        records = build_records_from_section_blocks(
            section_blocks,
            year="2024",
            company="nvda",
            source="sample",
            max_chars=500,
        )

        self.assertIn("Our Company", sections["1"])
        self.assertEqual(records[0]["item_title"], "Our Company")
        self.assertEqual(records[0]["company"], "nvda")
        self.assertEqual(
            records[0]["text"],
            'NVIDIA\'s "AI-training-as-a-service" platform - and software - pioneered accelerated computing.',
        )
        self.assertEqual(records[1]["item_title"], "Our Company")
        self.assertEqual(records[2]["item_title"], "Risks Related to Our Business and Industry")
        self.assertNotIn("html_tag", records[0])
        self.assertNotIn("html_id", records[0])

    def test_merges_cut_off_divs_and_groups_bullet_list_as_one_paragraph(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 1A. Risk Factors</div>
            <div style="font-weight:700">Operational Risks</div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>The DRIVE Hyperion platform consists of open, modular DRIVE Software</span>
            </div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>platform.</span>
            </div>
            <div style="padding-left:36pt;text-indent:-18pt">
              <span>&#8226;</span><span>First risk item;</span>
            </div>
            <div style="padding-left:36pt;text-indent:-18pt">
              <span>&#8226;</span><span>Second risk item;</span>
            </div>
            <div style="padding-left:36pt;text-indent:-18pt">
              <span>&#8226;</span><span>Third risk item continues</span>
            </div>
            <div style="padding-left:36pt;text-indent:-18pt">
              <span>onto the next rendered block.</span>
            </div>
            <div style="font-weight:700">Item 1B. Unresolved Staff Comments</div>
          </body>
        </html>
        """

        blocks = html_to_blocks(html)
        section_blocks = extract_section_blocks(blocks, items=("1A",))
        merged = merge_continued_blocks(section_blocks["1A"])
        records = build_records_from_section_blocks(
            {"1A": merged},
            year="2024",
            company="nvda",
            source="sample",
            max_chars=500,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0]["text"],
            "The DRIVE Hyperion platform consists of open, modular DRIVE Software platform.\n"
            "• First risk item;\n"
            "• Second risk item;\n"
            "• Third risk item continues onto the next rendered block.",
        )

    def test_generic_child_headers_keep_parent_context(self):
        records = build_records_from_section_blocks(
            {
                "1": [
                    FilingBlock(1, "div", "DCAI"),
                    FilingBlock(2, "div", "Overview"),
                    FilingBlock(3, "div", "DCAI delivers workload-optimized data center products."),
                    FilingBlock(4, "div", "Market Trends"),
                    FilingBlock(5, "div", "Demand for AI infrastructure increased."),
                    FilingBlock(6, "div", "Intel Foundry"),
                    FilingBlock(7, "div", "Overview"),
                    FilingBlock(8, "div", "Intel Foundry provides manufacturing services."),
                ]
            },
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )

        self.assertEqual([record["item_title"] for record in records], [
            "DCAI > Overview",
            "DCAI > Market Trends",
            "Intel Foundry > Overview",
        ])
        self.assertEqual(records[0]["section_path"], ["DCAI", "Overview"])
        self.assertEqual(records[1]["section_path"], ["DCAI", "Market Trends"])

    def test_font_size_controls_header_hierarchy(self):
        records = build_records_from_section_blocks(
            {
                "1": [
                    FilingBlock(1, "div", "Availability of Company Information", font_size=14),
                    FilingBlock(2, "div", "We post filings on our website.", font_size=9),
                    FilingBlock(3, "div", "Overview", font_size=18),
                    FilingBlock(4, "div", "Intel designs semiconductor products.", font_size=9),
                    FilingBlock(5, "div", "DCAI", font_size=12),
                    FilingBlock(6, "div", "Overview", font_size=10),
                    FilingBlock(7, "div", "DCAI delivers workload-optimized data center products.", font_size=9),
                    FilingBlock(8, "div", "Market Trends", font_size=10),
                    FilingBlock(9, "div", "Demand for AI infrastructure increased.", font_size=9),
                ]
            },
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )

        self.assertEqual([record["item_title"] for record in records], [
            "Availability of Company Information",
            "Overview",
            "Overview > DCAI > Overview",
            "Overview > DCAI > Market Trends",
        ])
        self.assertEqual(records[1]["section_path"], ["Overview"])
        self.assertEqual(records[2]["section_path"], ["Overview", "DCAI", "Overview"])

    def test_product_style_headings_are_detected_as_same_level_headers(self):
        records = build_records_from_section_blocks(
            {
                "1": merge_continued_blocks([
                    FilingBlock(1, "div", "Our Business", font_size=18),
                    FilingBlock(2, "div", "Products", font_size=14),
                    FilingBlock(3, "div", "x86 Architecture and Ecosystem", font_size=12),
                    FilingBlock(4, "div", "Our x86 architecture remains foundational.", font_size=9),
                    FilingBlock(5, "div", "xPU and AI Accelerators", font_size=12),
                    FilingBlock(6, "div", "We develop CPUs, GPUs, NPUs and accelerators.", font_size=9),
                    FilingBlock(7, "div", "Key Products", font_size=12),
                    FilingBlock(8, "div", "We derived most revenue from CCG and DCAI.", font_size=9),
                ])
            },
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )

        self.assertEqual([record["item_title"] for record in records], [
            "Our Business > Products > x86 Architecture and Ecosystem",
            "Our Business > Products > xPU and AI Accelerators",
            "Our Business > Products > Key Products",
        ])

    def test_all_caps_breaks_same_size_header_ties_without_treating_acronyms_as_caps_headers(self):
        records = build_records_from_section_blocks(
            {
                "1A": [
                    FilingBlock(1, "div", "RISK FACTORS", font_size=12),
                    FilingBlock(2, "div", "General risk discussion.", font_size=9),
                    FilingBlock(3, "div", "Operational Risks", font_size=12),
                    FilingBlock(4, "div", "Operations may be disrupted.", font_size=9),
                    FilingBlock(5, "div", "MARKET RISKS", font_size=12),
                    FilingBlock(6, "div", "Markets may be volatile.", font_size=9),
                ]
            },
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )

        self.assertEqual([record["item_title"] for record in records], [
            "RISK FACTORS",
            "RISK FACTORS > Operational Risks",
            "MARKET RISKS",
        ])
        self.assertEqual(records[1]["section_path"], ["RISK FACTORS", "Operational Risks"])

        acronym_records = build_records_from_section_blocks(
            {
                "1": [
                    FilingBlock(1, "div", "DCAI", font_size=12),
                    FilingBlock(2, "div", "DCAI text.", font_size=9),
                    FilingBlock(3, "div", "Intel Foundry", font_size=12),
                    FilingBlock(4, "div", "Foundry text.", font_size=9),
                ]
            },
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )

        self.assertEqual([record["item_title"] for record in acronym_records], ["DCAI", "Intel Foundry"])

    def test_drops_repeated_financial_statement_headers_before_merging(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 15. Exhibits and Financial Statement Schedules</div>
            <div style="font-weight:700">Product Sales Revenue</div>
            <div>
              Revenue from product sales is recognized upon transfer of control. Certain products are
            </div>
            <div style="font-weight:700">NVIDIA CORPORATION AND SUBSIDIARIES</div>
            <div style="font-weight:700">NOTES TO THE CONSOLIDATED FINANCIAL STATEMENTS</div>
            <div style="font-weight:700">(Continued)</div>
            <div>
              sold with support or an extended warranty. Revenue is recognized net of allowances.
            </div>
            <div style="font-weight:700">Item 16. Form 10-K Summary</div>
          </body>
        </html>
        """

        blocks = html_to_blocks(html)
        section_blocks = extract_section_blocks(blocks, items=("15",))
        merged = merge_continued_blocks(section_blocks["15"])
        records = build_records_from_section_blocks(
            {"15": merged},
            year="2023",
            company="nvda",
            source="sample",
            max_chars=500,
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["item_title"], "Product Sales Revenue")
        self.assertEqual(
            records[0]["text"],
            (
                "Revenue from product sales is recognized upon transfer of control. Certain products are "
                "sold with support or an extended warranty. Revenue is recognized net of allowances."
            ),
        )

    def test_item_15_uses_toc_headers_when_available(self):
        html = """
        <html>
          <body>
            <div>Item 15. Exhibits, Financial Statement Schedules.</div>
            <table>
              <tr><td>1</td><td>Financial statements</td></tr>
            </table>
            <div>(a) Filed herewith.</div>
            <div>page 44 not used</div>
            <div>Table of contents</div>
            <table>
              <tr><td>Financial:</td></tr>
              <tr><td>Management's discussion and analysis:</td><td>10</td></tr>
              <tr><td style="padding-left:18pt">Introduction</td><td>11</td></tr>
              <tr><td style="padding-left:18pt">Executive Overview</td><td>12</td></tr>
              <tr><td>Firmwide Risk Management</td><td>20</td></tr>
            </table>
            <div style="font-weight:700">Financial</div>
            <div style="font-weight:700">Management's discussion and analysis</div>
            <table><tr><td>INTRODUCTION</td></tr></table>
            <div>Management explains the operating environment.</div>
            <table><tr><td>EXECUTIVE OVERVIEW</td></tr></table>
            <div>Net revenue increased from the prior year.</div>
            <div style="font-weight:700">Management's discussion and analysis</div>
            <div>Repeated page headers should not reset the current section.</div>
            <div style="font-weight:700">Investment banking fees increased, reflecting in CIB:</div>
            <div>This bold lead-in should remain body text.</div>
            <table><tr><td>FIRMWIDE RISK MANAGEMENT</td></tr></table>
            <div>Risk management is embedded in business activities.</div>
            <div>Item 16. Form 10-K Summary</div>
          </body>
        </html>
        """

        section_blocks = extract_item15_toc_section_blocks(html)
        self.assertIsNotNone(section_blocks)
        merged = merge_continued_blocks(section_blocks or [])
        records = build_records_from_section_blocks(
            {"15": merged},
            year="2024",
            company="jpm",
            source="sample",
            max_chars=500,
        )

        titles_by_text = {record["text"]: record["item_title"] for record in records}
        self.assertEqual(
            titles_by_text["Management explains the operating environment."],
            "Management's discussion and analysis > Introduction",
        )
        self.assertEqual(
            titles_by_text["Net revenue increased from the prior year."],
            "Management's discussion and analysis > Executive Overview",
        )
        self.assertEqual(
            titles_by_text["Repeated page headers should not reset the current section."],
            "Management's discussion and analysis > Executive Overview",
        )
        self.assertEqual(
            titles_by_text["Investment banking fees increased, reflecting in CIB: This bold lead-in should remain body text."],
            "Management's discussion and analysis > Executive Overview",
        )
        self.assertEqual(
            titles_by_text["Risk management is embedded in business activities."],
            "Firmwide Risk Management",
        )

    def test_item_15_toc_bold_difference_does_not_create_hierarchy_without_indent(self):
        html = """
        <html>
          <body>
            <div>Item 15. Exhibits, Financial Statement Schedules.</div>
            <div>Table of contents</div>
            <table>
              <tr><td>Management's discussion and analysis</td><td>10</td></tr>
              <tr><td>Executive Overview</td><td>11</td></tr>
              <tr><td style="font-weight:700">Consolidated Results of Operations</td><td>20</td></tr>
              <tr><td>Consolidated Balance Sheets and Cash Flows Analysis</td><td>30</td></tr>
              <tr><td style="padding-left:4.5pt;text-indent:-4.5pt">Explanation and Reconciliation of Non-GAAP Measures</td><td>40</td></tr>
            </table>
            <div style="font-weight:700">Consolidated Results of Operations</div>
            <div>Operations text.</div>
            <div style="font-weight:700">Consolidated Balance Sheets and Cash Flows Analysis</div>
            <div style="font-size:12pt;font-weight:700">Consolidated balance sheets analysis</div>
            <div>Balance sheet text.</div>
            <div style="font-weight:700">Explanation and Reconciliation of Non-GAAP Measures</div>
            <div>Non-GAAP text.</div>
            <div>Item 16. Form 10-K Summary</div>
          </body>
        </html>
        """

        section_blocks = extract_item15_toc_section_blocks(html)
        self.assertIsNotNone(section_blocks)
        records = build_records_from_section_blocks(
            {"15": merge_continued_blocks(section_blocks or [])},
            year="2024",
            company="jpm",
            source="sample",
            max_chars=500,
        )

        titles_by_text = {record["text"]: record["item_title"] for record in records}
        self.assertEqual(titles_by_text["Operations text."], "Consolidated Results of Operations")
        self.assertEqual(
            titles_by_text["Balance sheet text."],
            "Consolidated Balance Sheets and Cash Flows Analysis > Consolidated balance sheets analysis",
        )
        self.assertEqual(
            titles_by_text["Non-GAAP text."],
            "Explanation and Reconciliation of Non-GAAP Measures",
        )

    def test_item_15_ignores_non_narrative_table_of_contents(self):
        html = """
        <html>
          <body>
            <div>Item 15. Exhibits, Financial Statement Schedules.</div>
            <div>Table of contents</div>
            <table>
              <tr><td>Net income</td><td>2024</td><td>2023</td></tr>
              <tr><td>Revenue</td><td>100</td><td>90</td></tr>
              <tr><td>Diluted</td><td>1.00</td><td>0.90</td></tr>
              <tr><td>Operating income</td><td>80</td><td>70</td></tr>
            </table>
            <div style="font-weight:700">Revenue Recognition</div>
            <div>Revenue is recognized when control transfers.</div>
            <div>Item 16. Form 10-K Summary</div>
          </body>
        </html>
        """

        self.assertIsNone(extract_item15_toc_section_blocks(html))

    def test_bold_bullet_is_not_treated_as_subheader(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 1A. Risk Factors</div>
            <div style="font-weight:700">Operational</div>
            <div style="font-weight:700;margin-bottom:6pt;padding-left:9pt;text-indent:-9pt">
              <span>&#8226;</span><span> effects of climate change</span>
            </div>
            <div style="margin-bottom:6pt;padding-left:9pt;text-indent:-9pt">
              <span>&#8226;</span><span> natural disasters or severe weather conditions</span>
            </div>
            <div style="margin-bottom:6pt">
              <span>JPMorgan Chase maintains a Firmwide resiliency program.</span>
            </div>
            <div style="font-weight:700">Item 1B. Unresolved Staff Comments</div>
          </body>
        </html>
        """

        blocks = html_to_blocks(html)
        section_blocks = extract_section_blocks(blocks, items=("1A",))
        records = build_records_from_section_blocks(
            section_blocks,
            year="2023",
            company="jpm",
            source="sample",
            max_chars=500,
        )

        self.assertEqual(records[0]["text"], "• effects of climate change")
        self.assertEqual(records[0]["item_title"], "Operational")
        self.assertEqual(records[1]["text"], "• natural disasters or severe weather conditions")
        self.assertEqual(records[1]["item_title"], "Operational")
        self.assertEqual(records[2]["item_title"], "Operational")

    def test_sentence_lead_ins_stay_with_bullets_in_both_html_parsers(self):
        introductions = [
            '<span style="font-weight:700">Total trading-related assets (average and period-end)</span>'
            '<span style="font-weight:400"> increased reflecting:</span>',
            '<strong>Revenue</strong> increased driven by:',
            '<b>Our results</b> improved because of:',
            '<span style="font-weight:700">Revenue increased driven by:</span>',
            '<span style="font-weight:700">Our results<span style="font-weight:400"> improved because of:</span></span>',
        ]
        for introduction in introductions:
            for parser in (html_to_blocks, html_to_structure_events):
                with self.subTest(introduction=introduction, parser=parser.__name__):
                    source = ('<div style="font-weight:700">Balance Sheet</div>'
                              f'<div>{introduction}</div>'
                              '<div>• growth across asset classes; and</div>'
                              '<div>• an increased volume of agreements.</div>'
                              '<div>Total deposits increased during the year.</div>'
                              '<div style="font-weight:700">Sources of Revenue:</div>'
                              '<div>Fees are earned from advisory services.</div>')
                    parsed = parser(source)
                    blocks = parsed if parser is html_to_blocks else [
                        FilingBlock(event.index, event.kind, event.text, bold=event.bold,
                                    mixed_bold=event.mixed_bold) for event in parsed
                    ]
                    intro = blocks[1].text
                    self.assertFalse(is_subheader_block(blocks[1]))
                    records = build_records_from_section_blocks(
                        {"7": merge_continued_blocks(blocks)}, "2025", "example", "sample", 1800,
                    )
                    self.assertEqual([r["item_title"] for r in records],
                                     ["Balance Sheet", "Balance Sheet", "Sources of Revenue:"])
                    self.assertEqual(records[0]["text"], intro +
                                     '\n• growth across asset classes; and\n• an increased volume of agreements.')
                    self.assertEqual(records[1]["text"], 'Total deposits increased during the year.')

    def test_colon_titles_survive_partial_emphasis_and_normal_weight_overrides(self):
        source = ('<div><b>Sources</b> of Revenue:</div>'
                  '<div><span style="font-weight:700">Income Taxes</span>:</div>'
                  '<h3>Business outlook:</h3>'
                  '<div style="font-weight:700">2025 Business Outlook</div>'
                  '<div style="font-weight:700">Our results<span style="font-weight:400"> improved because of:</span></div>')
        for parser in (html_to_blocks, html_to_structure_events):
            with self.subTest(parser=parser.__name__):
                parsed = parser(source)
                blocks = parsed if parser is html_to_blocks else [
                    FilingBlock(event.index, event.kind, event.text, bold=event.bold,
                                mixed_bold=event.mixed_bold) for event in parsed
                ]
                for block in blocks[:4]:
                    self.assertTrue(is_subheader_block(block), block.text)
                self.assertTrue(blocks[0].mixed_bold)
                self.assertFalse(blocks[1].mixed_bold)  # A plain colon is not narrative text.
                self.assertTrue(blocks[4].mixed_bold)
                self.assertFalse(is_subheader_block(blocks[4]))

    def test_fully_bold_sentence_introductions_are_not_headings(self):
        for text in ('Revenue increased driven by:', 'Assets increased reflecting:',
                     'The primary drivers were as follows:', 'Our operating costs consist of:'):
            with self.subTest(text=text):
                self.assertFalse(is_subheader_block(FilingBlock(1, 'div', text, bold=True)))
        self.assertTrue(is_subheader_block(FilingBlock(1, 'div', 'Drivers of Revenue:', bold=True)))

    def test_table_captions_do_not_replace_parent_or_enter_disclosure_text(self):
        for caption in ('Table 2:Ratios and Per Common Share Data', 'TABLE 9f: Balance Sheet',
                        'Table 25.2 - Restrictions on Cash', 'Table IV: Selected Ratios'):
            with self.subTest(caption=caption):
                source = ('<div>Item 7. Management Discussion</div>'
                          '<div style="font-weight:700">Overview</div>'
                          '<div>Table 2 presents selected ratios.</div>'
                          f'<div style="font-weight:700">{caption}</div>'
                          '<table><tr><td>Ratio</td><td>987654321</td></tr></table>'
                          '<div>(1)Represents net income divided by average assets.</div>'
                          '<div style="font-weight:700">Earnings Performance</div>'
                          '<div>Income increased this year.</div><div>Item 7A. Market Risk</div>')
                sections = extract_section_blocks(html_to_blocks(source), items=('7',))
                records = build_records_from_section_blocks(
                    {'7': merge_continued_blocks(sections['7'])}, '2025', 'example', 'sample', 1800)
                self.assertEqual([r['item_title'] for r in records],
                                 ['Overview', 'Overview', 'Earnings Performance'])
                self.assertEqual([r['text'] for r in records], [
                    'Table 2 presents selected ratios.',
                    '(1)Represents net income divided by average assets.', 'Income increased this year.'])

    def test_table_references_in_sentences_remain_narrative(self):
        for text in ('Table 2 presents selected ratios.',
                     'Table 2: Ratios and Per Common Share Data is discussed below.',
                     'Table 3.1 provides the amortized cost',
                     'Table 14.12 provides our significant assumptions'):
            with self.subTest(text=text):
                records = build_records_from_section_blocks(
                    {'7': [FilingBlock(1, 'div', text)]}, '2025', 'example', 'sample', 1800)
                self.assertEqual(records[0]['text'], text)

    def test_period_comparison_labels_keep_the_topic_without_entering_paragraphs(self):
        labels = ('Full year 2025 vs. full year 2024', 'FULL YEAR 2031 VS FULL YEAR 2030:',
                  'Fiscal year 2025 compared with fiscal year 2024', '2025 versus 2024',
                  'Fourth quarter 2025 vs. fourth quarter 2024', 'Q1 2025 compared to Q1 2024')
        for label in labels:
            with self.subTest(label=label):
                source = ('<div>Item 7. Management Discussion</div>'
                          '<div style="font-weight:700">Noninterest Income</div>'
                          '<div>We earn fees from customer services.</div>'
                          f'<div>{label}</div>'
                          '<div>Deposit-related fees increased this year.</div>'
                          '<div style="font-weight:700">Noninterest Expense</div>'
                          f'<div>{label}</div>'
                          '<div>Personnel expense increased this year.</div>'
                          '<div>Item 7A. Market Risk</div>')
                sections = extract_section_blocks(html_to_blocks(source), items=('7',))
                records = build_records_from_section_blocks(
                    {'7': merge_continued_blocks(sections['7'])}, '2025', 'example', 'sample', 1800)
                self.assertEqual([r['item_title'] for r in records],
                                 ['Noninterest Income', 'Noninterest Income', 'Noninterest Expense'])
                self.assertEqual([r['text'] for r in records], [
                    'We earn fees from customer services.', 'Deposit-related fees increased this year.',
                    'Personnel expense increased this year.'])
                self.assertFalse(is_subheader_block(FilingBlock(1, 'div', label)))

    def test_styled_period_comparison_labels_can_be_subheaders(self):
        records = build_records_from_section_blocks(
            {'15': [
                FilingBlock(1, 'div', 'Consolidated Results of Operations', bold=True, font_size=12),
                FilingBlock(2, 'div', '2024 compared with 2023', bold=True, font_size=10),
                FilingBlock(3, 'div', 'Net income increased from the prior year.', font_size=9),
            ]},
            '2024', 'jpm', 'sample', 1800)
        self.assertEqual(records[0]['item_title'],
                         'Consolidated Results of Operations > 2024 compared with 2023')
        self.assertEqual(records[0]['section_path'],
                         ['Consolidated Results of Operations', '2024 compared with 2023'])

    def test_period_references_in_narrative_and_topic_headings_are_preserved(self):
        sentences = ('Full year 2025 vs. full year 2024 revenue increased.',
                     'We compare full year 2025 vs. full year 2024.',
                     '2025 versus 2024 results reflect higher fees.')
        records = build_records_from_section_blocks(
            {'7': [FilingBlock(i, 'div', text) for i, text in enumerate(sentences)]},
            '2025', 'example', 'sample', 1800)
        self.assertEqual([r['text'] for r in records], list(sentences))
        for title in ('2025 Business Outlook', 'Revenue: 2025 vs. 2024', 'Fiscal Year Results'):
            self.assertTrue(is_subheader_block(FilingBlock(1, 'div', title, bold=True)))

    def test_cross_reference_index_extracts_report_style_sections(self):
        html = """
        <html><body>
          <div>Table of Contents</div>
          <table>
            <tr><td>Overview</td><td>3</td></tr>
            <tr><td>Our Business</td><td>6</td></tr>
            <tr><td>Risk Factors</td><td>37</td></tr>
            <tr><td>Management's Discussion and Analysis</td><td>21</td></tr>
            <tr><td>Financial Statements and Supplemental Details</td><td>56</td></tr>
            <tr><td>Exhibits</td><td>110</td></tr>
          </table>
          <table><tr><td>Overview</td></tr></table>
          <p>We design processors for customers.</p>
          <table><tr><td>Our Business</td></tr></table>
          <p>Our business includes client and data center products.</p>
          <table><tr><td>Risk Factors</td></tr></table>
          <p>Demand could decrease unexpectedly.</p>
          <table><tr><td>Management's Discussion and Analysis</td></tr></table>
          <p>Revenue increased due to stronger demand.</p>
          <table>
            <tr><td>▪CCG revenue decreased due to lower client volume.</td></tr>
            <tr><td>▪DCAI revenue increased due to higher server demand.</td></tr>
          </table>
          <table><tr><td>Financial Statements and Supplemental Details</td></tr></table>
          <p>The consolidated financial statements include our subsidiaries.</p>
          <table><tr><td>Notes to Consolidated Financial Statements</td></tr></table>
          <table><tr><td>Note 1:</td><td>Basis of Presentation</td></tr></table>
          <p>We prepare our financial statements in accordance with U.S. GAAP.</p>
          <table><tr><td>Exhibits</td></tr></table>
          <p>Exhibits are listed in the exhibit index.</p>
          <table>
            <tr><td>Form 10-K Cross-Reference Index</td></tr>
            <tr><td>Item Number</td><td>Item</td><td></td></tr>
            <tr><td>Item 1.</td><td>Business:</td></tr>
            <tr><td>General development of business</td><td>Pages 3-21</td></tr>
            <tr><td>Item 1A.</td><td>Risk Factors</td><td>Pages 37</td></tr>
            <tr><td>Item 7.</td><td>Management's Discussion and Analysis of Financial Condition and Results of Operations:</td></tr>
            <tr><td>Results of operations</td><td>Pages 21</td></tr>
            <tr><td>Critical accounting estimates</td><td>Pages 56</td></tr>
            <tr><td>Item 8.</td><td>Financial Statements and Supplementary Data</td><td>Pages 56</td></tr>
            <tr><td>Item 15.</td><td>Exhibits and Financial Statement Schedules</td><td>Pages 110</td></tr>
          </table>
        </body></html>
        """

        sections = extract_indexed_section_blocks(html, items=("1", "1A", "7", "8", "15"))
        records = build_records_from_section_blocks(
            {item: merge_continued_blocks(blocks) for item, blocks in sections.items()},
            year="2025",
            company="intc",
            source="sample",
            max_chars=500,
        )
        by_item = {item: " ".join(record["text"] for record in records if record["item"] == item)
                   for item in ("1", "1A", "7", "8", "15")}

        self.assertIn("processors", by_item["1"])
        self.assertNotIn("Revenue increased", by_item["1"])
        self.assertNotIn("CCG revenue decreased", by_item["1"])
        self.assertIn("Demand could decrease", by_item["1A"])
        self.assertIn("Revenue increased", by_item["7"])
        self.assertIn("▪CCG revenue decreased", by_item["7"])
        self.assertIn("▪DCAI revenue increased", by_item["7"])
        self.assertNotIn("U.S. GAAP", by_item["7"])
        self.assertIn("financial statements", by_item["8"])
        self.assertTrue(any(
            record["item"] == "8"
            and record["item_title"].endswith("Note 1: Basis of Presentation")
            and "U.S. GAAP" in record["text"]
            for record in records
        ))
        self.assertIn("Exhibits are listed", by_item["15"])

    def test_multi_column_toc_item_numbers_extract_by_title(self):
        html = """
        <html><body>
          <table>
            <tr><td>Table of Contents</td><td>Part</td><td>Item</td><td>Page</td></tr>
            <tr><td>Business</td><td>I</td><td>1</td><td>5</td></tr>
            <tr><td>Risk Factors</td><td></td><td>1A</td><td>13</td></tr>
            <tr><td>Management's Discussion and Analysis of Financial Condition and Results of Operations</td><td>II</td><td>7</td><td>26</td></tr>
            <tr><td>Financial Statements and Supplementary Data</td><td></td><td>8</td><td>78</td></tr>
            <tr><td>Exhibits and Financial Statement Schedules</td><td>IV</td><td>15</td><td>156</td></tr>
          </table>
          <h1>Business</h1><p>We advise institutional and wealth management clients.</p>
          <h1>Risk Factors</h1><p>Market conditions may affect our results.</p>
          <h1>Management's Discussion and Analysis of Financial Condition and Results of Operations</h1>
          <p>Net revenues increased from the prior year.</p>
          <h1>Financial Statements and Supplementary Data</h1>
          <p>The consolidated financial statements are presented below.</p>
          <h1>Exhibits and Financial Statement Schedules</h1>
          <p>Financial statement schedules are omitted.</p>
        </body></html>
        """

        sections = extract_indexed_section_blocks(html, items=("1", "1A", "7", "8", "15"))
        records = build_records_from_section_blocks(
            {item: merge_continued_blocks(blocks) for item, blocks in sections.items()},
            year="2025",
            company="ms",
            source="sample",
            max_chars=500,
        )

        self.assertEqual({record["item"] for record in records}, {"1", "1A", "7", "8", "15"})
        self.assertTrue(any(record["item"] == "7" and "Net revenues increased" in record["text"]
                            for record in records))


def incorporated_filing(year):
    def heading(item, title):
        return f'<table><tr><td>ITEM {item}.</td><td>{title}</td></tr></table>'
    return ('<html><body>' + heading('1', 'BUSINESS') +
            '<div>We manufacture industrial equipment.</div>' +
            '<table><tr><td>Revenue</td><td>987654321</td></tr></table>' +
            heading('1A', 'RISK FACTORS') +
            f'<div>Information in response to this Item 1A is in the {year} Annual Report under "Operating Review - Risk Overview." That information is incorporated by reference.</div>' +
            heading('1B', 'UNRESOLVED STAFF COMMENTS') + '<div>None.</div>' +
            heading('7', 'MANAGEMENT DISCUSSION') +
            f'<div>Information in response to this Item 7 is in the {year} Annual Report under "Operating Review." That information is incorporated by reference.</div>' +
            heading('7A', 'MARKET RISK') + '<div>None.</div>' +
            heading('8', 'FINANCIAL STATEMENTS') +
            f'<div>Information in response to this Item 8 is in the {year} Annual Report under "Accounts," under "Notes to the Accounts" and under "Quarterly Results." That information is incorporated by reference.</div>' +
            heading('9', 'ACCOUNTANTS') + '<div>None.</div></body></html>')


def incorporated_report(year):
    return f'''<html><body><table>
      <tr><td style="font-weight:700">Operating Review</td></tr>
      <tr><td>2</td><td>Performance</td></tr>
      <tr><td>3</td><td>Risk Overview</td></tr>
      <tr><td style="font-weight:700">Corporate Governance</td></tr>
      <tr><td style="font-weight:700">Accounts</td></tr>
      <tr><td style="font-weight:700">Notes to the Accounts</td></tr>
      <tr><td style="font-weight:700">Quarterly Results</td></tr>
      <tr><td style="font-weight:700">Glossary</td></tr></table>
      <div style="font-weight:700">Operating Review</div>
      <table><tr><td>Performance</td></tr></table>
      <div>Our revenue increased in {year}.</div>
      <table><tr><td>Risk Overview</td></tr></table>
      <div>Supply disruptions could affect our business in {year}.</div>
      <div style="font-weight:700">Risk Overview (continued)</div>
      <div>Customers could cancel orders.</div>
      <table><tr><td>Corporate Governance</td></tr></table>
      <div>This governance text is outside the requested sections.</div>
      <div style="font-weight:700">Accounts</div>
      <div>Our statements consolidate the subsidiaries.</div>
      <table><tr><td>Assets</td><td>987654321</td></tr></table>
      <div style="font-weight:700">Notes to the Accounts</div>
      <table><tr><td>Note 27. Income Taxes</td></tr></table>
      <div>Our tax expense changed in {year}.</div>
      <table><tr><td>Quarterly Results</td></tr>
        <tr><td>Revenue</td><td>987654321</td></tr></table>
      <div>Quarterly figures were revised in {year}.</div>
      <table><tr><td>Glossary</td></tr></table>
      <div>This glossary text is outside the requested sections.</div>
      </body></html>'''


def report_index(filename, document_type='EX-13'):
    return f'<html><table><tr><td>6</td><td>Annual Report</td><td><a href="{filename}">{filename}</a></td><td>{document_type}</td></tr></table></html>'


class IncorporatedReportTests(unittest.TestCase):
    def make_files(self, root, year='2031', primary='wrapper.htm', report='financial-data.htm', doc_type='EX-13'):
        source = root / primary
        source.write_text(incorporated_filing(year))
        (root / report).write_text(incorporated_report(year))
        (root / '0000999888-32-000001-index.htm').write_text(report_index(report, doc_type))
        return source

    def arguments(self, source, root, year='2031'):
        return [str(source), '--company', 'example', '--year', year, '--items', '1', '1A', '7', '8',
                '--out-dir', str(root / 'output')]

    def test_discovers_arbitrary_report_names_years_and_sections(self):
        for year, primary, report, doc_type in [
            ('2031', 'wrapper.htm', 'financial-data.htm', 'EX-13'),
            ('2032', 'financial-data.htm', 'wrapper.htm', 'EX-13.1'),
            ('2033', 'form.htm', 'shareholder-letter.htm', 'EX-99.1'),
        ]:
            with self.subTest(year=year), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.make_files(root, year, primary, report, doc_type)
                before = {p.name: p.read_bytes() for p in root.glob('*.htm')}
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(self.arguments(source, root, year)), 0)
                destination = root / 'output/example' / year
                records = json.loads((destination / f'{year}_chunks.json').read_text())
                self.assertEqual({r['item'] for r in records}, {'1', '1A', '7', '8'})
                self.assertEqual(len({r['id'] for r in records}), len(records))
                by_item = {item: ' '.join(r['text'] for r in records if r['item'] == item) for item in ('1', '1A', '7', '8')}
                self.assertIn('industrial equipment', by_item['1'])
                self.assertIn('Supply disruptions', by_item['1A'])
                self.assertIn('cancel orders', by_item['1A'])
                self.assertIn('revenue increased', by_item['7'])
                self.assertNotIn('Supply disruptions', by_item['7'])
                self.assertIn('tax expense', by_item['8'])
                self.assertIn('Quarterly figures were revised', by_item['8'])
                all_text = ' '.join(by_item.values())
                for unwanted in ('987654321', 'incorporated by reference', 'governance text', 'glossary text', '(continued)'):
                    self.assertNotIn(unwanted, all_text)
                for record in records:
                    self.assertEqual(record['source'], str(source if record['item'] == '1' else root / report))
                audit = json.loads((destination / f'{year}_sources.json').read_text())['referenced_items']
                self.assertEqual(audit['7']['excluded_sections'][0]['title'], 'Risk Overview')
                self.assertEqual(audit['8']['document_type'], doc_type)
                for filename, content in before.items():
                    self.assertEqual((root / filename).read_bytes(), content)

    def test_referenced_report_keeps_sentence_introduction_under_parent_heading(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_files(root)
            report = root / 'financial-data.htm'
            report.write_text(incorporated_report('2031').replace(
                '<div>Our revenue increased in 2031.</div>',
                '<div><span style="font-weight:700">Total trading-related assets (average and period-end)</span>'
                '<span style="font-weight:400"> increased reflecting:</span></div>'
                '<div>• growth across asset classes; and</div><div>• increased agreements.</div>'
                '<div>Total deposits increased.</div>'))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(self.arguments(source, root)), 0)
            records = json.loads((root / 'output/example/2031/2031_chunks.json').read_text())
            review = [r for r in records if r['item'] == '7']
            self.assertEqual([r['item_title'] for r in review], ['Performance', 'Performance'])
            self.assertEqual(review[0]['text'],
                             'Total trading-related assets (average and period-end) increased reflecting:'
                             '\n• growth across asset classes; and\n• increased agreements.')
            self.assertEqual(review[1]['text'], 'Total deposits increased.')
            self.assertEqual(review[0]['source'], str(report))

    def test_table_footnotes_use_enclosing_report_section_and_accounting_note(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_files(root)
            report = root / 'financial-data.htm'
            report.write_text(incorporated_report('2031').replace(
                '<tr><td style="font-weight:700">Notes to the Accounts</td></tr>',
                '<tr><td style="font-weight:700">Notes to the Accounts</td></tr>'
                '<tr><td>Note 27. Income Taxes</td></tr>').replace(
                '<div>Our revenue increased in 2031.</div>',
                '<div style="font-weight:700">Credit Quality</div>'
                '<div>Credit conditions improved.</div>'
                '<div style="font-weight:700">Table 2: Ratios</div>'
                '<table><tr><td>Assets</td><td>987654321</td></tr></table>'
                '<div>(1)Represents income divided by average assets.</div>'
                '<table><tr><td>Table 3: Equity Ratios</td></tr></table>'
                '<div>(2)Represents income divided by average equity.</div>').replace(
                '<div>Our tax expense changed in 2031.</div>',
                '<div>Our tax expense changed in 2031.</div>'
                '<div style="font-weight:700">Deferred Taxes</div>'
                '<div style="font-weight:700">Table 27.1: Tax Balances</div>'
                '<table><tr><td>Taxes</td><td>987654321</td></tr></table>'
                '<div>(1)Tax balances exclude interest.</div>'))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(self.arguments(source, root)), 0)
            records = json.loads((root / 'output/example/2031/2031_chunks.json').read_text())
            footnotes = [r for r in records if r['text'].startswith(('(1)', '(2)'))]
            self.assertEqual([r['item_title'] for r in footnotes],
                             ['Performance', 'Performance', 'Note 27. Income Taxes'])
            self.assertEqual([r['text'] for r in footnotes], [
                '(1)Represents income divided by average assets.',
                '(2)Represents income divided by average equity.', '(1)Tax balances exclude interest.'])
            self.assertTrue(all(r['source'] == str(report) for r in footnotes))
            self.assertTrue(all(not r['item_title'].startswith('Table ') for r in records))
            self.assertTrue(all('987654321' not in r['text'] for r in records))

    def test_numeric_table_labels_and_prose_bullets_do_not_open_outline_sections(self):
        report = '''<table>
          <tr><td style="font-weight:700">Financial Review</td></tr>
          <tr><td>Overview</td></tr><tr><td>Deposits</td></tr>
          <tr><td>Income Taxes</td></tr><tr><td>Earnings Performance</td></tr></table>
          <div>Financial Review</div><div>Overview</div>
          <table><tr><td>Year</td><td>2031</td></tr>
          <tr><td>Deposits:</td></tr><tr><td>Assets</td><td>987654321</td></tr></table>
          <div>• income taxes;</div><div style="font-weight:700">Table 3: Ratios</div>
          <div>(1)Represents average balances.</div><div>Earnings Performance</div>
          <div>Income increased.</div>'''
        nodes = report_outline(html_to_structure_events(report), [['Financial Review', 'Overview']])
        self.assertEqual([n['title'] for n in nodes], ['Financial Review', 'Overview', 'Earnings Performance'])

    def test_period_labels_in_referenced_reports_restore_the_enclosing_topic(self):
        for label_html in ('<div style="font-weight:700">Full year 2031 vs. full year 2030</div>',
                           '<table><tr><td>Full year 2031 vs. full year 2030</td></tr></table>'):
            with self.subTest(label_html=label_html), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.make_files(root)
                report = root / 'financial-data.htm'
                report.write_text(incorporated_report('2031').replace(
                    '<div>Our revenue increased in 2031.</div>',
                    '<div style="font-weight:700">NM - Not meaningful</div>' + label_html +
                    '<div>Our revenue increased in 2031.</div>'
                    '<div>Fees increased due to customer activity.</div>'))
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(self.arguments(source, root)), 0)
                records = json.loads((root / 'output/example/2031/2031_chunks.json').read_text())
                review = [r for r in records if r['item'] == '7']
                self.assertEqual([r['item_title'] for r in review], ['Performance', 'Performance'])
                self.assertEqual([r['text'] for r in review],
                                 ['Our revenue increased in 2031.', 'Fees increased due to customer activity.'])
                self.assertTrue(all(r['source'] == str(report) for r in review))

    def test_period_labels_in_html_headings_do_not_create_outline_sections(self):
        report = ('<h1>Operating Review</h1><h2>Performance</h2>'
                  '<h3>Full year 2031 vs. full year 2030</h3><p>Fees increased.</p>'
                  '<h1>Accounts</h1><p>Accounting discussion.</p>')
        nodes = report_outline(html_to_structure_events(report), [['Operating Review', 'Performance']])
        self.assertEqual([n['title'] for n in nodes], ['Operating Review', 'Performance', 'Accounts'])

    def test_api_mode_fetches_index_and_report_without_filename_assumptions(self):
        base = 'https://www.sec.gov/Archives/edgar/data/999888/000099988832000001/'
        responses = {base + 'main.htm': incorporated_filing('2031'),
                     base + '0000999888-32-000001-index.htm': report_index('/ix?doc=/Archives/edgar/data/999888/000099988832000001/changed-name.htm'),
                     base + 'changed-name.htm': incorporated_report('2031')}
        filing = {'accessionNumber': '0000999888-32-000001', 'primaryDocument': 'main.htm'}
        with TemporaryDirectory() as temporary, \
             patch('sec_disclosure.extraction.sec_10k_extractor.discover_10k_filing', return_value=filing), \
             patch('sec_disclosure.extraction.sec_10k_extractor.fetch_url_bytes', side_effect=lambda url, agent: responses[url].encode()) as fetch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--cik', '999888', '--company', 'example', '--year', '2031', '--items', '1A', '7', '8',
                                   '--user-agent', 'Test test@example.com', '--out-dir', temporary]), 0)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], list(responses))
            records = json.loads((Path(temporary) / 'example/2031/2031_chunks.json').read_text())
            self.assertTrue(all(r['source'] == base + 'changed-name.htm' for r in records))

    def test_reference_failures_do_not_replace_existing_outputs(self):
        for failure in ('missing-heading', 'ambiguous-document', 'pdf-document', 'outside-filing', 'missing-index', 'page-only'):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = self.make_files(root)
                index = root / '0000999888-32-000001-index.htm'
                if failure == 'missing-heading':
                    (root / 'financial-data.htm').write_text('<html><h1>Unrelated document</h1></html>')
                elif failure == 'ambiguous-document':
                    index.write_text(report_index('first.htm') + report_index('second.htm'))
                elif failure == 'pdf-document':
                    index.write_text(report_index('financial-data.pdf'))
                elif failure == 'outside-filing':
                    index.write_text(report_index('https://example.org/unrelated.htm'))
                elif failure == 'missing-index':
                    index.unlink()
                else:
                    source.write_text(incorporated_filing('2031').replace('under "Operating Review - Risk Overview."', 'on pages 12-20.'))
                output = root / 'output/example/2031'
                output.mkdir(parents=True)
                existing = output / '2031_chunks.json'
                existing.write_text('["existing results"]')
                errors = io.StringIO()
                with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                    self.assertEqual(main(self.arguments(source, root)), 2)
                self.assertEqual(existing.read_text(), '["existing results"]')
                self.assertIn('Error resolving incorporated report', errors.getvalue())

    def test_explicit_primary_only_and_overlap_options(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_files(root)
            with redirect_stdout(io.StringIO()), patch('sec_disclosure.extraction.sec_10k_extractor.discover_referenced_report') as discovery:
                self.assertEqual(main(self.arguments(source, root) + ['--no-follow-references']), 0)
                discovery.assert_not_called()
            output = root / 'output/example/2031/2031_chunks.json'
            self.assertIn('incorporated by reference', output.read_text())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(self.arguments(source, root) + ['--include-reference-overlaps']), 0)
            records = json.loads(output.read_text())
            self.assertTrue(any(r['item'] == '7' and 'Supply disruptions' in r['text'] for r in records))

    def test_html_heading_hierarchy_and_ambiguous_names(self):
        report = '<h1>Operating Review</h1><h2>Overview</h2><p>Business discussion.</p><h1>Accounts</h1><h2>Overview</h2><p>Accounting discussion.</p><h1>Glossary</h1>'
        nodes = report_outline(html_to_structure_events(report), [['Operating Review', 'Overview']])
        sections = referenced_section_ranges(nodes, [['Operating Review', 'Overview']])
        self.assertEqual(sections[0]['start'], 1)
        self.assertEqual(sections[0]['end'], 3)
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            referenced_section_ranges(nodes, [['Overview']])


if __name__ == "__main__":
    unittest.main()
