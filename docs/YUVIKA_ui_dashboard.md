# YUVIKA: UI dashboard

**Maintainer:** YUVIKA — contact YUVIKA for questions about the dashboard. 

**Run pipeline + Peer Comparison UI:** JING LE

## 1. Purpose and implementation status

A Streamlit app that lets a reader explore what changed in a company's 10-K from one year to the next and check the source text behind each change. It reads the saved results of the pipeline (aligned disclosures with change labels and materiality) and the outputs of the synthesis and peer comparison agents. It covers user story 9: filters, side-by-side comparison, synthesis and peer views, evidence access, export, and clear loading, empty and error states.

Implemented:

- **Overview** (`app/Home.py`): counts and charts for one company and year pair.
- **Change explorer**: filter by SEC Item, content category, change label, materiality, match status, match type and free text using controls in the page; click a row for the reasoning; export the filtered rows as CSV or JSON.
- **Side by side**: earlier and later disclosure text next to each other, with words removed (red, struck through) and added (green, underlined). Filters appear in the page above the disclosure selector, while company and years remain in the sidebar. One-sided matches show "No counterpart found". Sentence IDs, disclosure IDs and SEC links are listed under the text.
- **Synthesis report**: reads the synthesis agent's JSON, shows each finding with its cited evidence, financial figures, limitations and validation result; downloads as Markdown, evidence CSV and JSON.
- **Peer comparison**: reads the peer agent's `peer_comparison.json`, shows each target disclosure against each peer with the cited sentences on both sides; downloads as Markdown, CSV and JSON.
- **Run pipeline**: accepts a ticker and comma-separated fiscal years (1900-2200), shows the exact comparison pairs, confirms selections spanning more than two years, and starts or resumes `run_full_pipeline.py` in the background. Polls the runner's stage status, shows completed PostgreSQL saves, activity logs, and an in-app completion or failure notice.
- Two data sources behind one data layer (`app/sec_ui/data.py`): the JSON files on the `test-data` branch, or PostgreSQL through `sec_disclosure.storage.exports`.

Not implemented (planned): the chatbot (Aarav), user accounts and saving of user notes, and human review actions on `needs_review` matches.

Verification: see section 8.

## 2. Code and workflow

| File | Responsibility |
| --- | --- |
| `app/Home.py` | Overview page and the entry point. |
| `app/pages/0_Run_Pipeline.py` ... `4_Peer_Comparison.py` | One file per page; the run page launches and monitors the full pipeline. |
| `app/sec_ui/pipeline_runs.py` | Safe local subprocess launch, persisted UI job state, console-log tail, and mapping of saved database outputs to pipeline phases. |
| `app/sec_ui/config.py` | Data folders, environment variables, company names. |
| `app/sec_ui/data.py` | Finds data, loads a comparison, flattens each alignment into one record, filters and exports. No Streamlit code. |
| `app/sec_ui/diff.py` | Word-level text comparison with escaped HTML. No Streamlit code. |
| `app/sec_ui/reports.py` | Loaders and flatteners for the synthesis and peer outputs. If an agent changes its JSON layout, change only this file. |
| `app/sec_ui/components.py` | Shared sidebar, filters, caching and styling. |

```mermaid
flowchart TD
    A[test-data JSON files] --> D[data.py]
    B[(PostgreSQL)] --> D
    C[Synthesis and peer JSON] --> R[reports.py]
    U[Run pipeline page] --> J[run_full_pipeline.py]
    J --> B
    J --> S[status.json]
    S --> U
    D --> P[Explorer, Side by side, Overview]
    R --> Q[Synthesis, Peer pages]
    P --> E[CSV and JSON export]
    Q --> E
```

The exploration and report pages remain read-only. The Run pipeline page starts the existing CLI, which makes SEC and model API calls and saves results to PostgreSQL. It polls `status.json` every three seconds and clears the comparison cache when the run completes. While running, it shows a spinner and stage-level progress from saved extraction batch/check checkpoints, alignment matching/verification jobs, and materiality row counts. The bars count completed work units, not elapsed time or an ETA. Loaded comparisons are otherwise cached for 15 minutes; the sidebar button "Reload data" clears the cache.

## 3. Setup

Python 3.11 or newer and the packages in `requirements.txt` (the dashboard adds `streamlit>=1.50,<2`). Browsing saved data needs no API keys; starting a run requires PostgreSQL, `SEC_USER_AGENT`, `SEC_API_KEY`, and the SoCLaaS settings described in [the full pipeline guide](ZIYANG_full_pipeline.md).

```bash
pip install -r requirements.txt
git fetch origin test-data
git worktree add ../sec-filings-test-data origin/test-data   # about 227 MiB, once
streamlit run app/Home.py
```

The app finds `../sec-filings-test-data/test_data` by itself. Optional environment variables:

| Variable | Meaning |
| --- | --- |
| `SEC_UI_DATA_SOURCE` | `auto` (default: database if it has completed runs, otherwise files), `files`, or `postgres`. |
| `SEC_UI_ALIGNMENTS_DIR`, `SEC_UI_DISCLOSURES_DIR` | Folders laid out as `<company>/<previous>-<current>/alignments.json` and `<company>/<year>/disclosures.json`. |
| `SEC_UI_SYNTHESIS_DIRS` | Folders holding synthesis output JSON (default `data/synthesis`, `synthesis_dev`, `tests/fixtures/synthesis`). |
| `SEC_UI_PEER_DIR` | Folder laid out as `<TICKER>/<YEAR>/peer_comparison.json` (default `data/peer_comparison`). |
| `POSTGRES_*` or `DATABASE_URL` | Database settings, as in [database setup](YUVIKA_database_docker_setup.md). |

