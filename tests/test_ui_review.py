"""Manual label persistence, source identity and export checks."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from sec_ui import data, review

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def queue(monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(tmp_path / "no_disclosures"))
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "reviews"))
    records = data.load_comparison("amd", 2024, 2025)["records"]
    return [r for r in records if r["source_file"] == "needs_review.json"]


def test_saved_reviews_persist_keep_other_labels_and_leave_source_unchanged(queue):
    source = FIXTURES / "alignments/amd/2024-2025/needs_review.json"
    original = source.read_bytes()
    assert review.load_reviews("amd", 2024, 2025) == {}
    first = review.save_review(queue[0], "Modified", "  Human explanation.  ", expected=None)
    other = next(record for record in queue[1:] if not any(record[f"{side}_ids"] and not record[f"{side}_text"].strip()
                                                         for side in ("previous", "current")))
    review.save_review(other, "Expanded", "", expected=None)
    review.save_review(queue[0], "Reworded", "Updated explanation.", expected=first)
    saved = review.load_reviews("amd", 2024, 2025)
    assert len(saved) == 2
    assert saved[queue[0]["match_id"]]["change_label"] == "Reworded"
    assert first["notes"] == "Human explanation."
    assert review.load_reviews("amd", 2023, 2024) == {}
    assert source.read_bytes() == original


def test_reused_match_id_with_changed_source_needs_another_review(queue):
    record = queue[0]
    review.save_review(record, "Modified", "", expected=None)
    saved = review.load_reviews("amd", 2024, 2025)
    assert review.current_review(record, saved)
    changed = deepcopy(record)
    changed["current_text"] += " New filing wording."
    assert review.current_review(changed, saved) is None
    changed = deepcopy(record)
    changed["current_ids"].append("amd_2025_1A_D999")
    assert review.current_review(changed, saved) is None
    assert review.export_rows([changed], saved) == []


def test_stale_session_cannot_overwrite_a_newer_review(queue):
    first = review.save_review(queue[0], "Modified", "", expected=None)
    with pytest.raises(data.DataError, match="another session"):
        review.save_review(queue[0], "Unchanged", "", expected=None)
    assert review.load_reviews("amd", 2024, 2025)[queue[0]["match_id"]] == first


@pytest.mark.parametrize("label", [None, "", "Not labelled", "invalid"])
def test_invalid_labels_are_not_saved(queue, label):
    with pytest.raises(data.DataError, match="Choose a change label"):
        review.save_review(queue[0], label, "", expected=None)
    assert not review.review_path("amd", 2024, 2025).exists()


def test_completed_llm_labelled_records_can_be_overridden(queue):
    record = {**queue[0], "source_file": "alignments.json", "change_status": "completed", "change_label": "Unchanged"}
    saved = review.save_review(record, "Modified", "", expected=None)
    assert saved["label_source"] == "manual"
    assert saved["change_label"] == "Modified"


def test_invalid_saved_file_is_reported_and_never_overwritten(queue):
    path = review.review_path("amd", 2024, 2025)
    path.parent.mkdir(parents=True)
    path.write_text("{broken")
    with pytest.raises(data.DataError, match="Could not read"):
        review.save_review(queue[0], "Modified", "", expected=None)
    assert path.read_text() == "{broken"
    path.write_text(json.dumps({"schema_version": 2, "reviews": {}}))
    with pytest.raises(data.DataError, match="Invalid manual review"):
        review.load_reviews("amd", 2024, 2025)


def test_failed_save_keeps_previous_reviews(queue, monkeypatch):
    first = review.save_review(queue[0], "Modified", "", expected=None)
    original = review.review_path("amd", 2024, 2025).read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(data.DataError, match="Could not save"):
        review.save_review(queue[0], "Unchanged", "", expected=first)
    assert review.review_path("amd", 2024, 2025).read_bytes() == original
    assert not list(review.review_path("amd", 2024, 2025).parent.glob("*.tmp"))


def test_export_keeps_manual_and_original_labels_text_and_references(queue):
    review.save_review(queue[0], "Modified", "Human decision.", expected=None)
    saved = review.load_reviews("amd", 2024, 2025)
    (row,) = review.export_rows(queue, saved)
    assert row["manual_change_label"] == "Modified"
    assert row["manual_notes"] == "Human decision."
    assert row["change_label"] == row["final_change_label"] == "Modified"
    assert row["final_label_source"] == "manual"
    assert row["match_id"] == queue[0]["match_id"]
    assert row["previous_text"] and row["current_text"]
    assert row["sentence_ids"] and "sec.gov" in row["source_urls"]
    assert set(saved[queue[0]["match_id"]]["previous_ids"]) == set(queue[0]["previous_ids"])


def test_review_path_rejects_path_traversal(queue):
    with pytest.raises(data.DataError, match="Invalid company"):
        review.review_path("../other", 2024, 2025)


def test_final_change_defaults_to_llm_and_can_switch_back_after_override(queue):
    record = deepcopy(queue[0])
    record["methods"]["llm"] = "Modified"
    record.update(source_file="alignments.json", change_status="completed")
    assert review.final_change(record, {})["change_label"] == "Modified"
    saved = review.save_review(record, "Expanded", "Human correction.", expected=None)
    current = review.final_change(record, {record["match_id"]: saved})
    assert current["change_label"] == "Expanded" and current["final_label_source"] == "manual"
    saved = review.save_review(record, "Expanded", "Use the model again.", expected=saved, label_source="llm")
    current = review.final_change(record, {record["match_id"]: saved})
    assert current["change_label"] == "Modified" and current["final_label_source"] == "llm"
    assert current["manual_change_label"] == "Expanded"
    changed = {**record, "current_text": record["current_text"] + " Additional source text."}
    assert review.current_review(changed, {record["match_id"]: saved}) is None
    assert review.final_change(changed, {record["match_id"]: saved})["final_label_source"] == "llm"


def test_final_exports_include_unreviewed_llm_results(queue):
    record = deepcopy(queue[0])
    record["methods"]["llm"] = "Reworded"
    (row,) = review.export_rows([record], {}, include_unreviewed=True)
    assert row["change_label"] == row["llm_label"] == row["final_change_label"] == "Reworded"
    assert row["final_label_source"] == "llm" and row["manual_change_label"] is None
    assert row["reviewed_at"] == ""


def test_legacy_manual_review_keeps_its_selected_label(queue):
    record = queue[0]
    saved = review.save_review(record, "Reduced", "Legacy review.", expected=None)
    for field in ("label_source", "manual_change_label", "llm_change_label"):
        saved.pop(field)
    path = review.review_path("amd", 2024, 2025)
    payload = json.loads(path.read_text())
    payload["reviews"][record["match_id"]] = saved
    path.write_text(json.dumps(payload))
    resolved = review.final_change(record, review.load_reviews("amd", 2024, 2025))
    assert resolved["change_label"] == "Reduced" and resolved["final_label_source"] == "manual"


def test_invalid_final_source_and_missing_llm_cannot_be_saved(queue):
    with pytest.raises(data.DataError, match="LLM or Manual"):
        review.save_review(queue[0], "Expanded", "", expected=None, label_source="semantic")
    record = deepcopy(queue[0])
    record["methods"]["llm"] = None
    with pytest.raises(data.DataError, match="Choose a change label"):
        review.save_review(record, "Expanded", "", expected=None, label_source="llm")


def test_selecting_llm_after_source_changes_does_not_revive_stale_manual_label(queue):
    record = deepcopy(queue[0])
    record["methods"]["llm"] = "Modified"
    saved = review.save_review(record, "Expanded", "Old source.", expected=None)
    changed = {**record, "current_text": record["current_text"] + " New evidence."}
    refreshed = review.save_review(changed, None, "New source.", expected=saved, label_source="llm")
    assert refreshed["manual_change_label"] is None
    assert refreshed["change_label"] == "Modified"


def test_ai_approved_requires_a_completed_label_without_active_review_flags(queue):
    record = deepcopy(queue[0])
    record.update(source_file="alignments.json", status="ai_verified", change_status="completed")
    record["methods"]["llm"] = "Modified"
    assert review.ai_approved_change(record)
    assert not review.ai_approved_change({**record, "source_file": "needs_review.json"})
    assert not review.ai_approved_change({**record, "change_status": "needs_review"})
    assert not review.ai_approved_change({**record, "change_status": "not_started"})
    assert not review.ai_approved_change({**record, "status": "needs_review"})
    record["methods"]["llm"] = None
    assert not review.ai_approved_change(record)
