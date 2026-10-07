"""Align two years of saved disclosures with bounded matching and verification agents."""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from sec_disclosure.llm.client import LLMError
from sec_disclosure.llm.config import load_config
from sec_disclosure.llm.disclosures import SOURCE_UNIT_POLICY, digest, write_json
from sec_disclosure.llm.request_pacing import RequestPacer
from .alignment_data import AlignmentData
from .alignment_exact import EXACT_POLICY, accepted_status, exact_matches
from .alignment_prompts import MATCHING_PROMPT, VERIFICATION_PROMPT
from .alignment_runtime import RunLimit, Runtime
from .alignment_graph import orchestration_manifest
from .alignment_workflow import AlignmentWorkflow
from .alignment_repair import ProposalError, RepairSource, VerificationRepair
from .alignment_grouping import ASSIGNMENT_POLICY, GROUPING_POLICY, consolidate_alignments, grouping_summary, supported_match


MANIFEST_VERSION = "1"
REPORT_VERSION = "8"
FINAL_ALIGNMENT_STATUSES = ("ai_verified", "auto_matched", "unmatched")
LEGACY_RESULT_FILES = ("ai_verified.json", "unmatched.json", "review_candidates.json",
                       "unmatched_previous.json", "unmatched_current.json")
ITEM_SCOPE_NOTE = (
    "Comparisons are restricted to the same SEC Item; one-to-many and many-to-one matches are allowed within an Item. "
    "Evidence-supported overlapping links with one shared disclosure are consolidated; original decisions are retained in grouping.source_alignments. "
    "Other supported matched groups may share disclosures while retaining their original links; overlap alone does not require review. "
)
REPORT_NOTE = (
    "Auto-matched means complete original text equality after bullet/whitespace normalization, with no LLM call for that link. "
    "Mixed exact/LLM groups retain each link's method in grouping.source_alignments. "
    "AI-verified means model review plus deterministic source checks, not human verification. "
    "Unmatched means a validated verifier decision found no counterpart in the comparison's saved disclosures, "
    "not proof of filing-wide absence. Current-only unmatched disclosures are introduced_disclosure; "
    "previous-only unmatched disclosures are removed_disclosure. Unmatched decisions remain needs_review until the run completes. "
    "Invalid, unfinished or conflicting decisions remain needs_review. "
    "Scope is saved narrative disclosures, not all tables or the entire filing."
)


def ids(value, allowed, name, *, empty=True):
    if (not isinstance(value, list) or (not empty and not value) or
            any(not isinstance(key, str) or key not in allowed for key in value) or
            len(value) != len(set(value))):
        raise ValueError(f"{name} must contain unique allowed IDs.")
    return value


def validate_matches(action, visible, anchors, data):
    if action.get("action") != "propose" or not isinstance(action.get("matches"), list):
        raise ValueError("Matching agent must return action:propose with matches.")
    previous = {key for key in visible if data.records[key]["fiscal_year"] == data.years[0]}
    seen, results = set(), []
    for row in action["matches"]:
        if not isinstance(row, dict):
            raise ValueError("Every matches entry must be an object, not a string or ID.")
        current = row.get("current_id")
        if current not in anchors or current in seen:
            raise ValueError("Every current anchor must occur exactly once.")
        selected = ids(row.get("previous_ids"), previous, "previous_ids")
        data.item_for([current, *selected])
        if not isinstance(row.get("rationale"), str) or not row["rationale"].strip():
            raise ValueError("A specific matching rationale is required.")
        results.append({"current_id": current, "previous_ids": selected, "rationale": row["rationale"]})
        seen.add(current)
    if seen != set(anchors):
        raise ValueError("Matching output omitted an anchor.")
    return results


def connected_groups(data, matches, covered_ids=()):
    adjacency = {key: set() for key in data.records}
    for row in matches:
        for previous in row["previous_ids"]:
            data.item_for([row["current_id"], previous])
            adjacency[row["current_id"]].add(previous)
            adjacency[previous].add(row["current_id"])
    seen, result = set(), []
    for key in sorted(adjacency):
        if key in seen:
            continue
        if key in covered_ids and not adjacency[key]:
            continue  # Already covered by an exact link; never invent an unmatched job.
        pending, group = [key], set()
        while pending:
            node = pending.pop()
            if node in group:
                continue
            group.add(node)
            pending.extend(adjacency[node] - group)
        seen.update(group)
        previous = sorted(k for k in group if data.records[k]["fiscal_year"] == data.years[0])
        current = sorted(group - set(previous))
        result.append({"group_id": f"group_{len(result) + 1:04d}", "previous_ids": previous, "current_ids": current})
    return result


def relationship(previous, current):
    if not previous:
        return "current_only"
    if not current:
        return "previous_only"
    return ("one" if len(previous) == 1 else "many") + "_to_" + ("one" if len(current) == 1 else "many")


