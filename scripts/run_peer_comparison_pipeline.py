#!/usr/bin/env python3
"""Compare existing PostgreSQL companies and save the peer report."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.pipelines.run_peer_comparison_pipeline import main


if __name__ == "__main__":
    raise SystemExit(main())
