# HTML table metrics for Items 1, 1A and 7

`extract_html_metrics.py` is a separate entry point. It reads the original
filing, classifies table candidates, and exports **untagged cells** from financial
or quantitative tables. `html_table_metrics.py` contains the new classification
and extraction logic. `html_filing_source.py` coordinates source verification
using the API pipeline's existing SEC, metadata and report readers. The API
pipeline keeps its current behavior and output.

Only financial/quantitative table records are included in the output. Narrative
and review tables are omitted; their aggregate counts remain in the final
classification summary.

## Run using an existing API export's cached filing

```bash
python3 extract_html_metrics.py \
  --api-metrics tests/for_table_development/intel/intel_2025_api_metrics_with_values.json \
  --company Intel --year 2025 --offline
```

Here `--api-metrics` only locates the original filing and verifies its SHA-256
against the export's provenance. It does not supply amounts, map concepts, or
merge API and HTML records. No sec-api.io API key or request is needed.

Alternatively, download the original filing from SEC using `SEC_USER_AGENT`:

```bash
python3 extract_html_metrics.py --ticker INTC --company Intel --year 2025
```

The same command works for other issuers, for example:

```bash
python3 extract_html_metrics.py --ticker WFC --company WellsFargo --year 2025
```

Use `--cik 72971` instead of `--ticker WFC` when ticker lookup is unavailable.
Use `--accession 0000072971-26-000133` with either live source option to select a
specific original 10-K. The accession must match the issuer and fiscal year;
10-K/A amendments are excluded. No `SEC_API_KEY` is needed.

Or use `--filing path/to/original.htm`. Input must be the original Inline XBRL
XHTML, not PDF-converted HTML. Item locations, tag detection, fiscal-calendar
evidence and currency codes use the existing source readers. Amounts are parsed
from visible HTML cells. Incorporated reports use the existing verified-report
resolver; offline runs require those reports to be cached.

Fiscal year is verified **after** resolving incorporated reports, using official
`dei:DocumentFiscalYearFocus` facts and their contexts across the verified
document set. For example, Wells Fargo FY2025 keeps this tag in Exhibit 13,
not its primary 10-K. The evidence is saved in `verification.fiscal_year`, with
the document ID and fact locator. Missing or conflicting year evidence still
raises an error; the requested year and filename are not used as substitutes.

## Shared filing and boundary handling

| Edge case | HTML pipeline behavior |
|---|---|
| Ticker/CIK lookup, older filings, amendments or multiple candidate 10-Ks | Uses the same `SecClient` and `discover_sec_filings` as the API pipeline; supports historical submissions and explicit `--accession` |
| SEC throttling/timeouts or temporary server errors | Uses the same bounded retries, request pacing, User-Agent requirement and immutable document cache; HTTP 403 is reported, not bypassed |
| Missing year in the primary document (WFC) or shared contexts (Mays/KFFB) | Reads official DEI facts after the linked reports and shared contexts are resolved |
| API metadata outside `CoverPage` (KFFB) | No provider metadata layout dependency: HTML identity comes from the filing documents |
| Different issuer, year, form or report date | Rejects conflicts across documents, official DEI, SEC entity contexts and the selected SEC record |
| Internal Item indexes/page cross-references (Intel/JPMorgan) | Uses the existing printed-page and section resolver |
| Financial review in a separate report (WFC/FFBC) | Follows the verified same-accession annual report and its named sections |
| External page ranges or prefixed pages (Mays/PEBK) | Uses the referenced report's verified printed labels, never PDF page ordinals |
| No Exhibit 13 hyperlink (IBOC) | Uses the same accession's SEC document index and a uniquely identified HTML EX-13 |
| Split/styled headings and statement inventories (IBCP/KFFB) | Uses the shared TOC, heading and statement-index checks |
| Item 7 needs Item 8 to prove a boundary (IBCP) | Resolves Item 8 as auxiliary evidence, then removes its membership before exporting HTML tables |
| Visible text refers to a hidden tag in another filing document (FFBC) | Follows a unique fact ID across the verified set; local IDs take precedence and ambiguous IDs fail |
| Stale or mismatched saved source | Checks hashes and URL/accession provenance, including sidecars even when `--filing-url` is explicit; rechecks source files before writing |

