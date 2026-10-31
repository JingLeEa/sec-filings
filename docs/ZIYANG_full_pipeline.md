# Full pipeline with PostgreSQL inputs and storage

Run commands from the repository root using the environment containing
`requirements.txt`. Docker/PostgreSQL must be running. Configure
`SEC_USER_AGENT` and the sec-api.io `SEC_API_KEY` in the repository `.env`.
Disclosure extraction and later stages also require the SoCLaaS configuration
described in [shared LLM setup](ZIYANG_llm_setup.md).

## Run the complete pipeline

Cache, progress and output artifacts are stored in PostgreSQL by default. The
pipeline creates no local result files, request caches or SQLite checkpoint
files. `pipeline_artifacts` stores these artifacts alongside the normalized
stage tables; graph execution uses memory and commits checkpoints to PostgreSQL.

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD \
  --years 2023 2024 2025
```

To also export result files locally, opt in explicitly:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 \
  --save-local-output --data-dir ./exports
```

The export contains reports and extracted data. Requests, jobs, manifests,
progress markers, download caches and graph checkpoints remain in PostgreSQL.
Changing the export directory does not change the cache or restart paid work.

AMD and fiscal years 2023, 2024 and 2025 are the defaults. Selected years are
sorted and deduplicated. Adjacent selections are compared: here, 2023–2024 and
2024–2025. Alignment and later stages require at least two years.

The runner executes these stages in order and saves each completed stage to
PostgreSQL. Every downstream stage uses `--input-source postgres`; it queries
the database directly for the previous stage's data. Local input JSON downloads
are not required and there is no automatic fallback to local files:

1. Preprocess narrative text and API/XBRL table metrics for every selected year,
   then load the verified XBRL exports into PostgreSQL.
2. Extract disclosures for every selected year.
3. Align disclosures for every adjacent year pair.
4. Run LLM change classification and save the updated alignment results.
5. Run the materiality agent and save the final alignment results again.
6. Synthesize the material changes and XBRL financial facts, then save the
   validated report and token-usage record to PostgreSQL `synthesis_reports`.

Synthesis is the default endpoint. Peer comparison remains a standalone command
and is not part of this pipeline.
Change analysis uses the LLM classification path only; the pipeline does not run
lexical analysis, semantic analysis or embedding requests.

| Stage | Input source |
| --- | --- |
| Text and API/XBRL preprocessing | SEC/API downloads or their caches |
| Disclosure extraction | PostgreSQL preprocessing snapshot |
| Disclosure alignment | PostgreSQL raw evidence, disclosures and extraction audit metadata |
| LLM change classification | PostgreSQL finalized alignments and source evidence |
| Materiality | PostgreSQL alignments containing change analysis |
| XBRL facts | Loaded from each verified yearly API/XBRL export into `xbrl_value_context` |
| Synthesis | PostgreSQL materiality assessments, source evidence and `xbrl_value_context` |

Normalized result rows and their `pipeline_stage_snapshots` documents are saved
in the same database transaction. The snapshots retain exact source hashes,
completion status and excluded/unassigned evidence needed by downstream agents.
JSON reports, model requests and checkpoints are stored as PostgreSQL artifacts.
Local copies are produced only with `--save-local-output`. Standalone downstream
scripts also default to PostgreSQL inputs; their legacy file workflows are
separate from the two pipeline commands.

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
| `change-analysis` | LLM change labels added to `change_analysis` and saved |
| `materiality` | Materiality classification and its PostgreSQL save |
| `synthesis` | Validated synthesis report and token usage saved to `synthesis_reports`; this is the default |

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

Or run materiality from the saved change-analysis results and optionally export
its result files to another local directory:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 \
  --from-stage materiality --until materiality \
  --save-local-output --data-dir /tmp/amd-materiality
```

To run synthesis from materiality and XBRL data already saved in PostgreSQL:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker AMD --years 2023 2024 2025 --from-stage synthesis
```

Starting after preprocessing does not require SEC download credentials.
`--from-stage` must be at or before `--until`. To continue farther later, use a
later target or omit `--until` for all remaining stages.

After all materiality results have been saved, the default full run executes the
packaged synthesis agent. It reads material changes and loaded XBRL facts from
PostgreSQL and stores the validated report, response cache and token usage in
the database. Batch findings are followed by
one final synthesis request for the executive summary and whole-period analyses.
Finding batches run concurrently (18 workers by default, configurable with
`--synthesis-finding-workers` and capped at the SoCLaaS limit of 20). The synthesis
workload remains capped at 50 changes by default; raise it with
`--synthesis-max-changes` to expose more batches to the worker pool. For example,
180 changes with a batch size of 10 and 18 workers schedules nine batches per
adjacent-year pair for two pairs, or six per pair for three pairs, while keeping
the total findings concurrency below the provider limit. The completed report
is also upserted into PostgreSQL `synthesis_reports`, keyed by company and years;
the JSON is also available for explicit local export.
Use `--until materiality` to stop after materiality has been saved. The legacy
`--skip-synthesis` option also limits the endpoint to materiality.
The pipeline forwards `--env-file` to synthesis, which uses the shared SoCLaaS
configuration and records provider-reported token usage.
Successful model responses are reused from stage caches.
When PostgreSQL mode encounters an implementation-only change in an existing
alignment checkpoint, it archives job decisions and revalidates cached responses.
Changed evidence, model or run settings still invalidate the affected cached work.
For an intentional fresh configuration, select a different PostgreSQL
`--cache-namespace`, for example `--cache-namespace model-v2`. This keeps the
previous cache intact. Changing a local export directory does not create a fresh
cache. Automatic legacy import applies only to the default `main` namespace.

