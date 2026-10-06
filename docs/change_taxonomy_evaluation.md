# Evaluate change taxonomy

The evaluator compares the benchmark CSV's **Change Taxonomy** with saved
`change_analysis.final_taxonomy` values. It makes no model calls and uses the
Python standard library. Annotation files, alignment results and the other
change-analysis fields are read-only inputs.

For Micron (`MU`), run from the repository root:

```bash
python3 scripts/evaluate_change_taxonomy.py --ticker MU
```

Each run rereads the annotation and saved alignment results, recomputes the
evaluation, and replaces `data/evaluation/mu/evaluation_report.md` with the
current Markdown report. No counts or predictions are cached between runs.

The same pipeline works for any ticker; there is no default company or issuer
name lookup table. Tickers are case-insensitive and determine the result and
output folders:

```text
data/annotation/mu.csv
data/alignments/mu/<previous-year>-<current-year>/alignments_result.json
data/evaluation/mu/evaluation_report.md
```

Annotation selection follows these rules:

1. With `--ticker MU`, prefer `data/annotation/mu.csv`.
2. If that file is absent, use the sole annotation CSV, or identify a unique
   matching CSV among several files using its `Company` values or sentence-ID
   prefixes. For example, `micron.csv` with `Company = Micron` and
   `MU_2024_Item1A_P001_S01` identifies MU. The same company label then selects
   rows whose IDs are blank.
3. With no arguments, use the sole CSV and infer its ticker from source-ID
   prefixes, falling back to the `Company` value if it is a ticker. Multiple
   files or companies require `--ticker` or an explicit `--annotation`.
4. Ambiguous file matches require `--annotation PATH`.

If the CSV uses a display name that cannot be associated with the ticker through
its IDs, specify that label explicitly:

```bash
python3 scripts/evaluate_change_taxonomy.py --ticker MU --company "Micron Technology"
```

This selects `Company = Micron Technology` rows and records their canonical
company as MU. Contradictory ticker prefixes in recognized source IDs are rejected.
For a CSV containing several companies, `--ticker` selects only that company's
rows. Renaming a file does not replace its company metadata.

Default paths are resolved against the repository, so the script also works
when launched from another working directory or an IDE. Explicit relative path
arguments are resolved against the caller's working directory.

To evaluate AMD, use `--ticker AMD`; for NVIDIA, use `--ticker NVDA` with the
corresponding result folders. To override the annotation or data locations:

```bash
python3 scripts/evaluate_change_taxonomy.py \
  --ticker MU \
  --annotation data/annotation/micron.csv \
  --results-dir data/alignments \
  --output-dir data/evaluation/mu
```

`--results-dir` is the root containing company/year-pair folders. `--items 1A 8`
optionally filters benchmark Items; the default already evaluates only Items
present in the annotation. `--review-dir tests/fixtures/alignments` optionally
includes `<ticker>/*/needs_review.json` for mapping and coverage diagnostics. Review
records are never scored. A review record overlapping an accepted prediction can
make a sentence pair ambiguous, so specify this option consistently across runs.

Rerunning replaces the six evaluation outputs in the selected output folder.
Use a separate `--output-dir` to retain another run. Input hashes in the summary
identify exactly which annotation and result files were evaluated.

## Match the actual filings first

SEC accession IDs in the benchmark's **Previous/Current Filing SEC URL** fields
are matched against `evidence[].sentences[].source_url` in the results. Dashed
index URLs and archive directory URLs identify the same filing. These filing
identities take precedence over nominal year columns. If URL metadata is absent,
the evaluator falls back to company and the supplied previous/current years.
Known conflicting filing identities never fall back to a matching nominal year.

The supplied AMD annotation uses filing years in its year columns, while the
alignment results use fiscal years:

| Annotation year columns | Actual result comparison | Available? |
| --- | --- | --- |
| 2023–2024 | 2022–2023 | No saved result |
| 2024–2025 | 2023–2024 | Yes |

The 2024–2025 fiscal-year result has no corresponding filing pair in this
benchmark. Missing comparison rows remain in overall benchmark coverage with
`missing_prediction`; they do not become taxonomy errors. The evaluator derives
this mapping from the supplied filing URLs without rewriting the CSV or applying
a guessed year offset. Row reports preserve annotation years and separately
record `comparison_previous_year`, `comparison_current_year`, and `period_matching`.

## Decide which sentence pairs are comparable

For each benchmark row, source lookup stays within the resolved comparison,
the SEC Item and the previous/current side. IDs are checked against text. When
IDs have been renumbered, a unique formatting-normalized exact text match can
resolve the source occurrence. Normalization handles leading bullets, whitespace,
curly quotes and typographic dashes. It preserves case, words, amounts, dates and
other punctuation. No fuzzy similarity threshold or text-substring matching is
used to assign a prediction.

