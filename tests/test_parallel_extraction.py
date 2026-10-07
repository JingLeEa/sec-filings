import io
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sec_disclosure.llm import disclosures, request_pacing
from sec_disclosure.llm.client import CompletionResult, LLMError
from sec_disclosure.llm.config import LLMConfig


class ParallelExtractionTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for year in ("2023", "2024", "2025"):
            folder = self.root / "raw/amd" / year
            folder.mkdir(parents=True)
            paragraphs, sentences = [], []
            for item, text in (("7", f"Revenue rose in {year}."), ("8", "Debt matures in 2030.")):
                paragraph = {"id": f"amd_{year}_{item}_P001", "company": "amd", "year": year,
                             "item": item, "item_title": "Results", "section_path": ["Results"],
                             "source": f"https://example.test/{year}.htm", "text": text}
                paragraphs.append(paragraph)
                sentences.append({**paragraph, "id": paragraph["id"] + "_S001",
                                  "chunk_id": paragraph["id"], "sentence_index": 1})
            (folder / f"{year}_chunks.json").write_text(json.dumps(paragraphs))
            (folder / f"{year}_chunk_sentences.json").write_text(json.dumps(sentences))

    def response(self, prompt, **kwargs):
        payload, _ = json.JSONDecoder().raw_decode(prompt)
        if payload.get("task") == "consolidate_subsection":
            return CompletionResult('{"merges":[],"relationships":[]}', "test-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        if payload.get("task") == "check_disclosure_boundary":
            groups = [{"candidate_ids": [candidate["candidate_id"]], "resolved": True,
                       "reason": "This is a complete independent topic."}
                      for candidate in payload["candidates"]]
            return CompletionResult(json.dumps({"groups": groups}), "test-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
        selected = [unit for row in payload["paragraphs"] for unit, _ in row["sentences"]]
        body = {"disclosures": [{"topic": "Reported results", "summary": "The filing reports this fact.",
                                "taxonomy": "Financial & Capital Resources", "unit_ids": selected,
                                "review_reason": ""}], "excluded": []}
        return CompletionResult(json.dumps(body), "test-model", "stop",
                                {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

    def split_filings(self, paragraphs_per_year=2):
        for year in ("2023", "2024", "2025"):
            folder = self.root / "raw/amd" / year
            paragraphs, sentences = [], []
            for number in range(1, paragraphs_per_year + 1):
                paragraph = {"id": f"amd_{year}_7_P{number:03d}", "company": "amd", "year": year,
                             "item": "7", "item_title": "Results", "section_path": ["Results"],
                             "source": f"https://example.test/{year}.htm",
                             "text": "Revenue " + "increased " * 65 + f"in {year}."}
                paragraphs.append(paragraph)
                sentences.append({**paragraph, "id": paragraph["id"] + "_S001",
                                  "chunk_id": paragraph["id"], "sentence_index": 1})
            (folder / f"{year}_chunks.json").write_text(json.dumps(paragraphs))
            (folder / f"{year}_chunk_sentences.json").write_text(json.dumps(sentences))

    def run_cli(self, *extra, responder=None, years=("2023", "2024", "2025"), batch_workers=1):
        batch_options = [] if batch_workers is None else ["--batch-workers", str(batch_workers)]
        output = io.StringIO()
        with patch.object(disclosures, "load_config", return_value=LLMConfig(
                "https://example.test/v1", "fake-key", "default")) as config, \
             patch.object(disclosures, "request_completion", side_effect=responder or self.response) as api, \
             redirect_stdout(output), redirect_stderr(output):
            code = disclosures.main(["--ticker", "amd", "--years", *years,
                                     "--data-dir", str(self.root), "--request-interval", "0",
                                     "--rate-limit-cooldown", "0", "--response-retry-backoff", "0",
                                     *batch_options, *extra])
        return code, api.call_count, config.call_count, output.getvalue()

    def usage(self, year):
        return json.loads((self.root / "disclosures/amd" / year / "token_usage.json").read_text())

    def test_three_years_actually_overlap_and_resume_without_new_calls(self):
        barrier = threading.Barrier(3, timeout=5)
        years_in_flight = set()
        lock = threading.Lock()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload["item"] == "7":
                with lock:
                    years_in_flight.add(payload["fiscal_year"])
                barrier.wait()
                self.assertEqual(years_in_flight, {"2023", "2024", "2025"})
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=response)[:3], (0, 6, 1))
        for year in ("2023", "2024", "2025"):
            usage = self.usage(year)
            self.assertTrue(usage["run_complete"])
            self.assertEqual(usage["reported_tokens"]["total_tokens"], 250)
            document = json.loads((self.root / "disclosures/amd" / year / "disclosures.json").read_text())
            self.assertEqual(document["fiscal_year"], year)
            for row in document["disclosures"]:
                self.assertTrue(row["disclosure_id"].startswith(f"amd_{year}_"))
                self.assertEqual(row["sources"][0]["source_url"], f"https://example.test/{year}.htm")
        self.assertEqual(self.run_cli()[:2], (0, 0))

    def test_default_three_years_run_ten_batches_each_with_thirty_overlapping_requests(self):
        self.split_filings(11)
        barrier = threading.Barrier(30, timeout=10)
        lock = threading.Lock()
        active, peaks, started = Counter(), Counter(), Counter()
        peak_total = 0

        def response(prompt, **kwargs):
            nonlocal peak_total
            payload = json.loads(prompt)
            year = payload["fiscal_year"]
            if payload.get("task") in ("check_disclosure_boundary", "consolidate_subsection"):
                self.assertEqual(started[year], 11)
                return self.response(prompt, **kwargs)
            with lock:
                started[year] += 1
                first_wave = started[year] <= 10
                active[year] += 1
                peaks[year] = max(peaks[year], active[year])
                peak_total = max(peak_total, sum(active.values()))
            try:
                if first_wave:
                    barrier.wait()
                return self.response(prompt, **kwargs)
            finally:
                with lock:
                    active[year] -= 1

        code, calls, _, output = self.run_cli("--batch-chars", "1000", responder=response, batch_workers=None)
        self.assertEqual((code, calls), (0, 66))
        self.assertEqual(peaks, {"2023": 10, "2024": 10, "2025": 10})
        self.assertEqual(peak_total, 30)
        self.assertIn("10 batch workers per year (30 concurrent extraction batches)", output)
        self.assertIn("up to 30 simultaneous API requests", output)
        self.assertTrue(all(self.usage(year)["run_complete"] for year in ("2023", "2024", "2025")))
        self.assertEqual(self.run_cli("--batch-chars", "1000", batch_workers=None)[:2], (0, 0))

    def test_startup_reports_a_smaller_shared_api_cap_without_output_writes(self):
        code, calls, _, output = self.run_cli("--max-concurrent-requests", "8", "--dry-run", batch_workers=None)
        self.assertEqual((code, calls), (0, 0))
        self.assertIn("30 concurrent extraction batches", output)
        self.assertIn("up to 8 simultaneous API requests", output)
        self.assertFalse((self.root / "disclosures").exists())

    def test_request_cap_is_per_year_and_resume_keeps_usage(self):
        self.assertEqual(self.run_cli("--max-requests", "1")[:2], (2, 3))
        for year in ("2023", "2024", "2025"):
            self.assertFalse(self.usage(year)["run_complete"])
            self.assertEqual(self.usage(year)["attempted_requests"], 1)
        self.assertEqual(self.run_cli()[:2], (0, 3))
        self.assertTrue(all(self.usage(year)["run_complete"] for year in ("2023", "2024", "2025")))

    def test_parallel_years_finish_extraction_before_ordered_boundary_checks(self):
        self.split_filings()
        extracted = Counter()
        lock = threading.Lock()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            year = payload["fiscal_year"]
            if payload.get("task") == "consolidate_subsection":
                self.assertEqual(extracted[year], 2)
                return self.response(prompt, **kwargs)
            if payload.get("task") == "check_disclosure_boundary":
                self.assertEqual(extracted[year], 2)
                groups = [{"candidate_ids": [candidate["candidate_id"]], "resolved": True,
                           "reason": "This is a complete independent topic."}
                          for candidate in payload["candidates"]]
                return CompletionResult(json.dumps({"groups": groups}), "test-model", "stop",
                                        {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
            with lock:
                extracted[year] += 1
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli("--batch-chars", "1000", responder=response)[:2], (0, 12))
        for year in ("2023", "2024", "2025"):
            self.assertTrue(self.usage(year)["run_complete"])
            self.assertEqual(self.usage(year)["by_stage"]["boundary_check"]["requests"], 1)

    def test_failed_year_does_not_stop_others_or_retry_non_429(self):
        def response(prompt, **kwargs):
            if json.loads(prompt)["fiscal_year"] == "2024":
                raise LLMError("SoCLaaS returned HTTP 401.", status_code=401)
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=response)[:2], (1, 5))
        self.assertTrue(self.usage("2023")["run_complete"])
        self.assertFalse(self.usage("2024")["run_complete"])
        self.assertEqual(self.usage("2024")["attempted_requests"], 1)
        self.assertTrue(self.usage("2025")["run_complete"])
        self.assertEqual(self.run_cli()[:2], (1, 0))
        self.assertEqual(self.run_cli("--retry-failed")[:2], (0, 2))

    def test_429_retry_preserves_failed_attempt_and_unknown_usage(self):
        attempts = Counter()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            key = payload["fiscal_year"], payload["item"]
            attempts[key] += 1
            if key == ("2023", "7") and attempts[key] == 1:
                raise LLMError("SoCLaaS returned HTTP 429.", status_code=429)
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(responder=response)[:2], (0, 7))
        usage = self.usage("2023")
        self.assertEqual(usage["attempted_requests"], 3)
        self.assertEqual(usage["requests_with_unknown_usage"], 1)
        self.assertEqual(usage["reported_tokens"]["total_tokens"], 250)
        history = sorted((self.root / "disclosures/amd/2023/requests").glob("batch_001_attempt_*.json"))
        self.assertEqual([json.loads(p.read_text())["status"] for p in history], ["failed", "completed"])

    def test_persistent_429_is_bounded_and_request_cap_includes_retries(self):
        def response(prompt, **kwargs):
            raise LLMError("SoCLaaS returned HTTP 429.", status_code=429)

        self.assertEqual(self.run_cli(responder=response, years=("2023",))[:2], (1, 3))
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 3)
        self.assertEqual(self.run_cli("--max-requests", "1", responder=response, years=("2024",))[:2], (1, 1))

    def test_dry_run_and_preflight_error_make_no_requests_or_outputs(self):
        self.assertEqual(self.run_cli("--dry-run")[:3], (0, 0, 0))
        self.assertFalse((self.root / "disclosures").exists())
        (self.root / "raw/amd/2025/2025_chunks.json").unlink()
        self.assertEqual(self.run_cli()[:2], (1, 0))
        self.assertFalse((self.root / "disclosures").exists())

    def test_all_manifests_checked_before_any_year_is_mutated(self):
        self.assertEqual(self.run_cli("--max-requests", "1")[:2], (2, 3))
        manifest_path = self.root / "disclosures/amd/2025/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["requested_model"] = "different-model"
        manifest_path.write_text(json.dumps(manifest))
        original = {p: p.read_bytes() for p in (self.root / "disclosures").rglob('*') if p.is_file()}
        self.assertEqual(self.run_cli()[:2], (1, 0))
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_output_root_uses_company_and_year_subdirectories(self):
        alternative = self.root / "new_disclosures"
        self.assertEqual(self.run_cli("--output-root", str(alternative))[:2], (0, 6))
        self.assertFalse((self.root / "disclosures").exists())
        self.assertTrue(all((alternative / "amd" / year / "disclosures.json").is_file()
                            for year in ("2023", "2024", "2025")))

    def test_cli_rejects_duplicate_years_shared_output_and_invalid_workers(self):
        for extra, years in (([], ("2023", "2023")), (["--output-dir", str(self.root / "shared")], ("2023", "2024")),
                             (["--workers", "0"], ("2023", "2024"))):
            with self.subTest(extra=extra, years=years), self.assertRaises(SystemExit) as error:
                self.run_cli(*extra, years=years)
            self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.root / "disclosures").exists())

    def test_workers_limit_simultaneous_year_jobs(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        first_pair = threading.Barrier(2, timeout=5)
        initial_years = set()

        def response(prompt, **kwargs):
            nonlocal active, peak
            payload = json.loads(prompt)
            with lock:
                active += 1
                peak = max(peak, active)
                if payload["item"] == "7":
                    initial_years.add(payload["fiscal_year"])
                wait = payload["item"] == "7" and payload["fiscal_year"] in {"2023", "2024"}
            if wait:
                first_pair.wait()
            result = self.response(prompt, **kwargs)
            with lock:
                active -= 1
            return result

        self.assertEqual(self.run_cli("--workers", "2", responder=response)[:2], (0, 6))
        self.assertEqual(peak, 2)
        self.assertEqual(initial_years, {"2023", "2024", "2025"})

    def test_fault_tolerant_retries_temporary_errors_with_exponential_backoff(self):
        attempts = Counter()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            key = payload["fiscal_year"], payload["item"]
            attempts[key] += 1
            if key == ("2023", "7"):
                if attempts[key] == 1:
                    raise LLMError("SoCLaaS request timed out.", retryable=True)
                if attempts[key] == 2:
                    raise LLMError("SoCLaaS returned HTTP 503.", status_code=503)
            return self.response(prompt, **kwargs)

        with patch.object(disclosures.time, "sleep") as sleep:
            self.assertEqual(self.run_cli("--fault-tolerant", "--retry-backoff", "2", responder=response)[:2], (0, 8))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 2)
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 250)

    def test_unlimited_retries_recover_beyond_default_limit_and_cap_backoff(self):
        attempts = 0

        def response(prompt, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts <= 8:
                raise LLMError("Temporary server failure.", status_code=503, retryable=True)
            return self.response(prompt, **kwargs)

        with patch.object(disclosures.time, "sleep") as sleep:
            self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "-1", "--retry-backoff", "2",
                                          responder=response, years=("2023",))[:2], (0, 10))
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16, 32, 60, 60, 60])
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 8)
        self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "-1", years=("2023",))[:2], (0, 0))

    def test_resume_uses_latest_attempt_after_attempt_numbers_exceed_three_digits(self):
        self.assertEqual(self.run_cli(years=("2023",))[:2], (0, 2))
        directory = self.root / "disclosures/amd/2023/requests"
        path = directory / "batch_001_attempt_001.json"
        completed = json.loads(path.read_text())
        path.unlink()
        completed["attempt"] = 1000
        (directory / "batch_001_attempt_1000.json").write_text(json.dumps(completed))
        failed = {**completed, "attempt": 999, "status": "failed", "retryable": True,
                  "error": "Temporary API failure.", "status_code": 503}
        failed.pop("result")
        (directory / "batch_001_attempt_999.json").write_text(json.dumps(failed))
        self.assertEqual(self.run_cli("--fault-tolerant", "--max-retries", "-1", years=("2023",))[:2], (0, 0))
        self.assertTrue(self.usage("2023")["run_complete"])

    def test_unlimited_api_retries_and_custom_boundary_correction_limit(self):
        self.split_filings()
        attempts = Counter()

        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            if payload.get("task") == "consolidate_subsection":
                return self.response(prompt, **kwargs)
            boundary = payload.get("task") == "check_disclosure_boundary"
            key = "boundary" if boundary else payload["paragraphs"][0]["sentences"][0][0]
            attempts[key] += 1
            if attempts[key] <= 5:
                if not boundary:
                    raise LLMError("Rate limited.", status_code=429, retryable=True)
                return CompletionResult("not JSON", "test-model", "stop",
                                        {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})
            return self.response(prompt, **kwargs)

        options = ("--fault-tolerant", "--max-retries", "-1", "--max-response-retries", "5",
                   "--retry-backoff", "0", "--batch-chars", "1000")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (0, 19))
        self.assertTrue(self.usage("2023")["run_complete"])
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 10)

    def test_unlimited_retries_still_obey_request_and_explicit_rate_limit_caps(self):
        def response(prompt, **kwargs):
            raise LLMError("Rate limited.", status_code=429, retryable=True)

        options = ("--fault-tolerant", "--max-retries", "-1", "--retry-backoff", "0")
        self.assertEqual(self.run_cli(*options, "--max-requests", "2", responder=response, years=("2023",))[:2], (1, 2))
        self.assertEqual(self.run_cli(*options, "--rate-limit-retries", "0", responder=response, years=("2024",))[:2], (1, 2))

    def test_extract_then_align_uses_completed_years_custom_roots_and_cached_resume(self):
        from sec_disclosure.agents import alignment_runtime, disclosure_alignment

        output_root = self.root / "custom_disclosures"
        alignment_root = self.root / "custom_alignments"
        pairs = []
        both_pairs = threading.Barrier(2, timeout=5)

        def response(prompt, **kwargs):
            # Every extraction year must finish before the first alignment call.
            for year in ("2023", "2024", "2025"):
                usage = json.loads((output_root / "amd" / year / "token_usage.json").read_text())
                self.assertTrue(usage["run_complete"])
            payload = json.loads(prompt)
            if "anchors" in payload:
                previous, current = payload["previous_year"], payload["current_year"]
                pairs.append((previous, current))
                both_pairs.wait()
                body = {"action": "propose", "matches": [
                    {"current_id": key, "previous_ids": [key.replace(current, previous)],
                     "rationale": "Same reported result."} for key in payload["anchors"]]}
            else:
                records = {row["disclosure_id"]: row for row in payload["records"]}
                body = {"action": "finalize", "alignments": [
                    {"previous_ids": group["previous_ids"], "current_ids": group["current_ids"],
                     "explanation": "Both discuss the reported result.", "needs_review": False, "review_reason": "",
                     "evidence": [{"disclosure_id": key, "sentence_ids": [records[key]["sentences"][0]["sentence_id"]]}
                                  for key in group["previous_ids"] + group["current_ids"]]}
                    for group in payload["proposed_groups"]]}
            return CompletionResult(json.dumps(body), "test-model", "stop",
                                    {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125})

        options = ("--align", "--output-root", str(output_root), "--alignment-output-root", str(alignment_root),
                   "--fault-tolerant", "--max-retries", "-1", "--alignment-max-requests", "-1",
                   "--alignment-max-total-tokens", "-1")
        with patch.object(disclosure_alignment, "load_config", return_value=LLMConfig("https://example.test/v1", "fake-key", "default")), \
             patch.object(alignment_runtime, "request_completion", side_effect=response) as api:
            self.assertEqual(self.run_cli(*options, years=("2025", "2023", "2024"))[:2], (0, 6))
            self.assertEqual(api.call_count, 4)
            self.assertCountEqual(pairs, [("2023", "2024"), ("2024", "2025")])
            for pair in ("2023-2024", "2024-2025"):
                report = json.loads((alignment_root / "amd" / pair / "alignments.json").read_text())
                self.assertTrue(report["run_complete"])
                self.assertEqual(report["counts"], {"ai_verified": 1, "auto_matched": 1})
            self.assertEqual(self.run_cli(*options, years=("2025", "2023", "2024"))[:2], (0, 0))
            self.assertEqual(api.call_count, 4)

    def test_alignment_waits_on_partial_or_failed_extraction_and_dry_run_is_read_only(self):
        from sec_disclosure.agents import disclosure_alignment

        with patch.object(disclosure_alignment, "main") as align:
            code, calls, _, output = self.run_cli("--align", "--dry-run")
            self.assertEqual((code, calls), (0, 0))
            self.assertIn("2023-2024, 2024-2025", output)
            self.assertFalse((self.root / "disclosures").exists())
            self.assertEqual(self.run_cli("--align", "--max-requests", "1")[:2], (2, 3))

            def response(prompt, **kwargs):
                raise LLMError("Unauthorized.", status_code=401, retryable=False)

            self.assertEqual(self.run_cli("--align", responder=response)[:2], (1, 3))
            align.assert_not_called()

    def test_alignment_pause_propagates_while_other_pairs_continue(self):
        from sec_disclosure.agents import disclosure_alignment

        def pause_first(arguments, **kwargs):
            previous = arguments[arguments.index("--previous-year") + 1]
            return 2 if previous == "2023" else 0

        with patch.object(disclosure_alignment, "main", side_effect=pause_first) as align:
            self.assertEqual(self.run_cli("--align")[:2], (2, 6))
            self.assertEqual(align.call_count, 2)
        with patch.object(disclosure_alignment, "main", return_value=0) as align:
            self.assertEqual(self.run_cli("--align", "--env-file", "test.env", "--alignment-workers", "4",
                                          "--alignment-max-new-requests", "2", "--alignment-revalidate-cache",
                                          "--retry-failed", "--fault-tolerant", "--max-retries", "-1")[:2], (0, 0))
            self.assertEqual(align.call_count, 2)
            arguments = align.call_args_list[0].args[0]
            for flag, value in (("--env-file", "test.env"), ("--workers", "4"), ("--max-new-requests", "2"),
                                ("--max-retries", "-1")):
                self.assertEqual(arguments[arguments.index(flag) + 1], value)
            for flag in ("--retry-failed", "--fault-tolerant", "--revalidate-cache"):
                self.assertIn(flag, arguments)

    def test_alignment_failure_is_reported_while_other_pairs_continue(self):
        from sec_disclosure.agents import disclosure_alignment

        def fail_first(arguments, **kwargs):
            previous = arguments[arguments.index("--previous-year") + 1]
            if previous == "2023":
                raise RuntimeError("Unexpected pair failure.")
            return 2

        with patch.object(disclosure_alignment, "main", side_effect=fail_first) as align:
            code, calls, _, output = self.run_cli("--align")
        self.assertEqual((code, calls), (1, 6))
        self.assertEqual(align.call_count, 2)
        self.assertIn('Alignment exit codes: {"2023-2024": 1, "2024-2025": 2}', output)

    def test_alignment_pair_workers_can_select_sequential_comparisons(self):
        from sec_disclosure.agents import disclosure_alignment

        pairs = []

        def align(arguments, **kwargs):
            previous = arguments[arguments.index("--previous-year") + 1]
            current = arguments[arguments.index("--current-year") + 1]
            pairs.append((previous, current))
            return 0

        with patch.object(disclosure_alignment, "main", side_effect=align):
            code, calls, _, output = self.run_cli("--align", "--alignment-pair-workers", "1",
                                                years=("2025", "2023", "2024"))
        self.assertEqual((code, calls), (0, 6))
        self.assertEqual(pairs, [("2023", "2024"), ("2024", "2025")])
        self.assertIn("across up to 1 parallel pairs", output)

    def test_parallel_alignment_pairs_share_one_api_cap_and_separate_ledgers(self):
        from sec_disclosure.agents import alignment_runtime, disclosure_alignment

        for cap_options, expected_cap in (((), 30), (("--max-concurrent-requests", "12"), 12)):
            with self.subTest(cap_options=cap_options):
                lock = threading.Lock()
                all_attempted, pool_full, release = threading.Event(), threading.Event(), threading.Event()
                both_pairs = threading.Barrier(2, timeout=5)
                attempted = active = peak = 0
                runtimes = []
                folder = self.root / ("explicit_cap" if cap_options else "default_cap")

                class TrackingPacer(request_pacing.RequestPacer):
                    @contextmanager
                    def request(pacer):
                        nonlocal attempted
                        with lock:
                            attempted += 1
                            if attempted == 60:
                                all_attempted.set()
                        with super().request():
                            yield

                def response(*args, **kwargs):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                        if active == expected_cap:
                            pool_full.set()
                    try:
                        self.assertTrue(release.wait(timeout=5))
                        return CompletionResult("{}", "test-model", "stop",
                                                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                    finally:
                        with lock:
                            active -= 1

                def align(arguments, *, request_pacer):
                    previous = arguments[arguments.index("--previous-year") + 1]
                    current = arguments[arguments.index("--current-year") + 1]
                    args = SimpleNamespace(workers=30, request_interval=0, rate_limit_cooldown=0,
                        retry_failed=False, max_new_requests=None, max_requests=100, max_total_tokens=10000,
                        max_steps=4, max_tokens=100, max_prompt_chars=10000, timeout=5)
                    runtime = alignment_runtime.Runtime(folder / f"{previous}-{current}",
                        LLMConfig("https://example.test/v1", "fake-key", "default"), args, request_pacer=request_pacer)
                    with lock:
                        runtimes.append(runtime)
                    both_pairs.wait()
                    with ThreadPoolExecutor(max_workers=30) as executor:
                        futures = [executor.submit(runtime.call, "matching", f"job_{i}", str(i), "system")
                                   for i in range(30)]
                        for future in futures:
                            future.result(timeout=5)
                    return 0

                with patch.object(disclosures, "run_year", return_value=0), \
                     patch.object(disclosures, "RequestPacer", TrackingPacer), \
                     patch.object(disclosure_alignment, "main", side_effect=align), \
                     patch.object(alignment_runtime, "request_completion", side_effect=response) as api, \
                     ThreadPoolExecutor(max_workers=1) as coordinator:
                    future = coordinator.submit(self.run_cli, "--align", *cap_options, batch_workers=None)
                    try:
                        self.assertTrue(all_attempted.wait(timeout=5))
                        self.assertTrue(pool_full.wait(timeout=5))
                        self.assertEqual(api.call_count, expected_cap)
                        self.assertEqual(active, expected_cap)
                    finally:
                        release.set()
                    self.assertEqual(future.result(timeout=5)[:2], (0, 0))
                    self.assertEqual(api.call_count, 60)
                self.assertEqual((peak, active), (expected_cap, 0))
                self.assertIs(runtimes[0].pacer, runtimes[1].pacer)
                for runtime in runtimes:
                    self.assertEqual(len(runtime.requests), 30)
                    self.assertEqual(runtime.report()["reported_tokens"]["total_tokens"], 60)
                    self.assertTrue(all(r["status"] == "completed" for r in runtime.requests))

    def test_alignment_inherits_extraction_concurrency_and_honors_shared_cap(self):
        from sec_disclosure.agents import disclosure_alignment

        cases = [
            ((), ("2023", "2024", "2025"), 30),
            (("--workers", "2"), ("2023", "2024", "2025"), 20),
            (("--workers", "10"), ("2023", "2024"), 20),
            (("--max-concurrent-requests", "5"), ("2023", "2024", "2025"), 5),
            (("--max-concurrent-requests", "20"), ("2023", "2024", "2025"), 20),
            (("--max-concurrent-requests", "60"), ("2023", "2024", "2025"), 30),
            (("--batch-workers", "4"), ("2023", "2024", "2025"), 12),
            (("--alignment-workers", "2"), ("2023", "2024", "2025"), 2),
            (("--alignment-workers", "20", "--max-concurrent-requests", "5"), ("2023", "2024", "2025"), 5),
        ]
        for options, years, expected in cases:
            with self.subTest(options=options, years=years), \
                 patch.object(disclosures, "run_year", return_value=0), \
                 patch.object(disclosure_alignment, "main", return_value=0) as align:
                code, calls, _, output = self.run_cli("--align", *options, years=years, batch_workers=None)
                self.assertEqual((code, calls), (0, 0))
                self.assertEqual(align.call_count, len(years) - 1)
                self.assertIn(f"Alignment will use up to {expected} concurrent jobs per pair", output)
                for call in align.call_args_list:
                    arguments = call.args[0]
                    self.assertEqual(arguments[arguments.index("--workers") + 1], str(expected))
                if align.call_count == 2:
                    self.assertIs(align.call_args_list[0].kwargs["request_pacer"],
                                  align.call_args_list[1].kwargs["request_pacer"])

    def test_dry_run_shows_inherited_alignment_concurrency_without_calls_or_outputs(self):
        from sec_disclosure.agents import disclosure_alignment

        with patch.object(disclosure_alignment, "main") as align:
            code, calls, _, output = self.run_cli("--align", "--dry-run", batch_workers=None)
            self.assertEqual((code, calls), (0, 0))
            self.assertIn("Alignment will use up to 30 concurrent jobs per pair", output)
            align.assert_not_called()
            self.assertFalse((self.root / "disclosures").exists())

    def test_alignment_and_unlimited_retry_options_validate_before_extraction(self):
        for extra, years in ((("--align",), ("2023",)), (("--max-retries", "-2"), ("2023", "2024")),
                             (("--rate-limit-retries", "-2"), ("2023", "2024")),
                             (("--align", "--alignment-workers", "0"), ("2023", "2024")),
                             (("--align", "--alignment-pair-workers", "0"), ("2023", "2024")),
                             (("--align", "--alignment-max-requests", "-2"), ("2023", "2024")),
                             (("--align", "--alignment-max-total-tokens", "0"), ("2023", "2024"))):
            with self.subTest(extra=extra), self.assertRaises(SystemExit):
                self.run_cli(*extra, years=years)
        self.assertFalse((self.root / "disclosures").exists())

    def test_fault_tolerant_corrects_malformed_and_truncated_responses(self):
        attempts = Counter()
        prompts = []

        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            key = payload["fiscal_year"], payload["item"]
            attempts[key] += 1
            result = self.response(prompt, **kwargs)
            if key == ("2023", "7"):
                prompts.append(prompt)
                if attempts[key] == 1:
                    return CompletionResult("not JSON", result.model, "stop", result.usage)
                if attempts[key] == 2:
                    return CompletionResult(result.text, result.model, "length", result.usage)
            return result

        self.assertEqual(self.run_cli("--fault-tolerant", "--retry-backoff", "0", responder=response)[:2], (0, 8))
        self.assertIn("exact allowed ID", prompts[1])
        self.assertIn("length", prompts[2])
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 500)
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 0)

    def test_default_three_corrections_then_continues_later_batches_and_resumes_only_failure(self):
        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            result = self.response(prompt, **kwargs)
            return CompletionResult("not JSON", result.model, "stop", result.usage) if payload["item"] == "7" else result

        options = ("--fault-tolerant", "--retry-backoff", "0")
        code, calls, _, output = self.run_cli(*options, responder=response, years=("2023",))
        self.assertEqual((code, calls), (1, 5))
        self.assertIn("continuing later extraction batches", output)
        self.assertIn("Correction retry 3/3", output)
        usage = self.usage("2023")
        self.assertFalse(usage["run_complete"])
        self.assertEqual(usage["pending_batches"], [])
        self.assertEqual([f["batch_id"] for f in usage["failed_batches"]], ["batch_001"])
        document = json.loads((self.root / "disclosures/amd/2023/disclosures.json").read_text())
        self.assertEqual([row["item"] for row in document["disclosures"]], ["8"])
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 1))
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 750)
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 0))

    def test_ten_api_retries_then_moves_on_with_parallel_batches(self):
        attempts = Counter()
        lock = threading.Lock()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            with lock:
                attempts[payload["item"]] += 1
            if payload["item"] == "7":
                raise LLMError("Temporary API failure.", status_code=503, retryable=True)
            return self.response(prompt, **kwargs)

        options = ("--fault-tolerant", "--max-retries", "10", "--retry-backoff", "0", "--batch-workers", "2")
        code, calls, _, output = self.run_cli(*options, responder=response, years=("2023",))
        self.assertEqual((code, calls), (1, 12))
        self.assertEqual(attempts, {"7": 11, "8": 1})
        self.assertIn("continuing later extraction batches", output)
        usage = self.usage("2023")
        self.assertEqual(usage["pending_batches"], [])
        self.assertEqual([batch["batch_id"] for batch in usage["failed_batches"]], ["batch_001"])
        self.assertEqual(usage["requests_with_unknown_usage"], 11)
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 1))

    def test_fault_tolerant_recovers_boundary_json_and_keeps_order(self):
        self.split_filings(3)
        attempts = Counter()

        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            result = self.response(prompt, **kwargs)
            if payload.get("task") == "check_disclosure_boundary":
                key = payload["fiscal_year"], payload["boundary_id"]
                attempts[key] += 1
                if payload["boundary_id"] == "boundary_001" and attempts[key] == 1:
                    return CompletionResult('{"groups": []}', result.model, "stop", result.usage)
                if payload["boundary_id"] == "boundary_002":
                    self.assertEqual(attempts[(payload["fiscal_year"], "boundary_001")], 2)
            return result

        options = ("--fault-tolerant", "--retry-backoff", "0", "--batch-chars", "1000")
        self.assertEqual(self.run_cli(*options, responder=response)[:2], (0, 21))
        self.assertTrue(all(self.usage(year)["run_complete"] for year in ("2023", "2024", "2025")))

    def test_exhausted_boundary_waits_and_resumes_its_dependent_checks(self):
        self.split_filings(3)

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") == "check_disclosure_boundary":
                raise LLMError("Cannot connect to SoCLaaS.", retryable=True)
            return self.response(prompt, **kwargs)

        options = ("--fault-tolerant", "--max-retries", "0", "--retry-backoff", "0", "--batch-chars", "1000")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (1, 4))
        usage = self.usage("2023")
        self.assertFalse(usage["run_complete"])
        self.assertEqual([f["boundary_id"] for f in usage["failed_boundary_checks"]], ["boundary_001"])
        self.assertEqual(usage["pending_boundary_checks"], ["boundary_002"])
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 3))
        self.assertEqual(self.usage("2023")["attempted_requests"], 7)
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 1)

    def test_fault_tolerant_respects_request_cap_during_retries(self):
        def response(prompt, **kwargs):
            raise LLMError("SoCLaaS returned HTTP 503.", status_code=503)

        options = ("--fault-tolerant", "--retry-backoff", "0", "--max-requests", "2")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (1, 2))
        self.assertEqual(self.usage("2023")["pending_batches"], ["batch_002"])

    def test_fault_tolerant_does_not_retry_permanent_auth_or_quota_failures(self):
        for year, status in (("2023", 401), ("2024", 429)):
            def response(prompt, **kwargs):
                raise LLMError(f"SoCLaaS returned HTTP {status}.", status_code=status, retryable=False)
            self.assertEqual(self.run_cli("--fault-tolerant", "--retry-backoff", "0",
                                          responder=response, years=(year,))[:2], (1, 1))
            self.assertEqual(self.run_cli("--fault-tolerant", years=(year,))[:2], (1, 0))

    def test_fault_tolerant_resumes_interrupted_request_with_unknown_usage(self):
        self.assertEqual(self.run_cli(years=("2023",))[:2], (0, 2))
        path = self.root / "disclosures/amd/2023/requests/batch_001_attempt_001.json"
        saved = json.loads(path.read_text())
        saved["status"] = "started"
        saved.pop("result")
        path.write_text(json.dumps(saved))
        self.assertEqual(self.run_cli("--fault-tolerant", years=("2023",))[:2], (0, 1))
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 1)

    def test_unexpected_year_worker_error_is_isolated_without_exposing_details(self):
        original = disclosures.run_year

        def run_year(args, *remaining):
            if args.year == "2024":
                raise RuntimeError("fake-key")
            return original(args, *remaining)

        with patch.object(disclosures, "run_year", side_effect=run_year):
            code, calls, _, output = self.run_cli("--fault-tolerant")
        self.assertEqual((code, calls), (1, 4))
        self.assertNotIn("fake-key", output)
        self.assertTrue(self.usage("2023")["run_complete"])
        self.assertTrue(self.usage("2025")["run_complete"])

    def test_unexpected_client_failure_is_saved_with_unknown_usage(self):
        def response(prompt, **kwargs):
            raise RuntimeError("fake-key")

        code, calls, _, output = self.run_cli("--fault-tolerant", responder=response, years=("2023",))
        self.assertEqual((code, calls), (1, 1))
        self.assertNotIn("fake-key", output)
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 1)
        path = self.root / "disclosures/amd/2023/requests/batch_001_attempt_001.json"
        self.assertNotIn("fake-key", path.read_text())

    def test_batches_within_one_year_overlap_and_keep_source_order(self):
        barrier = threading.Barrier(2, timeout=5)
        finished_second = threading.Event()

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            barrier.wait()
            if payload["item"] == "7":
                self.assertTrue(finished_second.wait(5))
            else:
                finished_second.set()
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli("--batch-workers", "2", responder=response, years=("2023",))[:2], (0, 2))
        document = json.loads((self.root / "disclosures/amd/2023/disclosures.json").read_text())
        self.assertEqual([row["item"] for row in document["disclosures"]], ["7", "8"])
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 250)
        self.assertEqual(self.run_cli("--batch-workers", "2", years=("2023",))[:2], (0, 0))

    def test_single_year_parallel_batches_default_to_shared_pacing_and_429_retries(self):
        counts, lock = Counter(), threading.Lock()
        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            with lock:
                counts[payload["item"]] += 1
                attempt = counts[payload["item"]]
            if payload["item"] == "7" and attempt <= 2:
                raise LLMError("SoCLaaS returned HTTP 429.", status_code=429)
            return self.response(prompt, **kwargs)
        with patch.object(disclosures, "load_config", return_value=LLMConfig(
                "https://example.test/v1", "fake-key", "default")), \
             patch.object(disclosures, "RequestPacer", wraps=request_pacing.RequestPacer) as pacer, \
             patch.object(request_pacing.RequestPacer, "acquire"), \
             patch.object(disclosures, "request_completion", side_effect=response) as api, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = disclosures.main(["--ticker", "amd", "--year", "2023", "--data-dir", str(self.root),
                                       "--batch-workers", "2", "--rate-limit-cooldown", "0"])
        self.assertEqual(status, 0)
        self.assertEqual(api.call_count, 4)
        self.assertEqual(pacer.call_args.args, (2.0, 0.0, None))
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 2)
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 250)

    def test_parallel_batch_request_cap_is_atomic_with_retries(self):
        self.split_filings(6)

        def response(prompt, **kwargs):
            raise LLMError("SoCLaaS returned HTTP 503.", status_code=503)

        options = ("--batch-workers", "4", "--batch-chars", "1000", "--max-requests", "3",
                   "--fault-tolerant", "--retry-backoff", "0")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (1, 3))
        usage = self.usage("2023")
        self.assertEqual(usage["attempted_requests"], 3)
        self.assertEqual(usage["requests_with_unknown_usage"], 3)
        self.assertFalse(usage["run_complete"])

    def test_parallel_batch_failures_recover_without_losing_other_results(self):
        barrier = threading.Barrier(2, timeout=5)
        counts = Counter()
        lock = threading.Lock()

        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            with lock:
                counts[payload["item"]] += 1
                attempt = counts[payload["item"]]
            result = self.response(prompt, **kwargs)
            if attempt == 1:
                barrier.wait()
                if payload["item"] == "7":
                    return CompletionResult("invalid JSON", result.model, "stop", result.usage)
                raise LLMError("SoCLaaS request timed out.", retryable=True)
            return result

        options = ("--batch-workers", "2", "--fault-tolerant", "--retry-backoff", "0")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (0, 4))
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 375)
        self.assertEqual(self.usage("2023")["requests_with_unknown_usage"], 1)

    def test_global_api_cap_applies_across_years_and_parallel_batches(self):
        active = peak = 0
        lock = threading.Lock()
        barrier = threading.Barrier(2, timeout=5)

        def response(prompt, **kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait()
                return self.response(prompt, **kwargs)
            finally:
                with lock:
                    active -= 1

        options = ("--batch-workers", "2", "--max-concurrent-requests", "2")
        self.assertEqual(self.run_cli(*options, responder=response)[:2], (0, 6))
        self.assertEqual(peak, 2)

    def test_parallel_extraction_finishes_before_sequential_boundaries(self):
        self.split_filings(3)
        seen = set()
        lock = threading.Lock()
        barrier = threading.Barrier(3, timeout=5)

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            if payload.get("task") in ("check_disclosure_boundary", "consolidate_subsection"):
                self.assertEqual(seen, {1, 2, 3})
            else:
                with lock:
                    seen.add(payload["paragraphs"][0]["sentences"][0][0])
                barrier.wait()
            return self.response(prompt, **kwargs)

        options = ("--batch-workers", "3", "--batch-chars", "1000")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (0, 6))
        self.assertTrue(self.usage("2023")["run_complete"])

    def test_invalid_batch_worker_and_global_cap_are_rejected(self):
        for options in (("--batch-workers", "0"), ("--max-concurrent-requests", "0")):
            with self.subTest(options=options), self.assertRaises(SystemExit) as error:
                self.run_cli(*options)
            self.assertEqual(error.exception.code, 2)


class RequestPacingTests(TestCase):
    def test_shared_interval_and_provider_cooldown_delay_subsequent_starts(self):
        now = 100.0
        waits = []

        def wait(seconds):
            nonlocal now
            waits.append(seconds)
            now += seconds

        pacer = request_pacing.RequestPacer(interval=2, cooldown=5)
        with patch.object(request_pacing.time, "monotonic", side_effect=lambda: now), \
             patch.object(pacer.condition, "wait", side_effect=wait):
            pacer.acquire()
            self.assertEqual(now, 100)
            pacer.acquire()
            self.assertEqual(now, 102)
            pacer.rate_limited(retry_after=10)
            pacer.acquire()
            self.assertEqual(now, 112)
            pacer.acquire()
            self.assertEqual(now, 114)
        self.assertEqual(waits, [2, 10, 2])
