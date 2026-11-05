"""Launch full-pipeline jobs and read their durable progress for the dashboard."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from threading import Lock, Thread
from uuid import uuid4

import psutil

from . import config


ROOT = config.ROOT
PHASES = ("preprocessing", "extraction", "alignment", "change-analysis", "materiality", "synthesis")
_START_LOCK = Lock()
HISTORY_PREFIX = 'pipeline_run_history/'
_HISTORY_TIMEZONE = ZoneInfo('Asia/Singapore')


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


def _history_store():
    src = str(config.ROOT / 'src')
    if src not in sys.path:
        sys.path.insert(0, src)
    from sec_disclosure.storage.artifacts import PostgresArtifactStore
    return PostgresArtifactStore('main')


def _history_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (AttributeError, TypeError, ValueError):
        return None


def history_time_label(value):
    parsed = _history_time(value)
    return parsed.astimezone(_HISTORY_TIMEZONE).strftime('%d %b %Y, %H:%M:%S') if parsed else 'Unknown'


def _matching_progress(job, progress):
    if not isinstance(progress, dict):
        return False
    started = _history_time(job.get('started_at'))
    invocation = _history_time((progress or {}).get('last_invocation_at') or (progress or {}).get('started_at'))
    ended = _history_time(job.get('completed_at'))
    return bool(started and invocation and started <= invocation and (ended is None or invocation <= ended))


def _record_history(job, *, archive=False):
    """One durable entry per dashboard invocation; resumes never replace older IDs."""
    try:
        ticker, years = normalized_selection(job['ticker'], job['years'])
        run_id = job['run_id']
        if not isinstance(run_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', run_id):
            raise ValueError('invalid run ID')
        store = _history_store()
        store.ensure_schema()
        key = HISTORY_PREFIX + run_id + '.json'
        saved = store.get(key)
        saved = json.loads(saved[1]) if saved and saved[0] == 'file' else {}
        if not isinstance(saved, dict):
            saved = {}
        # After a dashboard restart the old process monitor may be gone. Preserve
        # its final checkpoint before a resume replaces the selection's status.
        # An already finished record must not regress to a stale launcher copy.
        if archive and _history_time(saved.get('completed_at')):
            return
        record = {**saved, **job, 'ticker': ticker, 'years': list(years), 'source': 'Dashboard',
                  'activity_log': log_tail(ticker, years, job, limit=100) or saved.get('activity_log', '')}
        if archive and record.get('status') in {'starting', 'running', 'stopping'} and not _pid_alive(record.get('pid')):
            record['status'] = 'stopped'
        progress = _postgres_documents([_status_key(ticker, years)]).get(_status_key(ticker, years))
        if _matching_progress(job, progress):
            record['progress'] = progress
            if not record.get('completed_at') and progress.get('completed_at'):
                record['completed_at'] = progress['completed_at']
        store.put(key, json.dumps(record, ensure_ascii=False).encode('utf-8'))
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        raise RunError('Could not save run history to PostgreSQL. Check the database connection.') from None


def _write_job(folder, job, *, require_history=False):
    job.pop('history_warning', None)
    _write_json(folder / 'ui_run.json', job)
    try:
        _record_history(job)
    except RunError as error:
        job['history_warning'] = str(error)
        _write_json(folder / 'ui_run.json', job)
        if require_history:
            raise


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid < 1:
        return False
    # os.kill(pid, 0) terminates the process on Windows.
    return psutil.pid_exists(pid)


def _launch_options():
    options = {'env': {**os.environ, 'PYTHONIOENCODING': 'utf-8'}}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    return options


def _pipeline_python() -> str:
    """Use the project's environment even when Streamlit was started elsewhere."""
    relative = 'Scripts/python.exe' if os.name == 'nt' else 'bin/python'
    path = ROOT / '.venv' / relative
    return str(path) if path.is_file() else sys.executable


def active_run() -> dict | None:
    """Return the UI-launched pipeline still running anywhere in this workspace."""
    runs = ROOT / "data" / "pipeline_runs"
    for path in runs.glob("*/*/ui_run.json"):
        try:
            job = _read_json(path)
        except RunError:
            continue
        if job and job.get("status") in {"starting", "running", "stopping"} and _pid_alive(job.get("pid")):
            return job
    return None


def _finish_job(process: subprocess.Popen, folder: Path, run_id: str) -> None:
    code = process.wait()
    path = folder / "ui_run.json"
    try:
        with _START_LOCK:
            job = _read_json(path)
            if job and job.get("run_id") == run_id:
                job.update(status="complete" if code == 0 else "stopped", exit_code=code,
                           completed_at=datetime.now(timezone.utc).isoformat())
                _write_job(folder, job)
    except (OSError, RunError):
        # PostgreSQL progress and the local console log remain available if this write fails.
        pass


