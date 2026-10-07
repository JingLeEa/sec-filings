"""Durable request ledger for materiality classification."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from sec_disclosure.llm.client import request_completion
from sec_disclosure.llm.disclosures import digest, usage_report, write_json

from .alignment_runtime import RunLimit


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


class MaterialityRuntime:
    def __init__(self, output: Path, config, args, classification_run_id=None):
        self.output, self.config, self.args = output, config, args
        self.classification_run_id = classification_run_id
        self.directory = output / "requests"
        self.directory.mkdir(parents=True, exist_ok=True)
        all_requests = [json.loads(path.read_text()) for path in sorted(self.directory.glob("*.json"))]
        self.requests = [
            request for request in all_requests
            if request.get("classification_run_id") == classification_run_id
        ]
        self.new_requests = 0
        self.retry_acknowledged = {
            (request["request_hash"], request["attempt"])
            for request in self.requests
            if args.retry_failed and request["status"] in {"failed", "started"}
        }

    def timing_report(self):
        durations = [
            request.get("elapsed_seconds") for request in self.requests
            if type(request.get("elapsed_seconds")) in {int, float}
        ]
        total_requests = len(self.requests)
        return {
            "cumulative_api_seconds": (
                round(sum(durations), 3) if len(durations) == total_requests else None
            ),
            "api_requests_timed": len(durations),
            "api_requests_total": total_requests,
        }

    def unknown_budget(self):
        reserved, unacknowledged = [], []
        for record in self.requests:
            usage = record.get("result", {}).get("usage")
            if isinstance(usage, dict) and all(
                    type(usage.get(key)) is int
                    for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
                continue
            key = (record["request_hash"], record["attempt"])
            if key not in self.retry_acknowledged:
                unacknowledged.append(key)
                continue
            reserved.append({
                "request_hash": key[0],
                "attempt": key[1],
                "job_id": record["job_id"],
                "estimated_tokens": len((record["system_prompt"] + record["prompt"]).encode())
                + record["max_tokens"],
            })
        return {
            "estimated_tokens": sum(row["estimated_tokens"] for row in reserved),
            "attempts": reserved,
            "unacknowledged_attempt_count": len(unacknowledged),
        }

    def report(self):
        report = usage_report(self.requests, 0)
        report["by_agent"] = report.pop("by_item")
        report.pop("planned_batches")
        report["new_requests_this_invocation"] = self.new_requests
        report["limits"] = {
            key: getattr(self.args, key)
            for key in ("max_requests", "max_total_tokens", "max_steps", "max_tokens", "max_prompt_chars")
        }
        report["notes"].append(
            "Materiality-stage tokens only. Admission reserves UTF-8 prompt bytes plus the maximum completion."
        )
        report["unknown_usage_budget_reserve"] = self.unknown_budget()
        report["budget_accounted_tokens"] = (
            report["reported_tokens"]["total_tokens"]
            + report["unknown_usage_budget_reserve"]["estimated_tokens"]
        )
        report["timing"] = self.timing_report()
        return report

    def call(self, job_id: str, prompt: str, system: str):
        spec = {
            "prompt": prompt,
            "system_prompt": system,
            "model": self.config.model,
            "base_url": self.config.base_url,
            "max_tokens": self.args.max_tokens,
        }
        if self.classification_run_id is not None:
            spec["classification_run_id"] = self.classification_run_id
        key = digest(json.dumps(spec, sort_keys=True).encode())
        history = [record for record in self.requests if record["request_hash"] == key]
        if history and history[-1]["status"] == "completed":
            return history[-1]["result"]
        if history and not self.args.retry_failed:
            raise RuntimeError(
                "An earlier materiality API attempt failed or was interrupted; inspect requests and use "
                "--retry-failed to allow another paid attempt."
            )
        if self.args.max_new_requests is not None and self.new_requests >= self.args.max_new_requests:
            raise RunLimit("Invocation request limit reached; resume to continue.")
        usage = self.report()
        if len(self.requests) >= self.args.max_requests:
            raise RunLimit("Total materiality request limit reached.")
        if usage["unknown_usage_budget_reserve"]["unacknowledged_attempt_count"]:
            raise RunLimit("A materiality API attempt has unknown usage; inspect it before retrying.")
        reservation = len((system + prompt).encode()) + self.args.max_tokens
        if usage["budget_accounted_tokens"] + reservation > self.args.max_total_tokens:
            raise RunLimit("Materiality token budget cannot accommodate the next request.")
        if len(system + prompt) > self.args.max_prompt_chars:
            raise ValueError("Materiality agent context exceeds max_prompt_chars.")

        attempt = len(history) + 1
        record = {
            "request_hash": key,
            "job_id": job_id,
            "item": "materiality",
            "stage": "materiality",
            "agent": "materiality",
            "attempt": attempt,
            "status": "started",
            "started_at": datetime.now(timezone.utc).isoformat(),
            **spec,
        }
        path = self.directory / f"{key}_attempt_{attempt:03d}.json"
        write_json(path, record)
        self.requests.append(record)
        self.new_requests += 1
        print(f"{job_id}: materiality request {self.new_requests}", flush=True)
        request_started = perf_counter()
        try:
            response = request_completion(
                prompt,
                config=self.config,
                system_prompt=system,
                json_mode=True,
                max_tokens=self.args.max_tokens,
                timeout=self.args.timeout,
            )
            record.update(status="completed", result=asdict(response))
        except Exception as error:
            record.update(status="failed", error=type(error).__name__)
            raise
        finally:
            record["completed_at"] = datetime.now(timezone.utc).isoformat()
            record["elapsed_seconds"] = round(perf_counter() - request_started, 3)
            write_json(path, record)
            write_json(self.output / "token_usage.json", self.report())
        return record["result"]
