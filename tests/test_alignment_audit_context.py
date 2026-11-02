"""Audit paragraphs can be inspected without becoming extracted disclosures."""
from copy import deepcopy

import pytest

from sec_disclosure.agents.alignment_data import AlignmentData
from sec_disclosure.agents.disclosure_alignment import validate_alignments, validate_matches
from sec_disclosure.indexing.disclosure_retrieval import DisclosureIndex


@pytest.fixture
def evidence():
    data = AlignmentData.__new__(AlignmentData)
    data.years = ("2023", "2024")
    data.records, data.audit, data.paragraphs = {}, {}, {}
    for year in data.years:
        paragraphs = []
        for item in ("1", "1A"):
            pid = f"amd_{year}_{item}_P001"
            selected = f"Selected {item} competition disclosure."
            unselected = f"Ungrouped {item} Nvidia competition risk."
            sid = f"{pid}_S001"
            key = f"amd_{year}_{item}_D001"
            data.records[key] = {
                "disclosure_id": key, "fiscal_year": year, "item": item,
                "section": "Competition", "taxonomy": "Strategy & Business Model",
                "summary": "Competition disclosure", "extraction_status": "source_validated",
                "verification": {"review_reasons": []}, "sentence_map": {sid: selected},
                "content": selected, "sources": [{"paragraph_id": pid,
                    "source_url": "https://www.sec.gov/example.htm",
                    "sentences": [{"sentence_id": sid, "text": selected}]}],
            }
            paragraphs.append({"id": pid, "item": item, "text": selected + " " + unselected})
            audit_key = pid + "_AUDIT"
            data.audit[audit_key] = {
                "disclosure_id": audit_key, "paragraph_id": pid, "fiscal_year": year,
                "item": item, "content": unselected, "summary": "", "section": "Competition",
                "sentences": [{"sentence_id": f"{pid}_S002", "text": unselected}],
                "reasons": ["Not assigned to a disclosure"], "kind": "ungrouped_or_excluded_evidence",
            }
        data.paragraphs[year] = paragraphs
    data.index, data.audit_index = DisclosureIndex(data.records), DisclosureIndex(data.audit)
    return data


def test_search_marks_audit_hits_as_context_without_adding_them_to_visible(evidence):
    visible = set()
    result = evidence.tool({"action": "search", "queries": [{"year": "2024", "query": "Nvidia competition"}]},
                           visible, item="1A")
    (hit,) = result["search_results"][0]["audit_hits"]
    assert hit["record"]["context_only"] is True
    assert hit["record"]["valid_alignment_member"] is False
    assert visible == {"amd_2024_1A_D001"}
    assert not visible.intersection(evidence.audit)


@pytest.mark.parametrize("requested", ["amd_2024_1A_P001_AUDIT", "amd_2024_1A_P001"])
def test_exact_audit_or_paragraph_context_exposes_only_valid_selected_disclosures(evidence, requested):
    visible = set()
    (result,) = evidence.tool({"action": "context", "disclosure_ids": [requested]}, visible, item="1A")["context_results"]
    assert result["disclosure_id"] == "amd_2024_1A_P001_AUDIT"
    assert result["context_only"] is True and result["valid_alignment_member"] is False
    assert result["sentences"][0]["sentence_id"] == "amd_2024_1A_P001_S002"
    assert [d["disclosure_id"] for d in result["eligible_disclosures"]] == ["amd_2024_1A_D001"]
    assert result["eligible_disclosures"][0]["sentences"] == [
        {"sentence_id": "amd_2024_1A_P001_S001", "text": "Selected 1A competition disclosure."}]
    assert visible == {"amd_2024_1A_D001"}
    assert result["paragraph_context_only"] == [{
        "paragraph_id": "amd_2024_1A_P001", "text": "Selected 1A competition disclosure. Ungrouped 1A Nvidia competition risk."}]


def test_audit_context_with_no_selected_disclosure_reports_extraction_gap(evidence):
    del evidence.records["amd_2024_1A_D001"]
    visible = set()
    (result,) = evidence.tool({"action": "context", "disclosure_ids": ["amd_2024_1A_P001_AUDIT"]},
                             visible, item="1A")["context_results"]
    assert result["eligible_disclosures"] == [] and visible == set()
    assert "extraction gap" in result["guidance"]


def test_audit_context_cannot_cross_item_and_does_not_partially_expand_visibility(evidence):
    visible = set()
    with pytest.raises(ValueError, match="different SEC Item"):
        evidence.tool({"action": "context", "disclosure_ids": ["amd_2024_1A_P001_AUDIT", "amd_2024_1_P001_AUDIT"]},
                      visible, item="1A")
    assert visible == set()


def test_unknown_context_reports_exact_visible_disclosure_ids(evidence):
    visible = {"amd_2024_1A_D001"}
    with pytest.raises(ValueError, match="Unknown context ID") as failure:
        evidence.tool({"action": "context", "disclosure_ids": ["amd_2024_1A_P999"]}, visible, item="1A")
    assert "amd_2024_1A_D001" in str(failure.value)
    assert "exact saved audit ID" in str(failure.value)


def test_invisible_valid_disclosure_still_requires_search(evidence):
    with pytest.raises(ValueError, match="not visible.*Search"):
        evidence.tool({"action": "context", "disclosure_ids": ["amd_2024_1A_D001"]}, set(), item="1A")


