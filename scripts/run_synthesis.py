#!/usr/bin/env python3
"""Run the packaged disclosure and financial synthesis agent."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.agents.synthesis import main


if __name__ == "__main__":
    raise SystemExit(main())
