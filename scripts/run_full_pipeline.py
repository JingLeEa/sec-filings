#!/usr/bin/env python3
"""Run the full SEC disclosure pipeline and persist every stage to PostgreSQL."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.pipelines.run_full_pipeline import main

if __name__ == "__main__":
    raise SystemExit(main())