def test_inspecting_audit_cannot_make_it_an_alignment_member(evidence):
    visible = {"amd_2024_1A_D001"}
    evidence.tool({"action": "context", "disclosure_ids": ["amd_2023_1A_P001_AUDIT"]}, visible, item="1A")
    with pytest.raises(ValueError, match="unique allowed IDs"):
        validate_matches({"action": "propose", "matches": [{
            "current_id": "amd_2024_1A_D001", "previous_ids": ["amd_2023_1A_P001_AUDIT"],
            "rationale": "The same source topic."}]}, visible, ["amd_2024_1A_D001"], evidence)
    assert visible.issubset(evidence.records)


def test_inspecting_audit_does_not_make_unselected_sentence_a_valid_citation(evidence):
    visible = {"amd_2024_1A_D001"}
    evidence.tool({"action": "context", "disclosure_ids": ["amd_2024_1A_P001_AUDIT"]}, visible, item="1A")
    with pytest.raises(ValueError, match="sentence_ids must contain unique allowed IDs"):
        validate_alignments({"action": "finalize", "alignments": [{
            "previous_ids": [], "current_ids": ["amd_2024_1A_D001"],
            "explanation": "No selected counterpart was found.", "needs_review": True,
            "review_reason": "Extraction gap", "evidence": [{
                "disclosure_id": "amd_2024_1A_D001", "sentence_ids": ["amd_2024_1A_P001_S002"]}]}]},
            visible, {"amd_2024_1A_D001"}, evidence)


def test_audit_context_returns_copies_not_mutable_source_records(evidence):
    original = deepcopy(evidence.audit)
    result = evidence.audit_context("amd_2024_1A_P001_AUDIT")
    result["sentences"][0]["text"] = "changed"
    result["reasons"].append("changed")
    assert evidence.audit == original


def audit_gap_proposal():
    return {"previous_ids": ["amd_2023_1A_D001"], "current_ids": [],
            "explanation": "The counterpart text exists only in ungrouped 2024 audit context.",
            "needs_review": True, "review_reason": "The counterpart was not extracted as a disclosure.",
            "evidence": [
                {"disclosure_id": "amd_2023_1A_D001", "sentence_ids": ["amd_2023_1A_P001_S001"]},
                {"disclosure_id": "amd_2024_1A_P001_AUDIT", "sentence_ids": ["amd_2024_1A_P001_S002"]}]}


def test_reviewed_audit_gap_is_context_and_never_certified_as_absence(evidence):
    from sec_disclosure.agents.alignment_repair import VerificationRepair
    from sec_disclosure.agents.disclosure_alignment import review_row, refresh_report_statuses
    required = {"amd_2023_1A_D001"}
    repair = VerificationRepair(evidence, required, validate_alignments, review_row)
    repair.receive({"action": "finalize", "alignments": [audit_gap_proposal()]}, set(evidence.records))
    assert not repair.pending
    (row,) = repair.results()
    assert [citation["disclosure_id"] for citation in row["evidence"]] == ["amd_2023_1A_D001"]
    assert row["audit_context"][0]["audit_id"] == "amd_2024_1A_P001_AUDIT"
    assert row["audit_context"][0]["context_only"] is True
    report = {"alignments": [row], "run_complete": True,
              "coverage": {"pending_disclosure_ids": [], "conflicting_disclosure_ids": []}}
    refresh_report_statuses(report)
    assert row["status"] == "needs_review"
    assert "audit_context_requires_review" in row["review_reasons"]
    assert "unmatched_type" not in row


@pytest.mark.parametrize("change", ["unknown_sentence", "no_review", "cross_item"])
def test_invalid_or_unacknowledged_audit_evidence_is_not_normalized(evidence, change):
    from sec_disclosure.agents.alignment_repair import VerificationRepair
    from sec_disclosure.agents.disclosure_alignment import review_row
    proposal = audit_gap_proposal()
    if change == "unknown_sentence":
        proposal["evidence"][1]["sentence_ids"] = ["invented_sentence"]
    elif change == "no_review":
        proposal.update(needs_review=False, review_reason="")
    else:
        proposal["evidence"][1] = {"disclosure_id": "amd_2024_1_P001_AUDIT", "sentence_ids": ["amd_2024_1_P001_S002"]}
    repair = VerificationRepair(evidence, {"amd_2023_1A_D001"}, validate_alignments, review_row)
    repair.receive({"action": "finalize", "alignments": [proposal]}, set(evidence.records))
    assert repair.pending == {"amd_2023_1A_D001"}
    assert not repair.accepted


def test_historical_audit_guess_does_not_shadow_a_later_valid_match(evidence):
    from sec_disclosure.agents.alignment_repair import VerificationRepair
    from sec_disclosure.agents.disclosure_alignment import review_row
    repair = VerificationRepair(evidence, {"amd_2023_1A_D001"}, validate_alignments, review_row)
    repair.allow_audit_context = False
    repair.receive({"action": "finalize", "alignments": [audit_gap_proposal()]}, set(evidence.records))
    assert repair.pending
    valid = audit_gap_proposal()
    valid.update(current_ids=["amd_2024_1A_D001"], needs_review=False, review_reason="")
    valid["evidence"][1] = {"disclosure_id": "amd_2024_1A_D001", "sentence_ids": ["amd_2024_1A_P001_S001"]}
    repair.receive({"action": "finalize", "alignments": [valid]}, set(evidence.records))
    repair.allow_audit_context = True
    repair.recover_audit_context(set(evidence.records))
    assert len(repair.accepted) == 1
    assert repair.accepted[0]["status"] == "ai_verified"
