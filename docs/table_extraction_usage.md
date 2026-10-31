# Table Extraction Usage

Run commands from the repository root after installing the dependencies. Set `SEC_USER_AGENT` for SEC requests and `SEC_API_KEY` for sec-api.io requests.

## Item boundaries and printed pages

The API/XBRL and HTML table pipelines call the existing text preprocessor's
section selector in `extraction/sec_10k_extractor.py`. A table-side adapter maps
the selected section back to start/end positions in the original filing HTML.
It uses the same closing Items and longest-section selection for repeated Item
headings. Text preprocessing code and outputs are unchanged.

Tables and facts before the first narrative paragraph or after the last one
remain inside the full heading-to-heading span. Printed page labels are still
verified separately from footers; one printed page can contain several Items.
Explicit incorporated sections, page references and same-accession reports
retain their existing verification and take priority over physical membership.
For sections with no retained narrative, the verified table-heading fallback
remains available. Membership audit scopes record shared ranges under
`item_boundaries`, with `method: "text_preprocessing"` and original HTML positions.
Existing exports are preserved; choose a new output directory to generate new
results with the shared section selection.

## Export existing XBRL data from Docker

Use the dedicated exporter to read data that is already in PostgreSQL:

```bash
docker compose up -d
python3 scripts/export_xbrl_from_postgres.py --list
python3 scripts/export_xbrl_from_postgres.py --company Intel --year 2025
python3 scripts/export_xbrl_from_postgres.py --company Intel --year 2025 --format csv
```

No SEC User-Agent, API key, original JSON file or source cache is needed. Connection
settings use the loader's `DATABASE_URL` or `POSTGRES_*` from the shell/repository
`.env`, defaulting to the local Docker database. The exporter uses a read-only
transaction and does not create tables or modify stored data.

`--company` is the label used when loading, such as `Intel`, not necessarily the
ticker. `--year` selects the source filing year; all comparative periods remain.
If several snapshots match, the command requires an explicit selection:

```bash
python3 scripts/export_xbrl_from_postgres.py --list --company Intel --year 2025
python3 scripts/export_xbrl_from_postgres.py --extraction-id 123 --format csv --output data/intel_values.csv
```

By default, files go to `data/table_output/<company>/db_exports/` and include the
filing year and extraction ID in the filename. Existing files are never overwritten;
use `--output PATH` for another destination.

JSON reconstructs the yearly `api-metrics-1.0` structure, including metadata,
`metrics[]`, `value[]`, source evidence and unknown properties. Amounts use their
stored decimal text; nil, zero, dimensions, accuracy types and array order are
preserved. This reconstructs stored records, not the original file bytes: object
key order/formatting can differ, and optional source fields may appear as null.
The original verification metadata is retained; source caches are not rechecked.

CSV contains one row per value with its concept, label, period, unit, status and
snapshot identity. `dimensions`, `items` and `source_labels` are JSON inside cells,
so several source occurrences do not multiply financial amounts. Accuracy cells
also use JSON: blank means absent, `null` means explicitly null, and `"-6"` retains
a string type. Source row/column labels stay in `source_labels`; the database
does not store printed table layouts. JSON remains the full-fidelity stored export.

## HTML 10-K tables to nested JSON

For a separate export of **untagged financial/quantitative HTML cells in Items
1, 1A and 7**, run:

```bash
python3 scripts/extract_html_metrics.py --ticker WFC --company WellsFargo --year 2025
```

This uses `SEC_USER_AGENT` and writes only
`data/table_output/wellsfargo/wellsfargo_2025_html_metrics_with_values.json`.
It shares the API pipeline's SEC selection and verified incorporated-report
handling, supports `--cik` and `--accession`, and does not call sec-api.io.
Only financial/quantitative table records are exported. Narrative/review table
records are omitted; their counts remain in the classification summary at the
end of the JSON. Existing exports are preserved.
See [HTML_METRICS_GUIDE.md](HTML_METRICS_GUIDE.md) for source checks, edge-case
coverage, offline inputs and the distinction between filing verification and
unresolved table values.

