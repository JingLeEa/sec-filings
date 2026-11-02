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
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.postgres:
        from sec_disclosure.storage.human_reviews import workspace
        from sec_disclosure.storage.artifacts import ArtifactPath
        prefix = str(args.queue_dir).split("/manual_reviews/", 1)[0]
        with workspace(args.env_file, prefix=prefix):
            queue = ArtifactPath(args.queue_dir)
            process_queue(queue, wait_for_pipeline=True, capture_output=True)
    else:
        process_queue(args.queue_dir, wait_for_pipeline=True)
