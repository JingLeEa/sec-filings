# Automated SEC Disclosure Comparison

This project builds an automated pipeline for comparing SEC 10-K disclosures across years and peer companies. It extracts relevant filing sections, removes repeated boilerplate, prepares human annotation files, and provides the foundation for semantic, LLM-assisted, and agentic disclosure analysis.

The current implementation focuses on the preprocessing and benchmark dataset workflow: SEC filing extraction, sentence-level lexical comparison, table extraction, annotation export, ID conversion, and paragraph-context recovery.

## Current Capabilities

- Download 10-K filings through the SEC submissions API.
- Extract Items 1, 1A, 7, 8, and 15 from filing HTML.
- Clean HTML, tables, repeated headers, and page artifacts.
- Chunk disclosures with stable IDs such as `2024_1A_P001`.
- Compare consecutive-year disclosures and remove unchanged sentences.
- Handle manually verified section title mappings.
- Export narrative and table annotation files for human review.
- Convert older annotation IDs after extraction logic changes.
- Add original paragraph context back into annotation CSVs.

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
├── evaluation/      # planned benchmark evaluation
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

## Documentation

- [Pipeline Usage](docs/pipeline_usage.md): detailed extraction, comparison, ID conversion, paragraph context, and table commands.
- [Disclosure Annotation Export](docs/disclosure_annotation_export.md): detailed guide for exporting narrative comparison rows to Google Sheets.

## Development

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

Generated files are written under `data/`, which is ignored by Git.