def validate_alignments(action, visible, required, data):
    if action.get("action") != "finalize" or not isinstance(action.get("alignments"), list):
        raise ValueError("Verifier must return action:finalize with alignments.")
    previous = {key for key in visible if data.records[key]["fiscal_year"] == data.years[0]}
    current = set(visible) - previous
    seen, results = set(), []
    for row in action["alignments"]:
        if not isinstance(row, dict):
            raise ValueError("Every alignments entry must be an object.")
        before = ids(row.get("previous_ids"), previous, "previous_ids")
        after = ids(row.get("current_ids"), current, "current_ids")
        members = set(before + after)
        if not members or not members & required:
            raise ValueError("Rows must be nonempty and include a required disclosure.")
        if len({data.records[key]["item"] for key in members}) != 1:
            raise ProposalError("All disclosures in an alignment must belong to the same SEC Item.",
                                "previous_ids/current_ids", {key: data.records[key]["item"] for key in sorted(members)},
                                "cross_item_alignment")
        review = row.get("needs_review")
        if type(review) is not bool:
            raise ValueError("needs_review must be a boolean.")
        if not isinstance(row.get("explanation"), str) or not row["explanation"].strip():
            raise ValueError("A concrete alignment explanation is required.")
        if not isinstance(row.get("review_reason", ""), str) or (review and not row.get("review_reason", "").strip()):
            raise ValueError("A review reason is required when needs_review is true.")
        evidence, cited = [], set()
        if not isinstance(row.get("evidence"), list):
            raise ValueError("Every alignment requires source sentence references.")
        for citation in row["evidence"]:
            if not isinstance(citation, dict):
                raise ValueError("Each evidence entry must be an object with disclosure_id and sentence_ids.")
            key = citation.get("disclosure_id")
            if key not in members or key in cited:
                raise ValueError("Evidence must identify each row member exactly once.")
            record = data.records[key]
            sentence_ids = ids(citation.get("sentence_ids"), record["sentence_map"], "sentence_ids", empty=False)
            source_by_id = {sentence["sentence_id"]: source for source in record["sources"] for sentence in source["sentences"]}
            evidence.append({"disclosure_id": key, "sentences": [
                {"sentence_id": sid, "paragraph_id": source_by_id[sid]["paragraph_id"],
                 "source_url": source_by_id[sid]["source_url"], "text": record["sentence_map"][sid]}
                for sid in sentence_ids]})
            cited.add(key)
        if cited != members:
            raise ValueError("Cite at least one selected sentence for EVERY disclosure in the alignment.")
        flags = []
        if review:
            flags.append("verifier_requested_review")
        if not before or not after:
            flags.append("absence_not_proven_by_retrieval")
        if any(data.records[key]["extraction_status"] == "needs_review" for key in members):
            flags.append("input_extraction_needs_review")
        results.append({"previous_ids": before, "current_ids": after,
                        "relationship": relationship(before, after),
                        "explanation": row["explanation"], "evidence": evidence,
                        "status": "needs_review" if flags else "ai_verified",
                        "review_reasons": flags, "verifier_review_reason": row.get("review_reason", ""),
                        "match_method": "llm"})
        seen.update(members)
    if not required <= seen:
        raise ValueError("Verification omitted required disclosures.")
    return results


def review_row(group, reason):
    return {"previous_ids": group["previous_ids"], "current_ids": group["current_ids"],
            "relationship": relationship(group["previous_ids"], group["current_ids"]),
            "explanation": reason, "evidence": [], "status": "needs_review",
            "review_reasons": ["agent_did_not_finish"], "verifier_review_reason": reason}


def validate_verification_response(action, visible, required, data):
    """One-response validation; the runtime retains this state for corrections."""
    repair = VerificationRepair(data, required, validate_alignments, review_row)
    repair.receive(action, visible)
    return repair.results()


def validate_residual_alignments(action, visible, required, data, exact_ids):
    """An established exact match cannot also be declared unmatched by an agent."""
    rows = validate_alignments(action, visible, required, data)
    for row in rows:
        if (not row["previous_ids"] or not row["current_ids"]) and exact_ids.intersection(row["previous_ids"] + row["current_ids"]):
            raise ProposalError("This disclosure already has an established exact-text counterpart. Return only additional links or an unmatched decision for the remaining required disclosure.",
                                "previous_ids/current_ids", row["previous_ids"] + row["current_ids"], "contradicts_exact_match")
    return rows


def batches(values, size):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def verification_jobs(groups, data):
    """Keep whole groups and pack small ones within a single SEC Item."""
    pending, size, item = [], 0, None
    for group in groups:
        group_item = data.item_for(group["previous_ids"] + group["current_ids"])
        length = sum(len(json.dumps(data.view(key, full=True))) for key in group["previous_ids"] + group["current_ids"])
        if pending and (group_item != item or len(pending) >= 4 or size + length > 26000):
            yield pending
            pending, size = [], 0
        pending.append(group)
        item = group_item
        size += length
    if pending:
        yield pending