## Resume an interrupted run

Resume is enabled by default. Rerun the same command, for example:

```bash
.venv/bin/python scripts/run_full_pipeline.py --ticker C --years 2023 2024 2025
```

The runner reads the PostgreSQL artifact `pipeline_runs/c/2023-2024-2025/status.json` and skips
completed stages. If it stopped during materiality, it starts at the unfinished
materiality work and its PostgreSQL save. Each year pair is tracked separately:
if one pair already finished and saved, only the other pair resumes. An unfinished
stage uses its existing batch/response caches. Raising the materiality budget or
changing worker counts preserves completed stages.

If the stopped stage asks for `--retry-failed`, inspect the error and resolve
the connection or provider issue first. Then resume with:

```bash
.venv/bin/python scripts/run_full_pipeline.py \
  --ticker NVDA --years 2024 2025 --retry-failed
```

This flag is forwarded to disclosure extraction, alignment, change analysis
and materiality, and does not invalidate completed stage checkpoints. Successful
responses and earlier attempts stay cached; request/token limits and estimated
allowances for unknown usage still apply. The UI exposes the same action as
**Retry failed AI requests on resume** when the log requests this flag. Select
it before **Resume pipeline**. Another paid attempt is allowed for that
invocation; this does not create an unlimited pipeline restart loop. Synthesis
uses its own response cache and does not accept this flag.

Synthesis is tracked as `synthesis_and_save`. A failed synthesis request or
PostgreSQL save leaves that stage incomplete; rerunning retries synthesis while
skipping completed upstream stages. A completed synthesis report is also skipped
on resume. Rerunning materiality or saving changed XBRL inputs invalidates the
synthesis completion marker. Changing `--synthesis-max-changes` or
`--synthesis-finding-batch-size` reruns synthesis only; changing its worker count
preserves a completed report.

The database progress record is retained across invocations, including runs with a different
`--until` or `--from-stage`. For example, rerunning without `--until extraction`
continues at alignment after a successful extraction-only run. A fully completed
selection exits without executing stages. Another machine connected to the same
database can reuse the cache and checkpoints; `--data-dir` only controls optional
local exports.

The Item fallback also stores page maps, pointer resolutions, and request caches
in PostgreSQL `pipeline_artifacts`, under `filing_page_cache/` in the selected
cache namespace. This cache is excluded from local result exports. Existing
`data/filing_page_cache/` files beneath the working directory are imported once
into the default namespace; the originals are left untouched. Moving the cache
does not invalidate completed preprocessing stages.

On the first run after this storage change, existing local artifacts for the
selected ticker are imported once into PostgreSQL. Completed stage signatures
are migrated, and old path references are resolved against database artifacts.
Original files are retained without modification. An older local runner must
finish before its cache is imported. Interrupted SQLite state with an outstanding
WAL is rebuilt from the separately cached request ledger rather than importing
an incomplete database image.

Use `--dry-run` to see completed stages marked `[cached; skipped]` and the commands
that remain. To deliberately execute selected stages again, use `--no-resume`:

```bash
.venv/bin/python scripts/run_full_pipeline.py --ticker C --years 2023 2024 2025 \
  --from-stage materiality --no-resume
```

This retains internal response caches. It does not request a fresh model
classification. Forcing an upstream stage to execute again invalidates completion
markers for its dependent stages, including later stages outside `--until`.
Changing synthesis workload options also invalidates that stage. Use `--no-resume`
when intentionally refreshing source
data or changing external model/database configuration. One runner at a time may
run a company's pipeline at a time, including overlapping year selections and
different cache namespaces. PostgreSQL advisory locks enforce this across machines.

## Preview, configuration and progress

Add `--dry-run` to print only the selected commands. It makes no API requests or
database writes, and reads PostgreSQL progress to identify completed stages:

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

Default concurrency for three selected years:

