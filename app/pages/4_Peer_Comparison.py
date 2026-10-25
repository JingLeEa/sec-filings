"""Compare a company's disclosures with the same-year disclosures of its peers."""
import json

import pandas as pd
import streamlit as st

from sec_ui import components, config, data, reports

components.setup("Peer comparison")
st.title("Peer comparison")
st.caption("How a company's disclosure on a topic compares with its peers' disclosures in the same year and SEC Item.")

files = reports.list_peer_files()
if not files:
    st.info("No peer comparison output was found yet.")
    st.markdown(
        "The peer comparison agent writes `data/peer_comparison/<TICKER>/<YEAR>/peer_comparison.json`. "
        "Run it, for example:\n\n"
        "```\nPYTHONPATH=src python3 scripts/compare_peer_disclosures.py --ticker NVDA --peers AMD AVGO INTC --year 2024 "
        "--disclosures-dir ../sec-filings-test-data/test_data/disclosures "
        "--alignments-dir ../sec-filings-test-data/test_data/alignments --env-file .env --max-requests 5\n```\n\n"
        "Or set `SEC_UI_PEER_DIR` to a folder that already holds results."
    )
    st.caption(f"Looked in: {config.peer_dir()}")
    st.stop()

labelled = {}
for path in files:
    try:
        labelled[f"{reports.peer_label(path)}"] = path
    except Exception:  # noqa: BLE001
        continue
st.sidebar.header("Comparison")
choice = st.sidebar.selectbox("Target, year and peers", list(labelled), key="sel_peer")
path = labelled[choice]
try:
    peer = reports.load_peer(path)
except data.DataError as error:
    st.error(str(error))
    st.stop()

if peer.get("_sample_data"):
    st.warning(components.safe(f"Sample data: {peer['_sample_data']}"))
if not peer.get("run_complete", True):
    st.warning(f"Partial run: {peer.get('jobs_completed', '?')} of {peer.get('jobs_planned', '?')} target disclosures compared. "
               "Rerun the agent with the same arguments to continue.")

rows = reports.peer_rows(peer)
if not rows:
    st.info("This output has no comparisons yet.")
    st.stop()
frame = pd.DataFrame(rows)

st.sidebar.header("Filters")
items = sorted(frame["item"].dropna().unique(), key=components._item_order)
sel_items = st.sidebar.multiselect("SEC Item", items, key="peer_items", format_func=lambda i: config.ITEM_TITLES.get(i, f"Item {i}"))
peers = sorted(frame["peer_ticker"].unique())
sel_peers = st.sidebar.multiselect("Peer", peers, key="peer_peers")
statuses = [s for s in reports.PEER_STATUS_LABELS if s in set(frame["status"])]
sel_status = st.sidebar.multiselect("Result", statuses, key="peer_status", format_func=lambda s: reports.PEER_STATUS_LABELS[s])
needle = st.sidebar.text_input("Search text", key="peer_text")

view = frame
if sel_items:
    view = view[view["item"].isin(sel_items)]
if sel_peers:
    view = view[view["peer_ticker"].isin(sel_peers)]
if sel_status:
    view = view[view["status"].isin(sel_status)]
if needle.strip():
    n = needle.strip().lower()
    view = view[view[["target_topic", "comparison"]].apply(lambda col: col.str.lower().str.contains(n, regex=False)).any(axis=1)]

m1, m2, m3 = st.columns(3)
m1.metric("Target disclosures", f"{view['target_disclosure_id'].nunique():,}")
m2.metric("Peer comparisons made", f"{int((view['status'] == 'compared').sum()):,}")
m3.metric("Peers with no comparison", f"{int((view['status'] != 'compared').sum()):,}")
if view.empty:
    st.info("Nothing matches these filters.")
    st.stop()

for disclosure_id, group in view.groupby("target_disclosure_id", sort=False):
    first = group.iloc[0]
    title = first["target_topic"] or disclosure_id
    with st.expander(components.safe(f"Item {first['item']} | {title}")):
        st.caption(f"Target disclosure {disclosure_id}" + (f" | year-over-year: {first['target_change_label']}" if first["target_change_label"] else ""))
        job = next((j for j in peer["comparisons"] if j.get("target_disclosure_id") == disclosure_id), {})
        for _, row in group.iterrows():
            label = reports.PEER_STATUS_LABELS.get(row["status"], row["status"])
            st.markdown(f"**{row['peer_ticker']}** ({label}" + (f", {row['relationship'].replace('_', ' ')}" if row["relationship"] else "") + ")")
            st.write(components.safe(row["comparison"]) or "No explanation saved.")
            if row["status"] != "compared":
                st.caption("A missing comparison does not show the peer is silent on this topic; it means the saved retrieval found nothing usable.")
                continue
            peer_result = next((p for p in job.get("peer_results", []) if p.get("peer_ticker") == row["peer_ticker"]), {})
            comp = next((c for c in peer_result.get("comparisons", []) if c.get("peer_disclosure_id") == row["peer_disclosure_id"]), {})
            left, right = st.columns(2)
            for column, side, name in ((left, "target", peer.get("target", "Target")), (right, "peer", row["peer_ticker"])):
                with column:
                    st.markdown(f"_{name} sentences cited_")
                    for citation in comp.get("evidence", []):
                        if citation.get("side") != side:
                            continue
                        for sentence in citation.get("sentences", []):
                            link = f" [sec.gov]({sentence['source_url']})" if sentence.get("source_url") else ""
                            st.markdown(f"> {components.safe(sentence.get('text', ''))}\n\n`{sentence.get('sentence_id', '')}`{link}")

st.divider()
st.subheader("Download")
base = f"{peer.get('target', 'peer')}_{peer.get('fiscal_year', '')}_peer_comparison"
d1, d2, d3 = st.columns(3)
d1.download_button("Report (Markdown)", reports.peer_markdown(peer), file_name=f"{base}.md", mime="text/markdown", width="stretch")
d2.download_button("Table (CSV)", reports.rows_to_csv(view.to_dict("records")), file_name=f"{base}.csv", mime="text/csv", width="stretch")
d3.download_button("Full output (JSON)", json.dumps(peer, indent=2, ensure_ascii=False), file_name="peer_comparison.json",
                   mime="application/json", width="stretch")
st.caption("Peer statements are model-generated analysis, not human-verified conclusions. Exports keep disclosure and sentence IDs and SEC URLs.")