def save_report(output, data, candidates, matches, groups, decisions, runtime, *, complete, error=None):
    rows = consolidate_alignments(apply_extraction_policy_to_alignments(decisions, data), data)
    for row in rows:
        data.item_for(row["previous_ids"] + row["current_ids"])
    represented = {key for row in rows for key in row["previous_ids"] + row["current_ids"]}
    pending = sorted(set(data.records) - represented)
    for key in pending:
        previous = [key] if data.records[key]["fiscal_year"] == data.years[0] else []
        rows.append(review_row({"previous_ids": previous, "current_ids": [] if previous else [key]},
                               "Alignment has not completed for this disclosure."))
    owners, conflicts = mark_assignment_conflicts(rows, data)
    rows.sort(key=lambda row: (min(row["current_ids"] or row["previous_ids"]), tuple(row["previous_ids"])))
    finalized = []
    for i, row in enumerate(rows, 1):
        details = {side + "_disclosures": [
            {key: data.records[d][key] for key in ("disclosure_id", "item", "section", "taxonomy", "summary", "extraction_status")}
            for d in row[side + "_ids"]] for side in ("previous", "current")}
        finalized.append({"match_id": f"{data.ticker}_{data.years[0]}_{data.years[1]}_M{i:04d}", **row, **details})
    counts = Counter(row["status"] for row in finalized)
    report = {"schema_version": REPORT_VERSION, "company": data.ticker,
              "comparison_scope": "same_item",
              "previous_year": data.years[0], "current_year": data.years[1],
              "run_complete": complete, "error": error,
              "note": ITEM_SCOPE_NOTE + REPORT_NOTE,
              "grouping_summary": grouping_summary(rows),
              "assignment_policy": ASSIGNMENT_POLICY,
              "source_unit_policy": deepcopy(SOURCE_UNIT_POLICY),
              "coverage": {"input_disclosures": len(data.records), "represented_disclosures": len(owners),
                           "pending_disclosure_ids": pending, "conflicting_disclosure_ids": sorted(conflicts),
                           "permitted_overlap_disclosure_ids": sorted(key for key, indices in owners.items()
                                                                      if len(indices) > 1 and key not in conflicts)},
              "counts": dict(counts),
              "alignments": finalized}
    exact_path = output / "automatic_matches.json"
    if exact_path.exists():
        automatic = json.loads(exact_path.read_text())
        report["automatic_matching"] = {"policy": automatic["policy"], "exact_pairs": len(automatic["alignments"]),
                                        "api_requests": 0, "api_tokens": 0,
                                        "source_warning_pairs": sum(bool(row["review_reasons"]) for row in automatic["alignments"])}
    write_json(output / "candidate_matches.json", candidates)
    write_json(output / "matching_proposals.json", matches)
    write_json(output / "proposed_groups.json", groups)
    usage = runtime.report()
    usage.update(run_complete=complete, company=data.ticker, previous_year=data.years[0], current_year=data.years[1],
                 extraction_tokens_by_year=data.extraction_usage)
    write_json(output / "token_usage.json", usage)
    validation_jobs = [json.loads(p.read_text()) for p in sorted((output / "validation_errors").glob("*.json"))]
    write_json(output / "validation_errors.json", {"schema_version": REPORT_VERSION, "company": data.ticker,
               "previous_year": data.years[0], "current_year": data.years[1], "jobs": validation_jobs})
    write_alignment_reports(output, report, usage)
    return report


def mark_assignment_conflicts(rows, data):
    """Allow source-validated matched overlaps; keep contradictory/invalid reuse flagged.

    Do not merge a many-to-many chain into a larger Cartesian product: the
    original rows define the exact proposed links. Only the obsolete conflict
    flag may be cleared; other extraction, metadata and verifier concerns stay.
    """
    owners = defaultdict(list)
    for index, row in enumerate(rows):
        for key in row["previous_ids"] + row["current_ids"]:
            owners[key].append(index)
    supported = [supported_match(row, data) for row in rows]
    conflicts = {key: indices for key, indices in owners.items()
                 if len(indices) > 1 and not all(supported[index] for index in indices)}
    affected = {index for indices in conflicts.values() for index in indices}
    for index, row in enumerate(rows):
        if index not in affected and supported[index] and "conflicting_alignment_assignment" in row["review_reasons"]:
            row["review_reasons"] = [flag for flag in row["review_reasons"] if flag != "conflicting_alignment_assignment"]
            if not row["review_reasons"]:
                row["status"] = accepted_status(row)
    for indices in conflicts.values():
        for index in indices:
            rows[index]["status"] = "needs_review"
            rows[index]["review_reasons"] = sorted(set(rows[index]["review_reasons"] + ["conflicting_alignment_assignment"]))
    return owners, conflicts


def apply_extraction_policy_to_alignments(decisions, data):
    """Refresh only extraction flags affected by the waived source-selection rules."""
    rows = deepcopy(decisions)
    for row in rows:
        members = row["previous_ids"] + row["current_ids"]
        if not any(data.records[key]["verification"].get("ignored_review_reasons") for key in members):
            continue
        for side in ("previous", "current"):
            for detail in row.get(side + "_disclosures", []):
                detail["extraction_status"] = data.records[detail["disclosure_id"]]["extraction_status"]
        if any(data.records[key]["extraction_status"] == "needs_review" for key in members):
            continue
        both_sides = bool(row["previous_ids"] and row["current_ids"])
        # Metadata policy changes must not promote a match with bad citations.
        if both_sides and not supported_match(row, data):
            continue
        for field in ("review_reasons", "unmatched_notes"):
            if field in row:
                row[field] = [flag for flag in row[field] if flag != "input_extraction_needs_review"]
        if both_sides and row["status"] == "needs_review" and not row["review_reasons"]:
            row["status"] = accepted_status(row)
    return rows


