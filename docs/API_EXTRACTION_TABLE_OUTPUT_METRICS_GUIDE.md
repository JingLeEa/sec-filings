# Guide to the API metrics JSON

This guide describes the current API pipeline output for Items **1, 1A, 7 and 8**.
The schema remains `api-metrics-1.0` through exporter `1.11.0`. Examples use the
[Intel FY2023 output](../tests/for_table_development/intel/intel_2023_api_metrics_with_values.json).

The pipeline names its final file `<company>_<year>_api_metrics_with_values.json`,
for example `intel_2023_api_metrics_with_values.json`. Earlier outputs named
`metrics_with_values.json` use the same JSON structure. The lower-level exporter
still uses that generic name when called directly.

Each company has one folder, with a separate file for each filing year:

```text
tests/for_table_development/intel/
├── intel_2023_api_metrics_with_values.json
├── intel_2024_api_metrics_with_values.json
└── intel_2025_api_metrics_with_values.json
```

Use the same `--company` label across runs. The pipeline preserves existing
files and refuses to overwrite the same company/year filename. Use a different
`--output-dir` for an alternative Item selection for that company and year.

Share this guide together with the JSON when asking a teammate or AI to explain
the data. The JSON supplies the actual company figures; this guide explains
how to interpret them across the selected Items.

## 1. The overall structure

**Company → metrics → values → source locations.**

The output groups data by financial metric. One metric can contain values from
several tables or paragraphs, periods and dimensional contexts. It is not a
reconstruction of each printed table.

```text
<company>_<year>_api_metrics_with_values.json
├── schema_version
├── exporter_version
├── source
├── membership_report
├── requested_items[]
├── company
├── taxonomy_year
├── counts
│   ├── verified_concepts
│   ├── company_wide
│   └── dimension_only
├── notes[]
├── metrics[]
│   ├── concept, query_name, label, definition
│   ├── period_type, scope
│   ├── company_wide_annual_or_year_end_years[]
│   ├── units[], api_groups[], items[]
│   └── value[]
│       ├── value, unit, status
│       ├── period: instant OR startDate + endDate
│       ├── dimensions[]: axis + member
│       ├── decimals / precision (when supplied)
│       ├── items[]
│       └── source_labels[]
│           ├── row_label, column_label, label_method, status
│           ├── page, source_fact_id, source_locator
│           └── document_id, source (for multiple filing documents)
└── verification
    ├── source
    ├── taxonomy_source
    ├── checked_api_entries, included_api_entries, excluded_api_entries
    ├── unique_values, repeated_api_entries_merged
    ├── source_label_status
    └── exclusion_counts
```

## 2. File-level fields

| Field | Meaning |
|---|---|
| `schema_version` | Version of the JSON structure: `api-metrics-1.0`. |
| `exporter_version` | Version of the code that generated this file. |
| `source` | Local path to the original API response or response cache. |
| `membership_report` | `null` in the default pipeline because membership checks run in memory. A separate report path can appear in the lower-level exporter's full-report mode. |
| `requested_items` | Items checked. This does not guarantee that every requested Item contributes values. |
| `company` | Verified company name. |
| `taxonomy_year` | Version of the official US-GAAP metric dictionary, not the year of every value. |
| `counts.verified_concepts` | Number of exported metrics, not tables or individual values. |
| `counts.company_wide` | Number of metrics with at least one value without explicit dimensions. |
| `counts.dimension_only` | Number of metrics whose values all have dimensions. |
| `notes` | Extraction rules and limitations. |
| `metrics` | List of metrics and their actual values. |
| `verification` | Provenance, inclusion/exclusion counts and label-resolution counts. |

The checked Intel FY2023 file has **357 metrics**: 283 `company_wide` and
74 `dimension_only`. These counts describe that file, not every company.

## 3. Each metric: metrics[]

| Field | Meaning |
|---|---|
| `concept` | Full XBRL identifier, such as `us-gaap:NetIncomeLoss`. |
| `query_name` | Concept name without its namespace, such as `NetIncomeLoss`. |
| `label` | Official readable metric name. It may differ from the filing's row label. |
| `definition` | Official taxonomy definition, not an AI-generated explanation. Missing definitions remain `null`. |
| `period_type` | Official concept type: `instant` for a date or `duration` for a period. |
| `scope` | Classification of the available values; see below. |
| `company_wide_annual_or_year_end_years` | Fiscal years with qualifying undimensioned annual/year-end values. This does not list every period in `value[]`. |
| `units` | Units present in undimensioned values. This can be empty for a metric that still has dimensional values. |
| `api_groups` | Original API groups containing the metric. They are not guaranteed to be original table names or table boundaries. |
| `items` | Combined Item memberships across this metric's values. |
| `value` | Array of actual value records. |

