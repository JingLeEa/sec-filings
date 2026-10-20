"""OpenAI Batch evaluation runner tests without network requests."""

import importlib.util
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_alignment_eval_openai_batch.py"
SPEC = importlib.util.spec_from_file_location("run_alignment_eval_openai_batch", SCRIPT)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


class FakeFiles:
    def __init__(self):
        self.input_lines = []
        self.output = ""

    def create(self, *, file, purpose):
        assert purpose == "batch"
        self.input_lines = [json.loads(line) for line in file.read().decode().splitlines()]
        return SimpleNamespace(id="file_input")

    def content(self, file_id):
        assert file_id == "file_output"
        return SimpleNamespace(text=self.output)


class FakeBatches:
    def __init__(self):
        self.status = "in_progress"
        self.submissions = 0

    def create(self, **kwargs):
        assert kwargs["endpoint"] == "/v1/responses"
        self.submissions += 1
        return SimpleNamespace(id="batch_1")

    def retrieve(self, batch_id):
        assert batch_id == "batch_1"
        return SimpleNamespace(id=batch_id, status=self.status,
                               output_file_id="file_output", error_file_id=None)


class FakeClient:
    def __init__(self):
        self.files = FakeFiles()
        self.batches = FakeBatches()

    def finish(self):
        lines = []
        for request in self.files.input_lines:
            payload = json.loads(request["body"]["input"])
            if request["custom_id"].startswith("change"):
                ids = [row["match_id"] for row in payload["alignments"]]
                action, collection = "classify", "analyses"
                results = [{"match_id": key, "llm": "Modified"} for key in ids]
            else:
                ids = [row["match_id"] for row in payload["rows"]]
                action, collection = "classify_materiality", "classifications"
                results = [{"match_id": key, "materiality": "Yes", "materiality_score": 0.8,
                            "confidence_score": 0.9, "reason": "The risk exposure expanded.",
                            "key_change": "Expanded risk exposure"} for key in ids]
            body = {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps({"action": action,
                                                               collection: results})}]}],
                    "usage": {"input_tokens": 100, "output_tokens": 20}}
            lines.append(json.dumps({"custom_id": request["custom_id"],
                                     "response": {"status_code": 200, "body": body}, "error": None}))
        self.files.output = "\n".join(reversed(lines))
        self.batches.status = "completed"


