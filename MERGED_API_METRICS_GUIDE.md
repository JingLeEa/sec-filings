# Guide to the merged API metrics JSON

This guide explains the output of [merge_api_metrics.py](merge_api_metrics.py),
using the [merged Intel FY2023–FY2025 file](tests/for_table_development/intel/intel_2023_2025_merged_api_metrics_with_values.json).
It describes schema `api-metrics-merged-1.0`, merger version `1.1.0`.
For the original yearly format, see the [single-year API metrics guide](API_EXTRACTION_TABLE_OUTPUT_METRICS_GUIDE.md).

**Company → metrics grouped by concept → values from multiple filings.**
The JSON stores financial facts and their source information. It does not
reconstruct the layout or boundaries of individual printed tables.

Merged files use ASCII-only JSON with Unicode escapes (for example, `\u00a0`
for a non-breaking space and `\u2014` for an em dash). This avoids editor warnings
about ambiguous or invisible Unicode characters. A JSON parser restores the exact
original source text; labels, metadata and financial values are unchanged.

## 1. Overall structure

```text
intel_2023_2025_merged_api_metrics_with_values.json
├── schema_version, merger_version
├── company, cik
├── filing_years[], taxonomy_years[], requested_items[]
├── counts
│   ├── verified_concepts
│   ├── company_wide
│   ├── dimension_only
│   └── value_records
├── notes[]
├── filings[]                              One entry per original yearly file
│   ├── filing_year
│   ├── metrics_file, metrics_file_sha256
│   └── All original file-level fields     Includes original verification; excludes metrics[]
├── metrics[]                              One entry per exact concept ID
│   ├── concept, query_name, label, definition
│   ├── period_type, scope
│   ├── company_wide_annual_or_year_end_years[]
│   ├── units[], api_groups[], items[]
│   ├── metadata_source_filing_year
│   ├── source_filing_years[]
│   ├── metadata_by_filing[]
│   │   ├── source_filing_year, taxonomy_year
│   │   └── metadata                       Original metric fields, excluding value[]
│   └── value[]
│       ├── value, unit, status
│       ├── period                         instant OR startDate + endDate
│       ├── dimensions[]
│       ├── decimals / precision           When supplied
│       ├── items[]
│       ├── source_labels[]
│       └── source_filing_year
└── verification                           Checks of the merge itself
```

## 2. File-level fields

| Field | Meaning |
|---|---|
| `schema_version` | Structure version: `api-metrics-merged-1.0`. |
| `merger_version` | Version of the merging code. Original exporter versions remain in `filings[]`. |
| `company` | Company name from the latest input filing. |
| `cik` | SEC company identifier when available; otherwise `null`. |
| `filing_years` | Fiscal years of the input filings, e.g. `[2023, 2024, 2025]`. These are selected input years, not every value's reporting year. |
| `taxonomy_years` | Distinct US-GAAP dictionary versions used by the inputs. A taxonomy year need not equal its filing year. |
| `requested_items` | Common Item selection across the inputs, currently usually `1`, `1A`, `7`, `8`. Not every Item necessarily contributes values. |
| `notes` | Merge rules and interpretation notes. |
| `filings` | Source-file metadata and the original extraction verification for each filing. |
| `metrics` | Combined metrics, grouped by exact `concept`. |
| `verification` | Merge counts and issuer-check method; it does not rerun extraction or source-fact verification. |

The `counts` object describes the combined data:

| Field | Meaning | Intel example |
|---|---|---:|
| `verified_concepts` | Number of distinct concept IDs in `metrics[]`. | 450 |
| `company_wide` | Metrics with at least one value without explicit dimensions. | 338 |
| `dimension_only` | Metrics whose retained values all have explicit dimensions. | 112 |
| `value_records` | Retained records across all `value[]` arrays, including identical comparative values from different filings. | 4,619 |

These are counts of metrics and records, not counts of printed tables.

## 3. Each metric: `metrics[]`

