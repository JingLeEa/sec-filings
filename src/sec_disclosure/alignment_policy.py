"""Exclude completed-run review cases that have no counterpart to compare."""
from collections import Counter
from copy import deepcopy

EXCLUSION_POLICY = "unresolved_one_sided_review_v1"


def unreviewable_alignment(row):
    return row.get("status") == "needs_review" and not (row.get("previous_ids") and row.get("current_ids"))


def exclude_unreviewable(report):
    """Mutate a final report, retaining exclusion IDs for coverage and audit.

    Partial results still need their one-sided proposals for recovery. Supported
    absence decisions must first be promoted to `unmatched` by reporting checks.
    """
    if not report.get("run_complete"):
        return []
    removed = [row for row in report["alignments"] if unreviewable_alignment(row)]
    if not removed:
        return []
    report["alignments"] = [row for row in report["alignments"] if not unreviewable_alignment(row)]
    report["alignment_exclusion_policy"] = EXCLUSION_POLICY
    excluded = {(row.get("match_id"), tuple(row["previous_ids"]), tuple(row["current_ids"])): row
                for row in report.get("excluded_alignments", [])}
    for row in removed:
        entry = {key: row.get(key, []) for key in ("match_id", "previous_ids", "current_ids")}
        entry["reason"] = "unresolved_alignment_without_counterpart"
        excluded[(entry["match_id"], tuple(entry["previous_ids"]), tuple(entry["current_ids"]))] = entry
    report["excluded_alignments"] = list(excluded.values())
    report["counts"] = dict(Counter(row["status"] for row in report["alignments"]))
    members = {key for row in report["alignments"] for side in ("previous", "current") for key in row[side + "_ids"]}
    excluded_ids = {key for row in excluded.values() for side in ("previous", "current") for key in row[side + "_ids"]} - members
    coverage = report.setdefault("coverage", {})
    coverage.update(represented_disclosures=len(members), excluded_disclosure_ids=sorted(excluded_ids))
    for key in ("pending_disclosure_ids", "conflicting_disclosure_ids", "permitted_overlap_disclosure_ids"):
        if key in coverage:
            coverage[key] = [value for value in coverage[key] if value not in excluded_ids]
    return removed


def exclude_from_reports(reports):
    """Clean both canonical partitions together so totals and metadata agree."""
    combined = deepcopy(reports["alignments.json"])
    combined["alignments"] = [row for report in reports.values() for row in report["alignments"]]
    removed = exclude_unreviewable(combined)
    if not removed:
        return []
    excluded_ids = {row["match_id"] for row in removed}
    for report in reports.values():
        report["alignments"] = [row for row in report["alignments"] if row["match_id"] not in excluded_ids]
        report["alignment_count"] = len(report["alignments"])
        report["counts"] = dict(Counter(row["status"] for row in report["alignments"]))
        report["overall_counts"] = combined["counts"]
        for key in ("coverage", "alignment_exclusion_policy", "excluded_alignments"):
            report[key] = deepcopy(combined[key])
    return removed
