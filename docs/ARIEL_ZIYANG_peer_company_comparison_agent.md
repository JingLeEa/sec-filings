# Peer comparison pipeline

For native Windows commands, follow [Windows setup](windows_setup.md).

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

The peer agent retrieves up to three candidates total per peer using local
TF-IDF ranking across the eligible Items, then asks the LLM to compare their
source evidence. Candidates always use the same fiscal year. Item eligibility is:

| Target's original Item | Eligible peer Items |
| --- | --- |
| 1 | 1 |
| 1A | 1A |
| 7 | 7, 15 |
| 8 | 8, 15 |
| 15 | 7, 8, 15 |

Selecting Item 7 or 8 automatically includes Item 15 target disclosures and
candidate evidence, covering issuers that place their financial narrative there.
Selecting only Item 7 keeps peer candidates restricted to Items 7 and 15;
selecting only Item 8 keeps them restricted to Items 8 and 15. Explicitly
selecting Item 15 permits its targets to retrieve peers' Items 7, 8 and 15.
Items 7 and 8 do not match each other directly. Material-change mode still
requires each selected target disclosure, including Item 15 targets, to have
stage-5 materiality `Yes`. Check prerequisites to see the expanded target count.

For cross-Item matches involving Item 15, the model must identify substantive
related MD&A or financial content; generic exhibits, signatures and page
pointers alone are not comparable evidence. Item eligibility enables retrieval,
not a claim that every candidate is comparable. Original source Items and IDs
are preserved. Reports, CSV/JSON downloads and cited evidence retain the Items
on both sides, and the UI/Markdown show pairs such as `JPM Item 15 ↔ BAC Item 7`.

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
| `--max-validation-attempts N` | `3` | Response attempts per target per round, including the first response and any corrections |
| `--max-retries N` | `2` | Additional automatic retry rounds per target after a retryable failure: `2` allows three rounds total; `0` disables additional rounds |
| `--retry-backoff SECONDS` | `5 seconds` | Delay before the first automatic retry; subsequent delays double, capped at 60 seconds unless the server requests a longer wait |
| `--data-dir` | Repository `data` | Root for optional local report exports |
| `--output-dir` | Derived from target/year/peers | Local result destination when `--save-local-output` is set |
| `--save-local-output` | Disabled | Export reports and token usage locally; caches remain in PostgreSQL |

With the defaults, automatic retries wait 5 seconds before the first retry and
10 seconds before the second. Each round allows up to three response-validation
attempts, so persistently invalid responses can require up to nine API calls per
target across all three rounds. Temporary API failures can end a round earlier.
Every new call counts toward `--max-requests`. A server `Retry-After` can extend
the wait, and HTTP 429 also applies a shared cooldown across workers.

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

Compatible profiles now share validated target jobs through a content-addressed
`shared_jobs/` cache for the same target, year, peer set and target-disclosure
selection mode. Material changes and All disclosures never share cached jobs,
even when their selected disclosures happen to be identical. Switching modes
uses that mode's separate cache; returning to a mode can reuse its own results.
For example, within the same mode, running
Item 1 and then selecting Items 1, 1A, 7 and 8 reuses unchanged Item 1 results
and requests only additional or changed comparisons. Each new report contains
only its selected/automatically included targets; the earlier report remains
stored under its own selection key. Reports are upserted using the existing
database key, without adding tables or appending duplicate target rows.

A reusable job covers one target disclosure and all requested peers, not an
independent disclosure pair. Its fingerprint includes the actual target evidence,
retrieved candidates, source context, model/endpoint, prompt and matching policy.
The target-selection mode is part of both the job fingerprint and legacy-profile
compatibility checks, preventing cross-mode reuse through either cache path.
TF-IDF is fitted within the eligible Item/year corpus, so adding unrelated Items
does not change an Item's ranking. Adding Item 8 to an Item 7 selection can still
change Item 15's eligible candidates: unchanged Item 7 work is reused, while the
affected Item 15 job is rerun. Changed evidence, candidates, model settings or
policy also prevent unsafe reuse. Materiality eligibility and database
prerequisites are rechecked before cache lookup.

Existing compatible profile-only jobs can be adopted when the saved model input
and cited evidence still match and their original source-export fingerprint can
be verified. If an older cache lacks per-job evidence fingerprints and its source
exports changed, it is conservatively recomputed once. Older matching policies and incompatible jobs
are retained and skipped. Original requests and usage ledgers are not copied or
rewritten during reuse. `jobs_reused` and `new_api_requests` describe the latest
invocation; `token_usage` retains the current profile's recorded request history.
Reused jobs link to the original profile/request in `cache_provenance`. The UI,
activity log and Markdown report show reuse counts. New attempts, including
automatic retries, still count against `--max-requests`; a zero budget permits
cache reuse but makes no new model requests.

The Item-15 matching policy uses a new cache-policy version. Results created
under the earlier same-Item-only rules are retained in their old profiles and
are not reused for the new matching policy. Normal PostgreSQL runs select the
new profile automatically; previously saved reports remain viewable until a new
report for that selection is successfully saved.

With `--save-local-output`, the default export for AMD versus NVDA in 2024 is:

```text
data/peer_comparison/amd/2024/nvda/
```

The export folder contains `peer_comparison.json` and `token_usage.json`.

Rerun the same command to reuse completed target jobs and successful model
responses. The pipeline rechecks PostgreSQL on **every** invocation, including
cached runs. Cache profiles include source data and comparison settings, so
changed comparison evidence cannot silently reuse stale jobs. Another machine connected to
the same PostgreSQL database can resume without copying any local cache. Existing
local peer caches are imported once without modifying their original files.

Exit code `0` means the complete report was saved to PostgreSQL. Code `2` means a
partial report was saved after reaching the request limit. Code `1` indicates a
prerequisite, comparison or storage failure. A failed database save cannot be
reported as success; cached jobs remain available for retry.

Candidate retrieval can miss related disclosures. A `no_candidate_retrieved`
result does not establish filing-wide absence. Model conclusions remain bounded
by the supplied disclosures and cited evidence.
