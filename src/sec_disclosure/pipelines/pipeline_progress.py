"""Pipeline completion checkpoints, dependency invalidation and invocation locking."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json


# Scheduling, credentials used for SEC identification, and admission limits do
# not change a completed result. In particular, raising a budget must preserve
# completed stages and the unfinished stage's own response caches.
EXECUTION_OPTIONS = {
    "--workers", "--batch-workers", "--max-concurrent-requests", "--concurrency",
    "--request-interval", "--rate-limit-cooldown", "--rate-limit-retries",
    "--max-requests", "--max-new-requests", "--max-total-tokens", "--timeout",
    "--retry-timeout", "--user-agent",
}


def stage_signature(stage):
    arguments = iter(stage.arguments)
    retained = []
    for argument in arguments:
        if argument in EXECUTION_OPTIONS:
            next(arguments)
        elif argument != "--fault-tolerant":
            retained.append(argument)
    value = json.dumps([stage.script, retained], separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def read_progress(path, ticker, years):
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text())
        if (not isinstance(state, dict) or state.get("ticker") != ticker
                or state.get("years") != years or not isinstance(state.get("stages"), list)):
            raise ValueError
        for entry in state["stages"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("stage"), str)
                    or entry.get("status") not in {"running", "stopped", "complete"}):
                raise ValueError
        return state
    except (ValueError, TypeError):
        raise ValueError("Pipeline progress is invalid. Restore status.json, or use --no-resume "
                         "to execute the selected stages using their existing caches.") from None


def stage_dependencies(stages):
    raw_saves = {stage.name for stage in stages if stage.name.startswith("save_text_")}
    dependencies = {}
    for stage in stages:
        if stage.name.startswith("save_text_"):
            parents = {stage.name.removeprefix("save_")}
        elif stage.phase == "extraction":
            parents = raw_saves | {"api_tables_and_save"}
        elif stage.phase == "preprocessing":
            parents = set()
        else:
            suffix = "_".join(stage.comparison)
            if stage.phase == "alignment":
                parents = {"extract_disclosures_and_save"}
            elif stage.name.startswith("llm_changes_"):
                parents = {f"align_and_save_{suffix}"}
            elif stage.name.startswith("lexical_semantic_"):
                parents = {f"llm_changes_{suffix}"}
            elif stage.name.startswith("save_changes_"):
                parents = {f"lexical_semantic_{suffix}"}
            elif stage.name.startswith("save_materiality_"):
                parents = {f"materiality_{suffix}"}
            else:
                parents = {f"save_changes_{suffix}"}
        dependencies[stage.name] = parents
    return dependencies


def resumable_entries(previous, all_stages, selected, *, resume=True):
    """Keep independent completed pairs, but invalidate dependents of rerun work."""
    known = {stage.name: stage for stage in all_stages}
    entries = {entry["stage"]: dict(entry) for entry in previous.get("stages", [])
               if entry["stage"] in known}
    invalid = {stage.name for stage in selected if not resume or stage.name not in entries}
    for name, entry in entries.items():
        # Existing pre-resume status files have no signature; import their
        # successful stage markers once, then record signatures going forward.
        if (entry["status"] != "complete" or entry.get("exit_code", 0) != 0
                or entry.get("command_sha256", stage_signature(known[name])) != stage_signature(known[name])):
            invalid.add(name)
    dependencies = stage_dependencies(all_stages)
    for stage in all_stages:
        if dependencies[stage.name] & invalid:
            invalid.add(stage.name)
    for name in invalid:
        entries.pop(name, None)
    for name, entry in entries.items():
        entry["command_sha256"] = stage_signature(known[name])
    return entries


@contextmanager
def invocation_lock(folder):
    """Only one runner may update a company's year-selection checkpoint."""
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".run.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A pipeline for this ticker and year selection is already running. "
                             "Wait for it to finish before rerunning.") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
