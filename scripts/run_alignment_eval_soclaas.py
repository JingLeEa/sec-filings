#!/usr/bin/env python3
"""Evaluate prepared alignments with the SoCLaaS Chat Completions API."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import alignment_eval_shared as shared
from sec_disclosure.llm.client import LLMError, request_completion
from sec_disclosure.llm.config import LLMConfig, load_config


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--alignment-root", type=Path, default=Path("data/alignments_for_eval_soclaas"))
    parser.add_argument("--disclosures-dir", type=Path, default=Path("data/disclosures"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model", default="x-test-2")
    parser.add_argument("--stage", choices=("both", "change", "materiality"), default="both")
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--workers", type=int, default=1,
                        help="Maximum concurrent SoCLaaS requests.")
    parser.add_argument("--max-tokens", type=int, default=16384)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--request-interval", type=float, default=1)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--max-invalid-retries", type=int, default=3,
                        help="Additional attempts for invalid model responses.")
    parser.add_argument("--retry-backoff", type=float, default=5)
    parser.add_argument("--max-new-rows", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not shared.re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", args.ticker):
        parser.error("--ticker must be a safe company directory name.")
    if (not shared.re.fullmatch(r"\d{4}", args.previous_year)
            or not shared.re.fullmatch(r"\d{4}", args.current_year)
            or args.previous_year >= args.current_year):
        parser.error("Years must be increasing four-digit fiscal years.")
    if not args.model.strip() or any(char.isspace() for char in args.model):
        parser.error("--model must be a nonempty model ID without whitespace.")
    if (min(args.batch_size, args.workers, args.max_tokens) < 1
            or min(args.max_retries, args.max_invalid_retries) < 0):
        parser.error("Batch size, workers and token limit must be positive; retries must be nonnegative.")
    if any(not math.isfinite(value) or value < 0 for value in
           (args.timeout, args.request_interval, args.retry_backoff)) or args.timeout == 0:
        parser.error("Timeout must be positive; request interval and retry backoff must be nonnegative.")
    if args.max_new_rows is not None and args.max_new_rows < 1:
        parser.error("--max-new-rows must be positive.")
    return args


def usage_counts(usage):
    return (usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))


def apply_record(record, document, alignment_path, ledger, ledger_path, errors, errors_path):
    record_id = record["record_id"]
    if record_id not in ledger["record_ids"]:
        prompt_tokens, completion_tokens = usage_counts(record.get("usage") or {})
        ledger["prompt_tokens"] += prompt_tokens
        ledger["completion_tokens"] += completion_tokens
        ledger["requests"] += 1
        ledger["record_ids"].append(record_id)
        shared.atomic_json(ledger_path, ledger)
    try:
        if record["finish_reason"] != "stop":
            raise ValueError(f"Completion finished with {record['finish_reason']!r}.")
        rows = shared.validate_results(record["spec"]["stage"], json.loads(record["text"]),
                                       record["spec"]["match_ids"])
    except (ValueError, KeyError, TypeError) as error:
        errors[record_id] = {"custom_id": record["custom_id"], "error": str(error)}
        shared.atomic_json(errors_path, errors)
        return 0, True
    by_id = {row["match_id"]: row for row in document["alignments"]}
    for row in rows:
        target = by_id[row["match_id"]]
        if record["spec"]["stage"] == "change":
            target["change_analysis"].update(status="completed" if row["llm"] else "needs_review",
                                             llm=row["llm"])
        else:
            target["materiality_analysis"] = {key: row[key] for key in
                                               ("materiality", "materiality_score", "confidence_score", "reason")}
    shared.atomic_json(alignment_path, document)
    return len(rows), False


class RequestPacer:
    def __init__(self, interval):
        self.interval = interval
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_start)
            self.next_start = start + self.interval
        if start > now:
            time.sleep(start - now)


def request_with_retries(completion, body, settings, args, pacer):
    for attempt in range(args.max_retries + 1):
        try:
            pacer.wait()
            return completion(body["input"], config=settings,
                              system_prompt=body["instructions"], json_mode=True,
                              max_tokens=args.max_tokens, timeout=args.timeout)
        except LLMError as error:
            if not error.retryable or attempt == args.max_retries:
                raise
            delay = max(error.retry_after or 0, args.retry_backoff * 2 ** attempt)
            if delay > 60:
                raise
            print(f"SoCLaaS request failed; retrying in {delay:g}s: {error}", flush=True)
            time.sleep(delay)


def run(args, *, completion=request_completion, config=None):
    settings = config if config is not None else load_config(args.env_file)
    settings = replace(settings, model=args.model)
    ticker = args.ticker.lower()
    pair = f"{args.previous_year}-{args.current_year}"
    alignment_path = args.alignment_root / ticker / pair / "alignments.json"
    document, records = shared.load_inputs(alignment_path, args.disclosures_dir)
    if (document["company"], str(document["previous_year"]), str(document["current_year"])) != (
            ticker, args.previous_year, args.current_year):
        raise ValueError("Alignment company or years do not match the requested pair.")
    work = alignment_path.parent / "soclaas_eval"
    config_path = work / "config.json"
    identity = {"provider": "soclaas", "model": settings.model, "base_url": settings.base_url,
                "source_hash": shared.source_identity(document, records),
                "change_prompt_hash": shared.fingerprint(shared.CHANGE_ANALYSIS_PROMPT),
                "materiality_prompt_hash": shared.fingerprint(shared.MATERIALITY_PROMPT)}
    if config_path.exists() and shared.read_json(config_path) != identity:
        raise ValueError("Model, endpoint, prompt, or source evidence changed; use a new prepared alignment directory.")
    if (alignment_path.parent / "openai_eval" / "config.json").exists():
        raise ValueError("This alignment directory contains OpenAI evaluation state; prepare a separate SoCLaaS copy.")
    stages = ("change", "materiality") if args.stage == "both" else (args.stage,)
    if args.dry_run:
        shared.apply_deterministic(document)
        pending = shared.pending_rows(document, stages)
        if args.max_new_rows:
            pending = pending[:args.max_new_rows]
        requests, _ = shared.make_requests(pending, records, stages, settings.model, "low",
                                           args.batch_size, document, args.max_tokens)
        print(f"Dry run: {len(pending)} pending rows in {len(requests)} SoCLaaS requests "
              f"with up to {args.workers} workers. No files changed.")
        return 0
    if not config_path.exists():
        if any(row["change_analysis"].get("llm") is not None or
               row["materiality_analysis"].get("materiality") is not None
               for row in document["alignments"]):
            raise ValueError("Prepare a clean alignment copy before starting a SoCLaaS evaluation.")
        shared.atomic_json(config_path, identity)
    if shared.apply_deterministic(document):
        shared.atomic_json(alignment_path, document)
    ledger_path = work / "token_usage.json"
    errors_path = work / "errors.json"
    ledger = shared.read_json(ledger_path) if ledger_path.exists() else {
        "prompt_tokens": 0, "completion_tokens": 0, "requests": 0, "record_ids": []}
    errors = shared.read_json(errors_path) if errors_path.exists() else {}
    archive = work / "responses"
    for path in sorted(archive.glob("*.json")):
        apply_record(shared.read_json(path), document, alignment_path, ledger, ledger_path,
                     errors, errors_path)
    pending = shared.pending_rows(document, stages)
    if args.max_new_rows:
        pending = pending[:args.max_new_rows]
    requests, specs = shared.make_requests(pending, records, stages, settings.model, "low",
                                           args.batch_size, document, args.max_tokens)
    if not requests:
        print("Evaluation already complete for the selected stage(s).")
        return 0
    archive.mkdir(parents=True, exist_ok=True)
    print(f"Sending {len(requests)} SoCLaaS requests for {len(pending)} rows with model "
          f"{settings.model} and up to {args.workers} workers.", flush=True)
    pacer = RequestPacer(args.request_interval)
    request_iter = iter(requests)
    active = {}
    failures = finished = 0
    interrupted = stop_submitting = False
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        def submit_next():
            request = next(request_iter, None)
            if request is not None:
                future = executor.submit(request_with_retries, completion, request["body"],
                                         settings, args, pacer)
                active[future] = (request, 0)

        for _ in range(min(args.workers, len(requests))):
            submit_next()
        while active:
            try:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                interrupted = stop_submitting = True
                print("Stopping after in-flight requests finish; their results will be saved.", flush=True)
                continue
            for future in done:
                request, invalid_attempts = active.pop(future)
                try:
                    result = future.result()
                except Exception as error:
                    failures += 1
                    stop_submitting = True
                    errors[f"request_{time.time_ns()}_{uuid4().hex}"] = {
                        "custom_id": request["custom_id"], "error": str(error)}
                    shared.atomic_json(errors_path, errors)
                    outcome = f"stopped after API error: {error}"
                else:
                    record_id = f"{time.time_ns()}_{uuid4().hex}"
                    record = {"record_id": record_id, "custom_id": request["custom_id"],
                              "spec": specs[request["custom_id"]], "text": result.text,
                              "finish_reason": result.finish_reason, "usage": result.usage,
                              "reported_model": result.model}
                    shared.atomic_json(archive / f"{record_id}.json", record)
                    completed, invalid = apply_record(record, document, alignment_path, ledger,
                                                      ledger_path, errors, errors_path)
                    if (invalid and invalid_attempts < args.max_invalid_retries
                            and not interrupted and not stop_submitting):
                        print(f"{request['custom_id']}: invalid response; retry "
                              f"{invalid_attempts + 1}/{args.max_invalid_retries}", flush=True)
                        retry = executor.submit(request_with_retries, completion, request["body"],
                                                settings, args, pacer)
                        active[retry] = (request, invalid_attempts + 1)
                        continue
                    failures += int(invalid)
                    outcome = "invalid response" if invalid else f"saved {completed} rows"
                finished += 1
                print(f"{request['custom_id']}: {outcome} ({finished}/{len(requests)})", flush=True)
                if not stop_submitting:
                    submit_next()
    print(f"SoCLaaS evaluation finished: {ledger['requests']} responses, "
          f"{ledger['prompt_tokens']} prompt and {ledger['completion_tokens']} completion tokens; "
          f"{failures} new requests need retry.")
    return 130 if interrupted else 1 if failures else 0


def main(argv=None, *, completion=request_completion, config=None):
    args = parse_args(argv)
    if args.dry_run:
        return run(args, completion=completion, config=config)
    work = (args.alignment_root / args.ticker.lower() /
            f"{args.previous_year}-{args.current_year}" / "soclaas_eval")
    work.mkdir(parents=True, exist_ok=True)
    with (work / "runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another SoCLaaS evaluation is running for this year-pair.") from error
        return run(args, completion=completion, config=config)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"SoCLaaS evaluation stopped: {error}") from error
