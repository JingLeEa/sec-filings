"""Ask questions in plain English and get answers that cite the matches they come from."""
import pandas as pd
import streamlit as st

from sec_ui import chat, components, config, landing

components.setup("Chatbot")
landing.inject_css()
st.title("Ask the chatbot")
st.caption("Ask about changes, risks, financial figures or how companies compare. "
           "Answers come from the saved comparison results and name the match each fact comes from.")

st.session_state.setdefault("chat_history", [])

# ------------------------------------------------------------------ settings and connection

st.sidebar.header("Chatbot")
use_llm = st.sidebar.toggle("AI-written answers", value=True,
                            help="Off: the same database search, with a plain template answer. Faster and needs no AI key.")
use_embeddings = st.sidebar.toggle("Meaning-based search", value=True,
                                   help="Helps with topic questions. It needs the AI key and embedded evidence; "
                                        "otherwise keyword search is used.")
try:
    bot = chat.session_bot(use_llm, use_embeddings)
except chat.ChatUnavailable as error:
    st.error("The chatbot is not available right now.")
    st.markdown(components.safe(str(error)))
    st.markdown("**To get it running:**\n\n" + chat.SETUP_STEPS)
    st.stop()
except Exception as error:  # noqa: BLE001 - never show a stack trace to a reader
    st.error(f"The chatbot could not start: {components.safe(error)}")
    st.stop()

st.sidebar.caption(components.safe(f"Answers: {bot.llm_description}"))
if use_llm and not bot.llm_enabled:
    st.sidebar.info("No AI key found, so answers are templates built from the search results.")
if st.sidebar.button("Clear conversation", width="stretch"):
    chat.forget_conversation()
    st.rerun()
last = st.session_state["chat_history"][-1] if st.session_state["chat_history"] else None
if last is not None and getattr(last, "filters", None):
    shown = last.filters
    parts = []
    if shown.get("companies"):
        parts.append(", ".join(config.company_label(c) for c in shown["companies"]))
    if shown.get("previous_year") and shown.get("current_year"):
        parts.append(f"FY{shown['previous_year']} to FY{shown['current_year']}")
    elif shown.get("current_year"):
        parts.append(f"FY{shown['current_year']} 10-K (compared with the year before)")
    elif shown.get("previous_year"):
        parts.append(f"since FY{shown['previous_year']}")
    if shown.get("items"):
        parts.append("Item " + ", ".join(shown["items"]))
    if parts:
        st.sidebar.markdown("**Remembered for follow-ups**")
        st.sidebar.caption(components.safe("; ".join(parts)) + ". Name another company or year to change it.")


# ------------------------------------------------------------------ rendering one answer

def render_reply(reply, key: str):
    st.markdown(components.safe(reply.answer))
    pills = [landing.pill(chat.intent_name(reply.intent), landing.INDIGO),
             landing.pill("AI-written" if reply.mode == "llm" else "Template answer", landing.PURPLE if reply.mode == "llm" else landing.GREY)]
    if reply.grounding is not None:
        ok = reply.grounding.grounded
        pills.append(landing.pill("Citations checked" if ok else "Check flagged items", landing.GREEN if ok else landing.AMBER))
    if reply.elapsed_ms is not None:
        pills.append(landing.pill(f"{reply.elapsed_ms / 1000:.1f} s", landing.GREY))
    st.markdown("".join(pills), unsafe_allow_html=True)
    for warning in reply.warnings:
        st.warning(components.safe(warning))

    alignment_ids = [c for c in reply.citations if chat.parse_match_id(c)]
    if alignment_ids:
        left, right = st.columns([4, 1], vertical_alignment="bottom")
        pick = left.selectbox(f"Open a cited match side by side ({len(alignment_ids)} cited)", alignment_ids,
                              key=f"cite_{key}", format_func=lambda m: m)
        if right.button("Open", key=f"open_{key}", width="stretch"):
            chat.open_in_side_by_side(pick)
    elif reply.results:
        st.caption("This answer cites no individual match, so there is nothing to open side by side.")

    row_count = sum(len(r.rows) for r in reply.results)
    with st.expander(f"Where this came from ({row_count} rows from {len(reply.results)} searches)"):
        for number, result in enumerate(reply.results):
            st.markdown(f"**{components.safe(result.name)}** ({len(result.rows)} rows)")
            if result.rows:
                frame = pd.DataFrame(result.rows, columns=result.columns or None)
                frame = frame[[c for c in frame.columns if c not in {"previous_ids", "current_ids"}]]
                st.dataframe(frame, hide_index=True, width="stretch", key=f"rows_{key}_{number}")
        if reply.notes:
            st.markdown("**Notes**\n" + "\n".join(f"- {components.safe(n)}" for n in reply.notes))
    with st.expander("The database query"):
        for name, sql, params in reply.queries:
            st.markdown(f"**{components.safe(name)}**")
            st.code(sql.strip(), language="sql")
            if params:
                shown = {k: v for k, v in params.items() if k != "qvec"} if isinstance(params, dict) else params
                st.caption(components.safe(f"Parameters: {shown}"))


# ------------------------------------------------------------------ history and new question

for number, turn in enumerate(st.session_state["chat_history"]):
    if isinstance(turn, dict):  # an error shown as part of the conversation
        with st.chat_message("user"):
            st.markdown(components.safe(turn["question"]))
        with st.chat_message("assistant"):
            st.error(turn["error"])
        continue
    with st.chat_message("user"):
        st.markdown(components.safe(turn.question))
    with st.chat_message("assistant"):
        render_reply(turn, str(number))

if not st.session_state["chat_history"]:
    st.markdown("#### Try one of these")
    try:
        source = components.cached_source()
        company = st.session_state.get("sel_company") or (source.companies[0] if source.companies else "amd")
        pairs = source.pairs(company) if source.companies else []
        previous, current = pairs[-1] if pairs else (2024, 2025)
    except Exception:  # noqa: BLE001
        company, previous, current = "amd", 2024, 2025
    name = config.COMPANY_NAMES.get(company, (company.upper(), ""))[0]
    pending = None
    for column, question in zip(st.columns(2) * 3, chat.example_questions(name, previous, current)):
        if column.button(question, key=f"try_{question[:30]}", width="stretch"):
            pending = question
    st.caption("Name the company and the year in your question. Short follow-ups such as \"what about Intel?\" reuse the earlier scope. "
               "If the evidence is not enough, the chatbot says so.")
else:
    pending = None

typed = st.chat_input("Ask about disclosure changes...")
question = typed or pending or st.session_state.pop("chat_prefill", None)
if question:
    with st.chat_message("user"):
        st.markdown(components.safe(question))
    with st.chat_message("assistant"):
        with st.spinner("Searching the saved results..."):
            try:
                reply = bot.ask(question)
            except ValueError:
                reply = None
                st.warning("Type a question first.")
            except Exception as error:  # noqa: BLE001 - database or AI errors become a message in the conversation
                reply = None
                message = components.safe(f"Sorry, that question could not be answered. {error}")
                st.error(message)
                st.session_state["chat_history"].append({"question": question, "error": message})
        if reply is not None:
            st.session_state["chat_history"].append(reply)
    if reply is not None or st.session_state["chat_history"][-1:] and isinstance(st.session_state["chat_history"][-1], dict):
        st.rerun()  # redraw from the saved history so the page looks the same as after any other action
