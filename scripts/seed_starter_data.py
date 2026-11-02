#!/usr/bin/env python3
"""Load the bundled starter reports into missing PostgreSQL scopes without model calls."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sec_disclosure.storage.starter_data import BUNDLE, StarterDataError, seed_database


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=BUNDLE)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args(argv)
    try:
        result = seed_database(bundle=args.bundle, env_file=args.env_file)
    except StarterDataError as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Starter data ready for {len(result['companies'])} companies. "
          f"Imported {sum(result['imported'].values())} scopes; preserved {result['skipped']} existing scopes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