Extract numerical tables from **one selected Item** of two full 10-K HTML filings.
Nested JSON is the default output. Main extractor version 1.3.0 adds verified labels for unlabelled footer totals and explicit nulls for missing percentage displays. The existing CSV comparison remains available with `--output-format csv` or `--output-format both`.

It runs locally using Python 3.10+ and `lxml`. No LLM, API key, pandas, browser automation, or PDF parsing is involved.

## One command: SEC API to your annotation TSV

Use the command-line entry points under `scripts/`. Install the dependencies once with `python -m pip install -r requirements.txt`, then run only the exporter:

```bash
python scripts/export_table_annotations.py \
  --ticker MU --company Micron \
  --previous-year 2024 --current-year 2025 --item 7 \
  --user-agent "Your name your-email@example.com" \
  --output-dir data/table_output/item7_all_tables_annotations
```
or the command below if you wish to get result from specify table

```bash
python scripts/export_table_annotations.py \
  --ticker MU --company Micron \
  --previous-year 2024 --current-year 2025 --item 7 \
  --table "Consolidated Results" \
  --user-agent "Your name your-email@example.com" \
  --output-dir data/table_output/consolidated_results_annotations
```

Replace the contact details with your own. If `--output-dir` is omitted, table annotation files are written under `data/table_output/table_annotation_export/`.

The output folder contains:

| File | Purpose |
|---|---|
| `result.json` | The selected table from each filing, plus the extractor's provenance. |
| `table_annotations.tsv` | Your 17 columns with a header and one complete table-pair annotation row. |
| `paste_into_sheets.tsv` | The same annotation row without a header, using fully quoted TSV fields. |
| `paste_into_sheets.html` | A local copy helper that supplies a 17-cell HTML table and a quoted-text fallback to the clipboard. |

Open **`paste_into_sheets.html` in your browser**, click **Copy row**, single-click column **A** of an empty annotation row in Google Sheets, then paste normally with **Cmd+V / Ctrl+V**.

## Run the API metrics pipeline

To extract **FY2023, FY2024 and FY2025** together, enter the company ticker once:

```bash
python3 scripts/run_api_metrics_batch.py --ticker INTC --company Intel
```

No year or output path is required. `--company` is an optional folder/filename
label; `--ticker INTC` alone uses the `intc` folder. A CIK can be supplied instead
of a ticker. The batch uses the existing `SEC_USER_AGENT` and `SEC_API_KEY`
environment variables and the same API extraction pipeline for each year.

Start PostgreSQL first with `docker compose up -d`. By default each yearly export
is also saved to the Docker database. Missing XBRL tables are installed automatically.
Connection settings use `DATABASE_URL` or `POSTGRES_*` from the shell/repository
`.env`; see [XBRL database schema](XBRL_DATABASE_SCHEMA.md). Add `--no-db` for
file-only extraction.

With `--company Intel`, the three outputs are:

```text
data/table_output/intel/intel_2023_api_metrics_with_values.json
data/table_output/intel/intel_2024_api_metrics_with_values.json
data/table_output/intel/intel_2025_api_metrics_with_values.json
```

Years run sequentially. Existing regular output files are kept and imported into
PostgreSQL after checking their structure and original cached sources, without
new API calls. With `--no-db`, existing exports are skipped without revalidation.
A failed extraction or database save is reported and later years are still
attempted. The command exits nonzero if any attempted year fails, making a
partial run visible. Rerunning creates missing outputs and retries database saves.
The JSON is kept if the database save fails. To create
fresh exports, select a new `--output-dir`. The console shows a per-year summary;
no batch report or other extra output JSON is created. Optional `--years 2024 2025`
overrides the default years, and `--items 7 8` narrows the requested Items.

To also combine the yearly files into **one JSON grouped by `concept`**, add
`--merge`:

```bash
python3 scripts/run_api_metrics_batch.py --ticker INTC --company Intel --merge
```

If the three yearly outputs already exist, merge them directly without any
SEC or sec-api.io calls:

```bash
python3 scripts/merge_api_metrics.py --company Intel
```

