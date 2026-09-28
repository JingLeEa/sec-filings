# LLM disclosure grouping and token usage

This stage reads an existing extraction for exactly one company and fiscal year.
It does not download filings or run other companies/years. The initial trial is
AMD 2024, Items 1, 1A, 7 and 8.

```bash
.venv/bin/python scripts/extract_disclosures.py --ticker amd --year 2024 --dry-run
.venv/bin/python scripts/extract_disclosures.py --ticker amd --year 2024
```

The first command validates input and shows batch sizes without an API request or
output writes. The second uses the protected SoCLaaS configuration described in
[LLM setup](llm_setup.md). It sends the extracted filing text to that endpoint.

Inputs are `data/raw/amd/2024/2024_chunks.json` and
`data/raw/amd/2024/2024_chunk_sentences.json`. Outputs go to
`data/disclosures/amd/2024/`, which is ignored by Git:

| Output | Contents |
| --- | --- |
| `disclosures.json` | Candidates passing deterministic source checks and the minimum of two source sentence units. |
| `review_candidates.json` | Single-unit disclosures, batch-boundary cases, possible missing tables, and candidates the model marked incomplete. |
| `disclosures.md` | Readable results grouped by Item, including review candidates, summaries, taxonomy, original evidence and IDs. |
| `excluded_sources.json` | Original text and reasons for every deterministic or model-proposed exclusion; model exclusions are explicitly unverified and need review. |
| `unassigned_sources.json` | Evidence the model omitted or whose proposal was rejected; retained with paragraph context for review. |
| `invalid_proposals.json` | Quarantined proposals with invented/repeated IDs, missing summaries, or invalid taxonomy. |
| `token_usage.json` | Actual reported input/output/total tokens, per-Item totals, coverage, pending/failed batches and returned model names. |
| `requests/` | Each API attempt and its raw response and usage, saved before JSON parsing/validation. |
| `manifest.json` | Input hashes, prompt hashes, model and batching configuration for safe resumption. |

Each disclosure includes a stable-within-this-run ID such as `amd_2024_7_D001`,
Item, section, concise generated summary, verbatim extracted content, one of the
existing nine content taxonomy categories, source paragraph IDs and sentence IDs.
IDs are assigned in source batch order within each Item; a fresh model run may
group evidence differently and produce different IDs.

The model returns summaries, category names and integer evidence references. The
program retrieves the actual text from the input files; it never relies on the
model to reproduce original text. Full paragraph selections and partial sentence
selections can coexist. Full original paragraphs are also retained as context for
partial selections, without adding unselected sentences to disclosure content.

Items cannot be mixed; proposals spanning sections go to review. Sentence IDs must exist, match their parent
metadata and occur in the parent text. Every source unit is accounted for once as
disclosure evidence, an exclusion, or explicitly unassigned evidence for review.
Repeated or invented evidence IDs quarantine the affected proposals. Missing IDs
are retained for review rather than silently dropped or automatically retried.

## Cost and resuming

The default batch target is 16,000 serialized input characters, keeping whole
sections together where possible and never splitting a paragraph. Very large
sections span batches; affected edge candidates are flagged for review. The
character limit is not a token estimate. Requests are sequential, use JSON mode,
and have no automatic SDK retries. Default output limit is 6,000 tokens per
request; the timeout is 180 seconds.

To try just one new batch:

```bash
.venv/bin/python scripts/extract_disclosures.py --ticker amd --year 2024 --max-requests 1
```

A request-limited partial run exits with code 2. Rerun without that option to
continue. Successful saved responses are reused without API calls. A completed
run can be rerun to regenerate the derived files from its cached responses.

Failed/truncated responses still count in `token_usage.json`. Errors or interrupted
requests without reported usage are marked unknown, never treated as free.
After inspecting a failure, `--retry-failed` explicitly permits another API attempt;
the ledger retains and counts both attempts. There is no automatic repair call.
Changed inputs, model, batch size, prompt or output token limit require a new
`--output-dir`, so incompatible caches cannot be silently mixed. Separate output
directories have separate usage ledgers; add their totals when comparing trials.

`prompt_tokens`, `completion_tokens` and `total_tokens` are taken from API usage,
not estimated from text length. Cached prompt tokens and reasoning tokens, if
provided, are subsets already included in those totals. Currency cost is left
unknown because no account-specific billing rates have been verified.

## Verification limits

`source_validated` means provenance and structural checks passed. It does not
certify that the generated summary, taxonomy, exclusions or grouping are correct.
These require semantic review. Every model-proposed exclusion is marked
`verification_status: needs_review`, with its original paragraph available for
auditing. The model can incorrectly exclude a substantive fact even when told to
retain it; exclusions are not certified as boilerplate. No extraction files change.

Source text is the cleaned extraction. HTML tables are absent, paragraph blocks
may have been merged, and some sentence records contain several grammatical
sentences. The two-unit minimum is therefore based on existing source records.
Meaningful single-unit facts are retained for review rather than padded with
unrelated evidence. This stage does not repair the existing extractor's boundaries.

Only the selected filing is used. Summaries describe what that historical filing
reported, without updating pending transactions or forecasts using later events.
