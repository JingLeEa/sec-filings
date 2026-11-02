"""Launch and monitor the PostgreSQL-backed SEC disclosure pipeline."""
import re

import streamlit as st

from sec_ui import components, pipeline_runs


STAGES = (
    ("preprocessing", "Preprocessing", "Preparing filing text and financial tables",
     "Extracts filing sections and financial metrics, then saves them. Missing sections are checked with AI; "
     "timed-out downloads retry automatically, so this stage may take longer."),
    ("extraction", "Disclosure extraction", "Hang on, we are extracting disclosures",
     "Identifies company-specific disclosures and checks their supporting source sentences."),
    ("alignment", "Alignment", "Matching disclosures across years",
     "Connects related disclosures between selected filings and identifies unmatched ones."),
    ("change-analysis", "Change analysis", "Analyzing disclosure changes",
     "Compares aligned disclosures and labels their changes using the supporting text."),
    ("materiality", "Materiality", "Assessing materiality",
     "Evaluates whether each change meaningfully affects the company's business, risk, operations, or finances."),
    ("synthesis", "Synthesis", "Preparing the synthesis report",
     "Summarizes material changes and supporting evidence, then saves the report."),
)


def parse_years(value):
    parts = [part.strip() for part in value.split(",")]
    if not all(re.fullmatch(r"\d{4}", part) for part in parts):
        return None
    years = sorted({int(part) for part in parts})
    return years if all(1900 <= year <= 2200 for year in years) else None


def show_progress(ticker, years):
    try:
        job, progress = pipeline_runs.read_run(ticker, years)
    except pipeline_runs.RunError as error:
        st.error(str(error))
        return

    phases = pipeline_runs.phase_progress(years, progress)
    by_name = {phase["phase"]: phase for phase in phases}
    completed = sum(phase["status"] == "complete" for phase in phases)
    job_status = (job or {}).get("status")
    full_complete = bool(progress and progress.get("full_pipeline_complete"))
    log = pipeline_runs.log_tail(ticker, years, job)
    if job_status == "stopped":
        for phase in phases:
            if phase["status"] == "running":
                phase["status"] = "stopped"

    st.subheader("Run status")
    st.caption(f"{ticker} | FY{', FY'.join(map(str, years))} | {len(years) - 1} comparison pair(s)")

    if full_complete:
        st.success(f"Pipeline complete. All {len(STAGES)} stages have been saved to PostgreSQL.")
        if job and st.session_state.get("pipeline_notified_run") != job.get("run_id"):
            st.session_state["pipeline_notified_run"] = job["run_id"]
            st.cache_data.clear()
            st.toast(f"{ticker} pipeline complete", icon=":material/check_circle:")
    elif job_status == "complete":
        st.warning("The process exited, but the full-pipeline completion marker is missing. Check the activity log.")
    elif job_status == "stopped":
        st.error("Pipeline stopped. Saved work is retained; resume the same selection to continue.")
        if "Cannot connect to PostgreSQL" in log:
            st.warning("PostgreSQL is unavailable. Start Docker Desktop, run `docker compose up -d` "
                       "in the project folder, and check `docker compose ps`. Then click Resume pipeline.")
        if st.session_state.get("pipeline_notified_run") != job.get("run_id"):
            st.session_state["pipeline_notified_run"] = job["run_id"]
            st.toast(f"{ticker} pipeline stopped", icon=":material/error:")
    elif job_status in {"starting", "running"}:
        active = next((phase for phase in phases if phase["status"] == "running"), None)
        if active:
            _, _, message, description = next(stage for stage in STAGES if stage[0] == active["phase"])
            st.info(f"**{message}**\n\n{description}")
        else:
            st.info("Checking setup and preparing the next stage.")
    elif progress:
        st.info("Saved pipeline progress found. Continue the run to finish the remaining stages.")
    else:
        st.caption("No pipeline run found for this selection.")

    st.progress(completed / len(STAGES), text=f"{completed} of {len(STAGES)} stages complete")
    for index, (phase_key, title, _, description) in enumerate(STAGES, 1):
        phase = by_name[phase_key]
        label, icon, color = {
            "complete": ("Completed", ":material/check_circle:", "green"),
            "running": ("Running", ":material/progress_activity:", "blue"),
            "stopped": ("Stopped", ":material/error:", "red"),
            "waiting": ("Waiting", ":material/schedule:", "gray"),
        }[phase["status"]]
        st.markdown(f"**{index:02d}  {title}**")
        if phase["status"] == "running":
            st.markdown(
                '<span class="sec-pipeline-running-badge" role="status">'
                '<span class="sec-pipeline-running-spinner" aria-hidden="true"></span>Running</span>',
                unsafe_allow_html=True,
            )
        else:
            st.badge(label, icon=icon, color=color)
        st.caption(description)
        st.caption(f"{phase['saved']} of {phase['total']} outputs loaded to database")
        if phase["status"] == "running":
            detail_reader = getattr(pipeline_runs, "stage_progress", None)
            try:
                work = ([row for stage in phase["active"] for row in detail_reader(ticker, years, stage)]
                        if detail_reader else [])
            except pipeline_runs.RunError as error:
                st.warning(str(error))
                work = []
            if work:
                for row in work:
                    if row["total"]:
                        st.progress(min(1.0, row["done"] / row["total"]),
                                    text=f"{row['label']}: {row['done']} of {row['total']} steps")
                    else:
                        st.progress(0.0, text=f"{row['label']}: preparing")
                    if row["detail"]:
                        st.caption(row["detail"])
            else:
                st.progress(phase["saved"] / phase["total"],
                            text=f"{phase['saved']} of {phase['total']} database outputs")
                if detail_reader is None:
                    st.caption("Restart the dashboard to load detailed stage progress.")
        with st.expander("Database outputs", expanded=phase["status"] in {"running", "stopped"}):
            for output, saved in phase["outputs"]:
                st.markdown(f"**{output}**")
                st.badge("Loaded to database" if saved else "Waiting",
                         icon=":material/database:" if saved else ":material/schedule:",
                         color="green" if saved else "gray")
        if index < len(STAGES):
            st.divider()

    if job and log:
        with st.expander("Activity log", expanded=job_status == "stopped"):
            st.code(log, language="text")


