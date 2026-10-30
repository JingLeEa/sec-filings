from copy import deepcopy
import json
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

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
            "verification": {"status": "needs_review" if flags else "source_validated",
                             "source_unit_count": 1, "review_reasons": flags,
                             "model_review_reason": "Confirm the disclosure type." if "model_requested_review" in flags else ""}}


@pytest.fixture
def inputs(monkeypatch, tmp_path):
    root = tmp_path / "disclosures"
    records = [disclosure(901, ["possible_missing_table_context"]),
               disclosure(902, ["model_requested_review"]),
               disclosure(903, ["possible_missing_table_context", "model_requested_review"]),
               disclosure(905, [])]
    for year, rows in [(2024, records), (2023, [disclosure(904, ["model_requested_review"], 2023)])]:
        folder = root / "amd" / str(year)
        folder.mkdir(parents=True)
        accepted = [row for row in rows if row["verification"]["status"] == "source_validated"]
        pending = [row for row in rows if row["verification"]["status"] == "needs_review"]
        (folder / "disclosures.json").write_text(json.dumps({"disclosures": accepted}))
        (folder / "review_candidates.json").write_text(json.dumps({"disclosures": pending}))
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(root))
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(ROOT / "tests/fixtures/alignments"))
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "reviews"))
    monkeypatch.setenv("SEC_UI_REFRESH_AUTOSTART", "0")
    st.cache_data.clear()
    yield records
    st.cache_data.clear()


def app():
    at = AppTest.from_file(str(ROOT / "app/pages/5_Manual_Review.py"), default_timeout=60).run()
    return at.segmented_control(key="manual_section").set_value("Disclosure").run()


def click(at, text):
    return next(button for button in at.button if button.label == text).click().run()


def test_disclosure_queue_does_not_require_alignment_and_honors_pipeline_flags(inputs, monkeypatch, tmp_path):
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path / "no_alignment"))
    source = disclosure_data.discover_source()
    assert source.years("amd") == [2023, 2024]
    records = disclosure_data.load_disclosures("amd", 2024, source)
    assert [row["disclosure_id"] for row in records] == [row["disclosure_id"] for row in inputs[:3]]
    assert [row["verification"]["review_reasons"] for row in records] == [
        row["verification"]["review_reasons"] for row in inputs[:3]]
    at = app()
    assert not at.exception and not at.error
    assert {m.label: m.value for m in at.metric} == {"Review queue": "3", "Reviewed": "0", "Pending": "3"}


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
    fresh.segmented_control(key="manual_section").set_value("Alignment").run()
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


def test_validated_disclosures_are_editable_and_can_return_to_llm(inputs):
    records = disclosure_data.load_disclosures("amd", 2024, include_all=True)
    row = next(row for row in records if not row["needs_review"])
    assert row["disclosure_id"] == inputs[3]["disclosure_id"]
    default = review.final_disclosure(row, {})
    assert default["final_label_source"] == "llm" and default["final_disclosure_type"] == row["taxonomy"]
    saved = review.save_disclosure_review(row, "Operations & Capacity", "Correction.", expected=None)
    assert review.final_disclosure(row, {row["disclosure_id"]: saved})["taxonomy"] == "Operations & Capacity"
    saved = review.save_disclosure_review(row, "Operations & Capacity", "Use LLM.", expected=saved, label_source="llm")
    final = review.final_disclosure(row, {row["disclosure_id"]: saved})
    assert final["taxonomy"] == row["taxonomy"] and final["final_label_source"] == "llm"
    assert final["manual_disclosure_type"] == "Operations & Capacity"
    rows = review.export_disclosure_rows(records, {row["disclosure_id"]: saved}, include_unreviewed=True)
    assert len(rows) == len(inputs)
    assert all(row["final_disclosure_type"] == row["llm_disclosure_type"] for row in rows)


def test_disclosure_ai_approved_scope_shows_type_and_manual_only_form(inputs):
    at = AppTest.from_file(str(ROOT / "app/pages/5_Manual_Review.py"), default_timeout=60).run()
    at.segmented_control(key="manual_section").set_value("Disclosure").run()
    assert at.segmented_control(key="review_scope").options == ["Needs review", "AI approved"]
    assert all(radio.label != "Final label source" for radio in at.radio)
    assert not at.selectbox(key="disclosure_type").disabled
    at.segmented_control(key="review_scope").select("AI approved").run()
    assert next(metric.value for metric in at.metric if metric.label == "Review queue") == "1"
    at.selectbox(key="disclosure_selected").select(inputs[3]["disclosure_id"]).run()
    ai_type = inputs[3]["taxonomy"]
    assert any(f"AI-approved disclosure type: **{ai_type}**" in message.value for message in at.success)
    assert any(f"AI type: {ai_type}" in label for label in at.selectbox(key="disclosure_selected").options)
    at.selectbox(key="disclosure_type").select("Operations & Capacity")
    click(at, "Save review")
    assert not at.exception and not at.error
    assert at.selectbox(key="disclosure_type").value == "Operations & Capacity"
    at.segmented_control(key="review_scope").select("Needs review").run()
    assert next(metric.value for metric in at.metric if metric.label == "Review queue") == "3"
    assert all(not label.startswith(inputs[3]["disclosure_id"].rsplit("_", 1)[-1] + " |")
               for label in at.selectbox(key="disclosure_selected").options)


