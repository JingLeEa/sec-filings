#!/usr/bin/env python3
"""Load extracted disclosure schema 3 or 4 into PostgreSQL."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.disclosures import (  # noqa: E402
    disclosure_row, main, store_disclosures, validate_report,
)


if __name__ == "__main__":
    raise SystemExit(main())