The exact scope rules are:

- **`company_wide`:** at least one exported value has `dimensions: []`.
  The same metric may also contain dimensional values.
- **`dimension_only`:** every exported value has at least one dimension.
  This does **not** mean the metric is missing or untagged.

A dimension identifies a category: for example, a business segment, geography,
equity component or debt instrument. Each record's dimensions matter.

**An empty dimension array does not automatically mean “Total.”** Read the
concept, definition and source labels. A concept specifically about common stock
still concerns common stock even if it has no dimensions.

## 4. Each value: metrics[].value[]

The actual amount is stored at **`metrics[i].value[j].value`**.
The first `value` is an array; the second is one amount.

| Field | Meaning |
|---|---|
| `value` | API amount, normally an exact decimal string. A nil fact uses `null`. |
| `unit` | Unit for this record, such as `USD`, shares, `pure` or USD per share. |
| `period.instant` | A balance **as of** a date, such as cash on a fiscal year-end date. |
| `period.startDate`, `period.endDate` | Start and end of a reporting period. It can be annual, quarterly or another duration. |
| `dimensions` | Explicit category labels. `[]` means no explicit dimensions. |
| `dimensions[].axis` | Category type, such as business segment. |
| `dimensions[].member` | Selected category, such as a particular segment. |
| `status` | `reported`: an amount is supplied. `nil`: the original fact explicitly supplies no value. |
| `decimals` | Accuracy, not scaling. For example, `-6` describes accuracy to millions. |
| `precision` | Alternative accuracy information in significant digits, when supplied. |
| `items` | Items where this particular value was verified. It may belong to several Items. |
| `source_labels` | Original filing occurrences supporting the value. |

Amounts are already in their stated XBRL unit. For example,
`"value": "43000000", "unit": "USD"` means **USD 43 million**.
Do not multiply by `decimals` or apply a table's “in millions” heading again.
Use Python `Decimal` for exact arithmetic.

A reported `"0"` is zero; `null` with `status: "nil"` is not zero.
A filename's fiscal year does not limit its records to that year: comparative
years may also be included. Empty metric-level year/unit lists do not mean its
`value[]` is empty.

## 5. Source labels: where the value appeared

| Field | Meaning |
|---|---|
| `row_label` | Row wording associated with the occurrence; can be `null`. |
| `column_label` | Column wording associated with the occurrence; can be `null`. |
| `label_method` | How labels were associated, such as `table_layout` or `html_headers`; can be `null`. |
| `status` | Label-resolution result, independent of the value's `reported`/`nil` status. |
| `page` | Printed page label in the source document, when available; not a PDF viewer page index. |
| `source_fact_id` | Original XBRL fact identifier, when present. It is not an amount or a globally unique identifier across filings. |
| `source_locator` | XPath locating the source occurrence or containing table cell. |
| `document_id` | Present for multiple source documents, such as `primary` or `report1`. |
| `source` | Local source document path, included for multiple source documents. |

Label statuses include:

| Status | Meaning |
|---|---|
| `resolved` | Both row and column labels were found. |
| `partial` | Only one of those labels was found. |
| `unresolved` | Neither label was resolved for the table cell. |
| `not_in_table` | The occurrence is outside a table, for example in prose. |
| `source_not_found` | The label lookup could not locate the source occurrence. |

A partial or missing label does not, by itself, mean the numeric value is missing.
Fact matching and label recovery are separate checks. Do not invent a “Total”
label when the source label is absent.

## 6. Actual Intel example

This excerpt retains one value from Intel's FY2023 `NetIncomeLoss` metric.
Other metric fields and value records are omitted for readability.

```json
{
  "concept": "us-gaap:NetIncomeLoss",
  "label": "Net Income (Loss) Attributable to Parent",
  "scope": "company_wide",
  "items": ["8"],
  "value": [
    {
      "value": "19868000000",
      "unit": "USD",
      "period": {
        "startDate": "2020-12-27",
        "endDate": "2021-12-25"
      },
      "dimensions": [],
      "status": "reported",
      "decimals": "-6",
      "items": ["8"],
      "source_labels": [
        {
          "row_label": "Net income attributable to Intel",
          "column_label": "Dec 25, 2021",
          "label_method": "table_layout",
          "status": "resolved",
          "page": "74",
          "source_fact_id": "f-88",
          "source_locator": "/*/*[2]/*[1205]/*/*[18]/*[12]"
        },
        {
          "row_label": "Net income attributable to Intel",
          "column_label": "Dec 25, 2021",
          "label_method": "table_layout",
          "status": "resolved",
          "page": "88",
          "source_fact_id": "f-577",
          "source_locator": "/*/*[2]/ix:nonNumeric[21]/ix:nonNumeric/*/*/*[5]/*[12]"
        }
      ]
    }
  ]
}
```