def start_run(ticker: str, years, *, retry_failed: bool = False, resume_only: bool = False) -> dict:
    ticker, years = normalized_selection(ticker, years)
    folder = run_folder(ticker, years)
    with _START_LOCK:
        running = active_run()
        if running:
            raise RunError(f"{running['ticker'].upper()} is already running for FY{', FY'.join(map(str, running['years']))}. "
                           "Wait for it to finish before starting another pipeline.")
        _, progress = read_run(ticker, years)
        if progress and progress.get('full_pipeline_complete'):
            if progress.get('completion_source') == 'starter_data':
                raise RunError('Starter results for this company and fiscal-year selection are already available. View the saved Synthesis Report.')
            if resume_only:
                raise RunError('This company and fiscal-year selection has already completed. Refresh run history to see its latest result.')
        folder.mkdir(parents=True, exist_ok=True)
        previous = _read_json(folder / "ui_run.json")
        if previous and previous.get("status") in {"starting", "running", "stopping"} and _pid_alive(previous.get("pid")):
            raise RunError("This pipeline is already running. Watch its progress below.")
        # Resume permits unfinished requests to run again; completed responses
        # remain cached and the existing request/token budgets still apply.
        retry_failed = retry_failed or bool(previous and previous.get('stopped_by_user'))

        run_id = uuid4().hex
        log_name = f"ui_run_{run_id[:12]}.log"
        job = {"run_id": run_id, "ticker": ticker, "years": list(years),
               "status": "starting", "started_at": datetime.now(timezone.utc).isoformat(),
               "log_file": log_name, "retry_failed": retry_failed}
        try:
            if previous:
                _record_history(previous, archive=True)
            _write_job(folder, job, require_history=True)
        except RunError as error:
            job.update(status='stopped', error=str(error), completed_at=datetime.now(timezone.utc).isoformat())
            _write_json(folder / 'ui_run.json', job)
            raise
        command = [_pipeline_python(), "-u", str(ROOT / "scripts" / "run_full_pipeline.py"),
                   "--ticker", ticker, "--years", *map(str, years), "--data-dir", str(ROOT / "data")]
        if retry_failed:
            command.append("--retry-failed")
        try:
            with (folder / log_name).open("w", encoding="utf-8") as output:
                process = subprocess.Popen(command, cwd=ROOT, stdout=output, stderr=subprocess.STDOUT,
                                           **_launch_options())
        except OSError as error:
            job.update(status="stopped", error=f"Could not start pipeline ({type(error).__name__}).",
                       completed_at=datetime.now(timezone.utc).isoformat())
            _write_job(folder, job)
            raise RunError(job["error"]) from error
        job.update(pid=process.pid, status="running")
        _write_job(folder, job)
        Thread(target=_finish_job, args=(process, folder, run_id), daemon=True).start()
        return job


def _pipeline_process_matches(pid, ticker, years) -> bool:
    """Do not signal a stale PID that now belongs to another program/run."""
    try:
        arguments = psutil.Process(pid).cmdline()
        script = os.path.normcase(str((ROOT / 'scripts/run_full_pipeline.py').resolve()))
        if not any(os.path.normcase(str(Path(argument).resolve())) == script for argument in arguments[1:]):
            return False
        selected_ticker = arguments[arguments.index('--ticker') + 1]
        start = arguments.index('--years') + 1
        selected_years = []
        for value in arguments[start:]:
            if value.startswith('--'):
                break
            selected_years.append(int(value))
        return selected_ticker.lower() == ticker and tuple(sorted(set(selected_years))) == years
    except (psutil.Error, OSError, ValueError, IndexError):
        return False


def _finish_stop(folder, run_id):
    """Also finish stops after a dashboard restart lost the Popen monitor."""
    while True:
        with _START_LOCK:
            path = folder / 'ui_run.json'
            try:
                job = _read_json(path)
                if not job or job.get('run_id') != run_id or job.get('status') != 'stopping':
                    return
                if not _pid_alive(job.get('pid')):
                    job.update(status='stopped', completed_at=datetime.now(timezone.utc).isoformat())
                    _write_job(folder, job)
                    return
            except (OSError, RunError):
                return
        time.sleep(0.2)


