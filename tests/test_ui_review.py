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
    review.save_review(queue[1], "Expanded", "", expected=None)
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


def test_completed_labelled_records_cannot_be_labelled_from_review_queue(queue):
    record = {**queue[0], "source_file": "alignments.json", "change_status": "completed", "change_label": "Unchanged"}
    with pytest.raises(data.DataError, match="needs_review.json"):
        review.save_review(record, "New", "", expected=None)


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
    assert row["change_label"] == queue[0]["change_label"]
    assert row["match_id"] == queue[0]["match_id"]
    assert row["previous_text"] and row["current_text"]
    assert row["sentence_ids"] and "sec.gov" in row["source_urls"]
    assert set(saved[queue[0]["match_id"]]["previous_ids"]) == set(queue[0]["previous_ids"])


def test_review_path_rejects_path_traversal(queue):
    with pytest.raises(data.DataError, match="Invalid company"):
        review.review_path("../other", 2024, 2025)