| Field | Meaning in the merged file |
|---|---|
| `concept` | Exact XBRL identifier and merge key, e.g. `us-gaap:NetIncomeLoss`. Different IDs remain separate even if their labels look similar. |
| `query_name` | Concept name without `us-gaap:`. |
| `label` | Main readable taxonomy name, from the latest input filing containing this concept. It need not match the printed row label. |
| `definition` | Main official taxonomy definition, from that same filing. Missing definitions remain `null`. |
| `period_type` | Taxonomy classification, `instant` or `duration`, from that same filing. |
| `scope` | Recomputed from all combined values: `company_wide` if any value has `dimensions: []`; otherwise `dimension_only`. |
| `company_wide_annual_or_year_end_years` | Union of the original qualifying undimensioned annual/year-end year lists. Not a list of source filing years or every available period. |
| `units` | Union of the original metric-level unit lists, which describe undimensioned values. Check each value's `unit` for dimensional records. |
| `api_groups` | Union of the original API group names. These are not guaranteed printed table names. |
| `items` | Union of the original metric's Item memberships. Each value retains its own `items`. |
| `metadata_source_filing_year` | Filing year supplying the main displayed label, definition and period type. |
| `source_filing_years` | Input filing years in which this concept exists. Some concepts appear in only one or two filings. |
| `metadata_by_filing` | Original metric metadata for each contributing filing, including its original label, definition, scope and summary lists. |
| `value` | Retained value records after latest-filing conflict selection, each with an added `source_filing_year`. |

`company_wide` does not mean every value is undimensioned. A metric can contain
both company-wide and segment-specific values. **Empty dimensions do not imply
that a printed row is labelled “Total.”**

## 4. Each value: `metrics[].value[]`

The actual amount is at **`metrics[i].value[j].value`**. The outer `value` is a
list; the inner `value` is one amount.

| Field | Meaning |
|---|---|
| `value` | Original API amount, normally a decimal string. `null` with `status: "nil"` means explicitly nil, not zero. |
| `unit` | Unit of this amount, e.g. `USD`, shares or a ratio unit. |
| `period.instant` | Balance as of a specific date. |
| `period.startDate`, `period.endDate` | Dates covered by a duration value. Not necessarily a full year. |
| `dimensions` | Explicit categories such as business segment or geography. `[]` means no explicit dimensions. Entries identify an `axis` and `member`. |
| `status` | `reported` or `nil`; separate from source-label resolution status. |
| `decimals` / `precision` | Accuracy information when supplied. These do not scale the stored amount. |
| `items` | Items where this value was verified in its source filing. |
| `source_labels` | Original source occurrences, including row/column wording and locations. |
| `source_filing_year` | Input filing that supplied this record. This is the only field added to each original value. |

Amounts are already in their XBRL units: `"1689000000"` with `unit: "USD"`
means USD 1.689 billion. Do not multiply by a table's “in millions” heading again.

Within `source_labels[]`, `row_label` and `column_label` preserve source wording;
`label_method` describes how they were found. Label `status` can be `resolved`,
`partial`, `unresolved`, `not_in_table` or `source_not_found`. Labels may be null.
`page`, `source_fact_id` and `source_locator` locate the occurrence. Multiple-document
filings may also include `document_id` and `source`.
Fact IDs and locators are local to their source filing/document, not globally unique.

## 5. How the years and metadata connect

**Match by year, not by the entries' order or proximity in the JSON.**
`metadata_by_filing[]` and `value[]` are separate lists inside the same metric.
The final metadata entry does not govern all the values that follow it.

For a value with `source_filing_year: 2023`:

1. Find the same metric's `metadata_by_filing` entry with `source_filing_year: 2023`.
2. Read its `metadata` for that filing's original metric description.
3. Find the file-level `filings` entry with `filing_year: 2023` for source paths and verification.

The three year-related ideas are distinct:

| Field | Answers |
|---|---|
| A value's `source_filing_year` | Which fiscal-year filing supplied this record? |
| A value's `period` | Which date or reporting period does the amount describe? |
| A metric's `metadata_source_filing_year` | Which filing supplied its main displayed description? |

For example, this is an actual Intel net-income record, with `source_labels`
omitted here for readability:

