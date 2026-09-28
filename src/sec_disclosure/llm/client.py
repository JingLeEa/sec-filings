"""Small text client for the SoCLaaS Chat Completions endpoint."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from .config import LLMConfig, load_config


class LLMError(RuntimeError):
    """A request failed, with a message safe to display without credentials."""


@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str
    finish_reason: str | None
    usage: dict[str, Any] | None


def request_completion(prompt: str, *, config: LLMConfig | None = None,
                       max_tokens: int = 256, timeout: float = 60.0,
                       system_prompt: str | None = None,
                       json_mode: bool = False) -> CompletionResult:
    """Return text AND reported usage, including truncated/empty completions.

    Callers performing batch work must save this result before parsing the text,
    so a failed parse never hides tokens already consumed. No automatic retries.
    """
    if not prompt.strip():
        raise ValueError("Prompt must not be empty.")
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive.")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive, finite number.")
    settings = config if config is not None else load_config()
    messages: list[ChatCompletionMessageParam] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    options: dict[str, Any] = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        with OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                    timeout=timeout, max_retries=0) as client:
            response = client.chat.completions.create(
                model=settings.model,
                messages=messages,
                max_tokens=max_tokens,
                **options,
            )
    except APITimeoutError:
        raise LLMError("SoCLaaS request timed out. Try again or increase --timeout.") from None
    except APIConnectionError:
        raise LLMError("Cannot connect to SoCLaaS. Check the endpoint and your network connection.") from None
    except APIStatusError as error:
        advice = {
            400: "Check the model and request parameters.",
            401: "Check SOCLAAS_API_KEY; it may be invalid or expired.",
            403: "Check your account's access to the service and selected model.",
            404: "Check SOCLAAS_BASE_URL and SOCLAAS_MODEL.",
            429: "Your rate limit or quota was reached; check the SoCLaaS portal.",
        }.get(error.status_code, "The service could not complete the request; try again later.")
        raise LLMError(f"SoCLaaS returned HTTP {error.status_code}. {advice}") from None
    except APIError:
        raise LLMError("SoCLaaS returned an unexpected API response.") from None
    choice = response.choices[0] if response.choices else None
    return CompletionResult(
        text=(choice.message.content or "") if choice else "",
        model=response.model,
        finish_reason=choice.finish_reason if choice else None,
        usage=response.usage.model_dump() if response.usage is not None else None,
    )


def complete(prompt: str, *, config: LLMConfig | None = None,
             max_tokens: int = 256, timeout: float = 60.0) -> str:
    """Send one bounded request and return nonempty, non-truncated text."""
    result = request_completion(prompt, config=config, max_tokens=max_tokens, timeout=timeout)
    if result.finish_reason is None:
        raise LLMError("SoCLaaS returned no completion choices.")
    if result.finish_reason == "length":
        raise LLMError("The reply reached its token limit. Increase --max-tokens.")
    text = result.text
    if not text or not text.strip():
        raise LLMError("SoCLaaS returned no assistant text. Check the model or increase --max-tokens.")
    return text.strip()
