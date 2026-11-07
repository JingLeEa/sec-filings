"""Where the UI looks for data. Everything can be overridden with environment variables.

SEC_UI_DATA_SOURCE      auto (default) | files | postgres
SEC_UI_ALIGNMENTS_DIR   folder with <company>/<previous>-<current>/alignments.json
SEC_UI_DISCLOSURES_DIR  folder with <company>/<year>/disclosures.json
SEC_UI_PEER_DIR         folder with <TARGET>/<YEAR>/peer_comparison.json
SEC_UI_SYNTHESIS_DIRS   folders (os.pathsep separated) holding synthesis output JSON files
SEC_UI_MANUAL_REVIEWS_DIR legacy import folder; write destination only in files mode
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

COMPANY_NAMES = {
    "amd": ("AMD", "Semiconductors"),
    "avgo": ("Broadcom", "Semiconductors"),
    "intc": ("Intel", "Semiconductors"),
    "mu": ("Micron", "Semiconductors"),
    "nvda": ("NVIDIA", "Semiconductors"),
    "qcom": ("Qualcomm", "Semiconductors"),
    "bac": ("Bank of America", "Banking"),
    "c": ("Citigroup", "Banking"),
    "gs": ("Goldman Sachs", "Banking"),
    "jpm": ("JPMorgan Chase", "Banking"),
    "ms": ("Morgan Stanley", "Banking"),
    "txn": ("Texas Instruments", "Semiconductors"),
    "adi": ("Analog Devices", "Semiconductors"),
    "mchp": ("Microchip Technology", "Semiconductors"),
    "on": ("onsemi", "Semiconductors"),
    "nxpi": ("NXP Semiconductors", "Semiconductors"),
    "mpwr": ("Monolithic Power Systems", "Semiconductors"),
    "mrvl": ("Marvell Technology", "Semiconductors"),
    "swks": ("Skyworks Solutions", "Semiconductors"),
    "crus": ("Cirrus Logic", "Semiconductors"),
    "wfc": ("Wells Fargo", "Banking"),
    "usb": ("U.S. Bancorp", "Banking"),
    "pnc": ("PNC Financial Services", "Banking"),
    "tfc": ("Truist Financial", "Banking"),
    "cof": ("Capital One", "Banking"),
    "fitb": ("Fifth Third Bancorp", "Banking"),
    "rf": ("Regions Financial", "Banking"),
    "hban": ("Huntington Bancshares", "Banking"),
    "cfg": ("Citizens Financial Group", "Banking"),
    "unh": ("UnitedHealth", "Health insurance"),
}

CONTENT_CATEGORIES = [
    "Strategy & Business Model",
    "Operations & Capacity",
    "Technology & AI",
    "Cybersecurity & Data",
    "Supply Chain & Third Parties",
    "Regulation, Legal & Compliance",
    "Financial & Capital Resources",
    "Human Capital & Organization",
    "Other / Unclassified",
]

CHANGE_LABELS = ["New", "Removed", "Expanded", "Reduced", "Modified", "Reworded", "Unchanged"]

ITEM_TITLES = {
    "1": "Item 1 Business",
    "1A": "Item 1A Risk Factors",
    "7": "Item 7 MD&A",
    "8": "Item 8 Financials",
    "15": "Item 15 Exhibits",
}


def company_label(slug: str) -> str:
    name = COMPANY_NAMES.get(slug.lower(), (slug.upper(), ""))[0]
    return name if name == slug.upper() else f"{name} ({slug.upper()})"


def _first_dir(candidates):
    for path in candidates:
        if path and Path(path).is_dir():
            return Path(path)
    return None


def _env_path(name):
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def data_source_mode() -> str:
    mode = os.environ.get("SEC_UI_DATA_SOURCE", "auto").strip().lower()
    return mode if mode in {"auto", "files", "postgres"} else "auto"


def alignments_dir():
    return _env_path("SEC_UI_ALIGNMENTS_DIR") or _first_dir([
        ROOT / "test_data" / "alignments",
        ROOT.parent / "sec-filings-test-data" / "test_data" / "alignments",
        ROOT / "data" / "downstream" / "alignments",
        ROOT / "tests" / "fixtures" / "alignments",
    ])


def disclosures_dir():
    return _env_path("SEC_UI_DISCLOSURES_DIR") or _first_dir([
        ROOT / "test_data" / "disclosures",
        ROOT.parent / "sec-filings-test-data" / "test_data" / "disclosures",
        ROOT / "data" / "downstream" / "disclosures",
    ])


def peer_dir():
    return _env_path("SEC_UI_PEER_DIR") or (ROOT / "data" / "peer_comparison")


def legacy_manual_reviews_dir():
    return _env_path("SEC_UI_MANUAL_REVIEWS_DIR") or (ROOT / "data" / "manual_reviews")


def manual_reviews_dir():
    if data_source_mode() == "files":
        return legacy_manual_reviews_dir()
    from .review_storage import root
    return root()


def synthesis_dirs():
    value = os.environ.get("SEC_UI_SYNTHESIS_DIRS")
    if value:
        return [Path(p).expanduser() for p in value.split(os.pathsep) if p]
    return [ROOT / "data" / "synthesis", ROOT / "synthesis_dev", ROOT / "tests" / "fixtures" / "synthesis"]
