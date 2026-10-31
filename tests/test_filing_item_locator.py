"""The independent locator selects source ranges without local Item extraction."""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from sec_disclosure.agents.filing_item_locator import (
    _block_pages, _covers_index_pages, _explicit_item_label, _global_heading_options,
    _index_references, _indexed_sections, _outline_locations,
    _select_ranges, locate_item_blocks,
)
from sec_disclosure.extraction.sec_10k_extractor import (
    html_to_blocks, html_to_structure_events,
)
from sec_disclosure.llm.config import LLMConfig


CONFIG = LLMConfig("https://example.test/v1", "test-key", "qwen-test")


class FilingItemLocatorTests(unittest.TestCase):
    def test_explicit_item_label_does_not_confuse_item_10_with_item_1(self):
        self.assertEqual(_explicit_item_label("Item 10. Directors"), "10")
        self.assertEqual(_explicit_item_label("Item 1A. Risk Factors"), "1A")
        self.assertIsNone(_explicit_item_label("Our Item 1 discussion continues."))

    def test_pointer_heading_is_not_a_body_start_option(self):
        html = ("<h2>Item 8. Financial Statements</h2>"
                "<p>The statements are included in this annual report.</p>"
                "<h2>Item 9. Accountants</h2>"
                "<h2>Item 15. Exhibits</h2>"
                "<h2>Report of Independent Registered Public Accounting Firm</h2>"
                "<p>We audited the accompanying statements.</p>"
                "<h2>Exhibit Index</h2><h2>Item 16. Summary</h2>")
        blocks = html_to_blocks(html)
        options = _global_heading_options(blocks, "8")
        ids = {entry["block_id"] for entry in options}
        self.assertNotIn(blocks[0].index, ids)
        self.assertIn(blocks[4].index, ids)
        self.assertIn(blocks[6].index, ids)

    def test_long_descriptive_section_has_compact_boundary_options(self):
        html = ("<h2>Item 6. Reserved</h2>"
                "<h2>Management's Discussion and Analysis</h2>"
                + "".join(f"<h3>Subsection {i}</h3><p>Details.</p>"
                          for i in range(350))
                + "<h2>Item 7A. Market Risk</h2>"
                "<h2>Report of Management on Internal Control</h2>"
                + "".join(f"<h3>Financial note {i}</h3><p>Details.</p>"
                          for i in range(350))
                + "<div>Item 9. Accountants</div>")
        blocks = html_to_blocks(html)
        references = [
            {"item": "7", "title": "Management's Discussion and Analysis"},
            {"item": "8", "title": "Financial Statements and Supplementary Data"},
        ]
        for item, start_text, end_text in (
            ("7", "Management's Discussion and Analysis", "Item 7A. Market Risk"),
            ("8", "Report of Management on Internal Control", "Item 9. Accountants"),
        ):
            options = _global_heading_options(blocks, item, references)
            texts = {entry["text"] for entry in options}
            self.assertLessEqual(len(options), 100)
            self.assertIn(start_text, texts)
            self.assertIn(end_text, texts)

    def test_model_assigns_overlapping_index_pages_once(self):
        blocks = html_to_blocks("".join(
            f"<p>Company narrative for printed page {page} and operations.</p>"
            for page in range(1, 11)
        ))
        pages = {block.index: number for number, block in enumerate(blocks, 1)}
        refs = [{"item": "1", "title": "Business", "pages": "1-5"},
                {"item": "7", "title": "Management Discussion", "pages": "3-8"}]

        def model(prompt, validate, **_kwargs):
            self.assertIn('"1"', prompt)
            self.assertIn('"7"', prompt)
            return validate({"owner": "7"}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            sections, attempts = _indexed_sections(
                blocks, {"1": set(range(1, 6)), "7": set(range(3, 9))},
                refs, pages, set(), config=CONFIG, timeout=10, max_tokens=1200, retries=3,
            )
        self.assertEqual(attempts, 1)
        self.assertEqual([pages[block.index] for block in sections["1"]], [1, 2])
        self.assertEqual([pages[block.index] for block in sections["7"]], [3, 4, 5, 6, 7, 8])

        with patch("sec_disclosure.agents.filing_item_locator._ask",
                   side_effect=lambda prompt, validate, **kwargs: (
                       validate({"owner": None}), 1,
                   )):
            unresolved, _ = _indexed_sections(
                blocks, {"1": set(range(1, 6)), "7": set(range(3, 9))},
                refs, pages, set(), config=CONFIG, timeout=10, max_tokens=1200, retries=3,
            )
        self.assertEqual(unresolved, {})

    def test_heading_map_can_locate_item8_report_after_item15(self):
        html = ("<div><b>Item 8. Financial Statements</b></div>"
                "<p>See our financial statements later in this report.</p>"
                "<div><b>Item 9. Changes in Accountants</b></div>"
                "<div><b>Item 15. Exhibits and Financial Statements</b></div>"
                "<div><b>Report of Independent Registered Public Accounting Firm</b></div>"
                "<p>Our financial statements have been audited.</p>"
                "<p>Additional financial report narrative.</p>"
                "<div><b>Exhibit Index</b></div>")
        blocks = html_to_blocks(html)
        calls = []

        def model(prompt, validate, **_kwargs):
            calls.append(prompt)
            return validate({"ranges": [{"start_block": blocks[4].index,
                                         "end_block_exclusive": blocks[7].index}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._index_tables",
                   return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references",
                   return_value=([{"item": "8", "title": "Financial Statements",
                                  "pages": "2", "row_id": "T1-R0"}], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            result = locate_item_blocks(html, blocks, ["8"], config=CONFIG, page_recovery=False)
        self.assertEqual([block.index for block in result.sections["8"]],
                         [blocks[4].index, blocks[5].index, blocks[6].index])
        self.assertEqual(len(calls), 1)

    def test_rejects_small_fragment_of_multipage_index_reference(self):
        blocks = html_to_blocks("".join(f"<p>Page {page} narrative.</p>"
                                        for page in range(1, 21)))
        block_pages = {block.index: page for page, block in enumerate(blocks, 1)}
        refs = {"8": set(range(1, 21)), "1": {5, 6}}
        with redirect_stderr(io.StringIO()) as errors:
            self.assertFalse(_covers_index_pages("8", blocks[10:12], refs, block_pages))
        self.assertIn("covers only", errors.getvalue())
        self.assertTrue(_covers_index_pages("8", blocks[6:19], refs, block_pages))

    def test_empty_outline_object_means_no_locations(self):
        def model(_prompt, validate, **_kwargs):
            return validate({}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            locations, attempts = _outline_locations(
                html_to_blocks("<p>Some filing text.</p>"), ["8"],
                config=CONFIG, timeout=10, max_tokens=1200, retries=3,
            )
        self.assertEqual(locations, [])
        self.assertEqual(attempts, 1)

    def test_single_index_page_is_a_start_hint_not_a_body_scope(self):
        html = ("<table><tr><td>Item 8</td><td>Page 2</td></tr></table>"
                + "".join(f"<p>Narrative on page {page}.</p><div>Section | {page}</div>"
                          for page in range(1, 13)))
        with patch("sec_disclosure.agents.filing_item_locator._index_tables",
                   return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references",
                   return_value=([{"item": "8", "title": "Financial Statements",
                                  "pages": "2", "row_id": "T1-R0"}], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._outline_locations",
                   return_value=([], 0)) as outline, \
             patch("sec_disclosure.agents.filing_item_locator._select_ranges") as select:
            result = locate_item_blocks(html, html_to_blocks(html), ["8"], config=CONFIG, page_recovery=False)
        self.assertEqual(result.sections, {})
        outline.assert_called_once()
        select.assert_not_called()

    def test_index_page_list_can_continue_across_rows(self):
        html = ("<table><tr><th>Item Number</th><th>Page</th></tr>"
                "<tr><td>1. Business</td><td>4-32, 130-135,</td></tr>"
                "<tr><td></td><td>137, 169-173, 314-315</td></tr></table>")
        table = next(event for event in html_to_structure_events(html) if event.kind == "table")
        pages = "4-32, 130-135, 137, 169-173, 314-315"

        def model(_prompt, validate, **_kwargs):
            return validate({"references": [{"item": "1", "row_id": f"T{table.index}-R1",
                                            "title": "Business", "pages": pages}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            refs, attempts = _index_references([table], ["1"], config=CONFIG,
                                               timeout=10, max_tokens=1200, retries=3)
        self.assertEqual(attempts, 1)
        self.assertEqual(refs[0]["pages"], pages)

    def test_range_cannot_cross_an_explicit_different_item(self):
        html = ("<div>Item 7. Management Discussion</div><p>Revenue improved.</p>"
                "<div>Item 7A. Market Risk</div><p>Interest rates varied.</p>"
                "<div>Item 8. Financial Statements</div>")
        blocks = html_to_blocks(html)
        options = [{"block_id": blocks[0].index}, {"block_id": blocks[-1].index}]

        def model(_prompt, validate, **_kwargs):
            return validate({"ranges": [{"start_block": blocks[0].index,
                                         "end_block_exclusive": blocks[-1].index}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model), \
             redirect_stderr(io.StringIO()) as errors:
            selected, _ = _select_ranges("7", blocks, options, context="", used=set(),
                                         config=CONFIG, timeout=10, max_tokens=1200, retries=3)
        self.assertEqual(selected, [])
        self.assertIn("crosses explicit Item 7A", errors.getvalue())

    def test_item_heading_range_must_reach_the_next_item_heading(self):
        html = ("<h2>Item 1A. Risk Factors</h2><p>Risk one.</p>"
                "<h3>Additional risks</h3><p>Risk two.</p>"
                "<h2>Item 1B. Unresolved Staff Comments</h2>")
        blocks = html_to_blocks(html)
        options = [{"block_id": block.index} for block in (blocks[0], blocks[2], blocks[4])]

        def model(_prompt, validate, **_kwargs):
            return validate({"ranges": [{"start_block": blocks[0].index,
                                         "end_block_exclusive": blocks[2].index}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model), \
             redirect_stderr(io.StringIO()) as errors:
            selected, _ = _select_ranges("1A", blocks, options, context="", used=set(),
                                         config=CONFIG, timeout=10, max_tokens=1200, retries=3)
        self.assertEqual(selected, [])
        self.assertIn("stops before the next explicit Item heading", errors.getvalue())

    def test_item_range_can_end_at_a_section_heading_before_next_item(self):
        html = ("<div>Item 1A. Risk Factors.</div><p>First risk.</p>"
                "<p>Further risk discussion.</p><h2>Parts I and II</h2>"
                "<div>Item 1B. Unresolved Staff Comments.</div>")
        blocks = html_to_blocks(html)
        options = [{"block_id": blocks[0].index}, {"block_id": blocks[3].index},
                   {"block_id": blocks[4].index}]

        def model(_prompt, validate, **_kwargs):
            return validate({"ranges": [{"start_block": blocks[0].index,
                                         "end_block_exclusive": blocks[3].index}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            selected, _ = _select_ranges("1A", blocks, options, context="", used=set(),
                                         config=CONFIG, timeout=10, max_tokens=1200, retries=3)
        self.assertEqual([block.index for block in selected],
                         [blocks[0].index, blocks[1].index, blocks[2].index])

    def test_page_reference_pointer_is_not_accepted_as_item_body(self):
        html = ("<div>Item 7. Management Discussion</div>"
                "<p>The discussion appears on pages 52-167 of this report.</p>"
                "<div>Item 7A. Market Risk</div>")
        blocks = html_to_blocks(html)
        options = [{"block_id": block.index} for block in blocks]

        def model(_prompt, validate, **_kwargs):
            return validate({"ranges": [{"start_block": blocks[0].index,
                                         "end_block_exclusive": blocks[2].index}]}), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model), \
             redirect_stderr(io.StringIO()) as errors:
            selected, _ = _select_ranges("7", blocks, options, context="", used=set(),
                                         config=CONFIG, timeout=10, max_tokens=1200, retries=3)
        self.assertEqual(selected, [])
        self.assertIn("only a pointer", errors.getvalue())

    def test_model_selects_index_table_rows_and_body_ranges(self):
        html = ("<table><tr><th>Section</th><th>Pages</th></tr>"
                "<tr><td>Risk Factors</td><td>2-4</td></tr>"
                "<tr><td>Management's Discussion</td><td>5-8</td></tr>"
                "<tr><td>Financial Statements</td><td>9-12</td></tr></table>"
                + "".join(f"<p>Filing narrative for page {page} details.</p>"
                          f"<div>Section | {page}</div>" for page in range(1, 13)))
        blocks = html_to_blocks(html)
        events = html_to_structure_events(html)
        pages, _ = _block_pages(events, blocks)
        first_by_page = {
            page: next(block.index for block in blocks if pages.get(block.index) == page)
            for page in range(1, 13)
        }
        table_id = next(event.index for event in events if event.kind == "table")
        requests = []

        def model(prompt, validate, **_kwargs):
            requests.append(prompt)
            if prompt.startswith("Identify tables"):
                answer = {"table_ids": [table_id]}
            elif prompt.startswith("Read this filing navigation"):
                answer = {"references": [
                    {"item": "1A", "row_id": f"T{table_id}-R1", "title": "Risk Factors", "pages": "2-4"},
                    {"item": "7", "row_id": f"T{table_id}-R2", "title": "Management's Discussion", "pages": "5-8"},
                    {"item": "8", "row_id": f"T{table_id}-R3", "title": "Financial Statements", "pages": "9-12"},
                ]}
            else:
                item = prompt.split("SEC Item ", 1)[1].split(".", 1)[0]
                first, end = {"1A": (2, 5), "7": (5, 9), "8": (9, None)}[item]
                answer = {"ranges": [{
                    "start_block": first_by_page[first],
                    "end_block_exclusive": first_by_page[end] if end else blocks[-1].index + 1,
                }]}
            return validate(answer), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model), \
             patch("sec_disclosure.extraction.sec_10k_extractor.extract_section_blocks",
                   side_effect=AssertionError("deterministic Item extractor must not run")):
            result = locate_item_blocks(html, blocks, ["1A", "7", "8"], config=CONFIG, page_recovery=False)
        self.assertEqual(set(result.sections), {"1A", "7", "8"})
        self.assertEqual(result.attempts, 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual([len(result.sections[item]) for item in ("1A", "7", "8")], [3, 4, 4])
        self.assertTrue(all("Section |" not in block.text
                            for section in result.sections.values() for block in section))

    def test_global_heading_map_uses_model_boundaries_without_an_index(self):
        html = ("<h2>Item 7. Management's Discussion</h2><p>Revenue improved.</p>"
                "<h2>Item 7A. Market Risk</h2><p>Interest rates varied.</p>")
        blocks = html_to_blocks(html)
        prompts = []

        def model(prompt, validate, **_kwargs):
            prompts.append(prompt)
            if prompt.startswith("In this ordered filing outline"):
                answer = {"locations": [
                    {"item": "7", "role": "start", "block_id": blocks[0].index},
                    {"item": "7", "role": "end", "block_id": blocks[2].index},
                ]}
            else:
                answer = {"ranges": [{"start_block": blocks[0].index,
                                      "end_block_exclusive": blocks[2].index}]}
            return validate(answer), 1

        with patch("sec_disclosure.agents.filing_item_locator._ask", side_effect=model):
            result = locate_item_blocks(html, blocks, ["7"], config=CONFIG, page_recovery=False)
        self.assertEqual([block.text for block in result.sections["7"]],
                         ["Item 7. Management's Discussion", "Revenue improved."])
        self.assertEqual(len(prompts), 1)

    def test_rejects_item_15_as_a_requested_output(self):
        with self.assertRaisesRegex(ValueError, "only 1, 1A, 7, and 8"):
            locate_item_blocks("<p>Body</p>", html_to_blocks("<p>Body</p>"),
                               ["15"], config=CONFIG)


if __name__ == "__main__":
    unittest.main()
