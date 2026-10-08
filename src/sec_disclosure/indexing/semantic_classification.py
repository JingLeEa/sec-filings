"""Rule-based change classification from semantic retrieval scores.

Takes the candidates produced by SemanticIndex.candidates() and the
disclosure records, and returns a classification for each alignment row
that can be written directly into change_analysis.semantic.

This is a pure scoring pass — no LLM calls, no network, deterministic.
The rules map similarity-score bands to change labels using the same
vocabulary as the alignment pipeline: Added, Removed, Unchanged, Modified,
Expanded, Reduced, Relocated.

Score ceiling
~~~~~~~~~~~~~
While summaries are empty (the LLM disclosure stage has not run yet),
the 0.35 summary weight contributes nothing and the maximum possible
score is 0.65. All thresholds are expressed as fractions of ``ceiling``
so they stay correct when summaries arrive and the ceiling moves to 1.0.
"""

from __future__ import annotations

from typing import Any

# ----- thresholds as fractions of the score ceiling -----------------------
# These come from the smoke-test separation data in the handoff doc.
# random-pair median ≈ 0.54 of ceiling; true-pair median ≈ 0.92 of ceiling.

UNCHANGED_FLOOR = 0.97      # ≥ 97% of ceiling → almost certainly verbatim
MODIFIED_FLOOR = 0.70       # 70–97% → same disclosure, wording changed
WEAK_FLOOR = 0.55           # 55–70% → plausible match but uncertain
# below 0.55 → no credible counterpart found

SUMMARY_WEIGHT = 0.35
CONTENT_WEIGHT = 0.65


def _ceiling(records: dict[str, dict]) -> float:
    """Return 1.0 when summaries are populated, 0.65 when they are empty."""
    has_summary = any(
        bool((r.get("summary") or "").strip())
        for r in records.values()
    )
    return 1.0 if has_summary else CONTENT_WEIGHT


