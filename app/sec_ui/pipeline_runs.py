"""Launch full-pipeline jobs and read their durable progress for the dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock, Thread
from uuid import uuid4

from . import config


ROOT = config.ROOT
PHASES = ("preprocessing", "extraction", "alignment", "change-analysis", "materiality", "synthesis")
_START_LOCK = Lock()


class RunError(ValueError):
    """A pipeline job could not be started or its progress could not be read."""


def normalized_selection(ticker: str, years) -> tuple[str, tuple[int, ...]]:
    ticker = ticker.strip().lower()
    try:
        selected = tuple(sorted(set(years)))
    except TypeError as error:
        raise RunError("Select at least two valid fiscal years.") from error
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ticker):
        raise RunError("Enter a valid company ticker.")
    if len(selected) < 2 or any(not isinstance(year, int) or not 1900 <= year <= 2200 for year in selected):
        raise RunError("Select at least two distinct fiscal years from 1900 to 2200.")
    return ticker, selected


def run_folder(ticker: str, years) -> Path:
    ticker, years = normalized_selection(ticker, years)
    return ROOT / "data" / "pipeline_runs" / ticker / "-".join(map(str, years))


def _read_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RunError(f"Could not read pipeline progress: {path.name}.") from error
    if not isinstance(value, dict):
        raise RunError(f"Invalid pipeline progress: {path.name}.")
    return value


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pipeline_python() -> str:
    """Use the project's environment even when Streamlit was started elsewhere."""
    for path in (ROOT / ".venv" / "bin" / "python", ROOT / ".venv" / "Scripts" / "python.exe"):
        if path.is_file():
            return str(path)
    return sys.executable


def active_run() -> dict | None:
    """Return the UI-launched pipeline still running anywhere in this workspace."""
    runs = ROOT / "data" / "pipeline_runs"
    for path in runs.glob("*/*/ui_run.json"):
        try:
            job = _read_json(path)
        except RunError:
            continue
        if job and job.get("status") in {"starting", "running"} and _pid_alive(job.get("pid")):
            return job
    return None


def _finish_job(process: subprocess.Popen, folder: Path, run_id: str) -> None:
    code = process.wait()
    path = folder / "ui_run.json"
    try:
        job = _read_json(path)
        if job and job.get("run_id") == run_id:
            job.update(status="complete" if code == 0 else "stopped", exit_code=code,
                       completed_at=datetime.now(timezone.utc).isoformat())
            _write_json(path, job)
    except (OSError, RunError):
        # PostgreSQL progress and the local console log remain available if this write fails.
        pass


def start_run(ticker: str, years, *, retry_failed: bool = False) -> dict:
    ticker, years = normalized_selection(ticker, years)
    folder = run_folder(ticker, years)
    with _START_LOCK:
        running = active_run()
        if running:
            raise RunError(f"{running['ticker'].upper()} is already running for FY{', FY'.join(map(str, running['years']))}. "
                           "Wait for it to finish before starting another pipeline.")
        folder.mkdir(parents=True, exist_ok=True)
        previous = _read_json(folder / "ui_run.json")
        if previous and previous.get("status") in {"starting", "running"} and _pid_alive(previous.get("pid")):
            raise RunError("This pipeline is already running. Watch its progress below.")

        run_id = uuid4().hex
        log_name = f"ui_run_{run_id[:12]}.log"
        job = {"run_id": run_id, "ticker": ticker, "years": list(years),
               "status": "starting", "started_at": datetime.now(timezone.utc).isoformat(),
               "log_file": log_name, "retry_failed": retry_failed}
        _write_json(folder / "ui_run.json", job)
        command = [_pipeline_python(), "-u", str(ROOT / "scripts" / "run_full_pipeline.py"),
                   "--ticker", ticker, "--years", *map(str, years), "--data-dir", str(ROOT / "data")]
        if retry_failed:
            command.append("--retry-failed")
        try:
            with (folder / log_name).open("w", encoding="utf-8") as output:
                process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                           start_new_session=True)
        except OSError as error:
            job.update(status="stopped", error=f"Could not start pipeline ({type(error).__name__}).",
                       completed_at=datetime.now(timezone.utc).isoformat())
            _write_json(folder / "ui_run.json", job)
            raise RunError(job["error"]) from error
        job.update(pid=process.pid, status="running")
        _write_json(folder / "ui_run.json", job)
        Thread(target=_finish_job, args=(process, folder, run_id), daemon=True).start()
        return job


