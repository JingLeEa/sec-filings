"""Classify materiality for finalized disclosure alignment rows."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sec_disclosure.llm.client import LLMError
from sec_disclosure.llm.config import load_config
from sec_disclosure.llm.disclosures import digest, write_json

from .alignment_runtime import RunLimit
from .materiality_graph import GRAPH_POLICY, MaterialityAgentGraph
from .materiality_prompts import MATERIALITY_PROMPT
from .materiality_runtime import MaterialityRuntime


MATERIALITY_FIELD = "materiality_analysis"
MATERIALITY_LABELS = {"Yes", "No", "Uncertain"}
MATERIALITY_THRESHOLD = 0.50
MIN_CONFIDENCE = 0.60
ANALYSIS_FIELD_ORDER = (
    "materiality", "materiality_score", "confidence_score", "reason", "key_change",
)
ANALYSIS_FIELDS = set(ANALYSIS_FIELD_ORDER)
LEGACY_ANALYSIS_FIELDS = {"materiality", "materiality_score", "reason", "key_change"}
EXACT_TEXT_ANALYSIS = {
    "materiality": "No",
    "materiality_score": 0.0,
    "confidence_score": 1.0,
    "reason": (
        "The aligned disclosures are exact-text matches under the automatic matching policy, "
        "so no meaningful disclosure change is identified."
    ),
    "key_change": "No substantive change",
}


def new_run_state(classification_run_id, *, started_at=None):
    initial_request_count = 0 if started_at is not None else None
    return {
        "schema_version": "2",
        "classification_run_id": classification_run_id,
        "timing": {
            "started_at": started_at,
            "completed_at": None,
            "wall_clock_seconds": None,
            "cumulative_api_seconds": None,
            "api_requests_timed": initial_request_count,
            "api_requests_total": initial_request_count,
        },
    }


def normalize_run_state(value):
    if not isinstance(value, dict):
        raise ValueError("Materiality run state must be a JSON object.")
    run_id = value.get("classification_run_id")
    if run_id is not None and not isinstance(run_id, str):
        raise ValueError("Materiality run state has an invalid classification_run_id.")
    result = new_run_state(run_id)
    timing = value.get("timing")
    if isinstance(timing, dict):
        for key in result["timing"]:
            if key in timing:
                result["timing"][key] = timing[key]
    if (result["timing"]["started_at"] is None
            and result["timing"]["completed_at"] is None
            and result["timing"]["cumulative_api_seconds"] is None):
        result["timing"]["api_requests_timed"] = None
        result["timing"]["api_requests_total"] = None
    return result


def update_run_timing(run_state, runtime=None, *, complete=False):
    if run_state is None:
        return
    timing = run_state["timing"]
    if runtime is not None:
        timing.update(runtime.timing_report())
    if complete and timing["started_at"] is not None and timing["completed_at"] is None:
        timing["completed_at"] = datetime.now(timezone.utc).isoformat()
        started = datetime.fromisoformat(timing["started_at"])
        completed = datetime.fromisoformat(timing["completed_at"])
        timing["wall_clock_seconds"] = round((completed - started).total_seconds(), 3)


def label_for_scores(materiality_score, confidence_score):
    if confidence_score < MIN_CONFIDENCE:
        return "Uncertain"
    return "Yes" if materiality_score >= MATERIALITY_THRESHOLD else "No"


def validate_analysis(value, *, context="materiality_analysis"):
    if not isinstance(value, dict) or set(value) != ANALYSIS_FIELDS:
        raise ValueError(f"{context} must contain exactly {sorted(ANALYSIS_FIELDS)}.")
    label = value["materiality"]
    if not isinstance(label, str) or label not in MATERIALITY_LABELS:
        raise ValueError(f"{context}.materiality must be Yes, No, or Uncertain.")
    for key in ("materiality_score", "confidence_score"):
        score = value[key]
        if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError(f"{context}.{key} must be a finite number from 0 to 1.")
    expected_label = label_for_scores(value["materiality_score"], value["confidence_score"])
    if label != expected_label:
        raise ValueError(
            f"{context}.materiality must be {expected_label} for materiality_score "
            f"{value['materiality_score']} and confidence_score {value['confidence_score']}."
        )
    for key in ("reason", "key_change"):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError(f"{context}.{key} must be a nonempty string.")
    return {
        "materiality": label,
        "materiality_score": round(float(value["materiality_score"]), 12),
        "confidence_score": round(float(value["confidence_score"]), 12),
        "reason": value["reason"].strip(),
        "key_change": value["key_change"].strip(),
    }


def normalize_saved_analysis(value, *, context="materiality_analysis"):
    """Validate current results or migrate the former confidence-only schema."""
    if isinstance(value, dict) and set(value) == ANALYSIS_FIELDS:
        normalized = validate_analysis(value, context=context)
        return normalized, normalized != value
    if not isinstance(value, dict) or set(value) != LEGACY_ANALYSIS_FIELDS:
        raise ValueError(
            f"{context} must use the current fields {sorted(ANALYSIS_FIELDS)} or the "
            f"legacy fields {sorted(LEGACY_ANALYSIS_FIELDS)}."
        )

    label = value["materiality"]
    old_score = value["materiality_score"]
    if not isinstance(label, str) or label not in MATERIALITY_LABELS:
        raise ValueError(f"{context}.materiality must be Yes, No, or Uncertain.")
    if label == "Uncertain":
        if old_score is not None:
            raise ValueError(f"{context}.materiality_score must be null in legacy Uncertain rows.")
        materiality_score, confidence_score = 0.5, 0.0
    else:
        if (type(old_score) not in {int, float} or not math.isfinite(old_score)
                or not 0 <= old_score <= 1):
            raise ValueError(f"{context}.materiality_score must be a finite number from 0 to 1.")
        confidence_score = float(old_score)
        materiality_score = confidence_score if label == "Yes" else 1.0 - confidence_score

    migrated = {
        "materiality": label_for_scores(materiality_score, confidence_score),
        "materiality_score": materiality_score,
        "confidence_score": confidence_score,
        "reason": value["reason"],
        "key_change": value["key_change"],
    }
    return validate_analysis(migrated, context=context), True


def validate_action(action, expected_ids):
    if not isinstance(action, dict) or set(action) != {"action", "classifications"}:
        raise ValueError("Response must contain exactly action and classifications.")
    if action["action"] != "classify_materiality" or not isinstance(action["classifications"], list):
        raise ValueError("Expected action:classify_materiality with a classifications list.")
    if len(action["classifications"]) != len(expected_ids):
        raise ValueError("Return exactly one classification for every supplied match_id.")
    results = []
    for index, (row, expected_id) in enumerate(zip(action["classifications"], expected_ids), 1):
        if not isinstance(row, dict) or set(row) != {"match_id", *ANALYSIS_FIELDS}:
            raise ValueError(f"classifications[{index}] has an invalid schema.")
        if row["match_id"] != expected_id:
            raise ValueError("Classifications must use every supplied match_id in the supplied order.")
        analysis = validate_analysis(
            {key: row[key] for key in ANALYSIS_FIELD_ORDER},
            context=f"classifications[{index}]",
        )
        results.append({"match_id": expected_id, **analysis})
    return results


def decision_view(row):
    """Return the row fields that determine whether saved materiality is still applicable."""
    return {key: value for key, value in row.items()
            if key not in {MATERIALITY_FIELD, "change_analysis"}}


def evidence_view(row):
    result = {
        "match_id": row["match_id"],
        "status": row["status"],
        "relationship": row["relationship"],
        "alignment_explanation": row["explanation"],
        "previous_disclosures": row.get("previous_disclosures", []),
        "current_disclosures": row.get("current_disclosures", []),
        "evidence": [
            {
                "disclosure_id": citation["disclosure_id"],
                "sentences": [
                    {"sentence_id": sentence["sentence_id"], "text": sentence["text"]}
                    for sentence in citation.get("sentences", [])
                ],
            }
            for citation in row.get("evidence", [])
        ],
    }
    if row["status"] == "unmatched":
        result["unmatched_type"] = row.get("unmatched_type")
    change_analysis = row.get("change_analysis")
    if isinstance(change_analysis, dict) and change_analysis.get("status") not in {None, "not_started"}:
        result["change_analysis_reference"] = change_analysis
    return result


def batches(rows, size):
    for index in range(0, len(rows), size):
        yield rows[index:index + size]


def load_document(path, *, validate_materiality=True):
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError("alignments.json must contain a JSON object.")
    if not document.get("run_complete"):
        raise ValueError("Materiality classification requires a completed alignment run.")
    if document.get("included_statuses") != ["ai_verified", "auto_matched", "unmatched"]:
        raise ValueError("alignments.json is not the canonical finalized alignment partition.")
    rows = document.get("alignments")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("alignments.json must contain an alignment-row list.")
    match_ids = [row.get("match_id") for row in rows]
    if any(not isinstance(match_id, str) or not match_id for match_id in match_ids) or len(set(match_ids)) != len(rows):
        raise ValueError("alignments.json must contain rows with unique match_id values.")
    migrated_count = 0
    for row in rows:
        required = {"match_id", "status", "relationship", "previous_ids", "current_ids", "explanation"}
        if not required <= set(row):
            raise ValueError("Every alignment row is missing one or more required fields.")
        if row["status"] not in {"ai_verified", "auto_matched", "unmatched"}:
            raise ValueError("alignments.json contains a non-final alignment row.")
        if validate_materiality and MATERIALITY_FIELD in row:
            row[MATERIALITY_FIELD], migrated = normalize_saved_analysis(
                row[MATERIALITY_FIELD], context=f"{row['match_id']}.{MATERIALITY_FIELD}"
            )
            migrated_count += migrated
    return document, migrated_count


def write_summary(
        output, rows, runtime=None, *, error=None, classification_run_id=None, run_state=None):
    labels = Counter(
        row[MATERIALITY_FIELD]["materiality"] for row in rows if MATERIALITY_FIELD in row
    )
    summary = {
        "graph_policy": GRAPH_POLICY,
        "alignment_rows": len(rows),
        "classified_rows": sum(labels.values()),
        "pending_rows": len(rows) - sum(labels.values()),
        "counts": dict(labels),
        "scoring_policy": {
            "materiality_threshold": MATERIALITY_THRESHOLD,
            "minimum_confidence": MIN_CONFIDENCE,
        },
        "complete": sum(labels.values()) == len(rows),
        "error": error,
        "timing": (
            dict(run_state["timing"])
            if run_state is not None else new_run_state(None)["timing"]
        ),
    }
    if classification_run_id is not None:
        summary["classification_run_id"] = classification_run_id
    if runtime is not None:
        summary["reported_tokens"] = runtime.report()["reported_tokens"]
    else:
        summary_path = output / "summary.json"
        if summary_path.exists():
            prior_summary = json.loads(summary_path.read_text())
            if (isinstance(prior_summary, dict)
                    and prior_summary.get("classification_run_id") == classification_run_id
                    and isinstance(prior_summary.get("reported_tokens"), dict)):
                summary["reported_tokens"] = prior_summary["reported_tokens"]
    write_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--previous-year", required=True)
    parser.add_argument("--current-year", required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--alignment-dir", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--max-requests", type=int, default=200)
    parser.add_argument("--max-new-requests", type=int)
    parser.add_argument("--max-total-tokens", type=int, default=750000)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--max-prompt-chars", type=int, default=150000)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--reclassify",
        action="store_true",
        help="Discard saved materiality and start a fresh model run; resume later without this flag.",
    )
    args = parser.parse_args(argv)

    ticker, years = args.ticker.lower(), (args.previous_year, args.current_year)
    if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", ticker)
            or any(not re.fullmatch(r"\d{4}", year) for year in years)
            or years[0] >= years[1]):
        parser.error("Use a valid ticker and increasing four-digit fiscal years.")
    limits = (args.batch_size, args.max_steps, args.max_requests, args.max_total_tokens,
              args.max_tokens, args.max_prompt_chars)
    if (any(value < 1 for value in limits)
            or args.max_new_requests is not None and args.max_new_requests < 1
            or not math.isfinite(args.timeout) or args.timeout <= 0):
        parser.error("Limits and timeout must be positive.")

    alignment_dir = args.alignment_dir or args.data_dir / "alignments" / ticker / f"{years[0]}-{years[1]}"
    path = alignment_dir / "alignments.json"
    output = alignment_dir / "materiality"
    runtime = None
    classification_run_id = None
    run_state = None
    document, error, exit_code = None, None, 0
    try:
        document, migrated_count = load_document(path, validate_materiality=not args.reclassify)
        identity = (document.get("company"), str(document.get("previous_year")), str(document.get("current_year")))
        if identity != (ticker, *years):
            raise ValueError("alignments.json belongs to a different company or year comparison.")
        rows = document["alignments"]

        changed = bool(migrated_count)
        run_state_path = output / "run_state.json"
        if args.reclassify:
            classification_run_id = uuid4().hex
            run_state = new_run_state(
                classification_run_id, started_at=datetime.now(timezone.utc).isoformat()
            )
            write_json(run_state_path, run_state)
            for row in rows:
                changed = row.pop(MATERIALITY_FIELD, None) is not None or changed
        else:
            prior_state_path = run_state_path
            if not prior_state_path.exists():
                prior_state_path = output / "manifest.json"
            if prior_state_path.exists():
                prior_state = json.loads(prior_state_path.read_text())
                run_state = normalize_run_state(prior_state)
                classification_run_id = run_state["classification_run_id"]
        for row in rows:
            if row["status"] == "auto_matched" and row.get("match_method") == "exact_text":
                exact = dict(EXACT_TEXT_ANALYSIS)
                if row.get(MATERIALITY_FIELD) != exact:
                    row[MATERIALITY_FIELD] = exact
                    changed = True
        if changed:
            write_json(path, document)

        pending = [row for row in rows if MATERIALITY_FIELD not in row]
        if run_state is None:
            if pending:
                classification_run_id = uuid4().hex
                run_state = new_run_state(
                    classification_run_id, started_at=datetime.now(timezone.utc).isoformat()
                )
            else:
                run_state = new_run_state(classification_run_id)
            write_json(run_state_path, run_state)
        if not pending:
            update_run_timing(run_state, complete=True)
            write_json(run_state_path, run_state)
            summary = write_summary(
                output,
                rows,
                classification_run_id=classification_run_id,
                run_state=run_state,
            )
            print(f"Materiality already complete for {len(rows)} alignment rows: {summary['counts']}.", flush=True)
            return 0

        config = load_config(args.env_file)
        implementation = [Path(__file__), Path(__file__).with_name("materiality_graph.py"),
                          Path(__file__).with_name("materiality_prompts.py"),
                          Path(__file__).with_name("materiality_runtime.py")]
        source_hash = digest(json.dumps(
            [decision_view(row) for row in rows], sort_keys=True, ensure_ascii=False
        ).encode())
        write_json(output / "manifest.json", {
            "schema_version": "2",
            "source_alignment_hash": source_hash,
            "company": ticker,
            "previous_year": years[0],
            "current_year": years[1],
            "graph_policy": GRAPH_POLICY,
            "classification_run_id": classification_run_id,
            "scoring_policy": {
                "materiality_threshold": MATERIALITY_THRESHOLD,
                "minimum_confidence": MIN_CONFIDENCE,
            },
            "base_url": config.base_url,
            "model": config.model,
            "settings": {key: getattr(args, key) for key in
                         ("batch_size", "max_steps", "max_tokens", "max_prompt_chars")},
            "implementation_hashes": {file.name: digest(file.read_bytes()) for file in implementation},
        })
        runtime = MaterialityRuntime(output, config, args, classification_run_id)

        for group in batches(pending, args.batch_size):
            payload = {
                "company": ticker,
                "previous_year": years[0],
                "current_year": years[1],
                "rows": [evidence_view(row) for row in group],
            }
            expected_ids = [row["match_id"] for row in group]
            identity_hash = digest(json.dumps({
                "payload": payload,
                "system": MATERIALITY_PROMPT,
                "max_steps": args.max_steps,
                "classification_run_id": classification_run_id,
            }, sort_keys=True, ensure_ascii=False).encode())[:16]
            job_id = f"materiality_{identity_hash}"
            graph = MaterialityAgentGraph(
                runtime,
                job_id,
                MATERIALITY_PROMPT,
                payload,
                lambda action, ids=expected_ids: validate_action(action, ids),
            )
            result = graph.run()
            by_id = {row["match_id"]: row for row in result}
            for row in group:
                classified = by_id[row["match_id"]]
                row[MATERIALITY_FIELD] = {
                    key: classified[key]
                    for key in ANALYSIS_FIELD_ORDER
                }
            write_json(output / "jobs" / f"{job_id}.json", {
                "job_id": job_id,
                "match_ids": expected_ids,
                "classifications": result,
            })
            write_json(path, document)
            update_run_timing(run_state, runtime)
            write_json(run_state_path, run_state)
            write_summary(
                output,
                rows,
                runtime,
                classification_run_id=classification_run_id,
                run_state=run_state,
            )

        update_run_timing(run_state, runtime, complete=True)
        write_json(run_state_path, run_state)
        summary = write_summary(
            output,
            rows,
            runtime,
            classification_run_id=classification_run_id,
            run_state=run_state,
        )
        print(
            f"Materiality complete for {summary['classified_rows']} alignment rows: {summary['counts']}; "
            f"reported tokens {runtime.report()['reported_tokens']['total_tokens']:,}.",
            flush=True,
        )
    except RunLimit as exc:
        error, exit_code = str(exc), 2
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError, LLMError) as exc:
        error, exit_code = str(exc), 1
    finally:
        if document is not None and error:
            update_run_timing(run_state, runtime)
            if run_state is not None:
                write_json(output / "run_state.json", run_state)
            write_summary(
                output,
                document["alignments"],
                runtime,
                error=error,
                classification_run_id=classification_run_id,
                run_state=run_state,
            )
    if error:
        print(f"Materiality stopped: {error}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
