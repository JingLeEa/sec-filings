# Export narrative comparisons to Google Sheets

`export_disclosure_annotations.py` is a standalone Python 3.10+ script. It reads an existing narrative comparison JSON plus the corresponding extraction chunk JSON files, and creates a 20-column TSV and an offline browser copy helper. No extra packages, SEC downloads, API keys, or LLM calls are needed. Existing extraction, comparison, and table-export scripts are not modified.

## Micron 2024 versus 2025

To run extraction for both years, comparison, and TSV export in **one command**, use the separate runner. Replace the contact details with your own:

```bash
python3 run_disclosure_pipeline.py \
  --ticker MU --company Micron --industry Semiconductors \
  --previous-year 2024 --current-year 2025 \
  --user-agent "Your Name your.email@example.com"
```

The runner defaults to Items **1, 1A, 7, and 8**, excluding Item 15. It calls the three existing scripts in order and stops if any step fails. Each run fetches both filings again. `--user-agent` can be omitted if `SEC_USER_AGENT` is already set.

Intermediate chunk files go to `data/raw/mu/<year>/`; comparison JSON goes to `data/comparison/mu/2024_vs_2025/`; final TSV and HTML go to `data/disclosure_output/mu/2024_vs_2025/all_items_diff/`. Existing generated files at those locations are refreshed; the scripts themselves are not changed. Extraction warnings about missing sections remain visible. If a step fails, later steps do not run, and files from a previous run may still exist.

Optional runner settings include `--items 7` for only Item 7, `--items 1 1A 7 8 15` to include Item 15, `--annotator "Your Name"`, `--split "Development/Validation"`, `--max-chars 1000`, and a repeatable `--title-map "ITEM::OLD_TITLE::NEW_TITLE"`. Use `--data-dir data/another_run` to keep a separate set of intermediate and final outputs. The company label affects TSV cells; directory names continue to use the ticker.

If the comparison JSON already exists, you can run only the exporter:

Run from the project directory after extracting and comparing the filings:

```bash
python3 export_disclosure_annotations.py \
  --input data/comparison/mu/2024_vs_2025/all_items_diff.json \
  --company Micron \
  --industry Semiconductors \
  --items 1 1A 7 8
```

Use `--annotator "Your Name"` or `--split "Development/Validation"` if you want to fill those fields. Otherwise, they remain blank unless supplied by the input JSON.

Outputs go to `data/disclosure_output/mu/2024_vs_2025/all_items_diff/`:

| File | Purpose |
| --- | --- |
| `disclosure_annotations.tsv` | All annotation rows with the 20-column header. |
| `paste_into_sheets.tsv` | The same rows without a header, for an existing annotation sheet. |
| `paste_into_sheets.html` | Browser preview and Copy rows button. |

Open `paste_into_sheets.html` in your browser. Click **Copy rows**, single-click column **A** in the first empty row of your Google Sheet, then paste normally with **Cmd+V / Ctrl+V**. Ensure the destination has enough empty rows and 20 empty columns (A through T). Check **Include column headers** when pasting into a new sheet. Columns after T are not included in the export.

If automatic copy is unavailable, click **Select rows for manual copy** and press Cmd+C / Ctrl+C. The TSV can also be imported using **Tab** as the separator. Quoted TSV fields may contain embedded line breaks; copy the entire file, not individual physical lines. Do not use “Split text to columns” on disclosure text. The browser helper preserves cell boundaries through an HTML table as well as quoted TSV.

## What one row means

Each row contains **one suggested previous/current sentence pair**, or **one unmatched sentence with the other side blank**. The unit is an individual `sentence` occurrence in the comparison JSON. The exporter preserves its text as supplied; it does not combine sentences or change the upstream sentence splitting. Unchanged sentences were removed by the comparison step.

