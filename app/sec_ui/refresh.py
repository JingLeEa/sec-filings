"""Queue post-review pipeline work and display its durable progress."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from uuid import uuid4

import streamlit as st

from . import config, data


def backend():
    src = str(config.ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from sec_disclosure.pipelines import review_jobs
    return review_jobs


def queue_dir():
    return config.manual_reviews_dir() / "pipeline_refresh"


def request(stage, record, saved, mode, *, force=False):
    disclosures = config.disclosures_dir() or config.ROOT / "data/disclosures"
    alignments = config.alignments_dir() or config.ROOT / "data/alignments"
    pipeline_data = Path(os.environ.get("SEC_UI_PIPELINE_DATA_DIR",
                         config.ROOT / "data" if mode == "postgres" else Path(disclosures).parent))
    settings = {"input_source": mode, "data_dir": str(pipeline_data.resolve()),
                "disclosures_dir": str(Path(disclosures).resolve()), "alignments_dir": str(Path(alignments).resolve()),
                "reviews_dir": str(config.manual_reviews_dir().resolve()),
                "embedding_model": os.environ.get("SEC_UI_EMBEDDING_MODEL", "bge-m3")}
    if env_file := os.environ.get("SEC_UI_PIPELINE_ENV_FILE"):
        settings["env_file"] = str(Path(env_file).expanduser().resolve())
    jobs = backend()
    try:
        arguments = dict(stage=stage, company=record["company"],
                         previous=record["fiscal_year"] if stage == "disclosure" else record["previous_year"],
                         current=None if stage == "disclosure" else record["current_year"],
                         record_id=record["disclosure_id"] if stage == "disclosure" else saved["match_id"],
                         saved=saved, settings=settings)
        job = jobs.enqueue(queue_dir(), **arguments)
        if force and job["status"] == "failed":
            job = jobs.retry(queue_dir(), job["job_id"])
        elif force and job["status"] in {"complete", "superseded"}:
            arguments["settings"] = {**settings, "refresh_request_id": uuid4().hex}
            job = jobs.enqueue(queue_dir(), **arguments)
        jobs.launch_worker(queue_dir())
        return job
    except (OSError, ValueError) as error:
        raise data.DataError("Your review is saved, but the pipeline refresh could not start. "
                             "Use Refresh downstream results to retry. " + str(error)) from None


def worker_running():
    import fcntl
    path = queue_dir() / "worker.lock"
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


@st.fragment(run_every=3)
def status_panel(company, years):
    jobs = backend()
    rows = [row for row in jobs.jobs(queue_dir(), company)
            if set(years) & {row["previous_year"], row.get("current_year")}]
    if not rows:
        return
    active = [row for row in rows if row["status"] in jobs.ACTIVE]
    if active and not worker_running():
        try:
            jobs.launch_worker(queue_dir())
        except OSError:
            st.warning("The refresh worker could not start. Your reviews remain saved; retry the refresh below.")
    completed = tuple(row["job_id"] for row in rows if row["status"] == "complete")
    cache_key = f"_review_refresh_completed_{company}"
    if completed and st.session_state.get(cache_key) != completed:
        st.session_state[cache_key] = completed
        st.cache_data.clear()
        st.rerun(scope="app")
    with st.expander("Pipeline updates", expanded=bool(active or any(row["status"] == "failed" for row in rows))):
        for row in rows[:5]:
            scope = (f"FY{row['previous_year']}" if row["stage"] == "disclosure"
                     else f"FY{row['previous_year']}–FY{row['current_year']}")
            text = f"{scope} · {row['record_id'].rsplit('_', 1)[-1]} · {row['phase']}"
            if row["status"] == "failed":
                st.warning(text + ". " + row["error"])
                if st.button("Retry pipeline refresh", key="retry_refresh_" + row["job_id"]):
                    jobs.retry(queue_dir(), row["job_id"])
                    jobs.launch_worker(queue_dir())
                    st.rerun()
            elif row["status"] == "complete":
                st.success(text)
            elif row["status"] == "superseded":
                st.caption(text)
            else:
                st.info(text)
