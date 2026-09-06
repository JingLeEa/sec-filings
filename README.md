# Simple SEC 10-K Extraction Pipeline

This project uses the SEC submissions API to find a 10-K filing, removes HTML/tables/page noise, extracts Item 1, Item 1A, Item 7, and Item 8, then writes paragraph/disclosure chunks with IDs like `2024_P001`.

For filings like NVIDIA's inline XBRL HTML, displayed paragraphs are usually stored as styled `<div>` blocks rather than `<p>` tags. The extractor therefore chunks by meaningful HTML block and carries the latest short subheader, such as `Our Company` or `Data Center`, into each chunk's `item_title`.

## Usage

Use the SEC API with a ticker and fiscal year:

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
python3 sec_10k_extractor.py --ticker NVDA --year 2024
```

You can still use a direct SEC filing URL or local HTML file when needed:

```bash
python3 sec_10k_extractor.py "https://www.sec.gov/Archives/edgar/data/.../.../nvda-20240128.htm" --year 2024
```

## Outputs

The pipeline writes extracted chunk outputs to `data/raw/<company>/<year>/`:

- `<year>_chunks.json`: structured chunks for LLM input.
- `<year>_chunks.txt`: readable chunk file for manual copy/paste.
- `<year>_item_1.txt`, `<year>_item_1a.txt`, `<year>_item_7.txt`, `<year>_item_8.txt`: cleaned full section text.

SEC API extraction reads the filing HTML in memory and does not save downloaded HTML files.

Each JSON chunk has:

```json
{
  "id": "2024_P001",
  "company": "nvda",
  "year": "2024",
  "item": "1",
  "item_default_title": "Business",
  "item_title": "Our Company",
  "item_chunk_index": 1,
  "source_block_index": 123,
  "text": "...",
  "source": "..."
}
```

## Notes

- The extractor removes HTML tables before section text is written, matching the goal of clean disclosure text. Item 8 financial statements often contain important tables, so this pipeline is best for narrative extraction rather than numeric statement reconstruction.
- SEC downloads should use a descriptive `SEC_USER_AGENT` with your name/email.
- API extraction uses the ticker as the company folder by default. For local files, company/year are inferred from filenames like `nvda-20240128.htm`.
- If a filing has unusual headings, lower `--max-chars` for smaller LLM chunks or inspect the item TXT files to confirm boundaries.
- `item` is the SEC item number. `item_default_title` is the standard SEC heading, while `item_title` is the most recent subheader found inside that item.
- Generated files live under `data/`, which is ignored by Git.

## Compare Chunk Files

After extracting two years, compare the chunk JSON files to remove unchanged sentences before LLM review:

```bash
python3 compare_item_changes.py data/raw/nvda/2023/2023_chunks.json data/raw/nvda/2024/2024_chunks.json
```

Outputs are written to `data/comparison/<company>/<old_year>_vs_<new_year>/`.

If you manually verified that a subheader was renamed, pass an explicit title mapping with `--title-map`. The comparison still uses exact sentence matching; it does not fuzzy-match headers.

```bash
python3 compare_item_changes.py data/raw/nvda/2024/2024_chunks.json data/raw/nvda/2025/2025_chunks.json \
  --title-map "1A::Risks Related to Demand, Supply and Manufacturing::Risks Related to Demand, Supply, and Manufacturing"
```

You can also call the comparison logic from Python:

```python
from pathlib import Path

from compare_item_changes import compare_records, load_records

old_records = load_records(Path("data/raw/nvda/2024/2024_chunks.json"))
new_records = load_records(Path("data/raw/nvda/2025/2025_chunks.json"))

comparison = compare_records(
    old_records,
    new_records,
    old_year="2024",
    new_year="2025",
    company="nvda",
    title_mappings={
        ("1A", "Risks Related to Demand, Supply and Manufacturing"):
            "Risks Related to Demand, Supply, and Manufacturing"
    },
)
```

## Convert Annotation IDs

If chunk IDs change after rerunning extraction, convert an existing annotation CSV to the latest IDs:

```bash
python3 convert_annotation_ids.py data/id_conversion/nvidia_input.csv \
  --previous-json data/raw/nvda/2023/2023_chunks.json \
  --current-json data/raw/nvda/2024/2024_chunks.json \
  --output-csv data/id_conversion/nvidia_input_converted.csv
```

The input CSV must include these columns:

- `Previous Paragraph / Chunk ID`
- `Current Paragraph / Chunk ID`
- `Previous Disclosure Text`
- `Current Disclosure Text`

The converter keeps the original CSV columns, updates the previous/current chunk ID columns when the disclosure text matches the latest chunks, and adds audit columns such as `Previous ID Conversion Status` and `Current ID Conversion Status`.

# HTML 10-K tables to nested JSON

Extract numerical tables from **one selected Item** of two full 10-K HTML filings.
Nested JSON is the default output. Main extractor version 1.3.0 adds verified labels for unlabelled footer totals and explicit nulls for missing percentage displays. The existing CSV comparison remains available with `--output-format csv` or `--output-format both`.

It runs locally using Python 3.10+ and `lxml`. No LLM, API key, pandas, browser automation, or PDF parsing is involved.

## One command: SEC API to your annotation TSV

Keep `export_table_annotations.py` and `compare_html_tables.py` in the same folder. Install the dependency once with `python -m pip install -r requirements.txt`, then run only the exporter:

```bash
python export_table_annotations.py \
  --ticker MU --company Micron \
  --previous-year 2024 --current-year 2025 --item 7 \
  --user-agent "Your name your-email@example.com" \
  --output-dir data/table_output/item7_all_tables_annotations
```
or the command below if you wish to get result from specify table

```bash
python export_table_annotations.py \
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