def _text_overlap(text_a: str, text_b: str) -> float:
    """Quick character-level Jaccard as a proxy for verbatim similarity."""
    if not text_a or not text_b:
        return 0.0
    set_a = set(text_a.split())
    set_b = set(text_b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def classify_pair(
    current_record: dict,
    best_candidate: dict | None,
    records: dict[str, dict],
    ceiling: float,
) -> dict[str, Any]:
    """Classify one current-year disclosure against its best retrieval candidate.

    Returns a dict suitable for ``change_analysis.semantic``::

        {
            "label": "Modified",
            "matched_id": "intel_2024_1A_P041",
            "score": 0.612,
            "normalised_score": 0.941,
            "rationale": "..."
        }
    """
    if best_candidate is None or ceiling == 0:
        return {
            "label": "Added",
            "matched_id": None,
            "score": None,
            "normalised_score": None,
            "rationale": "No previous-year candidates were available.",
        }

    score = best_candidate["score"]
    matched_id = best_candidate["disclosure_id"]
    normalised = score / ceiling if ceiling > 0 else 0.0

    prev_record = records.get(matched_id, {})
    prev_content = prev_record.get("content", "")
    curr_content = current_record.get("content", "")
    word_overlap = _text_overlap(prev_content, curr_content)

    prev_section = prev_record.get("section", "")
    curr_section = current_record.get("section", "")

    base = {
        "matched_id": matched_id,
        "score": round(score, 6),
        "normalised_score": round(normalised, 4),
    }

    # --- Unchanged: near-ceiling score AND high word overlap ---------------
    if normalised >= UNCHANGED_FLOOR and word_overlap >= 0.90:
        return {**base, "label": "Unchanged",
                "rationale": f"Score {normalised:.2%} of ceiling with {word_overlap:.0%} word overlap — text is effectively identical."}

    # --- Relocated: high semantic match but section changed -----------------
    if normalised >= MODIFIED_FLOOR and prev_section and curr_section and prev_section != curr_section:
        return {**base, "label": "Relocated",
                "rationale": f"Score {normalised:.2%} of ceiling — content is similar but section changed from '{prev_section}' to '{curr_section}'."}

    # --- Unchanged at high score even without perfect overlap (may be
    #     whitespace / punctuation normalisation differences) ---------------
    if normalised >= UNCHANGED_FLOOR:
        return {**base, "label": "Unchanged",
                "rationale": f"Score {normalised:.2%} of ceiling — semantically identical despite surface differences (word overlap {word_overlap:.0%})."}

    # --- Modified / Expanded / Reduced: good match, wording changed --------
    if normalised >= MODIFIED_FLOOR:
        # Try to distinguish expanded vs reduced by content length ratio.
        prev_len = len(prev_content.split())
        curr_len = len(curr_content.split())
        if prev_len and curr_len:
            ratio = curr_len / prev_len
            if ratio >= 1.4:
                return {**base, "label": "Expanded",
                        "rationale": f"Score {normalised:.2%} of ceiling — content grew from ~{prev_len} to ~{curr_len} words ({ratio:.1f}×)."}
            if ratio <= 0.7:
                return {**base, "label": "Reduced",
                        "rationale": f"Score {normalised:.2%} of ceiling — content shrank from ~{prev_len} to ~{curr_len} words ({ratio:.1f}×)."}
        return {**base, "label": "Modified",
                "rationale": f"Score {normalised:.2%} of ceiling with {word_overlap:.0%} word overlap — same disclosure, wording changed."}

    # --- Weak match band: label as Modified but flag low confidence ---------
    if normalised >= WEAK_FLOOR:
        return {**base, "label": "Modified",
                "rationale": f"Score {normalised:.2%} of ceiling — weak match, possibly a substantial rewrite. Manual review recommended."}

    # --- Below threshold: treat as Added (no credible counterpart) ---------
    return {**base, "label": "Added",
            "rationale": f"Score {normalised:.2%} of ceiling — below the plausible-match threshold ({WEAK_FLOOR:.0%}). Treated as a new disclosure."}


def classify_removed(
    previous_id: str,
    candidates: dict[str, list[dict]],
    ceiling: float,
) -> dict[str, Any]:
    """Check whether a previous-year disclosure appears as a reverse-retrieval
    hit on any current-year disclosure. If not, it was removed."""
    for current_id, hits in candidates.items():
        for hit in hits:
            if hit["disclosure_id"] == previous_id:
                normalised = hit["score"] / ceiling if ceiling else 0.0
                if normalised >= WEAK_FLOOR:
                    return {
                        "label": "see_current_side",
                        "matched_id": current_id,
                        "score": round(hit["score"], 6),
                        "normalised_score": round(normalised, 4),
                        "rationale": f"This previous-year disclosure matched current-year {current_id} via reverse retrieval.",
                    }
    return {
        "label": "Removed",
        "matched_id": None,
        "score": None,
        "normalised_score": None,
        "rationale": "No current-year disclosure scored above the plausible-match threshold.",
    }


def classify_alignment_row(
    row: dict,
    candidates: dict[str, list[dict]],
    records: dict[str, dict],
    ceiling: float | None = None,
) -> dict[str, Any]:
    """Produce the ``change_analysis.semantic`` value for one alignment row.

    Parameters
    ----------
    row : dict
        An alignment row with ``previous_ids``, ``current_ids``,
        ``previous_disclosures``, ``current_disclosures``.
    candidates : dict
        The output of ``SemanticIndex.candidates()``.
    records : dict
        The flat disclosure records dict passed to SemanticIndex.
    ceiling : float, optional
        Score ceiling; auto-detected from records if not supplied.
    """
    if ceiling is None:
        ceiling = _ceiling(records)

    previous_ids = row.get("previous_ids", [])
    current_ids = row.get("current_ids", [])

    # --- single-sided rows ------------------------------------------------
    if not previous_ids and current_ids:
        # Introduced disclosure — check if retrieval found any counterpart.
        current_id = current_ids[0]
        hits = candidates.get(current_id, [])
        best = hits[0] if hits else None
        current_record = records.get(current_id, {})
        return classify_pair(current_record, best, records, ceiling)

    if previous_ids and not current_ids:
        return classify_removed(previous_ids[0], candidates, ceiling)

    # --- both sides populated (the common case) ---------------------------
    if len(current_ids) == 1 and len(previous_ids) == 1:
        current_id = current_ids[0]
        previous_id = previous_ids[0]
        hits = candidates.get(current_id, [])
        # Find the specific previous-year ID in the candidate list.
        matched_hit = next((h for h in hits if h["disclosure_id"] == previous_id), None)
        if matched_hit is None and hits:
            # The alignment agent paired them but retrieval didn't — still
            # classify based on text comparison with score = None.
            matched_hit = {"disclosure_id": previous_id, "score": 0.0, "text_score": 0.0}
        current_record = records.get(current_id, {})
        return classify_pair(current_record, matched_hit, records, ceiling)

    # --- many-to-one or one-to-many (merges / splits) ---------------------
    # For splits and merges we just report Modified with evidence from
    # the best-scoring pair, since the relationship is inherently complex.
    best_score = 0.0
    best_pair = None
    for cid in current_ids:
        for hit in candidates.get(cid, []):
            if hit["disclosure_id"] in previous_ids and hit["score"] > best_score:
                best_score = hit["score"]
                best_pair = (cid, hit)
    normalised = best_score / ceiling if ceiling else 0.0
    if best_pair:
        cid, hit = best_pair
        n_prev, n_curr = len(previous_ids), len(current_ids)
        direction = "split" if n_prev == 1 and n_curr > 1 else "merge" if n_prev > 1 and n_curr == 1 else "regroup"
        return {
            "label": "Modified",
            "matched_id": hit["disclosure_id"],
            "score": round(hit["score"], 6),
            "normalised_score": round(normalised, 4),
            "rationale": f"{direction.title()}: {n_prev} previous → {n_curr} current. Best pair scored {normalised:.2%} of ceiling.",
        }

    return {
        "label": "Modified",
        "matched_id": None,
        "score": None,
        "normalised_score": None,
        "rationale": f"Multi-sided alignment ({len(previous_ids)} previous, {len(current_ids)} current) with no retrieval overlap.",
    }


def classify_all(
    alignment_rows: list[dict],
    candidates: dict[str, list[dict]],
    records: dict[str, dict],
) -> list[dict]:
    """Classify every alignment row and return the rows with
    ``change_analysis.semantic`` populated.

    This mutates the rows in place and also returns them for chaining.
    """
    ceiling = _ceiling(records)
    for row in alignment_rows:
        analysis = row.setdefault("change_analysis", {
            "status": "not_started", "lexical": None, "semantic": None,
            "llm": None, "final_taxonomy": None,
        })
        analysis["semantic"] = classify_alignment_row(row, candidates, records, ceiling)
        if analysis["status"] == "not_started":
            analysis["status"] = "semantic_done"
    return alignment_rows