Both populated source occurrences must belong to a unique common alignment.
For consolidated groups, the original links in `grouping.source_alignments`
must support their disclosure membership; sharing a large group alone does not
establish every possible link. A one-sided benchmark row requires a corresponding
one-sided saved comparison.

For example, benchmark A → B and saved A → C are not comparable taxonomy cases.
When A and B can both be located but are in different supported comparisons,
the row is `pair_mismatch`. When B cannot be found in the saved citations, it is
`unmapped_evidence`: selected citations are incomplete, so their omission alone
does not prove an incorrect alignment. Neither case contributes to taxonomy
precision, recall or F1.

The result labels describe disclosure groups, while the benchmark labels
sentences. The primary score therefore projects a group's prediction onto its
uniquely mapped benchmark rows **only when those rows share one gold label**.
A group with different mapped gold labels is `mixed_gold_group`; there is no
majority vote or selection of the label that agrees with the prediction. This
is a conditional score on a restricted subset, not a complete assessment of
sentence classification. Uniform mapped labels do not establish that uncited or
unannotated sentences have the same label.

## Labels and scoring

The six classes are `New`, `Removed`, `Reworded`, `Expanded`, `Reduced` and
`Modified`. Case and surrounding whitespace are normalized; `Unchanged` maps
explicitly to `Reworded`, following the supplied benchmark convention. Scalar
strings and singleton label arrays are supported. Unknown labels and arrays of
multiple labels are excluded as `invalid_taxonomy`.

Only `change_analysis.final_taxonomy` supplies the prediction. A null label is
`missing_taxonomy`; the evaluator does not infer a label from an empty side,
exact matching, `unmatched_type`, or the lexical, semantic or LLM stages. A
populated final label remains usable when `change_analysis.status` is still
`not_started`, since the current result files intentionally changed only the
final label. Top-level `needs_review` cases remain outside the score.

Accuracy, per-class precision/recall/F1 and the confusion matrix use only scored
rows. Macro-F1 is the unweighted mean over all six classes. A class absent from
the scored subset contributes zero; class support and coverage are shown beside
its metrics. Weighted-F1 weights each class by scored gold support. When no
rows can be scored, the scores are JSON `null` and empty CSV cells.

Coverage is scored rows divided by all selected benchmark rows, independently
of taxonomy accuracy. Reports break down coverage by class, annotation period,
Item, and resolved fiscal-year result period. A high score on a small subset
does not describe performance across the entire benchmark.

The readable report emphasizes **coverage of available comparisons**: scored
CSV rows divided by CSV rows whose SEC filing pair has a saved comparison. It
also shows **full benchmark coverage**, which retains rows with unavailable
filing comparisons in the denominator. Both percentages count annotation CSV
rows. Disclosure groups and disclosure-ID pairs are separate inventory units.

The inventory expands each finalized group's previous/current disclosure IDs,
deduplicates identical pairs within a comparison, and retains original-link
support when a consolidated group provides it. One-sided disclosure cases and
review groups are reported separately. These counts never replace the CSV-row
coverage denominator. `summary.json` includes `available_comparisons`,
`available_by_item`, `benchmark_comparisons`, `alignment_inventory`, and
`coverage_accounting` for reproducing the report's tables.

## Outputs

| File | Contents |
| --- | --- |
| `evaluation_report.md` | Markdown evaluation report with the same content as the HTML version; includes all detail sections and links to the audit files |
| `evaluation_report.html` | Self-contained readable report with executive metrics, coverage formulas and units, filing mapping, alignment inventory, class scores, confusion matrix, exclusions, limitations and input hashes; open in a browser or print |
| `summary.json` | Method, input hashes, overall metrics, coverage reasons, class metrics, confusion matrices, and period/Item breakdowns |
| `row_results.csv` | Each benchmark Record ID, original text, located source sentences, candidate and selected match IDs, final label, and eligibility reason |
| `per_class_metrics.csv` | Precision, recall, F1, scored support and benchmark coverage, overall and by annotation period |
| `confusion_matrix.csv` | Gold/predicted label counts, overall and by annotation period |

The Markdown and HTML reports are generated on every evaluation run. Their class coverage uses
available-comparison annotation rows; the existing per-class CSV keeps full
benchmark coverage overall and by annotation period. All taxonomy scores still
use the same eligible row subset. Linked audit CSVs and JSON are beside the
report in the output folder.

Every benchmark row receives exactly one status: `scored`, `pair_mismatch`,
`missing_prediction`, `unmapped_evidence`, `ambiguous_sentence`,
`ambiguous_prediction`, `needs_review`, `missing_taxonomy`, `invalid_taxonomy`,
or `mixed_gold_group`. The row report is the place to inspect exclusions.

Original alignment explanations, content taxonomy, materiality, and raw
lexical/semantic/LLM results do not affect the taxonomy metrics. Nested source
alignments are link provenance rather than additional predictions.

Run the evaluator's tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_evaluate_change_taxonomy.py' -v
```
