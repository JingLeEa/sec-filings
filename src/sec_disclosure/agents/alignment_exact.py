"""Conservative full-text equality before any model-based alignment."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict


EXACT_POLICY = "same_item_unique_full_text_bullets_whitespace_v1"
# Only line-leading list markers. Keep numbered lists, minus signs before
# amounts, inline punctuation, case, and every word/number unchanged.
_BULLET = re.compile(r"^[^\S\n]*(?:[•◦▪‣⁃∙●][^\S\n]*|[-*][^\S\n]+(?=[A-Za-z]))")


def normalize_text(content):
    return " ".join("\n".join(_BULLET.sub("", line, count=1) for line in content.splitlines()).split())


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def accepted_status(row):
    return "auto_matched" if row.get("match_method") == "exact_text" else "ai_verified"


def valid_exact_proof(row, data):
    members = row["previous_ids"] + row["current_ids"]
    texts = {normalize_text(data.records[key]["content"]) for key in members}
    return (len(texts) == 1 and "" not in texts and row.get("exact_match") == {
        "policy": EXACT_POLICY, "normalized_text_sha256": text_hash(next(iter(texts)))})


def exact_matches(data):
    """Only unambiguous equality; duplicate text within either year goes to LLM.

    Index actual strings, not hashes or retrieval scores. The hash is audit
    metadata only. All citations retain the unmodified original source text.
    """
    buckets = defaultdict(lambda: defaultdict(list))
    for key, record in sorted(data.records.items()):
        text = normalize_text(record["content"])
        if text:
            buckets[(record["item"], text)][record["fiscal_year"]].append(key)
    rows = []
    for (_, text), by_year in buckets.items():
        before, after = (by_year[year] for year in data.years)
        if len(before) != 1 or len(after) != 1:
            continue
        flags = ["input_extraction_needs_review"] if any(
            data.records[key]["extraction_status"] == "needs_review" for key in before + after) else []
        rows.append({"previous_ids": before, "current_ids": after, "relationship": "one_to_one",
                     "match_method": "exact_text",
                     "exact_match": {"policy": EXACT_POLICY, "normalized_text_sha256": text_hash(text)},
                     "explanation": "The complete selected original text is identical within the same SEC Item after removing line-leading bullet formatting and normalizing whitespace. Neither LLM agent was used for this link.",
                     "evidence": [{"disclosure_id": key, "sentences": [
                         {"sentence_id": sentence["sentence_id"], "paragraph_id": source["paragraph_id"],
                          "source_url": source["source_url"], "text": sentence["text"]}
                         for source in data.records[key]["sources"] for sentence in source["sentences"]]}
                         for key in before + after],
                     "status": "needs_review" if flags else "auto_matched",
                     "review_reasons": flags, "verifier_review_reason": ""})
    return sorted(rows, key=lambda row: row["current_ids"])