def stop_run(ticker: str, years, *, run_id: str) -> dict:
    """Stop only the selected local runner; preserve every committed checkpoint."""
    ticker, years = normalized_selection(ticker, years)
    folder = run_folder(ticker, years)
    path = folder / 'ui_run.json'
    with _START_LOCK:
        job = _read_json(path)
        if not job or job.get('run_id') != run_id:
            raise RunError('The pipeline run changed. Refresh the page before stopping it.')
        if job.get('status') not in {'starting', 'running', 'stopping'}:
            return job
        if not _pid_alive(job.get('pid')):
            job.update(status='stopped', completed_at=datetime.now(timezone.utc).isoformat())
            _write_job(folder, job)
            return job
        if job['status'] == 'stopping':
            return job
        pid = job['pid']
        if pid <= 1 or not _pipeline_process_matches(pid, ticker, years):
            raise RunError('Cannot verify the pipeline process. Refresh the page; no process was stopped.')
        if os.name == 'posix':
            try:
                if os.getpgid(pid) != pid or pid == os.getpgrp():
                    raise RunError('The pipeline does not have its own process group; no process was stopped.')
            except ProcessLookupError:
                job.update(status='stopped', completed_at=datetime.now(timezone.utc).isoformat())
                _write_job(folder, job)
                return job
        previous = dict(job)
        job.update(status='stopping', stopped_by_user=True,
                   stop_requested_at=datetime.now(timezone.utc).isoformat())
        _write_job(folder, job)
        try:
            if os.name == 'posix':
                os.killpg(pid, signal.SIGTERM)
            else:
                os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            job.update(status='stopped', completed_at=datetime.now(timezone.utc).isoformat())
            _write_job(folder, job)
            return job
        except OSError as error:
            _write_job(folder, previous)
            raise RunError('Could not stop the pipeline process. Check process permissions and try again.') from error
        Thread(target=_finish_stop, args=(folder, run_id), daemon=True).start()
        return job


def read_run(ticker: str, years) -> tuple[dict | None, dict | None]:
    folder = run_folder(ticker, years)
    job = _read_json(folder / "ui_run.json")
    progress = _progress_documents(ticker, years).get(_status_key(ticker, years))
    if job and progress and progress.get("last_invocation_at", "") < job.get("started_at", ""):
        progress = None
    if job and job.get("status") in {"starting", "running", "stopping"}:
        job = dict(job)
        if progress and progress.get("full_pipeline_complete"):
            job["status"] = "complete"
        elif job["status"] in {'running', 'stopping'} and not _pid_alive(job.get("pid")):
            job["status"] = "stopped"
            if not job.get('stopped_by_user'):
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
        if any(entry.get("status") == "stopped" for entry in phase_entries):
            status = "stopped"
        elif any(entry.get("status") == "running" for entry in phase_entries):
            status = "running"
        elif saved == len(saves):
            status = "complete"
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


def _history_documents():
    """Read only run metadata/checkpoints, never filing or model-response caches."""
    from .data import _postgres_connection
    try:
        with _postgres_connection() as connection, connection.cursor() as cursor:
            connection.read_only = True
            cursor.execute("SELECT to_regclass('pipeline_artifacts')")
            if cursor.fetchone()[0] is None:
                return {}
            cursor.execute(
                "SELECT path, content FROM pipeline_artifacts WHERE namespace=%s AND kind='file' AND "
                "(starts_with(path,%s) OR path ~ '^pipeline_runs/[^/]+/[0-9-]+/status\\.json$')",
                ('main', HISTORY_PREFIX))
            rows = cursor.fetchall()
    except Exception:
        raise RunError('Run history could not be loaded from PostgreSQL. Showing available local launcher records.') from None
    documents = {}
    for key, content in rows:
        try:
            value = json.loads(bytes(content))
            if isinstance(value, dict):
                documents[key] = value
        except (TypeError, ValueError):
            continue
    return documents


def _history_progress(value):
    if not isinstance(value, dict):
        return {}
    stages = value.get('stages')
    return {**value, 'stages': [item for item in stages if isinstance(item, dict)]
            if isinstance(stages, list) else []}


def _history_entry(value):
    try:
        ticker, years = normalized_selection(value['ticker'], value['years'])
        if not isinstance(value.get('run_id', ''), str):
            return None
        return {**value, 'ticker': ticker, 'years': list(years),
                'status': value.get('status') if isinstance(value.get('status'), str) else None,
                'source': value.get('source') if isinstance(value.get('source'), str) else 'Saved run',
                **({'progress': _history_progress(value['progress'])} if 'progress' in value else {})}
    except (RunError, KeyError, TypeError, AttributeError):
        return None


