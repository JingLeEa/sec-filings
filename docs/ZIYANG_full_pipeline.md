# Full pipeline with PostgreSQL inputs and storage

Run commands from the repository root using the environment containing
`requirements.txt`. Docker/PostgreSQL must be running. Configure
`SEC_USER_AGENT` and the sec-api.io `SEC_API_KEY` in the repository `.env`.
Disclosure extraction and later stages also require the SoCLaaS configuration
described in [shared LLM setup](ZIYANG_llm_setup.md).

## Run the complete pipeline

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD \
  --years 2023 2024 2025
```

AMD and fiscal years 2023, 2024 and 2025 are the defaults. Selected years are
sorted and deduplicated. Adjacent selections are compared: here, 2023–2024 and
2024–2025. Alignment and later stages require at least two years.

The runner executes these stages in order and saves each completed stage to
PostgreSQL. Every downstream stage uses `--input-source postgres`; it queries
the database directly for the previous stage's data. Local input JSON downloads
are not required and there is no automatic fallback to local files:

1. Preprocess narrative text and API/XBRL table metrics for every selected year.
2. Extract disclosures for every selected year.
3. Align disclosures for every adjacent year pair.
4. Run LLM change classification and lexical/semantic analysis, then save the
   updated alignment results.
5. Run the materiality agent and save the final alignment results again.

| Stage | Input source |
| --- | --- |
| Text and API/XBRL preprocessing | SEC/API downloads or their caches |
| Disclosure extraction | PostgreSQL preprocessing snapshot |
| Disclosure alignment | PostgreSQL raw evidence, disclosures and extraction audit metadata |
| LLM change classification | PostgreSQL finalized alignments and source evidence |
| Lexical/semantic analysis | PostgreSQL alignments containing the saved LLM labels, and source evidence |
| Materiality | PostgreSQL alignments containing change analysis |

Normalized result rows and their `pipeline_stage_snapshots` documents are saved
in the same database transaction. The snapshots retain exact source hashes,
completion status and excluded/unassigned evidence needed by downstream agents.
JSON reports and model checkpoints may still be produced as local outputs and
caches; they are not the required input handoff between stages. Standalone
downstream scripts also default to PostgreSQL. Use `--input-source files` only
when deliberately running the legacy file workflow.

For results saved before database input snapshots were introduced, import the
existing metadata once, without downloads, model calls or replacing normalized
records:

```bash
.venv/bin/python scripts/import_pipeline_snapshots.py --ticker AMD --years 2023 2024 2025
```

The importer verifies that local data matches normalized PostgreSQL records
before publishing any snapshot. New pipeline runs publish snapshots automatically.

## Stop after a chosen stage

Use `--until` to include a stage and its PostgreSQL saves, then stop. Every
selected year or comparison in that stage finishes before the runner exits.

| `--until` | Last completed work |
| --- | --- |
| `preprocessing` | Text and API/XBRL table preprocessing, saved to PostgreSQL |
| `extraction` | Disclosure extraction, saved to PostgreSQL |
| `alignment` | Disclosure alignment for all adjacent pairs, saved to PostgreSQL |
| `change-analysis` | Lexical, semantic and LLM results added to `change_analysis` and saved |
| `materiality` | Materiality classification and the final PostgreSQL save; this is the default |

For example, run preprocessing and disclosure extraction, save both stages, and
stop before alignment:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD \
  --years 2023 2024 2025 \
  --until extraction
```

To stop after alignment instead, use `--until alignment`. Preprocessing and
extraction can also run for a single year:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2024 --until extraction
```

`--until` sets the stopping point. Use `--from-stage` to skip earlier stages and
read their already-saved inputs from PostgreSQL. For example, run alignment only:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 \
  --from-stage alignment --until alignment
```

Or run materiality from the saved change-analysis results, with no local input
JSON and a separate output/checkpoint directory:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 \
  --from-stage materiality --data-dir /tmp/amd-materiality
