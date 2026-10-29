"""Persistent final-label choices, retaining original model labels and evidence."""
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


def label_source(saved):
    # Older review files contain manual annotations without an explicit source.
    return saved.get("label_source", "manual") if saved else "llm"


def _valid_choice(saved, labels, manual_field):
    return (saved.get("label_source", "manual") in {"llm", "manual"}
            and (saved.get(manual_field) is None or saved[manual_field] in labels))


def _manual_label(saved, field, legacy_field):
    return saved.get(field, saved[legacy_field]) if saved else None


def final_change(record, reviews):
    saved = current_review(record, reviews)
    llm = record["methods"].get("llm")
    llm = llm if llm in config.CHANGE_LABELS else None
    manual = _manual_label(saved, "manual_change_label", "change_label")
    source = label_source(saved)
    final = manual if source == "manual" else llm
    return {**record, "llm_change_label": llm, "manual_change_label": manual,
            "final_label_source": source, "final_change_label": final,
            "change_label": final or data.NOT_LABELLED}


def final_disclosure(record, reviews):
    saved = current_disclosure_review(record, reviews)
    llm = record.get("llm_disclosure_type", record.get("taxonomy"))
    manual = _manual_label(saved, "manual_disclosure_type", "disclosure_type")
    source = label_source(saved)
    final = manual if source == "manual" else llm
    return {**record, "llm_disclosure_type": llm, "manual_disclosure_type": manual,
            "final_label_source": source, "final_disclosure_type": final,
            "taxonomy": final or data.NOT_CLASSIFIED}


def apply_final_choices(comparison):
    """Resolve uncached annotations over cached pipeline data on every page run."""
    meta = comparison["meta"]
    company, previous, current = meta["company"], meta["previous_year"], meta["current_year"]
    changes = load_reviews(company, previous, current)
    types = {year: load_disclosure_reviews(company, year) for year in (previous, current)}
    records = []
    for original in comparison["records"]:
        record = final_change(original, changes)
        for side, year in (("previous", previous), ("current", current)):
            record[side + "_members"] = [final_disclosure(
                {**member, "company": company, "fiscal_year": year}, types[year])
                for member in original[side + "_members"]]
        categories = list(dict.fromkeys(member["taxonomy"] for member in
                         record["current_members"] + record["previous_members"]
                         if member["taxonomy"] != data.NOT_CLASSIFIED))
        record.update(categories=categories, category=categories[0] if categories else data.NOT_CLASSIFIED)
        records.append(record)
    return {**comparison, "records": records}


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


def ai_approved_change(record):
    return (not requires_change_review(record)
            and record.get("status") in {"ai_verified", "auto_matched", "unmatched"}
            and record.get("change_status") == "completed"
            and record["methods"].get("llm") in config.CHANGE_LABELS)


def ai_approved_disclosure(record):
    return not record["needs_review"] and record.get("taxonomy") in config.CONTENT_CATEGORIES


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
                or not _valid_choice(saved, config.CHANGE_LABELS, "manual_change_label")
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
    if saved and saved["source_fingerprint"] == fingerprint(record):
        return saved
    # A pipeline rerun may renumber groups. Keep a review only for identical
    # members and displayed source text, even when its old match ID differs.
    pending, versions = list(reviews.values()), []
    while pending:
        candidate = pending.pop()
        versions.append(candidate)
        pending.extend(candidate.get("previous_reviews", []))
    for candidate in sorted(versions, key=lambda value: value["reviewed_at"], reverse=True):
        if (candidate.get("previous_ids") == record["previous_ids"]
                and candidate.get("current_ids") == record["current_ids"]
                and candidate["source_fingerprint"] == fingerprint({**record, "match_id": candidate["match_id"]})):
            return candidate
    return None


def save_review(record: dict, label: str, notes: str, *, expected: dict | None, label_source="manual") -> dict:
    if label_source not in {"llm", "manual"}:
        raise data.DataError("Choose LLM or Manual for the final label source.")
    llm = record["methods"].get("llm")
    final = llm if label_source == "llm" else label
    if final not in config.CHANGE_LABELS:
        raise data.DataError("Choose a change label before saving.")
    if any(record[f"{side}_ids"] and not record[f"{side}_text"].strip() for side in ("previous", "current")):
        raise data.DataError("Source text is missing for a disclosure in this match.")
    # Streamlit sessions share this lock. Read again inside it to retain other saved labels.
    with _SAVE_LOCK:
        company, previous, current = record["company"], record["previous_year"], record["current_year"]
        path = review_path(company, previous, current)
        reviews = load_reviews(company, previous, current)
        if reviews.get(record["match_id"]) != expected:
            raise data.DataError("This review was updated in another session. Reload data before saving again.")
        prior_review = current_review(record, reviews)
        saved = {
            "match_id": record["match_id"], "change_label": final, "notes": notes.strip(),
            "label_source": label_source, "llm_change_label": llm,
            "manual_change_label": label if label in config.CHANGE_LABELS else
                                   _manual_label(prior_review, "manual_change_label", "change_label"),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": fingerprint(record), "source_file": record["source_file"],
            "previous_ids": record["previous_ids"], "current_ids": record["current_ids"],
            "original_change_label": record.get("pipeline_change_label", record["change_label"]),
        }
        if expected and expected["source_fingerprint"] != fingerprint(record):
            saved["previous_reviews"] = expected.get("previous_reviews", []) + [
                {key: value for key, value in expected.items() if key != "previous_reviews"}]
        elif expected and expected.get("previous_reviews"):
            saved["previous_reviews"] = expected["previous_reviews"]
        reviews[record["match_id"]] = saved
        payload = {"schema_version": 1, "company": company, "previous_year": previous,
                   "current_year": current, "reviews": reviews}
        _write_reviews(path, payload)
    return saved


