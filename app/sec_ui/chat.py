"""Glue between the Streamlit chatbot page and Aarav's chatbot (src/sec_disclosure/chatbot).

Kept free of page layout so it can be tested, and so the page still loads when the chatbot code or its
database is missing: every problem comes back as a plain message instead of a traceback.

One database connection is shared by all browser sessions (the chatbot's Database is lock-protected).
Each session gets its own Chatbot, because the Chatbot remembers the previous question for follow-ups.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

MATCH_ID = re.compile(r"^(?P<company>[a-z]+)_(?P<previous>\d{4})_(?P<current>\d{4})_M\d+$")

INTENT_NAMES = {
    "material_changes": "Most material changes", "count": "Counting changes", "list_changes": "List of changes",
    "topic_search": "Topic search", "topic": "Topic search", "low_confidence": "Low-confidence results",
    "compare_companies": "Company comparison", "distribution": "Types of change", "method_disagreements": "Method disagreements",
    "summary": "Summary", "match_detail": "One match in detail", "peer_comparison": "Peer comparison",
    "peer_compare": "Peer comparison", "financial_trends": "Financial trends", "financial": "Financial trends",
    "custom_sql": "Custom question",
}

SETUP_STEPS = (
    "1. Start the database: `docker compose up -d`\n"
    "2. Load the team's results, or run the pipeline for a company (see `docs/AARAV_chatbot.md`).\n"
    "3. Check that `POSTGRES_*` (or `DATABASE_URL`) in your `.env` points to that database.\n"
    "4. Optional, for AI-written answers: add the `SOCLAAS_*` settings to `.env`. Without them the chatbot "
    "still answers, using templates."
)


class ChatUnavailable(RuntimeError):
    """The chatbot cannot be used right now. The message is safe to show to a user."""


def parse_match_id(match_id: str):
    """('amd', 2024, 2025) for 'amd_2024_2025_M0001', or None if it is not an alignment match ID."""
    found = MATCH_ID.match(str(match_id or ""))
    return (found["company"], int(found["previous"]), int(found["current"])) if found else None


def intent_name(intent: str) -> str:
    return INTENT_NAMES.get(intent, str(intent or "").replace("_", " ").capitalize() or "Question")


def _load_chatbot():
    try:
        from sec_disclosure.chatbot import Chatbot
        from sec_disclosure.chatbot.db import Database, DatabaseError
    except ImportError as error:
        raise ChatUnavailable(
            "The chatbot code is not part of this branch yet, or a package it needs is missing "
            f"({error.name or 'import error'}). Merge the chatbot branch and run `pip install -r requirements.txt`."
        ) from None
    return Chatbot, Database, DatabaseError


@st.cache_resource(show_spinner="Connecting to the database...")
def _shared_database():
    _, Database, DatabaseError = _load_chatbot()
    database = Database()
    try:
        database.ensure_schema()
    except DatabaseError as error:
        database.close()
        raise ChatUnavailable(f"{error}") from None
    return database


def create_bot(use_llm: bool, use_embeddings: bool):
    """A chatbot for this browser session. Raises ChatUnavailable with a readable reason."""
    Chatbot, _, DatabaseError = _load_chatbot()
    try:
        return Chatbot(db=_shared_database(), use_llm=use_llm, use_embeddings=use_embeddings, ensure_schema=False)
    except ChatUnavailable:
        _shared_database.clear()
        raise
    except (DatabaseError, ValueError) as error:
        raise ChatUnavailable(str(error)) from None


def session_bot(use_llm: bool, use_embeddings: bool):
    """The session's chatbot. It is rebuilt when a setting changes, keeping the remembered scope."""
    settings = (use_llm, use_embeddings)
    if st.session_state.get("chat_bot_settings") != settings or st.session_state.get("chat_bot") is None:
        previous = st.session_state.get("chat_bot")
        bot = create_bot(use_llm, use_embeddings)
        if previous is not None:
            bot.last_plan = getattr(previous, "last_plan", None)
        st.session_state["chat_bot"], st.session_state["chat_bot_settings"] = bot, settings
    return st.session_state["chat_bot"]