| Stage | Parallel work | Shared API concurrency |
| --- | --- | --- |
| Text and API/XBRL preprocessing | One year/job at a time, with ordered database saves | Sequential downloads; no LLM requests |
| Disclosure extraction | 3 years × 6 batch workers | 18 requests, shared across years |
| Alignment | 2 adjacent-year pairs × 9 workers | 18 requests, shared across pairs |
| Change analysis | 2 adjacent-year pairs × 9 LLM batch workers, followed by each pair's database save | 18 requests, shared across pairs |
| Materiality | 2 adjacent-year pairs × 9 classification/verification workers | 18 requests, shared across pairs |
| Synthesis | 18 finding workers, followed by one final report request | At most 5 finding requests with the default 50 changes / 10 per batch; hard worker maximum 20 |

Available work can reduce these concurrency levels. API request starts are spaced
by two seconds in the concurrent agent stages. Synthesis uses the same spacing
and a shared rate-limit cooldown; a failed request stops the stage and the next
invocation reuses successfully cached responses.

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
finishes LLM classification and its database save in that order. Matching and
LLM change classification have no cumulative request
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
| `--data-dir PATH` | Local export destination, used with `--save-local-output`; also the source for one-time legacy import |
| `--save-local-output` | Opt in to local result exports; caches and progress always remain in PostgreSQL |
| `--cache-namespace NAME` | PostgreSQL cache scope; default `main`. Use another name for an intentional fresh model/configuration run |
| `--from-stage STAGE` | Skip earlier stages and read saved PostgreSQL inputs; default `preprocessing` |
| `--resume` / `--no-resume` | Skip completed stages by default, or deliberately execute the selected stages using their internal caches |
| `--retry-failed` | Permit new attempts for failed/interrupted requests in unfinished extraction, alignment, change analysis and materiality; preserve successful caches and existing budgets |
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
| `--synthesis-finding-workers N` | Concurrent synthesis finding batches; default `18`, maximum `20` |
| `--synthesis-max-changes N` | Maximum material changes selected for synthesis; default `50` |
| `--synthesis-finding-batch-size N` | Changes per synthesis finding batch; default `10` |

A failure or request-budget pause stops subsequent stages. Rerun the same
command to skip completed stages and resume unfinished work. Progress is stored
in `pipeline_artifacts` under `pipeline_runs/amd/2023-2024-2025/status.json`.
`until` records the requested endpoint; `complete` means that selected run
finished, and `full_pipeline_complete` is true only when the selected endpoint is
synthesis and its PostgreSQL save has succeeded.
`from_stage` records the starting point and `input_source` is `postgres`.
`skipped_stages` lists stages reused by the latest invocation; `last_invocation_at`
records that invocation's start. `started_at` retains the original run start.
`extraction_concurrency` records the year workers, batch workers per year, and
shared request cap used by that invocation. `alignment_concurrency` records pair
workers, workers per pair, and the shared alignment API cap. A running process keeps the settings
it started with; restart the command to use changed defaults and reuse saved work.
`materiality_concurrency` records the corresponding pair workers, workers per pair,
and shared materiality API cap.
`synthesis_concurrency` records finding workers, batch size, maximum changes,
the resulting maximum simultaneous requests and the single final report worker.
`synthesis_status` records pending, running, complete, failed or skipped.

## Output and database fields

The paths below are logical PostgreSQL artifact keys. They become local result
paths beneath `--data-dir` only when `--save-local-output` is supplied.

- Narrative artifacts: `raw/amd/<year>/`. The raw loader uses `--sync`, which
  removes obsolete raw IDs while preserving filing rows and downstream disclosures.
- Table artifacts: `table_output/amd/`. Yearly API/XBRL exports are stored in
  PostgreSQL. The merged JSON is also a PostgreSQL artifact. Table metrics are
  separate from the narrative inputs consumed by the agents.
- Disclosure artifacts: `disclosures/amd/<year>/`.
- Alignment artifacts: `alignments/amd/<previous>-<current>/`.
- Change analysis: the alignment row's `change_analysis` object contains
  `lexical`, `semantic`, `llm`, and `final_taxonomy`, stored in the existing
  PostgreSQL `change_analysis` JSONB column. New LLM-only results keep `lexical`,
  `semantic` and `final_taxonomy` null; the validated label is stored in `llm`.
  Synthesis reads that `llm` label directly. Existing deterministic labels for
  verified identical groups and introduced/removed disclosures are preserved.
- Materiality: `materiality_analysis` is added to finalized alignment rows and
  stored in PostgreSQL at `alignments.extra.materiality_analysis`.
- Synthesis: `synthesis/<COMPANY>_<FIRST_YEAR>_<LAST_YEAR>_synthesis_output.json`
  and PostgreSQL `synthesis_reports.report` contain the report, source catalog,
  selected financial facts, coverage details and token usage. The database
  transaction must commit before the report artifact is published or the pipeline
  records success. The writer creates `synthesis_reports` when needed.

The database contains each stage's result records and published audit snapshots.
PostgreSQL response caches remain available to avoid repeating API work;
the merged XBRL JSON is a derived export of the individually stored yearly facts.

Rows requiring alignment review remain in `needs_review.json` and are deferred
from the change-analysis and materiality stages.
