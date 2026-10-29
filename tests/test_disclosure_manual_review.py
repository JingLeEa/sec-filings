from copy import deepcopy
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sec_disclosure.llm.disclosures import apply_source_unit_policy
from sec_ui import config, data, disclosure_data, review

ROOT = Path(__file__).resolve().parents[1]


def disclosure(number, flags, year=2024):
    disclosure_id = f"amd_{year}_1A_D{number:03d}"
    paragraph_id = f"amd_{year}_1A_P{number:03d}"
    text = f"Disclosure {number} describes supplier dependencies."
    return {"disclosure_id": disclosure_id, "company": "amd", "fiscal_year": year, "item": "1A",
            "section": "Supply chain", "summary": f"Supplier dependency {number}.",
            "taxonomy": "Supply Chain & Third Parties", "content": text,
            "sources": [{"paragraph_id": paragraph_id, "selected_text": text,
                         "source_url": "https://www.sec.gov/example.htm",
                         "sentences": [{"sentence_id": paragraph_id+"_S001", "text": text}]}],
            "verification": {"status": "needs_review", "source_unit_count": 1, "review_reasons": flags,
                             "model_review_reason": "Confirm the disclosure type." if "model_requested_review" in flags else ""}}


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    root = tmp_path / "disclosures"
    records = [disclosure(901, ["possible_missing_table_context"]),
               disclosure(902, ["model_requested_review"]),
               disclosure(903, ["possible_missing_table_context", "model_requested_review"])]
    for year, rows in [(2024, records), (2023, [disclosure(904, ["model_requested_review"], 2023)])]:
        folder = root / "amd" / str(year)
        folder.mkdir(parents=True)
        (folder / "disclosures.json").write_text(json.dumps({"disclosures": []}))
        (folder / "review_candidates.json").write_text(json.dumps({"disclosures": rows}))
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(root))
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(ROOT / "tests/fixtures/alignments"))
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "reviews"))
    st.cache_data.clear()
    yield records
    st.cache_data.clear()


def app():
    at = AppTest.from_file(str(ROOT / "app/pages/5_Manual_Review.py"), default_timeout=60).run()
    return at.radio(key="manual_section").set_value("Disclosure").run()


def click(at, text):
    return next(button for button in at.button if button.label == text).click().run()


def test_table_warning_is_retired_but_explicit_human_review_survives():
    original = {"status": "needs_review", "source_unit_count": 1,
                "review_reasons": ["possible_missing_table_context"], "model_review_reason": ""}
    assert apply_source_unit_policy(original)["status"] == "source_validated"
    both = {**original, "review_reasons": ["possible_missing_table_context", "model_requested_review"],
            "model_review_reason": "Confirm classification."}
    actual = apply_source_unit_policy(both)
    assert actual["status"] == "needs_review"
    assert actual["review_reasons"] == ["model_requested_review"]
    assert actual["model_review_reason"] == "Confirm classification."
    assert apply_source_unit_policy(actual) == actual
    assert original["review_reasons"] == ["possible_missing_table_context"]
    assert apply_source_unit_policy({**original, "source_unit_count": 0})["status"] == "needs_review"


def test_disclosure_queue_does_not_require_alignment_and_excludes_table_only_flags(inputs, monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path / "no_alignment"))
    source = disclosure_data.discover_source()
    assert source.years("amd") == [2023, 2024]
    records = disclosure_data.load_disclosures("amd", 2024, source)
    assert [row["disclosure_id"] for row in records] == [inputs[1]["disclosure_id"], inputs[2]["disclosure_id"]]
    assert all(row["verification"]["review_reasons"] == ["model_requested_review"] for row in records)
    at = app()
    assert not at.exception and not at.error
    assert {m.label: m.value for m in at.metric} == {"Review queue": "2", "Reviewed": "0", "Pending": "2"}


def test_disclosure_type_validation_persistence_editing_and_stage_isolation(inputs):
    at = app()
    assert at.selectbox(key="disclosure_type").value is None
    click(at, "Save review")
    assert any("Choose a disclosure type" in error.value for error in at.error)
    at.selectbox(key="disclosure_type").select("Operations & Capacity")
    at.text_area(key="disclosure_notes").input("Human-confirmed operations topic.")
    click(at, "Save review")
    assert not at.exception and not at.error
    assert next(metric.value for metric in at.metric if metric.label == "Reviewed") == "1"
    fresh = app()
    assert fresh.selectbox(key="disclosure_type").value == "Operations & Capacity"
    assert fresh.text_area(key="disclosure_notes").value == "Human-confirmed operations topic."
    fresh.selectbox(key="disclosure_type").select("Supply Chain & Third Parties")
    click(fresh, "Save review")
    fresh.radio(key="manual_section").set_value("Alignment").run()
    assert not fresh.exception
    assert fresh.selectbox(key="manual_label").value is None
    assert review.load_reviews("amd", 2023, 2024) == {}


def test_disclosure_save_next_and_year_change_do_not_copy_labels(inputs):
    at = app()
    first = at.selectbox(key="disclosure_selected").value
    at.selectbox(key="disclosure_type").select(config.CONTENT_CATEGORIES[0])
    click(at, "Save and next")
    assert not at.exception
    assert at.selectbox(key="disclosure_selected").value != first
    assert at.selectbox(key="disclosure_type").value is None
    at.sidebar.selectbox(key="sel_disclosure_year").select(2023).run()
    assert not at.exception
    assert at.selectbox(key="disclosure_type").value is None
    assert next(metric.value for metric in at.metric if metric.label == "Reviewed") == "0"


def test_disclosure_reviews_keep_source_identity_reject_conflicts_and_export(inputs):
    row = disclosure_data.load_disclosures("amd", 2024)[0]
    saved = review.save_disclosure_review(row, config.CONTENT_CATEGORIES[0], "Human notes.", expected=None)
    with pytest.raises(data.DataError, match="another session"):
        review.save_disclosure_review(row, config.CONTENT_CATEGORIES[1], "", expected=None)
    reviews = review.load_disclosure_reviews("amd", 2024)
    assert review.current_disclosure_review(row, reviews) == saved
    changed = deepcopy(row)
    changed["content"] += " New selected source sentence."
    assert review.current_disclosure_review(changed, reviews) is None
    (export,) = review.export_disclosure_rows([row], reviews)
    assert export["manual_disclosure_type"] == config.CONTENT_CATEGORIES[0]
    assert export["original_disclosure_type"] == row["taxonomy"]
    assert export["paragraph_ids"] and export["sentence_ids"] and export["source_urls"]
    assert review.load_reviews("amd", 2023, 2024) == {}


def test_alignment_queue_accepts_uncertain_change_analysis_but_not_unstarted_stages(inputs):
    group = data.load_comparison("amd", 2024, 2025)["records"][0]
    group.update(source_file="alignments.json", change_status="not_started", change_label=data.NOT_LABELLED)
    assert not review.requires_change_review(group)
    group["change_status"] = "needs_review"
    assert review.requires_change_review(group)
    review.save_review(group, "Modified", "Human change analysis.", expected=None)
    assert review.current_review(group, review.load_reviews("amd", 2024, 2025))["change_label"] == "Modified"
