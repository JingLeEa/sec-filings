"""SoCLaaS settings for the chatbot (the only LLM service it uses).

Each of SOCLAAS_BASE_URL, SOCLAAS_API_KEY and SOCLAAS_MODEL is taken from the first place that sets it:
  1. an exported environment variable (same rule as the pipeline, so a stale export wins - check this first
     when SoCLaaS answers HTTP 401)
  2. the repository .env
  3. ~/.config/soclaas/soclaas.env
SOCLAAS_CHATBOT_MODEL, if set, replaces SOCLAAS_MODEL for the chatbot only (e.g. qwen3.6:35b) while the
pipeline stages keep SOCLAAS_MODEL. Embeddings always use bge-m3 on the same endpoint.

Without complete settings the chatbot runs offline (templated answers from the same SQL). The key is never
printed: LLMConfig.api_key is excluded from repr and descriptions name only where the key came from.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from ..llm.config import LLMConfig

ROOT = Path(__file__).resolve().parents[3]
HOME_FILE = Path.home() / ".config" / "soclaas" / "soclaas.env"
NAMES = ("SOCLAAS_BASE_URL", "SOCLAAS_API_KEY", "SOCLAAS_MODEL", "SOCLAAS_CHATBOT_MODEL")


@dataclass(frozen=True)
class SoclaasSettings:
    config: LLMConfig | None
    description: str            # safe to show: no key, no URL
    key_source: str | None


def _valid_base_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        url.port
    except ValueError:
        return False
    return (url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
            and not url.query and not url.fragment and not any(c.isspace() for c in value))


def load_soclaas(env_file: Path | None = None, *, home_file: Path | None = None) -> SoclaasSettings:
    repo_env = Path(env_file).expanduser() if env_file is not None else ROOT / ".env"
    home = home_file if home_file is not None else Path.home() / ".config" / "soclaas" / "soclaas.env"
    layers = [("environment variable", {n: os.environ.get(n) for n in NAMES})]
    for label, path in ((f"{repo_env.name} in the repository" if env_file is None else str(repo_env), repo_env),
                        ("~/.config/soclaas/soclaas.env", home)):
        if path.is_file():
            layers.append((label, dotenv_values(path, interpolate=False)))

    values: dict[str, str] = {}
    sources: dict[str, str] = {}
    for name in NAMES:
        for label, layer in layers:
            value = (layer.get(name) or "").strip()
            if value:
                values[name], sources[name] = value, label
                break

    missing = [n for n in NAMES[:3] if not values.get(n)]
    if missing:
        return SoclaasSettings(None, f"offline ({', '.join(missing)} not set)", sources.get("SOCLAAS_API_KEY"))
    base_url = values["SOCLAAS_BASE_URL"].rstrip("/")
    if not _valid_base_url(base_url):
        return SoclaasSettings(None, "offline (SOCLAAS_BASE_URL must be an https URL without credentials)", None)
    if any(c.isspace() for c in values["SOCLAAS_API_KEY"]):
        return SoclaasSettings(None, "offline (SOCLAAS_API_KEY contains whitespace)", None)
    model = values.get("SOCLAAS_CHATBOT_MODEL") or values["SOCLAAS_MODEL"]
    config = LLMConfig(base_url=base_url, api_key=values["SOCLAAS_API_KEY"], model=model)
    return SoclaasSettings(config, f"SoCLaaS · model {model} · key from {sources['SOCLAAS_API_KEY']}",
                           sources["SOCLAAS_API_KEY"])


def load_chat_config(env_file: Path | None = None) -> tuple[LLMConfig | None, str]:
    """``(config or None, description)`` for chat completions."""
    settings = load_soclaas(env_file)
    return settings.config, settings.description


def load_embedding_config(env_file: Path | None = None) -> LLMConfig | None:
    """Same SoCLaaS endpoint and key; the embedding model (bge-m3) is chosen by the caller."""
    return load_soclaas(env_file).config