def read_run(ticker: str, years) -> tuple[dict | None, dict | None]:
    folder = run_folder(ticker, years)
    job = _read_json(folder / "ui_run.json")
    progress = _progress_documents(ticker, years).get(_status_key(ticker, years))
    if job and progress and progress.get("last_invocation_at", "") < job.get("started_at", ""):
        progress = None
    if job and job.get("status") in {"starting", "running"}:
        job = dict(job)
        if progress and progress.get("full_pipeline_complete"):
            job["status"] = "complete"
        elif job["status"] == "running" and not _pid_alive(job.get("pid")):
            job["status"] = "stopped"
            job.setdefault("error", "The pipeline process ended. Check the activity log for details.")
    return job, progress


def log_tail(ticker: str, years, job: dict | None, limit: int = 24) -> str:
    if not job:
        return ""
    name = job.get("log_file", "")
    if not isinstance(name, str) or not re.fullmatch(r"ui_run_[a-f0-9]{12}\.log", name):
        return ""
    path = run_folder(ticker, years) / name
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 16384))
            return "\n".join(handle.read().decode("utf-8", errors="replace").splitlines()[-limit:])
    except OSError:
        return ""


def database_saves(years) -> dict[str, list[tuple[str, str]]]:
    years = tuple(sorted(set(years)))
    pairs = list(zip(years, years[1:]))
    return {
        "preprocessing": ([(f"FY{year} filing text", f"save_text_{year}") for year in years]
                          + [(f"FY{year} financial tables", f"save_xbrl_{year}") for year in years]),
        "extraction": [("Disclosures", "extract_disclosures_and_save")],
        "alignment": [(f"FY{a} to FY{b} alignments", f"align_and_save_{a}_{b}") for a, b in pairs],
        "change-analysis": [output for a, b in pairs for output in (
            (f"FY{a} to FY{b} LLM change labels", f"llm_changes_{a}_{b}"),
            (f"FY{a} to FY{b} complete change analysis", f"save_changes_{a}_{b}"),
        )],
        "materiality": [(f"FY{a} to FY{b} materiality", f"save_materiality_{a}_{b}") for a, b in pairs],
        "synthesis": [("Synthesis report", "synthesis_and_save")],
    }


def phase_progress(years, progress: dict | None) -> list[dict]:
    entries = {entry.get("stage"): entry for entry in (progress or {}).get("stages", [])
               if isinstance(entry, dict)}
    result = []
    for phase, saves in database_saves(years).items():
        outputs = [(label, entries.get(stage, {}).get("status") == "complete") for label, stage in saves]
        saved = sum(done for _, done in outputs)
        phase_entries = [entry for entry in entries.values() if entry.get("phase") == phase]
        if saved == len(saves):
            status = "complete"
        elif any(entry.get("status") == "stopped" for entry in phase_entries):
            status = "stopped"
        elif any(entry.get("status") == "running" for entry in phase_entries):
            status = "running"
        else:
            status = "waiting"
        active = [entry["stage"] for entry in phase_entries if entry.get("status") == "running"]
        result.append({"phase": phase, "status": status, "saved": saved, "total": len(saves),
                       "outputs": outputs, "active": active})
    return result


def _optional_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _status_key(ticker, years):
    ticker, years = normalized_selection(ticker, years)
    return f"pipeline_runs/{ticker}/{'-'.join(map(str, years))}/status.json"


