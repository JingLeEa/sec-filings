"""Integration and retry tests for the independent four-Item locator."""

import io
import json
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.agents.filing_item_fallback import (
    FallbackResult, InvalidFallbackResponse, _request_json,
    write_agent_only_outputs,
)
from sec_disclosure.extraction.sec_10k_extractor import (
    apply_item_fallback, html_to_blocks, main, parse_args,
)
from sec_disclosure.llm.client import CompletionResult, LLMError
from sec_disclosure.llm.config import LLMConfig


CONFIG = LLMConfig("https://example.test/v1", "test-key", "qwen-test")
HTML = ("<div>Item 1. Business</div><p>We manufacture components.</p>"
        "<div>Item 1A. Risk Factors</div><p>Demand may decline.</p>"
        "<div>Item 7. Management Discussion</div><p>Revenue increased.</p>"
        "<div>Item 8. Financial Statements</div><p>Statements include subsidiaries.</p>")


def response(value):
    return CompletionResult(json.dumps(value), "qwen-test", "stop", None)


class FilingItemFallbackTests(unittest.TestCase):
    def test_invalid_json_is_retried_up_to_three_times(self):
        def validate(value):
            if not isinstance(value.get("ranges"), list):
                raise InvalidFallbackResponse("ranges must be a list")
            return value["ranges"]

        with patch("sec_disclosure.agents.filing_item_fallback.request_completion",
                   side_effect=[response({"wrong": 1}) for _ in range(3)]
                   + [response({"ranges": []})]) as api:
            result, count = _request_json("Find ranges", validate, config=CONFIG,
                                          timeout=10, max_tokens=1200, retries=3)
        self.assertEqual(result, [])
        self.assertEqual(count, 4)
        self.assertEqual(api.call_count, 4)

    def test_retryable_timeout_retries_and_saves_valid_result(self):
        with patch("sec_disclosure.agents.filing_item_fallback.request_completion",
                   side_effect=[LLMError("timed out", retryable=True),
                                response({"ranges": []})]) as api, \
             patch("sec_disclosure.agents.filing_item_fallback.time.sleep"):
            result, count = _request_json("Find ranges", lambda value: value,
                                          config=CONFIG, timeout=10,
                                          max_tokens=1200, retries=3)
        self.assertEqual(result, {"ranges": []})
        self.assertEqual(count, 2)
        self.assertEqual(api.call_count, 2)

    def test_agent_only_output_uses_locator_without_local_item_sections(self):
        blocks = html_to_blocks(HTML)
        with TemporaryDirectory() as temporary, \
             patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks",
                   return_value=FallbackResult({"1": blocks[:2]}, 1)) as locator, \
             patch("sec_disclosure.extraction.sec_10k_extractor.extract_section_blocks",
                   side_effect=AssertionError("deterministic Item extraction must not run")):
            output = Path(temporary)
            result, chunks, sentences, mismatches = write_agent_only_outputs(
                HTML, "https://example.test/filing.htm", "sample", "2024",
                ["1", "8"], output, config=CONFIG,
            )
            self.assertEqual(set(result.sections), {"1"})
            self.assertGreater(chunks, 0)
            self.assertGreater(sentences, 0)
            self.assertEqual(mismatches, 0)
            self.assertTrue((output / "2024_item_1.txt").is_file())
            self.assertFalse((output / "2024_item_8.txt").exists())
            locator.assert_called_once()
            self.assertEqual(locator.call_args.args[2], ["1", "8"])
            self.assertTrue(locator.call_args.kwargs["page_recovery"])

    def test_agent_only_baseline_selection_explicitly_disables_default_page_recovery(self):
        with TemporaryDirectory() as tmp, \
             patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks", return_value=FallbackResult({}, 0)) as locator:
            write_agent_only_outputs(HTML, "source", "sample", "2024", ["1"], Path(tmp),
                                     config=CONFIG, page_recovery=False)
        self.assertFalse(locator.call_args.kwargs["page_recovery"])

    def test_extractor_default_and_opt_out_reach_the_locator(self):
        for flags, expected in (([], True), (["--no-agent-page-recovery"], False), (["--agent-page-recovery"], True)):
            with self.subTest(flags=flags):
                args = parse_args(["sample.htm", "--year", "2025", *flags])
                self.assertEqual(args.agent_page_recovery, expected)
                with patch("sec_disclosure.llm.config.load_config", return_value=CONFIG), \
                     patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks", return_value=FallbackResult({}, 0)) as locator, \
                     redirect_stdout(io.StringIO()):
                    apply_item_fallback(HTML, html_to_blocks(HTML), {}, ["1"], args)
                self.assertEqual(locator.call_args.kwargs["page_recovery"], expected)

    def test_disable_all_agents_takes_precedence_over_checks_and_page_recovery(self):
        args = parse_args(["sample.htm", "--year", "2025", "--no-agent-fallback", "--agent-check-items", "8", "--agent-page-recovery"])
        with patch("sec_disclosure.llm.config.load_config") as config, \
             patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks") as locator:
            result = apply_item_fallback(HTML, html_to_blocks(HTML), {}, ["8"], args)
        self.assertEqual(result, set())
        config.assert_not_called()
        locator.assert_not_called()

    def test_normal_preprocessing_accepts_locator_source_blocks(self):
        blocks = html_to_blocks(HTML)
        selections = {"1": blocks[:2], "1A": blocks[2:4],
                      "7": blocks[4:6], "8": blocks[6:8]}
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "sample-20241231.htm"
            source.write_text(HTML, encoding="utf-8")
            output = Path(temporary) / "out"
            with patch("sec_disclosure.llm.config.load_config", return_value=CONFIG), \
                 patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks",
                       return_value=FallbackResult(selections, 1)) as locator, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main([str(source), "--company", "sample", "--year", "2024",
                             "--out-dir", str(output), "--no-follow-references"])
            self.assertEqual(code, 0)
            locator.assert_called_once()
            chunks = json.loads((output / "sample" / "2024" / "2024_chunks.json").read_text())
            self.assertEqual({row["item"] for row in chunks}, set(selections))

    def test_recovered_item8_moves_source_blocks_out_of_existing_item15(self):
        html = ("<div>Item 8. Financial Statements</div>"
                "<p>The information required by this Item is set forth in our Consolidated Financial Statements.</p>"
                "<div>Item 9. Changes in Accountants</div><p>None.</p>"
                "<div>Item 15. Exhibits and Financial Statements</div>"
                "<h2>Report of Independent Registered Public Accounting Firm</h2>"
                "<p>Our consolidated statements are audited.</p><div>EXHIBIT INDEX</div>")
        blocks = html_to_blocks(html)
        sections = {"8": [blocks[1]], "15": [
            replace(blocks[5], text=blocks[5].text + " merged text"), *blocks[6:]
        ]}
        args = Namespace(no_agent_fallback=False, env_file=None,
                         agent_timeout=180, agent_max_tokens=1200)
        with patch("sec_disclosure.llm.config.load_config", return_value=CONFIG), \
             patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks",
                   return_value=FallbackResult({"8": blocks[5:7]}, 1)), \
             redirect_stdout(io.StringIO()):
            result = apply_item_fallback(html, blocks, sections, ["8", "15"], args)
        self.assertEqual(result, {"8"})
        self.assertEqual(sections["8"], blocks[5:7])
        self.assertEqual([block.text for block in sections["15"]], ["EXHIBIT INDEX"])

    def test_missing_item_can_take_its_source_blocks_from_item15(self):
        html = ("<p>Management discussion body.</p><p>Revenue improved.</p>"
                "<p>Exhibit list follows.</p>")
        blocks = html_to_blocks(html)
        sections = {"15": list(blocks)}
        args = Namespace(no_agent_fallback=False, env_file=None,
                         agent_timeout=180, agent_max_tokens=1200)
        with patch("sec_disclosure.llm.config.load_config", return_value=CONFIG), \
             patch("sec_disclosure.agents.filing_item_locator.locate_item_blocks",
                   return_value=FallbackResult({"7": blocks[:2]}, 1)), \
             redirect_stdout(io.StringIO()):
            recovered = apply_item_fallback(html, blocks, sections, ["7"], args)
        self.assertEqual(recovered, {"7"})
        self.assertEqual(sections["7"], blocks[:2])
        self.assertEqual(sections["15"], blocks[2:])


if __name__ == "__main__":
    unittest.main()
