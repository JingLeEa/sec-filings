"""Run each dashboard page headlessly with Streamlit's AppTest and check what it shows."""
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
FIXTURES = Path(__file__).parent / "fixtures"
PAGES = ["Home.py", "pages/1_Change_Explorer.py", "pages/2_Side_by_Side.py",
         "pages/3_Synthesis_Report.py", "pages/4_Peer_Comparison.py"]


@pytest.fixture(autouse=True)
def sample_environment(monkeypatch):
    import streamlit as st
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(FIXTURES / "no_disclosures_here"))
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(FIXTURES / "synthesis"))
    monkeypatch.setenv("SEC_UI_PEER_DIR", str(FIXTURES / "peer_comparison"))
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run(page: str) -> AppTest:
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
