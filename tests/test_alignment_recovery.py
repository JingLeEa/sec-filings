"""Saved verifier recovery retains earlier decisions and original job scope."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sec_disclosure.agents.alignment_repair import RepairSource, VerificationRepair
from sec_disclosure.llm.disclosures import write_json


IDS = [f"x_{year}_7_D{number:03d}" for year in (2023, 2024) for number in (1, 2)]


def fake_data():
    records = {key: {"item": "7", "fiscal_year": key.split("_")[1]} for key in IDS}

    def item_for(keys):
        items = {records[key]["item"] for key in keys}
        if len(items) != 1:
            raise ValueError("Cross-Item evidence")
        return next(iter(items))

    return SimpleNamespace(records=records, hashes={}, years=("2023", "2024"), item_for=item_for)


def proposal(before, after):
    return {"previous_ids": [before], "current_ids": [after], "explanation": "Same topic.",
            "needs_review": False, "review_reason": "",
            "evidence": [{"disclosure_id": key, "sentence_ids": [key + "_S001"]} for key in (before, after)]}


def action(*rows):
    return {"action": "finalize", "alignments": list(rows)}


def request(folder, name, payload, result, *, finish_reason="stop", job_id="verification_001"):
    path = folder / "requests" / f"{name}.json"
    write_json(path, {"agent": "verification", "status": "completed", "job_id": job_id,
                      "started_at": name, "prompt": json.dumps(payload),
                      "result": {"text": json.dumps(result), "finish_reason": finish_reason}})
    return path.resolve()


@pytest.fixture
def source(tmp_path):
    folder = tmp_path / "source"
    groups = [{"previous_ids": [IDS[0]], "current_ids": [IDS[2]]},
              {"previous_ids": [IDS[1]], "current_ids": [IDS[3]]}]
    original = {"item": "7", "previous_year": "2023", "current_year": "2024",
                "required_ids": IDS, "records": [{"disclosure_id": key} for key in IDS],
                "proposed_groups": groups, "history": [], "remaining_turns_including_this_one": 4}
    first, second = action(proposal(IDS[0], IDS[2])), action(proposal(IDS[1], IDS[3]))
    correction = {**original, "repair_mode": True, "required_ids": [IDS[1], IDS[3]],
                  "records": [{"disclosure_id": key} for key in (IDS[1], IDS[3])],
                  "history": [], "remaining_turns_including_this_one": 3}
    first_path = request(folder, "001", original, first)
    second_path = request(folder, "002", correction, second)
    write_json(folder / "manifest.json", {"comparison_scope": "same_item", "input_hashes": {}})
    write_json(folder / "candidate_matches.json", {})
    write_json(folder / "matching_proposals.json", [])
    write_json(folder / "proposed_groups.json", groups)
    write_json(folder / "token_usage.json", {"reported_tokens": {"total_tokens": 0}})
    write_json(folder / "jobs/verification_001.json", {"groups": groups, "result": [*first["alignments"], *second["alignments"]]})
    write_json(folder / "traces/verification_001.json", [
        {"action": first, "pending_disclosure_ids": [IDS[1], IDS[3]]},
        {"action": second, "pending_disclosure_ids": []},
    ])
    return SimpleNamespace(folder=folder, data=fake_data(), groups=groups, original=original,
                           first=first, second=second, first_path=first_path, second_path=second_path)


def test_recovery_retains_every_accepted_turn_after_history_reset(source):
    imported = RepairSource(source.folder, source.data)
    (job,) = imported.jobs
    assert set(job["payload"]["required_ids"]) == set(IDS)
    assert "history" not in job["payload"]
    assert "remaining_turns_including_this_one" not in job["payload"]
    assert [entry["action"] for entry in job["imported_actions"]] == [source.first, source.second]
    assert [entry["source_request"] for entry in job["imported_actions"]] == [str(source.first_path), str(source.second_path)]

    def validate(event, visible, required, data):
        return [{**deepcopy(event["alignments"][0]), "status": "ai_verified", "review_reasons": []}]

    repair = VerificationRepair(source.data, set(IDS), validate, lambda group, reason: group)
    for entry in job["imported_actions"]:
        repair.receive(entry["action"], entry["visible"])
    assert not repair.pending
    assert len(repair.accepted) == 2
    assert imported.saved_results["verification_001"] == [*source.first["alignments"], *source.second["alignments"]]


def test_last_tool_action_does_not_lose_earlier_accepted_finalization(source):
    trace_path = source.folder / "traces/verification_001.json"
    trace = json.loads(trace_path.read_text())
    trace.pop()
    trace.append({"action": {"action": "context", "disclosure_ids": [IDS[1]]}, "observation": {"context_results": []}})
    write_json(trace_path, trace)
    imported = RepairSource(source.folder, source.data)
    assert [entry["action"] for entry in imported.jobs[0]["imported_actions"]] == [source.first]


def test_truncated_responses_are_not_imported_as_valid_decisions(source):
    write_json(source.folder / "traces/verification_001.json", [{"validation_error": "Response did not finish normally."}])
    original = json.loads(source.first_path.read_text())
    original["result"]["finish_reason"] = "length"
    write_json(source.first_path, original)
    imported = RepairSource(source.folder, source.data)
    assert imported.jobs[0]["imported_actions"] == []
    assert set(imported.jobs[0]["payload"]["required_ids"]) == set(IDS)


def test_forged_finalization_without_exact_saved_response_is_rejected(source):
    trace = json.loads((source.folder / "traces/verification_001.json").read_text())
    trace[-1]["action"]["alignments"][0]["explanation"] = "Not present in any saved response."
    write_json(source.folder / "traces/verification_001.json", trace)
    with pytest.raises(ValueError, match="No exact saved verifier request"):
        RepairSource(source.folder, source.data)


def test_recovery_rejects_unknown_disclosure_visibility(source):
    payload = json.loads(source.first_path.read_text())
    prompt = json.loads(payload["prompt"])
    prompt["records"].append({"disclosure_id": "invented"})
    payload["prompt"] = json.dumps(prompt)
    write_json(source.first_path, payload)
    with pytest.raises(ValueError, match="Invalid saved verifier scope"):
        RepairSource(source.folder, source.data)


def test_repeat_recovery_uses_exact_imported_source_references_without_new_requests(source, tmp_path):
    imported = RepairSource(source.folder, source.data)
    recovered = tmp_path / "recovered"
    for name in ("manifest.json", "candidate_matches.json", "matching_proposals.json", "proposed_groups.json",
                 "token_usage.json", "jobs/verification_001.json"):
        target = recovered / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source.folder / name).read_bytes())
    trace = [{"action": entry["action"], "source_request": entry["source_request"], "cached_seed": True,
              "visible_disclosure_ids": sorted(entry["visible"])} for entry in imported.jobs[0]["imported_actions"]]
    write_json(recovered / "traces/verification_001.json", trace)
    repeated = RepairSource(recovered, source.data)
    assert [entry["action"] for entry in repeated.jobs[0]["imported_actions"]] == [source.first, source.second]
    assert set(repeated.jobs[0]["payload"]["required_ids"]) == set(IDS)


def test_missing_optional_report_files_keep_job_recovery_available(source):
    imported = RepairSource(source.folder, source.data)
    assert imported.report_rows == []
    row = {"match_id": "x_2023_2024_M0001", "change_analysis": {"llm": "Modified"}}
    write_json(source.folder / "needs_review.json", {"alignments": [row]})
    assert RepairSource(source.folder, source.data).report_rows == [row]


def test_scope_from_last_correction_cannot_replace_original_scope(source):
    source.first_path.unlink()
    with pytest.raises(ValueError, match="No full-scope saved verifier request"):
        RepairSource(source.folder, source.data)


def test_context_visibility_exposes_only_saved_eligible_disclosures():
    observed = RepairSource._observation_visible({"context_results": [
        {"disclosure_id": "x_2024_7_P001_AUDIT", "eligible_disclosures": [{"disclosure_id": IDS[0]}]}]})
    assert observed == {IDS[0]}
