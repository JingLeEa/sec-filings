# Automatic printed-page recovery

For the baseline LLM stages and their integration with the extractor and full
pipeline, see [fallback LLM Item extraction](JINGLE_fallback_llm_extraction.md).

Printed-page recovery is enabled by default as a fallback after the baseline
Item locator. It runs only for
Items that its existing stages did not recover, and checks short Item 7/8
printed-page pointers even if a candidate body was returned. It reads the index references
automatically, then sends bounded HTML chunks that retain layout tables to the
model. The model returns printed page labels and footer source-event IDs. Code
validates those IDs and translates page intervals to the original narrative-block
IDs before the normal merging, footnote handling, and chunk generation.

## Compare extraction methods

The comparison helpers are local evaluation tools. Their dot-prefixed filenames
are excluded by this repository's `.gitignore`; they are not part of the
production extractor. From the repository root:

```bash
.venv/bin/python scripts/.compare_filing_item_fallback.py \
  --ticker intc --year 2025 --use-cached-html --agent-mode both
```

This requires a cached `source.html` under
`data/item_fallback_comparison/intc/2025/`. Omit `--use-cached-html` to download
the filing using `SEC_USER_AGENT`. The SoCLaaS settings are read from `.env`.
Comparisons make model requests but do not write to PostgreSQL. Repeat this
command with a different `--ticker` or `--year` for other filings; the separate
list-runner script has been removed.

Modes are `baseline`, `page-recovery`, and `both`; the default is `baseline`.
This comparison default deliberately selects the baseline-only control; it is
different from production extraction, which enables automatic page fallback.
The `page-recovery` mode still runs the baseline locator first. It enables the
additional recovery stage; it does not force page mapping or skip the baseline.
The `both` mode runs deterministic extraction once and retains independent
baseline and page-recovery outputs. Each run has a fresh timestamped folder:

- `deterministic/`: local extractor output with its LLM fallback disabled.
- `agent/`: baseline independent agent output, when requested.
- `agent_page_recovery/`: independent agent with automatic page fallback enabled.
- `baseline_report.json`: baseline report in `both` mode.
- `report.json`: page-recovery report, or baseline report in baseline-only mode.
- `*_responses.jsonl`: responses, reported usage, actual model, and timings.
- `*_agent_selection.json`: original source block IDs and boundary text.

The dot-prefixed comparison script contains the implementation directly;
no non-hidden comparison script or compatibility wrapper is required.

## Automatic fallback in extraction

Extraction starts with deterministic code, then uses the baseline LLM locator
when needed, then printed-page recovery when the baseline cannot select a body
or a qualifying page pointer needs checking. No enable flag is required. If code
extraction succeeds and no qualifying suspect pointer is detected, both agents
are skipped.

```bash
.venv/bin/python scripts/extract_filings.py \
  --ticker intc --year 2025 --env-file .env \
  --user-agent "Your Name you@example.com"
```

Replace the example contact with your SEC identification. `--user-agent` only
identifies SEC download requests; it does not select or trigger an LLM agent.
It can be omitted if `SEC_USER_AGENT` is set in the environment.

### Force a baseline agent check

To invoke the baseline agent even when deterministic extraction found the
requested Items, use `--agent-check-items`:

```bash
.venv/bin/python scripts/extract_filings.py \
  --ticker txn --year 2025 --env-file .env \
  --user-agent "Your Name you@example.com" \
  --agent-check-items 1 1A 7 8
```

This checks all four Items with the baseline agent, with page recovery available
automatically afterward. Use `--agent-check-items 8` to check only Item 8. Add
`--no-agent-page-recovery` to keep the check baseline-only.
Validated replacements are subject to the normal overlap checks; forcing a
check does not guarantee that existing extraction will be replaced.

### When page recovery runs

Within an agent invocation, page recovery is considered automatically after
baseline index, heading, and outline selection unless explicitly disabled, when:

- At least one requested Item has no validated selection; or
- A short explicit Item 7/8 section points to printed pages, even if a candidate
  body was returned. This allows verification of that body's referenced scope.

Usable printed-page references are still required. If all Items are selected
and no qualifying pointer exists, the page stage is skipped. A nonempty but
incorrect selection can therefore still bypass it. There is currently no CLI
flag that forces page recovery unconditionally.

### Disable or explicitly re-enable the fallback

- `--no-agent-page-recovery`: disable just printed-page recovery; the baseline
  LLM fallback remains available.
