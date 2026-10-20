#!/usr/bin/env python3
"""Classify prepared alignment rows with OpenAI Batch, then resume and save results."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from alignment_eval_shared import (CHANGE_ANALYSIS_PROMPT, MATERIALITY_PROMPT, MATERIALITY_FIELDS,
    atomic_json, read_json, fingerprint, observed_spend, other_active_batches,
    projected_cost, load_inputs, source_identity, apply_deterministic, pending_rows,
    make_requests, validate_results, collect_responses)

END_STATES = {"completed", "failed", "expired", "cancelled"}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--alignment-root", type=Path, default=Path("data/alignments_for_eval_openai_batch"))
    parser.add_argument("--disclosures-dir", type=Path, default=Path("data/disclosures"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--stage", choices=("both", "change", "materiality"), default="both")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-output-tokens", type=int, default=8000)
    parser.add_argument("--max-new-rows", type=int,
                        help="Submit at most this many unfinished rows; useful for a pilot.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print pending request counts and an upper-output cost projection without writing files.")
    parser.add_argument("--submit-only", action="store_true",
                        help="Return after submission; rerun the command to collect the batch.")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--max-spend-usd", type=float, default=45.0,
                        help="Stop submissions when projected spend across --alignment-root exceeds this amount.")
    parser.add_argument("--batch-input-usd-per-million", type=float,
                        help="Batch input price; defaults to current Sol pricing for gpt-6.1-sol only.")
    parser.add_argument("--batch-output-usd-per-million", type=float,
                        help="Batch output price; defaults to current Sol pricing for gpt-6.1-sol only.")
    args = parser.parse_args(argv)
    if min(args.batch_size, args.max_output_tokens, args.poll_seconds) < 1 or args.max_spend_usd <= 0:
        parser.error("Batch size, output tokens, poll seconds, and spend limit must be positive.")
    if args.max_new_rows is not None and args.max_new_rows < 1:
        parser.error("--max-new-rows must be positive.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]*", args.ticker):
        parser.error("--ticker must be a safe company directory name.")
    if (not re.fullmatch(r"\d{4}", args.previous_year)
            or not re.fullmatch(r"\d{4}", args.current_year)
            or args.previous_year >= args.current_year):
        parser.error("Years must be increasing four-digit fiscal years.")
    if any(value is not None and value < 0 for value in
           (args.batch_input_usd_per_million, args.batch_output_usd_per_million)):
        parser.error("Token prices must be nonnegative.")
    return args


def main(argv=None, *, client=None):
    args = parse_args(argv)
    load_dotenv(args.env_file)
    model = args.model or os.environ.get("OPENAI_MODEL") or "gpt-6.1-sol"
    if model == "gpt-6.1-sol":
        if args.batch_input_usd_per_million is None:
            args.batch_input_usd_per_million = 1.0
        if args.batch_output_usd_per_million is None:
            args.batch_output_usd_per_million = 5.0
    elif args.batch_input_usd_per_million is None or args.batch_output_usd_per_million is None:
        raise ValueError("For models other than gpt-6.1-sol, supply both Batch token prices.")
    ticker = args.ticker.lower()
    pair = f"{args.previous_year}-{args.current_year}"
    alignment_path = args.alignment_root / ticker / pair / "alignments.json"
    document, records = load_inputs(alignment_path, args.disclosures_dir)
    fallback_ids = sorted(key for key, record in records.items()
                          if record.get("evidence_source") == "alignment_citations")
    if document["company"] != ticker or (str(document["previous_year"]), str(document["current_year"])) != (
            args.previous_year, args.current_year):
        raise ValueError("Alignment company or years do not match the requested pair.")
    work = alignment_path.parent / "openai_eval"
    config_path, manifest_path = work / "config.json", work / "active_batch.json"
    config = {"model": model, "reasoning_effort": args.reasoning_effort,
              "source_hash": source_identity(document, records),
              "change_prompt_hash": fingerprint(CHANGE_ANALYSIS_PROMPT),
              "materiality_prompt_hash": fingerprint(MATERIALITY_PROMPT)}
    if config_path.exists() and read_json(config_path) != config:
        raise ValueError("Model, prompt, or source evidence changed; use a new prepared alignment directory.")
    stages = ("change", "materiality") if args.stage == "both" else (args.stage,)
    if args.dry_run:
        apply_deterministic(document)
        pending = pending_rows(document, stages)
        if args.max_new_rows:
            pending = pending[:args.max_new_rows]
        requests, _ = make_requests(pending, records, stages, model, args.reasoning_effort,
                                    args.batch_size, document, args.max_output_tokens)
        estimate = projected_cost(requests, args.batch_input_usd_per_million,
                                  args.batch_output_usd_per_million)
        print(f"Dry run: {len(pending)} LLM rows, {len(requests)} Batch requests; "
              f"planning estimate with maximum output: ${estimate:.2f}; "
              f"{len(fallback_ids)} citation-only disclosures. No files changed.")
        return 0
    if not config_path.exists():
        atomic_json(config_path, config)
    if fallback_ids:
        atomic_json(work / "source_fallbacks.json", {"disclosure_ids": fallback_ids,
                                                      "source": "alignment_citations"})
    if apply_deterministic(document):
        atomic_json(alignment_path, document)
    pending = pending_rows(document, stages)
    if not pending and not manifest_path.exists():
        print("Evaluation already complete for the selected stage(s).")
        return 0
    if client is None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("Set OPENAI_API_KEY in --env-file before submitting or collecting a batch.")
        from openai import OpenAI
        client = OpenAI(api_key=key)
    if manifest_path.exists():
        manifest = read_json(manifest_path)
    else:
        ledger_path = work / "token_usage.json"
        ledger = read_json(ledger_path) if ledger_path.exists() else {"input_tokens": 0, "output_tokens": 0,
                                                                     "estimated_usd": 0.0, "batches": 0,
                                                                     "batch_ids": []}
        if other_active_batches(args.alignment_root, manifest_path):
            raise ValueError("Another evaluation batch is active under --alignment-root; collect it first.")
        spent = observed_spend(args.alignment_root)
        if spent >= args.max_spend_usd:
            raise ValueError("Local spend threshold reached across evaluation files; no new batch submitted.")
        if args.max_new_rows:
            pending = pending[:args.max_new_rows]
        requests, specs = make_requests(pending, records, stages, model, args.reasoning_effort,
                                       args.batch_size, document, args.max_output_tokens)
        if not requests:
            print("No LLM requests needed.")
            return 0
        estimated_next_usd = projected_cost(requests, args.batch_input_usd_per_million,
                                            args.batch_output_usd_per_million)
        if spent + estimated_next_usd > args.max_spend_usd:
            raise ValueError("Conservative next-batch cost estimate exceeds --max-spend-usd; "
                             "reduce --max-new-rows or batch size. This estimate is not a billing guarantee.")
        work.mkdir(parents=True, exist_ok=True)
        batch_number = ledger["batches"] + 1
        request_path = work / f"batch_{batch_number:04d}_input.jsonl"
        request_path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests),
                                encoding="utf-8")
        with request_path.open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="batch")
        manifest = {"batch_number": batch_number, "input_file_id": uploaded.id,
                    "batch_id": None, "specs": specs}
        atomic_json(manifest_path, manifest)
        batch = client.batches.create(input_file_id=uploaded.id, endpoint="/v1/responses",
                                      completion_window="24h")
        manifest["batch_id"] = batch.id
        atomic_json(manifest_path, manifest)
        print(f"Submitted {len(requests)} requests covering {len(pending)} rows: {batch.id}", flush=True)
        if args.submit_only:
            return 0
    if manifest["batch_id"] is None:
        raise ValueError(f"Batch upload exists without a batch ID in {manifest_path}; inspect before retrying.")
    while True:
        batch = client.batches.retrieve(manifest["batch_id"])
        if batch.status in END_STATES:
            break
        print(f"Batch {batch.id}: {batch.status}; checking again in {args.poll_seconds}s", flush=True)
        if args.submit_only:
            return 0
        time.sleep(args.poll_seconds)
    if not batch.output_file_id:
        atomic_json(work / f"batch_{manifest['batch_number']:04d}_failed.json",
                    {"batch_id": batch.id, "status": batch.status})
        manifest_path.unlink()
        raise ValueError(f"Batch {batch.id} ended as {batch.status} without results; rerun to resubmit pending rows.")
    response_path = work / f"batch_{manifest['batch_number']:04d}_output.jsonl"
    if not response_path.exists():
        output_text = client.files.content(batch.output_file_id).text
        if batch.error_file_id:
            output_text += "\n" + client.files.content(batch.error_file_id).text
        response_path.write_text(output_text, encoding="utf-8")
    usage, errors, completed = collect_responses(response_path.read_text(encoding="utf-8"),
                                                  manifest["specs"], document)
    atomic_json(alignment_path, document)
    ledger_path = work / "token_usage.json"
    ledger = read_json(ledger_path) if ledger_path.exists() else {"input_tokens": 0, "output_tokens": 0,
                                                                 "estimated_usd": 0.0, "batches": 0,
                                                                 "batch_ids": []}
    if manifest["batch_id"] not in ledger.get("batch_ids", []):
        ledger["input_tokens"] += usage["input_tokens"]
        ledger["output_tokens"] += usage["output_tokens"]
        ledger["estimated_usd"] = round((ledger["input_tokens"] * args.batch_input_usd_per_million
                                        + ledger["output_tokens"] * args.batch_output_usd_per_million) / 1_000_000, 6)
        ledger["batches"] += 1
        ledger.setdefault("batch_ids", []).append(manifest["batch_id"])
        atomic_json(ledger_path, ledger)
    atomic_json(work / f"batch_{manifest['batch_number']:04d}_errors.json", errors)
    manifest_path.unlink()
    print(f"Saved {completed} results; {len(errors)} requests need review or retry. "
          f"Reported usage: {usage}; local estimated total: ${ledger['estimated_usd']:.2f}.")
    if errors or batch.status != "completed":
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Evaluation stopped: {error}") from error
