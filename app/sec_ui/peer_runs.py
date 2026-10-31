"""Check saved peer inputs, launch the peer CLI, and read PostgreSQL results."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import subprocess
import sys
from threading import Lock, Thread
from uuid import uuid4

from . import data, pipeline_runs

RunError = pipeline_runs.RunError
_START_LOCK = Lock()
ITEMS = ("1", "1A", "7", "8", "15")
SELECTION_FIELDS = ("ticker", "peers", "year", "items", "target_selection")


def selection(ticker, peers, year, items=("1", "1A", "7", "8"), target_selection="material_changes"):
    ticker = ticker.strip().lower()
    peers = [peer.strip().lower() for peer in peers]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ticker) or not peers or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", peer) for peer in peers):
        raise RunError("Enter a valid target and at least one peer ticker.")
    if ticker in peers or len(set(peers)) != len(peers):
        raise RunError("Peer tickers must be unique and must not include the target.")
    if type(year) is not int or not 1900 <= year <= 2200:
        raise RunError("Select a fiscal year from 1900 to 2200.")
    if not items or any(item not in ITEMS for item in items):
        raise RunError("Select at least one supported SEC Item.")
    if target_selection not in {"material_changes", "all"}:
        raise RunError("Choose material changes or all disclosures.")
    return dict(ticker=ticker, peers=sorted(peers), year=year,
                items=sorted(set(items)), target_selection=target_selection)


def selection_key(value):
    value = selection(**{key: value[key] for key in SELECTION_FIELDS})
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


def command_arguments(value):
    value = selection(**value)
    return ["--ticker", value["ticker"], "--peers", *value["peers"], "--year", str(value["year"]),
            "--items", *value["items"], "--target-selection", value["target_selection"]]


def check_inputs(value):
    """Apply the backend's evidence and materiality gates without LLM calls."""
    value = selection(**value)
    src = str(pipeline_runs.ROOT / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    from sec_disclosure.agents import peer_comparison as agent
    from sec_disclosure.storage.peer_comparison import read_peer_inputs

    try:
        args = agent.parse_args(command_arguments(value) + ["--dry-run"])
        inputs = read_peer_inputs(args.ticker, args.peers, int(args.year), env_file=args.env_file)
        targets, peers, _, total = agent.prepare_comparison_inputs(args, inputs)
    except (ValueError, OSError, RuntimeError) as error:
        raise RunError(str(error)) from None
    return {"target_disclosures": len(targets), "all_target_disclosures": total,
            "peer_disclosures": {peer.upper(): len(rows) for peer, rows in peers.items()}}


def _folder(value):
    return pipeline_runs.ROOT / "data/peer_runs" / selection_key(value)


def active_run():
    for path in (pipeline_runs.ROOT / "data/peer_runs").glob("*/ui_run.json"):
        try:
            job = pipeline_runs._read_json(path)
        except RunError:
            continue
        if job and job.get("status") in {"starting", "running"} and pipeline_runs._pid_alive(job.get("pid")):
            return job
    return None


def read_run(value):
    job = pipeline_runs._read_json(_folder(value) / "ui_run.json")
    if job and job.get("status") == "running" and not pipeline_runs._pid_alive(job.get("pid")):
        job = dict(job, status="stopped")
    return job


def start_run(value):
    value = selection(**value)
    with _START_LOCK:
        if active_run():
            raise RunError("A peer comparison is already running. Wait for it to finish.")
        if pipeline_runs.active_run():
            raise RunError("The main pipeline is still running. Wait for its inputs to be saved first.")
        readiness = check_inputs(value)
        folder = _folder(value)
        folder.mkdir(parents=True, exist_ok=True)
        run_id = uuid4().hex
        job = {**value, "run_id": run_id, "status": "starting", "readiness": readiness,
               "started_at": datetime.now(timezone.utc).isoformat(), "log_file": f"ui_run_{run_id[:12]}.log"}
        pipeline_runs._write_json(folder / "ui_run.json", job)
        command = [pipeline_runs._pipeline_python(), "-u",
                   str(pipeline_runs.ROOT / "scripts/run_peer_comparison_pipeline.py"), *command_arguments(value)]
        try:
            with (folder / job["log_file"]).open("w", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=pipeline_runs.ROOT, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            job.update(status="stopped", error="Could not start the peer comparison process.")
            pipeline_runs._write_json(folder / "ui_run.json", job)
            raise RunError(job["error"]) from None
        job.update(status="running", pid=process.pid)
        pipeline_runs._write_json(folder / "ui_run.json", job)
        Thread(target=pipeline_runs._finish_job, args=(process, folder, run_id), daemon=True).start()
        return job


def log_tail(job):
    name = job.get("log_file", "")
    if not isinstance(name, str) or not re.fullmatch(r"ui_run_[a-f0-9]{12}\.log", name):
        return ""
    try:
        with (_folder(job) / name).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - 16384))
            return "\n".join(stream.read().decode("utf-8", errors="replace").splitlines()[-24:])
    except OSError:
        return ""


def _query(table, sql, parameters=()):
    try:
        with data._postgres_connection() as connection, connection.cursor() as cursor:
            connection.read_only = True
            cursor.execute("SELECT to_regclass(%s)", (table,))
            if cursor.fetchone()[0] is None:
                return []
            cursor.execute(sql, parameters)
            return cursor.fetchall()
    except data.DataError as error:
        raise RunError(str(error)) from None
    except Exception:
        raise RunError("Could not read peer comparison results from PostgreSQL. Check the database connection and schemas.") from None


def saved_reports():
    rows = _query("peer_comparison_reports", """
        SELECT target_ticker, fiscal_year, peer_tickers, items, target_selection, run_complete
        FROM peer_comparison_reports ORDER BY saved_at DESC
    """)
    return [dict(ticker=ticker, year=year, peers=peers, items=items,
                 target_selection=mode, run_complete=complete)
            for ticker, year, peers, items, mode, complete in rows]


def load_report(value):
    value = selection(**{key: value[key] for key in SELECTION_FIELDS})
    rows = _query("peer_comparison_reports", """
        SELECT report FROM peer_comparison_reports
        WHERE target_ticker=%s AND fiscal_year=%s AND peer_tickers=%s AND items=%s AND target_selection=%s
    """, (value["ticker"].upper(), value["year"], [peer.upper() for peer in value["peers"]],
           value["items"], value["target_selection"]))
    if not rows or not isinstance(rows[0][0], dict) or not isinstance(rows[0][0].get("comparisons"), list):
        raise RunError("The selected peer report is unavailable or invalid. Refresh saved results.")
    return rows[0][0]


def progress(job):
    prefix = f"peer_comparison/{job['ticker'].lower()}/{job['year']}/{'-'.join(sorted(peer.lower() for peer in job['peers']))}/runs/"
    rows = _query("pipeline_artifacts", """
        SELECT content FROM pipeline_artifacts
        WHERE namespace='peer' AND kind='file' AND starts_with(path, %s)
          AND path LIKE %s AND modified_at >= %s::timestamptz
        ORDER BY modified_at DESC LIMIT 10
    """, (prefix, "%/peer_comparison.json", job["started_at"]))
    for (content,) in rows:
        try:
            report = json.loads(bytes(content))
        except (ValueError, TypeError):
            continue
        if (isinstance(report, dict) and report.get("target_selection") == job["target_selection"]
                and sorted(report.get("items", [])) == sorted(job["items"])):
            return {key: report.get(key) for key in ("jobs_completed", "jobs_planned", "run_complete")}
    return None
