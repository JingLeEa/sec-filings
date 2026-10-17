#!/usr/bin/env python3
"""Load a completed alignment snapshot into PostgreSQL without model requests."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.alignments import (  # noqa: E402
    alignment_row, main, member_rows, store_alignments, validate_report,
)


if __name__ == "__main__":
    raise SystemExit(main())
