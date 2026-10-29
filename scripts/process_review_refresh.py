#!/usr/bin/env python3
"""Process saved human reviews through the remaining pipeline stages."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sec_disclosure.pipelines.review_jobs import process_queue


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue-dir", type=Path, required=True)
    process_queue(parser.parse_args().queue_dir)