All report links remain bound to the selected SEC accession. Missing or
ambiguous report boundaries still stop the run; the whole external report is
never assigned to Item 7 as a fallback. Images are still ignored.

Live filing bytes reuse `data/sec_cache/<URL-hash>.html`, with a provenance
sidecar for offline replay. Incorporated reports and SEC indexes use the shared
cache. Only the final HTML metrics JSON is written to the company output folder.
An existing final output is preserved.

The default output is:

```text
tests/for_table_development/intel/intel_2025_html_metrics_with_values.json
```

Use `--items 7` for a subset of the supported Items. Use a different
`--output-dir` to rerun without overwriting an existing result. Only one output
JSON is written; classification details are inside it.

## JSON structure

```text
schema_version: html-metrics-1.0
company, fiscal_year, requested_items
counts
metrics[]
  metric_id, concept, query_name, label, label_source, definition
  period_type, scope, units[], api_groups[], items[]
  value[]
    value, unit, period, dimensions, status, items[]
    source_type, display_text, context_labels[]
    source_labels[]
    extraction_status, issues[]
    normalization, period_evidence
table_classifications[]  ← financial tables only
  table_id, items[], page, title, title_source, source_locator
  classification, reason, evidence, footnotes[]
  exported_values, values_needing_review
verification
  fiscal_year, identity, source
  documents (including report_discovery / boundary_evidence when used)
  cross_document_hidden_references
classification_summary  ← last property in the file
```

- `metrics[] → value[]` resembles the API output, but this is a **different
  schema**. Readers must support null concepts, unknown scope and unresolved
  values. Existing API-only query tools are not automatically compatible.
- `concept`, `query_name` and `definition` are null: no official concept or
  definition is guessed. `metric_id` identifies a table/row/measurement group.
- `label` comes from the filing. Amount and percentage columns are kept in
  separate metric groups. Metrics in different tables are not automatically
  merged, even when their labels or amounts match.
- `dimensions: null` means no verified XBRL dimension mapping. `context_labels`
  preserves headings such as CCG, DCAI, United States and Total. It does not
  imply company-wide scope. The table title remains in `source_labels`.
- `value` is a decimal string in the documented base unit. For example, 52,853
  in USD millions becomes `"52853000000"`; 29% becomes `"0.29"` with unit `pure`;
  35 million square feet becomes `"35000000"` with unit `square_feet`.
- `normalization` records the original headings/symbols and scale exponent.
  Per-share values are not multiplied by a table's “in millions” scale.
  A percent sign printed only beside the first year can apply to other years
  in the same row when their column wording matches and the row has no currency
  markers. `row_percent_source_locators` records that evidence; mixed amount
  and change columns do not inherit the sign. Units-and-year header rows are
  treated as headings, not metric values.
- `period_evidence` records the date heading and, for annual durations, the
  unique supporting source context. Missing/ambiguous periods remain unresolved.
- `status: reported` means a printed amount was normalized. Check
  `extraction_status` too: a known amount can still have an unresolved period
  or label. `status: unresolved` retains a null value and the original display.
- Dashes are not assumed to mean zero or XBRL nil. No missing amounts are
  calculated. Blank spacing cells and date/header cells are not metric values.
- `source_fact_id` is null for HTML values. Page, table, row/column labels,
  document and cell XPath provide traceability instead.

## Classification and counts

| Classification | Meaning | Treatment |
|---|---|---|
| `financial` | A financial or quantitative grid with separate measurements and labels | Export untagged value cells; skip individually tagged cells |
| `narrative` | Prose, a text/header layout, or a nonfinancial roster | Omit table records and text; retain aggregate counts only |
| `review` | Unclear numeric relationships, mixed prose/data, invalid spans, or mixed image/text content | Omit table records and cells; retain aggregate counts only |