- Each populated side has exactly one source chunk ID and one source sentence. IDs are never combined into a semicolon-separated list.
- Several rows can share a chunk ID because one paragraph/chunk can contain multiple sentences.
- The Original Paragraph cells include the full extracted paragraph, including unchanged sentences. Repeated chunk IDs repeat the same original paragraph in each relevant row.
- Unmatched sides have blank section, ID, and text cells. A blank side means no match was assigned; it does **not** confirm that a disclosure was added or removed.
- Previously mapped old/new section titles remain separate in their respective columns.
- Equivalent heading formats share a section preference during sentence matching. For Item 8, `Note 25. Income Taxes`, `Note 26. Income Taxes`, and `income taxes` belong to the same section. Original heading labels remain in their respective Previous/Current Section cells. Sentences can also match across different subsections within the same Item.
- **Change Taxonomy, Content Taxonomy, Materiality, and Rationale / Evidence stay blank for annotation.** A year-specific sentence may have been reworded or moved, so the exporter does not label it New or Removed automatically.

The exporter first groups equivalent headings within the same Item. All headings ignore capitalization, repeated whitespace, and trailing periods/colons. Item 8 headings also ignore a leading numbered `Note` label, including formats such as `Note 25.`, `Note 25 -`, `Note No. 25:`, and `Note (25)`. The remaining topic must match exactly to receive the same section preference: `Income Taxes` and `Interest Expense` remain distinct headings. Bare note numbers without a topic stay distinct, and meaningful numbers inside topics are retained. Existing manual title mappings take precedence over automatic heading aliases for this preference; they do not prohibit matching sentences across subsections.

Identical normalized sentences found under these equivalent headings are removed from both years before matching. The matcher then reserves exact-text matches anywhere within the same Item before matching edits, and omits these unchanged pairs too. Duplicate occurrences are counted separately, so an extra occurrence on one side survives. The exporter prints the total additional unchanged sentence pairs it removed. The original extraction and comparison JSON files are not modified.

Sentence matching now uses the method from `filing_sentence_annotator/compare_filings.py`, implemented locally in this exporter. No copy of that project or additional package is needed:

- Similarity is **80% token-sequence similarity + 20% character-sequence similarity**, using Python's `SequenceMatcher` on lowercase text. Tokenization retains punctuation and numbers; amounts and years are not replaced or ignored.
- The default threshold is **0.55**. A weighted word index retrieves up to **12 previous-sentence candidates** for each current sentence across the same Item. It ignores the reference tool's common stop words when finding candidates.
- Eligible candidates receive a **0.025 ranking bonus** when their headings are equivalent or explicitly mapped. The raw similarity must meet the threshold before the bonus applies.
- Candidates are considered in descending rank and assigned **one-to-one**, using each sentence occurrence at most once. The former mutual-best-match requirement and 0.08 ambiguity margin no longer apply. Equal scores use the reference method's deterministic index tie-breaking; ties can produce a pair.
- Exact unchanged sentences are reserved first. With duplicate exact text, the reference method prefers the same section, then the closest relative sentence position. Edited sentences are matched by wording and section preference, not chunk IDs.

For example, the 2023/2024 MBU revenue sentences score approximately **0.6955**, and the EBU revenue sentences score approximately **0.7074**. Both qualify at the default threshold. This is the same matching method applied to the existing comparison JSON; results can differ from the PDF annotator when its extracted sentences or input scope differ.

Every remaining source sentence occurrence appears exactly once, including repeated text or repeated chunk IDs. Within each Item, rows follow the previous sentences in comparison-group order, followed by unmatched current sentences in comparison-group order. The exporter retains this previous-side layout rather than adopting the reference tool's current-side layout or separate deletion file. Current sentences can match reordered previous sentences; their IDs retain the original source locations.

**Suggested pairs require review.** This method accepts more pairs, including across subsections, and can pair similar-looking sentences with different meanings. Large rewrites, moved disclosures, splits, and merges can still remain unmatched. Source text and chunk IDs retain any extraction mistakes in the input JSON. Review both suggested pairs and unmatched sentences before assigning labels. Taxonomy, materiality, and rationale remain blank; the reference tool's automatic New/Modified labels and alternative-candidate reports are not added.

