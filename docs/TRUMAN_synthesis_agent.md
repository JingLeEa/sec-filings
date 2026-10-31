# Synthesis Agent User Guide

The synthesis agent creates an evidence-grounded report from material disclosure
changes and company-wide XBRL facts already stored in PostgreSQL. It writes a
portable JSON report and also upserts that report into the `synthesis_reports`
table.
Change taxonomies come directly from the saved `change_analysis.llm` labels;
synthesis does not require lexical or semantic enrichment.

## Prerequisites

1. Start the project's PostgreSQL service from the repository root:

   ```powershell
   docker compose up -d db
   docker compose ps
   ```

   Wait until the `db` service reports `healthy`.
2. Configure database access and SoCLaaS credentials in the repository `.env`
   file, or supply an alternate file with `--env-file`.
3. Ensure that the requested company and years have completed materiality
   classifications in PostgreSQL. Synthesis only selects changes explicitly
   labeled material. Load XBRL facts for the financial analysis when available;
   the report explains when no company-wide facts were retrieved.
4. The writer creates `synthesis_reports` if needed, and fresh databases receive
   it from `db/init/06_synthesis_reports.sql`. If the pipeline database user cannot
   create tables, apply the migration once with a database owner:

   ```powershell
   Get-Content db\init\06_synthesis_reports.sql |
     docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U sec -d sec_disclosure
   ```

   Replace the example database user/name if your Compose configuration differs.

## Run synthesis directly

From the repository root, activate or use the project's Python environment and
run `scripts/run_synthesis.py`:

```powershell
.\.venv\Scripts\python.exe scripts\run_synthesis.py `
  --company AMD `
  --years 2023 2024 2025 `
  --env-file .env `
  --data-dir data `
  --max-changes 50 `
  --finding-workers 18
```

Years are sorted and treated as adjacent comparisons: the example analyzes
2023–24 and 2024–25. The agent distributes its maximum selected changes across
the requested pairs, then fills unused quota from other material changes if a
pair has fewer eligible rows.

The synthesis worker pool runs finding batches concurrently and has a hard
maximum of 20 workers. It defaults to 18; the total number of concurrent requests
may be lower when there are fewer finding batches. The default batch size is 10
changes. For example, 180 changes produce up to 18 batches: across two pairs that
is nine batches per pair, or across three pairs six per pair, with a shared pool
of 18 workers. Increasing `--max-changes` increases both the amount of evidence
sent for analysis and potential API usage.

Other useful options:

| Option | Default | Description |
| --- | ---: | --- |
| `--limit-per-pair` | 500 | Maximum alignments retrieved for each adjacent-year pair. |
| `--max-changes` | 50 | Maximum explicitly material changes selected. |
| `--finding-batch-size` | 10 | Number of changes supplied to each findings request. |
| `--finding-workers` | 18 | Concurrent findings batches; valid range is 1–20. |
| `--finding-target` | One per batch | Minimum number of findings requested. Every selected change must still be cited; related changes may be consolidated. |
| `--timeout` | 180 seconds | Timeout for each SoCLaaS request. |
| `--max-tokens` | 7000 | Maximum completion tokens per request. |
| `--env-file` | Repository `.env` / defaults | Optional file for literal database and SoCLaaS settings. |
| `--data-dir` | Repository `data` | Directory for report, response cache, and usage records. |

Use the shared SoCLaaS configuration and check its model-specific limits before
increasing request sizes or concurrency. A live run can make paid API requests.

## Run through the full pipeline

When all upstream PostgreSQL pipeline stages are complete, the full pipeline
invokes synthesis automatically:

```powershell
.\.venv\Scripts\python.exe scripts\run_full_pipeline.py `
  --ticker AMD `
  --years 2023 2024 2025 `
  --synthesis-max-changes 180 `
  --synthesis-finding-workers 18
```

Synthesis is the full pipeline's default final stage; `--until synthesis` selects
it explicitly. Its defaults are 50 changes, 10 changes per batch and 18 finding
workers, so at most five finding requests can run together for the default
workload. `--synthesis-finding-batch-size` controls the batch size. Finding requests
share two-second start spacing and rate-limit cooldowns; the final report request
runs after all finding batches finish. Peer comparison is outside the pipeline.

Use `--until materiality` (or the legacy `--skip-synthesis`) for an upstream-only
run. Use `--from-stage synthesis` to read already-saved PostgreSQL inputs directly.
Synthesis and its database save are tracked together as `synthesis_and_save`:
failed runs retry that stage, completed runs skip it, and rerunning upstream
materiality or XBRL saves invalidates its completion marker.

The full pipeline stores synthesis responses, usage and output artifacts in
PostgreSQL with no local cache or output files. Add `--save-local-output` to the
full-pipeline command to export report/usage files beneath `--data-dir`; cache
files remain in PostgreSQL. The direct standalone command above retains its
explicit file-oriented workflow.

## Outputs and validation

For the standalone file workflow, synthesis writes:

- `data/synthesis/<COMPANY>_<FIRST_YEAR>_<LAST_YEAR>_synthesis_output.json`
- `data/synthesis/<COMPANY>_<FIRST_YEAR>_<LAST_YEAR>_token_usage.json`
- cached successful responses under `data/synthesis/cache/`

The final JSON is saved only after findings and the final report pass structural
and evidence-ID validation. The validation requires complete citation coverage
of selected material changes, permits consolidation into fewer findings, and
records coverage details in the report. Validation does not independently
establish that a cited source entails every statement.

The report is also upserted into PostgreSQL `synthesis_reports`, keyed by
uppercase company and the ordered year array. Re-running the same company/year
selection updates that database row. The stored report also includes token usage.
The database transaction must commit successfully before the local report is
published and the pipeline marks synthesis complete. A connection or commit
failure stops the stage and retains successful model-response caches for retry.
The local JSON remains a portable copy.
Token usage is provider-reported; cached responses are recorded separately and
do not represent new API charges.

## Offline tests

Run the synthesis and pipeline regression tests without connecting to
PostgreSQL or making LLM requests:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests\test_synthesis_agent.py `
  tests\test_full_pipeline.py `
  tests\test_synthesis_integration.py -q
```
