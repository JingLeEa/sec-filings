"""Consolidate already supported split/merge links without inventing new links."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from .alignment_exact import accepted_status, valid_exact_proof


GROUPING_POLICY = "shared_disclosure_star_v1"
ASSIGNMENT_POLICY = "supported_matched_overlaps_v1"
CONFLICT = "conflicting_alignment_assignment"
BLOCKED = {"agent_did_not_finish", "invalid_or_omitted_verifier_proposal", "absence_not_proven_by_retrieval"}


def supported_match(row, data):
    """Require both years, one Item, and exact selected evidence for every member."""
    before, after = row["previous_ids"], row["current_ids"]
    if not before or not after or set(row["review_reasons"]) & BLOCKED:
        return False
    if row["status"] not in {"auto_matched", "ai_verified", "needs_review"}:
        return False
    if row["status"] == "needs_review" and not row["review_reasons"]:
        return False  # Do not silently clear unexplained uncertainty.
    if not isinstance(row.get("explanation"), str) or not row["explanation"].strip():
        return False
    members = set(before + after)
    if len(members) != len(before) + len(after) or not members <= data.records.keys():
        return False
    for keys, year in ((before, data.years[0]), (after, data.years[1])):
        if any(data.records[key]["fiscal_year"] != year for key in keys):
            return False
    if len({data.records[key]["item"] for key in members}) != 1:
        return False
    if row.get("match_method") == "exact_text" or row["status"] == "auto_matched":
        if row.get("match_method") != "exact_text" or not valid_exact_proof(row, data):
            return False
    cited = set()
    for entry in row.get("evidence", []):
        key = entry.get("disclosure_id")
        if key not in members or key in cited or not entry.get("sentences"):
            return False
        selected = {s["sentence_id"]: {"sentence_id": s["sentence_id"], "text": s["text"],
                    "paragraph_id": source["paragraph_id"], "source_url": source["source_url"]}
                    for source in data.records[key]["sources"] for s in source["sentences"]}
        if any(s != selected.get(s.get("sentence_id")) for s in entry["sentences"]):
            return False
        cited.add(key)
    return cited == members


def consolidate_alignments(decisions, data):
    """Merge whole overlap components only when one side has a single disclosure.

    A shared hub preserves every existing edge. Arbitrary many-to-many chains,
    matched/unmatched contradictions, and incomplete decisions stay separate.
    This checks source fidelity, not semantic correctness of model decisions.
    """
    rows = deepcopy(decisions)
    owners = defaultdict(set)
    for i, row in enumerate(rows):
        row.pop("change_type", None)
        for key in row["previous_ids"] + row["current_ids"]:
            owners[key].add(i)
    visited, result = set(), []
    for i in range(len(rows)):
        if i in visited:
            continue
        pending, component = [i], set()
        while pending:
            index = pending.pop()
            if index in component:
                continue
            component.add(index)
            for key in rows[index]["previous_ids"] + rows[index]["current_ids"]:
                pending.extend(owners[key] - component)
        visited.update(component)
        originals = [rows[index] for index in sorted(component)]
        before = sorted({key for row in originals for key in row["previous_ids"]})
        after = sorted({key for row in originals for key in row["current_ids"]})
        if (len(originals) < 2 or min(len(before), len(after)) != 1
                or not all(supported_match(row, data) for row in originals)
                or len({data.records[key]["item"] for key in before + after}) != 1):
            result.extend(originals)
            continue
        merged = deepcopy(originals[0])
        flags = sorted({flag for row in originals for flag in row["review_reasons"]} - {CONFLICT})
        if any(data.records[key]["extraction_status"] == "needs_review" for key in before + after):
            flags = sorted(set(flags) | {"input_extraction_needs_review"})
        evidence = defaultdict(dict)
        for row in originals:
            for entry in row["evidence"]:
                for sentence in entry["sentences"]:
                    evidence[entry["disclosure_id"]][sentence["sentence_id"]] = sentence
        source_fields = ("match_id", "previous_ids", "current_ids", "relationship", "explanation", "evidence", "status",
                         "review_reasons", "verifier_review_reason", "metadata_repairs", "match_method", "exact_match")
        sources = []
        for row in originals:
            sources.extend(row["grouping"]["source_alignments"] if "grouping" in row else
                           [{key: row[key] for key in source_fields if key in row}])
        methods = {source.get("match_method", "llm") for source in sources}
        if "exact_text" in methods:
            merged["match_method"] = "exact_text" if methods == {"exact_text"} else "exact_text_and_llm"
        if merged.get("match_method") != "exact_text":
            merged.pop("exact_match", None)
        merged.update(previous_ids=before, current_ids=after,
                      relationship=("one" if len(before) == 1 else "many") + "_to_" + ("one" if len(after) == 1 else "many"),
                      explanation="\n\n".join(dict.fromkeys(row["explanation"] for row in originals)),
                      evidence=[{"disclosure_id": key, "sentences": list(evidence[key].values())} for key in before + after],
                      status="needs_review" if flags else accepted_status(merged), review_reasons=flags,
                      verifier_review_reason="\n\n".join(dict.fromkeys(row["verifier_review_reason"] for row in originals
                                                                        if row["verifier_review_reason"])),
                      grouping={"method": GROUPING_POLICY, "source_alignments": sources})
        repairs = sorted({field for row in originals for field in row.get("metadata_repairs", [])})
        if repairs:
            merged["metadata_repairs"] = repairs
        # Refresh nested member metadata when consolidating an exported report.
        for side in ("previous", "current"):
            field = side + "_disclosures"
            if field in merged:
                merged[field] = [{key: data.records[d][key] for key in
                                 ("disclosure_id", "item", "section", "taxonomy", "summary", "extraction_status")}
                                for d in merged[side + "_ids"]]
        result.append(merged)
    return result


def grouping_summary(rows):
    groups = [row["grouping"] for row in rows if "grouping" in row]
    source_rows = sum(len(group["source_alignments"]) for group in groups)
    return {"policy": GROUPING_POLICY, "consolidated_groups": len(groups),
            "source_alignment_rows": source_rows, "rows_combined": source_rows - len(groups)}
