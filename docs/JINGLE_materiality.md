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

The prompt applies an explicit substantive-change rubric across business,
strategy, risk, operations, third-party dependencies, regulation, technology,
finance, and human capital. Pure wording, formatting, reordering, terminology,
boilerplate, and non-substantive routine updates are generally non-material.
Categories, introduced or removed text, and numerical updates are never automatic
labels; the decision must consider magnitude and company-specific consequences.

## 2. Code and workflow

| Code | Responsibility |
| --- | --- |
| [`scripts/classify_materiality.py`](../scripts/classify_materiality.py) | CLI entry point |
| [`agents/materiality.py`](../src/sec_disclosure/agents/materiality.py) | Input validation, batching, deterministic exact matches and in-place output updates |
| [`agents/materiality_graph.py`](../src/sec_disclosure/agents/materiality_graph.py) | Shared LangGraph model/validation loop for classifier and verifier jobs |
| [`agents/materiality_runtime.py`](../src/sec_disclosure/agents/materiality_runtime.py) | API request cache, token limits and usage ledger |
| [`agents/materiality_prompts.py`](../src/sec_disclosure/agents/materiality_prompts.py) | Evidence and output contract for the materiality agent |

Exact-text `auto_matched` rows are assigned `No` locally with materiality strength
`0.0` and confidence `1.0` and consume no API tokens. Other finalized rows are
sent in bounded batches to the materiality agent. Python validates exact IDs, row
coverage, labels, scores, reasons and key changes. Invalid responses receive
bounded correction turns. If a multi-row job exhausts those turns, the workflow
automatically divides it into smaller jobs until each group succeeds or a
single-row job proves unrecoverable.

After all first-pass classifications are saved, the same CLI automatically routes
rows with `confidence_score < 0.60` to an independent materiality verifier. The
verifier receives the alignment evidence and tentative analysis, may confirm or
replace it, and must obey the same score-to-label rules. A verifier may retain
`Uncertain` when the evidence remains inadequate; the durable verification ledger
prevents that row from looping on later resumes. Use `--verification-threshold`
to change the routing threshold, `--verification-batch-size` to change verifier
batching, or `--skip-verification` for a classifier-only diagnostic run.

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

Batch-size recovery is automatic. A validation-exhausted classifier or verifier
batch is divided in half recursively, for example `8 -> 4 -> 2 -> 1`. Each smaller
payload has a new job identity, so it does not return to the exhausted checkpoint.
Successful split groups are saved immediately. The CLI stops only if a one-row job
still cannot produce a valid result or another run limit or API error occurs.

API-bound batches can run concurrently within the classifier and verifier stages.
The default `--concurrency 2` runs two batches in parallel; `--concurrency 1`
restores sequential behavior. Request admission and token reservations are atomic,
and completed workers enter a short synchronized commit section before updating
shared JSON artifacts. Classification still finishes before verification begins.

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
| `verifications.json` | Initial and final analyses for rows routed to the verifier |
| `requests/` | Durable request/response ledger with per-request completion time and duration; API keys are not stored |
| `graph/checkpoints.sqlite` or `graph/<job_id>.sqlite` | Per-batch LangGraph checkpoints; parallel mode isolates active jobs in separate databases |
| `traces/` and `jobs/` | Validation history and completed classifications |
| `token_usage.json` | Provider-reported materiality token usage |
| `summary.json` | Classification counts, progress, token usage and run timing |

`summary.json` reports `started_at`, `completed_at`, total wall-clock seconds,
cumulative API seconds, and the number of timed API requests. Wall-clock time
includes pauses between invocations; cumulative API time sums only model request
durations. Timing fields are `null` for runs completed before timing was added
because their exact duration cannot be reconstructed reliably.

Default limits are 8 rows per batch, 9 concurrent batches, 3 model turns per batch,
200 cumulative requests, 1,500,000 total budgeted tokens per year pair, 4,000 completion tokens,
150,000 prompt characters, a 180-second initial timeout and a 300-second
single-row retry timeout. Timeout recovery is enabled by default: a timed-out
multi-row request is divided immediately, while a timed-out single-row request is
retried once at the longer limit. Every timed-out attempt's unknown usage is
conservatively reserved against the token budget. `--retry-failed` explicitly
permits another paid attempt after automatic recovery is exhausted or another
request fails.

## 5. Troubleshooting and tuning

