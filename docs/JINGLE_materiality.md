# JINGLE: Disclosure materiality

**Maintainer:** JINGLE — contact JINGLE for materiality-stage questions.

## 1. Purpose and scope

The materiality stage classifies every finalized row in `alignments.json` by
answering:

> Does this disclosure change represent a meaningful change in the company's
> business, risk, financial position, exposure, operations, or strategic priorities?

The unit of classification is an alignment row, including supported one-to-many,
many-to-one, many-to-many, introduced-disclosure and removed-disclosure rows.
`needs_review.json` is outside the first version's scope and is never modified.

The stage preserves the alignment row's top-level `status`. For `status: unmatched`
only, `current_only` means a newly introduced disclosure and `previous_only` means
a removed disclosure. Introduction or removal is evidence of change, but does not
automatically make the change material.

## 2. Code and workflow

| Code | Responsibility |
| --- | --- |
| [`scripts/classify_materiality.py`](../scripts/classify_materiality.py) | CLI entry point |
| [`agents/materiality.py`](../src/sec_disclosure/agents/materiality.py) | Input validation, batching, deterministic exact matches and in-place output updates |
| [`agents/materiality_graph.py`](../src/sec_disclosure/agents/materiality_graph.py) | LangGraph model/validation loop and SQLite checkpoints |
| [`agents/materiality_runtime.py`](../src/sec_disclosure/agents/materiality_runtime.py) | API request cache, token limits and usage ledger |
| [`agents/materiality_prompts.py`](../src/sec_disclosure/agents/materiality_prompts.py) | Evidence and output contract for the materiality agent |

Exact-text `auto_matched` rows are assigned `No` locally with materiality strength
`0.0` and confidence `1.0` and consume no API tokens. Other finalized rows are
sent in bounded batches to the materiality agent. Python validates exact IDs, row
coverage, labels, scores, reasons and key changes. Invalid responses receive
bounded correction turns.

The model receives selected source sentences, alignment explanations and
disclosure metadata. A completed `change_analysis` is included when available,
but the prompt identifies it as optional reference material rather than evidence.
Materiality therefore does not depend on pipeline execution order.

Classification is alignment-row level, not sentence level. A one-to-many,
many-to-one, or many-to-many alignment can therefore contain evidence sentences
from several disclosures, and the model assesses their combined change. The stage
does not load each extraction record's complete `content` field; it uses the
sentences selected as alignment evidence plus disclosure metadata such as the
summary. The current `change_analysis` objects with `status: not_started` are
placeholders, not completed chunk-level analysis.

## 3. Output contract

The stage modifies `alignments.json` in place by adding `materiality_analysis` at
the same row level as `change_analysis`:

```json
{
  "status": "ai_verified",
  "change_analysis": {
    "status": "not_started",
    "lexical": null,
    "semantic": null,
    "llm": null,
    "final_taxonomy": null
  },
  "materiality_analysis": {
    "materiality": "Yes",
    "materiality_score": 0.82,
    "confidence_score": 0.86,
    "reason": "The disclosure expands the company's exposure to external parties by adding customers and extending dependency from application development to ongoing enhancement and maintenance.",
    "key_change": "Broader third-party dependency"
  }
}
```

`materiality_score` measures the strength or likelihood of materiality from `0.0`
(clearly not meaningful) to `1.0` (clearly meaningful). `confidence_score` measures
how confidently the evidence supports that assessment. The label is derived from
the scores: confidence below `0.60` is `Uncertain`; otherwise materiality strength
of at least `0.50` is `Yes`, and lower strength is `No`. Both scores remain numeric
for `Uncertain`. `reason` is an evidence-grounded explanation and `key_change` is a
concise noun phrase.

Saved results from the former confidence-only schema are migrated when the file is
next loaded. An old `Yes` score becomes both materiality strength and confidence;
an old `No` score becomes confidence while its materiality strength is `1 - score`.
An old `Uncertain` becomes strength `0.5` and confidence `0.0`. This conversion
keeps completed runs resumable without spending tokens on reclassification.

No materiality-specific status replaces the existing alignment `status`. This
keeps existing status-based database logic stable; a database schema that rejects
unknown row properties must still be migrated to accept the new nested field.
The alignment report remains schema version `8`, matching the existing downstream
analysis-extension pattern used by `change_analysis`.

## 4. Run and resume

Complete disclosure alignment first, then run from the repository root:

```bash
.venv/bin/python scripts/classify_materiality.py \
  --ticker amd \
  --previous-year 2023 \
  --current-year 2024 \
  --env-file .env
```

The default input is
`data/alignments/TICKER/PREVIOUS_YEAR-CURRENT_YEAR/alignments.json`.
Use `--alignment-dir` to select an existing comparison directory directly.

Completed batches are written atomically to `alignments.json`. Repeat the same
command after a budget stop or interruption; rows with valid saved materiality
are skipped, completed API responses are cached, and an interrupted graph resumes
from `materiality/graph/checkpoints.sqlite`. Use `--max-new-requests 1` for a
one-request pilot, then remove it to continue.

Use `--reclassify` to discard all saved `materiality_analysis` values and start a
genuinely fresh model run. The stage assigns the run a new identifier so prior API
responses and graph checkpoints cannot be replayed as new classifications:

```bash
.venv/bin/python scripts/classify_materiality.py \
  --ticker nvda \
  --previous-year 2024 \
  --current-year 2025 \
  --env-file .env \
  --reclassify \
  --max-new-requests 1
```

If a reclassification pauses, resume with the normal command without
`--reclassify`; repeating the flag intentionally starts another fresh run and
discards the partial results. Exact-text rows are still classified locally.

Runtime artifacts are stored under the comparison's `materiality/` directory:

| Path | Contents |
| --- | --- |
| `manifest.json` | Source-row fingerprint, model, settings and implementation hashes |
| `run_state.json` | Active reclassification run identifier used for cache isolation and resume |
| `requests/` | Durable request/response ledger with per-request completion time and duration; API keys are not stored |
| `graph/checkpoints.sqlite` | Per-batch LangGraph checkpoints |
| `traces/` and `jobs/` | Validation history and completed classifications |
| `token_usage.json` | Provider-reported materiality token usage |
| `summary.json` | Classification counts, progress, token usage and run timing |

`summary.json` reports `started_at`, `completed_at`, total wall-clock seconds,
cumulative API seconds, and the number of timed API requests. Wall-clock time
includes pauses between invocations; cumulative API time sums only model request
durations. Timing fields are `null` for runs completed before timing was added
because their exact duration cannot be reconstructed reliably.

Default limits are 8 rows per batch, 3 model turns per batch, 200 cumulative
requests, 750,000 total budgeted tokens, 4,000 completion tokens, 150,000 prompt
characters and 180 seconds per request. `--retry-failed` explicitly permits a
new paid attempt after a failed or interrupted request with unknown usage.

## 5. Safety and downstream behavior

The input must be a completed canonical `alignments.json` partition containing
only `ai_verified`, `auto_matched`, and `unmatched` rows. The CLI rejects partial
alignment runs, duplicate IDs, malformed saved materiality and comparison
metadata that does not match the command.

The materiality stage never writes `needs_review.json`. A later alignment rerun
preserves `materiality_analysis` only when the underlying alignment decision,
evidence and metadata are unchanged. Changed alignment rows do not inherit stale
materiality from the prior decision and must be classified again.