def load_alignment_report(output):
    """Read all decisions from the two final files, or a legacy combined report."""
    report = json.loads((output / "alignments.json").read_text())
    if "included_statuses" not in report:
        return report
    review = json.loads((output / "needs_review.json").read_text())
    identity = ("schema_version", "company", "comparison_scope", "previous_year", "current_year",
                "run_complete", "error", "overall_counts")
    if any(report[key] != review[key] for key in identity):
        raise ValueError("alignments.json and needs_review.json must belong to the same saved report.")
    if (report["included_statuses"] != list(FINAL_ALIGNMENT_STATUSES)
            or review["included_statuses"] != ["needs_review"]
            or any(row["status"] not in FINAL_ALIGNMENT_STATUSES for row in report["alignments"])
            or any(row["status"] != "needs_review" for row in review["alignments"])):
        raise ValueError("Saved alignment files contain statuses outside their declared partition.")
    rows = sorted(report["alignments"] + review["alignments"], key=lambda row: row["match_id"])
    if len({row["match_id"] for row in rows}) != len(rows):
        raise ValueError("Saved alignment files contain duplicate match IDs.")
    report["alignments"] = rows
    report["counts"] = dict(Counter(row["status"] for row in rows))
    for key in ("included_statuses", "alignment_count", "overall_counts"):
        report.pop(key)
    return report


def regroup_saved_report(output, data, *, extraction_policy_only=False):
    """Refresh a saved same-Item report locally, preserving IDs, caches and usage."""
    path = output / "alignments.json"
    original_bytes = path.read_bytes()
    original = load_alignment_report(output)
    manifest = json.loads((output / "manifest.json").read_text())
    if original.get("comparison_scope") != "same_item" or manifest.get("comparison_scope") != "same_item":
        raise ValueError("Regrouping requires a saved same-Item run.")
    if (original["company"], original["previous_year"], original["current_year"]) != (data.ticker, *data.years):
        raise ValueError("Saved report belongs to a different company or comparison.")
    canonical = lambda values: {str(Path(k).resolve()): v for k, v in values.items()}
    if canonical(manifest["input_hashes"]) != canonical(data.hashes):
        raise ValueError("Saved report inputs have changed; cannot regroup stale evidence.")
    for row in original["alignments"]:
        data.item_for(row["previous_ids"] + row["current_ids"])
        for side, year in (("previous", data.years[0]), ("current", data.years[1])):
            if any(data.records[key]["fiscal_year"] != year for key in row[side + "_ids"]):
                raise ValueError("Saved report contains a disclosure on the wrong year side.")
    report = deepcopy(original)
    rows = apply_extraction_policy_to_alignments(original["alignments"], data)
    report["alignments"] = sorted(rows if extraction_policy_only else consolidate_alignments(rows, data),
                                  key=lambda row: row["match_id"])
    report["source_unit_policy"] = deepcopy(SOURCE_UNIT_POLICY)
    owners, conflicts = mark_assignment_conflicts(report["alignments"], data)
    report["assignment_policy"] = ASSIGNMENT_POLICY
    report["coverage"].update(represented_disclosures=len(owners), conflicting_disclosure_ids=sorted(conflicts),
                              permitted_overlap_disclosure_ids=sorted(key for key, indices in owners.items()
                                                                     if len(indices) > 1 and key not in conflicts))
    report["grouping_summary"] = grouping_summary(report["alignments"])
    refresh_report_statuses(report)
    if report == original and not any((output / name).exists() for name in LEGACY_RESULT_FILES):
        return report
    usage = json.loads((output / "token_usage.json").read_text())
    operation = "extraction_policy" if extraction_policy_only else "overlap_grouping"
    review_path = output / "needs_review.json"
    saved_bytes = original_bytes + (review_path.read_bytes() if review_path.exists() else b"")
    archive = output / "archives" / (f"before_{operation}_" + digest(saved_bytes)[:16])
    archive.mkdir(parents=True, exist_ok=False)
    for filename in ("alignments.json", "alignments.md", "ai_verified.json", "unmatched.json", "needs_review.json",
                     "review_candidates.json", "unmatched_previous.json",
                     "unmatched_current.json", "run_summary.json", "regrouping_summary.json", "extraction_policy_summary.json"):
        if (output / filename).exists():
            shutil.copy2(output / filename, archive / filename)
    write_alignment_reports(output, report, usage)
    summary = {"policy": GROUPING_POLICY, "assignment_policy": ASSIGNMENT_POLICY,
               "additional_api_requests": 0, "additional_api_tokens": 0,
               "archive": str(archive), "counts_before": original["counts"], "counts_after": report["counts"],
               "alignment_rows_before": len(original["alignments"]), "alignment_rows_after": len(report["alignments"]),
               "grouping_summary": report["grouping_summary"],
               "review_reasons_after": dict(Counter(flag for row in report["alignments"] for flag in row["review_reasons"])),
               "groups": [{"match_id": row["match_id"], "previous_ids": row["previous_ids"], "current_ids": row["current_ids"],
                           "source_match_ids": [source.get("match_id") for source in row["grouping"]["source_alignments"]],
                           "status": row["status"], "review_reasons": row["review_reasons"]}
                          for row in report["alignments"] if "grouping" in row]}
    if extraction_policy_only:
        before_by_id = {row["match_id"]: row for row in original["alignments"]}
        summary = {key: value for key, value in summary.items() if key not in {"policy", "groups"}}
        summary["source_unit_policy"] = deepcopy(SOURCE_UNIT_POLICY)
        summary["source_disclosures_with_waived_warnings"] = [
            {"disclosure_id": key, "extraction_status": row["extraction_status"],
             "ignored_review_reasons": row["verification"]["ignored_review_reasons"],
             "remaining_review_reasons": row["verification"]["review_reasons"]}
            for key, row in sorted(data.records.items()) if row["verification"].get("ignored_review_reasons")]
        summary["changed_alignments"] = [
            {"match_id": row["match_id"], "status_before": before_by_id[row["match_id"]]["status"],
             "status_after": row["status"], "review_reasons": row["review_reasons"]}
            for row in report["alignments"] if row != before_by_id[row["match_id"]]]
    write_json(output / ("extraction_policy_summary.json" if extraction_policy_only else "regrouping_summary.json"), summary)
    summary_path = output / "run_summary.json"
    if summary_path.exists():
        run_summary = json.loads(summary_path.read_text())
        by_item = defaultdict(Counter)
        for row in report["alignments"]:
            by_item[data.item_for(row["previous_ids"] + row["current_ids"])][row["status"]] += 1
        run_summary.update(alignment_rows=len(report["alignments"]), counts=report["counts"],
                           relationship_counts=dict(Counter(row["relationship"] for row in report["alignments"])),
                           counts_by_item=dict(by_item), review_reasons_overlapping=summary["review_reasons_after"],
                           conflicting_disclosure_ids=len(conflicts), grouping_summary=report["grouping_summary"],
                           assignment_policy=ASSIGNMENT_POLICY,
                           permitted_overlap_disclosure_ids=report["coverage"]["permitted_overlap_disclosure_ids"],
                           source_unit_policy=deepcopy(SOURCE_UNIT_POLICY),
                           exact_cited_sentences_checked=sum(len(e["sentences"]) for row in report["alignments"] for e in row["evidence"]))
        write_json(summary_path, run_summary)
    return report


