"""Batched embeddings client for the SoCLaaS embeddings endpoint.

Mirrors llm/client.py: same config loading, same LLMError, same status-code
advice, no automatic retries. Two things chat does not need are added here.

Batching, because the endpoint accepts a list and one request per sentence
would be thousands of round trips.

An on-disk cache, because retrieval is tuned by re-running it. Embedding the
six filings is one cheap pass (about 22k sentences, well under a million
tokens), but paying and waiting for it on every threshold change is not. The
cache key is the model plus the exact text, so changing model or text misses
correctly and nothing stale is ever served.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI

from .client import LLMError
from .config import LLMConfig, load_config

DEFAULT_EMBED_MODEL = "bge-m3"
DEFAULT_BATCH_SIZE = 64
DEFAULT_CACHE_DIR = Path("data/embeddings")


def cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(f"{model}\u0000{text}".encode("utf-8")).hexdigest()
    return digest


class EmbeddingCache:
    """Flat on-disk cache of one JSON file per (model, text) digest."""

    def __init__(self, directory: Path | str | None = DEFAULT_CACHE_DIR):
        self.directory = Path(directory) if directory is not None else None
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> list[float] | None:
        if self.directory is None:
            return None
        path = self.directory / f"{key}.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None  # A corrupt entry is a miss, never an error.

    def put(self, key: str, vector: list[float]) -> None:
        if self.directory is None:
            return
        path = self.directory / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(vector), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass  # A cache that cannot be written must not fail the request.


def _request_batch(client: OpenAI, model: str, batch: list[str]) -> list[list[float]]:
    try:
        response = client.embeddings.create(model=model, input=batch)
    except APITimeoutError:
        raise LLMError("SoCLaaS embeddings request timed out.", retryable=True) from None
    except APIConnectionError:
        raise LLMError("Cannot connect to SoCLaaS. Check the endpoint and your network connection.",
                       retryable=True) from None
    except APIStatusError as error:
        advice = {
            400: "Check the model and request parameters.",
            401: "Check SOCLAAS_API_KEY; it may be invalid or expired.",
            403: "Check your account's access to the service and the embedding model.",
            404: "Check SOCLAAS_BASE_URL and the embedding model name.",
            413: "The batch was too large; reduce batch_size.",
            429: "Your rate limit or quota was reached; check the SoCLaaS portal.",
        }.get(error.status_code, "The service could not complete the request; try again later.")
        retry_after = None
        try:
            value = float(error.response.headers.get("retry-after", ""))
            if math.isfinite(value) and value >= 0:
                retry_after = value
        except (TypeError, ValueError):
            pass
        raise LLMError(f"SoCLaaS returned HTTP {error.status_code}. {advice}",
                       status_code=error.status_code, retry_after=retry_after,
                       retryable=error.status_code in (408, 409, 429) or error.status_code >= 500) from None
    except APIError:
        raise LLMError("SoCLaaS returned an unexpected API response.", retryable=True) from None

    ordered = sorted(response.data, key=lambda item: item.index)
    if len(ordered) != len(batch):
        raise LLMError(f"Embeddings response returned {len(ordered)} vectors for {len(batch)} inputs.")
    return [list(item.embedding) for item in ordered]


def embed_texts(
    texts: list[str],
    *,
    config: LLMConfig | None = None,
    model: str = DEFAULT_EMBED_MODEL,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cache: EmbeddingCache | None = None,
    timeout: float = 60.0,
    progress: bool = False,
) -> list[list[float]]:
    """Embed texts in input order.

    Empty and whitespace-only texts return a zero vector without a request, so a
    record with no summary scores zero on that field rather than failing - the
    same behaviour the TF-IDF index already has for an empty document.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive.")
    cache = cache if cache is not None else EmbeddingCache()

    results: list[list[float] | None] = [None] * len(texts)
    wanted: dict[str, list[int]] = {}

    for position, text in enumerate(texts):
        if not text or not text.strip():
            results[position] = []  # Filled with zeros once the dimension is known.
            continue
        hit = cache.get(cache_key(model, text))
        if hit is not None:
            results[position] = hit
        else:
            wanted.setdefault(text, []).append(position)

    pending = list(wanted)
    if pending:
        settings = config if config is not None else load_config()
        with OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                    timeout=timeout, max_retries=0) as client:
            for start in range(0, len(pending), batch_size):
                batch = pending[start:start + batch_size]
                vectors = _request_batch(client, model, batch)
                for text, vector in zip(batch, vectors):
                    cache.put(cache_key(model, text), vector)
                    for position in wanted[text]:
                        results[position] = vector
                if progress:
                    print(f"  embedded {min(start + batch_size, len(pending))}/{len(pending)} unique texts")

    dimension = next((len(v) for v in results if v), 0)
    return [list(v) if v else [0.0] * dimension for v in results]


def make_embedder(*, config: LLMConfig | None = None, model: str = DEFAULT_EMBED_MODEL,
                  batch_size: int = DEFAULT_BATCH_SIZE,
                  cache_dir: Path | str | None = DEFAULT_CACHE_DIR,
                  progress: bool = False):
    """Return a callable the index can hold, so the backend stays swappable."""
    cache = EmbeddingCache(cache_dir)

    def embedder(texts: list[str]) -> list[list[float]]:
        return embed_texts(texts, config=config, model=model, batch_size=batch_size,
                           cache=cache, progress=progress)

    return embedder
