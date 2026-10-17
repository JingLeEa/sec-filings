#!/usr/bin/env python3
"""Populate lexical and semantic fields after LLM change classification."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.analysis.aligned_changes import main

if __name__ == "__main__":
    raise SystemExit(main())
