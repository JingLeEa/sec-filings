# Bundled starter data

This directory is a versioned input dataset for new installations. It is tracked
in the main branch so a clone needs no second checkout, Git command, model API
key or SEC download to browse the example companies.

`reports.tar.gz` contains 127 saved reports from `origin/test-data`, pinned to the
commit in `manifest.json`. The manifest records each report's checksum and scope.
Completed reports exclude 680 unresolved, one-sided alignment review cases that
have no counterpart for a person to compare. All 3,101 accepted New/Removed
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
invent them or mark a full pipeline as complete. The starter results support
browsing, chatbot queries and stored peer reports. Run the real pipeline to
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

`SEC_UI_BOOTSTRAP_TEST_DATA=0` disables automatic importing. Explicit
`SEC_UI_DATA_SOURCE=files` also skips it for offline fixture development. A
database/import failure stops normal app startup with setup instructions rather
than presenting local fixtures as successfully imported PostgreSQL data.

Maintainers can refresh the shipped bundle intentionally:

```bash
git fetch origin test-data
.venv/bin/python scripts/bundle_starter_data.py
```

Review and commit the new archive and manifest together. App startup never fetches
Git branches or runs model requests.
