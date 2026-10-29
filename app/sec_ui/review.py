"""Persistent human labels for the needs_review queue, separate from pipeline output."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from . import config, data

_SAVE_LOCK = Lock()


def _write_reviews(path, payload):
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".manual_reviews.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(payload, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError:
        raise data.DataError(f"Could not save the review at {path}. Check that the folder is writable.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def requires_change_review(record):
    return (record["source_file"] == "needs_review.json"
            or record.get("change_status") == "needs_review")


def review_path(company: str, previous: int, current: int) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", company) or previous >= current:
        raise data.DataError("Invalid company or filing years for manual review.")
    return config.manual_reviews_dir() / company / f"{previous}-{current}" / "manual_reviews.json"


def load_reviews(company: str, previous: int, current: int) -> dict:
    path = review_path(company, previous, current)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise data.DataError(f"Could not read saved manual reviews at {path}.") from None
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("company") != company or payload.get("previous_year") != previous
            or payload.get("current_year") != current or not isinstance(payload.get("reviews"), dict)):
        raise data.DataError(f"Invalid manual review file at {path}.")
    for match_id, saved in payload["reviews"].items():
        if (not isinstance(saved, dict) or saved.get("match_id") != match_id
                or saved.get("change_label") not in config.CHANGE_LABELS
                or not all(isinstance(saved.get(key), str)
                           for key in ("notes", "reviewed_at", "source_fingerprint"))):
            raise data.DataError(f"Invalid manual review entry at {path}.")
    return payload["reviews"]


def fingerprint(record: dict) -> str:
    """A reused match ID must not silently inherit a label for different text or members."""
    source = {key: record[key] for key in (
        "company", "previous_year", "current_year", "match_id", "previous_ids", "current_ids",
        "previous_text", "current_text", "text_source",
    )}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def current_review(record: dict, reviews: dict) -> dict | None:
    saved = reviews.get(record["match_id"])
    return saved if saved and saved["source_fingerprint"] == fingerprint(record) else None


def save_review(record: dict, label: str, notes: str, *, expected: dict | None) -> dict:
    if not requires_change_review(record) and (not expected or expected.get("source_fingerprint") != fingerprint(record)):
        raise data.DataError("Only groups needing a change label or from needs_review.json can be reviewed here.")
    if label not in config.CHANGE_LABELS:
        raise data.DataError("Choose a change label before saving.")
    # Streamlit sessions share this lock. Read again inside it to retain other saved labels.
    with _SAVE_LOCK:
        company, previous, current = record["company"], record["previous_year"], record["current_year"]
        path = review_path(company, previous, current)
        reviews = load_reviews(company, previous, current)
        if reviews.get(record["match_id"]) != expected:
            raise data.DataError("This review was updated in another session. Reload data before saving again.")
        saved = {
            "match_id": record["match_id"], "change_label": label, "notes": notes.strip(),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": fingerprint(record), "source_file": record["source_file"],
            "previous_ids": record["previous_ids"], "current_ids": record["current_ids"],
            "original_change_label": record["change_label"],
        }
        reviews[record["match_id"]] = saved
        payload = {"schema_version": 1, "company": company, "previous_year": previous,
                   "current_year": current, "reviews": reviews}
        _write_reviews(path, payload)
    return saved


def export_rows(records: list, reviews: dict) -> list:
    rows = []
    for record in records:
        saved = current_review(record, reviews)
        if saved:
            row = data.export_rows([record])[0]
            row.update(manual_change_label=saved["change_label"], manual_notes=saved["notes"],
                       reviewed_at=saved["reviewed_at"], source_fingerprint=saved["source_fingerprint"])
            rows.append(row)
    return rows


def disclosure_review_path(company, year):
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", company) or not 1900 <= int(year) <= 2200:
        raise data.DataError("Invalid company or filing year for disclosure review.")
    return config.manual_reviews_dir() / "disclosures" / company / str(year) / "manual_reviews.json"


def load_disclosure_reviews(company, year):
    path = disclosure_review_path(company, year)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise data.DataError(f"Could not read saved disclosure reviews at {path}.") from None
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("stage") != "disclosure"
            or payload.get("company") != company or payload.get("fiscal_year") != int(year)
            or not isinstance(payload.get("reviews"), dict)):
        raise data.DataError(f"Invalid disclosure review file at {path}.")
    for disclosure_id, saved in payload["reviews"].items():
        if (not isinstance(saved, dict) or saved.get("disclosure_id") != disclosure_id
                or saved.get("disclosure_type") not in config.CONTENT_CATEGORIES
                or not all(isinstance(saved.get(field), str) for field in ("notes", "reviewed_at", "source_fingerprint"))):
            raise data.DataError(f"Invalid disclosure review entry at {path}.")
    return payload["reviews"]


def disclosure_fingerprint(record):
    source = {field: record.get(field) for field in ("company", "fiscal_year", "disclosure_id", "content", "sources")}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def current_disclosure_review(record, reviews):
    saved = reviews.get(record["disclosure_id"])
    return saved if saved and saved["source_fingerprint"] == disclosure_fingerprint(record) else None


def save_disclosure_review(record, disclosure_type, notes, *, expected):
    if disclosure_type not in config.CONTENT_CATEGORIES:
        raise data.DataError("Choose a disclosure type before saving.")
    if not (record.get("content") or "").strip():
        raise data.DataError("Source text is missing for this disclosure.")
    with _SAVE_LOCK:
        company, year, disclosure_id = record["company"], record["fiscal_year"], record["disclosure_id"]
        path = disclosure_review_path(company, year)
        reviews = load_disclosure_reviews(company, year)
        if reviews.get(disclosure_id) != expected:
            raise data.DataError("This disclosure review was updated in another session. Reload data before saving again.")
        saved = {"disclosure_id": disclosure_id, "disclosure_type": disclosure_type,
                 "original_disclosure_type": record.get("taxonomy", ""), "notes": notes.strip(),
                 "reviewed_at": datetime.now(timezone.utc).isoformat(),
                 "source_fingerprint": disclosure_fingerprint(record)}
        reviews[disclosure_id] = saved
        _write_reviews(path, {"schema_version": 1, "stage": "disclosure", "company": company,
                              "fiscal_year": int(year), "reviews": reviews})
    return saved


def export_disclosure_rows(records, reviews):
    rows = []
    for record in records:
        saved = current_disclosure_review(record, reviews)
        if not saved:
            continue
        sources = record.get("sources") or []
        rows.append({"disclosure_id": record["disclosure_id"], "company": record["company"],
                     "fiscal_year": record["fiscal_year"], "item": record.get("item", ""),
                     "original_disclosure_type": record.get("taxonomy", ""),
                     "manual_disclosure_type": saved["disclosure_type"], "manual_notes": saved["notes"],
                     "reviewed_at": saved["reviewed_at"], "source_fingerprint": saved["source_fingerprint"],
                     "content": record.get("content", ""),
                     "paragraph_ids": "; ".join(dict.fromkeys(s.get("paragraph_id", "") for s in sources)),
                     "sentence_ids": "; ".join(sentence.get("sentence_id", "") for s in sources for sentence in s.get("sentences", [])),
                     "source_urls": "; ".join(dict.fromkeys(s.get("source_url", "") for s in sources))})
    return rows
