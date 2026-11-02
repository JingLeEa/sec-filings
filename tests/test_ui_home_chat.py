"""Headless tests for the home page and the chatbot page (no database, no AI key, no network)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from sec_ui import chat, landing  # noqa: E402

APP = Path(__file__).resolve().parents[1] / "app"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def sample_environment(monkeypatch):
    import streamlit as st
    monkeypatch.setenv("SEC_UI_DATA_SOURCE", "files")
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(FIXTURES / "alignments"))
    monkeypatch.setenv("SEC_UI_DISCLOSURES_DIR", str(FIXTURES / "no_disclosures_here"))
    monkeypatch.setenv("SEC_UI_SYNTHESIS_DIRS", str(FIXTURES / "synthesis"))
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def run(page: str, **state) -> AppTest:
    at = AppTest.from_file(str(APP / page), default_timeout=60)
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


def text_of(at) -> str:
    return " ".join([m.value for m in at.markdown] + [h.value for h in at.subheader] + [t.value for t in at.title])


# ------------------------------------------------------------------ home page


def test_home_runs_and_explains_each_number():
    at = run("Home.py")
    assert not at.exception, [e.value for e in at.exception]
    shown = {m.label: m.value for m in at.metric}
    assert shown["Matched groups"] == "432" and int(shown["Need review"]) == 45
    assert list(shown) == ["Matched groups", "With a change", "Material changes", "Need review"]
    text = text_of(at)
    for phrase in ("What it counts:", "What would you like to do?", "Key terms", "Good to know"):
        assert phrase in text
    assert text.count("What it counts:") == 4


def test_home_shows_a_card_for_every_page_that_exists():
    text = text_of(run("Home.py"))
    for feature in landing.available_features():
        assert feature["title"] in text
    assert "Explore changes" in text


def test_home_without_data_still_shows_the_menu(monkeypatch, tmp_path):
    import streamlit as st
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("SEC_UI_ALIGNMENTS_DIR", str(empty))
    st.cache_data.clear()
    at = run("Home.py")
    assert not at.exception
    assert "What would you like to do?" in text_of(at)
    assert any("No comparison data" in e.value for e in at.error) or any("No comparison data" in i.value for i in at.info)


def test_home_snapshot_follows_the_sidebar_choice():
    at = run("Home.py")
    at.sidebar.selectbox(key="sel_pair").select("FY2023 to FY2024").run()
    assert not at.exception
    assert "FY2023 to FY2024" in " ".join(h.value for h in at.subheader)


def test_features_skip_pages_that_do_not_exist(monkeypatch, tmp_path):
    (tmp_path / "1_Change_Explorer.py").write_text("")
    monkeypatch.setattr(landing, "PAGES_DIR", tmp_path)
    assert [f["key"] for f in landing.available_features()] == ["explorer"]
    assert landing.has_page("1_Change_Explorer.py") and not landing.has_page("6_Chatbot.py")


def test_every_feature_has_a_colour_a_button_label_and_bullets():
    keys = [f["key"] for f in landing.FEATURES]
    assert len(keys) == len(set(keys))
    css = landing.feature_css()
    for feature in landing.FEATURES:
        assert feature["cta"] and len(feature["bullets"]) >= 3 and feature["color"].startswith("#")
        assert f"st-key-card_{feature['key']}" in css


def test_change_label_guide_covers_every_label():
    from sec_ui import config
    assert set(config.CHANGE_LABELS) <= set(landing.CHANGE_LABEL_INFO)


# ------------------------------------------------------------------ chat helpers


def test_match_ids_are_parsed():
    assert chat.parse_match_id("amd_2024_2025_M0001") == ("amd", 2024, 2025)
    assert chat.parse_match_id("nvda_2023_2024_M0107") == ("nvda", 2023, 2024)
    for bad in ("nvda_2024_1A_D044", "", None, "amd_2024_M0001"):
        assert chat.parse_match_id(bad) is None


def test_intent_names_are_readable():
    assert chat.intent_name("material_changes") == "Most material changes"
    assert chat.intent_name("something_new") == "Something new"


def test_example_questions_name_the_company_and_years():
    questions = chat.example_questions("AMD", 2024, 2025)
    assert len(questions) == 6 and all("AMD" in q for q in questions)
    assert any("2024" in q and "2025" in q for q in questions)


# ------------------------------------------------------------------ chat page with a stand-in chatbot


def fake_reply(question, answer="An answer.", citations=("amd_2024_2025_M0001", "nvda_2024_1A_D044"), grounded=True):
    return SimpleNamespace(
        question=question, answer=answer, intent="material_changes", filters={"companies": ["amd"], "current_year": 2025},
        mode="offline", results=[SimpleNamespace(name="material", rows=[{"match_id": "amd_2024_2025_M0001", "x": 1}],
                                                 columns=["match_id", "x"], sql="SELECT 1", params=None)],
        retrieval_method=None, citations=list(citations), warnings=[], notes=["a note"],
        grounding=SimpleNamespace(grounded=grounded), elapsed_ms=120.0,
        queries=[("material", "SELECT 1", None)])


class FakeBot:
    llm_description = "offline (test)"
    llm_enabled = False

    def __init__(self, answer="An answer.", error=None, **extra):
        self.answer, self.error, self.asked, self.last_plan, self.extra = answer, error, [], None, extra

    def ask(self, question):
        self.asked.append(question)
        if not question.strip():
            raise ValueError("Ask a question.")
        if self.error:
            raise self.error
        return fake_reply(question, self.answer, **self.extra)

    def reset(self):
        self.last_plan = None


@pytest.fixture()
def bot(monkeypatch):
    fake = FakeBot()
    monkeypatch.setattr(chat, "create_bot", lambda use_llm, use_embeddings: fake)
    return fake


def test_chat_page_starts_with_examples(bot):
    at = run("pages/6_Chatbot.py")
    assert not at.exception, [e.value for e in at.exception]
    labels = [b.label for b in at.button]
    assert any("most material changes" in label for label in labels)
    assert "Try one of these" in text_of(at)


def test_asking_a_question_shows_answer_citations_and_evidence(bot):
    at = run("pages/6_Chatbot.py")
    at.chat_input[0].set_value("What changed at AMD?").run()
    assert not at.exception, [e.value for e in at.exception]
    assert bot.asked == ["What changed at AMD?"]
    assert [m.name for m in at.chat_message] == ["user", "assistant"]
    assert "An answer." in text_of(at)
    # only real alignment matches can be opened side by side, a disclosure pair cannot
    options = at.selectbox[0].options
    assert options == ["amd_2024_2025_M0001"]
    assert "Citations checked" in text_of(at) and "Most material changes" in text_of(at)
    assert [e.label for e in at.expander][:2] == ["Where this came from (1 rows from 1 searches)", "The database query"]


def test_dollar_amounts_are_escaped_so_they_are_not_read_as_maths(monkeypatch):
    fake = FakeBot(answer="Revenue rose from $5 billion to $7 billion.")
    monkeypatch.setattr(chat, "create_bot", lambda use_llm, use_embeddings: fake)
    at = run("pages/6_Chatbot.py")
    at.chat_input[0].set_value("revenue?").run()
    assert "\\$5 billion to \\$7 billion" in text_of(at)


def test_clicking_an_example_asks_it(bot):
    at = run("pages/6_Chatbot.py")
    example = next(b for b in at.button if "How many new risk factors" in b.label)
    example.click().run()
    assert bot.asked and "How many new risk factors" in bot.asked[0]
    assert len(at.chat_message) == 2


def test_home_prefill_is_asked_once(bot):
    at = run("pages/6_Chatbot.py", chat_prefill="Summarise AMD for me")
    assert bot.asked == ["Summarise AMD for me"]
    assert "chat_prefill" not in at.session_state
    at.run()
    assert bot.asked == ["Summarise AMD for me"]  # not asked again on the next run


def test_follow_up_scope_is_shown_in_the_sidebar(bot):
    at = run("pages/6_Chatbot.py")
    at.chat_input[0].set_value("What changed at AMD?").run()
    sidebar = " ".join(m.value for m in at.sidebar.markdown) + " ".join(c.value for c in at.sidebar.caption)
    assert "Remembered for follow-ups" in sidebar and "AMD" in sidebar and "FY2025 10-K" in sidebar and "?" not in sidebar


def test_clear_conversation_empties_the_page(bot):
    at = run("pages/6_Chatbot.py")
    at.chat_input[0].set_value("What changed at AMD?").run()
    assert len(at.chat_message) == 2
    next(b for b in at.sidebar.button if b.label == "Clear conversation").click().run()
    assert len(at.chat_message) == 0 and "Try one of these" in text_of(at)


def test_a_failing_question_becomes_a_message_not_a_crash(monkeypatch):
    fake = FakeBot(error=RuntimeError("Query 'x' failed: timeout"))
    monkeypatch.setattr(chat, "create_bot", lambda use_llm, use_embeddings: fake)
    at = run("pages/6_Chatbot.py")
    at.chat_input[0].set_value("slow question").run()
    assert not at.exception
    assert any("could not be answered" in e.value and "timeout" in e.value for e in at.error)
    at.run()  # and it stays in the conversation on the next run
    assert any("could not be answered" in e.value for e in at.error)


def test_unavailable_chatbot_explains_how_to_fix_it(monkeypatch):
    def broken(use_llm, use_embeddings):
        raise chat.ChatUnavailable("Cannot connect to PostgreSQL (OperationalError).")
    monkeypatch.setattr(chat, "create_bot", broken)
    at = run("pages/6_Chatbot.py")
    assert not at.exception
    assert any("not available" in e.value for e in at.error)
    text = text_of(at)
    assert "Cannot connect to PostgreSQL" in text and "docker compose up -d" in text
    assert not at.chat_input  # nothing to type into until it works


def test_missing_chatbot_code_gives_a_readable_reason(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("sec_disclosure.chatbot"):
            raise ImportError("no chatbot here", name="sec_disclosure.chatbot")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(chat.ChatUnavailable, match="not part of this branch"):
        chat._load_chatbot()
