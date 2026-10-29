"""Read the earlier and later disclosure together, with additions and removals highlighted."""
import streamlit as st

from sec_ui import components, config

components.setup("Side by side")
st.title("Side by side")

source, company, previous, current = components.comparison_picker()
comparison = components.load_selected(source, company, previous, current)
records = comparison["records"]
if not records:
    st.info("This comparison has no records yet. Pick another company or year pair.")
    st.stop()

filtered = components.filters(records)
if not filtered:
    st.info("No disclosures match these filters. Clear a filter in the sidebar to see more.")
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

components.side_by_side(record, previous, current, key="side_highlight")

st.subheader("Why this match and this label")
if record["explanation"]:
    st.markdown(f"**Match reasoning:** {components.safe(record['explanation'])}")
if record["materiality_reason"]:
    score = record["materiality_score"]
    st.markdown(f"**Materiality ({record['materiality']}):** {components.safe(record['materiality_reason'])}"
                + (f" Score {score:.2f}." if score is not None else ""))
methods = record["methods"]
st.caption("Change label by method: " + ", ".join(f"{k} = {v or 'n/a'}" for k, v in methods.items())
           + f". Final label: {record['change_label']} ({record['final_label_source'].upper()}).")

components.supporting_evidence(record, previous, current)
