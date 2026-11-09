#!/usr/bin/env python3
"""Read API settings from Google Secret Manager, then launch the UI or a script."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sec_disclosure.cloud_secrets import main


if __name__ == '__main__':
    raise SystemExit(main())
