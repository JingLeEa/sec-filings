"""SEC Disclosure Comparison dashboard: overview page.

Run with:  streamlit run app/Home.py
"""
import pandas as pd
import streamlit as st

from sec_ui import components, config, data, reports

components.setup("Overview")
st.title("SEC disclosure comparison")
st.caption("What changed in a company's 10-K from one year to the next, with the source text behind every change.")
st.page_link("pages/0_Run_Pipeline.py", label="Run pipeline", icon=":material/play_circle:")

source, company, previous, current = components.comparison_picker()
comparison = components.load_selected(source, company, previous, current)
records, meta = comparison["records"], comparison["meta"]

def _bar(order, counts):
    frame = pd.DataFrame({"Label": order, "Records": [counts[k] for k in order]})
    st.bar_chart(frame, x="Label", y="Records", horizontal=True, sort=False, height=300, x_label=" ", y_label="Matches")


st.subheader(f"{config.company_label(company)}: FY{previous} to FY{current}")
if not records:
    st.info("This comparison has no records yet. Pick another company or year pair.")
    st.stop()
if not meta.get("run_complete", True):
    st.warning("This alignment run is not marked complete, so some matches may still change.")

changed = [r for r in records if r["change_label"] not in ("Unchanged", data.NOT_LABELLED)]
material = [r for r in records if r["materiality"] == "Material"]
review = [r for r in records if r["status"] == "needs_review"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Matched groups", f"{len(records):,}")
c2.metric("With a change", f"{len(changed):,}")
c3.metric("Material changes", f"{len(material):,}")
c4.metric("Need review", f"{len(review):,}")

left, right = st.columns(2)
with left:
    st.markdown("**By change label**")
    counts = data.count_by(records, "change_label")
    order = [c for c in config.CHANGE_LABELS + [data.NOT_LABELLED] if c in counts]
    _bar(order, counts)
with right:
    st.markdown("**By SEC Item**")
    counts = data.count_by(records, "item")
    order = sorted(counts, key=components._item_order)
    _bar([config.ITEM_TITLES.get(i, f"Item {i}") for i in order], {config.ITEM_TITLES.get(i, f"Item {i}"): counts[i] for i in order})

st.markdown("**By content category**")
counts = data.count_by(records, "category")
order = [c for c in config.CONTENT_CATEGORIES + [data.NOT_CLASSIFIED] if c in counts]
_bar(order, counts)

with st.expander("How to read these numbers"):
    st.markdown(components.safe(meta.get("note") or "No method note was saved with this run."))

st.divider()
st.subheader("Where to go next")
n1, n2, n3, n4 = st.columns(4)
n1.page_link("pages/1_Change_Explorer.py", label="Change explorer", icon=":material/table_rows:")
n2.page_link("pages/2_Side_by_Side.py", label="Side by side", icon=":material/compare:")
n3.page_link("pages/3_Synthesis_Report.py", label="Synthesis report", icon=":material/summarize:")
n4.page_link("pages/4_Peer_Comparison.py", label="Peer comparison", icon=":material/groups:")
n1.caption("Filter every matched disclosure and export it.")
n2.caption("Read earlier and later text together with changes highlighted.")
syn, peer = reports.list_synthesis_files(), reports.list_peer_files()
n3.caption(f"{len(syn)} report file(s) found." if syn else "No synthesis output found yet.")
n4.caption(f"{len(peer)} peer output(s) found." if peer else "No peer comparison output found yet.")
