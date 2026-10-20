"""Streamlit chat UI for the SEC 10-K disclosure-change chatbot.

    pip install -r requirements.txt -r app/requirements.txt
    streamlit run app/streamlit_app.py

Needs PostgreSQL running with the alignment fixtures loaded (see docs/chatbot.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sec_disclosure.chatbot import Chatbot  # noqa: E402
from sec_disclosure.chatbot.db import DatabaseError  # noqa: E402

EXAMPLES = [
    "What are the most material changes in Bank of America's 2024 10-K?",
    "How many new risk factors did Intel add in 2025?",
    "Show me disclosures that were removed from JPMorgan's Item 1A between 2023 and 2024",
    "What changed about climate risk across all three companies?",
    "Which disclosures have low confidence scores in their materiality assessment?",
    "Compare the change patterns between BAC and JPM for 2024-2025",
    "What are the most common types of changes in Item 7?",
    "Show me the semantic vs lexical disagreements",
    "Which disclosures were expanded and why?",
    "Summarise the key changes for BAC 2023 to 2024",
]
HIDDEN_COLUMNS = {"previous_ids", "current_ids"}

st.set_page_config(page_title="10-K Change Chatbot", page_icon="📑", layout="wide")


@st.cache_resource(show_spinner="Connecting to PostgreSQL…")
def get_bot(use_llm: bool, use_embeddings: bool) -> Chatbot:
    return Chatbot(use_llm=use_llm, use_embeddings=use_embeddings)


def frame(result) -> pd.DataFrame:
    df = pd.DataFrame(result.rows, columns=result.columns or None)
    return df[[c for c in df.columns if c not in HIDDEN_COLUMNS]] if not df.empty else df


def render_details(reply) -> None:
    meta = [f"`{reply.intent}`", "LLM answer" if reply.mode == "llm" else "templated answer (offline)"]
    if reply.retrieval_method:
        meta.append(f"retrieval: {reply.retrieval_method}")
    if reply.filters:
        meta.append("filters: " + ", ".join(f"{k}={v}" for k, v in reply.filters.items()))
    st.caption(" · ".join(meta))
    for warning in reply.warnings:
        st.warning(warning)
    if reply.grounding is not None:
        g = reply.grounding
        status = "✅ grounded" if g.grounded else "⚠️ check flagged items"
        st.caption(f"Grounding: {status} — {len(g.cited_match_ids)} match IDs cited, {g.checked_quotes} quotes checked")
    with st.expander(f"Evidence ({sum(len(r.rows) for r in reply.results)} rows from {len(reply.results)} queries)"):
        for result in reply.results:
            st.markdown(f"**{result.name}** — {len(result.rows)} rows")
            if result.rows:
                st.dataframe(frame(result), width="stretch", hide_index=True)
    with st.expander("SQL"):
        for name, sql, params in reply.queries:
            st.markdown(f"**{name}**")
            st.code(sql.strip(), language="sql")
            if params:
                shown = {k: v for k, v in params.items() if k != "qvec"} if isinstance(params, dict) else params
                st.caption(f"params: {shown}")
        if reply.notes:
            st.markdown("**Notes**\n" + "\n".join(f"- {n}" for n in reply.notes))


with st.sidebar:
    st.header("📑 10-K Change Chatbot")
    use_llm = st.toggle("Use LLM for answers", value=True, help="Off = deterministic templated answers from SQL")
    use_embeddings = st.toggle("Use pgvector search", value=True, help="Needs SOCLAAS_* and embedded evidence")
    try:
        bot = get_bot(use_llm, use_embeddings)
    except (DatabaseError, ValueError) as error:
        st.error(str(error))
        st.stop()
    st.caption(f"Answers: {bot.llm_description}")
    try:
        sources = bot.db.embedding_sources()
        st.caption(f"Vectors: {sources['evidence']:,} evidence · {sources['disclosures']:,} disclosure")
        overview = pd.DataFrame(bot.db.run_overview())
        if not overview.empty:
            overview.insert(1, "period", overview.pop("previous_year").astype(str) + "→" + overview.pop("current_year").astype(str))
            overview = overview[["company", "period", "alignments", "needs_review"]]
        st.markdown("**Loaded comparisons**")
        st.dataframe(overview, hide_index=True, width="stretch")
    except DatabaseError as error:
        st.error(str(error))
    st.markdown("**Try asking**")
    for example in EXAMPLES:
        if st.button(example, width="stretch"):
            st.session_state.pending = example
    if st.button("Clear conversation", type="secondary"):
        st.session_state.history = []
        bot.reset()

st.session_state.setdefault("history", [])

for turn in st.session_state.history:
    with st.chat_message("user"):
        st.markdown(turn.question)
    with st.chat_message("assistant"):
        st.markdown(turn.answer)
        render_details(turn)

question = st.chat_input("Ask about disclosure changes…") or st.session_state.pop("pending", None)
if question:
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        with st.spinner("Querying…"):
            try:
                reply = bot.ask(question)
            except DatabaseError as error:
                st.error(str(error))
                st.stop()
        st.markdown(reply.answer)
        render_details(reply)
    st.session_state.history.append(reply)