- `--no-agent-fallback`: disable both LLM paths in `extract_filings.py`.
- `--agent-page-recovery`: retained for compatibility and explicit re-enabling;
  it is redundant during a normal run because the default is enabled.

### Check which stages actually ran

| Console message | Meaning |
| --- | --- |
| `Item fallback agent: checking ...` | The production extractor invoked the baseline Item locator. |
| `Printed-page recovery: ...` | The additional page-recovery stage was entered; footer and cache counts describe its mapping result. |
| `Item <n>: recovered by fallback agent ...` | An agent-selected replacement was accepted; this message alone does not identify whether page mapping was used. |

A page map can come from cache, so entering page recovery does not necessarily
make a new mapping request. A zero-footer result or missing-reference warning
also does not establish successful recovery. In comparison runs, inspect
`*_responses.jsonl` for `page_mapping` and `page_reference` requests and the
agent log for cached mapping activity. The enabled flag and output files alone
do not establish which stage produced the text.

`scripts/run_full_pipeline.py` inherits automatic page fallback from the
extractor's defaults. It does not expose or forward page-recovery controls.
Use `extract_filings.py` directly to disable recovery or change the page cache
directory. Full-pipeline runs use repository `data/filing_page_cache/` even with
a custom `--data-dir`.

## Boundaries, cache, and limitations

- Tables stay intact in the model evidence, including table-based headings and
  footers. They are not automatically included as numeric narrative output.
- Overlapping chunks retain global source-event IDs. Repeated identical footer
  reports are deduplicated; conflicting or reversed page sequences are rejected.
- Only consecutive printed-page boundaries establish a page interval. Missing
  pages are never bridged by assuming that distant footers bound all content.
- Missing boundaries trigger bounded rereads around nearby located footers. After
  two validated examples establish an identical labeled footer-table layout,
  unique matching footers between observed page anchors can fill omissions.
  Numeric-only layouts, ambiguous candidates, extrapolation, and conflicting
  page labels are excluded from this expansion.
- Numeric and prefixed page ranges such as `F-1–F-28` are supported. A single TOC
  start page is not treated as a complete Item scope. The locator can read that
  actual page and follow a pointer to another range only when the range is
  grounded in a verbatim source quote. The first F-1 page can start after an
  observed final footer of the preceding main-page sequence.
- Navigation-table IDs and large financial-data tables are invalid footer IDs.
- Explicit Item indexes take precedence over model-assigned subtopic TOC rows
  when both supply references for the same Item. A source-quoted body pointer
  defines the allowed scope: missing or unrelated pages invalidate a candidate
  selection and trigger recovery. HTML entities in copied quotes are decoded
  before exact source matching.
- A page containing only tables can have verified boundaries even when the
  narrative-block projection contains no text for it. This feature preserves
  the existing narrative table-filtering policy; source-table text can still
  require separate review or the existing table extraction path.
- A filing-wide page map and successful individual requests are cached under
  `data/filing_page_cache/`. The key includes source content, configured endpoint
  and model, and mapping-policy version. Credentials are not stored. The request
  cache includes public filing evidence, responses, usage, and actual model.
- `--page-cache-dir` changes the comparison cache; extraction accepts
  `--agent-page-cache-dir`. Changing source/model/policy invalidates the cache.
  A provider changing the backend behind an unchanged model alias is not detected;
  use a new cache directory when reevaluating that backend.
- Completed reports and text overlap do not certify semantic accuracy. Shared
  page ownership, descriptive heading boundaries, pointer-only results, and
  unusually short/long output need source review. This feature does not add a
  mandatory completeness gate to database loading.

## Offline checks

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_filing*.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_full_pipeline.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_sec_10k_extractor.py'
```

The comparison-only tests are also local and ignored. Run them explicitly:

```bash
PYTHONPATH=src .venv/bin/python tests/.test_filing_item_comparison.py
```

The local summarizer can still read a saved baseline manifest and a historical
batch summary (it does not create or run a new batch):

```bash
.venv/bin/python scripts/.summarize_filing_item_evaluation.py \
  --baseline-manifest data/item_fallback_comparison/evaluation_2025/baseline_manifest.json \
  --batch-summary data/item_fallback_comparison/_batches/<run>/summary.json \
  --output-dir data/item_fallback_comparison/evaluation_2025
```

The summarizer does not call an API. It compares presence, text sizes, reported
usage and token overlap, and separately checks original block IDs, ordering,
cross-Item sharing, foreign Item headings, and explicit-heading spans. It saves
boundary excerpts for reviewing cases that these structural checks cannot settle.
