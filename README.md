# Simple SEC 10-K Extraction Pipeline

This project downloads a SEC 10-K HTML filing, removes HTML/tables/page noise, extracts Item 1, Item 1A, Item 7, and Item 8, then writes paragraph/disclosure chunks with IDs like `2024_P001`.

For filings like NVIDIA's inline XBRL HTML, displayed paragraphs are usually stored as styled `<div>` blocks rather than `<p>` tags. The extractor therefore chunks by meaningful HTML block and carries the latest short subheader, such as `Our Company` or `Data Center`, into each chunk's `item_title`.

## Usage

Use a SEC filing URL:

```bash
export SEC_USER_AGENT="Your Name your.email@example.com"
python3 sec_10k_extractor.py "https://www.sec.gov/Archives/edgar/data/.../.../nvda-20240128.htm"
```

Or use a local HTML file:

```bash
python3 sec_10k_extractor.py data/raw/nvda-20240128.htm
```

## Outputs

For filenames like `nvda-20240128.htm`, the pipeline writes to `output/<company>/<year>/`:

- `<year>_chunks.json`: structured chunks for LLM input.
- `<year>_chunks.txt`: readable chunk file for manual copy/paste.
- `<year>_item_1.txt`, `<year>_item_1a.txt`, `<year>_item_7.txt`, `<year>_item_8.txt`: cleaned full section text.

Downloaded SEC HTML files are saved in `data/raw/`.

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
- Company/year are inferred from the downloaded or local filing filename. For `nvda-20240128.htm`, company is `nvda` and year is `2024`.
- If a filing has unusual headings, lower `--max-chars` for smaller LLM chunks or inspect the item TXT files to confirm boundaries.
- `item` is the SEC item number. `item_default_title` is the standard SEC heading, while `item_title` is the most recent subheader found inside that item.

## Compare Two Years Manually

Run the script once per year, then give the LLM the two JSON or TXT chunk files:

```bash
python3 sec_10k_extractor.py data/raw/nvda-20230129.htm
python3 sec_10k_extractor.py data/raw/nvda-20240128.htm
```

Use `output/nvda/2023/2023_chunks.txt` and `output/nvda/2024/2024_chunks.txt` as the manual LLM inputs.
