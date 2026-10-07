#!/usr/bin/env python3
"""Extract disclosures for one or several years, optionally followed by alignment."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.llm.disclosures import main


if __name__ == "__main__":
    raise SystemExit(main())
