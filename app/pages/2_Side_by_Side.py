"""Read the earlier and later disclosure together, with additions and removals highlighted."""
import pandas as pd
import streamlit as st

from sec_ui import components, config, data, diff

components.setup("Side by side")
st.title("Side by side")

source, company, previous, current = components.comparison_picker()
comparison = components.load_selected(source, company, previous, current)
records = comparison["records"]
if not records:
    st.info("This comparison has no records yet. Pick another company or year pair.")
    st.stop()

filtered = components.filters(records, location="page")
if not filtered:
    st.info("No disclosures match these filters. Clear a filter above to see more.")
    st.stop()

ids = [r["match_id"] for r in filtered]
by_id = {r["match_id"]: r for r in filtered}
if st.session_state.get("selected_match") not in by_id:
    st.session_state["selected_match"] = ids[0]


def _label(match_id: str) -> str:
    r = by_id[match_id]
    text = (r["key_change"] or r["current_summary"] or r["previous_summary"] or "").strip()
    return f"{match_id[-5:]} | Item {r['item']} | {r['change_label']} | {text[:70]}"


def _step(delta: int):
    position = ids.index(st.session_state["selected_match"]) + delta
    st.session_state["selected_match"] = ids[max(0, min(len(ids) - 1, position))]


top, prev_col, next_col = st.columns([6, 1, 1], vertical_alignment="bottom")
top.selectbox(f"Disclosure ({len(ids):,} match the filters)", ids, key="selected_match", format_func=_label)
position = ids.index(st.session_state["selected_match"])
prev_col.button("Previous", on_click=_step, args=(-1,), disabled=position == 0, width="stretch")
next_col.button("Next", on_click=_step, args=(1,), disabled=position == len(ids) - 1, width="stretch")

record = by_id[st.session_state["selected_match"]]
components.chips(
    (record["change_label"], ""), (record["materiality"], "accent" if record["materiality"] == "Material" else ""),
    (record["status_label"], components.tone_for_status(record["status"])), (record["relationship_label"], ""),
    (config.ITEM_TITLES.get(record["item"], f"Item {record['item']}"), ""), (record["category"], ""),
)
if record["key_change"]:
    st.markdown(f"**{components.safe(record['key_change'])}**")
if record["status"] == "needs_review":
    st.warning(components.safe("This match needs review before it is relied on. "
                               + (record["verifier_review_reason"] or "; ".join(record["review_reasons"]) or "")))
if record["sections"]:
    st.caption(components.safe("Section: " + "; ".join(record["sections"])))

show_marks = st.toggle("Highlight changes", value=True)
missing = record["missing_side"]
if missing == "later":
    prev_html, curr_html = diff.whole_side_html(record["previous_text"], "removed"), ""
    stats = "The whole earlier disclosure has no counterpart in the later filing."
elif missing == "earlier":
    prev_html, curr_html = "", diff.whole_side_html(record["current_text"], "added")
    stats = "The whole later disclosure has no counterpart in the earlier filing."
else:
    result = diff.diff_texts(record["previous_text"], record["current_text"])
    prev_html, curr_html = result["previous_html"], result["current_html"]
    stats = ("The wording is the same apart from spacing." if result["identical"]
             else f"{result['removed_words']:,} words removed, {result['added_words']:,} words added.")
if not show_marks:
    prev_html, curr_html = diff.plain_html(record["previous_text"]), diff.plain_html(record["current_text"])

left, right = st.columns(2)
for column, title, body, empty_msg in (
    (left, f"FY{previous} (earlier)", prev_html, f"No counterpart found in the FY{previous} filing."),
    (right, f"FY{current} (later)", curr_html, f"No counterpart found in the FY{current} filing."),
):
    with column:
        st.markdown(f'<div class="sidehead">{title}</div>', unsafe_allow_html=True)
        if body.strip():
            st.markdown(f'<div class="diffbox">{body}</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="diffbox empty">{empty_msg}</div>', unsafe_allow_html=True)
if record["text_source"] == "cited sentences":
    st.warning("The full disclosure text was not found, so this view shows only the sentences cited as evidence. "
               "The comparison may be partial. Point SEC_UI_DISCLOSURES_DIR at the disclosures folder to see everything.")
st.caption(f"{stats}  Red with strike-through = removed. Green with underline = added.")

if record["missing_side"]:
    st.info("No counterpart found means the saved comparison for this pair found none. "
            "It is not proof that the topic is missing from the whole filing.")

st.subheader("Why this match and this label")
if record["explanation"]:
    st.markdown(f"**Match reasoning:** {components.safe(record['explanation'])}")
if record["materiality_reason"]:
    score = record["materiality_score"]
    st.markdown(f"**Materiality ({record['materiality']}):** {components.safe(record['materiality_reason'])}"
                + (f" Score {score:.2f}." if score is not None else ""))
methods = record["methods"]
st.caption("Change label by method: " + ", ".join(f"{k} = {v or 'n/a'}" for k, v in methods.items())
           + f". Final label: {record['change_label']}.")

st.subheader("Supporting evidence")
if record["evidence"]:
    frame = pd.DataFrame(record["evidence"])[["side", "disclosure_id", "sentence_id", "text", "source_url"]]
    frame["side"] = frame["side"].map({"earlier": f"FY{previous}", "later": f"FY{current}"}).fillna(frame["side"])
    st.dataframe(frame, hide_index=True, width="stretch", column_config={
        "side": st.column_config.TextColumn("Filing", width="small"),
        "disclosure_id": st.column_config.TextColumn("Disclosure ID"),
        "sentence_id": st.column_config.TextColumn("Sentence ID"),
        "text": st.column_config.TextColumn("Sentence", width="large"),
        "source_url": st.column_config.LinkColumn("SEC filing", display_text="Open on sec.gov"),
    })
else:
    st.info("No sentence-level evidence was saved for this match.")

with st.expander("Disclosures in this match"):
    for side, label in (("previous_members", f"FY{previous}"), ("current_members", f"FY{current}")):
        for member in record[side]:
            st.markdown(f"**{label}** `{member.get('disclosure_id', '')}` Item {member.get('item', '')}, "
                        f"{member.get('section', '') or 'no section'}")
            st.caption(components.safe(member.get("summary")) or "No summary saved.")
