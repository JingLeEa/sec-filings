import unittest

from sec_10k_extractor import (
    build_records,
    build_records_from_section_blocks,
    extract_section_blocks,
    extract_sections,
    html_to_blocks,
    html_to_clean_text,
    infer_company_year_from_filename,
    normalize_source,
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

    def test_extracts_body_items_and_assigns_global_ids(self):
        html = """
        <html>
          <body>
            <table>
              <tr><td>Item 1. Business</td><td>5</td></tr>
              <tr><td>Item 1A. Risk Factors</td><td>9</td></tr>
              <tr><td>Item 7. Management's Discussion and Analysis</td><td>30</td></tr>
              <tr><td>Item 8. Financial Statements and Supplementary Data</td><td>60</td></tr>
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
        self.assertNotIn("100", sections["8"])
        self.assertEqual(records[0]["id"], "2024_P001")
        self.assertEqual(records[0]["company"], "nvda")
        self.assertEqual(records[-1]["id"], f"2024_P{len(records):03d}")

    def test_div_blocks_become_chunks_with_subheader_titles(self):
        html = """
        <html>
          <body>
            <div style="font-weight:700">Item 1. Business</div>
            <div style="margin-bottom:3pt;font-weight:700">Our Company</div>
            <div style="margin-bottom:9pt;text-align:justify">
              <span>NVIDIA pioneered accelerated computing.</span>
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
        self.assertEqual(records[0]["text"], "NVIDIA pioneered accelerated computing.")
        self.assertEqual(records[1]["item_title"], "Our Company")
        self.assertEqual(records[2]["item_title"], "Risks Related to Our Business and Industry")
        self.assertNotIn("html_tag", records[0])
        self.assertNotIn("html_id", records[0])


if __name__ == "__main__":
    unittest.main()
