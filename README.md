# Automated SEC Disclosure Comparison

This project builds an automated pipeline for comparing SEC 10-K disclosures across years and peer companies. It extracts relevant filing sections, removes repeated boilerplate, prepares human annotation files, and provides the foundation for semantic, LLM-assisted, and agentic disclosure analysis.

The current implementation focuses on the preprocessing and benchmark dataset workflow: SEC filing extraction, sentence-level lexical comparison, table extraction, annotation export, ID conversion, and paragraph-context recovery.

## Current Capabilities

- Download 10-K filings through the SEC submissions API.
- Extract Items 1, 1A, 7, 8, and 15 from filing HTML.
- Clean HTML, tables, repeated headers, and page artifacts.
- Chunk disclosures with stable IDs such as `nvda_2024_1A_P001`.
- Compare consecutive-year disclosures and remove unchanged sentences.
- Handle manually verified section title mappings.
- Export narrative and table annotation files for human review.
- Convert older annotation IDs after extraction logic changes.
- Add original paragraph context back into annotation CSVs.
- Evaluate saved change-taxonomy predictions against sentence annotations, with conditional scores and explicit coverage diagnostics.

## Project Layout

```text
src/sec_disclosure/
├── extraction/      # implemented SEC filing and Item extraction
├── comparison/      # implemented lexical and table comparison
├── annotation/      # implemented annotation export, ID conversion, context tools
├── pipelines/       # implemented end-to-end runner
├── agents/          # planned agentic workflow
├── llm/             # planned shared LLM client and prompts
├── indexing/        # planned embeddings/vector search
├── evaluation/      # implemented change-taxonomy benchmark evaluation
└── utils/           # planned shared utilities

scripts/             # command-line entry points
docs/                # detailed workflow notes and annotation guides
data/                # generated outputs; ignored by Git
tests/               # unit tests
```

## Quick Start

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

For development, install the package in editable mode:

```bash
python3 -m pip install -e .
```

Set a SEC User-Agent:

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
```

Extract a filing:

```bash
python3 scripts/extract_filings.py --ticker NVDA --year 2024
```

Compare two extracted years:

```bash
python3 scripts/compare_filings.py \
  data/raw/nvda/2023/2023_chunks.json \
  data/raw/nvda/2024/2024_chunks.json
```

Run extraction, comparison, and disclosure annotation export together:

```bash
python3 scripts/run_disclosure_pipeline.py \
  --ticker WFC \
  --company "Wells Fargo" \
  --industry Banking \
  --previous-year 2024 \
  --current-year 2025
```

Evaluate change-taxonomy results for any company, for example Micron (`MU`):

```bash
python3 scripts/evaluate_change_taxonomy.py --ticker MU
```

The command prefers `data/annotation/mu.csv`, or discovers a matching CSV using
its company label and source-ID ticker prefixes. It supports `Company = Micron`
with `MU_...` sentence IDs. Use `--company "Micron Technology"` if the display
name cannot be associated with MU through its IDs. Every run rereads the inputs
and regenerates the Markdown report. There is no default company; with a sole
annotation CSV, running without arguments infers its ticker. Use `--ticker AMD`
or `--ticker NVDA` to select those companies when multiple benchmarks exist.

Results are read from `data/alignments/<ticker>/*/alignments_result.json`; reports
are saved to `data/evaluation/<ticker>/`. The evaluator verifies SEC filing
identities before matching sentence pairs, so filing-year annotation columns
can be matched to fiscal-year result folders.
Open `data/evaluation/<ticker>/evaluation_report.md` (or the HTML version) for a readable report with
coverage calculations, alignment counts, class scores and evaluation limitations.

## Documentation

- [Pipeline Usage](docs/pipeline_usage.md): detailed extraction, comparison, ID conversion, paragraph context, and table commands.
- [Current Text Preprocessing Flow](docs/current_text_preprocessing_flow.md): current extraction, chunking, sentence splitting, bullet handling, and ID behavior.
- [Disclosure Annotation Export](docs/disclosure_annotation_export.md): detailed guide for exporting narrative comparison rows to Google Sheets.
- [Change Taxonomy Evaluation](docs/change_taxonomy_evaluation.md): pairing rules, filing identity matching, metrics, exclusions, and evaluation commands.

## Development

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Generated files are written under `data/`, which is ignored by Git.
