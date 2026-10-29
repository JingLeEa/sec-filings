"""Source-bound human decisions shared by extraction and downstream refreshes."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

from sec_disclosure.annotation.export_table_annotations import CONTENT_LABELS

ROOT = Path(__file__).resolve().parents[2]
CHANGE_LABELS = ("New", "Removed", "Expanded", "Reduced", "Modified", "Reworded", "Unchanged")
FINAL_STATUSES = ["ai_verified", "auto_matched", "unmatched"]


class StaleReview(ValueError):
    pass


def review_root():
    return Path(os.environ.get("SEC_UI_MANUAL_REVIEWS_DIR") or ROOT / "data/manual_reviews").expanduser()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def annotation_path(root, company, stage, previous, current=None):
    if stage == "disclosure":
        return Path(root) / "disclosures" / company / str(previous) / "manual_reviews.json"
    return Path(root) / company / f"{previous}-{current}" / "manual_reviews.json"


def read_reviews(root, company, stage, previous, current=None):
    path = annotation_path(root, company, stage, previous, current)
    if not path.exists():
        return {}
    document = json.loads(path.read_text())
    if (document.get("schema_version") != 1 or document.get("company") != company
            or not isinstance(document.get("reviews"), dict)):
        raise ValueError(f"Invalid human review file: {path.name}")
    if stage == "disclosure":
        valid = document.get("stage") == "disclosure" and document.get("fiscal_year") == int(previous)
    else:
        valid = document.get("previous_year") == int(previous) and document.get("current_year") == int(current)
    if not valid:
        raise ValueError("Human reviews belong to a different filing or comparison.")
    return document["reviews"]


def disclosure_fingerprint(row):
    return fingerprint({field: row.get(field) for field in
                        ("company", "fiscal_year", "disclosure_id", "content", "sources")})


def apply_disclosure_review(row, saved):
    result = deepcopy(row)
    if not saved or saved.get("label_source", "manual") != "manual":
        return result
    identity = {**row, "fiscal_year": int(row["fiscal_year"])}
    if saved.get("source_fingerprint") != disclosure_fingerprint(identity):
        return result
    label = saved.get("manual_disclosure_type", saved.get("disclosure_type"))
    if label not in CONTENT_LABELS:
        raise ValueError("Invalid human disclosure type.")
    if not row.get("content", "").strip() or not row.get("sources"):
        raise ValueError("Human disclosure review requires original source evidence.")
    original = result.get("human_review") or {}
    previous = original.get("previous_verification", deepcopy(result.get("verification") or {}))
    result["llm_disclosure_type"] = result.get("llm_disclosure_type", result["taxonomy"])
    result["taxonomy"] = label
    from sec_disclosure.llm.disclosures import apply_source_unit_policy
    verification = apply_source_unit_policy(result.get("verification") or {})
    result["verification"] = verification
    was_needs_review = verification.get("status") == "needs_review"
    verification["review_reasons"] = [reason for reason in verification.get("review_reasons", [])
                                      if reason != "model_requested_review"]
    verification["model_review_reason"] = ""
    if verification.get("source_unit_count", 0) >= 1:
        verification["status"] = "needs_review" if verification["review_reasons"] else "source_validated"
    result["human_review"] = {"stage": "disclosure", "disclosure_type": label,
                              "reviewed_at": saved["reviewed_at"], "notes": saved.get("notes", ""),
                              "source_fingerprint": saved["source_fingerprint"],
                              "previous_verification": previous,
                              "was_needs_review": original.get("was_needs_review", was_needs_review)}
    return result


def apply_disclosure_reports(reports, reviews):
    reports = deepcopy(reports)
    rows = [row for name in ("disclosures.json", "review_candidates.json")
            for row in reports[name]["disclosures"]]
    original = rows
    rows = [apply_disclosure_review(row, reviews.get(row["disclosure_id"])) for row in rows]
    if rows == original:
        return reports
    for name, status in (("disclosures.json", "source_validated"), ("review_candidates.json", "needs_review")):
        reports[name]["disclosures"] = [row for row in rows if row["verification"]["status"] == status]
    usage = reports.get("token_usage.json")
    if usage is not None:
        usage["source_validated_disclosures"] = len(reports["disclosures.json"]["disclosures"])
        usage["review_candidates"] = len(reports["review_candidates.json"]["disclosures"])
    return reports


def alignment_identity(row, records, company, previous, current, *, match_id=None, text_source="disclosure"):
    before, after = sorted(row["previous_ids"]), sorted(row["current_ids"])
    texts = {}
    for side, ids in (("previous", before), ("current", after)):
        if text_source == "cited sentences":
            texts[side] = " ".join(sentence["text"] for entry in row.get("evidence", [])
                                   if entry["disclosure_id"] in ids for sentence in entry["sentences"])
        else:
            texts[side] = "\n\n".join(records[key]["content"] for key in ids)
    return {"company": company, "previous_year": int(previous), "current_year": int(current),
            "match_id": match_id or row["match_id"], "previous_ids": before, "current_ids": after,
            "previous_text": texts["previous"], "current_text": texts["current"], "text_source": text_source}


def bound_alignment_review(row, records, company, previous, current, saved):
    if saved.get("label_source", "manual") != "manual":
        return False
    if (sorted(saved.get("previous_ids", [])) != sorted(row["previous_ids"])
            or sorted(saved.get("current_ids", [])) != sorted(row["current_ids"])):
        return False
    for text_source in ("disclosure", "cited sentences"):
        view = alignment_identity(row, records, company, previous, current,
                                  match_id=saved["match_id"], text_source=text_source)
        if fingerprint(view) == saved.get("source_fingerprint"):
            return True
    return False


def manual_change_label(row):
    saved = row.get("manual_review") or {}
    label = saved.get("change_label")
    return label if saved.get("source_fingerprint") and label in CHANGE_LABELS else None


def review_versions(reviews):
    pending, rows = list(reviews.values()), []
    while pending:
        saved = pending.pop()
        rows.append(saved)
        pending.extend(saved.get("previous_reviews", []))
    return sorted(rows, key=lambda saved: saved.get("reviewed_at", ""), reverse=True)


def apply_alignment_reviews(report, records, reviews):
    """Match by exact membership/text, preserving human labels across renumbering."""
    report = deepcopy(report)
    company, previous, current = report["company"], report["previous_year"], report["current_year"]
    for row in report["alignments"]:
        saved = next((saved for saved in review_versions(reviews)
                      if bound_alignment_review(row, records, company, previous, current, saved)), None)
        if saved is None:
            continue
        label = saved.get("manual_change_label", saved.get("change_label"))
        if label not in CHANGE_LABELS:
            raise ValueError("Invalid human change label.")
        view = alignment_identity(row, records, company, previous, current)
        original = row.get("manual_review") or {}
        if original.get("change_label") != label or original.get("reviewed_at") != saved["reviewed_at"]:
            row.pop("materiality_analysis", None)
        row["manual_review"] = {"stage": "alignment", "change_label": label,
                                "reviewed_at": saved["reviewed_at"], "notes": saved.get("notes", ""),
                                "source_fingerprint": fingerprint(view), "original_match_id": saved["match_id"]}
        analysis = row.setdefault("change_analysis", {})
        if analysis.get("llm") is None and saved.get("llm_change_label") in CHANGE_LABELS:
            analysis["llm"] = saved["llm_change_label"]
        analysis.update(status="completed", final_taxonomy=label)
        analysis.setdefault("llm", None)
        analysis.setdefault("lexical", None)
        analysis.setdefault("semantic", None)
    return report


def split_reports(report):
    reports = {}
    counts = dict(Counter(row["status"] for row in report["alignments"]))
    for name, statuses in (("alignments.json", FINAL_STATUSES), ("needs_review.json", ["needs_review"])):
        rows = [row for row in report["alignments"] if row["status"] in statuses]
        reports[name] = {**{key: value for key, value in report.items() if key != "alignments"},
                         "included_statuses": statuses, "alignments": rows, "alignment_count": len(rows),
                         "overall_counts": counts, "counts": dict(Counter(row["status"] for row in rows))}
    return reports
