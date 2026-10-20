#!/usr/bin/env python3
"""Classify prepared alignment rows with concurrent standard OpenAI API requests."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import alignment_eval_shared as shared


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--alignment-root", type=Path, default=Path("data/alignments_for_eval_openai_standard"))
    parser.add_argument("--disclosures-dir", type=Path, default=Path("data/disclosures"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--stage", choices=("both", "change", "materiality"), default="both")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="Alignment rows per API request, not OpenAI Batch API size.")
    parser.add_argument("--workers", type=int, default=4,
                        help="Maximum simultaneous standard API requests.")
    parser.add_argument("--max-output-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=float, default=300,
                        help="Seconds per API attempt; the SDK may retry transient errors.")
    parser.add_argument("--max-new-rows", type=int)
    parser.add_argument("--max-invalid-retries", type=int, default=3,
                        help="Additional attempts for invalid model responses.")
    parser.add_argument("--max-spend-usd", type=float, default=70.0,
                        help="Local estimate across this alignment root, not an account billing cap.")
    parser.add_argument("--input-usd-per-million", type=float)
    parser.add_argument("--output-usd-per-million", type=float)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if min(args.batch_size, args.workers, args.max_output_tokens, args.timeout, args.max_spend_usd) <= 0:
        parser.error("Batch size, workers, output tokens, timeout and spend limit must be positive.")
    if args.max_new_rows is not None and args.max_new_rows < 1:
        parser.error("--max-new-rows must be positive.")
    if args.max_invalid_retries < 0:
        parser.error("--max-invalid-retries must be nonnegative.")
    if not shared.re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", args.ticker):
        parser.error("--ticker must be a safe company directory name.")
    if (not shared.re.fullmatch(r"\d{4}", args.previous_year)
            or not shared.re.fullmatch(r"\d{4}", args.current_year)
            or args.previous_year >= args.current_year):
        parser.error("Years must be increasing four-digit fiscal years.")
    if any(value is not None and value < 0 for value in
           (args.input_usd_per_million, args.output_usd_per_million)):
        parser.error("Token prices must be nonnegative.")
    return args


def spend_so_far(root):
    batch = shared.observed_spend(root)
    standard = sum(shared.read_json(path)["estimated_usd"]
                   for path in root.glob("*/*/openai_eval/standard_token_usage.json"))
    return batch + standard


def empty_ledger():
    return {"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0,
            "requests": 0, "response_record_ids": []}


def response_cost(usage, input_price, output_price, *, sol):
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    details = usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens", 0) if sol else 0
    cache_write = details.get("cache_write_tokens", 0) if sol else 0
    regular = max(input_tokens - cached - cache_write, 0)
    return (regular * input_price + cached * input_price * 0.05
            + cache_write * input_price * 1.25
            + output_tokens * output_price) / 1_000_000


def apply_response_record(record, document, alignment_path, ledger, ledger_path,
                          errors, errors_path, input_price, output_price, *, sol):
    request_id = record["request_id"]
    custom_id = record["custom_id"]
    raw = json.dumps({"custom_id": custom_id,
                      "response": {"status_code": 200, "body": record["body"]},
                      "error": None})
    usage, failures, completed = shared.collect_responses(raw, {custom_id: record["spec"]}, document)
    if request_id not in ledger["response_record_ids"]:
        ledger["input_tokens"] += usage["input_tokens"]
        ledger["output_tokens"] += usage["output_tokens"]
        ledger["estimated_usd"] = round(
            ledger["estimated_usd"] + response_cost(record["body"].get("usage") or {},
                                                   input_price, output_price, sol=sol), 6)
        ledger["requests"] += 1
        ledger["response_record_ids"].append(request_id)
        shared.atomic_json(ledger_path, ledger)
    if failures:
        errors[request_id] = failures[0]
        shared.atomic_json(errors_path, errors)
    if completed:
        shared.atomic_json(alignment_path, document)
    return completed, bool(failures)


def call_standard(client, request):
    response = client.responses.create(**request["body"])
    return response.model_dump()


def run(args, *, client=None):
    load_dotenv(args.env_file)
    model = args.model or os.environ.get("OPENAI_MODEL") or "gpt-6.1-sol"
    sol = model == "gpt-6.1-sol"
    if sol:
        input_price = args.input_usd_per_million if args.input_usd_per_million is not None else 2.0
        output_price = args.output_usd_per_million if args.output_usd_per_million is not None else 10.0
    elif args.input_usd_per_million is None or args.output_usd_per_million is None:
        raise ValueError("For models other than gpt-6.1-sol, supply both standard API token prices.")
    else:
        input_price, output_price = args.input_usd_per_million, args.output_usd_per_million
    ticker = args.ticker.lower()
    pair = f"{args.previous_year}-{args.current_year}"
    alignment_path = args.alignment_root / ticker / pair / "alignments.json"
    document, records = shared.load_inputs(alignment_path, args.disclosures_dir)
    if document["company"] != ticker or (str(document["previous_year"]),
                                           str(document["current_year"])) != (
            args.previous_year, args.current_year):
        raise ValueError("Alignment company or years do not match the requested pair.")
    work = alignment_path.parent / "openai_eval"
    config_path = work / "config.json"
    config = {"model": model, "reasoning_effort": args.reasoning_effort,
              "source_hash": shared.source_identity(document, records),
              "change_prompt_hash": shared.fingerprint(shared.CHANGE_ANALYSIS_PROMPT),
              "materiality_prompt_hash": shared.fingerprint(shared.MATERIALITY_PROMPT)}
    if config_path.exists() and shared.read_json(config_path) != config:
        raise ValueError("Model, prompt, or source evidence changed; use a new prepared alignment directory.")
    stages = ("change", "materiality") if args.stage == "both" else (args.stage,)
    if args.dry_run:
        shared.apply_deterministic(document)
        pending = shared.pending_rows(document, stages)
        if args.max_new_rows:
            pending = pending[:args.max_new_rows]
        requests, _ = shared.make_requests(pending, records, stages, model, args.reasoning_effort,
                                           args.batch_size, document, args.max_output_tokens)
        estimate = shared.projected_cost(requests, input_price, output_price)
        print(f"Dry run: {len(pending)} pending rows, {len(requests)} standard API requests "
              f"with up to {args.workers} concurrent; conservative cost projection ${estimate:.2f}. "
              "No files changed.")
        return 0
    if (work / "active_batch.json").exists():
        raise ValueError("An OpenAI Batch job is active for this year-pair; collect it with "
                         "run_alignment_eval_openai_batch.py before using the standard API runner.")
    if not config_path.exists() and (work / "standard_responses").exists():
        raise ValueError("Saved standard responses exist without config.json; inspect them before resuming.")
    if not config_path.exists():
        shared.atomic_json(config_path, config)
    if shared.apply_deterministic(document):
        shared.atomic_json(alignment_path, document)
    ledger_path = work / "standard_token_usage.json"
    errors_path = work / "standard_errors.json"
    ledger = shared.read_json(ledger_path) if ledger_path.exists() else empty_ledger()
    errors = shared.read_json(errors_path) if errors_path.exists() else {}
    archive = work / "standard_responses"
    for path in sorted(archive.glob("*.json")):
        record = shared.read_json(path)
        apply_response_record(record, document, alignment_path, ledger, ledger_path,
                              errors, errors_path, input_price, output_price, sol=sol)
    pending = shared.pending_rows(document, stages)
    if args.max_new_rows:
        pending = pending[:args.max_new_rows]
    requests, specs = shared.make_requests(pending, records, stages, model, args.reasoning_effort,
                                           args.batch_size, document, args.max_output_tokens)
    if not requests:
        print("Evaluation already complete for the selected stage(s).")
        return 0
    spent = spend_so_far(args.alignment_root)
    projection = shared.projected_cost(requests, input_price * 1.25, output_price)
    if spent >= args.max_spend_usd or spent + projection > args.max_spend_usd:
        raise ValueError(f"Local spend estimate ${spent:.2f} plus conservative request projection "
                         f"${projection:.2f} exceeds --max-spend-usd ${args.max_spend_usd:.2f}. "
                         "This is not an account billing cap; reduce --max-new-rows if needed.")
    if client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("Set OPENAI_API_KEY in --env-file before sending standard API requests.")
        from openai import OpenAI
        client = OpenAI(api_key=key, timeout=args.timeout, max_retries=2)
    archive.mkdir(parents=True, exist_ok=True)
    print(f"Sending {len(requests)} standard API requests for {len(pending)} rows "
          f"with {args.workers} workers; prior estimated spend ${spent:.2f}.", flush=True)
    request_iter = iter(requests)
    active = {}
    saved_rows = failures = finished = 0
    interrupted = False
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        def submit_next():
            request = next(request_iter, None)
            if request is not None:
                active[executor.submit(call_standard, client, request)] = (request, 0)

        for _ in range(min(args.workers, len(requests))):
            submit_next()
        while active:
            try:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                interrupted = True
                print("Stopping after in-flight requests finish; their results will be saved.", flush=True)
                continue
            for future in done:
                request, invalid_attempts = active.pop(future)
                try:
                    body = future.result()
                except Exception as error:
                    failures += 1
                    error_id = f"{time.time_ns()}_{uuid4().hex}"
                    errors[error_id] = {"custom_id": request["custom_id"], "error": str(error)}
                    shared.atomic_json(errors_path, errors)
                    outcome = f"failed: {error}"
                else:
                    record_id = f"{time.time_ns()}_{uuid4().hex}"
                    record = {"request_id": record_id, "custom_id": request["custom_id"],
                              "spec": specs[request["custom_id"]], "body": body}
                    shared.atomic_json(archive / f"{record_id}.json", record)
                    completed, invalid = apply_response_record(
                        record, document, alignment_path, ledger, ledger_path,
                        errors, errors_path, input_price, output_price, sol=sol)
                    saved_rows += completed
                    retry_cost = shared.projected_cost([request], input_price * 1.25, output_price)
                    if (invalid and invalid_attempts < args.max_invalid_retries and not interrupted
                            and spend_so_far(args.alignment_root) + retry_cost <= args.max_spend_usd):
                        print(f"{request['custom_id']}: invalid response; retry "
                              f"{invalid_attempts + 1}/{args.max_invalid_retries}", flush=True)
                        active[executor.submit(call_standard, client, request)] = (
                            request, invalid_attempts + 1)
                        continue
                    failures += int(invalid)
                    outcome = f"saved {completed} rows" if not invalid else "invalid response"
                finished += 1
                print(f"{request['custom_id']}: {outcome} ({finished}/{len(requests)})", flush=True)
                if not interrupted:
                    submit_next()
    print(f"Saved {saved_rows} rows; {failures} requests failed or need retry. "
          f"Recorded standard API estimate: ${ledger['estimated_usd']:.2f}.")
    return 130 if interrupted else 1 if failures else 0


def main(argv=None, *, client=None):
    args = parse_args(argv)
    if args.dry_run:
        return run(args, client=client)
    work = (args.alignment_root / args.ticker.lower() /
            f"{args.previous_year}-{args.current_year}" / "openai_eval")
    work.mkdir(parents=True, exist_ok=True)
    with (work / "standard_runner.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another standard evaluation is running for this year-pair.") from error
        return run(args, client=client)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Standard evaluation stopped: {error}") from error
