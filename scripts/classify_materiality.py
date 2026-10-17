#!/usr/bin/env python3
"""CLI wrapper for alignment-level materiality classification."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.agents.materiality import main


if __name__ == "__main__":
    raise SystemExit(main())
