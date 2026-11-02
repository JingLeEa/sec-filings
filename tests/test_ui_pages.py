"""Run each dashboard page headlessly with Streamlit's AppTest and check what it shows."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
FIXTURES = Path(__file__).parent / "fixtures"
PAGES = ["Home.py", "pages/0_Run_Pipeline.py", "pages/1_Change_Explorer.py", "pages/2_Side_by_Side.py",
         "pages/3_Synthesis_Report.py", "pages/4_Peer_Comparison.py"]


@pytest.fixture(autouse=True)
def sample_environment(monkeypatch, tmp_path):
    import streamlit as st
    from sec_ui import pipeline_runs
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(FIXTURES / "no_disclosures_here"))
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(FIXTURES / "synthesis"))
    monkeypatch.setenv("SEC_UI_PEER_DIR", str(FIXTURES / "peer_comparison"))
    monkeypatch.setattr(pipeline_runs, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline_runs, "_postgres_documents", lambda *args: {})
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run(page: str) -> AppTest:
    if page == "pages/4_Peer_Comparison.py":
        return AppTest.from_file(str(APP / "Home.py"), default_timeout=60).run().switch_page(page).run()
    return AppTest.from_file(str(APP / page), default_timeout=60).run()


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


def test_pipeline_page_shows_running_stage_and_saved_database_outputs(monkeypatch):
    from sec_ui import pipeline_runs
    progress = {"stages": [
        {"stage": "save_text_2023", "phase": "preprocessing", "status": "complete"},
        {"stage": "save_text_2024", "phase": "preprocessing", "status": "complete"},
        {"stage": "save_text_2025", "phase": "preprocessing", "status": "complete"},
        *[{"stage": f"save_xbrl_{year}", "phase": "preprocessing", "status": "complete"}
          for year in (2023, 2024, 2025)],
        {"stage": "extract_disclosures_and_save", "phase": "extraction", "status": "running"},
    ]}
    monkeypatch.setattr(pipeline_runs, "read_run", lambda ticker, years: ({"status": "running"}, progress))
    at = run("pages/0_Run_Pipeline.py")
    assert any("extracting disclosures" in message.value for message in at.info)
    assert any("6 of 6 outputs loaded to database" in message.value for message in at.caption)
    assert any(status.label == "Pipeline running" and status.state == "running" for status in at.get("status"))
    assert not at.exception


def test_pipeline_page_shows_alignment_job_progress(monkeypatch, tmp_path):
    import json
    from sec_ui import pipeline_runs
    folder = tmp_path / "data/alignments/amd/2023-2024"
    (folder / "jobs").mkdir(parents=True)
    (folder / "candidate_matches.json").write_text(json.dumps({str(i): [] for i in range(5)}))
    (folder / "jobs/matching_001.json").write_text(json.dumps({"anchors": ["0", "1"]}))
    progress = {"stages": [{"stage": "align_and_save_2023_2024", "phase": "alignment", "status": "running"}]}
    monkeypatch.setattr(pipeline_runs, "read_run", lambda ticker, years: ({"status": "running"}, progress))
    at = run("pages/0_Run_Pipeline.py")
    assert any("FY2023 to FY2024 alignment: 2 of 5 steps" in item.text for item in at.get("progress"))
    assert not at.exception


def test_pipeline_page_shows_other_active_company_and_blocks_new_start(monkeypatch):
    import json
    from sec_ui import pipeline_runs
    folder = pipeline_runs.run_folder("unh", [2023, 2024])
    folder.mkdir(parents=True)
    (folder / "ui_run.json").write_text(json.dumps({
        "run_id": "active", "ticker": "unh", "years": [2023, 2024],
        "status": "running", "pid": 12345,
    }))
    monkeypatch.setattr(pipeline_runs, "_pid_alive", lambda pid: True)
    at = run("pages/0_Run_Pipeline.py")
    assert any("UNH is running" in message.value for message in at.info)
    assert any("UNH | FY2023, FY2024" in message.value for message in at.caption)
    assert any(status.label == "Pipeline running" and status.state == "running" for status in at.get("status"))
    assert not any(button.label in {"Start pipeline", "Resume pipeline"} for button in at.button)
    assert not at.exception


def test_pipeline_page_shows_preflight_failure_and_resume(monkeypatch):
    from sec_ui import pipeline_runs
    monkeypatch.setattr(pipeline_runs, "read_run", lambda ticker, years: ({"run_id": "test", "status": "stopped"}, None))
    monkeypatch.setattr(pipeline_runs, "log_tail", lambda ticker, years, job:
                        "Pipeline preflight failed: Cannot connect to PostgreSQL.")
    at = run("pages/0_Run_Pipeline.py")
    assert any("Pipeline stopped" in message.value for message in at.error)
    assert any("docker compose up -d" in message.value for message in at.warning)
    assert any("Pipeline preflight failed" in code.value for code in at.code)
    assert any(button.label == "Resume pipeline" for button in at.button)
    assert not at.exception


def test_pipeline_page_shows_completion(monkeypatch):
    from sec_ui import pipeline_runs
    years = [2023, 2024, 2025]
    progress = {"full_pipeline_complete": True, "stages": [
        {"stage": stage, "phase": phase, "status": "complete"}
        for phase, saves in pipeline_runs.database_saves(years).items() for _, stage in saves
    ]}
    monkeypatch.setattr(pipeline_runs, "read_run", lambda ticker, selected: ({"run_id": "test", "status": "complete"}, progress))
    at = run("pages/0_Run_Pipeline.py")
    assert any("Pipeline complete" in message.value for message in at.success)
    assert any("6 of 6 stages complete" in message.text for message in at.get("progress"))
    assert not at.exception


def test_pipeline_page_requires_full_completion_marker(monkeypatch):
    from sec_ui import pipeline_runs
    monkeypatch.setattr(pipeline_runs, "read_run", lambda ticker, years: (
        {"run_id": "test", "status": "complete"}, {"complete": True, "full_pipeline_complete": False, "stages": []}
    ))
    at = run("pages/0_Run_Pipeline.py")
    assert not any("Pipeline complete" in message.value for message in at.success)
    assert any("completion marker is missing" in message.value for message in at.warning)
    assert not at.exception


def test_pipeline_page_starts_backend_only_after_confirmation(monkeypatch):
    from sec_ui import pipeline_runs
    calls = []
    monkeypatch.setattr(pipeline_runs, "start_run", lambda ticker, years: calls.append((ticker, years)))
    at = run("pages/0_Run_Pipeline.py")
    assert next(button for button in at.button if button.label == "Start pipeline").disabled
    at.checkbox(key="confirm_multi_year").check().run()
    next(button for button in at.button if button.label == "Start pipeline").click().run()
    assert calls == [("AMD", [2023, 2024, 2025])]
    assert not at.exception


def test_pipeline_page_requires_two_years():
    at = run("pages/0_Run_Pipeline.py")
    next(field for field in at.text_input if field.label == "Fiscal years").set_value("2024").run()
    assert next(button for button in at.button if button.label == "Start pipeline").disabled
    assert any("at least two distinct fiscal years" in message.value for message in at.warning)


def test_pipeline_page_accepts_older_nonconsecutive_years():
    at = run("pages/0_Run_Pipeline.py")
    next(field for field in at.text_input if field.label == "Fiscal years").set_value("1998, 2001").run()
    assert not next(button for button in at.button if button.label == "Start pipeline").disabled
    assert any("Comparisons: FY1998 to FY2001" in message.value for message in at.caption)


def test_pipeline_page_reconfirms_changed_year_selection():
    at = run("pages/0_Run_Pipeline.py")
    at.checkbox(key="confirm_multi_year").check().run()
    assert not next(button for button in at.button if button.label == "Start pipeline").disabled
    next(field for field in at.text_input if field.label == "Fiscal years").set_value("2021, 2023, 2025").run()
    assert not at.checkbox(key="confirm_multi_year").value
    assert next(button for button in at.button if button.label == "Start pipeline").disabled


def test_pipeline_page_reconfirms_changed_ticker():
    at = run("pages/0_Run_Pipeline.py")
    at.checkbox(key="confirm_multi_year").check().run()
    next(field for field in at.text_input if field.label == "Company ticker").set_value("NVDA").run()
    assert not at.checkbox(key="confirm_multi_year").value
    assert next(button for button in at.button if button.label == "Start pipeline").disabled


def test_pipeline_page_rejects_invalid_ticker():
    at = run("pages/0_Run_Pipeline.py")
    next(field for field in at.text_input if field.label == "Company ticker").set_value("A&B").run()
    assert next(button for button in at.button if button.label == "Start pipeline").disabled
    assert any("Enter a ticker beginning" in message.value for message in at.warning)


def test_explorer_filter_narrows_the_table():
    at = run("pages/1_Change_Explorer.py")
    assert metrics(at)["Showing"] == "432 of 432"
    assert not at.sidebar.multiselect
    at.multiselect(key="f_changes").select("Removed").run()
    assert not at.exception
    shown = int(metrics(at)["Showing"].split(" of ")[0])
    assert 0 < shown < 432


def test_explorer_filters_with_no_match_show_a_message():
    at = run("pages/1_Change_Explorer.py")
    at.multiselect(key="f_changes").select("Removed").run()
    at.multiselect(key="f_materiality").select("Not assessed").run()
    assert not at.exception
    # either rows remain or the friendly empty state is shown; never a crash
    assert at.info or int(metrics(at)["Showing"].split(" of ")[0]) > 0


def test_filters_persist_across_pages():
    at = run("Home.py")
    at.switch_page("pages/1_Change_Explorer.py").run()
    at.multiselect(key="f_changes").select("Modified").run()
    chosen = at.multiselect(key="f_changes").value
    assert chosen == ["Modified"]
    # AppTest keeps session state when the same instance moves to another page
    at.switch_page("pages/2_Side_by_Side.py").run()
    assert not at.exception
    assert at.multiselect(key="f_changes").value == ["Modified"]


def test_side_by_side_steps_through_records():
    at = run("pages/2_Side_by_Side.py")
    first = at.selectbox(key="selected_match").value
    next(b for b in at.button if b.label == "Next").click().run()
    assert not at.exception
    assert at.selectbox(key="selected_match").value != first


def test_side_by_side_marks_missing_counterpart():
    at = run("pages/2_Side_by_Side.py")
    at.multiselect(key="f_relationships").select("previous_only").run()
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
    home = run("Home.py")
    assert any("No comparison data" in e.value for e in home.error)
    assert any(link.label == "Run pipeline" for link in home.get("page_link"))
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
