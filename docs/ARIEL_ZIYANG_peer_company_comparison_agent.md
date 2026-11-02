# Peer comparison pipeline

Peer comparison runs separately from the main preprocessing-to-synthesis
pipeline. It compares one target ticker against one or more peer tickers for
the same fiscal year, using disclosures already stored in PostgreSQL.

## Required database inputs

Every requested ticker must have a `10-K` filing and at least one stored
disclosure for the selected fiscal year. A raw filing without extracted
disclosures does not qualify. Tickers are matched without regard to case.

The pipeline checks the target and **all** peers before creating output
directories, replaying cached comparisons or starting model
requests. Missing tickers, missing years, missing disclosures and database errors
stop the run. Local JSON files cannot satisfy or bypass this requirement.

To prepare missing inputs, run the [main pipeline](ZIYANG_full_pipeline.md) for
each company. For example:

```bash
.venv/bin/python scripts/run_full_pipeline.py --ticker AMD --years 2023 2024 2025
.venv/bin/python scripts/run_full_pipeline.py --ticker NVDA --years 2023 2024 2025
```

By default, peer comparison selects target disclosures whose materiality is
`Yes` in a completed adjacent-year alignment. For 2024, either 2023–2024 or
2024–2025 may provide that assessment. Complete materiality for the target before
using this default. Use `--target-selection all` to compare all target
disclosures; this still requires PostgreSQL data for every ticker.

## Preview and run

The dashboard's **Peer comparison** page connects to
`scripts/run_peer_comparison_pipeline.py`. Select a target, peers, one fiscal
year, Items, and either material changes or all disclosures. Click **Check
prerequisites** before **Start peer comparison**. The readiness check uses the
same saved-evidence and target-selection validation as the backend, with no
model requests or writes. Missing company/year data or material target
disclosures blocks launch and points to **Run pipeline**. Changing the selection
requires a new check; launch checks inputs again, and the CLI retains its own
database gate.

The UI runs comparison in a background process, polls comparison counts, and
shows an activity log. Stopped or partial runs can be resumed with the same
selection, reusing PostgreSQL caches. It does not start the main pipeline
automatically. In normal `auto` or `postgres` dashboard mode, saved reports are
read directly from `peer_comparison_reports`; no local export is required.
Local exports and marked sample reports remain viewable.
`SEC_UI_DATA_SOURCE=files` limits the saved-report viewer to local files.

UI process metadata and its console log are kept under `data/peer_runs/`.
Evidence, model caches, comparison progress and saved reports stay in PostgreSQL.

Check database inputs and candidate retrieval without model calls or writes:

```bash
.venv/bin/python scripts/run_peer_comparison_pipeline.py \
  --ticker AMD --peers NVDA --year 2024 --dry-run
```

Run the comparison and save its results:

```bash
.venv/bin/python scripts/run_peer_comparison_pipeline.py \
  --ticker AMD --peers NVDA --year 2024
```

By default, reports, caches and progress stay in PostgreSQL; no local files are
created. To also export reports and token usage locally:

```bash
.venv/bin/python scripts/run_peer_comparison_pipeline.py \
  --ticker AMD --peers NVDA --year 2024 \
  --save-local-output --output-dir ./exports/amd-nvda-2024
```

The local export never contains request caches, job caches, manifests or graph
checkpoints. Cache reuse is independent of the export directory.

Multiple peers are supported, and all must pass the database check:

```bash
.venv/bin/python scripts/run_peer_comparison_pipeline.py \
  --ticker AMD --peers NVDA INTC --year 2024
```

`--peer-ticker` is an alias for `--peers`. The original
`scripts/compare_peer_disclosures.py` command is also an alias for this pipeline;
it applies the same database prerequisites. Local input directory options are
not accepted by either command.

The pipeline uses the repository `.env`, exported database/SoCLaaS settings and
the shared default SoCLaaS configuration. `--env-file PATH` selects another
literal configuration file. Exported variables take priority.

## Comparison behavior and concurrency

The pipeline reads normalized filings, disclosures, alignments and alignment
members in one consistent, read-only transaction. It loads accepted and
review-candidate disclosures, validates their source sentences and retains their
review status. Available completed adjacent-year alignments provide context,
including materiality and the saved `change_analysis.llm` label.

The existing peer agent retrieves up to three same-Item candidates per peer
using local TF-IDF ranking, then asks the LLM to compare their source evidence.
This retrieval does not run the main pipeline's lexical or semantic change
analysis. Each comparison must cite exact sentences from the target and peer.
A missing candidate is distinct from a retrieved candidate judged incomparable.

There are **6 concurrent target-comparison workers** by default, with a maximum
of **20**. Each worker handles one target disclosure and all requested peers,
including any bounded correction attempts. Available target work may use fewer
workers. Peer comparison never starts the main pipeline or downloads filings.

| Option | Default | Purpose |
| --- | --- | --- |
| `--year YEAR` | Required | Shared fiscal year, 1900–2200 |
| `--items` | `1 1A 7 8` | SEC Items to compare; Item `15` is also supported |
| `--target-selection` | `material_changes` | Select material target disclosures, or `all` |
| `--workers` | `6` | Concurrent target comparisons; maximum `20` |
| `--top-k` | `3` | Retrieved candidates per peer and target |
| `--max-requests` | `-1` | New requests allowed this invocation; `-1` is unlimited |
| `--max-validation-attempts` | `3` | Maximum attempts to produce a valid cited result |
| `--data-dir` | Repository `data` | Root for optional local report exports |
| `--output-dir` | Derived from target/year/peers | Local result destination when `--save-local-output` is set |
| `--save-local-output` | Disabled | Export reports and token usage locally; caches remain in PostgreSQL |

## PostgreSQL output and resume

Reports, citations and token usage are upserted together into
`peer_comparison_reports.report`. Rows are keyed by target ticker, fiscal year,
sorted peer tickers, Items and target selection. `run_complete` records whether
every target comparison finished. The writer creates the table when needed;
fresh databases receive it from `db/init/07_peer_comparison_reports.sql`.

Requests, validated jobs, manifests and report artifacts are committed to
`pipeline_artifacts` in the `peer` namespace as the comparison runs. Different
input/model/settings fingerprints get separate cache profiles automatically.
PostgreSQL advisory locks prevent concurrent writers to the same comparison.

With `--save-local-output`, the default export for AMD versus NVDA in 2024 is:

```text
data/peer_comparison/amd/2024/nvda/
```

The export folder contains `peer_comparison.json` and `token_usage.json`.

Rerun the same command to reuse completed target jobs and successful model
responses. The pipeline rechecks PostgreSQL on **every** invocation, including
cached runs. Cache profiles include source data and comparison settings, so
changed inputs cannot silently reuse stale jobs. Another machine connected to
the same PostgreSQL database can resume without copying any local cache. Existing
local peer caches are imported once without modifying their original files.

Exit code `0` means the complete report was saved to PostgreSQL. Code `2` means a
partial report was saved after reaching the request limit. Code `1` indicates a
prerequisite, comparison or storage failure. A failed database save cannot be
reported as success; cached jobs remain available for retry.

Candidate retrieval can miss related disclosures. A `no_candidate_retrieved`
result does not establish filing-wide absence. Model conclusions remain bounded
by the supplied disclosures and cited evidence.
