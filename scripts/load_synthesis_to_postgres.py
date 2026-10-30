#!/usr/bin/env python3
"""Load synthesis agent report files (*_synthesis_output.json) into PostgreSQL for the chatbot.

The synthesis agent already saves every report it generates to synthesis_reports; use this for report files
produced elsewhere (a teammate's run, the samples in test_data/synthesis).

    python scripts/load_synthesis_to_postgres.py data/synthesis
    python scripts/load_synthesis_to_postgres.py test_data/synthesis --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.synthesis_reports import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