```json
{
  "value": "1689000000",
  "unit": "USD",
  "period": {
    "startDate": "2023-01-01",
    "endDate": "2023-12-30"
  },
  "dimensions": [],
  "status": "reported",
  "decimals": "-6",
  "items": ["8"],
  "source_filing_year": 2025
}
```

This is **2023 net income reported in the FY2025 filing**. Its matching metadata
entry has `source_filing_year: 2025`. The FY2023 and FY2024 filings also report
this same amount and period, so the merged metric retains three records.
Do not add those three records together: they are repeated reporting of one period.

The merged filename's `2023_2025` refers to the source filings. Comparative
periods before 2023 remain in the output; the merger does not filter them out.

## 6. Single-year output versus merged output

| Aspect | Single-year JSON | Merged JSON |
|---|---|---|
| Schema | `api-metrics-1.0` | `api-metrics-merged-1.0` |
| Typical filename | `intel_2023_api_metrics_with_values.json` | `intel_2023_2025_merged_api_metrics_with_values.json` |
| Input coverage | One filing, which can include comparative periods. | Several filings, retaining all their periods. |
| Main data shape | `metrics[] → value[]` | Same core shape. |
| Concept grouping | One metric per concept in that filing. | One metric per exact concept across all inputs. |
| Value fields | Original value and source-label fields. | Same fields plus `source_filing_year`. |
| Value duplicates | The exporter already consolidates matching entries within that filing. | Keeps identical repeated amounts; removes older amounts that disagree with the latest filing for the same context. |
| Labels and definitions | One version per metric. | Latest available version displayed; all originals in `metadata_by_filing`. |
| Scope and summary lists | Describe that filing's retained values. | Recomputed scope and combined lists; original versions remain in `metadata_by_filing`. |
| Source and exporter metadata | Top-level `source`, `exporter_version`, `membership_report`, `taxonomy_year`. | Preserved separately inside each `filings[]` entry. |
| Taxonomy versions | One `taxonomy_year`. | `taxonomy_years[]` plus each filing's original version. |
| Verification | Original extraction, membership and exclusion checks. | Top-level merge checks; original checks preserved at `filings[].verification`. |
| Counts | Concept and scope counts for one filing. | Unique combined concepts, combined scope counts and `value_records`. |

**The complete schemas are different even though `metrics[] → value[]` stays the
same.** Code that reads top-level `source` or `verification.exclusion_counts`
must instead use the appropriate `filings[]` entry for merged files.

### How conflicts are resolved

The merger groups by **concept + full period + unit + dimensions**. Dimension
order is ignored, but different axis/member combinations stay separate. Different
duration start dates stay separate even when their end dates match.

For each context:

1. Find the highest `source_filing_year` that actually contains the context.
2. Keep every amount reported for that context in that filing.
3. Remove older records whose amounts differ from those newest amounts.
4. Keep older records that report an identical amount, with their own source labels.

For example, Truist reports the same 2024 securities-loss context as
`-6651000000` in its FY2024 filing and `6651000000` in its FY2025 filing.
The merged output keeps the FY2025 record and removes the conflicting FY2024
record. The original FY2024 JSON still contains its original value.

The comparison uses exact decimal amounts: `"100"`, `"100.00"` and `"1E2"` are
equal. It does not round using `decimals` or `precision`; even a rounding-sized
difference between filings follows the same latest-filing policy. `nil` is
distinct from a reported amount, including zero. A newer nil replaces an older
numeric value for the same context, and a newer numeric value replaces an older nil.

If the latest filing has several different amounts for one context, all remain.
The merger cannot use filing recency to choose between records from the same
filing. These contexts are counted in
`verification.latest_filing_contexts_with_multiple_amounts`; they may reflect
different rounding accuracy and are not automatically errors.

**Latest means the highest input filing fiscal year containing that context**,
not the fact's period year, taxonomy year or latest year containing the concept
in general. One export per fiscal year is supported; this does not choose
between same-year amendments by SEC acceptance timestamp.

This is a user-selected recency policy, not proof that newer source tagging is
correct. It applies even when the older amount may be more plausible. No amounts
are summed, sign-corrected or relabelled, and source occurrences from discarded
records are not attached to the selected newer record.

