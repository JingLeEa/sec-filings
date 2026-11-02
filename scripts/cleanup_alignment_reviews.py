#!/usr/bin/env python3
"""Exclude unresolved one-sided reviews, optionally waiting for active pipelines."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sec_disclosure.storage.alignment_cleanup import cleanup_existing
from sec_disclosure.storage.artifacts import ArtifactStorageError, PostgresArtifactStore

STATUS_KEY = "manual_reviews/pipeline_refresh/exclusion_cleanup.json"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--poll-interval", type=float, default=15)
    args = parser.parse_args(argv)
    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    store = PostgresArtifactStore("human-reviews", args.env_file)
    removed = 0
    try:
        store.ensure_schema()
        with store.lock("alignment-review-cleanup/worker"):
            while True:
                pending = set()
                with store.connection() as conn, conn.cursor() as cur:
                    removed += cleanup_existing(cur, pending=pending)
                    status = {"status": "waiting" if pending else "complete", "excluded_alignments": removed,
                              "pending_companies": sorted(pending), "updated_at": datetime.now(timezone.utc).isoformat()}
                    cur.execute("""INSERT INTO pipeline_artifacts(namespace,path,kind,content)
                        VALUES (%s,%s,'file',%s) ON CONFLICT(namespace,path) DO UPDATE SET
                        content=EXCLUDED.content,modified_at=now()""",
                                (store.namespace, STATUS_KEY, json.dumps(status).encode()))
                if not pending or not args.wait:
                    print(json.dumps(status), flush=True)
                    return 0
                time.sleep(args.poll_interval)
    except ValueError as error:
        if "already running" in str(error):
            return 0
        raise
    except ArtifactStorageError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