Completed batches are durable. Unless a completely fresh classification is
intended, rerun without `--reclassify`; the workflow skips completed rows and
continues the classifier or verifier stage that still has pending work.

| Symptom | Recommended change | Why |
| --- | --- | --- |
| A multi-row classifier or verifier job exhausts validation | No manual batch change is required | The workflow automatically divides the failed group and retries smaller jobs |
| A one-row job exhausts validation | Inspect its trace and evidence; increase `--max-steps` only if responses are improving | A singleton cannot be divided further and may contain malformed or unusually difficult evidence |
| A response is truncated or has `finish_reason` other than `stop` | Allow automatic splitting first; increase `--max-tokens` only if a one-row response still needs more output space | Smaller payloads reduce both prompt and response size without broadly increasing token allowance |
| `Invocation request limit reached` | Rerun normally without `--max-new-requests`, or set it to a larger per-invocation pilot value | This is an intentional pause, not an API failure |
| `Total materiality request limit reached` | Raise `--max-requests` after checking `token_usage.json` | The active run has reached its cumulative request cap |
| `Materiality token budget cannot accommodate the next request` | Raise `--max-total-tokens` deliberately, or reduce batch size if prompt size is the cause | Admission control stops before making an unaffordable request |
| `context exceeds max_prompt_chars` | Reduce batch size; raise `--max-prompt-chars` only if the provider supports the larger context | The limit protects against oversized prompts |
| A multi-row request times out | No manual change is normally required | The workflow reserves its estimated maximum usage and immediately reroutes the rows through smaller batches |
| A one-row request times out once | No manual change is normally required | A singleton cannot be divided, so it is retried once with `--retry-timeout` (300 seconds by default) |
| A one-row request times out at both limits | Resume with `--retry-failed --timeout 300`, or choose a larger explicit timeout | Automatic recovery is exhausted; explicit retry acknowledges both prior attempts' possible unreported usage |
| A non-timeout API request is recorded as failed or interrupted with unknown usage | Inspect `requests/`, then use `--retry-failed` to authorize another paid attempt | Connection, quota and service errors are not improved by splitting the payload |
| Too many or too few rows are routed to the verifier | Adjust `--verification-threshold`; `0.60` matches `Uncertain`, while `0.80` also reviews lower-confidence `Yes` and `No` results | Routing uses strict `confidence_score < threshold` |

For example, if an eight-row classifier job repeatedly omits one `match_id`, the
same invocation now continues with output similar to:

```text
Materiality classifier batch of 8 rows exhausted validation; retrying automatically as 4 and 4 rows.
```

A multi-row timeout produces an adaptive splitting message:

```text
Materiality classifier batch of 8 rows timed out; rerouting automatically as 4 and 4 rows.
```

A single-row timeout instead produces an explicit escalation message:

```text
materiality_...: request timed out after 180s; retrying once with 300s.
```

Timed-out attempts are retained in `requests/`; none is treated as free. Use
`--no-timeout-recovery` to restore stop-on-first-timeout behavior for diagnostics
or strict manual cost control.

For a faster production run using the conservative concurrency setting:

```bash
.venv/bin/python scripts/classify_materiality.py \
  --ticker nvda \
  --previous-year 2024 \
  --current-year 2025 \
  --env-file .env \
  --batch-size 8 \
  --verification-batch-size 4 \
  --concurrency 2 \
  --timeout 180 \
  --retry-timeout 300
```

Start above concurrency `2` only after confirming that the provider's rate limits
and the configured token budget can accommodate the additional in-flight calls.

Do not add `--reclassify`: that would discard every completed materiality result.
Do not add `--retry-failed` when a request completed and only its content failed
validation. The adaptive jobs are new payloads and do not represent retries of a
failed API call.

`--max-steps` controls correction turns per job. Increase it only when traces show
that responses are progressively approaching a valid result. Automatic splitting
handles repeated omissions. `--skip-verification` is for classifier-only
diagnostics; normal production runs should leave verification enabled.

## 6. Safety and downstream behavior

The input must be a completed canonical `alignments.json` partition containing
only `ai_verified`, `auto_matched`, and `unmatched` rows. The CLI rejects partial
alignment runs, duplicate IDs, malformed saved materiality and comparison
metadata that does not match the command.

The materiality stage never writes `needs_review.json`. A later alignment rerun
preserves `materiality_analysis` only when the underlying alignment decision,
evidence and metadata are unchanged. Changed alignment rows do not inherit stale
materiality from the prior decision and must be classified again.