def forget_conversation():
    """Start a new, empty conversation. Earlier ones stay saved and can be reopened from the sidebar."""
    st.session_state["chat_history"] = []
    st.session_state["chat_conversation_id"] = None
    bot = st.session_state.get("chat_bot")
    if bot is not None:
        bot.reset()


# ------------------------------------------------------------------ saved conversations

def chat_history_store():
    """The saved-conversation store (chatbot_conversations / chatbot_turns), or None if it cannot be used.
    History is a convenience: when it fails, the chatbot still answers, it just does not remember."""
    try:
        from sec_disclosure.chatbot.history import ChatHistory
        return ChatHistory(_shared_database())
    except Exception:  # noqa: BLE001
        return None


def restore_latest_conversation(bot) -> None:
    """On the first visit in a browser session, reopen the most recent conversation so nothing asked earlier
    is lost after a reload or an app restart."""
    if "chat_conversation_id" in st.session_state:
        return
    st.session_state["chat_conversation_id"] = None
    store = chat_history_store()
    if store is None:
        return
    try:
        latest = store.latest()
        if latest:
            open_conversation(bot, latest, store)
    except Exception:  # noqa: BLE001
        st.session_state["chat_history_problem"] = "Earlier questions could not be loaded."


def open_conversation(bot, conversation_id: str, store=None) -> None:
    """Show a saved conversation and continue it: follow-ups reuse its last company and years."""
    store = store or chat_history_store()
    if store is None:
        return
    st.session_state["chat_history"] = store.turns(conversation_id)
    st.session_state["chat_conversation_id"] = conversation_id
    bot.last_plan = store.last_plan(conversation_id)


def save_turn(question: str, *, reply=None, plan=None, error: str | None = None) -> None:
    """Save one question to the current conversation (creating it on the first question)."""
    store = chat_history_store()
    if store is None:
        st.session_state["chat_history_problem"] = "Questions are not being saved (the history table is unavailable)."
        return
    try:
        st.session_state["chat_conversation_id"] = store.save_turn(
            st.session_state.get("chat_conversation_id"), question, reply=reply, plan=plan, error=error, source="app")
        st.session_state.pop("chat_history_problem", None)
    except Exception:  # noqa: BLE001
        st.session_state["chat_history_problem"] = "The last question could not be saved to the history."


def saved_conversations(limit: int = 50) -> list:
    store = chat_history_store()
    if store is None:
        return []
    try:
        return store.conversations(limit=limit)
    except Exception:  # noqa: BLE001
        return []


def delete_conversation(conversation_id: str) -> None:
    store = chat_history_store()
    if store is not None:
        store.delete(conversation_id)
    forget_conversation()


def example_questions(company_name: str, previous: int, current: int) -> list:
    return [
        f"What are the most material changes in {company_name}'s {current} 10-K?",
        f"How many new risk factors did {company_name} add in {current}?",
        f"What did {company_name} remove between {previous} and {current}?",
        f"Summarise the key changes for {company_name} from {previous} to {current}",
        f"How did {company_name}'s revenue and gross margin change?",
        f"How does {company_name} compare to its peers on export controls?",
    ]


def open_in_side_by_side(match_id: str) -> bool:
    """Point the side-by-side page at this match and go there. False if the ID is not an alignment match."""
    parsed = parse_match_id(match_id)
    if parsed is None:
        return False
    company, previous, current = parsed
    st.session_state["sel_company"] = company
    st.session_state["sel_pair"] = f"FY{previous} to FY{current}"
    for key in ("f_items", "f_categories", "f_changes", "f_materiality", "f_statuses", "f_relationships"):
        st.session_state[key] = []
    st.session_state["f_text"] = ""
    st.session_state["selected_match"] = match_id
    st.switch_page("pages/4_Side_by_Side.py")
    return True
