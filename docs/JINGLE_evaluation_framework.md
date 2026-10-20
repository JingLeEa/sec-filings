# JINGLE: Alignment evaluation framework

## Purpose

This framework compares the pipeline's Qwen change-analysis and materiality
predictions against a separately generated model reference. The comparison uses
the same finalized disclosure alignments on both sides, so differences concern
classification rather than which disclosures were paired. A model reference is
not human-verified ground truth; report precision, recall and F1 as agreement
*relative to that reference*.

The default reference runner is SoCLaaS (`x-test-2`). Two optional OpenAI
runners use the standard API or Batch API. Keep a separate prepared alignment
root for each provider/model/run. Never run two providers against the same
`alignments.json`: each runner writes its classifications in place.

| Script | Role | Default alignment root |
| --- | --- | --- |
| `scripts/run_alignment_evaluation.py` | Prepare, classify and evaluate with `--llm` | `data/alignments_for_eval_<llm>` |
| `scripts/run_alignment_eval_soclaas.py` | SoCLaaS classification runner | `data/alignments_for_eval_soclaas` |
| `scripts/run_alignment_eval_openai_batch.py` | Submit, collect and resume OpenAI Batch jobs | `data/alignments_for_eval_openai_batch` |
| `scripts/run_alignment_eval_openai_standard.py` | Concurrent standard OpenAI requests | `data/alignments_for_eval_openai_standard` |
| `scripts/alignment_eval_shared.py` | Shared source loading, prompts, request planning and result validation | n/a |

## One-command workflow

`run_alignment_evaluation.py` runs preparation, the selected model runner, and
comparison in that order for one company/year-pair. The default is SoCLaaS
`x-test-2`. It reads the finalized Qwen alignments from `data/alignments` and
disclosures from `data/disclosures`. It does not modify the source alignments.
Use `--input-root` or `--disclosures-dir` only when those files live elsewhere.

```bash
.venv/bin/python scripts/run_alignment_evaluation.py \
  --ticker amd --previous-year 2023 --current-year 2024 \
  --env-file .env
```

The default directories are `data/alignments_for_eval_soclaas` for the
classified copy and `data/evaluation_soclaas` for the reports. Other providers
use the same suffix pattern: `openai_standard` or `openai_batch`. The original
Qwen alignment file remains the comparison candidate.

Select OpenAI standard requests and a model explicitly. For a non-default
OpenAI model, supply its input/output prices in USD per million tokens so the
runner can enforce its local spend estimate.

```bash
.venv/bin/python scripts/run_alignment_evaluation.py \
  --ticker amd --previous-year 2023 --current-year 2024 \
  --llm openai_standard --model YOUR_MODEL_ID \
  --input-usd-per-million INPUT_PRICE \
  --output-usd-per-million OUTPUT_PRICE \
  --workers 2 --env-file .env
```

For OpenAI Batch, submit now and rerun the same command later to collect and
evaluate. Omit `--submit-only` on the collection run. The wrapper does not
evaluate while a Batch job is active or any classification rows remain pending.

```bash
.venv/bin/python scripts/run_alignment_evaluation.py \
  --ticker amd --previous-year 2023 --current-year 2024 \
  --llm openai_batch --submit-only --env-file .env
```

Rerunning the workflow reuses a prepared copy and the classifier's saved
responses; it does not strip completed predictions again. If the source
alignment content has changed, it stops rather than mixing old predictions
with new evidence. Use a new `--alignment-root` and `--evaluation-root` for a
new source version or another model under the same provider. `--model` is
forwarded to the selected runner, and `--reference-label` can set a different
display name in the comparison report. `--max-new-rows` is useful for a pilot:
the wrapper defers comparison until all eligible rows are classified. SoCLaaS
and standard OpenAI retry invalid responses up to three additional times per
request by default (`--max-invalid-retries 3`). All attempts are recorded and
count toward token usage; API/network retries are separate. A row still pending
after those attempts can be retried by rerunning the command. OpenAI Batch
does not have immediate validation retries: collect its results, then rerun
to submit only rows that remain pending.

The provider-specific scripts below remain available for preparing multiple
companies at once, inspecting pending requests, or running the stages
separately.

## Prepare isolated inputs

`prepare_alignments_for_eval.py` copies a completed `alignments.json` and strips
previous change/materiality predictions, including nested copies. It leaves
alignment membership and evidence intact. Its default output root is the
SoCLaaS root. For example, prepare both AVGO year-pairs from the existing
alignment reports:

```bash
.venv/bin/python scripts/prepare_alignments_for_eval.py \
  --ticker avgo --input-dir data/alignments
```

Prepare separate OpenAI copies when comparing providers:

```bash
.venv/bin/python scripts/prepare_alignments_for_eval.py \
  --ticker avgo --input-dir data/alignments \
  --output-root data/alignments_for_eval_openai_batch

.venv/bin/python scripts/prepare_alignments_for_eval.py \
  --ticker avgo --input-dir data/alignments \
  --output-root data/alignments_for_eval_openai_standard
```

The preparation script will not replace a different existing output without
`--overwrite`. Do not overwrite a partially classified run to resume it;
rerun its evaluation command instead. A new model, prompt or source-evidence
version needs a new prepared directory.

## Run classification