The detector considers meaningful rows/columns, standalone measurements,
headings, financial/measurement labels, long prose, footnote markers and image
content. A `<table>` tag or an XBRL tag alone does not establish a financial
table. Outer wrappers are not counted a second time. Image-only tables,
including an image with a short caption, are ignored. A table containing actual
HTML measurements is still checked even if it also contains an image.

`classification_summary` is deliberately written **at the end** of the JSON:

```json
{
  "total_tables": 55,
  "financial": 21,
  "narrative": 34,
  "review": 0,
  "ignored_image_only_tables": 6
}
```

These are the Intel FY2025 candidate-table counts. Six image-only tables on
pages 8, 11, 13, 14, 15 and 16 are ignored; their images have not been interpreted.
They are excluded from `total_tables` and all three classification counts.
`ignored_image_only_by_item` also records their Item memberships.
Pure structural/navigation/
empty tables are excluded before classification and their document-wide counts
are reported separately. Thus this is not a count of every HTML `<table>` tag.
The actual summary also includes per-Item counts, skipped tagged cells,
exported values and value-review issues.

`total_tables` counts candidates inspected, including the omitted tables.
`table_classifications` contains only the 21 financial table records in this
example. All 34 narrative records are omitted. The existing classifier is
unchanged: its `financial` category also includes quantitative tables such as
facility areas.

One table can belong to more than one requested Item. It counts once in the
overall total and once in each applicable per-Item count. Item 1A has no table
candidates in this Intel run; its ordinary narrative paragraphs are outside the
table classifier's scope.

**Table review and value review are separate.** Intel's accepted financial tables
contain five untagged dashes that remain unresolved. Those are separate from the
six ignored image-only tables; no candidate tables need classification review
in this run. Filtering out review tables does not remove values marked
`extraction_status: needs_review` within accepted financial tables. Tagged values repeated elsewhere
in the filing may also appear as untagged HTML disclosures here. Do not sum
repeated disclosures or combine this output with API values blindly.

## Validation scope

The initial real-filing validation uses Intel FY2025, particularly printed pages
21, 23, 25, 30 and 32. It checks segment headers, three-year panels, negative
amounts, percentages, per-share exceptions, cash balances and facility areas.
Synthetic tests cover prose containing tagged/untagged numbers, single-row
tables, hidden-fact references, text-block wrappers, year-shaped amounts,
ambiguous units/periods, malformed spans, image-only tables, overlapping Item
membership and output preservation.

Wells Fargo FY2025 was also run from its cached primary 10-K and verified
Exhibit 13. Classification found 51 financial/quantitative candidates and 5 narrative
candidates; new exports retain only the financial table records. Of 3,717 untagged values, 2,414 still have `needs_review`, mainly
because period headings could not be resolved. Successful fiscal-year
verification does not mean every bank table layout has been resolved.
The regression run leaves Intel FY2025 and Wells Fargo FY2025 metrics, values
and classification counts unchanged. New runs add source/identity/boundary
evidence under `verification`; `metrics[] → value[]` and the final
`classification_summary` retain their structure. Existing output files are not
rewritten by the tests.

The cached regression set covers 14 filings: Intel FY2023–2025, Micron FY2023
and FY2025, plus JPMorgan, WFC, FFBC, J.W. Mays, PEBK, IBOC, IBCP, KFFB and Dauch
FY2025. It exercises the full HTML CLI, checks source identity and requested
Items, and reconciles output counts. These checks use temporary exports and
make no SEC or sec-api.io requests:

```bash
HTML_METRICS_INTEGRATION=1 python3 -m unittest discover -s tests -p 'test_html_filing_regressions.py'
```

Missing local fixtures are reported as skipped. Synthetic source/HTML tests can
be run without the real filing caches:

```bash
python3 -m unittest discover -s tests -p 'test_html*.py'
```

This is a bounded HTML extractor, not a guarantee that all companies' visual
layouts can be reconstructed. Images outside table candidates are not OCR'd,
and table classification does not establish accounting-concept equivalence.
