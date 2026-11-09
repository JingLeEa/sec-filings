# Bundled starter data

This directory is a versioned input dataset for new installations. It is tracked
in the main branch so a clone needs no second checkout, Git command, model API
key or SEC download to browse the example companies.

`reports.tar.gz` contains 133 saved reports based on `origin/test-data`, pinned to
the commit in `manifest.json`.
The manifest records each report's checksum, scope and any supplemental source.
Completed reports exclude 819 unresolved, one-sided alignment review cases that
have no counterpart for a person to compare. All 3,118 accepted New/Removed
results are retained. Exclusion IDs and reasons remain in report metadata for
coverage accounting; excluded rows do not enter the review queue or downstream
analysis. Rebuilding the bundle applies this rule automatically.

The bundle includes:

| Data | Scopes |
| --- | ---: |
| Disclosures | 33 company/filing years |
| Alignments and their saved change/materiality results | 22 year comparisons |
| Synthesis | 11 reports |
| Peer comparison | 12 reports, preserving their completion status |

Companies: AMD, AVGO, BAC, C, GS, INTC, JPM, MS, MU, NVDA and QCOM.

### Financial figures in the saved synthesis reports

The reports follow `test-data` commit
`5cfccfe4cc6fb55bae1836a7f211106950bf6d63` (9 November 2026). All synthesis
reports are taken directly from that commit, including the newly added
Citigroup report, updated financial figures, and the corrected GS and NVDA
reports generated from their current disclosure and alignment inputs.

| Synthesis report | Financial metrics | Calculated trends |
| --- | ---: | ---: |
| AMD FY2023–FY2025 | 61 | 81 |
| AVGO FY2023–FY2025 | 52 | 65 |
| BAC FY2023–FY2025 | 30 | 29 |
| C FY2023–FY2025 | 32 | 30 |
| GS FY2023–FY2024 | 30 | 29 |
| INTC FY2023–FY2025 | 56 | 73 |
| JPM FY2023–FY2025 | 30 | 29 |
| MS FY2023–FY2025 | 32 | 34 |
| MU FY2023–FY2025 | 58 | 75 |
| NVDA FY2023–FY2025 | 48 | 47 |
| QCOM FY2023–FY2025 | 41 | 41 |

The older JPM supplemental override has been retired because the branch now
contains the updated report itself. All 11 bundled synthesis reports include
structured financial metrics. Financial data is not generated during bundling
or app startup.

All bundled reports passed active disclosure-ID and verbatim sentence-citation
checks. GS and NVDA synthesis each cover 50 selected material changes, with 241
and 244 source sentence citations respectively. Their reports match the current
bundled disclosures and alignments.

On the first app launch, the reports are streamed into PostgreSQL in one
transaction. The archive is never extracted to local files. Each new app process
checks which scopes already exist and imports only missing scopes. Existing
disclosures, partial or complete alignment runs, synthesis reports and peer
reports are retained. Startup also removes unresolved one-sided review rows from
already loaded completed comparisons, updating normalized rows and snapshots in
the same transaction. Existing human labels/notes and accepted results remain
untouched. For companies with an active pipeline, startup launches a single
background cleanup worker that waits for the company lock to be released; its
status stays in PostgreSQL. Concurrent launches share a database lock; a failed import
rolls back and can be retried by reloading the app.

These files are shipped inputs, separate from generated pipeline output. The
test-data branch does not contain original raw preprocessing inputs, standalone XBRL exports
or the complete extraction/alignment audit metadata. The bootstrap does not
invent those inputs or historical execution records. When stored disclosures,
completed adjacent-year alignments, and a matching synthesis report are all
available, it writes a completion marker tagged `completion_source=starter_data`.
This also backfills earlier installations without reimporting their reports.
Existing pipeline checkpoints, including unfinished runs, are preserved; active
pipeline locks are respected. The dashboard shows that starter results are ready
and does not offer another run for that exact company/year selection.

The starter results support browsing, chatbot queries and stored peer reports.
The normal full-pipeline command also reuses that completion marker. A deliberate
fresh execution requires `--no-resume`. Run the real pipeline to
obtain full preprocessing inputs before using downstream manual-review reruns.
Historical request/job caches are excluded from the bundle.

Citigroup's review-candidate disclosures for 2023–2025 and both alignment review
reports are now included. These resolve the 39 disclosure IDs absent from the
earlier bundle. Active Citigroup alignment and synthesis references validate
against the included disclosures.

Normal setup, after installing dependencies:

```bash
docker compose up -d
.venv/bin/python -m streamlit run app/Home.py
```

To run the same import explicitly:

```bash
.venv/bin/python scripts/seed_starter_data.py
```

To load a newer bundled pack into an existing database, including updates to
older starter records:

```bash
.venv/bin/python scripts/seed_starter_data.py --refresh
```

On Windows PowerShell, use
`.\.venv\Scripts\python.exe scripts\seed_starter_data.py --refresh`.
Pull the updated `main` branch and start PostgreSQL first, then click **Reload data**
in the UI or restart Streamlit
after the command completes. No model calls, SEC downloads or local result
exports are performed.

Refresh is explicit; ordinary startup remains missing-only. It replaces only
records whose starter provenance is intact. Normalized disclosures and alignment
rows must still match their saved starter snapshots. Legacy synthesis reports
must match a historical report fingerprint in the bundle manifest and the import
transaction timestamp shared with seeded filing snapshots; legacy peer reports
use historical fingerprints and their recorded starter import source. New
synthesis and peer imports carry a checksum of the imported payload so later
edits are detectable.

Human reviews, pending review jobs, active pipelines, non-starter checkpoints,
and pipeline-produced snapshots protect the affected company. Unverifiable
records protect the company too; peer reports involving protected companies are
preserved. The loader shares pipeline and review locks with their writers, and
updates the database in one transaction: a failed update rolls back. It reports
refreshed, unchanged and protected scope counts without clearing caches, run
history, human labels, or raw/XBRL inputs.

`SEC_UI_BOOTSTRAP_TEST_DATA=0` disables automatic importing. Explicit
`SEC_UI_DATA_SOURCE=files` also skips it for offline fixture development. A
database/import failure stops normal app startup with setup instructions rather
than presenting local fixtures as successfully imported PostgreSQL data.

Maintainers can refresh the shipped bundle intentionally:

```bash
git fetch origin test-data
.venv/bin/python scripts/bundle_starter_data.py
```

Optional validated reports in `starter_data/supplemental/` override matching synthesis
reports from the branch during every rebuild. A supplement must have the same
filename, company and fiscal years as the branch report; its source path and
checksum are recorded in the bundle manifest. There are no active supplemental
overrides in this bundle. Retire an override once its updated report is included
in the source branch so it cannot replace a newer branch version.

Review and commit the archive, manifest and supplemental reports together. App
startup never fetches Git branches or runs model requests. Existing PostgreSQL
reports remain protected during startup; `seed_starter_data.py --refresh` is the
explicit update path for unchanged older starter records. Rebuilding the bundle
does not connect to or modify PostgreSQL.
