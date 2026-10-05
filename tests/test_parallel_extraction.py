import io
import json
import threading
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
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

    def run_cli(self, *extra, responder=None, years=("2023", "2024", "2025")):
        output = io.StringIO()
        with patch.object(disclosures, "load_config", return_value=LLMConfig(
                "https://example.test/v1", "fake-key", "default")) as config, \
             patch.object(disclosures, "request_completion", side_effect=responder or self.response) as api, \
             redirect_stdout(output), redirect_stderr(output):
            code = disclosures.main(["--ticker", "amd", "--years", *years,
                                     "--data-dir", str(self.root), "--request-interval", "0",
                                     "--rate-limit-cooldown", "0", *extra])
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

        self.assertEqual(self.run_cli("--batch-chars", "1000", responder=response)[:2], (0, 9))
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

    def test_exhausted_batch_continues_later_batches_and_resumes_only_failure(self):
        def response(prompt, **kwargs):
            payload, _ = json.JSONDecoder().raw_decode(prompt)
            result = self.response(prompt, **kwargs)
            return CompletionResult("not JSON", result.model, "stop", result.usage) if payload["item"] == "7" else result

        options = ("--fault-tolerant", "--max-retries", "1", "--retry-backoff", "0")
        code, calls, _, output = self.run_cli(*options, responder=response, years=("2023",))
        self.assertEqual((code, calls), (1, 3))
        self.assertIn("continuing later extraction batches", output)
        usage = self.usage("2023")
        self.assertFalse(usage["run_complete"])
        self.assertEqual(usage["pending_batches"], [])
        self.assertEqual([f["batch_id"] for f in usage["failed_batches"]], ["batch_001"])
        document = json.loads((self.root / "disclosures/amd/2023/disclosures.json").read_text())
        self.assertEqual([row["item"] for row in document["disclosures"]], ["8"])
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 1))
        self.assertEqual(self.usage("2023")["reported_tokens"]["total_tokens"], 500)
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 0))

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
        self.assertEqual(self.run_cli(*options, responder=response)[:2], (0, 18))
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
        self.assertEqual(self.run_cli(*options, years=("2023",))[:2], (0, 2))
        self.assertEqual(self.usage("2023")["attempted_requests"], 6)
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
            if payload.get("task") == "check_disclosure_boundary":
                self.assertEqual(seen, {1, 2, 3})
            else:
                with lock:
                    seen.add(payload["paragraphs"][0]["sentences"][0][0])
                barrier.wait()
            return self.response(prompt, **kwargs)

        options = ("--batch-workers", "3", "--batch-chars", "1000")
        self.assertEqual(self.run_cli(*options, responder=response, years=("2023",))[:2], (0, 5))
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
