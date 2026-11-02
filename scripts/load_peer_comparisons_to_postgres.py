#!/usr/bin/env python3
"""Load peer-comparison agent reports (peer_comparison.json) into PostgreSQL for the chatbot.

    python scripts/load_peer_comparisons_to_postgres.py data/peer_comparison/nvda/2024
    python scripts/load_peer_comparisons_to_postgres.py data/peer_comparison/*/2024 --dry-run
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.storage.peer_comparisons import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
