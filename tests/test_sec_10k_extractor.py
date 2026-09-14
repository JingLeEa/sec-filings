import unittest

from sec_10k_extractor import (
    build_records,
    build_records_from_section_blocks,
    discover_10k_filing,
    extract_item15_toc_section_blocks,
    extract_section_blocks,
    extract_sections,
    html_to_blocks,
    html_to_clean_text,
    infer_company_year_from_filename,
    merge_continued_blocks,
    make_chunk_id,
    normalize_source,
    parse_args,
    section_blocks_to_text,
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
        self.assertEqual(args.out_dir, "data/raw")
        self.assertIsNone(args.source)

    def test_discover_10k_filing_reads_sec_historical_submissions(self):
        import sec_10k_extractor

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
        self.assertEqual(make_chunk_id("2024", "1", 1), "2024_1_P001")
        self.assertEqual(make_chunk_id("2024", "1A", 1), "2024_1A_P001")
        self.assertEqual(make_chunk_id("2024", "7", 12), "2024_7_P012")

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
        sections = extract_sections(text)
        records = build_records(sections, year="2024", company="nvda", source="sample", max_chars=500, min_chars=1)

        self.assertIn("1", sections)
        self.assertIn("1A", sections)
        self.assertIn("7", sections)
        self.assertIn("8", sections)
        self.assertIn("15", sections)
        self.assertNotIn("100", sections["8"])
        self.assertNotIn("10.1", sections["15"])
        self.assertNotIn("Signed by the registrant", sections["15"])
        self.assertEqual(records[0]["id"], "2024_1_P001")
        self.assertEqual(records[0]["company"], "nvda")
        self.assertEqual(
            [record["id"] for record in records],
            ["2024_1_P001", "2024_1A_P001", "2024_7_P001", "2024_8_P001", "2024_15_P001"],
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
        self.assertEqual(records[0]["id"], "2024_15_P001")
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
              <tr><td>Introduction</td><td>11</td></tr>
              <tr><td>Executive Overview</td><td>12</td></tr>
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
        self.assertEqual(titles_by_text["Management explains the operating environment."], "Introduction")
        self.assertEqual(titles_by_text["Net revenue increased from the prior year."], "Executive Overview")
        self.assertEqual(
            titles_by_text["Repeated page headers should not reset the current section."],
            "Executive Overview",
        )
        self.assertEqual(
            titles_by_text["Investment banking fees increased, reflecting in CIB: This bold lead-in should remain body text."],
            "Executive Overview",
        )
        self.assertEqual(
            titles_by_text["Risk management is embedded in business activities."],
            "Firmwide Risk Management",
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


if __name__ == "__main__":
    unittest.main()
