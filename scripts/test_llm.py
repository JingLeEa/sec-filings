#!/usr/bin/env python3
"""Check local LLM settings or send one small SoCLaaS request."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.llm.client import LLMError, complete
from sec_disclosure.llm.config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path,
                        help="Env file path (default: ~/.config/soclaas/soclaas.env). Exported settings take priority.")
    parser.add_argument("--check-config", action="store_true",
                        help="Validate local settings without making an API request.")
    parser.add_argument("--prompt", default="Reply with exactly: Connection successful.")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--timeout", type=float, default=60.0, help="Request timeout in seconds.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env_file)
        if args.check_config:
            print("Configuration valid (API key hidden; no request sent).")
            return 0
        reply = complete(args.prompt, config=config, max_tokens=args.max_tokens,
                         timeout=args.timeout)
    except (ValueError, OSError, LLMError) as error:
        print(f"LLM setup error: {error}", file=sys.stderr)
        return 1
    print("SoCLaaS connection successful.")
    print(reply.replace(config.api_key, "[REDACTED]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
