"""Durable review refresh queue; model work runs outside the Streamlit process."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from sec_disclosure.llm.disclosures import write_json

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
    with (queue / "worker.log").open("ab") as log:
        return subprocess.Popen([sys.executable, str(ROOT / "scripts/process_review_refresh.py"),
                          "--queue-dir", str(queue.resolve())], cwd=ROOT, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def process_queue(queue, *, runner=None):
    """One worker owns the queue; a later invocation resumes a crashed worker."""
    import fcntl
    if runner is None:
        from .review_refresh import run_job
        runner = run_job
    queue = Path(queue)
    queue.mkdir(parents=True, exist_ok=True)
    with (queue / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
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
                runner(job, queue / "runs" / job["job_id"], update)
            except Exception as error:
                from .review_refresh import SupersededReview
                if isinstance(error, SupersededReview):
                    update(status="superseded", phase="A newer review replaced this refresh", error=None)
                else:
                    update(status="failed", phase="Refresh stopped; review is saved",
                           error=f"{type(error).__name__}: {error}"[:1200])
            else:
                phase = ("Updated through materiality" if job.get("affected_pairs", True)
                         else "Disclosure updated; no comparison years are available")
                update(status="complete", phase=phase, completed_at=now())
