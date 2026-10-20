"""SoCLaaS evaluation tests use a fake completion function, never the network."""

import importlib.util
import json
import threading
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_alignment_eval_soclaas.py"
SPEC = importlib.util.spec_from_file_location("run_alignment_eval_soclaas", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FakeCompletion:
    def __init__(self):
        self.calls = []
        self.invalid = False
        self.error = None

    def __call__(self, prompt, *, config, system_prompt, json_mode, max_tokens, timeout):
        self.calls.append({"prompt": prompt, "model": config.model,
                           "system_prompt": system_prompt, "json_mode": json_mode,
                           "max_tokens": max_tokens, "timeout": timeout})
        if self.error:
            raise self.error
        payload = json.loads(prompt)
        if "alignments" in payload:
            rows = [{"match_id": row["match_id"], "llm": "Modified"}
                    for row in payload["alignments"]]
            if self.invalid:
                rows[0]["llm"] = "not_a_label"
            result = {"action": "classify", "analyses": rows}
        else:
            result = {"action": "classify_materiality", "classifications": [
                {"match_id": row["match_id"], "materiality": "Yes",
                 "materiality_score": 0.8, "confidence_score": 0.9,
                 "reason": "Risk exposure expanded.", "key_change": "Expanded risk exposure"}
                for row in payload["rows"]]}
        return Result(json.dumps(result))


class Result:
    def __init__(self, text):
        self.text = text
        self.model = "deepseek-v4.1-flash-1017"
        self.finish_reason = "stop"
        self.usage = {"prompt_tokens": 100, "completion_tokens": 20}


class SoclaasEvalTests(unittest.TestCase):
    def test_default_alignment_root(self):
        args = runner.parse_args(["--ticker", "amd", "--previous-year", "2023",
                                  "--current-year", "2024"])
        self.assertEqual(args.alignment_root, Path("data/alignments_for_eval_soclaas"))

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.alignment = self.root / "alignments" / "amd" / "2023-2024" / "alignments.json"
        self.alignment.parent.mkdir(parents=True)
        rows = []
        for number, status, before, after in (
            (1, "ai_verified", ["old_1"], ["new_1"]),
            (2, "auto_matched", ["old_2"], ["new_2"]),
            (3, "unmatched", ["old_3"], []),
        ):
            rows.append({"match_id": f"M{number}", "status": status,
                         "relationship": "one_to_one" if after else "previous_only",
                         "previous_ids": before, "current_ids": after,
                         "explanation": "Aligned source evidence.", "evidence": [],
                         "previous_disclosures": [], "current_disclosures": [],
                         "change_analysis": {"status": "not_started", "llm": None},
                         "materiality_analysis": {"materiality": None, "materiality_score": None,
                                                  "confidence_score": None, "reason": None}})
        self.alignment.write_text(json.dumps({"company": "amd", "previous_year": "2023",
                                              "current_year": "2024", "run_complete": True,
                                              "alignments": rows}), encoding="utf-8")
        self.disclosures = self.root / "disclosures"
        for year, keys in (("2023", ["old_1", "old_2", "old_3"]),
                           ("2024", ["new_1", "new_2"])):
            folder = self.disclosures / "amd" / year
            folder.mkdir(parents=True)
            (folder / "disclosures.json").write_text(json.dumps({
                "company": "amd", "fiscal_year": year,
                "disclosures": [{"disclosure_id": key, "fiscal_year": year,
                                 "item": "1A", "section": "Risks",
                                 "content": f"Original source text {key}"} for key in keys],
            }), encoding="utf-8")
        self.completion = FakeCompletion()
        self.config = runner.LLMConfig(base_url="https://soclaas.example/v1",
                                       api_key="fake", model="default")

    def run_script(self, *extra):
        args = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                "--alignment-root", str(self.root / "alignments"),
                "--disclosures-dir", str(self.disclosures), "--request-interval", "0", *extra]
        with redirect_stdout(StringIO()):
            return runner.main(args, completion=self.completion, config=self.config)

    def test_both_stages_save_and_resume(self):
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(len(self.completion.calls), 2)
        self.assertTrue(all(call["model"] == "x-test-2" and call["json_mode"]
                            for call in self.completion.calls))
        self.assertTrue(all(call["max_tokens"] == 16384 for call in self.completion.calls))
        self.assertEqual(self.completion.calls[0]["system_prompt"],
                         runner.shared.CHANGE_ANALYSIS_PROMPT)
        self.assertEqual(self.completion.calls[1]["system_prompt"],
                         runner.shared.MATERIALITY_PROMPT)
        self.assertTrue(all("materiality_analysis" not in call["prompt"]
                            for call in self.completion.calls))
        document = json.loads(self.alignment.read_text(encoding="utf-8"))
        matched, exact, removed = document["alignments"]
        self.assertEqual(matched["change_analysis"]["llm"], "Modified")
        self.assertEqual(matched["materiality_analysis"]["materiality"], "Yes")
        self.assertEqual(exact["change_analysis"]["llm"], "Unchanged")
        self.assertEqual(exact["materiality_analysis"]["materiality"], "No")
        self.assertEqual(removed["change_analysis"]["llm"], "Removed")
        ledger_path = self.alignment.parent / "soclaas_eval" / "token_usage.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual((ledger["prompt_tokens"], ledger["completion_tokens"], ledger["requests"]),
                         (200, 40, 2))
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(len(self.completion.calls), 2)
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8")), ledger)

    def test_invalid_response_is_counted_and_retryable(self):
        self.completion.invalid = True
        self.assertEqual(self.run_script("--stage", "change"), 1)
        work = self.alignment.parent / "soclaas_eval"
        ledger = json.loads((work / "token_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 4)
        self.completion.invalid = False
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(len(self.completion.calls), 5)
        self.assertEqual(json.loads(self.alignment.read_text(encoding="utf-8"))["alignments"][0]
                         ["change_analysis"]["llm"], "Modified")
        ledger = json.loads((work / "token_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 5)

    def test_invalid_response_recovers_within_same_run(self):
        completion = self.completion
        original = completion.__call__

        def eventually_valid(*args, **kwargs):
            completion.invalid = len(completion.calls) < 2
            return original(*args, **kwargs)

        self.completion = eventually_valid
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(len(completion.calls), 3)
        ledger = json.loads((self.alignment.parent / "soclaas_eval" / "token_usage.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 3)
        self.assertEqual(json.loads(self.alignment.read_text(encoding="utf-8"))["alignments"][0]
                         ["change_analysis"]["llm"], "Modified")

    def test_rate_limit_stops_without_losing_pending_rows(self):
        self.completion.error = runner.LLMError("Rate limit", status_code=429,
                                               retry_after=120, retryable=True)
        self.assertEqual(self.run_script("--stage", "change"), 1)
        self.assertEqual(len(self.completion.calls), 1)
        self.assertTrue((self.alignment.parent / "soclaas_eval" / "errors.json").exists())
        self.completion.error = None
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(json.loads(self.alignment.read_text(encoding="utf-8"))["alignments"][0]
                         ["change_analysis"]["llm"], "Modified")

    def test_two_workers_send_requests_concurrently(self):
        barrier = threading.Barrier(2)
        original = self.completion

        def concurrent_completion(*args, **kwargs):
            barrier.wait(timeout=2)
            return original(*args, **kwargs)

        self.completion = concurrent_completion
        self.assertEqual(self.run_script("--workers", "2"), 0)
        self.assertEqual(len(original.calls), 2)

    def test_request_interval_paces_parallel_starts(self):
        starts = []
        original = self.completion

        def timed_completion(*args, **kwargs):
            starts.append(time.monotonic())
            return original(*args, **kwargs)

        self.completion = timed_completion
        self.assertEqual(self.run_script("--workers", "2", "--request-interval", "0.05"), 0)
        self.assertEqual(len(starts), 2)
        first, second = sorted(starts)
        self.assertGreaterEqual(second - first, 0.04)

    def test_parallel_api_error_drains_in_flight_without_new_requests(self):
        barrier = threading.Barrier(2)
        started = []
        original = self.completion

        def limited_completion(prompt, **kwargs):
            payload = json.loads(prompt)
            started.append(payload)
            barrier.wait(timeout=2)
            if "alignments" in payload:
                raise runner.LLMError("Rate limit", status_code=429,
                                      retry_after=120, retryable=True)
            time.sleep(0.05)
            return original(prompt, **kwargs)

        self.completion = limited_completion
        self.assertEqual(self.run_script("--workers", "2", "--batch-size", "1"), 1)
        self.assertEqual(len(started), 2)
        ledger = json.loads((self.alignment.parent / "soclaas_eval" / "token_usage.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 1)
        saved = json.loads(self.alignment.read_text(encoding="utf-8"))["alignments"]
        self.assertEqual(saved[0]["change_analysis"]["status"], "not_started")
        self.assertEqual(saved[0]["materiality_analysis"]["materiality"], "Yes")
        self.assertIsNone(saved[2]["materiality_analysis"]["materiality"])

    def test_rejects_openai_state_or_model_change(self):
        openai = self.alignment.parent / "openai_eval"
        openai.mkdir()
        runner.shared.atomic_json(openai / "config.json", {"model": "gpt-6-luna"})
        with self.assertRaisesRegex(ValueError, "separate SoCLaaS copy"):
            self.run_script()
        (openai / "config.json").unlink()
        self.assertEqual(self.run_script("--stage", "change"), 0)
        with self.assertRaisesRegex(ValueError, "Model, endpoint"):
            self.run_script("--model", "another-model")

    def test_dry_run_does_not_write_or_call(self):
        original = self.alignment.read_bytes()
        self.assertEqual(self.run_script("--dry-run", "--max-new-rows", "1"), 0)
        self.assertEqual(self.alignment.read_bytes(), original)
        self.assertFalse((self.alignment.parent / "soclaas_eval").exists())
        self.assertEqual(self.completion.calls, [])


if __name__ == "__main__":
    unittest.main()