This is **USD 19.868 billion of net income attributable to the parent** for
27 December 2020 through 25 December 2021, reported as a comparative figure in
the FY2023 filing. The record is verified in Item 8 and has no explicit dimensions.

The same value appears on printed pages **74 and 88**. It is stored once with
two source occurrences; those occurrences should not be added together.

## 7. Verification and the latest metadata handling

| Field inside `verification` | Meaning |
|---|---|
| `source` | API request, input paths, content hashes and filing-binding evidence. May also describe incorporated reports and their verified sections. |
| `taxonomy_source` | Official taxonomy version, package/source locations and hashes. |
| `checked_api_entries` | Number of API entries accounted for by the export audit. Entries are not tables. |
| `included_api_entries` | Entries accepted for export before repeated records are merged. |
| `excluded_api_entries` | Entries not exported. |
| `unique_values` | Number of records across all exported `value[]` arrays after merging. |
| `repeated_api_entries_merged` | Included repeated entries consolidated into existing value records. |
| `source_label_status` | Counts of label-resolution statuses across source occurrences. These are not counts of unique amounts. |
| `exclusion_counts` | Counts by exclusion reason, such as unverified Item membership or a custom/ambiguous concept namespace. |

The latest code accepts identity metadata inside API `CoverPage` or at the
response root. Missing fields can use official DEI metadata from the bound
original filing. Issuer CIK evidence can come from filing entity contexts or
the verified SEC filing URL. Conflicts still stop extraction; command-line
company/year labels do not replace source evidence.

When nonstandard metadata sources are used,
**`verification.source.identity_metadata`** records each field's value and
method, with API paths or filing evidence as appropriate. It may be absent from
an ordinary `CoverPage` export such as this Intel example.

This metadata handling does not change the metric/value structure. The original
API response remains unchanged. Cached downloads and identity-validation
diagnostics live separately under `data/sec_cache/`. Each run writes only
`<company>_<year>_api_metrics_with_values.json` into the company's output folder.

### Financial appendices referenced through Item 15

The section resolver also follows an explicit internal chain from Item 8 to
Item 15 and then to a financial statement index on a cited printed page.
MEI FY2025 uses this arrangement: Item 15 cites page F-1, whose links lead to
statements on F-5 through F-9 and notes beginning on F-10. These pages physically
follow Item 16 and the signatures, but the verified reference assigns the
statements and notes to Item 8. The range stops before Schedule II on F-41.

This requires a unique index page, internal links whose destinations match the
printed pages and nearby titles, ordered statement/notes targets, continuous
page evidence, and an explicit schedule/exhibit boundary after the notes.
Missing or conflicting evidence raises an error. It does not include an entire
appendix just because it follows Item 16. The membership report records the
method as `referenced_financial_index`; the one-file metrics output continues
to store per-value Item memberships and source locations in its existing schema.

An offline regression using the cached MEI filing and API response is available:

```bash
SEC_MEI_API_INTEGRATION=1 PYTHONPATH=tests:src python3 -m unittest test_api_referenced_financial_index
```

### Older financial statement layouts (exporter 1.11.0)

The same metric/value schema also supports these verified Item 8 layouts:

| Layout | Boundary verification |
|---|---|
| Extra date links in a statement-index row (NVIDIA FY2023–2024) | The printed-page link and statement-title link agree. Other links contain only dates, including dates split across adjacent anchors. The destination title/page, ordered statements, notes and closing schedule all need verification. |
| Final note uses a company-specific TextBlock (Microchip FY2023–2024) | A visible styled `Note <number>. <title>` contains the matching tagged title; the complete continuation chain closes the note. Unrelated trailing content still causes an error. This identifies the notes boundary; it does not export custom concepts as official US-GAAP metrics. |
| Units appear in the heading (ON FY2023) | A recognized trailing units parenthesis is ignored only when comparing the statement title. Source labels keep their original wording. |
| Index abbreviates “Consolidated Statements of Comprehensive Income” (Diodes FY2023–2024) | The exact link and printed page must lead to one unambiguous comprehensive-income/loss heading. Arbitrary approximate title matches are not accepted. |

