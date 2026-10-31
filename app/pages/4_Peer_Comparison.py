"""Compare a company's disclosures with the same-year disclosures of its peers."""
import json
import re

import pandas as pd
import streamlit as st

from sec_ui import components, config, data, peer_runs, pipeline_runs, reports

components.setup("Peer comparison")
st.title("Peer comparison")
st.caption("How a company's disclosure on a topic compares with its peers' disclosures in the same year and SEC Item.")


@st.fragment(run_every="3s")
def run_peer_panel(value):
    running = peer_runs.active_run()
    try:
        selected = peer_runs.read_run(value) if value else None
    except peer_runs.RunError as error:
        st.error(str(error))
        selected = None
    blocked = running is not None or (selected or {}).get("status") in {"starting", "running"}
    key = peer_runs.selection_key(value) if value else None
    checked = st.session_state.get("peer_input_check", {})

    if st.button("Check prerequisites", disabled=value is None or blocked):
        try:
            with st.spinner("Checking saved disclosures and materiality…"):
                summary = peer_runs.check_inputs(value)
            checked = {"key": key, "ready": True, "summary": summary}
        except peer_runs.RunError as error:
            message = str(error).replace(
                "Run scripts/run_full_pipeline.py for each missing ticker/year first.",
                "Open Run pipeline and prepare each missing company for this fiscal year first.")
            checked = {"key": key, "ready": False, "message": message}
        st.session_state["peer_input_check"] = checked

    ready = value is not None and checked.get("key") == key and checked.get("ready")
    if checked.get("key") == key and key is not None:
        if ready:
            summary = checked["summary"]
            st.success(f"Inputs ready: {summary['target_disclosures']} target disclosures selected. "
                       "Saved disclosures are available for every peer.")
        else:
            st.warning(checked["message"])
    elif value:
        st.caption("Check prerequisites for this selection before starting. This check makes no AI requests.")

    main_running = pipeline_runs.active_run()
    if main_running:
        st.info("The main pipeline is still running. Wait for it to save its inputs before starting a peer comparison.")
    label = "Resume peer comparison" if (selected or {}).get("status") == "stopped" else "Start peer comparison"
    if st.button(label, type="primary", disabled=not ready or blocked or bool(main_running)):
        try:
            with st.spinner("Rechecking inputs and starting comparison…"):
                peer_runs.start_run(value)
        except peer_runs.RunError as error:
            st.error(str(error))
        else:
            st.rerun()

    job = running or selected
    if not job:
        return
    st.caption(f"{job['ticker'].upper()} | FY{job['year']} | Peers: {', '.join(peer.upper() for peer in job['peers'])}")
    status = job.get("status")
    if status in {"starting", "running"}:
        st.status("Peer comparison running", state="running", expanded=False)
        try:
            work = peer_runs.progress(job)
        except peer_runs.RunError as error:
            st.warning(str(error))
            work = None
        if work and isinstance(work.get("jobs_planned"), int) and work["jobs_planned"] > 0:
            done = work.get("jobs_completed") or 0
            st.progress(min(1.0, done / work["jobs_planned"]),
                        text=f"{done} of {work['jobs_planned']} target disclosures compared")
        else:
            st.caption("Preparing saved evidence and comparison jobs.")
    elif status == "complete":
        st.success("Peer comparison complete. The report and citations were saved to PostgreSQL.")
        if st.session_state.get("peer_notified_run") != job["run_id"]:
            st.session_state["peer_notified_run"] = job["run_id"]
            st.session_state["peer_finished_selection"] = peer_runs.selection_key(job)
            st.toast("Peer comparison complete", icon=":material/check_circle:")
            st.rerun()
    elif status == "stopped":
        st.warning("Peer comparison stopped or is incomplete. Saved comparison jobs are retained. "
                   "Review the activity log, check prerequisites, and resume the same selection.")
    log = peer_runs.log_tail(job)
    if log:
        with st.expander("Peer comparison activity log", expanded=status == "stopped"):
            st.code(log, language="text")


st.subheader("Run a peer comparison")
st.info("Prepare the target company and every peer in Run pipeline first. Each must have a saved 10-K "
        "and extracted disclosures for the same fiscal year. Material changes also needs the target's "
        "adjacent-year materiality results. Peer comparison uses these saved inputs; it does not run the main pipeline.")
st.page_link("pages/0_Run_Pipeline.py", label="Open Run pipeline", icon=":material/play_circle:")
left, middle, right = st.columns([1, 2, 1])
ticker = left.text_input("Target ticker", value="NVDA", max_chars=20).strip().upper()
peer_text = middle.text_input("Peer tickers", value="AMD, AVGO, INTC", help="Separate tickers with commas or spaces.")
year = right.number_input("Fiscal year", min_value=1900, max_value=2200, value=2024, step=1)
items = st.multiselect("Items to compare", peer_runs.ITEMS, default=["1", "1A", "7", "8"])
mode = st.selectbox("Target disclosures", ["material_changes", "all"],
                    format_func=lambda value: "Material changes" if value == "material_changes" else "All disclosures")
if mode == "material_changes":
    adjacent = int(year) - 1 if year > 1900 else int(year) + 1
    st.caption(f"For the target, complete Run pipeline through Materiality with FY{year} and an adjacent year "
               f"(for example FY{adjacent}). Only disclosures marked material will be selected.")
else:
    st.caption("All disclosures still requires saved disclosures for every company. Materiality is not required.")
try:
    value = peer_runs.selection(ticker, [part for part in re.split(r"[,\s]+", peer_text.strip()) if part],
                                int(year), items, mode)
except peer_runs.RunError as error:
    st.warning(str(error))
    value = None
run_peer_panel(value)

st.divider()
st.subheader("Saved comparisons")
labelled = {}
if config.data_source_mode() != "files":
    try:
        for ref in peer_runs.saved_reports():
            label = (f"{ref['ticker']} {ref['year']} vs {', '.join(ref['peers'])} | "
                     f"Items {', '.join(ref['items'])} | {ref['target_selection'].replace('_', ' ')} | PostgreSQL")
            labelled[label] = ("postgres", ref)
            if st.session_state.get("peer_finished_selection") == peer_runs.selection_key(ref):
                st.session_state["sel_peer"] = label
                del st.session_state["peer_finished_selection"]
    except peer_runs.RunError as error:
        st.warning(str(error))
for path in reports.list_peer_files():
    try:
        label = f"{reports.peer_label(path)} | Local file"
        if label in labelled:
            label += f" | {path.parent}"
        labelled[label] = ("files", path)
    except data.DataError:
        continue
if not labelled:
    st.info("No peer comparison output was found yet. Check prerequisites and start a comparison above.")
    st.stop()

st.sidebar.header("Comparison")
choice = st.sidebar.selectbox("Target, year and peers", list(labelled), key="sel_peer")
source, ref = labelled[choice]
try:
    peer = peer_runs.load_report(ref) if source == "postgres" else reports.load_peer(ref)
except (data.DataError, peer_runs.RunError) as error:
    st.error(str(error))
    st.stop()
st.caption("Report source: PostgreSQL" if source == "postgres" else "Report source: local file")

if peer.get("_sample_data"):
    st.warning(components.safe(f"Sample data: {peer['_sample_data']}"))
if not peer.get("run_complete", True):
    st.warning(f"Partial run: {peer.get('jobs_completed', '?')} of {peer.get('jobs_planned', '?')} target disclosures compared. "
               "Use the controls above with the same target, peers, year, Items and target selection to resume.")

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