The core `metrics[] → value[]` structure stays the same. However, the merged
file is no longer a complete archive of every input value. To recover an older
discarded record, read the yearly file referenced by `filings[].metrics_file`.
`metadata_by_filing` retains descriptions and summary metadata, not removed amounts.

## 7. Why preserve metadata for every filing?

The same concept ID can have revised taxonomy wording. In these Intel inputs,
**26 concepts differ in label and/or definition**: 12 have label changes,
24 have definition changes, and 10 are in both groups.

For `us-gaap:CostOfGoodsAndServicesSold`:

| Filing year | Original label |
|---|---|
| 2023 | Cost of Goods and Services Sold |
| 2024 | Cost of Goods and Services Sold |
| 2025 | Cost of Product and Service Sold |

The merged main label uses the 2025 version. `metadata_by_filing` retains all
three originals. These differences existed before merging. The merger groups
exact concept IDs; it does not establish semantic equivalence across taxonomy revisions.

## 8. Reading a value with its matching metadata in Python

This example reads the 2023-period net-income value from the FY2025 filing and
looks up metadata using its source year. It does not modify any files.

```python
import json
from pathlib import Path

path = Path(
    "tests/for_table_development/intel/"
    "intel_2023_2025_merged_api_metrics_with_values.json"
)
data = json.loads(path.read_text(encoding="utf-8"))
metric = next(m for m in data["metrics"] if m["concept"] == "us-gaap:NetIncomeLoss")

metadata_by_year = {
    entry["source_filing_year"]: entry["metadata"]
    for entry in metric["metadata_by_filing"]
}
filings_by_year = {entry["filing_year"]: entry for entry in data["filings"]}

for record in metric["value"]:
    if (
        record["source_filing_year"] == 2025
        and record["period"] == {"startDate": "2023-01-01", "endDate": "2023-12-30"}
        and record["unit"] == "USD"
        and record["dimensions"] == []
    ):
        year = record["source_filing_year"]
        original_metadata = metadata_by_year[year]
        source_file = filings_by_year[year]["metrics_file"]
        print(original_metadata["label"], record["value"], source_file)
```

## 9. Verification of the current Intel merge

| Source filing | Original concepts | Original value records | Retained merged records |
|---|---:|---:|---|
| FY2023 | 357 | 1,443 | 1,420 |
| FY2024 | 362 | 1,646 | 1,603 |
| FY2025 | 388 | 1,596 | 1,596 |
| Combined | 450 unique concepts | 4,685 | 4,619 |

The merger removed 66 older conflicting records across 63 contexts. Every
retained record exactly matches its original, apart from the added source year.
Every newest-filing context record is retained; no period, unit or dimension
combination disappeared. The 18 contexts with multiple amounts in their latest
filing remain available for review.

Original metric metadata is preserved in `metadata_by_filing`; original
file-level metadata is preserved in `filings[]`. File hashes still match the
unchanged yearly inputs. This checks the merge policy and preservation of
retained records, not correctness against the underlying SEC filings anew.

The top-level `verification` fields mean:

| Field | Meaning |
|---|---|
| `input_files` | Number of source yearly files: 3. |
| `input_metric_records` | Sum of their metric counts: 1,107, before grouping shared concepts. |
| `input_value_records`, `output_value_records` | Records before and after merging: 4,685 and 4,619. |
| `values_removed` | Older conflicting records dropped: 66. Input count minus output count. |
| `merge_key` | Field used to group metrics: `concept`. |
| `conflict_policy` | `latest_source_filing_year_per_context`. |
| `conflict_context_key` | Fields compared to identify a context: concept, period, unit and dimensions. |
| `conflicting_contexts_resolved` | Contexts where at least one older conflicting record was removed: 63. A context can still have multiple amounts from its latest filing. |
| `latest_filing_contexts_with_multiple_amounts` | Contexts whose latest filing contains more than one distinct amount/nil state: 18. Retained for review. |
| `issuer_check` | How company identity was checked: `sec_cik` here, or `company_name_and_available_sec_cik` when complete CIK evidence is unavailable. |

These counts describe the checked Intel files; other companies and input sets
will have different counts.
