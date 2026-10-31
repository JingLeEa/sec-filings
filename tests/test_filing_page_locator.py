"""Offline checks for automatic page-to-source fallback and its opt-out."""
import io
import json
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sec_disclosure.agents.filing_page_locator import (
    PageMapResult, bind_block_pages, event_html, evidence_chunks, locate_printed_pages,
    parse_printed_pages, unique_markers, validate_markers,
    resolve_page_pointers,
    expand_repeated_footers,
    page_boundaries,
    prefer_item_indexes,
)
from sec_disclosure.agents.filing_item_locator import locate_item_blocks
from sec_disclosure.extraction.sec_10k_extractor import html_to_blocks, html_to_structure_events, is_pointer_only_item
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.llm.config import LLMConfig

CONFIG = LLMConfig("https://example.test/v1", "test-key", "test-model")


def footer(label, page):
    return f"<table><tr><td>{label}</td><td>{page}</td></tr></table>"


class FilingPageLocatorTests(unittest.TestCase):
    def test_optional_pointer_trigger_recognizes_included_in_an_annual_report(self):
        blocks = html_to_blocks("<p>The information required by this item is included in this Annual Report on pages F-1 through F-27.</p>")
        self.assertFalse(is_pointer_only_item("8", blocks))
        self.assertTrue(is_pointer_only_item("8", blocks, extended=True))

    def test_formal_item_index_is_not_overridden_by_subtopic_toc_guesses(self):
        events = html_to_structure_events('<table><tr><td>Item 7.</td><td>Discussion</td><td>33</td></tr><tr><td>Item 8.</td><td>Statements</td><td>34</td></tr></table><table><tr><td>Credit risk</td><td>109</td></tr></table>')
        formal = {"item": "7", "pages": "33", "row_id": f"T{events[0].index}-R0"}
        topic = {"item": "7", "pages": "109", "row_id": f"T{events[1].index}-R0"}
        no_formal = {"item": "1A", "pages": "9-31", "row_id": f"T{events[1].index}-R0"}
        self.assertEqual(prefer_item_indexes(events, [formal, topic, no_formal]), [formal, no_formal])

    def test_disjoint_and_prefixed_ranges_keep_their_namespace(self):
        self.assertEqual(parse_printed_pages("pages 3-5, 8; 12-13"), {"3", "4", "5", "8", "12", "13"})
        self.assertEqual(parse_printed_pages("F-1 through F-3"), {"F-1", "F-2", "F-3"})
        for invalid in ("3-1", "F-1-S-3", "1-999", "unknown", "3, rubbish"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_printed_pages(invalid)

    def test_tables_are_intact_and_seams_overlap_without_losing_source_ids(self):
        html = "".join(f"<p>{n} {'narrative ' * 35}</p>" + footer("Section", n) for n in range(1, 12))
        events = html_to_structure_events(html)
        chunks = evidence_chunks(events, 1000, overlap=2)
        self.assertGreater(len(chunks), 1)
        self.assertEqual({e.index for chunk in chunks for e in chunk}, {e.index for e in events})
        for chunk in chunks:
            self.assertLessEqual(sum(len(event_html(e)) + 1 for e in chunk), 1000)
        self.assertTrue(set(e.index for e in chunks[0]) & set(e.index for e in chunks[1]))
        with self.assertRaises(ValueError):
            evidence_chunks(html_to_structure_events("<table><tr><td>" + "x" * 1500 + "</td></tr></table>"), 1000)

    def test_footer_validation_rejects_invented_ids_pages_indexes_and_data_tables(self):
        events = html_to_structure_events(footer("Risk Factors", 37) + "<table>" + "<tr><td>37</td></tr>" * 4 + "</table>")
        good = {"markers": [{"page": 37, "source_block_id": events[0].index}]}
        self.assertEqual(validate_markers(good, events, set()), [("37", events[0].index)])
        self.assertEqual(validate_markers({"markers": [{"page": "37", "source_block_id": str(events[0].index)}]}, events, set()),
                         [("37", events[0].index)])
        for value, excluded in ((good, {events[0].index}),
                                ({"markers": [{"page": 37, "source_block_id": 999}]}, set()),
                                ({"markers": [{"page": 38, "source_block_id": events[0].index}]}, set()),
                                ({"markers": [{"page": 37, "source_block_id": events[1].index}]}, set())):
            with self.assertRaises(ValueError):
                validate_markers(value, events, excluded)

    def test_duplicate_overlap_is_deduplicated_but_conflicts_are_not_guessed(self):
        markers, issues = unique_markers([("37", 10), ("37", 10), ("38", 20)])
        self.assertEqual(markers, {"37": 10, "38": 20})
        self.assertFalse(issues)
        markers, issues = unique_markers([("37", 10), ("37", 11), ("38", 20)])
        self.assertNotIn("37", markers)
        self.assertTrue(issues)
        markers, issues = unique_markers([("37", 20), ("38", 10)])
        self.assertFalse(markers)
        self.assertTrue(issues)

    def test_binding_uses_original_block_ids_and_does_not_bridge_missing_pages(self):
        html = footer("Earlier", 36) + "<table><tr><td>Risk Factors</td></tr></table><p>Risk one.</p>" + footer("Risk Factors", 37) + "<p>Risk two.</p>" + footer("Risk Factors", 38)
        events, blocks = html_to_structure_events(html), html_to_blocks(html)
        ids = {e.text: e.index for e in events}
        mapped, _ = bind_block_pages(events, blocks, {"36": ids["Earlier 36"], "37": ids["Risk Factors 37"], "38": ids["Risk Factors 38"]})
        self.assertEqual(mapped, {blocks[0].index: "37", blocks[1].index: "38"})
        self.assertNotEqual(blocks[0].index, ids["Risk one."])
        mapped, _ = bind_block_pages(events, blocks, {"36": ids["Earlier 36"], "38": ids["Risk Factors 38"]})
        self.assertFalse(mapped)

    def test_missing_preceding_footer_is_retried_automatically_then_map_is_reused(self):
        events = html_to_structure_events(footer("MD&A", 36) + "<p>Risk one.</p>" + footer("Risk Factors", 37) + "<p>Risk two.</p>" + footer("Risk Factors", 38))
        ids = {e.text: e.index for e in events}
        refs = [{"item": "1A", "pages": "37-38", "title": "Risk Factors", "row_id": "T99-R1"}]
        responses = [CompletionResult(json.dumps({"markers": [{"page": p, "source_block_id": ids[f"Risk Factors {p}"]} for p in (37, 38)]}), "test-model", "stop", None),
                     CompletionResult(json.dumps({"markers": [{"page": 36, "source_block_id": ids["MD&A 36"]}]}), "test-model", "stop", None)]
        with TemporaryDirectory() as tmp, patch("sec_disclosure.agents.filing_item_fallback.request_completion", side_effect=responses) as request:
            result = locate_printed_pages(events, refs, [], config=CONFIG, cache_dir=Path(tmp), retries=0)
            self.assertEqual(set(result.markers), {"36", "37", "38"})
            self.assertEqual(request.call_count, 2)
            self.assertIn("Boundary follow-up", request.call_args.args[0])
            # A second Item in the same filing uses the cached physical page map.
            result = locate_printed_pages(events, [{**refs[0], "item": "7"}], [], config=CONFIG, cache_dir=Path(tmp), retries=0)
            self.assertEqual(result.attempts, 0)
            self.assertEqual(request.call_count, 2)

    def test_default_item_recovery_requires_no_enable_flag_or_manual_page_ids(self):
        html = footer("Prior", 36) + "<p>Risk one.</p>" + footer("Risk Factors", 37) + "<p>Risk two.</p>" + footer("Risk Factors", 38)
        blocks, events = html_to_blocks(html), html_to_structure_events(html)
        markers = {str(p): next(e.index for e in events if e.text.endswith(str(p))) for p in (36, 37, 38)}
        refs = [{"item": "1A", "pages": "37-38", "title": "Risk Factors", "row_id": "T99-R1"}]
        with patch("sec_disclosure.agents.filing_item_locator._index_tables", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references", return_value=(refs, 0)), \
             patch("sec_disclosure.agents.filing_item_locator._global_heading_options", return_value=[]), \
             patch("sec_disclosure.agents.filing_item_locator._outline_locations", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_page_locator.locate_printed_pages", return_value=PageMapResult(markers, 1, 0, ())) as mapper, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = locate_item_blocks(html, blocks, ["1A"], config=CONFIG)
        self.assertEqual(result.sections["1A"], blocks)
        self.assertEqual(mapper.call_args.args[1], refs)

    def test_explicit_baseline_opt_out_does_not_call_page_mapper(self):
        with patch("sec_disclosure.agents.filing_item_locator._index_tables", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._outline_locations", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_page_locator.locate_printed_pages") as mapper:
            locate_item_blocks("<p>Body.</p>", html_to_blocks("<p>Body.</p>"), ["1A"], config=CONFIG, page_recovery=False)
            mapper.assert_not_called()

    def test_default_does_not_map_pages_when_baseline_selection_is_complete(self):
        html = "<h2>Item 1. Business</h2><p>Our business.</p><h2>Item 1A. Risk Factors</h2>"
        blocks = html_to_blocks(html)
        with patch("sec_disclosure.agents.filing_item_locator._index_tables", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._select_ranges", return_value=(blocks[:2], 1)), \
             patch("sec_disclosure.agents.filing_page_locator.locate_printed_pages") as mapper:
            result = locate_item_blocks(html, blocks, ["1"], config=CONFIG)
        self.assertEqual(result.sections["1"], blocks[:2])
        mapper.assert_not_called()

    def test_bad_proposal_does_not_erase_other_grounded_footers(self):
        events = html_to_structure_events(footer("Section", 1))
        values = [{"markers": [{"page": 1, "source_block_id": events[0].index}, {"page": 2, "source_block_id": 999}]}, {"markers": []}]
        responses = [CompletionResult(json.dumps(value), "test", "stop", None) for value in values]
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion", side_effect=responses):
            result = locate_printed_pages(events, [{"item": "1", "pages": "1"}], [], config=CONFIG, retries=1)
        self.assertEqual(result.markers, {"1": events[0].index})

    def test_repeated_layout_requires_two_examples_unique_candidates_and_page_order(self):
        html = footer("Section", 1) + footer("Section", 2) + footer("Revenue", 3) + footer("Section", 3) + footer("Section", 99) + footer("Section", 4) + footer("Section", 5)
        events = html_to_structure_events(html)
        by_text = {event.text: event.index for event in events}
        known = {"1": by_text["Section 1"], "5": by_text["Section 5"]}
        expanded, inferred = expand_repeated_footers(events, known)
        self.assertEqual(inferred, {"2", "3", "4"})
        self.assertEqual(expanded["3"], by_text["Section 3"])
        self.assertNotIn("99", expanded)
        self.assertFalse(expand_repeated_footers(events, {"1": known["1"]})[1])
        self.assertNotIn("3", expand_repeated_footers(events, known, {"3"})[0])
        duplicate = html_to_structure_events(html + footer("Section", 3))
        self.assertNotIn("3", expand_repeated_footers(duplicate, known)[0])
        reversed_events = html_to_structure_events(footer("Section", 1) + footer("Section", 3) + footer("Section", 2) + footer("Section", 5))
        anchors = {"1": reversed_events[0].index, "5": reversed_events[-1].index}
        self.assertEqual(expand_repeated_footers(reversed_events, anchors)[0], anchors)

    def test_table_only_page_can_have_verified_boundaries_without_narrative_blocks(self):
        events = html_to_structure_events(footer("Section", 1) + "<table><tr><td>Financial data</td><td>100</td></tr></table>" + footer("Section", 2))
        markers = {"1": events[0].index, "2": events[-1].index}
        self.assertIn("2", page_boundaries(markers))
        self.assertFalse(bind_block_pages(events, [], markers)[0])

    def test_prefixed_footer_pages_are_distinct_from_numeric_pages(self):
        html = footer("Main", 1) + "<p>Main text.</p>" + footer("Main", 2) + "<p>Auditor report.</p>" + footer("Financial", "F-1") + "<p>Financial text.</p>" + footer("Financial", "F-2")
        events, blocks = html_to_structure_events(html), html_to_blocks(html)
        markers = {label: next(e.index for e in events if e.text.endswith(label)) for label in ("1", "2", "F-1", "F-2")}
        mapped, _ = bind_block_pages(events, blocks, markers)
        self.assertEqual(mapped[blocks[0].index], "2")
        self.assertEqual(mapped[blocks[1].index], "F-1")
        self.assertEqual(mapped[blocks[2].index], "F-2")

    def test_single_toc_page_is_expanded_only_from_a_grounded_body_pointer(self):
        quote = "The firm's statements dated February 13, 2026 (PCAOB 238) are included on pages F-1 through F-3."
        events = html_to_structure_events(footer("Prior", 47) + "<h2>Item 8. Financial Statements</h2><p>" + quote + "</p>" + footer("Main", 48))
        markers = {"47": events[0].index, "48": events[-1].index}
        refs = [{"item": "8", "pages": "48", "title": "Financial Statements", "row_id": "T99-R1"}]
        result = CompletionResult(json.dumps({"references": [{"pages": "F-1-F-3", "quote": quote.replace("'", "&#x27;")}]}), "test", "stop", None)
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion", return_value=result):
            resolved, attempts = resolve_page_pointers(events, refs, ["8"], markers, config=CONFIG, timeout=10)
        self.assertEqual(resolved[0]["pages"], "F-1-F-3")
        self.assertTrue(resolved[0]["resolved_from_pointer"])
        self.assertEqual(attempts, 1)
        invented = CompletionResult(json.dumps({"references": [{"pages": "F-1-F-4", "quote": quote}]}), "test", "stop", None)
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion", return_value=invented):
            resolved, _ = resolve_page_pointers(events, refs, ["8"], markers, config=CONFIG, timeout=10)
        self.assertFalse(resolved)

    def test_pointer_quote_cannot_come_from_another_item_on_the_same_page(self):
        own = "Our discussion appears on pages 46-160."
        other = "Financial statements appear on pages 162-314."
        events = html_to_structure_events(footer("Prior", 32) + "<h2>Item 7. Discussion</h2><p>" + own + "</p><h2>Item 8. Financial Statements</h2><p>" + other + "</p>" + footer("Main", 33))
        responses = [CompletionResult(json.dumps({"references": [{"pages": "162-314", "quote": other}]}), "test", "stop", None),
                     CompletionResult(json.dumps({"references": [{"pages": "46-160", "quote": own}]}), "test", "stop", None)]
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion", side_effect=responses):
            resolved, attempts = resolve_page_pointers(events, [{"item": "7", "pages": "33", "row_id": "T99-R0"}], ["7"], {"32": events[0].index, "33": events[-1].index}, config=CONFIG, timeout=10)
        self.assertEqual(resolved[0]["pages"], "46-160")
        self.assertEqual(attempts, 2)

    def test_pointer_resolution_keeps_disjoint_supplementary_paragraphs(self):
        first = "Statements appear on pages 162-314."
        second = "The glossary is included on pages 320-327."
        events = html_to_structure_events(footer("Prior", 33) + "<h2>Item 8. Financial Statements</h2><p>" + first + "</p><p>" + second + "</p>" + footer("Main", 34))
        entries = [{"pages": "162-314", "quote": first}, {"pages": "320-327", "quote": second}]
        responses = [CompletionResult(json.dumps({"references": entries[:1]}), "test", "stop", None),
                     CompletionResult(json.dumps({"references": entries}), "test", "stop", None)]
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion", side_effect=responses):
            resolved, attempts = resolve_page_pointers(events, [{"item": "8", "pages": "34", "row_id": "T99-R0"}], ["8"], {"33": events[0].index, "34": events[-1].index}, config=CONFIG, timeout=10)
        self.assertEqual([ref["pages"] for ref in resolved], ["162-314", "320-327"])
        self.assertEqual(attempts, 2)

    def test_disjoint_index_pages_do_not_include_the_gap(self):
        html = "".join(f"<p>Body for printed page {page}.</p>" + footer("Section", page) for page in range(1, 8))
        blocks, events = html_to_blocks(html), html_to_structure_events(html)
        refs = [{"item": "1A", "pages": "2-3, 6-7", "title": "Risk Factors", "row_id": "T99-R1"}]
        markers = {str(page): next(e.index for e in events if e.kind == "table" and e.text == f"Section {page}") for page in range(1, 8)}
        with patch("sec_disclosure.agents.filing_item_locator._index_tables", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references", return_value=(refs, 0)), \
             patch("sec_disclosure.agents.filing_item_locator._global_heading_options", return_value=[]), \
             patch("sec_disclosure.agents.filing_item_locator._outline_locations", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_page_locator.locate_printed_pages", return_value=PageMapResult(markers, 1, 0, ())), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = locate_item_blocks(html, blocks, ["1A"], config=CONFIG, page_recovery=True)
        self.assertEqual(result.sections["1A"], [blocks[p - 1] for p in (2, 3, 6, 7)])

    def test_verified_pointer_replaces_previously_accepted_highlights_fragment(self):
        html = "".join(("<p>Highlights one.</p><p>Highlights two.</p>" if page == 2 else f"<p>Body {page}.</p>") + footer("Section", page) for page in range(1, 11))
        blocks, events = html_to_blocks(html), html_to_structure_events(html)
        markers = {str(page): next(e.index for e in events if e.text == f"Section {page}") for page in range(1, 11)}
        refs = [{"item": "7", "pages": "3", "title": "Discussion", "row_id": "T99-R1"},
                {"item": "8", "pages": "2", "title": "Financial", "row_id": "T99-R2"}]
        resolved = [{**refs[1], "pages": "5-10", "resolved_from_pointer": True}]
        def select(item, *args, **kwargs):
            return ([b for b in blocks if b.text.startswith("Highlights")] if item == "8" else []), 1
        with patch("sec_disclosure.agents.filing_item_locator._index_tables", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_item_locator._index_references", return_value=(refs, 0)), \
             patch("sec_disclosure.agents.filing_item_locator._global_heading_options", return_value=[{"block_id": 1}]), \
             patch("sec_disclosure.agents.filing_item_locator._select_ranges", side_effect=select), \
             patch("sec_disclosure.agents.filing_item_locator._outline_locations", return_value=([], 0)), \
             patch("sec_disclosure.agents.filing_page_locator.locate_printed_pages", return_value=PageMapResult(markers, 1, 0, ())), \
             patch("sec_disclosure.agents.filing_page_locator.resolve_page_pointers", return_value=(resolved, 1)), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = locate_item_blocks(html, blocks, ["7", "8"], config=CONFIG, page_recovery=True)
        self.assertEqual([b.text for b in result.sections["8"]], [f"Body {page}." for page in range(5, 11)])


if __name__ == "__main__":
    unittest.main()