def empty_change_analysis():
    return {"status": "not_started", "lexical": None, "semantic": None,
            "llm": None, "final_taxonomy": None}


def refresh_report_statuses(report):
    """Classify supported single-sided decisions after global checks, including legacy reports.

    This only changes reporting. Keep the verifier's uncertainty and extraction
    flags as unmatched notes; do not reinterpret failed verification as absence.
    """
    owners = Counter(key for row in report["alignments"] for key in row["previous_ids"] + row["current_ids"])
    # These overlaps were checked against authoritative source evidence by
    # mark_assignment_conflicts. A reporting-only refresh cannot certify new ones.
    permitted = (set(report["coverage"].get("permitted_overlap_disclosure_ids", []))
                 if report.get("assignment_policy") == ASSIGNMENT_POLICY else set())
    one_sided = {key for row in report["alignments"] if not row["previous_ids"] or not row["current_ids"]
                 for key in row["previous_ids"] + row["current_ids"]}
    conflicts = set(report["coverage"]["conflicting_disclosure_ids"]) | {
        key for key, count in owners.items() if count > 1 and (key not in permitted or key in one_sided)}
    report["coverage"]["conflicting_disclosure_ids"] = sorted(conflicts)
    unresolved_ids = set(report["coverage"]["pending_disclosure_ids"]) | conflicts
    blocked_reasons = {"agent_did_not_finish", "invalid_or_omitted_verifier_proposal", "conflicting_alignment_assignment"}
    for row in report["alignments"]:
        row.setdefault("change_analysis", empty_change_analysis())
        before, after = row["previous_ids"], row["current_ids"]
        members = set(before + after)
        flags = list(dict.fromkeys(row["review_reasons"] + row.pop("unmatched_notes", [])))
        if members & conflicts:
            if "conflicting_alignment_assignment" not in flags:
                flags.append("conflicting_alignment_assignment")
            row["status"] = "needs_review"
        cited = {entry["disclosure_id"] for entry in row["evidence"] if entry.get("sentences")}
        supported_unmatched = (
            report["run_complete"] and bool(before) != bool(after) and "absence_not_proven_by_retrieval" in flags
            and not set(flags) & blocked_reasons and not members & unresolved_ids
            and cited == members
        )
        if supported_unmatched:
            row["status"] = "unmatched"
            row["unmatched_type"] = "removed_disclosure" if before else "introduced_disclosure"
            row["unmatched_notes"] = flags
            row["review_reasons"] = []
        else:
            row.pop("unmatched_type", None)
            if row["status"] == "unmatched":
                row["status"] = "needs_review"
            row["review_reasons"] = flags
    report["schema_version"] = REPORT_VERSION
    report["note"] = (ITEM_SCOPE_NOTE if report.get("comparison_scope") == "same_item" else "") + REPORT_NOTE
    report["counts"] = dict(Counter(row["status"] for row in report["alignments"]))


