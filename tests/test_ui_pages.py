"""Run each dashboard page headlessly with Streamlit's AppTest and check what it shows."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
FIXTURES = Path(__file__).parent / "fixtures"
PAGES = ["Home.py", "pages/1_Change_Explorer.py", "pages/2_Side_by_Side.py",
         "pages/3_Synthesis_Report.py", "pages/4_Peer_Comparison.py", "pages/5_Manual_Review.py"]


@pytest.fixture(autouse=True)
def sample_environment(monkeypatch, tmp_path):
    import streamlit as st
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(FIXTURES / "no_disclosures_here"))
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(FIXTURES / "synthesis"))
    monkeypatch.setenv("SEC_UI_PEER_DIR", str(FIXTURES / "peer_comparison"))
    monkeypatch.setenv("SEC_UI_MANUAL_REVIEWS_DIR", str(tmp_path / "manual_reviews"))
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run(page: str) -> AppTest:
    return AppTest.from_file(str(APP / page), default_timeout=60).run()


def manual_review():
    return run("pages/5_Manual_Review.py")


def metrics(at):
    return {m.label: m.value for m in at.metric}


@pytest.mark.parametrize("page", PAGES)
def test_every_page_runs_without_errors(page):
    at = run(page)
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, [e.value for e in at.error]


def test_home_shows_counts():
    at = run("Home.py")
    shown = metrics(at)
    assert shown["Matched groups"] == "432"
    assert int(shown["Need review"]) == 45


def test_explorer_filter_narrows_the_table():
    at = run("pages/1_Change_Explorer.py")
    assert metrics(at)["Showing"] == "432 of 432"
    at.sidebar.multiselect(key="f_changes").select("Removed").run()
    assert not at.exception
    shown = int(metrics(at)["Showing"].split(" of ")[0])
    assert 0 < shown < 432


def test_explorer_filters_with_no_match_show_a_message():
    at = run("pages/1_Change_Explorer.py")
    at.sidebar.multiselect(key="f_changes").select("Removed").run()
    at.sidebar.multiselect(key="f_materiality").select("Not assessed").run()
    assert not at.exception
    # either rows remain or the friendly empty state is shown; never a crash
    assert at.info or int(metrics(at)["Showing"].split(" of ")[0]) > 0


def test_filters_persist_across_pages():
    at = run("Home.py")
    at.switch_page("pages/1_Change_Explorer.py").run()
    at.sidebar.multiselect(key="f_changes").select("Modified").run()
    chosen = at.sidebar.multiselect(key="f_changes").value
    assert chosen == ["Modified"]
    # AppTest keeps session state when the same instance moves to another page
    at.switch_page("pages/2_Side_by_Side.py").run()
    assert not at.exception
    assert at.sidebar.multiselect(key="f_changes").value == ["Modified"]


def test_side_by_side_steps_through_records():
    at = run("pages/2_Side_by_Side.py")
    first = at.selectbox(key="selected_match").value
    next(b for b in at.button if b.label == "Next").click().run()
    assert not at.exception
    assert at.selectbox(key="selected_match").value != first


def test_side_by_side_marks_missing_counterpart():
    at = run("pages/2_Side_by_Side.py")
    at.sidebar.multiselect(key="f_relationships").select("previous_only").run()
    html = " ".join(m.value for m in at.markdown)
    assert "No counterpart found in the FY2025 filing." in html
    assert 'class="del"' in html


def test_synthesis_page_escapes_dollar_signs_and_links_evidence():
    at = run("pages/3_Synthesis_Report.py")
    text = " ".join(m.value for m in at.markdown)
    assert "\\$" in text
    assert "amd_2023_2024_M0269" in text
    assert metrics(at)["Citation check"] == "Passed"


def test_peer_page_shows_sample_banner_and_results():
    at = run("pages/4_Peer_Comparison.py")
    assert any("Sample data" in w.value for w in at.warning)
    shown = metrics(at)
    assert shown["Target disclosures"] == "2" and shown["Peer comparisons made"] == "3"


def test_no_data_gives_empty_states_not_crashes(monkeypatch, tmp_path):
    import streamlit as st
    empty = tmp_path / "empty"
    empty.mkdir()
    for var in ("SEC_UI_ALIGNMENTS_DIR", "SEC_UI_SYNTHESIS_DIRS", "SEC_UI_PEER_DIR"):
        monkeypatch.setenv(var, str(empty))
    st.cache_data.clear()
    for page in PAGES:
        at = run(page)
        assert not at.exception, (page, [e.value for e in at.exception])
    assert any("No comparison data" in e.value for e in run("Home.py").error)
    assert any("No synthesis output" in i.value for i in run("pages/3_Synthesis_Report.py").info)
    assert any("No peer comparison output" in i.value for i in run("pages/4_Peer_Comparison.py").info)


def test_broken_alignment_file_shows_error_message(monkeypatch, tmp_path):
    import streamlit as st
    folder = tmp_path / "amd" / "2024-2025"
    folder.mkdir(parents=True)
    (folder / "alignments.json").write_text("{broken")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path))
    st.cache_data.clear()
    at = run("pages/1_Change_Explorer.py")
    assert not at.exception
    assert any("Could not load this comparison" in e.value for e in at.error)


def test_manual_review_only_shows_needs_review_queue():
    at = manual_review()
    assert metrics(at) == {"Review queue": "45", "Reviewed": "0", "Pending": "45"}
    assert at.selectbox(key="manual_label").value is None
    html = " ".join(m.value for m in at.markdown)
    assert "FY2024 (earlier)" in html and "FY2025 (later)" in html
    assert 'class="diffbox"' in html


def test_ai_approved_scope_shows_labels_and_allows_manual_override_across_pages():
    from sec_ui import components, data, review
    at = run("Home.py")
    at.switch_page("pages/5_Manual_Review.py").run()
    assert at.segmented_control(key="review_scope").options == ["Needs review", "AI approved"]
    assert all(radio.label != "Final label source" for radio in at.radio)
    at.segmented_control(key="review_scope").select("AI approved").run()
    cached = components.cached_comparison("amd", 2024, 2025, "files")
    approved = [record for record in cached["records"] if review.ai_approved_change(record)]
    assert metrics(at)["Review queue"] == str(len(approved))
    record = next(record for record in approved if record["previous_text"] and record["current_text"])
    match_id, llm = record["match_id"], record["methods"]["llm"]
    at.selectbox(key="manual_selected").select(match_id).run()
    assert not at.selectbox(key="manual_label").disabled
    assert any(f"AI-approved change label: **{llm}**" in message.value for message in at.success)
    assert any(f"AI label: {llm}" in label for label in at.selectbox(key="manual_selected").options)
    manual = "Reduced" if llm != "Reduced" else "Expanded"
    at.selectbox(key="manual_label").select(manual)
    next(button for button in at.button if button.label == "Save review").click().run()
    assert not at.exception and not at.error
    (final,) = [row for row in data.load_comparison("amd", 2024, 2025)["records"] if row["match_id"] == match_id]
    assert final["change_label"] == manual
    assert next(row for row in cached["records"] if row["match_id"] == match_id)["change_label"] == llm
    at.switch_page("pages/2_Side_by_Side.py").run()
    at.selectbox(key="selected_match").select(match_id).run()
    assert any(f"Final label: {manual}" in caption.value for caption in at.caption)
    fresh = run("pages/5_Manual_Review.py")
    assert all(not label.startswith(match_id.rsplit("_", 1)[-1] + " |")
               for label in fresh.selectbox(key="manual_selected").options)
    fresh.segmented_control(key="review_scope").select("AI approved").run()
    fresh.selectbox(key="manual_selected").select(match_id).run()
    assert fresh.selectbox(key="manual_label").value == manual
    assert any(f"AI-approved change label: **{llm}**" in message.value for message in fresh.success)
    assert not fresh.exception and not fresh.error
    final = next(row for row in data.load_comparison("amd", 2024, 2025)["records"] if row["match_id"] == match_id)
    assert final["change_label"] == manual and final["final_label_source"] == "manual"


@pytest.mark.parametrize("page", ["pages/2_Side_by_Side.py", "pages/5_Manual_Review.py"])
def test_comparison_pages_show_disclosure_paragraph_and_sentence_sources(page):
    at = run(page)
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    assert 'class="sourcehead"' in html
    assert 'class="source-label">Disclosure</span>' in html
    frame = at.dataframe[0].value
    assert {"disclosure_id", "paragraph_id", "sentence_id", "text", "cited"} <= set(frame.columns)
    assert frame["disclosure_id"].notna().all()
    assert frame["sentence_id"].notna().all()
    assert frame["cited"].all()  # These fixtures expose cited sentences only.


def test_manual_review_requires_a_label():
    at = manual_review()
    next(b for b in at.button if b.label == "Save review").click().run()
    assert not at.exception
    assert any("Choose a change label" in e.value for e in at.error)
    assert metrics(at)["Reviewed"] == "0"


def test_manual_review_save_persists_and_can_be_edited():
    at = manual_review()
    match_id = at.selectbox(key="manual_selected").value
    at.selectbox(key="manual_label").select("Modified")
    at.text_area(key="manual_notes").input("Changed the impairment wording.")
    next(b for b in at.button if b.label == "Save review").click().run()
    assert not at.exception, [e.value for e in at.exception]
    assert metrics(at)["Reviewed"] == "1"
    assert any("Saved Modified" in s.value for s in at.success)
    # A fresh app session must read the label from disk, not another session's widgets.
    fresh = manual_review()
    assert fresh.selectbox(key="manual_selected").value == match_id
    assert fresh.selectbox(key="manual_label").value == "Modified"
    assert fresh.text_area(key="manual_notes").value == "Changed the impairment wording."
    fresh.selectbox(key="manual_label").select("Reworded")
    next(b for b in fresh.button if b.label == "Save review").click().run()
    assert not fresh.exception
    assert metrics(fresh)["Reviewed"] == "1"
    assert manual_review().selectbox(key="manual_label").value == "Reworded"


def test_manual_review_save_and_next_does_not_copy_labels():
    at = manual_review()
    first = at.selectbox(key="manual_selected").value
    at.selectbox(key="manual_label").select("Expanded")
    at.text_area(key="manual_notes").input("Only for the first disclosure.")
    next(b for b in at.button if b.label == "Save and next").click().run()
    assert not at.exception
    assert at.selectbox(key="manual_selected").value != first
    assert at.selectbox(key="manual_label").value is None
    assert at.text_area(key="manual_notes").value == ""
    assert metrics(at)["Pending"] == "44"
    at.sidebar.selectbox(key="manual_status").select("Reviewed").run()
    assert at.selectbox(key="manual_selected").value == first
    assert at.selectbox(key="manual_label").value == "Expanded"


def test_manual_review_year_switch_does_not_copy_labels():
    at = manual_review()
    at.selectbox(key="manual_label").select("Modified")
    next(b for b in at.button if b.label == "Save review").click().run()
    at.sidebar.selectbox(key="sel_pair").select("FY2023 to FY2024").run()
    assert not at.exception
    assert metrics(at)["Reviewed"] == "0"
    assert at.selectbox(key="manual_label").value is None


def test_manual_review_empty_filter_keeps_downloads():
    at = manual_review()
    at.sidebar.selectbox(key="manual_status").select("Reviewed").run()
    assert not at.exception
    assert any("No disclosures match" in i.value for i in at.info)
    assert len(at.get("download_button")) == 2


def test_manual_review_handles_missing_and_broken_review_file(monkeypatch, tmp_path):
    import json
    import streamlit as st
    folder = tmp_path / "alignments" / "amd" / "2024-2025"
    folder.mkdir(parents=True)
    main = json.loads((FIXTURES / "alignments/amd/2024-2025/alignments.json").read_text())
    (folder / "alignments.json").write_text(json.dumps(main))
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path / "alignments"))
    st.cache_data.clear()
    at = manual_review()
    assert not at.exception
    assert any("No alignment groups need review" in i.value for i in at.info)
    (folder / "needs_review.json").write_text("{broken")
    st.cache_data.clear()
    at = manual_review()
    assert not at.exception
    assert any("Could not load this comparison" in e.value for e in at.error)


def test_manual_review_finishing_pending_queue_keeps_exports(monkeypatch, tmp_path):
    import json
    import streamlit as st
    folder = tmp_path / "alignments" / "amd" / "2024-2025"
    folder.mkdir(parents=True)
    source = json.loads((FIXTURES / "alignments/amd/2024-2025/needs_review.json").read_text())
    (folder / "alignments.json").write_text(json.dumps({"alignments": []}))
    (folder / "needs_review.json").write_text(json.dumps({"alignments": source["alignments"][:1]}))
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(tmp_path / "alignments"))
    st.cache_data.clear()
    at = manual_review()
    at.sidebar.selectbox(key="manual_status").select("Pending").run()
    at.selectbox(key="manual_label").select("Reworded")
    next(b for b in at.button if b.label == "Save and next").click().run()
    assert not at.exception
    assert metrics(at)["Reviewed"] == "1" and metrics(at)["Pending"] == "0"
    assert any("All alignment groups" in s.value for s in at.success)
    assert len(at.get("download_button")) == 2


def test_manual_review_does_not_label_missing_source_text():
    from sec_ui import components
    at = manual_review()
    comparison = components.cached_comparison("amd", 2024, 2025, "files")
    missing = next(r for r in comparison["records"] if r["source_file"] == "needs_review.json"
                   and any(r[f"{side}_ids"] and not r[f"{side}_text"].strip() for side in ("previous", "current")))
    at.selectbox(key="manual_selected").select(missing["match_id"]).run()
    assert not at.exception
    assert any("Source text is missing" in e.value for e in at.error)
    assert all(b.disabled for b in at.button if b.label in {"Save review", "Save and next"})


def test_manual_review_conflict_can_be_resolved_with_reload():
    from sec_ui import components, review
    at = manual_review()
    match_id = at.selectbox(key="manual_selected").value
    comparison = components.cached_comparison("amd", 2024, 2025, "files")
    record = next(r for r in comparison["records"] if r["match_id"] == match_id)
    review.save_review(record, "Reworded", "Saved in another tab.", expected=None)
    at.selectbox(key="manual_label").select("Modified")
    next(b for b in at.button if b.label == "Save review").click().run()
    assert not at.exception
    assert any("another session" in e.value for e in at.error)
    next(b for b in at.button if b.label == "Reload data").click().run()
    assert not at.exception
    assert not at.error
    assert at.selectbox(key="manual_label").value == "Reworded"
    assert at.text_area(key="manual_notes").value == "Saved in another tab."