```

Starting after preprocessing does not require SEC download credentials.
`--from-stage` must be at or before `--until`. To continue farther later, use a
later target or omit `--until` for all remaining stages.
Successful model responses and embeddings are reused from stage caches.
When PostgreSQL mode encounters an implementation-only change in an existing
alignment checkpoint, it archives job decisions and revalidates cached responses.
Changed evidence, model or run settings still require a separate output directory.

## Resume an interrupted run

The dashboard also starts automatic refreshes after a human saves a review. A
disclosure-type review updates that extraction result and reruns only comparisons
containing its filing year. An alignment change-label review keeps the saved group
and human label, then updates downstream analysis and materiality. These jobs use
a durable background queue with progress, failure/retry controls and source-bound
human decisions; see [manual review](ZIYANG_manual_review.md). Their workspaces are
separate from full-run checkpoints, and unchanged request/embedding results can be
reused. The automatic endpoint is materiality.

Resume is enabled by default. Rerun the same command, for example:

```bash
.venv/bin/python scripts/run_full_pipeline.py --ticker C --years 2023 2024 2025
```

The runner reads `data/pipeline_runs/c/2023-2024-2025/status.json` and skips
completed stages. If it stopped during materiality, it starts at the unfinished
materiality work and its PostgreSQL save. Each year pair is tracked separately:
if one pair already finished and saved, only the other pair resumes. An unfinished
stage uses its existing batch/response caches. Raising the materiality budget or
changing worker counts preserves completed stages.

The progress file is retained across invocations, including runs with a different
`--until` or `--from-stage`. For example, rerunning without `--until extraction`
continues at alignment after a successful extraction-only run. A fully completed
selection exits without executing stages. Keep the same `--data-dir` to reuse
this pipeline checkpoint; upstream data still comes from PostgreSQL.

Use `--dry-run` to see completed stages marked `[cached; skipped]` and the commands
that remain. To deliberately execute selected stages again, use `--no-resume`:

```bash
.venv/bin/python scripts/run_full_pipeline.py --ticker C --years 2023 2024 2025 \
  --from-stage materiality --no-resume
```

This retains internal response caches. It does not request a fresh model
classification. Forcing an upstream stage to execute again invalidates completion
markers for its dependent stages, including later stages outside `--until`.
Changing an output option such as `--embedding-model` also invalidates the affected
stage and its dependents. Use `--no-resume` when intentionally refreshing source
data or changing external model/database configuration. One runner at a time may
update a ticker/year-selection checkpoint.

## Preview, configuration and progress

Add `--dry-run` to print only the selected commands. It makes no API requests or
database writes:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 --until extraction --dry-run
```

The runner reads the repository `.env` for SEC/database settings. When
`--env-file` is omitted, exported SoCLaaS settings or the protected default
`~/.config/soclaas/soclaas.env` supply the LLM configuration. Pass `--env-file`
to select another configuration file. Exported variables take priority.
Preprocessing alone does not require SoCLaaS credentials.

Other options:

Disclosure extraction defaults to **3 concurrent year jobs × 6 batch workers per
year**, with a shared cap of **18 simultaneous API requests** for the three-year
run. All three years can start together. Boundary checks remain ordered within
each year, and the existing shared two-second spacing between request starts
still applies. Available independent work determines how many slots are active.

Year workers, batch workers and the API cap are separate controls. If the API cap
is omitted, it is calculated from active year workers × batch workers. For a
single selected year, the defaults therefore allow six simultaneous requests.

Alignment and LLM change analysis default to **2 concurrent year pairs × 9
workers per pair**, sharing **18 simultaneous API request slots** for the
three-year run. Both 2023–2024 and 2024–2025 can run together. The pairs share
request spacing and rate-limit cooldowns. Each pair saves its own results;
the next pipeline phase waits for all pairs. Within change analysis, each pair
finishes LLM classification, lexical/semantic enrichment and its database save
in that order. Matching and LLM change classification have no cumulative request
cap by default; the existing 30-million-token budget per pair still applies.

