"""Launch and monitor the PostgreSQL-backed SEC disclosure pipeline."""
import re

import pandas as pd
import streamlit as st

from sec_ui import components, pipeline_runs


STAGES = (
    ("preprocessing", "Preprocessing", "Preparing filing text and financial tables",
     "Prepares filing text and financial tables in parallel and saves both to PostgreSQL. "
     "Disclosure extraction waits for both to finish. Missing sections are checked with AI; "
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

    if progress and progress.get('full_pipeline_complete') and progress.get('completion_source') == 'starter_data':
        st.subheader('Run status')
        st.success('Starter results are ready. View the saved Synthesis Report; no new pipeline run is needed.')
        st.caption(f"{ticker} | FY{', FY'.join(map(str, years))} | Imported starter reports")
        return

    phases = pipeline_runs.phase_progress(years, progress)
    by_name = {phase["phase"]: phase for phase in phases}
    completed = sum(phase["status"] == "complete" for phase in phases)
    job_status = (job or {}).get("status")
    full_complete = bool(progress and progress.get("full_pipeline_complete"))
    log = pipeline_runs.log_tail(ticker, years, job)
    exhausted = [entry for entry in (progress or {}).get('stages', [])
                 if entry.get('status') == 'stopped' and entry.get('retry_exhausted')]
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
    elif job_status == 'stopping':
        st.info('Stopping pipeline. Saved progress is retained; Resume pipeline will be available once it stops.')
    elif job_status == 'stopped' and job.get('stopped_by_user'):
        st.info('Pipeline stopped by you. Saved progress is retained. Resume this run, '
                'or change the company and fiscal years to start another pipeline.')
        if st.session_state.get('pipeline_notified_run') != job.get('run_id'):
            st.session_state['pipeline_notified_run'] = job['run_id']
            st.toast(f'{ticker} pipeline stopped', icon=':material/pause_circle:')
    elif job_status == "stopped" or exhausted and job_status not in {'starting', 'running'}:
        if exhausted:
            retries = max(entry['max_retries'] for entry in exhausted)
            st.error(f"Pipeline stopped after {retries} automatic retries. Saved work is retained; "
                     "click Resume pipeline to try again.")
        else:
            st.error("Pipeline stopped. Saved work is retained; resume the same selection to continue.")
        if "Cannot connect to PostgreSQL" in log:
            st.warning("PostgreSQL is unavailable. Start Docker Desktop, run `docker compose up -d` "
                       "in the project folder, and check `docker compose ps`. Then click Resume pipeline.")
        if job and st.session_state.get("pipeline_notified_run") != job.get("run_id"):
            st.session_state["pipeline_notified_run"] = job["run_id"]
            st.toast(f"{ticker} pipeline stopped", icon=":material/error:")
    elif job_status in {"starting", "running"}:
        for entry in (progress or {}).get('stages', []):
            if entry.get('status') == 'running' and (entry.get('retrying') or entry.get('retries_used', 0)):
                title = next(stage[1] for stage in STAGES if stage[0] == entry['phase'])
                number = entry.get('retry_number') if entry.get('retrying') else entry['retries_used']
                st.warning(f"{title}: automatic retry {number} of {entry['max_retries']}. "
                           "Saved results and successful responses are reused.")
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
        if phase['status'] == 'stopped' and (job or {}).get('stopped_by_user'):
            icon, color = ':material/pause_circle:', 'gray'
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


def run_setup(running):
    st.subheader("Run setup")
    query = (st.query_params.get("ticker"), st.query_params.get("years"))
    fields = ("pipeline_setup_ticker", "pipeline_setup_years")
    initialize = any(key not in st.session_state for key in fields)
    if not running and (initialize or st.session_state.get("pipeline_setup_query") != query):
        defaults = ("AMD", "2023, 2024, 2025")
        if None in query:
            history, _ = pipeline_runs.read_run_history()
            if history:
                latest = history[0]
                defaults = (latest['ticker'].upper(), ', '.join(map(str, latest['years'])))
        for key, value, default in zip(fields, query, defaults):
            st.session_state[key] = default if value is None else value
    st.session_state["pipeline_setup_query"] = query
    if running:
        st.session_state["pipeline_setup_ticker"] = running["ticker"].upper()
        st.session_state["pipeline_setup_years"] = ", ".join(map(str, running["years"]))

    locked = running is not None
    left, right = st.columns([1, 2])
    with left:
        ticker = st.text_input("Company ticker", key="pipeline_setup_ticker",
                               max_chars=20, disabled=locked).strip().upper()
    with right:
        year_input = st.text_input(
            "Fiscal years", key="pipeline_setup_years", disabled=locked,
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
    if locked:
        st.caption("Run setup is locked while this pipeline is running.")

    job, progress = None, None
    if valid_ticker and valid_years:
        try:
            job, progress = pipeline_runs.read_run(ticker, years)
        except pipeline_runs.RunError as error:
            st.error(str(error))
    complete = bool(progress and progress.get('full_pipeline_complete'))
    if not locked and (job or progress) and (job or {}).get('status') not in {'starting', 'running', 'stopping'}:
        selection = f"{ticker} | FY{', FY'.join(map(str, years))}"
        if complete:
            label = 'Starter results already available' if progress.get('completion_source') == 'starter_data' else 'Already completed'
            st.success(f'{label} for **{selection}**. '
                       'View the saved results on the Synthesis Report page.')
        elif progress:
            st.info(f'Previous run found for **{selection}**. Saved progress is available. '
                    'Select **Resume pipeline** to continue using the saved work.')
        else:
            st.warning(f'Previous attempt found for **{selection}**. '
                       'Completion has not been confirmed. Select **Resume pipeline** to try again.')

    confirmed = True
    if valid_years and len(years) > 2 and not (complete and not locked):
        selected = (ticker, *years)
        if st.session_state.get("confirmation_selection") != selected:
            st.session_state["confirmation_selection"] = selected
            st.session_state["confirm_multi_year"] = False
        if locked:
            st.session_state["confirm_multi_year"] = True
        st.warning(f"{len(years)} fiscal years create {len(years) - 1} comparison pairs. "
                   "This may take longer and use more API requests.")
        confirmed = st.checkbox("Continue with these fiscal years", key="confirm_multi_year", disabled=locked)
    return ticker, years, confirmed, job, progress


def resume_retry_options(ticker, years, job, progress, *, prefix='pipeline_retry', log=None):
    exhausted = any(entry.get('status') == 'stopped' and entry.get('retry_exhausted')
                    for entry in (progress or {}).get('stages', []))
    manual_stop = bool(job and job.get('stopped_by_user'))
    retry_failed = exhausted or manual_stop
    log = pipeline_runs.log_tail(ticker, years, job) if log is None else log
    retry_required = bool(job and job.get("status") == "stopped"
                          and not (exhausted or manual_stop) and "--retry-failed" in log)
    if retry_required:
        st.warning("An AI request failed or was interrupted. Select the retry option below to resume. "
                   "Successful responses and completed stages will be reused.")
        retry_failed = st.checkbox(
            "Retry failed AI requests on resume",
            key=f"{prefix}_{ticker}_{'-'.join(map(str, years))}_{job.get('run_id', '')}",
            help="Applies to failed requests in unfinished extraction, alignment, change analysis and materiality. "
                 "Existing request and token limits still apply.",
        )
    return retry_failed, not retry_required or retry_failed


@st.fragment(run_every="3s")
def run_panel():
    running = pipeline_runs.active_run()
    ticker, years, confirmed, job, progress = run_setup(running)
    valid = bool(re.fullmatch(r"[A-Z0-9][A-Z0-9.-]*", ticker)) and years is not None and len(years) >= 2

    selected_running = (job or {}).get("status") in {"starting", "running", "stopping"}
    complete = bool(progress and progress.get("full_pipeline_complete"))
    active_job = running or (job if selected_running else None)
    if active_job:
        stopping = active_job.get('status') == 'stopping'
        st.status('Stopping pipeline' if stopping else 'Pipeline running', state='running', expanded=False)
        if st.button('Stop pipeline', icon=':material/stop_circle:',
                     disabled=stopping or not active_job.get('pid'),
                     help='Stops processing and keeps completed results. Unfinished requests may run again when you resume.'):
            try:
                pipeline_runs.stop_run(active_job['ticker'], active_job['years'], run_id=active_job['run_id'])
            except pipeline_runs.RunError as error:
                st.error(str(error))
            else:
                st.rerun()
    elif not complete:
        label = "Resume pipeline" if job or progress else "Start pipeline"
        retry_failed, retry_allowed = resume_retry_options(ticker, years, job, progress)
        if st.button(label, icon=":material/play_arrow:", type="primary",
                     disabled=not (valid and confirmed and retry_allowed)):
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


def history_resume(record, records, active):
    if record['display_status'] not in {'Failed', 'Interrupted', 'Stopped by user', 'Unfinished'}:
        return
    ticker, years = record['ticker'].upper(), record['years']
    selection = f"{ticker} | FY{', FY'.join(map(str, years))}"
    st.markdown(f'**Resume {selection}**')
    st.caption('Continues from the latest saved checkpoints for this company and these years. '
               'Completed stages and successful responses are reused; a new history entry records the resume attempt.')
    latest = next(row for row in records if (row['ticker'], row['years']) == (record['ticker'], years))
    try:
        job, progress = pipeline_runs.read_run(ticker, years)
    except pipeline_runs.RunError as error:
        st.error(str(error))
        return
    complete = bool(progress and progress.get('full_pipeline_complete')) or latest['display_status'] == 'Completed'
    if complete:
        st.info('This company and fiscal-year selection has completed in a later attempt. View its saved Synthesis Report.')
    if active:
        st.info(f"{active['ticker'].upper()} is already running. Wait for it to finish or stop it before resuming another run.")
    retry_failed, retry_allowed = False, True
    if not complete and not active:
        job = job or latest
        retry_failed, retry_allowed = resume_retry_options(ticker, years, job, progress or latest.get('progress'),
            prefix='pipeline_history_retry', log=pipeline_runs.history_log(job))
    can_resume = not complete and active is None and retry_allowed
    if st.button('Resume pipeline', key='pipeline_history_resume', icon=':material/play_arrow:',
                 type='primary', disabled=not can_resume) and can_resume:
        try:
            pipeline_runs.start_run(ticker, years, resume_only=True, retry_failed=retry_failed)
        except pipeline_runs.RunError as error:
            st.error(str(error))
        else:
            st.query_params['ticker'] = ticker
            st.query_params['years'] = ', '.join(map(str, years))
            st.session_state['pipeline_history_notice'] = f'Resumed {selection}. Watch progress in Run setup & progress.'
            st.rerun(scope='app')


@st.fragment(run_every="10s")
def history_panel():
    st.subheader('Run history')
    st.caption('Past companies and fiscal years, newest first. Times are in Singapore time (SGT). '
               'New Start and Resume attempts are retained separately in PostgreSQL.')
    records, warning = pipeline_runs.read_run_history()
    if warning:
        st.warning(warning)
    if notice := st.session_state.pop('pipeline_history_notice', None):
        st.success(notice)
    st.button('Refresh history', icon=':material/refresh:', key='pipeline_history_refresh')
    if not records:
        st.info('No previous pipeline runs found. Your next run will appear here automatically.')
        return

    company, status, fiscal = st.columns(3)
    companies = {row['ticker'].upper() for row in records}
    previous_company = st.session_state.get('pipeline_history_company')
    if previous_company and previous_company != 'All companies':
        companies.add(previous_company)
    statuses = {row['display_status'] for row in records}
    previous_status = st.session_state.get('pipeline_history_status')
    if previous_status and previous_status != 'All statuses':
        statuses.add(previous_status)
    years = {year for row in records for year in row['years']}
    years.update(st.session_state.get('pipeline_history_years', []))
    chosen_company = company.selectbox('Company', ['All companies', *sorted(companies)],
                                       key='pipeline_history_company')
    chosen_status = status.selectbox('Status', ['All statuses', *sorted(statuses)],
                                    key='pipeline_history_status')
    chosen_years = fiscal.multiselect('Contains fiscal years', sorted(years),
                                      key='pipeline_history_years', help='Show runs containing every selected fiscal year.')
    filtered = [row for row in records if (chosen_company == 'All companies' or row['ticker'].upper() == chosen_company)
                and (chosen_status == 'All statuses' or row['display_status'] == chosen_status)
                and set(chosen_years).issubset(row['years'])]
    if not filtered:
        st.info('No runs match these filters.')
        return
    table = pd.DataFrame([{
        'Company': row['ticker'].upper(), 'Fiscal years': ', '.join(map(str, row['years'])),
        'Started (SGT)': pipeline_runs.history_time_label(row.get('started_at')),
        'Finished (SGT)': pipeline_runs.history_time_label(row['completed_at']) if row.get('completed_at') else '—',
        'Status': row['display_status'], 'Last stage': row['last_stage'],
    } for row in filtered])
    st.caption(f'{len(filtered)} run record(s). Older runs show the records/checkpoints that were retained; '
               'overwritten attempts cannot be reconstructed reliably.')
    st.dataframe(table, hide_index=True, width='stretch')
    st.download_button('Download run history CSV', table.to_csv(index=False).encode('utf-8'),
                       file_name='pipeline_run_history.csv', mime='text/csv', key='pipeline_history_download')

    with st.expander('Run details', expanded=True):
        lookup = {row['run_id']: row for row in filtered}
        if st.session_state.get('pipeline_history_run') not in lookup:
            st.session_state['pipeline_history_run'] = next(iter(lookup))

        def label(run_id):
            row = lookup[run_id]
            return (f"{pipeline_runs.history_time_label(row.get('started_at'))} | {row['ticker'].upper()} | "
                    f"FY{', FY'.join(map(str, row['years']))} | {row['display_status']}")

        selected = st.selectbox('Saved run', list(lookup), format_func=label, key='pipeline_history_run')
        record = lookup[selected]
        st.caption('History source: ' + record.get('source', 'Saved run'))
        if record.get('error'):
            st.warning(record['error'])
        active = pipeline_runs.active_run()
        history_resume(record, records, active)
        if st.button('Load selection in Run setup', key='pipeline_history_load', disabled=active is not None,
                     help='Fills in the company and years without starting a pipeline. Stop an active run before changing its setup.'):
            st.query_params['ticker'] = record['ticker'].upper()
            st.query_params['years'] = ', '.join(map(str, record['years']))
            st.rerun(scope='app')
        log = pipeline_runs.history_log(record)
        if log:
            st.caption('Recent activity from this attempt (up to 100 lines)')
            st.code(log, language='text')
        else:
            st.caption('Detailed activity text was not retained for this older run.')


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

setup_tab, history_tab = st.tabs(['Run setup & progress', 'Run history'])
with setup_tab:
    run_panel()
with history_tab:
    history_panel()