def _history_status(record):
    progress = record.get('progress') or {}
    status = record.get('status')
    if record.get('stopped_by_user') and status in {'stopped', 'stopping'}:
        return 'Stopping' if status == 'stopping' else 'Stopped by user'
    if progress.get('full_pipeline_complete'):
        return 'Completed'
    if status in {'starting', 'running', 'stopping'}:
        if record.get('_local') and _pid_alive(record.get('pid')):
            return {'starting': 'Starting', 'running': 'Running', 'stopping': 'Stopping'}[status]
        if not progress.get('complete') and not any(entry.get('status') == 'stopped' for entry in progress.get('stages', [])):
            return 'Interrupted' if record.get('_local') else 'Unfinished'
    if status == 'stopped' or any(entry.get('status') == 'stopped' for entry in progress.get('stages', [])):
        return 'Failed' if (record.get('error') or record.get('exit_code') or
                            any(entry.get('exit_code') for entry in progress.get('stages', []))) else 'Interrupted'
    if progress.get('complete'):
        return 'Completed to ' + str(progress.get('until', 'selected stage')).replace('-', ' ')
    return 'Finished (unconfirmed)' if status == 'complete' else 'Saved progress'


def _history_last_stage(record):
    if (record.get('progress') or {}).get('completion_source') == 'starter_data':
        return 'Starter reports imported'
    stages = (record.get('progress') or {}).get('stages', [])
    entry = (next((item for item in stages if item.get('status') == 'stopped'), None)
             or next((item for item in stages if item.get('status') == 'running'), None)
             or next((item for item in reversed(stages) if item.get('status') == 'complete'), None))
    return str(entry.get('stage', 'Unknown')) if entry else 'Not recorded'


def read_run_history():
    """Discover past selections now; preserve individual dashboard attempts going forward."""
    warning = None
    try:
        documents = _history_documents()
    except RunError as error:
        documents, warning = {}, str(error)
    records = {}
    for key, value in documents.items():
        if key.startswith(HISTORY_PREFIX) and (entry := _history_entry(value)) and entry.get('run_id'):
            records[entry['run_id']] = entry

    for path in (ROOT / 'data/pipeline_runs').glob('*/*/ui_run.json'):
        try:
            job = _read_json(path)
        except RunError:
            continue
        entry = _history_entry(job) if job else None
        if not entry or not entry.get('run_id'):
            continue
        old = records.get(entry['run_id'], {})
        # A stale launcher copy must not overwrite a terminal database record.
        if (_history_time(old.get('completed_at')) and
                (not _history_time(entry.get('completed_at')) or
                 _history_time(old['completed_at']) > _history_time(entry['completed_at']))):
            continue
        records[entry['run_id']] = {**old, **entry, '_local': True,
                                    'source': old.get('source', 'Existing dashboard record')}

    for key, progress in documents.items():
        match = re.fullmatch(r'pipeline_runs/([^/]+)/(\d{4}(?:-\d{4})+)/status\.json', key)
        if not match:
            continue
        ticker, year_text = match.groups()
        entry = _history_entry({'ticker': ticker, 'years': list(map(int, year_text.split('-')))})
        if not entry:
            continue
        selection = _history_entry({'ticker': progress.get('ticker', ticker),
                                    'years': progress.get('years', entry['years'])})
        if not selection or (selection['ticker'], selection['years']) != (entry['ticker'], entry['years']):
            continue
        progress = _history_progress(progress)
        matches = [record for record in records.values()
                   if (record['ticker'], record['years']) == (entry['ticker'], entry['years'])
                   and _matching_progress(record, progress)]
        if matches:
            record = max(matches, key=lambda record: _history_time(record['started_at']))
            record['progress'] = progress
        else:
            started = progress.get('last_invocation_at') or progress.get('started_at')
            run_id = f'checkpoint:{ticker}:{year_text}:{started or "unknown"}'
            records[run_id] = {**entry, 'run_id': run_id, 'started_at': started,
                               'completed_at': progress.get('completed_at'), 'progress': progress,
                               'source': ('Starter data' if progress.get('completion_source') == 'starter_data'
                                          else 'Saved pipeline checkpoint')}

    rows = list(records.values())
    for record in rows:
        record['display_status'] = _history_status(record)
        record['last_stage'] = _history_last_stage(record)
    rows.sort(key=lambda record: _history_time(record.get('started_at')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return rows, warning


def history_log(record):
    saved = record.get('activity_log')
    return log_tail(record['ticker'], record['years'], record, limit=100) or (saved if isinstance(saved, str) else '')


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
                documents[path.relative_to(ROOT / "data").as_posix()] = _optional_json(path)
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
            usage = documents.get((folder / "token_usage.json").relative_to(data).as_posix()) or {}
            manifest = documents.get((folder / "manifest.json").relative_to(data).as_posix()) or {}
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
        prefix = folder.relative_to(data).as_posix()
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
