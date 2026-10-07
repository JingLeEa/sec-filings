import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sec_disclosure.agents import materiality as module
from sec_disclosure.agents import materiality_runtime
from sec_disclosure.llm.client import CompletionResult
from sec_disclosure.llm.config import LLMConfig
from sec_disclosure.llm.disclosures import write_json


class MaterialityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "alignments/amd/2023-2024"
        write_json(self.output / "alignments.json", self.document())
        write_json(self.output / "needs_review.json", {"sentinel": "must remain untouched"})

    def row(self, number, *, status="ai_verified", relationship="one_to_one"):
        before = [] if relationship == "current_only" else [f"amd_2023_1A_D{number:03d}"]
        after = [] if relationship == "previous_only" else [f"amd_2024_1A_D{number:03d}"]
        evidence = []
        for disclosure_id in before + after:
            evidence.append({
                "disclosure_id": disclosure_id,
                "sentences": [{
                    "sentence_id": disclosure_id.replace("_D", "_P") + "_S001",
                    "paragraph_id": disclosure_id.replace("_D", "_P"),
                    "source_url": "https://example.test/filing",
                    "text": f"Evidence for {disclosure_id}.",
                }],
            })
        row = {
            "match_id": f"amd_2023_2024_M{number:04d}",
            "previous_ids": before,
            "current_ids": after,
            "relationship": relationship,
            "explanation": "The aligned evidence describes how the disclosure changed.",
            "evidence": evidence,
            "status": status,
            "review_reasons": [],
            "verifier_review_reason": "",
            "match_method": "exact_text" if status == "auto_matched" else "llm",
            "previous_disclosures": [
                {"disclosure_id": key, "summary": f"Previous summary {number}", "taxonomy": "Operations & Capacity"}
                for key in before
            ],
            "current_disclosures": [
                {"disclosure_id": key, "summary": f"Current summary {number}", "taxonomy": "Operations & Capacity"}
                for key in after
            ],
            "change_analysis": {
                "status": "not_started", "lexical": None, "semantic": None,
                "llm": None, "final_taxonomy": None,
            },
        }
        if status == "unmatched":
            row["unmatched_type"] = "introduced_disclosure" if relationship == "current_only" else "removed_disclosure"
        return row

    def document(self, rows=None):
        rows = rows or [
            self.row(1, status="auto_matched"),
            self.row(2),
            self.row(3, status="unmatched", relationship="current_only"),
        ]
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "schema_version": "8",
            "company": "amd",
            "comparison_scope": "same_item",
            "previous_year": "2023",
            "current_year": "2024",
            "run_complete": True,
            "error": None,
            "included_statuses": ["ai_verified", "auto_matched", "unmatched"],
            "alignment_count": len(rows),
            "counts": counts,
            "overall_counts": counts,
            "alignments": rows,
        }

    def response(self, prompt, **kwargs):
        payload = json.loads(prompt)
        classifications = []
        for row in payload["rows"]:
            label = "No" if row["relationship"] == "current_only" else "Yes"
            classifications.append({
                "match_id": row["match_id"],
                "materiality": label,
                "materiality_score": 0.2 if label == "No" else 0.8,
                "confidence_score": 0.86,
                "reason": "The evidence supports a concrete materiality decision.",
                "key_change": "Changed operating exposure",
            })
        return CompletionResult(
            json.dumps({"action": "classify_materiality", "classifications": classifications}),
            "test-model", "stop",
            {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
        )

    def run_cli(self, *args, response=None):
        with patch.object(module, "load_config", return_value=LLMConfig(
                "https://example.test/v1", "fake-secret", "test-model")), \
             patch.object(materiality_runtime, "request_completion", side_effect=response or self.response) as api, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = module.main([
                "--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                "--data-dir", str(self.root), *args,
            ])
        return status, api.call_count

    def saved(self):
        return json.loads((self.output / "alignments.json").read_text())

    def test_prompt_contains_substantive_decision_boundaries(self):
        prompt = " ".join(module.MATERIALITY_PROMPT.split())
        for phrase in (
            "business activity, product, service, market, or strategy",
            "routine updates that do not substantively change the disclosure",
            "Judge substance, magnitude, and company-specific consequences",
            "Do not classify a change as material solely",
        ):
            self.assertIn(phrase, prompt)
        verifier_prompt = " ".join(module.MATERIALITY_VERIFICATION_PROMPT.split())
        self.assertIn("Review every supplied low-confidence materiality decision", verifier_prompt)
        self.assertIn("It is valid to retain Uncertain", verifier_prompt)

    def test_classifies_final_rows_and_leaves_review_file_untouched(self):
        document = self.saved()
        document["alignments"][1]["change_analysis"] = {
            "status": "completed", "lexical": {"score": 0.5}, "semantic": None,
            "llm": None, "final_taxonomy": ["expanded"],
        }
        write_json(self.output / "alignments.json", document)
        review_bytes = (self.output / "needs_review.json").read_bytes()

        seen = []

        def response(prompt, **kwargs):
            seen.append(json.loads(prompt))
            return self.response(prompt, **kwargs)

        self.assertEqual(self.run_cli(response=response), (0, 1))
        saved = self.saved()
        self.assertEqual((self.output / "needs_review.json").read_bytes(), review_bytes)
        self.assertEqual([row["status"] for row in saved["alignments"]],
                         ["auto_matched", "ai_verified", "unmatched"])
        self.assertEqual(saved["alignments"][0][module.MATERIALITY_FIELD], module.EXACT_TEXT_ANALYSIS)
        self.assertEqual(saved["alignments"][1][module.MATERIALITY_FIELD]["materiality"], "Yes")
        self.assertEqual(saved["alignments"][2][module.MATERIALITY_FIELD]["materiality"], "No")
        self.assertIn("change_analysis_reference", seen[0]["rows"][0])
        self.assertNotIn("change_analysis_reference", seen[0]["rows"][1])
        self.assertEqual(seen[0]["rows"][1]["unmatched_type"], "introduced_disclosure")
        reported_tokens = json.loads(
            (self.output / "materiality/summary.json").read_text()
        )["reported_tokens"]
        self.assertEqual(self.run_cli(), (0, 0))
        self.assertEqual(
            json.loads((self.output / "materiality/summary.json").read_text())["reported_tokens"],
            reported_tokens,
        )

    def test_single_workflow_verifies_low_confidence_rows_and_resumes(self):
        rows = [self.row(number) for number in range(1, 4)]
        write_json(self.output / "alignments.json", self.document(rows))

        def response(prompt, **kwargs):
            payload = json.loads(prompt)
            verifier = "MATERIALITY VERIFIER" in kwargs["system_prompt"]
            classifications = []
            for row in payload["rows"]:
                number = int(row["match_id"][-4:])
                if verifier and number == 1:
                    analysis = {
                        "materiality": "No", "materiality_score": 0.2,
                        "confidence_score": 0.9, "reason": "The verifier resolved the change.",
                        "key_change": "Non-substantive wording change",
                    }
                elif number in {1, 2}:
                    analysis = {
                        "materiality": "Uncertain", "materiality_score": 0.55,
                        "confidence_score": 0.4, "reason": "The evidence remains incomplete.",
                        "key_change": "Unclear operating change",
                    }
                else:
                    analysis = {
                        "materiality": "Yes", "materiality_score": 0.8,
                        "confidence_score": 0.9, "reason": "The evidence supports materiality.",
                        "key_change": "Expanded operating exposure",
                    }
                classifications.append({"match_id": row["match_id"], **analysis})
            action = "verify_materiality" if verifier else "classify_materiality"
            return CompletionResult(
                json.dumps({"action": action, "classifications": classifications}),
                "test-model", "stop",
                {"prompt_tokens": 100, "completion_tokens": 25, "total_tokens": 125},
            )

        self.assertEqual(self.run_cli("--max-new-requests", "1", response=response), (2, 1))
        paused = json.loads((self.output / "materiality/summary.json").read_text())
        self.assertTrue(paused["classification_complete"])
        self.assertFalse(paused["complete"])
        self.assertEqual(paused["verification"]["pending_rows"], 2)
        self.assertEqual(self.run_cli(response=response), (0, 1))
        saved = self.saved()["alignments"]
        self.assertEqual(saved[0][module.MATERIALITY_FIELD]["materiality"], "No")
        self.assertEqual(saved[0][module.MATERIALITY_FIELD]["confidence_score"], 0.9)
        self.assertEqual(saved[1][module.MATERIALITY_FIELD]["materiality"], "Uncertain")
        ledger = json.loads((self.output / "materiality/verifications.json").read_text())
        self.assertEqual(len(ledger["records"]), 2)
        self.assertTrue(ledger["records"][0]["changed"])
        self.assertFalse(ledger["records"][1]["changed"])
        summary = json.loads((self.output / "materiality/summary.json").read_text())
        self.assertTrue(summary["complete"])
        self.assertEqual(summary["verification"]["verified_rows"], 2)
        self.assertEqual(summary["verification"]["low_confidence_rows"], 1)
        self.assertEqual(summary["verification"]["pending_rows"], 0)
        self.assertEqual(self.run_cli(response=response), (0, 0))

    def test_pause_and_resume_writes_only_completed_batches(self):
        rows = [self.row(number) for number in range(1, 4)]
        write_json(self.output / "alignments.json", self.document(rows))
        self.assertEqual(self.run_cli("--batch-size", "1", "--max-new-requests", "1"), (2, 1))
        self.assertEqual(sum(module.MATERIALITY_FIELD in row for row in self.saved()["alignments"]), 1)
        self.assertEqual(self.run_cli("--batch-size", "1"), (0, 2))
        self.assertTrue(all(module.MATERIALITY_FIELD in row for row in self.saved()["alignments"]))

    def test_reclassify_starts_fresh_run_and_resumes_without_flag(self):
        rows = [self.row(number) for number in range(1, 4)]
        write_json(self.output / "alignments.json", self.document(rows))
        self.assertEqual(self.run_cli("--batch-size", "1"), (0, 3))

        self.assertEqual(self.run_cli(
            "--reclassify", "--batch-size", "1", "--max-new-requests", "1"
        ), (2, 1))
        self.assertEqual(
            sum(module.MATERIALITY_FIELD in row for row in self.saved()["alignments"]), 1
        )
        manifest = json.loads((self.output / "materiality/manifest.json").read_text())
        self.assertIsInstance(manifest["classification_run_id"], str)
        run_state = json.loads((self.output / "materiality/run_state.json").read_text())
        self.assertEqual(run_state["classification_run_id"], manifest["classification_run_id"])

        self.assertEqual(self.run_cli("--batch-size", "1"), (0, 2))
        self.assertTrue(all(
            module.MATERIALITY_FIELD in row for row in self.saved()["alignments"]
        ))
        summary = json.loads((self.output / "materiality/summary.json").read_text())
        timing = summary["timing"]
        self.assertIsInstance(timing["started_at"], str)
        self.assertIsInstance(timing["completed_at"], str)
        self.assertGreaterEqual(timing["wall_clock_seconds"], 0)
        self.assertGreaterEqual(timing["cumulative_api_seconds"], 0)
        self.assertEqual(timing["api_requests_timed"], 3)
        self.assertEqual(timing["api_requests_total"], 3)
        request_records = [
            json.loads(path.read_text())
            for path in (self.output / "materiality/requests").glob("*.json")
        ]
        active_records = [
            record for record in request_records
            if record.get("classification_run_id") == manifest["classification_run_id"]
        ]
        self.assertTrue(all("completed_at" in record for record in active_records))
        self.assertTrue(all("elapsed_seconds" in record for record in active_records))

    def test_exact_only_requires_no_configuration_or_api(self):
        write_json(self.output / "alignments.json", self.document([
            self.row(1, status="auto_matched"),
        ]))
        with patch.object(module, "load_config", side_effect=AssertionError("config should not load")), \
             patch.object(materiality_runtime, "request_completion", side_effect=AssertionError("API should not run")), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            status = module.main([
                "--ticker", "amd", "--previous-year", "2023", "--current-year", "2024",
                "--data-dir", str(self.root),
            ])
        self.assertEqual(status, 0)
        self.assertEqual(self.saved()["alignments"][0][module.MATERIALITY_FIELD], module.EXACT_TEXT_ANALYSIS)

    def test_validator_derives_label_from_strength_and_confidence(self):
        match_id = "amd_2023_2024_M0001"
        action = {"action": "classify_materiality", "classifications": [{
            "match_id": match_id,
            "materiality": "Uncertain",
            "materiality_score": 0.5,
            "confidence_score": 0.4,
            "reason": "The supplied evidence is incomplete.",
            "key_change": "Unclear disclosure change",
        }]}
        self.assertEqual(module.validate_action(action, [match_id])[0]["materiality"], "Uncertain")
        action["classifications"][0]["confidence_score"] = 0.8
        with self.assertRaisesRegex(ValueError, "must be Yes"):
            module.validate_action(action, [match_id])

    def test_migrates_legacy_confidence_scores_without_api_calls(self):
        rows = [self.row(1), self.row(2), self.row(3)]
        rows[0][module.MATERIALITY_FIELD] = {
            "materiality": "Yes", "materiality_score": 0.86,
            "reason": "Meaningful expansion.", "key_change": "Expanded exposure",
        }
        rows[1][module.MATERIALITY_FIELD] = {
            "materiality": "No", "materiality_score": 0.9,
            "reason": "Only wording changed.", "key_change": "Wording update",
        }
        rows[2][module.MATERIALITY_FIELD] = {
            "materiality": "Uncertain", "materiality_score": None,
            "reason": "Evidence was incomplete.", "key_change": "Unclear change",
        }
        write_json(self.output / "alignments.json", self.document(rows))

        self.assertEqual(self.run_cli("--skip-verification"), (0, 0))
        first, second, third = [row[module.MATERIALITY_FIELD] for row in self.saved()["alignments"]]
        self.assertEqual((first["materiality_score"], first["confidence_score"]), (0.86, 0.86))
        self.assertEqual(second["materiality_score"], 0.1)
        self.assertEqual(second["confidence_score"], 0.9)
        self.assertEqual(
            (third["materiality"], third["materiality_score"], third["confidence_score"]),
            ("Uncertain", 0.5, 0.0),
        )

    def test_cli_wrapper_imports_src_package_without_editable_install(self):
        repository = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(repository / "scripts/classify_materiality.py"), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Classify materiality", result.stdout)


if __name__ == "__main__":
    unittest.main()
