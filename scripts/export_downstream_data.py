#!/usr/bin/env python3
"""Join normalized PostgreSQL tables into downstream disclosure/alignment JSON."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.exports import (  # noqa: E402
    export_reports, get_alignment_reports, get_disclosure_reports, main,
)


if __name__ == "__main__":
    raise SystemExit(main())
