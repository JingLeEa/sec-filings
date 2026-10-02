"""Load SoCLaaS settings without executing the env file or exposing its key."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str


def load_config(env_file: str | Path | None = None) -> LLMConfig:
    """Read the protected env file; exported environment variables take priority.

    The default path is independent of the current working directory. An explicit
    missing file is an error, but environment-only configuration is supported when
    the default file does not exist. Values are parsed literally, not expanded.
    """
    path = (Path(env_file).expanduser() if env_file is not None
            else Path.home() / ".config" / "soclaas" / "soclaas.env")
    if env_file is not None and not path.is_file():
        raise ValueError(f"Environment file not found: {path}")
    values = dotenv_values(path, interpolate=False) if path.is_file() else {}
    names = ("SOCLAAS_BASE_URL", "SOCLAAS_API_KEY", "SOCLAAS_MODEL")
    settings = {name: os.environ.get(name, values.get(name) or "").strip()
                for name in names}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise ValueError(
            f"Missing settings: {', '.join(missing)}. Set them in {path} "
            "or export them in your environment."
        )
    base_url = settings["SOCLAAS_BASE_URL"].rstrip("/")
    try:
        url = urlsplit(base_url)
        valid_url = (url.scheme == "https" and bool(url.hostname)
                     and not url.username and not url.password
                     and not url.query and not url.fragment)
        url.port  # Validate malformed port values without displaying the URL.
    except ValueError:
        valid_url = False
    if not valid_url or any(char.isspace() for char in base_url):
        raise ValueError("SOCLAAS_BASE_URL must be an HTTPS API base URL without credentials, query, or fragment.")
    if any(char.isspace() for char in settings["SOCLAAS_API_KEY"]):
        raise ValueError("SOCLAAS_API_KEY must not contain whitespace.")
    return LLMConfig(base_url=base_url, api_key=settings["SOCLAAS_API_KEY"],
                     model=settings["SOCLAAS_MODEL"])