The merged file is
`data/table_output/intel/intel_2023_2025_merged_api_metrics_with_values.json`.
The original yearly files are preserved. Existing merged files are also
protected; the batch keeps an existing merged file without revalidation. Use the
standalone merger's `--output path/to/new.json` to write a new copy. The database
stores original yearly records; the merged JSON remains a derived output.
The merger requires every requested year and matching issuer/Item selection;
an incomplete batch does not produce a merged result.

The merged schema is `api-metrics-merged-1.0`. It keeps `metrics[] → value[]`,
with one entry per exact `concept`. Each value gains `source_filing_year`,
which links to `filings[]` provenance and the metric's `metadata_by_filing`.
Definitions and labels at the metric level use the latest filing containing
that concept; all original metadata versions remain available. Counts are
recomputed across the combined concept set.

For the same **concept + full period + unit + dimensions**, the latest source
filing year containing that context wins: older records with different amounts
are removed from the merged output. Dimension order is ignored. Identical
comparative amounts remain separate with their original provenance. Nil and
zero are different; accuracy (`decimals`/`precision`) does not change the comparison.
If the latest filing itself has multiple amounts for one context, they are kept
and counted in `verification.latest_filing_contexts_with_multiple_amounts`.
`verification.values_removed` records how many older conflicting records were
dropped. This is a recency policy, not a guarantee that newer tagging is correct.
Original yearly files and all per-filing metric metadata remain intact.
2023–2025 refers to **source filing years**, selected from the filenames, so
comparative value periods such as 2021 or 2022 remain in the merged data.

With `SEC_USER_AGENT` and your sec-api.io `SEC_API_KEY` set, run:

```bash
python3 scripts/run_api_metrics_pipeline.py --ticker MU --company Micron --year 2023
```

This checks API facts against Items **1, 1A, 7 and 8**, adds official US-GAAP
labels and definitions, and writes **only one output file per run** by default:

```text
data/table_output/micron/micron_2023_api_metrics_with_values.json
```

The filename is `<company>_<year>_api_metrics_with_values.json`, using the
lowercase output label supplied by `--company` (or the ticker/CIK/input stem
when omitted). For Intel FY2023 it is `intel_2023_api_metrics_with_values.json`.
All years for a company share `data/table_output/<company>/`; the year
stays in each filename. Use the same `--company` label across runs to keep them
together. The JSON structure is unchanged. Existing files are preserved, and a
run targeting an existing filename imports that file into PostgreSQL without
downloads or API calls. With `--no-db`, an existing filename remains an error.

Membership checks and export validation still run in memory. The existing
`metrics[]` and `value[]` structure, dimensions and source labels are retained.
A compact `verification` object in the final JSON stores source hashes,
taxonomy provenance and inclusion/exclusion counts. `membership_report` is
`null` because no separate report is saved. The runner does not create a
catalogue file, membership report, audit file or `pipeline_run.json`.

For the JSON structure, field meanings and an annotated Intel example, see
[the metrics JSON guide](API_EXTRACTION_TABLE_OUTPUT_METRICS_GUIDE.md). Share it with the output JSON
when asking a teammate or AI to interpret the data.

Downloads are reused from the internal `data/sec_cache/` cache (the filing/API
pair is under `api_metrics/`), and taxonomy packages use `data/taxonomy_cache/`.
These caches keep repeat runs from needing another paid API request. The API
key is sent only in the Authorization header, never saved in output or cache.
Use `--items 7 8` to select a subset. `--output-dir` can override the default
folder, but is not required. To save another Item selection for the same company
and year, use a different `--output-dir` because the filename is the same.
Existing results are not overwritten or deleted.

Saved inputs can be reused without an API call:

```bash
python3 scripts/run_api_metrics_pipeline.py \
  --filing data/source_cache/intc-20231230.htm \
  --xbrl-json data/table_output/intel_2023_item_8_api_check/sec_api_xbrl.json \
  --company Intel --year 2023 --offline
```

`--offline` requires the original filing/API provenance and the matching
official taxonomy package already cached. The lower-level `export_api_metrics.py`
retains its full-report mode; the runner selects its `--metrics-only` mode
automatically, so no extra flag is needed in the commands above.

