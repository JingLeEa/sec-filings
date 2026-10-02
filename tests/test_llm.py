import importlib.util
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from openai import OpenAI

from sec_disclosure.llm import client
from sec_disclosure.llm.config import LLMConfig, load_config


KEY = "test-key-not-a-real-secret"
ENV = ("SOCLAAS_BASE_URL=https://gateway.example/v1/\n"
       f"export SOCLAAS_API_KEY='{KEY}'\n"
       "SOCLAAS_MODEL=default\n")


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.file = self.home / ".config/soclaas/soclaas.env"
        self.file.parent.mkdir(parents=True)
        self.file.write_text(ENV)

    def test_default_file_loads_without_exporting_or_revealing_key(self):
        with patch("sec_disclosure.llm.config.Path.home", return_value=self.home):
            config = load_config()
        self.assertEqual(config.base_url, "https://gateway.example/v1")
        self.assertEqual(config.api_key, KEY)
        self.assertEqual(config.model, "default")
        self.assertNotIn(KEY, repr(config))
        self.assertNotIn("SOCLAAS_API_KEY", os.environ)

    def test_exported_values_override_file(self):
        with patch.dict(os.environ, {"SOCLAAS_MODEL": "chosen-model"}):
            self.assertEqual(load_config(self.file).model, "chosen-model")

    def test_environment_only_configuration(self):
        self.file.unlink()
        with patch("sec_disclosure.llm.config.Path.home", return_value=self.home), \
             patch.dict(os.environ, {"SOCLAAS_BASE_URL": "https://gateway.example/v1",
                                     "SOCLAAS_API_KEY": KEY, "SOCLAAS_MODEL": "default"}):
            self.assertEqual(load_config().api_key, KEY)

    def test_explicit_missing_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "Environment file not found"):
            load_config(self.home / "missing.env")

    def test_missing_settings_reports_names_only(self):
        self.file.write_text(f"SOCLAAS_API_KEY={KEY}\n")
        with self.assertRaises(ValueError) as error:
            load_config(self.file)
        self.assertIn("SOCLAAS_BASE_URL", str(error.exception))
        self.assertIn("SOCLAAS_MODEL", str(error.exception))
        self.assertNotIn(KEY, str(error.exception))

    def test_key_is_literal_and_not_interpolated(self):
        self.file.write_text(ENV.replace(KEY, "literal-${OTHER_SECRET}"))
        with patch.dict(os.environ, {"OTHER_SECRET": "expanded"}):
            self.assertEqual(load_config(self.file).api_key, "literal-${OTHER_SECRET}")

    def test_rejects_unsafe_or_invalid_endpoints_without_printing_them(self):
        for url in ("http://gateway.example/v1", f"https://{KEY}@gateway.example/v1",
                    f"https://gateway.example/v1?key={KEY}", "https://[invalid",
                    "https://gateway.example:bad/v1"):
            with self.subTest(url=url), patch.dict(os.environ, {"SOCLAAS_BASE_URL": url}):
                with self.assertRaises(ValueError) as error:
                    load_config(self.file)
                self.assertNotIn(KEY, str(error.exception))


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.config = LLMConfig("https://gateway.example/v1", KEY, "default")

    def request(self, handler):
        """Run the real SDK against a local mock transport, with dummy credentials."""
        sdk = OpenAI(api_key=KEY, base_url=self.config.base_url, max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(client, "OpenAI", return_value=sdk) as factory:
            result = client.complete("Hello", config=self.config, max_tokens=64, timeout=10)
        self.assertEqual(factory.call_args.kwargs["base_url"], self.config.base_url)
        self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
        self.assertEqual(factory.call_args.kwargs["timeout"], 10)
        self.assertTrue(sdk.is_closed())
        return result

    def payload(self, text="Connected.", finish_reason="stop"):
        return {"id": "test", "object": "chat.completion", "created": 0,
                "model": "default", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish_reason}]}

    def test_configured_endpoint_authentication_model_and_token_limit(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://gateway.example/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], f"Bearer {KEY}")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "default")
            self.assertEqual(body["messages"], [{"role": "user", "content": "Hello"}])
            self.assertEqual(body["max_tokens"], 64)
            return httpx.Response(200, json=self.payload())
        self.assertEqual(self.request(handler), "Connected.")

    def test_http_errors_hide_response_body_and_do_not_retry(self):
        for status in (400, 401, 403, 404, 429, 503):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, json={"error": {"message": KEY}})
            with self.subTest(status=status), self.assertRaises(client.LLMError) as error:
                self.request(handler)
            self.assertIn(str(status), str(error.exception))
            self.assertNotIn(KEY, str(error.exception))
            self.assertEqual(len(requests), 1)
            self.assertEqual(error.exception.retryable, status in (429, 503))

    def test_connection_and_timeout_errors_hide_underlying_details(self):
        for kind in (httpx.ConnectError, httpx.ReadTimeout):
            def handler(request):
                raise kind(KEY, request=request)
            with self.subTest(kind=kind), self.assertRaises(client.LLMError) as error:
                self.request(handler)
            self.assertNotIn(KEY, str(error.exception))
            self.assertTrue(error.exception.retryable)

    def test_insufficient_quota_is_permanent_even_when_http_429(self):
        with self.assertRaises(client.LLMError) as error:
            self.request(lambda request: httpx.Response(429, json={"error": {
                "message": KEY, "code": "insufficient_quota"}}))
        self.assertEqual(error.exception.status_code, 429)
        self.assertFalse(error.exception.retryable)
        self.assertNotIn(KEY, str(error.exception))

    def test_rate_limit_exposes_safe_status_and_retry_after_metadata(self):
        for header, expected in (("12.5", 12.5), ("bad-header", None), ("NaN", None), ("-1", None)):
            with self.subTest(header=header), self.assertRaises(client.LLMError) as error:
                self.request(lambda request: httpx.Response(429, headers={"Retry-After": header},
                                                           json={"error": {"message": KEY}}))
            self.assertEqual(error.exception.status_code, 429)
            self.assertEqual(error.exception.retry_after, expected)
            self.assertNotIn(KEY, str(error.exception))

    def test_empty_and_truncated_replies_are_errors(self):
        empty_choices = self.payload()
        empty_choices["choices"] = []
        for payload in (self.payload(None), self.payload("  "), empty_choices,
                        self.payload("Partial", "length")):
            with self.subTest(payload=payload), self.assertRaises(client.LLMError):
                self.request(lambda request: httpx.Response(200, json=payload))

    def test_invalid_inputs_never_open_a_client(self):
        with patch.object(client, "OpenAI") as factory:
            for arguments in ({"prompt": " "}, {"max_tokens": 0}, {"timeout": 0},
                              {"timeout": float("nan")}, {"timeout": float("inf")}):
                with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                    client.complete(**{"prompt": "Hello", "config": self.config, **arguments})
            factory.assert_not_called()


class CommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parents[1] / "scripts/test_llm.py"
        spec = importlib.util.spec_from_file_location("llm_command", script)
        cls.command = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.command)

    def test_config_check_does_not_send_a_request(self):
        output = io.StringIO()
        with patch.object(self.command, "load_config", return_value=LLMConfig(
                "https://gateway.example/v1", KEY, "default")), \
             patch.object(self.command, "complete") as complete, redirect_stdout(output):
            self.assertEqual(self.command.main(["--check-config"]), 0)
        complete.assert_not_called()
        self.assertIn("no request sent", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())

    def test_failure_returns_nonzero_without_traceback(self):
        output = io.StringIO()
        with patch.object(self.command, "load_config", side_effect=ValueError("Missing settings")), \
             redirect_stderr(output):
            self.assertEqual(self.command.main([]), 1)
        self.assertIn("Missing settings", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


class UsageReportingTests(unittest.TestCase):
    def test_truncated_json_keeps_provider_usage_and_reasoning_details(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertEqual(body["messages"][0], {"role": "system", "content": "Return JSON."})
            return httpx.Response(200, json={
                "id": "test", "object": "chat.completion", "created": 0, "model": "actual-model",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": '{"partial":'},
                             "finish_reason": "length"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150,
                          "completion_tokens_details": {"reasoning_tokens": 20}},
            })
        sdk = OpenAI(api_key=KEY, base_url="https://gateway.example/v1", max_retries=0,
                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        with patch.object(client, "OpenAI", return_value=sdk):
            result = client.request_completion("Input", config=LLMConfig(
                "https://gateway.example/v1", KEY, "default"), system_prompt="Return JSON.", json_mode=True)
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.model, "actual-model")
        self.assertEqual(result.usage["total_tokens"], 150)
        self.assertEqual(result.usage["completion_tokens_details"]["reasoning_tokens"], 20)
        self.assertEqual(result.text, '{"partial":')


if __name__ == "__main__":
    unittest.main()
