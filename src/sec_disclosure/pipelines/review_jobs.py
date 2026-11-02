"""Durable review refresh queue; model work runs outside the Streamlit process."""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager, ExitStack, redirect_stdout, redirect_stderr
import hashlib
import json
import os
from sec_disclosure.storage.artifacts import ArtifactPath as Path, ArtifactStorageError
import re
import subprocess
import sys
import time

from sec_disclosure.llm.disclosures import write_json
from sec_disclosure.storage.human_reviews import document_lock

ROOT = Path(__file__).resolve().parents[3]
ACTIVE = {"queued", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def job_path(queue, job_id):
    if not re.fullmatch(r"[a-f0-9]{24}", job_id):
        raise ValueError("Invalid pipeline refresh job ID.")
    return Path(queue) / "jobs" / f"{job_id}.json"


def enqueue(queue, *, stage, company, previous, current=None, record_id, saved, settings):
    if (stage not in {"disclosure", "alignment"} or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", company)
            or not 1900 <= int(previous) <= 2200
            or stage == "alignment" and (current is None or not int(previous) < int(current) <= 2200)):
        raise ValueError("Invalid review refresh scope.")
    if settings.get("input_source") not in {"files", "postgres"}:
        raise ValueError("Choose a file or PostgreSQL pipeline source.")
    identity = {"stage": stage, "company": company, "previous_year": int(previous),
                "current_year": int(current) if current is not None else None, "record_id": record_id,
                "review": saved, "settings": settings}
    job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    path = job_path(queue, job_id)
    with document_lock(path):
        if path.exists():
            return json.loads(path.read_text())
        job = {**identity, "job_id": job_id, "status": "queued", "phase": "Waiting to refresh",
               "created_at": now(), "updated_at": now(), "completed_steps": [], "attempts": 0, "error": None}
        write_json(path, job)
        return job


def jobs(queue, company=None):
    rows = []
    for path in sorted((Path(queue) / "jobs").glob("*.json")):
        row = json.loads(path.read_text())
        if company is None or row["company"] == company:
            rows.append(row)
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def retry(queue, job_id):
    path = job_path(queue, job_id)
    with document_lock(path):
        job = json.loads(path.read_text())
        if job["status"] == "failed":
            job.update(status="queued", error=None, phase="Waiting to retry", updated_at=now())
            write_json(path, job)
        return job


def launch_worker(queue):
    if os.environ.get("SEC_UI_REFRESH_AUTOSTART", "1") == "0":
        return
    queue = Path(queue)
    queue.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ROOT / "scripts/process_review_refresh.py"),
               "--queue-dir", str(queue.resolve())]
    if hasattr(queue, "artifact_store"):
        command.append("--postgres")
        if queue.artifact_store.env_file:
            command += ["--env-file", str(queue.artifact_store.env_file)]
        return subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
    with (queue / "worker.log").open("ab") as log:
        return subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)


@contextmanager
def worker_lock(queue):
    queue = Path(queue)
    if hasattr(queue, "artifact_store"):
        with ExitStack() as stack:
            try:
                stack.enter_context(queue.artifact_store.lock("review_refresh/worker"))
            except ValueError:
                yield False
                return
            yield True
    else:
        import fcntl
        queue.mkdir(parents=True, exist_ok=True)
        with (queue / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True


def worker_running(queue):
    with worker_lock(queue) as acquired:
        return not acquired


def process_queue(queue, *, runner=None, wait_for_pipeline=False, capture_output=False):
    """One worker owns the queue; a later invocation resumes a crashed worker."""
    if runner is None:
        from .review_refresh import run_job
        runner = run_job
    queue = Path(queue)
    queue.mkdir(parents=True, exist_ok=True)
    with worker_lock(queue) as acquired:
        if not acquired:
            return
        with ExitStack() as stack:
            if capture_output:
                log = stack.enter_context((queue / "worker.log").open("a"))
                stack.enter_context(redirect_stdout(log))
                stack.enter_context(redirect_stderr(log))
            _drain(queue, runner, wait_for_pipeline)


def _drain(queue, runner, wait_for_pipeline):
    # No other worker owns this lock, so any running job was interrupted.
    for job in jobs(queue):
        if job["status"] == "running":
            job.update(status="queued", phase="Resuming interrupted refresh", updated_at=now())
            write_json(job_path(queue, job["job_id"]), job)
    while pending := [row for row in reversed(jobs(queue)) if row["status"] == "queued"]:
        job = pending[0]
        path = job_path(queue, job["job_id"])

        def update(**values):
            job.update(values, updated_at=now())
            write_json(path, job)

        update(status="running", attempts=job["attempts"] + 1, error=None)
        try:
            effective = job
            if hasattr(queue, "artifact_store"):
                # Jobs survive process/checkout changes; their old virtual root
                # need not be registered in this worker process.
                effective = {**job, "settings": {**job["settings"], "reviews_dir": str(queue.parent),
                             "env_file": queue.artifact_store.env_file}}
            runner(effective, queue / "runs" / job["job_id"], update)
        except Exception as error:
            from .review_refresh import SupersededReview, PipelineBusy
            if isinstance(error, PipelineBusy):
                update(status="queued", phase="Waiting for the current company pipeline", error=None)
                if wait_for_pipeline:
                    time.sleep(3)
                    continue
                return
            elif isinstance(error, SupersededReview):
                update(status="superseded", phase="A newer review replaced this refresh", error=None)
            else:
                update(status="failed", phase="Refresh stopped; review is saved",
                       error=f"{type(error).__name__}: {error}"[:1200])
        else:
            endpoint = "synthesis" if job["settings"]["input_source"] == "postgres" else "materiality"
            phase = (f"Updated through {endpoint}" if job.get("affected_pairs", True)
                     else "Disclosure updated; no comparison years are available")
            update(status="complete", phase=phase, completed_at=now())