def _postgres_documents(keys, jobs_prefix=None):
    """Read public progress JSON without loading source files or model caches."""
    from .data import DataError, _postgres_connection

    try:
        with _postgres_connection() as connection, connection.cursor() as cursor:
            connection.read_only = True
            cursor.execute("SELECT to_regclass('pipeline_artifacts')")
            if cursor.fetchone()[0] is None:
                return {}
            cursor.execute(
                "SELECT path, content FROM pipeline_artifacts "
                "WHERE namespace = %s AND kind = 'file' AND "
                "(path = ANY(%s) OR (starts_with(path, %s) AND "
                "path ~ '/jobs/(matching|verification)_[^/]+\\.json$'))",
                ("main", list(keys), jobs_prefix or "/__no_jobs__/"),
            )
            rows = cursor.fetchall()
    except DataError:
        return {}
    except Exception:
        raise RunError("Could not read pipeline progress from PostgreSQL. Check the database connection.") from None
    documents = {}
    for key, content in rows:
        try:
            documents[key] = json.loads(bytes(content))
        except (TypeError, ValueError):
            if key == keys[0]:
                raise RunError("Invalid pipeline progress: status.json.") from None
    if keys[0] in documents and not isinstance(documents[keys[0]], dict):
        raise RunError("Invalid pipeline progress: status.json.")
    return documents


def _progress_documents(ticker, years, keys=(), jobs_prefix=None):
    status_key = _status_key(ticker, years)
    requested = [status_key, *keys]
    documents = _postgres_documents(requested, jobs_prefix)
    # A database run owns its checkpoints, even if stale local files exist.
    if status_key in documents:
        return documents
    documents = {status_key: _read_json(ROOT / "data" / status_key)}
    documents.update({key: _optional_json(ROOT / "data" / key) for key in keys})
    if jobs_prefix:
        for pattern in ("matching_*.json", "verification_*.json"):
            for path in (ROOT / "data" / jobs_prefix).glob(pattern):
                documents[str(path.relative_to(ROOT / "data"))] = _optional_json(path)
    return documents


def _progress_row(label: str, done: int | None = None, total: int | None = None,
                  detail: str = "") -> dict:
    return {"label": label, "done": done, "total": total, "detail": detail}


