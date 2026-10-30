"""Synthesis requests obey the stage's shared limiter."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sec_disclosure.agents import synthesis
from sec_disclosure.llm.client import LLMError
from sec_disclosure.llm.config import LLMConfig
from sec_disclosure.llm.request_pacing import RequestPacer


class PipelineRequestLimitTests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig("https://example.test/v1", "fake-key", "test-model")

    def test_synthesis_requests_wait_for_slots_and_cached_replay_uses_none(self):
        pacer = RequestPacer(0, 0, 1)
        ready, started = threading.Event(), threading.Event()
        admit = pacer.request

        @contextmanager
        def admission():
            ready.set()
            with admit():
                yield

        def request(*args, **kwargs):
            started.set()
            return SimpleNamespace(text='{"answer": 1}', model="test-model", finish_reason="stop",
                                   usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})

        with TemporaryDirectory() as temp, \
             patch.object(synthesis, "request_completion", side_effect=request) as send, \
             patch.object(pacer, "request", side_effect=admission), \
             ThreadPoolExecutor(max_workers=1) as pool:
            root = Path(temp)
            calls = synthesis.SynthesisCalls(self.config, root / "cache", root / "usage.json",
                                             10, 100, request_pacer=pacer)
            # Occupy the admission slot before the finding worker starts.
            pacer.slots.acquire()
            future = pool.submit(calls.complete, "findings-1", "prompt", "system")
            try:
                self.assertTrue(ready.wait(2))
                self.assertFalse(started.wait(0.05))
            finally:
                pacer.slots.release()
            self.assertEqual(future.result(timeout=2), '{"answer": 1}')
            self.assertTrue(ready.is_set())
            self.assertEqual(calls.complete("findings-1", "prompt", "system"), '{"answer": 1}')
            send.assert_called_once()

    def test_transient_429_updates_synthesis_shared_cooldown_and_preserves_usage_failure(self):
        pacer = RequestPacer(0, 0, 1)
        failure = LLMError("Rate limit", status_code=429, retry_after=12, retryable=True)
        with TemporaryDirectory() as temp, \
             patch.object(synthesis, "request_completion", side_effect=failure), \
             patch.object(pacer, "rate_limited", wraps=pacer.rate_limited) as cooldown:
            root = Path(temp)
            calls = synthesis.SynthesisCalls(self.config, root / "cache", root / "usage.json",
                                             10, 100, request_pacer=pacer)
            with self.assertRaises(LLMError):
                calls.complete("findings-1", "prompt", "system")
            cooldown.assert_called_once_with(12)
            usage = json.loads((root / "usage.json").read_text())
            self.assertEqual(usage["requests_with_unknown_usage"], 1)
            self.assertFalse((root / "cache").exists())


if __name__ == "__main__":
    unittest.main()