If no verified numeric US-GAAP metrics remain after filtering, the run now
returns an error with exclusion counts and writes no output. A requested Item
can legitimately have no eligible numeric facts; zero results require review
instead of a misleading “Pipeline complete” message.

Same-filing financial appendices after Item 16 are supported when Item 8
explicitly cites their printed page range or the Item 15 statement index.
Index links, statement titles, unique continuous printed pages and the end of
the notes are verified before assigning facts to Item 8. A later schedule is
included only when Item 8 explicitly incorporates it and its index destination
and ending are verified; unrelated exhibits remain outside. A nested
“Item 1. Financial Statements” heading in such an appendix does not replace
the genuine Item 1 Business section. Empty named `href="#"` anchors inside
body headings are accepted; TOC navigation links are still excluded.

Older statement indexes may put unrelated internal links on the date wording
(NVIDIA FY2023–2024). If the usual index reader cannot resolve the range, the
page-number and statement-title links must agree, and the destination title,
printed page, statement order and closing schedule are verified separately.
Only date-only link fragments can be ignored. Statement headings may include
a recognized units suffix (ON FY2023); an abbreviated comprehensive-income
index title requires a unique matching heading at its linked page (Diodes).
A company-specific final note text block requires a styled, numbered note
heading and a complete continuation chain with no unexplained trailing content
(Microchip FY2023–2024). These rules change section verification, not the
`metrics[]` / `value[]` schema or the API amounts.

Run the boundary regression checks with:

```bash
PYTHONPATH=tests:src python3 -m unittest test_api_older_filing_layouts test_api_primary_appendix
```

Identity metadata can appear inside the API's `CoverPage` group or directly at
the response root (as in KFFB FY2025). Both layouts are accepted; duplicate
metadata must agree. Missing fields are read from official DEI tags in the
source-bound original filing, with SEC entity contexts or the verified filing
URL supplying the issuer CIK when needed. A URL/accession and source hash must
bind saved inputs before this fallback is used. `--year` and `--company` never
replace missing source evidence. Invalid or conflicting metadata still fails.
Nonstandard metadata sources are recorded per field under
`verification.source.identity_metadata`; original API JSON and financial amounts
are preserved. Contexts missing from a primary document are resolved from its
verified incorporated report during fact matching.

Live downloads are cached before identity validation. A rejected identity
response remains in `data/sec_cache/api_metrics/<request-hash>/sec_api_xbrl.json`,
with its reason in `identity_validation.json`. Retries revalidate that response
without another paid request. These cache files are not final metric exports;
each run still writes only `<company>_<year>_api_metrics_with_values.json`.

