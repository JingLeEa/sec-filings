#!/usr/bin/env python3
"""Install the app and Google CLI, sign in, and verify the shared API settings."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sec_disclosure.app_setup import main


if __name__ == '__main__':
    raise SystemExit(main())
