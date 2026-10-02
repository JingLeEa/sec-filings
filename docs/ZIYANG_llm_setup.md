# ZIYANG: Shared backend and LLM setup

**Maintainer:** ZIYANG — contact ZIYANG for setup questions.

Use this guide before [disclosure extraction](ZIYANG_disclosure_extraction.md) or
[disclosure alignment](ZIYANG_disclosure_alignment.md). Each stage document covers its
inputs, commands and final JSON contract. Use the
[stage template](backend_stage_template.md) to document another backend stage.

## 1. Install the project

Clone the project's GitHub repository and select the branch containing the
backend work. A clone contains committed, pushed files, not another person's
uncommitted workspace. Run commands from the repository root, where
`requirements.txt`, `pyproject.toml` and `.env.example` are located.

The project requires Python **3.10 or newer**. Install its declared dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

On Windows PowerShell, create the environment with `py -3 -m venv .venv` and
substitute `.venv\Scripts\python.exe` for `.venv/bin/python` in these guides.
Select this environment as your IDE interpreter. Rerun the install command after
pulling dependency changes. The requirements include LangGraph and SQLite
checkpoint support. No LangSmith account or extra LangGraph API key is needed.

## 2. Configure a local `.env`

You do **not** need `~/.config/soclaas/soclaas.env` when passing
`--env-file .env`. If `.env` does not exist, copy the template:

```bash
cp .env.example .env
chmod 600 .env
```

On PowerShell use `Copy-Item .env.example .env`; `chmod` is for macOS/Linux.
Edit an existing `.env` instead of overwriting it. Fill in these placeholders:

```dotenv
SEC_USER_AGENT="Your Name your.email@example.com"
SOCLAAS_BASE_URL="https://soclaas-api.comp.nus.edu.sg/v1"
SOCLAAS_API_KEY="YOUR_API_KEY"
SOCLAAS_MODEL="default"
```

Use the endpoint/model supplied for your account. `.env` is ignored by Git.
Keep actual keys out of example files, docs and fixtures. Each groupmate supplies
their own configuration; a shared key consumes its owner's quota.

[`config.py`](../src/sec_disclosure/llm/config.py) applies these rules:

- `--env-file .env` selects the local file; it is **not auto-loaded**.
- Without that option, the default is `~/.config/soclaas/soclaas.env`.
- Exported `SOCLAAS_*` variables override values in the selected file.
- The file is parsed directly, without executing it. Sourcing it or editing
  shell startup files is unnecessary.
- An explicitly selected missing file is an error. Without an explicit file,
  all three `SOCLAAS_*` settings may instead come from the environment.

If using the home-directory file, use directory permissions `700` and file
permissions `600`. After rotating a key, clear stale exported values or reload
your shell so they do not override the updated file.

The filing downloader does not load the project `.env`. Supply
`--user-agent "Your Name your.email@example.com"` explicitly, as shown in the
extraction guide, or export `SEC_USER_AGENT`.

## 3. Check configuration and connection

Local check; **no API request or token usage**:

```bash
.venv/bin/python scripts/test_llm.py --env-file .env --check-config
```

Expected output:

```text
Configuration valid (API key hidden; no request sent).
```

This checks settings, not authentication or quota. Optionally send one small
request; this **uses API tokens**:

```bash
.venv/bin/python scripts/test_llm.py --env-file .env
```

The test defaults to one request, a 60-second timeout and a 256-token completion
limit. `--prompt`, `--timeout` and `--max-tokens` customize it. Automatic retries
are disabled. Truncated/empty replies are errors; credentials and raw error
response bodies are not printed.

## 4. Choose a workflow

| Goal | Next document | API usage |
| --- | --- | --- |
| Prepare filings and extract disclosures | [Extraction inputs](ZIYANG_disclosure_extraction.md#4-inputs) and [run commands](ZIYANG_disclosure_extraction.md#5-run-and-rerun) | SEC preprocessing uses no LLM; fresh disclosure extraction does |
| Run 2023, 2024 and 2025 in parallel with fault tolerance | [Complete parallel command, retry flags and resume instructions](ZIYANG_disclosure_extraction.md#parallel-run-with-fault-tolerance) | Fresh work and retries use LLM tokens; successful cached work is reused |
| Align two completed extraction years | [Alignment inputs](ZIYANG_disclosure_alignment.md#4-inputs) and [run commands](ZIYANG_disclosure_alignment.md#5-run-and-rerun) | Exact matching/retrieval use no LLM; agent calls do |
| Develop a downstream JSON reader | [Extraction examples](ZIYANG_disclosure_extraction.md#7-example-data-and-downstream-usage) or [alignment examples](ZIYANG_disclosure_alignment.md#7-example-data-and-downstream-usage) | No API key or calls needed |
| Document another backend stage | [Stage template](backend_stage_template.md) | None |

`data/` contains local generated files and is ignored by Git. The fixture under
`tests/fixtures/alignments/amd/{2023-2024,2024-2025}/` contains exactly two files
per comparison from its newest completed LangGraph run: `alignments.json` and
`needs_review.json`.
The [alignment guide](ZIYANG_disclosure_alignment.md#7-example-data-and-downstream-usage)
records source paths, counts, hashes and copy commands. Raw filings, extraction
outputs, audits and runtime state stay in local `data/` directories. The fixture
supports downstream report reading; fresh runs and cached resume require the
separate inputs and compatible runtime state described in the stage guides.

## 5. Troubleshooting

| Problem | Action |
| --- | --- |
| Missing script after cloning | Check the branch and that required code has been committed and pushed. |
| Missing home configuration | Pass `--env-file .env`; the home file is optional. |
| `.env` not found | Run from the repo root or supply an absolute path. |
| Missing/wrong settings | Fill all three `SOCLAAS_*` values and check exported overrides, including empty ones. |
| Local check passes but API fails | Check provider availability, key/model access and quota. |
| Missing input JSON | Follow extraction section 4; Git does not include generated `data/`. |
| Manifest rejects changed inputs/settings/code | Follow the stage's fresh-run or migration instructions; do not edit hashes or delete ledgers to force a resume. |
| Request/token limit reached | Inspect `token_usage.json` and resume with an appropriate limit. |
| API attempt failed | Inspect the failure first. `--retry-failed` permits another paid attempt; see the stage's accounting rules. |
| Computer slept | Resume later from the same output directory. Local execution needs an awake computer. |

Run only one writer per output directory. Preserve request ledgers and, for
alignment, JSON jobs and the entire `graph/` directory together. Resume state is
not a separate recovery backup; normal commands do not create the temporary
archive used during a manual data cleanup.