All amounts still come from the original API response. The exporter version
changes, but periods, dimensions, values and source labels for previously
supported filings should remain identical. Regression exports are written to
a separate validation folder so the earlier outputs remain available.

### Banking filing boundaries (exporter 1.9.0)

The API metric/value schema stays `api-metrics-1.0`. The shared section reader
also recognizes these verified layouts:

| Filing layout | Evidence used |
|---|---|
| Item headings inside contents/heading tables (BAC, COF) | The first populated row must be a standalone styled Item title. Forward TOC links are excluded; an immediate empty self-anchor is allowed. Numerical grids cannot supply these headings. |
| Numbered Form 10-K cross-reference index (C) | An explicit index heading, Item-number rows and printed-page ranges, including comma-separated ranges wrapped across rows. Overlapping Item memberships are retained. |
| Annual report named before the cited pages (USB) | An affirmative incorporation statement, the same-accession Exhibit 13, and uniquely verified printed pages. Positioned footer tables and publisher abbreviations can supply page labels. |
| Hierarchical contents with links in the page column (BNY) | Bold TOC groups establish containment; internal fragment links, matching nearby titles and continuous printed pages establish section boundaries. |
| Item 7 explicitly incorporates MD&A and individual notes (BNY) | The MD&A group and only the named notes receive Item 7 membership. Two notes sharing a page still use distinct fragment boundaries. |
| Item 8 delegates to an Item 15 annual-report page inventory (BNY) | A unique statement/notes inventory explicitly identifying report pages; page ranges must be ordered, non-overlapping and verified in the report. |

Amounts continue to come from the API. These rules add filing-location evidence;
they do not invent facts or automatically assign an entire annual report to an
Item. Ambiguous links, missing pages and unverified titles still stop extraction.
Partial/unresolved source labels remain explicitly flagged.

An explicit cross-reference heading can also introduce Intel's existing
`Item 1.` / `Page` / `Pages` layout. If the bare-number parser does not find a
usable index, the existing Item-prefixed parser still runs, preserving wrapped
business-description rows and their page references.

Run the synthetic boundary checks with:

```bash
PYTHONPATH=tests:src python3 -m unittest test_api_banking_boundaries
```

With the five original bank filings and incorporated reports already cached:

```bash
BANK_API_INTEGRATION=1 PYTHONPATH=tests:src python3 -m unittest test_api_banking_boundaries
```

For Bank of New York Mellon, use the current SEC ticker `BNY` or `--cik 1390777`.
Its FY2025 document filename and historical text still use `BK`.

## 8. Interpretation rules and a prompt for teammates

- Definitions and labels come from the official taxonomy; filing row/column
  wording comes from `source_labels`. They serve different purposes.
- Financial amounts come from the API. The filing supplies location, context
  and label evidence; the pipeline does not calculate replacement amounts.
- Only verified official numeric US-GAAP metrics are included. A metric absent
  from this JSON is not proof that the company never disclosed it.
- Identical records can be merged across API groups. Different periods, units,
  dimensions, values or accuracy remain separate records.
- Do not sum overlapping dimensional views, reinterpret a dimensional equity
  component as a different concept, or infer a total from empty dimensions.
- An original table name is not guaranteed by this schema. API groups and
  taxonomy labels are not substitutes for a verified printed table title.

Give an AI this guide and the JSON with the following prompt:

```text
Explain the attached API metrics JSON using the accompanying guide.
Use plain language and actual records from the JSON.

Identify the company, requested Items and metric counts. For any amount, include
its concept, unit, exact period, dimensions, Item membership and available source
labels/pages. Distinguish metric-level scope from each value's dimensions.

Do not assume an undimensioned value is a total, a duration is annual, an API
group is a printed table, decimals is a multiplier, or nil is zero. Do not add
repeated source occurrences or overlapping dimensional views together.

Explain missing/partial labels separately from missing numeric values. State
when a requested metric is excluded, absent or ambiguous instead of inventing
a number or mapping. If only the guide is supplied, explain the format and
request the JSON before reporting additional company figures.
```

For commands and pipeline behavior, see [README.md](../README.md).
Older `item_8_metrics_with_values.json` files share the core metric/value fields
but may lack `items`, `source_labels` or `verification`, and their `source`
may point to a named API intermediate file. Check the actual file before
applying the current schema's optional fields.