def write_alignment_json_reports(output, report):
    """Partition all decisions into exactly two final result files, without model calls."""
    refresh_report_statuses(report)
    unknown = set(report["counts"]) - {"auto_matched", "ai_verified", "unmatched", "needs_review"}
    if unknown:
        raise ValueError(f"Cannot export unknown alignment statuses: {sorted(unknown)}.")
    previous_path = output / "alignments.json"
    if previous_path.exists():
        previous = {row["match_id"]: row for row in load_alignment_report(output)["alignments"]}
        def without_analysis(row):
            decision = {key: value for key, value in row.items() if key != "change_analysis"}
            # Cached jobs write partial reports before the final completion check.
            # Moving a supported absence decision between the two files must
            # not erase analysis when its underlying decision is unchanged.
            if bool(row["previous_ids"]) != bool(row["current_ids"]):
                flags = list(dict.fromkeys(row["review_reasons"] + row.get("unmatched_notes", [])))
                blocked = {"agent_did_not_finish", "invalid_or_omitted_verifier_proposal", "conflicting_alignment_assignment"}
                members = set(row["previous_ids"] + row["current_ids"])
                cited = {entry["disclosure_id"] for entry in row["evidence"] if entry.get("sentences")}
                if "absence_not_proven_by_retrieval" in flags and not set(flags) & blocked and cited == members:
                    decision.update(status="unmatched", review_reasons=flags)
                    decision.pop("unmatched_notes", None)
                    decision.pop("unmatched_type", None)
            return decision
        for row in report["alignments"]:
            old = previous.get(row["match_id"])
            # Cached alignment reruns must not erase later classification work.
            # A changed group, evidence or decision must not inherit stale labels.
            if (old and row["change_analysis"] == empty_change_analysis()
                    and without_analysis(row) == without_analysis(old) and "change_analysis" in old):
                row["change_analysis"] = deepcopy(old["change_analysis"])
    for filename, statuses in (("alignments.json", list(FINAL_ALIGNMENT_STATUSES)),
                               ("needs_review.json", ["needs_review"])):
        rows = [row for row in report["alignments"] if row["status"] in statuses]
        export = {key: value for key, value in report.items() if key != "alignments"}
        export.update(comparison_scope=report.get("comparison_scope", "legacy_all_items"),
                      included_statuses=statuses, alignment_count=len(rows),
                      counts=dict(Counter(row["status"] for row in rows)), overall_counts=report["counts"],
                      alignments=rows)
        write_json(output / filename, export)
    legacy_paths = [output / name for name in LEGACY_RESULT_FILES if (output / name).exists()]
    if legacy_paths:
        fingerprint = digest(b"".join(path.name.encode() + b"\0" + path.read_bytes() for path in legacy_paths))[:16]
        archive = output / "archives" / f"legacy_result_views_{fingerprint}"
        archive.mkdir(parents=True, exist_ok=True)
        for path in legacy_paths:
            path.rename(archive / path.name)