Materiality also defaults to **2 concurrent year pairs × 9 batch workers per
pair**, sharing **18 simultaneous API request slots**, two-second request-start
spacing and a 60-second cooldown after temporary HTTP 429 errors (extended by
`Retry-After`). Temporary 429s get up to ten additional attempts; permanent quota
failures stop the affected pair. Each pair finishes classification and verification,
then saves its materiality results to PostgreSQL. Completed work in the other pair
is allowed to finish and save if one pair stops. These settings allow concurrency;
the service may still throttle requests. Existing materiality request and token
budgets remain enforced per pair.

| Option | Purpose |
| --- | --- |
| `--data-dir PATH` | Change the root for generated outputs and caches |
| `--from-stage STAGE` | Skip earlier stages and read saved PostgreSQL inputs; default `preprocessing` |
| `--resume` / `--no-resume` | Skip completed stages by default, or deliberately execute the selected stages using their internal caches |
| `--user-agent "Name email@example.com"` | Override the SEC contact |
| `--workers N` / `--year-workers N` | Concurrent disclosure-extraction years; default `3` |
| `--batch-workers N` | Extraction/consolidation batch workers per year; default `6` |
| `--max-concurrent-requests N` | Shared extraction API cap; default active year workers × batch workers (`18` for the default three-year run) |
| `--alignment-workers N` | Alignment/change-analysis jobs per pair; default `9` |
| `--alignment-pair-workers N` | Concurrent adjacent-year pairs; default `2` |
| `--alignment-max-concurrent-requests N` | Shared alignment/LLM change-analysis API cap; default active pairs × workers per pair (`18` for two pairs × nine workers) |
| `--materiality-concurrency N` / `--materiality-workers N` | Materiality batch workers per pair; default `9` |
| `--materiality-pair-workers N` | Concurrent materiality year pairs; default `2` |
| `--materiality-max-concurrent-requests N` | Shared materiality API cap; default active pairs × workers per pair (`18` for two pairs × nine workers) |
| `--embedding-model MODEL` | Select the semantic embedding model; default `bge-m3` |

A failure or request-budget pause stops subsequent stages. Rerun the same
command to skip completed stages and resume unfinished work. Progress is recorded in
`data/pipeline_runs/amd/2023-2024-2025/status.json` for the example above.
`until` records the requested endpoint; `complete` means that selected run
finished, and `full_pipeline_complete` is true only when materiality also finished.
`from_stage` records the starting point and `input_source` is `postgres`.
`skipped_stages` lists stages reused by the latest invocation; `last_invocation_at`
records that invocation's start. `started_at` retains the original run start.
`extraction_concurrency` records the year workers, batch workers per year, and
shared request cap used by that invocation. `alignment_concurrency` records pair
workers, workers per pair, and the shared alignment API cap. A running process keeps the settings
it started with; restart the command to use changed defaults and reuse saved work.
`materiality_concurrency` records the corresponding pair workers, workers per pair,
and shared materiality API cap.

## Output and database fields

- Narrative files: `data/raw/amd/<year>/`. The raw loader uses `--sync`, which
  removes obsolete raw IDs while preserving filing rows and downstream disclosures.
- Table files: `data/table_output/amd/`. Yearly API/XBRL exports are stored in
  PostgreSQL. The merged JSON remains a derived local file. Table metrics are
  separate from the narrative inputs consumed by the agents.
- Disclosure files: `data/disclosures/amd/<year>/`.
- Alignment files: `data/alignments/amd/<previous>-<current>/`.
- Change analysis: the alignment row's `change_analysis` object contains
  `lexical`, `semantic`, `llm`, and `final_taxonomy`, stored in the existing
  PostgreSQL `change_analysis` JSONB column. Semantic analysis uses cached
  embeddings. The final taxonomy uses the validated LLM label, retaining
  independent lexical and semantic results for inspection.
- Materiality: `materiality_analysis` is added to finalized alignment rows and
  stored in PostgreSQL at `alignments.extra.materiality_analysis`.

Rows requiring alignment review remain in `needs_review.json` and are deferred
from the change-analysis and materiality stages.
