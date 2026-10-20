#!/usr/bin/env python3
"""Compare a target company's saved disclosures with same-year peer filings."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.agents.peer_comparison import main


if __name__ == "__main__":
    raise SystemExit(main())
