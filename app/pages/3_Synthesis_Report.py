"""Read the synthesis agent's multi-year report, with every finding linked to its evidence."""
import json

import pandas as pd
import streamlit as st

from sec_ui import components, config, data, reports

components.setup("Synthesis report")
st.title("Synthesis report")
st.caption("A multi-year summary written by the synthesis agent from material changes and reported financial figures.")

files = reports.list_synthesis_files()
if not files:
    st.info("No synthesis output was found yet.")
    st.markdown(
        "The synthesis agent writes a JSON file such as `AMD_2023_2025_synthesis_output.json`. "
        "Put it in `data/synthesis/` or set `SEC_UI_SYNTHESIS_DIRS` to the folder that holds it, then reload."
    )
    st.caption("Looked in: " + ", ".join(str(p) for p in config.synthesis_dirs()))
    st.stop()

labelled = {}
for path in files:
    try:
        labelled[f"{reports.synthesis_label(path)} ({path.name})"] = path
    except Exception:  # noqa: BLE001
        continue
st.sidebar.header("Report")
choice = st.sidebar.selectbox("Synthesis output", list(labelled), key="sel_synthesis")
path = labelled[choice]
try:
    synth = reports.load_synthesis(path)
except data.DataError as error:
    st.error(str(error))
    st.stop()

report = synth["report"]
validation = synth.get("validation") or {}
summary = synth.get("input_summary") or {}

m1, m2, m3 = st.columns(3)
m1.metric("Material changes used", summary.get("material_changes_selected", "n/a"))
m2.metric("Financial figures supplied", summary.get("financial_metrics_supplied", "n/a"))
m3.metric("Citation check", "Passed" if validation.get("valid") else ("Not passed" if validation else "Not run"))
if validation.get("note"):
    st.caption(validation["note"])
if validation.get("invalid_evidence_ids"):
    st.error("Some evidence IDs in this report do not exist: " + ", ".join(validation["invalid_evidence_ids"]))

st.subheader("Executive summary")
st.write(components.safe(report.get("executive_summary")) or "Not provided.")

st.subheader("Key findings")
findings = report.get("key_findings", [])
if not findings:
    st.info("This report has no key findings.")
for number, finding in enumerate(findings, 1):
    with st.expander(components.safe(f"{number}. {finding.get('title', 'Untitled')}"), expanded=number == 1):
        st.write(components.safe(finding.get("analysis", "")))
        ids = finding.get("evidence_ids", [])
        if not ids:
            st.warning("This finding cites no evidence.")
        for evidence_id in ids:
            entry = reports.evidence_for(synth, evidence_id)
            metric = reports.metric_for(synth, evidence_id) if not entry else None
            if metric:
                st.markdown(f"**Reported figure** {components.safe(reports.metric_text(metric))}")
                st.caption(f"XBRL ID: {evidence_id}")
                continue
            if not entry:
                st.error(f"Evidence {evidence_id} is not in the source catalog.")
                continue
            materiality = entry.get("materiality") or {}
            st.markdown(f"**{evidence_id}** FY{entry.get('previous_year')} to FY{entry.get('current_year')}")
            components.chips((entry.get("taxonomy", "unlabelled"), ""), (entry.get("alignment_status", ""), components.tone_for_status(entry.get("alignment_status", ""))),
                             (entry.get("relationship", ""), ""))
            if materiality.get("reason"):
                st.caption(components.safe(materiality["reason"]))
            sources = entry.get("sources", [])
            if sources:
                frame = pd.DataFrame(sources).reindex(columns=["disclosure_id", "sentence_id", "text", "source_url"])
                st.dataframe(frame, hide_index=True, width="stretch", column_config={
                    "disclosure_id": "Disclosure ID", "sentence_id": "Sentence ID",
                    "text": st.column_config.TextColumn("Sentence", width="large"),
                    "source_url": st.column_config.LinkColumn("SEC filing", display_text="Open on sec.gov")})

for key, title in (("longitudinal_analysis", "How the disclosures changed over time"), ("financial_analysis", "Financial analysis")):
    if report.get(key):
        st.subheader(title)
        st.write(components.safe(report[key]))

trends = reports.trend_rows(synth)
if trends:
    st.subheader("Financial figures used")
    st.dataframe(pd.DataFrame(trends), hide_index=True, width="stretch")
    st.caption("Figures come from XBRL data in the filings. Source IDs are listed in the last column.")

limits = report.get("limitations") if isinstance(report.get("limitations"), list) else (
    [report["limitations"]] if report.get("limitations") else [])
limits += [x for x in synth.get("limitations", []) if x not in limits]
if limits:
    with st.expander("Limitations"):
        for item in limits:
            st.markdown(f"- {components.safe(item)}")
if synth.get("materiality_policy"):
    with st.expander("Which changes were included"):
        st.write(components.safe(synth["materiality_policy"]))

st.divider()
st.subheader("Download")
base = path.stem
d1, d2, d3 = st.columns(3)
d1.download_button("Report (Markdown)", reports.synthesis_markdown(synth), file_name=f"{base}.md", mime="text/markdown", width="stretch")
d2.download_button("Evidence (CSV)", reports.rows_to_csv(reports.synthesis_evidence_rows(synth)), file_name=f"{base}_evidence.csv",
                   mime="text/csv", width="stretch")
d3.download_button("Full output (JSON)", json.dumps(synth, indent=2, ensure_ascii=False), file_name=path.name,
                   mime="application/json", width="stretch")
st.caption(f"Source file: {path}")
