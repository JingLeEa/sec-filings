#!/usr/bin/env python3
"""Run the matching/verification workflow on two saved disclosure years."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.agents.disclosure_alignment import main


if __name__ == "__main__":
    raise SystemExit(main())
