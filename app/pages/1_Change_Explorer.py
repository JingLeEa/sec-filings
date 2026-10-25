"""Filter all matched disclosures for one company and year pair, then open one to compare."""
import json

import pandas as pd
import streamlit as st

from sec_ui import components, config, data, reports

components.setup("Change explorer")
st.title("Change explorer")

source, company, previous, current = components.comparison_picker()
comparison = components.load_selected(source, company, previous, current)
records = comparison["records"]
if not records:
    st.info("This comparison has no records yet. Pick another company or year pair.")
    st.stop()

filtered = components.filters(records)
st.caption(f"{config.company_label(company)}: FY{previous} to FY{current}")

m1, m2, m3 = st.columns(3)
m1.metric("Showing", f"{len(filtered):,} of {len(records):,}")
m2.metric("Material", f"{sum(r['materiality'] == 'Material' for r in filtered):,}")
m3.metric("Need review", f"{sum(r['status'] == 'needs_review' for r in filtered):,}")

if not filtered:
    st.info("No disclosures match these filters. Clear a filter in the sidebar to see more.")
    st.stop()

frame = pd.DataFrame([data.table_row(r) for r in filtered], columns=data.TABLE_COLUMNS)
event = st.dataframe(
    frame, hide_index=True, width="stretch", height=420,
    on_select="rerun", selection_mode="single-row", key="explorer_table",
    column_config={
        "match": st.column_config.TextColumn("Match", width="small"),
        "item": st.column_config.TextColumn("Item", width="small"),
        "change_label": st.column_config.TextColumn("Change", width="small"),
        "materiality": st.column_config.TextColumn("Materiality", width="small"),
        "category": st.column_config.TextColumn("Category"),
        "relationship_label": st.column_config.TextColumn("Match type"),
        "status_label": st.column_config.TextColumn("Status"),
        "summary": st.column_config.TextColumn("Summary", width="large"),
    },
)
st.caption("Click a row to see its details.")

picked = event.selection.rows if event and event.selection else []
if picked:
    record = filtered[picked[0]]
    st.session_state["selected_match"] = record["match_id"]
    st.divider()
    st.subheader(components.safe(record["key_change"] or record["match_id"]))
    components.chips(
        (record["change_label"], ""), (record["materiality"], "accent" if record["materiality"] == "Material" else ""),
        (record["status_label"], components.tone_for_status(record["status"])), (record["relationship_label"], ""),
        (config.ITEM_TITLES.get(record["item"], f"Item {record['item']}"), ""), (record["category"], ""),
    )
    if record["status"] == "needs_review":
        st.warning(components.safe("This match needs review. " + (record["verifier_review_reason"] or "; ".join(record["review_reasons"]) or "")))
    if record["missing_side"]:
        side = "later" if record["missing_side"] == "later" else "earlier"
        st.info(f"No counterpart was found in the {side} filing for this disclosure. "
                "That means the saved comparison found none, not that the topic is absent from the whole filing.")
    st.markdown(components.safe(record["current_summary"] or record["previous_summary"]) or "_No summary saved._")
    if record["explanation"]:
        st.markdown(f"**Why they were matched:** {components.safe(record['explanation'])}")
    if record["materiality_reason"]:
        score = record["materiality_score"]
        st.markdown(f"**Materiality:** {components.safe(record['materiality_reason'])}" + (f" (score {score:.2f})" if score is not None else ""))
    methods = record["methods"]
    st.caption("Method labels: " + ", ".join(f"{k} = {v or 'n/a'}" for k, v in methods.items()))
    if st.button("Open side by side", type="primary"):
        st.switch_page("pages/2_Side_by_Side.py")

st.divider()
st.subheader("Export")
include_text = st.checkbox("Include full text in the export", value=True)
rows = data.export_rows(filtered, include_text=include_text)
name = f"{company}_{previous}_{current}_changes"
d1, d2 = st.columns(2)
d1.download_button("Download CSV", reports.rows_to_csv(rows), file_name=f"{name}.csv", mime="text/csv", width="stretch")
d2.download_button("Download JSON", json.dumps(rows, indent=2, ensure_ascii=False), file_name=f"{name}.json",
                   mime="application/json", width="stretch")
st.caption("Exports keep the match ID, disclosure IDs, sentence IDs and SEC source URLs for every row, "
           "and respect the filters above.")