def test_review_scope_stays_selected_across_sections_and_dashboard_pages(inputs):
    at = AppTest.from_file(str(ROOT / "app/Home.py"), default_timeout=60).run()
    at.switch_page("pages/5_Manual_Review.py").run()
    at.segmented_control(key="review_scope").set_value("AI approved").run()
    at.segmented_control(key="manual_section").set_value("Disclosure").run()
    assert not at.exception and not at.error
    assert at.segmented_control(key="review_scope").value == "AI approved"
    at.segmented_control(key="review_scope").set_value("Needs review").run()
    at.segmented_control(key="manual_section").set_value("Alignment").run()
    assert not at.exception and not at.error
    assert at.segmented_control(key="review_scope").value == "Needs review"
    at.segmented_control(key="review_scope").set_value("AI approved").run()
    at.switch_page("Home.py").run()
    at.switch_page("pages/5_Manual_Review.py").run()
    assert at.segmented_control(key="review_scope").value == "AI approved"
    at.segmented_control(key="review_scope").set_value(None).run()
    at.segmented_control(key="manual_section").set_value(None).run()
    assert not at.exception and not at.error
    assert at.segmented_control(key="review_scope").value == "AI approved"
    assert at.segmented_control(key="manual_section").value == "Alignment"


def test_manual_disclosure_type_reaches_dashboard_categories(inputs):
    row = disclosure_data.load_disclosures("amd", 2024)[0]
    review.save_disclosure_review(row, "Operations & Capacity", "Correction.", expected=None)
    group = data.normalise({"match_id": "test_M001", "previous_ids": [row["disclosure_id"]],
                           "current_ids": [], "change_analysis": {"llm": "Removed"}},
                          {row["disclosure_id"]: row}, "amd", 2024, 2025)
    source = {"meta": {"company": "amd", "previous_year": 2024, "current_year": 2025}, "records": [group]}
    (final,) = review.apply_final_choices(source)["records"]
    assert final["category"] == "Operations & Capacity"
    assert final["previous_members"][0]["llm_disclosure_type"] == row["taxonomy"]
    assert source["records"][0]["category"] == row["taxonomy"]


def test_selecting_llm_for_changed_disclosure_does_not_revive_stale_manual_type(inputs):
    row = disclosure_data.load_disclosures("amd", 2024)[0]
    saved = review.save_disclosure_review(row, "Operations & Capacity", "Old source.", expected=None)
    changed = {**row, "content": row["content"] + " New evidence."}
    refreshed = review.save_disclosure_review(changed, None, "New source.", expected=saved, label_source="llm")
    assert refreshed["manual_disclosure_type"] is None
    assert refreshed["disclosure_type"] == row["taxonomy"]


def test_review_save_queues_refresh_and_ui_can_retry_failed_work(inputs):
    from sec_ui import refresh
    from sec_disclosure.llm.disclosures import write_json
    jobs = refresh.backend()
    at = app()
    at.selectbox(key="disclosure_type").select("Operations & Capacity")
    click(at, "Save review")
    (job,) = jobs.jobs(refresh.queue_dir())
    assert job["stage"] == "disclosure" and job["status"] == "queued"
    assert job["review"]["disclosure_type"] == "Operations & Capacity"
    job.update(status="failed", phase="Refresh stopped; review is saved", error="Temporary failure")
    write_json(jobs.job_path(refresh.queue_dir(), job["job_id"]), job)
    at.run()
    assert any("Temporary failure" in message.value for message in at.warning)
    click(at, "Retry pipeline refresh")
    assert jobs.jobs(refresh.queue_dir())[0]["status"] == "queued"
    job.update(status="complete", phase="Updated through materiality")
    write_json(jobs.job_path(refresh.queue_dir(), job["job_id"]), job)
    at.run()
    assert not at.exception and not at.error
    assert at.selectbox(key="disclosure_type").value == "Operations & Capacity"
    click(at, "Refresh downstream results")
    assert any(row["status"] == "queued" and row["job_id"] != job["job_id"] for row in jobs.jobs(refresh.queue_dir()))