To start a run from the UI, configure `.env` as in the full pipeline guide, start the database with `docker compose up -d`, then run the dashboard:

```bash
.venv/bin/python -m streamlit run app/Home.py --server.address 127.0.0.1
```

The Run Pipeline page can launch jobs directly. It shows the active UI-launched pipeline even if another ticker or year selection is entered, and disables new launches until that process exits. To browse existing PostgreSQL results, set `SEC_UI_DATA_SOURCE=postgres`; the default `auto` mode also prefers completed database results over files.

## 4. Inputs

- Aligned disclosures: `alignments.json` (and `needs_review.json` when present) per company and year pair, as written by the alignment, change-analysis and materiality stages. Full text comes from `disclosures.json`. Without it the side-by-side page shows only the sentences cited as evidence and says so.
- Synthesis output: one JSON file per company, with `report`, `source_catalog`, `financial_metrics`, `financial_trends`, `validation`.
- Peer output: `peer_comparison.json` (schema version 2) from `scripts/compare_peer_disclosures.py`.

## 5. Run and rerun

`streamlit run app/Home.py`, then open the address it prints. On **Run Pipeline**, select a ticker and two or more years. The UI sorts and deduplicates the years, shows the adjacent selected pairs it will compare, and starts the full PostgreSQL pipeline. For more than two years, confirm the extra work before starting. One UI-launched pipeline can run at a time; its progress remains visible even when the form shows another selection. A stopped run can be resumed with the same ticker and years; completed stages are reused by the CLI. The page stays responsive during execution and shows a toast and persistent result when the process ends. The browser must remain open to see the toast; the saved status and log remain available after reloading.

The runner writes `data/pipeline_runs/<ticker>/<years>/status.json`. The UI writes `ui_run.json` and a per-attempt `ui_run_*.log` beside it so preflight errors and process exits are visible even before the runner creates status. A PostgreSQL connection failure shows a reminder to start Docker Desktop, run `docker compose up -d`, and resume. "Loaded to database" appears only after the corresponding save step reports completion. After completion, the overview and explorer use PostgreSQL results in `auto` mode; click "Reload data" if a previously opened page still shows old data. To preview the peer page before real peer output exists, point `SEC_UI_PEER_DIR` at `tests/fixtures/peer_comparison`; the page shows a sample-data banner.

## 6. Final output JSON structure

The report pages do not write pipeline outputs. The Run pipeline page launches the existing runner, which writes its normal database records and local checkpoints. Dashboard exports are:

- Change CSV and JSON: one row per match with `match_id`, `company`, years, `item`, `sections`, `content_category`, `relationship`, `status`, the lexical, semantic and LLM labels, the final `change_label`, `materiality`, scores, `key_change`, reasons, `previous_disclosure_ids`, `current_disclosure_ids`, `sentence_ids`, `source_urls` and (optional) both texts.
- Synthesis downloads: Markdown report, evidence CSV (one row per cited sentence with its finding, sentence ID and SEC URL), the original JSON.
- Peer downloads: Markdown report, CSV (one row per target disclosure and peer, with sentence IDs and URLs), the original JSON.

## 7. Example data and downstream usage

`tests/fixtures/alignments/amd` (alignment files only), `tests/fixtures/synthesis` (the synthesis agent's real AMD 2023-2025 output) and `tests/fixtures/peer_comparison` (made-up wording around real cited sentences, marked `_sample_data`). The record layout is documented in the `normalise` function of `app/sec_ui/data.py`. The chatbot can reuse `data.load_comparison` and `data.filter_records` without Streamlit.

## 8. Validation and limitations

Dashboard data and page tests are in `tests/test_ui_data.py` and `tests/test_ui_pages.py`. The run-page service has isolated tests in `tests/test_ui_pipeline_runs.py`; they mock process launch and never make SEC, LLM, or PostgreSQL requests. The actual full pipeline has not been launched end to end from the UI in these tests. Earlier checks found that AMD 2024-2025 loads 432 matches (387 aligned + 45 needing review) identically from files and PostgreSQL when `SEC_UI_TEST_POSTGRES=1` and a matching database is available.

Limitations:

- "No counterpart found" means the saved comparison found none, not that the topic is absent from the whole filing.
- Change labels, materiality and peer statements are model output. The dashboard shows them as saved and does not re-check them.
- Synthesis "citation check passed" means IDs exist, not that the claims follow from the text.
- The peer page has only been run on made-up sample output, because no real peer output is committed yet.
- The PostgreSQL check used a database without the pgvector extension (the vector tables are not needed by the dashboard); a database created from the full Docker image was not tested here.
- The Run pipeline notification is in-app only; it does not send email or desktop alerts. The UI is intended for local, single-server use and does not yet offer a stop/cancel button.
- Tested on Python 3.13 and Streamlit 1.66. The Python 3.14 setup used by the team was not tested.
- Very large comparisons (Citigroup has about 1,100 matches) load in under a second from files; the first PostgreSQL load of a large pair has not been timed.
