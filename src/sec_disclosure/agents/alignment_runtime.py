"""Durable API ledger and entry points to the LangGraph agent workflows."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from itertools import count
from pathlib import Path

from sec_disclosure.llm.client import LLMError, request_completion
from sec_disclosure.llm.request_pacing import RequestPacer
from sec_disclosure.llm.disclosures import digest, retryable_request, usage_report, write_json


class RunLimit(RuntimeError):
    """A run/request budget stopped execution; cached work can be resumed."""


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


class Runtime:
    def __init__(self, output: Path, config, args, *, request_pacer: RequestPacer | None = None):
        self.output, self.config, self.args = output, config, args
        self.directory = output / "requests"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.requests = [json.loads(p.read_text()) for p in sorted(self.directory.glob("*.json"))]
        self.requests.sort(key=lambda record: (record["request_hash"], record["attempt"]))
        self.new_requests = 0
        self.condition = threading.Condition(threading.RLock())
        self.checkpoint_setup_lock = threading.Lock()
        self.in_flight = {}
        self.pacer = request_pacer if request_pacer is not None else RequestPacer(
            args.request_interval, args.rate_limit_cooldown, args.workers)
        self.fault_tolerant = getattr(args, "fault_tolerant", False)
        self.max_retries = getattr(args, "max_retries", 10)
        self.retry_backoff = getattr(args, "retry_backoff", 5)
        # Explicit retries and automatic recovery both reserve estimated usage.
        # Successful responses lacking usage still stop admission of new calls.
        self.retry_acknowledged = {(r["request_hash"], r["attempt"]) for r in self.requests
                                   if r["status"] in {"failed", "started"}
                                   and (args.retry_failed or self.fault_tolerant and retryable_request(r))}

    def unknown_budget(self):
        with self.condition:
            reserved, unacknowledged = [], []
            for record in self.requests:
                usage = record.get("result", {}).get("usage")
                if isinstance(usage, dict) and all(type(usage.get(key)) is int for key in
                                                 ("prompt_tokens", "completion_tokens", "total_tokens")):
                    continue
                key = (record["request_hash"], record["attempt"])
                if key in self.in_flight:
                    continue
                if key not in self.retry_acknowledged:
                    unacknowledged.append(key)
                    continue
                reserved.append({"request_hash": key[0], "attempt": key[1], "job_id": record["job_id"],
                    "estimated_tokens": len((record["system_prompt"] + record["prompt"]).encode()) + record["max_tokens"]})
            return {"estimated_tokens": sum(r["estimated_tokens"] for r in reserved), "attempts": reserved,
                    "unacknowledged_attempt_count": len(unacknowledged)}

    def report(self) -> dict:
        with self.condition:
            report = usage_report(self.requests, 0)
            report["by_agent"] = report.pop("by_item")
            report.pop("planned_batches")
            report["new_requests_this_invocation"] = self.new_requests
            report["limits"] = {key: getattr(self.args, key) for key in
                                ("max_requests", "max_total_tokens", "max_steps", "max_tokens", "max_prompt_chars")}
            report["notes"].append("Alignment tokens only; input extraction usage is reported separately. Token admission reserves UTF-8 prompt bytes plus the maximum completion as a conservative estimate, not a provider tokenizer guarantee.")
            report["unknown_usage_budget_reserve"] = self.unknown_budget()
            report["in_flight_budget_reserve"] = {"estimated_tokens": sum(self.in_flight.values()),
                                                   "attempt_count": len(self.in_flight)}
            report["budget_accounted_tokens"] = (report["reported_tokens"]["total_tokens"]
                                                 + report["unknown_usage_budget_reserve"]["estimated_tokens"]
                                                 + report["in_flight_budget_reserve"]["estimated_tokens"])
            if report["unknown_usage_budget_reserve"]["estimated_tokens"]:
                report["notes"].append("--retry-failed or --fault-tolerant reserves an estimated token allowance for failed/interrupted attempts. This allowance counts against the run cap but is not provider-reported usage; unknown usage remains unknown.")
            if hasattr(self, "source_usage"):
                report["source_run_reported_tokens"] = self.source_usage["reported_tokens"]
                report["notes"].append("Repair run: reported_tokens and limits cover new correction requests only. Source-run usage is historical and is not added again.")
            return report

    def call(self, role: str, job_id: str, prompt: str, system: str) -> dict:
        for retry in count():
            try:
                return self._call_once(role, job_id, prompt, system)
            except LLMError as error:
                recoverable = retryable_request({"status": "failed", "error": str(error),
                                                "status_code": error.status_code, "retryable": error.retryable})
                if not self.fault_tolerant or not recoverable or retry == self.max_retries:
                    raise
                retry_label = "unlimited" if self.max_retries == -1 else str(self.max_retries)
                if error.status_code == 429:
                    print(f"[{self.output.name}] {job_id}: HTTP 429 saved; shared cooldown before retry {retry + 1}/{retry_label}.", flush=True)
                else:
                    delay = min(self.retry_backoff * (2 ** min(retry, 16)), 60)
                    print(f"[{self.output.name}] {job_id}: retry {retry + 1}/{retry_label} in {delay:g}s; previous attempt preserved.", flush=True)
                    time.sleep(delay)

    def _call_once(self, role: str, job_id: str, prompt: str, system: str) -> dict:
        spec = {"prompt": prompt, "system_prompt": system, "model": self.config.model,
                "base_url": self.config.base_url, "max_tokens": self.args.max_tokens}
        key = digest(json.dumps(spec, sort_keys=True).encode())

        def cached():
            while any(attempt[0] == key for attempt in self.in_flight):
                self.condition.wait()
            history = [r for r in self.requests if r["request_hash"] == key]
            return history, history[-1]["result"] if history and history[-1]["status"] == "completed" else None

        with self.condition:
            _, result = cached()
            if result is not None:
                return result
        with self.pacer.request():
            with self.condition:
                history, result = cached()
                if result is not None:
                    return result
                if history and not (self.args.retry_failed or self.fault_tolerant and retryable_request(history[-1])):
                    raise RuntimeError("An earlier API attempt failed or was interrupted; inspect requests and use --retry-failed to allow another paid attempt.")
                if self.args.max_new_requests is not None and self.new_requests >= self.args.max_new_requests:
                    raise RunLimit("Invocation request limit reached; resume to continue.")
                usage = self.report()
                if self.args.max_requests != -1 and len(self.requests) >= self.args.max_requests:
                    raise RunLimit("Total alignment request limit reached.")
                if usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"]:
                    raise RunLimit("An API attempt has unknown usage; inspect it before increasing the budget or retrying.")
                reservation = len((system + prompt).encode()) + self.args.max_tokens
                if self.args.max_total_tokens != -1 and usage["budget_accounted_tokens"] + reservation > self.args.max_total_tokens:
                    raise RunLimit("Alignment token budget cannot accommodate the next request.")
                if len(system + prompt) > self.args.max_prompt_chars:
                    raise ValueError("Agent context exceeds max_prompt_chars; retained for review.")
                attempt = len(history) + 1
                record = {"request_hash": key, "job_id": job_id, "item": role, "agent": role,
                          "attempt": attempt, "status": "started", "started_at": datetime.now(timezone.utc).isoformat(),
                          **spec}
                path = self.directory / f"{key}_attempt_{attempt:03d}.json"
                write_json(path, record)
                self.requests.append(record)
                self.in_flight[(key, attempt)] = reservation
                self.new_requests += 1
                print(f"[{self.output.name}] {job_id}: {role} request {self.new_requests}", flush=True)
            updates = {}
            try:
                response = request_completion(prompt, config=self.config, system_prompt=system,
                                              json_mode=True, max_tokens=self.args.max_tokens, timeout=self.args.timeout)
                updates = {"status": "completed", "result": asdict(response)}
            except Exception as error:
                updates = {"status": "failed", "error": type(error).__name__}
                if isinstance(error, LLMError):
                    updates.update(error=str(error), status_code=error.status_code, retryable=error.retryable)
                    updates["retryable"] = retryable_request(updates)
                    if error.status_code == 429 and updates["retryable"]:
                        self.pacer.rate_limited(error.retry_after)
                raise
            finally:
                with self.condition:
                    record.update(updates)
                    self.in_flight.pop((key, attempt))
                    if self.fault_tolerant and record["status"] == "failed" and retryable_request(record):
                        self.retry_acknowledged.add((key, attempt))
                    try:
                        write_json(path, record)
                        write_json(self.output / "token_usage.json", self.report())
                    finally:
                        self.condition.notify_all()
            return record["result"]

    def agent(self, role, job_id, system, payload, data, visible, validate):
        from .alignment_graph import AlignmentAgentGraph

        runner = AlignmentAgentGraph(self, role, job_id, system, payload, data, visible, validate=validate)
        result = runner.run()
        visible.update(runner.visible)
        return result

    def verification(self, job_id, system, payload, data, visible, repair, *, seed=None, source_request=None, offline=False):
        from .alignment_graph import AlignmentAgentGraph

        runner = AlignmentAgentGraph(self, "verification", job_id, system, payload, data, visible,
                                     repair=repair, seed=seed, source_request=source_request, offline=offline)
        result = runner.run()
        visible.update(runner.visible)
        return result