def export_rows(records: list, reviews: dict, *, include_unreviewed=False) -> list:
    rows = []
    for record in records:
        saved = current_review(record, reviews)
        if saved or include_unreviewed:
            final = final_change(record, reviews)
            row = data.export_rows([final])[0]
            row.update(manual_change_label=final["manual_change_label"], manual_notes=saved["notes"] if saved else "",
                       reviewed_at=saved["reviewed_at"] if saved else "",
                       source_fingerprint=fingerprint(record))
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
                or not _valid_choice(saved, config.CONTENT_CATEGORIES, "manual_disclosure_type")
                or not all(isinstance(saved.get(field), str) for field in ("notes", "reviewed_at", "source_fingerprint"))):
            raise data.DataError(f"Invalid disclosure review entry at {path}.")
    return payload["reviews"]


def disclosure_fingerprint(record):
    source = {field: record.get(field) for field in ("company", "fiscal_year", "disclosure_id", "content", "sources")}
    return hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def current_disclosure_review(record, reviews):
    saved = reviews.get(record["disclosure_id"])
    return saved if saved and saved["source_fingerprint"] == disclosure_fingerprint(record) else None


def save_disclosure_review(record, disclosure_type, notes, *, expected, label_source="manual"):
    if label_source not in {"llm", "manual"}:
        raise data.DataError("Choose LLM or Manual for the final label source.")
    llm = record.get("llm_disclosure_type", record.get("taxonomy"))
    final = llm if label_source == "llm" else disclosure_type
    if final not in config.CONTENT_CATEGORIES:
        raise data.DataError("Choose a disclosure type before saving.")
    if not (record.get("content") or "").strip():
        raise data.DataError("Source text is missing for this disclosure.")
    with _SAVE_LOCK:
        company, year, disclosure_id = record["company"], record["fiscal_year"], record["disclosure_id"]
        path = disclosure_review_path(company, year)
        reviews = load_disclosure_reviews(company, year)
        if reviews.get(disclosure_id) != expected:
            raise data.DataError("This disclosure review was updated in another session. Reload data before saving again.")
        prior_review = current_disclosure_review(record, reviews)
        saved = {"disclosure_id": disclosure_id, "disclosure_type": final,
                 "label_source": label_source, "llm_disclosure_type": llm,
                 "manual_disclosure_type": disclosure_type if disclosure_type in config.CONTENT_CATEGORIES else
                                           _manual_label(prior_review, "manual_disclosure_type", "disclosure_type"),
                 "original_disclosure_type": llm or "", "notes": notes.strip(),
                 "reviewed_at": datetime.now(timezone.utc).isoformat(),
                 "source_fingerprint": disclosure_fingerprint(record)}
        reviews[disclosure_id] = saved
        _write_reviews(path, {"schema_version": 1, "stage": "disclosure", "company": company,
                              "fiscal_year": int(year), "reviews": reviews})
    return saved


def export_disclosure_rows(records, reviews, *, include_unreviewed=False):
    rows = []
    for record in records:
        saved = current_disclosure_review(record, reviews)
        if not saved and not include_unreviewed:
            continue
        final = final_disclosure(record, reviews)
        sources = record.get("sources") or []
        rows.append({"disclosure_id": record["disclosure_id"], "company": record["company"],
                     "fiscal_year": record["fiscal_year"], "item": record.get("item", ""),
                     "original_disclosure_type": final["llm_disclosure_type"],
                     "llm_disclosure_type": final["llm_disclosure_type"],
                     "manual_disclosure_type": final["manual_disclosure_type"],
                     "final_disclosure_type": final["final_disclosure_type"], "final_label_source": final["final_label_source"],
                     "manual_notes": saved["notes"] if saved else "",
                     "reviewed_at": saved["reviewed_at"] if saved else "", "source_fingerprint": disclosure_fingerprint(record),
                     "content": record.get("content", ""),
                     "paragraph_ids": "; ".join(dict.fromkeys(s.get("paragraph_id", "") for s in sources)),
                     "sentence_ids": "; ".join(sentence.get("sentence_id", "") for s in sources for sentence in s.get("sentences", [])),
                     "source_urls": "; ".join(dict.fromkeys(s.get("source_url", "") for s in sources))})
    return rows
