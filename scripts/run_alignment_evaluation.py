#!/usr/bin/env python3
"""Prepare, classify, and compare alignment predictions in one resumable run."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import alignment_eval_shared as shared
import evaluate_alignment_predictions as comparison
import prepare_alignments_for_eval as preparation
import run_alignment_eval_openai_batch as openai_batch
import run_alignment_eval_openai_standard as openai_standard
import run_alignment_eval_soclaas as soclaas


RUNNERS = {
    "soclaas": soclaas,
    "openai_batch": openai_batch,
    "openai_standard": openai_standard,
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--llm", choices=tuple(RUNNERS), default="soclaas")
    parser.add_argument("--model", help="Model ID passed to the selected provider.")
    parser.add_argument("--reference-label",
                        help="Display name for this model in evaluation reports; defaults to its model ID.")
    parser.add_argument("--input-root", type=Path, default=Path("data/alignments"),
                        help="Source finalized alignments; never modified by this script.")
    parser.add_argument("--candidate-root", type=Path,
                        help="Qwen predictions to compare; defaults to --input-root.")
    parser.add_argument("--alignment-root", type=Path,
                        help="Override data/alignments_for_eval_<llm> for a separate model run.")
    parser.add_argument("--evaluation-root", "--output-root", dest="evaluation_root", type=Path,
                        help="Override data/evaluation_<llm> for this model run.")
    parser.add_argument("--disclosures-dir", type=Path, default=Path("data/disclosures"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--workers", type=int,
                        help="Concurrent calls for SoCLaaS or standard OpenAI; not Batch API.")
    parser.add_argument("--max-new-rows", type=int,
                        help="Pilot cap; rerun without it to finish remaining rows.")
    parser.add_argument("--max-invalid-retries", type=int,
                        help="Additional attempts for invalid responses (default: 3; SoCLaaS or standard OpenAI).")
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--timeout", type=float,
                        help="Per-request timeout for SoCLaaS or standard OpenAI.")
    parser.add_argument("--request-interval", type=float,
                        help="Seconds between SoCLaaS request starts.")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"),
                        help="OpenAI reasoning effort.")
    parser.add_argument("--max-spend-usd", type=float,
                        help="Local OpenAI estimate guard, not an account billing cap.")
    parser.add_argument("--input-usd-per-million", type=float,
                        help="Input-token price for a non-default OpenAI model.")
    parser.add_argument("--output-usd-per-million", type=float,
                        help="Output-token price for a non-default OpenAI model.")
    parser.add_argument("--submit-only", action="store_true",
                        help="For OpenAI Batch, submit and return; rerun to collect and evaluate.")
    args = parser.parse_args(argv)
    args.ticker = args.ticker.lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", args.ticker):
        parser.error("--ticker must be a safe company directory name.")
    if (not re.fullmatch(r"\d{4}", args.previous_year)
            or not re.fullmatch(r"\d{4}", args.current_year)
            or args.previous_year >= args.current_year):
        parser.error("Years must be increasing four-digit fiscal years.")
    if args.model is not None and (not args.model.strip() or any(c.isspace() for c in args.model)):
        parser.error("--model must be a nonempty model ID without whitespace.")
    if args.reference_label is not None and not args.reference_label.strip():
        parser.error("--reference-label must not be empty.")
    for name in ("batch_size", "workers", "max_new_rows", "max_output_tokens", "timeout",
                 "max_spend_usd"):
        value = getattr(args, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive.")
    if args.request_interval is not None and args.request_interval < 0:
        parser.error("--request-interval must be nonnegative.")
    if args.max_invalid_retries is not None and args.max_invalid_retries < 0:
        parser.error("--max-invalid-retries must be nonnegative.")
    prices = (args.input_usd_per_million, args.output_usd_per_million)
    if any(price is not None and price < 0 for price in prices) or (prices[0] is None) != (prices[1] is None):
        parser.error("Supply both nonnegative OpenAI token prices, or neither.")
    if args.llm == "soclaas" and (args.reasoning_effort or args.max_spend_usd or prices[0] is not None
                                  or args.submit_only):
        parser.error("OpenAI pricing, reasoning, spend and --submit-only do not apply to SoCLaaS.")
    if args.llm != "soclaas" and args.request_interval is not None:
        parser.error("--request-interval applies only to SoCLaaS.")
    if args.llm == "openai_batch" and (args.workers is not None or args.timeout is not None):
        parser.error("OpenAI Batch does not use --workers or --timeout.")
    if args.llm == "openai_batch" and args.max_invalid_retries is not None:
        parser.error("--max-invalid-retries does not apply to OpenAI Batch.")
    if args.llm != "openai_batch" and args.submit_only:
        parser.error("--submit-only applies only to OpenAI Batch.")
    return args


def provider_paths(args):
    alignment_root = args.alignment_root or Path(f"data/alignments_for_eval_{args.llm}")
    evaluation_root = args.evaluation_root or Path(f"data/evaluation_{args.llm}")
    return alignment_root, evaluation_root


def ensure_prepared(args, alignment_root, pair):
    source_path = args.input_root / args.ticker / pair / "alignments.json"
    target = alignment_root / args.ticker / pair / "alignments.json"
    if source_path.resolve() == target.resolve():
        raise ValueError("Prepared alignment root must differ from --input-root.")
    source = preparation.prepare_document(shared.read_json(source_path), args.ticker, pair)
    if target.exists():
        existing = preparation.prepare_document(shared.read_json(target), args.ticker, pair)
        if existing != source:
            raise ValueError(f"Source alignments changed since preparation: {target}. "
                             "Use a new --alignment-root; existing classifications were preserved.")
        print(f"Resuming prepared alignments: {target}", flush=True)
    else:
        shared.atomic_json(target, source)
        print(f"Prepared {len(source['alignments'])} rows: {target}", flush=True)
    return target


def classifier_argv(args, alignment_root):
    command = ["--ticker", args.ticker, "--previous-year", args.previous_year,
               "--current-year", args.current_year, "--alignment-root", str(alignment_root),
               "--disclosures-dir", str(args.disclosures_dir), "--env-file", str(args.env_file)]
    model = args.model or ("x-test-2" if args.llm == "soclaas" else None)
    if model:
        command.extend(("--model", model))
    for name in ("batch_size", "workers", "max_new_rows", "timeout"):
        value = getattr(args, name)
        if value is not None:
            command.extend((f"--{name.replace('_', '-')}", str(value)))
    if args.max_invalid_retries is not None:
        command.extend(("--max-invalid-retries", str(args.max_invalid_retries)))
    if args.max_output_tokens is not None:
        option = "--max-tokens" if args.llm == "soclaas" else "--max-output-tokens"
        command.extend((option, str(args.max_output_tokens)))
    if args.llm == "soclaas":
        if args.request_interval is not None:
            command.extend(("--request-interval", str(args.request_interval)))
    else:
        if args.reasoning_effort:
            command.extend(("--reasoning-effort", args.reasoning_effort))
        if args.max_spend_usd is not None:
            command.extend(("--max-spend-usd", str(args.max_spend_usd)))
        if args.input_usd_per_million is not None:
            input_option = ("--batch-input-usd-per-million" if args.llm == "openai_batch"
                            else "--input-usd-per-million")
            output_option = ("--batch-output-usd-per-million" if args.llm == "openai_batch"
                             else "--output-usd-per-million")
            command.extend((input_option, str(args.input_usd_per_million),
                            output_option, str(args.output_usd_per_million)))
        if args.submit_only:
            command.append("--submit-only")
    return command


def run(args, *, classifier=None, evaluator=comparison.main):
    pair = f"{args.previous_year}-{args.current_year}"
    alignment_root, evaluation_root = provider_paths(args)
    target = ensure_prepared(args, alignment_root, pair)
    runner = classifier or RUNNERS[args.llm].main
    result = runner(classifier_argv(args, alignment_root))
    if result:
        print("Classification stopped; comparison was not run. Rerun to resume.", flush=True)
        return result
    if args.llm == "openai_batch" and (target.parent / "openai_eval" / "active_batch.json").exists():
        print("OpenAI Batch submitted but not collected. Rerun this command after completion.", flush=True)
        return 0
    document = shared.read_json(target)
    pending = shared.pending_rows(document, ("change", "materiality"))
    if pending:
        print(f"{len(pending)} rows still need classification; comparison deferred. "
              "Rerun without --max-new-rows to finish.", flush=True)
        return 0
    state = ("soclaas_eval" if args.llm == "soclaas" else "openai_eval")
    config_path = target.parent / state / "config.json"
    configured_model = shared.read_json(config_path)["model"] if config_path.exists() else None
    label = args.reference_label or configured_model or args.model or (
        "x-test-2" if args.llm == "soclaas" else "gpt-6.1-sol")
    candidate_root = args.candidate_root or args.input_root
    return evaluator(["--ticker", args.ticker, "--year-pair", pair,
                      "--reference-root", str(alignment_root),
                      "--candidate-root", str(candidate_root),
                      "--output-root", str(evaluation_root), "--model", label])


def main(argv=None, *, classifier=None, evaluator=comparison.main):
    return run(parse_args(argv), classifier=classifier, evaluator=evaluator)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(f"Alignment evaluation stopped: {error}") from error
