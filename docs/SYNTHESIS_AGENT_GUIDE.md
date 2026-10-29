# Synthesis Agent User Guide

The synthesis agent creates an evidence-grounded report from material disclosure
changes and company-wide XBRL facts already stored in PostgreSQL. It writes a
portable JSON report and also upserts that report into the `synthesis_reports`
table.

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
4. Ensure the `synthesis_reports` table exists. Fresh databases receive it from
   `db/init/06_synthesis_reports.sql`. For an existing Docker volume, apply the
   migration once:

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

The full pipeline's synthesis defaults remain 50 changes and 18 finding workers.
Use `--skip-synthesis` to run upstream stages without making synthesis requests.
This is useful for pipeline testing and upstream-only runs. The pipeline does
not invoke synthesis when stopped at an earlier `--until` stage.

## Outputs and validation

For the default data directory, synthesis writes:

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
selection updates that database row. The local JSON remains a portable copy.
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
