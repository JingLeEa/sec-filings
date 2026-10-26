"""Test synthesis orchestration without running upstream pipeline stages."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.pipelines import run_full_pipeline as pipeline


class TestSynthesisIntegration(unittest.TestCase):

    def make_args(self, until="materiality"):
        return SimpleNamespace(
            ticker="amd",
            years=[2023, 2024, 2025],
            until=until,
            from_stage="preprocessing",
            resume=True,
            workers=1,
            batch_workers=1,
            max_concurrent_requests=1,
            alignment_pair_workers=1,
            alignment_workers=1,
            alignment_max_concurrent_requests=1,
            materiality_pair_workers=1,
            materiality_concurrency=1,
            materiality_max_concurrent_requests=1,
            data_dir=Path("data"),
            env_file=None,
        )

    def run_case(self, until, synthesis_exit_code):
        args = self.make_args(until)

        with tempfile.TemporaryDirectory() as temp_dir:
            log_dir = Path(temp_dir)

            with patch.object(
                pipeline.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=synthesis_exit_code),
            ) as mock_run, patch.object(
                pipeline,
                "execute_stage",
                side_effect=AssertionError("Upstream stage was executed!"),
            ):
                code = pipeline.run_pipeline(
                    args=args,
                    stages=[],
                    all_stages=[],
                    log_dir=log_dir,
                    previous={},
                    entries={},
                    pending=[],
                    skipped=[],
                )

            status = json.loads(
                (log_dir / "status.json").read_text(encoding="utf-8")
            )

            return code, status, mock_run

    def test_success(self):
        code, status, mock_run = self.run_case("materiality", 0)

        self.assertEqual(code, 0)
        self.assertTrue(status["complete"])
        self.assertTrue(status["full_pipeline_complete"])
        self.assertEqual(status["synthesis_status"], "complete")
        mock_run.assert_called_once()

        command = mock_run.call_args.args[0]
        self.assertEqual(command[2:], [
            "--company", "AMD",
            "--years", "2023", "2024", "2025",
            "--data-dir", "data",
        ])

    def test_failure(self):
        code, status, mock_run = self.run_case("materiality", 1)

        self.assertNotEqual(code, 0)
        self.assertFalse(status["complete"])
        self.assertFalse(status["full_pipeline_complete"])
        self.assertEqual(status["synthesis_status"], "failed")
        mock_run.assert_called_once()

    def test_partial_pipeline_skips_synthesis(self):
        code, status, mock_run = self.run_case("alignment", 0)

        self.assertEqual(code, 0)
        self.assertTrue(status["complete"])
        self.assertFalse(status["full_pipeline_complete"])
        self.assertNotIn("synthesis_status", status)
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
