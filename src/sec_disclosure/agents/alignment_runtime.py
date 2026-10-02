"""Durable API ledger and entry points to the LangGraph agent workflows."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sec_disclosure.llm.client import request_completion
from sec_disclosure.llm.disclosures import digest, usage_report, write_json


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
    def __init__(self, output: Path, config, args):
        self.output, self.config, self.args = output, config, args
        self.directory = output / "requests"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.requests = [json.loads(p.read_text()) for p in sorted(self.directory.glob("*.json"))]
        self.new_requests = 0
        # --retry-failed acknowledges only interrupted/failed attempts already
        # present at startup. New failures or successful responses lacking usage
        # still stop the run; retries never turn unknown usage into zero.
        self.retry_acknowledged = {(r["request_hash"], r["attempt"]) for r in self.requests
                                   if args.retry_failed and r["status"] in {"failed", "started"}}

    def unknown_budget(self):
        reserved, unacknowledged = [], []
        for record in self.requests:
            usage = record.get("result", {}).get("usage")
            if isinstance(usage, dict) and all(type(usage.get(key)) is int for key in
                                             ("prompt_tokens", "completion_tokens", "total_tokens")):
                continue
            key = (record["request_hash"], record["attempt"])
            if key not in self.retry_acknowledged:
                unacknowledged.append(key)
                continue
            reserved.append({"request_hash": key[0], "attempt": key[1], "job_id": record["job_id"],
                "estimated_tokens": len((record["system_prompt"] + record["prompt"]).encode()) + record["max_tokens"]})
        return {"estimated_tokens": sum(r["estimated_tokens"] for r in reserved), "attempts": reserved,
                "unacknowledged_attempt_count": len(unacknowledged)}

    def report(self) -> dict:
        report = usage_report(self.requests, 0)
        report["by_agent"] = report.pop("by_item")
        report.pop("planned_batches")
        report["new_requests_this_invocation"] = self.new_requests
        report["limits"] = {key: getattr(self.args, key) for key in
                            ("max_requests", "max_total_tokens", "max_steps", "max_tokens", "max_prompt_chars")}
        report["notes"].append("Alignment tokens only; input extraction usage is reported separately. Token admission reserves UTF-8 prompt bytes plus the maximum completion as a conservative estimate, not a provider tokenizer guarantee.")
        report["unknown_usage_budget_reserve"] = self.unknown_budget()
        report["budget_accounted_tokens"] = (report["reported_tokens"]["total_tokens"]
                                             + report["unknown_usage_budget_reserve"]["estimated_tokens"])
        if report["unknown_usage_budget_reserve"]["estimated_tokens"]:
            report["notes"].append("--retry-failed reserves an estimated token allowance for previously failed/interrupted attempts. This allowance counts against the run cap but is not provider-reported usage; unknown usage remains unknown.")
        if hasattr(self, "source_usage"):
            report["source_run_reported_tokens"] = self.source_usage["reported_tokens"]
            report["notes"].append("Repair run: reported_tokens and limits cover new correction requests only. Source-run usage is historical and is not added again.")
        return report

    def call(self, role: str, job_id: str, prompt: str, system: str) -> dict:
        spec = {"prompt": prompt, "system_prompt": system, "model": self.config.model,
                "base_url": self.config.base_url, "max_tokens": self.args.max_tokens}
        key = digest(json.dumps(spec, sort_keys=True).encode())
        history = [r for r in self.requests if r["request_hash"] == key]
        if history and history[-1]["status"] == "completed":
            return history[-1]["result"]
        if history and not self.args.retry_failed:
            raise RuntimeError("An earlier API attempt failed or was interrupted; inspect requests and use --retry-failed to allow another paid attempt.")
        if self.args.max_new_requests is not None and self.new_requests >= self.args.max_new_requests:
            raise RunLimit("Invocation request limit reached; resume to continue.")
        usage = self.report()
        if len(self.requests) >= self.args.max_requests:
            raise RunLimit("Total alignment request limit reached.")
        if usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"]:
            raise RunLimit("An API attempt has unknown usage; inspect it before increasing the budget or retrying.")
        reservation = len((system + prompt).encode()) + self.args.max_tokens
        if usage["budget_accounted_tokens"] + reservation > self.args.max_total_tokens:
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
        self.new_requests += 1
        print(f"{job_id}: {role} request {self.new_requests}", flush=True)
        try:
            response = request_completion(prompt, config=self.config, system_prompt=system,
                                          json_mode=True, max_tokens=self.args.max_tokens, timeout=self.args.timeout)
            record.update(status="completed", result=asdict(response))
        except Exception as error:
            record.update(status="failed", error=type(error).__name__)
            raise
        finally:
            write_json(path, record)
            write_json(self.output / "token_usage.json", self.report())
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