def stage_progress(ticker: str, years, stage: str) -> list[dict]:
    """Read durable agent checkpoints for the currently running pipeline step."""
    ticker, years = normalized_selection(ticker, years)
    data = ROOT / "data"
    if stage == "extract_disclosures_and_save":
        documents = _progress_documents(ticker, years, [
            f"disclosures/{ticker}/{year}/{name}.json"
            for year in years for name in ("token_usage", "manifest")
        ])
        rows = []
        for year in years:
            folder = data / "disclosures" / ticker / str(year)
            usage = documents.get(str((folder / "token_usage.json").relative_to(data))) or {}
            manifest = documents.get(str((folder / "manifest.json").relative_to(data))) or {}
            batches = usage.get("planned_batches") or len(manifest.get("batches", []))
            if not batches:
                rows.append(_progress_row(f"FY{year}: preparing extraction batches"))
                continue
            pending = usage.get("pending_batches")
            failed = usage.get("failed_batches") or []
            batch_done = max(0, batches - len(pending) - len(failed)) if isinstance(pending, list) else 0
            checks = usage.get("planned_boundary_checks", 0)
            check_pending = usage.get("pending_boundary_checks")
            check_failed = usage.get("failed_boundary_checks") or []
            check_done = max(0, checks - len(check_pending) - len(check_failed)) if isinstance(check_pending, list) else 0
            consolidations = usage.get("planned_consolidations", 0)
            consolidation_pending = usage.get("pending_consolidations")
            consolidation_failed = usage.get("failed_consolidations") or []
            consolidation_done = (max(0, consolidations - len(consolidation_pending) - len(consolidation_failed))
                                  if isinstance(consolidation_pending, list) else 0)
            total = batches + checks + consolidations
            done = total if usage.get("run_complete") else batch_done + check_done + consolidation_done
            if usage.get("run_complete"):
                batch_done, check_done, consolidation_done = batches, checks, consolidations
            detail = f"{batch_done}/{batches} batches"
            if checks:
                detail += f" · {check_done}/{checks} boundary checks"
            if consolidations:
                detail += f" · {consolidation_done}/{consolidations} consolidations"
            if usage.get("run_complete"):
                detail += " · extraction complete; saving to database"
            rows.append(_progress_row(f"FY{year} disclosure extraction", done, total, detail))
        return rows

    match = re.fullmatch(r"align_and_save_(\d{4})_(\d{4})", stage)
    if match:
        previous, current = match.groups()
        folder = data / "alignments" / ticker / f"{previous}-{current}"
        prefix = str(folder.relative_to(data))
        documents = _progress_documents(ticker, years,
                                        [f"{prefix}/{name}.json" for name in ("alignments", "candidate_matches", "proposed_groups")],
                                        f"{prefix}/jobs/")
        report = documents.get(f"{prefix}/alignments.json") or {}
        label = f"FY{previous} to FY{current} alignment"
        if report.get("run_complete"):
            return [_progress_row(label, 1, 1, "Matching and verification done; saving to database")]
        candidates = documents.get(f"{prefix}/candidate_matches.json")
        if not isinstance(candidates, dict):
            return [_progress_row(label, detail="Preparing candidate matches")]
        total = len(candidates)
        matching = [saved for key, saved in documents.items()
                    if key.startswith(f"{prefix}/jobs/matching_") and isinstance(saved, dict)]
        matched = sum(len(saved["anchors"]) for saved in matching if isinstance(saved.get("anchors"), list))
        if matched < total:
            jobs = len(matching)
            return [_progress_row(label, min(matched, total), total,
                                  f"Matching {matched}/{total} current disclosures · {jobs} batch jobs saved")]
        groups = documents.get(f"{prefix}/proposed_groups.json")
        if not isinstance(groups, list):
            return [_progress_row(label, detail="Grouping matched disclosures")]
        if not groups:
            return [_progress_row(label, detail="Finalizing exact matches")]
        verified = sum(len(saved.get("groups", [])) for key, saved in documents.items()
                       if key.startswith(f"{prefix}/jobs/verification_") and isinstance(saved, dict)
                       and not saved.get("repair_pending")
                       and isinstance(saved.get("result"), list))
        return [_progress_row(label, min(verified, len(groups)), len(groups),
                              f"Verified {min(verified, len(groups))}/{len(groups)} disclosure groups")]

    match = re.fullmatch(r"materiality_(\d{4})_(\d{4})", stage)
    if match:
        previous, current = match.groups()
        key = f"alignments/{ticker}/{previous}-{current}/materiality/summary.json"
        summary = _progress_documents(ticker, years, [key]).get(key) or {}
        total = summary.get("alignment_rows")
        done = summary.get("classified_rows")
        if isinstance(total, int) and total > 0 and isinstance(done, int):
            return [_progress_row(f"FY{previous} to FY{current} materiality", min(done, total), total,
                                  f"{min(done, total)}/{total} alignment rows classified")]
        return [_progress_row(f"FY{previous} to FY{current} materiality", detail="Preparing classifications")]

    match = re.fullmatch(r"llm_changes_(\d{4})_(\d{4})", stage)
    if match:
        previous, current = match.groups()
        key = f"alignments/{ticker}/{previous}-{current}/alignments.json"
        report = _progress_documents(ticker, years, [key]).get(key) or {}
        summary = report.get("change_analysis_summary") or {}
        total = summary.get("eligible_alignments")
        states = summary.get("by_status") or {}
        if isinstance(total, int) and total > 0:
            done = min(total, states.get("completed", 0) + states.get("needs_review", 0))
            return [_progress_row(f"FY{previous} to FY{current} LLM change analysis", done, total,
                                  f"{done}/{total} eligible alignments classified")]
        return [_progress_row(f"FY{previous} to FY{current} LLM change analysis",
                              detail="Preparing change-analysis batches")]

    return []