def write_alignment_reports(output, report, usage):
    """Render saved alignment decisions without making model calls or revalidating them."""
    write_alignment_json_reports(output, report)
    counts = Counter(report["counts"])
    lines = [f"# {report['company'].upper()} {report['previous_year']}–{report['current_year']} disclosure alignment", "", report["note"], "",
             f"Run complete: {report['run_complete']}. Auto-matched: {counts['auto_matched']}; AI-verified: {counts['ai_verified']}; unmatched: {counts['unmatched']}; needs review: {counts['needs_review']}.", "",
             f"Alignment API tokens: {usage['reported_tokens']['total_tokens']:,}. Unknown-usage requests: {usage['requests_with_unknown_usage']}.", ""]
    for row in report["alignments"]:
        lines.extend([f"## {row['match_id']}", "", f"**{row['relationship']} · {row['status']}**", "",
                      "Previous: " + (", ".join(row["previous_ids"]) or "No counterpart found"), "",
                      "Current: " + (", ".join(row["current_ids"]) or "No counterpart found"), "", row["explanation"], ""])
        if row["review_reasons"]:
            lines.extend(["Review: " + "; ".join(row["review_reasons"]) + ". " + row["verifier_review_reason"], ""])
        if row.get("unmatched_notes"):
            lines.extend(["Unmatched notes: " + "; ".join(row["unmatched_notes"]) + ". " + row["verifier_review_reason"], ""])
        if row.get("grouping"):
            sources = row["grouping"]["source_alignments"]
            identifiers = [source["match_id"] for source in sources if "match_id" in source]
            lines.extend([f"Grouped from {len(sources)} supported proposals with a shared disclosure. " +
                          ("Original match IDs: " + ", ".join(identifiers) + "." if identifiers else ""), ""])
        for evidence in row["evidence"]:
            for sentence in evidence["sentences"]:
                lines.extend([f"**{sentence['sentence_id']}**", "", sentence["text"], ""])
    (output / "alignments.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv=None, *, request_pacer: RequestPacer | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--disclosures-dir", type=Path,
                        help="Alternative disclosure root containing ticker/year directories; raw evidence still comes from --data-dir/raw.")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=6)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent jobs within each matching/verification phase (default: 1).")
    parser.add_argument("--request-interval", type=float, help="Shared seconds between API request starts (default: 2 with multiple workers, otherwise 0).")
    parser.add_argument("--rate-limit-cooldown", type=float, default=60, help="Shared HTTP 429 cooldown in seconds (default: 60).")
    parser.add_argument("--max-steps", type=int, default=4, help="Agent turns per job, including tool requests/corrections.")
    parser.add_argument("--max-requests", type=int, default=400, help="Total paid requests allowed for this pair, including prior attempts; -1 removes the cap.")
    parser.add_argument("--max-new-requests", type=int, help="Pause after this many NEW requests; rerun to resume.")
    parser.add_argument("--max-total-tokens", type=int, default=1500000, help="Total alignment token budget; -1 removes the cap (default: 1500000).")
    parser.add_argument("--max-tokens", type=int, default=6000, help="Maximum completion tokens per request.")
    parser.add_argument("--max-prompt-chars", type=int, default=150000)
    parser.add_argument("--timeout", type=float, default=180, help="Total deadline in seconds for each API attempt, including response reads (default: 180).")
    parser.add_argument("--fault-tolerant", action="store_true", help="Automatically retry temporary API failures and resume recoverable failed/interrupted requests.")
    parser.add_argument("--max-retries", type=int, default=10, help="Additional attempts per API call with --fault-tolerant; -1 allows unlimited retries (default: 10).")
    parser.add_argument("--retry-backoff", type=float, default=5, help="Initial retry delay in seconds; doubles up to 60 seconds. HTTP 429 uses the shared cooldown (default: 5).")
    parser.add_argument("--retry-failed", action="store_true",
                        help="Retry prior failures; reserve estimated tokens for their unknown usage within the total cap.")
    parser.add_argument("--repair-from", type=Path, help="Reuse a saved run's matching and verifier responses in a separate output directory.")
    parser.add_argument("--offline-repair", action="store_true", help="With --repair-from, apply local metadata repair without API calls; leave remaining corrections pending.")
    parser.add_argument("--revalidate-cache", action="store_true",
                        help="After an implementation change, archive job decisions and replay exact cached API responses through current validation. Inputs/settings must be unchanged; new prompts still cost tokens.")
    parser.add_argument("--regroup-only", action="store_true", help="Consolidate supported overlaps in saved same-Item reports; archive reports and make no API calls.")
    parser.add_argument("--refresh-extraction-policy", action="store_true",
                        help="Apply the current source-selection review policy to saved same-Item reports, without API calls or changing historical extraction files.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if (args.regroup_only or args.refresh_extraction_policy) and (args.repair_from or args.offline_repair or args.revalidate_cache or args.dry_run
                                                              or args.regroup_only and args.refresh_extraction_policy):
        parser.error("Choose one offline report refresh; do not combine it with repair, cache revalidation, or --dry-run.")
    if args.offline_repair and not args.repair_from:
        parser.error("--offline-repair requires --repair-from.")
    ticker, years = args.ticker.lower(), (args.previous_year, args.current_year)
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ticker) or any(not re.fullmatch(r"\d{4}", year) for year in years) or years[0] >= years[1]:
        parser.error("Use a valid ticker and increasing four-digit fiscal years.")
    limits = (args.top_k, args.batch_size, args.max_steps, args.max_tokens, args.max_prompt_chars, args.workers)
    if (any(n < 1 for n in limits) or any(n != -1 and n < 1 for n in (args.max_requests, args.max_total_tokens))
            or args.max_new_requests is not None and args.max_new_requests < 1 or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("Limits and timeout must be positive; request/token caps also accept -1 for unlimited.")
    if args.max_retries < -1:
        parser.error("--max-retries must be nonnegative or -1 for unlimited.")
    args.request_interval = args.request_interval if args.request_interval is not None else (2.0 if args.workers > 1 else 0.0)
    if any(not math.isfinite(value) or value < 0 for value in (args.request_interval, args.rate_limit_cooldown, args.retry_backoff)):
        parser.error("Request interval, rate-limit cooldown and retry backoff must be finite and nonnegative.")
    output = args.output_dir or args.data_dir / "alignments" / ticker / f"{years[0]}-{years[1]}"
    if args.repair_from and (output.resolve() == args.repair_from.resolve() or args.repair_from.resolve() in output.resolve().parents):
        parser.error("Repair output must be a separate directory outside --repair-from.")
    runtime = None
    candidates, matches, groups, decisions, automatic = {}, [], [], [], []
    complete, error, exit_code = False, None, 0
    try:
        data = AlignmentData(args.data_dir, ticker, years, disclosures_dir=args.disclosures_dir)
        if args.regroup_only or args.refresh_extraction_policy:
            report = regroup_saved_report(output, data, extraction_policy_only=args.refresh_extraction_policy)
            print(f"Refreshed {output}: {report['counts']}; 0 additional API requests or tokens.", flush=True)
            return 0
        source = RepairSource(args.repair_from, data) if args.repair_from else None
        automatic = source.automatic if source else exact_matches(data)
        exact_ids = {key for row in automatic for key in row["previous_ids"] + row["current_ids"]}
        candidates = {key: hits for key, hits in data.index.candidates(*years, args.top_k).items() if key not in exact_ids}
        print(f"{ticker.upper()} {years[0]}–{years[1]}: " + json.dumps(dict(Counter(d['fiscal_year'] for d in data.records.values()))) + " disclosures (includes review candidates).", flush=True)
        print(f"Exact full-text matches: {len(automatic)} pairs; {len(candidates)} current disclosures remain for the LLM matching agent. No similarity threshold is used for automatic matches.", flush=True)
        print(f"Local retrieval: {sum(map(len, candidates.values()))} candidate edges; no API tokens.", flush=True)
        if args.dry_run:
            return 0
        config = load_config(args.env_file) if set(data.records) - exact_ids else None
        implementation = [Path(__file__), Path(__file__).with_name("alignment_data.py"),
                          Path(__file__).with_name("alignment_prompts.py"), Path(__file__).with_name("alignment_runtime.py"),
                          Path(__file__).with_name("alignment_repair.py"),
                          Path(__file__).with_name("alignment_graph.py"),
                          Path(__file__).with_name("alignment_workflow.py"),
                          Path(__file__).with_name("alignment_grouping.py"),
                          Path(__file__).with_name("alignment_exact.py"),
                          Path(__file__).parents[1] / "llm/disclosures.py",
                          Path(__file__).parents[1] / "llm/concurrency.py",
                          Path(__file__).parents[1] / "llm/request_pacing.py",
                          Path(__file__).parents[1] / "indexing/disclosure_retrieval.py"]
        manifest = {"schema_version": MANIFEST_VERSION, "input_hashes": data.hashes,
                    "orchestration": orchestration_manifest(),
                    "comparison_scope": "same_item",
                    "exact_matching_policy": EXACT_POLICY if not source or source.exact_policy else None,
                    "implementation_hashes": {p.name: digest(p.read_bytes()) for p in implementation},
                    "base_url": config.base_url if config else None, "model": config.model if config else None,
                    "settings": {key: getattr(args, key) for key in ("top_k", "batch_size", "max_steps", "max_tokens", "max_prompt_chars")}}
        if source:
            manifest["repair_source_hashes"] = source.hashes
        manifest_path = output / "manifest.json"
        if manifest_path.exists():
            previous_manifest = json.loads(manifest_path.read_text())
            if previous_manifest.get("comparison_scope") != "same_item":
                raise ValueError("This saved run predates same-Item-only comparison. Start a fresh run with a new --output-dir; legacy cross-Item jobs cannot be resumed or revalidated under this scope.")
            if previous_manifest != manifest:
                unchanged = lambda value: {key: item for key, item in value.items() if key not in {"implementation_hashes", "orchestration"}}
                if not args.revalidate_cache or unchanged(previous_manifest) != unchanged(manifest):
                    raise ValueError("Inputs, implementation, prompts or model changed. Use a new --output-dir, or --revalidate-cache for implementation-only changes with unchanged inputs/settings.")
                archive = output / "archives" / digest(manifest_path.read_bytes())[:16]
                if archive.exists():
                    raise ValueError("An archive already exists for this implementation; inspect it before revalidating again.")
                archive.mkdir(parents=True)
                manifest_path.rename(archive / "manifest.json")
                for directory in ("jobs", "traces", "validation_errors", "graph"):
                    if (output / directory).exists():
                        (output / directory).rename(archive / directory)
                print("Archived previous job decisions; replaying cached responses through current validation. Token ledger retained.", flush=True)
        write_json(manifest_path, manifest)
        write_json(output / "automatic_matches.json", {"policy": manifest["exact_matching_policy"], "alignments": automatic})
        runtime = Runtime(output, config, args, request_pacer=request_pacer)
        if source:
            runtime.source_usage = source.usage
            candidates, matches, groups = source.candidates, source.matches, source.groups
        workflow = AlignmentWorkflow(args, data, runtime, candidates, automatic, source)
        state = workflow.run()
        matches, groups, complete = state["matches"], state["groups"], state["complete"]
        if not complete:
            error, exit_code = "Offline repair complete; targeted model corrections remain pending.", 2
    except RunLimit as exc:
        error, exit_code = str(exc), 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError, LLMError) as exc:
        error, exit_code = str(exc), 1
    finally:
        if runtime is not None:
            # The coordinator can stop inside a graph node. Its portable job
            # artifacts contain the progress needed for the final partial report.
            if (output / "matching_proposals.json").exists():
                matches = json.loads((output / "matching_proposals.json").read_text())
            if (output / "proposed_groups.json").exists():
                groups = json.loads((output / "proposed_groups.json").read_text())
            decisions = checkpoint_decisions(output)
            report = save_report(output, data, candidates, matches, groups, decisions, runtime, complete=complete, error=error)
            print(f"Saved {output}: {report['counts']}; reported alignment tokens {runtime.report()['reported_tokens']['total_tokens']:,}.", flush=True)
    if error:
        print(f"Alignment stopped: {error}", file=sys.stderr)
    return exit_code


def checkpoint_decisions(output):
    """Include all saved jobs when a resumed repair pauses partway through."""
    automatic = output / "automatic_matches.json"
    rows = json.loads(automatic.read_text())["alignments"] if automatic.exists() else []
    for path in sorted((output / "jobs").glob("verification_*.json")):
        saved = json.loads(path.read_text())
        rows.extend(saved["result"] if saved["result"] is not None else [review_row(group, saved["error"]) for group in saved["groups"]])
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