@st.fragment(run_every="3s")
def run_panel(ticker, years, confirmed):
    valid = bool(re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", ticker)) and years is not None and len(years) >= 2
    running = pipeline_runs.active_run()
    job, progress = None, None
    if valid:
        try:
            job, progress = pipeline_runs.read_run(ticker, years)
        except pipeline_runs.RunError as error:
            st.error(str(error))

    selected_running = (job or {}).get("status") in {"starting", "running"}
    complete = bool(progress and progress.get("full_pipeline_complete"))
    if running:
        running_ticker = running["ticker"].upper()
        running_years = running["years"]
        if running_ticker != ticker or tuple(running_years) != tuple(years or ()):
            st.info(f"{running_ticker} is running for FY{', FY'.join(map(str, running_years))}. "
                    "Its progress is shown below. Wait for it to finish before starting another pipeline.")
        st.status("Pipeline running", state="running", expanded=False)
    elif selected_running:
        st.status("Pipeline running", state="running", expanded=False)
    elif not complete:
        label = "Resume pipeline" if job or progress else "Start pipeline"
        retry_failed = False
        retry_required = bool(job and job.get("status") == "stopped"
                              and "--retry-failed" in pipeline_runs.log_tail(ticker, years, job))
        if retry_required:
            st.warning("An AI request failed or was interrupted. Select the retry option below to resume. "
                       "Successful responses and completed stages will be reused.")
            retry_failed = st.checkbox(
                "Retry failed AI requests on resume",
                key=f"pipeline_retry_{ticker}_{'-'.join(map(str, years))}_{job.get('run_id', '')}",
                help="Applies to failed requests in unfinished extraction, alignment, change analysis and materiality. "
                     "Existing request and token limits still apply.",
            )
        if st.button(label, icon=":material/play_arrow:", type="primary",
                     disabled=not (valid and confirmed and (not retry_required or retry_failed))):
            try:
                options = {"retry_failed": True} if retry_failed else {}
                pipeline_runs.start_run(ticker, years, **options)
            except pipeline_runs.RunError as error:
                st.error(str(error))
            else:
                st.query_params["ticker"] = ticker
                st.query_params["years"] = ", ".join(map(str, years))
                st.rerun()

    st.divider()
    if running:
        show_progress(running["ticker"].upper(), running["years"])
    elif valid:
        show_progress(ticker, years)
    else:
        st.caption("Run status will appear after a valid ticker and fiscal years are entered.")


components.setup("Run pipeline")
st.markdown("""
<style>
.sec-pipeline-running-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.15rem 0.6rem;
    border-radius: 0.4rem;
    background: rgba(37, 99, 235, 0.12);
    color: #1d4ed8;
    font-size: 0.75rem;
    font-weight: 600;
}
.sec-pipeline-running-spinner {
    width: 0.75rem;
    height: 0.75rem;
    border: 2px solid currentColor;
    border-left-color: transparent;
    border-radius: 50%;
    animation: sec-pipeline-spin 0.8s linear infinite;
}
@keyframes sec-pipeline-spin { to { transform: rotate(360deg); } }
@media (prefers-color-scheme: dark) {
    .sec-pipeline-running-badge { color: #93c5fd; }
}
@media (prefers-reduced-motion: reduce) {
    .sec-pipeline-running-spinner { animation: none; }
}
</style>
""", unsafe_allow_html=True)
st.title("Run pipeline")
st.caption("Runs locally using PostgreSQL and the configured SEC and SoCLaaS credentials.")
st.caption("Before starting, make sure Docker Desktop is running and run `docker compose up -d` "
           "in the project folder to start PostgreSQL.")

st.subheader("Run setup")
left, right = st.columns([1, 2])
with left:
    ticker = st.text_input("Company ticker", value=st.query_params.get("ticker", "AMD"),
                           max_chars=20).strip().upper()
with right:
    year_input = st.text_input(
        "Fiscal years", value=st.query_params.get("years", "2023, 2024, 2025"),
        help="Enter two or more fiscal years, separated by commas. They do not need to be consecutive.",
    )
years = parse_years(year_input)
valid_ticker = bool(re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", ticker))
valid_years = years is not None and len(years) >= 2
if not valid_ticker:
    st.warning("Enter a ticker beginning with a letter or number, using only letters, numbers, periods, or hyphens.")
if not valid_years:
    st.warning("Enter at least two distinct fiscal years from 1900 to 2200, separated by commas.")

if valid_ticker and valid_years:
    st.caption("Comparisons: " + "; ".join(f"FY{a} to FY{b}" for a, b in zip(years, years[1:])))

confirmed = True
if valid_years and len(years) > 2:
    selected = (ticker, *years)
    if st.session_state.get("confirmation_selection") != selected:
        st.session_state["confirmation_selection"] = selected
        st.session_state["confirm_multi_year"] = False
    st.warning(f"{len(years)} fiscal years create {len(years) - 1} comparison pairs. "
               "This may take longer and use more API requests.")
    confirmed = st.checkbox("Continue with these fiscal years", key="confirm_multi_year")

run_panel(ticker, years, confirmed)
