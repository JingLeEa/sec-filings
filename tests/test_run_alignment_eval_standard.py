"""Concurrent standard-API evaluation tests without network requests."""

import importlib.util
import fcntl
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_alignment_eval_openai_standard.py"
SPEC = importlib.util.spec_from_file_location("run_alignment_eval_openai_standard", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def model_dump(self):
        return self.body


class FakeResponses:
    def __init__(self):
        self.calls = []
        self.invalid_change = False
        self.fail_change = False

    def create(self, **body):
        self.calls.append(body)
        payload = json.loads(body["input"])
        if "alignments" in payload:
            if self.fail_change:
                raise RuntimeError("Temporary API failure")
            ids = [row["match_id"] for row in payload["alignments"]]
            result = {"action": "classify", "analyses": [
                {"match_id": key, "llm": "Modified"} for key in ids]}
            if self.invalid_change:
                result["analyses"][0]["llm"] = "not_a_label"
        else:
            ids = [row["match_id"] for row in payload["rows"]]
            result = {"action": "classify_materiality", "classifications": [
                {"match_id": key, "materiality": "Yes", "materiality_score": 0.8,
                 "confidence_score": 0.9, "reason": "Risk exposure expanded.",
                 "key_change": "Expanded risk exposure"} for key in ids]}
        return FakeResponse({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(result)}]}],
            "usage": {"input_tokens": 100, "output_tokens": 20}})


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()


class StandardEvalTests(unittest.TestCase):
    def test_default_alignment_root(self):
        args = runner.parse_args(["--ticker", "amd", "--previous-year", "2023",
                                  "--current-year", "2024"])
        self.assertEqual(args.alignment_root, Path("data/alignments_for_eval_openai_standard"))

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
        self.client = FakeClient()

    def run_script(self, *extra):
        args = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                "--alignment-root", str(self.root / "alignments"),
                "--disclosures-dir", str(self.disclosures),
                "--model", "gpt-6.1-sol", "--workers", "2", *extra]
        with redirect_stdout(StringIO()):
            return runner.main(args, client=self.client)

    def test_both_stages_save_and_resume(self):
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(len(self.client.responses.calls), 2)
        self.assertTrue(all(call["reasoning"] == {"effort": "low"}
                            for call in self.client.responses.calls))
        self.assertTrue(all("materiality_analysis" not in call["input"]
                            for call in self.client.responses.calls))
        document = json.loads(self.alignment.read_text(encoding="utf-8"))
        matched, exact, removed = document["alignments"]
        self.assertEqual(matched["change_analysis"], {"status": "completed", "llm": "Modified"})
        self.assertEqual(matched["materiality_analysis"]["materiality"], "Yes")
        self.assertEqual(exact["change_analysis"]["llm"], "Unchanged")
        self.assertEqual(exact["materiality_analysis"]["materiality"], "No")
        self.assertEqual(removed["change_analysis"]["llm"], "Removed")
        ledger_path = self.alignment.parent / "openai_eval" / "standard_token_usage.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"], ledger["requests"]),
                         (200, 40, 2))
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(len(self.client.responses.calls), 2)
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8")), ledger)

    def test_replays_saved_response_without_double_charge(self):
        self.assertEqual(self.run_script("--stage", "change"), 0)
        ledger_path = self.alignment.parent / "openai_eval" / "standard_token_usage.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        document = json.loads(self.alignment.read_text(encoding="utf-8"))
        document["alignments"][0]["change_analysis"] = {"status": "not_started", "llm": None}
        self.alignment.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(len(self.client.responses.calls), 1)
        self.assertEqual(json.loads(ledger_path.read_text(encoding="utf-8")), ledger)
        restored = json.loads(self.alignment.read_text(encoding="utf-8"))
        self.assertEqual(restored["alignments"][0]["change_analysis"]["llm"], "Modified")

    def test_invalid_response_is_billed_and_retryable(self):
        self.client.responses.invalid_change = True
        self.assertEqual(self.run_script("--stage", "change"), 1)
        work = self.alignment.parent / "openai_eval"
        ledger = json.loads((work / "standard_token_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 4)
        self.assertTrue(json.loads((work / "standard_errors.json").read_text(encoding="utf-8")))
        self.client.responses.invalid_change = False
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(len(self.client.responses.calls), 5)
        saved = json.loads(self.alignment.read_text(encoding="utf-8"))
        self.assertEqual(saved["alignments"][0]["change_analysis"]["llm"], "Modified")
        ledger = json.loads((work / "standard_token_usage.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 5)

    def test_invalid_response_recovers_within_same_run(self):
        responses = self.client.responses
        original = responses.create

        def eventually_valid(**body):
            responses.invalid_change = len(responses.calls) < 2
            return original(**body)

        responses.create = eventually_valid
        self.assertEqual(self.run_script("--stage", "change"), 0)
        self.assertEqual(len(responses.calls), 3)
        ledger = json.loads((self.alignment.parent / "openai_eval" / "standard_token_usage.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(ledger["requests"], 3)

    def test_active_batch_blocks_standard_requests(self):
        work = self.alignment.parent / "openai_eval"
        work.mkdir()
        runner.shared.atomic_json(work / "active_batch.json", {"batch_id": "batch_active"})
        with self.assertRaisesRegex(ValueError, "Batch job is active"):
            self.run_script()
        self.assertEqual(len(self.client.responses.calls), 0)

    def test_second_standard_process_is_blocked(self):
        work = self.alignment.parent / "openai_eval"
        work.mkdir()
        with (work / "standard_runner.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(ValueError, "Another standard evaluation"):
                self.run_script()
        self.assertEqual(len(self.client.responses.calls), 0)

    def test_api_failure_leaves_rows_pending_for_resume(self):
        self.client.responses.fail_change = True
        self.assertEqual(self.run_script("--stage", "change"), 1)
        work = self.alignment.parent / "openai_eval"
        self.assertTrue(json.loads((work / "standard_errors.json").read_text(encoding="utf-8")))
        self.assertFalse((work / "standard_token_usage.json").exists())
        self.client.responses.fail_change = False
        self.assertEqual(self.run_script("--stage", "change"), 0)
        saved = json.loads(self.alignment.read_text(encoding="utf-8"))
        self.assertEqual(saved["alignments"][0]["change_analysis"]["llm"], "Modified")

    def test_spend_guard_includes_prior_batch_usage(self):
        work = self.alignment.parent / "openai_eval"
        work.mkdir()
        runner.shared.atomic_json(work / "token_usage.json", {"estimated_usd": 70.0})
        with self.assertRaisesRegex(ValueError, "spend estimate"):
            self.run_script()
        self.assertEqual(len(self.client.responses.calls), 0)

    def test_dry_run_does_not_write_or_call_api(self):
        original = self.alignment.read_bytes()
        self.assertEqual(self.run_script("--dry-run", "--max-new-rows", "1"), 0)
        self.assertEqual(self.alignment.read_bytes(), original)
        self.assertFalse((self.alignment.parent / "openai_eval").exists())
        self.assertEqual(len(self.client.responses.calls), 0)


if __name__ == "__main__":
    unittest.main()