Run from the repository root. The runners load source disclosures from
`data/disclosures` by default; use `--disclosures-dir` if they are elsewhere.
They use original disclosure content for change analysis and selected alignment
evidence for materiality. Completed exact-text rows receive deterministic
`Unchanged`/`No` labels; one-sided rows receive deterministic `New`/`Removed`
change labels, but their materiality is still assessed by the model.

For the default SoCLaaS workflow, set `SOCLAAS_BASE_URL`, `SOCLAAS_API_KEY` and
`SOCLAAS_MODEL` in `.env` (the CLI model defaults to `x-test-2`):

```bash
.venv/bin/python scripts/run_alignment_eval_soclaas.py \
  --ticker avgo --previous-year 2023 --current-year 2024 \
  --env-file .env --workers 2
```

SoCLaaS defaults to four alignment rows per request, one worker, a 180-second
per-attempt timeout, two retries for retryable API errors, and three additional
attempts for invalid responses. `--workers 2`
allows two requests in flight; `--request-interval` paces their starts. With
only one pending request, extra workers have no effect. `--dry-run` prints the
pending row/request counts without changing files. Completed responses are
archived under `soclaas_eval/responses/`; rerunning skips saved rows. A
graceful Ctrl-C waits for in-flight calls to finish and save.

For OpenAI, set `OPENAI_API_KEY` in `.env`. Both OpenAI runners default to
`gpt-6.1-sol`; `OPENAI_MODEL` or `--model` can override it. For a different
model, provide the relevant input/output token prices required by the runner.

```bash
.venv/bin/python scripts/run_alignment_eval_openai_batch.py \
  --ticker avgo --previous-year 2023 --current-year 2024 \
  --env-file .env --submit-only

.venv/bin/python scripts/run_alignment_eval_openai_standard.py \
  --ticker avgo --previous-year 2023 --current-year 2024 \
  --env-file .env --workers 2
```

Rerun the Batch command without `--submit-only` to collect a finished job.
If some requests failed, the collector saves successes and writes
`openai_eval/batch_NNNN_errors.json`; rerun again to submit only rows still
pending. Standard API results are archived under
`openai_eval/standard_responses/` and also resume by skipping saved rows.
`--max-spend-usd` is a local estimate guard, not an account billing limit.
Avoid starting standard and Batch processes against the same year-pair at
once.

All three runners call `alignment_eval_shared.py` for the same source-evidence
construction, production prompt text, deterministic cases and output
validation. The transport differs: OpenAI uses Responses JSON schema, while
SoCLaaS uses Chat Completions JSON mode. Each model produces its own results;
no reference label is included in the prompts.

## Output and comparison

The classified copy retains the original row IDs, evidence and statuses. It
fills `change_analysis.llm` and `materiality_analysis` with `materiality`,
`materiality_score`, `confidence_score` and `reason`. The model's `key_change`
is validated but not stored in this compact evaluation copy. Runtime state,
errors and provider-reported token usage stay beside the alignment file under
`soclaas_eval/` or `openai_eval/`.

`evaluate_alignment_predictions.py` compares the reference file to the Qwen
candidate file by `match_id` and rejects changed alignment membership or
evidence. By default it reads the SoCLaaS root, compares against
`data/alignments`, and writes `data/evaluation_soclaas`. The default report
label is `DeepSeek`; pass `--model` to name another reference accurately.

```bash
.venv/bin/python scripts/evaluate_alignment_predictions.py \
  --ticker avgo --year-pair 2023-2024 --model DeepSeek
```

For an OpenAI or manually annotated reference, supply its root, a distinct
output root and the correct model label:

```bash
.venv/bin/python scripts/evaluate_alignment_predictions.py \
  --ticker avgo --year-pair 2023-2024 --model ChatGPT \
  --reference-root data/alignments_for_eval_openai_batch \
  --output-root data/evaluation_openai_batch
```

Each comparison writes `agreement.json` (full counts, confusion matrices and
disagreements), `metrics.json` (compact ranking metrics) and `report.txt`
(human-readable tables). The change section reports exact taxonomy agreement
for LLM, lexical and semantic methods, plus binary substantive-change results.
The binary mapping is `Modified`/`Expanded`/`Reduced` to substantive and
`Reworded`/`Unchanged` to non-substantive; `New`/`Removed` do not enter matched
row binary scoring. Materiality reports per-label precision, recall and F1 for
`Yes`, `No` and `Uncertain`. The primary materiality score is
`yes_no_macro_f1_vs_reference`, the average F1 of reference-supported `Yes`
and `No` classes. The original three-class `macro_f1_vs_reference` is retained
and shown separately. An `Uncertain` row has no separate weight in the primary
average, but a `Yes`/`No` prediction on that row can still affect precision.
Rows missing either prediction are excluded from compared-row metrics, so read
the coverage counts alongside F1.

For case-by-case review, run `export_materiality_disagreements.py` or
`export_change_analysis_disagreements.py` with the same `--ticker`,
`--year-pair`, `--model`, reference/candidate roots and output root. They add
Markdown reports with the relevant source disclosures. Their defaults also
point to the SoCLaaS reference and `data/evaluation_soclaas`.

## Interpretation limits

Agreement with a stronger model is a useful diagnostic, not proof that either
model is correct. Inspect low-coverage groups, class imbalance, uncertain
cases and disagreement examples before ranking companies. A reference produced
in a chat interface must be saved as the actual annotated alignment JSON;
a benchmark manifest containing only totals cannot be evaluated. Keep the
original prepared input or manifest separately for provenance.