class AlignmentEvalTests(unittest.TestCase):
    def test_default_alignment_root(self):
        args = runner.parse_args(["--ticker", "amd", "--previous-year", "2023",
                                  "--current-year", "2024"])
        self.assertEqual(args.alignment_root, Path("data/alignments_for_eval_openai_batch"))

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
            document = {"company": "amd", "fiscal_year": year, "disclosures": [
                {"disclosure_id": key, "fiscal_year": year, "item": "1A",
                 "section": "Risks", "content": f"Original source text {key}"} for key in keys]}
            (folder / "disclosures.json").write_text(json.dumps(document), encoding="utf-8")
            (folder / "review_candidates.json").write_text(
                json.dumps({**document, "disclosures": []}), encoding="utf-8")
        self.client = FakeClient()

    def run_script(self, *extra):
        args = ["--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                "--alignment-root", str(self.root / "alignments"),
                "--disclosures-dir", str(self.disclosures), "--model", "gpt-6.1-sol", *extra]
        with redirect_stdout(StringIO()):
            return runner.main(args, client=self.client)

    def test_submit_resume_and_collect_both_stages(self):
        self.assertEqual(self.run_script("--submit-only"), 0)
        self.assertEqual(self.client.batches.submissions, 1)
        self.assertEqual(len(self.client.files.input_lines), 2)
        for request in self.client.files.input_lines:
            self.assertEqual(request["body"]["reasoning"], {"effort": "low"})
            payload = json.loads(request["body"]["input"])
            self.assertNotIn("materiality_analysis", request["body"]["input"])
            if request["custom_id"].startswith("change"):
                self.assertEqual(request["body"]["instructions"], runner.CHANGE_ANALYSIS_PROMPT)
                self.assertEqual(payload["change_analysis"], True)
                self.assertEqual(payload["item"], "1A")
                self.assertNotIn("llm", request["body"]["input"])
            else:
                self.assertEqual(request["body"]["instructions"], runner.MATERIALITY_PROMPT)
                self.assertNotIn("change_analysis", request["body"]["input"])
        self.assertEqual(self.run_script("--submit-only"), 0)
        self.assertEqual(self.client.batches.submissions, 1)
        self.client.finish()
        self.assertEqual(self.run_script(), 0)
        document = json.loads(self.alignment.read_text(encoding="utf-8"))
        matched, exact, removed = document["alignments"]
        self.assertEqual(matched["change_analysis"], {"status": "completed", "llm": "Modified"})
        self.assertEqual(set(matched["materiality_analysis"]), runner.MATERIALITY_FIELDS)
        self.assertNotIn("key_change", matched["materiality_analysis"])
        self.assertEqual(exact["change_analysis"]["llm"], "Unchanged")
        self.assertEqual(exact["materiality_analysis"]["materiality"], "No")
        self.assertEqual(removed["change_analysis"]["llm"], "Removed")
        ledger = json.loads((self.alignment.parent / "openai_eval" / "token_usage.json").read_text())
        self.assertEqual(ledger["input_tokens"], 200)
        self.assertEqual(ledger["output_tokens"], 40)
        self.assertEqual(self.run_script(), 0)
        self.assertEqual(self.client.batches.submissions, 1)

    def test_rejects_inconsistent_materiality(self):
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            runner.validate_results("materiality", {"action": "classify_materiality",
                "classifications": [{"match_id": "M1",
                "materiality": "No", "materiality_score": 0.8, "confidence_score": 0.9,
                "reason": "Substantive change.", "key_change": "Change"}]}, ["M1"])

    def test_change_requests_use_production_prompt_and_one_item_per_batch(self):
        document, records = runner.load_inputs(self.alignment, self.disclosures)
        second = dict(document["alignments"][0], match_id="M4",
                      previous_ids=["old_3"], current_ids=["new_2"])
        records["old_3"]["item"] = "7"
        records["new_2"]["item"] = "7"
        requests, _ = runner.make_requests([document["alignments"][0], second], records,
                                           ("change",), "gpt-6.1-sol", "low", 8, document)
        self.assertEqual(len(requests), 2)
        self.assertEqual([json.loads(request["body"]["input"])["item"] for request in requests],
                         ["1A", "7"])
        self.assertTrue(all(request["body"]["instructions"] == runner.CHANGE_ANALYSIS_PROMPT
                            for request in requests))

    def test_spend_guard_prevents_submission(self):
        work = self.alignment.parent / "openai_eval"
        work.mkdir()
        runner.atomic_json(work / "token_usage.json", {"input_tokens": 0, "output_tokens": 0,
                         "estimated_usd": 45.0, "batches": 0, "batch_ids": []})
        with self.assertRaisesRegex(ValueError, "spend threshold"):
            self.run_script()
        self.assertEqual(self.client.batches.submissions, 0)

    def test_citation_fallback_for_missing_review_candidate(self):
        source = self.disclosures / "amd" / "2023" / "disclosures.json"
        document = json.loads(source.read_text(encoding="utf-8"))
        document["disclosures"] = [row for row in document["disclosures"]
                                   if row["disclosure_id"] != "old_3"]
        source.write_text(json.dumps(document), encoding="utf-8")
        alignment = json.loads(self.alignment.read_text(encoding="utf-8"))
        alignment["alignments"][2]["evidence"] = [{"disclosure_id": "old_3", "sentences": [
            {"sentence_id": "S1", "text": "Cited source sentence."}]}]
        alignment["alignments"][2]["previous_disclosures"] = [
            {"disclosure_id": "old_3", "item": "1A", "section": "Risks"}]
        self.alignment.write_text(json.dumps(alignment), encoding="utf-8")
        _, records = runner.load_inputs(self.alignment, self.disclosures)
        self.assertEqual(records["old_3"]["content"], "Cited source sentence.")
        self.assertEqual(records["old_3"]["evidence_source"], "alignment_citations")

    def test_completed_batch_not_counted_twice(self):
        self.run_script("--submit-only")
        work = self.alignment.parent / "openai_eval"
        manifest = json.loads((work / "active_batch.json").read_text())
        self.client.finish()
        self.run_script()
        ledger = json.loads((work / "token_usage.json").read_text())
        runner.atomic_json(work / "active_batch.json", manifest)
        self.run_script()
        self.assertEqual(json.loads((work / "token_usage.json").read_text()), ledger)

    def test_cancelled_batch_collects_partial_output_with_blank_separator(self):
        self.run_script("--submit-only")
        self.client.finish()
        self.client.batches.status = "cancelled"
        success = self.client.files.output.splitlines()[0]
        missing_id = next(request["custom_id"] for request in self.client.files.input_lines
                          if request["custom_id"] != json.loads(success)["custom_id"])
        failure = json.dumps({"custom_id": missing_id, "response": None,
                              "error": {"code": "batch_cancelled"}})
        self.client.files.output = success + "\n\n" + failure + "\n"

        self.assertEqual(self.run_script(), 1)
        work = self.alignment.parent / "openai_eval"
        self.assertFalse((work / "active_batch.json").exists())
        errors = json.loads((work / "batch_0001_errors.json").read_text(encoding="utf-8"))
        self.assertEqual(len(errors), 1)
        ledger = json.loads((work / "token_usage.json").read_text(encoding="utf-8"))
        self.assertEqual((ledger["input_tokens"], ledger["output_tokens"]), (100, 20))

    def test_other_pair_usage_counts_toward_spend_guard(self):
        other = self.root / "alignments" / "bac" / "2023-2024" / "openai_eval"
        other.mkdir(parents=True)
        runner.atomic_json(other / "token_usage.json", {"estimated_usd": 45.0})
        with self.assertRaisesRegex(ValueError, "spend threshold"):
            self.run_script()
        self.assertEqual(self.client.batches.submissions, 0)

    def test_other_active_batch_blocks_new_submission(self):
        other = self.root / "alignments" / "bac" / "2023-2024" / "openai_eval"
        other.mkdir(parents=True)
        runner.atomic_json(other / "active_batch.json", {"batch_id": "batch_other"})
        with self.assertRaisesRegex(ValueError, "Another evaluation batch"):
            self.run_script()
        self.assertEqual(self.client.batches.submissions, 0)

    def test_dry_run_does_not_change_files_or_submit(self):
        original = self.alignment.read_bytes()
        self.assertEqual(self.run_script("--dry-run", "--max-new-rows", "1"), 0)
        self.assertEqual(self.alignment.read_bytes(), original)
        self.assertFalse((self.alignment.parent / "openai_eval").exists())
        self.assertEqual(self.client.batches.submissions, 0)

    def test_other_model_requires_explicit_prices(self):
        with self.assertRaisesRegex(ValueError, "supply both Batch token prices"):
            self.run_script("--model", "gpt-6-luna", "--dry-run")


if __name__ == "__main__":
    unittest.main()