For stricter matching, add `--match-threshold 0.9` to the standalone exporter command. This option accepts values from 0 to 1, inclusive, as in the reference tool. The one-command runner automatically uses the exporter's default sentence matching without any change to the runner.

## Column order

1. Annotator
2. Company
3. Industry
4. Split
5. Filing Form
6. Previous Fiscal Year
7. Current Fiscal Year
8. Item
9. Previous Section / Subsection
10. Previous Paragraph / Chunk ID
11. Previous Disclosure Text
12. Current Section / Subsection
13. Current Paragraph / Chunk ID
14. Current Disclosure Text
15. Change Taxonomy
16. Content Taxonomy
17. Materiality
18. Rationale / Evidence
19. Previous Original Paragraph
20. Current Original Paragraph

The original 17 columns keep their order. The three new columns follow Materiality, ending at Current Original Paragraph.

## How original paragraphs are filled

For each populated disclosure side, the exporter looks up its exact chunk ID in that year's extraction JSON. It checks the year, company, Item, and whether the sentence belongs to the saved chunk. It does not change IDs or guess a replacement if an old comparison no longer matches the extraction. Missing files, missing IDs, duplicate IDs, and mismatched text produce an error before any export files are replaced. An absent disclosure side keeps its Original Paragraph cell blank.

The usual source files are:

```text
data/raw/mu/2024/2024_chunks.json
data/raw/mu/2025/2025_chunks.json
```

The exporter finds `raw/` beside the input's `comparison/` directory, so the one-command runner's custom `--data-dir` also works automatically. For other comparison locations, the default is `data/raw`. The lookup uses the comparison JSON's company value (`mu`), not the display label supplied with `--company Micron`.

When extraction split a long paragraph into several chunks, the exporter rejoins chunks with the same Item, filing source, and `source_block_index`, in saved source order. It retains the original sentence's single chunk ID in the annotation row. It does not join adjacent paragraphs or unrelated chunks. If older chunk JSON lacks block metadata, the available context is the full saved chunk; the exporter cannot reconstruct a larger original paragraph without that metadata. The text is the cleaned extraction, so removed HTML tables are not restored.

For a custom extraction location, add `--chunks-root /path/to/raw`, or specify the two files explicitly:

```bash
python3 export_disclosure_annotations.py \
  --input data/comparison/mu/2024_vs_2025/all_items_diff.json \
  --previous-json data/raw/mu/2024/2024_chunks.json \
  --current-json data/raw/mu/2025/2025_chunks.json \
  --company Micron --industry Semiconductors
```

Each paragraph stays inside one cell, including its quotes and line breaks. The text column names match the existing annotation-ID/context helpers. This exporter writes TSV; those helpers currently accept CSV, so download/export as CSV before using them. The exporter now fills paragraph context directly, so the separate context helper is not needed for these new exports.

## Other inputs and repeated runs

For just Item 7, use the corresponding comparison JSON:

```bash
python3 export_disclosure_annotations.py \
  --input data/comparison/mu/2024_vs_2025/item_7_diff.json \
  --company Micron --industry Semiconductors
```

Its default output folder ends in `item_7_diff/`, keeping it separate from the all-items export. `--items` filters an all-items input; omit it to include every item in that JSON. An explicitly requested item absent from the input produces an error.

Use `--output-dir data/disclosure_output/my_export` to choose an exact destination. Repeating a command replaces only this exporter's three output files in that destination. Different filters on the same input use the same default destination, so supply different output directories if you want to keep each filtered version. The input JSON and existing code remain unchanged.

Quoting preserves tabs, quotes, Unicode, and line breaks. Formula-like cell text is prefixed with an apostrophe for spreadsheet transport. The exporter verifies that every TSV row round-trips to exactly 20 cells before writing the output files. A comparison with no changes produces a header-only TSV and an empty headerless TSV.
