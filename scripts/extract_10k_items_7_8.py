#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.table_extraction.extract_10k_items_7_8 import main


if __name__ == "__main__":
    raise SystemExit(main())
