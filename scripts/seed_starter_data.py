#!/usr/bin/env python3
"""Load bundled starter data; optionally refresh untouched older starter records."""
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
    parser.add_argument("--refresh", action="store_true",
                        help="Also update older starter records, preserving human reviews, own pipeline results and edits")
    args = parser.parse_args(argv)
    try:
        result = seed_database(bundle=args.bundle, env_file=args.env_file,
                               **({"refresh": True} if args.refresh else {}))
    except StarterDataError as error:
        print(str(error), file=sys.stderr)
        return 1
    updated = sum(result.get("refreshed", {}).values())
    print(f"Starter data ready for {len(result['companies'])} companies. "
          f"Imported {sum(result['imported'].values())} scopes; preserved {result['skipped'] - updated} existing scopes. "
          f"Excluded {result.get('excluded_alignments', 0)} unresolved one-sided alignments.")
    if args.refresh:
        print(f"Refreshed {updated} existing starter scopes; "
              f"{result['unchanged']} unchanged; {result['protected_scopes']} protected.")
        for company, reason in result['protected_companies'].items():
            print(f"  Preserved {company.upper()}: {reason}.")
        print("Click Reload data in the dashboard, or restart Streamlit, to read the updated reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
