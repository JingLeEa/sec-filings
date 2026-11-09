# Bundled starter data

This directory is a versioned input dataset for new installations. It is tracked
in the main branch so a clone needs no second checkout, Git command, model API
key or SEC download to browse the example companies.

`reports.tar.gz` contains 127 saved reports based on `origin/test-data`, pinned to
the commit in `manifest.json`, with explicit supplemental synthesis refreshes.
The manifest records each report's checksum, scope and any supplemental source.
Completed reports exclude 638 unresolved, one-sided alignment review cases that
have no counterpart for a person to compare. All 3,118 accepted New/Removed
results are retained. Exclusion IDs and reasons remain in report metadata for
coverage accounting; excluded rows do not enter the review queue or downstream
analysis. Rebuilding the bundle applies this rule automatically.

The bundle includes:

| Data | Scopes |
| --- | ---: |
| Disclosures | 33 company/filing years |
| Alignments and their saved change/materiality results | 22 year comparisons |
| Synthesis | 10 reports |
| Peer comparison | 12 reports, preserving their completion status |

Companies: AMD, AVGO, BAC, C, GS, INTC, JPM, MS, MU, NVDA and QCOM.

### Financial figures in the saved synthesis reports

The base reports follow `test-data` commit
`0f6795397c834bf0ef77c1e4b9f4c2dcc5f7635c` (8 November 2026). JPM's
FY2023–FY2025 synthesis was refreshed separately on 9 November 2026 after
extracting and verifying its original filings' XBRL figures. The refreshed report
retains the same 180 material disclosure changes and passes evidence validation.

| Synthesis report | Financial metrics | Calculated trends |
| --- | ---: | ---: |
| AMD FY2023–FY2025 | 61 | 81 |
| JPM FY2023–FY2025 | 30 | 29 |
| Other bundled companies | 0 | 0 |

JPM's refreshed report is tracked in
`supplemental/jpm_2023_2025_synthesis_output.json`. Bundling uses it in place of
the matching, finance-free branch report. A missing figures table in the other
reports reflects their source data, rather than a failed import. Financial data
is not generated during bundling or app startup.

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
test-data branch does not contain original raw preprocessing inputs, XBRL facts
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

The source's Citigroup alignments reference 39 disclosure IDs absent from its
disclosure exports. For those starter comparisons only, the UI reads the original
saved alignment snapshot from PostgreSQL and shows available cited evidence when
the full disclosure text is missing. It does not create replacement text. Normal
pipeline and peer-input validation still reject incomplete normalized inputs.

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

Validated reports in `starter_data/supplemental/` override matching synthesis
reports from the branch during every rebuild. A supplement must have the same
filename, company and fiscal years as the branch report; its source path and
checksum are recorded in the bundle manifest. Keep supplemental inputs with the
archive so later rebuilds retain financial refreshes.

Review and commit the archive, manifest and supplemental reports together. App
startup never fetches Git branches or runs model requests. Existing PostgreSQL
reports remain protected during startup; `seed_starter_data.py --refresh` is the
explicit update path for unchanged older starter records. JPM's report was also
regenerated directly in the originating PostgreSQL database.