For filings such as Wells Fargo's, the API pipeline follows the primary 10-K's
explicit link to an HTML Exhibit 13 annual/financial report in the same SEC
accession. It resolves the named incorporated sections using that report's
table of contents or headings, then checks API facts against both documents.
Explicit shareholder-report page references are also supported, such as
J.W. Mays' FY2025 Item 8 (report pages 3–21) and Item 7 (pages 22–26).
Exhibit footnote markers such as `13*` are accepted. Page numbers must be
verified printed labels in the linked report; missing, duplicate, out-of-order
or discontinuous page boundaries stop the run. The ranges are read from the
filing rather than configured per company.
Appendix page labels and parenthesized exhibit numbers are supported too:
PEBK's FY2025 `Exhibit (13)` places Item 7 on A-4–A-19 and Item 8 on A-20–A-62,
with page labels inside footer tables. When introductory wording names a wider
range, the explicit incorporation sentence determines the included pages;
conflicting incorporation clauses still stop the run. The decoder also supports
the report's Registry 3 `ixt:zerodash` tags, so verified reported zeros are retained.
All exhibit indexes are searched, accepting labels such as `Exhibit 13—` and
`(13)**`. If the annual-report hyperlink is absent, the API pipeline consults
the SEC document list for the same accession and requires one HTML document
typed `EX-13` (or `EX-13.n`) and the exact selected primary 10-K. SEC viewer
links are resolved to the original document. Ambiguous or invalid exhibit
links still stop extraction. The fallback records the document-list URL,
hash and selected row under `verification.source.documents.report1.report_discovery`;
the document list is cached under `data/sec_cache/` for offline reuse.
An Item may reference its Annual Report without repeating “Exhibit 13” when
the filing's exhibit index identifies that report. Distinct affirmative page
clauses are combined without filling gaps: IBOC FY2025 incorporates Item 7
pages 2–23 and Item 8 pages 27–78 plus 79–80. Unequal overlapping page claims
remain ambiguous. Page inclusion does not create API facts for untagged tables.
Unquoted Exhibit 13 references are supported as well. For example, FFBC names
its review section without quotation marks and lists the individual financial
statements in Item 15. The resolver checks those names against standalone report
headings, repeated running headings and verified printed pages, and records the
Item 15 evidence. Missing or ambiguous section boundaries stop extraction
instead of silently treating the short incorporation paragraph as the whole Item.
For reports with styled text instead of HTML heading tags, a fallback verifies
the referenced titles against a unique printed-page contents table. It joins
adjacent centered bold title fragments on the same page and checks every page
through the next verified section boundary. Item 8 can supply its statement
inventory in a text-only table; that inventory identifies the individual
statements within the report's financial-statements section. For IBCP FY2025
(Independent Bank Corporation), this resolves MD&A on pages 40–61 and the
incorporated Item 8 reports, statements, notes and quarterly data on pages
62–146. The final section may end at the last printed footer only when no
visible content follows it. A specifically referenced heading can open an
XBRL text block; ordinary headings inside notes cannot supply arbitrary boundaries.
The output retains TOC, title, end-boundary and statement-inventory evidence
under `verification.source.documents.report1.incorporated_sections`.
Explicit Item 8 references to an Item 15 statement list can also follow the
list's direct Exhibit 13 fragment links. Each linked statement title and printed
page must match. Linked report contents entries locate split headings without
requiring HTML heading tags. KFFB FY2025 uses these links for Item 7 (pages 4–24)
and Item 8 (selected data on pages 2–3, followed by reports/statements/notes on
pages 25–68). The final notes boundary follows the last note's complete XBRL
text-block continuation chain; later visible facts or unresolved references
stop the run rather than silently extending the notes into employee material.
An explicit reference to another Item in the primary filing is also retained;
overlapping Item memberships can appear on the same value.

The API section reader also handles standalone Item titles inside heading tables
(BAC/COF), numbered cross-reference indexes with wrapped page ranges (Citigroup),
and annual-report page references whose printed labels sit inside positioned
footer tables (U.S. Bancorp). Hierarchical report contents can use page-column
fragment links: BNY's Item 7 resolves MD&A plus its explicitly named notes, while
Item 8 follows the Item 15 statement/page inventory. These paths still require
verified titles, page labels and section boundaries. The JSON schema is unchanged.
Use ticker `BNY` or `--cik 1390777` for Bank of New York Mellon; `BK` appears in
the historical FY2025 filing. See
[API_EXTRACTION_TABLE_OUTPUT_METRICS_GUIDE.md](API_EXTRACTION_TABLE_OUTPUT_METRICS_GUIDE.md)
for the boundary rules and cached regression commands.

Missing XBRL contexts and units can be shared between the verified filing
documents. Facts, IDs, source locations and labels remain document-specific.
For these filings, each `source_labels[]` entry includes `document_id` and
`source`; `verification.source.documents` records each document's SEC URL,
content hash and incorporated section ranges (or `incorporated_pages` for
explicit page references). Amounts still come only from
the original API response. Each run still writes only
`<company>_<year>_api_metrics_with_values.json`.

Linked reports are downloaded automatically using `SEC_USER_AGENT` and cached
under `data/sec_cache/incorporated_reports/`. `--offline` reuses that cache or
a same-folder report with a matching `*-source.json` / `*.htm.source.json`
URL/hash sidecar. A missing, ambiguous, untagged or non-HTML report stops the
run; the code does not guess section boundaries or follow unrelated exhibits.
