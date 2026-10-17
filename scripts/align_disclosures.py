#!/usr/bin/env python3
"""Align two saved disclosure years and store successful results in PostgreSQL."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.agents.disclosure_alignment import main


if __name__ == "__main__":
    raise SystemExit(main())
