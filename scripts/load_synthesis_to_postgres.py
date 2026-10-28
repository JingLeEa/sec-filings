#!/usr/bin/env python3
"""Load synthesis agent reports (*_synthesis_output.json) into PostgreSQL for the chatbot.

    python scripts/load_synthesis_to_postgres.py data/synthesis
    python scripts/load_synthesis_to_postgres.py data/synthesis/AMD_2023_2025_synthesis_output.json --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.synthesis_reports import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
